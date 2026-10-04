import importlib.util
import sys
import copy
import pytest
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


replay = load_module("alpha_replay", BASE / "alpha_replay.py")


@pytest.mark.parametrize("fields", [
    {}, {"quote_status": "fresh"},
    {"quote_status": "fresh", "quote_observed_at": "2026-09-09T03:59:29+00:00"},
    {"quote_status": "fresh", "quote_observed_at": "2026-09-09T04:00:01+00:00"},
    {"quote_status": "fresh", "quote_observed_at": "2026-09-09T04:00:00"},
    {"quote_status": "stale", "quote_observed_at": "2026-09-09T04:00:00+00:00"},
    {"quote_status": "unavailable", "quote_observed_at": "2026-09-09T04:00:00+00:00"},
])
def test_r3_unverified_observation_cannot_create_replay_baseline(fields):
    row = {"chain": "bsc", "contract_address": "0xabc", "price_usd": 1, "mcap": 50_000, **fields}
    result = replay.update_replay_history({}, [row], "2026-09-09T04:00:00+00:00")
    assert result["rows"] == {}


def test_r3_stale_then_fresh_creates_only_true_first_baseline():
    now = "2026-09-09T04:00:00+00:00"
    stale = {"chain": "bsc", "contract_address": "0xabc", "price_usd": .004,
             "mcap": 200_000, "quote_status": "stale", "quote_observed_at": "2026-09-01T00:00:00+00:00"}
    history = replay.update_replay_history({}, [stale], now)
    fresh = {**stale, "price_usd": .001, "mcap": 50_000, "quote_status": "fresh",
             "quote_observed_at": "2026-09-09T03:59:50+00:00"}
    result = replay.update_replay_history(history, [fresh], now)["rows"]["bsc:0xabc"]
    assert result["first_price_usd"] == .001
    assert result["first_snapshot"]["mcap"] == 50_000
    assert result["first_snapshot"]["quote_observed_at"] == fresh["quote_observed_at"]


def test_unverified_first_discovery_is_retained_until_fresh_quote_creates_baseline():
    discovered_at = "2026-09-09T04:00:00+00:00"
    candidate = {
        "symbol": "NEW",
        "chain": "bsc",
        "contract_address": "0xnew",
        "price_usd": 0.004,
        "mcap": 40_000,
        "valuation_type": "market_cap",
        "quote_status": "unavailable",
        "quote_observed_at": None,
        "source_labels": ["GMGN"],
    }

    history = replay.update_replay_history({}, [candidate], discovered_at)

    assert history["rows"] == {}
    pending = history["pending_first_snapshots"]["bsc:0xnew"]
    assert pending["first_seen_at"] == discovered_at
    assert pending["first_snapshot"]["mcap"] == 40_000

    quoted_at = "2026-09-09T04:00:30+00:00"
    history = replay.update_replay_history(
        history,
        [{**candidate, "price_usd": 0.001, "mcap": 10_000,
          "quote_status": "fresh", "quote_observed_at": quoted_at}],
        quoted_at,
    )

    assert "bsc:0xnew" not in history["pending_first_snapshots"]
    row = history["rows"]["bsc:0xnew"]
    assert row["first_seen_at"] == discovered_at
    assert row["first_tradeable_quote_at"] == quoted_at
    assert row["first_price_usd"] == 0.001
    assert row["first_snapshot"]["mcap"] == 10_000
    assert row["discovery_first_seen_at"] == discovered_at
    assert row["discovery_snapshot"]["mcap"] == 40_000


def test_r3_existing_baseline_is_not_repaired_and_stale_does_not_update_it():
    now = "2026-09-09T04:00:00+00:00"
    baseline = {"first_seen_at": "2026-09-01T00:00:00+00:00", "first_price_usd": 0,
                "first_snapshot": {}, "observations": [], "latest_price_usd": .004}
    history = {"rows": {"bsc:0xabc": baseline}}
    before = copy.deepcopy(history)
    row = {"chain": "bsc", "contract_address": "0xabc", "price_usd": .001,
           "mcap": 50_000, "quote_status": "stale", "quote_observed_at": now}
    stale_result = replay.update_replay_history(history, [row], now)
    assert stale_result["rows"]["bsc:0xabc"] == baseline
    result = replay.update_replay_history(history, [{**row, "quote_status": "fresh"}], now)
    assert result["rows"]["bsc:0xabc"]["first_snapshot"] == {}
    assert result["rows"]["bsc:0xabc"]["first_price_usd"] == 0
    assert history == before


