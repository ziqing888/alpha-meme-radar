import importlib.util
import sys
from pathlib import Path

import pytest


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gold_watch = load_module("alpha_gold_watch", BASE / "alpha_gold_watch.py")


def update_fresh_watch(state, rows, now_iso, **kwargs):
    """Existing behavior fixtures represent a new source observation each minute."""
    rows = [{**row, "observed_at": now_iso} for row in rows]
    return gold_watch.update_watch_state(state, rows, now_iso, **kwargs)


def test_okx_is_one_independent_screening_source():
    row = {"source_labels": ["OKX", "GMGN"], "sources": ["okx_trenches", "okx_signal"]}
    assert gold_watch.screening_sources(row) == ["OKX", "GMGN"]
    assert gold_watch.confirmation_source_check(row)["ok"] is True
    assert gold_watch.confirmation_source_check({"sources": ["okx_trenches", "okx_signal"]})["ok"] is False


def test_okx_signal_panel_confirms_with_second_source_but_trenches_does_not():
    signal_with_ds = {"source_labels": ["OKX", "DS"], "sources": ["okx_signal", "profile_latest"]}
    trenches_with_ds = {"source_labels": ["OKX", "DS"], "sources": ["okx_trenches", "profile_latest"]}

    signal_check = gold_watch.confirmation_source_check(signal_with_ds)
    assert signal_check["ok"] is True
    assert "OKX信号" in signal_check["core_sources"]

    trenches_check = gold_watch.confirmation_source_check(trenches_with_ds)
    assert trenches_check["ok"] is False
    assert "OKX信号" not in trenches_check["core_sources"]


def test_display_only_ds_label_does_not_manufacture_double_source_confirmation():
    check = gold_watch.confirmation_source_check({
        "source_labels": ["GMGN", "DS", "Alpha_AI"],
        "sources": ["gmgn_skills_hot_searches"],
    })
    assert check["ok"] is False
    assert check["sources"] == ["GMGN"]


def test_plain_watch_rows_are_review_only_not_aggregate_discoveries():
    assert gold_watch.watch_v2_tier({"status": "watch", "ticket_stage": "late"}) == "review"
    assert gold_watch.watch_v2_tier({"status": "seed_pool", "ticket_stage": "seed"}) == "discovered"


def candidate(
    symbol: str,
    conviction: float,
    *,
    smart_money: int = 20,
    kol: int = 5,
    age: float = 4,
    mcap: float = 240_000,
) -> dict:
    return {
        "symbol": symbol,
        "chain": "solana",
        "contract_address": f"Mint{symbol}",
        "price_usd": 0.001,
        "mcap": mcap,
        "liquidity": 45_000,
        "pair_age_hours": age,
        "smart_money": smart_money,
        "kol": kol,
        "top10_holder_pct": 18,
        "source_labels": ["GMGN", "DS", "Birdeye"],
        "source_count": 3,
        "gold_dog_score": 82,
        "gold_dog_conviction_score": conviction,
        "entry_level": "强候选",
        "entry_stage": "新池",
        "gold_dog_rationale": ["新池", "聪明钱/KOL共振"],
        "recommendation_bucket": "ambush",
        "recommendation_action": "可小仓试探",
    }


def test_first_strong_scan_tracks_without_promoting():
    state = update_fresh_watch(
        {},
        [candidate("DOG", 90)],
        now_iso="2026-08-14T10:00:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintdog"]
    assert item["scan_count"] == 1
    assert item["strong_scan_count"] == 1
    assert item["status"] == "candidate"
    assert state["pick"] is None
    assert state["alerts"] == []


def test_high_score_discovery_without_second_core_source_alerts_once():
    row = candidate("DISCOVERY", 80, age=4, mcap=80_000)
    row["source_labels"] = ["985", "听风"]
    row["sources"] = ["985_monitor", "wind_monitor"]
    state = update_fresh_watch({}, [row], "2026-09-07T12:00:00Z")

    assert [alert["alert_type"] for alert in state["new_alerts"]] == ["discovered"]
    assert state["new_alerts"][0]["symbol"] == "DISCOVERY"
    assert state["alerted_keys"] == []
    assert not state["candidates"]["solana:mintdiscovery"].get("first_confirmed_at")

    state = update_fresh_watch(state, [{**row, "observed_at": "2026-09-07T12:01:00Z"}], "2026-09-07T12:01:00Z")
    assert state["new_alerts"] == []


def test_seed_multi_source_first_discovery_alerts_once():
    row = candidate("SEED", 90, age=1, mcap=20_000)
    row["quote_observed_at"] = "2026-09-07T12:00:00Z"
    row["quote_status"] = "fresh"
    row["quote_fingerprint"] = "seed-first-observation"

    state = update_fresh_watch({}, [row], "2026-09-07T12:00:00Z")

    assert [(alert["alert_type"], alert["symbol"]) for alert in state["new_alerts"]] == [("discovered", "SEED")]
    assert state["candidates"]["solana:mintseed"]["ticket_stage"] == "seed"
    assert state["candidates"]["solana:mintseed"]["source_confirmation_ok"] is True


def test_high_risk_focus_alert_is_emitted_even_without_buy_confirmation():
    row = candidate("RISK", 90, age=1, mcap=80_000)
    row["gmgn_risk_flags"] = ["bundler_39.2"]
    row["change_h1"] = 120
    row["quote_observed_at"] = "2026-09-07T12:00:00Z"
    row["quote_status"] = "fresh"
    row["quote_fingerprint"] = "risk-first-observation"

    state = update_fresh_watch({}, [row], "2026-09-07T12:00:00Z")

    risk_alerts = [alert for alert in state["new_alerts"] if alert["alert_type"] == "high_risk"]
    assert len(risk_alerts) == 1
    assert risk_alerts[0]["symbol"] == "RISK"
    assert "bundler 39.2%" in risk_alerts[0]["reason"]
    assert risk_alerts[0]["change_h1"] == 120


def test_price_spike_alone_is_not_a_high_risk_alert():
    row = candidate("MOMENTUM", 90, age=1, mcap=80_000)
    row["change_h1"] = 568
    row["gmgn_risk_flags"] = []
    row["quote_observed_at"] = "2026-09-07T12:00:00Z"
    row["quote_status"] = "fresh"
    row["quote_fingerprint"] = "momentum-first-observation"

    state = update_fresh_watch({}, [row], "2026-09-07T12:00:00Z")

    assert not [alert for alert in state["new_alerts"] if alert["alert_type"] == "high_risk"]


