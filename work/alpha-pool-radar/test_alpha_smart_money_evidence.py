import copy
import json

import pytest

from alpha_smart_money_evidence import assess_smart_money, enrich_smart_money


NOW = "2026-09-07T12:00:00+00:00"
TOKEN = "0x" + "a" * 40
WALLET = "0x" + "b" * 40
WALLET2 = "0x" + "c" * 40
SKILL_WALLET = "0x" + "1" * 40
SKILL_WALLET2 = "0x" + "2" * 40
TX = "0x" + "d" * 64


def profitable_profile(address):
    stats = {"realized_profit_usd": 1000, "token_count": 30, "sell_count": 30, "buy_count": 40}
    return {"chain": "bsc", "address": address, "status": "profit_history_supported",
            "evidence_checked_at": NOW, "stats_30d": stats, "stats_7d": stats,
            "activity": {"observed_token_count": 20, "tokens_with_buy_and_sell": 12,
                "sell_cost_basis_coverage": 1, "sells_with_reported_cost_basis": 20,
                "profitable_tokens_in_observed_sell_sample": 10, "largest_token_trade_share": .1,
                "largest_positive_token_margin_share": .2, "sample_reported_sale_margin_usd": 100}}


@pytest.fixture(autouse=True)
def qualified_watchlist(tmp_path):
    (tmp_path / "gmgn-smart-money-top50.json").write_text(json.dumps({"wallets": [
        profitable_profile(WALLET), profitable_profile(WALLET2)]}), encoding="utf-8")


def trade(**overrides):
    return {"chain": "bsc", "contract_address": TOKEN, "monitor985_wallet": WALLET,
            "monitor985_tx_hash": TX, "monitor985_trade_side": "BUY",
            "monitor985_trade_amount_usd": 120, "observed_at": NOW, **overrides}


def inbox(tmp_path, name, rows):
    path = tmp_path / "meme-source-inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"source": name, "fetched_at": NOW, "data": rows}), encoding="utf-8")
    return path


def enrich(tmp_path, row=None):
    return enrich_smart_money([row or {"chain": "bsc", "contract_address": TOKEN}], tmp_path, NOW)[0]["smart_money_evidence"]


