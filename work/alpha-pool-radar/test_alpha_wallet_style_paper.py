import importlib.util
import sys
import json
import pytest
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paper = load_module("alpha_wallet_style_paper", BASE / "alpha_wallet_style_paper.py")


@pytest.mark.parametrize("quote", [None, {}, {"symbol": "TEST"}, {"price_usd": 0},
                                  {"price_usd": -1}, {"price_usd": "NaN"},
                                  {"price_usd": "Infinity"}, {"price_usd": True}])
def test_summary_missing_quote_uses_stale_last_mark(quote):
    state = {"cash_usd": 900, "open_positions": [
        {"key": "bsc:test", "size_usd": 100, "remaining_fraction": 1,
         "entry_price_usd": 1, "last_price_usd": 2}]}
    result = paper.account_summary(state, {"bsc:test": quote} if quote is not None else {})
    assert result["equity_usd"] == 1100
    assert result["unrealized_pnl_usd"] == 100
    assert result["valuation_status"] == "stale"
    assert result["stale_positions"] == 1
    json.dumps(result, allow_nan=False)


def test_summary_without_any_valid_mark_is_unknown():
    state = {"cash_usd": 900, "open_positions": [
        {"key": "bsc:test", "size_usd": 100, "entry_price_usd": 1}]}
    result = paper.account_summary(state, {})
    assert result["equity_usd"] is None
    assert result["unrealized_pnl_usd"] is None
    assert result["open_exposure_usd"] == 100
    assert result["valuation_status"] == "unavailable"
    assert result["unpriced_positions"] == 1
    assert paper.money(result["equity_usd"]) == "unknown"


def test_summary_fresh_partial_position_preserves_cost_and_pnl():
    state = {"cash_usd": 1000, "realized_pnl_usd": 80, "open_positions": [
        {"key": "bsc:test", "size_usd": 100, "remaining_fraction": .2,
         "entry_price_usd": 1, "last_price_usd": 1.5}]}
    result = paper.account_summary(state, {"bsc:test": {"price_usd": 2}})
    assert result["open_exposure_usd"] == 20
    assert result["unrealized_pnl_usd"] == 20
    assert result["realized_pnl_usd"] == 80
    assert result["equity_usd"] == 1040
    assert result["valuation_status"] == "fresh"
    assert result["unpriced_positions"] == result["stale_positions"] == 0
    assert state["open_positions"][0]["last_price_usd"] == 1.5


@pytest.mark.parametrize("entry,last", [(1, "NaN"), (1, -2), (None, 2),
                                       ("Infinity", 2), (True, 2)])
def test_invalid_entry_or_last_mark_keeps_summary_unknown(entry, last):
    state = {"cash_usd": 900, "open_positions": [
        {"key": "bsc:test", "size_usd": 100, "entry_price_usd": entry,
         "last_price_usd": last}]}
    result = paper.account_summary(state, {})
    assert result["unrealized_pnl_usd"] is None and result["equity_usd"] is None
    assert result["valuation_status"] == "unavailable"
    json.dumps(result, allow_nan=False)


def test_summary_empty_account_remains_priced():
    result = paper.account_summary({"cash_usd": 1000, "open_positions": []}, {})
    assert result["equity_usd"] == 1000
    assert result["unrealized_pnl_usd"] == 0
    assert result["valuation_status"] == "fresh"


@pytest.mark.parametrize("mark,status", [(None, "unavailable"), (2, "stale")])
def test_summary_json_and_markdown_preserve_unknown_and_stale(tmp_path, mark, status):
    position = {
        "key": "bsc:test", "symbol": "TEST", "contract_address": "0xtest",
        "bucket": "fixture", "size_usd": 100, "remaining_fraction": 1,
        "entry_price_usd": 1, "last_price_usd": mark, "entry_mcap_usd": 10000,
        "stop_price_usd": .8, "tp1_price_usd": 2, "tp2_price_usd": 3,
    }
    state = {"cash_usd": 900, "open_positions": [position]}
    state["last_summary"] = paper.account_summary(state, {})
    path = tmp_path / "paper-fixture.json"
    paper.save_json(path, state)
    saved = json.loads(path.read_text(encoding="utf-8"))
    summary = saved["last_summary"]
    assert summary == state["last_summary"]
    json.dumps(saved, allow_nan=False)
    report = paper.render_markdown({"state": saved, "events": [], "candidates": []},
                                   "2026-09-09T12:00:00+00:00")
    assert "ret=unknown" in report
    if mark is None:
        assert summary["equity_usd"] is summary["unrealized_pnl_usd"] is None
        assert "- equity: unknown" in report
        assert "- unrealized_pnl: unknown" in report
        assert "- equity: $0.00" not in report
        assert "- unpriced_positions: 1" in report
    else:
        assert summary["equity_usd"] == 1100
        assert summary["unrealized_pnl_usd"] == 100
        assert "- equity: $1.1K" in report
        assert "- stale_positions: 1" in report
    assert "- valuation_status: " + status in report


def test_report_preserves_confirmed_zero_pnl():
    state = {"cash_usd": 1000, "open_positions": []}
    state["last_summary"] = paper.account_summary(state, {})
    report = paper.render_markdown({"state": state, "events": [], "candidates": []},
                                   "2026-09-09T12:00:00+00:00")
    assert "- unrealized_pnl: $0.00" in report
    assert "- valuation_status: fresh" in report


