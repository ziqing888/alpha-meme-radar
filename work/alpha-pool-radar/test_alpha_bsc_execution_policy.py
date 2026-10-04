from datetime import datetime, timedelta, timezone

import pytest

from alpha_bsc_execution_policy import (
    ExecutionConfig,
    entry_decision,
    exit_decision,
    scaled_notional,
    token_key,
)


CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 7, 22, 0, tzinfo=CN)


def valid_row(**overrides):
    row = {
        "chain": "bsc",
        "contract_address": "0xABC",
        "pool_address": "0xPOOL",
        "execution_arm": "first_discovery",
        "signal_at": NOW.isoformat(),
        "first_seen_at": (NOW - timedelta(minutes=10)).isoformat(),
        "quote_at": NOW.isoformat(),
        "quote_status": "fresh",
        "mcap": 50_000,
        "liquidity_usd": 20_000,
        "price_usd": 1.0,
        "entry_score": 75,
        "pair_age_hours": 1,
        "change_m5": 3,
        "change_h1": 15,
        "gmgn_risk_flags": [],
        "watch_status": "active",
    }
    row.update(overrides)
    return row


def position_at(price, **overrides):
    position = {
        "chain": "bsc",
        "contract_address": "0xABC",
        "pool_address": "0xPOOL",
        "entry_price_usd": price,
        "entry_at": (NOW - timedelta(minutes=10)).isoformat(),
        "high_price_usd": price,
        "tp1_hit": False,
        "remaining_fraction": 1.0,
    }
    position.update(overrides)
    return position


def quote_at(price, **overrides):
    quote = {
        "chain": "bsc",
        "contract_address": "0xABC",
        "pool_address": "0xPOOL",
        "price_usd": price,
        "quote_at": NOW.isoformat(),
        "quote_status": "fresh",
    }
    quote.update(overrides)
    return quote


def test_scaled_notional_scales_and_caps_the_legacy_range():
    assert scaled_notional(100) == 35.0
    assert scaled_notional(200) == 55.0
    assert scaled_notional(10) == 3.5


def test_scaled_policy_freezes_100u_execution_limits():
    config = ExecutionConfig.from_env({"BSC_EXECUTION_CAPITAL_USD": "100"})
    assert config.reference_capital_usd == 100.0
    assert config.order_notional_usd == 5.0
    assert config.max_open_positions == 3
    assert config.max_exposure_usd == 15.0
    assert config.daily_loss_limit_usd == 8.0
    assert config.profile == "strict_45m"


def test_entry_requires_bsc_strict_first_discovery_and_fresh_quote():
    decision = entry_decision(valid_row(), NOW, ExecutionConfig.paper())
    assert decision.accepted is True
    assert decision.arm == "first_discovery"
    assert decision.notional_usd == 5.0


def test_entry_accepts_multi_source_narrative_breakout_arm():
    row = valid_row(
        execution_arm="narrative_breakout",
        mcap=668_900,
        liquidity_usd=303_000,
        change_h1=58,
        source_labels=["OKX", "GMGN", "DS"],
        sources=["okx_signal", "gmgn_trending"],
        entry_score=100,
    )
    decision = entry_decision(row, NOW, ExecutionConfig.paper())

    assert decision.accepted is True
    assert decision.arm == "narrative_breakout"


@pytest.mark.parametrize("arm,mcap", [("first_discovery", 50_000), ("narrative_breakout", 668_900)])
def test_rating_and_score_do_not_replace_entry_rules(arm, mcap):
    row = valid_row(execution_arm=arm, mcap=mcap, liquidity_usd=303_000,
                    source_groups=["okx", "gmgn"], entry_score=15, early_rating="C")
    assert entry_decision(row, NOW, ExecutionConfig.paper()).accepted


@pytest.mark.parametrize("m5,accepted", [(-25, True), (40, True), (45, True), (-26, False), (46, False)])
def test_live_momentum_uses_original_discovery_range(m5, accepted):
    assert entry_decision(valid_row(change_m5=m5), NOW, ExecutionConfig.paper()).accepted is accepted


def test_narrative_keeps_source_liquidity_and_overheating_limits():
    base = valid_row(execution_arm="narrative_breakout", mcap=668_900,
                     liquidity_usd=303_000, source_groups=["okx", "gmgn"], change_h1=58)
    for change in ({"source_groups": ["gmgn"]}, {"liquidity_usd": 29_999},
                   {"change_h1": 101}, {"mcap": 1_000_001},
                   {"risk_flags": ["bundler_39.2"]}):
        assert not entry_decision({**base, **change}, NOW, ExecutionConfig.paper()).accepted


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"chain": "robinhood"}, "bsc_only"),
        ({"contract_address": ""}, "missing_token_identity"),
        ({"pool_address": ""}, "missing_pool_identity"),
        ({"execution_arm": "pullback"}, "strict_first_discovery_only"),
        ({"signal_at": (NOW - timedelta(minutes=46)).isoformat()}, "stale_signal"),
        ({"quote_at": (NOW - timedelta(seconds=31)).isoformat()}, "stale_quote"),
        ({"quote_status": "quarantined"}, "quote_not_fresh"),
        ({"liquidity_usd": 7_999}, "low_liquidity"),
        ({"mcap": 300_001}, "first_discovery_mcap"),
        ({"watch_status": "invalidated"}, "invalidated_status"),
        ({"gmgn_risk_flags": ["honeypot"]}, "risk_flags"),
        ({"price_usd": 0}, "invalid_price"),
    ],
)
def test_entry_rejects_invalid_first_discovery_rows(change, reason):
    assert entry_decision({**valid_row(), **change}, NOW, ExecutionConfig.paper()).reason == reason


