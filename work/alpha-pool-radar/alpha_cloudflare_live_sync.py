#!/usr/bin/env python3
"""Push local live monitor-v3 (+ light report) to Cloudflare Pages with low latency."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_cloud_report import DEFAULT_SITE_URL, env_value, write_status
from alpha_cloud_sync import _public_safe, slim_for_cloudflare
from alpha_fast_track import overlay_report, read_json, utc_now

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "outputs"
DEFAULT_REPORT = DEFAULT_OUT_DIR / "alpha-radar-report-latest.json"
DEFAULT_STATUS = DEFAULT_OUT_DIR / "alpha-cloudflare-live-sync-status.json"
DEFAULT_MONITOR_URL = "http://127.0.0.1:8765/monitor-v3.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slim_monitor_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    monitor = _public_safe(payload.get("monitor_v3"))
    if not isinstance(monitor, dict):
        return {"monitor_v3": {"schema_version": 3, "observed_at": now_iso(), "monitor_status": "unknown", "source_health": {}, "tokens": [], "rejections": [], "alerts": []}}
    tokens = []
    for token in (monitor.get("tokens") or [])[:120]:
        if not isinstance(token, dict):
            continue
        row = dict(token)
        if isinstance(row.get("events"), list):
            row["events"] = row["events"][-20:]
        if isinstance(row.get("state_history"), list):
            row["state_history"] = row["state_history"][-20:]
        tokens.append(row)
    slim = {
        **{k: v for k, v in monitor.items() if k not in {"tokens", "rejections", "alerts"}},
        "tokens": tokens,
        "rejections": [],
        "alerts": (monitor.get("alerts") or [])[:50],
    }
    return {"monitor_v3": slim}


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(request, timeout=8) as response:
        body = response.read()
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("monitor_not_object")
    return payload


def post_gzip(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), compresslevel=3)
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Content-Encoding": "gzip",
            "Accept": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {"ok": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": detail}


def sync_monitor(site_url: str, token: str, monitor_url: str) -> dict[str, Any]:
    local = fetch_json(monitor_url)
    envelope = slim_monitor_envelope(local)
    result = post_gzip(site_url.rstrip("/") + "/api/monitor-v3", token, envelope)
    tokens = envelope["monitor_v3"].get("tokens") or []
    return {
        **result,
        "monitor_tokens": len(tokens) if isinstance(tokens, list) else 0,
        "observed_at": envelope["monitor_v3"].get("observed_at"),
        "monitor_status": envelope["monitor_v3"].get("monitor_status"),
    }


def sync_report(site_url: str, token: str, report_path: Path, out_dir: Path) -> dict[str, Any]:
    report = read_json(report_path)
    overlay = read_json(out_dir / "alpha-fast-track.json")
    merged = overlay_report(report, overlay, now=utc_now()) if overlay else report
    if not isinstance(merged, dict):
        merged = {}
    # Prefer freshest monitor from local if present on disk sync path already handled separately.
    merged = slim_for_cloudflare(merged)
    result = post_gzip(site_url.rstrip("/") + "/api/report", token, merged)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--report-every", type=int, default=0, help="Push report every N monitor cycles; 0 disables report push")
    parser.add_argument("--monitor-url", default=DEFAULT_MONITOR_URL)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--site-url", default=env_value("ALPHA_REPORT_SITE_URL", DEFAULT_SITE_URL))
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = env_value("ALPHA_REPORT_WRITE_TOKEN", "")
    if not token:
        write_status(args.status, {"ok": False, "error": "missing ALPHA_REPORT_WRITE_TOKEN", "updated_at": now_iso()})
        print(json.dumps({"ok": False, "error": "missing ALPHA_REPORT_WRITE_TOKEN"}))
        return 1
    cycle = 0
    while True:
        started = now_iso()
        status: dict[str, Any] = {"started_at": started, "site_url": args.site_url}
        try:
            monitor_result = sync_monitor(args.site_url, token, args.monitor_url)
            status["monitor"] = monitor_result
            if args.report_every > 0 and cycle % args.report_every == 0:
                status["report"] = sync_report(args.site_url, token, args.report, args.out_dir)
            status["ok"] = bool(monitor_result.get("ok"))
            status["finished_at"] = now_iso()
        except Exception as exc:  # noqa: BLE001
            status = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "started_at": started,
                "finished_at": now_iso(),
            }
        write_status(args.status, status)
        print(json.dumps(status, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if status.get("ok") else 1
        cycle += 1
        time.sleep(max(1.0, float(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
