import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import alpha_wallet_forward_paper as paper


TOKEN = "0x" + "a" * 40
WALLETS = ["0x" + "b" * 40, "0x" + "c" * 40]
BASE = datetime.fromisoformat("2026-09-07T10:00:00+00:00")


def at(n=0):
    return (BASE + timedelta(seconds=n)).isoformat()


def profile(wallet, chain="bsc"):
    stats = {"realized_profit_usd": 2000, "token_count": 30, "sell_count": 30}
    return {"address": wallet, "chain": chain, "status": "profit_history_supported",
            "evidence_checked_at": at(-60), "stats_7d": stats, "stats_30d": stats,
            "activity": {"observed_token_count": 20, "tokens_with_buy_and_sell": 12,
                         "sell_cost_basis_coverage": 1, "sells_with_reported_cost_basis": 20,
                         "profitable_tokens_in_observed_sell_sample": 10,
                         "largest_token_trade_share": .1, "largest_positive_token_margin_share": .2,
                         "sample_reported_sale_margin_usd": 100}}


def event(index=0, n=5, **overrides):
    return {"chain": "bsc", "token_address": TOKEN, "wallets": [WALLETS[index]],
            "tx_hash": "0x" + str(index + 1) * 64, "observed_at": at(n),
            "direction": "buy", "amount_usd": 500, "flow_eligible": True,
            "provenance": [{"source": "gmgn_profitable_wallet_trades"}], **overrides}


def row(n=10, events=None, price=1, **overrides):
    return {"chain": "bsc", "contract_address": TOKEN, "symbol": "TEST",
            "price_usd": price, "pair_address": "0x" + "d" * 40,
            "quote_observed_at": at(n), "quote_status": "fresh", "quote_fingerprint": str(n),
            "liquidity": 20000, "mcap": 50000, "first_seen_at": at(-300),
            "pair_age_hours": 1, "change_m5": 3, "change_h1": 10,
            "buy_count5m": 10, "sell_count5m": 2, "volume5m": 1500,
            "top10_holder_pct": 20, "max_holder_pct": 5,
            "smart_money_evidence": {"qualified_wallet_profiles": [profile(w) for w in WALLETS],
                                     "events": events if events is not None else [event(0), event(1)]},
            **overrides}


def load(path):
    return json.loads((path / "profitable-wallet-paper" / "state.json").read_text())


def start(path):
    return paper.run_once([], path, at())


def test_startup_inbox_is_baseline_not_retroactive_entry(tmp_path):
    result = paper.run_once([row()], tmp_path, at(10))
    assert result["events"] == []
    again = paper.run_once([row(20)], tmp_path, at(20))
    assert not again["events"]
    assert all(r["baseline"] for r in load(tmp_path)["receipts"].values())


def test_next_quote_fill_costs_and_receipt_immutability(tmp_path):
    start(tmp_path)
    intent = paper.run_once([row()], tmp_path, at(10))
    assert [e["type"] for e in intent["events"]] == ["intent"]
    paper.run_once([row()], tmp_path, at(11))
    state = load(tmp_path)
    assert not state["execution"]["positions"]
    assert {r["first_received_at"] for r in state["receipts"].values()} == {at(10)}
    fill = paper.run_once([row(20, price=1.1)], tmp_path, at(20))
    assert [e["type"] for e in fill["events"]] == ["fill"]
    assert fill["events"][0]["quote"]["price_usd"] == 1.1
    assert fill["events"][0]["debit_usd"] > 35
    assert load(tmp_path)["execution"]["assumptions"]["daily_order_cap"] is None
    assert not (tmp_path / "alpha-execution-state.json").exists()


@pytest.mark.parametrize("bad", ["unqualified", "future", "prestart", "missing_tx", "wrong_chain"])
def test_bad_receipts_never_become_future_qualified(tmp_path, bad):
    start(tmp_path)
    r = row()
    if bad == "unqualified":
        r["smart_money_evidence"]["qualified_wallet_profiles"] = []
    if bad == "future":
        r["smart_money_evidence"]["events"][0]["observed_at"] = at(30)
    if bad == "prestart":
        r["smart_money_evidence"]["events"][0]["observed_at"] = at(-10)
    if bad == "missing_tx":
        r["smart_money_evidence"]["events"][0]["tx_hash"] = ""
    if bad == "wrong_chain":
        r["smart_money_evidence"]["qualified_wallet_profiles"][0]["chain"] = "base"
    assert not paper.run_once([r], tmp_path, at(10))["events"]
    if bad != "missing_tx":
        assert not paper.run_once([row(40)], tmp_path, at(40))["events"]


def test_old_event_first_encountered_after_start_is_not_backfilled(tmp_path):
    start(tmp_path)
    r = row(events=[event(0, -50), event(1, -50)])
    assert not paper.run_once([r], tmp_path, at(10))["events"]


