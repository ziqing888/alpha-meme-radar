#!/usr/bin/env python3
"""Read-only Bitquery HTTP poller for BSC launchpad first-layer sources."""

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

try:
    import winreg  # type: ignore
except Exception:  # noqa: BLE001
    winreg = None

import alpha_flap_live_export as flap
import alpha_fourmeme_live_export as fourmeme

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATUS = ROOT / "outputs" / "bsc-launchpad-poll-status.json"
DEFAULT_HTTP_URL = "https://streaming.bitquery.io/graphql"
DEFAULT_LIMIT = 40
DEFAULT_INTERVAL_SECONDS = 60
ENV_FILES = (
    ROOT / ".env",
    ROOT / ".env.local",
    ROOT / ".env.bitquery",
    ROOT / "work" / "alpha-pool-radar" / ".env",
)

EVENT_FIELDS = """
Log { Signature { Name Signature } }
Arguments {
  Name
  Type
  Value {
    ... on EVM_ABI_Address_Value_Arg { address }
    ... on EVM_ABI_String_Value_Arg { string }
    ... on EVM_ABI_BigInt_Value_Arg { bigInteger }
    ... on EVM_ABI_Integer_Value_Arg { integer }
    ... on EVM_ABI_Bytes_Value_Arg { hex }
    ... on EVM_ABI_Boolean_Value_Arg { bool }
  }
}
Transaction { Hash To From }
Block { Time Number }
""".strip()


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


def resolve_token() -> str:
    file_values = dotenv_values()
    for name in ("BITQUERY_API_KEY", "BITQUERY_TOKEN"):
        value = os.environ.get(name, "").strip() or windows_environment_value(name) or file_values.get(name, "").strip()
        if value:
            return value
    return ""


def parse_block_time_seconds(value: Any) -> int:
    if not value:
        return 0
    text = str(value).strip()
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return 0


def attach_creation_timestamp(row: dict[str, Any]) -> dict[str, Any]:
    created = parse_block_time_seconds(row.get("first_seen_at") or row.get("launch_time"))
    if created > 0:
        row["creation_timestamp"] = created
        row["open_timestamp"] = created
        row["pool_created_at"] = created
    return row


def bitquery_post(query: str, *, token: str, url: str, timeout_seconds: int) -> dict[str, Any]:
    body = json.dumps({"query": query}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"bitquery_http_{exc.code}: {detail}") from exc


def fourmeme_query(limit: int) -> str:
    return f"""
query {{
  EVM(network: bsc) {{
    Events(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
      where: {{
        LogHeader: {{Address: {{is: "{fourmeme.FOURMEME_PROXY}"}}}}
        Log: {{Signature: {{Name: {{in: ["TokenCreate", "TokenPurchase"]}}}}}}
      }}
    ) {{
      {EVENT_FIELDS}
    }}
  }}
}}
""".strip()


def flap_query(limit: int) -> str:
    return f"""
query {{
  EVM(network: bsc) {{
    Events(
      limit: {{count: {limit}}}
      orderBy: {{descending: Block_Time}}
      where: {{
        LogHeader: {{Address: {{is: "{flap.FLAP_PORTAL_CONTRACT}"}}}}
        Log: {{Signature: {{Name: {{in: ["TokenCreated", "TokenCurveSetV2", "TokenQuoteSet", "TokenMigrated"]}}}}}}
      }}
    ) {{
      {EVENT_FIELDS}
    }}
  }}
}}
""".strip()


def response_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(json.dumps(errors, ensure_ascii=False))
    evm = ((payload.get("data") or {}).get("EVM") or {}) if isinstance(payload.get("data"), dict) else {}
    return [event for event in evm.get("Events") or [] if isinstance(event, dict)]


def poll_fourmeme(*, token: str, url: str, limit: int, timeout_seconds: int) -> dict[str, Any]:
    snapshot = fourmeme.FourMemeSnapshot(limit)
    payload = bitquery_post(fourmeme_query(limit), token=token, url=url, timeout_seconds=timeout_seconds)
    raw_events = response_events(payload)
    for event in raw_events:
        row = fourmeme.normalize_token_create_event(event)
        if row:
            snapshot.add(attach_creation_timestamp(row))
    out_payload = snapshot.payload()
    out_payload["poll_mode"] = "http"
    out_payload["raw_events"] = len(raw_events)
    write_json(fourmeme.DEFAULT_OUT, out_payload)
    status = {
        "ok": True,
        "reason": "",
        "mode": "http",
        "finished_at": utc_now_iso(),
        "out": str(fourmeme.DEFAULT_OUT),
        "rows": out_payload["count"],
        "raw_events": len(raw_events),
        "total_events": out_payload["total_events"],
    }
    write_json(fourmeme.DEFAULT_STATUS, status)
    return status


def poll_flap(*, token: str, url: str, limit: int, timeout_seconds: int) -> dict[str, Any]:
    snapshot = flap.FlapSnapshot(limit)
    payload = bitquery_post(flap_query(limit), token=token, url=url, timeout_seconds=timeout_seconds)
    raw_events = response_events(payload)
    for event in raw_events:
        row = flap.normalize_token_created_event(event)
        if row:
            snapshot.add(attach_creation_timestamp(row))
    out_payload = snapshot.payload()
    out_payload["poll_mode"] = "http"
    out_payload["raw_events"] = len(raw_events)
    write_json(flap.DEFAULT_OUT, out_payload)
    status = {
        "ok": True,
        "reason": "",
        "mode": "http",
        "finished_at": utc_now_iso(),
        "out": str(flap.DEFAULT_OUT),
        "rows": out_payload["count"],
        "raw_events": len(raw_events),
        "total_events": out_payload["total_events"],
    }
    write_json(flap.DEFAULT_STATUS, status)
    return status


def missing_token_status(status_path: Path) -> dict[str, Any]:
    status = {
        "ok": False,
        "reason": "missing_bitquery_token",
        "mode": "http",
        "finished_at": utc_now_iso(),
        "sources": {},
    }
    write_json(status_path, status)
    return status


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    token = args.token or resolve_token()
    if not token:
        return missing_token_status(args.status)
    results: dict[str, Any] = {}
    for source, func in (("fourmeme_launchpad", poll_fourmeme), ("flap_launchpad", poll_flap)):
        try:
            results[source] = func(token=token, url=args.url, limit=args.limit, timeout_seconds=args.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            results[source] = {
                "ok": False,
                "reason": "poll_error",
                "mode": "http",
                "error": str(exc),
                "finished_at": utc_now_iso(),
            }
    status = {
        "ok": all(bool(item.get("ok")) for item in results.values()),
        "reason": "" if all(bool(item.get("ok")) for item in results.values()) else "partial_poll_error",
        "mode": "http",
        "updated_at": utc_now_iso(),
        "sources": results,
    }
    write_json(args.status, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only BSC launchpad HTTP poller.")
    parser.add_argument("--token", default="")
    parser.add_argument("--url", default=os.environ.get("BITQUERY_GRAPHQL_HTTP_URL", DEFAULT_HTTP_URL))
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--limit", type=int, default=int(os.environ.get("BSC_LAUNCHPAD_POLL_LIMIT", DEFAULT_LIMIT)))
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("BSC_LAUNCHPAD_POLL_TIMEOUT_SECONDS", "25")))
    parser.add_argument("--interval-seconds", type=int, default=int(os.environ.get("BSC_LAUNCHPAD_POLL_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)))
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    while True:
        run_once(args)
        if args.once:
            return 0
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