def test_three_confirmed_scans_promote_single_pick_and_alert_once():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("DOG", 90)],
            now_iso=f"2026-08-14T10:0{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["pick"]["symbol"] == "DOG"
    assert state["pick"]["watch_status"] == "strong_candidate"
    assert state["candidates"]["solana:mintdog"]["strong_scan_count"] == 3
    assert [alert["alert_type"] for alert in state["alerts"]] == ["early", "confirmed"]
    assert state["alerts"][1]["market_cap"] == 240_000
    assert state["alerts"][1]["mcap"] == 240_000
    assert state["alerts"][1]["first_confirmed_mcap"] == 240_000
    assert state["alerts"][1]["first_confirmed_at"] == "2026-08-14T10:02:00+08:00"
    assert state["pick"]["watch_first_seen_mcap"] == 240_000
    assert state["pick"]["watch_alert_mcap"] == 240_000
    assert state["pick"]["watch_first_confirmed_mcap"] == 240_000
    assert state["pick"]["watch_first_confirmed_at"] == "2026-08-14T10:02:00+08:00"
    assert state["pick"]["watch_alerted_at"] == "2026-08-14T10:02:00+08:00"
    assert state["pick"]["watch_entry_reason"] == "新池 / 聪明钱/KOL共振"
    assert state["pick"]["watch_alert_gate_reason"] == "已进确认层"
    assert state["candidates"]["solana:mintdog"]["min_confirmations"] == 3

    state = update_fresh_watch(
        state,
        [candidate("DOG", 91)],
        now_iso="2026-08-14T10:03:00+08:00",
        min_confirmations=3,
    )
    assert len(state["alerts"]) == 2
    assert state["alerts"][0]["ticket_stage"] == "confirmed"
    assert state["pick"] is None
    assert state["confirmed_pick"] is None
    assert state["candidates"]["solana:mintdog"]["alert_eligible"] is True
    assert state["candidates"]["solana:mintdog"]["alert_mcap"] == 240_000
    assert state["candidates"]["solana:mintdog"]["first_confirmed_mcap"] == 240_000


def test_all_confirmed_candidates_alert_and_highest_rank_remains_pick():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [
                candidate("LEAD", 96, smart_money=80, kol=20),
                candidate("RUNNER", 94, smart_money=30, kol=8),
            ],
            now_iso=f"2026-08-14T10:2{minute}:00+08:00",
            min_confirmations=3,
        )

    lead = state["candidates"]["solana:mintlead"]
    runner = state["candidates"]["solana:mintrunner"]
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [
        ("early", "LEAD"), ("early", "RUNNER"), ("confirmed", "LEAD"), ("confirmed", "RUNNER")
    ]
    assert state["pick"]["symbol"] == "LEAD"
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["new_alerts"]] == [
        ("confirmed", "LEAD"), ("confirmed", "RUNNER")
    ]
    assert lead["first_confirmed_mcap"] == 240_000
    assert runner["first_confirmed_mcap"] == 240_000
    assert runner["first_confirmed_at"] == "2026-08-14T10:22:00+08:00"
    assert runner.get("alerted_at") == "2026-08-14T10:22:00+08:00"
    assert runner["first_confirmation_active"] is True
    assert state["summary"]["historical_first_confirmed_count"] == 2


def test_existing_alert_backfills_confirmation_mcap_on_pick():
    state = {
        "alerts": [
            {
                "created_at": "2026-08-14T10:02:00+08:00",
                "symbol": "DOG",
                "chain": "solana",
                "contract_address": "MintDOG",
                "mcap": 117_000,
                "gold_dog_conviction_score": 94,
                "watch_strong_scan_count": 3,
            }
        ],
        "alerted_keys": ["solana:mintdog"],
        "candidates": {
            "solana:mintdog": {
                "symbol": "DOG",
                "chain": "solana",
                "contract_address": "MintDOG",
                "first_seen_at": "2026-08-14T10:00:00+08:00",
                "first_seen_mcap": 35_000,
                "strong_scan_count": 3,
                "consecutive_strong_scan_count": 3,
                "status": "strong_candidate",
            }
        },
    }

    state = update_fresh_watch(
        state,
        [candidate("DOG", 95, mcap=218_000)],
        now_iso="2026-08-14T10:10:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintdog"]
    assert state["pick"] is None
    assert item["first_seen_mcap"] == 35_000
    assert item["alert_mcap"] == 117_000
    assert item["first_confirmed_mcap"] == 117_000
    assert item["first_confirmed_at"] == "2026-08-14T10:02:00+08:00"
    assert item["alerted_at"] == "2026-08-14T10:02:00+08:00"


def test_old_confirmed_pick_does_not_beat_one_scan_hotter_candidate():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("STEADY", 84)],
            now_iso=f"2026-08-14T11:0{minute}:00+08:00",
            min_confirmations=3,
        )

    state = update_fresh_watch(
        state,
        [candidate("STEADY", 84), candidate("FLASH", 99, smart_money=40, kol=14)],
        now_iso="2026-08-14T11:03:00+08:00",
        min_confirmations=3,
    )

    assert state["pick"] is None
    assert state["confirmed_pick"] is None
    assert state["candidates"]["solana:mintflash"]["status"] == "candidate"


def test_confirmed_token_does_not_alert_again_after_pick_rotates_back():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("DOG", 90), candidate("CAT", 70)],
            now_iso=f"2026-08-14T13:0{minute}:00+08:00",
            min_confirmations=3,
        )
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [
        ("early", "DOG"), ("confirmed", "DOG")
    ]

    for minute in range(3, 6):
        state = update_fresh_watch(
            state,
            [candidate("DOG", 83), candidate("CAT", 95, smart_money=50, kol=16)],
            now_iso=f"2026-08-14T13:0{minute}:00+08:00",
            min_confirmations=3,
        )
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [
        ("early", "DOG"), ("confirmed", "DOG"), ("early", "CAT"), ("confirmed", "CAT")
    ]

    state = update_fresh_watch(
        state,
        [candidate("DOG", 99, smart_money=60, kol=20), candidate("CAT", 84)],
        now_iso="2026-08-14T13:06:00+08:00",
        min_confirmations=3,
    )

    assert state["pick"] is None
    assert state["confirmed_pick"] is None
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [
        ("early", "DOG"), ("confirmed", "DOG"), ("early", "CAT"), ("confirmed", "CAT")
    ]


