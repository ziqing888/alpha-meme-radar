import json
from decimal import Decimal

import pytest

import alpha_okx_roundtrip_live as roundtrip_mod
from alpha_okx_roundtrip_live import Roundtrip, RoundtripError, USDT, NATIVE

WALLET = '0x' + '1' * 40
POOL = '0x' + '2' * 40
BUY = '0x' + 'a' * 64
SELL = '0x' + 'b' * 64
APPROVE = '0x' + 'c' * 64


class Fake:
    wallet_address = WALLET
    def __init__(self):
        self.calls = []
        self.tokens = 99 * 10**18
        self.sent = []
        self.pending = False
        self.bad_sell = False
        self.mined = set()
    def preflight(self): return {'enabled': True}
    def verify_connection(self): return {'ready': True}
    def ready_for_entries(self): return True
    def market(self): return {'native_price_usd': '1000'}
    def balance(self, token=NATIVE):
        return {'amount_atomic': str(self.tokens if token == USDT else 10**17)}
    def set_context(self, token, pool): assert (token, pool) == (USDT, POOL)
    def submit(self, token, side, amount, slippage, on_prepared):
        assert slippage == '1'
        self.calls.append((side, int(amount)))
        h = BUY if side == 'buy' else SELL
        on_prepared(h)
        self.sent.append(h)
        return {'tx_hash': h}
    def order(self, h):
        if self.pending: return {'status': 'pending'}
        if h == APPROVE:
            return {'approval_confirmed': True, 'finalized': True, 'gas_native': '0.00001'}
        buy = h == BUY
        if h not in self.mined:
            self.tokens += 10**18 if buy else -10**18
            self.mined.add(h)
        return {'status': 'confirmed', 'finalized': True, 'fill_complete': True,
                'native_amount_proven': True, 'fill_anomalies': [], 'tx_hash': h,
                'wallet_address': WALLET, 'chain': 'bsc',
                'input_token': NATIVE if buy else USDT,
                'output_token': USDT if buy else NATIVE,
                'input_amount': str(10**15 if buy else (10**18 + int(self.bad_sell))),
                'output_amount': str(10**18 if buy else 99 * 10**13),
                'gas_native': '0.00001'}
    def ensure_allowance(self, token, amount, **kwargs): return {'ready': True}


def preview():
    return {'native_input_atomic': str(10**15), 'buy_pool': POOL, 'sell_pool': POOL}


def runner(tmp_path, fake=None):
    fake = fake or Fake()
    r = Roundtrip(fake, tmp_path, preview=preview, sleep=lambda _: None, wait_polls=2)
    return r, fake


def test_one_round_only_sells_receipt_delta_not_existing_wallet(tmp_path):
    r, f = runner(tmp_path)
    out = r.run()
    assert out['state'] == 'completed'
    assert f.calls == [('buy', 10**15), ('sell', 10**18)]
    assert out['remaining_atomic'] == '0'
    assert Decimal(out['pnl_native']) == Decimal('-0.00003')
    with pytest.raises(RoundtripError, match='test_already_attempted'):
        runner(tmp_path, f)[0].run()
    assert len(f.calls) == 2


def test_ambiguous_buy_never_retried_or_sold(tmp_path):
    r, f = runner(tmp_path)
    f.pending = True
    out = r.run()
    assert out['state'] == 'attention'
    assert out['reason'] == 'confirmation_timeout'
    assert f.calls == [('buy', 10**15)]
    assert out['buy_tx'] == BUY


def test_preview_failure_blocks_before_buy_and_redacts(tmp_path):
    r, f = runner(tmp_path)
    def fail(): raise RuntimeError('SECRET_API_KEY')
    r.preview = fail
    out = r.run()
    assert out['state'] == 'blocked' and not f.calls
    assert 'SECRET' not in json.dumps(out)


def test_sell_amount_anomaly_not_success(tmp_path):
    r, f = runner(tmp_path)
    f.bad_sell = True
    out = r.run()
    assert out['state'] == 'attention'
    assert out['reason'] == 'fill_identity_or_amount_mismatch'


def test_unresolved_transport_blocks_before_buy(tmp_path):
    r, f = runner(tmp_path)
    f.ready_for_entries = lambda: False
    out = r.run()
    assert out['state'] == 'blocked' and not f.calls
    assert out['check_stage'] == 'idle'


