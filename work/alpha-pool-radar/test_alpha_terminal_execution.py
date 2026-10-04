import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from alpha_terminal_execution import ExecutionControl

WALLET = '0x' + '2' * 40


def now():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def control(tmp_path):
    root, repo = tmp_path / 'config', tmp_path / 'repo'
    root.mkdir()
    (repo / 'outputs').mkdir(parents=True)
    secrets = root / 'okx-live-secrets'
    secrets.mkdir()
    config = {'schema_version': 1, 'wallet_address': WALLET, 'state_root': str(root / 'gmgn-live')}
    for field in ('wallet_private_key_dpapi_path', 'api_key_dpapi_path', 'secret_key_dpapi_path', 'passphrase_dpapi_path'):
        config[field] = str(secrets / field)
    (root / 'okx-live-config.json').write_text(json.dumps(config))
    processes, launches = [], []
    c = ExecutionControl(config_root=root, repo_root=repo, process_reader=lambda: processes,
                         secure_path=lambda _: None,
                         launcher=lambda *args: launches.append(args) or 123)
    return c, processes, launches


def running(control, supported=True):
    c, processes, _ = control
    processes.extend([
        {'ProcessId': 10, 'ParentProcessId': 0, 'Name': 'powershell.exe',
         'CommandLine': 'powershell -File start_okx_dex_sdk_live.ps1 -Daemon -Chain bsc -Live'},
        {'ProcessId': 11, 'ParentProcessId': 10, 'Name': 'node.exe',
         'CommandLine': 'node okx_dex_live_daemon.mjs --live --amount-usd 1'}])
    receipt = {'supported': supported, 'pid': 11, 'chain': 'bsc', 'wallet': WALLET,
               'updated_at': now(), 'amount_usd': '1', 'amount_native_atomic': None,
               'exit_only': False, 'request_id': None, 'state': 'ready'}
    c._path('bsc', 'live-status').write_text(json.dumps({'chain': 'bsc', 'terminal_control': receipt}))
    return receipt


def payload(action='apply', **overrides):
    chain = overrides.get('chain', 'bsc')
    value = {'action': action, 'chain': chain, 'wallet_address': WALLET,
             'request_id': str(uuid4()), 'expected_pid': 21 if chain == 'robinhood' else 11}
    if action in {'apply', 'start'}:
        value['amount_usd'] = '2' if chain == 'robinhood' else '1'
    return value | overrides


def running_chain(control, chain):
    if chain == 'bsc':
        return running(control)
    c, processes, _ = control
    processes.extend([
        {'ProcessId': 20, 'ParentProcessId': 0, 'Name': 'powershell.exe',
         'CommandLine': 'powershell -File start_okx_dex_sdk_live.ps1 -Daemon -Chain robinhood -Live'},
        {'ProcessId': 21, 'ParentProcessId': 20, 'Name': 'node.exe',
         'CommandLine': 'node okx_dex_live_daemon.mjs --live --amount-usd 2'},
    ])
    receipt = {'supported': True, 'pid': 21, 'chain': 'robinhood', 'wallet': WALLET,
               'updated_at': now(), 'amount_usd': '2', 'amount_native_atomic': None,
               'exit_only': False, 'request_id': None, 'state': 'ready'}
    c._path('robinhood', 'live-status').write_text(json.dumps({
        'chain': 'robinhood', 'terminal_control': receipt}))
    return receipt


def test_start_is_explicit_deduplicated_and_fixed_launcher(control):
    c, _, launches = control
    request = payload('start')
    assert c.dispatch('strategy/command', request)[0] == 202
    assert len(launches) == 1 and launches[0][1:] == ('bsc', WALLET, '1')
    assert c.dispatch('strategy/command', request)[0] == 202
    assert len(launches) == 1
    assert c.dispatch('strategy/command', payload('start'))[1]['error'] == 'already_starting'


def test_start_rejects_current_daemon(control):
    c, _, launches = control
    running(control)
    assert c.dispatch('strategy/command', payload('start'))[1]['error'] == 'already_running'
    assert not launches


