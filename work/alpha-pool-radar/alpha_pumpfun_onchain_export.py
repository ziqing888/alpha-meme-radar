#!/usr/bin/env python3
"""Read-only Solana Pump.fun logsSubscribe exporter for the Meme first-layer inbox."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
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
DEFAULT_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "pumpfun-onchain.json"
DEFAULT_STATUS = ROOT / "outputs" / "pumpfun-onchain-export-status.json"
DEFAULT_MAX_ROWS = 120
DEFAULT_ONCE_DURATION_SECONDS = 25
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_RECONNECT_SECONDS = 5
BASE58_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


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


def helius_url(scheme: str) -> str:
    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        return ""
    return f"{scheme}://mainnet.helius-rpc.com/?api-key={api_key}"


def resolve_wss_url() -> str:
    explicit = env_first("SOLANA_PUMPFUN_WSS_URL", "SOLANA_WSS_URL", "HELIUS_WSS_URL", "QUICKNODE_WSS_URL")
    if explicit:
        return explicit
    return helius_url("wss")


def resolve_rpc_url(wss_url: str = "") -> str:
    explicit = env_first("SOLANA_PUMPFUN_RPC_URL", "SOLANA_RPC_URL", "HELIUS_RPC_URL", "QUICKNODE_RPC_URL")
    if explicit:
        return explicit
    helius = helius_url("https")
    if helius:
        return helius
    if wss_url.startswith("wss://"):
        return "https://" + wss_url[len("wss://") :]
    if wss_url.startswith("ws://"):
        return "http://" + wss_url[len("ws://") :]
    return ""


def token_key(row: dict[str, Any]) -> str:
    mint = str(row.get("mint") or row.get("tokenAddress") or row.get("address") or "").strip().lower()
    return f"solana:{mint}" if mint else ""


def account_key_to_str(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("pubkey") or value.get("address") or "")
    return str(value or "")


def looks_like_create(logs: list[str]) -> bool:
    joined = "\n".join(str(log) for log in logs)
    needles = ("Instruction: Create", "CreateEvent", "create_event", "Program log: Create")
    return any(needle in joined for needle in needles)


def extract_base58_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(extract_base58_values(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(extract_base58_values(item))
        return found
    return BASE58_RE.findall(str(value or ""))


def likely_mint(candidates: list[str], program_id: str) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate == program_id or candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    for candidate in ordered:
        if candidate.lower().endswith("pump"):
            return candidate
    return ordered[0] if ordered else ""


def fetch_transaction(rpc_url: str, signature: str, timeout_seconds: int) -> dict[str, Any]:
    if not rpc_url or not signature:
        return {}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed",
            },
        ],
    }
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "AlphaRadarPumpfunOnchain/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    result = data.get("result")
    return result if isinstance(result, dict) else {}


def extract_mint_from_transaction(tx: dict[str, Any], program_id: str) -> str:
    candidates: list[str] = []
    meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
    for balance in meta.get("postTokenBalances") or []:
        if isinstance(balance, dict) and balance.get("mint"):
            candidates.append(str(balance.get("mint")))
    message = ((tx.get("transaction") or {}).get("message") or {}) if isinstance(tx.get("transaction"), dict) else {}
    for key in message.get("accountKeys") or []:
        candidates.append(account_key_to_str(key))
    for instruction in message.get("instructions") or []:
        if not isinstance(instruction, dict):
            continue
        parsed = instruction.get("parsed")
        if isinstance(parsed, dict):
            candidates.extend(extract_base58_values(parsed))
        candidates.extend(str(account) for account in (instruction.get("accounts") or []) if account)
    return likely_mint(candidates, program_id)


def normalize_notification(value: dict[str, Any], *, rpc_url: str, program_id: str, timeout_seconds: int) -> dict[str, Any] | None:
    signature = str(value.get("signature") or "")
    logs = [str(log) for log in (value.get("logs") or [])]
    if not signature or value.get("err") or not looks_like_create(logs):
        return None
    tx: dict[str, Any] = {}
    mint = likely_mint(extract_base58_values(logs), program_id)
    if not mint and rpc_url:
        try:
            tx = fetch_transaction(rpc_url, signature, timeout_seconds)
            mint = extract_mint_from_transaction(tx, program_id)
        except Exception:
            tx = {}
    if not mint:
        return None
    return {
        "chain": "solana",
        "chainId": "solana",
        "address": mint,
        "mint": mint,
        "tokenAddress": mint,
        "symbol": mint[:6],
        "name": f"Pump.fun {mint[:6]}",
        "marketCap": 0,
        "liquidityUsd": 0,
        "volume24hUsd": 0,
        "source_family": "pumpfun_onchain",
        "source_origin": "solana_logsSubscribe",
        "launchpad_platform": "Pump.fun",
        "platform": "Pump.fun",
        "pumpfun_event_type": "create",
        "signature": signature,
        "program_id": program_id,
        "market_data_pending": True,
        "first_seen_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }


class OnchainSnapshot:
    def __init__(self, max_rows: int) -> None:
        self.max_rows = max(1, max_rows)
        self.rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.total_notifications = 0
        self.create_events = 0

    def add(self, row: dict[str, Any]) -> None:
        key = token_key(row)
        if not key:
            return
        self.create_events += 1
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
            "source": "pumpfun_onchain",
            "source_family": "pumpfun_onchain",
            "updated_at": utc_now_iso(),
            "reason": reason,
            "count": len(rows),
            "total_notifications": self.total_notifications,
            "create_events": self.create_events,
            "data": rows,
        }


def subscribe_message(program_id: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [{"mentions": [program_id]}, {"commitment": "processed"}],
        }
    )


def stream_once(
    *,
    wss_url: str,
    rpc_url: str,
    program_id: str,
    out_path: Path,
    status_path: Path,
    max_rows: int,
    duration_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    snapshot = OnchainSnapshot(max_rows)
    if not wss_url:
        status = {
            "ok": False,
            "reason": "missing_solana_wss",
            "message": "Set SOLANA_PUMPFUN_WSS_URL, HELIUS_API_KEY, HELIUS_WSS_URL, or QUICKNODE_WSS_URL.",
            "finished_at": utc_now_iso(),
            "out": str(out_path),
        }
        write_json(status_path, status)
        write_json(out_path, snapshot.payload(ok=False, reason="missing_solana_wss"))
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
            ws.send(subscribe_message(program_id))
            while time.monotonic() - started < max(1, duration_seconds):
                raw = ws.recv()
                data = json.loads(raw)
                params = data.get("params") if isinstance(data.get("params"), dict) else {}
                result = params.get("result") if isinstance(params.get("result"), dict) else {}
                value = result.get("value") if isinstance(result.get("value"), dict) else {}
                if not value:
                    continue
                snapshot.total_notifications += 1
                row = normalize_notification(value, rpc_url=rpc_url, program_id=program_id, timeout_seconds=timeout_seconds)
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
            "total_notifications": snapshot.total_notifications,
            "create_events": snapshot.create_events,
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
        "total_notifications": payload["total_notifications"],
        "create_events": payload["create_events"],
        "program_id": program_id,
        "rpc_configured": bool(rpc_url),
    }
    write_json(status_path, status)
    return status


def run_watch(args: argparse.Namespace) -> None:
    while True:
        stream_once(
            wss_url=args.wss_url,
            rpc_url=args.rpc_url,
            program_id=args.program_id,
            out_path=args.out,
            status_path=args.status,
            max_rows=args.max_rows,
            duration_seconds=args.once_duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        if args.once:
            return
        time.sleep(max(1, args.reconnect_seconds))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Pump.fun on-chain logs exporter.")
    parser.add_argument("--wss-url", default=resolve_wss_url())
    parser.add_argument("--rpc-url", default="")
    parser.add_argument("--program-id", default=os.environ.get("PUMPFUN_PROGRAM_ID", DEFAULT_PROGRAM_ID))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--max-rows", type=int, default=int(os.environ.get("PUMPFUN_ONCHAIN_MAX_ROWS", DEFAULT_MAX_ROWS)))
    parser.add_argument("--once-duration-seconds", type=int, default=int(os.environ.get("PUMPFUN_ONCHAIN_ONCE_DURATION_SECONDS", DEFAULT_ONCE_DURATION_SECONDS)))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("PUMPFUN_ONCHAIN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)))
    parser.add_argument("--reconnect-seconds", type=int, default=int(os.environ.get("PUMPFUN_ONCHAIN_RECONNECT_SECONDS", DEFAULT_RECONNECT_SECONDS)))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.rpc_url:
        args.rpc_url = resolve_rpc_url(args.wss_url)
    return args


def main() -> int:
    args = parse_args()
    status = stream_once(
        wss_url=args.wss_url,
        rpc_url=args.rpc_url,
        program_id=args.program_id,
        out_path=args.out,
        status_path=args.status,
        max_rows=args.max_rows,
        duration_seconds=args.once_duration_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    if args.once:
        return 0 if status.get("ok") or status.get("reason") == "missing_solana_wss" else 1
    while True:
        time.sleep(max(1, args.reconnect_seconds))
        status = stream_once(
            wss_url=args.wss_url,
            rpc_url=args.rpc_url,
            program_id=args.program_id,
            out_path=args.out,
            status_path=args.status,
            max_rows=args.max_rows,
            duration_seconds=args.once_duration_seconds,
            timeout_seconds=args.timeout_seconds,
        )


if __name__ == "__main__":
    raise SystemExit(main())
