from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

import alpha_execution_audit as execution
import alpha_execution_challenger as challenger
from alpha_wallet_forward_paper import exclusive_lock


START = datetime(2026, 9, 8, tzinfo=timezone.utc)


def at(seconds=0):
    return (START + timedelta(seconds=seconds)).isoformat()


def row(seconds=0, price=1.0, **extra):
    return {"chain": "bsc", "contract_address": "0xabc", "symbol": "TEST",
            "pool_address": "0xpool", "quote_at": at(seconds),
            "quote_status": "fresh", "quote_fingerprint": f"q-{seconds}",
            "price_usd": price, "liquidity_usd": 20000,
            "pair_age_hours": 200, "volume5m": 1000,
            "buy_count5m": 10, "sell_count5m": 3,
            "change_m5": 3, "change_h1": 10, **extra}


def run(path, seconds, price=1, **extra):
    return challenger.run_once([row(seconds, price, **extra)], path, at(seconds))


def state_path(path):
    return path / challenger.DIRECTORY / "state.json"


def state(path):
    return json.loads(state_path(path).read_text(encoding="utf-8"))


def revival(path, final_extra=None):
    for seconds in (0, 300, 600, 900, 1200, 1500, 1740):
        run(path, seconds)
    run(path, 1770, 1.01)
    return run(path, 1800, 1.02, volume5m=2000, **(final_extra or {}))


def pullback(path, prices=(1, .7, .76, .72, .75, .8), final_extra=None):
    challenger.run_once([], path, at())
    for seconds, price in zip((1650, 1680, 1710, 1740, 1770, 1800), prices):
        result = run(path, seconds, price, pair_age_hours=12,
                     **((final_extra or {}) if seconds == 1800 else {}))
    return result


def test_no_initial_trades_or_backfilled_evidence_during_warmup(tmp_path):
    historical = [row(i, 1 + i / 10000) for i in range(-1800, 0, 60)]
    first = challenger.run_once(historical + [row()], tmp_path, at())
    assert first["warmup"] and first["cash_usd"] == 1000
    assert first["observation_count"] == 1
    assert first["events"] == []
    for seconds in (300, 600, 900, 1200, 1500, 1740, 1770, 1799):
        result = run(tmp_path, seconds, 1 + seconds / 10000, volume5m=10000)
        assert result["warmup"] and not result["events"]
    assert not state(tmp_path)["execution"]["orders"]
    assert state(tmp_path)["execution"]["assumptions"]["buy_notional_usd"] == 35


def test_empty_input_persists_original_warmup_clock(tmp_path):
    for seconds in (0, 900, 1799, 1800):
        result = challenger.run_once([], tmp_path, at(seconds))
        saved = state(tmp_path)
        assert result["started_at"] == saved["started_at"] == at()
        assert result["warmup"] is (seconds < 1800)
        assert saved["execution"]["last_run_at"] == at(seconds)
        assert result["observation_count"] == 0
        assert not result["events"] and result["cash_usd"] == 1000


@pytest.mark.parametrize("extra,seconds,reason", [
    ({"quote_at": at()}, 60, "stale_quote"),
    ({"quote_at": at()}, 20, "non_advancing_quote"),
    ({"quote_fingerprint": "q-0"}, 20, "duplicate_quote_fingerprint"),
    ({"pool_address": "0xother"}, 20, "pool_changed_pending_verification"),
    ({"pair_address": "0xother"}, 20, "source_quote_conflicting_identity"),
    ({"token_address": "0xdifferent"}, 20, "source_quote_conflicting_identity"),
    ({"quote_status": "stale"}, 20, "source_quote_stale"),
    ({"quote_at": at(21)}, 20, "future_quote"),
    ({"quote_observed_at": at()}, 20, "source_quote_conflicting_timestamp"),
])
def test_invalid_quotes_never_add_evidence(tmp_path, extra, seconds, reason):
    run(tmp_path, 0)
    result = run(tmp_path, seconds, **extra)
    assert result["observations_added"] == 0
    assert result["observation_count"] == 1
    assert result["exclusion_counts"][reason] == 1
    assert not result["events"]


def test_conflicting_volume_on_same_observation_is_not_evidence(tmp_path):
    result = challenger.run_once([row(), row(volume5m=9000)], tmp_path, at())
    assert result["observation_count"] == 0
    assert result["exclusion_counts"]["source_quote_conflicting_observation"] == 1


