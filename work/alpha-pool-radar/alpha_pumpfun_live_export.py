#!/usr/bin/env python3
"""Read-only Pump.fun live event exporter for the Meme first-layer inbox."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVER_URL = "https://muhammetakkurtt--pump-fun-real-time-monitor.apify.actor"
DEFAULT_ENDPOINT = "all"
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "pumpfun-live.json"
DEFAULT_STATUS = ROOT / "outputs" / "pumpfun-live-export-status.json"
DEFAULT_MAX_ROWS = 80
DEFAULT_CONNECT_TIMEOUT_SECONDS = 20
DEFAULT_ONCE_DURATION_SECONDS = 25
DEFAULT_RECONNECT_SECONDS = 5


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def token_key(row: dict[str, Any]) -> str:
    chain = str(row.get("chain") or row.get("chainId") or "solana").strip().lower()
    address = str(row.get("address") or row.get("mint") or row.get("tokenAddress") or "").strip().lower()
    return f"{chain}:{address}" if address else ""


def compact_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def unwrap_event_data(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict):
        return {**inner, "raw_event_wrapper": data}
    return data


def normalize_event_row(event_type: str, data: Any, *, origin: str) -> dict[str, Any] | None:
    row = unwrap_event_data(data)
    if not row:
        return None
    address = str(
        first_present(
            row,
            (
                "mint",
                "tokenAddress",
                "token_address",
                "address",
                "contractAddress",
                "baseMint",
                "base_mint",
            ),
        )
        or ""
    ).strip()
    if not address:
        return None
    symbol = str(first_present(row, ("symbol", "ticker", "tokenSymbol", "name")) or "").strip()
    name = str(first_present(row, ("name", "tokenName", "symbol", "ticker")) or symbol).strip()
    market_cap = compact_float(first_present(row, ("marketCap", "market_cap", "mcap", "usdMarketCap", "fdv")))
    liquidity = compact_float(first_present(row, ("liquidityUsd", "liquidity_usd", "liquidity", "virtualSolReserves")))
    volume = compact_float(first_present(row, ("volume24hUsd", "volume_24h_usd", "volume", "volumeUsd", "usdVolume")))
    created_at = first_present(row, ("created_at", "createdAt", "timestamp", "time", "createdTimestamp"))
    normalized = {
        **row,
        "chain": "solana",
        "chainId": "solana",
        "address": address,
        "mint": address,
        "tokenAddress": address,
        "symbol": symbol,
        "name": name,
        "marketCap": market_cap,
        "liquidityUsd": liquidity,
        "volume24hUsd": volume,
        "source_family": "pumpfun_live",
        "source_origin": origin[:180],
        "launchpad_platform": "Pump.fun",
        "platform": "Pump.fun",
        "pumpfun_event_type": event_type,
        "first_seen_at": created_at or utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    return normalized


class PumpfunSnapshot:
    def __init__(self, max_rows: int) -> None:
        self.max_rows = max(1, max_rows)
        self.rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.total_events = 0
        self.last_event_type = ""

    def add(self, row: dict[str, Any]) -> None:
        key = token_key(row)
        if not key:
            return
        self.total_events += 1
        self.last_event_type = str(row.get("pumpfun_event_type") or "")
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
            "source": "pumpfun_live",
            "source_family": "pumpfun_live",
            "updated_at": utc_now_iso(),
            "reason": reason,
            "count": len(rows),
            "total_events": self.total_events,
            "last_event_type": self.last_event_type,
            "data": rows,
        }


def build_request(server_url: str, endpoint: str, api_token: str) -> urllib.request.Request:
    url = f"{server_url.rstrip('/')}/events/{endpoint.strip('/')}"
    return urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "text/event-stream",
            "Connection": "keep-alive",
            "User-Agent": "AlphaRadarPumpfunExporter/1.0",
        },
    )


def stream_once(
    *,
    server_url: str,
    endpoint: str,
    api_token: str,
    out_path: Path,
    status_path: Path,
    max_rows: int,
    duration_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    snapshot = PumpfunSnapshot(max_rows)
    if not api_token:
        status = {
            "ok": False,
            "reason": "missing_apify_token",
            "message": "Set APIFY_TOKEN to enable Pump.fun live first-layer export.",
            "finished_at": utc_now_iso(),
            "out": str(out_path),
        }
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="missing_apify_token"))
        return status

    started = time.monotonic()
    current_event = "message"
    request = build_request(server_url, endpoint, api_token)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            while time.monotonic() - started < max(1, duration_seconds):
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip() or "message"
                    continue
                if not line.startswith("data:"):
                    continue
                raw_data = line.split(":", 1)[1].strip()
                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError:
                    continue
                row = normalize_event_row(current_event, data, origin=f"{server_url}/events/{endpoint}")
                if row:
                    snapshot.add(row)
                    write_json(out_path, snapshot.payload())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        status = {
            "ok": False,
            "reason": f"http_{exc.code}",
            "error": body or str(exc),
            "finished_at": utc_now_iso(),
            "out": str(out_path),
        }
        write_json(status_path, status)
        return status
    except Exception as exc:  # noqa: BLE001
        status = {
            "ok": False,
            "reason": "stream_error",
            "error": str(exc),
            "finished_at": utc_now_iso(),
            "out": str(out_path),
        }
        write_json(status_path, status)
        return status

    payload = snapshot.payload()
    write_json(out_path, payload)
    status = {
        "ok": True,
        "reason": "",
        "finished_at": utc_now_iso(),
        "out": str(out_path),
        "rows": payload["count"],
        "total_events": payload["total_events"],
        "endpoint": endpoint,
    }
    write_json(status_path, status)
    return status


def run_watch(args: argparse.Namespace) -> None:
    while True:
        status = stream_once(
            server_url=args.server_url,
            endpoint=args.endpoint,
            api_token=args.api_token,
            out_path=args.out,
            status_path=args.status,
            max_rows=args.max_rows,
            duration_seconds=args.once_duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(status, ensure_ascii=False))
        time.sleep(max(1, args.reconnect_seconds))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Pump.fun SSE exporter for Alpha Meme Radar.")
    parser.add_argument("--server-url", default=os.environ.get("PUMPFUN_SERVER_URL", DEFAULT_SERVER_URL))
    parser.add_argument("--endpoint", default=os.environ.get("PUMPFUN_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--api-token", default=os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN") or "")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_CONNECT_TIMEOUT_SECONDS)
    parser.add_argument("--once-duration-seconds", type=int, default=DEFAULT_ONCE_DURATION_SECONDS)
    parser.add_argument("--reconnect-seconds", type=int, default=DEFAULT_RECONNECT_SECONDS)
    parser.add_argument("--watch", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.watch:
        run_watch(args)
        return
    status = stream_once(
        server_url=args.server_url,
        endpoint=args.endpoint,
        api_token=args.api_token,
        out_path=args.out,
        status_path=args.status,
        max_rows=args.max_rows,
        duration_seconds=args.once_duration_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
