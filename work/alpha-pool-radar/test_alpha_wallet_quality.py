from copy import deepcopy

import pytest

from alpha_wallet_quality import qualified_wallets, wallet_identity


NOW = "2026-09-07T12:00:00+00:00"
ADDRESS = "0x" + "a" * 40


def good_wallet(address=ADDRESS, chain="bsc"):
    return {
        "chain": chain, "address": address, "status": "profit_history_supported",
        "evidence_checked_at": "2026-09-06T12:00:00+00:00",
        "stats_30d": {"wallet": address, "period_requested": "30d", "realized_profit_usd": 1000,
                      "token_count": 30, "sell_count": 40, "reported_buy_fees_usd": 25},
        "stats_7d": {"wallet": address, "period_requested": "7d", "realized_profit_usd": 100,
                     "token_count": 10, "sell_count": 15},
        "activity": {"observed_token_count": 10, "tokens_with_buy_and_sell": 6,
                     "sell_cost_basis_coverage": .9, "sells_with_reported_cost_basis": 18,
                     "profitable_tokens_in_observed_sell_sample": 5,
                     "largest_token_trade_share": .2, "largest_positive_token_margin_share": .4,
                     "sample_reported_sale_margin_usd": 70, "history_complete": False,
                     "independent_net_profit_usd": None},
        "independent_profit_verified": False,
    }


def test_good_row_retains_raw_evidence_and_does_not_mutate_input():
    row = good_wallet()
    original = deepcopy(row)
    result = qualified_wallets({"wallets": [row]}, NOW)
    assert len(result) == 1
    assert result[0]["rank"] == 1
    assert result[0]["stats_30d"] == row["stats_30d"]
    assert result[0]["activity"] == row["activity"]
    assert result[0]["independent_profit_verified"] is False
    result[0]["activity"]["history_complete"] = True
    assert row == original


def test_legacy_kol_or_trust_label_never_qualifies_but_profitable_kol_can():
    legacy = {"chain": "bsc", "address": ADDRESS, "score": 100, "tags": ["KOL", "leader"], "trusted": True}
    assert qualified_wallets({"wallets": [legacy]}, NOW) == []
    legacy.update(status="profit_history_supported", evidence_checked_at=NOW)
    assert qualified_wallets({"wallets": [legacy]}, NOW) == []
    good = good_wallet()
    good["tags"] = ["KOL", "leader"]
    assert len(qualified_wallets({"wallets": [good]}, NOW)) == 1
    good["status"] = "positive_stats_watch_only"
    assert qualified_wallets({"wallets": [good]}, NOW) == []


@pytest.mark.parametrize("section,field", [
    ("stats_30d", "realized_profit_usd"), ("stats_30d", "token_count"), ("stats_30d", "sell_count"),
    ("stats_7d", "realized_profit_usd"), ("stats_7d", "token_count"), ("stats_7d", "sell_count"),
    ("activity", "observed_token_count"), ("activity", "tokens_with_buy_and_sell"),
    ("activity", "sell_cost_basis_coverage"), ("activity", "sells_with_reported_cost_basis"),
    ("activity", "profitable_tokens_in_observed_sell_sample"), ("activity", "largest_token_trade_share"),
    ("activity", "largest_positive_token_margin_share"), ("activity", "sample_reported_sale_margin_usd"),
])
def test_every_gate_metric_is_required(section, field):
    row = good_wallet()
    del row[section][field]
    assert qualified_wallets({"wallets": [row]}, NOW) == []


@pytest.mark.parametrize("section,field,value", [
    ("stats_30d", "realized_profit_usd", 500), ("stats_7d", "realized_profit_usd", 0),
    ("stats_30d", "token_count", 19), ("stats_7d", "sell_count", 4),
    ("stats_30d", "realized_profit_usd", "nan"), ("stats_7d", "token_count", float("inf")),
    ("stats_30d", "sell_count", 30.5), ("stats_7d", "realized_profit_usd", True),
    ("activity", "sell_cost_basis_coverage", .79), ("activity", "sell_cost_basis_coverage", 90),
    ("activity", "sells_with_reported_cost_basis", 9),
    ("activity", "profitable_tokens_in_observed_sell_sample", 2),
    ("activity", "largest_token_trade_share", .76),
    ("activity", "largest_positive_token_margin_share", .81),
    ("activity", "sample_reported_sale_margin_usd", -10),
])
def test_status_does_not_override_bad_numerical_evidence(section, field, value):
    row = good_wallet()
    row[section][field] = value
    assert qualified_wallets({"wallets": [row]}, NOW) == []


@pytest.mark.parametrize("stamp", [None, "", "bad", "2026-09-06T12:00:00", "2026-09-04T11:59:59+00:00", "2026-09-07T12:00:01+00:00"])
def test_missing_stale_or_future_evidence_cannot_use_report_timestamp(stamp):
    row = good_wallet()
    row["evidence_checked_at"] = stamp
    assert qualified_wallets({"wallets": [row], "updated_at": NOW}, NOW) == []


def test_three_day_boundary_is_inclusive_and_aliases_deduplicate():
    row = good_wallet()
    row["evidence_checked_at"] = "2026-09-04T20:00:00+08:00"
    alias = good_wallet("0x" + "A" * 40, "56")
    result = qualified_wallets({"wallets": [row, alias]}, NOW)
    assert len(qualified_wallets({"wallets": [row]}, NOW)) == 1
    assert len(result) == 1 and result[0]["chain"] == "bsc"
    assert result[0]["evidence_checked_at"] == alias["evidence_checked_at"]


@pytest.mark.parametrize("chain,address", [("unknown", ADDRESS), ("bsc", "0xabc"), ("sol", ADDRESS),
                                         ("bsc", "0x" + "0" * 40), ("sol", "z" * 44),
                                         ("sol", "O" * 44)])
def test_invalid_chain_address_rejected(chain, address):
    assert qualified_wallets({"wallets": [good_wallet(address, chain)]}, NOW) == []


def test_solana_address_validated_as_32_bytes_and_keeps_case():
    address = "So11111111111111111111111111111111111111112"
    assert wallet_identity("solana", address) == ("sol", address)
    assert len(qualified_wallets({"wallets": [good_wallet(address, "solana")]}, NOW)) == 1


def test_wrong_wallet_or_window_cannot_be_attached_to_a_candidate():
    row = good_wallet()
    row["stats_7d"]["wallet"] = "0x" + "b" * 40
    assert qualified_wallets({"wallets": [row]}, NOW) == []
    row = good_wallet()
    row["stats_7d"]["period_requested"] = "30d"
    assert qualified_wallets({"wallets": [row]}, NOW) == []


def test_supported_list_fallback_limits_and_chain_fairness():
    rows = [good_wallet("0x" + str(i) * 40) for i in range(1, 5)] + [good_wallet(chain="base")]
    result = qualified_wallets({"supported_wallets": rows}, NOW, limit=2)
    assert {r["chain"] for r in result} == {"bsc", "base"}
    assert qualified_wallets({"wallets": [], "supported_wallets": rows}, NOW) == []
    for invalid in ({}, None, {"wallets": None}, {"wallets": [None, [], 3]}):
        assert qualified_wallets(invalid, NOW) == []
    assert qualified_wallets({"wallets": rows}, NOW, limit=0) == []
    assert qualified_wallets({"wallets": rows}, "missing") == []
