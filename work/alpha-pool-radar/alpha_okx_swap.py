"""Read-only construction of OKX DEX quote and swap transactions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode

HOST = "https://web3.okx.com"
CHAIN_BSC = "56"
QUOTE_PATH = "/api/v5/dex/aggregator/quote"
APPROVE_PATH = "/api/v5/dex/aggregator/approve-transaction"
SWAP_PATH = "/api/v5/dex/aggregator/swap"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_STATIC_WORD_COUNT = 7
_OKX_DYNAMIC_SELECTORS = frozenset({
    "e99bfa95", "591b3d08", "b80c2f09", "03b87e5f", "44014e98",
    "b8815477", "0d5f0e3b", "08298b5a", "9871efa4", "0c307f76",
})


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str],
        body: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Mapping[str, Any]: ...


class Signer(Protocol):
    def sign(self, transaction: Mapping[str, Any]) -> str: ...


class Broadcaster(Protocol):
    def send(self, raw_transaction: str) -> str: ...

    def receipt(self, tx_hash: str, timeout_seconds: int) -> dict[str, Any] | None: ...


def _credential(credentials: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = credentials.get(name)
        if isinstance(value, str):
            return value
    return ""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact_transaction(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return transaction-shaped data with credentials and signing material removed."""
    secret_names = {
        "privatekey", "secret", "secretkey", "signature", "rawtransaction",
        "authorization", "okaccesssign", "okaccesssecretkey", "apikey",
        "passphrase", "mnemonic", "seed",
    }

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key).replace("-", "").replace("_", "").lower() not in secret_names
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    result = clean(payload)
    return result if isinstance(result, dict) else {}


