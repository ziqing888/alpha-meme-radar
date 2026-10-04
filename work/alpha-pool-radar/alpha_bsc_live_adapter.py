"""Fail-closed BSC live execution boundary.

The adapter accepts an already constructed OKX swap client and keeps signing
and broadcast concerns separate.  It has no approval flow and never exposes
signing material in returned structures or exception messages.
"""

from __future__ import annotations

import json
import hmac
import math
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from alpha_okx_swap import OkxSwapClient


CHAIN_ID_BSC = 56
BSC_NATIVE_TOKEN = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_PUBLIC_ERROR_CODES = frozenset({
    "address_mismatch", "broadcast_failed", "invalid_amount", "invalid_calldata",
    "invalid_counter_token", "invalid_derived_address", "invalid_private_key",
    "invalid_pool", "invalid_raw_transaction", "invalid_router_config",
    "invalid_rpc_response", "invalid_signed_transaction", "invalid_slippage",
    "invalid_token", "invalid_transaction", "invalid_transaction_from",
    "invalid_transaction_hash", "invalid_value", "invalid_wallet_address",
    "live_disabled", "missing_eth_account", "missing_private_key",
    "missing_rpc_url", "missing_unsigned_transaction", "okx_build_failed",
    "okx_context_mismatch", "opaque_error", "pool_unverifiable", "prepared_invalid",
    "receipt_failed",
    "receipt_timeout", "rpc_error", "rpc_timeout", "router_not_allowed",
    "signing_failed", "transaction_from_mismatch", "transaction_reverted", "value_mismatch",
    "calldata_mismatch", "calldata_unsupported",
    "unknown_receipt_status", "wrong_chain",
})
_PUBLIC_STATUS_VALUES = frozenset({
    "confirmed", "error", "pending", "prepared", "rejected", "reverted", "unknown",
})
_TRANSACTION_FIELDS = (
    "chainId", "from", "to", "data", "value", "gas", "gasPrice",
    "maxFeePerGas", "maxPriorityFeePerGas", "nonce", "type", "accessList",
    "maxFeePerBlobGas", "blobVersionedHashes",
)
_CONTEXT_FIELDS = (
    "chain_id", "token_address", "amount_atomic", "slippage_percent",
    "pool_address", "from_token_address", "target_token_address", "quoted_out", "min_out",
)

# Selectors from the official OKX EVM DEX Router ABI.  The router uses
# dynamic tuples/arrays, so these are decoded separately from the legacy test
# ABI below.
_OKX_DYNAMIC_SELECTORS = frozenset({
    "0xe99bfa95",  # smartSwapByInvest
    "0x591b3d08",  # smartSwapByInvestWithRefund
    "0xb80c2f09",  # smartSwapByOrderId
    "0x03b87e5f",  # smartSwapTo
    "0x44014e98",  # uniswapV3SwapToWithBaseRequest
    "0xb8815477",  # unxswapToWithBaseRequest
    "0x0d5f0e3b",  # uniswapV3SwapTo
    "0x08298b5a",  # unxswapTo
    "0x9871efa4",  # unxswapByOrderId
})


