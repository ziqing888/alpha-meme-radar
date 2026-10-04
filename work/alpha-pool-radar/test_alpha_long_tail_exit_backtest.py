from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpha_long_tail_exit_backtest import (
    CURRENT_EXIT,
    LONG_TAIL_EXIT,
    augment_entries_with_fast_quotes,
    build_backtest,
    markdown_report,
    select_replay_entries,
    stress_return,
    summarize_results,
    simulate_exit,
)


TZ = timezone(timedelta(hours=8))
ENTRY_AT = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)


def point(minutes: int, multiple: float) -> dict[str, object]:
    return {
        "seen_at": (ENTRY_AT + timedelta(minutes=minutes)).isoformat(),
        "price_usd": multiple,
    }


def test_current_exit_sells_eighty_percent_at_two_x_then_time_stops() -> None:
    result = simulate_exit(
        entry_price=1.0,
        entry_at=ENTRY_AT,
        observations=[point(30, 2.1), point(60, 1.6), point(95, 1.4)],
        model=CURRENT_EXIT,
    )

    assert result["resolved"] is True
    assert result["exit_reason"] == "time_stop"
    assert result["fills"] == [
        {"reason": "take_profit_2x", "fraction": 0.8, "multiple": 2.0, "minutes": 30.0},
        {"reason": "time_stop", "fraction": 0.2, "multiple": 1.4, "minutes": 95.0},
    ]
    assert round(result["gross_return"], 4) == 0.88


def test_long_tail_exit_keeps_thirty_percent_for_large_winner() -> None:
    result = simulate_exit(
        entry_price=1.0,
        entry_at=ENTRY_AT,
        observations=[point(30, 2.1), point(120, 5.3), point(180, 6.0), point(240, 3.5)],
        model=LONG_TAIL_EXIT,
    )

    assert result["resolved"] is True
    assert result["exit_reason"] == "trailing_stop"
    assert result["fills"] == [
        {"reason": "take_profit_2x", "fraction": 0.5, "multiple": 2.0, "minutes": 30.0},
        {"reason": "take_profit_3x", "fraction": 0.1, "multiple": 3.0, "minutes": 120.0},
        {"reason": "take_profit_5x", "fraction": 0.1, "multiple": 5.0, "minutes": 120.0},
        {"reason": "trailing_stop", "fraction": 0.3, "multiple": 3.5, "minutes": 240.0},
    ]
    assert round(result["gross_return"], 4) == 1.85


def test_long_tail_only_unlocks_twenty_four_hours_after_two_x() -> None:
    result = simulate_exit(
        entry_price=1.0,
        entry_at=ENTRY_AT,
        observations=[point(30, 1.2), point(95, 1.1), point(120, 5.3)],
        model=LONG_TAIL_EXIT,
    )

    assert result["resolved"] is True
    assert result["exit_reason"] == "time_stop"
    assert result["fills"] == [
        {"reason": "time_stop", "fraction": 1.0, "multiple": 1.1, "minutes": 95.0},
    ]
    assert result["peak_multiple"] == 1.2


def test_stop_uses_observed_gap_price_instead_of_ideal_threshold() -> None:
    result = simulate_exit(
        entry_price=1.0,
        entry_at=ENTRY_AT,
        observations=[point(1, 0.70)],
        model=LONG_TAIL_EXIT,
    )

    assert result["exit_reason"] == "stop_loss"
    assert result["fills"][0]["multiple"] == 0.70
    assert round(result["gross_return"], 4) == -0.30


def test_points_at_or_before_entry_are_not_future_path() -> None:
    result = simulate_exit(
        entry_price=1.0,
        entry_at=ENTRY_AT,
        observations=[point(0, 10.0), point(1, 0.9)],
        model=LONG_TAIL_EXIT,
    )

    assert result["peak_multiple"] == 1.0
    assert result["resolved"] is False
    assert result["exit_reason"] == "mark_end"
    assert round(result["gross_return"], 4) == -0.10


def test_price_returning_long_after_deadline_is_not_used_as_time_stop_fill() -> None:
    result = simulate_exit(
        entry_price=1.0,
        entry_at=ENTRY_AT,
        observations=[point(30, 1.2), point(300, 10.0)],
        model=CURRENT_EXIT,
    )

    assert result["resolved"] is False
    assert result["exit_reason"] == "mark_end"
    assert result["peak_multiple"] == 1.2
    assert round(result["gross_return"], 4) == 0.2


def test_stress_return_charges_roundtrip_friction_and_each_swap() -> None:
    result = simulate_exit(
        entry_price=1.0,
        entry_at=ENTRY_AT,
        observations=[point(30, 2.1), point(95, 1.4)],
        model=CURRENT_EXIT,
    )

    stressed = stress_return(result, notional_usd=5.0, roundtrip_cost_pct=5.0, gas_usd_per_swap=0.12)

    # Gross 1.88x, 5% full-position friction, and entry + two exit swaps of gas.
    assert round(stressed, 4) == 0.758


