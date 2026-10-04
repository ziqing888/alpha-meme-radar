#!/usr/bin/env python3
"""Keep one persistent SSH channel between local signals and remote execution."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[3]
OUTPUTS = REPO / "outputs"
CHAINS = {
    "bsc": "okx-dex-sdk-live",
    "robinhood": "okx-dex-sdk-robinhood-live",
}


def lock_process(chain: str):
    path = OUTPUTS / f".remote-{chain}-sync.lock"
    handle = path.open("a+b")
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        handle.close()
        return None
    return handle


def atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def decode_json(value: str | None, maximum: int) -> bytes | None:
    if value is None:
        return None
    data = base64.b64decode(value, validate=True)
    if not 0 < len(data) <= maximum:
        raise ValueError("invalid_remote_payload_size")
    json.loads(data)
    return data


def write_status(path: Path, *, ok: bool, chain: str, host: str, latency_ms: float | None, error: str = "") -> None:
    atomic_write(path, (json.dumps({
        "ok": ok,
        "chain": chain,
        "remote_host": host,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "error": error,
    }, indent=2) + "\n").encode())


def launch(args: argparse.Namespace, error_log):
    return subprocess.Popen([
        "ssh", "-i", str(args.identity_file), "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15", "-o", "ConnectionAttempts=2",
        "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=2",
        f"{args.remote_user}@{args.remote_host}",
        "/opt/alpha-radar/bin/alpha-sync-bridge", args.chain,
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log,
       text=True, encoding="utf-8", errors="strict", bufsize=1,
       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def cycle(process, args: argparse.Namespace) -> float:
    source = OUTPUTS / f"{args.chain}-execution-input.json"
    data = source.read_bytes()
    payload = json.loads(data)
    if not 0 < len(data) <= 2 * 1024 * 1024 or not isinstance(payload, dict):
        raise ValueError("invalid_local_input")
    request = json.dumps({"chain": args.chain, "input_b64": base64.b64encode(data).decode("ascii")}, separators=(",", ":"))
    started = time.monotonic()
    process.stdin.write(request + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise ConnectionError(f"remote_bridge_closed:{process.poll()}")
    response = json.loads(line)
    if response.get("ok") is not True or response.get("chain") != args.chain:
        raise RuntimeError(response.get("error") or "remote_bridge_rejected")
    stem = CHAINS[args.chain]
    state = decode_json(response.get("state_b64"), 8 * 1024 * 1024)
    status = decode_json(response.get("status_b64"), 2 * 1024 * 1024)
    if state is not None:
        atomic_write(OUTPUTS / f"{stem}-state.json", state)
    if status is not None:
        atomic_write(OUTPUTS / f"{stem}-status.json", status)
    return (time.monotonic() - started) * 1000


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", choices=CHAINS, default="robinhood")
    parser.add_argument("--remote-host", required=True)
    parser.add_argument("--remote-user", default="alpha-radar")
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=3)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval_seconds < 1 or not args.identity_file.is_file():
        return 64
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    process_lock = lock_process(args.chain)
    if process_lock is None:
        return 0
    status_path = OUTPUTS / f"remote-{args.chain}-sync-status.json"
    error_path = OUTPUTS / f"remote-{args.chain}-sync.stderr.log"
    with process_lock, error_path.open("ab", buffering=0) as error_log:
        while True:
            process = launch(args, error_log)
            try:
                while True:
                    started = time.monotonic()
                    latency = cycle(process, args)
                    write_status(status_path, ok=True, chain=args.chain, host=args.remote_host, latency_ms=latency)
                    if args.once:
                        return 0
                    time.sleep(max(0.1, args.interval_seconds - (time.monotonic() - started)))
            except Exception as exc:
                write_status(status_path, ok=False, chain=args.chain, host=args.remote_host,
                             latency_ms=None, error=f"{type(exc).__name__}:{exc}"[:300])
                if args.once:
                    return 1
                time.sleep(2)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
