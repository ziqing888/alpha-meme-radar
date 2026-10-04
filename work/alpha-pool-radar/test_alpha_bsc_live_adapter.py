from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alpha_bsc_live_adapter as adapter_module
from alpha_bsc_live_adapter import (
    AdapterError,
    BSC_NATIVE_TOKEN,
    OkxBscSwapAbi,
    JsonRpcBroadcaster,
    LiveExecutionAdapter,
    LocalEvmSigner,
    redact_transaction,
)


TOKEN = "0x" + "1" * 40
POOL = "0x" + "2" * 40
WALLET = "0x" + "3" * 40
NATIVE = BSC_NATIVE_TOKEN
ERC20_IN = "0x" + "6" * 40
ROUTER = "0x" + "5" * 40
SWAP_SELECTOR = "0x12345678"
PRIVATE_KEY = "0x" + "a" * 64
RAW_TX = "0x" + "deadbeef"
TX_HASH = "0x" + "b" * 64


def test_rpc_http_identifies_client_and_preserves_request(monkeypatch):
    captured = []

    def open_request(request, timeout):
        captured.append((request, timeout))
        return io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x38"}')

    monkeypatch.setattr(adapter_module, "urlopen", open_request)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    result = JsonRpcBroadcaster._urllib_transport("https://rpc.example.test", payload, 3)
    request, timeout = captured[0]
    assert request.get_header("User-agent") == "alpha-radar/1.0"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == payload
    assert timeout == 3
    assert result["result"] == "0x38"


class FakeAccount:
    address = WALLET

    def sign_transaction(self, transaction: dict[str, Any]) -> SimpleNamespace:
        assert transaction["chainId"] == 56
        return SimpleNamespace(raw_transaction=bytes.fromhex("1234"))


class FakeAccountApi:
    @staticmethod
    def from_key(private_key: str) -> FakeAccount:
        assert private_key == PRIVATE_KEY
        return FakeAccount()


class FakeSigner:
    wallet_address = WALLET

    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.verifications: list[tuple[str, dict[str, Any]]] = []

    def sign(self, transaction: dict[str, Any]) -> str:
        self.transactions.append(transaction)
        return RAW_TX

    def verify_signed_transaction(self, raw_transaction: str, expected_transaction: dict[str, Any]) -> bool:
        self.verifications.append((raw_transaction, expected_transaction))
        return raw_transaction == RAW_TX


class ReplacingRawSigner(FakeSigner):
    def sign(self, transaction: dict[str, Any]) -> str:
        self.transactions.append(transaction)
        return "0xfeed"

    def verify_signed_transaction(self, raw_transaction: str, expected_transaction: dict[str, Any]) -> bool:
        self.verifications.append((raw_transaction, expected_transaction))
        return False


class MissingVerifySigner(FakeSigner):
    verify_signed_transaction = None


class FakeBroadcaster:
    def __init__(self, receipt: dict[str, Any] | None = None, receipt_error: Exception | None = None) -> None:
        self.sent: list[str] = []
        self.receipt_value = receipt
        self.receipt_error = receipt_error
        self.last_error: dict[str, str] | None = None

    def send(self, raw_transaction: str) -> str:
        self.sent.append(raw_transaction)
        return TX_HASH

    def receipt(self, tx_hash: str, timeout_seconds: int) -> dict[str, Any] | None:
        assert tx_hash == TX_HASH
        if self.receipt_error is not None:
            raise self.receipt_error
        return self.receipt_value


class FakeOkxSwapClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.tx_overrides: dict[str, Any] = {}
        self.context_overrides: dict[str, Any] = {}
        self.calldata_overrides: dict[str, Any] = {}
        self.return_non_mapping_tx = False

    @staticmethod
    def _word(value: Any) -> str:
        if isinstance(value, str) and value.startswith("0x"):
            number = int(value, 16)
        else:
            number = int(value)
        return f"{number:064x}"

    def build_swap_bsc_with_context(
        self,
        token_address: str,
        amount_atomic: str,
        from_token: str,
        slippage_percent: str,
        wallet_address: str,
        pool_address: str,
        chain_id: int,
    ) -> dict[str, Any]:
        self.calls.append({
            "token_address": token_address,
            "amount_atomic": amount_atomic,
            "from_token": from_token,
            "slippage_percent": slippage_percent,
            "wallet_address": wallet_address,
        })
        transaction = {
            "chainId": 56,
            "from": WALLET,
            "to": ROUTER,
            "data": "0xabcdef",
            "value": amount_atomic if from_token.lower() == NATIVE.lower() else "0x0",
            "private_key": PRIVATE_KEY,
            "unknown_upstream_field": "do-not-forward",
        }
        transaction.update(self.tx_overrides)
        context = {
            "chain_id": chain_id,
            "token_address": TOKEN,
            "amount_atomic": amount_atomic,
            "slippage_percent": slippage_percent,
            "pool_address": pool_address,
            "from_token_address": from_token,
            "target_token_address": token_address,
            "quoted_out": amount_atomic,
            "min_out": str(int(int(amount_atomic) * (100 - float(slippage_percent)) // 100)),
        }
        context.update(self.context_overrides)
        token_in = from_token
        token_out = token_address
        direction = 0 if token_address == TOKEN else 1
        words = [
            token_in, token_out, pool_address, wallet_address,
            amount_atomic, context["min_out"], direction,
        ]
        calldata = SWAP_SELECTOR + "".join(self._word(value) for value in words)
        calldata = self.calldata_overrides.get("data", calldata)
        calldata = self.tx_overrides.get("data", calldata)
        transaction["data"] = calldata
        tx_payload: Any = [] if self.return_non_mapping_tx else transaction
        return {
            "ok": True,
            "status": "ok",
            "request_context": context,
            "tx": tx_payload,
        }


class LegacyOkxSwapClient:
    def build_swap_bsc(self, **kwargs: str) -> dict[str, Any]:
        return {"ok": True, "tx": {}}


def make_execution_adapter(
    *, receipt: dict[str, Any] | None | object = ..., enabled: str = "true", armed: bool = True,
    okx: Any | None = None, receipt_error: Exception | None = None,
    environment: dict[str, str] | None = None,
    signer: Any | None = None,
) -> tuple[LiveExecutionAdapter, FakeSigner, FakeBroadcaster, FakeOkxSwapClient]:
    signer = signer or FakeSigner()
    receipt_value = {"status": "0x1", "blockNumber": "0x10"} if receipt is ... else receipt
    broadcaster = FakeBroadcaster(receipt_value, receipt_error=receipt_error)
    okx = okx or FakeOkxSwapClient()
    env = environment or {"LIVE_TRADING_ENABLED": enabled}
    execution = LiveExecutionAdapter(
        signer=signer,
        broadcaster=broadcaster,
        okx_client=okx,
        wallet_address=WALLET,
        allowed_router_addresses={ROUTER},
        swap_abi=OkxBscSwapAbi(selector=SWAP_SELECTOR),
        env=env,
        armed=armed,
        receipt_timeout_seconds=0,
    )
    return execution, signer, broadcaster, okx  # type: ignore[return-value]


def _dynamic_word(value: Any) -> str:
    if isinstance(value, str) and value.startswith("0x"):
        value = int(value, 16)
    return f"{int(value):064x}"


def _uniswap_v3_dynamic_data(wallet: str, amount: int, minimum: int, pool: str) -> str:
    # uniswapV3SwapTo(receiver, amount, minReturn, pools), with one pool word.
    words = [
        _dynamic_word(wallet), _dynamic_word(amount), _dynamic_word(minimum),
        _dynamic_word(128), _dynamic_word(1), _dynamic_word(pool),
    ]
    return "0x0d5f0e3b" + "".join(words)


def test_disabled_by_default_rejects_before_okx_or_broadcast():
    execution, _, broadcaster, okx = make_execution_adapter(enabled="false")

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result == {"ok": False, "status": "rejected", "error": "live_disabled"}
    assert not broadcaster.sent
    assert not okx.calls


def test_official_dynamic_uniswap_v3_calldata_is_bound_before_signing():
    okx = FakeOkxSwapClient()
    okx.calldata_overrides["data"] = _uniswap_v3_dynamic_data(WALLET, 1000, 990, POOL)
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["status"] == "confirmed"
    assert result["side"] == "buy"
    assert len(signer.transactions) == 1
    assert broadcaster.sent == [RAW_TX]


def test_official_dynamic_calldata_rejects_wrong_pool_before_signing():
    okx = FakeOkxSwapClient()
    okx.calldata_overrides["data"] = _uniswap_v3_dynamic_data(WALLET, 1000, 990, "0x" + "4" * 40)
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == "calldata_mismatch"
    assert not signer.transactions
    assert not broadcaster.sent


def test_wrong_chain_is_rejected_fail_closed():
    execution, _, broadcaster, _ = make_execution_adapter()

    result = execution.buy(TOKEN, POOL, "1000", "1", chain_id=1, from_token_address=NATIVE)

    assert result["ok"] is False
    assert result["error"] == "wrong_chain"
    assert not broadcaster.sent


@pytest.mark.parametrize("chain_id", ["bsc", 56.9, "56.9", True])
def test_chain_id_is_strictly_bsc_integer_or_string_56(chain_id: Any):
    execution, _, broadcaster, _ = make_execution_adapter()

    result = execution.buy(TOKEN, POOL, "1000", "1", chain_id=chain_id, from_token_address=NATIVE)

    assert result == {"ok": False, "status": "rejected", "error": "wrong_chain"}
    assert not broadcaster.sent


def test_pool_is_rejected_when_okx_builder_cannot_verify_pool_context():
    execution, _, broadcaster, _ = make_execution_adapter(okx=LegacyOkxSwapClient())

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == "pool_unverifiable"
    assert not broadcaster.sent


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("from", "0x" + "9" * 40, "transaction_from_mismatch"),
        ("to", "0x" + "8" * 40, "router_not_allowed"),
        ("data", "0x", "invalid_calldata"),
        ("value", -1, "invalid_value"),
        ("value", 0.0, "invalid_value"),
    ],
)
def test_transaction_semantics_are_validated_before_signing(field: str, value: Any, expected: str):
    okx = FakeOkxSwapClient()
    okx.tx_overrides[field] = value
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == expected
    assert not signer.transactions
    assert not broadcaster.sent


