"""Sequential simulation fixtures, never real transactions."""
import pytest
import alpha_okx_roundtrip_preflight as mod
from alpha_okx_dag_preflight import PreflightError, USDT, BSC_ROUTER

WALLET = "0x" + "12" * 20
HASH = "0x" + "ab" * 32
BUY = {"from": WALLET, "to": BSC_ROUTER, "data": "0x1234", "value": "100"}
SELL = {**BUY, "value": "0"}
BLOCK = {"number": "0x123", "hash": HASH, "timestamp": 1800000000}


class Rpc:
    def __init__(self, *, amounts=None, failed=None, parent=HASH, native=90, child="0x124", timestamp=1800000001):
        self.amounts = amounts or [1000, 110, 1110, 1, 1, 90, 1010]
        self.failed, self.parent = failed, parent
        self.native, self.child, self.timestamp = native, child, timestamp

    def call(self, method, params):
        if method == "eth_getBlockByNumber":
            return {"hash": self.parent}
        assert method == "eth_simulateV1"
        body, block = params
        assert block == "0x123"
        assert body["validation"] is False
        assert body["traceTransfers"] is True
        assert set(body["blockStateCalls"][0]) == {"calls"}
        calls = body["blockStateCalls"][0]["calls"]
        assert len(calls) == 7
        assert int(calls[3]["data"][-64:], 16) == 0
        assert int(calls[4]["data"][-64:], 16) == 100
        return [{"parentHash": self.parent, "number": self.child, "timestamp": hex(self.timestamp), "calls": [
            {"status": "0x0" if i == self.failed else "0x1", "returnData": "0x" + f"{v:064x}",
             "logs": [{"address": mod.OKX_NATIVE, "topics": [mod.TRANSFER_TOPIC,
                       "0x" + BSC_ROUTER[2:].zfill(64), "0x" + WALLET[2:].zfill(64)],
                       "data": "0x" + f"{self.native:064x}"}] if i == 5 else [],
             "gasUsed": "0x5208"} for i, v in enumerate(self.amounts)]}]


def run(rpc):
    return mod.simulate_roundtrip(rpc, buy=BUY, sell=SELL, token=USDT, block=BLOCK,
                                  buy_minimum=100, sell_amount=100, sell_minimum=80,
                                  calldata_deadlines=[1800000300, 1800000300],
                                  quote_deadline=mod.time.monotonic() + 10)


def test_sequential_balances_prove_sell_uses_new_purchase():
    result = run(Rpc())
    assert result["status"] == "simulated_round_trip"
    assert result["bought_atomic"] == "110"
    assert result["remaining_new_token_atomic"] == "10"
    assert result["broadcast"] is False
    assert result["execution_ready"] is False


@pytest.mark.parametrize("failed", range(7))
def test_each_failed_call_rejects_roundtrip(failed):
    with pytest.raises(PreflightError, match="roundtrip_call_failed_" + str(failed)):
        run(Rpc(failed=failed))


def test_existing_balance_cannot_hide_failed_buy():
    with pytest.raises(PreflightError, match="roundtrip_buy_delta_insufficient"):
        run(Rpc(amounts=[1000, 110, 1050, 1, 1, 90, 950]))


def test_wrong_sell_delta_rejected():
    with pytest.raises(PreflightError, match="roundtrip_sell_delta_mismatch"):
        run(Rpc(amounts=[1000, 110, 1110, 1, 1, 90, 1020]))


def test_reorg_rejected():
    with pytest.raises(PreflightError, match="roundtrip_parent_mismatch"):
        run(Rpc(parent="0x" + "cd" * 32))


def test_router_return_cannot_replace_native_receipt():
    with pytest.raises(PreflightError, match="roundtrip_native_receipt_insufficient"):
        run(Rpc(native=0))


@pytest.mark.parametrize("kwargs,error", [
    ({"child": "0x125"}, "roundtrip_child_number_invalid"),
    ({"timestamp": 0}, "roundtrip_child_timestamp_invalid"),
    ({"timestamp": 1800000500}, "roundtrip_child_timestamp_invalid"),
])
def test_child_block_checked(kwargs, error):
    with pytest.raises(PreflightError, match="^" + error + "$"):
        run(Rpc(**kwargs))


def test_meme_token_used_in_both_read_only_routes(monkeypatch):
    token = '0x' + '77' * 20
    calls = []
    class Transport:
        def __init__(self, env):
            assert env['OKX_LIVE_ENABLED'] == env['OKX_ALLOW_AUTOMATED_TRADES'] == '0'
            assert 'BSC_PRIVATE_KEY' not in env
        def market(self): return {'max_buy_amount_atomic': '5000'}
        def _client(self): return self
        def _request(self, path, params):
            calls.append(params)
            return {'code': '0', 'data': [{'tx': {}, 'routerResult': {'toTokenAmount': '1000'}}]}
        def _route_identity(self, route, source, target, amount): pass
    class ReadOnly:
        def __init__(self, url): pass
        def call(self, method, params):
            assert method == 'eth_chainId'
            return '0x38'
    def inspect(tx, **kwargs):
        assert kwargs['source'] == token or kwargs['target'] == token
        return {'min_output_atomic': '900', 'deadline': 1800000300, 'hops': [],
                'blockers': ['pool_factory_unverified']}
    def simulate(rpc, **kwargs):
        assert kwargs['token'] == token
        return {'status': 'simulated_round_trip'}
    monkeypatch.setattr(mod, 'OkxLiveTransport', Transport)
    monkeypatch.setattr(mod, 'ReadOnlyRpc', ReadOnly)
    monkeypatch.setattr(mod, 'inspect_dag', inspect)
    monkeypatch.setattr(mod, 'pin_block', lambda _: {**BLOCK, 'reference': {}})
    monkeypatch.setattr(mod, 'simulate_roundtrip', simulate)
    monkeypatch.setattr(mod, 'attest_adapters', lambda *_: [])
    result = mod.run({'wallet_address': WALLET, 'bsc_rpc_url': 'https://fixture.invalid'},
                     notional_usd='1', token=token)
    assert result['token'] == token
    assert [(x['fromTokenAddress'], x['toTokenAddress']) for x in calls] == [
        (mod.OKX_NATIVE, token), (token, mod.OKX_NATIVE)]
    assert calls[0]['amount'] == '1000'
