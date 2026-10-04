import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).with_name("alpha_scoring.py")
SPEC = importlib.util.spec_from_file_location("alpha_scoring", MODULE_PATH)
scoring = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = scoring
SPEC.loader.exec_module(scoring)


def test_score_row_marks_concentrated_high_activity_candidate():
    row = SimpleNamespace(
        market_cap=12_000_000,
        dex_market_cap=0,
        fdv=0,
        alpha_volume24h=12_000_000,
        dex_volume24h=4_000_000,
        pair_age_hours=12,
        futures_symbol="TESTUSDT",
        futures_quote_volume24h=30_000_000,
        oi_change_1h_pct=6,
        funding_rate_pct=0.04,
        hot_tag=True,
        social_count=2,
        top10_holder_pct=60,
        max_holder_pct=20,
        risk_level="medium",
        flags=[],
        notes=[],
        score=0,
    )

    scoring.score_row(row, max_market_cap=200_000_000)

    assert row.score == 135.0
    assert row.notes == []
    assert {
        "small_cap",
        "high_volume_to_mcap",
        "fresh_pool_24h",
        "listed_futures",
        "oi_rising",
        "funding_abnormal",
        "top10_concentrated",
        "single_holder_risk",
        "risk_medium",
    }.issubset(set(row.flags))


def test_score_row_notes_missing_holder_adapter():
    row = SimpleNamespace(
        market_cap=40_000_000,
        dex_market_cap=0,
        fdv=0,
        alpha_volume24h=1_000_000,
        dex_volume24h=0,
        pair_age_hours=None,
        futures_symbol="",
        futures_quote_volume24h=0,
        oi_change_1h_pct=None,
        funding_rate_pct=None,
        hot_tag=False,
        social_count=0,
        top10_holder_pct=None,
        max_holder_pct=None,
        risk_level="",
        flags=[],
        notes=[],
        score=0,
    )

    scoring.score_row(row, max_market_cap=200_000_000)

    assert row.score == 20
    assert row.flags == ["small_cap"]
    assert row.notes == ["Top10 holder needs explorer adapter"]


def test_score_row_surfaces_high_control_as_observation_until_stage_model_confirms():
    row = SimpleNamespace(
        market_cap=18_000_000,
        dex_market_cap=0,
        fdv=0,
        alpha_volume24h=9_000_000,
        dex_volume24h=2_000_000,
        alpha_change24h=68,
        futures_price_change24h=61,
        pair_age_hours=36,
        futures_symbol="PUMPUSDT",
        futures_quote_volume24h=18_000_000,
        oi_change_1h_pct=2,
        funding_rate_pct=0.01,
        hot_tag=False,
        social_count=1,
        top10_holder_pct=64,
        max_holder_pct=22,
        risk_level="",
        flags=[],
        notes=[],
        score=0,
    )

    scoring.score_row(row, max_market_cap=200_000_000)

    assert row.stage == "高控盘风险"
    assert row.action == ""
    assert row.direction == "观察"
    assert row.alpha_stage == "dealer_risk"
    assert row.alpha_stage_label == "高控盘风险"
    assert row.alpha_type == "dealer_risk"
    assert row.alpha_label == "高控盘风险"
    assert row.holding_text == ""
    assert row.protection_text == ""
    assert "alpha_big_gain" in row.flags
    assert "high_control_pump" in row.flags
    assert row.score >= 90


def test_score_row_marks_overheated_gain_as_observation_without_action():
    row = SimpleNamespace(
        market_cap=80_000_000,
        dex_market_cap=0,
        fdv=0,
        alpha_volume24h=18_000_000,
        dex_volume24h=0,
        alpha_change24h=135,
        futures_price_change24h=120,
        pair_age_hours=72,
        futures_symbol="HOTUSDT",
        futures_quote_volume24h=40_000_000,
        oi_change_1h_pct=1,
        funding_rate_pct=0.02,
        hot_tag=True,
        social_count=2,
        top10_holder_pct=28,
        max_holder_pct=9,
        risk_level="",
        flags=[],
        notes=[],
        score=0,
    )

    scoring.score_row(row, max_market_cap=200_000_000)

    assert row.stage == "观察"
    assert row.action == ""
    assert row.direction == "观察"
    assert row.alpha_stage == "watch"
    assert row.alpha_stage_label == "观察"
    assert row.alpha_type == "watch"
    assert row.alpha_label == "观察"
    assert row.protection_text == ""
    assert "alpha_overheated" in row.flags
    assert row.score >= 60