def test_existing_confirmed_alert_does_not_remain_current_pick():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("DOG", 90)],
            now_iso=f"2026-08-14T13:1{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["confirmed_pick"]["symbol"] == "DOG"
    state = update_fresh_watch(
        state,
        [candidate("DOG", 95, smart_money=60, kol=20)],
        now_iso="2026-08-14T13:13:00+08:00",
        min_confirmations=3,
    )

    assert len(state["alerts"]) == 2
    assert state["pick"] is None
    assert state["confirmed_pick"] is None
    assert state["candidates"]["solana:mintdog"]["first_confirmation_active"] is False
    assert state["candidates"]["solana:mintdog"]["watch_v2_tier"] == "review"


def test_first_confirmation_is_active_only_on_the_alert_scan():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("FLASH", 93, age=3, mcap=180_000)],
            now_iso=f"2026-08-14T13:2{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintflash"]
    assert item["first_confirmation_active"] is True
    assert item["watch_v2_tier"] == "confirmed"
    assert state["confirmed_pick"]["watch_first_confirmation_active"] is True
    assert state["confirmed_pick"]["watch_v2_tier"] == "confirmed"

    state = update_fresh_watch(
        state,
        [candidate("FLASH", 94, age=3.3, mcap=220_000)],
        now_iso="2026-08-14T13:23:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintflash"]
    assert item["first_confirmation_active"] is False
    assert item["watch_v2_tier"] == "review"
    assert state["confirmed_pick"] is None


def test_high_holder_risk_invalidates_before_confirmation():
    risky = candidate("RISK", 95, age=3, mcap=180_000)
    risky["top10_holder_pct"] = 72
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [risky],
            now_iso=f"2026-08-14T13:3{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintrisk"]
    assert item["status"] == "invalidated"
    assert item["watch_v2_tier"] == "review"
    assert "Top10" in item["invalid_reason"]
    assert state["alerts"] == []


def test_late_confirmed_token_expires_instead_of_becoming_pick():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("OLD", 90, age=18, mcap=900_000)],
            now_iso=f"2026-08-14T14:0{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["pick"] is None
    assert state["confirmed_pick"] is None
    assert state["candidates"]["solana:mintold"]["status"] == "expired"
    assert state["candidates"]["solana:mintold"]["ticket_stage"] == "late"
    assert state["alerts"] == []
    assert state["summary"]["expired_count"] == 1


def test_seed_pool_tracks_tiny_new_pairs_and_alerts_first_discovery_once():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("SEED", 92, age=1, mcap=20_000)],
            now_iso=f"2026-08-14T15:0{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintseed"]
    assert item["status"] == "seed_pool"
    assert item["ticket_stage"] == "seed"
    assert state["pick"] is None
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [("discovered", "SEED")]
    assert state["summary"]["seed_pool_count"] == 1


def test_sub_seed_micro_caps_do_not_promote_to_front_stage():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("DUST", 96, age=0.5, mcap=2_000)],
            now_iso=f"2026-08-14T15:1{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintdust"]
    assert item["status"] == "watch"
    assert item["ticket_stage"] == "micro"
    assert state["pick"] is None
    assert state["alerts"] == []


def test_early_candidate_displays_and_alerts_after_two_strong_dual_source_confirmations():
    state = {}
    for minute in range(2):
        state = update_fresh_watch(
            state,
            [candidate("EARLY", 90, age=2, mcap=60_000)],
            now_iso=f"2026-08-14T16:0{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintearly"]
    assert item["status"] == "early_candidate"
    assert item["ticket_stage"] == "early"
    assert state["pick"]["symbol"] == "EARLY"
    assert state["pick"]["watch_status"] == "early_candidate"
    assert state["pick"]["watch_entry_reason"] == "新池 / 聪明钱/KOL共振"
    assert state["pick"]["watch_alert_gate_reason"] == "早鸟层，等确认"
    assert [alert["alert_type"] for alert in state["alerts"]] == ["early"]
    assert state["alerts"][0]["symbol"] == "EARLY"
    assert state["summary"]["early_candidate_count"] == 1


def test_bsc_two_source_candidate_waiting_for_recheck_shows_in_early_tier():
    row = candidate("WAITBSC", 90, age=4, mcap=120_000)
    row["chain"] = "bsc"
    row["contract_address"] = "0xWAITBSC"
    row["source_labels"] = ["GMGN", "DS"]
    row["sources"] = ["gmgn_live_trending", "profile_latest"]

    state = update_fresh_watch(
        {},
        [row],
        now_iso="2026-08-14T20:30:00+08:00",
        min_confirmations=3,
        chain_scope="bsc",
    )

    item = state["candidates"]["bsc:0xwaitbsc"]
    assert item["status"] == "candidate"
    assert item["ticket_stage"] == "confirmed"
    assert item["source_confirmation_ok"] is True
    assert item["watch_v2_tier"] == "early"
    assert state["summary"]["early_candidate_count"] == 0
    assert state["summary"]["first_confirmed_count"] == 0


def test_fresh_real_two_source_early_candidate_still_reaches_early_tier():
    row = candidate("REALBIRD", 90, age=0.6, mcap=72_000)
    row["chain"] = "robinhood"
    row["contract_address"] = "0xREALBIRD"
    row["source_labels"] = ["GMGN", "Noxa", "DS", "Alpha_AI"]
    row["sources"] = ["gmgn_skills_hot_searches", "noxa_launchpad"]
    row["quote_status"] = "fresh"
    row["quote_observed_at"] = "2026-09-07T12:00:00Z"
    row["quote_fingerprint"] = "realbird-market-1"

    state = update_fresh_watch(
        {},
        [row],
        now_iso="2026-09-07T12:00:00Z",
        min_confirmations=3,
    )

    item = state["candidates"]["robinhood:0xrealbird"]
    assert item["status"] == "candidate"
    assert item["confirmation_status"] == "pending_platform_labels_unverified"
    assert item["confirmation_evidence_ok"] is True
    assert item["watch_v2_tier"] == "early"


def test_gmgn_and_dexscreener_are_marked_as_second_screen_sources():
    state = {}
    row = candidate("SCREEN", 91, age=2, mcap=60_000)
    row["source_labels"] = ["GMGN", "DS"]
    row["sources"] = ["gmgn_live_trending", "profile_latest"]

    for minute in range(2):
        state = update_fresh_watch(
            state,
            [row],
            now_iso=f"2026-08-14T16:2{minute}:00+08:00",
            min_confirmations=3,
        )

    pick = state["pick"]
    assert pick["symbol"] == "SCREEN"
    assert pick["watch_pipeline_role"] == "second_screen"
    assert pick["watch_discovery_layer"] == "aggregator_second_screen"
    assert pick["watch_screening_sources"] == ["GMGN", "DS"]