def test_older_fingerprint_with_rewritten_timestamp_cannot_be_reused(tmp_path):
    run(tmp_path, 0)
    run(tmp_path, 30)
    result = run(tmp_path, 60, quote_fingerprint="q-0")
    assert result["observation_count"] == 2
    assert result["observations_added"] == 0
    assert result["exclusion_counts"]["source_quote_reused_quote_fingerprint"] == 1


def test_pre_start_quote_is_not_forward_evidence(tmp_path):
    result = challenger.run_once([row(-10)], tmp_path, at())
    assert result["observation_count"] == 0
    assert result["exclusion_counts"]["quote_before_experiment"] == 1


def test_old_age_and_injected_scores_history_do_not_create_signal(tmp_path):
    challenger.run_once([], tmp_path, at())
    for seconds, price in ((1740, 1), (1770, 1.01), (1800, 1.02)):
        result = run(tmp_path, seconds, price, volume5m=100000,
                     entry_score=100, old_meme_revival_active=True,
                     old_meme_revival_score=100, observations=[row(i) for i in range(0, 1600, 60)])
    assert not result["events"]
    assert result["exclusion_counts"]["missing_volume_baseline"] == 1


def test_revival_uses_spaced_prior_median_and_next_quote_fill(tmp_path):
    result = revival(tmp_path)
    assert [e["type"] for e in result["events"]] == ["intent"]
    assert result["events"][0]["arm"] == "old_revival"
    evidence = result["signals"][0]
    assert evidence["volume_median"] == 1000
    assert evidence["volume_observations"] == 6
    assert evidence["volume_span_seconds"] == 1500
    assert all(execution.timestamp(o["quote_at"]) <= execution.timestamp(at(1500))
               for o in evidence["volume_baseline"])
    assert result["cash_usd"] == 1000
    filled = run(tmp_path, 1830, 1.03, volume5m=2000)
    assert [e["type"] for e in filled["events"]] == ["fill"]
    assert filled["positions"][0]["quantity"] == pytest.approx(35 / 1.03)
    assert filled["positions"][0]["entry_price_usd"] == 1.03


@pytest.mark.parametrize("field,value,reason", [
    ("change_h1", None, "missing_positive_h1"),
    ("change_h1", 0, "missing_positive_h1"),
    ("buy_count5m", 4, "missing_positive_buy_flow"),
    ("sell_count5m", 10, "missing_positive_buy_flow"),
    ("sell_count5m", None, "missing_positive_buy_flow"),
])
def test_revival_missing_current_evidence_rejects(tmp_path, field, value, reason):
    result = revival(tmp_path, {field: value})
    assert not result["events"]
    assert result["exclusion_counts"][reason] == 1


@pytest.mark.parametrize("flags,expected", [
    ([{"private_key": "NEVER-PERSIST"}], True),
    ({"unknown_flag": False}, True),
    ("NEVER-PERSIST", True),
    (True, True), (False, False), ([], False), ({}, False), (None, False),
])
def test_risk_whitelist_keeps_only_presence_boolean(flags, expected):
    sanitized = challenger.public_quote(row(gmgn_risk_flags=flags, market_data_pending=True))
    assert sanitized["gmgn_risk_flags"] is expected
    assert sanitized["market_data_pending"] is True
    assert "NEVER-PERSIST" not in json.dumps(sanitized)
    assert challenger.public_quote(row(market_data_pending=False))["market_data_pending"] is False


