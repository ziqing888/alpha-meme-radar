"""Export strategy-approved candidates as GMGN execution intents.

The bridge is intentionally side-effect free. It creates auditable commands
for the GMGN CLI but does not invoke them or load a private key.
"""

from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from alpha_gmgn_execution_adapter import GmgnAdapterError, GmgnExecutionAdapter


SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _idempotency_key(row: Mapping[str, Any], token: str, signal_at: str) -> str:
    """Keep a signal stable across refreshes so one candidate cannot fan out."""
    raw = "|".join([
        str(row.get("chain") or "bsc").strip().lower(),
        token.strip().lower(),
        str(row.get("pool_address") or row.get("pair_address") or "").strip().lower(),
        signal_at.strip(),
        str(row.get("execution_arm") or "").strip().lower(),
    ])
    return "gmgn:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _configuration(values: Mapping[str, str], adapter: GmgnExecutionAdapter | None) -> dict[str, Any]:
    """Expose only non-secret readiness facts for the UI and operator logs."""
    amount = str(values.get("GMGN_BUY_AMOUNT_ATOMIC") or values.get("BSC_LIVE_BUY_AMOUNT_ATOMIC") or "").strip()
    slippage = str(values.get("GMGN_SLIPPAGE_PERCENT") or values.get("BSC_LIVE_SLIPPAGE_PERCENT") or "").strip()
    return {
        "wallet_address_configured": bool(str(values.get("GMGN_WALLET_ADDRESS") or values.get("BSC_WALLET_ADDRESS") or "").strip()),
        "runner_available": bool(adapter and adapter.runner),
        "buy_amount_configured": bool(amount),
        "slippage_configured": bool(slippage),
        "submission_enabled": False,
        "private_key_loaded": False,
    }


def export_intents(input_path: Path, output_path: Path, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = os.environ if env is None else env
    payload = _read(Path(input_path))
    signals = payload.get("signals") if isinstance(payload.get("signals"), list) else []
    wallet = str(values.get("GMGN_WALLET_ADDRESS") or values.get("BSC_WALLET_ADDRESS") or "").strip()
    default_amount = str(values.get("GMGN_BUY_AMOUNT_ATOMIC") or values.get("BSC_LIVE_BUY_AMOUNT_ATOMIC") or "").strip()
    default_slippage = str(values.get("GMGN_SLIPPAGE_PERCENT") or values.get("BSC_LIVE_SLIPPAGE_PERCENT") or "auto").strip()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "mode": "preview",
        "provider": "gmgn",
        "submitted": False,
        "count": 0,
        "intents": [],
        "errors": [],
    }
    if not wallet:
        result["status"] = "awaiting_local_wallet_address"
        result["configuration"] = _configuration(values, None)
        _write(Path(output_path), result)
        return result
    try:
        adapter = GmgnExecutionAdapter.from_env({**dict(values), "GMGN_WALLET_ADDRESS": wallet})
    except GmgnAdapterError as exc:
        result["status"] = str(exc)
        result["configuration"] = _configuration(values, None)
        _write(Path(output_path), result)
        return result
    result["configuration"] = _configuration(values, adapter)

    for row in signals:
        if not isinstance(row, dict):
            continue
        chain = str(row.get("chain") or "").strip().lower()
        if chain != "bsc":
            result["errors"].append({"reason": "unsupported_execution_chain", "chain": chain or "missing"})
            continue
        token = str(row.get("contract_address") or row.get("token_address") or row.get("address") or "").strip()
        amount = str(row.get("amount_atomic") or default_amount).strip()
        slippage = str(row.get("slippage_percent") or default_slippage).strip()
        try:
            intent = adapter.buy(
                token,
                str(row.get("pool_address") or row.get("pair_address") or ""),
                amount,
                slippage,
                chain_id=56,
                condition_orders=adapter.strategy_condition_orders(),
            )
        except GmgnAdapterError as exc:
            result["errors"].append({"reason": str(exc), "token_address": token.lower() if token.lower().startswith("0x") else token})
            continue
        intent.update({
            "key": f"bsc:{token.lower()}",
            "idempotency_key": _idempotency_key(row, token, str(row.get("signal_at") or row.get("quote_at") or "")),
            "symbol": row.get("symbol"),
            "signal_at": row.get("signal_at"),
            "quote_at": row.get("quote_at"),
            "execution_arm": row.get("execution_arm"),
            "entry_score": row.get("entry_score") or row.get("execution_candidate_score"),
            "route_label": row.get("route_label"),
            "candidate_reason": row.get("execution_candidate_reason") or row.get("entry_reason") or [],
        })
        result["intents"].append(intent)
    result["count"] = len(result["intents"])
    result["status"] = "ready_for_operator_review" if result["intents"] else ("no_candidates" if not result["errors"] else "candidate_errors")
    _write(Path(output_path), result)
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("outputs/bsc-execution-input.json"))
    parser.add_argument("--output", type=Path, default=Path("outputs/gmgn-execution-intents.json"))
    args = parser.parse_args()
    print(json.dumps(export_intents(args.input, args.output), ensure_ascii=False, sort_keys=True))