def test_onchain_launch_source_is_marked_as_first_layer_with_aggregator_screening():
    state = {}
    row = candidate("CHAIN", 91, age=0.2, mcap=60_000)
    row["source_labels"] = ["Pump", "GMGN", "DEBOT"]
    row["sources"] = ["pump_fun_new_pool", "gmgn_live_trending", "debot_signal"]

    for minute in range(2):
        state = update_fresh_watch(
            state,
            [row],
            now_iso=f"2026-08-14T16:3{minute}:00+08:00",
            min_confirmations=3,
        )

    pick = state["pick"]
    assert pick["symbol"] == "CHAIN"
    assert pick["watch_pipeline_role"] == "first_layer"
    assert pick["watch_discovery_layer"] == "onchain_first_layer"
    assert pick["watch_screening_sources"] == ["GMGN", "DEBOT"]


def test_watch_state_keeps_early_pick_when_late_confirmed_exists():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [
                candidate("EARLY", 90, age=2, mcap=60_000),
                candidate("LATE", 96, age=18, mcap=1_300_000),
            ],
            now_iso=f"2026-08-14T16:1{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["pick"]["symbol"] == "EARLY"
    assert state["early_pick"]["symbol"] == "EARLY"
    assert state["early_pick"]["watch_status"] == "early_candidate"
    assert state["confirmed_pick"] is None


