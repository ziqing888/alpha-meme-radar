import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("strict_execution", Path(__file__).with_name("alpha_execution_audit.py"))
paper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paper)

BASE = datetime.fromisoformat("2026-09-07T10:00:00+08:00")


def at(seconds=0):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def candidate(token="0xAbC", arm="first_discovery", **extra):
    row = {"chain": "bsc", "contract_address": token, "symbol": "SAME",
           "execution_arm": arm, "signal_at": at(), "entry_score": 75,
           "first_seen_at": at(-600), "pair_age_hours": 1,
           "mcap": 50000, "change_m5": 3, "change_h1": 15}
    if arm == "pullback":
        row.update(drawdown_from_peak_pct=-20)
    elif arm == "old_revival":
        row.update(pair_age_hours=200, old_meme_revival_active=True, old_meme_revival_score=75)
    return {**row, **extra}


def quote(seconds=0, price=1, token="0xabc", **extra):
    return {"chain": "bnb", "contract_address": token, "pool_address": "0xPool",
            "quote_at": at(seconds), "price_usd": price, "liquidity_usd": 20000, **extra}


def report(seconds=0, price=1, candidates=None, **qextra):
    return {"execution_quotes": [quote(seconds, price, **qextra)],
            "execution_candidates": [candidate()] if candidates is None else candidates}


def state(path):
    return json.loads((path / "alpha-execution-state.json").read_text())


def open_position(path):
    paper.run_once(report(), path, at())
    return paper.run_once(report(10), path, at(10))


def test_decision_then_next_observation_fill_and_restart_idempotence(tmp_path):
    original = report()
    before = deepcopy(original)
    first = paper.run_once(original, tmp_path, at())
    assert original == before
    assert [e["type"] for e in first["events"]] == ["intent"]
    assert first["arms"]["first_discovery"]["open"] == 0
    repeated = paper.run_once(report(0, 999), tmp_path, at())
    assert repeated["status"] == "ignored_non_advancing_observation"
    assert not repeated["events"]
    reused = paper.run_once(report(), tmp_path, at(1))
    assert not reused["events"]
    filled = paper.run_once(report(10, 1.1), tmp_path, at(10))
    assert [e["type"] for e in filled["events"]] == ["fill"]
    assert filled["events"][0]["quote"]["price_usd"] == 1.1
    assert len(state(tmp_path)["positions"]) == 1
    assert len((tmp_path / "alpha-execution-events.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("bad,reason", [
    ({"quote_at": None}, "missing_quote_timestamp"),
    ({"quote_at": "2026-09-07T10:00:00"}, "missing_quote_timestamp"),
    ({"quote_at": at(-31)}, "stale_quote"),
    ({"quote_at": at(1)}, "future_quote"),
    ({"pool_address": ""}, "missing_pool_identity"),
    ({"liquidity_usd": None}, "missing_or_low_liquidity"),
    ({"price_usd": float("nan")}, "invalid_price"),
    ({"quote_status": "quarantined"}, "source_quote_quarantined"),
])
def test_bad_quotes_never_create_orders(tmp_path, bad, reason):
    result = paper.run_once(report(**bad), tmp_path, at())
    assert result["status"] == "pending_quotes"
    assert reason in result["quote_issues"].values()
    assert not state(tmp_path)["orders"]


def test_canonical_identity_never_symbol_or_cross_chain(tmp_path):
    quotes = [quote(token="0xABC"), quote(price=999, token="0xother"),
              quote(price=888, chain="base")]
    paper.run_once({"execution_quotes": quotes, "execution_candidates": [candidate()]}, tmp_path, at())
    result = paper.run_once(report(10, 1.1), tmp_path, at(10))
    assert result["events"][0]["quote"]["price_usd"] == 1.1
    assert paper.token_key({"chain": "sol", "contract_address": "AbCd"}) != paper.token_key({"chain": "solana", "contract_address": "abcd"})


def test_pool_is_pinned_and_duplicate_conflicts_are_rejected(tmp_path):
    open_position(tmp_path)
    changed = paper.run_once(report(20, 9, pool_address="0xother"), tmp_path, at(20))
    assert "pool_changed_pending_verification" in changed["quote_issues"].values()
    assert changed["equity_usd"] is None
    result = paper.run_once({"execution_quotes": [quote(30, 1), quote(30, 2)]}, tmp_path, at(30))
    assert "conflicting_quote" in result["quote_issues"].values()


