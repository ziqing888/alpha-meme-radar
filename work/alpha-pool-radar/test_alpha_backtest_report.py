import importlib.util
import json
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


load_module("alpha_replay", BASE / "alpha_replay.py")
backtest = load_module("alpha_backtest_report", BASE / "alpha_backtest_report.py")


def test_build_backtest_payload_ranks_best_edges_and_weak_edges():
    history = {
        "summary": {
            "tracked_count": 20,
            "actionable_count": 8,
            "hit_count": 3,
            "hit_rate_pct": 37.5,
            "by_profile": {
                "by_source_combo": {
                    "DS+GMGN": {
                        "count": 6,
                        "hit_rate_pct": 50,
                        "runner_rate_pct": 33.33,
                        "gold_rate_pct": 16.67,
                        "avg_return_1h_pct": 8,
                        "max_return_pct": 140,
                    },
                    "unknown": {
                        "count": 9,
                        "hit_rate_pct": 0,
                        "runner_rate_pct": 0,
                        "gold_rate_pct": 0,
                        "avg_return_1h_pct": -18,
                        "max_return_pct": 8,
                    },
                },
                "by_feature": {
                    "fresh_pool": {
                        "count": 5,
                        "hit_rate_pct": 60,
                        "runner_rate_pct": 40,
                        "gold_rate_pct": 20,
                        "avg_return_1h_pct": 12,
                        "max_return_pct": 180,
                    },
                    "stale_pool": {
                        "count": 7,
                        "hit_rate_pct": 0,
                        "runner_rate_pct": 0,
                        "gold_rate_pct": 0,
                        "avg_return_1h_pct": -11,
                        "max_return_pct": 5,
                    },
                },
            },
        }
    }

    payload = backtest.build_backtest_payload(history, min_count=5)

    assert payload["summary"]["actionable_count"] == 8
    assert payload["best_edges"][0]["name"] == "fresh_pool"
    assert payload["best_edges"][0]["kind"] == "feature"
    assert payload["weak_edges"][0]["name"] in {"stale_pool", "unknown"}
    assert payload["next_rules"][0]["action"] == "加权"


def test_render_backtest_markdown_mentions_sample_size_and_edges():
    payload = {
        "summary": {"tracked_count": 20, "actionable_count": 8, "hit_rate_pct": 37.5},
        "best_edges": [
            {"kind": "feature", "name": "fresh_pool", "count": 5, "hit_rate_pct": 60, "runner_rate_pct": 40, "gold_rate_pct": 20, "avg_return_1h_pct": 12, "max_return_pct": 180}
        ],
        "weak_edges": [
            {"kind": "source", "name": "unknown", "count": 9, "hit_rate_pct": 0, "runner_rate_pct": 0, "gold_rate_pct": 0, "avg_return_1h_pct": -18, "max_return_pct": 8}
        ],
        "next_rules": [
            {"action": "加权", "name": "fresh_pool", "reason": "金狗率 20.00%，runner 40.00%"}
        ],
        "gold_pick_audit": {
            "gold_return_pct": 900,
            "gold_min_peak_mcap": 1_000_000,
            "total_gold_count": 3,
            "caught_gold_count": 1,
            "wrong_gold_pick_count": 2,
            "ten_x_mcap_unknown_count": 4,
            "ten_x_below_mcap_count": 1,
            "gold_capture_rate_pct": 33.33,
            "gold_pick_precision_pct": 33.33,
        },
        "strategy_optimization": {
            "primary_rule": {
                "name": "fresh_pool",
                "count": 5,
                "gold_rate_pct": 20,
                "gold_capture_rate_pct": 33.33,
                "false_positive_rate_pct": 40,
            },
            "recommendations": ["主推只保留一只，影子池继续学习。"],
        },
    }

    text = backtest.render_markdown(payload)

    assert "金狗回测报告" in text
    assert "样本" in text
    assert "10x" in text
    assert "$1M" in text
    assert "抓到金狗" in text
    assert "抓错主推" in text
    assert "10x待确认" in text
    assert "策略优化" in text
    assert "fresh_pool" in text
    assert "unknown" in text


