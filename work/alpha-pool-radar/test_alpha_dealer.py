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


load_module("alpha_pool_radar", BASE / "alpha_pool_radar.py")
dealer = load_module("alpha_dealer", BASE / "alpha_dealer.py")


def test_dealer_score_rewards_small_cap_volume_oi_and_holder_concentration():
    row = {
        "mcap": 20_000_000,
        "volume24h": 8_000_000,
        "oi_change_1h_pct": 8,
        "funding_rate_pct": -0.06,
        "liquidity": 200_000,
        "pair_age_hours": 24,
        "top10_holder_pct": 58,
        "max_holder_pct": 22,
    }

    score, flags = dealer.dealer_score(row)

    assert score > 100
    assert any("Top10" in flag for flag in flags)
    assert any("OI+" in flag for flag in flags)


def test_dealer_profile_marks_up_volume_as_pump_not_generic_volume():
    row = {
        "market_cap": 10_000_000,
        "dex_volume24h": 4_000_000,
        "alpha_change24h_pct": 22,
        "top10_holder_pct": 20,
    }

    profile = dealer.dealer_profile(row)

    assert profile["dealer_type"] == "pump"
    assert profile["dealer_label"] == "拉盘型"
    assert profile["dealer_volume_usd"] == 4_000_000
    assert profile["dealer_volume_to_mcap_pct"] == 40.0
    assert "上涨" in profile["dealer_explain"]


def test_dealer_profile_prefers_strong_buyflow_confirmation():
    row = {
        "market_cap": 10_000_000,
        "dex_volume24h": 4_000_000,
        "alpha_change24h_pct": 12,
        "alpha_buyflow_score": 72,
        "alpha_buyflow_label": "强买盘",
        "alpha_buyflow_explain": "买卖比2.10 / 成交笔数420",
        "top10_holder_pct": 20,
    }

    profile = dealer.dealer_profile(row)

    assert profile["dealer_type"] == "buyflow_pump"
    assert profile["dealer_label"] == "活跃上涨型"
    assert "强买盘" in profile["dealer_explain"]


def test_dealer_profile_marks_controlled_contract_pump():
    row = {
        "market_cap": 15_000_000,
        "alpha_volume24h": 1_000_000,
        "futures_quote_volume24h": 45_000_000,
        "alpha_change24h_pct": 18,
        "oi_change_1h_pct": 9,
        "funding_rate_pct": 0.04,
        "top10_holder_pct": 68,
        "max_holder_pct": 24,
    }

    profile = dealer.dealer_profile(row)

    assert profile["dealer_type"] == "controlled_contract_pump"
    assert profile["dealer_label"] == "高控爆拉型"
    assert profile["dealer_contract_volume_to_mcap_pct"] == 300.0
    assert "高控盘配合合约爆拉" in profile["dealer_explain"]


def test_dealer_score_rewards_controlled_contract_pump():
    row = {
        "market_cap": 15_000_000,
        "alpha_volume24h": 1_000_000,
        "futures_quote_volume24h": 45_000_000,
        "alpha_change24h_pct": 18,
        "oi_change_1h_pct": 9,
        "top10_holder_pct": 68,
    }

    score, flags = dealer.dealer_score(row)

    assert score >= 90
    assert "高控爆拉" in flags
    assert "合约量/市值高" in flags


def test_dealer_profile_marks_contract_pump_without_holder_data():
    row = {
        "market_cap": 20_000_000,
        "alpha_volume24h": 2_000_000,
        "futures_quote_volume24h": 60_000_000,
        "alpha_change24h_pct": 13,
        "oi_change_1h_pct": 3,
    }

    profile = dealer.dealer_profile(row)

    assert profile["dealer_type"] == "contract_pump"
    assert profile["dealer_label"] == "合约爆拉型"
    assert "暂缺Top10数据" in profile["dealer_explain"]


def test_dealer_score_rewards_contract_pump_without_holder_data():
    row = {
        "market_cap": 20_000_000,
        "alpha_volume24h": 2_000_000,
        "futures_quote_volume24h": 60_000_000,
        "alpha_change24h_pct": 13,
        "oi_change_1h_pct": 3,
    }

    _score, flags = dealer.dealer_score(row)

    assert "合约爆拉" in flags
    assert "高控爆拉" not in flags


def test_dealer_profile_rejects_contract_pump_when_oi_collapses():
    row = {
        "market_cap": 20_000_000,
        "alpha_volume24h": 2_000_000,
        "futures_quote_volume24h": 60_000_000,
        "alpha_change24h_pct": 13,
        "oi_change_1h_pct": -9,
    }

    profile = dealer.dealer_profile(row)
    _score, flags = dealer.dealer_score(row)

    assert profile["dealer_type"] != "contract_pump"
    assert "合约爆拉" not in flags


def test_dealer_score_rewards_buyflow_confirmation():
    base_row = {
        "market_cap": 30_000_000,
        "dex_volume24h": 2_000_000,
    }
    buyflow_row = {**base_row, "alpha_buyflow_score": 70}

    base_score, base_flags = dealer.dealer_score(base_row)
    buyflow_score, buyflow_flags = dealer.dealer_score(buyflow_row)

    assert buyflow_score > base_score
    assert "高度活跃" in buyflow_flags
    assert "强买盘" not in base_flags


def test_dealer_profile_marks_down_volume_as_distribution():
    row = {
        "market_cap": 10_000_000,
        "dex_volume24h": 4_000_000,
        "alpha_change24h_pct": -18,
        "top10_holder_pct": 20,
    }

    profile = dealer.dealer_profile(row)

    assert profile["dealer_type"] == "distribution"
    assert profile["dealer_label"] == "出货型"
    assert "下跌" in profile["dealer_explain"]


def test_dealer_profile_does_not_call_holder_risk_volume_expansion():
    row = {
        "market_cap": 20_000_000,
        "dex_volume24h": 20_000,
        "alpha_change24h_pct": -3,
        "top10_holder_pct": 72,
    }

    profile = dealer.dealer_profile(row)

    assert profile["dealer_type"] == "holder_risk"
    assert profile["dealer_label"] == "高控盘型"
    assert "成交放大" not in profile["dealer_explain"]


def test_holder_coverage_summary_counts_unique_tokens_with_holder_data():
    rows = [
        {"chain": "base", "symbol": "AAA", "contract_address": "0x1", "top10_holder_pct": 51.2},
        {"chain": "base", "symbol": "AAA", "contract_address": "0x1", "top10_holder_pct": 51.2},
        {"chain": "bsc", "symbol": "BBB", "contract_address": "0x2", "top10_holder_pct": None},
        {"chain": "solana", "symbol": "CCC", "token_address": "So111", "top10_holder_pct": 12.3},
    ]

    summary = dealer.holder_coverage_summary(rows)

    assert summary == {
        "unique_tokens": 3,
        "with_top10": 2,
        "coverage_pct": 66.67,
        "sources": {"unknown": 2},
    }
