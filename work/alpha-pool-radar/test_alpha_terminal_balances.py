"""Read-only collector tests with isolated config and fake network transport."""
import importlib
import json
import threading
import time

import pytest


WALLET = '0x' + '1' * 40
OTHER = '0x' + '2' * 40
NOW = 1_800_000_000.0


@pytest.fixture
def module():
    return importlib.import_module('alpha_terminal_balances')


@pytest.fixture
def root(tmp_path):
    (tmp_path / 'okx-live-config.json').write_text(json.dumps({
        'bsc_rpc_url': 'https://bsc.invalid/',
        'robinhood_rpc_url': 'https://rh.invalid/',
        'api_key_dpapi_path': 'DO_NOT_READ',
    }), encoding='utf-8')
    return tmp_path


def transport(calls, clock=lambda: NOW):
    def fetch(url, payload, timeout):
        calls.append((url, payload, timeout))
        if payload:
            if payload['method'] == 'eth_chainId':
                assert payload['params'] == []
                value = hex(56 if url == 'https://bsc.invalid/' else 4663)
            elif payload['method'] == 'eth_getBalance':
                value = hex(2 * 10 ** 18)
            else:
                assert payload['method'] == 'eth_call'
                assert url == 'https://bsc.invalid/'
                assert payload['params'][0]['to'] == '0x55d398326f99059ff775485246999027b3197955'
                assert payload['params'][0]['data'] == '0x70a08231' + WALLET[2:].rjust(64, '0')
                value = '0x' + f'{7 * 10 ** 18:064x}'
            return {'jsonrpc': '2.0', 'id': 1, 'result': value}
        symbol = 'BNB' if 'BNB-USDT' in url else 'ETH'
        return {'code': '0', 'data': [{
            'instId': symbol + '-USDT', 'last': '500' if symbol == 'BNB' else '2000',
            'ts': str(int(clock() * 1000)),
        }]}
    return fetch


def eventually(collector, predicate, wallet=WALLET):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        rows = collector.snapshot(wallet)
        if predicate(rows):
            return rows
        time.sleep(.005)
    pytest.fail('collector did not publish expected fixture state')


def test_background_balances_exact_schema_and_read_only_calls(module, root):
    calls = []
    with module.Collector(root=root, fetch_json=transport(calls), clock=lambda: NOW) as collector:
        rows = eventually(collector, lambda rows: all(row['status'] == 'fresh' for row in rows))
        assert set(rows[0]) == {'chain', 'native_symbol', 'native_balance', 'native_price_usd',
                                'native_balance_usd', 'usdt_balance', 'updated_at', 'status'}
        assert rows[0]['native_balance'] == 2
        assert rows[0]['native_price_usd'] == 500
        assert rows[0]['native_balance_usd'] == 1000
        assert rows[0]['usdt_balance'] == 7
        assert rows[1]['chain'] == 'robinhood' and rows[1]['native_symbol'] == 'ETH'
        assert rows[1]['native_balance_usd'] == 4000
        assert rows[1]['usdt_balance'] is None
        assert len(calls) == 7
        assert all(timeout == 4 for _, _, timeout in calls)
        assert collector.interval == 20
        assert 'DO_NOT_READ' not in json.dumps(rows, allow_nan=False)
        rows[0]['native_balance'] = 999
        assert collector.snapshot(WALLET)[0]['native_balance'] == 2


def test_snapshot_never_waits_for_slow_network_and_no_duplicate_workers(module, root):
    entered, release = threading.Event(), threading.Event()
    calls = []
    real = transport(calls)

    def slow(*args):
        entered.set()
        release.wait(2)
        return real(*args)

    collector = module.Collector(root=root, fetch_json=slow, clock=lambda: NOW)
    try:
        collector.snapshot(WALLET)
        assert entered.wait(1)
        start = time.monotonic()
        for _ in range(100):
            rows = collector.snapshot(WALLET)
            assert rows[0]['native_balance'] is None
        assert time.monotonic() - start < .25
        assert len([t for t in threading.enumerate() if t.name == collector.thread_name]) == 1
    finally:
        release.set()
        collector.close(timeout=2)
    assert not collector.is_alive()


def test_periodic_refresh_failure_preserves_value_and_original_timestamp(module, root):
    failed = threading.Event()
    calls = []
    real = transport(calls)

    def fetch(*args):
        if failed.is_set():
            raise TimeoutError('DO_NOT_EXPORT')
        return real(*args)

    with module.Collector(root=root, interval=.04, fetch_json=fetch, clock=lambda: NOW) as collector:
        rows = eventually(collector, lambda rows: all(r['status'] == 'fresh' for r in rows))
        original_time = rows[0]['updated_at']
        failed.set()
        rows = eventually(collector, lambda rows: all(r['status'] == 'stale' for r in rows))
        assert rows[0]['native_balance'] == 2 and rows[0]['usdt_balance'] == 7
        assert rows[0]['updated_at'] == original_time
        assert 'DO_NOT_EXPORT' not in json.dumps(rows)