def test_preflight_failure_records_check_stage(tmp_path):
    from alpha_okx_roundtrip_live import OkxTransportError
    r, f = runner(tmp_path)
    def fail():
        raise OkxTransportError('missing_eth_account')
    f.preflight = fail
    out = r.run()
    assert out['reason'] == 'missing_eth_account'
    assert out['check_stage'] == 'preflight'
    assert out['eth_account']['spec_found'] is True
    assert out['eth_account']['import_ok'] is True


def test_disabled_never_submits(tmp_path):
    r, f = runner(tmp_path)
    f.preflight = lambda: {'enabled': False}
    assert r.run()['reason'] == 'explicit_live_environment_required'
    assert not f.calls


def test_approval_pending_is_waited_then_no_duplicate_approval(tmp_path):
    r, f = runner(tmp_path)
    calls = []
    def allowance(token, amount, **kwargs):
        calls.append(amount)
        return {'ready': True} if len(calls) == 2 else {'ready': False, 'status': 'pending', 'approval_tx_hash': APPROVE}
    f.ensure_allowance = allowance
    out = r.run()
    assert out['state'] == 'completed'
    assert out['approval_txs'] == [APPROVE]
    assert len(calls) == 2
    assert Decimal(out['gas_native']) == Decimal('0.00003')


def test_existing_strategy_ledger_positions_blocks(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as db:
        db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY, data TEXT)')
        db.execute('INSERT INTO ledger VALUES (1, ?)', (json.dumps({'wallet': WALLET, 'positions': {'x': {}}, 'orders': {}}),))
    r, f = runner(tmp_path)
    assert r.run()['reason'] == 'strategy_account_not_idle'
    assert not f.calls


def test_terminal_order_fill_anomaly_blocks_empty_account(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as db:
        db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY, data TEXT)')
        db.execute('INSERT INTO ledger VALUES (1, ?)', (json.dumps({
            'wallet': WALLET, 'positions': {},
            'orders': {'past_sell': {'status': 'filled', 'fill_anomaly': 'below_minimum'}}}),))
    r, f = runner(tmp_path)
    assert r.run()['reason'] == 'strategy_account_not_idle'
    assert not f.calls


def test_after_buy_balance_change_does_not_sell_original_tokens(tmp_path):
    r, f = runner(tmp_path)
    original = f.balance
    def balance(token=NATIVE):
        if token == USDT and f.sent:
            return {'amount_atomic': '0'}
        return original(token)
    f.balance = balance
    out = r.run()
    assert out['reason'] == 'wallet_balance_changed'
    assert f.calls == [('buy', 10**15)]
    assert out['remaining_atomic'] == str(10**18)


def test_crashed_attempt_is_not_restarted(tmp_path):
    (tmp_path / 'roundtrip-attempt.json').write_text('{}')
    r, f = runner(tmp_path)
    with pytest.raises(RoundtripError, match='test_already_attempted'):
        r.run()
    assert not f.calls


def test_new_session_keeps_wallet_ledger_and_ignores_legacy_roundtrip_record(tmp_path):
    account = tmp_path / 'wallet'
    session = account / 'roundtrip-sessions' / 'manual001'
    account.mkdir()
    (account / 'roundtrip-status.json').write_text(json.dumps({'state': 'checking'}))
    f = Fake()
    r = Roundtrip(f, session, preview=preview, sleep=lambda _: None, wait_polls=2,
                  account_directory=account)
    out = r.run()
    assert out['state'] == 'completed'
    assert (session / 'roundtrip-status.json').is_file()
    assert (account / 'roundtrip-status.json').read_text() == json.dumps({'state': 'checking'})
    assert f.calls == [('buy', 10**15), ('sell', 10**18)]


def test_new_session_uses_wallet_ledger_for_idle_checks(tmp_path):
    import sqlite3
    account = tmp_path / 'wallet'
    session = account / 'roundtrip-sessions' / 'manual001'
    account.mkdir()
    with sqlite3.connect(account / 'ledger.sqlite3') as db:
        db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY, data TEXT)')
        db.execute('INSERT INTO ledger VALUES (1, ?)', (json.dumps({
            'wallet': WALLET, 'positions': {'open': {}}, 'orders': {}}),))
    f = Fake()
    r = Roundtrip(f, session, preview=preview, sleep=lambda _: None, wait_polls=2,
                  account_directory=account)
    assert r.run()['reason'] == 'strategy_account_not_idle'
    assert not f.calls


