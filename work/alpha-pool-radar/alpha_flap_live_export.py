#!/usr/bin/env python3
"""Read-only Flap.sh launchpad exporter for the Meme first-layer inbox."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import websocket  # type: ignore
except Exception:  # noqa: BLE001
    websocket = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
FLAP_LAUNCHPAD_CONTRACT = "0x1de460f363af910f51726def188f9004276bf4bc"
FLAP_PORTAL_CONTRACT = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
DEFAULT_GRAPHQL_WSS = "wss://streaming.bitquery.io/graphql"
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "flap-launches.json"
DEFAULT_STATUS = ROOT / "outputs" / "flap-live-export-status.json"
DEFAULT_MAX_ROWS = 120
DEFAULT_ONCE_DURATION_SECONDS = 25
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_RECONNECT_SECONDS = 5

FLAP_TOKEN_CREATED_SUBSCRIPTION = """
subscription {
  EVM(network: bsc) {
    Events(
      orderBy: { descending: Block_Time }
      where: {
        LogHeader: { Address: { is: "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0" } }
        Log: { Signature: { Name: { in: ["TokenCreated", "TokenCurveSetV2", "TokenQuoteSet", "TokenMigrated"] } } }
      }
    ) {
      Log { Signature { Name Signature } }
      Arguments {
        Name
        Type
        Value {
          ... on EVM_ABI_Address_Value_Arg { address }
          ... on EVM_ABI_String_Value_Arg { string }
          ... on EVM_ABI_BigInt_Value_Arg { bigInteger }
          ... on EVM_ABI_Integer_Value_Arg { integer }
          ... on EVM_ABI_Bytes_Value_Arg { hex }
          ... on EVM_ABI_Boolean_Value_Arg { bool }
        }
      }
      Transaction { Hash To From }
      Block { Time Number }
    }
  }
}
""".strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def env_token() -> str:
    return os.environ.get("BITQUERY_API_KEY", "").strip() or os.environ.get("BITQUERY_TOKEN", "").strip()


def graphql_wss_url(token: str) -> str:
    base = os.environ.get("BITQUERY_GRAPHQL_WSS_URL", DEFAULT_GRAPHQL_WSS).strip()
    if not token:
        return base
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}token={urllib.parse.quote(token)}"


def arg_value(arg: dict[str, Any]) -> Any:
    value = arg.get("Value")
    if not isinstance(value, dict):
        return None
    for key in ("address", "string", "bigInteger", "integer", "hex", "bool"):
        if value.get(key) not in (None, ""):
            return value.get(key)
    return None


def event_arguments(event: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for arg in event.get("Arguments") or []:
        if not isinstance(arg, dict):
            continue
        name = str(arg.get("Name") or "").strip()
        if name:
            args[name] = arg_value(arg)
    return args


def first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lower = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
        value = lower.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def event_name(event: dict[str, Any]) -> str:
    log = event.get("Log") if isinstance(event.get("Log"), dict) else {}
    signature = log.get("Signature") if isinstance(log.get("Signature"), dict) else {}
    return str(signature.get("Name") or "")


def looks_like_flap_token(address: str) -> bool:
    lower = address.lower()
    return lower.startswith("0x") and len(lower) >= 42 and (lower.endswith("7777") or lower.endswith("8888"))


def to_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def progress_pct(args: dict[str, Any]) -> float:
    value = first_present(
        args,
        (
            "bondingCurveProgress",
            "bonding_curve_progress",
            "curveProgress",
            "progress",
            "progressBps",
            "thresholdProgress",
            "fillPct",
            "fill",
            "completion",
            "completeRate",
            "rate",
        ),
    )
    progress = to_float(value)
    if progress <= 0:
        return 0.0
    if progress <= 1:
        progress *= 100
    if 100 < progress <= 10_000:
        progress /= 100
    if progress > 10_000:
        progress /= 100
    return round(max(0.0, min(100.0, progress)), 4)


def quote_bnb(args: dict[str, Any]) -> float:
    value = first_present(args, ("bnbAmount", "amountBNB", "quoteAmount", "quote", "payAmount", "cost", "value", "amount"))
    amount = to_float(value)
    if amount > 10_000_000_000:
        amount /= 1e18
    return round(max(0.0, amount), 8)


def lifecycle_fields(args: dict[str, Any], event_type: str) -> dict[str, Any]:
    progress = progress_pct(args)
    event_lower = event_type.lower()
    quote_count = 1 if "quote" in event_lower else 0
    migrated = "migrat" in event_lower
    stage = "launchpad_new"
    label = "发射台首发"
    if migrated:
        stage = "migrated"
        label = "已迁移待行情"
    elif progress >= 95:
        stage = "near_graduation"
        label = "曲线快毕业"
    elif progress >= 60 or quote_count:
        stage = "curve_accelerating"
        label = "曲线加速"
    return {
        "launchpad_lifecycle_stage": stage,
        "launchpad_stage_label": label,
        "bonding_curve_progress_pct": progress,
        "curve_progress_pct": progress,
        "purchase_count": quote_count,
        "buy_count": quote_count,
        "purchase_bnb": quote_bnb(args),
        "bnb_per_hour": 0.0,
        "migration_confirmed": migrated,
    }


def normalize_token_created_event(event: dict[str, Any]) -> dict[str, Any] | None:
    args = event_arguments(event)
    token = str(
        first_present(args, ("token", "currency", "tokenAddress", "contract", "tokenContract", "flapToken", "baseToken"))
        or ""
    ).strip()
    if not looks_like_flap_token(token):
        return None
    symbol = str(first_present(args, ("symbol", "ticker")) or token[-8:].upper()).strip()
    name = str(first_present(args, ("name", "tokenName")) or f"Flap {symbol}").strip()
    tx = event.get("Transaction") if isinstance(event.get("Transaction"), dict) else {}
    block = event.get("Block") if isinstance(event.get("Block"), dict) else {}
    suffix = token[-4:].lower()
    token_type = "tax" if suffix == "7777" else "standard" if suffix == "8888" else "unknown"
    return {
        "chain": "bsc",
        "chainId": "bsc",
        "address": token.lower(),
        "tokenAddress": token.lower(),
        "contractAddress": token.lower(),
        "symbol": symbol,
        "name": name,
        "marketCap": 0,
        "liquidityUsd": 0,
        "volume24hUsd": 0,
        "source_family": "flap_launchpad",
        "source_origin": "bitquery_flap_token_created",
        "launchpad_platform": "Flap.sh",
        "platform": "Flap.sh",
        "bsc_event_type": event_name(event) or "TokenCreated",
        "flap_launchpad_contract": FLAP_LAUNCHPAD_CONTRACT,
        "flap_portal_contract": FLAP_PORTAL_CONTRACT,
        "flap_token_type": token_type,
        "creator": str(first_present(args, ("creator", "user", "owner", "deployer")) or tx.get("From") or "").lower(),
        "transaction_hash": tx.get("Hash") or "",
        "market_data_pending": True,
        "first_seen_at": block.get("Time") or utc_now_iso(),
        "last_activity_at": block.get("Time") or utc_now_iso(),
        "updated_at": utc_now_iso(),
        **lifecycle_fields(args, event_name(event) or "TokenCreated"),
    }


class FlapSnapshot:
    def __init__(self, max_rows: int) -> None:
        self.max_rows = max(1, max_rows)
        self.rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.total_events = 0

    def add(self, row: dict[str, Any]) -> None:
        token = str(row.get("tokenAddress") or "").lower()
        key = f"bsc:{token}"
        if token:
            self.total_events += 1
            if key in self.rows:
                previous = self.rows.pop(key)
                row["purchase_count"] = int(previous.get("purchase_count") or 0) + int(row.get("purchase_count") or 0)
                row["buy_count"] = row["purchase_count"]
                row["purchase_bnb"] = round(to_float(previous.get("purchase_bnb")) + to_float(row.get("purchase_bnb")), 8)
                if to_float(row.get("bonding_curve_progress_pct")) <= 0:
                    row["bonding_curve_progress_pct"] = to_float(previous.get("bonding_curve_progress_pct"))
                    row["curve_progress_pct"] = row["bonding_curve_progress_pct"]
                if str(previous.get("first_seen_at") or ""):
                    row["first_seen_at"] = previous.get("first_seen_at")
                if bool(previous.get("migration_confirmed")):
                    row["migration_confirmed"] = True
                row = {**previous, **row}
                if bool(row.get("migration_confirmed")):
                    row["launchpad_lifecycle_stage"] = "migrated"
                    row["launchpad_stage_label"] = "已迁移待行情"
                elif to_float(row.get("bonding_curve_progress_pct")) >= 95:
                    row["launchpad_lifecycle_stage"] = "near_graduation"
                    row["launchpad_stage_label"] = "曲线快毕业"
                elif to_float(row.get("bonding_curve_progress_pct")) >= 60 or int(row.get("purchase_count") or 0) >= 2:
                    row["launchpad_lifecycle_stage"] = "curve_accelerating"
                    row["launchpad_stage_label"] = "曲线加速"
            self.rows[key] = row
            while len(self.rows) > self.max_rows:
                self.rows.popitem(last=False)

    def payload(self, *, ok: bool = True, reason: str = "") -> dict[str, Any]:
        rows = list(reversed(list(self.rows.values())))
        return {
            "ok": ok,
            "source": "flap_launchpad",
            "source_family": "flap_launchpad",
            "updated_at": utc_now_iso(),
            "reason": reason,
            "count": len(rows),
            "total_events": self.total_events,
            "data": rows,
        }


def stream_once(
    *,
    token: str,
    out_path: Path,
    status_path: Path,
    max_rows: int,
    duration_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    snapshot = FlapSnapshot(max_rows)
    if not token:
        status = {
            "ok": False,
            "reason": "missing_bitquery_token",
            "message": "Set BITQUERY_API_KEY or BITQUERY_TOKEN to enable Flap.sh launchpad realtime.",
            "finished_at": utc_now_iso(),
            "out": str(out_path),
        }
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="missing_bitquery_token"))
        return status
    if websocket is None:
        status = {"ok": False, "reason": "missing_websocket_client", "finished_at": utc_now_iso(), "out": str(out_path)}
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="missing_websocket_client"))
        return status

    started = time.monotonic()
    try:
        ws = websocket.create_connection(graphql_wss_url(token), timeout=timeout_seconds, subprotocols=["graphql-transport-ws"])
        try:
            ws.settimeout(min(2, max(1, timeout_seconds)))
            ws.send(json.dumps({"type": "connection_init"}))
            ws.send(json.dumps({"id": "flap-token-created", "type": "subscribe", "payload": {"query": FLAP_TOKEN_CREATED_SUBSCRIPTION}}))
            while time.monotonic() - started < max(1, duration_seconds):
                try:
                    message = ws.recv()
                except Exception as exc:  # noqa: BLE001
                    if websocket is not None and isinstance(exc, websocket.WebSocketTimeoutException):
                        continue
                    raise
                data = json.loads(message)
                if data.get("type") == "connection_error" or data.get("type") == "error":
                    raise RuntimeError(json.dumps(data, ensure_ascii=False))
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                evm = ((payload.get("data") or {}).get("EVM") or {}) if isinstance(payload.get("data"), dict) else {}
                for event in evm.get("Events") or []:
                    if not isinstance(event, dict):
                        continue
                    row = normalize_token_created_event(event)
                    if row:
                        snapshot.add(row)
                        write_json(out_path, snapshot.payload())
        finally:
            ws.close()
    except Exception as exc:  # noqa: BLE001
        status = {
            "ok": False,
            "reason": "stream_error",
            "error": str(exc),
            "finished_at": utc_now_iso(),
            "out": str(out_path),
            "rows": len(snapshot.rows),
            "total_events": snapshot.total_events,
        }
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="stream_error"))
        return status

    payload = snapshot.payload()
    write_json(out_path, payload)
    status = {"ok": True, "reason": "", "finished_at": utc_now_iso(), "out": str(out_path), "rows": payload["count"], "total_events": payload["total_events"]}
    write_json(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Flap.sh launchpad realtime exporter.")
    parser.add_argument("--token", default=env_token())
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--max-rows", type=int, default=int(os.environ.get("FLAP_MAX_ROWS", DEFAULT_MAX_ROWS)))
    parser.add_argument("--once-duration-seconds", type=int, default=int(os.environ.get("FLAP_ONCE_DURATION_SECONDS", DEFAULT_ONCE_DURATION_SECONDS)))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("FLAP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)))
    parser.add_argument("--reconnect-seconds", type=int, default=int(os.environ.get("FLAP_RECONNECT_SECONDS", DEFAULT_RECONNECT_SECONDS)))
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        status = stream_once(
            token=args.token,
            out_path=args.out,
            status_path=args.status,
            max_rows=args.max_rows,
            duration_seconds=args.once_duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        if args.once:
            return 0 if status.get("ok") or status.get("reason") == "missing_bitquery_token" else 1
        time.sleep(max(1, args.reconnect_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
