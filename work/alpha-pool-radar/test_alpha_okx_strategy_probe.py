"""Strategy selection and one-shot execution use fixtures only."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from alpha_okx_strategy_probe import ProbeError, select_candidate

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
TOKEN = '0x' + '3' * 40
POOL = '0x' + '2' * 40


def payload(now=NOW):
    row = {'chain': 'bsc', 'contract_address': TOKEN, 'pool_address': POOL,
           'symbol': 'MEME', 'execution_arm': 'first_discovery',
           'signal_at': now.isoformat(), 'first_seen_at': now.isoformat(),
           'mcap': 50000, 'change_m5': 10, 'change_h1': 30, 'risk_flags': []}
    quote = {'chain': 'bsc', 'contract_address': TOKEN, 'pool_address': POOL,
             'quote_at': now.isoformat(), 'quote_status': 'fresh',
             'liquidity_usd': 20000, 'price_usd': 0.001}
    return {'updated_at': now.isoformat(), 'signals': [row], 'quotes': [quote]}


def test_original_policy_selects_candidate_not_rating():
    data = payload()
    data['signals'][0]['score'] = 0
    result = select_candidate(data, NOW)
    assert result['contract_address'] == TOKEN
    assert result['execution_arm'] == 'first_discovery'


@pytest.mark.parametrize('mutate', [
    lambda d: d['signals'][0].update(risk_flags=['bundler']),
    lambda d: d['signals'][0].update(chain='robinhood'),
    lambda d: d['signals'][0].update(change_m5=80),
    lambda d: d['signals'][0].update(mcap=2000000),
    lambda d: d['quotes'][0].update(quote_status='stale'),
    lambda d: d['quotes'].append(dict(d['quotes'][0])),
    lambda d: d['signals'].append(dict(d['signals'][0])),
    lambda d: d['signals'][0].update(contract_address='0x' + '0' * 40),
])
def test_rejected_candidates_not_bought(mutate):
    data = payload()
    mutate(data)
    with pytest.raises(ProbeError, match='no_strategy_candidate'):
        select_candidate(data, NOW)


def test_quote_cannot_overwrite_strategy_evidence():
    data = payload()
    data['signals'][0]['risk_flags'] = ['bundler']
    data['quotes'][0].update(risk_flags=[], execution_arm='first_discovery')
    with pytest.raises(ProbeError):
        select_candidate(data, NOW)


def test_stale_input_not_candidate():
    with pytest.raises(ProbeError, match='strategy_input_stale'):
        select_candidate(payload(), NOW + timedelta(seconds=31))


def test_narrative_arm_uses_same_source_and_momentum_rules():
    data = payload()
    data['signals'][0].update(execution_arm='narrative_breakout', mcap=650000,
                              sources=['OKX', 'GMGN'], change_h1=90)
    data['quotes'][0]['liquidity_usd'] = 50000
    assert select_candidate(data, NOW)['execution_arm'] == 'narrative_breakout'
    data['signals'][0]['sources'] = ['GMGN']
    with pytest.raises(ProbeError):
        select_candidate(data, NOW)


def test_existing_history_and_daily_loss_respected():
    for account in ({'orders': {'id': {'token': TOKEN}}},
                    {'daily_losses': {'2026-09-08': '8'}}):
        with pytest.raises(ProbeError):
            select_candidate(payload(), NOW, account=account)


def test_pinned_candidate_never_falls_back_to_another_coin():
    data = payload()
    data['signals'][0]['contract_address'] = '0x' + '4' * 40
    data['quotes'][0]['contract_address'] = '0x' + '4' * 40
    with pytest.raises(ProbeError):
        select_candidate(data, NOW, token=TOKEN, pool=POOL)


def test_meme_roundtrip_receipts_and_existing_holdings(tmp_path):
    from test_alpha_okx_roundtrip_live import Fake, preview, USDT, NATIVE
    from alpha_okx_roundtrip_live import Roundtrip

    class MemeFake(Fake):
        def security(self, token):
            assert token == TOKEN
            return {'safe': True}
        def set_context(self, token, pool):
            assert (token, pool) == (TOKEN, POOL)
        def balance(self, token=NATIVE):
            return super().balance(USDT if token == TOKEN else token)
        def order(self, tx):
            result = super().order(tx)
            for key in ('input_token', 'output_token'):
                if result.get(key) == USDT:
                    result[key] = TOKEN
            return result
        def submit(self, token, *args, **kwargs):
            assert token == TOKEN
            return super().submit(token, *args, **kwargs)

    path = tmp_path / 'input.json'
    path.write_text(json.dumps(payload()))
    fake = MemeFake()
    r = Roundtrip(fake, tmp_path / 'account', preview=preview,
                  strategy_input=path, clock=lambda: NOW, sleep=lambda _: None)
    report = r.run()
    assert report['state'] == 'completed'
    assert report['test_mode'] == 'strategy_meme'
    assert report['token'] == TOKEN and report['symbol'] == 'MEME'
    assert report['execution_arm'] == 'first_discovery'
    assert fake.calls == [('buy', 10**15), ('sell', 10**18)]
    assert fake.tokens == 99 * 10**18
    assert report['original_token_atomic'] == str(99 * 10**18)
    assert report['strategy_started'] is False


@pytest.mark.parametrize('cause', ['risk', 'expired', 'wrong_pool'])
def test_meme_preflight_failures_send_nothing(tmp_path, cause):
    from test_alpha_okx_roundtrip_live import Fake, preview
    from alpha_okx_roundtrip_live import Roundtrip
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(payload()))
    fake = Fake()
    fake.security = lambda _: {'safe': cause != 'risk'}
    current = [NOW]
    def plan():
        if cause == 'expired':
            current[0] += timedelta(seconds=31)
        return {**preview(), 'buy_pool': POOL if cause != 'wrong_pool' else '0x' + '4' * 40}
    r = Roundtrip(fake, tmp_path / 'account', preview=plan,
                  strategy_input=path, clock=lambda: current[0])
    report = r.run()
    assert report['state'] == 'blocked'
    assert not fake.calls


def test_signal_expiring_in_prepared_callback_does_not_broadcast(tmp_path):
    from test_alpha_okx_roundtrip_live import Fake, preview, BUY, USDT, NATIVE
    from alpha_okx_roundtrip_live import Roundtrip
    path = tmp_path / 'input.json'
    path.write_text(json.dumps(payload()))
    current = [NOW]
    fake = Fake()
    fake.security = lambda _: {'safe': True}
    fake.set_context = lambda *_: None
    fake.balance = lambda token=NATIVE: {'amount_atomic': str(10**17 if token == NATIVE else 99 * 10**18)}
    def prepare(token, side, amount, slippage, on_prepared):
        current[0] += timedelta(seconds=31)
        on_prepared(BUY)
        pytest.fail('must stop before broadcast')
    fake.submit = prepare
    report = Roundtrip(fake, tmp_path / 'account', preview=preview, strategy_input=path,
                       clock=lambda: current[0]).run()
    assert report['reason'] == 'strategy_candidate_expired'
    assert report['buy_tx'] == BUY
    assert report['state'] == 'attention'
