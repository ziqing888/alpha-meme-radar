from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_gmgn_execution_bridge import export_intents


TOKEN = "0x1111111111111111111111111111111111111111"
WALLET = "0x2222222222222222222222222222222222222222"


def test_bridge_exports_strategy_candidate_without_submitting(tmp_path: Path):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "gmgn.json"
    input_path.write_text(json.dumps({"signals": [{
        "chain": "bsc", "contract_address": TOKEN, "pool_address": "0x3333333333333333333333333333333333333333",
        "symbol": "DOG", "execution_arm": "first_discovery", "entry_score": 78,
    }]}), encoding="utf-8")

    result = export_intents(input_path, output_path, env={
        "GMGN_WALLET_ADDRESS": WALLET,
        "GMGN_BUY_AMOUNT_ATOMIC": "5000000000000000",
        "GMGN_SLIPPAGE_PERCENT": "3",
        "GMGN_CLI_PATH": "gmgn-cli",
    })

    assert result["status"] == "ready_for_operator_review"
    assert result["submitted"] is False
    assert result["count"] == 1
    assert "--condition-orders" in result["intents"][0]["command"]
    assert json.loads(output_path.read_text(encoding="utf-8"))["count"] == 1


def test_bridge_waits_for_wallet_without_generating_trade_intent(tmp_path: Path):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "gmgn.json"
    input_path.write_text(json.dumps({"signals": [{"chain": "bsc", "contract_address": TOKEN}]}), encoding="utf-8")

    result = export_intents(input_path, output_path, env={})

    assert result["status"] == "awaiting_local_wallet_address"
    assert result["intents"] == []
    assert result["submitted"] is False
