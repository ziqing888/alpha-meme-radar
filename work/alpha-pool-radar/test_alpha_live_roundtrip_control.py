"""No live launch, credentials, network requests or production state writes."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import alpha_live_roundtrip_control as mod

WALLET = '0x' + '12' * 20
RECIPIENT = '0x' + '34' * 20


@pytest.fixture
def control(tmp_path, monkeypatch):
    root = tmp_path / 'config'
    root.mkdir()
    (root / 'okx-live-config.json').write_text(json.dumps({
        'schema_version': 1, 'wallet_address': WALLET}))
    runner = tmp_path / 'runner'
    runner.mkdir()
    (runner / 'start_okx_live.ps1').write_text('param([switch]$RoundTrip)')
    (runner / 'alpha_okx_roundtrip_live.py').write_text('# fixture')
    powershell = tmp_path / 'powershell.exe'
    powershell.touch()
    monkeypatch.setattr(mod, 'POWERSHELL', powershell)
    child = Mock()
    child.poll.return_value = None
    popen = Mock(return_value=child)
    monkeypatch.setattr(mod.subprocess, 'Popen', popen)
    return mod.RoundtripControl(root, runner), root, popen, child


def start(controller, **changes):
    args = dict(payload={'confirmed': True, 'fee_enabled': False},
                host='127.0.0.1:8771', origin='http://127.0.0.1:8771',
                client='127.0.0.1', port=8771, csrf=controller.csrf_token,
                fetch_site='same-origin')
    args.update(changes)
    return controller.start(**args)


def test_launch_is_fixed_hidden_and_has_no_default_fee_consent(control):
    c, root, popen, _ = control
    code, result = start(c)
    assert code == 202 and result['roundtrip']['running'] is True
    args, kwargs = popen.call_args
    command = args[0]
    assert command[-5:] == ['-Live', '-RoundTrip', '-EnableDagRoutes', '-ExpectedWallet', WALLET]
    assert command[command.index('-File') + 1] == str(c.runner / 'start_okx_live.ps1')
    assert '-DagTrimRecipient' not in command
    assert kwargs['shell'] is False and kwargs['creationflags'] == mod.CREATE_NO_WINDOW
    assert kwargs['stdin'] == mod.subprocess.DEVNULL
    assert kwargs['stderr'] == mod.subprocess.STDOUT
    assert mod.Path(kwargs['stdout'].name).name == 'roundtrip-launch.log'
    marker = root / 'gmgn-live' / WALLET / 'roundtrip-ui-start.json'
    assert marker.is_file()
    assert not marker.with_name('roundtrip-status.json').exists()


@pytest.mark.parametrize('changes', [
    {'origin': None}, {'origin': 'null'}, {'origin': 'http://localhost:8771'},
    {'host': 'evil.example:8771'}, {'client': '192.168.1.1'},
    {'csrf': ''}, {'csrf': 'bad'}, {'fetch_site': 'cross-site'},
])
def test_origin_and_csrf_fail_before_any_claim(control, changes):
    c, root, popen, _ = control
    assert start(c, **changes)[0] == 403
    popen.assert_not_called()
    assert not (root / 'gmgn-live').exists()


@pytest.mark.parametrize('payload', [
    {}, {'confirmed': False, 'fee_enabled': False}, {'confirmed': 1, 'fee_enabled': False},
    {'confirmed': True, 'fee_enabled': 'true'},
    {'confirmed': True, 'fee_enabled': False, 'amount': '100'},
    {'confirmed': True, 'fee_enabled': False, 'wallet': WALLET},
    {'confirmed': True, 'fee_enabled': False, 'command': 'whoami'},
    {'confirmed': True, 'fee_enabled': False, 'trim_recipient': RECIPIENT},
])
def test_payload_cannot_change_trade_or_implicitly_enable_fees(control, payload):
    c, _, popen, _ = control
    assert start(c, payload=payload)[0] == 400
    popen.assert_not_called()


@pytest.mark.parametrize('cap', [0, 1, 100])
def test_explicit_fee_profile(control, cap):
    c, _, popen, _ = control
    assert start(c, payload={'confirmed': True, 'fee_enabled': True,
                            'trim_recipient': RECIPIENT, 'max_trim_per_mille': cap})[0] == 202
    command = popen.call_args.args[0]
    assert command[command.index('-ExpectedWallet') + 1] == WALLET
    if cap:
        assert command[-4:] == ['-DagTrimRecipient', RECIPIENT, '-DagMaxTrimPerMille', str(cap)]
    else:
        assert '-DagTrimRecipient' not in command


@pytest.mark.parametrize('cap,recipient', [
    (-1, RECIPIENT), (101, RECIPIENT), (True, RECIPIENT), ('10', RECIPIENT),
    (1.5, RECIPIENT), (1, '0x' + '00' * 20), (1, RECIPIENT + ';whoami'), (1, ''),
])
def test_invalid_fee_profile_rejected(control, cap, recipient):
    c, _, popen, _ = control
    assert start(c, payload={'confirmed': True, 'fee_enabled': True,
                            'trim_recipient': recipient, 'max_trim_per_mille': cap})[0] == 400
    popen.assert_not_called()


def test_concurrent_clicks_and_new_controller_cannot_retry(control):
    c, root, popen, _ = control
    other = mod.RoundtripControl(root, c.runner)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: start(c if index % 2 else other)[0], range(8)))
    assert results.count(202) == 1 and results.count(409) == 7
    assert popen.call_count == 1
    assert start(mod.RoundtripControl(root, c.runner))[0] == 409


@pytest.mark.parametrize('report', ['{}', 'broken', '{"state":"failed"}'])
def test_any_existing_report_blocks_even_corrupt_or_failed(control, report):
    c, root, popen, _ = control
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    (account / 'roundtrip-status.json').write_text(report)
    assert c.status()['can_start'] is False
    assert start(c)[0] == 409
    popen.assert_not_called()


def test_missing_runner_or_config_is_not_ready(control):
    c, root, popen, _ = control
    (c.runner / 'alpha_okx_roundtrip_live.py').unlink()
    assert c.status()['reason'] == 'runner_unavailable'
    assert start(c)[0] == 409
    (root / 'okx-live-config.json').unlink()
    assert c.status()['state'] == 'not_configured'
    popen.assert_not_called()


def test_launch_failure_consumes_one_shot_without_reflecting_error(control):
    c, root, popen, _ = control
    popen.side_effect = OSError('SECRET from provider')
    code, result = start(c)
    assert code == 503 and result['roundtrip']['reason'] == 'launch_failed'
    assert 'SECRET' not in json.dumps(result)
    assert start(c)[0] == 409
    assert start(mod.RoundtripControl(root, c.runner))[0] == 409


def test_child_exit_before_report_has_fallback_and_never_restarts(control):
    c, _, popen, child = control
    start(c)
    child.poll.return_value = 1
    assert c.status()['reason'] == 'child_exit_before_report'
    assert c.status()['state'] == 'failed'
    assert start(c)[0] == 409 and popen.call_count == 1


def test_status_is_whitelisted_and_report_is_authoritative(control):
    c, root, _, child = control
    start(c)
    account = root / 'gmgn-live' / WALLET
    (account / 'roundtrip-status.json').write_text(json.dumps({
        'wallet': WALLET, 'state': 'completed', 'reason': 'SECRET', 'run_id': 'SECRET',
        'updated_at': '2026-09-08T12:00:00+00:00', 'buy_tx': '0x' + 'ab' * 32,
        'approval_txs': ['SECRET', '0x' + 'cd' * 32], 'bought_atomic': '1100',
        'remaining_atomic': '0', 'gas_native': '0.00001', 'pnl_native': '-0.00001',
        'raw': {'secret_key': 'SECRET'}}))
    child.poll.return_value = 0
    result = c.status()
    assert result['state'] == 'completed' and result['can_start'] is False
    assert len(result['approval_txs']) == 1
    assert result['pnl_native'] == '-0.00001'
    assert 'SECRET' not in json.dumps(result)


def test_status_reads_never_create_account_or_start_child(control):
    c, root, popen, _ = control
    for _ in range(3):
        assert c.status()['can_start'] is True
    assert not (root / 'gmgn-live').exists()
    popen.assert_not_called()


def test_executor_claim_without_report_also_blocks(control):
    c, root, popen, _ = control
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    (account / 'roundtrip-attempt.json').write_text('broken')
    assert c.status()['reason'] == 'attempt_recorded_no_report'
    assert c.status()['attempted'] is True
    assert start(c)[0] == 409
    popen.assert_not_called()


def test_existing_report_retained_when_child_dies(control):
    c, root, _, child = control
    start(c)
    report = root / 'gmgn-live' / WALLET / 'roundtrip-status.json'
    text = json.dumps({'wallet': WALLET, 'state': 'buy_pending', 'updated_at': '2020-01-01T00:00:00Z'})
    report.write_text(text)
    child.poll.return_value = 1
    status = c.status()
    assert status['state'] == 'buy_pending'
    assert status['process_state'] == 'exited'
    assert status['report_fresh'] is False
    assert status['can_start'] is False
    assert report.read_text() == text


@pytest.mark.parametrize('state', ['checking', 'simulating', 'buy_submitting', 'sell_submitting'])
def test_executor_step_vocabulary(control, state):
    c, root, _, _ = control
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    (account / 'roundtrip-status.json').write_text(json.dumps({'wallet': WALLET, 'state': state}))
    assert c.status()['state'] == state


def history(root, *, recipient=RECIPIENT, rate=100):
    (root / 'okx-roundtrip-preflight.json').write_text(json.dumps({
        'status': 'historical_simulated_round_trip', 'created_at': '2026-09-08T19:44:55+08:00',
        'secret': 'SECRET', 'inspections': [
            {'trim': {'recipient': recipient, 'rate_per_1000': rate}},
            {'trim': {'recipient': recipient, 'rate_per_1000': rate}}]}))


def test_historical_fee_projection_does_not_authorize_fees(control):
    c, root, popen, _ = control
    history(root)
    profile = c.status()['historical_fee']
    assert profile['recipient'] == RECIPIENT and profile['max_trim_per_mille'] == 100
    assert profile['created_at'] == '2026-09-08T19:44:55+08:00'
    assert 'SECRET' not in json.dumps(c.status())
    assert start(c)[0] == 202
    assert '-DagTrimRecipient' not in popen.call_args.args[0]


@pytest.mark.parametrize('recipient,rate', [('SECRET', 100), (RECIPIENT, 101), (RECIPIENT, True)])
def test_invalid_historical_fee_never_prefills(control, recipient, rate):
    c, root, _, _ = control
    history(root, recipient=recipient, rate=rate)
    assert c.status()['historical_fee'] is None


def test_configuration_change_and_other_worker_disable_start(control):
    c, root, popen, _ = control
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    from datetime import datetime, timezone
    (account / 'status.json').write_text(json.dumps({'enabled': True, 'updated_at': datetime.now(timezone.utc).isoformat()}))
    assert c.status()['reason'] == 'worker_running'
    assert start(c)[0] == 409
    (root / 'okx-live-config.json').write_text(json.dumps({'schema_version': 1, 'wallet_address': RECIPIENT}))
    assert c.status()['reason'] == 'configuration_changed'
    assert start(c)[0] == 409
    popen.assert_not_called()


def test_small_native_amounts_are_not_lost():
    assert mod.amount_text('1E-8') == '0.00000001'
    assert mod.amount_text('-1E-8', signed=True) == '-0.00000001'
    assert mod.amount_text('NaN') is None
    assert mod.amount_text('1E999') is None


@pytest.mark.parametrize('mode', ['stablecoin', 'strategy_meme'])
def test_explicit_mode_only_adds_bounded_strategy_switch(control, mode):
    c, root, popen, _ = control
    code, response = start(c, payload={'confirmed': True, 'fee_enabled': False, 'test_mode': mode})
    assert code == 202
    command = popen.call_args.args[0]
    assert command.count('-StrategyMeme') == (1 if mode == 'strategy_meme' else 0)
    assert '-RoundTrip' in command and '-Live' in command
    assert command[command.index('-ExpectedWallet') + 1] == WALLET
    assert '-DagTrimRecipient' not in command
    assert response['roundtrip']['test_mode'] == mode
    marker = root / 'gmgn-live' / WALLET / 'roundtrip-ui-start.json'
    assert json.loads(marker.read_text())['test_mode'] == mode
    other = 'stablecoin' if mode == 'strategy_meme' else 'strategy_meme'
    assert start(c, payload={'confirmed': True, 'fee_enabled': False, 'test_mode': other})[0] == 409
    assert popen.call_count == 1
    assert mod.RoundtripControl(root, c.runner).status()['test_mode'] == mode


def test_legacy_mode_defaults_stablecoin(control):
    c, _, popen, _ = control
    assert c.status()['test_mode'] == 'stablecoin'
    assert start(c)[1]['roundtrip']['test_mode'] == 'stablecoin'
    assert '-StrategyMeme' not in popen.call_args.args[0]


@pytest.mark.parametrize('mode', [None, True, 1, [], {}, 'strategy', 'strategy_meme -Live', 'STABLECOIN'])
def test_invalid_modes_rejected_before_claim(control, mode):
    c, root, popen, _ = control
    assert start(c, payload={'confirmed': True, 'fee_enabled': False, 'test_mode': mode})[0] == 400
    assert not (root / 'gmgn-live').exists()
    popen.assert_not_called()


@pytest.mark.parametrize('extra', [{'token': RECIPIENT}, {'amount': '1'}, {'execution_arm': 'first_discovery'}])
def test_strategy_mode_cannot_accept_candidate_inputs(control, extra):
    c, _, popen, _ = control
    assert start(c, payload={'confirmed': True, 'fee_enabled': False, 'test_mode': 'strategy_meme', **extra})[0] == 400
    popen.assert_not_called()


@pytest.mark.parametrize('name', ['roundtrip-status.json', 'roundtrip-attempt.json', 'roundtrip-ui-start.json'])
def test_previous_attempt_of_any_mode_blocks_meme_start(control, name):
    c, root, popen, _ = control
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    path = account / name
    path.write_text('{"test_mode":"stablecoin"}')
    assert start(c, payload={'confirmed': True, 'fee_enabled': False, 'test_mode': 'strategy_meme'})[0] == 409
    assert path.read_text() == '{"test_mode":"stablecoin"}'
    popen.assert_not_called()


def test_strategy_mode_preserves_explicit_fee_policy(control):
    c, _, popen, _ = control
    assert start(c, payload={'confirmed': True, 'fee_enabled': True, 'test_mode': 'strategy_meme',
                            'trim_recipient': RECIPIENT, 'max_trim_per_mille': 15})[0] == 202
    command = popen.call_args.args[0]
    assert '-StrategyMeme' in command
    assert command[-4:] == ['-DagTrimRecipient', RECIPIENT, '-DagMaxTrimPerMille', '15']


def test_strategy_report_fields_are_projected_without_raw_data(control):
    c, root, _, _ = control
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    path = account / 'roundtrip-status.json'
    report = {'wallet': WALLET, 'state': 'checking', 'test_mode': 'strategy_meme',
              'token': RECIPIENT, 'symbol': 'MEME', 'execution_arm': 'first_discovery',
              'reason': 'strategy_input_stale', 'snapshot': {'key': 'SECRET'}}
    path.write_text(json.dumps(report))
    result = c.status()
    assert {k: result[k] for k in ('test_mode', 'token', 'symbol', 'execution_arm')} == {
        'test_mode': 'strategy_meme', 'token': RECIPIENT, 'symbol': 'MEME', 'execution_arm': 'first_discovery'}
    assert result['reason'] == 'strategy_input_stale'
    assert 'SECRET' not in json.dumps(result)
    report.update(token='not-an-address', symbol='<img src=x onerror=alert(1)>', execution_arm='SECRET')
    path.write_text(json.dumps(report))
    result = c.status()
    assert result['token'] is result['symbol'] is result['execution_arm'] is None
    assert 'SECRET' not in json.dumps(result) and '<img' not in json.dumps(result)


def test_legacy_report_has_stablecoin_mode(control):
    c, root, _, _ = control
    account = root / 'gmgn-live' / WALLET
    account.mkdir(parents=True)
    (account / 'roundtrip-status.json').write_text(json.dumps({'wallet': WALLET, 'state': 'completed'}))
    assert c.status()['test_mode'] == 'stablecoin'