def test_r7_fdv_only_does_not_seed_market_cap_baseline():
    now = "2026-09-09T04:00:00+00:00"
    row = {"chain": "bsc", "contract_address": "0xabc", "price_usd": .001,
           "mcap": None, "market_cap": None, "fdv": 50_000,
           "quote_status": "fresh", "quote_observed_at": now}
    assert replay.update_replay_history({}, [row], now)["rows"] == {}


@pytest.mark.parametrize("valuation_type", ["fdv", "unavailable", "unknown", None, ""])
def test_r7_explicit_fdv_cannot_seed_baseline_even_with_positive_mcap(valuation_type):
    now = "2026-09-09T04:00:00+00:00"
    row = {"chain": "bsc", "contract_address": "0xabc", "price_usd": .001,
           "mcap": 50_000, "valuation_type": valuation_type, "quote_status": "fresh", "quote_observed_at": now}
    assert replay.update_replay_history({}, [row], now)["rows"] == {}


def test_update_replay_history_records_first_latest_and_hit_status():
    first_rows = [
        {
            "symbol": "MOON",
            "chain": "base",
            "contract_address": "0xmoon",
            "price_usd": 1.0,
            "mcap": 50_000,
            "quote_status": "fresh",
            "quote_observed_at": "2026-08-14T00:00:00+08:00",
            "recommendation_bucket": "lead",
            "recommendation_action": "主推盯盘",
        }
    ]
    history = replay.update_replay_history({}, first_rows, now_iso="2026-08-14T00:00:00+08:00")

    second_rows = [
        {
            "symbol": "MOON",
            "chain": "base",
            "contract_address": "0xmoon",
            "price_usd": 1.2,
            "mcap": 60_000,
            "quote_status": "fresh",
            "quote_observed_at": "2026-08-14T01:30:00+08:00",
            "recommendation_bucket": "lead",
            "recommendation_action": "主推盯盘",
        }
    ]
    history = replay.update_replay_history(history, second_rows, now_iso="2026-08-14T01:30:00+08:00")
    row = history["rows"]["base:0xmoon"]

    assert row["first_seen_at"] == "2026-08-14T00:00:00+08:00"
    assert row["first_price_usd"] == 1.0
    assert row["latest_price_usd"] == 1.2
    assert row["return_since_first_pct"] == 20.0
    assert row["return_1h_pct"] == 20.0
    assert row["hit_status"] == "hit"
    assert row["observations"][-1]["bucket"] == "lead"


def test_replay_trajectory_keeps_ordered_milestones_after_recent_quotes_roll_over():
    start = "2026-09-09T00:00:00+00:00"
    base = {
        "symbol": "MOON",
        "chain": "bsc",
        "contract_address": "0xmoon",
        "mcap": 50_000,
        "quote_status": "fresh",
        "recommendation_bucket": "lead",
    }
    history = replay.update_replay_history(
        {}, [{**base, "price_usd": 1.0, "quote_observed_at": start}], start
    )

    prices = [2.1, 0.7, 3.1, 5.1, 10.1] + [1.0] * 100
    for minute, price in enumerate(prices, start=1):
        stamp = f"2026-09-09T{minute // 60:02d}:{minute % 60:02d}:00+00:00"
        history = replay.update_replay_history(
            history, [{**base, "price_usd": price, "quote_observed_at": stamp}], stamp
        )

    row = history["rows"]["bsc:0xmoon"]
    trajectory = row["trajectory"]
    assert len(row["observations"]) == 96
    assert trajectory["sample_count"] == 106
    assert trajectory["hit_2x_at"] == "2026-09-09T00:01:00+00:00"
    assert trajectory["stop_hit_at"] == "2026-09-09T00:02:00+00:00"
    assert trajectory["hit_3x_at"] == "2026-09-09T00:03:00+00:00"
    assert trajectory["hit_5x_at"] == "2026-09-09T00:04:00+00:00"
    assert trajectory["hit_10x_at"] == "2026-09-09T00:05:00+00:00"
    assert trajectory["hit_2x_before_stop"] is True
    assert trajectory["hit_3x_before_stop"] is False
    assert trajectory["hit_5x_before_stop"] is False
    assert trajectory["hit_10x_before_stop"] is False