class AdapterError(Exception):
    """Stable, secret-free error raised at the signing boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OkxBscSwapAbi:
    """Narrow static OKX BSC swap ABI accepted by this adapter.

    The configured selector identifies an integration-approved ABI with seven
    static words: tokenIn, tokenOut, pool, recipient, amountIn, minOut, and
    direction (0=buy, 1=sell). Dynamic or unknown calldata is rejected.
    """

    selector: str

    def __post_init__(self) -> None:
        if not isinstance(self.selector, str) or not re.fullmatch(r"0x[0-9a-fA-F]{8}", self.selector):
            raise AdapterError("invalid_calldata")


@dataclass(frozen=True)
class _PreparedExecution:
    side: str
    context: dict[str, Any]
    transaction: dict[str, Any]
    tag: bytes


def _load_eth_account() -> Any | None:
    try:
        import eth_account  # type: ignore
    except Exception:
        return None
    return eth_account


def _env_value(env: Mapping[str, str] | None, name: str) -> str:
    values = os.environ if env is None else env
    value = values.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _chain_is_bsc(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == CHAIN_ID_BSC
    return isinstance(value, str) and value.strip() == str(CHAIN_ID_BSC)


def _require_address(value: Any, error_code: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value.strip()):
        raise AdapterError(error_code)
    return value.strip()


def _amount_text(value: Any) -> str:
    if isinstance(value, bool):
        raise AdapterError("invalid_amount")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise AdapterError("invalid_amount")
    if not text.isdigit() or int(text) <= 0:
        raise AdapterError("invalid_amount")
    return text


def _slippage_text(value: Any) -> str:
    if isinstance(value, bool):
        raise AdapterError("invalid_slippage")
    text = str(value).strip()
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        raise AdapterError("invalid_slippage") from None
    if not math.isfinite(parsed) or parsed < 0 or parsed > 50:
        raise AdapterError("invalid_slippage")
    return text


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        raise AdapterError("invalid_value")
    if isinstance(value, int):
        if value < 0 or value > 2**256 - 1:
            raise AdapterError("invalid_value")
        return str(value)
    if not isinstance(value, str):
        raise AdapterError("invalid_value")
    text = value.strip()
    if text.startswith(("0x", "0X")):
        if not re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
            raise AdapterError("invalid_value")
        if int(text, 16) > 2**256 - 1:
            raise AdapterError("invalid_value")
        return text
    if not text.isdigit():
        raise AdapterError("invalid_value")
    if int(text) > 2**256 - 1:
        raise AdapterError("invalid_value")
    return text


def _value_atomic(value: Any) -> int:
    text = _value_text(value)
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError:
        raise AdapterError("invalid_value") from None


def _validate_transaction_semantics(
    transaction: Any,
    wallet_address: str,
    allowed_router_addresses: set[str],
    *,
    from_token: str,
    amount: str,
) -> dict[str, Any]:
    if not isinstance(transaction, Mapping):
        raise AdapterError("invalid_transaction")
    if not _chain_is_bsc(transaction.get("chainId")):
        raise AdapterError("wrong_chain")
    tx_from = transaction.get("from")
    if not isinstance(tx_from, str) or not _ADDRESS_RE.fullmatch(tx_from.strip()):
        raise AdapterError("invalid_transaction_from")
    if tx_from.strip().lower() != wallet_address.lower():
        raise AdapterError("transaction_from_mismatch")
    tx_to = transaction.get("to")
    if not isinstance(tx_to, str) or not _ADDRESS_RE.fullmatch(tx_to.strip()):
        raise AdapterError("router_not_allowed")
    if tx_to.strip().lower() not in allowed_router_addresses:
        raise AdapterError("router_not_allowed")
    data = transaction.get("data")
    if not isinstance(data, str) or not _HEX_RE.fullmatch(data) or len(data[2:]) % 2 != 0:
        raise AdapterError("invalid_calldata")
    if "value" not in transaction:
        raise AdapterError("invalid_value")
    actual_value = _value_atomic(transaction.get("value"))
    if not isinstance(from_token, str) or not _ADDRESS_RE.fullmatch(from_token.strip()):
        raise AdapterError("invalid_counter_token")
    try:
        amount_atomic = int(_amount_text(amount), 10)
    except AdapterError:
        raise AdapterError("invalid_amount") from None
    expected_value = amount_atomic if from_token.strip().lower() == BSC_NATIVE_TOKEN.lower() else 0
    if actual_value != expected_value:
        raise AdapterError("value_mismatch")
    return {key: transaction[key] for key in _TRANSACTION_FIELDS if key in transaction}


def _context_matches(context: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(context, Mapping):
        return False
    for key, value in expected.items():
        actual = context.get(key)
        if key in {"token_address", "pool_address"}:
            if not isinstance(actual, str) or actual.strip().lower() != str(value).lower():
                return False
        elif str(actual).strip() != str(value).strip():
            return False
    return True


def _context_uint(context: Mapping[str, Any], key: str) -> int:
    value = context.get(key)
    if isinstance(value, bool):
        raise AdapterError("calldata_mismatch")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise AdapterError("calldata_mismatch")
    if result < 0 or result > 2**256 - 1:
        raise AdapterError("calldata_mismatch")
    return result


def _word_address(word: str) -> str:
    if len(word) != 64 or word[:24] != "0" * 24:
        raise AdapterError("calldata_mismatch")
    return "0x" + word[24:]


def _decode_okx_bsc_swap_calldata(calldata: Any, abi: OkxBscSwapAbi) -> dict[str, Any]:
    if not isinstance(calldata, str) or not _HEX_RE.fullmatch(calldata):
        raise AdapterError("calldata_unsupported")
    body = calldata[2:]
    if body[:8].lower() != abi.selector[2:].lower():
        raise AdapterError("calldata_unsupported")
    if len(body) != 8 + 7 * 64:
        raise AdapterError("calldata_unsupported")
    words = [body[8 + index * 64:8 + (index + 1) * 64] for index in range(7)]
    try:
        direction = int(words[6], 16)
        decoded = {
            "token_in": _word_address(words[0]),
            "token_out": _word_address(words[1]),
            "pool": _word_address(words[2]),
            "recipient": _word_address(words[3]),
            "amount_in": int(words[4], 16),
            "min_out": int(words[5], 16),
            "direction": direction,
        }
    except ValueError:
        raise AdapterError("calldata_mismatch") from None
    if direction not in (0, 1):
        raise AdapterError("calldata_mismatch")
    return decoded


def _validate_okx_bsc_swap_calldata(
    calldata: Any,
    abi: OkxBscSwapAbi,
    *,
    side: str,
    token: str,
    pool: str,
    wallet: str,
    from_token: str,
    target_token: str,
    amount: str,
    slippage: str,
    context: Mapping[str, Any],
) -> None:
    if isinstance(calldata, str) and _HEX_RE.fullmatch(calldata) and len(calldata) >= 10:
        selector = calldata[:10].lower()
        if selector in _OKX_DYNAMIC_SELECTORS:
            _validate_okx_dynamic_calldata(
                calldata,
                side=side,
                token=token,
                pool=pool,
                wallet=wallet,
                from_token=from_token,
                target_token=target_token,
                amount=amount,
                context=context,
            )
            return
    decoded = _decode_okx_bsc_swap_calldata(calldata, abi)
    expected_direction = 0 if side == "buy" else 1
    if decoded["token_in"].lower() != from_token.lower():
        raise AdapterError("calldata_mismatch")
    if decoded["token_out"].lower() != target_token.lower():
        raise AdapterError("calldata_mismatch")
    if decoded["pool"].lower() != pool.lower():
        raise AdapterError("calldata_mismatch")
    if decoded["recipient"].lower() != wallet.lower():
        raise AdapterError("calldata_mismatch")
    if decoded["amount_in"] != int(amount) or decoded["direction"] != expected_direction:
        raise AdapterError("calldata_mismatch")

    quoted_out = _context_uint(context, "quoted_out")
    expected_min_out = _context_uint(context, "min_out")
    if quoted_out <= 0 or expected_min_out <= 0 or expected_min_out > quoted_out:
        raise AdapterError("calldata_mismatch")
    try:
        slippage_decimal = Decimal(slippage)
        calculated_min_out = int(
            Decimal(quoted_out) * (Decimal("100") - slippage_decimal) / Decimal("100")
        )
    except (InvalidOperation, ValueError):
        raise AdapterError("calldata_mismatch") from None
    if decoded["min_out"] != expected_min_out or expected_min_out != calculated_min_out:
        raise AdapterError("calldata_mismatch")


def _dynamic_word(words: list[str], index: int) -> int:
    if index < 0 or index >= len(words) or len(words[index]) != 64:
        raise AdapterError("calldata_mismatch")
    try:
        value = int(words[index], 16)
    except ValueError:
        raise AdapterError("calldata_mismatch") from None
    if value < 0 or value > 2**256 - 1:
        raise AdapterError("calldata_mismatch")
    return value


def _dynamic_address(words: list[str], index: int) -> str:
    word = words[index] if 0 <= index < len(words) else ""
    if len(word) != 64 or word[:24] != "0" * 24:
        raise AdapterError("calldata_mismatch")
    return "0x" + word[24:]


def _dynamic_offset(words: list[str], index: int, total_bytes: int) -> int:
    offset = _dynamic_word(words, index)
    if offset % 32 != 0 or offset < 32 or offset + 32 > total_bytes:
        raise AdapterError("calldata_mismatch")
    return offset


def _dynamic_array_sum(words: list[str], offset: int, total_bytes: int) -> int:
    start = offset // 32
    length = _dynamic_word(words, start)
    end = start + 1 + length
    if length <= 0 or end > len(words) or end * 32 > total_bytes:
        raise AdapterError("calldata_mismatch")
    return sum(_dynamic_word(words, index) for index in range(start + 1, end))


def _dynamic_base_request(words: list[str], start: int) -> tuple[str, str, int, int, int]:
    source = _dynamic_address(words, start)
    target = _dynamic_address(words, start + 1)
    amount = _dynamic_word(words, start + 2)
    minimum = _dynamic_word(words, start + 3)
    deadline = _dynamic_word(words, start + 4)
    if deadline <= 0:
        raise AdapterError("calldata_mismatch")
    return source, target, amount, minimum, deadline


def _validate_okx_dynamic_calldata(
    calldata: Any,
    *,
    side: str,
    token: str,
    pool: str,
    wallet: str,
    from_token: str,
    target_token: str,
    amount: str,
    context: Mapping[str, Any],
) -> None:
    if not isinstance(calldata, str) or not _HEX_RE.fullmatch(calldata):
        raise AdapterError("calldata_unsupported")
    body = calldata[2:]
    selector = "0x" + body[:8].lower()
    if selector not in _OKX_DYNAMIC_SELECTORS or len(body) < 8 or (len(body) - 8) % 64:
        raise AdapterError("calldata_unsupported")
    words = [body[8 + index * 64:8 + (index + 1) * 64] for index in range((len(body) - 8) // 64)]
    total_bytes = len(words) * 32
    expected_amount = int(amount)
    expected_min_out = _context_uint(context, "min_out")
    expected_source = from_token.lower()
    expected_target = target_token.lower()
    explicit_receiver: str | None = None
    batch_offset_index: int | None = None
    dynamic_offsets: list[int] = []

    if selector == "0xe99bfa95":  # smartSwapByInvest(baseRequest,...,to)
        source, target, actual_amount, minimum, _ = _dynamic_base_request(words, 0)
        batch_offset_index = 5
        explicit_receiver = _dynamic_address(words, 8)
        dynamic_offsets = [5, 6, 7]
    elif selector == "0x591b3d08":  # smartSwapByInvestWithRefund(...,to,refundTo)
        source, target, actual_amount, minimum, _ = _dynamic_base_request(words, 0)
        batch_offset_index = 5
        explicit_receiver = _dynamic_address(words, 8)
        refund = _dynamic_address(words, 9)
        if refund.lower() != wallet.lower():
            raise AdapterError("calldata_mismatch")
        dynamic_offsets = [5, 6, 7]
    elif selector == "0xb80c2f09":  # smartSwapByOrderId(orderId,baseRequest,...)
        source, target, actual_amount, minimum, _ = _dynamic_base_request(words, 1)
        batch_offset_index = 6
        dynamic_offsets = [6, 7, 8]
    elif selector == "0x03b87e5f":  # smartSwapTo(orderId,receiver,baseRequest,...)
        explicit_receiver = _dynamic_address(words, 1)
        source, target, actual_amount, minimum, _ = _dynamic_base_request(words, 2)
        batch_offset_index = 7
        dynamic_offsets = [7, 8, 9]
    elif selector in {"0x44014e98", "0xb8815477"}:
        explicit_receiver = _dynamic_address(words, 1)
        source, target, actual_amount, minimum, _ = _dynamic_base_request(words, 2)
        dynamic_offsets = [7]
    elif selector == "0x0d5f0e3b":  # uniswapV3SwapTo(receiver,amount,minReturn,pools)
        receiver_word = _dynamic_word(words, 0)
        explicit_receiver = "0x" + f"{receiver_word & ((1 << 160) - 1):040x}"
        source = from_token
        target = target_token
        actual_amount = _dynamic_word(words, 1)
        minimum = _dynamic_word(words, 2)
        dynamic_offsets = [3]
    elif selector == "0x08298b5a":  # unxswapTo(srcToken,amount,minReturn,receiver,pools)
        source_word = _dynamic_word(words, 0)
        source = "0x" + f"{source_word & ((1 << 160) - 1):040x}"
        target = target_token
        actual_amount = _dynamic_word(words, 1)
        minimum = _dynamic_word(words, 2)
        explicit_receiver = _dynamic_address(words, 3)
        dynamic_offsets = [4]
    else:  # unxswapByOrderId(srcToken,amount,minReturn,pools)
        source_word = _dynamic_word(words, 0)
        source = "0x" + f"{source_word & ((1 << 160) - 1):040x}"
        target = target_token
        actual_amount = _dynamic_word(words, 1)
        minimum = _dynamic_word(words, 2)
        dynamic_offsets = [3]

    if source.lower() != expected_source or target.lower() != expected_target:
        raise AdapterError("calldata_mismatch")
    if actual_amount != expected_amount or minimum != expected_min_out:
        raise AdapterError("calldata_mismatch")
    if explicit_receiver is not None and explicit_receiver.lower() != wallet.lower():
        raise AdapterError("calldata_mismatch")
    for index in dynamic_offsets:
        offset = _dynamic_offset(words, index, total_bytes)
        if index == batch_offset_index:
            if _dynamic_array_sum(words, offset, total_bytes) > expected_amount:
                raise AdapterError("calldata_mismatch")
    # OKX packs pool addresses into RouterPath.rawData or pool words. Require
    # the requested pool to be present in the exact calldata bytes.
    if pool[2:].lower() not in body.lower():
        raise AdapterError("calldata_mismatch")


def _status_is_success(value: Any) -> bool:
    return not isinstance(value, bool) and value in (1, "1", "0x1", "0X1")


def _status_is_failure(value: Any) -> bool:
    return not isinstance(value, bool) and value in (0, "0", "0x0", "0X0")


def _normalize_block_number(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        number = int(value, 16)
    else:
        return None
    if number < 0 or number > 2**64 - 1:
        return None
    return hex(number)


def redact_transaction(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively remove credential-like keys and values from public data."""
    sensitive = {
        "privatekey", "secret", "secretkey", "signature", "rawtransaction", "rawtx",
        "signedtransaction", "signedrawtransaction", "authorization", "okaccesssign",
        "okaccesssecretkey", "apikey", "passphrase", "mnemonic", "seed", "okxsecret",
        "transaction", "tx", "input", "calldata",
    }
    hash_keys = {"txhash", "transactionhash"}
    address_keys = {
        "tokenaddress", "pooladdress", "fromtokenaddress", "targettokenaddress", "walletaddress",
    }
    amount_keys = {"amountatomic", "quotedout", "minout"}
    marker = re.compile(r"(?:private[ _-]*key|secret|signature|raw[ _-]*(?:tx|transaction)|authorization|passphrase|mnemonic|seed)", re.IGNORECASE)

    def clean_string(value: str, key: str = "", in_receipt: bool = False) -> str:
        if marker.search(value):
            return "[REDACTED]"
        return "[REDACTED]"

    def normalize_amount(value: Any, *, positive: bool) -> str | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            number = value
        elif isinstance(value, str) and value.isdigit():
            number = int(value)
        else:
            return None
        if number < (1 if positive else 0) or number > 2**256 - 1:
            return None
        return str(number)

    def normalize_field(normalized: str, value: Any) -> Any:
        if normalized == "error":
            return value if isinstance(value, str) and value in _PUBLIC_ERROR_CODES else None
        if normalized == "side":
            return value if value in {"buy", "sell"} else None
        if normalized == "chainid":
            return CHAIN_ID_BSC if _chain_is_bsc(value) else None
        if normalized in address_keys:
            if isinstance(value, str) and _ADDRESS_RE.fullmatch(value.strip()):
                return value.strip().lower()
            return None
        if normalized in amount_keys:
            return normalize_amount(value, positive=normalized == "amountatomic")
        if normalized == "slippagepercent":
            if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
                return None
            try:
                text = _slippage_text(value)
                return format(Decimal(text), "f")
            except (AdapterError, InvalidOperation, ValueError):
                return None
        return None

    def clean_context(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, Any] = {}
        for field in _CONTEXT_FIELDS:
            if field not in value:
                continue
            normalized = re.sub(r"[^a-z0-9]", "", field.lower())
            normalized_value = normalize_field(normalized, value[field])
            if normalized_value is not None:
                result[field] = normalized_value
        return result

    def clean(value: Any, key: str = "", in_receipt: bool = False) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in sensitive or marker.search(str(key)):
                    continue
                if normalized == "requestcontext":
                    if isinstance(item, Mapping):
                        result[str(key)] = clean_context(item)
                    continue
                if normalized in hash_keys:
                    if isinstance(item, str) and _TX_HASH_RE.fullmatch(item):
                        result[str(key)] = "0x" + item[2:].lower()
                    continue
                if normalized == "blockhash":
                    if isinstance(item, str) and _TX_HASH_RE.fullmatch(item):
                        result[str(key)] = "0x" + item[2:].lower()
                    continue
                if normalized == "blocknumber":
                    normalized_number = _normalize_block_number(item)
                    if normalized_number is not None:
                        result[str(key)] = normalized_number
                    continue
                if normalized == "status":
                    if _status_is_success(item):
                        result[str(key)] = "0x1"
                    elif _status_is_failure(item):
                        result[str(key)] = "0x0"
                    elif not in_receipt and isinstance(item, str) and item in _PUBLIC_STATUS_VALUES:
                        result[str(key)] = item
                    continue
                if normalized in address_keys or normalized in amount_keys or normalized in {
                    "error", "side", "chainid", "slippagepercent",
                }:
                    normalized_value = normalize_field(normalized, item)
                    if normalized_value is not None:
                        result[str(key)] = normalized_value
                    continue
                result[str(key)] = clean(
                    item,
                    str(key),
                    in_receipt=in_receipt or normalized == "receipt",
                )
            return result
        if isinstance(value, list):
            return [clean(item, key, in_receipt=in_receipt) for item in value]
        if isinstance(value, tuple):
            return [clean(item, key, in_receipt=in_receipt) for item in value]
        if isinstance(value, str):
            return clean_string(value, key, in_receipt)
        return value

    cleaned = clean(payload)
    return cleaned if isinstance(cleaned, dict) else {}


