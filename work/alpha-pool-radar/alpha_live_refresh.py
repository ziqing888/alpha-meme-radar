#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import alpha_cloud_report


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
STATUS_PATH = ROOT / "outputs" / "alpha-live-refresh-status.json"
REPORT_PATH = ROOT / "outputs" / "alpha-radar-report-latest.json"
CLOUD_STATUS_PATH = ROOT / "outputs" / "alpha-cloud-report-status.json"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_SITE_URL = "https://vercel-site-eta-blush.vercel.app"
ALLOW_CLOUD_UPLOAD_ENV = "ALPHA_ALLOW_CLOUD_UPLOAD"


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def refresh_command(python_exe: str, script_dir: Path, *, deploy: bool) -> list[str]:
    if deploy:
        return [python_exe, str(script_dir / "update_vercel_site.py"), "--deploy"]
    return [python_exe, str(script_dir / "alpha_radar_report.py"), "--out-dir", str(ROOT / "outputs")]


def subprocess_runner(command: list[str], cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "ALPHA_MEME_CACHE_ONLY": "1",
            "ALPHA_MEME_CHAINS": os.environ.get("ALPHA_MEME_CHAINS", "*"),
        },
        **hidden_subprocess_kwargs(),
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output_tail": proc.stdout[-5000:],
    }


def write_status(status_path: Path, payload: dict[str, Any]) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_refresh_once(
    command: list[str],
    cwd: Path,
    status_path: Path,
    *,
    runner: Callable[[list[str], Path], dict[str, Any]] = subprocess_runner,
    cloud_upload: bool = False,
    report_path: Path = REPORT_PATH,
    cloud_site_url: str = DEFAULT_SITE_URL,
    cloud_token: str = "",
    cloud_status_path: Path = CLOUD_STATUS_PATH,
    cloud_uploader: Callable[[Path, str, str, Path], dict[str, Any]] = alpha_cloud_report.upload_report,
) -> dict[str, Any]:
    started_at = now_iso()
    result = runner(command, cwd)
    cloud_result = None
    cloud_upload_allowed = env_flag(ALLOW_CLOUD_UPLOAD_ENV)
    if cloud_upload and cloud_upload_allowed and result.get("ok"):
        if not cloud_token:
            cloud_token = alpha_cloud_report.env_value("ALPHA_REPORT_WRITE_TOKEN", "")
        cloud_result = cloud_uploader(report_path, cloud_site_url, cloud_token, cloud_status_path)
    status = {
        "ok": bool(result.get("ok")),
        "started_at": started_at,
        "finished_at": now_iso(),
        "command": command,
        "cwd": str(cwd),
        "returncode": result.get("returncode"),
        "output_tail": result.get("output_tail", ""),
    }
    if cloud_result is not None:
        status["cloud_upload"] = cloud_result
    write_status(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Continuously refresh local Alpha Radar data for the live web frontend.")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--deploy", action="store_true", help="Use the legacy site updater and deploy to Vercel.")
    parser.add_argument("--cloud-upload", action="store_true", help="Upload the refreshed report to the cloud /api/report endpoint.")
    parser.add_argument("--cloud-site-url", default=os.environ.get("ALPHA_REPORT_SITE_URL", DEFAULT_SITE_URL))
    parser.add_argument("--cloud-token", default=os.environ.get("ALPHA_REPORT_WRITE_TOKEN", ""))
    parser.add_argument("--cloud-status", type=Path, default=CLOUD_STATUS_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--once", action="store_true", help="Run one refresh and exit.")
    parser.add_argument("--status", type=Path, default=STATUS_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = refresh_command(sys.executable, SCRIPT_DIR, deploy=args.deploy)
    while True:
        status = run_refresh_once(
            command,
            ROOT,
            args.status,
            cloud_upload=args.cloud_upload,
            report_path=args.report,
            cloud_site_url=args.cloud_site_url,
            cloud_token=args.cloud_token,
            cloud_status_path=args.cloud_status,
        )
        if args.once:
            return 0 if status.get("ok") else 1
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