def test_evaluate_candidate_rules_finds_best_gold_dog_filter():
    rows = [
            {
                "recommendation_bucket": "ambush",
                "hit_status": "hit",
                "return_since_first_pct": 1200,
                "return_1h_pct": 40,
                "first_snapshot": {"mcap": 120_000, "pair_age_hours": 4, "smart_money": 35, "kol": 8, "top10_holder_pct": 18, "gold_dog_conviction_score": 84},
            },
        {
            "recommendation_bucket": "ambush",
            "hit_status": "hit",
            "return_since_first_pct": 65,
            "return_1h_pct": 25,
            "first_snapshot": {"mcap": 220_000, "pair_age_hours": 8, "smart_money": 24, "kol": 6, "top10_holder_pct": 22, "gold_dog_conviction_score": 78},
        },
        {
            "recommendation_bucket": "ambush",
            "hit_status": "miss",
            "return_since_first_pct": -30,
            "return_1h_pct": -30,
            "first_snapshot": {"mcap": 8_000_000, "pair_age_hours": 96, "smart_money": 2, "kol": 0, "top10_holder_pct": 48, "gold_dog_conviction_score": 44},
        },
    ]

    rules = backtest.evaluate_candidate_rules(rows, min_count=2)

    assert rules[0]["name"] == "高确认+新池"
    assert rules[0]["count"] == 2
    assert rules[0]["gold_rate_pct"] == 50.0
    assert rules[0]["hit_rate_pct"] == 100.0


def test_snapshot_metric_computes_derived_gold_dog_inputs():
    row = {
        "first_snapshot": {
            "mcap": 200_000,
            "volume24h": 500_000,
            "liquidity": 40_000,
            "source_labels": ["GMGN", "DS", "Alpha_AI"],
            "gmgn_risk_flags": ["sniper"],
            "narrative_tags": ["AI", "中文梗"],
        }
    }

    assert backtest.snapshot_metric(row, "volume_to_mcap") == 2.5
    assert backtest.snapshot_metric(row, "liquidity_to_mcap") == 0.2
    assert backtest.snapshot_metric(row, "source_count") == 3
    assert backtest.snapshot_metric(row, "risk_flag_count") == 1
    assert backtest.snapshot_metric(row, "narrative_count") == 2


def test_auto_threshold_search_rewards_early_liquidity_flow_profile():
    winners = [
            {
                "recommendation_bucket": "ambush",
                "hit_status": "hit",
                "return_since_first_pct": 1200,
                "return_1h_pct": 42,
                "first_snapshot": {
                "mcap": 180_000,
                "volume24h": 260_000,
                "liquidity": 35_000,
                "pair_age_hours": 5,
                "smart_money": 28,
                "kol": 7,
                "top10_holder_pct": 19,
                "gold_dog_conviction_score": 82,
                "source_labels": ["GMGN", "DS"],
            },
        },
        {
            "recommendation_bucket": "ambush",
            "hit_status": "hit",
            "return_since_first_pct": 75,
            "return_1h_pct": 18,
            "first_snapshot": {
                "mcap": 250_000,
                "volume24h": 210_000,
                "liquidity": 30_000,
                "pair_age_hours": 9,
                "smart_money": 22,
                "kol": 5,
                "top10_holder_pct": 22,
                "gold_dog_conviction_score": 76,
                "source_labels": ["GMGN", "DS"],
            },
        },
        {
            "recommendation_bucket": "ambush",
            "hit_status": "hit",
            "return_since_first_pct": 35,
            "return_1h_pct": 8,
            "first_snapshot": {
                "mcap": 280_000,
                "volume24h": 190_000,
                "liquidity": 25_000,
                "pair_age_hours": 10,
                "smart_money": 19,
                "kol": 4,
                "top10_holder_pct": 24,
                "gold_dog_conviction_score": 71,
                "source_labels": ["GMGN", "DS"],
            },
        },
    ]
    misses = [
        {
            "recommendation_bucket": "ambush",
            "hit_status": "miss",
            "return_since_first_pct": -25,
            "return_1h_pct": -25,
            "first_snapshot": {
                "mcap": 8_000_000,
                "volume24h": 80_000,
                "liquidity": 8_000,
                "pair_age_hours": 120,
                "smart_money": 1,
                "kol": 0,
                "top10_holder_pct": 55,
                "gold_dog_conviction_score": 38,
                "source_labels": [],
            },
        }
        for _ in range(3)
    ]

    rules = backtest.evaluate_candidate_rules([*winners, *misses], min_count=3)

    assert rules
    assert any("新池" in rule["name"] for rule in rules[:8])
    assert rules[0]["gold_rate_pct"] >= 33.33