def test_missing_exit_quote_keeps_quantity_cash_and_pending_valuation(tmp_path):
    opened = open_position(tmp_path)
    initial = state(tmp_path)
    missing = paper.run_once({}, tmp_path, at(5500))
    assert missing["cash_usd"] == opened["cash_usd"]
    assert missing["equity_usd"] is None
    assert missing["pending_valuations"] == 1
    assert [e["side"] for e in missing["events"]] == ["sell"]
    assert all(e["type"] == "intent" for e in missing["events"])
    oid = next(iter(initial["positions"]))
    assert state(tmp_path)["positions"][oid]["quantity"] == initial["positions"][oid]["quantity"]
    stale = paper.run_once(report(10, candidates=[]), tmp_path, at(5501))
    assert not stale["events"]
    filled = paper.run_once(report(5502, 0.8, candidates=[]), tmp_path, at(5502))
    assert filled["events"][0]["type"] == "fill"
    assert filled["events"][0]["quote"]["price_usd"] == 0.8


def test_sell_fill_uses_next_quote_and_costs_on_sell_proceeds(tmp_path):
    open_position(tmp_path)
    trigger = paper.run_once(report(20, 2.1, candidates=[]), tmp_path, at(20))
    assert trigger["events"][0]["type"] == "intent"
    assert not state(tmp_path)["closed"]
    filled = paper.run_once(report(30, 2.2, candidates=[]), tmp_path, at(30))
    sell = filled["events"][0]
    assert sell["quote"]["price_usd"] == 2.2
    assert sell["costs"]["fee_usd"] == pytest.approx(35 * 2.2 * 0.003)
    closed = state(tmp_path)["closed"][0]
    assert closed["sell_costs"]["tax_usd"] == pytest.approx(77 * 0.01)
    assert closed["sell_costs"]["depth_usd"] == pytest.approx(77 * 77 / 10000)
    assert filled["cash_usd"] == pytest.approx(1000 + closed["pnl_usd"])
    assert closed["pnl_usd"] == pytest.approx(77 - closed["sell_costs"]["total_usd"] - 35 - closed["buy_costs"]["total_usd"])


@pytest.mark.parametrize("jump", [10, 0.1])
def test_abnormal_jumps_remain_pending_without_independent_verification(tmp_path, jump):
    open_position(tmp_path)
    for seconds in (20, 30, 40):
        result = paper.run_once(report(seconds, jump, candidates=[]), tmp_path, at(seconds))
        assert "abnormal_jump_pending_verification" in result["quote_issues"].values()
        assert result["equity_usd"] is None
        assert not result["events"]
    assert not state(tmp_path)["closed"]
    proof = {"quote_at": at(20), "pool_address": "0xpool", "confirmed_at": at(45), "source": "independent-pool-check"}
    verified = paper.run_once(report(50, jump, candidates=[], verification=proof), tmp_path, at(50))
    assert verified["events"][0]["type"] == "intent"
    assert not state(tmp_path)["closed"]


def test_pending_sell_cannot_fill_on_unverified_jump(tmp_path):
    open_position(tmp_path)
    paper.run_once(report(20, 2.1, candidates=[]), tmp_path, at(20))
    jump = paper.run_once(report(30, 100, candidates=[]), tmp_path, at(30))
    assert not jump["events"]
    assert len(state(tmp_path)["orders"]) == 1
    normal = paper.run_once(report(40, 2.0, candidates=[]), tmp_path, at(40))
    assert normal["events"][0]["quote"]["price_usd"] == 2


def test_depth_limit_does_not_generate_fill(tmp_path):
    open_position(tmp_path)
    paper.run_once(report(20, 2.1, candidates=[]), tmp_path, at(20))
    result = paper.run_once(report(30, 5, candidates=[], liquidity_usd=8000), tmp_path, at(30))
    assert not result["events"]
    assert "depth_limit_pending" in result["quote_issues"].values()
    assert result["equity_usd"] is None


