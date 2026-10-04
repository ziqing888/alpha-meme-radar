from __future__ import annotations

import sys
from pathlib import Path

import pytest


BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from alpha_chain_strategy import StrategyPolicy, classify_signal, token_identity


NOW = "2026-09-09T12:10:00+00:00"
TOKEN = "0xABCDEFabcdefABCDEFabcdefABCDEFabcdef1234"


def current_row(**overrides):
    row = {
        "chain": "Robinhood",
        "contract_address": TOKEN,
        "symbol": "DOGSHIT",
        "signal_stage": "aggregate_early_bird",
        "price_usd": 1.25,
        "mcap": 250_000,
        "liquidity_usd": 25_000,
        "rank_score": 95,
        "pair_age_hours": 1,
        "recommendation_bucket": "ambush",
        "source_labels": ["GMGN", "DS"],
        "buy_route_ready": True,
        "sell_route_ready": True,
        "round_trip_loss_pct": 6,
        "buy_price_impact_pct": 3,
        "sell_price_impact_pct": 3,
        "hard_risk_pass": True,
        "sellable_cycles": 2,
        "sell_count": 1,
    }
    row.update(overrides)
    return row


def history(**overrides):
    first_snapshot = {
        "chain": "robinhood",
        "contract_address": TOKEN,
        "price_usd": 1.0,
        "mcap": 200_000,
        "liquidity_usd": 21_000,
    }
    first_snapshot.update(overrides.pop("first_snapshot", {}))
    result = {
        "first_seen_at": "2026-09-09T12:02:00+00:00",
        "first_snapshot": first_snapshot,
    }
    result.update(overrides)
    return result


def test_identity_uses_chain_and_contract_not_symbol():
    assert token_identity(current_row(symbol="OTHER")) == f"robinhood:{TOKEN.lower()}"


@pytest.mark.parametrize(
    "contract",
    [
        "0x1234",
        "0x" + "g" * 40,
        "0x" + "0" * 40,
        "not-an-address",
        "",
    ],
)
def test_supported_evm_identity_rejects_invalid_or_zero_contract(contract):
    row = current_row(contract_address=contract)

    assert token_identity(row) == ""
    result = classify_signal(row, history(), NOW)
    assert result["eligible"] is False
    assert result["reject_reason"] == "missing_token_identity"


def test_classification_uses_historical_first_snapshot():
    decision = classify_signal(current_row(first_seen_at=NOW), history(), NOW)

    assert decision["first_seen_at"] == "2026-09-09T12:02:00+00:00"
    assert decision["first_price_usd"] == 1.0
    assert decision["first_mcap_usd"] == 200_000
    assert decision["current_price_usd"] == 1.25
    assert decision["current_mcap_usd"] == 250_000
    assert decision["entry_delay_seconds"] == 480
    assert decision["markup_from_first"] == 1.25


def test_classification_prefers_discovery_baseline_over_later_tradeable_quote():
    stored = history(
        first_snapshot={"price_usd": 0.0001, "mcap": 100_000},
        first_price_usd=0.00005,
        first_mcap_usd=50_000,
        discovery_snapshot={
            "chain": "robinhood",
            "contract_address": TOKEN,
            "price_usd": 0.00005,
            "mcap": 50_000,
        },
    )
    decision = classify_signal(
        current_row(price_usd=0.00006, mcap=60_000),
        stored,
        NOW,
    )

    assert decision["first_price_usd"] == 0.00005
    assert decision["first_mcap_usd"] == 50_000
    assert decision["markup_from_first"] == 1.2
    assert decision["eligible"] is True


def test_chain_policies_expose_the_approved_limits():
    robinhood = StrategyPolicy.for_chain("4663")
    bsc = StrategyPolicy.for_chain("BNB")

    assert (
        robinhood.chain,
        robinhood.live_signal_stage,
        robinhood.order_notional_usd,
        robinhood.first_mcap_max_usd,
        robinhood.max_markup_from_first,
        robinhood.max_round_trip_loss_pct,
        robinhood.max_buy_price_impact_pct,
        robinhood.min_rank_score,
    ) == ("robinhood", "aggregate_early_bird", 2, 300_000, 1.25, 25, 12, 45)
    assert (
        bsc.chain,
        bsc.live_signal_stage,
        bsc.order_notional_usd,
        bsc.first_mcap_max_usd,
        bsc.max_markup_from_first,
        bsc.max_round_trip_loss_pct,
        bsc.max_sell_price_impact_pct,
        bsc.min_sellable_cycles,
        bsc.requires_observed_sell,
    ) == ("bsc", "aggregate_early_bird", 1, 100_000, 1.5, 25, 12, 2, True)


