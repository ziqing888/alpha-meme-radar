#!/usr/bin/env python3
"""Authenticated SSH stdio bridge for one chain's small execution payloads."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys


ROOT = Path("/var/lib/alpha-radar/outputs")
CHAINS = {
    "bsc": "okx-dex-sdk-live",
    "robinhood": "okx-dex-sdk-robinhood-live",
}


def encoded(path: Path, maximum: int) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if not 0 < len(data) <= maximum:
        raise ValueError(f"invalid_remote_file_size:{path.name}")
    json.loads(data)
    return base64.b64encode(data).decode("ascii")


def write_input(chain: str, text: str) -> None:
    data = base64.b64decode(text, validate=True)
    if not 0 < len(data) <= 2 * 1024 * 1024:
        raise ValueError("invalid_input_size")
    payload = json.loads(data)
    if not isinstance(payload, dict) or not isinstance(payload.get("signals"), list) or not isinstance(payload.get("quotes"), list):
        raise ValueError("invalid_input_schema")
    destination = ROOT / f"{chain}-execution-input.json"
    temporary = ROOT / f".{destination.name}.{os.getpid()}.tmp"
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, 0o640)
        os.chown(temporary, 0, os.stat(ROOT).st_gid)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def respond(value: dict) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    chain = sys.argv[1].lower() if len(sys.argv) == 2 else ""
    if chain not in CHAINS:
        return 64
    stem = CHAINS[chain]
    ROOT.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        try:
            if len(line) > 3 * 1024 * 1024:
                raise ValueError("request_too_large")
            request = json.loads(line)
            if set(request) != {"chain", "input_b64"} or request.get("chain") != chain:
                raise ValueError("invalid_request")
            write_input(chain, request["input_b64"])
            respond({
                "ok": True,
                "chain": chain,
                "state_b64": encoded(ROOT / f"{stem}-state.json", 8 * 1024 * 1024),
                "status_b64": encoded(ROOT / f"{stem}-status.json", 2 * 1024 * 1024),
            })
        except Exception as exc:
            respond({"ok": False, "chain": chain, "error": type(exc).__name__})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
