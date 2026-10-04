#!/usr/bin/env python3
"""Read-only hybrid ARC mainnet discovery adapter."""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


ARC_CHAIN_ID_HEX = "0x13b2"
DEFAULT_RPC_URLS = (
    "https://rpc.mainnet.arc.io",
    "https://rpc.blockdaemon.mainnet.arc.io",
    "https://rpc.drpc.mainnet.arc.io",
    "https://rpc.quicknode.mainnet.arc.io",
)
DEFAULT_ARCSCAN_BASE_URL = os.environ.get("ARC_SCAN_API_URL", "").strip()
MAX_BLOCK_SPAN = 250
MAX_RANGES_PER_CYCLE = 4
MAX_BLOCKS_PER_CYCLE = MAX_BLOCK_SPAN * MAX_RANGES_PER_CYCLE
MAX_LOG_QUERIES_PER_CYCLE = 64
OVERLAP_BLOCKS = 6
RECENT_EVENT_LIMIT = 2000
MAX_EVENT_IDS_PER_IDENTITY = 32
REQUEST_TIMEOUT_SECONDS = 3.0

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
ZERO_ADDRESS = "0x" + ("0" * 40)
ZERO_ADDRESS_TOPIC = "0x" + ("0" * 64)

NAME_SELECTOR = "0x06fdde03"
SYMBOL_SELECTOR = "0x95d89b41"
DECIMALS_SELECTOR = "0x313ce567"
TOTAL_SUPPLY_SELECTOR = "0x18160ddd"

ARCSCAN_FIELD_ALIASES = {
    "first_transfer_block": ("first_transfer_block", "firstTransferBlock"),
    "last_transfer_block": ("last_transfer_block", "lastTransferBlock"),
    "transfer_count": ("transfer_count", "transferCount"),
    "transfer_count_24h": ("transfer_count_24h", "transferCount24h"),
    "activity_count": ("activity_count", "activityCount"),
    "holder_count": ("holder_count", "holderCount", "holders"),
    "total_supply": ("total_supply", "totalSupply"),
}

ROOT = Path(__file__).resolve().parents[2]
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class VerificationResult:
    status: Literal["verified", "rejected", "unknown"]
    metadata: dict[str, object] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


class UnresolvedCandidateError(RuntimeError):
    """Raised when observed evidence cannot be classified without data loss."""


class RpcResponseError(RuntimeError):
    """Structured JSON-RPC error returned by a validated endpoint."""

    def __init__(self, code: object, message: object, data: object = None):
        self.code = code
        self.message = str(message or "rpc_error")
        self.data = data
        self.error = {"code": code, "message": self.message}
        if data is not None:
            self.error["data"] = data
        super().__init__(json.dumps(self.error, sort_keys=True, ensure_ascii=True))


class MalformedRpcResponseError(RuntimeError):
    """JSON-RPC error member that does not satisfy the structured error contract."""

    def __init__(self, error: object):
        self.error = error
        super().__init__(
            "invalid_rpc_error:"
            + json.dumps(error, sort_keys=True, ensure_ascii=True)
        )


class LogRangeProcessingError(RuntimeError):
    """Bounded log-range processing stopped before the interval was resolved."""

    def __init__(
        self,
        reason: str,
        from_block: int,
        to_block: int,
        query_budget: "_LogQueryBudget",
        rpc_error: RpcResponseError | None = None,
    ):
        self.reason = reason
        self.from_block = from_block
        self.to_block = to_block
        self.queries_used = query_budget.used
        self.query_budget = query_budget.limit
        self.rpc_error = rpc_error.error if rpc_error is not None else None
        evidence: dict[str, object] = {
            "error": reason,
            "from_block": from_block,
            "to_block": to_block,
            "log_queries_used": self.queries_used,
            "log_query_budget": self.query_budget,
        }
        if self.rpc_error is not None:
            evidence["rpc_error"] = self.rpc_error
        super().__init__(json.dumps(evidence, sort_keys=True, ensure_ascii=True))


@dataclass
class _LogQueryBudget:
    limit: int
    used: int = 0

    def claim(self, from_block: int, to_block: int) -> None:
        if self.used >= self.limit:
            raise LogRangeProcessingError(
                "log_query_budget_exhausted",
                from_block,
                to_block,
                self,
            )
        self.used += 1


