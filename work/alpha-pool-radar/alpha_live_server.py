#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from alpha_fast_track import overlay_report, read_json
from alpha_local_voice import VOICE_CACHE_DIR
from alpha_monitor_chains import normalize_monitor_chain
from alpha_system_health import build_system_health


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_REPORT = Path("outputs") / "alpha-radar-report-latest.json"
FAST_MEME_SNAPSHOT_NAME = "alpha-meme-fast-latest.json"
FAST_MEME_FIELDS = {
    "meme_rows",
    "meme_watch_universe",
    "meme_potential_rows",
    "meme_pending_rows",
    "meme_shadow_rows",
    "monitor_v3",
}
_JSON_CACHE: dict[Path, tuple[int, int, dict[str, Any]]] = {}
_JSON_CACHE_LOCK = threading.Lock()


def _read_json_cached(path: Path) -> dict[str, Any]:
    stat = path.stat()
    cache_key = path.resolve()
    signature = (stat.st_mtime_ns, stat.st_size)
    with _JSON_CACHE_LOCK:
        cached = _JSON_CACHE.get(cache_key)
        if cached and cached[:2] == signature:
            return cached[2]
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} is not an object")
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[cache_key] = (*signature, payload)
    return payload


def _positive_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(number) if number.is_integer() else number


def _monitor_identity(chain: Any, address: Any) -> str:
    normalized_chain = normalize_monitor_chain(chain)
    normalized_address = str(address or "").strip().lower()
    return f"{normalized_chain}:{normalized_address}" if normalized_chain and normalized_address else ""