def test_evaluate_candidate_rules_dedupes_same_matched_sample_set():
    rows = [
        {
            "key": f"winner-{index}",
            "recommendation_bucket": "ambush",
            "hit_status": "hit",
            "return_since_first_pct": 120,
            "return_1h_pct": 20,
            "first_snapshot": {
                "mcap": 120_000,
                "volume24h": 180_000,
                "pair_age_hours": 4,
                "smart_money": 35,
                "kol": 8,
                "top10_holder_pct": 18,
                "gold_dog_conviction_score": 90,
                "source_labels": ["GMGN", "DS"],
            },
        }
        for index in range(3)
    ]

    rules = backtest.evaluate_candidate_rules(rows, min_count=3)
    signatures = {
        tuple(sorted((rule.get("min") or {}).items())) + tuple(sorted((rule.get("max") or {}).items()))
        for rule in rules
    }

    assert len(rules) < 8
    assert len(signatures) == len(rules)


def test_evaluate_candidate_rules_learns_from_shadow_observation_rows():
    rows = [
            {
                "key": "shadow-gold-1",
                "recommendation_bucket": "shadow",
                "hit_status": "tracking",
                "return_since_first_pct": 1200,
                "return_1h_pct": 48,
            "first_snapshot": {
                "shadow_original_bucket": "ambush",
                "mcap": 160_000,
                "volume24h": 300_000,
                "pair_age_hours": 4,
                "smart_money": 34,
                "kol": 8,
                "top10_holder_pct": 18,
                "gold_dog_conviction_score": 86,
                "source_labels": ["GMGN", "DS", "Alpha_AI"],
            },
        },
            {
                "key": "shadow-gold-2",
                "recommendation_bucket": "shadow",
                "hit_status": "tracking",
                "return_since_first_pct": 1000,
                "return_1h_pct": 36,
            "first_snapshot": {
                "shadow_original_bucket": "ambush",
                "mcap": 220_000,
                "volume24h": 260_000,
                "pair_age_hours": 7,
                "smart_money": 30,
                "kol": 6,
                "top10_holder_pct": 21,
                "gold_dog_conviction_score": 80,
                "source_labels": ["GMGN", "DS"],
            },
        },
        {
            "key": "shadow-miss",
            "recommendation_bucket": "shadow",
            "hit_status": "tracking",
            "return_since_first_pct": -28,
            "return_1h_pct": -28,
            "first_snapshot": {
                "shadow_original_bucket": "pullback",
                "mcap": 6_000_000,
                "volume24h": 90_000,
                "pair_age_hours": 96,
                "smart_money": 2,
                "kol": 0,
                "top10_holder_pct": 52,
                "gold_dog_conviction_score": 35,
                "source_labels": [],
            },
        },
    ]

    rules = backtest.evaluate_candidate_rules(rows, min_count=2)

    assert rules
    assert rules[0]["gold_count"] == 2
    assert rules[0]["gold_rate_pct"] == 100.0


def test_evaluate_candidate_rules_reports_gold_capture_and_false_positives():
    rows = [
        {
            "key": "gold-a",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1200,
            "peak_mcap": 2_160_000,
            "first_snapshot": {"mcap": 180_000, "pair_age_hours": 4, "gold_dog_conviction_score": 88},
        },
        {
            "key": "gold-b",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1000,
            "peak_mcap": 2_420_000,
            "first_snapshot": {"mcap": 220_000, "pair_age_hours": 8, "gold_dog_conviction_score": 82},
        },
        {
            "key": "gold-c",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 950,
            "peak_mcap": 52_500_000,
            "first_snapshot": {"mcap": 5_000_000, "pair_age_hours": 4, "gold_dog_conviction_score": 86},
        },
        {
            "key": "noise",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": -40,
            "first_snapshot": {"mcap": 240_000, "pair_age_hours": 6, "gold_dog_conviction_score": 90},
        },
    ]

    rules = backtest.evaluate_candidate_rules(rows, min_count=2)
    micro_rule = next(rule for rule in rules if rule["name"] == "微市值+新池")

    assert micro_rule["gold_count"] == 2
    assert micro_rule["total_gold_count"] == 3
    assert micro_rule["gold_capture_rate_pct"] == 66.67
    assert micro_rule["false_positive_count"] == 1
    assert micro_rule["false_positive_rate_pct"] == 33.33


