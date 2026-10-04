"""Execution preparation tests with synthetic calldata and mocked chain proofs."""
import pytest
from eth_abi import decode, encode
import alpha_okx_dag_execution as mod
import alpha_okx_dag_preflight as dag

WALLET = "0x" + "12" * 20
POOL = "0x" + "34" * 20
NOW = 1800000000
AMOUNT = 10**18
MINIMUM = 10**15


def tx(trailer=b""):
    extra = encode(["uint160", "bytes"], [0, encode(["address", "address"], [dag.USDT, dag.WBNB])])
    path = ([dag.UNIVERSAL_V3_ADAPTER], [dag.UNIVERSAL_V3_ADAPTER],
            [int(POOL, 16) | (10000 << 160) | (1 << 176)], [extra], int(dag.USDT, 16))
    return {"from": WALLET, "to": dag.BSC_ROUTER, "value": "0", "chainId": 56,
            "data": dag.SELECTOR + (encode(dag.ABI, [1, WALLET,
                (int(dag.USDT, 16), dag.OKX_NATIVE, AMOUNT, MINIMUM, NOW + 3600), [path]]) + trailer).hex()}


class Rpc:
    def call(self, method, params):
        assert method in {"eth_getCode", "eth_call"}
        assert params[-1]["requireCanonical"] is True
        return "0x6000" if method == "eth_getCode" else "0x" + f"{MINIMUM:064x}"


@pytest.fixture
def proofs(monkeypatch):
    monkeypatch.setattr(mod, "ROUTER_RUNTIME_HASH", "0x" + dag.keccak(bytes.fromhex("6000")).hex())
    monkeypatch.setattr(dag, "pin_block", lambda rpc: {"reference": {"blockHash": "0x" + "ab" * 32, "requireCanonical": True}})
    monkeypatch.setattr(dag, "verify_v3_pool", lambda *a: {"verified": True})
    monkeypatch.setattr(dag, "verify_v3_adapter", lambda *a: {"runtime_source_verified": True})


def prepare(transaction, **kwargs):
    return mod.prepare_dag(transaction, rpc=Rpc(), wallet=WALLET, source=dag.USDT,
                           target=dag.OKX_NATIVE, amount=AMOUNT, minimum=MINIMUM,
                           expected_pool=kwargs.pop("expected_pool", POOL), now=NOW, **kwargs)


def test_preparation_shortens_only_deadline_and_does_not_mutate_input(proofs):
    original = tx()
    prepared = prepare(original)
    before = decode(dag.ABI, bytes.fromhex(original["data"][10:]))
    after = decode(dag.ABI, bytes.fromhex(prepared["data"][10:]))
    assert before[2][4] == NOW + 3600
    assert after[2][4] == NOW + 180
    assert before[:2] == after[:2] and before[2][:4] == after[2][:4] and before[3] == after[3]


def test_candidate_pool_must_be_on_endpoint(proofs):
    with pytest.raises(dag.PreflightError, match="dag_candidate_pool_mismatch"):
        prepare(tx(), expected_pool=WALLET)


def test_trim_needs_explicit_policy(proofs):
    prefix = dag.TRIM_FLAG << 208
    trailer = (prefix | MINIMUM * 2).to_bytes(32, "big") + (prefix | (100 << 160) | int(WALLET, 16)).to_bytes(32, "big")
    with pytest.raises(dag.PreflightError, match="dag_trim_consent_required"):
        prepare(tx(trailer))
    result = prepare(tx(trailer), trim_recipient=WALLET, max_trim_per_mille=100)
    assert bytes.fromhex(result["data"][-128:]) == trailer


def test_router_code_change_rejected(proofs, monkeypatch):
    monkeypatch.setattr(mod, "ROUTER_RUNTIME_HASH", "0x" + "00" * 32)
    with pytest.raises(dag.PreflightError, match="dag_router_runtime_mismatch"):
        prepare(tx())


def test_chain_simulation_must_meet_encoded_minimum(proofs, monkeypatch):
    monkeypatch.setattr(dag, "simulate", lambda *a, **k: {"status": "below_minimum"})
    with pytest.raises(dag.PreflightError, match="dag_simulation_failed"):
        prepare(tx())


@pytest.mark.parametrize("to_b", [0, 1 << 207])
def test_zero_trim_preserved_without_fee_consent(proofs, to_b):
    prefix = dag.TRIM_FLAG << 208
    trailer = (prefix | to_b | MINIMUM * 2).to_bytes(32, "big") + (prefix | int(WALLET, 16)).to_bytes(32, "big")
    assert prepare(tx(trailer))["data"].endswith(trailer.hex())


def test_transport_enabled_branch_prepares_before_signing(monkeypatch):
    from alpha_okx_live_transport import OkxLiveTransport, NATIVE
    transport = OkxLiveTransport(env={"BSC_WALLET_ADDRESS": WALLET, "OKX_DAG_ENABLED": "1"})
    transport.set_context(dag.USDT, POOL)
    observed = []
    def fake_prepare(transaction, **kwargs):
        observed.append(kwargs)
        assert kwargs["source"] == dag.USDT and kwargs["target"] == dag.OKX_NATIVE
        return {**transaction, "data": "0x1234"}
    monkeypatch.setattr(mod, "prepare_dag", fake_prepare)
    transaction = tx()
    transport._validate_calldata(transaction, dag.USDT, "sell", AMOUNT, MINIMUM, dag.USDT, NATIVE)
    assert transaction["data"] == "0x1234"
    assert observed[0]["expected_pool"] == POOL
    assert transport._signer is None


def test_preparation_rejects_deadline_expired_during_rpc(proofs, monkeypatch):
    monkeypatch.setattr(mod.time, "time", lambda: NOW + 400)
    with pytest.raises(dag.PreflightError, match="dag_deadline_too_close"):
        prepare(tx())


def test_presign_check_requires_thirty_seconds_remaining(monkeypatch):
    monkeypatch.setattr(mod.time, "time", lambda: NOW + 3580)
    with pytest.raises(dag.PreflightError, match="dag_deadline_too_close"):
        mod.require_fresh_deadline(tx())
