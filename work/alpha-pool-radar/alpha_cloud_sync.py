#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from alpha_cloud_report import DEFAULT_SITE_URL, env_value, upload_report, write_status
from alpha_fast_track import overlay_report, read_json, utc_now


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "outputs"
DEFAULT_REPORT = DEFAULT_OUT_DIR / "alpha-radar-report-latest.json"
DEFAULT_STATUS = DEFAULT_OUT_DIR / "alpha-cloud-sync-status.json"


PUBLIC_EMPTY_LIST_FIELDS = {
    "events",
    "wallet_addresses",
    "candidate_wallet_addresses",
    "historical_candidate_wallet_addresses",
    "historical_wallet_addresses",
    "conflicting_wallets",
    "evidence_ids",
    "candidate_evidence_ids",
    "historical_evidence_ids",
    "conflicting_evidence_ids",
    "okx_signal_wallet_addresses",
}
PUBLIC_SECRET_FIELDS = {
    "api_key",
    "authorization",
    "credential",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "secret_key",
    "wallet_address",
}


def _public_safe(value, field: str = ""):
    normalized = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
    if normalized in PUBLIC_EMPTY_LIST_FIELDS:
        return []
    if normalized in PUBLIC_SECRET_FIELDS or normalized.endswith(("_private_key", "_api_key", "_secret")):
        return None
    if isinstance(value, dict):
        projected = {}
        for key, item in value.items():
            safe = _public_safe(item, str(key))
            if safe is not None:
                projected[key] = safe
        return projected
    if isinstance(value, list):
        return [_public_safe(item) for item in value]
    if isinstance(value, str) and (
        re.match(r"^[A-Za-z]:[\\/]", value)
        or value.startswith(("/home/", "/Users/", "/root/"))
    ):
        return ""
    return value



def slim_for_cloudflare(report: dict) -> dict:
    """Keep monitor_v3 parseable for the web UI while staying under Pages limits."""
    payload = _public_safe(report)
    monitor = payload.get("monitor_v3")
    if isinstance(monitor, dict):
        slim_monitor = dict(monitor)
        tokens = slim_monitor.get("tokens")
        if isinstance(tokens, list):
            kept = []
            for token in tokens[:120]:
                if not isinstance(token, dict):
                    continue
                row = dict(token)
                if isinstance(row.get("events"), list):
                    row["events"] = row["events"][-20:]
                if isinstance(row.get("state_history"), list):
                    row["state_history"] = row["state_history"][-20:]
                kept.append(row)
            slim_monitor["tokens"] = kept
        # required by frontend validators; drop bulk only
        slim_monitor["rejections"] = []
        alerts = slim_monitor.get("alerts")
        if isinstance(alerts, list):
            slim_monitor["alerts"] = alerts[:50]
        payload["monitor_v3"] = slim_monitor
    universe = payload.get("meme_watch_universe")
    if isinstance(universe, list) and len(universe) > 80:
        payload["meme_watch_universe"] = universe[:80]
    return payload

def build_cloud_report(report_path: Path, out_dir: Path) -> Path:
    report = read_json(report_path)
    overlay = read_json(out_dir / "alpha-fast-track.json")
    merged = overlay_report(report, overlay, now=utc_now()) if overlay else report
    merged = slim_for_cloudflare(merged if isinstance(merged, dict) else {})
    path = out_dir / ".alpha-cloud-report.json"
    path.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return path


def sync_once(report_path: Path, out_dir: Path, site_url: str, status_path: Path) -> dict:
    started = utc_now()
    try:
        cloud_report = build_cloud_report(report_path, out_dir)
        result = upload_report(cloud_report, site_url, env_value("ALPHA_REPORT_WRITE_TOKEN", ""), status_path)
        status = {**result, "started_at": started, "finished_at": utc_now(), "report": str(report_path)}
    except Exception as exc:  # noqa: BLE001
        status = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "started_at": started, "finished_at": utc_now()}
    write_status(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload the local live report without rerunning the heavy scanner.")
    parser.add_argument("--interval-seconds", type=int, default=20)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--site-url", default=env_value("ALPHA_REPORT_SITE_URL", DEFAULT_SITE_URL))
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        status = sync_once(args.report, args.out_dir, args.site_url, args.status)
        if args.once:
            print(json.dumps(status, ensure_ascii=False))
            return 0 if status.get("ok") else 1
        time.sleep(max(5, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