@pytest.mark.parametrize('field,value', [('native_price_usd', '1001'), ('native_price_usd', 'NaN')])
def test_initial_budget_never_increased(tmp_path, field, value):
    r, f = runner(tmp_path)
    f.market = lambda: {field: value}
    assert r.run()['state'] == 'blocked'
    assert not f.calls


def test_unknown_approval_never_retried(tmp_path):
    r, f = runner(tmp_path)
    f.ensure_allowance = lambda *args, **kwargs: {'ready': False, 'status': 'unknown', 'approval_tx_hash': APPROVE}
    out = r.run()
    assert out['reason'] == 'approval_requires_reconciliation'
    assert len(f.calls) == 1
    assert out['approval_txs'] == [APPROVE]


def test_third_approval_probe_cannot_submit(tmp_path):
    r, f = runner(tmp_path)
    submits = []
    def approve(*args, allow_submit=True):
        if not allow_submit:
            return {'ready': False, 'status': 'blocked'}
        h = '0x' + str(len(submits) + 1) * 64
        submits.append(h)
        return {'ready': False, 'status': 'pending', 'approval_tx_hash': h}
    f.ensure_allowance = approve
    original = f.order
    f.order = lambda h: {'approval_confirmed': True, 'finalized': True, 'gas_native': '0.00001'} if h in submits else original(h)
    out = r.run()
    assert len(submits) == 2
    assert len(out['approval_txs']) == 2
    assert out['state'] == 'attention'


def test_finalized_gas_with_incomplete_fill_recorded_once(tmp_path):
    r, f = runner(tmp_path)
    f.order = lambda h: {'status': 'pending', 'finalized': True,
                         'gas_native': '0.00001', 'error': 'native_amount_unproven'}
    out = r.run()
    assert out['reason'] == 'confirmation_timeout'
    assert Decimal(out['gas_native']) == Decimal('0.00001')


def test_gas_cap_at_signing_boundary(monkeypatch, tmp_path):
    from alpha_okx_roundtrip_live import BoundedTransport, OkxLiveTransport
    import sqlite3
    t = BoundedTransport({'BSC_WALLET_ADDRESS': WALLET}, state_dir=tmp_path)
    t.set_test_budget(Decimal(1000), 10**15)
    signed = []
    monkeypatch.setattr(OkxLiveTransport, '_broadcast', lambda *args: signed.append(args))
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE transactions(nonce INTEGER, status TEXT)')
        intent = {'token': USDT, 'native_price_usd': '1000', 'amount': str(10**15), 'side': 'buy'}
        tx = {'gas': 600000, 'gasPrice': 10**9, 'nonce': 0}
        t._broadcast(db, tx, intent, 'swap')
        with pytest.raises(RoundtripError, match='test_gas_budget_exceeded'):
            t._broadcast(db, tx, {**intent, 'side': 'sell'}, 'swap')
    assert len(signed) == 1


def test_cancelled_nonce_cannot_raise_gas_after_budget_check(monkeypatch, tmp_path):
    from alpha_okx_roundtrip_live import BoundedTransport, OkxLiveTransport
    import sqlite3
    t = BoundedTransport({'BSC_WALLET_ADDRESS': WALLET}, state_dir=tmp_path)
    t.set_test_budget(Decimal(1000), 10**15)
    monkeypatch.setattr(OkxLiveTransport, '_broadcast', lambda *args: pytest.fail('must not sign'))
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE transactions(nonce INTEGER, status TEXT)')
        db.execute("INSERT INTO transactions VALUES (0, 'cancelled')")
        with pytest.raises(RoundtripError, match='test_cancelled_nonce_requires_review'):
            t._broadcast(db, {'gas': 100000, 'gasPrice': 10**9, 'nonce': 0},
                         {'token': USDT, 'native_price_usd': '1000', 'side': 'buy', 'amount': str(10**15)}, 'swap')