@pytest.mark.parametrize('action', ['apply', 'start'])
def test_failed_delivery_never_replays_as_submitted(control, monkeypatch, action):
    c, _, launches = control
    if action == 'apply':
        running(control)
    original = c._atomic
    target = c._path('bsc', 'terminal-start' if action == 'start' else 'terminal-control')
    def fail_delivery(path, content):
        if path == target:
            raise OSError('simulated disk failure')
        return original(path, content)
    monkeypatch.setattr(c, '_atomic', fail_delivery)
    request = payload(action)
    assert c.dispatch('strategy/command', request)[0] == 500
    monkeypatch.setattr(c, '_atomic', original)
    status, body = c.dispatch('strategy/command', request)
    assert status == 409
    assert body['error'] == ('start_outcome_unknown' if action == 'start' else 'command_delivery_unknown')
    assert not launches and not target.exists()


@pytest.mark.parametrize('action', ['apply', 'pause', 'resume', 'stop'])
def test_commands_wait_for_matching_receipt(control, action):
    c, _, launches = control
    receipt = running(control)
    request = payload(action)
    assert c.dispatch('strategy/command', request)[0] == 202
    assert c.runtime()['controls'][0]['state'] == 'pending'
    command = json.loads(c._path('bsc', 'terminal-control').read_text())
    assert command['expected_pid'] == 11 and command['wallet'] == WALLET
    receipt.update(request_id=request['request_id'], state='applied', action=action,
                   amount_usd='7', exit_only=True)
    c._path('bsc', 'live-status').write_text(json.dumps({'chain': 'bsc', 'terminal_control': receipt}))
    result = c.runtime()
    assert result['controls'][0]['state'] == 'applied'
    assert result['strategies'][0]['amount_usd'] == '7'
    assert result['strategies'][0]['exit_only'] is True
    assert not launches


def test_old_worker_not_killed_or_claimed_applied(control):
    c, _, launches = control
    running(control, False)
    assert c.dispatch('strategy/command', payload('pause'))[1]['error'] == 'worker_upgrade_required'
    assert c.runtime()['controls'][0]['state'] == 'legacy_worker'
    assert not launches and not c._path('bsc', 'terminal-control').exists()


@pytest.mark.parametrize('amount', ['0', '101', 'NaN', None, True, '1; rm', '1e2'])
def test_invalid_amount_cannot_reach_execution(control, amount):
    c, _, launches = control
    assert c.dispatch('strategy/command', payload('start', amount_usd=amount))[0] == 400
    assert not launches


def test_wallet_and_pid_guards(control):
    c, _, _ = control
    running(control)
    assert c.dispatch('strategy/command', payload(wallet_address='0x'+'3'*40))[1]['error'] == 'wallet_address_mismatch'
    assert c.dispatch('strategy/command', payload(expected_pid=12))[1]['error'] == 'worker_changed'


def test_unacknowledged_command_not_overwritten(control):
    c, _, _ = control
    running(control)
    first = payload('pause')
    c.dispatch('strategy/command', first)
    assert c.dispatch('strategy/command', payload('resume'))[1]['error'] == 'command_pending'
    assert c.dispatch('strategy/command', first)[0] == 202


def test_acknowledgement_lock_remains_active_for_125_seconds(control):
    c, _, _ = control
    receipt = running(control)
    prior = payload('pause')
    prior['created_at'] = (datetime.now(timezone.utc)-timedelta(seconds=122)).isoformat()
    c._path('bsc', 'terminal-control').write_text(json.dumps({
        'schema_version': 1, 'action': 'pause', 'chain': 'bsc', 'wallet': WALLET,
        'expected_pid': 11, 'request_id': prior['request_id'],
        'created_at': prior['created_at'],
    }))
    receipt['request_id'] = None
    c._path('bsc', 'live-status').write_text(json.dumps({'chain': 'bsc', 'terminal_control': receipt}))

    assert c.dispatch('strategy/command', payload('resume'))[1]['error'] == 'command_pending'


def test_stale_receipt_not_used_as_runtime_config(control):
    c, _, _ = control
    receipt = running(control)
    receipt.update(amount_usd='77', updated_at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat())
    c._path('bsc', 'live-status').write_text(json.dumps({'chain': 'bsc', 'terminal_control': receipt}))
    strategy = c.runtime()['strategies'][0]
    assert strategy['amount_usd'] is None
    assert strategy['configured_amount_usd'] == '1'
    assert strategy['effective_amount_usd'] is None
    assert strategy['executor_acknowledged_at'] is None
    assert c.runtime()['controls'][0]['state'] == 'heartbeat_stale'
    assert c.dispatch('strategy/command', payload())[1]['error'] == 'worker_heartbeat_stale'