def test_build_backtest_payload_outputs_strategy_optimization():
    history = {
        "rows": {
            "gold-a": {
                "key": "gold-a",
                "recommendation_bucket": "shadow",
                "return_since_first_pct": 1200,
                "peak_mcap": 2_160_000,
                "first_snapshot": {"mcap": 180_000, "pair_age_hours": 4, "gold_dog_conviction_score": 88},
            },
            "gold-b": {
                "key": "gold-b",
                "recommendation_bucket": "shadow",
                "return_since_first_pct": 1000,
                "peak_mcap": 2_420_000,
                "first_snapshot": {"mcap": 220_000, "pair_age_hours": 8, "gold_dog_conviction_score": 82},
            },
            "noise": {
                "key": "noise",
                "recommendation_bucket": "shadow",
                "return_since_first_pct": -40,
                "first_snapshot": {"mcap": 240_000, "pair_age_hours": 6, "gold_dog_conviction_score": 90},
            },
        },
        "summary": {"tracked_count": 3, "actionable_count": 0, "hit_count": 0, "miss_count": 0, "hit_rate_pct": 0},
    }

    payload = backtest.build_backtest_payload(history, min_count=2)

    strategy = payload["strategy_optimization"]
    assert strategy["target"] == "single_pick_gold_dog"
    assert strategy["total_gold_count"] == 2
    assert strategy["primary_rule"]["gold_capture_rate_pct"] == 100.0
    assert strategy["recommendations"]


def test_gold_pick_audit_measures_caught_missed_and_wrong_gold_picks():
    rows = [
        {
            "key": "caught",
            "recommendation_bucket": "ambush",
            "return_since_first_pct": 1200,
            "peak_mcap": 2_600_000,
            "first_snapshot": {"mcap": 200_000},
        },
        {
            "key": "wrong",
            "recommendation_bucket": "ambush",
            "return_since_first_pct": 350,
            "first_snapshot": {"mcap": 200_000},
        },
        {
            "key": "missed",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1500,
            "peak_mcap": 3_200_000,
            "first_snapshot": {"mcap": 200_000},
        },
        {
            "key": "noise",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": -40,
            "first_snapshot": {"mcap": 200_000},
        },
    ]

    audit = backtest.gold_pick_audit(rows)

    assert audit["total_gold_count"] == 2
    assert audit["caught_gold_count"] == 1
    assert audit["missed_gold_count"] == 1
    assert audit["wrong_gold_pick_count"] == 1
    assert audit["gold_capture_rate_pct"] == 50.0
    assert audit["gold_pick_precision_pct"] == 50.0


def test_default_gold_threshold_requires_ten_x_return():
    rows = [
        {
            "symbol": "TWOBAGGER",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 200,
            "first_snapshot": {"mcap": 200_000},
        },
        {
            "symbol": "TENX",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 900,
            "peak_mcap": 2_000_000,
            "first_snapshot": {"mcap": 200_000},
        },
    ]

    missed = backtest.missed_gold_diagnostics(rows)

    assert [row["symbol"] for row in missed] == ["TENX"]
    assert backtest.GOLD_RETURN_PCT == 900.0


def test_gold_dog_outcome_requires_one_million_peak_market_cap():
    rows = [
        {
            "symbol": "TINY10X",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1200,
            "peak_mcap": 650_000,
            "first_snapshot": {"mcap": 50_000},
        },
        {
            "symbol": "REAL10X",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1200,
            "peak_mcap": 1_100_000,
            "first_snapshot": {"mcap": 80_000},
        },
    ]

    missed = backtest.missed_gold_diagnostics(rows)
    audit = backtest.gold_pick_audit(rows)

    assert [row["symbol"] for row in missed] == ["REAL10X"]
    assert audit["total_gold_count"] == 1
    assert backtest.GOLD_MIN_PEAK_MCAP == 1_000_000.0


def test_gold_pick_audit_separates_unverified_ten_x_when_market_cap_is_missing():
    rows = [
        {
            "symbol": "UNKNOWNCAP",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1200,
            "first_snapshot": {"pair_age_hours": 1},
        },
        {
            "symbol": "SMALLCAP",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1200,
            "peak_mcap": 600_000,
            "first_snapshot": {"mcap": 50_000},
        },
        {
            "symbol": "REALDOG",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": 1200,
            "peak_mcap": 1_500_000,
            "first_snapshot": {"mcap": 100_000},
        },
    ]

    audit = backtest.gold_pick_audit(rows)

    assert audit["ten_x_count"] == 3
    assert audit["ten_x_mcap_unknown_count"] == 1
    assert audit["ten_x_below_mcap_count"] == 1
    assert audit["total_gold_count"] == 1