def test_transaction_value_is_bound_to_native_amount_and_accepts_numeric_equivalence():
    okx = FakeOkxSwapClient()
    okx.tx_overrides["value"] = "0x3e8"
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["ok"] is True
    assert result["status"] == "confirmed"
    assert signer.transactions
    assert broadcaster.sent == [RAW_TX]


def test_native_transaction_value_mismatch_is_rejected_before_signing():
    okx = FakeOkxSwapClient()
    okx.tx_overrides["value"] = "999"
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == "value_mismatch"
    assert not signer.transactions
    assert not broadcaster.sent


def test_erc20_transaction_value_must_be_zero():
    okx = FakeOkxSwapClient()
    okx.tx_overrides["value"] = "0x1"
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=ERC20_IN)

    assert result["error"] == "value_mismatch"
    assert not signer.transactions
    assert not broadcaster.sent


def test_non_mapping_or_non_strict_chain_transaction_is_rejected():
    okx = FakeOkxSwapClient()
    okx.tx_overrides = {"chainId": "bsc"}
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == "wrong_chain"
    assert not signer.transactions
    assert not broadcaster.sent


def test_okx_non_mapping_transaction_is_rejected_before_signing():
    okx = FakeOkxSwapClient()
    okx.return_non_mapping_tx = True
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == "invalid_transaction"
    assert not signer.transactions
    assert not broadcaster.sent


@pytest.mark.parametrize("signer", [ReplacingRawSigner(), MissingVerifySigner()])
def test_raw_transaction_must_be_verified_before_broadcast(signer: FakeSigner):
    execution, _, broadcaster, _ = make_execution_adapter(signer=signer)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result == {"ok": False, "status": "error", "error": "invalid_signed_transaction"}
    assert signer.transactions
    assert not broadcaster.sent


def test_local_signer_verifies_raw_transaction_against_unsigned_transaction():
    signer = LocalEvmSigner(
        wallet_address=WALLET,
        private_key=PRIVATE_KEY,
        account_module=SimpleNamespace(Account=FakeAccountApi),
    )
    transaction = {"chainId": 56, "from": WALLET}

    assert signer.verify_signed_transaction("0x1234", transaction) is True
    assert signer.verify_signed_transaction("0x1235", transaction) is False


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("token_in", "0x" + "9" * 40, "calldata_mismatch"),
        ("pool", "0x" + "9" * 40, "calldata_mismatch"),
        ("recipient", "0x" + "9" * 40, "calldata_mismatch"),
        ("amount", 1001, "calldata_mismatch"),
        ("min_out", 1, "calldata_mismatch"),
        ("direction", 1, "calldata_mismatch"),
    ],
)
def test_calldata_fields_are_bound_to_request_context(field: str, value: Any, expected: str):
    execution, signer, broadcaster, okx = make_execution_adapter()
    values: list[Any] = [NATIVE, TOKEN, POOL, WALLET, "1000", 990, 0]
    values[{"token_in": 0, "pool": 2, "recipient": 3, "amount": 4, "min_out": 5, "direction": 6}[field]] = value
    okx.calldata_overrides["data"] = SWAP_SELECTOR + "".join(okx._word(item) for item in values)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == expected
    assert not signer.transactions
    assert not broadcaster.sent