def test_distinct_arms_multiple_orders_same_day_and_full_loss_budget(tmp_path):
    rows = [candidate(f"0x{i}", arm) for arm in paper.ARMS for i in range(4)]
    qs = [quote(token=f"0x{i}") for i in range(4)]
    result = paper.run_once({"execution_candidates": rows, "execution_quotes": qs}, tmp_path, at())
    assert len(result["events"]) == 6
    assert all(a["pending_buys"] == 2 for a in result["arms"].values())
    assert all(a["risk_reserved_usd"] <= 100 for a in result["arms"].values())
    assert sum(a["risk_reserved_usd"] for a in result["arms"].values()) <= 300
    next_quotes = [quote(10, token=f"0x{i}") for i in range(4)]
    filled = paper.run_once({"execution_quotes": next_quotes}, tmp_path, at(10))
    assert len(filled["events"]) == 6
    assert all(a["open"] == 2 for a in filled["arms"].values())


@pytest.mark.parametrize("extra,reason", [
    ({"watch_ticket_stage": "late"}, "wrong_first_discovery_stage"),
    ({"first_seen_at": at(-2800)}, "first_discovery_window"),
    ({"pair_age_hours": 200}, "first_discovery_age_or_mcap"),
    ({"signal_at": None}, "missing_or_stale_signal_timestamp"),
    ({"execution_arm": "pullback"}, "pullback_reclaim_gate"),
    ({"execution_arm": "old_revival"}, "old_revival_gate"),
])
def test_strategy_gates_do_not_promote_wrong_stage(tmp_path, extra, reason):
    result = paper.run_once(report(candidates=[candidate(**extra)]), tmp_path, at())
    assert result["candidate_gates"][0]["reason"] == reason
    assert not result["events"]


def test_expired_intents_release_risk_without_fake_fill(tmp_path):
    paper.run_once(report(), tmp_path, at())
    result = paper.run_once({}, tmp_path, at(901))
    assert result["events"][0]["type"] == "cancel"
    assert result["cash_usd"] == 1000
    assert result["arms"]["first_discovery"]["risk_reserved_usd"] == 0


def test_fast_worker_schema_and_fingerprint(tmp_path):
    row = {"chain": "bsc", "contract_address": "0xabc", "price_usd": 1,
           "liquidity": 20000, "pair_address": "0xpool", "quote_observed_at": at(),
           "quote_status": "fresh", "quote_source": "dexscreener", "quote_fingerprint": "one"}
    paper.run_once({"meme_rows": [row], "execution_candidates": [candidate()]}, tmp_path, at())
    row["quote_observed_at"] = at(10)
    duplicate = paper.run_once({"meme_rows": [row]}, tmp_path, at(10))
    assert not duplicate["events"]
    assert "duplicate_quote_fingerprint" in duplicate["quote_issues"].values()
    row.update(quote_observed_at=at(20), quote_fingerprint="two")
    filled = paper.run_once({"meme_rows": [row]}, tmp_path, at(20))
    assert filled["events"][0]["type"] == "fill"
    row.update(quote_observed_at=at(30), quote_fingerprint="three", quote_status="stale")
    stale = paper.run_once({"meme_rows": [row]}, tmp_path, at(30))
    assert stale["equity_usd"] is None


def test_replay_is_causal_and_does_not_force_final_liquidation():
    frames = [{"observed_at": at(i), "report": report(i, price, candidates=[candidate()] if i == 0 else [])}
              for i, price in ((0, 1), (10, 1), (20, 2.1), (30, 2.2))]
    prefix = paper.causal_replay(frames[:2])
    full = paper.causal_replay(list(reversed(frames)))
    assert full["events"][:len(prefix["events"])] == prefix["events"]
    assert prefix["arms"]["first_discovery"]["open"] == 1
    assert prefix["arms"]["first_discovery"]["closed"] == 0
    assert full["arms"]["first_discovery"]["closed"] == 1
    same_time = paper.causal_replay([frames[0], frames[0]])
    assert all(e["type"] != "fill" for e in same_time["events"])


def test_legacy_history_cannot_invent_quote_timestamp_pool_or_liquidity():
    history = {"rows": {"bsc:0xabc": {"chain": "bsc", "contract_address": "0xabc",
        "first_snapshot": candidate(), "observations": [{"seen_at": at(), "price_usd": 1},
                                                         {"seen_at": at(10), "price_usd": 100}]}}}
    result = paper.causal_replay(history)
    assert result["status"] == "no_usable_data"
    assert result["skipped"]["missing_quote_timestamp"] == 2
    assert not result["events"]
    assert result["realized_pnl_usd"] == 0
    missing = paper.causal_replay([{"report": report()}])
    assert missing["status"] == "no_usable_data"
    assert missing["skipped"]["missing_observation_timestamp"] == 1


