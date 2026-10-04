"""Offline GMGN preview-path preflight.

This checks the local handoff configuration without loading a private key or
calling the GMGN API. It intentionally cannot report the system as live-ready.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from alpha_gmgn_execution_adapter import GmgnAdapterError, GmgnExecutionAdapter


def _text(values: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = values.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _check(ok: bool, reason: str | None = None) -> dict[str, Any]:
    return {"ok": bool(ok), **({"reason": reason} if not ok else {})}


def _amount_check(value: str) -> dict[str, Any]:
    return _check(value.isdigit() and int(value) > 0, "missing_or_invalid")


def _slippage_check(value: str) -> dict[str, Any]:
    if not value:
        return _check(False, "missing")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return _check(False, "invalid")
    return _check(0 <= parsed <= 50, "out_of_range")


def run_preflight(env: Mapping[str, Any] | None = None) -> dict[str, Any]:
    values = os.environ if env is None else env
    wallet = _text(values, "GMGN_WALLET_ADDRESS", "BSC_WALLET_ADDRESS")
    amount = _text(values, "GMGN_BUY_AMOUNT_ATOMIC", "BSC_LIVE_BUY_AMOUNT_ATOMIC")
    slippage = _text(values, "GMGN_SLIPPAGE_PERCENT", "BSC_LIVE_SLIPPAGE_PERCENT")
    try:
        adapter = GmgnExecutionAdapter.from_env({**dict(values), "GMGN_WALLET_ADDRESS": wallet}) if wallet else None
        wallet_check = _check(bool(adapter), "missing_or_invalid")
        runner_check = _check(bool(adapter and adapter.runner), "runner_unavailable")
    except GmgnAdapterError as exc:
        adapter = None
        wallet_check = _check(False, str(exc))
        runner_check = _check(False, "adapter_unavailable")
    checks = {
        "wallet_address": wallet_check,
        "gmgn_runner": runner_check,
        "buy_amount_atomic": _amount_check(amount),
        "slippage_percent": _slippage_check(slippage),
        "submission": _check(False, "preview_only"),
    }
    return {
        "ready_for_preview": all(item["ok"] for name, item in checks.items() if name != "submission"),
        "ready_for_submission": False,
        "checks": checks,
        "configuration": {
            "wallet_address_configured": bool(wallet),
            "runner_available": bool(adapter and adapter.runner),
            "buy_amount_configured": bool(amount),
            "slippage_configured": bool(slippage),
            "submission_enabled": False,
            "private_key_loaded": False,
        },
    }


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.parse_args(argv)
    result = run_preflight(env=env)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["ready_for_preview"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