_ACTIVE_LOG_QUERY_BUDGET: contextvars.ContextVar[_LogQueryBudget | None] = contextvars.ContextVar(
    "arc_log_query_budget",
    default=None,
)
_LOG_RANGE_HINT = re.compile(
    r"retry\s+with\s+(?:the\s+)?range\s+(0x[0-9a-f]+|\d+)\s*-\s*(0x[0-9a-f]+|\d+)",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_explicit_log_range_limit(error: RpcResponseError) -> bool:
    message = error.message.lower()
    return any(
        phrase in message
        for phrase in (
            "query exceeds max results",
            "request exceeded max allowed range",
            "too many results",
            "response size exceeded",
        )
    )


def _range_split_block(error: RpcResponseError, start: int, end: int) -> int:
    hint = _LOG_RANGE_HINT.search(error.message)
    if hint:
        try:
            hinted_start = int(hint.group(1), 16 if hint.group(1).lower().startswith("0x") else 10)
            hinted_end = int(hint.group(2), 16 if hint.group(2).lower().startswith("0x") else 10)
        except (ValueError, OverflowError):
            pass
        else:
            if hinted_start == start and start <= hinted_end < end:
                return hinted_end
    return start + ((end - start) // 2)


def _collect_topic_logs(
    rpc_url: str,
    start: int,
    end: int,
    topic_filter: list[object],
    timeout_seconds: float,
    query_budget: _LogQueryBudget,
) -> list[object]:
    pending = [(start, end)]
    collected: list[object] = []
    while pending:
        range_start, range_end = pending.pop(0)
        query_budget.claim(range_start, range_end)
        try:
            result = rpc_call(
                rpc_url,
                "eth_getLogs",
                [{"fromBlock": hex(range_start), "toBlock": hex(range_end), "topics": topic_filter}],
                timeout_seconds,
            )
        except RpcResponseError as exc:
            if not _is_explicit_log_range_limit(exc):
                raise
            if range_start == range_end:
                raise LogRangeProcessingError(
                    f"log_range_unresolved_single_block:{range_start}",
                    range_start,
                    range_end,
                    query_budget,
                    exc,
                ) from exc
            split_block = _range_split_block(exc, range_start, range_end)
            pending[0:0] = [
                (range_start, split_block),
                (split_block + 1, range_end),
            ]
            continue
        if not isinstance(result, list):
            raise RuntimeError(f"invalid_log_response:{topic_filter[0]}")
        collected.extend(result)
    return collected


def rpc_call(url: str, method: str, params: list[object], timeout_seconds: float) -> object:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "ArcMonitor/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("invalid_rpc_response")
    if payload.get("error") is not None:
        error = payload["error"]
        if not isinstance(error, Mapping):
            raise MalformedRpcResponseError(error)
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, bool) or not isinstance(code, (int, float)) or not isinstance(message, str):
            raise MalformedRpcResponseError(error)
        raise RpcResponseError(code, message, error.get("data"))
    if "result" not in payload:
        raise RuntimeError("missing_rpc_result")
    return payload["result"]


def validate_rpc_urls(
    urls: list[str], timeout_seconds: float
) -> tuple[list[str], list[dict[str, str]]]:
    valid: list[str] = []
    errors: list[dict[str, str]] = []
    for url in urls:
        try:
            chain_id = str(rpc_call(url, "eth_chainId", [], timeout_seconds)).lower()
            if chain_id != ARC_CHAIN_ID_HEX:
                errors.append({"url": url, "error": f"chain_id_mismatch:{chain_id}"})
            else:
                valid.append(url)
        except Exception as exc:  # noqa: BLE001 - endpoint isolation is intentional
            errors.append({"url": url, "error": str(exc)})
    return valid, errors


def _address(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text if EVM_ADDRESS.fullmatch(text) and text != ZERO_ADDRESS else None


def _topic_address(value: object) -> str | None:
    text = str(value or "").lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", text):
        return None
    return _address("0x" + text[-40:])


def _data_words(value: object) -> list[str]:
    text = str(value or "").lower()
    if not text.startswith("0x"):
        return []
    raw = text[2:]
    if len(raw) % 64 or not re.fullmatch(r"[0-9a-f]*", raw):
        return []
    return [raw[index : index + 64] for index in range(0, len(raw), 64)]


def _word_address(word: str) -> str | None:
    return _address("0x" + word[-40:]) if len(word) == 64 else None


def _base_log_row(log: dict[str, object], event_kind: str) -> dict[str, object]:
    transaction_hash = str(log.get("transactionHash") or "").strip().lower()
    log_value = log.get("logIndex")
    block_value = log.get("blockNumber")
    if not transaction_hash or log_value in (None, "") or block_value in (None, ""):
        raise UnresolvedCandidateError(f"incomplete_log_evidence:{event_kind}")
    try:
        log_index = int(str(log_value), 16)
        block_number = int(str(block_value), 16)
    except (TypeError, ValueError):
        raise UnresolvedCandidateError(f"incomplete_log_evidence:{event_kind}") from None
    if log_index < 0 or block_number < 0:
        raise UnresolvedCandidateError(f"incomplete_log_evidence:{event_kind}")
    return {
        "source": "arc_rpc",
        "event_kind": event_kind,
        "transaction_hash": transaction_hash,
        "log_index": hex(log_index),
        "block_number": block_number,
    }


def _candidates_from_log(log: dict[str, object], topic: str) -> list[dict[str, object]]:
    topics = log.get("topics") if isinstance(log.get("topics"), list) else []
    base: dict[str, object]
    candidates: list[str | None]
    pool_address: str | None = None
    if topic == TRANSFER_TOPIC:
        if len(topics) < 2 or str(topics[1]).lower() != ZERO_ADDRESS_TOPIC:
            return []
        base = _base_log_row(log, "erc20_mint")
        candidates = [_address(log.get("address"))]
    elif topic == PAIR_CREATED_TOPIC:
        words = _data_words(log.get("data"))
        base = _base_log_row(log, "v2_pair_created")
        candidates = [_topic_address(topics[1]) if len(topics) > 1 else None, _topic_address(topics[2]) if len(topics) > 2 else None]
        pool_address = _word_address(words[0]) if words else None
    elif topic == POOL_CREATED_TOPIC:
        words = _data_words(log.get("data"))
        base = _base_log_row(log, "v3_pool_created")
        candidates = [_topic_address(topics[1]) if len(topics) > 1 else None, _topic_address(topics[2]) if len(topics) > 2 else None]
        pool_address = _word_address(words[1]) if len(words) > 1 else None
    elif topic == INITIALIZE_TOPIC:
        base = _base_log_row(log, "v4_initialize")
        candidates = [_topic_address(topics[2]) if len(topics) > 2 else None, _topic_address(topics[3]) if len(topics) > 3 else None]
    else:
        return []
    rows = []
    for candidate in candidates:
        if candidate:
            row = {**base, "contract_address": candidate}
            if pool_address:
                row["pool_address"] = pool_address
            rows.append(row)
    return rows


def _decode_abi_string(value: object) -> str | None:
    text = str(value or "")
    if not text.startswith("0x"):
        return None
    try:
        raw = bytes.fromhex(text[2:])
    except ValueError:
        return None
    if len(raw) == 32:
        decoded = raw.rstrip(b"\x00")
    elif len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 > len(raw):
            return None
        length = int.from_bytes(raw[offset : offset + 32], "big")
        if offset + 32 + length > len(raw):
            return None
        decoded = raw[offset + 32 : offset + 32 + length]
    else:
        return None
    try:
        result = decoded.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return result or None


def _decode_abi_uint(value: object) -> int | None:
    text = str(value or "")
    if not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", text):
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _is_deterministic_contract_call_error(error: RpcResponseError) -> bool:
    message = error.message.strip().lower()
    return error.code == 3 or any(
        marker in message
        for marker in (
            "execution reverted",
            "invalid opcode",
            "selector not recognized",
            "function selector was not recognized",
            "function does not exist",
        )
    )


def _verify_erc20(
    rpc_url: str, contract_address: str, timeout_seconds: float
) -> VerificationResult:
    contract = _address(contract_address)
    if contract is None:
        return VerificationResult("rejected", errors=("invalid_address",))
    try:
        code = str(rpc_call(rpc_url, "eth_getCode", [contract, "latest"], timeout_seconds)).lower()
    except Exception as exc:  # noqa: BLE001 - transient transport errors remain unresolved
        return VerificationResult("unknown", errors=(f"eth_getCode:{exc}",))
    if code in {"", "0x", "0x0", "0x00"} or not code.startswith("0x"):
        return VerificationResult("rejected", errors=("empty_bytecode",))
    results: dict[str, object | None] = {}
    required_errors: list[str] = []
    for name, selector, decoder in (
        ("name", NAME_SELECTOR, _decode_abi_string),
        ("symbol", SYMBOL_SELECTOR, _decode_abi_string),
        ("decimals", DECIMALS_SELECTOR, _decode_abi_uint),
        ("total_supply", TOTAL_SUPPLY_SELECTOR, _decode_abi_uint),
    ):
        try:
            raw = rpc_call(
                rpc_url,
                "eth_call",
                [{"to": contract, "data": selector}, "latest"],
                timeout_seconds,
            )
            results[name] = decoder(raw)
        except RpcResponseError as exc:
            results[name] = None
            if name in {"decimals", "total_supply"}:
                if _is_deterministic_contract_call_error(exc):
                    return VerificationResult(
                        "rejected",
                        errors=(f"{name}:deterministic_call_rejection:{exc}",),
                    )
                required_errors.append(f"{name}:{exc}")
        except Exception as exc:  # noqa: BLE001 - metadata calls are independently isolated
            results[name] = None
            if name in {"decimals", "total_supply"}:
                required_errors.append(f"{name}:{exc}")
    if required_errors:
        return VerificationResult("unknown", errors=tuple(required_errors))
    decimals = results["decimals"]
    total_supply = results["total_supply"]
    if not isinstance(decimals, int) or decimals < 0 or decimals > 255 or not isinstance(total_supply, int):
        return VerificationResult("rejected", errors=("non_erc20_metadata",))
    return VerificationResult(
        "verified",
        metadata={key: value for key, value in results.items() if value is not None},
    )


def collect_rpc_events(
    rpc_url: str,
    from_block: int,
    to_block: int,
    timeout_seconds: float,
) -> list[dict[str, object]]:
    if to_block < from_block:
        return []
    query_budget = _ACTIVE_LOG_QUERY_BUDGET.get() or _LogQueryBudget(MAX_LOG_QUERIES_PER_CYCLE)
    raw_rows: list[dict[str, object]] = []
    topics = (TRANSFER_TOPIC, PAIR_CREATED_TOPIC, POOL_CREATED_TOPIC, INITIALIZE_TOPIC)
    start = max(0, int(from_block))
    final = min(max(0, int(to_block)), start + MAX_BLOCKS_PER_CYCLE - 1)
    while start <= final:
        end = min(final, start + MAX_BLOCK_SPAN - 1)
        for topic in topics:
            topic_filter: list[object] = [topic]
            if topic == TRANSFER_TOPIC:
                topic_filter.append(ZERO_ADDRESS_TOPIC)
            result = _collect_topic_logs(
                rpc_url,
                start,
                end,
                topic_filter,
                timeout_seconds,
                query_budget,
            )
            for log in result:
                if isinstance(log, dict):
                    raw_rows.extend(_candidates_from_log(log, topic))
        start = end + 1

    metadata: dict[str, VerificationResult] = {}
    verified: list[dict[str, object]] = []
    unresolved: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in raw_rows:
        contract = str(row["contract_address"])
        if contract not in metadata:
            metadata[contract] = _verify_erc20(rpc_url, contract, timeout_seconds)
        verification = metadata[contract]
        if verification.status == "unknown":
            unresolved.append(f"{contract}:{','.join(verification.errors)}")
            continue
        if verification.status == "rejected":
            continue
        key = (
            str(row.get("transaction_hash") or ""),
            str(row.get("log_index") or ""),
            contract,
            str(row.get("event_kind") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        verified.append({**row, **verification.metadata})
    if unresolved:
        raise UnresolvedCandidateError("unresolved_candidate:" + ";".join(sorted(set(unresolved))))
    return verified


def _http_json(url: str, timeout_seconds: float) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ArcMonitor/1.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _rows(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("data", "items", "results", "tokens", "contracts"):
        child = value.get(key)
        if isinstance(child, list):
            return [row for row in child if isinstance(row, dict)]
        if isinstance(child, dict):
            nested = _rows(child)
            if nested:
                return nested
    return []


def collect_arcscan_candidates(
    base_url: str, limit: int, timeout_seconds: float
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    if not str(base_url or "").strip():
        return [], []
    bounded_limit = max(1, min(int(limit), RECENT_EVENT_LIMIT))
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    successful = 0
    for kind in ("tokens", "smart-contracts"):
        url = f"{base_url.rstrip('/')}/{kind}?{urllib.parse.urlencode({'limit': bounded_limit})}"
        try:
            payload = _http_json(url, timeout_seconds)
            successful += 1
            for raw in _rows(payload)[:bounded_limit]:
                nested_address = raw.get("address")
                if isinstance(nested_address, Mapping):
                    nested_address = nested_address.get("hash")
                contract = (
                    raw.get("contract_address")
                    or raw.get("contractAddress")
                    or raw.get("token_address")
                    or raw.get("tokenAddress")
                    or raw.get("address_hash")
                    or nested_address
                )
                item = dict(raw)
                item.update(
                    {
                        "contract_address": str(contract or "").lower(),
                        "source": "arc_arcscan",
                        "event_kind": f"arcscan_{kind.rstrip('s')}",
                        "arcscan_id": str(raw.get("id") or raw.get("identifier") or contract or ""),
                    }
                )
                field_sources = dict(item.get("field_sources") or {})
                for canonical, aliases in ARCSCAN_FIELD_ALIASES.items():
                    value = next(
                        (raw[alias] for alias in aliases if raw.get(alias) is not None),
                        None,
                    )
                    if value is None:
                        continue
                    item[canonical] = value
                    field_sources[canonical] = "arc_arcscan"
                if item.get("holder_count") is not None:
                    item["holders"] = item["holder_count"]
                    field_sources["holders"] = "arc_arcscan"
                if field_sources:
                    item["field_sources"] = field_sources
                rows.append(item)
        except Exception as exc:  # noqa: BLE001 - indexes are independent
            errors.append({"url": url, "error": str(exc)})
    if successful and not rows:
        errors.append({"url": base_url, "error": "arcscan_lag"})
    return rows[: bounded_limit * 2], errors


def _stable_event_id(row: dict[str, object]) -> str:
    material = "|".join(
        (
            "arc",
            str(row.get("transaction_hash") or row.get("tx_hash") or row.get("arcscan_id") or row.get("id") or "").lower(),
            str(row.get("log_index") or row.get("event_index") or ""),
            str(row.get("contract_address") or "").lower(),
            str(row.get("event_kind") or row.get("event_type") or "discovery").lower(),
        )
    )
    return "arc:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_discoveries(
    rpc_rows: list[dict[str, object]],
    arcscan_rows: list[dict[str, object]],
    observed_at: str,
) -> list[dict[str, object]]:
    identities: dict[str, dict[str, object]] = {}
    for raw in [*rpc_rows, *arcscan_rows]:
        contract = _address(raw.get("contract_address"))
        if contract is None:
            continue
        source = str(raw.get("source") or ("arc_arcscan" if raw in arcscan_rows else "arc_rpc"))
        event_id = _stable_event_id({**raw, "contract_address": contract})
        identity = f"arc:{contract}"
        row = identities.get(identity)
        if row is None:
            row = {
                "chain": "arc",
                "chainId": "arc",
                "contract_address": contract,
                "tokenAddress": contract,
                "identity": identity,
                "provider_family": "onchain",
                "provider_feeds": [],
                "event_ids": [],
                "event_at": observed_at,
                "observed_at": observed_at,
                "evidence_role": "discovery",
                "signal_lane": "new_launch",
                "market_data_pending": True,
            }
            identities[identity] = row
        if source not in row["provider_feeds"]:
            row["provider_feeds"].append(source)
        if event_id not in row["event_ids"]:
            row["event_ids"].append(event_id)
        raw_field_sources = (
            raw.get("field_sources")
            if isinstance(raw.get("field_sources"), dict)
            else {}
        )
        row_field_sources = row.setdefault("field_sources", {})
        for key in (
            "name",
            "symbol",
            "decimals",
            "total_supply",
            "holders",
            "holder_count",
            "first_transfer_block",
            "last_transfer_block",
            "transfer_count",
            "transfer_count_24h",
            "activity_count",
            "pool_address",
            "block_number",
        ):
            if key in raw and raw[key] is not None and key not in row:
                row[key] = raw[key]
                row_field_sources[key] = raw_field_sources.get(key) or source
    normalized = []
    for identity in sorted(identities):
        row = identities[identity]
        row["provider_feeds"] = sorted(row["provider_feeds"])
        row["event_ids"] = sorted(row["event_ids"])[-MAX_EVENT_IDS_PER_IDENTITY:]
        row["provider_feed"] = row["provider_feeds"][0]
        row["source"] = row["provider_feed"]
        row["event_id"] = row["event_ids"][0]
        row["event_type"] = "new_launch"
        normalized.append(row)
    return normalized


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _preserved_count(path: Path) -> int:
    data = _read_json(path).get("data")
    return len(data) if isinstance(data, list) else 0


def _enrich_arcscan_rows(
    rows: list[dict[str, object]], rpc_url: str, timeout_seconds: float
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    metadata: dict[str, VerificationResult] = {}
    enriched: list[dict[str, object]] = []
    unresolved: list[dict[str, str]] = []
    for row in rows:
        contract = _address(row.get("contract_address"))
        if contract is None:
            continue
        if contract not in metadata:
            metadata[contract] = _verify_erc20(rpc_url, contract, timeout_seconds)
        verification = metadata[contract]
        if verification.status == "unknown":
            unresolved.append(
                {
                    "url": rpc_url,
                    "error": f"unresolved_candidate:{contract}:{','.join(verification.errors)}",
                }
            )
        elif verification.status == "verified":
            enriched_row = {**row, "contract_address": contract}
            field_sources = dict(enriched_row.get("field_sources") or {})
            for key, value in verification.metadata.items():
                if enriched_row.get(key) is None:
                    enriched_row[key] = value
                    field_sources[key] = "arc_rpc"
            if field_sources:
                enriched_row["field_sources"] = field_sources
            enriched.append(enriched_row)
    return enriched, unresolved


def run_once(
    out_dir: Path,
    rpc_urls: list[str],
    arcscan_base_url: str,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, object]:
    output = Path(out_dir)
    inbox_path = output / "meme-source-inbox" / "arc-onchain.json"
    status_path = output / "arc-public-chain-poll-status.json"
    state_path = output / "arc-public-chain-poll-state.json"
    observed_at = _utc_now()
    previous_state = _read_json(state_path)
    valid_urls, rpc_errors = validate_rpc_urls(list(rpc_urls or DEFAULT_RPC_URLS), timeout_seconds)
    query_budget = _LogQueryBudget(MAX_LOG_QUERIES_PER_CYCLE)
    if not valid_urls:
        status: dict[str, object] = {
            "status": "error",
            "ok": False,
            "checkpoint_advanced": False,
            "observed_at": observed_at,
            "rpc_errors": rpc_errors,
            "arcscan_errors": [],
            "log_queries_used": query_budget.used,
            "log_query_budget": query_budget.limit,
            "preserved_previous_rows": _preserved_count(inbox_path),
        }
        _atomic_json(status_path, status)
        return status

    validated_chain_id = ARC_CHAIN_ID_HEX

    rpc_url = ""
    head = -1
    processed_to_block = -1
    rpc_rows: list[dict[str, object]] = []
    last_completed = previous_state.get("last_completed_block")
    checkpoint = int(last_completed) if isinstance(last_completed, int) and last_completed >= 0 else None
    from_block = 0
    unresolved_contracts: set[str] = set()
    budget_token = _ACTIVE_LOG_QUERY_BUDGET.set(query_budget)
    try:
        for candidate_url in valid_urls:
            try:
                head = int(str(rpc_call(candidate_url, "eth_blockNumber", [], timeout_seconds)), 16)
                from_block = (
                    max(0, checkpoint - OVERLAP_BLOCKS + 1)
                    if checkpoint is not None
                    else max(0, head - MAX_BLOCK_SPAN + 1)
                )
                processed_to_block = min(head, from_block + MAX_BLOCKS_PER_CYCLE - 1)
                rpc_rows = collect_rpc_events(
                    candidate_url, from_block, processed_to_block, timeout_seconds
                )
                if unresolved_contracts:
                    fallback_contracts = {
                        str(row.get("contract_address") or "").lower()
                        for row in rpc_rows
                        if isinstance(row, dict)
                    }
                    missing = sorted(unresolved_contracts - fallback_contracts)
                    if missing:
                        rpc_errors.append({
                            "url": candidate_url,
                            "error": "fallback_missing_unresolved_candidates:" + ",".join(missing),
                        })
                        continue
                rpc_url = candidate_url
                break
            except UnresolvedCandidateError as exc:
                rpc_errors.append({"url": candidate_url, "error": str(exc)})
                observed_contracts = {
                    value.lower()
                    for value in re.findall(r"0x[0-9a-fA-F]{40}", str(exc))
                }
                if not observed_contracts:
                    break
                unresolved_contracts.update(observed_contracts)
            except Exception as exc:  # noqa: BLE001 - try the next validated endpoint
                rpc_errors.append({"url": candidate_url, "error": str(exc)})
    finally:
        _ACTIVE_LOG_QUERY_BUDGET.reset(budget_token)
    if not rpc_url:
        status = {
            "status": "error",
            "ok": False,
            "chain_id": validated_chain_id,
            "checkpoint_advanced": False,
            "observed_at": observed_at,
            "rpc_errors": rpc_errors,
            "arcscan_errors": [],
            "from_block": from_block,
            "head_block": head,
            "checkpoint_block": checkpoint,
            "log_queries_used": query_budget.used,
            "log_query_budget": query_budget.limit,
            "preserved_previous_rows": _preserved_count(inbox_path),
        }
        _atomic_json(status_path, status)
        return status

    arcscan_rows, arcscan_errors = collect_arcscan_candidates(
        arcscan_base_url, RECENT_EVENT_LIMIT // 2, timeout_seconds
    )
    verified_arcscan, unresolved_arcscan = _enrich_arcscan_rows(
        arcscan_rows, rpc_url, timeout_seconds
    )
    if unresolved_arcscan:
        arcscan_errors.extend(unresolved_arcscan)
        status = {
            "status": "degraded",
            "ok": False,
            "chain_id": validated_chain_id,
            "checkpoint_advanced": False,
            "observed_at": observed_at,
            "rpc_url": rpc_url,
            "rpc_errors": rpc_errors,
            "arcscan_errors": arcscan_errors,
            "from_block": from_block,
            "head_block": head,
            "checkpoint_block": checkpoint,
            "log_queries_used": query_budget.used,
            "log_query_budget": query_budget.limit,
            "preserved_previous_rows": _preserved_count(inbox_path),
        }
        _atomic_json(status_path, status)
        return status
    recent_ids = {
        str(value)
        for value in previous_state.get("recent_event_ids", [])
        if isinstance(value, str)
    }
    new_rpc = [row for row in rpc_rows if _stable_event_id(row) not in recent_ids]
    new_arcscan = [row for row in verified_arcscan if _stable_event_id(row) not in recent_ids]
    rows = normalize_discoveries(new_rpc, new_arcscan, observed_at)[:RECENT_EVENT_LIMIT]
    all_ids = [_stable_event_id(row) for row in [*rpc_rows, *verified_arcscan]]
    combined_ids = list(dict.fromkeys([*previous_state.get("recent_event_ids", []), *all_ids]))[-RECENT_EVENT_LIMIT:]
    inbox: dict[str, object] = {
        "source": "arc_onchain",
        "observed_at": observed_at,
        "count": len(rows),
        "data": rows,
    }
    state_status = "degraded" if rpc_errors or arcscan_errors else "ok"
    status = {
        "status": state_status,
        "ok": True,
        "chain_id": validated_chain_id,
        "checkpoint_advanced": True,
        "observed_at": observed_at,
        "rpc_url": rpc_url,
        "rpc_errors": rpc_errors,
        "arcscan_errors": arcscan_errors,
        "from_block": from_block,
        "head_block": head,
        "checkpoint_block": processed_to_block,
        "log_queries_used": query_budget.used,
        "log_query_budget": query_budget.limit,
        "row_count": len(rows),
    }
    new_state: dict[str, object] = {
        "last_completed_block": processed_to_block,
        "recent_event_ids": combined_ids,
        "endpoint_health": {
            "rpc_url": rpc_url,
            "rpc_errors": rpc_errors,
            "arcscan_errors": arcscan_errors,
            "status": state_status,
        },
        "updated_at": observed_at,
    }
    precommit_status = {
        **status,
        "status": "degraded",
        "ok": False,
        "checkpoint_advanced": False,
        "checkpoint_block": checkpoint,
        "error": "checkpoint_commit_pending",
    }
    _atomic_json(status_path, precommit_status)
    _atomic_json(inbox_path, inbox)
    _atomic_json(state_path, new_state)
    _atomic_json(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only ARC public-chain discovery poller.")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--rpc-url", dest="rpc_urls", action="append", default=[])
    parser.add_argument("--arcscan-base-url", default=DEFAULT_ARCSCAN_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=REQUEST_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status = run_once(
        out_dir=args.out_dir,
        rpc_urls=args.rpc_urls or list(DEFAULT_RPC_URLS),
        arcscan_base_url=args.arcscan_base_url,
        timeout_seconds=args.timeout_seconds,
    )
    return 0 if status.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
