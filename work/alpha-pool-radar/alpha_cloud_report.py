#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "outputs" / "alpha-radar-report-latest.json"
DEFAULT_STATUS = ROOT / "outputs" / "alpha-cloud-report-status.json"
DEFAULT_SITE_URL = "https://vercel-site-eta-blush.vercel.app"
ENV_FILES = [ROOT / ".env.local", ROOT / "outputs" / "vercel-site" / ".env.local"]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_status(status_path: Path, payload: dict[str, Any]) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_endpoint(site_url: str) -> str:
    return site_url.rstrip("/") + "/api/report"


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def env_value(name: str, default: str = "") -> str:
    if os.environ.get(name):
        return os.environ[name]
    for env_file in ENV_FILES:
        values = load_env_file(env_file)
        if values.get(name):
            return values[name]
    return default


def http_uploader(url: str, token: str, payload: str) -> dict[str, Any]:
    body = gzip.compress(payload.encode("utf-8"), compresslevel=6)
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Content-Encoding": "gzip",
            "User-Agent": "Mozilla/5.0 AlphaRadarCloudSync/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "step": "upload", "status": exc.code, "error": body}
    except Exception as exc:
        return {"ok": False, "step": "upload", "error": str(exc)}


def upload_report(
    report_path: Path,
    site_url: str,
    token: str,
    status_path: Path,
    *,
    uploader: Callable[[str, str, str], dict[str, Any]] = http_uploader,
) -> dict[str, Any]:
    if not token:
        result = {"ok": False, "step": "config", "error": "missing ALPHA_REPORT_WRITE_TOKEN"}
        write_status(status_path, {**result, "updated_at": now_iso()})
        return result
    try:
        payload = report_path.read_text(encoding="utf-8")
        json.loads(payload)
    except Exception as exc:
        result = {"ok": False, "step": "read_report", "error": str(exc)}
        write_status(status_path, {**result, "updated_at": now_iso()})
        return result

    endpoint = normalize_endpoint(site_url)
    result = uploader(endpoint, token, payload)
    persisted = result.get("persisted") is not False
    status = {
        **result,
        "ok": bool(result.get("ok")) and persisted,
        **({"error": result.get("error") or "report_not_persisted"} if not persisted else {}),
        "updated_at": now_iso(),
        "endpoint": endpoint,
    }
    write_status(status_path, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload the latest Alpha Radar report to the cloud API.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--site-url", default=env_value("ALPHA_REPORT_SITE_URL", DEFAULT_SITE_URL))
    parser.add_argument("--token", default=env_value("ALPHA_REPORT_WRITE_TOKEN", ""))
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = upload_report(args.report, args.site_url, args.token, args.status)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
