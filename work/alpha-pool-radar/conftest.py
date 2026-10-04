"""Keep the local test process isolated from active live workers."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest


STATUS_FILES = {
    "bsc": "okx-dex-sdk-live-status.json",
    "robinhood": "okx-dex-sdk-robinhood-live-status.json",
}


def _fresh_live_workers(outputs: Path) -> list[str]:
    now = datetime.now(timezone.utc)
    running = []
    for chain, name in STATUS_FILES.items():
        try:
            payload = json.loads((outputs / name).read_text(encoding="utf-8-sig"))
            updated = datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
            age = (now - updated.astimezone(timezone.utc)).total_seconds()
            control = payload.get("terminal_control") or {}
            if (
                payload.get("daemon_running") is True
                and 0 <= age <= 60
                and control.get("supported") is True
                and isinstance(control.get("pid"), int)
            ):
                running.append(chain)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return running


def pytest_sessionstart(session):
    if os.environ.get("ALPHA_TEST_ISOLATION_CONFIRMED") == "1":
        return
    outputs = Path(__file__).resolve().parents[2] / "outputs"
    running = _fresh_live_workers(outputs)
    if running:
        chains = ", ".join(running)
        raise pytest.UsageError(
            f"live workers active ({chains}); stop them before tests or run in an isolated "
            "checkout with ALPHA_TEST_ISOLATION_CONFIRMED=1"
        )
