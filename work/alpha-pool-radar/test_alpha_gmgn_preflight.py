from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_gmgn_preflight import run_preflight


WALLET = "0x" + "1" * 40


def test_preview_preflight_requires_the_optional_gmgn_runner():
    result = run_preflight(env={
        "GMGN_WALLET_ADDRESS": WALLET,
        "GMGN_BUY_AMOUNT_ATOMIC": "5000000000000000",
        "GMGN_SLIPPAGE_PERCENT": "3",
    })

    assert result["ready_for_preview"] is False
    assert result["ready_for_submission"] is False
    assert result["checks"]["gmgn_runner"]["reason"] == "runner_unavailable"
    assert result["checks"]["submission"]["reason"] == "preview_only"
    assert result["configuration"]["private_key_loaded"] is False


def test_preflight_fails_closed_without_wallet_or_amount():
    result = run_preflight(env={})

    assert result["ready_for_preview"] is False
    assert result["ready_for_submission"] is False
    assert result["checks"]["wallet_address"]["ok"] is False
    assert result["checks"]["buy_amount_atomic"]["ok"] is False
    json.dumps(result)