def _amount_text(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("invalid_amount")
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("invalid_amount")
    return text


def _slippage_text(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("invalid_slippage")
    text = str(value).strip()
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_slippage") from None
    if not parsed.is_finite() or parsed < 0 or parsed > 50:
        raise ValueError("invalid_slippage")
    return text


def _address_text(value: Any, error: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _ADDRESS_RE.fullmatch(text):
        raise ValueError(error)
    return text


def _min_out(quoted_out: str, slippage_percent: str) -> str:
    try:
        quoted = int(quoted_out, 10)
        slippage = Decimal(slippage_percent)
        result = int(Decimal(quoted) * (Decimal("100") - slippage) / Decimal("100"))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_quote") from None
    if quoted <= 0 or result <= 0 or result > quoted:
        raise ValueError("invalid_quote")
    return str(result)


def _is_static_seven_word_candidate(calldata: Any) -> bool:
    """Only identify the fixed-size shape; the adapter owns ABI semantics."""
    if not isinstance(calldata, str) or not _HEX_RE.fullmatch(calldata):
        return False
    body = calldata[2:]
    return len(body) == 8 + _STATIC_WORD_COUNT * 64


def _is_supported_calldata(calldata: Any) -> bool:
    if _is_static_seven_word_candidate(calldata):
        return True
    if not isinstance(calldata, str) or not _HEX_RE.fullmatch(calldata) or len(calldata) < 10:
        return False
    body = calldata[2:]
    return body[:8].lower() in _OKX_DYNAMIC_SELECTORS and (len(body) - 8) % 64 == 0


def _failure(
    params: Mapping[str, str],
    request_context: Mapping[str, Any],
    error: str,
    *,
    route: Any = None,
    price_impact: Any = None,
    status: str = "error",
) -> dict[str, Any]:
    safe_route = (
        {key: value for key, value in route.items() if key != "tx"}
        if isinstance(route, Mapping)
        else None
    )
    return {
        **dict(params),
        "ok": False,
        "status": status,
        "route": safe_route,
        "quote_age": None,
        "price_impact": price_impact,
        "tx": None,
        "request_context": dict(request_context),
        "error": error,
    }


def _response_row(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if str(response.get("code")) != "0":
        return None
    rows = response.get("data")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return None
    return rows[0]


class OkxSwapClient:
    # The contextual builder recognizes the official dynamic router family;
    # the live adapter performs the stronger field-level validation.
    supports_dynamic_calldata = True

    def __init__(self, http: HttpTransport, credentials: Mapping[str, Any]):
        self.http = http
        self.credentials = credentials

    def _headers(self, method: str, target: str, body: str = "") -> dict[str, str]:
        timestamp = _timestamp()
        secret = _credential(self.credentials, "secret_key", "secretKey", "OKX_SECRET_KEY")
        message = timestamp + method + target + body
        signature = base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OK-ACCESS-KEY": _credential(self.credentials, "api_key", "apiKey", "OKX_API_KEY"),
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-PASSPHRASE": _credential(self.credentials, "passphrase", "OKX_PASSPHRASE"),
            "OK-ACCESS-TIMESTAMP": timestamp,
        }

    def _request(self, path: str, params: dict[str, str]) -> Mapping[str, Any]:
        query = "?" + urlencode(params)
        return self.http.request("GET", path, params=params, headers=self._headers("GET", path + query))

    @staticmethod
    def _result(response: Mapping[str, Any], params: Mapping[str, str], *, transaction: Any = None) -> dict[str, Any]:
        if str(response.get("code")) != "0":
            return {
                **dict(params), "ok": False, "status": "error", "route": None,
                "quote_age": None, "price_impact": None, "tx": None,
                "error": "okx_api_" + str(response.get("code", "invalid_response")),
            }
        rows = response.get("data")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
            return {
                **dict(params), "ok": False, "status": "error", "route": None,
                "quote_age": None, "price_impact": None, "tx": None,
                "error": "invalid_okx_data",
            }
        row = rows[0]
        return {
            **dict(params), "ok": True, "status": "ok", "route": row,
            "quote_age": row.get("quoteAge") if row.get("quoteAge") is not None else row.get("quote_age"),
            "price_impact": (
                row.get("priceImpactPercentage")
                if row.get("priceImpactPercentage") is not None
                else row.get("priceImpact") if row.get("priceImpact") is not None
                else row.get("price_impact")
            ),
            "tx": transaction if transaction is not None else None, "error": None,
        }

    def quote_bsc(self, token_address: str, amount_atomic: str, from_token: str, slippage_percent: str) -> dict[str, Any]:
        params = {
            "chainIndex": CHAIN_BSC,
            "amount": str(amount_atomic),
            "fromTokenAddress": from_token,
            "toTokenAddress": token_address,
            "slippagePercent": str(slippage_percent),
        }
        try:
            return self._result(self._request(QUOTE_PATH, params), params)
        except Exception as exc:
            return {**params, "ok": False, "status": "error", "route": None, "quote_age": None,
                    "price_impact": None, "tx": None, "error": type(exc).__name__}

    def approve_bsc(self, token_address: str, amount_atomic: str, from_token: str) -> dict[str, Any]:
        params = {"chainIndex": CHAIN_BSC, "tokenContractAddress": from_token, "approveAmount": str(amount_atomic)}
        try:
            response = self._request(APPROVE_PATH, params)
            rows = response.get("data") if isinstance(response.get("data"), list) else []
            return self._result(response, params, transaction=rows[0] if rows else None)
        except Exception as exc:
            return {**params, "ok": False, "status": "error", "route": None, "quote_age": None,
                    "price_impact": None, "tx": None, "error": type(exc).__name__}

    def build_swap_bsc(self, token_address: str, amount_atomic: str, from_token: str, slippage_percent: str, wallet_address: str) -> dict[str, Any]:
        params = {
            "chainIndex": CHAIN_BSC, "amount": str(amount_atomic),
            "fromTokenAddress": from_token, "toTokenAddress": token_address,
            "slippage": str(slippage_percent), "userWalletAddress": wallet_address,
        }
        try:
            response = self._request(SWAP_PATH, params)
            rows = response.get("data") if isinstance(response.get("data"), list) else []
            tx = rows[0].get("tx") if rows and isinstance(rows[0], Mapping) else None
            result = self._result(response, params, transaction=tx)
            if result.get("ok") is True and not _is_supported_calldata(tx.get("data") if isinstance(tx, Mapping) else None):
                route = result.get("route")
                safe_route = (
                    {key: value for key, value in route.items() if key != "tx"}
                    if isinstance(route, Mapping)
                    else None
                )
                return {**result, "ok": False, "status": "rejected", "route": safe_route, "tx": None, "error": "calldata_unsupported"}
            return result
        except Exception as exc:
            return {**params, "ok": False, "status": "error", "route": None, "quote_age": None,
                    "price_impact": None, "tx": None, "error": type(exc).__name__}

    def build_swap_bsc_with_context(
        self,
        token_address: str,
        amount_atomic: str,
        from_token: str,
        slippage_percent: str,
        wallet_address: str,
        pool_address: str,
        chain_id: int | str = CHAIN_BSC,
    ) -> dict[str, Any]:
        """Build only an adapter-compatible static candidate from OKX's tx.

        OKX returns the complete EVM transaction in ``data[0].tx``.  Keep that
        mapping intact, including gas fields and signatureData, and add the
        chainId required by the local signer when OKX omits it.  The returned
        request_context is the binding between quote, pool, and swap request.
        Dynamic or otherwise non-fixed calldata is never promoted to a
        prepared transaction: the existing adapter's seven-word ABI is the
        only semantic validator for a static candidate.
        """
        base_params = {
            "chainIndex": str(chain_id),
            "amount": str(amount_atomic),
            "fromTokenAddress": from_token,
            "toTokenAddress": token_address,
            "slippage": str(slippage_percent),
            "userWalletAddress": wallet_address,
        }
        request_context: dict[str, Any] = {
            "chain_id": int(CHAIN_BSC),
            "token_address": token_address,
            "amount_atomic": str(amount_atomic),
            "slippage_percent": str(slippage_percent),
            "pool_address": pool_address,
            "from_token_address": from_token,
            "target_token_address": token_address,
        }
        try:
            if str(chain_id) != CHAIN_BSC:
                raise ValueError("wrong_chain")
            amount = _amount_text(amount_atomic)
            slippage = _slippage_text(slippage_percent)
            token = _address_text(token_address, "invalid_token")
            source = _address_text(from_token, "invalid_counter_token")
            wallet = _address_text(wallet_address, "invalid_wallet_address")
            pool = _address_text(pool_address, "invalid_pool")
        except ValueError as exc:
            return _failure(base_params, request_context, str(exc), status="rejected")

        request_context.update({
            "chain_id": int(CHAIN_BSC),
            "token_address": token,
            "amount_atomic": amount,
            "slippage_percent": slippage,
            "pool_address": pool,
            "from_token_address": source,
            "target_token_address": token,
        })
        quote_params = {
            "chainIndex": CHAIN_BSC,
            "amount": amount,
            "fromTokenAddress": source,
            "toTokenAddress": token,
            "slippagePercent": slippage,
        }
        try:
            quote_response = self._request(QUOTE_PATH, quote_params)
            quote_row = _response_row(quote_response)
            if quote_row is None:
                error = "okx_api_" + str(quote_response.get("code", "invalid_response"))
                if str(quote_response.get("code")) == "0":
                    error = "invalid_okx_data"
                return _failure(base_params, request_context, error)
            quoted_out = quote_row.get("toTokenAmount")
            if not isinstance(quoted_out, str) or not quoted_out.isdigit() or int(quoted_out) <= 0:
                return _failure(base_params, request_context, "invalid_quote", route=quote_row)
            from_amount = quote_row.get("fromTokenAmount")
            if from_amount is not None and str(from_amount) != amount:
                return _failure(base_params, request_context, "quote_context_mismatch", route=quote_row)
            try:
                minimum_out = _min_out(quoted_out, slippage)
            except ValueError as exc:
                return _failure(base_params, request_context, str(exc), route=quote_row)
            request_context.update({"quoted_out": quoted_out, "min_out": minimum_out})

            swap_response = self._request(SWAP_PATH, base_params)
            swap_row = _response_row(swap_response)
            if swap_row is None:
                error = "okx_api_" + str(swap_response.get("code", "invalid_response"))
                if str(swap_response.get("code")) == "0":
                    error = "invalid_okx_data"
                return _failure(base_params, request_context, error, route=quote_row)
            upstream_tx = swap_row.get("tx")
            if not isinstance(upstream_tx, Mapping):
                return _failure(base_params, request_context, "invalid_transaction", route=swap_row)
            tx = dict(upstream_tx)
            if "chainId" not in tx:
                tx["chainId"] = int(CHAIN_BSC)
            if str(tx.get("chainId")) != CHAIN_BSC:
                return _failure(base_params, request_context, "wrong_chain", route=swap_row)
            if not all(isinstance(tx.get(field), str) and tx[field].strip() for field in ("from", "to", "data", "value")):
                return _failure(base_params, request_context, "invalid_transaction", route=swap_row)
            if not _ADDRESS_RE.fullmatch(tx["from"].strip()) or not _ADDRESS_RE.fullmatch(tx["to"].strip()):
                return _failure(base_params, request_context, "invalid_transaction", route=swap_row)
            if tx["from"].strip().lower() != wallet.lower():
                return _failure(base_params, request_context, "transaction_from_mismatch", route=swap_row)
            if not _is_supported_calldata(tx["data"]):
                return _failure(
                    base_params,
                    request_context,
                    "calldata_unsupported",
                    route=swap_row,
                    price_impact=quote_row.get("priceImpactPercentage"),
                    status="rejected",
                )
            return {
                **base_params,
                "ok": True,
                "status": "ok",
                "route": swap_row,
                "quote_age": None,
                "price_impact": quote_row.get("priceImpactPercentage"),
                "tx": tx,
                "request_context": request_context,
                "error": None,
            }
        except Exception as exc:
            return _failure(base_params, request_context, type(exc).__name__)
