from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alpha_discovery_rank import compute_discovery_rank


NOW = datetime(2026, 9, 10, 4, 10, tzinfo=timezone.utc)


def complete_candidate(**overrides):
    row = {
        "chain": "robinhood",
        "contract_address": "0x" + "1" * 40,
        "first_seen_at": "2026-09-10T04:09:00+00:00",
        "source_groups": ["OKX", "GMGN", "DexScreener", "noxa"],
        "liquidity_usd": 100_000,
        "buy_count5m": 35,
        "sell_count5m": 15,
        "previous_tx_count5m": 25,
        "narrative_score": 80,
        "smart_money_score": 70,
        "top10_holder_pct": 24,
        "max_holder_pct": 8,
    }
    row.update(overrides)
    return row


def test_complete_evidence_produces_hand_checked_numeric_components():
    result = compute_discovery_rank(complete_candidate(), now=NOW)

    assert result == {
        "rank_score": 93.0,
        "rank_components": {
            "timing": 18.0,
            "source_evidence": 20.0,
            "liquidity": 15.0,
            "transaction_acceleration": 15.0,
            "narrative": 8.0,
            "smart_money": 7.0,
            "holder_structure": 10.0,
        },
    }
    assert result["rank_score"] == sum(result["rank_components"].values())
    assert all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in result["rank_components"].values()
    )


@pytest.mark.parametrize("legacy_field", ["score", "entry_score", "execution_candidate_score", "legacy_score"])
def test_legacy_scores_never_supply_or_change_discovery_rank(legacy_field):
    sparse = {legacy_field: 100}
    baseline = compute_discovery_rank({}, now=NOW)

    assert compute_discovery_rank(sparse, now=NOW) == baseline
    assert baseline["rank_score"] == 0.0


def test_immutable_first_snapshot_controls_timing_instead_of_refreshed_row():
    current = complete_candidate(first_seen_at="2026-09-10T04:09:55+00:00")
    first_snapshot = {"first_seen_at": "2026-09-10T04:05:00+00:00"}

    result = compute_discovery_rank(current, first_snapshot=first_snapshot, now=NOW)

    assert result["rank_components"]["timing"] == 10.0


def test_immutable_first_snapshot_controls_discovery_evidence():
    current = complete_candidate()
    first_snapshot = {
        "first_seen_at": "2026-09-10T04:05:00+00:00",
        "source_groups": ["OKX"],
        "liquidity_usd": 6_000,
        "buy_count5m": 4,
        "sell_count5m": 1,
        "previous_tx_count5m": 5,
        "top10_holder_pct": 60,
        "max_holder_pct": 30,
    }

    result = compute_discovery_rank(current, first_snapshot=first_snapshot, now=NOW)

    assert result["rank_components"] == {
        "timing": 10.0,
        "source_evidence": 5.0,
        "liquidity": 3.0,
        "transaction_acceleration": 1.0,
        "narrative": 8.0,
        "smart_money": 7.0,
        "holder_structure": 0.0,
    }


def test_independent_source_groups_are_normalized_and_deduplicated():
    row = complete_candidate(
        source_groups=["OKX Trending", "okx", "GMGN", "gmgn skills", "DS", "DexScreener"]
    )

    result = compute_discovery_rank(row, now=NOW)

    assert result["rank_components"]["source_evidence"] == 15.0


def test_processing_labels_do_not_inflate_independent_source_evidence():
    row = complete_candidate(
        source_groups=["proficy"],
        sources=["proficy_trending"],
        source_labels=["GMGN", "Proficy", "DS", "Alpha_AI"],
        source_count=1,
    )

    result = compute_discovery_rank(row, now=NOW)

    assert result["rank_components"]["source_evidence"] == 5.0


def test_missing_and_malformed_evidence_stays_finite_and_bounded():
    result = compute_discovery_rank(
        {
            "first_seen_at": "not-a-time",
            "source_count": float("nan"),
            "liquidity_usd": -1,
            "buy_count5m": "bad",
            "sell_count5m": -9,
            "narrative_score": 999,
            "smart_money_score": -20,
            "top10_holder_pct": 500,
            "max_holder_pct": -1,
        },
        now=NOW,
    )

    assert result["rank_score"] == 10.0
    assert 0.0 <= result["rank_score"] <= 100.0
    assert all(0.0 <= value <= 20.0 for value in result["rank_components"].values())


def test_rank_output_contains_no_trade_authorization_or_threshold_decision():
    result = compute_discovery_rank(complete_candidate(), now=NOW)

    forbidden = {"eligible", "accepted", "tradeable", "execution_mode", "threshold", "reject_reason"}
    assert forbidden.isdisjoint(result)
    assert set(result) == {"rank_score", "rank_components"}


def test_naive_datetime_is_rejected_instead_of_using_machine_timezone():
    with pytest.raises(ValueError, match="now_must_be_timezone_aware"):
        compute_discovery_rank(complete_candidate(), now=datetime(2026, 9, 10, 4, 10))