def test_sellers_cancel_pending_order_before_any_fill(tmp_path):
    start(tmp_path)
    paper.run_once([row()], tmp_path, at(10))
    sell = event(0, 15, tx_hash="0x" + "f" * 64, direction="sell", amount_usd=10000)
    result = paper.run_once([row(20, events=[event(0), event(1), sell])], tmp_path, at(20))
    assert not any(e["type"] == "fill" for e in result["events"])
    assert any(e["type"] == "cancel" for e in result["events"])


def test_conflicting_receipt_cannot_stay_a_buy(tmp_path):
    start(tmp_path)
    paper.run_once([row()], tmp_path, at(10))
    r = row(20, events=[event(0, amount_usd=1), event(1)])
    result = paper.run_once([r], tmp_path, at(20))
    assert not any(e["type"] == "fill" for e in result["events"])


def test_missing_concentration_and_kol_counts_do_not_open(tmp_path):
    start(tmp_path)
    r = row(top10_holder_pct=None, smart_money=999, kol=999)
    assert not paper.run_once([r], tmp_path, at(10))["events"]


def test_restart_duplicates_and_corrupt_state_fail_closed(tmp_path):
    start(tmp_path)
    paper.run_once([row()], tmp_path, at(10))
    original = load(tmp_path)
    assert not paper.run_once([row()], tmp_path, at(10))["events"]
    assert load(tmp_path) == original
    path = tmp_path / "profitable-wallet-paper" / "state.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        paper.run_once([row(20)], tmp_path, at(20))
    assert path.read_text() == "{broken"


def test_realized_pnl_is_after_both_sides_costs(tmp_path):
    start(tmp_path)
    paper.run_once([row()], tmp_path, at(10))
    paper.run_once([row(20)], tmp_path, at(20))
    sell_intent = paper.run_once([row(30, price=2.1)], tmp_path, at(30))
    assert any(e["type"] == "intent" and e["side"] == "sell" for e in sell_intent["events"])
    result = paper.run_once([row(40, price=2)], tmp_path, at(40))
    assert result["closed_count"] == 1
    assert 0 < result["realized_pnl_usd"] < 35
    assert result["events"][0]["costs"]["total_usd"] > 0


def test_one_receipt_cannot_buy_again_in_another_arm(tmp_path):
    start(tmp_path)
    paper.run_once([row()], tmp_path, at(10))
    paper.run_once([row(20)], tmp_path, at(20))
    result = paper.run_once([row(30, price=.85)], tmp_path, at(30))
    assert not any(e["type"] == "intent" and e["side"] == "buy" for e in result["events"])
    assert len(load(tmp_path)["execution"]["positions"]) == 1


@pytest.mark.parametrize("risk", [{"watch_status": "invalidated"}, {"market_data_pending": True},
                                  {"change_m5": -40}])
def test_new_risk_cancels_pending_buy(tmp_path, risk):
    start(tmp_path)
    paper.run_once([row()], tmp_path, at(10))
    result = paper.run_once([row(20, **risk)], tmp_path, at(20))
    assert any(e["type"] == "cancel" for e in result["events"])
    assert not any(e["type"] == "fill" for e in result["events"])


def test_current_refresh_does_not_invalidate_qualification_at_receipt(tmp_path):
    start(tmp_path)
    old = row()
    for p in old["smart_money_evidence"]["qualified_wallet_profiles"]:
        p["evidence_checked_at"] = at(10 - 3 * 86400 + 1)
    paper.run_once([old], tmp_path, at(10))
    result = paper.run_once([row(20)], tmp_path, at(20))
    assert any(e["type"] == "fill" for e in result["events"])


def test_pending_buy_keeps_wallet_liquidity_floor(tmp_path):
    start(tmp_path)
    paper.run_once([row()], tmp_path, at(10))
    result = paper.run_once([row(20, liquidity=9000)], tmp_path, at(20))
    assert any(e["type"] == "cancel" for e in result["events"])
    assert not any(e["type"] == "fill" for e in result["events"])


def test_later_upstream_first_seen_cannot_renew_discovery_window(tmp_path):
    paper.run_once([row(0, events=[], first_seen_at=at())], tmp_path, at())
    result = paper.run_once([row(2800, events=[event(0, 2795), event(1, 2795)],
                                 first_seen_at=at(2800))], tmp_path, at(2800))
    assert not result["events"]
    assert any(g["reason"] == "first_discovery_window" for g in result["candidate_gates"])


def test_crashed_process_releases_os_lock(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "writer.lock"
    script = "import os, pathlib; from alpha_wallet_forward_paper import exclusive_lock; "
    script += "lock = exclusive_lock(pathlib.Path(__import__('sys').argv[1])); lock.__enter__(); os._exit(0)"
    result = subprocess.run([sys.executable, "-c", script, str(path)],
                            cwd=Path(paper.__file__).parent, capture_output=True)
    assert result.returncode == 0, result.stderr
    with paper.exclusive_lock(path):
        with pytest.raises(OSError):
            with paper.exclusive_lock(path):
                pytest.fail("concurrent writer acquired lock")
