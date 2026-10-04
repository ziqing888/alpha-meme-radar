from __future__ import annotations

import json
from pathlib import Path

from alpha_first_discovery_backtest import (
    build_trade,
    cap_bucket,
    run_backtest,
    simulate_ordered_price_trade,
    simulate_partial_tp_trade,
)


def test_cap_bucket_boundaries() -> None:
    assert cap_bucket(10_000) == "<=10K"
    assert cap_bucket(10_001) == "10K-30K"
    assert cap_bucket(30_001) == "30K-100K"
    assert cap_bucket(100_001) == "100K-300K"
    assert cap_bucket(300_001) == "300K-1M"


def test_partial_tp_trade_hits_both_targets() -> None:
    trade = simulate_partial_tp_trade(peak_multiple=3.0, final_multiple=1.8)
    assert trade["path"] == "tp1_tp2_runner"
    assert round(trade["exit_multiple"], 3) == 1.745
    assert round(trade["return_pct"], 3) == 0.745


def test_partial_tp_trade_stops_unresolved_loser() -> None:
    trade = simulate_partial_tp_trade(peak_multiple=1.2, final_multiple=0.4)
    assert trade["path"] == "mark_or_stop"
    assert trade["exit_multiple"] == 0.78
    assert round(trade["return_pct"], 2) == -0.22


def test_ordered_price_trade_respects_stop_before_later_prices() -> None:
    trade = simulate_ordered_price_trade(
        first_price=1.0,
        observations=[
            {"seen_at": "2026-09-01T00:01:00+08:00", "price_usd": 0.77},
            {"seen_at": "2026-09-01T00:02:00+08:00", "price_usd": 2.50},
        ],
    )
    assert trade["path"] == "stop"
    assert trade["exit_multiple"] == 0.78


def test_build_trade_uses_entry_field() -> None:
    row = {
        "symbol": "TEST",
        "chain": "bsc",
        "contract_address": "0xabc",
        "first_seen_at": "2026-09-01T00:00:00+08:00",
        "first_confirmed_at": "2026-09-01T01:00:00+08:00",
        "first_seen_mcap": 10_000,
        "first_confirmed_mcap": 20_000,
        "max_seen_mcap": 60_000,
        "last_seen_mcap": 30_000,
        "status": "achieved_gold",
    }
    first = build_trade(row, "first_seen_mcap", "first_discovery")
    confirmed = build_trade(row, "first_confirmed_mcap", "first_confirmation")
    assert first is not None
    assert confirmed is not None
    assert first["peak_multiple"] == 6.0
    assert confirmed["peak_multiple"] == 3.0
    assert first["entry_at"] == "2026-09-01T00:00:00+08:00"
    assert confirmed["entry_at"] == "2026-09-01T01:00:00+08:00"


def test_run_backtest_compares_first_discovery_and_confirmation(tmp_path: Path) -> None:
    state = {
        "candidates": {
            "bsc:0x1": {
                "symbol": "EARLY",
                "chain": "bsc",
                "contract_address": "0x1",
                "first_seen_at": "2026-09-01T00:00:00+08:00",
                "first_confirmed_at": "2026-09-01T01:00:00+08:00",
                "first_seen_mcap": 10_000,
                "first_confirmed_mcap": 40_000,
                "max_seen_mcap": 100_000,
                "last_seen_mcap": 70_000,
                "status": "achieved_gold",
            },
            "bsc:0x2": {
                "symbol": "FAIL",
                "chain": "bsc",
                "contract_address": "0x2",
                "first_seen_at": "2026-09-01T00:00:00+08:00",
                "first_seen_mcap": 20_000,
                "max_seen_mcap": 22_000,
                "last_seen_mcap": 8_000,
                "status": "invalidated",
            },
            "eth:0x3": {
                "symbol": "OTHER",
                "chain": "eth",
                "contract_address": "0x3",
                "first_seen_mcap": 5_000,
                "max_seen_mcap": 50_000,
                "last_seen_mcap": 50_000,
            },
        }
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    result = run_backtest(path, max_first_seen_mcap=300_000)

    assert result["universe"]["all_candidates"] == 3
    assert result["universe"]["selected_first_seen_bsc_under_cap"] == 2
    assert result["first_discovery"]["summary"]["count"] == 2
    assert result["first_confirmation"]["summary"]["count"] == 1
    assert result["comparison_same_tokens"]["count"] == 1
    assert result["comparison_same_tokens"]["pairs"][0]["confirmation_paid_up_multiple"] == 4.0