def healthy_row(**overrides):
    row = {
        "symbol": "GOOD",
        "chain": "bsc",
        "contract_address": "0xgood",
        "price_usd": 0.001,
        "mcap": 180_000,
        "liquidity": 55_000,
        "volume24h": 420_000,
        "pair_age_hours": 4,
        "change_m5": 4,
        "change_h1": 28,
        "smart_money": 42,
        "kol": 9,
        "source_count": 3,
        "top10_holder_pct": 18,
        "max_holder_pct": 6,
        "gmgn_risk_flags": [],
        "recommendation_bucket": "ambush",
        "entry_score": 88,
        "gold_dog_score": 84,
        "gold_dog_conviction_score": 91,
        "backtest_rule_score": 94,
        "pro_signal_score": 82,
    }
    row.update(overrides)
    return row


def test_invalidated_ambush_is_not_opened():
    row = healthy_row(watch_status="invalidated", watch_invalid_reason="从监控最高市值回撤 50% ，确认票失效")

    result = paper.classify_candidate(row)

    assert not result["eligible"]
    assert "失效" in result["reject_reason"]
    assert result["score"] <= 69


def legacy_fixture_cycle(report, state, now):
    candidates, rows = paper.build_candidates(report)
    paper.open_positions(state, candidates, rows, now)
    return paper.run_paper_once(report, state, now)


def test_live_cycle_does_not_open_from_legacy_kol_labels():
    state = paper.load_state(Path("missing-state.json"))
    result = paper.run_paper_once({"meme_potential_rows": [healthy_row()]}, state,
                                 "2026-09-01T20:00:00+08:00")
    assert not result["events"]
    assert not state["open_positions"]
    assert state["entry_policy"] == "legacy_exit_only"
    assert all(not item["classification"]["eligible"] for item in result["candidates"])


def test_legacy_position_fixture_preserves_spot_accounting():
    report = {"meme_potential_rows": [healthy_row()], "meme_shadow_rows": []}
    state = paper.load_state(Path("missing-state.json"))

    result = legacy_fixture_cycle(report, state, "2026-09-01T20:00:00+08:00")

    position = result["state"]["open_positions"][0]
    assert position["symbol"] == "GOOD"
    assert position["margin_usd"] == position["size_usd"]
    assert position["leverage"] == "1x spot paper"
    assert position["stop_price_usd"] < position["entry_price_usd"]
    assert position["tp1_price_usd"] > position["entry_price_usd"]


def test_old_entry_helper_has_no_one_trade_per_day_limit():
    state = paper.load_state(Path("missing-state.json"))
    first = legacy_fixture_cycle(
        {"meme_potential_rows": [healthy_row(symbol="ONE", contract_address="0xone")], "meme_shadow_rows": []},
        state,
        "2026-09-01T20:00:00+08:00",
    )["state"]

    result = legacy_fixture_cycle(
        {
            "meme_potential_rows": [
                healthy_row(symbol="ONE", contract_address="0xone"),
                healthy_row(symbol="TWO", contract_address="0xtwo", mcap=210_000),
            ],
            "meme_shadow_rows": [],
        },
        first,
        "2026-09-01T20:10:00+08:00",
    )

    assert {position["symbol"] for position in result["state"]["open_positions"]} == {"ONE", "TWO"}


def test_confirmed_runner_pullback_can_open_as_second_entry():
    row = healthy_row(
        symbol="SECOND",
        recommendation_bucket="shadow",
        shadow_original_bucket="pullback",
        watch_status="achieved_gold",
        mcap=420_000,
        pair_age_hours=92,
        change_m5=-6,
        change_h1=4,
        smart_money=80,
        kol=24,
        top10_holder_pct=20,
    )

    result = paper.classify_candidate(row)

    assert result["eligible"]
    assert result["style_mode"] == "second_entry_pullback"


def test_tp1_sells_half_and_updates_realized_pnl():
    state = paper.load_state(Path("missing-state.json"))
    opened = legacy_fixture_cycle(
        {"meme_potential_rows": [healthy_row()], "meme_shadow_rows": []},
        state,
        "2026-09-01T20:00:00+08:00",
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [healthy_row(price_usd=0.0015)], "meme_shadow_rows": []},
        opened,
        "2026-09-01T20:15:00+08:00",
    )

    assert result["events"][0]["type"] == "take_profit_1"
    assert result["state"]["open_positions"][0]["remaining_fraction"] == 0.5
    assert result["state"]["realized_pnl_usd"] > 0


def test_stop_loss_closes_remaining_position():
    state = paper.load_state(Path("missing-state.json"))
    opened = legacy_fixture_cycle(
        {"meme_potential_rows": [healthy_row()], "meme_shadow_rows": []},
        state,
        "2026-09-01T20:00:00+08:00",
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [healthy_row(price_usd=0.00075)], "meme_shadow_rows": []},
        opened,
        "2026-09-01T20:20:00+08:00",
    )

    assert result["events"][0]["type"] == "stop_loss"
    assert not result["state"]["open_positions"]
    assert result["state"]["closed_positions"][0]["status"] == "closed"
