#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_radar_pipeline import (
    ERROR_BUILD_SITE,
    ERROR_DEPLOY_VERCEL,
    ERROR_REFRESH_DATA,
    ERROR_SITE_INCOMPLETE,
    STEP_BUILD_SITE,
    STEP_DEPLOY_VERCEL,
    STEP_REFRESH_DATA,
    build_command,
    default_pipeline_config,
    deploy_command,
    report_command,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = ROOT / "outputs"
SITE_DIR = OUT_DIR / "vercel-site"
STATUS_PATH = OUT_DIR / "alpha-radar-vercel-update-status.json"
REPORT_PATH = OUT_DIR / "alpha-radar-report-latest.json"
GOLD_STATE_PATH = OUT_DIR / "alpha-gold-watch-state.json"
DEFAULT_DEPLOY_MIN_INTERVAL_SECONDS = 15 * 60


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run_step(name: str, command: list[str], cwd: Path) -> dict[str, Any]:
    started = now_iso()
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"},
        **hidden_subprocess_kwargs(),
    )
    return {
        "name": name,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "started_at": started,
        "finished_at": now_iso(),
        "command": command,
        "output_tail": proc.stdout[-5000:],
    }


def write_status(status: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def alert_count(path: Path) -> int:
    payload = read_json(path)
    alerts = payload.get("alerts") or []
    return len(alerts) if isinstance(alerts, list) else 0


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_since(value: Any) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()


def should_skip_deploy(
    previous_status: dict[str, Any],
    *,
    deploy_min_interval_seconds: int,
    before_alert_count: int,
    after_alert_count: int,
) -> tuple[bool, str]:
    if deploy_min_interval_seconds <= 0:
        return False, ""
    if after_alert_count > before_alert_count:
        return False, ""
    last_deployed_at = previous_status.get("last_deployed_at")
    elapsed = seconds_since(last_deployed_at)
    if elapsed is None:
        return False, ""
    if elapsed < deploy_min_interval_seconds:
        remaining = int(deploy_min_interval_seconds - elapsed)
        return True, f"距离上次部署太近，且没有新金狗票，跳过本轮部署；约 {remaining} 秒后再发版"
    return False, ""


def vercel_deploy_command() -> list[str]:
    vercel = shutil.which("vercel") or shutil.which("vercel.cmd")
    if vercel:
        return [vercel, "--yes", "--prod"]
    npx = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
    return deploy_command(npx)


def required_site_files() -> list[Path]:
    return [SITE_DIR / "index.html", SITE_DIR / "report.json", SITE_DIR / "vercel.json"]


def update_site(deploy: bool, *, deploy_min_interval_seconds: int = DEFAULT_DEPLOY_MIN_INTERVAL_SECONDS) -> int:
    config = default_pipeline_config()
    steps: list[dict[str, Any]] = []
    previous_status = read_json(STATUS_PATH)
    before_alert_count = alert_count(GOLD_STATE_PATH)
    status: dict[str, Any] = {"ok": False, "updated_at": now_iso(), "deploy": deploy, "steps": steps}

    steps.append(run_step(STEP_REFRESH_DATA, report_command(sys.executable, SCRIPT_DIR, OUT_DIR, config), ROOT))
    if not steps[-1]["ok"]:
        status["error"] = ERROR_REFRESH_DATA
        write_status(status)
        return 1

    steps.append(run_step(STEP_BUILD_SITE, build_command(sys.executable, SCRIPT_DIR, REPORT_PATH, SITE_DIR), ROOT))
    if not steps[-1]["ok"]:
        status["error"] = ERROR_BUILD_SITE
        write_status(status)
        return 1

    missing = [str(path) for path in required_site_files() if not path.exists() or path.stat().st_size <= 0]
    if missing:
        status["error"] = ERROR_SITE_INCOMPLETE
        status["missing"] = missing
        write_status(status)
        return 1

    if deploy:
        after_alert_count = alert_count(GOLD_STATE_PATH)
        skip_deploy, skip_reason = should_skip_deploy(
            previous_status,
            deploy_min_interval_seconds=deploy_min_interval_seconds,
            before_alert_count=before_alert_count,
            after_alert_count=after_alert_count,
        )
        if skip_deploy:
            status["deploy_skipped"] = True
            status["deploy_skip_reason"] = skip_reason
            status["last_deployed_at"] = previous_status.get("last_deployed_at")
        else:
            steps.append(run_step(STEP_DEPLOY_VERCEL, vercel_deploy_command(), SITE_DIR))
            if not steps[-1]["ok"]:
                status["error"] = ERROR_DEPLOY_VERCEL
                write_status(status)
                return 1
            status["last_deployed_at"] = now_iso()

    status["ok"] = True
    status["updated_at"] = now_iso()
    status["site_dir"] = str(SITE_DIR)
    status["report"] = str(REPORT_PATH)
    status["url"] = config.site_url
    write_status(status)
    print(json.dumps({"ok": True, "url": config.site_url, "updated_at": status["updated_at"]}, ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="\u5237\u65b0 Alpha \u5996\u5e01\u96f7\u8fbe\u7f51\u9875\uff0c\u5e76\u53ef\u90e8\u7f72\u5230 Vercel\u3002")
    parser.add_argument("--deploy", action="store_true", help="\u90e8\u7f72\u5230\u5f53\u524d Vercel \u9879\u76ee\u3002")
    parser.add_argument(
        "--deploy-min-interval-seconds",
        type=int,
        default=DEFAULT_DEPLOY_MIN_INTERVAL_SECONDS,
        help="\u6ca1\u6709\u65b0\u91d1\u72d7\u7968\u65f6\uff0c\u4e24\u6b21 Vercel \u90e8\u7f72\u7684\u6700\u5c0f\u95f4\u9694\u3002\u8bbe\u4e3a 0 \u8868\u793a\u6bcf\u6b21\u90fd\u90e8\u7f72\u3002",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return update_site(deploy=args.deploy, deploy_min_interval_seconds=args.deploy_min_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