def test_missing_config_is_unavailable_without_network(module, tmp_path):
    calls = []
    with module.Collector(root=tmp_path, fetch_json=transport(calls)) as collector:
        rows = eventually(collector, lambda rows: all(r['status'] == 'unavailable' for r in rows))
        assert all(r['native_balance'] is None and r['updated_at'] is None for r in rows)
        assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_wallet_change_discards_old_inflight_result(module, root):
    entered, release = threading.Event(), threading.Event()

    def fetch(url, payload, timeout):
        entered.set()
        release.wait(1)
        if payload and payload['method'] == 'eth_chainId':
            return {'jsonrpc': '2.0', 'id': 1,
                    'result': hex(56 if url == 'https://bsc.invalid/' else 4663)}
        if payload and payload['method'] == 'eth_getBalance':
            return {'jsonrpc': '2.0', 'id': 1, 'result': hex(10 ** 18 if payload['params'][0] == WALLET else 3 * 10 ** 18)}
        if payload:
            return {'jsonrpc': '2.0', 'id': 1, 'result': '0x' + '0' * 64}
        return {'code': '0', 'data': [{'instId': 'BNB-USDT' if 'BNB' in url else 'ETH-USDT',
                                      'last': '1', 'ts': str(int(NOW * 1000))}]}

    with module.Collector(root=root, fetch_json=fetch, clock=lambda: NOW) as collector:
        collector.snapshot(WALLET)
        assert entered.wait(1)
        assert all(r['native_balance'] is None for r in collector.snapshot(OTHER))
        release.set()
        rows = eventually(collector, lambda rows: all(r['status'] == 'fresh' for r in rows), OTHER)
        assert all(r['native_balance'] == 3 for r in rows)


@pytest.mark.parametrize('bad', [None, '', 'secret', '0x' + '0' * 40, {}, WALLET + '?'])
def test_invalid_wallet_never_launches_network(module, root, bad):
    calls = []
    with module.Collector(root=root, fetch_json=transport(calls)) as collector:
        assert all(r['status'] == 'unavailable' for r in collector.snapshot(bad))
        assert calls == []
        assert not collector.is_alive()


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-1', '0'])
def test_invalid_ticker_price_does_not_create_fake_usd_balance(module, root, bad):
    real = transport([])

    def fetch(url, payload, timeout):
        response = real(url, payload, timeout)
        if not payload:
            response['data'][0]['last'] = bad
        return response

    with module.Collector(root=root, fetch_json=fetch, clock=lambda: NOW) as collector:
        rows = eventually(collector, lambda rows: all(r['native_balance'] == 2 for r in rows))
        assert all(r['native_price_usd'] is None and r['native_balance_usd'] is None for r in rows)
        assert all(r['status'] == 'stale' for r in rows)
        json.dumps(rows, allow_nan=False)


def test_age_marks_cached_rows_stale_without_new_io(module, root):
    now = [NOW]
    calls = []
    with module.Collector(root=root, fetch_json=transport(calls), clock=lambda: now[0]) as collector:
        eventually(collector, lambda rows: all(r['status'] == 'fresh' for r in rows))
        now[0] += 61
        assert all(r['status'] == 'stale' for r in collector.snapshot(WALLET))
        assert len(calls) == 7


def test_confirmed_zero_balances_are_not_treated_as_missing(module, root):
    real = transport([])

    def fetch(url, payload, timeout):
        result = real(url, payload, timeout)
        if payload and payload['method'] != 'eth_chainId':
            result['result'] = '0x' + '0' * (64 if payload['method'] == 'eth_call' else 1)
        return result

    with module.Collector(root=root, fetch_json=fetch, clock=lambda: NOW) as collector:
        rows = eventually(collector, lambda rows: all(r['status'] == 'fresh' for r in rows))
        assert rows[0]['usdt_balance'] == 0
        assert all(r['native_balance'] == r['native_balance_usd'] == 0 for r in rows)


@pytest.mark.parametrize('result', [None, '0x', '0x' + 'f' * 65, '-1', True])
def test_bad_rpc_response_leaves_balance_null(module, root, result):
    real = transport([])

    def fetch(url, payload, timeout):
        response = real(url, payload, timeout)
        if payload and payload['method'] != 'eth_chainId':
            response['result'] = result
        return response

    with module.Collector(root=root, fetch_json=fetch, clock=lambda: NOW) as collector:
        rows = eventually(collector, lambda rows: all(r['native_price_usd'] is not None for r in rows))
        assert all(r['native_balance'] is None and r['usdt_balance'] is None for r in rows)
        assert all(r['status'] == 'stale' for r in rows)


