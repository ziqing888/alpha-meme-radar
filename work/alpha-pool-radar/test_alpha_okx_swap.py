from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_okx_swap import OkxSwapClient, redact_transaction


WBNB = "0x" + "b" * 40
TEST_CREDENTIALS = {
    "api_key": "test-key",
    "secret_key": "test-secret",
    "passphrase": "test-passphrase",
}


class FakeHttp:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, *, params: dict[str, str], body: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, "params": params, "body": body, "headers": headers or {}})
        return self.payload


def test_quote_request_uses_bsc_chain_and_smallest_units():
    http = FakeHttp({"code": "0", "data": [{"toTokenAmount": "123"}]})
    client = OkxSwapClient(http=http, credentials=TEST_CREDENTIALS)

    result = client.quote_bsc("0x" + "1" * 40, "5000000000000000000", WBNB, "3")

    assert result["ok"] is True
    assert result["chainIndex"] == "56"
    assert result["amount"] == "5000000000000000000"
    assert http.calls[0]["path"].endswith("/quote")
    assert http.calls[0]["params"]["chainIndex"] == "56"
    assert http.calls[0]["params"]["amount"] == "5000000000000000000"


def test_swap_builder_rejects_non_static_calldata():
    http = FakeHttp({"code": "0", "data": [{"tx": {"data": "0xabc"}}]})
    client = OkxSwapClient(http=http, credentials=TEST_CREDENTIALS)

    result = client.build_swap_bsc("0x" + "1" * 40, "5000000000000000000", WBNB, "3", "0x" + "2" * 40)

    assert result == {
        "chainIndex": "56",
        "amount": "5000000000000000000",
        "fromTokenAddress": WBNB,
        "toTokenAddress": "0x" + "1" * 40,
        "slippage": "3",
        "userWalletAddress": "0x" + "2" * 40,
        "ok": False,
        "status": "rejected",
        "route": {},
        "quote_age": None,
        "price_impact": None,
        "tx": None,
        "error": "calldata_unsupported",
    }
    assert http.calls[0]["params"]["slippage"] == "3"
    assert "slippagePercent" not in http.calls[0]["params"]
    assert all(call["path"].endswith("/swap") for call in http.calls)