def test_deep_mcap_drawdown_enters_recoverable_pullback_watch():
    state = {}
    for minute, mcap in enumerate([120_000, 300_000, 240_000]):
        state = update_fresh_watch(
            state,
            [candidate("PUMP", 92, age=2 + minute * 0.1, mcap=mcap)],
            now_iso=f"2026-08-14T18:0{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["pick"]["symbol"] == "PUMP"
    state = update_fresh_watch(
        state,
        [candidate("PUMP", 94, age=2.4, mcap=120_000)],
        now_iso="2026-08-14T18:03:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintpump"]
    assert item["status"] == "pullback"
    assert item["invalid_reason"] == ""
    assert "回撤" in item["pullback_reason"]
    assert state["pick"] is None
    assert state["confirmed_pick"] is None


def test_pullback_recovery_replaces_terminal_invalidation():
    state = {}
    for minute, mcap in enumerate([120_000, 300_000, 240_000, 120_000]):
        state = update_fresh_watch(
            state,
            [candidate("RECOVER", 92, age=2 + minute * 0.1, mcap=mcap)],
            now_iso=f"2026-08-14T18:1{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["candidates"]["solana:mintrecover"]["status"] == "pullback"
    state = update_fresh_watch(
        state,
        [candidate("RECOVER", 94, age=2.5, mcap=230_000)],
        now_iso="2026-08-14T18:14:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintrecover"]
    assert item["status"] == "candidate"
    assert item["invalid_reason"] == ""
    assert item["recovered_at"] == "2026-08-14T18:14:00+08:00"
    assert [alert["alert_type"] for alert in state["new_alerts"]] == ["recovered"]
    assert "回撤修复" in state["new_alerts"][0]["reason"]


def test_pullback_recovery_does_not_alert_when_current_row_has_hard_risk():
    state = {}
    for minute, mcap in enumerate([120_000, 300_000, 240_000, 120_000]):
        state = update_fresh_watch(
            state,
            [candidate("RISKRECOVER", 92, age=2 + minute * 0.1, mcap=mcap)],
            now_iso=f"2026-08-14T18:2{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["candidates"]["solana:mintriskrecover"]["status"] == "pullback"
    state = update_fresh_watch(
        state,
        [candidate("RISKRECOVER", 94, age=2.5, mcap=230_000, smart_money=20, kol=5) | {"top10_holder_pct": 70}],
        now_iso="2026-08-14T18:24:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintriskrecover"]
    assert item["status"] == "invalidated"
    assert "Top10" in item["invalid_reason"]
    assert "recovered" not in [alert["alert_type"] for alert in state["new_alerts"]]


def test_big_winner_becomes_achieved_gold_instead_of_invalidated():
    state = update_fresh_watch(
        {},
        [candidate("WIN", 92, age=1, mcap=40_000)],
        now_iso="2026-08-14T18:20:00+08:00",
        min_confirmations=3,
    )
    state = update_fresh_watch(
        state,
        [candidate("WIN", 92, age=4, mcap=1_600_000)],
        now_iso="2026-08-14T18:21:00+08:00",
        min_confirmations=3,
    )
    state = update_fresh_watch(
        state,
        [candidate("WIN", 92, age=7, mcap=500_000)],
        now_iso="2026-08-14T18:22:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintwin"]
    assert item["status"] == "achieved_gold"
    assert "已成金狗" in item["achieved_gold_reason"]
    assert item["gold_capture_type"] == "review_only"
    assert state["pick"] is None
    assert state["summary"]["achieved_gold_count"] == 1
    assert state["summary"]["confirmed_runner_count"] == 0
    assert state["summary"]["review_only_achieved_gold_count"] == 1


def test_confirmed_winner_is_counted_as_confirmed_runner():
    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [candidate("RUN", 94, age=2 + minute * 0.1, mcap=140_000)],
            now_iso=f"2026-08-14T18:4{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["candidates"]["solana:mintrun"]["first_confirmed_mcap"] == 140_000
    state = update_fresh_watch(
        state,
        [candidate("RUN", 95, age=3, mcap=1_800_000)],
        now_iso="2026-08-14T18:43:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintrun"]
    assert item["status"] == "achieved_gold"
    assert item["gold_capture_type"] == "confirmed_runner"
    assert state["summary"]["historical_first_confirmed_count"] == 1
    assert state["summary"]["confirmed_runner_count"] == 1
    assert state["summary"]["review_only_achieved_gold_count"] == 0
    assert state["summary"]["first_confirmation_success_rate_pct"] == 100.0
    assert state["summary"]["latest_confirmed_runner_symbol"] == "RUN"


def test_mature_tens_of_millions_token_is_achieved_gold_not_current_ticket():
    state = update_fresh_watch(
        {},
        [candidate("NIULAI", 90, age=160, mcap=64_000_000)],
        now_iso="2026-08-14T18:30:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintniulai"]
    assert item["status"] == "achieved_gold"
    assert "当前市值" in item["achieved_gold_reason"]
    assert state["pick"] is None


def test_strong_confirmation_count_is_consecutive_not_lifetime():
    state = update_fresh_watch(
        {},
        [candidate("DOG", 90)],
        now_iso="2026-08-14T19:00:00+08:00",
        min_confirmations=3,
    )
    state = update_fresh_watch(
        state,
        [candidate("DOG", 60)],
        now_iso="2026-08-14T19:01:00+08:00",
        min_confirmations=3,
    )
    state = update_fresh_watch(
        state,
        [candidate("DOG", 90)],
        now_iso="2026-08-14T19:02:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintdog"]
    assert item["strong_scan_count"] == 1
    assert item["lifetime_strong_scan_count"] == 2
    assert state["pick"] is None


def test_watch_state_records_first_seen_market_cap_for_review():
    state = update_fresh_watch(
        {},
        [candidate("SAUCE", 90, age=0.8, mcap=28_000)],
        now_iso="2026-08-14T16:20:00+08:00",
        min_confirmations=3,
    )
    state = update_fresh_watch(
        state,
        [candidate("SAUCE", 91, age=1.1, mcap=84_000)],
        now_iso="2026-08-14T16:21:00+08:00",
        min_confirmations=3,
    )

    item = state["candidates"]["solana:mintsauce"]
    assert item["first_seen_mcap"] == 28_000
    assert item["last_seen_mcap"] == 84_000
    assert item["max_seen_mcap"] == 84_000
    assert item["mcap_multiple_from_first"] == 3


def test_gold_ticket_alerts_early_then_requires_three_confirmations_for_confirmation():
    state = {}
    for minute in range(2):
        state = update_fresh_watch(
            state,
            [candidate("GOLD", 92, age=4, mcap=180_000)],
            now_iso=f"2026-08-14T17:0{minute}:00+08:00",
            min_confirmations=3,
        )

    assert state["pick"] is None
    assert [alert["alert_type"] for alert in state["alerts"]] == ["early"]

    state = update_fresh_watch(
        state,
        [candidate("GOLD", 92, age=4, mcap=180_000)],
        now_iso="2026-08-14T17:02:00+08:00",
        min_confirmations=3,
    )

    assert state["pick"]["symbol"] == "GOLD"
    assert state["pick"]["watch_status"] == "strong_candidate"
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [
        ("early", "GOLD"), ("confirmed", "GOLD")
    ]


def test_watch_summary_explains_pending_confirmation():
    state = update_fresh_watch(
        {},
        [candidate("DOG", 84)],
        now_iso="2026-08-14T12:00:00+08:00",
        min_confirmations=3,
    )

    summary = gold_watch.watch_summary(state)

    assert summary["tracked_count"] == 1
    assert summary["strong_candidate_count"] == 0
    assert "还差 2 次确认" in summary["top_pending_reason"]


def test_single_source_observation_does_not_count_as_confirmed_alert():
    row = candidate("SOLO", 94, age=4, mcap=180_000)
    row["source_labels"] = ["GMGN"]
    row["sources"] = ["gmgn_live_trending"]
    row["source_count"] = 1

    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [row],
            now_iso=f"2026-08-14T20:0{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintsolo"]
    assert item["status"] == "candidate"
    assert item["source_confirmation_ok"] is False
    assert "第二个独立来源" in item["source_confirmation_reason"]
    assert "第二个独立来源" in state["summary"]["top_pending_reason"]
    assert state["pick"] is None
    assert [alert["alert_type"] for alert in state["alerts"]] == ["early"]
    assert state["alerts"][0]["first_confirmed_at"] is None
    assert state["alerted_keys"] == []
    assert not item.get("first_confirmed_at")


def test_strong_signal_requires_gmgn_or_debot_core_source():
    row = candidate("NOCORE", 94, age=4, mcap=180_000)
    row["source_labels"] = ["DS", "Birdeye"]
    row["sources"] = ["profile_latest", "birdeye_trending"]
    row["source_count"] = 2

    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [row],
            now_iso=f"2026-08-14T20:1{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintnocore"]
    assert item["status"] == "candidate"
    assert item["source_confirmation_ok"] is False
    assert "GMGN/DeBot" in item["source_confirmation_reason"]
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [("discovered", "NOCORE")]


def test_onchain_first_layer_plus_gmgn_counts_as_two_source_confirmation():
    row = candidate("CHAINOK", 94, age=4, mcap=180_000)
    row["source_labels"] = ["Pump", "GMGN"]
    row["sources"] = ["pump_fun_new_pool", "gmgn_live_trending"]
    row["source_count"] = 2

    state = {}
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [row],
            now_iso=f"2026-08-14T20:2{minute}:00+08:00",
            min_confirmations=3,
        )

    item = state["candidates"]["solana:mintchainok"]
    assert item["status"] == "strong_candidate"
    assert item["source_confirmation_ok"] is True
    assert item["source_confirmation_sources"] == ["ONCHAIN", "GMGN"]
    assert item["screening_sources"] == ["GMGN"]
    assert state["confirmed_pick"]["symbol"] == "CHAINOK"
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [
        ("early", "CHAINOK"), ("confirmed", "CHAINOK")
    ]


def test_bsc_scope_drops_non_bsc_candidates_and_keeps_bsc_confirmations():
    sol_row = candidate("SOLROW", 96, age=4, mcap=180_000)
    bsc_row = candidate("BSCROW", 96, age=4, mcap=180_000)
    bsc_row["chain"] = "bsc"
    bsc_row["contract_address"] = "0xBSCROW"
    bsc_row["source_labels"] = ["Flap", "GMGN"]
    bsc_row["sources"] = ["flap_launchpad", "gmgn_live_trending"]

    state = {
        "candidates": {
            "solana:old": {
                "symbol": "OLD",
                "chain": "solana",
                "contract_address": "MintOld",
                "last_seen_at": "2026-08-14T19:00:00+08:00",
            }
        },
        "alerts": [{"symbol": "OLD", "chain": "solana", "contract_address": "MintOld"}],
        "alerted_keys": ["solana:old"],
    }
    for minute in range(3):
        state = update_fresh_watch(
            state,
            [sol_row, bsc_row],
            now_iso=f"2026-08-14T21:0{minute}:00+08:00",
            min_confirmations=3,
            chain_scope="bsc",
        )

    assert "solana:old" not in state["candidates"]
    assert "solana:mintsolrow" not in state["candidates"]
    assert state["candidates"]["bsc:0xbscrow"]["status"] == "strong_candidate"
    assert state["candidates"]["bsc:0xbscrow"]["discovery_layer"] == "onchain_first_layer"
    assert state["candidates"]["bsc:0xbscrow"]["source_confirmation_sources"] == ["ONCHAIN", "GMGN"]
    assert state["confirmed_pick"]["symbol"] == "BSCROW"
    assert [(alert["alert_type"], alert["symbol"]) for alert in state["alerts"]] == [
        ("early", "BSCROW"), ("confirmed", "BSCROW")
    ]