class LocalEvmSigner:
    """Sign BSC transactions with a key supplied only by explicit input/env."""

    def __init__(
        self,
        wallet_address: str,
        private_key: str | None = None,
        *,
        chain_id: int = CHAIN_ID_BSC,
        env: Mapping[str, str] | None = None,
        account_module: Any | None = None,
    ) -> None:
        if not _chain_is_bsc(chain_id):
            raise AdapterError("wrong_chain")
        self.wallet_address = _require_address(wallet_address, "invalid_wallet_address")
        key = private_key if private_key is not None else _env_value(env, "BSC_PRIVATE_KEY")
        if not isinstance(key, str) or not key.strip():
            raise AdapterError("missing_private_key")

        module = account_module if account_module is not None else _load_eth_account()
        if module is None or not hasattr(module, "Account"):
            raise AdapterError("missing_eth_account")
        try:
            account = module.Account.from_key(key)
            derived = _require_address(account.address, "invalid_derived_address")
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("invalid_private_key") from None
        if derived.lower() != self.wallet_address.lower():
            raise AdapterError("address_mismatch")
        self._account = account

    @staticmethod
    def _raw_text(signed: Any) -> str | None:
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = getattr(signed, "rawTransaction", None)
        if isinstance(raw, bytes):
            return "0x" + raw.hex()
        if isinstance(raw, str) and _HEX_RE.fullmatch(raw) and len(raw) > 2:
            return raw
        return None

    def sign(self, transaction: Mapping[str, Any]) -> str:
        if not isinstance(transaction, Mapping) or not _chain_is_bsc(transaction.get("chainId")):
            raise AdapterError("wrong_chain")
        try:
            raw = self._raw_text(self._account.sign_transaction(dict(transaction)))
            if raw is not None:
                return raw
        except Exception:
            raise AdapterError("signing_failed") from None
        raise AdapterError("signing_failed")

    def verify_signed_transaction(
        self,
        raw_transaction: str,
        expected_transaction: Mapping[str, Any],
    ) -> bool:
        """Prove the raw bytes are the deterministic signature of this tx."""
        if not isinstance(raw_transaction, str) or not _HEX_RE.fullmatch(raw_transaction) or len(raw_transaction) <= 2:
            return False
        if not isinstance(expected_transaction, Mapping) or not _chain_is_bsc(expected_transaction.get("chainId")):
            return False
        tx_from = expected_transaction.get("from")
        if not isinstance(tx_from, str) or not _ADDRESS_RE.fullmatch(tx_from.strip()):
            return False
        if tx_from.strip().lower() != self.wallet_address.lower():
            return False
        try:
            expected_raw = self._raw_text(self._account.sign_transaction(dict(expected_transaction)))
        except Exception:
            return False
        return expected_raw is not None and hmac.compare_digest(raw_transaction, expected_raw)