def test_cross_source_transaction_dedup_flows_and_read_only_watchlist(tmp_path):
    inbox(tmp_path, "985-monitor.json", [trade(cluster_id="funding-cluster-1")])
    inbox(tmp_path, "985-fomo-wallets.json", [trade(cluster_id="funding-cluster-1")])
    inbox(tmp_path, "gmgn.json", [trade(monitor985_wallet=WALLET2, monitor985_tx_hash="0x" + "e" * 64,
                                      monitor985_trade_side="SELL", monitor985_trade_amount_usd=35)])
    watch = tmp_path / "gmgn-smart-money-top50.json"
    watch.write_text(json.dumps({"wallets": [{"address": WALLET, "chain": "56", "score": 999,
                                              "total_buy_usd": 90000}]}), encoding="utf-8")
    before_files = {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")}
    row = {"chain": "56", "contract_address": TOKEN, "source_labels": ["GMGN"]}
    original = copy.deepcopy(row)
    result = enrich(tmp_path, row)
    assert row == original
    assert result["unique_wallet_count"] == 2
    assert result["buy_usd"] == 120
    assert result["sell_usd"] == 35
    assert result["net_flow_usd"] == 85
    assert result["flow_event_count"] == 2
    assert len(result["events"][0]["provenance"]) == 2
    assert result["linked_clusters"] == ["funding-cluster-1"]
    assert result["watchlist"]["matched_wallets"] == []
    assert result["watchlist"]["proven_profitability"] is False
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*.json")} == before_files


def test_actual_985_aggregate_and_fomo_handle_shapes_are_not_wallets(tmp_path):
    inbox(tmp_path, "985-smartmoney.json", [{"chain": "bsc", "address": TOKEN, "smart_money": 34,
          "monitor985_smart_early_buyers": 3, "monitor985_smart_current_holders": 3,
          "monitor985_smart_peak_sellers": 3, "observed_at": NOW}])
    inbox(tmp_path, "985-fomo-wallets.json", [{"chain": "bsc", "address": TOKEN,
          "monitor985_fomo_handle": "pointfarmcap", "monitor985_trade_amount_usd": 28.89,
          "monitor985_trade_side": "BUY", "monitor985_tx_hash": TX, "observed_at": NOW}])
    result = enrich(tmp_path)
    assert result["unique_wallet_count"] == 0
    assert result["flow_event_count"] == 0
    assert result["aggregate_reports"][0]["reported_counts"]["smart_money"] == 34
    assert result["aggregate_counts_are_unique_wallets"] is False


@pytest.mark.parametrize("overrides", [
    {"monitor985_trade_side": ""}, {"monitor985_trade_side": "transfer"},
    {"monitor985_trade_amount_usd": -3}, {"monitor985_trade_amount_usd": "NaN"},
    {"monitor985_trade_amount_usd": float("inf")}, {"monitor985_trade_amount_usd": True},
    {"monitor985_trade_amount_usd": None, "volume24h": 9000},
    {"observed_at": None, "fetched_at": NOW}, {"observed_at": "2026-09-07T12:00:00"},
    {"observed_at": "2026-09-07T12:01:00+00:00"}, {"monitor985_tx_hash": "not-a-tx"},
    {"monitor985_wallet": "named-wallet"},
    {"wallets": [WALLET, WALLET2]},
])
def test_incomplete_ambiguous_or_invalid_trades_do_not_generate_flows(tmp_path, overrides):
    inbox(tmp_path, "events.json", [trade(**overrides)])
    assert enrich(tmp_path)["flow_event_count"] == 0


def test_old_events_stay_attributed_but_never_become_fresh_on_fetch(tmp_path):
    inbox(tmp_path, "events.json", [trade(observed_at="2026-09-06T12:00:00Z")])
    result = enrich(tmp_path)
    assert result["unique_wallet_count"] == 1
    assert result["fresh_unique_wallet_count"] == 0
    assert result["buy_usd"] == 0
    assert result["freshness"] == "stale"
    again = enrich_smart_money([{"chain": "bsc", "address": TOKEN}], tmp_path, "2026-09-07T12:05:00Z")[0]["smart_money_evidence"]
    assert again["fingerprint"] == result["fingerprint"]


def test_conflicting_duplicate_transactions_are_excluded(tmp_path):
    inbox(tmp_path, "a.json", [trade()])
    inbox(tmp_path, "b.json", [trade(monitor985_trade_amount_usd=900)])
    result = enrich(tmp_path)
    assert result["flow_event_count"] == 0
    assert result["events"][0]["conflicting_reports"] is True


def test_solana_case_chain_isolation_and_epoch_milliseconds(tmp_path):
    token = "A" * 32
    wallet = "B" * 32
    inbox(tmp_path, "sol.json", [{"chain": "sol", "address": token, "wallet": wallet,
                                  "observed_at": 1788782400000}])
    result = enrich(tmp_path, {"chain": "solana", "address": token})
    assert result["unique_wallets"] == [wallet]
    assert result["fresh_unique_wallet_count"] == 1
    assert enrich(tmp_path, {"chain": "solana", "address": token.lower()})["unique_wallet_count"] == 0
    inbox(tmp_path, "base.json", [trade(chain="base")])
    assert enrich(tmp_path)["unique_wallet_count"] == 0


def test_watchlist_alone_never_manufactures_token_evidence_and_bad_json_is_visible(tmp_path):
    (tmp_path / "gmgn-smart-money-top50.json").write_text(json.dumps({"wallets": [
        {"chain": "bsc", "address": WALLET, "representative_tokens": [{"token": "bsc:" + TOKEN}]}]}))
    path = inbox(tmp_path, "bad.json", [])
    path.write_text("{")
    result = enrich(tmp_path)
    assert result["unique_wallet_count"] == 0
    assert result["events"] == []
    assert result["read_errors"] == [str(path)]


def liquid_market(**overrides):
    return {"chain": "bsc", "contract_address": TOKEN, "liquidity": 20_000,
            "quote_status": "fresh", "quote_observed_at": NOW, "quote_fingerprint": "quote-1", **overrides}


def two_buys(**second_overrides):
    return [trade(), trade(monitor985_wallet=WALLET2, monitor985_tx_hash="0x" + "e" * 64, **second_overrides)]


def test_wallet_role_dedups_gmgn_rows_envelope_and_exposes_unknown_ownership(tmp_path):
    path = inbox(tmp_path, "gmgn-wallet-flow.json", [])
    rows = two_buys()
    path.write_text(json.dumps({"source": "gmgn_cli_smartmoney", "updated_at": NOW, "rows": rows + rows}))
    result = enrich(tmp_path, liquid_market())
    role = result["confirmation"]
    assert result["confirmation_status"] == "wallet_evidence_supported"
    assert role["buy_support"] is True
    assert role["buy_transaction_count"] == 2
    assert role["buy_usd"] == 240
    assert role["ownership_verified"] is False
    assert role["ownership_independence"] == "unknown"
    assert "是否由不同人控制仍未核实" in result["reason"]


def test_gmgn_skills_cluster_is_live_evidence_but_not_profit_history_qualification(tmp_path):
    skill_rows = [
        {"source_family": "gmgn_skills_smartmoney", "chain": "bsc", "base_address": TOKEN,
         "maker": SKILL_WALLET, "side": "buy", "amount_usd": 140, "transaction_hash": TX,
         "timestamp": NOW},
        {"source_family": "gmgn_skills_smartmoney", "chain": "bsc", "base_address": TOKEN,
         "maker": SKILL_WALLET2, "side": "buy", "amount_usd": 180, "transaction_hash": "0x" + "e" * 64,
         "timestamp": NOW},
    ]
    inbox(tmp_path, "gmgn-skills-smartmoney.json", skill_rows)
    inbox(tmp_path, "gmgn-skills-trending.json", [{"source_family": "gmgn_skills_trending", "chain": "bsc",
        "address": TOKEN, "gmgn_smart_degen_count": 7, "gmgn_market_rank": 3, "observed_at": NOW}])
    row = enrich_smart_money([{"chain": "bsc", "contract_address": TOKEN}], tmp_path, NOW)[0]
    skill = row["gmgn_skill_evidence"]
    assert skill["smartmoney_buy_wallet_count"] == 2
    assert skill["cluster_buy"] is True
    assert skill["smart_degen_count"] == 7
    evidence = row["smart_money_evidence"]
    assert evidence["candidate_buy_wallet_count"] == 2
    assert evidence["candidate_buy_transaction_count"] == 2
    assert evidence["candidate_layer"] == "cluster"
    assert evidence["candidate_verified_buy_wallet_count"] == 0
    assert row["smart_money_evidence"]["confirmation_status"] == "insufficient_wallet_evidence"


def test_gmgn_market_roles_remain_distinct_in_strategy_evidence(tmp_path):
    inbox(tmp_path, "gmgn-skills-signal.json", [{
        "source_family": "gmgn_skills_signal", "chain": "bsc", "token_address": TOKEN,
        "gmgn_signal_type": 12, "gmgn_signal_name": "smart_money_buy", "observed_at": NOW,
    }])
    inbox(tmp_path, "gmgn-skills-trenches.json", [{
        "source_family": "gmgn_skills_trenches", "chain": "bsc", "token_address": TOKEN,
        "gmgn_trenches_stage": "near_completion", "observed_at": NOW,
    }])
    inbox(tmp_path, "gmgn-skills-kol.json", [{
        "source_family": "gmgn_skills_kol", "chain": "bsc", "base_address": TOKEN,
        "maker": SKILL_WALLET, "side": "buy", "amount_usd": 80,
        "transaction_hash": TX, "timestamp": NOW,
    }])
    inbox(tmp_path, "gmgn-skills-hot-searches.json", [{
        "source_family": "gmgn_skills_hot_searches", "chain": "bsc", "token_address": TOKEN,
        "gmgn_hot_search_rank": 2, "gmgn_visiting_count": 450, "observed_at": NOW,
    }])

    row = enrich_smart_money([{"chain": "bsc", "contract_address": TOKEN}], tmp_path, NOW)[0]
    skill = row["gmgn_skill_evidence"]

    assert skill["signal_types"] == [12]
    assert skill["positive_signal"] is True
    assert skill["exit_signal"] is False
    assert skill["trenches_stages"] == ["near_completion"]
    assert skill["kol_buy_wallet_count"] == 1
    assert skill["hot_search_rank"] == 2
    assert skill["visiting_count"] == 450
    assert skill["cluster_buy"] is False


@pytest.mark.parametrize("failure", ["same_wallet", "same_tx", "same_cluster", "stale", "conflict", "foreign_token", "foreign_chain"])
def test_ineligible_wallet_evidence_cannot_supply_buy_support(tmp_path, failure):
    rows = two_buys()
    if failure == "same_wallet":
        rows[1]["monitor985_wallet"] = WALLET
    elif failure == "same_tx":
        rows[1]["monitor985_tx_hash"] = TX
    elif failure == "same_cluster":
        for row in rows:
            row["cluster_id"] = "linked-funder"
    elif failure == "stale":
        rows[1]["observed_at"] = "2026-09-07T11:00:00Z"
    elif failure == "conflict":
        rows.append({**rows[1], "monitor985_trade_side": "SELL"})
    elif failure == "foreign_token":
        rows[1]["contract_address"] = "0x" + "f" * 40
    elif failure == "foreign_chain":
        rows[1]["chain"] = "base"
    inbox(tmp_path, "flows.json", rows)
    assert enrich(tmp_path, liquid_market())["confirmation"]["buy_support"] is False


def test_transitive_cluster_overlap_and_duplicate_annotations_are_preserved(tmp_path):
    rows = two_buys()
    rows[0]["cluster_id"] = "funder-a"
    rows[1]["cluster_id"] = "funder-b"
    # The second report links the same wallet to both funding clusters.
    rows.append({**rows[0], "cluster_id": "funder-b"})
    inbox(tmp_path, "flows.json", rows)
    role = enrich(tmp_path, liquid_market())["confirmation"]
    assert role["non_overlapping_buy_groups"] == [[WALLET, WALLET2]]
    assert role["buy_support"] is False


@pytest.mark.parametrize("market", [{"liquidity": 9999}, {"liquidity": "NaN"},
                                    {"quote_status": "stale"}, {"quote_status": "quarantined"},
                                    {"quote_observed_at": None}, {"quote_fingerprint": None}])
def test_wallet_role_requires_actual_liquid_fresh_market(tmp_path, market):
    inbox(tmp_path, "flows.json", two_buys())
    role = enrich(tmp_path, liquid_market(**market))["confirmation"]
    assert role["buy_support"] is False
    assert role["confirmation_status"] == "waiting_liquid_fresh_market"


@pytest.mark.parametrize("liquidity,buy,sell,blocked,threshold", [
    (20_000, 0, 249, False, 250), (20_000, 0, 250, True, 250),
    (100_000, 0, 999, False, 1000), (100_000, 0, 1000, True, 1000),
    (20_000, 500, 800, False, 250), (20_000, 500, 1000, True, 250),
])
def test_material_sell_gate_has_absolute_floor_ratio_and_liquidity_threshold(tmp_path, liquidity, buy, sell, blocked, threshold):
    rows = [trade(monitor985_trade_amount_usd=buy),
            trade(monitor985_wallet=WALLET2, monitor985_tx_hash="0x" + "e" * 64,
                  monitor985_trade_side="SELL", monitor985_trade_amount_usd=sell)]
    inbox(tmp_path, "flows.json", rows)
    role = enrich(tmp_path, liquid_market(liquidity=liquidity))["confirmation"]
    assert role["sell_dominance"] is blocked
    assert role["thresholds"]["material_net_sell_usd"] == threshold


def test_cached_enrichment_is_revalidated_without_trusting_summary_counts(tmp_path):
    inbox(tmp_path, "flows.json", two_buys())
    row = enrich_smart_money([liquid_market()], tmp_path, NOW)[0]
    row["quote_observed_at"] = "2026-09-07T12:30:00Z"
    row["smart_money_evidence"]["buy_usd"] = 999999
    role = assess_smart_money(row, "2026-09-07T12:30:00Z")
    assert role["buy_support"] is False
    assert role["buy_wallet_count"] == 0
    assert role["buy_usd"] == 0


def test_platform_totals_never_become_actionable_wallet_support(tmp_path):
    row = liquid_market(smart_money=100, smart_money_evidence={"buy_usd": 999999, "unique_wallet_count": 500})
    role = assess_smart_money(row, NOW)
    assert role["buy_support"] is False
    assert role["buy_wallet_count"] == 0


def test_legacy_kol_wallets_do_not_supply_smart_money_support(tmp_path):
    (tmp_path / "gmgn-smart-money-top50.json").write_text(json.dumps({"wallets": [
        {"chain": "bsc", "address": a, "score": 999, "tags": ["kol", "smart_money"]}
        for a in (WALLET, WALLET2)]}), encoding="utf-8")
    inbox(tmp_path, "flows.json", two_buys())
    result = enrich(tmp_path, liquid_market())
    assert result["confirmation"]["buy_support"] is False
    assert result["qualified_wallet_count"] == 0
    assert result["unique_wallet_count"] == 2


def test_qualified_wallet_history_expires_on_cached_reassessment(tmp_path):
    inbox(tmp_path, "flows.json", two_buys())
    row = enrich_smart_money([liquid_market()], tmp_path, NOW)[0]
    assert assess_smart_money(row, NOW)["buy_support"] is True
    later = "2026-09-11T12:00:00+00:00"
    for e in row["smart_money_evidence"]["events"]:
        e["observed_at"] = later
    row["quote_observed_at"] = later
    assert assess_smart_money(row, later)["buy_support"] is False


def test_unqualified_seller_still_blocks_qualified_buy_support(tmp_path):
    inbox(tmp_path, "flows.json", [*two_buys(), trade(monitor985_wallet="0x" + "9" * 40,
        monitor985_tx_hash="0x" + "9" * 64, monitor985_trade_side="SELL", monitor985_trade_amount_usd=5000)])
    role = enrich(tmp_path, liquid_market())["confirmation"]
    assert role["buy_support"] is False
    assert role["sell_dominance"] is True
    assert role["sell_usd"] == 0
    assert role["attributed_sell_usd"] == 5000


def test_trade_window_does_not_extend_market_quote_freshness(tmp_path):
    inbox(tmp_path, "flows.json", two_buys())
    role = enrich(tmp_path, liquid_market(quote_observed_at="2026-09-07T11:59:29Z"))["confirmation"]
    assert role["buy_support"] is False
    assert role["market_fresh"] is False