def monitor_discovery_baselines(
    report: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    visible = {
        _monitor_identity(identity.get("chain"), identity.get("contract_address"))
        for token in snapshot.get("tokens") or []
        if isinstance(token, dict)
        and isinstance((identity := token.get("identity")), dict)
    }
    baselines: dict[str, dict[str, Any]] = {}
    for section in (
        "meme_watch_universe",
        "meme_potential_rows",
        "meme_shadow_rows",
        "meme_rows",
        "meme_heat_rows",
        "meme_conviction_rows",
    ):
        for row in report.get(section) or []:
            if not isinstance(row, dict):
                continue
            key = _monitor_identity(
                row.get("chain") or row.get("chain_id"),
                row.get("contract_address") or row.get("token_address"),
            )
            if key not in visible:
                continue
            replay = row.get("replay") if isinstance(row.get("replay"), dict) else {}
            first_snapshot = replay.get("first_snapshot") if isinstance(replay.get("first_snapshot"), dict) else {}
            first_market_cap = _positive_number(
                row.get("watch_first_seen_mcap")
                or replay.get("first_mcap_usd")
                or first_snapshot.get("mcap")
                or first_snapshot.get("market_cap")
            )
            first_price = _positive_number(
                replay.get("first_price_usd") or first_snapshot.get("price_usd")
            )
            peak_market_cap = _positive_number(
                row.get("watch_max_seen_mcap")
                or replay.get("max_mcap_usd")
                or first_market_cap
            )
            incoming = {
                "first_seen_at": row.get("watch_first_seen_at") or replay.get("first_seen_at") or None,
                "first_market_cap_usd": first_market_cap,
                "first_price_usd": first_price,
                "peak_market_cap_usd": peak_market_cap,
            }
            existing = baselines.get(key)
            if existing is None:
                baselines[key] = incoming
                continue
            existing_time = str(existing.get("first_seen_at") or "")
            incoming_time = str(incoming.get("first_seen_at") or "")
            if incoming_time and (not existing_time or incoming_time < existing_time):
                merged = {**existing, **{k: v for k, v in incoming.items() if v is not None}}
            else:
                merged = {**incoming, **{k: v for k, v in existing.items() if v is not None}}
            peaks = [
                value for value in (
                    _positive_number(existing.get("peak_market_cap_usd")),
                    peak_market_cap,
                ) if value is not None
            ]
            merged["peak_market_cap_usd"] = max(peaks) if peaks else None
            baselines[key] = merged
    return baselines


def compact_monitor_transport(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Trim retained projection history to the evidence needed by the live UI."""
    compact = copy.deepcopy(snapshot)
    for token in compact.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        events = [item for item in token.get("events") or [] if isinstance(item, dict)]
        keep_events = events[-6:]
        if events and events[0] not in keep_events:
            keep_events.insert(0, events[0])
        token["events"] = keep_events
        market = token.get("market") if isinstance(token.get("market"), dict) else {}
        snapshots = [item for item in market.get("snapshots") or [] if isinstance(item, dict)]
        keep_snapshots = snapshots[-1:]
        if snapshots:
            peak = max(snapshots, key=lambda item: float(item.get("market_cap_usd") or 0))
            for item in (snapshots[0], peak):
                if item not in keep_snapshots:
                    keep_snapshots.append(item)
        market["snapshots"] = sorted(
            keep_snapshots, key=lambda item: str(item.get("observed_at") or "")
        )
        token["market"] = market
        token["state_history"] = list(token.get("state_history") or [])[-8:]
        token["audit_facts"] = list(token.get("audit_facts") or [])[-8:]
        risk = token.get("risk") if isinstance(token.get("risk"), dict) else {}
        risk["evidence"] = list(risk.get("evidence") or [])[-8:]
        token["risk"] = risk
        resonance = token.get("resonance") if isinstance(token.get("resonance"), dict) else {}
        resonance["evidence_ids"] = list(resonance.get("evidence_ids") or [])[-12:]
        resonance["historical_evidence_ids"] = list(
            resonance.get("historical_evidence_ids") or []
        )[-12:]
        token["resonance"] = resonance
        wallet = token.get("wallet_evidence") if isinstance(token.get("wallet_evidence"), dict) else {}
        wallet["historical_wallet_addresses"] = list(
            wallet.get("historical_wallet_addresses") or []
        )[-12:]
        token["wallet_evidence"] = wallet
    compact["rejections"] = list(compact.get("rejections") or [])[-40:]
    alert_fields = {
        "key", "label", "severity", "policy", "chain", "contract_address", "symbol",
        "transition", "state_version", "event_at", "observed_at", "real_age_seconds",
        "event_age_seconds", "market", "verified_wallet_count", "risks", "missing_evidence",
        "evidence_ids",
    }
    compact["alerts"] = [
        {key: value for key, value in alert.items() if key in alert_fields}
        for alert in list(compact.get("alerts") or [])[-40:]
        if isinstance(alert, dict)
    ]
    return compact


def _timestamp_millis(value: Any) -> float:
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return 0
    return parsed.timestamp()


def overlay_fast_meme_snapshot(
    report: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    if not snapshot:
        return report
    report_meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    snapshot_meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    report_at = _timestamp_millis(report_meta.get("meme_live_generated_at"))
    snapshot_at = _timestamp_millis(snapshot_meta.get("meme_live_generated_at"))
    if report_at and snapshot_at < report_at:
        return report
    merged = dict(report)
    merged["meta"] = {**report_meta, **snapshot_meta}
    for field in FAST_MEME_FIELDS:
        if field in snapshot:
            merged[field] = snapshot[field]
    return merged


def load_report_payload(report_path: Path) -> tuple[int, dict[str, Any]]:
    try:
        payload = _read_json_cached(report_path)
    except Exception as exc:
        return 503, {"ok": False, "step": "read_report", "error": str(exc)}
    payload = overlay_fast_meme_snapshot(
        payload,
        read_json(report_path.parent / FAST_MEME_SNAPSHOT_NAME),
    )
    payload = overlay_report(payload, read_json(report_path.parent / "alpha-fast-track.json"))
    prelaunch = read_json(report_path.parent / "prelaunch-project-watch.json")
    if prelaunch:
        payload["prelaunch_projects"] = [{**row, "official_verified": False} for row in prelaunch.get("candidates") or []]
    return 200, payload


def load_monitor_payload(report_path: Path) -> tuple[int, dict[str, Any]]:
    compact_path = report_path.parent / "alpha-meme-monitor-v3-latest.json"
    try:
        snapshot = _read_json_cached(compact_path)
        visible = {
            _monitor_identity(identity.get("chain"), identity.get("contract_address"))
            for token in snapshot.get("tokens") or []
            if isinstance(token, dict)
            and isinstance((identity := token.get("identity")), dict)
        }
        baselines_path = report_path.parent / "alpha-monitor-baselines-latest.json"
        all_baselines = _read_json_cached(baselines_path) if baselines_path.exists() else {}
        baselines = {
            key: value
            for key, value in all_baselines.items()
            if key in visible and isinstance(value, dict)
        } if isinstance(all_baselines, dict) else {}
        intelligence_rows = []
        intelligence_path = report_path.parent / "token-intelligence.json"
        intelligence = _read_json_cached(intelligence_path) if intelligence_path.exists() else {}
        records = intelligence.get("records") if isinstance(intelligence.get("records"), dict) else {}
        for token in snapshot.get("tokens") or []:
            if not isinstance(token, dict):
                continue
            identity = token.get("identity") if isinstance(token.get("identity"), dict) else {}
            chain = normalize_monitor_chain(identity.get("chain"))
            address = str(identity.get("contract_address") or "").strip().lower()
            record = records.get(f"{chain}:{address}")
            if isinstance(record, dict):
                intelligence_rows.append(
                    {"chain": chain, "contract_address": address, "token_intelligence": record}
                )
        return 200, {
            "monitor_v3": compact_monitor_transport(snapshot),
            "monitor_baselines": baselines,
            "monitor_intelligence_rows": intelligence_rows,
        }
    except Exception:
        status, report = load_report_payload(report_path)
        if status != 200 or not isinstance(report.get("monitor_v3"), dict):
            return 503, {"ok": False, "step": "read_monitor", "error": "monitor_v3 unavailable"}
        return 200, {"monitor_v3": report["monitor_v3"]}


class LiveReportHandler(BaseHTTPRequestHandler):
    report_path: Path = DEFAULT_REPORT

    def do_OPTIONS(self) -> None:
        self._send_json(204, {})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            payload = build_system_health(self.report_path.parent)
            payload["report"] = str(self.report_path)
            self._send_json(200 if payload["ok"] else 503, payload)
            return
        if path == "/monitor-v3.json":
            status, payload = load_monitor_payload(self.report_path)
            self._send_json(status, payload)
            return
        if path.startswith("/voice/"):
            self._send_voice(unquote(path.removeprefix("/voice/")))
            return
        if path not in {"/", "/report.json"}:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        status, payload = load_report_payload(self.report_path)
        self._send_json(status, payload)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if status != 204:
            self.wfile.write(body)

    def _send_voice(self, file_name: str) -> None:
        cache_root = VOICE_CACHE_DIR.resolve()
        candidate = (cache_root / Path(file_name).name).resolve()
        valid_name = file_name == Path(file_name).name and file_name.startswith("kokoro-") and file_name.endswith(".wav")
        if not valid_name or cache_root not in candidate.parents or not candidate.exists():
            self._send_json(404, {"ok": False, "error": "voice_not_found"})
            return
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the latest Alpha Radar report as live JSON.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-fast-track", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    LiveReportHandler.report_path = args.report
    server = ThreadingHTTPServer((args.host, args.port), LiveReportHandler)
    if not args.no_fast_track:
        env = dict(os.environ)
        for name in ("ALPHA_NARRATIVE_BROWSER_COMMANDS", "ALPHA_NARRATIVE_TWEET_URLS", "ALPHA_NARRATIVE_TWEET_FILES", "ALPHA_NARRATIVE_TWEETS_FILE"):
            env[name] = ""
        subprocess.Popen([sys.executable, str(Path(__file__).with_name("alpha_fast_track.py")), "--out-dir", str(args.report.resolve().parent), "--report", str(args.report.resolve())], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"Alpha live report server: http://{args.host}:{args.port}/report.json")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
