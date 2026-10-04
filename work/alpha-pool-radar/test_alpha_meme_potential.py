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


meme_potential = load_module("alpha_meme_potential", BASE / "alpha_meme_potential.py")


def test_classifies_real_meme_potential_as_ambush():
    row = {
        "symbol": "CATX",
        "score": 82,
        "heat_score": 45,
        "mcap": 3_800_000,
        "liquidity": 95_000,
        "volume24h": 1_900_000,
        "change_m5": 8,
        "change_h1": 42,
        "change_h24": 120,
        "txns24h": 680,
        "pair_age_hours": 18,
        "smart_money": 18,
        "kol": 5,
        "holders": 900,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "top10_holder_pct": 34,
        "max_holder_pct": 9,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "ambush"
    assert result["recommendation_action"] == "可小仓试探"
    assert "量市比" in result["recommendation_reason"]


def test_classifies_smart_money_meme_as_small_probe():
    row = {
        "symbol": "SMART",
        "score": 72,
        "heat_score": 32,
        "mcap": 680_000,
        "liquidity": 72_000,
        "volume24h": 1_300_000,
        "change_m5": 6,
        "change_h1": 48,
        "txns24h": 1400,
        "pair_age_hours": 6,
        "smart_money": 54,
        "kol": 12,
        "holders": 860,
        "gmgn_risk_flags": [],
        "top10_holder_pct": 18,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "ambush"
    assert result["recommendation_action"] == "可小仓试探"
    assert result["entry_score"] >= 65
    assert "聪明钱 54" in result["recommendation_reason"]


def test_gmgn_skills_exit_cluster_blocks_new_entry():
    row = {
        "symbol": "EXIT",
        "score": 80,
        "heat_score": 50,
        "mcap": 400_000,
        "liquidity": 60_000,
        "volume24h": 500_000,
        "change_m5": 4,
        "change_h1": 18,
        "change_h24": 60,
        "txns24h": 300,
        "pair_age_hours": 8,
        "smart_money": 20,
        "kol": 4,
        "holders": 600,
        "gmgn_skill_evidence": {
            "smartmoney_buy_wallet_count": 1,
            "smartmoney_sell_wallet_count": 2,
            "cluster_exit": True,
        },
    }
    result = meme_potential.classify_meme_row(row)
    assert result["recommendation_action"] == "聪明钱退出聚集"
    assert result["recommendation_bucket"] == "pullback"


def test_proficy_only_signal_stays_observation_even_with_hot_market_data():
    row = {
        "symbol": "PROF",
        "score": 88,
        "heat_score": 32,
        "mcap": 92_000,
        "liquidity": 31_000,
        "volume24h": 140_000,
        "change_m5": 8,
        "change_h1": 36,
        "change_h24": 120,
        "txns24h": 260,
        "pair_age_hours": 3,
        "smart_money": 30,
        "kol": 8,
        "holders": 900,
        "source_labels": ["Proficy", "DS", "Alpha_AI"],
        "sources": ["proficy_trending"],
        "top10_holder_pct": 18,
        "max_holder_pct": 4,
        "gmgn_risk_flags": [],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "仅观察"
    assert "缺 GMGN" in result["recommendation_reason"]


def test_985_only_signal_stays_observation_even_with_hot_market_data():
    row = {
        "symbol": "NINE",
        "score": 90,
        "heat_score": 34,
        "mcap": 76_000,
        "liquidity": 22_000,
        "volume24h": 180_000,
        "change_m5": 5,
        "change_h1": 28,
        "change_h24": 96,
        "txns24h": 310,
        "pair_age_hours": 2,
        "smart_money": 25,
        "kol": 4,
        "holders": 700,
        "source_labels": ["985", "DS", "Alpha_AI"],
        "sources": ["985_monitor"],
        "top10_holder_pct": 20,
        "max_holder_pct": 5,
        "gmgn_risk_flags": [],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "仅观察"
    assert "985/Proficy" in result["recommendation_reason"]
    assert "缺 GMGN" in result["recommendation_reason"]


def test_985_wallet_only_signals_stay_observation_even_with_hot_market_data():
    for source, label in (("985_fomo_wallets", "985 FOMO"), ("985_smartmoney", "985 SM")):
        row = {
            "symbol": source.upper(),
            "score": 92,
            "heat_score": 38,
            "mcap": 86_000,
            "liquidity": 26_000,
            "volume24h": 210_000,
            "change_m5": 6,
            "change_h1": 32,
            "change_h24": 110,
            "txns24h": 420,
            "pair_age_hours": 2,
            "smart_money": 30,
            "kol": 4,
            "holders": 740,
            "source_labels": [label, "DS", "Alpha_AI"],
            "sources": [source],
            "top10_holder_pct": 20,
            "max_holder_pct": 5,
            "gmgn_risk_flags": [],
        }

        result = meme_potential.classify_meme_row(row)

        assert result["recommendation_bucket"] == "pullback"
        assert result["recommendation_action"] == "仅观察"
        assert "缺 GMGN" in result["recommendation_reason"]


def test_wind_only_signal_stays_observation_even_with_hot_market_data():
    row = {
        "symbol": "WIND",
        "score": 91,
        "heat_score": 34,
        "mcap": 88_000,
        "liquidity": 28_000,
        "volume24h": 190_000,
        "change_m5": 6,
        "change_h1": 31,
        "change_h24": 105,
        "txns24h": 360,
        "pair_age_hours": 2,
        "smart_money": 28,
        "kol": 6,
        "holders": 810,
        "source_labels": ["听风", "DS", "Alpha_AI"],
        "sources": ["wind_monitor"],
        "top10_holder_pct": 19,
        "max_holder_pct": 5,
        "gmgn_risk_flags": [],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "仅观察"
    assert "985/Proficy/听风" in result["recommendation_reason"]
    assert "缺 GMGN" in result["recommendation_reason"]


def test_classifies_high_gmgn_risk_as_do_not_touch():
    row = {
        "symbol": "RISKY",
        "score": 96,
        "heat_score": 80,
        "mcap": 500_000,
        "liquidity": 80_000,
        "volume24h": 2_000_000,
        "change_m5": 12,
        "change_h1": 60,
        "txns24h": 1200,
        "smart_money": 90,
        "kol": 20,
        "gmgn_risk_flags": ["rug_50.0%", "sniper_51"],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "danger"
    assert result["recommendation_action"] == "风险太高别碰"
    assert "rug_50.0%" in result["recommendation_reason"]
    assert "rug_50.0%" in result["risk_deduction_reasons"]
    assert "rug_50.0%" in result["risk_deduction_summary"]


def test_entry_score_orders_better_entry_above_hotter_noise():
    rows = [
        {
            "symbol": "HOTNOISE",
            "score": 98,
            "heat_score": 75,
            "mcap": 700_000,
            "liquidity": 80_000,
            "volume24h": 3_000_000,
            "change_m5": 20,
            "change_h1": 110,
            "txns24h": 1800,
            "smart_money": 4,
            "kol": 0,
            "holders": 500,
        },
        {
            "symbol": "SMART",
            "score": 76,
            "heat_score": 35,
            "mcap": 900_000,
            "liquidity": 95_000,
            "volume24h": 1_700_000,
            "change_m5": 5,
            "change_h1": 36,
            "txns24h": 900,
            "smart_money": 48,
            "kol": 15,
            "holders": 1200,
        },
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert result[0]["symbol"] == "SMART"
    assert result[0]["entry_score"] > result[1]["entry_score"]


def test_narrative_score_rewards_current_meme_narratives():
    base = {
        "score": 20,
        "heat_score": 10,
        "mcap": 2_000_000,
        "liquidity": 60_000,
        "volume24h": 900_000,
        "change_m5": 5,
        "change_h1": 38,
        "txns24h": 300,
        "smart_money": 5,
        "kol": 1,
        "holders": 820,
        "top10_holder_pct": 18,
    }
    plain = {**base, "symbol": "PLAIN", "name": "Plain Token"}
    narrative = {**base, "symbol": "GROKDOG", "name": "Grok Dog AI"}

    result = meme_potential.classify_meme_row(narrative)

    assert result["narrative_score"] >= 20
    assert {"AI", "动物", "马斯克系"}.issubset(set(result["narrative_tags"]))
    assert result["entry_score"] > meme_potential.entry_score(plain)
    assert "叙事" in result["recommendation_reason"]
    assert result["narrative_quality"] == "strong"
    assert result["narrative_reasons"]


def test_keyword_stuffing_lowers_meme_narrative_score():
    base = {
        "score": 20,
        "heat_score": 10,
        "mcap": 2_000_000,
        "liquidity": 60_000,
        "volume24h": 900_000,
        "change_m5": 5,
        "change_h1": 38,
        "txns24h": 300,
        "smart_money": 0,
        "kol": 0,
        "holders": 820,
        "top10_holder_pct": 18,
    }
    stuffed = {
        **base,
        "symbol": "AIELONTRUMPPEPEDOGE100X",
        "name": "AI Elon Trump Pepe Doge 100x Pump",
        "description": "AI AI AI Elon Trump Pepe Doge moon 100x 1000x.",
    }
    focused = {
        **base,
        "symbol": "AIPET",
        "name": "AI Pet",
        "description": "A tiny AI pet that replies to holders.",
        "twitter": "https://x.com/aipet",
        "smart_money": 12,
        "kol": 4,
    }

    stuffed_result = meme_potential.classify_meme_row(stuffed)
    focused_result = meme_potential.classify_meme_row(focused)

    assert "关键词堆砌" in stuffed_result["narrative_penalty_tags"]
    assert "关键词堆砌" in stuffed_result["risk_deduction_summary"]
    assert focused_result["narrative_score"] > stuffed_result["narrative_score"]


def test_entry_score_orders_strong_narrative_above_plain_same_metrics():
    rows = [
        {
            "symbol": "PLAIN",
            "name": "Plain Token",
            "score": 20,
            "heat_score": 10,
            "mcap": 2_000_000,
            "liquidity": 60_000,
            "volume24h": 900_000,
            "change_m5": 5,
            "change_h1": 38,
            "txns24h": 300,
            "smart_money": 5,
            "kol": 1,
            "holders": 820,
            "top10_holder_pct": 18,
        },
        {
            "symbol": "GROKDOG",
            "name": "Grok Dog AI",
            "score": 20,
            "heat_score": 10,
            "mcap": 2_000_000,
            "liquidity": 60_000,
            "volume24h": 900_000,
            "change_m5": 5,
            "change_h1": 38,
            "txns24h": 300,
            "smart_money": 5,
            "kol": 1,
            "holders": 820,
            "top10_holder_pct": 18,
        },
    ]

    result = meme_potential.build_meme_potential_rows(rows, include_rejects=True)

    assert result[0]["symbol"] == "GROKDOG"
    assert result[0]["entry_score"] > result[1]["entry_score"]


def test_pure_heat_without_pro_signal_waits_for_confirmation():
    row = {
        "symbol": "HOTNOISE",
        "name": "Hot Noise",
        "score": 92,
        "heat_score": 70,
        "mcap": 850_000,
        "liquidity": 90_000,
        "volume24h": 1_800_000,
        "change_m5": 8,
        "change_h1": 42,
        "txns24h": 1200,
        "smart_money": 0,
        "kol": 0,
        "holders": 900,
        "source_labels": ["DS"],
        "top10_holder_pct": 18,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["pro_signal_score"] < 45
    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "等回踩"
    assert "专业信号" in result["recommendation_reason"]


def test_smart_kol_narrative_convergence_gets_small_probe():
    row = {
        "symbol": "GROKDOG",
        "name": "Grok Dog AI",
        "score": 62,
        "heat_score": 24,
        "mcap": 1_200_000,
        "liquidity": 85_000,
        "volume24h": 1_100_000,
        "change_m5": 6,
        "change_h1": 44,
        "txns24h": 760,
        "smart_money": 26,
        "kol": 8,
        "holders": 1400,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "potential_label": "10x+",
        "top10_holder_pct": 19,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["pro_signal_score"] >= 60
    assert result["pro_signal_score"] < 95
    assert result["entry_score"] < 95
    assert result["recommendation_bucket"] == "ambush"
    assert result["recommendation_action"] == "可小仓试探"
    assert "聪明钱+KOL共振" in result["recommendation_reason"]


def test_debot_source_adds_resonance_to_pro_and_backtest_scores():
    base = {
        "symbol": "BSCM",
        "name": "BSC Meme",
        "score": 58,
        "heat_score": 24,
        "mcap": 120_000,
        "liquidity": 35_000,
        "volume24h": 180_000,
        "change_m5": 3,
        "change_h1": 18,
        "txns24h": 420,
        "pair_age_hours": 3,
        "smart_money": 8,
        "kol": 2,
        "holders": 520,
        "top10_holder_pct": 19,
        "source_count": 2,
    }
    without_debot = {**base, "source_labels": ["GMGN", "DS"], "sources": ["gmgn_live_trending", "profile_latest"]}
    with_debot = {
        **base,
        "source_labels": ["GMGN", "DS", "DeBot"],
        "sources": ["gmgn_live_trending", "profile_latest", "debot_signal"],
        "source_count": 3,
    }

    assert meme_potential.pro_signal_score(with_debot) > meme_potential.pro_signal_score(without_debot)
    assert meme_potential.backtest_profile_score(with_debot) > meme_potential.backtest_profile_score(without_debot)


def test_repeated_okx_trenches_adds_intensity_but_not_independent_source_count():
    base = {
        "symbol": "AITOKEN",
        "name": "AI Token",
        "score": 58,
        "heat_score": 24,
        "mcap": 90_000,
        "liquidity": 32_000,
        "volume24h": 160_000,
        "change_m5": 2,
        "change_h1": 16,
        "txns24h": 360,
        "pair_age_hours": 2,
        "smart_money": 4,
        "kol": 1,
        "holders": 420,
        "top10_holder_pct": 21,
        "source_labels": ["OKX", "DS", "Alpha_AI"],
        "source_count": 3,
    }
    single_okx = {**base, "sources": ["okx_trenches"], "source_hit_counts": {"okx_trenches": 1}, "source_repeat_score": 0}
    repeated_okx = {**base, "sources": ["okx_trenches"] * 8, "source_hit_counts": {"okx_trenches": 8}, "source_repeat_score": 24}

    assert repeated_okx["source_count"] == 3
    assert meme_potential.independent_source_count(repeated_okx) == 1
    assert meme_potential.pro_signal_score(repeated_okx) > meme_potential.pro_signal_score(single_okx)
    assert meme_potential.gmgn_skill_score(repeated_okx) > meme_potential.gmgn_skill_score(single_okx)
    assert meme_potential.backtest_profile_score(repeated_okx) > meme_potential.backtest_profile_score(single_okx)
    assert "Trenches重复命中x8" in meme_potential.gmgn_skill_tags(repeated_okx)


def test_display_labels_do_not_inflate_heat_confirmation_sources():
    row = {
        "symbol": "ONE",
        "mcap": 100_000,
        "liquidity": 30_000,
        "volume24h": 250_000,
        "change_h1": 80,
        "heat_score": 120,
        "sources": ["proficy_trending"],
        "source_groups": ["proficy"],
        "source_labels": ["GMGN", "Proficy", "DS", "Alpha_AI"],
        "source_count": 4,
    }

    annotation = meme_potential.meme_heat_annotation(row)

    assert meme_potential.source_groups(row) == {"proficy"}
    assert annotation["heat_priority"] == ""
    assert "源" not in annotation["heat_priority_reason"]


def test_okx_signal_panel_scores_as_confirmation_not_trenches_entry():
    base = {
        "symbol": "OKXGEM",
        "score": 76,
        "heat_score": 36,
        "mcap": 180_000,
        "liquidity": 55_000,
        "volume24h": 260_000,
        "change_m5": 4,
        "change_h1": 24,
        "txns24h": 520,
        "pair_age_hours": 3,
        "smart_money": 14,
        "kol": 4,
        "holders": 760,
        "top10_holder_pct": 24,
        "source_labels": ["OKX", "DS", "Alpha_AI"],
        "source_count": 3,
    }
    trenches = {**base, "sources": ["okx_trenches", "profile_latest"]}
    signal = {**base, "sources": ["okx_signal", "profile_latest"]}

    assert meme_potential.independent_source_count(trenches) == 2
    assert meme_potential.independent_source_count(signal) == 2
    assert meme_potential.pro_signal_score(signal) > meme_potential.pro_signal_score(trenches)
    assert meme_potential.gmgn_skill_score(signal) > meme_potential.gmgn_skill_score(trenches)


def test_binance_wallet_hot_and_signal_score_as_meme_sources():
    base = {
        "symbol": "BWGEM",
        "score": 72,
        "heat_score": 38,
        "mcap": 70_000,
        "liquidity": 28_000,
        "volume24h": 210_000,
        "change_m5": 4,
        "change_h1": 21,
        "txns24h": 480,
        "pair_age_hours": 2,
        "smart_money": 12,
        "kol": 3,
        "holders": 650,
        "top10_holder_pct": 23,
        "source_labels": ["币安钱包", "DS", "Alpha_AI"],
        "source_count": 2,
    }
    hot = {**base, "sources": ["binance_wallet_hot", "profile_latest"]}
    signal = {**base, "sources": ["binance_wallet_signal", "profile_latest"]}

    assert meme_potential.independent_source_count({"sources": ["binance_wallet_hot", "binance_wallet_signal"]}) == 1
    assert "币安钱包入口" in meme_potential.gmgn_skill_categories(hot)
    assert "新币入口" in meme_potential.gmgn_skill_categories(hot)
    assert "币安钱包热榜" in meme_potential.gmgn_skill_tags(hot)
    assert "币安钱包信号" in meme_potential.gmgn_skill_tags(signal)
    assert meme_potential.pro_signal_score(signal) > meme_potential.pro_signal_score(hot)
    assert meme_potential.gmgn_skill_score(signal) > 0


def test_new_gmgn_skills_sources_are_classified_by_role():
    row = {
        "sources": ["gmgn_skills_signal", "gmgn_skills_trenches", "gmgn_skills_trending", "gmgn_skills_kol"],
        "source_labels": ["GMGN"],
        "mcap": 25_000,
        "pair_age_hours": 1,
        "gmgn_skill_evidence": {
            "positive_signal": True,
            "exit_signal": False,
            "trenches_stages": ["near_completion"],
            "kol_buy_wallet_count": 2,
            "hot_search_rank": 6,
        },
    }

    categories = meme_potential.gmgn_skill_categories(row)
    tags = meme_potential.gmgn_skill_tags(row)

    assert "热榜趋势" in categories
    assert "新币入口" in categories
    assert "Skills资金信号" in categories
    assert "Skills KOL流" in categories
    assert "GMGN Signals买入" in tags
    assert "Trenches新币入口" in tags
    assert meme_potential.gmgn_skill_score(row) > 20


def test_bsc_stock_meme_is_priority_battlefield():
    row = {
        "symbol": "股狗",
        "name": "BSC Stock Dog",
        "chain": "bsc",
        "score": 72,
        "heat_score": 32,
        "mcap": 64_000,
        "liquidity": 24_000,
        "volume24h": 160_000,
        "change_m5": 5,
        "change_h1": 24,
        "txns24h": 410,
        "pair_age_hours": 2,
        "smart_money": 12,
        "kol": 4,
        "holders": 620,
        "top10_holder_pct": 22,
        "max_holder_pct": 6,
        "source_labels": ["币安钱包", "DS", "Alpha_AI"],
        "sources": ["binance_wallet_hot", "profile_latest"],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["priority_battlefield"] == "binance_bsc_stock_meme"
    assert result["priority_battlefield_label"] == "币安币股想象"
    assert result["priority_battlefield_score"] == 0
    assert "上所想象" in result["gmgn_skill_categories"]
    assert "币安币股想象" not in result["gmgn_skill_tags"]
    assert any(reason.startswith("后验催化:") for reason in result["gold_dog_rationale"])


def test_robinhood_okx_signal_is_priority_battlefield():
    row = {
        "symbol": "HOODDOG",
        "name": "Robinhood Dog",
        "chain": "robinhood",
        "score": 76,
        "heat_score": 35,
        "mcap": 82_000,
        "liquidity": 36_000,
        "volume24h": 220_000,
        "change_m5": 6,
        "change_h1": 31,
        "txns24h": 540,
        "pair_age_hours": 3,
        "smart_money": 16,
        "kol": 5,
        "holders": 760,
        "top10_holder_pct": 24,
        "max_holder_pct": 7,
        "source_labels": ["OKX", "GMGN", "DS", "Alpha_AI"],
        "sources": ["okx_signal", "gmgn_trenches", "profile_latest"],
        "okx_source_panel": "signal",
    }

    result = meme_potential.classify_meme_row(row)

    assert result["priority_battlefield"] == "okx_robinhood_meme"
    assert result["priority_battlefield_label"] == "OKX罗宾汉想象"
    assert result["priority_battlefield_score"] == 0
    assert "上所想象" in result["gmgn_skill_categories"]
    assert "OKX罗宾汉想象" not in result["gmgn_skill_tags"]
    assert any("OKX罗宾汉" in reason for reason in result["priority_battlefield_reasons"])


def test_gold_dog_candidate_outputs_five_factor_scores():
    row = {
        "symbol": "GROKDOG",
        "name": "Grok Dog AI",
        "score": 82,
        "heat_score": 38,
        "mcap": 800_000,
        "liquidity": 95_000,
        "volume24h": 1_100_000,
        "change_m5": 5,
        "change_h1": 42,
        "txns24h": 900,
        "pair_age_hours": 8,
        "smart_money": 32,
        "kol": 9,
        "holders": 1200,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "source_count": 3,
        "potential_label": "10x+",
        "top10_holder_pct": 22,
        "max_holder_pct": 8,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["gold_dog_score"] >= 70
    assert result["freshness_score"] >= 80
    assert result["pro_signal_score"] >= 60
    assert result["narrative_score"] >= 20
    assert result["risk_filter_score"] >= 75
    assert result["absorption_score"] >= 60


def test_gold_dog_candidate_outputs_trenchkit_style_factor_scores():
    row = {
        "symbol": "GROKDOG",
        "name": "Grok Dog AI",
        "description": "An AI dog agent with active holders.",
        "twitter": "https://x.com/grokdog",
        "score": 82,
        "heat_score": 38,
        "mcap": 800_000,
        "liquidity": 95_000,
        "volume24h": 1_100_000,
        "change_m5": 5,
        "change_h1": 42,
        "txns24h": 900,
        "pair_age_hours": 8,
        "smart_money": 32,
        "kol": 9,
        "holders": 1200,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "source_count": 3,
        "potential_label": "10x+",
        "top10_holder_pct": 22,
        "max_holder_pct": 8,
        "gmgn_risk_flags": [],
        "dev_graduation_rate": 0.36,
        "dev_ath_mcap": 1_400_000,
        "dev_created_token_count": 4,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["holder_quality_score"] >= 80
    assert result["security_filter_score"] >= 85
    assert result["liquidity_age_score"] >= 70
    assert result["dev_trust_score"] >= 60
    assert result["smart_kol_score"] >= 65
    assert result["bot_manipulation_score"] >= 80
    assert result["gold_dog_conviction_score"] >= 75
    assert result["entry_stage"] == "新池"
    assert result["entry_level"] in {"强候选", "可试探"}
    assert result["gold_dog_rationale"]


def test_gold_dog_candidate_outputs_cliffwatch_style_summary_scores():
    row = {
        "symbol": "GEMDOG",
        "name": "Gem Dog AI",
        "description": "AI dog agent with strong community holders.",
        "score": 88,
        "heat_score": 76,
        "mcap": 180_000,
        "liquidity": 82_000,
        "volume24h": 620_000,
        "change_m5": 8,
        "change_h1": 36,
        "txns24h": 780,
        "pair_age_hours": 4,
        "smart_money": 36,
        "kol": 8,
        "holders": 1300,
        "source_labels": ["GMGN", "DeBot", "Pump.fun"],
        "sources": ["gmgn_live_trending", "debot_signal", "pumpfun_live"],
        "source_count": 3,
        "top10_holder_pct": 19,
        "max_holder_pct": 6,
        "gmgn_risk_flags": [],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["cliff_risk_score"] <= 25
    assert result["cliff_risk_label"] == "低"
    assert result["cliff_hype_score"] >= 60
    assert result["cliff_hype_label"] in {"良好", "强"}
    assert result["cliff_gem_score"] >= 70
    assert result["cliff_gem_label"] in {"良好", "强"}


def test_pumpfun_onchain_create_is_first_layer_pending_not_confirmed_ticket():
    row = {
        "symbol": "MINT01",
        "name": "Pump.fun MINT01",
        "score": 44,
        "heat_score": 44,
        "mcap": 0,
        "liquidity": 0,
        "volume24h": 0,
        "txns24h": 0,
        "pair_age_hours": 0.001,
        "source_labels": ["Pump.fun"],
        "sources": ["pumpfun_onchain"],
        "source_count": 1,
        "market_data_pending": True,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "链上首发"
    assert "不是确认票" in result["recommendation_risk"]
    assert result["entry_stage"] == "新池"


def test_pending_launchpad_rows_do_not_enter_front_potential_pool():
    rows = [
        {
            "symbol": "WAIT01",
            "name": "Four.meme WAIT01",
            "chain": "bsc",
            "score": 48,
            "heat_score": 48,
            "mcap": 0,
            "liquidity": 0,
            "volume24h": 0,
            "txns24h": 0,
            "pair_age_hours": 0.001,
            "source_labels": ["Four.meme"],
            "sources": ["fourmeme_launchpad"],
            "source_count": 1,
            "market_data_pending": True,
        }
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert result == []


def test_near_graduation_launchpad_row_is_early_observation_not_confirmed_ticket():
    row = {
        "symbol": "FAST",
        "name": "Fast Curve",
        "chain": "bsc",
        "score": 60,
        "heat_score": 70,
        "mcap": 0,
        "liquidity": 0,
        "volume24h": 0,
        "txns24h": 0,
        "pair_age_hours": 0.001,
        "source_labels": ["Four.meme"],
        "sources": ["fourmeme_launchpad"],
        "source_count": 1,
        "market_data_pending": True,
        "launchpad_lifecycle_stage": "near_graduation",
        "launchpad_stage_label": "曲线快毕业",
        "bonding_curve_progress_pct": 96.2,
        "purchase_count": 7,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "曲线快毕业"
    assert "关键区间" in result["recommendation_reason"]
    assert "不是确认票" in result["recommendation_risk"]
    assert any(reason.startswith("发射台:") for reason in result["gold_dog_rationale"])
    assert meme_potential.build_meme_potential_rows([row]) == []


def test_cliffwatch_style_gem_score_is_capped_by_hard_risk():
    row = {
        "symbol": "HOTRUG",
        "name": "Hot Rug AI",
        "score": 94,
        "heat_score": 95,
        "mcap": 120_000,
        "liquidity": 74_000,
        "volume24h": 920_000,
        "change_m5": 18,
        "change_h1": 80,
        "txns24h": 1600,
        "pair_age_hours": 2,
        "smart_money": 40,
        "kol": 12,
        "holders": 500,
        "source_labels": ["GMGN", "DeBot"],
        "source_count": 2,
        "top10_holder_pct": 66,
        "max_holder_pct": 24,
        "gmgn_risk_flags": ["rug_score:92", "bundler_pct:38"],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["cliff_risk_score"] >= 45
    assert result["cliff_risk_label"] in {"偏高", "高危"}
    assert result["cliff_hype_score"] >= 70
    assert result["cliff_gem_score"] <= 45


def test_gmgn_skill_market_style_signals_are_exposed():
    row = {
        "symbol": "SKILLDOG",
        "name": "Skill Dog AI",
        "score": 76,
        "heat_score": 42,
        "mcap": 82_000,
        "liquidity": 46_000,
        "volume24h": 240_000,
        "change_m5": 7,
        "change_h1": 48,
        "txns24h": 620,
        "pair_age_hours": 3,
        "smart_money": 34,
        "kol": 8,
        "holders": 1180,
        "source_labels": ["GMGN", "DS"],
        "sources": ["gmgn_live_trending", "gmgn_trenches", "dexscreener_boosts"],
        "source_count": 3,
        "potential_label": "10x+",
        "top10_holder_pct": 18,
        "max_holder_pct": 7,
        "gmgn_risk_flags": [],
    }

    result = meme_potential.classify_meme_row(row)

    assert result["gmgn_skill_score"] >= 70
    assert "GMGN实时热榜" in result["gmgn_skill_tags"]
    assert "Trenches新币入口" in result["gmgn_skill_tags"]
    assert "早期候选" in result["gmgn_skill_tags"]
    assert "聪明钱" in result["gmgn_skill_categories"]
    assert "GMGN技能画像:热榜+聪明钱+早期池" in result["backtest_rule_tags"]
    assert any(reason.startswith("GMGN技能:") for reason in result["gold_dog_rationale"])


def test_missing_pair_age_is_information_incomplete_not_small_probe():
    row = {
        "symbol": "NOAGE",
        "name": "Grok Dog AI",
        "score": 92,
        "heat_score": 58,
        "mcap": 900_000,
        "liquidity": 180_000,
        "volume24h": 1_500_000,
        "change_m5": 4,
        "change_h1": 38,
        "txns24h": 1800,
        "pair_age_hours": None,
        "smart_money": 70,
        "kol": 22,
        "holders": 1800,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "source_count": 3,
        "potential_label": "100x+",
        "top10_holder_pct": 18,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "信息不全"
    assert result["freshness_score"] == 0


def test_build_meme_potential_rows_filters_missing_age_by_default():
    rows = [
        {"symbol": "NEW", "name": "Grok Dog AI", "score": 80, "heat_score": 38, "mcap": 1_000_000, "liquidity": 90_000, "volume24h": 1_200_000, "change_h1": 35, "txns24h": 700, "pair_age_hours": 5, "smart_money": 26, "kol": 8, "holders": 900, "source_labels": ["GMGN", "DS", "Alpha_AI"], "source_count": 3, "potential_label": "10x+"},
        {"symbol": "NOAGE", "name": "Grok Dog AI", "score": 98, "heat_score": 70, "mcap": 1_000_000, "liquidity": 90_000, "volume24h": 1_200_000, "change_h1": 35, "txns24h": 700, "pair_age_hours": None, "smart_money": 70, "kol": 22, "holders": 900, "source_labels": ["GMGN", "DS", "Alpha_AI"], "source_count": 3, "potential_label": "100x+"},
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert [row["symbol"] for row in result] == ["NEW"]


def test_only_extreme_pro_convergence_reaches_top_score_band():
    row = {
        "symbol": "GROKDOG",
        "name": "Grok Dog AI",
        "score": 92,
        "heat_score": 58,
        "mcap": 420_000,
        "liquidity": 160_000,
        "volume24h": 1_300_000,
        "change_m5": 7,
        "change_h1": 48,
        "txns24h": 2600,
        "smart_money": 70,
        "kol": 22,
        "holders": 1800,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "source_count": 3,
        "potential_label": "100x+",
        "top10_holder_pct": 18,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["pro_signal_score"] >= 95
    assert result["entry_score"] >= 95
    assert result["recommendation_bucket"] == "ambush"


def test_classifies_overheated_meme_as_pullback():
    row = {
        "symbol": "HOTDOG",
        "score": 95,
        "heat_score": 60,
        "mcap": 9_000_000,
        "liquidity": 210_000,
        "volume24h": 8_000_000,
        "change_m5": 72,
        "change_h1": 260,
        "change_h24": 900,
        "txns24h": 2000,
        "pair_age_hours": 12,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "太热别追"


def test_classifies_concentrated_meme_as_danger():
    row = {
        "symbol": "WHALE",
        "score": 88,
        "mcap": 2_000_000,
        "liquidity": 80_000,
        "volume24h": 1_500_000,
        "change_h1": 40,
        "pair_age_hours": 30,
        "top10_holder_pct": 68,
        "max_holder_pct": 24,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "danger"
    assert result["recommendation_action"] == "风险太高别碰"


def test_old_meme_pool_revival_is_watch_pullback_not_small_probe():
    row = {
        "symbol": "OLDMEME",
        "name": "Old Dog AI",
        "score": 92,
        "heat_score": 58,
        "mcap": 900_000,
        "liquidity": 180_000,
        "volume24h": 1_500_000,
        "change_m5": 4,
        "change_h1": 38,
        "txns24h": 1800,
        "pair_age_hours": 260,
        "smart_money": 70,
        "kol": 22,
        "holders": 1800,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "source_count": 3,
        "potential_label": "100x+",
        "top10_holder_pct": 18,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "老币复活盯回踩"
    assert result["entry_stage"] == "老币复活"
    assert result["old_meme_revival_active"] is True
    assert "老池复活首爆" in result["gmgn_skill_tags"]


def test_ordolike_four_day_revival_is_separate_from_new_pool():
    row = {
        "symbol": "ORDO",
        "name": "OrdoFi",
        "chain": "bsc",
        "score": 82,
        "heat_score": 44,
        "mcap": 2_790_000,
        "liquidity": 197_000,
        "volume24h": 665_700,
        "change_m5": 0,
        "change_h1": 16.75,
        "change_h24": 64,
        "txns24h": 980,
        "pair_age_hours": 96,
        "smart_money": 16,
        "kol": 4,
        "holders": 2200,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "sources": ["gmgn_live_trending", "profile_latest"],
        "source_count": 2,
        "top10_holder_pct": 25,
        "max_holder_pct": 8,
    }

    result = meme_potential.classify_meme_row(row)

    assert result["recommendation_bucket"] == "pullback"
    assert result["recommendation_action"] == "老币复活盯回踩"
    assert result["entry_stage"] == "老币复活"
    assert result["old_meme_revival_score"] >= 58
    assert any("多源" in reason for reason in result["old_meme_revival_reasons"])


def test_build_meme_potential_rows_filters_very_old_pools_by_default():
    rows = [
        {"symbol": "NEW", "name": "Grok Dog AI", "score": 80, "heat_score": 38, "mcap": 1_000_000, "liquidity": 90_000, "volume24h": 1_200_000, "change_h1": 35, "txns24h": 700, "pair_age_hours": 5, "smart_money": 26, "kol": 8, "holders": 900, "source_labels": ["GMGN", "DS", "Alpha_AI"], "source_count": 3, "potential_label": "10x+"},
        {"symbol": "OLD", "name": "Grok Dog AI", "score": 48, "heat_score": 12, "mcap": 12_000_000, "liquidity": 8_000, "volume24h": 35_000, "change_h1": 4, "txns24h": 30, "pair_age_hours": 260, "smart_money": 0, "kol": 0, "holders": 900, "source_labels": ["GMGN"], "source_count": 1},
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert [row["symbol"] for row in result] == ["NEW"]


def test_build_meme_potential_rows_keeps_only_six_hour_new_pool_window():
    base = {
        "score": 88,
        "heat_score": 42,
        "mcap": 180_000,
        "liquidity": 42_000,
        "volume24h": 360_000,
        "change_m5": 6,
        "change_h1": 28,
        "txns24h": 520,
        "smart_money": 18,
        "kol": 5,
        "source_labels": ["GMGN", "DS"],
        "quote_status": "fresh",
    }
    rows = [
        {**base, "symbol": "CURRENT", "pair_age_hours": 5.9},
        {**base, "symbol": "TOO_OLD", "pair_age_hours": 6.1},
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert [row["symbol"] for row in result] == ["CURRENT"]


def test_build_meme_potential_rows_rejects_explicitly_stale_quotes():
    base = {
        "score": 88,
        "heat_score": 42,
        "mcap": 180_000,
        "liquidity": 42_000,
        "volume24h": 360_000,
        "change_m5": 6,
        "change_h1": 28,
        "txns24h": 520,
        "pair_age_hours": 3,
        "smart_money": 18,
        "kol": 5,
        "source_labels": ["GMGN", "DS"],
    }
    rows = [
        {**base, "symbol": "FRESH", "quote_status": "fresh"},
        {**base, "symbol": "STALE", "quote_status": "stale"},
        {**base, "symbol": "NO_QUOTE", "quote_status": "unavailable"},
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert [row["symbol"] for row in result] == ["FRESH"]


def test_build_meme_potential_rows_keeps_actionable_old_revival():
    rows = [
        {"symbol": "NEW", "name": "Grok Dog AI", "score": 80, "heat_score": 38, "mcap": 1_000_000, "liquidity": 90_000, "volume24h": 1_200_000, "change_h1": 35, "txns24h": 700, "pair_age_hours": 18, "smart_money": 26, "kol": 8, "holders": 900, "source_labels": ["GMGN", "DS", "Alpha_AI"], "source_count": 3, "potential_label": "10x+"},
        {"symbol": "REVIVE", "name": "Old Runner", "score": 86, "heat_score": 44, "mcap": 900_000, "liquidity": 140_000, "volume24h": 650_000, "change_h1": 30, "txns24h": 900, "pair_age_hours": 300, "smart_money": 18, "kol": 4, "holders": 1600, "source_labels": ["GMGN", "DS", "OKX信号"], "sources": ["gmgn_live_trending", "okx_signal"], "source_count": 3, "top10_holder_pct": 20, "max_holder_pct": 7},
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert "REVIVE" in [row["symbol"] for row in result]
    assert next(row for row in result if row["symbol"] == "REVIVE")["old_meme_revival_active"] is True


def test_build_meme_potential_rows_keeps_only_actionable_front_rows():
    rows = [
        {"symbol": "GOOD", "name": "Grok Dog AI", "score": 80, "heat_score": 38, "mcap": 5_000_000, "liquidity": 90_000, "volume24h": 2_000_000, "change_h1": 35, "txns24h": 500, "pair_age_hours": 5, "smart_money": 26, "kol": 8, "holders": 900, "source_labels": ["GMGN", "DS", "Alpha_AI"], "source_count": 3, "potential_label": "10x+"},
        {"symbol": "HOT", "score": 98, "mcap": 5_000_000, "liquidity": 90_000, "volume24h": 4_000_000, "change_h1": 240, "change_m5": 80, "txns24h": 900, "pair_age_hours": 6},
        {"symbol": "BAD", "score": 30, "mcap": 40_000_000, "liquidity": 1_500, "volume24h": 20_000, "change_h1": 1, "txns24h": 5, "pair_age_hours": 500},
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert [row["symbol"] for row in result] == ["GOOD", "HOT"]
    assert result[0]["recommendation_label"] == "上车候选"
    assert result[1]["recommendation_bucket"] == "pullback"


def test_build_meme_potential_rows_can_scope_to_bsc_only():
    rows = [
        {"symbol": "BSCWIN", "chain": "bsc", "score": 88, "heat_score": 45, "mcap": 220_000, "liquidity": 45_000, "volume24h": 420_000, "change_h1": 28, "txns24h": 640, "pair_age_hours": 3, "source_labels": ["GMGN", "DS"], "source_count": 2},
        {"symbol": "SOLWIN", "chain": "solana", "score": 99, "heat_score": 60, "mcap": 180_000, "liquidity": 60_000, "volume24h": 700_000, "change_h1": 40, "txns24h": 900, "pair_age_hours": 2, "source_labels": ["GMGN", "DS"], "source_count": 2},
    ]

    result = meme_potential.build_meme_potential_rows(rows, chain_scope="bsc")

    assert [row["symbol"] for row in result] == ["BSCWIN"]


def test_build_meme_potential_rows_caps_default_list_to_eight_slots():
    rows = [
        {
            "symbol": f"GOOD{i}",
            "score": 92 - i,
            "heat_score": 45,
            "mcap": 4_000_000,
            "liquidity": 120_000,
            "volume24h": 2_000_000,
            "change_h1": 35,
            "txns24h": 600,
            "pair_age_hours": 5,
        }
        for i in range(12)
    ]

    result = meme_potential.build_meme_potential_rows(rows)

    assert len(result) == 8
    assert result[0]["symbol"] == "GOOD0"
    assert result[-1]["symbol"] == "GOOD7"


def test_build_meme_potential_rows_can_include_rejects_for_detail_use():
    rows = [
        {"symbol": "BAD", "score": 30, "mcap": 40_000_000, "liquidity": 1_500, "volume24h": 20_000, "change_h1": 1, "txns24h": 5, "pair_age_hours": 500},
    ]

    result = meme_potential.build_meme_potential_rows(rows, include_rejects=True)

    assert result[0]["symbol"] == "BAD"
    assert result[0]["recommendation_bucket"] == "reject"


def test_replay_calibration_keeps_true_gold_dog_candidate_action():
    row = {
        "symbol": "SMART",
        "score": 72,
        "heat_score": 32,
        "mcap": 680_000,
        "liquidity": 72_000,
        "volume24h": 1_300_000,
        "change_m5": 6,
        "change_h1": 48,
        "txns24h": 1400,
        "pair_age_hours": 6,
        "smart_money": 54,
        "kol": 12,
        "holders": 860,
        "gmgn_risk_flags": [],
        "top10_holder_pct": 18,
    }
    base = meme_potential.classify_meme_row(row)
    calibration = {
        base["recommendation_action"]: {
            "score_adjustment": -12,
            "risk_level": "weak",
            "reason": "回测拖累：1h均值-5.80%，胜率37.5%",
        }
    }

    adjusted = meme_potential.apply_replay_calibration(base, calibration)

    assert adjusted["entry_score"] == base["entry_score"]
    assert adjusted["recommendation_bucket"] == "ambush"
    assert adjusted["recommendation_action"] == base["recommendation_action"]
    assert adjusted["gold_dog_conviction_score"] >= 70
    assert adjusted["replay_score_adjustment"] == -12
    assert "回测拖累" in adjusted["recommendation_reason"]


def test_build_meme_potential_rows_prioritizes_backtested_gold_dog_profile():
    strong_profile = {
        "symbol": "EDGE",
        "score": 66,
        "heat_score": 30,
        "mcap": 380_000,
        "liquidity": 72_000,
        "volume24h": 580_000,
        "change_m5": 4,
        "change_h1": 28,
        "txns24h": 1300,
        "pair_age_hours": 5,
        "smart_money": 35,
        "kol": 8,
        "holders": 720,
        "gmgn_risk_flags": [],
        "top10_holder_pct": 18,
        "source_labels": ["GMGN", "DS"],
        "source_count": 2,
    }
    noisy_unknown = {
        "symbol": "NOISE",
        "score": 82,
        "heat_score": 45,
        "mcap": 8_000_000,
        "liquidity": 600_000,
        "volume24h": 9_000_000,
        "change_m5": 15,
        "change_h1": 72,
        "txns24h": 6000,
        "pair_age_hours": 90,
        "smart_money": 4,
        "kol": 0,
        "holders": 6000,
        "gmgn_risk_flags": [],
        "top10_holder_pct": 45,
    }

    result = meme_potential.build_meme_potential_rows([noisy_unknown, strong_profile])

    assert result[0]["symbol"] == "EDGE"
    assert result[0]["backtest_profile_score"] > result[1]["backtest_profile_score"]


def test_build_meme_potential_rows_prioritizes_backtested_early_gold_dog_rule():
    early_gold_profile = {
        "symbol": "EARLY",
        "score": 68,
        "heat_score": 28,
        "mcap": 260_000,
        "liquidity": 58_000,
        "volume24h": 360_000,
        "change_m5": 4,
        "change_h1": 26,
        "txns24h": 900,
        "pair_age_hours": 4,
        "smart_money": 34,
        "kol": 8,
        "holders": 760,
        "top10_holder_pct": 19,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "source_count": 3,
        "gold_dog_conviction_score": 88,
    }
    later_hot_profile = {
        "symbol": "BIGGER",
        "score": 88,
        "heat_score": 55,
        "mcap": 2_800_000,
        "liquidity": 260_000,
        "volume24h": 2_200_000,
        "change_m5": 8,
        "change_h1": 48,
        "txns24h": 3800,
        "pair_age_hours": 5,
        "smart_money": 40,
        "kol": 10,
        "holders": 2200,
        "top10_holder_pct": 21,
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "source_count": 3,
        "gold_dog_conviction_score": 88,
    }

    result = meme_potential.build_meme_potential_rows([later_hot_profile, early_gold_profile])

    assert result[0]["symbol"] == "EARLY"
    assert result[0]["backtest_rule_score"] > result[1]["backtest_rule_score"]
    assert any("高确认" in tag for tag in result[0]["backtest_rule_tags"])


def test_early_conviction_separates_large_narrative_from_late_momentum():
    row = {
        "symbol": "4Stock",
        "name": "BSC Stock Meme",
        "chain": "bsc",
        "mcap": 133_051,
        "liquidity": 818_468,
        "volume24h": 2_999,
        "pair_age_hours": 1.7,
        "holders": 3893,
        "top10_holder_pct": 13.6,
        "smart_money": 1,
        "kol": 1,
        "source_count": 5,
        "sources": ["gmgn_trending", "okx_signal", "985_monitor", "985_fomo_wallets", "wind_monitor"],
        "heat_score": 181,
        "gmgn_risk_flags": [],
    }

    result = meme_potential.meme_early_conviction_annotation(row)

    assert result["early_conviction_level"] == "high"
    assert result["early_conviction_label"] == "早期重点 · 大叙事"
    assert "币股大叙事" in result["early_conviction_reason"]
    assert result["early_conviction_data_status"] == "聪明钱历史待核验"
