import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from alpha_terminal_server import SESSION_PATH, TerminalService, make_server, parse_payload


def test_default_v2_session_path_is_workspace_outputs():
    repo = Path(__file__).resolve().parents[2]
    assert SESSION_PATH == repo / 'outputs' / 'terminal-v2-session.json'


@pytest.mark.parametrize('body', [b'[]', b'{"a":1,"a":2}', b'{"a":NaN}', b'null'])
def test_reject_invalid_json(body):
    with pytest.raises(ValueError):
        parse_payload(body)


@pytest.fixture
def server(tmp_path):
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'index.html').write_text('<h1>Terminal</h1>')
    calls = []
    service = SimpleNamespace(
        controller=SimpleNamespace(dispatch=lambda action, payload: (
            calls.append((action, payload)) or 202, {'ok': True, 'staged': True})),
        runtime=lambda: {'runtime': [], 'verified': False},
        snapshot=lambda root: {'wallet': None, 'orders': []})
    srv = make_server(0, root=tmp_path, service=service, dist=dist)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, calls
    srv.shutdown()
    srv.server_close()
    thread.join()


def request(server, path, method='GET', body=None, headers=None):
    srv, _ = server
    connection = http.client.HTTPConnection('127.0.0.1', srv.server_port, timeout=3)
    connection.request(method, path, body, headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_serves_build_and_snapshot(server):
    status, headers, body = request(server, '/?tab=wallets')
    assert status == 200 and b'Terminal' in body
    assert "script-src 'self'" in headers['Content-Security-Policy']
    assert headers['Cache-Control'] == 'no-store'
    status, _, body = request(server, '/api/terminal/snapshot')
    assert status == 200 and json.loads(body)['orders'] == []


@pytest.mark.parametrize('path', ['/../alpha_terminal_server.py', '/%2e%2e/secret', '/missing', '/.env'])
def test_static_allowlist_path(server, path):
    assert request(server, path)[0] == 404


def test_cross_origin_read_blocked(server):
    assert request(server, '/api/terminal/session', headers={'Origin': 'https://evil.example'})[0] == 403


def test_post_requires_origin_csrf_and_does_not_start_trades(server):
    srv, calls = server
    body = json.dumps({'chain': 'bsc', 'amount_usd': 5})
    headers = {'Content-Type': 'application/json'}
    assert request(server, '/api/terminal/strategy/config', 'POST', body, headers)[0] == 403
    headers.update({'Origin': f'http://127.0.0.1:{srv.server_port}', 'X-CSRF-Token': srv.control.csrf_token})
    assert request(server, '/api/terminal/strategy/config', 'POST', body, headers)[0] == 202
    assert calls == [('strategy/config', {'chain': 'bsc', 'amount_usd': 5})]
    assert request(server, '/api/terminal/strategy/start', 'POST', body, headers)[0] == 404
    assert request(server, '/roundtrip/start', 'POST', body, headers)[0] == 404


def test_runtime_failure_remains_unknown():
    service = TerminalService(controller=SimpleNamespace(runtime=lambda: {'ok': False, 'strategies': []}))
    assert service.runtime()['verified'] is False


def test_unassigned_worker_does_not_mean_stopped():
    service = TerminalService(controller=SimpleNamespace(runtime=lambda: {
        'ok': True, 'strategies': [{'chain': None, 'running': True}]}))
    assert service.runtime()['verified'] is False
    assert service.runtime()['unassigned_workers'] == 1


def test_runtime_keeps_configured_amount_distinct_from_executor_ack(tmp_path):
    requests = tmp_path / 'terminal-strategy-requests.json'
    requests.write_text(json.dumps({'strategies': {
        'bsc': {'amount_usd': '9', 'requested_at': '2026-09-09T12:00:00+00:00'},
    }}))
    controller = SimpleNamespace(
        requests_path=requests,
        runtime=lambda: {'ok': True, 'verified_at': '2026-09-09T12:00:02+00:00',
                         'controls': [], 'strategies': [{
                             'chain': 'bsc', 'amount_usd': '1',
                             'configured_amount_usd': '5', 'effective_amount_usd': '1',
                             'executor_acknowledged_at': '2026-09-09T12:00:01+00:00',
                         }]},
    )

    row = TerminalService(controller=controller).runtime()['runtime'][0]

    assert row['configured_amount_usd'] == '9'
    assert row['effective_amount_usd'] == '1'
    assert row['executor_acknowledged_at'] == '2026-09-09T12:00:01+00:00'


def test_snapshot_merges_only_validated_runtime_effective_strategy(monkeypatch, tmp_path):
    configured = {'strategy_version': 'chain_v2', 'signal_stage': 'aggregate_early_bird'}
    effective = {**configured, 'entry_route': 'bsc_aggregate_early_bird',
                 'acknowledged_at': '2026-09-09T12:00:01+00:00'}
    monkeypatch.setattr('alpha_terminal_server.terminal_snapshot', lambda root: {
        'wallet': None, 'positions': [], 'orders': [], 'candidates': [], 'events': [],
        'balances': [], 'chains': [
            {'chain': 'bsc', 'configured_strategy': configured, 'effective_strategy': None},
            {'chain': 'robinhood', 'configured_strategy': {}, 'effective_strategy': None},
        ],
    })
    controller = SimpleNamespace(requests_path=None, runtime=lambda: {
        'ok': True, 'verified_at': '2026-09-09T12:00:02+00:00', 'controls': [],
        'strategies': [{'chain': 'bsc', 'effective_strategy': effective}],
    })

    result = TerminalService(controller=controller).snapshot(tmp_path)

    assert result['chains'][0]['configured_strategy'] == configured
    assert 'strategy' not in result['chains'][0]
    assert result['chains'][0]['effective_strategy'] == effective
    assert result['chains'][1]['effective_strategy'] is None


def test_v2_session_hides_old_orders_and_restarts_realized_pnl(monkeypatch, tmp_path):
    session_path = tmp_path / 'terminal-v2-session.json'
    session_path.write_text(json.dumps({
        'schema_version': 1,
        'strategy_version': 'chain_v2',
        'started_at': {
            'bsc': '2026-09-09T12:00:00+00:00',
            'robinhood': '2026-09-09T12:05:00+00:00',
        },
    }), encoding='utf-8')
    monkeypatch.setattr('alpha_terminal_server.terminal_snapshot', lambda root: {
        'wallet': None, 'positions': [], 'candidates': [], 'balances': [],
        'chains': [
            {'chain': 'bsc', 'ledger_identity_status': 'verified',
             'realized_pnl_native': -9, 'realized_pnl_native_atomic': '-9000000000000000000'},
            {'chain': 'robinhood', 'ledger_identity_status': 'verified',
             'realized_pnl_native': -4, 'realized_pnl_native_atomic': '-4000000000000000000'},
        ],
        'orders': [
            {'chain': 'bsc', 'side': 'sell', 'status': 'filled',
             'time': '2026-09-09T11:59:59+00:00', 'pnl_native_atomic': '-50'},
            {'chain': 'bsc', 'side': 'sell', 'status': 'filled',
             'time': '2026-09-09T12:00:01+00:00', 'pnl_native_atomic': '10'},
            {'chain': 'robinhood', 'side': 'buy', 'status': 'filled',
             'time': '2026-09-09T12:04:59+00:00'},
        ],
        'events': [
            {'type': 'order', 'chain': 'bsc', 'time': '2026-09-09T11:59:59+00:00'},
            {'type': 'order', 'chain': 'bsc', 'time': '2026-09-09T12:00:01+00:00'},
            {'type': 'status', 'chain': 'bsc', 'time': '2026-09-09T12:00:02+00:00'},
        ],
    })
    controller = SimpleNamespace(requests_path=None, runtime=lambda: {
        'ok': True, 'verified_at': '2026-09-09T12:06:00+00:00',
        'controls': [], 'strategies': [],
    })

    result = TerminalService(controller=controller, session_path=session_path).snapshot(tmp_path)

    assert len(result['orders']) == 1
    assert result['orders'][0]['pnl_native_atomic'] == '10'
    assert [row['type'] for row in result['events']] == ['order', 'status']
    chains = {row['chain']: row for row in result['chains']}
    assert chains['bsc']['realized_pnl_native_atomic'] == '10'
    assert chains['bsc']['realized_pnl_native'] == 1e-17
    assert chains['bsc']['realized_pnl_status'] == 'verified'
    assert chains['robinhood']['realized_pnl_native_atomic'] == '0'
    assert chains['robinhood']['realized_pnl_native'] == 0
    assert result['session']['strategy_version'] == 'chain_v2'


def test_custom_root_isolates_controller(tmp_path):
    srv = make_server(0, root=tmp_path)
    try:
        assert srv.terminal.controller.root == tmp_path
    finally:
        srv.server_close()


def test_post_rejects_duplicates(server):
    srv, calls = server
    headers = {'Origin': f'http://127.0.0.1:{srv.server_port}', 'X-CSRF-Token': srv.control.csrf_token,
               'Content-Type': 'application/json'}
    assert request(server, '/api/terminal/strategy/config', 'POST', '{"chain":"bsc","chain":"robinhood"}', headers)[0] == 400
    assert not calls


def test_operator_command_uses_authenticated_dispatch_only(server):
    srv, calls = server
    path = '/api/terminal/strategy/command'
    data = json.dumps({'action': 'pause', 'chain': 'bsc'})
    assert request(server, path, 'POST', data, {'Content-Type': 'application/json'})[0] == 403
    assert not calls
    headers = {'Origin': f'http://127.0.0.1:{srv.server_port}', 'X-CSRF-Token': srv.control.csrf_token,
               'Content-Type': 'application/json'}
    assert request(server, path, 'POST', data, headers)[0] == 202
    assert calls == [('strategy/command', {'action': 'pause', 'chain': 'bsc'})]
