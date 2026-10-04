"""Synthetic ABI fixtures only; no keys, host configuration or network."""

import pytest
from eth_abi import encode

import alpha_okx_dag_preflight as mod

WALLET = "0x" + "12" * 20
ADAPTER = "0x" + "34" * 20
POOL = "0x" + "56" * 20
NOW = 1800000000
AMOUNT = 5 * 10**18
MINIMUM = 10**15


def fixture(*, receiver=WALLET, amount=AMOUNT, minimum=MINIMUM,
            expiry=NOW + 300, source=None, paths=None, trailer=b""):
    source = source or mod.USDT
    if paths is None:
        packed = int(POOL, 16) | (10000 << 160) | (1 << 176)
        extra = encode(["uint160", "bytes"], [0, encode(["address", "address"], [source, mod.WBNB])])
        paths = [([ADAPTER], [ADAPTER], [packed], [extra], int(source, 16))]
    data = mod.SELECTOR + (encode(mod.ABI, [42, receiver,
        (int(source, 16), mod.OKX_NATIVE, amount, minimum, expiry), paths]) + trailer).hex()
    return {"from": WALLET, "to": mod.BSC_ROUTER, "data": data, "value": "0"}


def inspect(tx, **kwargs):
    return mod.inspect_dag(tx, wallet=WALLET, source=mod.USDT,
                           target=mod.OKX_NATIVE, amount=AMOUNT,
                           minimum=MINIMUM, now=NOW, **kwargs)


def test_canonical_envelope_is_not_execution_authorization():
    result = inspect(fixture())
    assert result["envelope_valid"] is True
    assert result["execution_ready"] is False
    assert "adapter_runtime_unverified" in result["blockers"]
    assert result["hops"][0]["pool"] == POOL
    assert result["hops"][0]["extra_data_bytes"] == 160


@pytest.mark.parametrize("change,code", [
    ({"receiver": ADAPTER}, "recipient_mismatch"),
    ({"amount": AMOUNT + 1}, "amount_mismatch"),
    ({"minimum": MINIMUM - 1}, "minimum_output_insufficient"),
    ({"expiry": NOW}, "deadline_invalid"),
    ({"expiry": NOW + 1201}, "deadline_invalid"),
    ({"source": POOL}, "token_mismatch"),
    ({"trailer": bytes(32)}, "trailer_unsupported"),
    ({"trailer": bytes(64)}, "trim_flag_invalid"),
    ({"paths": []}, "path_count_unsupported"),
])
def test_reject_changed_envelope(change, code):
    with pytest.raises(mod.PreflightError, match="^" + code + "$"):
        inspect(fixture(**change))


@pytest.mark.parametrize("key,value,code", [
    ("from", ADAPTER, "sender_mismatch"),
    ("to", ADAPTER, "router_mismatch"),
    ("value", "1", "native_value_mismatch"),
    ("chainId", "0x1", "chain_mismatch"),
    ("data", "0xdeadbeef", "selector_unsupported"),
    ("data", "0x0c307f76ff", "abi_invalid"),
])
def test_reject_outer_transaction(key, value, code):
    tx = fixture()
    tx[key] = value
    with pytest.raises(mod.PreflightError, match="^" + code + "$"):
        inspect(tx)


def test_trim_decoded_but_never_silently_approved():
    prefix = mod.TRIM_FLAG << 208
    trailer = (prefix | (1 << 207) | MINIMUM * 2).to_bytes(32, "big")
    trailer += (prefix | (100 << 160) | int(ADAPTER, 16)).to_bytes(32, "big")
    result = inspect(fixture(trailer=trailer))
    assert result["trim"]["rate_per_1000"] == 100
    assert result["trim"]["recipient"] == ADAPTER
    assert "trim_recipient_unverified" in result["blockers"]


def test_trim_rate_over_limit_rejected():
    prefix = mod.TRIM_FLAG << 208
    trailer = (prefix | MINIMUM * 2).to_bytes(32, "big")
    trailer += (prefix | (101 << 160) | int(ADAPTER, 16)).to_bytes(32, "big")
    with pytest.raises(mod.PreflightError, match="trim_rate_invalid"):
        inspect(fixture(trailer=trailer))


@pytest.mark.parametrize("packed,code", [
    (int(POOL, 16) | (9999 << 160) | (1 << 176), "path_weight_invalid"),
    (int(POOL, 16) | (10000 << 160) | (2 << 176), "path_index_invalid"),
    (int(POOL, 16) | (10000 << 160) | (1 << 176) | (1 << 220), "path_flags_unsupported"),
])
def test_reject_packed_path(packed, code):
    paths = [([ADAPTER], [ADAPTER], [packed], [b""], int(mod.USDT, 16))]
    with pytest.raises(mod.PreflightError, match="^" + code + "$"):
        inspect(fixture(paths=paths))