def test_replay_horizon_returns_are_frozen_at_first_verified_quote_after_horizon():
    base = {
        "symbol": "MOON",
        "chain": "bsc",
        "contract_address": "0xmoon",
        "mcap": 50_000,
        "quote_status": "fresh",
        "recommendation_bucket": "lead",
    }
    start = "2026-09-09T00:00:00+00:00"
    history = replay.update_replay_history(
        {}, [{**base, "price_usd": 1.0, "quote_observed_at": start}], start
    )
    first_hour = "2026-09-09T01:01:00+00:00"
    history = replay.update_replay_history(
        history, [{**base, "price_usd": 1.5, "quote_observed_at": first_hour}], first_hour
    )
    later = "2026-09-09T02:00:00+00:00"
    history = replay.update_replay_history(
        history, [{**base, "price_usd": 0.5, "quote_observed_at": later}], later
    )

    row = history["rows"]["bsc:0xmoon"]
    assert row["return_1h_pct"] == 50.0
    assert row["trajectory"]["return_1h_observed_at"] == first_hour


def test_replay_marks_trajectory_partial_when_old_recent_quotes_do_not_cover_first_seen():
    history = {
        "rows": {
            "bsc:0xmoon": {
                "symbol": "MOON",
                "chain": "bsc",
                "contract_address": "0xmoon",
                "first_seen_at": "2026-09-07T00:00:00+00:00",
                "first_price_usd": 1.0,
                "first_snapshot": {"mcap": 50_000},
                "observations": [{
                    "seen_at": "2026-09-09T03:58:00+00:00",
                    "quote_observed_at": "2026-09-09T03:58:00+00:00",
                    "price_usd": 2.0,
                }],
            }
        }
    }
    current = {
        "symbol": "MOON",
        "chain": "bsc",
        "contract_address": "0xmoon",
        "mcap": 50_000,
        "price_usd": 2.1,
        "quote_status": "fresh",
        "quote_observed_at": "2026-09-09T04:00:00+00:00",
    }

    row = replay.update_replay_history(
        history, [current], "2026-09-09T04:00:00+00:00"
    )["rows"]["bsc:0xmoon"]

    assert row["trajectory"]["coverage_from_first"] is False


def test_quote_timestamp_just_before_scanner_first_seen_still_covers_baseline():
    observations = [{
        "seen_at": "2026-09-09T04:00:00+00:00",
        "quote_observed_at": "2026-09-09T03:59:30+00:00",
        "price_usd": 1.0,
    }]

    assert replay.observations_cover_first_seen(
        observations, "2026-09-09T04:00:00+00:00"
    ) is True


def test_replay_summary_counts_hit_rate_for_actionable_rows():
    history = {
        "rows": {
            "base:a": {"recommendation_bucket": "lead", "hit_status": "hit"},
            "base:b": {"recommendation_bucket": "ambush", "hit_status": "tracking"},
            "base:c": {"recommendation_bucket": "danger", "hit_status": "avoided"},
            "base:d": {"recommendation_bucket": "reject", "hit_status": "ignored"},
        }
    }

    summary = replay.replay_summary(history)

    assert summary["actionable_count"] == 2
    assert summary["hit_count"] == 1
    assert summary["hit_rate_pct"] == 50.0
    assert summary["risk_avoided_count"] == 1


def test_replay_leaderboards_rank_best_worst_and_risk_avoided():
    history = {
        "rows": {
            "base:a": {
                "symbol": "AAA",
                "recommendation_bucket": "lead",
                "hit_status": "hit",
                "return_since_first_pct": 42.5,
            },
            "base:b": {
                "symbol": "BBB",
                "recommendation_bucket": "ambush",
                "hit_status": "miss",
                "return_since_first_pct": -18.0,
            },
            "base:c": {
                "symbol": "CCC",
                "recommendation_bucket": "danger",
                "hit_status": "avoided",
                "return_since_first_pct": -12.0,
            },
            "base:d": {
                "symbol": "DDD",
                "recommendation_bucket": "danger",
                "hit_status": "watch_risk",
                "return_since_first_pct": 20.0,
            },
        }
    }

    boards = replay.replay_leaderboards(history, limit=2)

    assert [row["symbol"] for row in boards["best_hits"]] == ["AAA"]
    assert [row["symbol"] for row in boards["worst_misses"]] == ["BBB"]
    assert [row["symbol"] for row in boards["risk_avoided"]] == ["CCC"]