def test_entry_rejects_duplicate_and_exposure_overflow():
    duplicate = entry_decision({**valid_row(), "existing_token_keys": ["bsc:0xabc"]}, NOW, ExecutionConfig.paper())
    overflow = entry_decision({**valid_row(), "open_positions": 3}, NOW, ExecutionConfig.paper())
    assert duplicate.reason == "duplicate_token"
    assert overflow.reason == "exposure_limit"


@pytest.mark.parametrize(
    "field",
    ["open_positions", "current_exposure_usd", "daily_loss_usd"],
)
@pytest.mark.parametrize("value", [-1, float("nan"), "not-a-number", [], {}])
def test_entry_rejects_invalid_risk_counters_fail_closed(field, value):
    decision = entry_decision({**valid_row(), field: value}, NOW, ExecutionConfig.paper())
    assert decision.accepted is False
    assert decision.reason == "invalid_risk_state"


def test_uppercase_invalidated_watch_status_is_rejected():
    decision = entry_decision({**valid_row(), "watch_status": "INVALIDATED"}, NOW, ExecutionConfig.paper())
    assert decision.reason == "invalidated_status"


def test_expired_watch_status_does_not_override_explicit_execution_candidate():
    decision = entry_decision(
        {**valid_row(), "watch_status": "expired", "execution_candidate": True},
        NOW,
        ExecutionConfig.paper(),
    )
    assert decision.accepted is True


def test_missing_quote_status_is_not_fresh():
    row = valid_row()
    row.pop("quote_status")
    assert entry_decision(row, NOW, ExecutionConfig.paper()).reason == "quote_not_fresh"


def test_entry_rejects_non_positive_capital_or_order_notional():
    for capital in ("0", "-1"):
        config = ExecutionConfig.from_env({"BSC_EXECUTION_CAPITAL_USD": capital})
        assert config.order_notional_usd <= 0
        assert entry_decision(valid_row(), NOW, config).reason == "invalid_capital"
    config = ExecutionConfig.paper()
    zero_order = ExecutionConfig(**{**config.__dict__, "order_notional_usd": 0.0})
    assert entry_decision(valid_row(), NOW, zero_order).reason == "invalid_capital"


def test_exit_sells_eighty_percent_at_tp1_and_stops_all_at_minus_22():
    config = ExecutionConfig.paper()
    assert exit_decision(position_at(100.0), quote_at(200.0), NOW, config).sell_fraction == 0.8
    assert exit_decision(position_at(100.0), quote_at(78.0), NOW, config).sell_fraction == 1.0


def test_exit_applies_trailing_and_time_stop_to_remaining_position():
    config = ExecutionConfig.paper()
    trailing = exit_decision(
        position_at(100.0, high_price_usd=200.0, tp1_hit=True, remaining_fraction=0.2),
        quote_at(130.0), NOW, config,
    )
    timed = exit_decision(
        position_at(100.0, entry_at=(NOW - timedelta(minutes=91)).isoformat()),
        quote_at(110.0), NOW, config,
    )
    assert trailing.reason == "trailing_stop"
    assert trailing.sell_fraction == 0.2
    assert timed.reason == "time_stop"
    assert timed.sell_fraction == 1.0


@pytest.mark.parametrize(
    ("position_change", "quote_change", "reason"),
    [
        ({"chain": "ethereum"}, {}, "bsc_only"),
        ({}, {"chain": "ethereum"}, "bsc_only"),
        ({"contract_address": ""}, {}, "missing_token_identity"),
        ({}, {"contract_address": "0xDEF"}, "token_identity_mismatch"),
    ],
)
def test_exit_rejects_non_bsc_missing_or_mismatched_identity(position_change, quote_change, reason):
    result = exit_decision(
        {**position_at(100.0), **position_change},
        {**quote_at(200.0), **quote_change},
        NOW,
        ExecutionConfig.paper(),
    )
    assert result.exit is False
    assert result.reason == reason


def test_exit_requires_explicit_fresh_quote_status():
    quote = quote_at(200.0)
    quote.pop("quote_status")
    assert exit_decision(position_at(100.0), quote, NOW, ExecutionConfig.paper()).reason == "quote_not_fresh"


@pytest.mark.parametrize("pool_change", [{"pool_address": ""}, {"pool_address": "0xOTHER"}])
def test_exit_rejects_missing_or_changed_pool_identity(pool_change):
    result = exit_decision(
        position_at(100.0),
        {**quote_at(200.0), **pool_change},
        NOW,
        ExecutionConfig.paper(),
    )
    assert result.exit is False
    assert result.reason == ("missing_pool_identity" if not pool_change["pool_address"] else "pool_identity_mismatch")
