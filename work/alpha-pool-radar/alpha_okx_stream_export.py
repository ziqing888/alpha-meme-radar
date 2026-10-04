#!/usr/bin/env python3
"""Read-only OKX OnchainOS Signal/Memepump websocket exporter."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import websocket  # type: ignore
except Exception:  # noqa: BLE001
    websocket = None

import alpha_okx_market

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
WS_URL = "wss://wsdex.okx.com/ws/v6/dex"
DEFAULT_OUT_DIR = ROOT / "outputs" / "meme-source-inbox"
DEFAULT_SIGNAL_OUT = DEFAULT_OUT_DIR / "okx-signal-stream.json"
DEFAULT_MEMEPUMP_OUT = DEFAULT_OUT_DIR / "okx-memepump-stream.json"
DEFAULT_STATUS = ROOT / "outputs" / "okx-stream-export-status.json"
DEFAULT_MAX_ROWS = 240
DEFAULT_ONCE_DURATION_SECONDS = 25
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_RECONNECT_SECONDS = 5
CHANNELS = {
    "signal": {
        "channel": "dex-market-new-signal-openapi",
        "source": "okx_signal",
        "kind": "SIGNAL",
        "out": DEFAULT_SIGNAL_OUT,
    },
    "memepump": {
        "channel": "dex-market-memepump-new-token-openapi",
        "source": "okx_trenches",
        "kind": "TRENCHES",
        "out": DEFAULT_MEMEPUMP_OUT,
    },
}
CHAIN_BY_INDEX = {value: key for key, value in alpha_okx_market.CHAINS.items()}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def csv_items(value: str) -> list[str]:
    return [item.strip().lower() for item in str(value or "").split(",") if item.strip()]


def chain_indexes(chains: str) -> list[str]:
    indexes: list[str] = []
    for chain in csv_items(chains):
        index = alpha_okx_market.CHAINS.get(chain, chain)
        if index in alpha_okx_market.CHAINS.values() and index not in indexes:
            indexes.append(index)
    return indexes


def channel_keys(channels: str) -> list[str]:
    keys: list[str] = []
    for channel in csv_items(channels):
        if channel in CHANNELS and channel not in keys:
            keys.append(channel)
    return keys


def sign_login(timestamp: str, secret: str) -> str:
    payload = timestamp + "GET" + "/users/self/verify"
    return base64.b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()).decode()


def login_message() -> str:
    key, secret, passphrase = alpha_okx_market.credentials()
    timestamp = str(int(time.time()))
    return json.dumps(
        {
            "op": "login",
            "args": [
                {
                    "apiKey": key,
                    "passphrase": passphrase,
                    "timestamp": timestamp,
                    "sign": sign_login(timestamp, secret),
                }
            ],
        },
        separators=(",", ":"),
    )


def subscribe_message(chains: list[str], channels: list[str]) -> str:
    args = [
        {"channel": str(CHANNELS[channel_key]["channel"]), "chainIndex": chain_index}
        for chain_index in chains
        for channel_key in channels
    ]
    return json.dumps({"op": "subscribe", "args": args}, separators=(",", ":"))


def response_error(message: dict[str, Any]) -> str:
    code = str(message.get("code") or "").strip()
    text = str(message.get("msg") or "").strip()
    if code == "60029":
        return "ws_channel_not_whitelisted"
    if code == "60011":
        return "ws_login_required"
    if code == "60020":
        return "ws_subscription_limit"
    return ": ".join(part for part in (code, text) if part)


def wait_for_login(ws: Any, timeout_seconds: int) -> tuple[bool, str]:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            raw = ws.recv()
        except Exception as exc:  # noqa: BLE001
            if websocket is not None and isinstance(exc, websocket.WebSocketTimeoutException):
                continue
            raise
        if raw == "pong":
            continue
        message = json.loads(raw)
        if not isinstance(message, dict):
            continue
        if message.get("event") == "login" and str(message.get("code")) == "0":
            return True, ""
        if message.get("event") == "error":
            return False, response_error(message) or "login_error"
    return False, "login_timeout"


def chain_from_payload(row: dict[str, Any], arg: dict[str, Any]) -> str:
    index = str(row.get("chainIndex") or arg.get("chainIndex") or "").strip()
    return CHAIN_BY_INDEX.get(index, index)


def rows_from_message(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    arg = message.get("arg") if isinstance(message.get("arg"), dict) else {}
    channel = str(arg.get("channel") or message.get("channel") or "")
    if channel == CHANNELS["signal"]["channel"]:
        signal = message.get("signal")
        rows = [signal] if isinstance(signal, dict) else []
        return [("signal", {**row, "chainIndex": row.get("chainIndex") or arg.get("chainIndex")}) for row in rows]
    if channel == CHANNELS["memepump"]["channel"]:
        data = message.get("data") if isinstance(message.get("data"), list) else []
        return [
            ("memepump", {**row, "chainIndex": row.get("chainIndex") or arg.get("chainIndex")})
            for row in data
            if isinstance(row, dict)
        ]
    return []


def token_key(row: dict[str, Any]) -> str:
    chain = str(row.get("chain") or row.get("chainId") or "").strip().lower()
    address = str(row.get("tokenAddress") or row.get("address") or row.get("contractAddress") or "").strip().lower()
    return f"{chain}:{address}" if chain and address else ""


class OkxStreamSnapshot:
    def __init__(self, max_rows: int, outputs: dict[str, Path]) -> None:
        self.max_rows = max(1, max_rows)
        self.outputs = outputs
        self.rows: dict[str, OrderedDict[str, dict[str, Any]]] = {
            "okx_signal": OrderedDict(),
            "okx_trenches": OrderedDict(),
        }
        self.messages = 0
        self.events = 0
        self.rows_seen = 0
        self.load_existing()

    def load_existing(self) -> None:
        for source, path in self.outputs.items():
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                continue
            for row in rows[-self.max_rows :]:
                if isinstance(row, dict):
                    key = token_key(row)
                    if key:
                        self.rows.setdefault(source, OrderedDict())[key] = row

    def add(self, source: str, row: dict[str, Any]) -> None:
        key = token_key(row)
        if not key:
            return
        bucket = self.rows.setdefault(source, OrderedDict())
        if key in bucket:
            previous = bucket.pop(key)
            row = {**previous, **row}
        bucket[key] = row
        while len(bucket) > self.max_rows:
            bucket.popitem(last=False)

    def source_payload(self, source: str, *, ok: bool = True, reason: str = "") -> dict[str, Any]:
        rows = list(reversed(list(self.rows.get(source, OrderedDict()).values())))
        return {
            "ok": ok,
            "source": source,
            "source_family": source,
            "updated_at": utc_now_iso(),
            "reason": reason,
            "count": len(rows),
            "data": rows,
        }

    def write_outputs(self, *, ok: bool = True, reason: str = "") -> None:
        for source, path in self.outputs.items():
            write_json(path, self.source_payload(source, ok=ok, reason=reason))

    def counts(self) -> dict[str, int]:
        return {source: len(rows) for source, rows in self.rows.items()}


def normalize_stream_row(channel_key: str, row: dict[str, Any]) -> dict[str, Any] | None:
    spec = CHANNELS[channel_key]
    chain = chain_from_payload(row, {})
    if not chain:
        return None
    normalized = alpha_okx_market.normalize(row, chain, str(spec["kind"]))
    address = str(normalized.get("address") or "").strip()
    if not address:
        return None
    now = utc_now_iso()
    return {
        **normalized,
        "chainId": chain,
        "tokenAddress": address,
        "contractAddress": address,
        "source_family": spec["source"],
        "source_origin": f"okx_ws:{spec['channel']}",
        "first_seen_at": normalized.get("first_seen_at") or now,
        "updated_at": now,
    }


def stream_once(
    *,
    wss_url: str,
    chains: list[str],
    channels: list[str],
    outputs: dict[str, Path],
    status_path: Path,
    max_rows: int,
    duration_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    snapshot = OkxStreamSnapshot(max_rows, outputs)
    if not all(alpha_okx_market.credentials()):
        status = {
            "ok": False,
            "reason": "missing_market_api_credentials",
            "finished_at": utc_now_iso(),
            "outputs": {source: str(path) for source, path in outputs.items()},
            "counts": snapshot.counts(),
        }
        write_json(status_path, status)
        snapshot.write_outputs(ok=False, reason="missing_market_api_credentials")
        return status
    if not wss_url:
        status = {"ok": False, "reason": "missing_okx_ws_url", "finished_at": utc_now_iso(), "counts": snapshot.counts()}
        write_json(status_path, status)
        snapshot.write_outputs(ok=False, reason="missing_okx_ws_url")
        return status
    if websocket is None:
        status = {"ok": False, "reason": "missing_websocket_client", "finished_at": utc_now_iso(), "counts": snapshot.counts()}
        write_json(status_path, status)
        snapshot.write_outputs(ok=False, reason="missing_websocket_client")
        return status
    if not chains or not channels:
        status = {"ok": False, "reason": "empty_subscription", "finished_at": utc_now_iso(), "counts": snapshot.counts()}
        write_json(status_path, status)
        snapshot.write_outputs(ok=False, reason="empty_subscription")
        return status

    started = time.monotonic()
    errors: list[str] = []
    subscriptions = 0
    pings = 0
    try:
        ws = websocket.create_connection(wss_url, timeout=timeout_seconds)
        try:
            if hasattr(ws, "settimeout"):
                ws.settimeout(min(2, max(1, timeout_seconds)))
            ws.send(login_message())
            logged_in, login_error = wait_for_login(ws, timeout_seconds)
            if not logged_in:
                raise RuntimeError(login_error)
            ws.send(subscribe_message(chains, channels))
            while time.monotonic() - started < max(1, duration_seconds):
                try:
                    raw = ws.recv()
                except Exception as exc:  # noqa: BLE001
                    if websocket is not None and isinstance(exc, websocket.WebSocketTimeoutException):
                        ws.send("ping")
                        pings += 1
                        continue
                    raise
                if raw == "pong":
                    continue
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                snapshot.messages += 1
                if message.get("event") == "subscribe":
                    subscriptions += 1
                    continue
                if message.get("event") == "error":
                    errors.append(response_error(message) or "stream_error")
                    continue
                pushed_rows = rows_from_message(message)
                if pushed_rows:
                    snapshot.events += 1
                for channel_key, raw_row in pushed_rows:
                    snapshot.rows_seen += 1
                    normalized = normalize_stream_row(channel_key, raw_row)
                    if normalized:
                        snapshot.add(str(CHANNELS[channel_key]["source"]), normalized)
                if pushed_rows:
                    snapshot.write_outputs()
        finally:
            ws.close()
    except Exception as exc:  # noqa: BLE001
        status = {
            "ok": False,
            "reason": "stream_error",
            "error": str(exc),
            "finished_at": utc_now_iso(),
            "counts": snapshot.counts(),
            "messages": snapshot.messages,
            "events": snapshot.events,
            "rows_seen": snapshot.rows_seen,
            "pings": pings,
        }
        write_json(status_path, status)
        snapshot.write_outputs(ok=False, reason="stream_error")
        return status

    errors = list(dict.fromkeys(errors))
    ok = not errors
    reason = "; ".join(errors)
    snapshot.write_outputs(ok=ok, reason=reason)
    status = {
        "ok": ok,
        "reason": reason,
        "finished_at": utc_now_iso(),
        "counts": snapshot.counts(),
        "messages": snapshot.messages,
        "events": snapshot.events,
        "rows_seen": snapshot.rows_seen,
        "subscriptions": subscriptions,
        "subscribed_chains": chains,
        "subscribed_channels": [str(CHANNELS[channel]["channel"]) for channel in channels],
        "pings": pings,
    }
    write_json(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only OKX Signal/Memepump websocket exporter.")
    parser.add_argument("--wss-url", default=os.environ.get("OKX_MARKET_WS_URL", WS_URL))
    parser.add_argument("--chains", default=os.environ.get("ALPHA_MEME_CHAINS", "bsc,robinhood"))
    parser.add_argument("--channels", default=os.environ.get("OKX_STREAM_CHANNELS", "signal,memepump"))
    parser.add_argument("--signal-out", type=Path, default=DEFAULT_SIGNAL_OUT)
    parser.add_argument("--memepump-out", type=Path, default=DEFAULT_MEMEPUMP_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--max-rows", type=int, default=int(os.environ.get("OKX_STREAM_MAX_ROWS", DEFAULT_MAX_ROWS)))
    parser.add_argument(
        "--once-duration-seconds",
        type=int,
        default=int(os.environ.get("OKX_STREAM_ONCE_DURATION_SECONDS", DEFAULT_ONCE_DURATION_SECONDS)),
    )
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("OKX_STREAM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)))
    parser.add_argument("--reconnect-seconds", type=int, default=int(os.environ.get("OKX_STREAM_RECONNECT_SECONDS", DEFAULT_RECONNECT_SECONDS)))
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    channels = channel_keys(args.channels)
    chains = chain_indexes(args.chains)
    outputs = {"okx_signal": args.signal_out, "okx_trenches": args.memepump_out}
    while True:
        status = stream_once(
            wss_url=args.wss_url,
            chains=chains,
            channels=channels,
            outputs=outputs,
            status_path=args.status,
            max_rows=args.max_rows,
            duration_seconds=args.once_duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        if args.once:
            return 0 if status.get("ok") else 1
        time.sleep(max(1, args.reconnect_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
