#!/usr/bin/env python3
"""Read-only public BSC RPC poller for PancakeSwap new-pair events."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import alpha_bsc_onchain_export as bsc_onchain

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = bsc_onchain.DEFAULT_OUT
DEFAULT_STATUS = ROOT / "outputs" / "bsc-public-pair-poll-status.json"
DEFAULT_RPC_URLS = (
    "https://bsc-rpc.publicnode.com",
    "https://bnb.api.onfinality.io/public",
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
)
DEFAULT_LOOKBACK_BLOCKS = 20
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_QUOTE_ASSETS = (bsc_onchain.WBNB,)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def rpc_urls() -> list[str]:
    configured = os.environ.get("BSC_HTTP_RPC_URLS", "").strip() or os.environ.get("BSC_HTTP_RPC_URL", "").strip()
    if configured:
        return [url.strip() for url in configured.replace("\n", ",").split(",") if url.strip()]
    return list(DEFAULT_RPC_URLS)


def hex_quantity(value: int) -> str:
    return hex(max(0, int(value)))


def topic_address(address: str) -> str:
    cleaned = str(address or "").lower().replace("0x", "")
    if len(cleaned) != 40:
        return ""
    return "0x" + ("0" * 24) + cleaned


def quote_assets() -> list[str]:
    configured = os.environ.get("BSC_PUBLIC_PAIR_QUOTE_ASSETS", "").strip()
    values = configured.replace("\n", ",").split(",") if configured else list(DEFAULT_QUOTE_ASSETS)
    return [address for address in (item.strip() for item in values) if topic_address(address)]


def rpc_call(url: str, method: str, params: list[Any], *, timeout_seconds: int) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 AlphaRadar/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError(json.dumps(payload["error"], ensure_ascii=False))
    return payload.get("result")


def first_success(urls: list[str], method: str, params: list[Any], *, timeout_seconds: int) -> tuple[Any, str]:
    errors: list[str] = []
    for url in urls:
        for attempt in range(2):
            try:
                return rpc_call(url, method, params, timeout_seconds=timeout_seconds), url
            except (OSError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
                errors.append(f"{url}: {exc}")
                if attempt == 0:
                    time.sleep(0.2)
    raise RuntimeError("; ".join(errors) or "all_rpc_failed")


def block_number(urls: list[str], *, timeout_seconds: int) -> tuple[int, str]:
    result, url = first_success(urls, "eth_blockNumber", [], timeout_seconds=timeout_seconds)
    return int(str(result), 16), url


def pair_logs(
    urls: list[str],
    *,
    factory: str,
    from_block: int,
    to_block: int,
    quote_assets_: list[str],
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], str]:
    results: dict[str, dict[str, Any]] = {}
    used_url = ""
    for quote in quote_assets_:
        quote_topic = topic_address(quote)
        filters = (
            {
                "address": factory,
                "fromBlock": hex_quantity(from_block),
                "toBlock": hex_quantity(to_block),
                "topics": [bsc_onchain.PAIR_CREATED_TOPIC, quote_topic],
            },
            {
                "address": factory,
                "fromBlock": hex_quantity(from_block),
                "toBlock": hex_quantity(to_block),
                "topics": [bsc_onchain.PAIR_CREATED_TOPIC, None, quote_topic],
            },
        )
        for item_filter in filters:
            result, url = first_success(urls, "eth_getLogs", [item_filter], timeout_seconds=timeout_seconds)
            used_url = used_url or url
            for item in result or []:
                if isinstance(item, dict):
                    key = str(item.get("transactionHash") or "") + ":" + str(item.get("logIndex") or "")
                    results[key] = item
    return list(results.values()), used_url


def broad_pair_logs(
    urls: list[str],
    *,
    factory: str,
    from_block: int,
    to_block: int,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], str]:
    item_filter = {
            "address": factory,
            "fromBlock": hex_quantity(from_block),
            "toBlock": hex_quantity(to_block),
            "topics": [bsc_onchain.PAIR_CREATED_TOPIC],
        }
    result, url = first_success(urls, "eth_getLogs", [item_filter], timeout_seconds=timeout_seconds)
    return [item for item in (result or []) if isinstance(item, dict)], url


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    urls = args.rpc_urls or rpc_urls()
    snapshot = bsc_onchain.BscPairSnapshot(args.max_rows)
    started_at = utc_now_iso()
    try:
        latest_block, block_rpc_url = block_number(urls, timeout_seconds=args.timeout_seconds)
        from_block = max(0, latest_block - max(1, args.lookback_blocks))
        quotes = args.quote_assets or quote_assets()
        if args.broad:
            logs, logs_rpc_url = broad_pair_logs(
                urls,
                factory=args.factory,
                from_block=from_block,
                to_block=latest_block,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            logs, logs_rpc_url = pair_logs(
                urls,
                factory=args.factory,
                from_block=from_block,
                to_block=latest_block,
                quote_assets_=quotes,
                timeout_seconds=args.timeout_seconds,
            )
        for log in logs:
            snapshot.total_logs += 1
            row = bsc_onchain.normalize_pair_log(log, factory=args.factory)
            if row:
                row["source_origin"] = "pancakeswap_v2_pair_created_public_rpc"
                row["block_range_from"] = from_block
                row["block_range_to"] = latest_block
                snapshot.add(row)
        payload = snapshot.payload()
        payload["mode"] = "public_rpc_poll"
        payload["block_range_from"] = from_block
        payload["block_range_to"] = latest_block
        payload["rpc_url"] = logs_rpc_url
        payload["quote_assets"] = quotes
        write_json(args.out, payload)
        status = {
            "ok": True,
            "reason": "",
            "mode": "public_rpc_poll",
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "out": str(args.out),
            "rows": payload["count"],
            "total_logs": payload["total_logs"],
            "pair_events": payload["pair_events"],
            "latest_block": latest_block,
            "from_block": from_block,
            "block_rpc_url": block_rpc_url,
            "logs_rpc_url": logs_rpc_url,
            "quote_assets": quotes,
        }
    except Exception as exc:  # noqa: BLE001
        previous_payload = read_json(args.out)
        if not previous_payload:
            payload = snapshot.payload(ok=False, reason="public_rpc_error")
            payload["mode"] = "public_rpc_poll"
            write_json(args.out, payload)
        status = {
            "ok": False,
            "reason": "public_rpc_error",
            "mode": "public_rpc_poll",
            "started_at": started_at,
            "finished_at": utc_now_iso(),
            "out": str(args.out),
            "error": str(exc),
            "preserved_previous_rows": len(previous_payload.get("data") or []) if previous_payload else 0,
        }
    write_json(args.status, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only BSC public RPC PairCreated poller.")
    parser.add_argument("--rpc-url", dest="rpc_urls", action="append", default=[])
    parser.add_argument("--factory", default=os.environ.get("PANCAKE_V2_FACTORY", bsc_onchain.PANCAKE_V2_FACTORY))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--max-rows", type=int, default=int(os.environ.get("BSC_PUBLIC_PAIR_MAX_ROWS", "160")))
    parser.add_argument("--lookback-blocks", type=int, default=int(os.environ.get("BSC_PUBLIC_PAIR_LOOKBACK_BLOCKS", DEFAULT_LOOKBACK_BLOCKS)))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("BSC_PUBLIC_PAIR_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)))
    parser.add_argument("--interval-seconds", type=int, default=int(os.environ.get("BSC_PUBLIC_PAIR_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)))
    parser.add_argument("--quote-asset", dest="quote_assets", action="append", default=[])
    parser.add_argument("--broad", action="store_true", help="Poll all PairCreated logs without quote-asset filtering.")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        status = run_once(args)
        if args.once:
            return 0 if status.get("ok") else 1
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
