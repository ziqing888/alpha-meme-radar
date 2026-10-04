#!/usr/bin/env python3
"""Persist a pushed Alpha report and expose read-only monitor endpoints."""

from __future__ import annotations

import argparse
import gzip
import hmac
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_DEX_RESPONSE_BYTES = 8 * 1024 * 1024
SAFE_PART = re.compile(r"^[A-Za-z0-9_-]+$")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(body)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def decode_report(body: bytes, encoding: str) -> bytes:
    if encoding.lower() == "gzip":
        body = gzip.decompress(body)
    if len(body) > MAX_REPORT_BYTES:
        raise ValueError("report_too_large")
    payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise ValueError("invalid_report_shape")
    return body


def clean_part(value: str) -> str:
    value = value.strip()
    return value if SAFE_PART.fullmatch(value) else ""


def chain_aliases(chain: str) -> set[str]:
    aliases = {
        "bnb": {"bsc"},
        "bsc": {"bsc"},
        "binance-smart-chain": {"bsc"},
        "sol": {"solana"},
        "solana": {"solana"},
        "eth": {"ethereum", "eth"},
        "ethereum": {"ethereum", "eth"},
        "base": {"base"},
        "robinhood": {"robinhood", "4663"},
        "4663": {"robinhood", "4663"},
    }
    normalized = chain.lower()
    return aliases.get(normalized, {normalized})


def pair_metric(pair: dict[str, Any], section: str, key: str) -> float:
    try:
        return float((pair.get(section) or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def best_pair(pairs: Any, chain: str) -> dict[str, Any] | None:
    rows = [pair for pair in pairs or [] if isinstance(pair, dict)]
    aliases = chain_aliases(chain)
    same_chain = [pair for pair in rows if str(pair.get("chainId") or "").lower() in aliases]
    candidates = same_chain or rows
    return max(
        candidates,
        key=lambda pair: (pair_metric(pair, "liquidity", "usd"), pair_metric(pair, "volume", "h24")),
        default=None,
    )


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def quote_from_pair(pair: dict[str, Any]) -> dict[str, Any]:
    market_cap = finite_number(pair.get("marketCap") or pair.get("fdv"))
    fdv = finite_number(pair.get("fdv") or pair.get("marketCap"))
    price_change = pair.get("priceChange") or {}
    return {
        "priceUsd": finite_number(pair.get("priceUsd")),
        "marketCap": market_cap,
        "fdv": fdv,
        "liquidityUsd": pair_metric(pair, "liquidity", "usd"),
        "volume24h": pair_metric(pair, "volume", "h24"),
        "changeM5": finite_number(price_change.get("m5")),
        "changeH1": finite_number(price_change.get("h1")),
        "changeH24": finite_number(price_change.get("h24")),
        "pairAddress": pair.get("pairAddress"),
        "dexUrl": pair.get("url"),
        "chainId": pair.get("chainId"),
        "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
    }


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "AlphaRadar/1.0"})
    with urlopen(request, timeout=8) as response:
        body = response.read(MAX_DEX_RESPONSE_BYTES + 1)
    if len(body) > MAX_DEX_RESPONSE_BYTES:
        raise ValueError("dex_response_too_large")
    payload = json.loads(body)
    return payload if isinstance(payload, dict) else {}


def fetch_quote(
    target: dict[str, str],
    *,
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
) -> dict[str, Any]:
    kind, chain, address = target["kind"], target["chain"], target["address"]
    if kind == "token":
        payload = fetcher(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
        pair = best_pair(payload.get("pairs"), chain)
    else:
        payload = fetcher(f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{address}")
        pair = payload.get("pair")
    if not isinstance(pair, dict):
        raise ValueError("empty_pair")
    return quote_from_pair(pair)


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "AlphaReadonlyRelay/1.0"

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_report(self) -> None:
        report_path: Path = self.server.report_path
        gzip_path = report_path.with_suffix(report_path.suffix + ".gz")
        wants_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        selected = gzip_path if wants_gzip and gzip_path.exists() else report_path
        if not selected.exists():
            self.send_json(503, {"ok": False, "error": "report_not_uploaded"})
            return
        body = selected.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        if selected == gzip_path:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Content-Encoding")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in {"/api/report", "/report.json"}:
            self.send_report()
            return
        if parsed.path == "/api/health":
            path: Path = self.server.report_path
            self.send_json(
                200,
                {
                    "ok": True,
                    "report_ready": path.exists(),
                    "report_bytes": path.stat().st_size if path.exists() else 0,
                    "report_updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                    if path.exists()
                    else None,
                },
            )
            return
        if parsed.path == "/api/dex-price":
            self.handle_dex_price(parse_qs(parsed.query))
            return
        if parsed.path == "/api/dex-prices":
            self.send_json(410, {"ok": False, "error": "batch_quote_endpoint_retired"})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def handle_dex_price(self, query: dict[str, list[str]]) -> None:
        chain = clean_part((query.get("chain") or [""])[0])
        pair = clean_part((query.get("pair") or [""])[0])
        token = clean_part((query.get("token") or [""])[0])
        if not chain or not (pair or token):
            self.send_json(400, {"ok": False, "error": "missing_chain_and_pair_or_token"})
            return
        target = {"kind": "token" if token else "pair", "chain": chain, "address": token or pair}
        try:
            quote = fetch_quote(target, fetcher=self.server.dex_fetcher)
        except Exception as exc:  # noqa: BLE001
            self.send_json(502, {"ok": False, "error": str(exc)})
            return
        self.send_json(200, {"ok": True, **quote})

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/report":
            self.send_json(405, {"ok": False, "error": "read_only"})
            return
        expected: str = self.server.write_token
        actual = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not expected or not actual or not hmac.compare_digest(actual, expected):
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_json(413, {"ok": False, "error": "upload_too_large"})
            return
        try:
            body = decode_report(self.rfile.read(length), self.headers.get("Content-Encoding") or "")
            report_path: Path = self.server.report_path
            atomic_write(report_path, body)
            atomic_write(report_path.with_suffix(report_path.suffix + ".gz"), gzip.compress(body, compresslevel=6))
        except Exception as exc:  # noqa: BLE001
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        self.send_json(
            200,
            {"ok": True, "persisted": True, "bytes": len(body), "updated_at": now_iso()},
        )

    def reject_mutation(self) -> None:
        self.send_json(405, {"ok": False, "error": "read_only"})

    do_PUT = reject_mutation
    do_PATCH = reject_mutation
    do_DELETE = reject_mutation

    def log_message(self, _format: str, *_args: object) -> None:
        return


def make_server(
    port: int,
    *,
    report_path: Path,
    write_token: str,
    dex_fetcher: Callable[[str], dict[str, Any]] = fetch_json,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), RelayHandler)
    server.report_path = report_path
    server.write_token = write_token
    server.dex_fetcher = dex_fetcher
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--report", type=Path, default=Path("/var/lib/alpha-relay/report.json"))
    args = parser.parse_args()
    token = os.environ.get("ALPHA_REPORT_WRITE_TOKEN", "")
    if not token:
        raise SystemExit("ALPHA_REPORT_WRITE_TOKEN is required")
    server = make_server(args.port, report_path=args.report, write_token=token)
    print(json.dumps({"ready": True, "port": server.server_port, "report": str(args.report)}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