def test_missed_gold_diagnostics_explain_why_gold_was_not_selected():
    rows = [
        {
            "symbol": "MISSED",
            "recommendation_bucket": "pullback",
            "hit_status": "hit",
            "return_since_first_pct": 980,
            "peak_mcap": 1_176_000,
            "return_1h_pct": 80,
            "first_snapshot": {
                "mcap": 120_000,
                "pair_age_hours": 8,
                "smart_money": 3,
                "kol": 0,
                "top10_holder_pct": 46,
                "gold_dog_conviction_score": 42,
                "source_labels": [],
            },
        },
        {
            "symbol": "HIT",
            "recommendation_bucket": "ambush",
            "hit_status": "hit",
            "return_since_first_pct": 1300,
            "peak_mcap": 1_400_000,
            "return_1h_pct": 30,
            "first_snapshot": {
                "mcap": 100_000,
                "pair_age_hours": 4,
                "smart_money": 30,
                "kol": 8,
                "top10_holder_pct": 18,
                "gold_dog_conviction_score": 84,
                "source_labels": ["GMGN", "DS"],
            },
        },
    ]

    missed = backtest.missed_gold_diagnostics(rows)

    assert len(missed) == 1
    assert missed[0]["symbol"] == "MISSED"
    assert "没有进入唯一主推" in missed[0]["miss_reason"]
    assert "确认分太低" in missed[0]["miss_reason"]
    assert "Top10偏高" in missed[0]["miss_reason"]
    assert "聪明钱/KOL不足" in missed[0]["miss_reason"]


def test_missed_gold_diagnostics_uses_peak_return_not_final_return():
    rows = [
        {
            "symbol": "ROUNDTRIP",
            "recommendation_bucket": "shadow",
            "return_since_first_pct": -35,
            "peak_return_pct": 1200,
            "peak_mcap": 2_340_000,
            "first_snapshot": {
                "mcap": 180_000,
                "pair_age_hours": 6,
                "smart_money": 28,
                "kol": 7,
                "top10_holder_pct": 20,
                "gold_dog_conviction_score": 82,
                "source_labels": ["GMGN", "DS"],
            },
        }
    ]

    missed = backtest.missed_gold_diagnostics(rows)

    assert len(missed) == 1
    assert missed[0]["return_since_first_pct"] == 1200


def test_history_window_flags_when_not_enough_for_one_year_backtest():
    history = {
        "rows": {
            "a": {"first_seen_at": "2026-08-14T00:00:00+08:00", "latest_seen_at": "2026-08-15T00:00:00+08:00"},
            "b": {"first_seen_at": "2026-08-14T12:00:00+08:00", "latest_seen_at": "2026-08-15T01:00:00+08:00"},
        }
    }

    window = backtest.history_window_summary(history, target_days=365)

    assert window["target_days"] == 365
    assert window["coverage_days"] < 2
    assert window["coverage_ok"] is False
    assert "不足一年" in window["status"]


def test_load_backtest_history_merges_imported_history_files(tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    (out_dir / "alpha-radar-replay-history.json").write_text(
        json.dumps({"rows": {"solana:live": {"key": "solana:live", "symbol": "LIVE"}}}),
        encoding="utf-8",
    )
    import_path = tmp_path / "import.json"
    import_path.write_text(
        json.dumps(
            [
                {
                    "symbol": "OLD",
                    "chain": "solana",
                    "token_address": "MintOld",
                    "first_seen_at": "2025-08-15T00:00:00+08:00",
                    "first_price_usd": 0.001,
                    "max_return_pct": 250,
                    "mcap": 120000,
                    "pair_age_hours": 4,
                }
            ]
        ),
        encoding="utf-8",
    )

    history = backtest.load_backtest_history(out_dir, [import_path])

    assert "solana:live" in history["rows"]
    assert "solana:mintold" in history["rows"]
    assert history["summary"]["tracked_count"] == 2


def test_load_backtest_history_merges_melt_data_dir(tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    (out_dir / "alpha-radar-replay-history.json").write_text(json.dumps({"rows": {}}), encoding="utf-8")
    data_dir = tmp_path / "MELT" / "data"
    (data_dir / "label").mkdir(parents=True)
    (data_dir / "memecoin").mkdir(parents=True)
    (data_dir / "label" / "label.csv").write_text(
        "mint_address,min_ratio,manipulated,return_ratio,label\n"
        "MintB,0.2,no,2.5,low\n",
        encoding="utf-8",
    )
    (data_dir / "memecoin" / "memecoin_list.jsonl").write_text(
        '{"token_address":"MintB","time":"2025-03-01T00:00:00Z"}\n',
        encoding="utf-8",
    )

    history = backtest.load_backtest_history(out_dir, [], [data_dir])

    assert "solana:mintb" in history["rows"]
    assert history["rows"]["solana:mintb"]["peak_return_pct"] == 250.0
