#!/usr/bin/env python3
"""Read-only supervisor for BSC first-layer Meme sources."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import winreg  # type: ignore
except Exception:  # noqa: BLE001
    winreg = None

import alpha_bsc_onchain_export as bsc_onchain
import alpha_flap_live_export as flap
import alpha_fourmeme_live_export as fourmeme

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATUS = ROOT / "outputs" / "bsc-realtime-sources-status.json"
ENV_FILES = (
    ROOT / ".env",
    ROOT / ".env.local",
    ROOT / ".env.bitquery",
    ROOT / "work" / "alpha-pool-radar" / ".env",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def dotenv_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in ENV_FILES:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value:
                    values[key] = value
        except OSError:
            continue
    return values


def windows_environment_value(name: str) -> str:
    if os.name != "nt" or winreg is None:
        return ""
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for root_key, sub_key in locations:
        try:
            with winreg.OpenKey(root_key, sub_key) as key:
                value, _kind = winreg.QueryValueEx(key, name)
                if str(value).strip():
                    return str(value).strip()
        except OSError:
            continue
    return ""


def resolve_secret(names: tuple[str, ...], file_values: dict[str, str]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip() or windows_environment_value(name) or file_values.get(name, "").strip()
        if value:
            return value
    return ""


def run_bsc_pair_stream(args: argparse.Namespace, file_values: dict[str, str]) -> dict[str, Any]:
    wss_url = resolve_secret(("BSC_WSS_URL", "BNBCHAIN_WSS_URL", "BSC_PANCAKE_WSS_URL", "QUICKNODE_BSC_WSS_URL"), file_values)
    return bsc_onchain.stream_once(
        wss_url=wss_url,
        factory=os.environ.get("PANCAKE_V2_FACTORY", bsc_onchain.PANCAKE_V2_FACTORY),
        out_path=bsc_onchain.DEFAULT_OUT,
        status_path=bsc_onchain.DEFAULT_STATUS,
        max_rows=args.bsc_max_rows,
        duration_seconds=args.duration_seconds,
        timeout_seconds=args.timeout_seconds,
    )


def run_fourmeme_stream(args: argparse.Namespace, file_values: dict[str, str]) -> dict[str, Any]:
    token = resolve_secret(("BITQUERY_API_KEY", "BITQUERY_TOKEN"), file_values)
    return fourmeme.stream_once(
        token=token,
        out_path=fourmeme.DEFAULT_OUT,
        status_path=fourmeme.DEFAULT_STATUS,
        max_rows=args.launchpad_max_rows,
        duration_seconds=args.duration_seconds,
        timeout_seconds=args.timeout_seconds,
    )


def run_flap_stream(args: argparse.Namespace, file_values: dict[str, str]) -> dict[str, Any]:
    token = resolve_secret(("BITQUERY_API_KEY", "BITQUERY_TOKEN"), file_values)
    return flap.stream_once(
        token=token,
        out_path=flap.DEFAULT_OUT,
        status_path=flap.DEFAULT_STATUS,
        max_rows=args.launchpad_max_rows,
        duration_seconds=args.duration_seconds,
        timeout_seconds=args.timeout_seconds,
    )


def run_cycle(args: argparse.Namespace) -> dict[str, Any]:
    file_values = dotenv_values()
    tasks = {
        "bsc_onchain": run_bsc_pair_stream,
        "fourmeme_launchpad": run_fourmeme_stream,
        "flap_launchpad": run_flap_stream,
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(func, args, file_values): name for name, func in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[name] = {"ok": False, "reason": "supervisor_error", "error": str(exc), "finished_at": utc_now_iso()}
    status = {
        "ok": True,
        "updated_at": utc_now_iso(),
        "sources": results,
    }
    write_json(args.status, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only BSC Meme realtime source supervisor.")
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--duration-seconds", type=int, default=int(os.environ.get("BSC_REALTIME_DURATION_SECONDS", "25")))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("BSC_REALTIME_TIMEOUT_SECONDS", "20")))
    parser.add_argument("--interval-seconds", type=int, default=int(os.environ.get("BSC_REALTIME_INTERVAL_SECONDS", "60")))
    parser.add_argument("--bsc-max-rows", type=int, default=int(os.environ.get("BSC_ONCHAIN_MAX_ROWS", "160")))
    parser.add_argument("--launchpad-max-rows", type=int, default=int(os.environ.get("BSC_LAUNCHPAD_MAX_ROWS", "120")))
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        run_cycle(args)
        if args.once:
            return 0
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