@pytest.mark.parametrize('strategy', [False, True])
def test_real_transport_roundtrip_with_only_synthetic_rpc_and_key(tmp_path, monkeypatch, strategy):
    import test_alpha_okx_live_transport as fx
    from datetime import datetime, timezone
    from test_alpha_okx_strategy_probe import payload
    from alpha_okx_roundtrip_live import BoundedTransport
    if not strategy:
        monkeypatch.setattr(fx, 'TOKEN', USDT)
    monkeypatch.setattr(fx.mod, 'build_opener', lambda *args: pytest.fail('network forbidden'))
    signer = fx.RecordingSigner()
    class Rpc(fx.FakeRpc):
        token_balance = 10**9
        def _call(self, method, params, timeout_seconds=None):
            if method == 'eth_call' and params[0]['data'].startswith('0x70a08231'):
                return {'result': '0x' + fx.word(self.token_balance)}
            return super()._call(method, params, timeout_seconds=timeout_seconds)
        def mine(self, h):
            tx = self.transactions[h]
            super().mine(h)
            if not tx['input'].startswith('0x095ea7b3'):
                self.token_balance += fx.SELL if int(tx['value'], 16) else -fx.SELL
    rpc = Rpc(signer)
    env = {'BSC_WALLET_ADDRESS': fx.WALLET, 'BSC_PRIVATE_KEY': fx.KEY,
           'OKX_LIVE_ENABLED': '1', 'OKX_ALLOW_AUTOMATED_TRADES': '1',
           'OKX_API_KEY': 'fixture-key', 'OKX_SECRET_KEY': 'fixture-secret',
           'OKX_PASSPHRASE': 'fixture-pass', 'BSC_RPC_URL': 'https://fixture.invalid'}
    t = BoundedTransport(env, state_dir=tmp_path, rpc=rpc, http=fx.FakeHttp(),
                         signer=signer, security_reader=fx.risk)
    monkeypatch.setattr(t, 'verify_connection', lambda: {'ready': True})
    def mine(_):
        for h in rpc.sent:
            if h not in rpc.receipts: rpc.mine(h)
    path = tmp_path / 'input.json'
    data = payload(datetime.now(timezone.utc))
    for row in data['signals'] + data['quotes']:
        row.update(contract_address=fx.TOKEN, pool_address=fx.POOL)
    path.write_text(json.dumps(data))
    r = Roundtrip(t, tmp_path, preview=lambda: {'native_input_atomic': str(fx.BUY),
                  'buy_pool': fx.POOL, 'sell_pool': fx.POOL}, sleep=mine, wait_polls=3,
                  strategy_input=path if strategy else None)
    out = r.run()
    assert out['state'] == 'completed', out
    assert len(rpc.sent) == len(signer.signed) == 3
    assert rpc.token_balance == 10**9
    assert Decimal(out['gas_native']) == Decimal('0.00025')
    assert Decimal(out['pnl_native']) == -Decimal(out['gas_native'])
    assert r.t.test_approvals == 1 and r.t.test_swaps == {'buy', 'sell'}
    assert out['token'] == fx.TOKEN
    assert out['test_mode'] == ('strategy_meme' if strategy else 'stablecoin')


def test_approval_and_sell_receipt_boundaries(monkeypatch, tmp_path):
    import sqlite3
    from alpha_okx_roundtrip_live import BoundedTransport, OkxLiveTransport
    t = BoundedTransport({'BSC_WALLET_ADDRESS': WALLET}, state_dir=tmp_path)
    t.set_test_budget(Decimal(1000), 10**15)
    t.set_sell_budget(123)
    sent = []
    monkeypatch.setattr(OkxLiveTransport, '_broadcast', lambda *args: sent.append(args))
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE transactions(nonce INTEGER, status TEXT)')
        tx = {'gas': 1000, 'gasPrice': 10**9, 'nonce': 0}
        base = {'token': USDT, 'native_price_usd': '1000'}
        t._broadcast(db, tx, {**base, 'amount': '0'}, 'approval')
        t._broadcast(db, tx, {**base, 'amount': '123'}, 'approval')
        with pytest.raises(RoundtripError, match='test_approval_limit_exceeded'):
            t._broadcast(db, tx, {**base, 'amount': '123'}, 'approval')
        with pytest.raises(RoundtripError, match='test_sell_amount_mismatch'):
            t._broadcast(db, tx, {**base, 'side': 'sell', 'amount': '124'}, 'swap')
    assert len(sent) == 2


def test_status_replace_retries_reader_lock_only(monkeypatch, tmp_path):
    from pathlib import Path
    import alpha_okx_roundtrip_live as mod
    original = Path.replace
    calls = []
    def replace(path, target):
        calls.append(target)
        if len(calls) < 3: raise PermissionError('temporary reader lock')
        return original(path, target)
    monkeypatch.setattr(Path, 'replace', replace)
    monkeypatch.setattr(mod.time, 'sleep', lambda _: None)
    mod.atomic_write(tmp_path / 'status.json', {'state': 'blocked'})
    assert json.loads((tmp_path / 'status.json').read_text()) == {'state': 'blocked'}
    assert len(calls) == 3