def test_noncanonical_offset_alias_rejected():
    tx = fixture()
    data = bytearray.fromhex(tx["data"][10:])
    # Move the dynamic paths tail, leaving an unreferenced ABI word.
    offset = int.from_bytes(data[224:256], "big")
    data[224:256] = (offset + 32).to_bytes(32, "big")
    data[offset:offset] = bytes(32)
    tx["data"] = mod.SELECTOR + data.hex()
    with pytest.raises(mod.PreflightError, match="abi_noncanonical"):
        inspect(tx)


@pytest.mark.parametrize("method", ["eth_sendRawTransaction", "eth_sendTransaction", "personal_sign", "eth_sign"])
def test_rpc_cannot_submit_or_sign(method):
    rpc = mod.ReadOnlyRpc("https://example.invalid")
    with pytest.raises(mod.PreflightError, match="rpc_method_forbidden"):
        rpc.call(method, [])


def test_simulation_success_is_not_a_fill():
    class Rpc:
        def call(self, method, params):
            assert method == "eth_call"
            assert len(params) == 2  # No state override disguising balance/allowance.
            return "0x" + (MINIMUM + 1).to_bytes(32, "big").hex()
    result = mod.simulate(Rpc(), fixture(), "0x123", MINIMUM)
    assert result == {"status": "simulated", "output_atomic": str(MINIMUM + 1), "broadcast": False}


def test_simulation_empty_return_not_success():
    class Rpc:
        def call(self, *args):
            return "0x"
    assert mod.simulate(Rpc(), fixture(), "0x123", MINIMUM)["status"] == "invalid_return"


def test_simulation_can_inspect_provider_one_hour_deadline_without_allowing_live():
    result = inspect(fixture(expiry=NOW + 3600), simulation_only_long_deadline=True)
    assert result["execution_ready"] is False
    assert "deadline_exceeds_live_limit" in result["blockers"]
    with pytest.raises(mod.PreflightError, match="deadline_invalid"):
        inspect(fixture(expiry=NOW + 3601), simulation_only_long_deadline=True)


def test_native_input_is_wrapped_before_first_dag_node():
    packed = int(POOL, 16) | (10000 << 160) | (1 << 176)
    extra = encode(["uint160", "bytes"], [0, encode(["address", "address"], [mod.WBNB, mod.USDT])])
    path = ([ADAPTER], [ADAPTER], [packed], [extra], int(mod.WBNB, 16))
    tx = {"from": WALLET, "to": mod.BSC_ROUTER, "value": str(AMOUNT),
          "data": mod.SELECTOR + encode(mod.ABI, [42, WALLET,
             (int(mod.OKX_NATIVE, 16), mod.USDT, AMOUNT, MINIMUM, NOW + 300), [path]]).hex()}
    result = mod.inspect_dag(tx, wallet=WALLET, source=mod.OKX_NATIVE,
                            target=mod.USDT, amount=AMOUNT, minimum=MINIMUM, now=NOW)
    assert result["hops"][0]["input_token"] == mod.WBNB
    assert result["hops"][0]["output_token"] == mod.USDT


@pytest.mark.parametrize("broken", [False, True])
def test_two_hop_continuity(broken):
    middle = "0x" + "ab" * 20
    paths = []
    for i, (source, target) in enumerate([(mod.USDT, middle), (middle, mod.WBNB)]):
        pair_source = mod.USDT if broken and i == 1 else source
        extra = encode(["uint160", "bytes"], [0, encode(["address", "address"], [pair_source, target])])
        packed = int(POOL, 16) | (10000 << 160) | ((i + 1) << 176) | (i << 184)
        paths.append(([ADAPTER], [ADAPTER], [packed], [extra], int(source, 16)))
    if broken:
        with pytest.raises(mod.PreflightError, match="adapter_token_path_mismatch"):
            inspect(fixture(paths=paths))
    else:
        assert len(inspect(fixture(paths=paths))["hops"]) == 2


def test_below_minimum_simulation_not_success():
    class Rpc:
        def call(self, *args):
            return "0x" + (MINIMUM - 1).to_bytes(32, "big").hex()
    assert mod.simulate(Rpc(), fixture(), "0x123", MINIMUM)["status"] == "below_minimum"


def test_rpc_failure_not_success():
    class Rpc:
        def call(self, *args):
            raise mod.PreflightError("rpc_error_-32000")
    assert mod.simulate(Rpc(), fixture(), "0x123", MINIMUM) == {
        "status": "failed", "error": "rpc_error_-32000", "broadcast": False}


def test_legacy_execution_gate_still_rejects_dag():
    from alpha_okx_live_transport import OkxLiveTransport, OkxTransportError
    transport = OkxLiveTransport(env={"BSC_WALLET_ADDRESS": WALLET})
    with pytest.raises(OkxTransportError, match="calldata_unsupported"):
        transport._validate_calldata(fixture(), mod.USDT, "sell", AMOUNT, MINIMUM, mod.USDT, mod.OKX_NATIVE)


def test_slow_successful_call_is_expired(monkeypatch):
    times = iter([100, 106])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(times))
    class Rpc:
        def call(self, *args):
            return "0x" + MINIMUM.to_bytes(32, "big").hex()
    assert mod.simulate(Rpc(), fixture(), "0x123", MINIMUM,
                        quote_deadline=105)["status"] == "expired_quote"


