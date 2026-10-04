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


paper = load_module("alpha_first_discovery_paper", BASE / "alpha_first_discovery_paper.py")


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


NOW = "2026-09-02T14:00:00+08:00"


def healthy_row(**overrides):
    row = {
        "symbol": "FRESH",
        "chain": "bsc",
        "contract_address": "0xfresh",
        "price_usd": 0.00005,
        "mcap": 50_000,
        "liquidity": 24_000,
        "volume24h": 180_000,
        "change_m5": 5,
        "change_h1": 35,
        "smart_money": 38,
        "kol": 10,
        "source_count": 3,
        "top10_holder_pct": 20,
        "max_holder_pct": 7,
        "gmgn_risk_flags": [],
        "recommendation_bucket": "shadow",
        "entry_score": 88,
        "gold_dog_score": 86,
        "gold_dog_conviction_score": 92,
        "backtest_rule_score": 95,
        "pro_signal_score": 90,
    }
    row.update(overrides)
    return row


def replay_for(row, first_seen_at="2026-09-02T13:40:00+08:00", first_mcap=45_000):
    key = paper.token_key(row)
    return {
        "rows": {
            key: {
                "key": key,
                "symbol": row["symbol"],
                "chain": row.get("chain", "bsc"),
                "contract_address": row["contract_address"],
                "first_seen_at": first_seen_at,
                "first_price_usd": 0.000045,
                "first_snapshot": {
                    **row,
                    "price_usd": 0.000045,
                    "mcap": first_mcap,
                    "market_cap": first_mcap,
                    "score": 92,
                },
                "observations": [],
            }
        }
    }


def test_fresh_sweet_spot_candidate_opens_probe():
    row = healthy_row()
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    result = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row),
        state,
        NOW,
    )

    assert result["events"][0]["type"] == "open"
    position = result["state"]["open_positions"][0]
    assert position["strategy"] == "first_discovery_probe"
    assert position["bucket"] == "sweet_30k_100k"
    assert position["size_usd"] <= paper.SETTINGS["max_position_usd"]


def test_optimized_low_lottery_candidate_opens_without_volume_gate():
    row = healthy_row(mcap=22_000, volume24h=0)
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    result = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row, first_mcap=20_000),
        state,
        NOW,
    )

    assert result["events"][0]["type"] == "open"
    assert result["state"]["open_positions"][0]["bucket"] == "small_lottery_10k_30k"


def test_robinhood_first_discovery_can_open_probe():
    row = healthy_row(chain="robinhood", contract_address="0xrobin", mcap=55_000)
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    result = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row, first_mcap=52_000),
        state,
        NOW,
    )

    assert result["events"][0]["type"] == "open"
    assert result["state"]["open_positions"][0]["chain"] == "robinhood"


def test_same_day_can_open_more_than_one_first_discovery_across_runs():
    first = healthy_row(symbol="FIRST", contract_address="0xfirst", mcap=55_000)
    second = healthy_row(symbol="SECOND", contract_address="0xsecond", mcap=58_000)
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    opened = paper.run_paper_once(
        {"meme_potential_rows": [first], "meme_shadow_rows": []},
        replay_for(first, first_mcap=50_000),
        state,
        NOW,
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [first, second], "meme_shadow_rows": []},
        {
            "rows": {
                **replay_for(first, first_mcap=50_000)["rows"],
                **replay_for(second, first_mcap=52_000)["rows"],
            }
        },
        opened,
        "2026-09-02T14:10:00+08:00",
    )

    assert result["events"][0]["type"] == "open"
    assert result["events"][0]["symbol"] == "SECOND"
    assert len(result["state"]["open_positions"]) == 2


def test_optimized_score_is_no_longer_a_hard_gate():
    row = healthy_row(entry_score=20, gold_dog_score=20, gold_dog_conviction_score=20)
    classification = paper.classify_candidate(
        row,
        replay_for(row)["rows"][paper.token_key(row)],
        NOW,
    )

    assert classification["eligible"]


def test_smart_money_is_no_longer_a_hard_gate():
    row = healthy_row(smart_money=8)
    classification = paper.classify_candidate(
        row,
        replay_for(row)["rows"][paper.token_key(row)],
        NOW,
    )

    assert classification["eligible"]


def test_max_holder_still_rejects_concentrated_chips():
    row = healthy_row(top10_holder_pct=28, max_holder_pct=19)
    classification = paper.classify_candidate(
        row,
        replay_for(row)["rows"][paper.token_key(row)],
        NOW,
    )

    assert not classification["eligible"]
    assert "筹码集中" in classification["reject_reason"]


def test_stale_first_discovery_is_not_backfilled():
    row = healthy_row()
    classification = paper.classify_candidate(
        row,
        replay_for(row, first_seen_at="2026-09-02T12:00:00+08:00")["rows"][paper.token_key(row)],
        NOW,
    )

    assert not classification["eligible"]
    assert "超出新发现窗口" in classification["reject_reason"]


def test_late_100k_300k_first_mcap_bucket_is_accepted_inside_window():
    row = healthy_row(mcap=210_000)
    classification = paper.classify_candidate(
        row,
        replay_for(row, first_mcap=180_000)["rows"][paper.token_key(row)],
        NOW,
    )

    assert classification["eligible"]
    assert classification["bucket"] == "late_100k_300k"