def test_main_reports_existing_attempt_reason_without_wrapping(monkeypatch, tmp_path, capsys):
    class FakeTransport:
        wallet_address = WALLET
        state_dir = tmp_path

    (tmp_path / 'roundtrip-status.json').write_text('{}')
    monkeypatch.setenv('OKX_LIVE_ENABLED', '1')
    monkeypatch.setenv('OKX_ALLOW_AUTOMATED_TRADES', '1')
    monkeypatch.setattr(roundtrip_mod, 'BoundedTransport', lambda: FakeTransport())

    assert roundtrip_mod.main(['--live', '--strategy-meme']) == 1
    out = json.loads(capsys.readouterr().out)
    assert out['state'] == 'blocked'
    assert out['reason'] == 'test_already_attempted'
    assert out['last_step'] == 'checking'


def test_main_uses_explicit_session_directory_for_repeat_test(monkeypatch, tmp_path, capsys):
    class FakeTransport:
        wallet_address = WALLET
        state_dir = tmp_path

    seen = {}

    class FakeRoundtrip:
        def __init__(self, transport, directory, *, preview, strategy_input=None, account_directory=None):
            seen['directory'] = directory
            seen['account_directory'] = account_directory
            self.token = USDT
            self.report = {'state': 'checking'}

        def run(self):
            return {'state': 'completed', 'test_mode': 'strategy_meme'}

    (tmp_path / 'roundtrip-status.json').write_text('{}')
    monkeypatch.setenv('OKX_LIVE_ENABLED', '1')
    monkeypatch.setenv('OKX_ALLOW_AUTOMATED_TRADES', '1')
    monkeypatch.setattr(roundtrip_mod, 'BoundedTransport', lambda: FakeTransport())
    monkeypatch.setattr(roundtrip_mod, 'Roundtrip', FakeRoundtrip)

    assert roundtrip_mod.main(['--live', '--strategy-meme', '--session-id', 'manual001']) == 0
    assert seen['directory'] == tmp_path / 'roundtrip-sessions' / 'manual001'
    assert seen['account_directory'] == tmp_path
    assert json.loads(capsys.readouterr().out)['state'] == 'completed'


def test_signer_check_only_runs_preflight_without_live_gate(monkeypatch, tmp_path, capsys):
    calls = []

    class FakeTransport:
        wallet_address = WALLET
        state_dir = tmp_path

        def preflight(self):
            calls.append('preflight')
            return {'ok': True, 'enabled': False}

    monkeypatch.delenv('OKX_LIVE_ENABLED', raising=False)
    monkeypatch.delenv('OKX_ALLOW_AUTOMATED_TRADES', raising=False)
    monkeypatch.setattr(roundtrip_mod, 'BoundedTransport', lambda: FakeTransport())

    assert roundtrip_mod.main(['--signer-check']) == 0
    assert calls == ['preflight']
    out = json.loads(capsys.readouterr().out)
    assert out == {'configured': True, 'live_started': False, 'wallet': WALLET,
                   'provider': 'okx', 'signer_checked': True}


def test_preflight_only_uses_roundtrip_session_and_does_not_execute(monkeypatch, tmp_path, capsys):
    calls = []

    class FakeTransport:
        wallet_address = WALLET
        state_dir = tmp_path

        def preflight(self):
            calls.append('preflight')
            return {'ok': True, 'enabled': True}

    monkeypatch.setenv('OKX_LIVE_ENABLED', '1')
    monkeypatch.setenv('OKX_ALLOW_AUTOMATED_TRADES', '1')
    monkeypatch.setattr(roundtrip_mod, 'BoundedTransport', lambda: FakeTransport())

    assert roundtrip_mod.main(['--live', '--strategy-meme', '--preflight-only', '--session-id', 'manual002']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['state'] == 'preflight_ok'
    assert out['check_stage'] == 'preflight'
    assert out['test_mode'] == 'strategy_meme'
    assert out['eth_account']['spec_found'] is True
    assert out['eth_account']['import_ok'] is True
    assert calls == ['preflight']
    assert (tmp_path / 'roundtrip-sessions' / 'manual002' / 'roundtrip-status.json').is_file()