def quote(symbol="DOG", minute=0, **overrides):
    return {**candidate(symbol, 94), "quote_observed_at": f"2026-09-07T12:{minute:02d}:00Z",
            "quote_fingerprint": f"market-{minute}", "quote_status": "fresh",
            "pair_address": "pair-one", **overrides}


def test_cached_report_and_retimestamped_identical_quote_do_not_confirm():
    row = quote()
    state = gold_watch.update_watch_state({}, [row], "2026-09-07T12:00:00Z")
    for minute in range(1, 4):
        row = {**row, "quote_observed_at": f"2026-09-07T12:0{minute}:00Z",
               "updated_at": f"2026-09-07T12:0{minute}:00Z"}
        state = gold_watch.update_watch_state(state, [row], f"2026-09-07T12:0{minute}:00Z")
    assert state["candidates"]["solana:mintdog"]["strong_scan_count"] == 1
    assert state["new_alerts"] == []
    assert state["pick"] is None


def test_fast_polls_wait_for_meaningful_evidence_and_wall_time():
    state = gold_watch.update_watch_state({}, [quote()], "2026-09-07T12:00:00Z")
    for second in (10, 20, 59):
        stamp = f"2026-09-07T12:00:{second:02d}Z"
        state = gold_watch.update_watch_state(state, [quote(quote_observed_at=stamp, quote_fingerprint=str(second))], stamp)
    assert state["candidates"]["solana:mintdog"]["strong_scan_count"] == 1
    for minute in (1, 2):
        state = gold_watch.update_watch_state(state, [quote(minute=minute)], f"2026-09-07T12:0{minute}:00Z")
    assert [a["alert_type"] for a in state["new_alerts"]] == ["confirmed"]
    assert state["pick"]["symbol"] == "DOG"


@pytest.mark.parametrize("overrides", [
    {"quote_status": "stale"}, {"quote_status": "quarantined"},
    {"quote_observed_at": "2026-09-07T11:00:00Z"},
    {"quote_observed_at": "2026-09-07T12:05:00Z"},
    {"quote_observed_at": None}, {"quote_fingerprint": None},
])
def test_unusable_quote_cannot_be_rescued_by_fresh_smart_money(overrides):
    row = quote(**overrides, smart_money_evidence={"latest_evidence_at": "2026-09-07T12:00:00Z"})
    state = gold_watch.update_watch_state({}, [row], "2026-09-07T12:00:00Z", min_confirmations=1)
    assert state["new_alerts"] == []
    assert state["pick"] is None
    assert state["candidates"]["solana:mintdog"]["strong_scan_count"] == 0


def test_unavailable_quote_stays_pending_even_when_sources_are_fresh():
    state = {}
    for minute in range(2):
        row = {
            **quote(
                minute=minute,
                quote_status="unavailable",
                quote_observed_at=None,
                quote_fingerprint=None,
            ),
            "sources": ["gmgn_skills_hot_searches", "profile_latest"],
            "source_labels": ["GMGN", "DS"],
        }
        state = update_fresh_watch(state, [row], f"2026-09-07T12:0{minute}:00Z", min_confirmations=2)
    item = state["candidates"]["solana:mintdog"]
    assert item["confirmation_status"] == "waiting_fresh_market"
    assert item["confirmation_evidence_ok"] is False
    assert item["watch_v2_tier"] != "confirmed"
    assert state["new_alerts"] == []
    assert state["pick"] is None


def test_missing_source_time_and_duplicate_rows_cannot_manufacture_confirmations():
    state = {}
    for minute in range(3):
        stamp = f"2026-09-07T12:0{minute}:00Z"
        row = {**candidate("DOG", 94), "updated_at": stamp, "fetched_at": stamp}
        state = gold_watch.update_watch_state(state, [row, row], stamp)
    assert state["candidates"]["solana:mintdog"]["strong_scan_count"] == 0
    assert state["new_alerts"] == []


def test_lower_rank_new_confirmation_is_not_blocked_by_already_alerted_leader():
    state = gold_watch.update_watch_state({}, [quote("LEAD", gold_dog_conviction_score=99)],
                                         "2026-09-07T12:00:00Z", min_confirmations=1)
    state = gold_watch.update_watch_state(state, [quote("LEAD", 1, gold_dog_conviction_score=99), quote("NEXT", 1)],
                                         "2026-09-07T12:01:00Z", min_confirmations=1)
    assert [a["symbol"] for a in state["new_alerts"]] == ["NEXT"]
    assert state["pick"]["symbol"] == "NEXT"


def test_new_alert_batch_is_not_truncated_to_history_limit():
    rows = [quote(f"TOKEN{n}") for n in range(60)]
    state = gold_watch.update_watch_state({}, rows, "2026-09-07T12:00:00Z", min_confirmations=1)
    assert len(state["new_alerts"]) == 60
    assert len(state["alerted_keys"]) == 60
    assert len(state["alerts"]) == 50


def test_early_then_confirmed_then_invalidated_alerts_remain_distinct():
    row = quote(source_labels=["GMGN"], sources=["gmgn_live_trending"])
    state = gold_watch.update_watch_state({}, [row], "2026-09-07T12:00:00Z")
    assert [a["alert_type"] for a in state["new_alerts"]] == ["early"]
    assert state["alerted_keys"] == []
    # Reload from JSON to exercise alert restoration, not just in-memory state.
    import json
    state = json.loads(json.dumps(state))
    for minute in (1, 2):
        state = gold_watch.update_watch_state(state, [quote(minute=minute)], f"2026-09-07T12:0{minute}:00Z")
    assert [a["alert_type"] for a in state["alerts"]] == ["early", "confirmed"]
    assert state["candidates"]["solana:mintdog"]["first_confirmed_at"] == "2026-09-07T12:02:00Z"
    row = quote(minute=3, top10_holder_pct=70)
    state = gold_watch.update_watch_state(state, [row], "2026-09-07T12:03:00Z")
    assert [a["alert_type"] for a in state["new_alerts"]] == ["invalidated"]
    assert "Top10" in state["new_alerts"][0]["reason"]
    state = gold_watch.update_watch_state(state, [row], "2026-09-07T12:04:00Z")
    assert state["new_alerts"] == []


