import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_radar_tg_bot.py")
SPEC = importlib.util.spec_from_file_location("alpha_radar_tg_bot", MODULE_PATH)
bot = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = bot
SPEC.loader.exec_module(bot)


def sample_report():
    return {
        "meta": {"report_generated_at": "2026-08-14T01:00:00Z"},
        "dealer_rows": [
            {
                "symbol": "QUID",
                "dealer_score": 82,
                "top10_holder_pct": 71.1553,
                "max_holder_pct": 65.529,
                "oi_change_1h_pct": 0,
                "funding_rate_pct": 0,
                "dex_url": "https://dexscreener.com/base/quid",
                "dealer_flags": ["Top10 71.2%", "Max 65.5%"],
            }
        ],
        "alpha_rows": [
            {
                "symbol": "AKE",
                "score": 68,
                "oi_change_1h_pct": 6.5,
                "funding_rate_pct": -0.02,
                "top10_holder_pct": None,
                "dex_url": "https://dexscreener.com/bsc/ake",
                "flags": ["futures_listed", "oi_up_1h"],
            }
        ],
        "meme_rows": [
            {
                "symbol": "fomocoin",
                "score": 91,
                "heat_score": 44,
                "heat_flags": ["boost_top", "gmgn_trending"],
                "change_m5": 12.5,
                "change_h1": 42.8,
                "mcap": 1_200_000,
                "url": "https://dexscreener.com/solana/fomo",
            }
        ],
    }


def test_top_command_returns_three_section_summary():
    text, state = bot.handle_command("/top", sample_report(), {})

    assert "庄家雷达" in text
    assert "QUID" in text
    assert "Alpha" in text
    assert "AKE" in text
    assert "Meme" in text
    assert "fomocoin" in text
    assert state == {}


def test_watch_command_updates_watchlist():
    text, state = bot.handle_command("/watch QUID", sample_report(), {"watchlist": ["AKE"]})

    assert "QUID" in text
    assert state["watchlist"] == ["AKE", "QUID"]


def test_alerts_are_deduped_after_state_update():
    report = sample_report()
    state = {}

    first_alerts = bot.build_alert_messages(report, state, max_alerts=5)
    bot.mark_alerts_sent(state, first_alerts)
    second_alerts = bot.build_alert_messages(report, state, max_alerts=5)

    assert any("QUID" in alert["text"] for alert in first_alerts)
    assert any("AKE" in alert["text"] for alert in first_alerts)
    assert second_alerts == []


def test_compact_reasons_splits_flags_and_dedupes_top10():
    row = {
        "top10_holder_pct": 71.2,
        "flags": ["Top10 71.2%", "small_cap;high_volume_to_mcap;top10_concentrated"],
        "oi_change_1h_pct": 6.5,
    }

    reasons = bot.compact_reasons(row)

    assert reasons.count("Top10") == 1
    assert ";" not in reasons
    assert "small_cap" in reasons
    assert "high_volume_to_mcap" in reasons
    assert "OI 6.5%" in reasons


def test_alert_keeps_metric_line_from_repeating_top10_reason():
    row = sample_report()["dealer_rows"][0]

    text = bot.format_alert("dealer", row)

    assert text.count("Top10") == 1


def test_find_command_shows_risk_line():
    report = sample_report()
    report["alpha_rows"][0]["risk_level"] = "medium"
    report["alpha_rows"][0]["risk_score"] = 37
    report["alpha_rows"][0]["risk_source"] = "rugcheck"

    text, _ = bot.handle_command("/find AKE", report, {})

    assert "Risk: medium 37%" in text
    assert "rugcheck" in text


def test_meme_row_shows_heat_score_and_flags():
    text = bot.format_meme_row(sample_report()["meme_rows"][0], 1)

    assert "Heat 44" in text
    assert "boost_top" in bot.compact_reasons(sample_report()["meme_rows"][0], include_metrics=False)


def test_fast_track_alerts_are_compact_and_deduped():
    fast_track = {
        "alerts": [{
            "alert_type": "high_risk",
            "symbol": "BNC4",
            "chain": "bsc",
            "market_cap": 5_010_000,
            "liquidity_usd": 303_000,
            "gold_dog_conviction_score": 89,
            "reason": "高风险重点提醒：1h 涨幅 120.0%",
            "url": "https://gmgn.ai/bsc/token/BNC4",
        }]
    }
    state = {"fast_track_initialized": True}

    first = bot.build_fast_track_alert_messages(fast_track, state)
    bot.mark_fast_track_alerts_sent(state, first)
    second = bot.build_fast_track_alert_messages(fast_track, state)

    assert len(first) == 1
    assert "高风险重点" in first[0]["text"]
    assert "BNC4" in first[0]["text"]
    assert "$5.0M" in first[0]["text"]
    assert second == []


def test_fast_track_initializes_without_replaying_history():
    fast_track = {"alerts": [{"alert_type": "recovered", "symbol": "OLD"}]}
    state = {}

    assert bot.build_fast_track_alert_messages(fast_track, state) == []
    assert state["fast_track_initialized"] is True