class SequenceHttp(FakeHttp):
    def __init__(self, *payloads: dict[str, Any]):
        super().__init__(payloads[0])
        self.payloads = list(payloads)

    def request(self, method: str, path: str, *, params: dict[str, str], body: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        self.calls.append({"method": method, "path": path, "params": params, "body": body, "headers": headers or {}})
        return self.payloads.pop(0)


def _static_candidate_data() -> str:
    return "0x12345678" + "00" * (7 * 32)


def _official_tx(data: str) -> dict[str, Any]:
    return {
        "data": data,
        "from": "0x" + "2" * 40,
        "gas": "202500",
        "gasPrice": "32657616776",
        "maxPriorityFeePerGas": "2086453233",
        "maxSpendAmount": "100000000",
        "minReceiveAmount": "970",
        "signatureData": [""],
        "to": "0x" + "5" * 40,
        "value": "5000000000000000000",
    }


def test_contextual_builder_preserves_official_tx_and_complete_request_context():
    http = SequenceHttp(
        {"code": "0", "data": [{"chainIndex": "56", "fromTokenAmount": "5000000000000000000", "toTokenAmount": "1000"}]},
        {"code": "0", "data": [{"tx": _official_tx(_static_candidate_data()), "routerResult": {"route": "official"}}]},
    )
    client = OkxSwapClient(http=http, credentials=TEST_CREDENTIALS)

    result = client.build_swap_bsc_with_context(
        token_address="0x" + "1" * 40,
        amount_atomic="5000000000000000000",
        from_token=WBNB,
        slippage_percent="3",
        wallet_address="0x" + "2" * 40,
        pool_address="0x" + "4" * 40,
        chain_id=56,
    )

    assert result["ok"] is True
    assert result["request_context"] == {
        "chain_id": 56,
        "token_address": "0x" + "1" * 40,
        "amount_atomic": "5000000000000000000",
        "slippage_percent": "3",
        "pool_address": "0x" + "4" * 40,
        "from_token_address": WBNB,
        "target_token_address": "0x" + "1" * 40,
        "quoted_out": "1000",
        "min_out": "970",
    }
    assert result["tx"]["data"] == _static_candidate_data()
    assert result["tx"]["signatureData"] == [""]
    assert result["tx"]["chainId"] == 56
    assert [call["path"] for call in http.calls] == ["/api/v5/dex/aggregator/quote", "/api/v5/dex/aggregator/swap"]
    assert http.calls[1]["params"]["slippage"] == "3"


def test_contextual_builder_rejects_unknown_calldata_and_keeps_context():
    http = SequenceHttp(
        {"code": "0", "data": [{"fromTokenAmount": "5000000000000000000", "toTokenAmount": "1000"}]},
        {"code": "0", "data": [{"tx": _official_tx("0xdeadbeef" + "00" * (8 * 32))}]},
    )
    client = OkxSwapClient(http=http, credentials=TEST_CREDENTIALS)

    result = client.build_swap_bsc_with_context(
        "0x" + "1" * 40, "5000000000000000000", WBNB, "3", "0x" + "2" * 40, "0x" + "4" * 40
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert result["error"] == "calldata_unsupported"
    assert result["tx"] is None
    assert "tx" not in result["route"]
    assert result["request_context"]["quoted_out"] == "1000"
    assert result["request_context"]["min_out"] == "970"


def test_contextual_builder_rejects_api_or_input_failures_before_swap():
    http = SequenceHttp({"code": "50014", "msg": "bad quote"})
    client = OkxSwapClient(http=http, credentials=TEST_CREDENTIALS)

    result = client.build_swap_bsc_with_context(
        "0x" + "1" * 40, "1", WBNB, "3", "0x" + "2" * 40, "0x" + "4" * 40
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"] == "okx_api_50014"
    assert len(http.calls) == 1

    invalid = client.build_swap_bsc_with_context(
        "not-an-address", "1", WBNB, "3", "0x" + "2" * 40, "0x" + "4" * 40
    )
    assert invalid["status"] == "rejected"
    assert invalid["error"] == "invalid_token"


def test_error_response_is_structured_and_redaction_removes_secrets():
    client = OkxSwapClient(http=FakeHttp({"code": "50014", "msg": "bad quote"}), credentials=TEST_CREDENTIALS)

    result = client.quote_bsc("0x" + "1" * 40, "1", WBNB, "3")

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error"] == "okx_api_50014"
    assert redact_transaction({
        "privateKey": "secret", "PRIVATE_KEY": "secret", "private-key": "secret",
        "OK-ACCESS-SECRET-KEY": "secret", "ok_access_secret_key": "secret",
        "OK-ACCESS-SIGN": "sig", "ok_access_sign": "sig", "data": "0xabc",
    }) == {"data": "0xabc"}


def test_requests_are_signed_without_exposing_secret_header():
    http = FakeHttp({"code": "0", "data": [{}]})
    client = OkxSwapClient(http=http, credentials=TEST_CREDENTIALS)

    client.quote_bsc("0x" + "1" * 40, "1", WBNB, "3")

    headers = http.calls[0]["headers"]
    assert headers["OK-ACCESS-KEY"] == "test-key"
    assert headers["OK-ACCESS-PASSPHRASE"] == "test-passphrase"
    assert headers["OK-ACCESS-SIGN"]
    assert "secret" not in str(headers)


def test_price_impact_percentage_maps_to_structured_price_impact():
    client = OkxSwapClient(http=FakeHttp({
        "code": "0", "data": [{"priceImpactPercentage": "1.25"}],
    }), credentials=TEST_CREDENTIALS)

    result = client.quote_bsc("0x" + "1" * 40, "1", WBNB, "3")

    assert result["ok"] is True
    assert result["price_impact"] == "1.25"


def test_success_code_with_empty_or_invalid_data_is_structured_error():
    for data in ([], ["not-a-row"]):
        client = OkxSwapClient(http=FakeHttp({"code": "0", "data": data}), credentials=TEST_CREDENTIALS)

        result = client.quote_bsc("0x" + "1" * 40, "1", WBNB, "3")

        assert result["ok"] is False
        assert result["status"] == "error"
        assert result["error"] == "invalid_okx_data"