@pytest.mark.parametrize("risk", [{"top10_holder_pct": 70}, {"max_holder_pct": 30},
                                 {"change_h1": -30}, {"risk_flags": ["honeypot"]}])
def test_single_source_early_alert_keeps_hard_risk_gates(risk):
    row = quote(source_labels=["GMGN"], sources=["gmgn_live_trending"], **risk)
    state = gold_watch.update_watch_state({}, [row], "2026-09-07T12:00:00Z")
    assert state["alerts"] == []
    assert state["candidates"]["solana:mintdog"]["status"] == "invalidated"


@pytest.mark.parametrize("source", ["985_monitor", "bsc_onchain", "profile_latest"])
def test_single_source_initial_observation_accepts_actual_fresh_source(source):
    row = quote(source_labels=[], sources=[source])
    state = gold_watch.update_watch_state({}, [row], "2026-09-07T12:00:00Z")
    assert [alert["alert_type"] for alert in state["new_alerts"]] == ["early"]
    assert state["pick"] is None
    assert state["alerted_keys"] == []


def test_risky_observations_do_not_build_confirmation_streak():
    state = {}
    for minute in range(3):
        state = gold_watch.update_watch_state(state, [quote(minute=minute, top10_holder_pct=70)],
                                             f"2026-09-07T12:0{minute}:00Z")
    state = gold_watch.update_watch_state(state, [quote(minute=3)], "2026-09-07T12:03:00Z")
    assert state["candidates"]["solana:mintdog"]["strong_scan_count"] == 1
    assert state["new_alerts"] == []


def test_stale_risk_report_does_not_consume_fresh_invalidation_alert():
    state = gold_watch.update_watch_state({}, [quote()], "2026-09-07T12:00:00Z", min_confirmations=1)
    state = gold_watch.update_watch_state(state, [quote(minute=1, quote_status="stale", top10_holder_pct=70)],
                                         "2026-09-07T12:01:00Z")
    assert state["new_alerts"] == []
    state = gold_watch.update_watch_state(state, [quote(minute=2, top10_holder_pct=70)], "2026-09-07T12:02:00Z")
    assert [a["alert_type"] for a in state["new_alerts"]] == ["invalidated"]


def test_long_gap_breaks_confirmation_streak_even_without_intermediate_poll():
    state = gold_watch.update_watch_state({}, [quote()], "2026-09-07T12:00:00Z")
    state = gold_watch.update_watch_state(state, [quote(minute=20)], "2026-09-07T12:20:00Z")
    assert state["candidates"]["solana:mintdog"]["strong_scan_count"] == 1


def wallet_row(tmp_path, minute=0, *, sell=0, buys=True, **overrides):
    import json
    from alpha_smart_money_evidence import enrich_smart_money
    from test_alpha_smart_money_evidence import TOKEN, NOW, inbox, two_buys, trade, profitable_profile, WALLET, WALLET2

    (tmp_path / "gmgn-smart-money-top50.json").write_text(json.dumps({"wallets": [
        profitable_profile(a) for a in (WALLET, WALLET2, "0x" + "f" * 40)]}), encoding="utf-8")

    trades = two_buys() if buys else []
    if sell:
        trades.append(trade(monitor985_wallet="0x" + "f" * 40,
                            monitor985_tx_hash="0x" + "f" * 64,
                            monitor985_trade_side="SELL", monitor985_trade_amount_usd=sell))
    inbox(tmp_path, "gmgn-wallet-flow.json", trades)
    row = quote(minute=minute, chain="bsc", contract_address=TOKEN,
                source_labels=["GMGN"], sources=["gmgn_cli_smartmoney"], **overrides)
    return enrich_smart_money([row], tmp_path, NOW)[0]


@pytest.mark.parametrize("labels", [["GMGN"], []])
def test_wallet_evidence_confirms_without_two_platform_labels(tmp_path, labels):
    state = {}
    for minute in range(3):
        row = wallet_row(tmp_path, minute)
        row["source_labels"] = labels
        row["sources"] = ["gmgn_cli_smartmoney"] if labels else []
        state = gold_watch.update_watch_state(state, [row], f"2026-09-07T12:0{minute}:00Z")
    item = next(iter(state["candidates"].values()))
    assert item["source_confirmation_ok"] is False
    assert item["confirmation_status"] == "confirmed_wallet_evidence"
    assert state["pick"]["confirmation_basis"] == "wallet_evidence"
    assert [a["alert_type"] for a in state["new_alerts"]] == ["confirmed"]
    assert "是否由不同人控制仍未核实" in state["new_alerts"][0]["confirmation_reason"]


def test_wallet_evidence_needs_market_progression_even_with_one_confirmation_setting(tmp_path):
    row = wallet_row(tmp_path)
    state = gold_watch.update_watch_state({}, [row], "2026-09-07T12:00:00Z", min_confirmations=1)
    assert state["confirmed_pick"] is None
    for minute in (1, 2):
        row = {**row, "quote_observed_at": f"2026-09-07T12:0{minute}:00Z"}
        state = gold_watch.update_watch_state(state, [row], f"2026-09-07T12:0{minute}:00Z", min_confirmations=1)
    assert state["confirmed_pick"] is None
    row = wallet_row(tmp_path, 3)
    state = gold_watch.update_watch_state(state, [row], "2026-09-07T12:03:00Z", min_confirmations=1)
    assert state["confirmed_pick"]["confirmation_status"] == "confirmed_wallet_evidence"


def test_material_sell_dominance_blocks_platform_fallback_and_early_alert(tmp_path):
    state = {}
    for minute in range(3):
        row = wallet_row(tmp_path, minute, sell=2000)
        row["source_labels"] = ["GMGN", "DS"]
        state = gold_watch.update_watch_state(state, [row], f"2026-09-07T12:0{minute}:00Z")
    item = next(iter(state["candidates"].values()))
    assert item["source_confirmation_ok"] is False
    assert item["confirmation_status"] == "blocked_sell_dominance"
    assert item["strong_scan_count"] == 0
    assert state["pick"] is None
    assert state["alerts"] == []


