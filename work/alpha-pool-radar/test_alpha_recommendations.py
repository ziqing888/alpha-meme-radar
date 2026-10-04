import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recommendations = load_module("alpha_recommendations", BASE / "alpha_recommendations.py")


def test_build_recommendation_rows_groups_actionable_buckets():
    rows = [
        {
            "symbol": "LEAD",
            "score": 82,
            "dealer_score": 80,
            "market_cap": 45_000_000,
            "alpha_volume24h": 16_000_000,
            "oi_change_1h_pct": 8.2,
            "funding_rate_pct": 0.018,
            "alpha_change24h_pct": 12,
            "pair_age_hours": 72,
        },
        {
            "symbol": "AMB",
            "score": 70,
            "market_cap": 28_000_000,
            "alpha_volume24h": 4_000_000,
            "oi_change_1h_pct": 2.4,
            "funding_rate_pct": 0.01,
            "alpha_change24h_pct": 8,
            "pair_age_hours": 90,
        },
        {
            "symbol": "WAIT",
            "score": 66,
            "market_cap": 60_000_000,
            "alpha_volume24h": 8_000_000,
            "oi_change_1h_pct": 1.2,
            "funding_rate_pct": 0.07,
            "alpha_change24h_pct": 34,
            "pair_age_hours": 18,
        },
        {
            "symbol": "RISK",
            "score": 75,
            "market_cap": 18_000_000,
            "top10_holder_pct": 62,
            "max_holder_pct": 24,
        },
        {
            "symbol": "SKIP",
            "score": 31,
            "market_cap": 150_000_000,
            "alpha_volume24h": 200_000,
            "pair_age_hours": 400,
        },
    ]

    for row in rows:
        row.setdefault("top10_holder_pct", 30)
        row.setdefault("max_holder_pct", 5)
    result = recommendations.build_recommendation_rows(rows, dealer_rows=[])
    by_symbol = {row["symbol"]: row for row in result}

    assert [row["symbol"] for row in result] == ["LEAD", "AMB", "WAIT", "RISK", "SKIP"]
    assert by_symbol["LEAD"]["recommendation_bucket"] == "lead"
    assert by_symbol["LEAD"]["recommendation_action"] == "主推盯盘"
    assert "OI" in by_symbol["LEAD"]["recommendation_reason"]
    assert by_symbol["AMB"]["recommendation_bucket"] == "ambush"
    assert by_symbol["WAIT"]["recommendation_bucket"] == "pullback"
    assert by_symbol["RISK"]["recommendation_bucket"] == "danger"
    assert by_symbol["SKIP"]["recommendation_bucket"] == "reject"
    assert by_symbol["SKIP"]["recommendation_action"] == "放弃观察"


def test_build_recommendation_rows_dedupes_alpha_and_dealer_rows():
    alpha_rows = [{"symbol": "AAA", "chain": "base", "score": 71, "market_cap": 20_000_000}]
    dealer_rows = [{"symbol": "AAA", "chain": "base", "dealer_score": 80, "oi_change_1h_pct": 7}]

    result = recommendations.build_recommendation_rows(alpha_rows, dealer_rows)

    assert len(result) == 1
    assert result[0]["score"] == 71
    assert result[0]["dealer_score"] == 80


def test_classify_near_setup_as_ambush_before_big_move():
    row = {
        "symbol": "SETUP",
        "score": 61,
        "dealer_score": 58,
        "market_cap": 38_000_000,
        "alpha_volume24h": 7_500_000,
        "oi_change_1h_pct": 3.1,
        "funding_rate_pct": 0.012,
        "alpha_change24h_pct": 9.5,
        "pair_age_hours": 60,
        "top10_holder_pct": 35,
        "max_holder_pct": 9,
    }

    result = recommendations.classify_row(row)

    assert result["recommendation_bucket"] == "ambush"
    assert result["recommendation_action"] == "可埋伏"
    assert "量市比" in result["recommendation_reason"]


def test_keeps_overheated_setup_waiting_for_pullback():
    row = {
        "symbol": "HOT",
        "top10_holder_pct": 30,
        "max_holder_pct": 5,
        "score": 75,
        "dealer_score": 72,
        "market_cap": 42_000_000,
        "alpha_volume24h": 18_000_000,
        "oi_change_1h_pct": 6.5,
        "funding_rate_pct": 0.095,
        "alpha_change24h_pct": 42,
        "pair_age_hours": 30,
    }

    result = recommendations.classify_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "等回踩"


def test_negative_funding_squeeze_setup_can_be_ambush():
    row = {
        "symbol": "SHORTS",
        "score": 64,
        "dealer_score": 45,
        "market_cap": 62_000_000,
        "alpha_volume24h": 13_000_000,
        "oi_change_1h_pct": 1.5,
        "funding_rate_pct": -0.12,
        "alpha_change24h_pct": 4,
        "pair_age_hours": 120,
        "top10_holder_pct": 0,
        "max_holder_pct": 0,
    }

    result = recommendations.classify_row(row)

    assert result["recommendation_bucket"] == "ambush"
    assert result["recommendation_action"] == "可埋伏"
    assert "负费率" in result["recommendation_reason"]