def test_unsupported_calldata_selector_fails_closed_before_signing():
    okx = FakeOkxSwapClient()
    okx.calldata_overrides["data"] = "0xdeadbeef" + "00" * 7 * 32
    execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["error"] == "calldata_unsupported"
    assert not signer.transactions
    assert not broadcaster.sent


def test_forged_prepared_dict_cannot_reach_signer_or_broadcaster():
    execution, signer, broadcaster, _ = make_execution_adapter()

    result = execution._execute({"ok": True, "transaction": {}, "request_context": {}})

    assert result["error"] == "prepared_invalid"
    assert not signer.transactions
    assert not broadcaster.sent


def test_okx_context_mismatch_rejects_token_amount_slippage_or_pool():
    for field, value in (
        ("token_address", "0x" + "9" * 40),
        ("amount_atomic", "1001"),
        ("slippage_percent", "2"),
        ("pool_address", "0x" + "9" * 40),
    ):
        okx = FakeOkxSwapClient()
        okx.context_overrides[field] = value
        execution, signer, broadcaster, _ = make_execution_adapter(okx=okx)

        result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

        assert result["error"] == "okx_context_mismatch"
        assert not signer.transactions
        assert not broadcaster.sent


def test_execute_rechecks_live_flag_and_armed_before_dependencies():
    environment = {"LIVE_TRADING_ENABLED": "true"}
    execution, signer, broadcaster, _ = make_execution_adapter(environment=environment, armed=False)
    prepared = {
        "ok": True,
        "transaction": {"chainId": 56},
        "request_context": {},
    }

    result = execution._execute(prepared)

    assert result == {"ok": False, "status": "rejected", "error": "live_disabled"}
    assert not signer.transactions
    assert not broadcaster.sent

    execution.armed = True
    environment["LIVE_TRADING_ENABLED"] = "false"
    result = execution._execute(prepared)

    assert result == {"ok": False, "status": "rejected", "error": "live_disabled"}
    assert not signer.transactions
    assert not broadcaster.sent


def test_local_signer_rejects_wallet_address_mismatch():
    with pytest.raises(AdapterError) as error:
        LocalEvmSigner(
            wallet_address="0x" + "9" * 40,
            private_key=PRIVATE_KEY,
            account_module=SimpleNamespace(Account=FakeAccountApi),
        )

    assert error.value.code == "address_mismatch"
    assert PRIVATE_KEY not in str(error.value)


def test_local_signer_reports_stable_missing_dependency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(adapter_module, "_load_eth_account", lambda: None)

    with pytest.raises(AdapterError) as error:
        LocalEvmSigner(wallet_address=WALLET, private_key=PRIVATE_KEY)

    assert error.value.code == "missing_eth_account"
    assert PRIVATE_KEY not in str(error.value)