def test_robinhood_aggregate_early_bird_routes_live_with_complete_contract():
    result = classify_signal(current_row(rank_components={"timing": 32, "bad": "secret"}), history(), NOW)

    assert result["eligible"] is True
    assert result["entry_route"] == "robinhood_aggregate_early_bird"
    assert result["execution_mode"] == "live_candidate"
    assert result["reject_reason"] == ""
    assert result["rank_score"] == 95
    assert result["rank_components"] == {"timing": 32.0}
    assert result["strategy_version"] == "chain_v2"
    assert result["signal_stage"] == "aggregate_early_bird"
    assert all(
        value
        for name, value in result["policy_checks"].items()
        if name != "sellability_confirmation"
    )
    assert result["policy_checks"]["sellability_confirmation"] is None
    assert result["tradeability"]["status"] == "passed"
    assert result["tradeability"]["reject_reason"] == ""
    assert result["order_authorized"] is False
    assert result["requires_executor_tradeability_check"] is True
    assert set(result) == {
        "strategy_version",
        "signal_stage",
        "entry_route",
        "execution_mode",
        "rank_score",
        "legacy_score",
        "rank_components",
        "pair_age_hours",
        "recommendation_bucket",
        "independent_source_count",
        "first_seen_at",
        "first_price_usd",
        "first_mcap_usd",
        "current_price_usd",
        "current_mcap_usd",
        "entry_delay_seconds",
        "markup_from_first",
        "policy_checks",
        "tradeability",
        "eligible",
        "entry_authorized",
        "reject_reason",
        "order_authorized",
        "requires_executor_tradeability_check",
    }


def test_robinhood_aggregate_discovery_is_monitor_only():
    result = classify_signal(
        current_row(signal_stage="aggregate_discovery"),
        history(),
        NOW,
    )

    assert result["eligible"] is False
    assert result["execution_mode"] == "rejected"
    assert result["reject_reason"] == "robinhood_signal_stage"
    assert result["policy_checks"]["signal_stage"] is False


@pytest.mark.parametrize("bucket", [None, "", "shadow"])
def test_robinhood_v2_rank_is_not_vetoed_by_legacy_bucket(bucket):
    result = classify_signal(current_row(recommendation_bucket=bucket), history(), NOW)

    assert result["eligible"] is True
    assert result["policy_checks"]["decision_bucket"] is False


def test_bsc_early_bird_does_not_wait_for_monitoring_recommendation_bucket():
    row = current_row(
        chain="bsc",
        signal_stage="aggregate_early_bird",
        recommendation_bucket="shadow",
        mcap=50_000,
        price_usd=1.1,
    )
    first = history(first_snapshot={"chain": "bsc", "mcap": 45_000})

    result = classify_signal(row, first, NOW)

    assert result["eligible"] is True
    assert result["entry_authorized"] is True
    assert result["recommendation_bucket"] == "shadow"


def test_bsc_aggregate_confirmation_is_an_accepted_live_stage():
    row = current_row(
        chain="bsc",
        signal_stage="aggregate_confirmation",
        recommendation_bucket="ambush",
        mcap=50_000,
        price_usd=1.1,
    )
    first = history(first_snapshot={"chain": "bsc", "mcap": 45_000})

    result = classify_signal(row, first, NOW)

    assert result["eligible"] is True
    assert result["signal_stage"] == "aggregate_confirmation"
    assert result["entry_route"] == "bsc_aggregate_confirmation"


def test_robinhood_promoted_early_bird_is_accepted_without_raw_discovery_rewrite():
    result = classify_signal(
        current_row(signal_stage="aggregate_early_bird"),
        history(),
        NOW,
    )

    assert result["eligible"] is True
    assert result["signal_stage"] == "aggregate_early_bird"
    assert result["entry_route"] == "robinhood_aggregate_early_bird"


def test_robinhood_discovery_without_okx_evidence_is_live_candidate_pending_node_check():
    row = current_row()
    for key in (
        "buy_route_ready",
        "sell_route_ready",
        "round_trip_loss_pct",
        "buy_price_impact_pct",
        "sell_price_impact_pct",
    ):
        row.pop(key)

    result = classify_signal(row, history(), NOW)

    assert result["eligible"] is True
    assert result["execution_mode"] == "live_candidate"
    assert result["reject_reason"] == ""
    assert result["tradeability"]["status"] == "pending"
    assert result["tradeability"]["reject_reason"] == ""
    assert result["order_authorized"] is False