def test_stale_bsc_health_does_not_disable_robinhood_controls(control):
    c, processes, _ = control
    receipt = running(control)
    receipt['updated_at'] = (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
    c._path('bsc', 'live-status').write_text(json.dumps({'chain': 'bsc', 'terminal_control': receipt}))
    processes.extend([
        {'ProcessId': 20, 'ParentProcessId': 0, 'Name': 'powershell.exe',
         'CommandLine': 'powershell -File start_okx_dex_sdk_live.ps1 -Daemon -Chain robinhood -Live'},
        {'ProcessId': 21, 'ParentProcessId': 20, 'Name': 'node.exe',
         'CommandLine': 'node okx_dex_live_daemon.mjs --live --amount-usd 2'},
    ])
    robinhood = {'supported': True, 'pid': 21, 'chain': 'robinhood', 'wallet': WALLET,
                 'updated_at': now(), 'amount_usd': '2', 'amount_native_atomic': None,
                 'exit_only': False, 'request_id': None, 'state': 'ready'}
    c._path('robinhood', 'live-status').write_text(json.dumps({'chain': 'robinhood', 'terminal_control': robinhood}))

    result = c.runtime()

    assert result['ok'] is True
    assert next(row for row in result['controls'] if row['chain'] == 'bsc')['state'] == 'heartbeat_stale'
    assert next(row for row in result['controls'] if row['chain'] == 'robinhood')['supported'] is True


def test_old_start_uuid_is_not_replayed_after_other_start(control):
    c, _, launches = control
    first = payload('start')
    c.dispatch('strategy/command', first)
    prior = json.loads(c._path('bsc', 'terminal-start').read_text())
    prior['created_at'] = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
    c._path('bsc', 'terminal-start').write_text(json.dumps(prior))
    assert c.dispatch('strategy/command', payload('start'))[0] == 202
    assert len(launches) == 2
    assert c.dispatch('strategy/command', first)[0] == 202
    assert len(launches) == 2


def test_stop_receipt_survives_process_exit_and_old_timestamp(control):
    c, processes, _ = control
    receipt = running(control)
    command = payload('stop')
    c.dispatch('strategy/command', command)
    processes.clear()
    receipt.update(request_id=command['request_id'], state='applied', action='stop',
                   updated_at=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat())
    c._path('bsc', 'live-status').write_text(json.dumps({
        'chain': 'bsc', 'status':'stopped', 'terminal_control':receipt}))
    assert c.runtime()['controls'][0]['state'] == 'applied'
    assert c.runtime()['controls'][0]['reason'] == 'stopped'


def test_duplicate_chain_process_is_an_isolated_conflict(control):
    c, processes, _ = control
    running(control)
    processes.append(dict(processes[1], ProcessId=12))
    result = c.runtime()
    assert result['ok'] is True
    assert next(row for row in result['controls'] if row['chain'] == 'bsc') == {
        'chain': 'bsc', 'supported': False, 'state': 'conflict',
        'reason': 'duplicate_workers', 'request_id': None,
    }
    assert c.runtime()['controls'][0]['state'] == 'conflict'


def test_start_confirmation_matches_launcher(control):
    c, _, _ = control
    command = payload('start')
    c.dispatch('strategy/command', command)
    start = json.loads(c._path('bsc', 'terminal-start').read_text())
    start['launcher_pid'] = 10
    c._path('bsc', 'terminal-start').write_text(json.dumps(start))
    running(control)
    state = c.runtime()['controls'][0]
    assert state['state'] == 'applied' and state['reason'] == 'started'
    assert state['request_id'] == command['request_id']


def test_fresh_remote_worker_is_visible_but_local_controls_are_disabled(control):
    c, _, launches = control
    receipt = {'supported': True, 'pid': 901, 'chain': 'robinhood', 'wallet': WALLET,
               'updated_at': now(), 'amount_usd': '2', 'amount_native_atomic': None,
               'exit_only': False, 'request_id': None, 'state': 'ready'}
    c._path('robinhood', 'live-status').write_text(json.dumps({'chain': 'robinhood', 'terminal_control': receipt}))
    (c.repo / 'outputs' / 'remote-robinhood-sync-status.json').write_text(json.dumps({
        'ok': True, 'chain': 'robinhood', 'updated_at': now()}))

    result = c.runtime()

    worker = next(row for row in result['strategies'] if row['chain'] == 'robinhood')
    remote = next(row for row in result['controls'] if row['chain'] == 'robinhood')
    assert worker['remote'] is True and worker['location'] == 'japan' and worker['pid'] == 901
    assert remote == {'chain': 'robinhood', 'supported': False, 'state': 'remote_ready',
                      'reason': 'remote_controls_pending', 'request_id': None}
    status, body = c.dispatch('strategy/command', payload('start', chain='robinhood'))
    assert status == 409 and body['error'] == 'already_running'
    assert not launches


def test_wrong_amount_receipt_is_not_accepted_as_effective_v2(control):
    c, _, _ = control
    receipt = running(control)
    receipt.update(amount_usd='7', updated_at=now())
    c._path('bsc', 'live-status').write_text(json.dumps({'chain': 'bsc', 'terminal_control': receipt}))

    strategy = c.runtime()['strategies'][0]

    assert strategy['configured_amount_usd'] == '1'
    assert strategy['effective_amount_usd'] is None
    assert strategy['effective_strategy'] is None
    assert strategy['executor_acknowledged_at'] is None


@pytest.mark.parametrize(('target', 'other', 'pid'), [
    ('bsc', 'robinhood', 11),
    ('robinhood', 'bsc', 21),
])
def test_chain_command_never_mutates_other_chain_control_paths(control, target, other, pid):
    c, processes, _ = control
    processes.extend([
        {'ProcessId': pid - 1, 'ParentProcessId': 0, 'Name': 'powershell.exe',
         'CommandLine': f'powershell -File start_okx_dex_sdk_live.ps1 -Daemon -Chain {target} -Live'},
        {'ProcessId': pid, 'ParentProcessId': pid - 1, 'Name': 'node.exe',
         'CommandLine': f'node okx_dex_live_daemon.mjs --live --amount-usd {1 if target == "bsc" else 2}'},
    ])
    receipt = {'supported': True, 'pid': pid, 'chain': target, 'wallet': WALLET,
               'updated_at': now(), 'amount_usd': '1' if target == 'bsc' else '2', 'amount_native_atomic': None,
               'exit_only': False, 'request_id': None, 'state': 'ready'}
    c._path(target, 'live-status').write_text(json.dumps({'chain': target, 'terminal_control': receipt}))
    sentinels = {}
    for kind in ('terminal-control', 'terminal-start', 'live-status'):
        path = c._path(other, kind)
        path.write_text(json.dumps({'chain': other, 'sentinel': kind}))
        sentinels[path] = path.read_bytes()
    request = payload('pause', chain=target, expected_pid=pid)

    assert c.dispatch('strategy/command', request)[0] == 202
    assert json.loads(c._path(target, 'terminal-control').read_text())['chain'] == target
    assert all(path.read_bytes() == before for path, before in sentinels.items())


@pytest.mark.parametrize(('chain', 'wrong_amount'), [('bsc', '5'), ('robinhood', '1')])
@pytest.mark.parametrize('action', ['start', 'apply'])
def test_chain_v2_rejects_conflicting_order_size(control, chain, wrong_amount, action):
    c, _, launches = control
    if action == 'apply':
        running_chain(control, chain)

    status, body = c.dispatch('strategy/command', payload(
        action, chain=chain, amount_usd=wrong_amount))

    assert status == 409
    assert body['error'] == 'chain_v2_order_size_conflict'
    assert not launches


@pytest.mark.parametrize(('chain', 'stage', 'route'), [
    ('bsc', 'aggregate_early_bird', 'bsc_aggregate_early_bird'),
    ('robinhood', 'aggregate_early_bird', 'robinhood_aggregate_early_bird'),
])
def test_effective_strategy_requires_explicit_matching_daemon_ack(control, chain, stage, route):
    c, _, _ = control
    receipt = running_chain(control, chain)
    path = c._path(chain, 'live-status')

    runtime = next(row for row in c.runtime()['strategies'] if row['chain'] == chain)
    assert runtime['effective_strategy'] is None

    receipt.update(strategy_version='chain_v2', signal_stage=stage, entry_route=route)
    path.write_text(json.dumps({'chain': chain, 'terminal_control': receipt}))
    effective = next(row for row in c.runtime()['strategies']
                     if row['chain'] == chain)['effective_strategy']

    assert effective == {
        'strategy_version': 'chain_v2', 'signal_stage': stage,
        'entry_route': route, 'acknowledged_at': receipt['updated_at'],
    }


@pytest.mark.parametrize('changed', [
    {'strategy_version': 'chain_v1'},
    {'signal_stage': 'aggregate_discovery'},
    {'entry_route': 'robinhood_aggregate_discovery'},
    {'amount_usd': '7'},
    {'wallet': '0x' + '3' * 40},
    {'pid': 12},
])
def test_effective_strategy_rejects_mismatched_receipt_identity_or_policy(control, changed):
    c, _, _ = control
    receipt = running(control)
    receipt.update({
        'strategy_version': 'chain_v2', 'signal_stage': 'aggregate_early_bird',
        'entry_route': 'bsc_aggregate_early_bird', **changed,
    })
    c._path('bsc', 'live-status').write_text(json.dumps({
        'chain': 'bsc', 'terminal_control': receipt}))

    strategy = c.runtime()['strategies'][0]

    assert strategy['effective_strategy'] is None


def test_chain_command_lock_does_not_block_other_chain(control):
    c, _, _ = control
    running_chain(control, 'bsc')
    running_chain(control, 'robinhood')
    lock = c.root / '.terminal-control-bsc.lock'
    lock.write_text('held')
    try:
        assert c.dispatch('strategy/command', payload('pause', chain='bsc')) == (
            409, {'error': 'control_busy'})
        assert c.dispatch('strategy/command', payload('pause', chain='robinhood'))[0] == 202
    finally:
        lock.unlink(missing_ok=True)


def test_wrong_internal_status_chain_is_not_used(control):
    c, _, _ = control
    receipt = running(control)
    receipt.update(strategy_version='chain_v2', signal_stage='aggregate_early_bird',
                   entry_route='bsc_aggregate_early_bird')
    c._path('bsc', 'live-status').write_text(json.dumps({
        'chain': 'robinhood', 'terminal_control': receipt}))

    result = c.runtime()

    assert result['strategies'][0]['effective_strategy'] is None
    assert result['controls'][0]['state'] == 'legacy_worker'


def test_status_chain_identity_can_come_from_terminal_receipt(control):
    c, _, _ = control
    receipt = running(control)
    receipt.update(strategy_version='chain_v2', signal_stage='aggregate_early_bird',
                   entry_route='bsc_aggregate_early_bird')
    c._path('bsc', 'live-status').write_text(json.dumps({'terminal_control': receipt}))

    result = c.runtime()

    assert result['controls'][0]['supported'] is True
    assert result['strategies'][0]['effective_strategy']['strategy_version'] == 'chain_v2'


def test_wrong_internal_command_chain_does_not_create_pending_lock(control):
    c, _, _ = control
    running(control)
    c._path('bsc', 'terminal-control').write_text(json.dumps({
        'chain': 'robinhood', 'wallet': WALLET, 'expected_pid': 11,
        'request_id': str(uuid4()), 'created_at': now(), 'action': 'pause',
    }))

    assert c.dispatch('strategy/command', payload('resume'))[0] == 202
    assert json.loads(c._path('bsc', 'terminal-control').read_text())['chain'] == 'bsc'


def test_wrong_internal_start_chain_does_not_block_start(control):
    c, _, launches = control
    c._path('bsc', 'terminal-start').write_text(json.dumps({
        'chain': 'robinhood', 'wallet': WALLET, 'request_id': str(uuid4()),
        'created_at': now(), 'action': 'start',
    }))

    assert c.dispatch('strategy/command', payload('start'))[0] == 202
    assert len(launches) == 1