def test_multi_source_narrative_breakout_opens_separate_small_probe():
    row = healthy_row(
        symbol="BNC4",
        contract_address="0xbnc4",
        mcap=720_000,
        liquidity=120_000,
        change_h1=58,
        source_labels=["OKX", "GMGN", "DS"],
        sources=["okx_signal", "gmgn_trending"],
    )
    history = replay_for(row, first_mcap=668_900)
    classification = paper.classify_candidate(row, history["rows"][paper.token_key(row)], NOW)

    assert classification["eligible"]
    assert classification["bucket"] == "narrative_300k_1m"
    assert classification["execution_arm"] == "narrative_breakout"
    assert classification["route_label"] == "大叙事突破"


def test_narrative_breakout_requires_okx_plus_gmgn_or_ds():
    row = healthy_row(mcap=700_000, liquidity=100_000, source_labels=["GMGN"], sources=["gmgn_trending"])
    history = replay_for(row, first_mcap=650_000)
    classification = paper.classify_candidate(row, history["rows"][paper.token_key(row)], NOW)

    assert classification["eligible"] is False
    assert "大叙事突破缺少独立确认" in classification["reject_reason"]


def test_shadow_profile_accepts_late_but_still_early_discovery():
    row = healthy_row(mcap=42_000)
    history = replay_for(row, first_seen_at="2026-09-02T11:29:00+08:00", first_mcap=40_000)
    strict = paper.classify_candidate(row, history["rows"][paper.token_key(row)], NOW)
    try:
        paper.apply_profile("shadow-180m")
        shadow = paper.classify_candidate(row, history["rows"][paper.token_key(row)], NOW)
    finally:
        paper.apply_profile("strict-45m")

    assert not strict["eligible"]
    assert "超出新发现窗口" in strict["reject_reason"]
    assert shadow["eligible"]
    assert shadow["entry_delay_minutes"] == 151.0


def test_shadow_profile_rejects_late_low_lottery_discovery():
    row = healthy_row(mcap=24_000)
    history = replay_for(row, first_seen_at="2026-09-02T11:29:00+08:00", first_mcap=20_000)
    try:
        paper.apply_profile("shadow-180m")
        shadow = paper.classify_candidate(row, history["rows"][paper.token_key(row)], NOW)
    finally:
        paper.apply_profile("strict-45m")

    assert not shadow["eligible"]
    assert "10K-30K 小彩票池" in shadow["reject_reason"]
    assert "45 分钟内埋伏" in shadow["reject_reason"]


def test_extreme_overheat_is_rejected():
    row = healthy_row(change_m5=70, change_h1=650)
    classification = paper.classify_candidate(
        row,
        replay_for(row)["rows"][paper.token_key(row)],
        NOW,
    )

    assert not classification["eligible"]
    assert "1h" in classification["reject_reason"]
    assert "优化区间" in classification["reject_reason"]


def test_tp1_sells_half_and_tracks_realized_pnl():
    row = healthy_row()
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    opened = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row),
        state,
        NOW,
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [healthy_row(price_usd=0.0001)], "meme_shadow_rows": []},
        replay_for(row),
        opened,
        "2026-09-02T14:10:00+08:00",
    )

    assert result["events"][0]["type"] == "take_profit_1"
    assert result["state"]["open_positions"][0]["remaining_fraction"] == 0.2
    assert result["state"]["realized_pnl_usd"] > 0


def test_runner_trail_exits_tail_after_tp1_drawdown():
    row = healthy_row()
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    opened = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row),
        state,
        NOW,
    )["state"]
    tp1_state = paper.run_paper_once(
        {"meme_potential_rows": [healthy_row(price_usd=0.0001)], "meme_shadow_rows": []},
        replay_for(row),
        opened,
        "2026-09-02T14:10:00+08:00",
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [healthy_row(price_usd=0.000065)], "meme_shadow_rows": []},
        replay_for(row),
        tp1_state,
        "2026-09-02T14:20:00+08:00",
    )

    assert result["events"][0]["type"] == "runner_trailing_exit"
    assert not result["state"]["open_positions"]


def test_time_stop_closes_stalled_probe():
    row = healthy_row()
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    opened = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row),
        state,
        NOW,
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [healthy_row(price_usd=0.000052)], "meme_shadow_rows": []},
        replay_for(row),
        opened,
        "2026-09-02T15:31:00+08:00",
    )

    assert result["events"][0]["type"] == "time_stop_exit"
    assert not result["state"]["open_positions"]


def test_time_stop_releases_position_when_row_disappears_from_report():
    row = healthy_row()
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    opened = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row),
        state,
        NOW,
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [], "meme_shadow_rows": []},
        replay_for(row),
        opened,
        "2026-09-02T15:31:00+08:00",
    )

    assert result["events"][0]["type"] == "time_stop_exit"
    assert "报告缺失" in result["events"][0]["reason"]
    assert not result["state"]["open_positions"]


def test_stop_loss_closes_probe():
    row = healthy_row()
    state = paper.load_state(Path("missing-first-discovery-state.json"))
    opened = paper.run_paper_once(
        {"meme_potential_rows": [row], "meme_shadow_rows": []},
        replay_for(row),
        state,
        NOW,
    )["state"]

    result = paper.run_paper_once(
        {"meme_potential_rows": [healthy_row(price_usd=0.000035)], "meme_shadow_rows": []},
        replay_for(row),
        opened,
        "2026-09-02T14:10:00+08:00",
    )

    assert result["events"][0]["type"] == "stop_loss"
    assert not result["state"]["open_positions"]
    assert result["state"]["closed_positions"][0]["status"] == "closed"