def test_route_freshness_alone_never_proves_executable_tradeability():
    row = current_row()
    row.pop("buy_route_ready")
    row.pop("sell_route_ready")
    row["buy_route_fresh"] = True
    row["sell_route_fresh"] = True

    result = classify_signal(row, history(), NOW)

    assert result["eligible"] is True
    assert result["execution_mode"] == "live_candidate"
    assert result["tradeability"]["status"] == "pending"
    assert result["tradeability"]["buy_route_ready"] is None
    assert result["tradeability"]["sell_route_ready"] is None
    assert result["order_authorized"] is False


def test_robinhood_uses_markup_not_rank_or_high_m5_as_chase_gate():
    high_momentum = current_row(change_m5=283, rank_score=100, price_usd=1.10, mcap=220_000)
    assert classify_signal(high_momentum, history(), NOW)["eligible"] is True

    chased = current_row(change_m5=0, rank_score=95, price_usd=1.26, mcap=252_000)
    result = classify_signal(chased, history(), NOW)
    assert result["eligible"] is False
    assert result["reject_reason"] == "robinhood_markup_limit"


def test_robinhood_reports_buy_impact_failure_without_rejecting_discovery():
    result = classify_signal(current_row(buy_price_impact_pct=12.01), history(), NOW)
    assert result["eligible"] is True
    assert result["reject_reason"] == ""
    assert result["tradeability"]["status"] == "unavailable"
    assert result["tradeability"]["reject_reason"] == "robinhood_buy_impact_limit"

    result = classify_signal(current_row(sell_price_impact_pct=99), history(), NOW)
    assert result["eligible"] is True
    assert result["tradeability"]["status"] == "passed"


def bsc_row(**overrides):
    row = current_row(
        chain="bsc",
        signal_stage="aggregate_early_bird",
        price_usd=1.4,
        mcap=70_000,
        rank_score=91,
    )
    row.update(overrides)
    return row


def bsc_history(**overrides):
    first_snapshot = {
        "chain": "bsc",
        "contract_address": TOKEN,
        "price_usd": 1.0,
        "mcap": 50_000,
    }
    first_snapshot.update(overrides.pop("first_snapshot", {}))
    return history(first_snapshot=first_snapshot, **overrides)


def test_bsc_discovery_is_shadow_only():
    result = classify_signal(bsc_row(signal_stage="aggregate_discovery"), bsc_history(), NOW)

    assert result["eligible"] is False
    assert result["entry_route"] == "bsc_aggregate_discovery_shadow"
    assert result["execution_mode"] == "shadow"
    assert result["reject_reason"] == "bsc_shadow_only"


def test_bsc_early_bird_routes_to_executor_before_sellability_confirmation():
    result = classify_signal(bsc_row(sellable_cycles=None), bsc_history(), NOW)

    assert result["eligible"] is True
    assert result["entry_route"] == "bsc_aggregate_early_bird"
    assert result["execution_mode"] == "live_candidate"
    assert result["reject_reason"] == ""
    assert result["policy_checks"]["sellability_confirmation"] is None


def test_bsc_monitor_sellable_cycles_do_not_replace_executor_confirmation():
    result = classify_signal(bsc_row(sellable_cycles=1), bsc_history(), NOW)

    assert result["eligible"] is True
    assert result["reject_reason"] == ""
    assert result["policy_checks"]["sellability_confirmation"] is None


def test_bsc_early_bird_requires_observed_sell_and_sell_impact_limit():
    no_sell = classify_signal(bsc_row(sell_count=0), bsc_history(), NOW)
    high_impact = classify_signal(bsc_row(sell_price_impact_pct=12.01), bsc_history(), NOW)

    assert no_sell["reject_reason"] == "bsc_observed_sell_required"
    assert high_impact["eligible"] is True
    assert high_impact["reject_reason"] == ""
    assert high_impact["tradeability"]["status"] == "unavailable"
    assert high_impact["tradeability"]["reject_reason"] == "bsc_sell_impact_limit"


def test_bsc_observed_sell_accepts_dexscreener_sell_count5m():
    row = bsc_row(sell_count5m=2)
    row.pop("sell_count")

    result = classify_signal(row, bsc_history(), NOW)

    assert result["eligible"] is True
    assert result["policy_checks"]["observed_sell"] is True