def test_replay_summary_outputs_action_and_horizon_win_rates():
    history = {
        "rows": {
            "sol:a": {
                "recommendation_bucket": "ambush",
                "recommendation_action": "可小仓试探",
                "hit_status": "hit",
                "return_1h_pct": 18,
                "return_6h_pct": 30,
            },
            "sol:b": {
                "recommendation_bucket": "ambush",
                "recommendation_action": "可小仓试探",
                "hit_status": "miss",
                "return_1h_pct": -16,
                "return_6h_pct": -20,
            },
            "sol:c": {
                "recommendation_bucket": "pullback",
                "recommendation_action": "太热别追",
                "hit_status": "tracking",
                "return_1h_pct": -5,
            },
            "sol:d": {
                "recommendation_bucket": "danger",
                "recommendation_action": "风险太高别碰",
                "hit_status": "avoided",
                "return_1h_pct": -22,
            },
        }
    }

    summary = replay.replay_summary(history)

    small_probe = summary["by_action"]["可小仓试探"]
    assert small_probe["count"] == 2
    assert small_probe["hit_rate_pct"] == 50.0
    assert small_probe["avg_return_1h_pct"] == 1.0
    assert small_probe["avg_return_6h_pct"] == 5.0
    assert small_probe["horizon_win_rates"]["1h"]["win_rate_pct"] == 50.0
    assert small_probe["horizon_win_rates"]["6h"]["win_rate_pct"] == 50.0

    risk_action = summary["by_action"]["风险太高别碰"]
    assert risk_action["avoided_count"] == 1
    assert risk_action["horizon_win_rates"]["1h"]["win_rate_pct"] == 0.0


def test_replay_action_calibration_penalizes_losing_action():
    summary = {
        "by_action": {
            "SMALL_PROBE": {
                "count": 12,
                "hit_rate_pct": 16.67,
                "avg_return_1h_pct": -5.8,
                "horizon_win_rates": {"1h": {"count": 8, "win_rate_pct": 37.5, "avg_return_pct": -5.8}},
            },
            "WAIT_PULLBACK": {
                "count": 9,
                "hit_rate_pct": 0.0,
                "avg_return_1h_pct": 2.4,
                "horizon_win_rates": {"1h": {"count": 7, "win_rate_pct": 57.14, "avg_return_pct": 2.4}},
            },
        }
    }

    calibration = replay.replay_action_calibration(summary)

    assert calibration["SMALL_PROBE"]["score_adjustment"] == -12
    assert calibration["SMALL_PROBE"]["risk_level"] == "weak"
    assert "回测拖累" in calibration["SMALL_PROBE"]["reason"]
    assert calibration["WAIT_PULLBACK"]["score_adjustment"] == 3
    assert calibration["WAIT_PULLBACK"]["risk_level"] == "ok"