RpcTransport = Callable[[str, dict[str, Any], float], Mapping[str, Any]]


class JsonRpcBroadcaster:
    """Minimal standard JSON-RPC sender/receipt poller for BSC."""

    def __init__(
        self,
        rpc_url: str | None = None,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
        transport: RpcTransport | None = None,
    ) -> None:
        url = rpc_url if rpc_url is not None else _env_value(env, "BSC_RPC_URL")
        if not isinstance(url, str) or not url.strip():
            raise AdapterError("missing_rpc_url")
        self.rpc_url = url.strip()
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._transport = transport or self._urllib_transport
        self._request_id = 0
        self.last_error: dict[str, str] | None = None

    @staticmethod
    def _urllib_transport(url: str, payload: dict[str, Any], timeout: float) -> Mapping[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "alpha-radar/1.0",
        }, method="POST")
        with urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
        return decoded if isinstance(decoded, Mapping) else {}

    def _call(
        self,
        method: str,
        params: list[Any],
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any] | None:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        request_timeout = self.timeout_seconds if timeout_seconds is None else min(self.timeout_seconds, max(0.0, timeout_seconds))
        try:
            response = self._transport(self.rpc_url, payload, request_timeout)
        except (TimeoutError, URLError, OSError):
            self.last_error = {"code": "rpc_timeout"}
            return None
        except Exception:
            self.last_error = {"code": "rpc_error"}
            return None
        if not isinstance(response, Mapping):
            self.last_error = {"code": "invalid_rpc_response"}
            return None
        if response.get("error") is not None:
            self.last_error = {"code": "rpc_error"}
            return None
        return response

    def send(self, raw_transaction: str) -> str | None:
        self.last_error = None
        if not isinstance(raw_transaction, str) or not _HEX_RE.fullmatch(raw_transaction) or len(raw_transaction) <= 2:
            self.last_error = {"code": "invalid_raw_transaction"}
            return None
        response = self._call("eth_sendRawTransaction", [raw_transaction])
        if response is None:
            return None
        result = response.get("result")
        if not isinstance(result, str) or not _TX_HASH_RE.fullmatch(result):
            self.last_error = {"code": "invalid_transaction_hash"}
            return None
        return result

    def send_raw_transaction(self, raw_transaction: str) -> str | None:
        return self.send(raw_transaction)

    def receipt(self, tx_hash: str, timeout_seconds: float) -> dict[str, Any] | None:
        self.last_error = None
        if not isinstance(tx_hash, str) or not _TX_HASH_RE.fullmatch(tx_hash):
            self.last_error = {"code": "invalid_transaction_hash"}
            return None
        timeout = max(0.0, float(timeout_seconds))
        if timeout <= 0:
            self.last_error = {"code": "receipt_timeout"}
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.last_error = {"code": "receipt_timeout"}
                return None
            response = self._call("eth_getTransactionReceipt", [tx_hash], remaining)
            if response is None:
                return None
            if time.monotonic() >= deadline:
                self.last_error = {"code": "receipt_timeout"}
                return None
            result = response.get("result")
            if isinstance(result, Mapping):
                redacted = redact_transaction({"receipt": result})
                receipt = redacted.get("receipt")
                return receipt if isinstance(receipt, dict) else {}
            if result is not None:
                self.last_error = {"code": "invalid_rpc_response"}
                return None
            if time.monotonic() >= deadline:
                self.last_error = {"code": "receipt_timeout"}
                return None
            time.sleep(min(self.poll_interval_seconds, max(0.0, deadline - time.monotonic())))