def test_bsc_early_bird_without_okx_routes_still_requires_monitor_sellability():
    row = bsc_row()
    for key in (
        "buy_route_ready",
        "sell_route_ready",
        "round_trip_loss_pct",
        "buy_price_impact_pct",
        "sell_price_impact_pct",
    ):
        row.pop(key)

    result = classify_signal(row, bsc_history(), NOW)

    assert result["eligible"] is True
    assert result["execution_mode"] == "live_candidate"
    assert result["tradeability"]["status"] == "pending"


def test_monitor_hard_risk_remains_a_discovery_policy_rejection():
    result = classify_signal(current_row(hard_risk_pass=False), history(), NOW)

    assert result["eligible"] is False
    assert result["execution_mode"] == "rejected"
    assert result["reject_reason"] == "robinhood_hard_risk"


def test_missing_hard_risk_is_deferred_only_for_robinhood_exact_round_trip():
    robinhood = current_row()
    robinhood.pop("hard_risk_pass")
    bsc = bsc_row()
    bsc.pop("hard_risk_pass")

    robinhood_result = classify_signal(robinhood, history(), NOW)
    bsc_result = classify_signal(bsc, bsc_history(), NOW)

    assert robinhood_result["eligible"] is True
    assert robinhood_result["policy_checks"]["hard_risk"] is None
    assert bsc_result["eligible"] is False
    assert bsc_result["reject_reason"] == "bsc_hard_risk"


def test_bsc_100k_to_300k_first_mcap_remains_shadow():
    result = classify_signal(
        bsc_row(price_usd=1.05, mcap=157_500),
        bsc_history(first_snapshot={"price_usd": 1.0, "mcap": 150_000}),
        NOW,
    )

    assert result["eligible"] is False
    assert result["execution_mode"] == "shadow"
    assert result["reject_reason"] == "bsc_mcap_shadow_only"


def test_missing_immutable_history_fails_closed():
    result = classify_signal(current_row(), None, NOW)

    assert result["eligible"] is False
    assert result["reject_reason"] == "missing_first_snapshot"


def test_missing_rank_is_unavailable_not_fabricated_zero():
    result = classify_signal(current_row(rank_score=None), history(), NOW)

    assert result["rank_score"] is None
    assert result["eligible"] is False
    assert result["reject_reason"] == "robinhood_rank_unavailable"


def test_legacy_score_cannot_impersonate_v2_rank():
    result = classify_signal(
        current_row(rank_score=None, score=100, entry_score=100),
        history(),
        NOW,
    )

    assert result["rank_score"] is None
    assert result["legacy_score"] == 100
    assert result["eligible"] is False
    assert result["reject_reason"] == "robinhood_rank_unavailable"


def test_first_snapshot_must_match_current_chain_and_contract():
    wrong_contract = history(first_snapshot={"contract_address": "0x" + "9" * 40})
    wrong_chain = history(first_snapshot={"chain": "bsc"})

    for stored in (wrong_contract, wrong_chain):
        result = classify_signal(current_row(), stored, NOW)
        assert result["eligible"] is False
        assert result["reject_reason"] == "first_snapshot_identity_mismatch"
        assert result["policy_checks"]["first_snapshot_identity"] is False


def test_real_zero_rank_is_retained_as_priority_context_not_an_entry_veto():
    result = classify_signal(current_row(rank_score=0), history(), NOW)

    assert result["rank_score"] == 0
    assert result["eligible"] is True
    assert result["entry_authorized"] is True
    assert result["reject_reason"] == ""


@pytest.mark.parametrize("changes,reason", [
    ({"pair_age_hours": None}, "robinhood_pair_age"),
    ({"pair_age_hours": 7}, "robinhood_pair_age"),
    ({"source_labels": ["985_monitor", "985_smartmoney"]}, "robinhood_independent_sources"),
])
def test_live_authorization_requires_fresh_pool_and_independent_sources(changes, reason):
    result = classify_signal(current_row(**changes), history(), NOW)

    assert result["eligible"] is False
    assert result["entry_authorized"] is False
    assert result["reject_reason"] == reason


def test_monitoring_recommendation_cannot_veto_robinhood_execution_strategy():
    result = classify_signal(current_row(recommendation_bucket="pullback"), history(), NOW)

    assert result["eligible"] is True
    assert result["entry_authorized"] is True
    assert result["recommendation_bucket"] == "pullback"


def test_arc_has_no_execution_strategy():
    with pytest.raises(ValueError, match=r"unsupported_chain:arc"):
        StrategyPolicy.for_chain("arc")
