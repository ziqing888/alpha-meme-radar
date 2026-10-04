from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_gmgn_execution_adapter import GmgnAdapterError, GmgnExecutionAdapter


TOKEN = "0x1111111111111111111111111111111111111111"
WALLET = "0x2222222222222222222222222222222222222222"


def test_buy_preview_maps_native_bsc_input_and_does_not_submit():
    adapter = GmgnExecutionAdapter(wallet_address=WALLET, runner=("gmgn-cli",))

    result = adapter.buy(TOKEN, "0x3333333333333333333333333333333333333333", "5000000000000000", "3", chain_id=56)

    assert result["status"] == "preview"
    assert result["submitted"] is False
    assert result["command"][:2] == ["gmgn-cli", "swap"]
    assert result["command"][result["command"].index("--input-token") + 1] == "0x0000000000000000000000000000000000000000"
    assert "--yes" not in result["command"]


def test_sell_preview_attaches_current_strategy_exit_orders():
    adapter = GmgnExecutionAdapter(wallet_address=WALLET)

    result = adapter.preview(
        side="sell",
        token_address=TOKEN,
        amount_atomic="1000",
        slippage_percent="auto",
        chain="bsc",
        condition_orders=adapter.strategy_condition_orders(),
    )

    encoded = result["command"][result["command"].index("--condition-orders") + 1]
    conditions = json.loads(encoded)
    assert conditions[0]["price_scale"] == "100"
    assert conditions[0]["sell_ratio"] == "80"
    assert conditions[1]["order_type"] == "loss_stop"
    assert "--auto-slippage" in result["command"]
    assert "--sell-ratio-type" in result["command"]


def test_adapter_rejects_invalid_amount_and_chain():
    adapter = GmgnExecutionAdapter(wallet_address=WALLET)

    with pytest.raises(GmgnAdapterError, match="invalid_amount"):
        adapter.buy(TOKEN, "pool", "0", "3")
    with pytest.raises(GmgnAdapterError, match="unsupported_chain"):
        adapter.buy(TOKEN, "pool", "1", "3", chain="unknown")


def test_adapter_rejects_canonical_arc_monitor_chain_before_building_wallet_command():
    adapter = GmgnExecutionAdapter(wallet_address=WALLET, runner=("gmgn-cli",))

    with pytest.raises(GmgnAdapterError, match="unsupported_chain"):
        adapter.preview(
            side="buy",
            token_address=TOKEN,
            amount_atomic="1",
            slippage_percent="3",
            chain="arc",
            input_token="0x0000000000000000000000000000000000000000",
        )
