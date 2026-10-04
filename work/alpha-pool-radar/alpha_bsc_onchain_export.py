#!/usr/bin/env python3
"""Read-only BSC PancakeSwap new-pair exporter for the Meme first-layer inbox."""

from __future__ import annotations

import argparse
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
STABLES = {
    WBNB.lower(),
    "0x55d398326f99059ff775485246999027b3197955".lower(),  # USDT
    "0xe9e7cea3dedca5984780bafc599bd69add087d56".lower(),  # BUSD
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d".lower(),  # USDC
}
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "bsc-pancake-pairs.json"
DEFAULT_STATUS = ROOT / "outputs" / "bsc-onchain-export-status.json"
DEFAULT_MAX_ROWS = 160
DEFAULT_ONCE_DURATION_SECONDS = 25
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_RECONNECT_SECONDS = 5


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def resolve_wss_url() -> str:
    return env_first("BSC_WSS_URL", "BNBCHAIN_WSS_URL", "BSC_PANCAKE_WSS_URL", "QUICKNODE_BSC_WSS_URL")


def normalize_address(raw: str) -> str:
    value = str(raw or "").lower().replace("0x", "")
    if len(value) < 40:
        return ""
    return "0x" + value[-40:]


def topic_address(topic: str) -> str:
    return normalize_address(topic)


def data_address(data: str, word_index: int = 0) -> str:
    value = str(data or "").lower().replace("0x", "")
    start = word_index * 64
    return normalize_address(value[start : start + 64])


def choose_token(token0: str, token1: str) -> str:
    low0 = token0.lower()
    low1 = token1.lower()
    if low0 in STABLES and low1 not in STABLES:
        return token1
    if low1 in STABLES and low0 not in STABLES:
        return token0
    return token1 or token0


def normalize_pair_log(log: dict[str, Any], *, factory: str) -> dict[str, Any] | None:
    topics = [str(topic) for topic in (log.get("topics") or [])]
    if len(topics) < 3 or topics[0].lower() != PAIR_CREATED_TOPIC:
        return None
    token0 = topic_address(topics[1])
    token1 = topic_address(topics[2])
    pair = data_address(str(log.get("data") or ""), 0)
    token = choose_token(token0, token1)
    if not token or not pair:
        return None
    now = utc_now_iso()
    return {
        "chain": "bsc",
        "chainId": "bsc",
        "address": token,
        "tokenAddress": token,
        "contractAddress": token,
        "symbol": token[-6:].upper(),
        "name": f"BSC new pair {token[-6:].upper()}",
        "marketCap": 0,
        "liquidityUsd": 0,
        "volume24hUsd": 0,
        "source_family": "bsc_onchain",
        "source_origin": "pancakeswap_v2_pair_created",
        "launchpad_platform": "PancakeSwap",
        "platform": "PancakeSwap V2",
        "bsc_event_type": "PairCreated",
        "factory": factory,
        "pair_address": pair,
        "token0": token0,
        "token1": token1,
        "base_asset": "WBNB" if token0.lower() == WBNB.lower() or token1.lower() == WBNB.lower() else "",
        "transaction_hash": log.get("transactionHash") or "",
        "block_number": log.get("blockNumber") or "",
        "market_data_pending": True,
        "first_seen_at": now,
        "updated_at": now,
    }


class BscPairSnapshot:
    def __init__(self, max_rows: int) -> None:
        self.max_rows = max(1, max_rows)
        self.rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.total_logs = 0
        self.pair_events = 0

    def add(self, row: dict[str, Any]) -> None:
        key = f"bsc:{str(row.get('tokenAddress') or '').lower()}"
        if not key.endswith("0x") and key:
            self.pair_events += 1
            if key in self.rows:
                previous = self.rows.pop(key)
                row = {**previous, **row}
            self.rows[key] = row
            while len(self.rows) > self.max_rows:
                self.rows.popitem(last=False)

    def payload(self, *, ok: bool = True, reason: str = "") -> dict[str, Any]:
        rows = list(reversed(list(self.rows.values())))
        return {
            "ok": ok,
            "source": "bsc_onchain",
            "source_family": "bsc_onchain",
            "updated_at": utc_now_iso(),
            "reason": reason,
            "count": len(rows),
            "total_logs": self.total_logs,
            "pair_events": self.pair_events,
            "data": rows,
        }


def subscribe_message(factory: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": ["logs", {"address": factory, "topics": [PAIR_CREATED_TOPIC]}],
        }
    )


def stream_once(
    *,
    wss_url: str,
    factory: str,
    out_path: Path,
    status_path: Path,
    max_rows: int,
    duration_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    snapshot = BscPairSnapshot(max_rows)
    if not wss_url:
        status = {
            "ok": False,
            "reason": "missing_bsc_wss",
            "message": "Set BSC_WSS_URL, BNBCHAIN_WSS_URL, BSC_PANCAKE_WSS_URL, or QUICKNODE_BSC_WSS_URL.",
            "finished_at": utc_now_iso(),
            "out": str(out_path),
        }
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="missing_bsc_wss"))
        return status
    if websocket is None:
        status = {"ok": False, "reason": "missing_websocket_client", "finished_at": utc_now_iso(), "out": str(out_path)}
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="missing_websocket_client"))
        return status

    started = time.monotonic()
    try:
        ws = websocket.create_connection(wss_url, timeout=timeout_seconds)
        try:
            ws.settimeout(min(2, max(1, timeout_seconds)))
            ws.send(subscribe_message(factory))
            while time.monotonic() - started < max(1, duration_seconds):
                try:
                    raw = ws.recv()
                except Exception as exc:  # noqa: BLE001
                    if websocket is not None and isinstance(exc, websocket.WebSocketTimeoutException):
                        continue
                    raise
                data = json.loads(raw)
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                result = params.get("result") if isinstance(params.get("result"), dict) else {}
                if not result:
                    continue
                snapshot.total_logs += 1
                row = normalize_pair_log(result, factory=factory)
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
            "total_logs": snapshot.total_logs,
            "pair_events": snapshot.pair_events,
        }
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="stream_error"))
        return status

    payload = snapshot.payload()
    write_json(out_path, payload)
    status = {
        "ok": True,
        "reason": "",
        "finished_at": utc_now_iso(),
        "out": str(out_path),
        "rows": payload["count"],
        "total_logs": payload["total_logs"],
        "pair_events": payload["pair_events"],
        "factory": factory,
    }
    write_json(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only BSC PancakeSwap PairCreated exporter.")
    parser.add_argument("--wss-url", default=resolve_wss_url())
    parser.add_argument("--factory", default=os.environ.get("PANCAKE_V2_FACTORY", PANCAKE_V2_FACTORY))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--max-rows", type=int, default=int(os.environ.get("BSC_ONCHAIN_MAX_ROWS", DEFAULT_MAX_ROWS)))
    parser.add_argument("--once-duration-seconds", type=int, default=int(os.environ.get("BSC_ONCHAIN_ONCE_DURATION_SECONDS", DEFAULT_ONCE_DURATION_SECONDS)))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("BSC_ONCHAIN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)))
    parser.add_argument("--reconnect-seconds", type=int, default=int(os.environ.get("BSC_ONCHAIN_RECONNECT_SECONDS", DEFAULT_RECONNECT_SECONDS)))
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        status = stream_once(
            wss_url=args.wss_url,
            factory=args.factory,
            out_path=args.out,
            status_path=args.status,
            max_rows=args.max_rows,
            duration_seconds=args.once_duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        if args.once:
            return 0 if status.get("ok") or status.get("reason") == "missing_bsc_wss" else 1
        time.sleep(max(1, args.reconnect_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