@pytest.mark.parametrize("number,age,error", [
    ("provider-private-text", 0, "block_number_invalid"),
    ("0x123", 61, "block_not_fresh"),
    ("0x123", -11, "block_not_fresh"),
])
def test_block_validation(number, age, error):
    class Rpc:
        def call(self, method, params):
            if method == "eth_blockNumber":
                return number
            return {"number": number, "hash": "0x" + "ab" * 32, "timestamp": hex(NOW - age)}
    with pytest.raises(mod.PreflightError, match="^" + error + "$"):
        mod.pin_block(Rpc(), now=NOW)


def test_pin_block_uses_canonical_hash_not_unvalidated_number():
    class Rpc:
        def call(self, method, params):
            return "0x123" if method == "eth_blockNumber" else {
                "number": "0x123", "hash": "0x" + "ab" * 32, "timestamp": hex(NOW)}
    block = mod.pin_block(Rpc(), now=NOW)
    assert block["reference"] == {"blockHash": "0x" + "ab" * 32, "requireCanonical": True}


class PoolRpc:
    def __init__(self, *, claimed_factory=None, registered_pool=POOL, fee=500,
                 source=None, code="0x6000", chain="0x38"):
        self.claimed_factory = claimed_factory
        self.registered_pool, self.fee = registered_pool, fee
        self.source, self.code, self.chain = source or mod.USDT, code, chain
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "eth_chainId":
            return self.chain
        assert params[-1] == {"blockHash": "0x" + "ab" * 32, "requireCanonical": True}
        if method == "eth_getCode":
            return self.code
        tx = params[0]
        if tx["to"] == mod.PANCAKE_V3_FACTORY:
            assert tx["data"] == "0x" + mod.keccak(text="getPool(address,address,uint24)")[:4].hex() + encode(
                ["address", "address", "uint24"], [self.source, mod.WBNB, self.fee]).hex()
            return "0x" + encode(["address"], [self.registered_pool]).hex()
        if tx["data"] == "0xc45a0155":
            return "0x" + encode(["address"], [self.claimed_factory or mod.PANCAKE_V3_FACTORY]).hex()
        if tx["data"] == "0xddca3f43":
            return "0x" + encode(["uint256"], [self.fee]).hex()
        value = self.source if tx["data"] == "0x0dfe1681" else mod.WBNB
        return "0x" + encode(["address"], [value]).hex()


def pool_check(rpc):
    return mod.verify_v3_pool(rpc, {"pool": POOL, "input_token": mod.USDT, "output_token": mod.WBNB},
                              {"blockHash": "0x" + "ab" * 32, "requireCanonical": True})


def test_pool_must_be_registered_with_official_factory():
    result = pool_check(PoolRpc())
    assert result["verified"] is True
    assert result["factory"] == mod.PANCAKE_V3_FACTORY
    assert result["fee"] == 500


@pytest.mark.parametrize("change,error", [
    ({"claimed_factory": ADAPTER}, "pool_factory_mismatch"),
    ({"registered_pool": ADAPTER}, "pool_not_registered"),
    ({"source": ADAPTER}, "pool_tokens_mismatch"),
    ({"fee": 0}, "pool_fee_invalid"),
    ({"fee": 1000000}, "pool_fee_invalid"),
    ({"code": "0x"}, "contract_code_missing"),
    ({"chain": "0x1"}, "chain_mismatch"),
])
def test_pool_attestation_rejects_bad_evidence(change, error):
    with pytest.raises(mod.PreflightError, match="^" + error + "$"):
        pool_check(PoolRpc(**change))


def test_pool_attestation_requires_canonical_block_reference():
    with pytest.raises(mod.PreflightError, match="canonical_block_required"):
        mod.verify_v3_pool(PoolRpc(), {"pool": POOL}, "latest")


def test_verified_source_is_not_organizational_authorization(monkeypatch):
    monkeypatch.setattr(mod, "UNIVERSAL_V3_RUNTIME_HASH", "0x" + mod.keccak(bytes.fromhex("6000")).hex())
    class Rpc:
        def call(self, method, params):
            if method == "eth_chainId":
                return "0x38"
            assert params[-1]["requireCanonical"] is True
            return "0x6000" if method == "eth_getCode" else "0x" + encode(["address"], [mod.WBNB]).hex()
    result = mod.verify_v3_adapter(Rpc(), mod.UNIVERSAL_V3_ADAPTER,
                                   {"blockHash": "0x" + "ab" * 32, "requireCanonical": True})
    assert result["runtime_source_verified"] is True
    assert result["official_adapter_authorization_verified"] is False


def test_changed_adapter_code_rejected():
    class Rpc:
        def call(self, method, params):
            return "0x38" if method == "eth_chainId" else "0x6000"
    with pytest.raises(mod.PreflightError, match="adapter_runtime_mismatch"):
        mod.verify_v3_adapter(Rpc(), mod.UNIVERSAL_V3_ADAPTER,
                              {"blockHash": "0x" + "ab" * 32, "requireCanonical": True})