class LiveExecutionAdapter:
    """Explicitly enabled BSC buy/sell flow with a fail-closed result boundary."""

    def __init__(
        self,
        signer: Any,
        broadcaster: Any,
        okx_client: OkxSwapClient,
        wallet_address: str | None = None,
        *,
        chain_id: int = CHAIN_ID_BSC,
        env: Mapping[str, str] | None = None,
        receipt_timeout_seconds: float = 30.0,
        allowed_router_addresses: Any | None = None,
        armed: bool = False,
        swap_abi: OkxBscSwapAbi | None = None,
    ) -> None:
        self.signer = signer
        self.broadcaster = broadcaster
        self.okx_client = okx_client
        self.wallet_address = wallet_address or getattr(signer, "wallet_address", "")
        self.chain_id = chain_id
        self._env = env
        self.enabled = _env_value(env, "LIVE_TRADING_ENABLED").lower() == "true"
        self.armed = armed is True
        self.swap_abi = swap_abi
        self._prepared_key = secrets.token_bytes(32)
        if isinstance(allowed_router_addresses, str):
            allowed_router_addresses = [allowed_router_addresses]
        try:
            self.allowed_router_addresses = {
                _require_address(address, "invalid_router_config").lower()
                for address in (allowed_router_addresses or [])
            }
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("invalid_router_config") from None
        self.receipt_timeout_seconds = max(0.0, float(receipt_timeout_seconds))

    @staticmethod
    def _error(code: str, status: str = "error") -> dict[str, Any]:
        safe_code = code if isinstance(code, str) and code in _PUBLIC_ERROR_CODES else "opaque_error"
        safe_status = status if status in {"error", "rejected", "pending", "unknown", "reverted"} else "error"
        return redact_transaction({"ok": False, "status": safe_status, "error": safe_code})

    def _live_enabled(self) -> bool:
        return _env_value(self._env, "LIVE_TRADING_ENABLED").lower() == "true"

    def _prepare(
        self,
        side: str,
        token_address: str,
        pool_address: str,
        amount_atomic: Any,
        slippage_percent: Any,
        *,
        chain_id: Any,
        counter_token: str | None,
    ) -> dict[str, Any]:
        if not self._live_enabled() or self.armed is not True:
            return self._error("live_disabled", "rejected")
        if not _chain_is_bsc(self.chain_id) or not _chain_is_bsc(chain_id):
            return self._error("wrong_chain", "rejected")
        try:
            wallet = _require_address(self.wallet_address, "invalid_wallet_address")
            token = _require_address(token_address, "invalid_token")
            pool = _require_address(pool_address, "invalid_pool")
            amount = _amount_text(amount_atomic)
            slippage = _slippage_text(slippage_percent)
            other = _require_address(counter_token or BSC_NATIVE_TOKEN, "invalid_counter_token")
        except AdapterError as error:
            return self._error(error.code, "rejected")

        from_token = other if side == "buy" else token
        target_token = token if side == "buy" else other
        expected_context = {
            "chain_id": CHAIN_ID_BSC,
            "token_address": token,
            "amount_atomic": amount,
            "slippage_percent": slippage,
            "pool_address": pool,
            "from_token_address": from_token,
            "target_token_address": target_token,
        }
        builder = getattr(self.okx_client, "build_swap_bsc_with_context", None)
        if not callable(builder):
            return self._error("pool_unverifiable", "rejected")
        try:
            response = builder(
                token_address=target_token,
                amount_atomic=amount,
                from_token=from_token,
                slippage_percent=slippage,
                wallet_address=wallet,
                pool_address=pool,
                chain_id=CHAIN_ID_BSC,
            )
        except Exception:
            return self._error("okx_build_failed")
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            return self._error("okx_build_failed")
        if not _context_matches(response.get("request_context"), expected_context):
            return self._error("okx_context_mismatch")
        transaction = response.get("tx")
        if not isinstance(transaction, Mapping):
            return self._error("invalid_transaction")
        try:
            validated_transaction = _validate_transaction_semantics(
                transaction,
                wallet,
                self.allowed_router_addresses,
                from_token=from_token,
                amount=amount,
            )
            if self.swap_abi is None:
                raise AdapterError("calldata_unsupported")
            response_context = response.get("request_context")
            _validate_okx_bsc_swap_calldata(
                validated_transaction.get("data"),
                self.swap_abi,
                side=side,
                token=token,
                pool=pool,
                wallet=wallet,
                from_token=from_token,
                target_token=target_token,
                amount=amount,
                slippage=slippage,
                context=response_context,
            )
        except AdapterError as error:
            return self._error(error.code, "rejected")
        context = dict(response.get("request_context"))
        try:
            tag = self._prepared_tag(side, context, validated_transaction)
        except Exception:
            return self._error("opaque_error")
        return _PreparedExecution(side, context, validated_transaction, tag)

    def _prepared_tag(
        self,
        side: str,
        context: Mapping[str, Any],
        transaction: Mapping[str, Any],
    ) -> bytes:
        body = json.dumps(
            {"side": side, "context": dict(context), "transaction": dict(transaction)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hmac.new(self._prepared_key, body, "sha256").digest()

    def _verify_prepared(self, prepared: _PreparedExecution) -> bool:
        try:
            expected = self._prepared_tag(prepared.side, prepared.context, prepared.transaction)
        except Exception:
            return False
        return hmac.compare_digest(expected, prepared.tag)

    @staticmethod
    def _prepared_public(prepared: Any) -> dict[str, Any]:
        if isinstance(prepared, _PreparedExecution):
            return redact_transaction({
                "ok": True,
                "status": "prepared",
                "side": prepared.side,
                **prepared.context,
            })
        if isinstance(prepared, Mapping):
            return redact_transaction(prepared)
        return LiveExecutionAdapter._error("opaque_error")

    def _execute(self, prepared: Any) -> dict[str, Any]:
        if not self._live_enabled() or self.armed is not True:
            return self._error("live_disabled", "rejected")
        if not isinstance(prepared, _PreparedExecution) or not self._verify_prepared(prepared):
            return self._error("prepared_invalid")
        context = prepared.context
        expected_context = {
            "chain_id": CHAIN_ID_BSC,
            "token_address": context.get("token_address"),
            "amount_atomic": context.get("amount_atomic"),
            "slippage_percent": context.get("slippage_percent"),
            "pool_address": context.get("pool_address"),
            "from_token_address": context.get("from_token_address"),
            "target_token_address": context.get("target_token_address"),
        }
        if not _context_matches(context, expected_context):
            return self._error("okx_context_mismatch")
        try:
            wallet = _require_address(self.wallet_address, "invalid_wallet_address")
            token = _require_address(context.get("token_address"), "invalid_token")
            pool = _require_address(context.get("pool_address"), "invalid_pool")
            from_token = _require_address(context.get("from_token_address"), "invalid_counter_token")
            target_token = _require_address(context.get("target_token_address"), "invalid_counter_token")
            amount = _amount_text(context.get("amount_atomic"))
            slippage = _slippage_text(context.get("slippage_percent"))
            transaction = _validate_transaction_semantics(
                prepared.transaction,
                wallet,
                self.allowed_router_addresses,
                from_token=from_token,
                amount=amount,
            )
            if self.swap_abi is None:
                raise AdapterError("calldata_unsupported")
            _validate_okx_bsc_swap_calldata(
                transaction.get("data"),
                self.swap_abi,
                side=prepared.side,
                token=token,
                pool=pool,
                wallet=wallet,
                from_token=from_token,
                target_token=target_token,
                amount=amount,
                slippage=slippage,
                context=context,
            )
        except AdapterError as error:
            return self._error(error.code, "rejected")
        try:
            expected_unsigned_transaction = deepcopy(transaction)
            raw_transaction = self.signer.sign(deepcopy(expected_unsigned_transaction))
        except AdapterError as error:
            return self._error(error.code)
        except Exception:
            return self._error("signing_failed")
        if not isinstance(raw_transaction, str) or not _HEX_RE.fullmatch(raw_transaction) or len(raw_transaction) <= 2:
            return self._error("invalid_signed_transaction")
        verifier = getattr(self.signer, "verify_signed_transaction", None)
        if not callable(verifier):
            return self._error("invalid_signed_transaction")
        try:
            verified = verifier(raw_transaction, expected_unsigned_transaction)
        except Exception:
            verified = False
        if verified is not True:
            return self._error("invalid_signed_transaction")
        try:
            tx_hash = self.broadcaster.send(raw_transaction)
        except Exception:
            return self._error("broadcast_failed")
        if not isinstance(tx_hash, str) or not _TX_HASH_RE.fullmatch(tx_hash):
            return self._error(self._broadcaster_error("broadcast_failed"))
        try:
            receipt = self.broadcaster.receipt(tx_hash, self.receipt_timeout_seconds)
        except Exception:
            return self._result_after_broadcast(prepared, tx_hash, None, "receipt_failed", "unknown")
        if receipt is None:
            error = self._broadcaster_error("receipt_timeout")
            state = "pending" if error == "receipt_timeout" else "unknown"
            return self._result_after_broadcast(prepared, tx_hash, None, error, state)
        try:
            status = receipt.get("status") if isinstance(receipt, Mapping) else None
        except Exception:
            return self._result_after_broadcast(prepared, tx_hash, None, "receipt_failed", "unknown")
        if _status_is_success(status):
            return redact_transaction({**self._public_execution_fields(prepared), "status": "confirmed", "tx_hash": tx_hash, "receipt": receipt})
        if _status_is_failure(status):
            return redact_transaction({**self._public_execution_fields(prepared), "ok": False, "status": "reverted", "error": "transaction_reverted", "tx_hash": tx_hash, "receipt": receipt})
        return self._result_after_broadcast(prepared, tx_hash, receipt, "unknown_receipt_status", "unknown")

    @staticmethod
    def _public_execution_fields(prepared: _PreparedExecution) -> dict[str, Any]:
        return {
            "ok": True,
            "side": prepared.side,
            "chain_id": prepared.context.get("chain_id"),
            "token_address": prepared.context.get("token_address"),
            "pool_address": prepared.context.get("pool_address"),
            "amount_atomic": prepared.context.get("amount_atomic"),
            "slippage_percent": prepared.context.get("slippage_percent"),
        }

    @staticmethod
    def _result_after_broadcast(
        prepared: _PreparedExecution,
        tx_hash: str,
        receipt: Any,
        error: str,
        status: str,
    ) -> dict[str, Any]:
        safe_code = error if isinstance(error, str) and error in _PUBLIC_ERROR_CODES else "opaque_error"
        return redact_transaction({
            **LiveExecutionAdapter._public_execution_fields(prepared),
            "ok": False,
            "status": status,
            "error": safe_code,
            "tx_hash": tx_hash,
            "receipt": receipt,
        })

    def _broadcaster_error(self, fallback: str) -> str:
        error = getattr(self.broadcaster, "last_error", None)
        if isinstance(error, Mapping):
            code = error.get("code")
            if isinstance(code, str) and code in _PUBLIC_ERROR_CODES:
                return code
        return fallback if fallback in _PUBLIC_ERROR_CODES else "opaque_error"

    def prepare_buy(self, token_address: str, pool_address: str, amount_atomic: Any, slippage_percent: Any, *, chain_id: Any = CHAIN_ID_BSC, from_token_address: str | None = None) -> dict[str, Any]:
        try:
            prepared = self._prepare("buy", token_address, pool_address, amount_atomic, slippage_percent, chain_id=chain_id, counter_token=from_token_address)
            return self._prepared_public(prepared)
        except Exception:
            return self._error("opaque_error")

    def prepare_sell(self, token_address: str, pool_address: str, amount_atomic: Any, slippage_percent: Any, *, chain_id: Any = CHAIN_ID_BSC, to_token_address: str | None = None) -> dict[str, Any]:
        try:
            prepared = self._prepare("sell", token_address, pool_address, amount_atomic, slippage_percent, chain_id=chain_id, counter_token=to_token_address)
            return self._prepared_public(prepared)
        except Exception:
            return self._error("opaque_error")

    def buy(self, token_address: str, pool_address: str, amount_atomic: Any, slippage_percent: Any, *, chain_id: Any = CHAIN_ID_BSC, from_token_address: str | None = None) -> dict[str, Any]:
        try:
            prepared = self._prepare("buy", token_address, pool_address, amount_atomic, slippage_percent, chain_id=chain_id, counter_token=from_token_address)
            if isinstance(prepared, Mapping):
                return redact_transaction(prepared)
            return self._execute(prepared)
        except Exception:
            return self._error("opaque_error")

    def sell(self, token_address: str, pool_address: str, amount_atomic: Any, slippage_percent: Any, *, chain_id: Any = CHAIN_ID_BSC, to_token_address: str | None = None) -> dict[str, Any]:
        try:
            prepared = self._prepare("sell", token_address, pool_address, amount_atomic, slippage_percent, chain_id=chain_id, counter_token=to_token_address)
            if isinstance(prepared, Mapping):
                return redact_transaction(prepared)
            return self._execute(prepared)
        except Exception:
            return self._error("opaque_error")


__all__ = [
    "AdapterError",
    "BSC_NATIVE_TOKEN",
    "CHAIN_ID_BSC",
    "JsonRpcBroadcaster",
    "LiveExecutionAdapter",
    "LocalEvmSigner",
    "OkxBscSwapAbi",
    "redact_transaction",
]