@pytest.mark.parametrize("arm", ["old_revival", "pullback"])
@pytest.mark.parametrize("extra", [
    {"gmgn_risk_flags": [{"secret": "NEVER-PERSIST"}]},
    {"market_data_pending": True},
])
def test_valid_rising_candidate_is_vetoed_by_risk_or_pending(tmp_path, arm, extra):
    result = (revival(tmp_path, extra) if arm == "old_revival" else
              pullback(tmp_path, final_extra=extra))
    assert result["signals"][0]["reason"] == "pending_data_or_risk_flags"
    assert result["exclusion_counts"]["pending_data_or_risk_flags"] == 1
    assert result["accepted_quotes"] == 1
    assert not result["events"] and result["candidate_count"] == 0
    assert "NEVER-PERSIST" not in state_path(tmp_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("extra", [
    {"gmgn_risk_flags": [{"secret": "NEVER-PERSIST"}]},
    {"market_data_pending": True},
])
def test_new_risk_cancels_pending_buy_before_old_snapshot_can_fill(tmp_path, monkeypatch, extra):
    intent = revival(tmp_path)
    snapshot = deepcopy(intent["pending_orders"][0]["signal_snapshot"])
    assert not snapshot.get("gmgn_risk_flags") and not snapshot.get("market_data_pending")
    original_step = execution.step
    calls = []

    def checked_step(inputs, engine, now_iso):
        calls.append(now_iso)
        assert not engine["orders"]
        assert inputs["execution_candidates"] == []
        assert len(inputs["execution_quotes"]) == 1
        assert inputs["execution_quotes"][0][next(iter(extra))] is True
        return original_step(inputs, engine, now_iso)

    monkeypatch.setattr(execution, "step", checked_step)
    result = run(tmp_path, 1830, 1.03, volume5m=2000, **extra)
    assert calls == [at(1830)]
    assert [(e["type"], e["reason"]) for e in result["events"]] == [
        ("cancel", "pending_data_or_risk_flags")]
    assert result["events"][0]["signal_snapshot"] == snapshot
    assert not result["positions"] and not result["pending_orders"]
    assert result["cash_usd"] == 1000
    for path in (tmp_path / challenger.DIRECTORY).glob("*.json"):
        assert "NEVER-PERSIST" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("risk_first", [False, True])
def test_duplicate_quote_risk_veto_is_order_independent(tmp_path, risk_first):
    revival(tmp_path)
    clean = row(1830, 1.03, volume5m=2000)
    risky = {**clean, "gmgn_risk_flags": ["flag"]}
    rows = [risky, clean] if risk_first else [clean, risky]
    result = challenger.run_once(rows, tmp_path, at(1830))
    assert result["accepted_quotes"] == 1
    assert [(e["type"], e["reason"]) for e in result["events"]] == [
        ("cancel", "pending_data_or_risk_flags")]
    assert result["cash_usd"] == 1000


@pytest.mark.parametrize("extra", [
    {"gmgn_risk_flags": [{"secret": "NEVER-PERSIST"}]},
    {"market_data_pending": True},
])
def test_risk_does_not_suppress_exit_quotes_or_sell_fills(tmp_path, extra):
    revival(tmp_path)
    run(tmp_path, 1830, 1.02, volume5m=2000)
    trigger = run(tmp_path, 1860, .7, **extra)
    assert trigger["accepted_quotes"] == 1
    assert trigger["events"][0]["side"] == "sell"
    assert trigger["events"][0]["reason"] == "stop_loss"
    assert trigger["equity_usd"] is not None
    result = run(tmp_path, 1890, .69, **extra)
    assert [(e["type"], e["side"]) for e in result["events"]] == [("fill", "sell")]
    assert result["events"][0]["quote"]["price_usd"] == .69
    assert not result["positions"]


@pytest.mark.parametrize("volume", [None, True, 1999])
def test_genuine_current_volume_required(tmp_path, volume):
    for seconds in (0, 300, 600, 900, 1200, 1500, 1740):
        run(tmp_path, seconds)
    run(tmp_path, 1770, 1.01)
    result = run(tmp_path, 1800, 1.02, volume5m=volume, volume24h=10000000)
    assert not result["events"]
    assert result["exclusion_counts"]["missing_volume_expansion"] == 1


@pytest.mark.parametrize("baseline_times", [range(1400, 1520, 20), range(1200, 1560, 60)])
def test_dense_or_short_baseline_cannot_meet_fifteen_minute_span(tmp_path, baseline_times):
    challenger.run_once([], tmp_path, at())
    for seconds in baseline_times:
        run(tmp_path, seconds)
    run(tmp_path, 1740)
    run(tmp_path, 1770, 1.01)
    result = run(tmp_path, 1800, 1.02, volume5m=2000)
    assert not result["events"]
    assert result["exclusion_counts"]["missing_volume_baseline"] == 1


def test_pullback_requires_confirmed_higher_low_and_reclaim(tmp_path):
    result = pullback(tmp_path)
    assert [e["arm"] for e in result["events"]] == ["pullback"]
    evidence = result["signals"][0]
    assert evidence["drawdown_pct"] == pytest.approx(20)
    assert evidence["low"]["price_usd"] == .7
    assert evidence["higher_low"]["price_usd"] == .72
    assert evidence["bounce"]["price_usd"] == .76


@pytest.mark.parametrize("prices,drawdown", [
    ((1, .7, .76, .72, .75, .9), 10),
    ((1, .4, .5, .45, .48, .6), 40),
])
def test_pullback_drawdown_boundaries_are_inclusive(tmp_path, prices, drawdown):
    result = pullback(tmp_path, prices)
    assert result["signals"][0]["drawdown_pct"] == drawdown
    assert result["events"][0]["type"] == "intent"


@pytest.mark.parametrize("prices", [
    (1, .7, .76, .69, .75, .8),  # Lower low.
    (1, .7, .85, .72, .75, .8),  # Prior bounce not reclaimed.
    (1, .7, .76, .72, .8, .79),  # Interrupted consecutive rise.
    (1, .7, .76, .72, .8, .95),  # Less than 10 percent drawdown.
    (1, .4, .45, .42, .44, .5),  # More than 40 percent drawdown.
])
def test_pullback_missing_structure_rejects(tmp_path, prices):
    result = pullback(tmp_path, prices)
    assert not result["events"]
    assert not result["signals"][0]["eligible"]


def test_tightly_spaced_rises_and_foreign_token_cannot_confirm(tmp_path):
    challenger.run_once([], tmp_path, at())
    for seconds in (0, 300, 600, 900, 1200, 1500):
        if seconds:
            run(tmp_path, seconds)
    run(tmp_path, 1780)
    run(tmp_path, 1790, 1.01)
    result = run(tmp_path, 1800, 1.02, volume5m=2000)
    assert result["exclusion_counts"]["missing_consecutive_rises"] == 1
    other = run(tmp_path, 1830, 1.03, contract_address="0xother", volume5m=2000)
    assert not other["events"]
    assert other["signals"][0]["key"] == "bsc:0xother"


def ten_second_cadence(path, arm, replacements=None):
    prices = ({1740: .7, 1750: .76, 1760: .72, 1770: .77,
               1780: .78, 1790: .79, 1800: .8} if arm == "pullback" else
              {1760: 1, 1770: 1.005, 1780: 1.01, 1790: 1.015, 1800: 1.02})
    prices.update(replacements or {})
    for seconds in range(0, 1801, 10):
        result = run(path, seconds, prices.get(seconds, 1),
                     pair_age_hours=12 if arm == "pullback" else 200,
                     volume5m=2000 if seconds == 1800 else 1000)
        if seconds < 1800:
            assert result["warmup"] and not result["events"]
    return result


@pytest.mark.parametrize("arm", ["old_revival", "pullback"])
def test_ten_second_cadence_selects_latest_twenty_second_rises(tmp_path, arm):
    result = ten_second_cadence(tmp_path, arm)
    assert [(e["type"], e["arm"]) for e in result["events"]] == [("intent", arm)]
    evidence = result["signals"][0]
    assert [o["quote_at"] for o in evidence["rising_quotes"]] == [at(1760), at(1780), at(1800)]
    if arm == "pullback":
        assert evidence["higher_low"]["quote_at"] == at(1760)
        assert evidence["bounce"]["quote_at"] == at(1750)
        assert evidence["bounce"]["price_usd"] == .76
    filled = run(tmp_path, 1810, evidence["signal_price_usd"],
                 pair_age_hours=12 if arm == "pullback" else 200, volume5m=2000)
    assert filled["events"][0]["type"] == "fill"


@pytest.mark.parametrize("arm,seconds,price", [
    ("old_revival", 1770, .995),
    ("old_revival", 1770, 1.015),
    ("old_revival", 1790, 1.005),
    ("old_revival", 1790, 1.025),
    ("pullback", 1770, .69),
    ("pullback", 1770, .79),
    ("pullback", 1790, .75),
    ("pullback", 1790, .81),
])
def test_ten_second_intermediate_reversal_cannot_be_sampled_away(tmp_path, arm, seconds, price):
    result = ten_second_cadence(tmp_path, arm, {seconds: price})
    assert result["exclusion_counts"]["missing_consecutive_rises"] == 1
    assert not result["events"] and not result["signals"][0]["eligible"]


def test_drift_cancels_before_step_and_does_not_replace_intent(tmp_path, monkeypatch):
    revival(tmp_path)
    original_step = execution.step
    seen = []

    def checked_step(inputs, engine, now_iso):
        seen.append(True)
        assert not engine["orders"]
        assert inputs["execution_candidates"] == []
        return original_step(inputs, engine, now_iso)

    monkeypatch.setattr(execution, "step", checked_step)
    result = run(tmp_path, 1830, 1.08, volume5m=2000)
    assert seen
    assert result["cash_usd"] == 1000 and not result["positions"]
    assert [e["type"] for e in result["events"]] == ["cancel"]
    assert result["events"][0]["reason"] == "entry_price_drift"
    assert result["events"][0]["signal_price_usd"] == 1.02


def test_drift_is_measured_from_original_intent_not_previous_quote(tmp_path):
    revival(tmp_path)
    repeated = run(tmp_path, 1810, 1.02, quote_fingerprint="q-1800", volume5m=2000)
    assert not repeated["events"]
    result = run(tmp_path, 1830, 1.02 * 1.05, volume5m=2000)
    assert result["events"][0]["type"] == "fill"


def test_exits_receive_quotes_even_when_no_entry_signal_and_cooldown_uses_exit(tmp_path):
    revival(tmp_path)
    run(tmp_path, 1830, 1.02, volume5m=2000)
    triggered = run(tmp_path, 7230, 1.02, buy_count5m=0, volume5m=None)
    assert triggered["events"][0]["reason"] == "time_stop"
    missing = challenger.run_once([], tmp_path, at(7235))
    assert missing["equity_usd"] is None
    assert missing["pending_orders"][0]["side"] == "sell"
    exited = run(tmp_path, 7260, 1.02, buy_count5m=0, volume5m=None)
    assert exited["events"][0]["side"] == "sell"
    closed = state(tmp_path)["execution"]["closed"][0]
    assert closed["exit_at"] == at(7260)
    for seconds in (7500, 7800, 8100, 8400, 8700, 9000, 9240):
        run(tmp_path, seconds)
    run(tmp_path, 9270, 1.01)
    blocked = run(tmp_path, 9300, 1.02, volume5m=2000)
    assert blocked["exclusion_counts"]["reentry_cooldown"] == 1
    assert not blocked["events"]
    engine = state(tmp_path)["execution"]
    assert challenger.cooling_down(engine, "bsc:0xabc", "old_revival", execution.timestamp(at(28859)))
    assert not challenger.cooling_down(engine, "bsc:0xabc", "old_revival", execution.timestamp(at(28860)))
    assert not challenger.cooling_down(engine, "bsc:0xabc", "pullback", execution.timestamp(at(9300)))


def test_standalone_state_isolation_and_public_whitelist(tmp_path):
    baseline = tmp_path / "alpha-execution-state.json"
    baseline.write_bytes(b'{"cash_usd": 17}')
    active = tmp_path / "profitable-wallet-paper"
    active.mkdir()
    (active / "state.json").write_bytes(b"untouched")
    raw = row(secret="NEVER-PERSIST", private_key="NEVER-PERSIST",
              smart_money_evidence={"secret": "NEVER-PERSIST"},
              verification={"secret": "NEVER-PERSIST"})
    original = deepcopy(raw)
    result = challenger.run_once([raw], tmp_path, at())
    assert raw == original
    assert result["cash_usd"] == 1000
    assert baseline.read_bytes() == b'{"cash_usd": 17}'
    assert (active / "state.json").read_bytes() == b"untouched"
    for path in (tmp_path / challenger.DIRECTORY).glob("*.json"):
        assert "NEVER-PERSIST" not in path.read_text(encoding="utf-8")
    assert state(tmp_path)["execution"]["assumptions"] == execution.ASSUMPTIONS
    assert {p.name for p in tmp_path.iterdir()} == {
        "alpha-execution-state.json", "profitable-wallet-paper", challenger.DIRECTORY}


def test_fixed_config_corruption_clock_and_exclusive_lock(tmp_path):
    run(tmp_path, 0)
    before = state_path(tmp_path).read_bytes()
    for seconds in (0, -1):
        result = run(tmp_path, seconds)
        assert result["status"] == "ignored_non_advancing_observation"
        assert result["events"] == [] and result["observations_added"] == 0
        assert state_path(tmp_path).read_bytes() == before
    with pytest.raises(ValueError):
        challenger.run_once([], tmp_path, "2026-09-08T00:00:00")
    with exclusive_lock(tmp_path / challenger.DIRECTORY / "writer.lock"):
        with pytest.raises(OSError):
            run(tmp_path, 30)
    saved = state(tmp_path)
    saved["config"]["max_entry_drift"] = .99
    state_path(tmp_path).write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="fixed configuration"):
        run(tmp_path, 60)
    state_path(tmp_path).write_text("broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        run(tmp_path, 90)


def test_fast_worker_aliases_are_supported(tmp_path):
    native = row()
    worker = {k: v for k, v in native.items() if k not in
              {"pool_address", "liquidity_usd", "quote_at"}}
    worker.update(pair_address=native["pool_address"], liquidity=native["liquidity_usd"],
                  quote_observed_at=native["quote_at"])
    result = challenger.run_once([worker], tmp_path, at())
    assert result["observations_added"] == 1
    assert state(tmp_path)["execution"]["quotes"]["bsc:0xabc"]["pool_address"] == "0xpool"
