"""Single-writer BSC paper and shadow execution worker."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from alpha_bsc_execution_policy import ExecutionConfig, entry_decision, exit_decision, token_key


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs" / "bsc-execution-input.json"
DEFAULT_OUT_DIR = ROOT / "outputs" / "bsc-execution"
CN_TZ = timezone(timedelta(hours=8))
UTC = timezone.utc
STATE_VERSION = 1
INITIAL_CASH_USD = 100.0
BASE_SLIPPAGE = 0.005
FEE_RATE = 0.003
TOKEN_TAX_RATE = 0.01
GAS_USD = 0.12
EVENT_TYPES = {"intent", "fill", "exit_intent", "exit_fill", "shadow_fill", "shadow_exit"}
ORDER_STATUSES = {"pending", "filled", "shadow_filled", "shadow_observed"}
LIVE_PUBLIC_STATUSES = {"confirmed", "error", "pending", "prepared", "rejected", "reverted", "unknown"}
LIVE_PUBLIC_ERRORS = {
    "address_mismatch", "broadcast_failed", "invalid_amount", "invalid_calldata", "invalid_counter_token",
    "invalid_derived_address", "invalid_private_key", "invalid_pool", "invalid_raw_transaction",
    "invalid_router_config", "invalid_rpc_response", "invalid_signed_transaction", "invalid_slippage",
    "invalid_token", "invalid_transaction", "invalid_transaction_from", "invalid_transaction_hash",
    "invalid_value", "invalid_wallet_address", "live_disabled", "missing_eth_account", "missing_private_key",
    "missing_rpc_url", "missing_unsigned_transaction", "okx_build_failed", "okx_context_mismatch",
    "opaque_error", "pool_unverifiable", "prepared_invalid", "receipt_failed", "receipt_timeout", "rpc_error",
    "rpc_timeout", "router_not_allowed", "signing_failed", "transaction_from_mismatch", "transaction_reverted",
    "value_mismatch", "calldata_mismatch", "calldata_unsupported", "unknown_receipt_status", "wrong_chain",
    "invalid_live_result", "live_adapter_error",
}
LIVE_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
LIVE_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
LIVE_AMOUNT_RE = re.compile(r"^[0-9]+$")
LIVE_SLIPPAGE_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?)$")


class LedgerError(ValueError):
    """The authoritative ledger is corrupt or internally ambiguous."""


class InputError(ValueError):
    """The current input round is ambiguous or fails a required market check."""

    def __init__(self, reason: str, identity: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.identity = identity


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ)).astimezone(UTC)


def _iso(value: datetime) -> str:
    current = value if value.tzinfo else value.replace(tzinfo=CN_TZ)
    return current.isoformat()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    chain = str(row.get("chain") or row.get("network") or "").strip().lower()
    token = str(row.get("contract_address") or row.get("token_address") or row.get("address") or "").strip().lower()
    pool = str(row.get("pool_address") or row.get("pair_address") or "").strip().lower()
    return chain, token, pool


def idempotency_key(row: Mapping[str, Any], side: str) -> str:
    chain, token, pool = _identity(row)
    signal_at = str(row.get("signal_at") or row.get("quote_at") or row.get("quote_observed_at") or "").strip()
    return f"{chain}:{token}:{pool}:{signal_at}:{side.lower()}"


def _quote_price(quote: Mapping[str, Any]) -> float:
    return _number(quote.get("price_usd") if quote.get("price_usd") is not None else quote.get("price"))


def _quote_evidence(quote: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "chain", "contract_address", "token_address", "address", "pool_address", "pair_address",
        "quote_at", "quote_observed_at", "quote_status", "price_usd", "price", "liquidity_usd",
        "liquidity", "volume5m", "buy_count5m", "sell_count5m", "price_impact", "depth_impact", "quote_source",
        "quote_fingerprint", "impact_notional_usd", "impact_source", "price_impact_source",
    }
    return {key: value for key, value in quote.items() if key in allowed}


def _observed_at(quote: Mapping[str, Any]) -> datetime | None:
    return _stamp(quote.get("quote_observed_at"))


def _impact_values(quote: Mapping[str, Any]) -> tuple[float, float] | None:
    price_value = quote.get("price_impact") if "price_impact" in quote else quote.get("price_impact_percentage")
    depth_value = quote.get("depth_impact")
    if isinstance(price_value, bool) or isinstance(depth_value, bool) or price_value is None or depth_value is None:
        return None
    price = _number(price_value, math.nan)
    depth = _number(depth_value, math.nan)
    if not math.isfinite(price) or not math.isfinite(depth) or price < 0 or depth < 0 or price > 100 or depth > 100:
        return None
    return price / 100.0, depth / 100.0


def _fresh_quote(quote: Mapping[str, Any], now: datetime, config: ExecutionConfig) -> bool:
    observed = _observed_at(quote)
    if observed is None:
        return False
    current = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=CN_TZ).astimezone(UTC)
    age = (current - observed).total_seconds()
    return (
        0 <= age <= config.max_quote_age_seconds
        and str(quote.get("quote_status") or "").lower() == "fresh"
        and _quote_price(quote) > 0
        and _impact_values(quote) is not None
    )


def _day(now: datetime) -> str:
    current = now if now.tzinfo else now.replace(tzinfo=CN_TZ)
    return current.astimezone(CN_TZ).date().isoformat()


def _default_state(now: datetime) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at": _iso(now),
        "cash_usd": INITIAL_CASH_USD,
        "realized_pnl_usd": 0.0,
        "daily_loss_usd": 0.0,
        "daily_loss_date": _day(now),
        "positions": {},
        "orders": {},
        "pending_intents": {},
        "sequence": 0,
        "last_tx_id": None,
    }


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class _WriterLock:
    def __init__(self, out_dir: Path):
        digest = hashlib.sha256(str(out_dir.resolve()).encode("utf-8")).hexdigest()[:24]
        self.out_dir = out_dir
        self.name = f"Local\\AlphaExecutionWorker-{digest}"
        self.handle: Any = None
        self.lock_path = out_dir / ".worker.lock"
        self.lock_stream: Any = None
        self.acquired = False

    def __enter__(self) -> bool:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            self.handle = kernel.CreateMutexW(None, True, self.name)
            if not self.handle:
                return False
            already_exists = ctypes.get_last_error() == 183
            if already_exists:
                wait_result = kernel.WaitForSingleObject(self.handle, 0)
                if wait_result != 0:
                    kernel.CloseHandle(self.handle)
                    self.handle = None
                    return False
            self.acquired = True
            return True
        try:
            self.lock_stream = self.lock_path.open("x", encoding="ascii")
        except FileExistsError:
            return False
        self.acquired = True
        return True

    def __exit__(self, *_args: Any) -> None:
        if not self.acquired:
            return
        if os.name == "nt" and self.handle:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ReleaseMutex(self.handle)
            kernel.CloseHandle(self.handle)
        elif self.lock_stream:
            self.lock_stream.close()
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False


def _safe_live_result(value: Any) -> dict[str, Any]:
    """Keep only adapter fields that are safe to persist in public artifacts."""
    if not isinstance(value, Mapping):
        return {"ok": False, "status": "error", "error": "invalid_live_result"}
    result: dict[str, Any] = {}
    if isinstance(value.get("ok"), bool):
        result["ok"] = value["ok"]
    status = value.get("status")
    if isinstance(status, str) and status in LIVE_PUBLIC_STATUSES:
        result["status"] = status
    error = value.get("error")
    if isinstance(error, str) and error in LIVE_PUBLIC_ERRORS:
        result["error"] = error
    side = value.get("side")
    if side in {"buy", "sell"}:
        result["side"] = side
    chain_id = value.get("chain_id")
    if chain_id == 56 or chain_id == "56":
        result["chain_id"] = 56
    for field in ("token_address", "pool_address"):
        address = value.get(field)
        if isinstance(address, str) and LIVE_ADDRESS_RE.fullmatch(address.strip()):
            result[field] = address.strip().lower()
    amount = value.get("amount_atomic")
    if isinstance(amount, int) and not isinstance(amount, bool):
        amount = str(amount)
    if isinstance(amount, str) and LIVE_AMOUNT_RE.fullmatch(amount) and int(amount) > 0:
        result["amount_atomic"] = str(int(amount))
    slippage = value.get("slippage_percent")
    if isinstance(slippage, (int, float, str)) and not isinstance(slippage, bool):
        text = str(slippage).strip()
        try:
            valid_slippage = LIVE_SLIPPAGE_RE.fullmatch(text) and 0 <= float(text) <= 50
        except (TypeError, ValueError):
            valid_slippage = False
        if valid_slippage:
            result["slippage_percent"] = text
    tx_hash = value.get("tx_hash")
    if isinstance(tx_hash, str) and LIVE_TX_HASH_RE.fullmatch(tx_hash):
        result["tx_hash"] = "0x" + tx_hash[2:].lower()
    receipt = value.get("receipt")
    if isinstance(receipt, Mapping):
        safe_receipt: dict[str, Any] = {}
        receipt_status = receipt.get("status")
        if receipt_status in {"0x0", "0x1", 0, 1}:
            safe_receipt["status"] = "0x1" if receipt_status in {"0x1", 1} else "0x0"
        for field in ("blockHash", "transactionHash"):
            item = receipt.get(field)
            if isinstance(item, str) and LIVE_TX_HASH_RE.fullmatch(item):
                safe_receipt[field] = "0x" + item[2:].lower()
        block_number = receipt.get("blockNumber")
        if isinstance(block_number, int) and not isinstance(block_number, bool) and 0 <= block_number <= 2**64 - 1:
            safe_receipt["blockNumber"] = hex(block_number)
        elif isinstance(block_number, str) and re.fullmatch(r"0x[0-9a-fA-F]+", block_number):
            number = int(block_number, 16)
            if number <= 2**64 - 1:
                safe_receipt["blockNumber"] = hex(number)
        if safe_receipt:
            result["receipt"] = safe_receipt
    if "status" not in result:
        result["status"] = "error"
    if result.get("ok") is False and "error" not in result:
        result["error"] = "opaque_error"
    return result


def _safe_preflight(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"ready": False, "checks": {"adapter": {"ok": False, "reason": "missing"}}, "missing": ["adapter"], "reasons": []}
    checks: dict[str, dict[str, Any]] = {}
    raw_checks = value.get("checks")
    if isinstance(raw_checks, Mapping):
        for name, check in raw_checks.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", name) or not isinstance(check, Mapping):
                continue
            item = {"ok": check.get("ok") is True}
            reason = check.get("reason")
            if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", reason):
                item["reason"] = reason
            checks[name] = item
    missing = [item for item in value.get("missing", []) if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", item)]
    reasons = [item for item in value.get("reasons", []) if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", item)]
    ready = value.get("ready") is True and all(item.get("ok") is True for item in checks.values())
    return {"ready": ready, "checks": checks, "missing": missing, "reasons": reasons}


class ExecutionWorker:
    def __init__(
        self,
        input_path: Path,
        out_dir: Path,
        client: Any,
        mode: str = "paper",
        live_preflight: Mapping[str, Any] | None = None,
    ):
        if mode not in {"paper", "shadow", "live"}:
            raise ValueError("mode must be paper, shadow, or live")
        self.input_path = Path(input_path)
        self.out_dir = Path(out_dir)
        self.client = client
        self.mode = mode
        self.live_preflight = _safe_preflight(live_preflight) if live_preflight is not None else None
        self.config = ExecutionConfig.paper()
        self.state_path = self.out_dir / "state.json"
        self.events_path = self.out_dir / "events.jsonl"
        self.report_path = self.out_dir / "report.json"
        self.health_path = self.out_dir / "health.json"

    def _live_preflight(self) -> dict[str, Any]:
        if self.live_preflight is not None:
            preflight = dict(self.live_preflight)
        else:
            has_interface = callable(getattr(self.client, "buy", None)) and callable(getattr(self.client, "sell", None))
            preflight = {
                "ready": has_interface,
                "checks": {"adapter_interface": {"ok": has_interface, **({} if has_interface else {"reason": "missing"})}},
                "missing": [] if has_interface else ["adapter_interface"],
                "reasons": [],
            }
        has_interface = callable(getattr(self.client, "buy", None)) and callable(getattr(self.client, "sell", None))
        # An injected preflight is authoritative for deployment checks. Only
        # extend a ready preflight with the adapter-interface guard.
        if self.live_preflight is None and not has_interface:
            preflight["ready"] = False
            preflight.setdefault("checks", {})["adapter_interface"] = {"ok": False, "reason": "missing"}
            if "adapter_interface" not in preflight.setdefault("missing", []):
                preflight["missing"].append("adapter_interface")
        elif self.live_preflight is not None and preflight.get("ready") is True and not has_interface:
            preflight["ready"] = False
            preflight.setdefault("checks", {})["adapter_interface"] = {"ok": False, "reason": "missing"}
            if "adapter_interface" not in preflight.setdefault("missing", []):
                preflight["missing"].append("adapter_interface")
        armed = getattr(self.client, "armed", None)
        if armed is not None and armed is not True:
            preflight["ready"] = False
            preflight.setdefault("checks", {})["adapter_armed"] = {"ok": False, "reason": "not_true"}
            if "adapter_armed" not in preflight.setdefault("missing", []):
                preflight["missing"].append("adapter_armed")
        return _safe_preflight(preflight)

    @staticmethod
    def _live_order_parameters(row: Mapping[str, Any], side: str, fraction: float = 1.0) -> tuple[dict[str, Any] | None, str | None]:
        if side == "buy":
            amount_value = row.get("amount_atomic") or row.get("entry_amount_atomic") or row.get("native_amount_atomic")
        else:
            amount_value = row.get("sell_amount_atomic") or row.get("token_amount_atomic") or row.get("amount_atomic")
        if isinstance(amount_value, bool) or amount_value is None:
            return None, "missing_live_order_parameters"
        try:
            amount = int(str(amount_value).strip(), 10)
        except (TypeError, ValueError):
            return None, "invalid_live_order_parameters"
        if amount <= 0:
            return None, "invalid_live_order_parameters"
        if side == "sell":
            amount = math.floor(amount * max(0.0, min(1.0, fraction)))
            if amount <= 0:
                return None, "invalid_live_order_parameters"
        slippage_value = row.get("slippage_percent") or row.get("execution_slippage_percent")
        if slippage_value is None:
            return None, "missing_live_order_parameters"
        slippage = str(slippage_value).strip()
        try:
            if not LIVE_SLIPPAGE_RE.fullmatch(slippage) or not 0 <= float(slippage) <= 50:
                return None, "invalid_live_order_parameters"
        except (TypeError, ValueError):
            return None, "invalid_live_order_parameters"
        params: dict[str, Any] = {
            "amount_atomic": str(amount),
            "slippage_percent": slippage,
            "chain_id": 56,
        }
        if side == "buy":
            counter = row.get("from_token_address")
            if counter is not None:
                params["from_token_address"] = counter
        else:
            counter = row.get("to_token_address")
            if counter is not None:
                params["to_token_address"] = counter
        return params, None

    def _invoke_live(self, side: str, row: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
        method = getattr(self.client, side, None)
        if not callable(method):
            return {"ok": False, "status": "error", "error": "live_adapter_error"}
        request = {
            "token_address": _identity(row)[1],
            "pool_address": _identity(row)[2],
            **dict(params),
        }
        try:
            if side == "buy":
                result = method(
                    request["token_address"], request["pool_address"], request["amount_atomic"],
                    request["slippage_percent"], chain_id=request["chain_id"],
                    **({"from_token_address": request["from_token_address"]} if "from_token_address" in request else {}),
                )
            else:
                result = method(
                    request["token_address"], request["pool_address"], request["amount_atomic"],
                    request["slippage_percent"], chain_id=request["chain_id"],
                    **({"to_token_address": request["to_token_address"]} if "to_token_address" in request else {}),
                )
        except Exception:
            return {"ok": False, "status": "error", "error": "live_adapter_error"}
        return _safe_live_result(result)

    def _load_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise LedgerError("events_unreadable") from exc
        event_ids: set[int] = set()
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError("events_corrupt") from exc
            if not isinstance(event, dict) or not event.get("idempotency_key"):
                raise LedgerError("events_ambiguous")
            if event.get("type") not in EVENT_TYPES:
                raise LedgerError("event_type")
            event_id = event.get("id")
            if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
                raise LedgerError("event_id_format")
            if event_id in event_ids:
                raise LedgerError("event_id_duplicate")
            event_ids.add(event_id)
            if _stamp(event.get("time")) is None:
                raise LedgerError("event_schema")
            if not isinstance(event.get("tx_id"), str) or not event["tx_id"]:
                raise LedgerError("event_transaction")
            if not isinstance(event.get("tx_seq"), int) or not isinstance(event.get("tx_size"), int):
                raise LedgerError("event_transaction")
            if event["tx_seq"] <= 0 or event["tx_size"] <= 0 or event["tx_seq"] > event["tx_size"]:
                raise LedgerError("event_transaction")
            if not isinstance(event.get("tx_complete"), bool):
                raise LedgerError("event_transaction")
            events.append(event)
        grouped: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        closed: set[str] = set()
        current_tx: str | None = None
        for event in events:
            tx_id = event["tx_id"]
            if current_tx is not None and tx_id != current_tx:
                closed.add(current_tx)
            if tx_id in closed:
                raise LedgerError("duplicate_transaction")
            if tx_id not in grouped:
                grouped[tx_id] = []
                order.append(tx_id)
            elif grouped[tx_id][-1]["tx_seq"] >= event["tx_seq"]:
                raise LedgerError("duplicate_transaction")
            grouped[tx_id].append(event)
            current_tx = tx_id
        for tx_id, batch in grouped.items():
            expected = list(range(1, len(batch) + 1))
            if [event["tx_seq"] for event in batch] != expected or any(event["tx_size"] != len(batch) for event in batch):
                raise LedgerError("partial_transaction")
            if any(event["tx_complete"] for event in batch[:-1]) or not batch[-1]["tx_complete"]:
                raise LedgerError("partial_transaction")
        return events

    @staticmethod
    def _validate_position(key: str, position: Any, orders: Mapping[str, Any]) -> None:
        if not isinstance(position, dict) or position.get("position_id") != key:
            raise LedgerError("position_schema")
        required = {"position_id", "chain", "contract_address", "pool_address", "signal_at", "entry_at", "entry_price_usd", "entry_fill_price_usd", "quote_price_usd", "price_impact_rate", "depth_impact_rate", "quantity", "entry_notional_usd", "entry_fee_usd", "entry_gas_usd", "high_price_usd", "tp1_hit", "remaining_fraction"}
        if not required.issubset(position) or str(position.get("chain")).lower() != "bsc":
            raise LedgerError("position_schema")
        if not all(isinstance(position.get(field), str) and position[field] for field in ("contract_address", "pool_address", "signal_at", "entry_at")):
            raise LedgerError("position_schema")
        if _stamp(position["signal_at"]) is None or _stamp(position["entry_at"]) is None:
            raise LedgerError("position_schema")
        numeric = ("entry_price_usd", "entry_fill_price_usd", "quote_price_usd", "price_impact_rate", "depth_impact_rate", "quantity", "entry_notional_usd", "entry_fee_usd", "entry_gas_usd", "high_price_usd", "remaining_fraction")
        if any(not math.isfinite(_number(position.get(field), math.nan)) for field in numeric):
            raise LedgerError("position_schema")
        if any(_number(position.get(field)) <= 0 for field in ("entry_price_usd", "entry_fill_price_usd", "quote_price_usd", "quantity", "entry_notional_usd")):
            raise LedgerError("position_schema")
        if _number(position.get("price_impact_rate")) < 0 or _number(position.get("depth_impact_rate")) < 0 or not 0 < _number(position.get("remaining_fraction")) <= 1 or not isinstance(position.get("tp1_hit"), bool):
            raise LedgerError("position_schema")
        if key not in orders or orders[key].get("side") != "buy" or orders[key].get("status") != "filled":
            raise LedgerError("position_order_mismatch")

    @staticmethod
    def _validate_order(key: str, order: Any) -> None:
        if not isinstance(order, dict) or order.get("idempotency_key") != key:
            raise LedgerError("order_schema")
        required = {"idempotency_key", "side", "status", "chain", "contract_address", "pool_address", "signal_at", "intent_at", "quote_at", "observed_at", "quote"}
        if not required.issubset(order) or order.get("side") not in {"buy", "sell"} or order.get("status") not in ORDER_STATUSES:
            raise LedgerError("order_schema")
        if str(order.get("chain")).lower() != "bsc" or not all(isinstance(order.get(field), str) and order[field] for field in ("contract_address", "pool_address", "signal_at", "intent_at", "quote_at")):
            raise LedgerError("order_schema")
        if any(_stamp(order[field]) is None for field in ("signal_at", "intent_at", "quote_at", "observed_at")) or not isinstance(order.get("quote"), dict):
            raise LedgerError("order_schema")
        if order["side"] == "sell" and (not order.get("position_id") or not isinstance(order.get("sell_fraction"), (int, float)) or not order.get("reason")):
            raise LedgerError("order_schema")
        expected = idempotency_key(order, order["side"])
        if expected != key or _impact_values(order["quote"]) is None:
            raise LedgerError("order_schema")

    @classmethod
    def _validate_consistency(cls, state: Mapping[str, Any], events: list[dict[str, Any]]) -> None:
        latest_tx = events[-1]["tx_id"] if events else None
        if state.get("last_tx_id") != latest_tx:
            raise LedgerError("transaction_mismatch")
        event_groups: dict[str, list[str]] = {}
        for event in events:
            event_groups.setdefault(event["idempotency_key"], []).append(event["type"])
        for key, types in event_groups.items():
            if key not in state["orders"]:
                raise LedgerError("event_order_mismatch")
            if types not in (["intent"], ["intent", "fill"], ["intent", "shadow_fill"], ["exit_intent"], ["exit_intent", "exit_fill"], ["exit_intent", "shadow_exit"]):
                raise LedgerError("duplicate_submission")
        expected_by_status = {
            "pending": ["intent"],
            "filled": ["intent", "fill"],
            "shadow_filled": ["intent", "shadow_fill"],
            "shadow_observed": ["exit_intent", "shadow_exit"],
        }
        for key, order in state["orders"].items():
            cls._validate_order(key, order)
            types = event_groups.get(key, [])
            expected = expected_by_status[order["status"]]
            if order["side"] == "sell" and order["status"] in {"pending", "filled"}:
                expected = ["exit_intent"] if order["status"] == "pending" else ["exit_intent", "exit_fill"]
            if types != expected:
                raise LedgerError("order_event_mismatch")
        for key, intent in state["pending_intents"].items():
            cls._validate_order(key, intent)
            if state["orders"].get(key) != intent or intent["status"] != "pending":
                raise LedgerError("pending_order_mismatch")
        for key, position in state["positions"].items():
            cls._validate_position(key, position, state["orders"])

    def _load_state(self, now: datetime) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not self.state_path.exists():
            existing_events = self._load_events()
            if existing_events:
                raise LedgerError("state_missing_with_events")
            return _default_state(now), []
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LedgerError("state_corrupt") from exc
        if not isinstance(state, dict):
            raise LedgerError("state_ambiguous")
        required = {"version", "cash_usd", "realized_pnl_usd", "daily_loss_usd", "daily_loss_date", "positions", "orders", "pending_intents", "sequence", "last_tx_id"}
        if state.get("version") != STATE_VERSION or not required.issubset(state):
            raise LedgerError("state_schema")
        if not all(isinstance(state.get(field), dict) for field in ("positions", "orders", "pending_intents")):
            raise LedgerError("state_collections")
        if not isinstance(state.get("sequence"), int) or state["sequence"] < 0 or (state.get("last_tx_id") is not None and not isinstance(state.get("last_tx_id"), str)):
            raise LedgerError("state_sequence")
        for field in ("cash_usd", "realized_pnl_usd", "daily_loss_usd"):
            if not math.isfinite(_number(state.get(field), math.nan)):
                raise LedgerError("state_number")
        if _number(state.get("daily_loss_usd")) < 0:
            raise LedgerError("state_number")
        events = self._load_events()
        max_event_id = max((event["id"] for event in events), default=0)
        if state["sequence"] < max_event_id:
            raise LedgerError("state_sequence_behind_events")
        self._validate_consistency(state, events)
        return state, events

    @staticmethod
    def _quote_map(payload: Mapping[str, Any]) -> tuple[dict[tuple[str, str, str], dict[str, Any]], list[dict[str, Any]]]:
        raw_quotes = payload.get("quotes")
        if not isinstance(raw_quotes, list):
            raise ValueError("input_quotes_missing")
        quotes: dict[tuple[str, str, str], dict[str, Any]] = {}
        issues: list[dict[str, Any]] = []
        token_keys: set[tuple[str, str]] = set()
        for raw in raw_quotes:
            if not isinstance(raw, dict):
                raise InputError("invalid_quote")
            key = _identity(raw)
            if not all(key):
                raise InputError("missing_quote_identity")
            if key in quotes or key[:2] in token_keys:
                raise InputError("duplicate_quote", ":".join(key))
            if _impact_values(raw) is None:
                raise InputError("invalid_quote_impact", ":".join(key))
            quotes[key] = dict(raw)
            token_keys.add(key[:2])
        return quotes, issues

    def _read_input(self) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str], dict[str, Any]], list[dict[str, Any]]]:
        try:
            payload = json.loads(self.input_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("input_corrupt") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("signals"), list):
            raise ValueError("input_signals_missing")
        quotes, issues = self._quote_map(payload)
        signals = [row for row in payload["signals"] if isinstance(row, dict)]
        return signals, quotes, issues

    @staticmethod
    def _merge_signal_quote(signal: Mapping[str, Any], quote: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        if _identity(signal) != _identity(quote):
            if _identity(signal)[:2] == _identity(quote)[:2]:
                return None, "pool_identity_mismatch"
            return None, "quote_identity_mismatch"
        merged = dict(signal)
        # The signal owns discovery gates such as mcap; the quote owns only
        # executable market observations and must never replace signal data.
        for key, value in quote.items():
            if key not in {"mcap", "market_cap", "fdv"}:
                merged[key] = value
        merged["chain"], merged["contract_address"], merged["pool_address"] = _identity(signal)
        merged["quote_at"] = quote.get("quote_at") or quote.get("quote_observed_at")
        merged["price_usd"] = _quote_price(quote)
        merged["price"] = merged["price_usd"]
        return merged, None

    @staticmethod
    def _find_quote(identity: tuple[str, str, str], quotes: Mapping[tuple[str, str, str], dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
        exact = quotes.get(identity)
        if exact is not None:
            return exact, None
        same_token = [quote for key, quote in quotes.items() if key[:2] == identity[:2]]
        if len(same_token) == 1:
            return same_token[0], "pool_identity_mismatch"
        if len(same_token) > 1:
            return None, "ambiguous_quote"
        return None, "missing_quote"

    @staticmethod
    def _next_id(state: dict[str, Any]) -> int:
        state["sequence"] += 1
        return state["sequence"]

    def _event(self, state: dict[str, Any], event_type: str, key: str, now: datetime, **fields: Any) -> dict[str, Any]:
        return {
            "id": self._next_id(state),
            "type": event_type,
            "time": _iso(now),
            "idempotency_key": key,
            **fields,
        }

    def _buy_fill(self, state: dict[str, Any], key: str, row: Mapping[str, Any], quote: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        notional = self.config.order_notional_usd
        quote_price = _quote_price(quote)
        impact = _impact_values(quote)
        if impact is None:
            raise InputError("invalid_quote_impact", ":".join(_identity(quote)))
        price_impact, depth_impact = impact
        fill_price = quote_price * (1.0 + BASE_SLIPPAGE + price_impact + depth_impact)
        quantity = notional * (1.0 - TOKEN_TAX_RATE) / fill_price
        fee = notional * FEE_RATE
        total_debit = notional + fee + GAS_USD
        position_id = key
        state["cash_usd"] -= total_debit
        state["positions"][position_id] = {
            "position_id": position_id,
            "chain": _identity(row)[0],
            "contract_address": _identity(row)[1],
            "pool_address": _identity(row)[2],
            "signal_at": row.get("signal_at"),
            "entry_at": _iso(now),
            "entry_price_usd": quote_price,
            "entry_fill_price_usd": fill_price,
            "quote_price_usd": quote_price,
            "price_impact_rate": price_impact,
            "depth_impact_rate": depth_impact,
            "quantity": quantity,
            "entry_notional_usd": notional,
            "entry_fee_usd": fee,
            "entry_gas_usd": GAS_USD,
            "high_price_usd": quote_price,
            "tp1_hit": False,
            "remaining_fraction": 1.0,
        }
        state["orders"][key]["status"] = "filled"
        state["orders"][key]["filled_at"] = _iso(now)
        state["pending_intents"].pop(key, None)
        return self._event(
            state, "fill", key, now, side="buy", status="filled", notional_usd=notional,
            fill_price_usd=fill_price, quote_price_usd=quote_price, quantity=quantity,
            fee_usd=fee, tax_rate=TOKEN_TAX_RATE, slippage_rate=BASE_SLIPPAGE,
            price_impact_rate=price_impact, depth_impact_rate=depth_impact,
            impact_cost_usd=notional * (price_impact + depth_impact), gas_usd=GAS_USD,
            quote=_quote_evidence(quote),
        )

    def _shadow_fill(self, state: dict[str, Any], key: str, quote: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        state["orders"][key]["status"] = "shadow_filled"
        state["orders"][key]["filled_at"] = _iso(now)
        state["pending_intents"].pop(key, None)
        return self._event(
            state, "shadow_fill", key, now, side="buy", status="observed", notional_usd=self.config.order_notional_usd,
            quote_price_usd=_quote_price(quote), quote=_quote_evidence(quote), cash_changed=False,
        )

    def _sell_fill(self, state: dict[str, Any], key: str, position: dict[str, Any], quote: Mapping[str, Any], fraction: float, reason: str, now: datetime) -> dict[str, Any]:
        fraction = max(0.0, min(1.0, fraction))
        quote_price = _quote_price(quote)
        impact = _impact_values(quote)
        if impact is None:
            raise InputError("invalid_quote_impact", ":".join(_identity(quote)))
        price_impact, depth_impact = impact
        fill_price = quote_price * max(0.0, 1.0 - BASE_SLIPPAGE - price_impact - depth_impact)
        if fill_price <= 0:
            raise InputError("invalid_quote_impact", ":".join(_identity(quote)))
        quantity = _number(position.get("quantity")) * fraction
        gross = quantity * fill_price
        tax = gross * TOKEN_TAX_RATE
        fee = (gross - tax) * FEE_RATE
        proceeds = gross - tax - fee - GAS_USD
        allocated_cost = (_number(position.get("entry_notional_usd")) + _number(position.get("entry_fee_usd")) + _number(position.get("entry_gas_usd"))) * fraction
        pnl = proceeds - allocated_cost
        state["cash_usd"] += proceeds
        state["realized_pnl_usd"] += pnl
        if pnl < 0:
            state["daily_loss_usd"] += -pnl
        position["remaining_fraction"] = max(0.0, _number(position.get("remaining_fraction"), 1.0) - fraction)
        position["quantity"] = max(0.0, _number(position.get("quantity")) * (1.0 - fraction))
        if reason == "take_profit_1":
            position["tp1_hit"] = True
        if position["remaining_fraction"] <= 1e-9:
            state["positions"].pop(position["position_id"], None)
        state["orders"][key]["status"] = "filled"
        state["orders"][key]["filled_at"] = _iso(now)
        state["pending_intents"].pop(key, None)
        return self._event(
            state, "exit_fill", key, now, side="sell", reason=reason, sell_fraction=fraction,
            fill_price_usd=fill_price, quote_price_usd=quote_price, quantity=quantity,
            gross_proceeds_usd=gross, proceeds_usd=proceeds, fee_usd=fee, tax_usd=tax,
            gas_usd=GAS_USD, pnl_usd=pnl, price_impact_rate=price_impact,
            depth_impact_rate=depth_impact, impact_cost_usd=gross * (price_impact + depth_impact),
            quote=_quote_evidence(quote),
        )

    def _record_intent(self, state: dict[str, Any], key: str, side: str, row: Mapping[str, Any], quote: Mapping[str, Any], now: datetime, *, reason: str | None = None, sell_fraction: float | None = None) -> dict[str, Any]:
        intent = {
            "idempotency_key": key,
            "side": side,
            "chain": _identity(row)[0],
            "contract_address": _identity(row)[1],
            "pool_address": _identity(row)[2],
            "signal_at": row.get("signal_at") or row.get("quote_at"),
            "intent_at": _iso(now),
            "quote_at": quote.get("quote_at") or quote.get("quote_observed_at"),
            "observed_at": quote.get("quote_observed_at"),
            "quote": _quote_evidence(quote),
            "status": "pending",
        }
        if reason is not None:
            intent["reason"] = reason
        if sell_fraction is not None:
            intent["sell_fraction"] = sell_fraction
        if row.get("position_id"):
            intent["position_id"] = row["position_id"]
        state["orders"][key] = intent.copy()
        state["pending_intents"][key] = intent
        return self._event(
            state, "intent" if side == "buy" else "exit_intent", key, now,
            side=side, status="pending", quote=_quote_evidence(quote),
            reason=reason, sell_fraction=sell_fraction,
        )

    def _process_pending(self, state: dict[str, Any], quotes: dict[tuple[str, str, str], dict[str, Any]], now: datetime, events: list[dict[str, Any]], rejections: list[dict[str, Any]]) -> None:
        pending = list(state["pending_intents"].values())
        for intent in pending:
            if self.mode == "live":
                # A live submission is never retried by the worker. Reconciliation
                # must establish its chain outcome before another action is allowed.
                continue
            identity = (intent.get("chain", ""), intent.get("contract_address", ""), intent.get("pool_address", ""))
            quote = quotes.get(identity)
            if not quote or not _fresh_quote(quote, now, self.config):
                continue
            if _impact_values(quote) is None:
                rejections.append({"reason": "invalid_quote_impact", "identity": ":".join(identity)})
                continue
            previous_observed_at = _stamp(intent.get("observed_at"))
            current_observed_at = _observed_at(quote)
            if previous_observed_at and current_observed_at and current_observed_at <= previous_observed_at:
                continue
            key = intent["idempotency_key"]
            if intent.get("side") == "buy":
                if self.mode == "paper":
                    events.append(self._buy_fill(state, key, intent, quote, now))
                elif self.mode == "shadow":
                    events.append(self._shadow_fill(state, key, quote, now))
            elif intent.get("side") == "sell":
                position_id = intent.get("position_id")
                position = state["positions"].get(position_id)
                if not isinstance(position, dict):
                    raise LedgerError("pending_position_mismatch")
                if self.mode == "paper":
                    events.append(self._sell_fill(state, key, position, quote, _number(intent.get("sell_fraction")), str(intent.get("reason") or "exit"), now))
                elif self.mode == "shadow":
                    state["orders"][key]["status"] = "shadow_observed"
                    state["pending_intents"].pop(key, None)
                    events.append(self._event(state, "shadow_exit", key, now, side="sell", reason=intent.get("reason"), sell_fraction=intent.get("sell_fraction"), quote=_quote_evidence(quote), cash_changed=False))

    def _process_entries(self, state: dict[str, Any], signals: list[dict[str, Any]], quotes: dict[tuple[str, str, str], dict[str, Any]], now: datetime, events: list[dict[str, Any]], rejections: list[dict[str, Any]]) -> None:
        open_tokens = {token_key(position) for position in state["positions"].values()}
        pending_buys = [intent for intent in state["pending_intents"].values() if intent.get("side") == "buy"]
        for signal in signals:
            identity = _identity(signal)
            quote, quote_issue = self._find_quote(identity, quotes)
            if quote is None:
                rejections.append({"reason": quote_issue or "missing_quote", "identity": ":".join(identity)})
                continue
            if quote_issue:
                rejections.append({"reason": quote_issue, "identity": ":".join(identity)})
                continue
            merged, identity_issue = self._merge_signal_quote(signal, quote)
            if identity_issue:
                rejections.append({"reason": identity_issue, "identity": ":".join(identity)})
                continue
            assert merged is not None
            merged["open_positions"] = len(state["positions"]) + len(pending_buys)
            merged["current_exposure_usd"] = (
                sum(_number(position.get("entry_notional_usd")) * _number(position.get("remaining_fraction"), 1.0) for position in state["positions"].values())
                + len(pending_buys) * self.config.order_notional_usd
            )
            merged["daily_loss_usd"] = state["daily_loss_usd"]
            merged["existing_token_keys"] = list(open_tokens)
            decision = entry_decision(merged, now, self.config)
            if not decision.accepted:
                rejections.append({"reason": decision.reason, "token": decision.token, "identity": ":".join(identity)})
                continue
            execution_params = None
            if self.mode == "live":
                execution_params, parameter_issue = self._live_order_parameters(merged, "buy")
                if execution_params is None:
                    rejections.append({"reason": parameter_issue or "invalid_live_order_parameters", "identity": ":".join(identity)})
                    continue
            key = idempotency_key(signal, "buy")
            if key in state["orders"]:
                continue
            intent = self._record_intent(state, key, "buy", merged, quote, now)
            events.append(intent)
            pending_buys.append(state["pending_intents"][key])
            if self.mode == "live" and execution_params is not None:
                execution = self._invoke_live("buy", merged, execution_params)
                intent["execution"] = execution
                state["orders"][key]["execution"] = execution
                state["pending_intents"][key]["execution"] = execution
                if execution.get("ok") is True and execution.get("status") == "confirmed":
                    fill = self._buy_fill(state, key, merged, quote, now)
                    pending_buys[:] = [item for item in pending_buys if item.get("idempotency_key") != key]
                    position = state["positions"][key]
                    position["entry_amount_atomic"] = execution_params["amount_atomic"]
                    position["sell_amount_atomic"] = str(
                        merged.get("sell_amount_atomic") or merged.get("token_amount_atomic")
                        or execution_params["amount_atomic"]
                    )
                    position["slippage_percent"] = execution_params["slippage_percent"]
                    if "from_token_address" in execution_params:
                        position["from_token_address"] = execution_params["from_token_address"]
                    fill["execution"] = execution
                    events.append(fill)
                else:
                    state["orders"][key]["live_submitted"] = True
                    state["pending_intents"][key]["live_submitted"] = True
            quote_at = _stamp(quote.get("quote_at") or quote.get("quote_observed_at"))
            signal_at = _stamp(signal.get("signal_at"))
            if quote_at is None or signal_at is None or quote_at < signal_at:
                continue
            if self.mode == "shadow":
                events.append(self._shadow_fill(state, key, quote, now))

    def _process_exits(self, state: dict[str, Any], quotes: dict[tuple[str, str, str], dict[str, Any]], now: datetime, events: list[dict[str, Any]], rejections: list[dict[str, Any]]) -> None:
        for position in list(state["positions"].values()):
            identity = _identity(position)
            quote = quotes.get(identity)
            if not quote:
                continue
            if _impact_values(quote) is None:
                rejections.append({"reason": "invalid_quote_impact", "identity": ":".join(identity)})
                continue
            if _fresh_quote(quote, now, self.config):
                position["high_price_usd"] = max(_number(position.get("high_price_usd")), _quote_price(quote))
            else:
                rejections.append({"reason": "stale_exit_quote", "identity": ":".join(identity)})
                continue
            decision = exit_decision(position, quote, now, self.config)
            if not decision.exit:
                continue
            exit_at = quote.get("quote_at") or quote.get("quote_observed_at")
            key = idempotency_key({**position, "signal_at": exit_at, "quote_at": exit_at}, "sell")
            if key in state["orders"]:
                continue
            execution_params = None
            if self.mode == "live":
                execution_params, parameter_issue = self._live_order_parameters(position, "sell", decision.sell_fraction)
                if execution_params is None:
                    rejections.append({"reason": parameter_issue or "invalid_live_order_parameters", "identity": ":".join(identity)})
                    continue
            intent = self._record_intent(state, key, "sell", {**position, "signal_at": quote.get("quote_at"), "position_id": position["position_id"]}, quote, now, reason=decision.reason, sell_fraction=decision.sell_fraction)
            events.append(intent)
            state["orders"][key]["position_id"] = position["position_id"]
            state["pending_intents"][key]["position_id"] = position["position_id"]
            if self.mode == "live":
                assert execution_params is not None
                execution = self._invoke_live("sell", position, execution_params)
                intent["execution"] = execution
                state["orders"][key]["execution"] = execution
                state["pending_intents"][key]["execution"] = execution
                if execution.get("ok") is True and execution.get("status") == "confirmed":
                    fill = self._sell_fill(state, key, position, quote, decision.sell_fraction, decision.reason, now)
                    fill["execution"] = execution
                    events.append(fill)
                else:
                    state["orders"][key]["live_submitted"] = True
                    state["pending_intents"][key]["live_submitted"] = True
            if self.mode == "shadow":
                state["orders"][key]["status"] = "shadow_observed"
                state["pending_intents"].pop(key, None)
                events.append(self._event(state, "shadow_exit", key, now, side="sell", reason=decision.reason, sell_fraction=decision.sell_fraction, quote=_quote_evidence(quote), cash_changed=False))

    def _summary(self, state: Mapping[str, Any]) -> dict[str, Any]:
        exposure = sum(_number(position.get("entry_notional_usd")) * _number(position.get("remaining_fraction"), 1.0) for position in state["positions"].values())
        return {
            "open_count": len(state["positions"]),
            "open_exposure_usd": round(exposure, 8),
            "cash_usd": round(_number(state.get("cash_usd")), 8),
            "realized_pnl_usd": round(_number(state.get("realized_pnl_usd")), 8),
            "daily_loss_usd": round(_number(state.get("daily_loss_usd")), 8),
            "pending_count": len(state["pending_intents"]),
        }

    def _append_events(self, events: list[dict[str, Any]]) -> None:
        existing = self.events_path.read_text(encoding="utf-8") if self.events_path.exists() else ""
        temporary = self.events_path.with_name(f".{self.events_path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(existing)
            for event in events:
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.events_path)

    def _commit(self, state: dict[str, Any], events: list[dict[str, Any]], now: datetime) -> None:
        if not events:
            if not self.events_path.exists():
                self.events_path.touch()
            return
        tx_id = hashlib.sha256(f"{os.getpid()}:{time.time_ns()}:{state['sequence']}".encode("ascii")).hexdigest()
        size = len(events)
        committed_events = []
        for index, event in enumerate(events, start=1):
            committed_events.append({
                **event,
                "tx_id": tx_id,
                "tx_seq": index,
                "tx_size": size,
                "tx_complete": index == size,
            })
        # Events are replaced first. If the following state replace is
        # interrupted, the tx marker makes the next read fail closed.
        self._append_events(committed_events)
        state["last_tx_id"] = tx_id
        events[:] = committed_events

    def _write_artifacts(self, state: dict[str, Any], result: dict[str, Any], now: datetime) -> None:
        state["updated_at"] = _iso(now)
        _atomic_write(self.state_path, state)
        report = {"updated_at": _iso(now), "mode": self.mode, "status": result["status"], "summary": result["summary"], "events": result["events"], "rejections": result["rejections"]}
        health = {"updated_at": _iso(now), "status": result["status"], "ledger": "ok", "mode": self.mode, "summary": result["summary"]}
        if "preflight" in result:
            report["preflight"] = result["preflight"]
            health["preflight"] = result["preflight"]
        _atomic_write(self.report_path, report)
        _atomic_write(self.health_path, health)

    def run_once(self, now: datetime) -> dict[str, Any]:
        current = now if now.tzinfo else now.replace(tzinfo=CN_TZ)
        with _WriterLock(self.out_dir) as acquired:
            if not acquired:
                return {"status": "locked", "events": [], "rejections": [], "summary": {"open_count": 0}}
            try:
                state, _existing_events = self._load_state(current)
            except LedgerError as exc:
                result = {"status": "ledger_error", "error": str(exc), "events": [], "rejections": [], "summary": {"open_count": 0}}
                return result
            if state.get("daily_loss_date") != _day(current):
                state["daily_loss_date"] = _day(current)
                state["daily_loss_usd"] = 0.0
            if self.mode == "live":
                preflight = self._live_preflight()
                if not preflight["ready"]:
                    result = {
                        "status": "live_preflight_failed",
                        "events": [],
                        "rejections": [{"reason": reason} for reason in preflight["reasons"]],
                        "summary": self._summary(state),
                        "preflight": preflight,
                    }
                    if preflight["missing"] and not result["rejections"]:
                        result["rejections"] = [{"reason": "missing_live_dependency", "dependency": item} for item in preflight["missing"]]
                    self.events_path.touch(exist_ok=True)
                    self._write_artifacts(state, result, current)
                    return result
            try:
                signals, quotes, input_issues = self._read_input()
            except InputError as exc:
                result = {"status": "input_error", "error": str(exc), "events": [], "rejections": [{"reason": exc.reason, **({"identity": exc.identity} if exc.identity else {})}], "summary": self._summary(state)}
                self.events_path.touch(exist_ok=True)
                self._write_artifacts(state, result, current)
                return result
            except ValueError as exc:
                result = {"status": "input_error", "error": str(exc), "events": [], "rejections": [], "summary": self._summary(state)}
                self.events_path.touch(exist_ok=True)
                self._write_artifacts(state, result, current)
                return result
            events: list[dict[str, Any]] = []
            rejections = list(input_issues)
            try:
                self._process_pending(state, quotes, current, events, rejections)
                self._process_exits(state, quotes, current, events, rejections)
                if state["daily_loss_usd"] < self.config.daily_loss_limit_usd:
                    self._process_entries(state, signals, quotes, current, events, rejections)
                else:
                    rejections.append({"reason": "daily_loss_circuit_breaker"})
                self._commit(state, events, current)
            except (LedgerError, InputError) as exc:
                return {"status": "ledger_error", "error": str(exc), "events": [], "rejections": [], "summary": {"open_count": 0}}
            status = "ok" if events or not rejections else "no_action"
            result = {"status": status, "events": events, "rejections": rejections, "summary": self._summary(state)}
            self._write_artifacts(state, result, current)
            return result


class _NoopClient:
    """CLI paper client; it has no signing or broadcasting surface."""


class _OkxHttpTransport:
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str],
        body: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Mapping[str, Any]:
        query = urllib.parse.urlencode(params)
        url = f"https://web3.okx.com{path}?{query}" if query else f"https://web3.okx.com{path}"
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=encoded, headers=headers or {}, method=method)
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, Mapping) else {}


def _present_env_check(values: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = values.get(name)
    return {"ok": isinstance(value, str) and bool(value.strip())}


def _build_live_client(env: Mapping[str, Any] | None = None) -> tuple[Any | None, dict[str, Any]]:
    """Build the live adapter only after an offline, secret-free preflight."""
    values = dict(os.environ if env is None else env)
    try:
        import alpha_bsc_live_preflight as preflight
        from alpha_bsc_live_adapter import LiveExecutionAdapter, LocalEvmSigner, JsonRpcBroadcaster, OkxBscSwapAbi
        from alpha_okx_swap import OkxSwapClient
    except Exception:
        return None, _safe_preflight({
            "ready": False,
            "checks": {"worker_dependencies": {"ok": False, "reason": "missing"}},
            "missing": ["worker_dependencies"],
            "reasons": [],
        })

    # Reuse the existing local market credential loader. It prefers process
    # environment values and falls back to the git-ignored local OKX file.
    try:
        import alpha_okx_market
        api_key, secret_key, passphrase = alpha_okx_market.credentials()
    except Exception:
        api_key, secret_key, passphrase = "", "", ""
    credentials = {
        "api_key": values.get("OKX_API_KEY") or api_key,
        "secret_key": values.get("OKX_SECRET_KEY") or secret_key,
        "passphrase": values.get("OKX_PASSPHRASE") or passphrase,
    }
    okx_client = OkxSwapClient(_OkxHttpTransport(), credentials)
    result = preflight.run_preflight(env=values, okx_client=okx_client)
    checks = dict(result.get("checks") or {})
    missing = list(result.get("missing") or [])
    for name, credential in zip(
        ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"),
        (credentials["api_key"], credentials["secret_key"], credentials["passphrase"]),
    ):
        check = {"ok": isinstance(credential, str) and bool(credential.strip())}
        checks[name] = check
        if not check["ok"]:
            missing.append(name)
    result = {
        "ready": bool(result.get("ready")) and all(check.get("ok") is True for check in checks.values()),
        "checks": checks,
        "missing": list(dict.fromkeys(missing)),
        "reasons": list(result.get("reasons") or []),
    }
    safe_result = _safe_preflight(result)
    if not safe_result["ready"]:
        return None, safe_result
    try:
        routers = [item.strip() for item in str(values["BSC_ALLOWED_ROUTER_ADDRESSES"]).split(",") if item.strip()]
        signer = LocalEvmSigner(values["BSC_WALLET_ADDRESS"], env=values)
        broadcaster = JsonRpcBroadcaster(env=values)
        adapter = LiveExecutionAdapter(
            signer=signer,
            broadcaster=broadcaster,
            okx_client=okx_client,
            wallet_address=values["BSC_WALLET_ADDRESS"],
            env=values,
            allowed_router_addresses=routers,
            armed=True,
            swap_abi=OkxBscSwapAbi(values["BSC_OKX_SWAP_SELECTOR"]),
        )
    except Exception:
        failed = {
            **safe_result,
            "ready": False,
            "checks": {**safe_result["checks"], "worker_adapter": {"ok": False, "reason": "construction_failed"}},
            "reasons": [*safe_result["reasons"], "worker_adapter:construction_failed"],
        }
        return None, _safe_preflight(failed)
    return adapter, safe_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("paper", "shadow", "live"), default="paper")
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        client: Any = _NoopClient()
        live_preflight = None
        if args.mode == "live":
            client, live_preflight = _build_live_client()
        result = ExecutionWorker(
            args.input_path,
            args.out_dir,
            client,
            mode=args.mode,
            live_preflight=live_preflight,
        ).run_once(datetime.now(CN_TZ))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.once:
            return 0 if result["status"] in {"ok", "no_action"} else 1
        time.sleep(max(1.0, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