def test_summary_reports_tail_concentration_and_unresolved_worst_case() -> None:
    rows = [
        {"gross_return": -0.22, "stress_return": -0.34, "resolved": True, "peak_multiple": 1.0},
        {"gross_return": -0.22, "stress_return": -0.34, "resolved": False, "peak_multiple": 1.1, "conservative_return": -1.0},
        {"gross_return": 2.05, "stress_return": 1.90, "resolved": True, "peak_multiple": 6.0},
    ]

    summary = summarize_results(rows)

    assert summary["count"] == 3
    assert summary["five_x_rate"] == 1 / 3
    assert summary["unresolved_count"] == 1
    assert round(summary["gross_total_units"], 4) == 1.61
    assert round(summary["conservative_total_units"], 4) == 0.83
    assert round(summary["conservative_stress_total_units"], 4) == 0.83
    assert summary["top_1pct_profit_share"] == 1.0


def replay_row(key: str, *, liquidity: float, prices: list[tuple[int, float]], mcap: float = 50_000) -> dict[str, object]:
    chain, token = key.split(":", 1)
    observations = [point(minutes, price) for minutes, price in prices]
    return {
        "key": key,
        "chain": chain,
        "contract_address": token,
        "symbol": token.upper(),
        "first_seen_at": ENTRY_AT.isoformat(),
        "first_price_usd": 1.0,
        "observations": observations,
        "first_snapshot": {
            "chain": chain,
            "contract_address": token,
            "symbol": token.upper(),
            "mcap": mcap,
            "price_usd": 1.0,
            "liquidity": liquidity,
            "volume24h": 100_000,
            "smart_money": 0,
            "kol": 0,
            "source_labels": ["OKX", "GMGN"],
            "pair_age_hours": 1,
            "change_m5": 10,
            "change_h1": 20,
            "top10_holder_pct": 10,
            "max_holder_pct": 5,
            "gmgn_risk_flags": [],
        },
    }


def test_selection_uses_only_information_present_at_first_discovery() -> None:
    replay = {
        "rows": {
            "bsc:dog": replay_row("bsc:dog", liquidity=20_000, prices=[(5, 2.1)]),
            "robinhood:cat": replay_row("robinhood:cat", liquidity=20_000, prices=[(5, 0.7)]),
            "bsc:thin": replay_row("bsc:thin", liquidity=2_000, prices=[(5, 20.0)]),
            "bsc:too_big": replay_row("bsc:too_big", liquidity=20_000, mcap=150_000, prices=[(5, 20.0)]),
            "solana:other": replay_row("solana:other", liquidity=20_000, prices=[(5, 20.0)]),
        }
    }

    selected, rejected = select_replay_entries(replay)

    assert {row["key"] for row in selected} == {"bsc:dog", "robinhood:cat"}
    assert all("score_bucket" in row for row in selected)
    assert rejected["池子太薄"] == 1
    assert rejected["bsc_validated_first_mcap"] == 1
    assert rejected["unsupported_live_chain"] == 1


def test_backtest_compares_models_on_the_exact_same_entries() -> None:
    replay = {
        "rows": {
            "bsc:dog": replay_row("bsc:dog", liquidity=20_000, prices=[(30, 2.1), (120, 5.3), (180, 6.0), (240, 3.5)]),
            "robinhood:cat": replay_row("robinhood:cat", liquidity=20_000, prices=[(5, 0.7)]),
        }
    }

    result = build_backtest(replay, notional_usd=5.0, roundtrip_cost_pct=5.0, gas_usd_per_swap=0.12)

    assert result["universe"]["eligible_entries"] == 2
    by_name = {row["name"]: row for row in result["models"]}
    assert by_name[CURRENT_EXIT.name]["summary"]["count"] == 2
    assert by_name[LONG_TAIL_EXIT.name]["summary"]["count"] == 2
    assert by_name[LONG_TAIL_EXIT.name]["summary"]["gross_total_units"] > by_name[CURRENT_EXIT.name]["summary"]["gross_total_units"]
    assert result["comparison"]["same_entry_count"] == 2
    assert set(by_name[LONG_TAIL_EXIT.name]["by_chain"]) == {"bsc", "robinhood"}
    assert by_name[LONG_TAIL_EXIT.name]["by_score_bucket"]
    report = markdown_report(result)
    assert "按首次市值" in report
    assert "按首次评分" in report


def test_fast_quote_augmentation_adds_only_matching_future_fresh_quotes(tmp_path: Path) -> None:
    entry = select_replay_entries({"rows": {"bsc:dog": replay_row("bsc:dog", liquidity=20_000, prices=[(5, 1.1)])}})[0][0]
    rows = [
        {"chain": "bsc", "contract_address": "dog", "price_usd": 1.2, "quote_observed_at": point(6, 1)["seen_at"], "quote_status": "fresh"},
        {"chain": "bsc", "contract_address": "dog", "price_usd": 9.0, "quote_observed_at": point(7, 1)["seen_at"], "quote_status": "stale"},
        {"chain": "bsc", "contract_address": "other", "price_usd": 5.0, "quote_observed_at": point(8, 1)["seen_at"], "quote_status": "fresh"},
    ]
    path = tmp_path / "quotes.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    augmented, stats = augment_entries_with_fast_quotes([entry], path, max_horizon_minutes=90)

    assert stats == {"lines_read": 3, "matched_quotes": 1, "matched_entries": 1, "invalid_lines": 0}
    assert len(augmented[0]["observations"]) == 2
    assert augmented[0]["observations"][-1]["price_usd"] == 1.2