def test_json_rpc_broadcaster_sends_only_signed_raw_transaction():
    calls: list[dict[str, Any]] = []

    def rpc(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        return {"jsonrpc": "2.0", "id": payload["id"], "result": TX_HASH}

    broadcaster = JsonRpcBroadcaster("http://fake-bsc", transport=rpc)

    result = broadcaster.send_raw_transaction(RAW_TX)

    assert result == TX_HASH
    assert calls[0]["payload"]["method"] == "eth_sendRawTransaction"
    assert calls[0]["payload"]["params"] == [RAW_TX]


@pytest.mark.parametrize("returned_hash", ["0x1234", "0X" + "b" * 64, PRIVATE_KEY + "x", "b" * 64])
def test_json_rpc_broadcaster_rejects_noncanonical_transaction_hash(returned_hash: str):
    broadcaster = JsonRpcBroadcaster(
        "http://fake-bsc",
        transport=lambda url, payload, timeout: {"result": returned_hash},
    )

    assert broadcaster.send(RAW_TX) is None
    assert broadcaster.last_error == {"code": "invalid_transaction_hash"}


def test_json_rpc_receipt_timeout_returns_none_and_structured_error():
    def rpc(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert payload["method"] == "eth_getTransactionReceipt"
        return {"jsonrpc": "2.0", "id": payload["id"], "result": None}

    broadcaster = JsonRpcBroadcaster("http://fake-bsc", transport=rpc)

    result = broadcaster.receipt(TX_HASH, timeout_seconds=0)

    assert result is None
    assert broadcaster.last_error == {"code": "receipt_timeout"}


def test_json_rpc_receipt_zero_timeout_does_not_call_rpc():
    calls: list[dict[str, Any]] = []
    broadcaster = JsonRpcBroadcaster(
        "http://fake-bsc",
        transport=lambda url, payload, timeout: calls.append(payload) or {"result": None},
    )

    assert broadcaster.receipt(TX_HASH, timeout_seconds=0) is None
    assert not calls
    assert broadcaster.last_error == {"code": "receipt_timeout"}


def test_json_rpc_receipt_call_timeout_never_exceeds_remaining_deadline(monkeypatch: pytest.MonkeyPatch):
    current = [0.0]
    calls: list[float] = []

    def monotonic() -> float:
        return current[0]

    def sleep(seconds: float) -> None:
        current[0] += seconds

    def rpc(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append(timeout)
        current[0] += 0.6
        return {"result": None}

    monkeypatch.setattr(adapter_module.time, "monotonic", monotonic)
    monkeypatch.setattr(adapter_module.time, "sleep", sleep)
    broadcaster = JsonRpcBroadcaster("http://fake-bsc", transport=rpc, poll_interval_seconds=0.1)

    assert broadcaster.receipt(TX_HASH, timeout_seconds=1) is None
    assert calls[0] == pytest.approx(1.0)
    assert calls[1] <= 0.4 + 1e-9
    assert broadcaster.last_error == {"code": "receipt_timeout"}


def test_json_rpc_receipt_redacts_invalid_safe_fields_and_arbitrary_strings():
    broadcaster = JsonRpcBroadcaster(
        "http://fake-bsc",
        transport=lambda url, payload, timeout: {
            "result": {
                "status": "confirmed",
                "blockHash": "0x" + "c" * 65,
                "blockNumber": PRIVATE_KEY,
                "opaque": PRIVATE_KEY,
            }
        },
    )

    result = broadcaster.receipt(TX_HASH, timeout_seconds=1)

    assert result == {"opaque": "[REDACTED]"}
    assert PRIVATE_KEY not in repr(result)


def test_redaction_removes_private_key_raw_transaction_and_okx_material():
    result = redact_transaction({
        "private_key": PRIVATE_KEY,
        "raw_transaction": RAW_TX,
        "signature": "sig",
        "okx_secret": "secret",
        "transaction": {"data": "0xabc"},
        "nested": {"OK-ACCESS-SIGN": "sig2", "value": 1},
    })

    rendered = repr(result)
    assert PRIVATE_KEY not in rendered
    assert RAW_TX not in rendered
    assert "sig" not in rendered
    assert "secret" not in rendered
    assert result == {"nested": {"value": 1}}


def test_redaction_scrubs_secret_strings_under_arbitrary_fields_and_receipts():
    result = redact_transaction({
        "arbitrary": PRIVATE_KEY,
        "raw_payload": "0x12",
        "opaque": "Q2hhbmdlTWUtaW50ZW50aW9uYWxseQ==",
        "receipt": {"opaque": RAW_TX, "credential": "very-secret-value", "tx_hash": TX_HASH},
    })

    rendered = repr(result)
    assert PRIVATE_KEY not in rendered
    assert RAW_TX not in rendered
    assert "0x12" not in rendered
    assert "Q2hhbmdlTWUtaW50ZW50aW9uYWxseQ==" not in rendered
    assert "very-secret-value" not in rendered
    assert TX_HASH in rendered


def test_redaction_keeps_only_valid_transaction_hash_fields():
    result = redact_transaction({
        "tx_hash": TX_HASH,
        "transactionHash": "not-a-hash",
        "nested": {
            "tx_hash": PRIVATE_KEY + "x",
            "transactionHash": "0x" + "c" * 64,
        },
    })

    assert result["tx_hash"] == TX_HASH
    assert "transactionHash" not in result
    assert "tx_hash" not in result["nested"]
    assert result["nested"]["transactionHash"] == "0x" + "c" * 64


def test_redaction_validates_and_normalizes_receipt_safe_fields():
    result = redact_transaction({
        "status": "0x01",
        "blockHash": "0x" + "c" * 65,
        "blockNumber": PRIVATE_KEY,
        "nested": {
            "status": "0x1",
            "blockHash": "0x" + "d" * 64,
            "blockNumber": "0x0010",
        },
    })

    assert "status" not in result
    assert "blockHash" not in result
    assert "blockNumber" not in result
    assert result["nested"] == {
        "status": "0x1",
        "blockHash": "0x" + "d" * 64,
        "blockNumber": "0x10",
    }


def test_redaction_does_not_trust_safe_strings_and_filters_request_context():
    result = redact_transaction({
        "error": PRIVATE_KEY,
        "token_address": PRIVATE_KEY,
        "amount_atomic": PRIVATE_KEY,
        "chain_id": PRIVATE_KEY,
        "request_context": {
            "chain_id": 56,
            "token_address": TOKEN,
            "pool_address": POOL,
            "amount_atomic": "1000",
            "slippage_percent": "1",
            "from_token_address": NATIVE,
            "target_token_address": TOKEN,
            "quoted_out": "990",
            "min_out": "980",
            "extra": "do-not-return",
            "private_key": PRIVATE_KEY,
        },
    })

    assert "error" not in result
    assert "token_address" not in result
    assert "amount_atomic" not in result
    assert "chain_id" not in result
    assert result["request_context"] == {
        "chain_id": 56,
        "token_address": TOKEN.lower(),
        "pool_address": POOL.lower(),
        "amount_atomic": "1000",
        "slippage_percent": "1",
        "from_token_address": NATIVE.lower(),
        "target_token_address": TOKEN.lower(),
        "quoted_out": "990",
        "min_out": "980",
    }


def test_tampering_with_sealed_prepared_execution_is_rejected():
    execution, signer, broadcaster, _ = make_execution_adapter()
    prepared = execution._prepare(
        "buy", TOKEN, POOL, "1000", "1", chain_id=56, counter_token=NATIVE
    )
    assert isinstance(prepared, adapter_module._PreparedExecution)
    prepared.transaction["data"] = "0x12"

    result = execution._execute(prepared)

    assert result["error"] == "prepared_invalid"
    assert not signer.transactions
    assert not broadcaster.sent


def test_buy_and_sell_happy_paths_sign_broadcast_and_confirm():
    execution, signer, broadcaster, okx = make_execution_adapter()

    buy = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)
    sell = execution.sell(TOKEN, POOL, "250", "2", to_token_address=NATIVE)

    assert buy["ok"] is True
    assert buy["status"] == "confirmed"
    assert buy["tx_hash"] == TX_HASH
    assert sell["ok"] is True
    assert sell["status"] == "confirmed"
    assert [call["from_token"] for call in okx.calls] == [NATIVE, TOKEN]
    assert len(signer.transactions) == 2
    assert len(signer.verifications) == 2
    assert "private_key" not in signer.transactions[0]
    assert "unknown_upstream_field" not in signer.transactions[0]
    assert broadcaster.sent == [RAW_TX, RAW_TX]
    assert "transaction" not in buy
    assert RAW_TX not in repr(buy)
    assert PRIVATE_KEY not in repr(buy)


def test_receipt_timeout_preserves_hash_and_pending_state():
    execution, signer, broadcaster, _ = make_execution_adapter(receipt=None)

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["ok"] is False
    assert result["status"] == "pending"
    assert result["error"] == "receipt_timeout"
    assert result["tx_hash"] == TX_HASH
    assert signer.transactions
    assert broadcaster.sent == [RAW_TX]


def test_receipt_error_preserves_hash_and_unknown_state():
    execution, _, broadcaster, _ = make_execution_adapter(receipt_error=RuntimeError(PRIVATE_KEY))

    result = execution.buy(TOKEN, POOL, "1000", "1", from_token_address=NATIVE)

    assert result["ok"] is False
    assert result["status"] == "unknown"
    assert result["error"] == "receipt_failed"
    assert result["tx_hash"] == TX_HASH
    assert PRIVATE_KEY not in repr(result)
    assert RAW_TX not in repr(result)