def test_audit_ranks_pnl_and_marks_missing_quote_exits_and_jumps():
    ledger = {"closed_positions": [{"key": "small", "realized_pnl_usd": 1},
                                   {"key": "big", "realized_pnl_usd": 400}],
              "events": [{"type": "open", "key": "big", "time": at(), "price_usd": 1},
                         {"type": "take_profit_1", "key": "big", "time": at(10), "price_usd": 20, "pnl_usd": 400},
                         {"type": "time_stop_exit", "key": "small", "time": at(20), "price_usd": 1, "reason": "missing quote; last price"}]}
    before = deepcopy(ledger)
    audit = paper.audit_ledger(ledger)
    assert audit["largest_pnl_claims"][0]["key"] == "big"
    assert "abnormal_execution_jump" in audit["findings"][0]["flags"]
    assert "missing_quote_fallback_exit" in audit["findings"][1]["flags"]
    assert audit["validated_pnl_usd"] is None
    assert ledger == before


def test_only_separate_files_and_corrupt_state_fail_closed(tmp_path):
    old = tmp_path / "alpha-first-discovery-paper-state.json"
    old.write_text('{"cash_usd": 123}')
    paper.run_once({}, tmp_path, at())
    assert old.read_text() == '{"cash_usd": 123}'
    assert {p.name for p in tmp_path.iterdir()} == {old.name, "alpha-execution-state.json",
        "alpha-execution-events.jsonl", "alpha-execution-report.json", "alpha-execution.lock"}
    (tmp_path / "alpha-execution-state.json").write_text("broken")
    with pytest.raises(json.JSONDecodeError):
        paper.run_once({}, tmp_path, at(10))
    assert (tmp_path / "alpha-execution.lock").exists()
    assert old.read_text() == '{"cash_usd": 123}'


def test_lock_and_invalid_clock_do_not_mutate_state(tmp_path):
    lock = tmp_path / "alpha-execution.lock"
    lock.touch()
    paper.run_once({}, tmp_path, at())
    assert lock.exists()
    with paper.exclusive_lock(lock):
        with pytest.raises(OSError):
            paper.run_once({}, tmp_path, at(1))
    with pytest.raises(ValueError):
        paper.run_once({}, tmp_path, "2026-09-07T10:00:00")