def test_update_replay_history_keeps_first_seen_gold_dog_features():
    history = replay.update_replay_history(
        {},
        [
            {
                "symbol": "DOG",
                "chain": "solana",
                "contract_address": "MintDog",
                "price_usd": 0.001,
                "quote_status": "fresh",
                "quote_observed_at": "2026-08-14T00:00:00+08:00",
                "recommendation_bucket": "ambush",
                "recommendation_action": "可小仓试探",
                "source_labels": ["GMGN", "Birdeye", "DS"],
                "sources": ["gmgn_trending", "birdeye_trending", "profile_latest"],
                "source_groups": ["gmgn", "birdeye", "dexscreener"],
                "source_count": 3,
                "source_hit_counts": {"gmgn_trending": 2, "birdeye_trending": 1},
                "source_hit_total": 3,
                "source_repeat_score": 2,
                "source_repeat_flags": ["gmgn_trending_x2"],
                "mcap": 420_000,
                "liquidity": 82_000,
                "volume24h": 690_000,
                "pair_age_hours": 4,
                "smart_money": 33,
                "kol": 8,
                "top10_holder_pct": 18,
                "gold_dog_conviction_score": 84,
            }
        ],
        now_iso="2026-08-14T00:00:00+08:00",
    )

    row = history["rows"]["solana:mintdog"]

    assert row["first_snapshot"]["source_labels"] == ["GMGN", "Birdeye", "DS"]
    assert row["first_snapshot"]["source_groups"] == ["gmgn", "birdeye", "dexscreener"]
    assert row["first_snapshot"]["source_count"] == 3
    assert row["first_snapshot"]["source_hit_total"] == 3
    assert row["first_snapshot"]["source_repeat_flags"] == ["gmgn_trending_x2"]
    assert row["first_snapshot"]["mcap"] == 420_000
    assert row["first_snapshot"]["pair_age_hours"] == 4
    assert row["first_snapshot"]["gold_dog_conviction_score"] == 84


def test_replay_profile_summary_groups_early_gold_dog_features():
    history = {
        "rows": {
            "sol:a": {
                "recommendation_bucket": "ambush",
                "hit_status": "hit",
                "return_1h_pct": 32,
                "first_snapshot": {
                    "source_labels": ["GMGN", "Birdeye", "DS"],
                    "mcap": 420_000,
                    "pair_age_hours": 4,
                    "smart_money": 33,
                    "kol": 8,
                    "top10_holder_pct": 18,
                    "gold_dog_conviction_score": 84,
                },
            },
            "sol:b": {
                "recommendation_bucket": "ambush",
                "hit_status": "miss",
                "return_1h_pct": -22,
                "first_snapshot": {
                    "source_labels": ["GMGN", "DS"],
                    "mcap": 8_000_000,
                    "pair_age_hours": 90,
                    "smart_money": 2,
                    "kol": 0,
                    "top10_holder_pct": 46,
                    "gold_dog_conviction_score": 45,
                },
            },
        }
    }

    summary = replay.replay_profile_summary(history, min_count=1)

    assert summary["by_source_combo"]["Birdeye+DS+GMGN"]["hit_rate_pct"] == 100.0
    assert summary["by_source_combo"]["DS+GMGN"]["hit_rate_pct"] == 0.0
    assert summary["by_feature"]["fresh_pool"]["avg_return_1h_pct"] == 32.0
    assert summary["by_feature"]["stale_pool"]["avg_return_1h_pct"] == -22.0
    assert summary["by_feature"]["strong_smart_money"]["hit_rate_pct"] == 100.0
    assert summary["by_feature"]["fresh_pool"]["gold_count"] == 0


def test_grouped_replay_stats_uses_peak_return_for_gold_dog_outcome():
    rows = [
        {
            "hit_status": "tracking",
            "return_since_first_pct": -20,
            "peak_return_pct": 1200,
            "peak_mcap": 2_600_000,
            "return_1h_pct": 35,
        },
        {
            "hit_status": "tracking",
            "return_since_first_pct": -10,
            "peak_return_pct": 45,
            "return_1h_pct": -8,
        },
    ]

    stats = replay.grouped_replay_stats(rows)

    assert stats["runner_count"] == 1
    assert stats["gold_count"] == 1
    assert stats["max_return_pct"] == 1200


def test_grouped_replay_stats_counts_only_ten_x_returns_with_one_million_peak_market_cap_as_gold_dog():
    stats = replay.grouped_replay_stats(
        [
            {"hit_status": "hit", "return_since_first_pct": 1200, "peak_mcap": 2_600_000, "return_1h_pct": 80},
            {"hit_status": "hit", "return_since_first_pct": 1200, "peak_mcap": 600_000, "return_1h_pct": 40},
            {"hit_status": "hit", "return_since_first_pct": 55, "return_1h_pct": 20},
            {"hit_status": "miss", "return_since_first_pct": -24, "return_1h_pct": -24},
        ]
    )

    assert stats["gold_count"] == 1
    assert stats["gold_rate_pct"] == 25.0
    assert stats["runner_count"] == 3
    assert stats["runner_rate_pct"] == 75.0
    assert stats["max_return_pct"] == 1200.0