@pytest.mark.parametrize("initial_type", ["early", "confirmed"])
def test_deterioration_alert_for_previously_alerted_token_is_once_per_episode(tmp_path, initial_type):
    state = {}
    count = 1 if initial_type == "early" else 3
    for minute in range(count):
        state = gold_watch.update_watch_state(state, [wallet_row(tmp_path, minute)], f"2026-09-07T12:0{minute}:00Z")
    assert state["alerts"][-1]["alert_type"] == initial_type
    for minute in (3, 4):
        row = wallet_row(tmp_path, minute, sell=2000)
        state = gold_watch.update_watch_state(state, [row], f"2026-09-07T12:0{minute}:00Z")
        assert [a["alert_type"] for a in state["new_alerts"]] == (["deterioration"] if minute == 3 else [])
    assert state["alerts"][-1]["first_confirmed_at"] is None
    assert state["alerts"][-1]["confirmation_status"] == "blocked_sell_dominance"
    state = gold_watch.update_watch_state(state, [wallet_row(tmp_path, 5)], "2026-09-07T12:05:00Z")
    state = gold_watch.update_watch_state(state, [wallet_row(tmp_path, 6, sell=2500)], "2026-09-07T12:06:00Z")
    assert [a["alert_type"] for a in state["new_alerts"]] == ["deterioration"]


@pytest.mark.parametrize("risk", [{"top10_holder_pct": 70}, {"max_holder_pct": 30},
                                 {"risk_flags": ["honeypot"]}, {"change_h1": -30}])
def test_hard_risk_overrides_wallet_support_and_deterioration(tmp_path, risk):
    state = gold_watch.update_watch_state({}, [wallet_row(tmp_path)], "2026-09-07T12:00:00Z")
    row = wallet_row(tmp_path, 1, sell=2000, **risk)
    state = gold_watch.update_watch_state(state, [row], "2026-09-07T12:01:00Z")
    assert next(iter(state["candidates"].values()))["confirmation_status"] == "blocked_risk"
    assert [a["alert_type"] for a in state["new_alerts"]] == ["invalidated"]
    assert state["pick"] is None


def test_aggregate_only_platform_fallback_remains_explicitly_unverified():
    state = {}
    for minute in range(3):
        row = quote(minute=minute, smart_money=500,
                    smart_money_evidence={"unique_wallet_count": 500, "buy_usd": 999999})
        state = gold_watch.update_watch_state(state, [row], f"2026-09-07T12:0{minute}:00Z")
    assert state["pick"]["confirmation_status"] == "confirmed_platform_labels_unverified"
    assert state["pick"]["smart_money_confirmation"]["buy_support"] is False
    assert "尚无足够的逐笔买入证据" in state["new_alerts"][0]["confirmation_reason"]


def test_write_watch_state_replaces_only_complete_json(tmp_path, monkeypatch):
    import json
    path = tmp_path / "state.json"
    gold_watch.write_watch_state(path, {"generation": 1})
    replace = gold_watch.os.replace
    calls = []

    def inspect_replace(source, destination):
        assert json.loads(path.read_text()) == {"generation": 1}
        assert source.parent == path.parent
        assert json.loads(source.read_text()) == {"generation": 2, "rows": [1, 2, 3]}
        calls.append(source)
        return replace(source, destination)

    monkeypatch.setattr(gold_watch.os, "replace", inspect_replace)
    gold_watch.write_watch_state(path, {"generation": 2, "rows": [1, 2, 3]})
    assert len(calls) == 1
    assert json.loads(path.read_text())["generation"] == 2
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["serialize", "replace"])
def test_atomic_write_failure_preserves_previous_state_and_cleans_temp(tmp_path, monkeypatch, failure):
    path = tmp_path / "state.json"
    gold_watch.write_watch_state(path, {"generation": 1})
    old = path.read_bytes()
    if failure == "replace":
        def fail_replace(*args):
            raise PermissionError("reader holds old file")
        monkeypatch.setattr(gold_watch.os, "replace", fail_replace)
        monkeypatch.setattr(gold_watch.time, "sleep", lambda _: None)
        state, error = {"generation": 2}, PermissionError
    else:
        state, error = {"generation": 2, "bad": object()}, TypeError
    with pytest.raises(error):
        gold_watch.write_watch_state(path, state)
    assert path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_retries_transient_windows_reader_lock(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    gold_watch.write_watch_state(path, {"generation": 1})
    replace = gold_watch.os.replace
    attempts = []

    def busy_once(source, destination):
        attempts.append(source)
        if len(attempts) == 1:
            raise PermissionError("reader briefly holds file")
        return replace(source, destination)

    monkeypatch.setattr(gold_watch.os, "replace", busy_once)
    monkeypatch.setattr(gold_watch.time, "sleep", lambda _: None)
    gold_watch.write_watch_state(path, {"generation": 2})
    assert len(attempts) == 2
    assert gold_watch.load_watch_state(path) == {"generation": 2}
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_writes_allow_concurrent_complete_json_reads(tmp_path):
    import threading

    path = tmp_path / "state.json"
    gold_watch.write_watch_state(path, {"generation": 0, "payload": [0] * 100})
    stop = threading.Event()
    ready = threading.Event()
    failures = []
    reads = []

    def read_state():
        while not stop.is_set():
            try:
                state = gold_watch.load_watch_state(path)
                assert state["payload"] == [state["generation"]] * 100
                reads.append(state["generation"])
            except Exception as error:
                failures.append(error)
            finally:
                ready.set()

    reader = threading.Thread(target=read_state)
    reader.start()
    try:
        assert ready.wait(2)
        for generation in range(1, 15):
            gold_watch.write_watch_state(path, {"generation": generation, "payload": [generation] * 100})
    finally:
        stop.set()
        reader.join(timeout=2)
    assert not reader.is_alive()
    assert reads
    assert not failures


def test_watch_state_drops_undated_rows_and_caps_recent_history():
    now = "2026-09-10T04:00:00+00:00"
    candidates = {
        f"bsc:0x{index:040x}": {
            "chain": "bsc",
            "contract_address": f"0x{index:040x}",
            "last_seen_at": "2026-09-10T03:00:00+00:00",
        }
        for index in range(gold_watch.MAX_STORED_CANDIDATES + 2)
    }
    candidates["bsc:undated"] = {"chain": "bsc", "contract_address": "undated"}

    state = gold_watch.update_watch_state({"candidates": candidates}, [], now)

    assert len(state["candidates"]) == gold_watch.MAX_STORED_CANDIDATES
    assert "bsc:undated" not in state["candidates"]