def test_crashed_execution_process_releases_os_lock(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "alpha-execution.lock"
    script = "import os, pathlib; from alpha_execution_audit import exclusive_lock; "
    script += "lock = exclusive_lock(pathlib.Path(__import__('sys').argv[1])); lock.__enter__(); os._exit(0)"
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(paper.__file__).parent,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    with paper.exclusive_lock(path):
        pass


def worker_row(seconds=0, price=1, **extra):
    return {"chain": "bsc", "contract_address": "0xabc", "price_usd": price,
            "mcap": 50000, "liquidity": 20000, "pair_address": "0xpool",
            "quote_observed_at": at(seconds), "quote_status": "fresh",
            "quote_fingerprint": f"observation-{seconds}", "pair_age_hours": 1,
            "buy_count5m": 10, "sell_count5m": 3, "volume5m": 1000,
            "change_m5": 3, "change_h1": 15, "first_seen_at": at(-600), **extra}


def test_worker_raw_rows_generate_independent_flow_signals(tmp_path):
    first = paper.run_once({"meme_rows": [worker_row()]}, tmp_path, at())
    assert first["events"][0]["arm"] == "first_discovery"
    assert first["events"][0]["type"] == "intent"
    filled = paper.run_once({"meme_rows": [worker_row(10)]}, tmp_path, at(10))
    assert filled["events"][0]["type"] == "fill"
    assert filled["positions"][0]["quantity"] == 35
    assert filled["pending_orders"] == []


def test_worker_old_radar_score_cannot_refresh_a_dead_signal(tmp_path):
    row = worker_row(entry_score=100, buy_count5m=0, volume5m=0)
    result = paper.run_once({"meme_rows": [row]}, tmp_path, at())
    assert not result["events"]
    assert result["candidate_gates"][0]["reason"] == "insufficient_entry_score"


def test_worker_pullback_uses_only_previously_accepted_peak(tmp_path):
    paper.run_once({"meme_rows": [worker_row(0, 1, buy_count5m=0)]}, tmp_path, at())
    result = paper.run_once({"meme_rows": [worker_row(10, 0.8)]}, tmp_path, at(10))
    assert result["events"][0]["arm"] == "pullback"
    assert result["arms"]["first_discovery"]["pending_buys"] == 0


def test_worker_old_revival_is_separate_and_missing_discovery_time_is_not_invented(tmp_path):
    young = paper.run_once({"meme_rows": [worker_row(first_seen_at=None)]}, tmp_path, at())
    assert young["candidate_gates"][0]["reason"] == "first_discovery_window"
    old = paper.run_once({"meme_rows": [worker_row(10, pair_age_hours=200)]}, tmp_path, at(10))
    assert old["events"][0]["arm"] == "old_revival"
    assert old["arms"]["first_discovery"]["pending_buys"] == 0


def test_worker_frames_replay_same_as_forward_api(tmp_path):
    frames = [{"observed_at": at(i), "report": {"meme_rows": [worker_row(i, price)]}}
              for i, price in ((0, 1), (10, 1), (20, 2.1), (30, 2.2))]
    for frame in frames:
        forward = paper.run_once(frame["report"], tmp_path, frame["observed_at"])
    replay = paper.causal_replay(frames)
    assert replay["arms"] == forward["arms"]
    assert replay["cash_usd"] == forward["cash_usd"]
    assert replay["events"] == state(tmp_path)["events"]


def test_duplicate_fingerprint_values_fresh_cache_without_refreshing_timestamp(tmp_path):
    paper.run_once({"meme_rows": [worker_row()]}, tmp_path, at())
    opened = paper.run_once({"meme_rows": [worker_row(10)]}, tmp_path, at(10))
    for seconds in (20, 40):
        result = paper.run_once({"meme_rows": [worker_row(seconds, quote_fingerprint="observation-10")]}, tmp_path, at(seconds))
        assert result["events"] == []
        assert result["equity_usd"] == opened["equity_usd"]
        assert result["pending_valuations"] == 0
        position = result["positions"][0]
        assert position["valuation_quote_at"] == at(10)
        assert position["valuation_quote_age_seconds"] == seconds - 10
        assert position["valuation_quote_reused"] is True
        assert position["valuation_price_usd"] == 1
        assert state(tmp_path)["quotes"]["bsc:0xabc"]["quote_at"] == at(10)
    expired = paper.run_once({"meme_rows": [worker_row(41, quote_fingerprint="observation-10")]}, tmp_path, at(41))
    assert expired["equity_usd"] is None
    assert expired["pending_valuations"] == 1
    assert expired["positions"][0]["valuation_quote_at"] is None
    assert expired["events"] == []


def test_non_advancing_quote_can_mark_without_filling(tmp_path):
    opened = open_position(tmp_path)
    repeated = paper.run_once(report(10, candidates=[]), tmp_path, at(20))
    assert repeated["equity_usd"] == opened["equity_usd"]
    assert repeated["positions"][0]["valuation_quote_reused"] is True
    assert repeated["positions"][0]["valuation_quote_at"] == at(10)
    assert repeated["events"] == []


def test_cached_valuation_cannot_fill_pending_sell(tmp_path):
    paper.run_once({"meme_rows": [worker_row()]}, tmp_path, at())
    paper.run_once({"meme_rows": [worker_row(10)]}, tmp_path, at(10))
    trigger = paper.run_once({"meme_rows": [worker_row(20, 2.1)]}, tmp_path, at(20))
    assert trigger["events"][0]["side"] == "sell"
    repeated = paper.run_once({"meme_rows": [worker_row(30, 2.1, quote_fingerprint="observation-20")]}, tmp_path, at(30))
    assert repeated["events"] == []
    assert repeated["equity_usd"] == trigger["equity_usd"]
    assert repeated["cash_usd"] == trigger["cash_usd"]
    assert repeated["realized_pnl_usd"] == 0
    assert len(repeated["pending_orders"]) == 1
    filled = paper.run_once({"meme_rows": [worker_row(40, 2.2)]}, tmp_path, at(40))
    assert filled["events"][0]["type"] == "fill"
    assert filled["events"][0]["quote"]["quote_at"] == at(40)


@pytest.mark.parametrize("status", ["stale", "quarantined", "unavailable"])
def test_source_invalid_status_cannot_reuse_fresh_cache(tmp_path, status):
    paper.run_once({"meme_rows": [worker_row()]}, tmp_path, at())
    paper.run_once({"meme_rows": [worker_row(10)]}, tmp_path, at(10))
    result = paper.run_once({"meme_rows": [worker_row(20, quote_status=status, quote_fingerprint="observation-10")]}, tmp_path, at(20))
    assert result["equity_usd"] is None
    assert result["positions"][0]["valuation_quote_reused"] is False
    assert result["events"] == []


def test_quarantined_jump_cannot_be_hidden_by_duplicate_cache(tmp_path):
    paper.run_once({"meme_rows": [worker_row()]}, tmp_path, at())
    paper.run_once({"meme_rows": [worker_row(10)]}, tmp_path, at(10))
    paper.run_once({"meme_rows": [worker_row(20, 10)]}, tmp_path, at(20))
    result = paper.run_once({"meme_rows": [worker_row(25, quote_fingerprint="observation-10")]}, tmp_path, at(25))
    assert result["equity_usd"] is None
    assert result["events"] == []


@pytest.mark.parametrize("extra", [{"price_usd": 10}, {"liquidity": 10000}])
def test_conflicting_duplicate_fingerprint_stays_pending(tmp_path, extra):
    paper.run_once({"meme_rows": [worker_row()]}, tmp_path, at())
    paper.run_once({"meme_rows": [worker_row(10)]}, tmp_path, at(10))
    result = paper.run_once({"meme_rows": [worker_row(20, quote_fingerprint="observation-10", **extra)]}, tmp_path, at(20))
    assert "conflicting_quote" in result["quote_issues"].values()
    assert result["equity_usd"] is None
    assert result["events"] == []


def test_frontend_aggregate_realized_pnl_is_persisted_and_sums_arms(tmp_path):
    rows = [candidate(), candidate(arm="pullback")]
    paper.run_once(report(candidates=rows), tmp_path, at())
    paper.run_once(report(10, candidates=[]), tmp_path, at(10))
    paper.run_once(report(20, 2.1, candidates=[]), tmp_path, at(20))
    result = paper.run_once(report(30, 2.2, candidates=[]), tmp_path, at(30))
    total = sum(p["pnl_usd"] for p in state(tmp_path)["closed"])
    assert total > 0
    assert result["realized_pnl_usd"] == pytest.approx(total)
    assert state(tmp_path)["realized_pnl_usd"] == pytest.approx(total)
    assert sum(a["realized_pnl_usd"] for a in result["arms"].values()) == pytest.approx(total)
    saved_report = json.loads((tmp_path / "alpha-execution-report.json").read_text())
    assert saved_report["realized_pnl_usd"] == pytest.approx(total)


def test_execution_state_compacts_and_caps_rebuildable_quotes():
    current = paper.new_state()
    pinned_key = "bsc:0x" + "f" * 40
    for index in range(paper.MAX_PERSISTED_QUOTES + 5):
        token = "0x" + f"{index:040x}"
        key = "bsc:" + token
        current["quotes"][key] = quote(
            index,
            token=token,
            quote_fingerprint=f"quote-{index}",
            smart_money_evidence={"payload": "x" * 1000},
        )
    current["quotes"][pinned_key] = quote(
        -paper.EXECUTION_QUOTE_RETENTION_SECONDS - 1,
        token="0x" + "f" * 40,
        smart_money_evidence={"payload": "old-but-held"},
    )
    current["positions"]["first|" + pinned_key] = {
        "key": pinned_key,
        "arm": "first_discovery",
    }

    paper.compact_execution_state(current, at(paper.MAX_PERSISTED_QUOTES + 10))

    assert len(current["quotes"]) == paper.MAX_PERSISTED_QUOTES
    assert pinned_key in current["quotes"]
    assert "smart_money_evidence" not in current["quotes"][pinned_key]
    assert set(current["quotes"][pinned_key]) <= set(paper.PERSISTED_QUOTE_FIELDS)