@pytest.mark.parametrize('url', ['file:///secret', 'https://key:password@example.com/',
                                'https://example.com/?api_key=secret', 'https://example.com/#secret'])
def test_only_public_rpc_config_urls_are_selected(module, root, url):
    (root / 'okx-live-config.json').write_text(json.dumps({
        'bsc_rpc_url': url, 'robinhood_rpc_url': url,
    }))
    assert module._rpc_urls(root) == {'bsc': None, 'robinhood': None}


def test_network_response_read_is_bounded(module, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            assert count == module.MAX_RESPONSE + 1
            return b'x' * count

    monkeypatch.setattr(module, 'urlopen', lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match='response_too_large'):
        module._fetch_json('https://fixture.invalid', None, 4)


def test_reads_config_only_without_writes_or_secret_files(module, root, monkeypatch):
    from pathlib import Path
    original = Path.open
    accessed = []

    def public_read(path, mode='r', *args, **kwargs):
        assert path == root / 'okx-live-config.json'
        assert mode == 'rb'
        accessed.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', public_read)
    with module.Collector(root=root, fetch_json=transport([]), clock=lambda: NOW) as collector:
        eventually(collector, lambda rows: all(r['status'] == 'fresh' for r in rows))
    assert accessed


def test_stale_ticker_timestamp_is_not_relabelled_as_current(module, root):
    real = transport([], clock=lambda: NOW - 300)
    with module.Collector(root=root, fetch_json=real, clock=lambda: NOW) as collector:
        rows = eventually(collector, lambda rows: all(r['native_balance'] == 2 for r in rows))
        assert all(r['native_price_usd'] is None and r['status'] == 'stale' for r in rows)


def test_refresh_recovers_without_restarting_collector(module, root):
    recovered = threading.Event()
    real = transport([])

    def fetch(*args):
        if not recovered.is_set():
            raise TimeoutError('unavailable')
        return real(*args)

    with module.Collector(root=root, interval=.04, fetch_json=fetch, clock=lambda: NOW) as collector:
        assert all(r['status'] == 'unavailable' for r in collector.snapshot(WALLET))
        recovered.set()
        rows = eventually(collector, lambda rows: all(r['status'] == 'fresh' for r in rows))
        assert rows[0]['native_balance'] == 2


@pytest.mark.parametrize('chain,result', [('bsc', '0x1237'), ('robinhood', '0x38'),
                                         ('bsc', None), ('bsc', True), ('bsc', '0x')])
def test_wrong_or_unknown_rpc_chain_never_valued(module, root, chain, result):
    calls = []
    real = transport([])

    def fetch(url, payload, timeout):
        calls.append(payload['method'] if payload else 'ticker')
        if payload and payload['method'] == 'eth_chainId':
            return {'jsonrpc': '2.0', 'id': 1, 'result': result}
        return real(url, payload, timeout)

    with module.Collector(root=root, fetch_json=fetch, clock=lambda: NOW) as collector:
        values, failed = collector._collect(chain, WALLET, 'https://bsc.invalid/')
        assert failed and values == {}
        assert calls == ['eth_chainId']


def test_chain_identity_failure_keeps_only_previous_verified_cache(module, root):
    switched = threading.Event()
    now = [NOW]
    real = transport([], clock=lambda: now[0])

    def fetch(url, payload, timeout):
        if switched.is_set():
            assert payload['method'] == 'eth_chainId'
            return {'jsonrpc': '2.0', 'id': 1, 'result': '0x1'}
        return real(url, payload, timeout)

    with module.Collector(root=root, interval=.04, fetch_json=fetch, clock=lambda: now[0]) as collector:
        before = eventually(collector, lambda rows: all(r['status'] == 'fresh' for r in rows))
        now[0] += 20
        switched.set()
        after = eventually(collector, lambda rows: all(r['status'] == 'stale' for r in rows))
        for old, new in zip(before, after):
            assert {**new, 'status': 'fresh'} == old


@pytest.mark.parametrize('response', [
    {}, {'id': True, 'result': '0x38'}, {'id': 2, 'result': '0x38'},
    {'id': 1, 'result': '0x38', 'error': {'code': -1}},
])
def test_invalid_chain_id_envelope_is_unavailable(module, root, response):
    calls = []

    def fetch(url, payload, timeout):
        calls.append(payload['method'])
        return response

    with module.Collector(root=root, fetch_json=fetch) as collector:
        assert collector._collect('bsc', WALLET, 'https://bsc.invalid/') == ({}, True)
        assert calls == ['eth_chainId']
