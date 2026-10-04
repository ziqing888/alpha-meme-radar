import importlib.util
import json
import sys
from pathlib import Path
import pytest


MODULE_PATH = Path(__file__).with_name("alpha_meme_live_refresh.py")
SPEC = importlib.util.spec_from_file_location("alpha_meme_live_refresh", MODULE_PATH)
meme_live = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = meme_live
SPEC.loader.exec_module(meme_live)


@pytest.fixture(autouse=True)
def isolate_background_token_research(monkeypatch):
    monkeypatch.setattr(
        meme_live,
        "schedule_token_intelligence_research",
        lambda *args, **kwargs: False,
    )


def test_fast_report_publishes_monitor_v3_from_one_observation_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(meme_live, "now_iso", lambda: "2026-09-10T12:00:02+00:00")
    raw_event = {
        "event_id": "fast-raw-event", "chain": "bsc",
        "contract_address": "0x2222222222222222222222222222222222222222",
        "provider_family": "gmgn", "provider_feed": "gmgn_trenches",
        "event_type": "new_launch", "event_at": "2026-09-10T12:00:00+00:00",
        "observed_at": "2026-09-10T12:00:01+00:00", "evidence_role": "discovery",
        "signal_lane": "new_launch", "counts_for_resonance": True,
        "provider_event_id": "trench-1", "source_url": None,
        "raw_fingerprint": "fast-raw-1", "upstream_provider": None,
        "event_time_basis": "event_at", "payload": {},
    }
    batch = {"candidates": [], "events": [raw_event], "rejections": [], "errors": [],
             "feed_counts": {"gmgn_trenches": 1}}
    batch_calls = []
    snapshots = []

    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy loader called")))
    monkeypatch.setattr(meme_live, "load_meme_observation_batch", lambda limit, concurrency: batch_calls.append((limit, concurrency)) or batch, raising=False)
    monkeypatch.setattr(meme_live, "refresh_token_intelligence", lambda *args, **kwargs: {})
    monkeypatch.setattr(meme_live, "refresh_rating_v2_shadow", lambda *args, **kwargs: {"mode": "shadow_only_no_execution_effect"})

    payload, _ = meme_live.build_meme_live_report(
        {"meta": {}, "meme_rows": []}, out_dir=tmp_path,
        meme_source_limit=20, meme_top=10, meme_potential_top=1,
        meme_shadow_top=10, concurrency=2, gold_watch_confirmations=3,
        fast_publish=lambda snapshot: snapshots.append(snapshot),
    )

    assert batch_calls == [(20, 2)]
    assert payload["meme_rows"] == []
    assert payload["monitor_v3"]["schema_version"] == 3
    assert payload["monitor_v3"]["tokens"][0]["events"] == [raw_event]
    assert snapshots[0]["monitor_v3"] == payload["monitor_v3"]
    assert not (tmp_path / "alpha-meme-monitor-v3-latest.json").exists()
    rating_v2 = json.loads((tmp_path / "alpha-rating-v2-dataset.json").read_text(encoding="utf-8"))
    assert "0x2222222222222222222222222222222222222222" not in json.dumps(rating_v2)


def test_fast_source_failure_reuses_no_legacy_rows_as_events(tmp_path, monkeypatch):
    monkeypatch.setattr(meme_live, "now_iso", lambda: "2026-09-10T12:00:02+00:00")
    raw_event = {
        "event_id": "stable-event", "chain": "bsc",
        "contract_address": "0x5555555555555555555555555555555555555555",
        "provider_family": "gmgn", "provider_feed": "gmgn_trenches",
        "event_type": "new_launch", "event_at": "2026-09-10T12:00:00+00:00",
        "observed_at": "2026-09-10T12:00:01+00:00", "evidence_role": "discovery",
        "signal_lane": "new_launch", "counts_for_resonance": True,
        "provider_event_id": "stable-1", "source_url": None,
        "raw_fingerprint": "stable-raw", "upstream_provider": None,
        "event_time_basis": "event_at", "payload": {},
    }
    first_batch = {
        "candidates": [], "events": [raw_event], "rejections": [], "errors": [],
        "feed_counts": {"gmgn_trenches": 1},
    }
    calls = 0

    def load_batch(limit, concurrency):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_batch
        raise TimeoutError("gmgn timed out")

    monkeypatch.setattr(meme_live, "load_meme_observation_batch", load_batch)
    monkeypatch.setattr(meme_live, "refresh_token_intelligence", lambda *args, **kwargs: {})
    monkeypatch.setattr(meme_live, "refresh_rating_v2_shadow", lambda *args, **kwargs: {"mode": "shadow_only_no_execution_effect"})

    kwargs = {
        "out_dir": tmp_path, "meme_source_limit": 20, "meme_top": 10,
        "meme_potential_top": 1, "meme_shadow_top": 10, "concurrency": 2,
        "gold_watch_confirmations": 3,
    }
    first, _ = meme_live.build_meme_live_report({"meta": {}, "meme_rows": []}, **kwargs)
    second, _ = meme_live.build_meme_live_report(first, **kwargs)

    token = second["monitor_v3"]["tokens"][0]
    assert calls == 2
    assert [event["event_id"] for event in token["events"]] == ["stable-event"]
    assert token["events"][0]["event_at"] == "2026-09-10T12:00:00+00:00"
    assert token["events"][0]["observed_at"] == "2026-09-10T12:00:01+00:00"
    assert second["monitor_v3"]["monitor_status"] == "degraded"
    assert second["monitor_v3"]["source_health"]["gmgn"]["status"] == "error"
    assert "meme_sources" not in second["monitor_v3"]["source_health"]


def test_merge_refresh_changes_keeps_newer_concurrent_monitor_v3():
    previous = {"monitor_v3": {"schema_version": 3, "observed_at": "2026-09-10T12:00:00+00:00", "tokens": []}}
    updated = {"monitor_v3": {"schema_version": 3, "observed_at": "2026-09-10T12:01:00+00:00", "tokens": [{"id": "older"}]}}
    latest = {"monitor_v3": {"schema_version": 3, "observed_at": "2026-09-10T12:02:00+00:00", "tokens": [{"id": "newer"}]}}

    merged = meme_live.merge_refresh_changes(previous, updated, latest)

    assert merged["monitor_v3"] == latest["monitor_v3"]


def test_full_report_writer_arbitrates_against_newer_persisted_v3(tmp_path):
    report_path = tmp_path / "alpha-radar-report-latest.json"
    state_path = tmp_path / "alpha-meme-monitor-v3-state.json"
    older = {"schema_version": 3, "observed_at": "2026-09-10T12:01:00+00:00", "tokens": [{"id": "older"}]}
    newer = {"schema_version": 3, "observed_at": "2026-09-10T12:02:00+00:00", "tokens": [{"id": "newer"}]}
    state_path.write_text(json.dumps(newer), encoding="utf-8")
    payload = {"meta": {}, "monitor_v3": older}

    writer = getattr(meme_live, "publish_latest_report", None)
    if writer is None:
        meme_live.atomic_write_json(report_path, payload)
        written = payload
    else:
        written = writer(report_path, payload, out_dir=tmp_path)

    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["monitor_v3"] == newer
    assert persisted["monitor_v3"] == newer


@pytest.fixture(autouse=True)
def isolated_source_stubs(monkeypatch):
    monkeypatch.setattr(meme_live.alpha_gmgn_wallet_flow, "refresh", lambda out_dir: {"ok": True, "reason": "test_stub"})
    monkeypatch.setattr(meme_live, "refresh_prelaunch_watch", lambda out_dir: {"candidates": [], "sources": {}, "errors": []})
    for name in ("refresh_bsc_public_pairs", "refresh_noxa_launchpad", "refresh_proficy_trending",
                 "refresh_985_monitor", "refresh_wind_monitor", "refresh_gmgn_smart_money_top50"):
        monkeypatch.setattr(meme_live, name, lambda out_dir: {"ok": True, "reason": "test_stub"})


def test_gmgn_collector_supersedes_legacy_wallet_activity(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(meme_live, "refresh_gmgn_skills", lambda out_dir: calls.append("skills") or {"ok": True})
    monkeypatch.setattr(meme_live.alpha_gmgn_wallet_flow, "refresh", lambda out_dir: calls.append("wallet") or {"ok": True})

    skills_status, wallet_status = meme_live.refresh_gmgn_sources(tmp_path)

    assert calls == ["skills"]
    assert skills_status["ok"] is True
    assert wallet_status["skipped"] is True
    assert wallet_status["reason"] == "superseded_by_gmgn_skills_track"


def test_publish_token_intelligence_cache_updates_report(tmp_path):
    token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text(
        json.dumps({"meta": {}, "meme_rows": [{"chain": "bsc", "contract_address": token}]}),
        encoding="utf-8",
    )
    (tmp_path / "token-intelligence.json").write_text(
        json.dumps({
            "schema_version": 1,
            "status": "ready",
            "generated_at": "2026-09-10T02:00:00+00:00",
            "records": {f"bsc:{token}": {
                "status": "ready",
                "one_line_judgement": "exact result",
                "project_narrative": "grounded narrative",
                "attention_evidence": [],
                "smart_wallets": [],
                "identity": {"official_status": "match", "official_contract": token, "alternate_contracts": []},
                "risks": [],
                "missing_evidence": [],
                "sources": [],
                "generated_at": "2026-09-10T02:00:00+00:00",
            }},
        }),
        encoding="utf-8",
    )

    attached = meme_live.publish_token_intelligence_cache(
        report_path,
        tmp_path,
        now="2026-09-10T02:01:00+00:00",
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert attached == 1
    assert report["meme_rows"][0]["token_intelligence"]["one_line_judgement"] == "exact result"


def test_refresh_publishes_live_report_before_slow_paper_cycle(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    report_path.write_text(json.dumps({"meta": {}, "meme_rows": []}), encoding="utf-8")
    live_report = {
        "meta": {"meme_live_generated_at": "2026-09-10T01:00:00+00:00"},
        "meme_rows": [{"symbol": "FAST"}],
        "meme_watch_universe": [],
    }
    observed = []
    published_during_build = []

    monkeypatch.setattr(meme_live, "refresh_gmgn_skills", lambda out_dir: {"ok": True})

    def build_report(*args, **kwargs):
        kwargs["fast_publish"](live_report)
        published_during_build.append(json.loads(report_path.read_text(encoding="utf-8")))
        return live_report, {}

    monkeypatch.setattr(meme_live, "build_meme_live_report", build_report)

    def paper_cycle(report, *, out_dir, now):
        observed.append(json.loads(report_path.read_text(encoding="utf-8")))
        return {"mode": "read_only_paper"}

    monkeypatch.setattr(meme_live, "run_paper_trading_cycle", paper_cycle)
    monkeypatch.setattr(meme_live, "schedule_token_intelligence_research", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        meme_live,
        "refresh_replay_followups",
        lambda *args, **kwargs: {"rows": [], "status": {"checked_at": "2026-09-10T01:00:00+00:00"}},
    )

    status = meme_live.run_meme_refresh_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        tweet_collector=lambda out_dir: {"ok": True},
    )

    assert status["ok"] is True
    assert published_during_build[0]["meme_rows"] == [{"symbol": "FAST"}]
    assert observed[0]["meme_rows"] == [{"symbol": "FAST"}]


def test_refresh_async_research_cannot_write_report_after_return(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    report_path.write_text(json.dumps({"meta": {}, "meme_rows": []}), encoding="utf-8")
    scheduled_report_paths = []
    live_report = {
        "meta": {"meme_live_generated_at": "2026-09-10T01:00:00+00:00"},
        "meme_rows": [{"symbol": "READABLE"}],
        "meme_watch_universe": [],
    }

    monkeypatch.setattr(meme_live, "refresh_gmgn_sources", lambda out_dir: ({"ok": True}, {"ok": True}))
    monkeypatch.setattr(meme_live, "build_meme_live_report", lambda *args, **kwargs: (live_report, {}))
    monkeypatch.setattr(meme_live, "run_paper_trading_cycle", lambda *args, **kwargs: {"mode": "read_only_paper"})
    monkeypatch.setattr(
        meme_live,
        "refresh_replay_followups",
        lambda *args, **kwargs: {"rows": [], "status": {"checked_at": "2026-09-10T01:00:00+00:00"}},
    )

    def schedule_research(report, *, out_dir, now, auxiliary_rows=(), report_path=None):
        scheduled_report_paths.append(report_path)
        return True

    monkeypatch.setattr(meme_live, "schedule_token_intelligence_research", schedule_research)

    status = meme_live.run_meme_refresh_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        tweet_collector=lambda out_dir: {"ok": True},
    )

    published = json.loads(report_path.read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert status["token_intelligence_research_scheduled"] is True
    assert published["meme_rows"] == [{"symbol": "READABLE"}]
    assert scheduled_report_paths == [None]


def test_fast_snapshot_classifies_new_gold_watch_candidate(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    token = "0xabc"
    row = {
        "quote_observed_at": meme_live.now_iso(),
        "quote_status": "fresh",
        "quote_fingerprint": "first-observation",
        "symbol": "FAST",
        "name": "Fast Token",
        "chain": "bsc",
        "chain_id": "bsc",
        "contract_address": token,
        "token_address": token,
        "mcap": 25_000,
        "market_cap": 25_000,
        "pair_age_hours": 0.5,
        "gold_dog_conviction_score": 78,
        "gold_dog_score": 76,
        "sources": ["gmgn_live_trending"],
        "source_labels": ["GMGN"],
        "source_count": 1,
    }
    (out_dir / "alpha-gold-watch-state.json").write_text(
        json.dumps({"candidates": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([row], []))
    monkeypatch.setattr(
        meme_live,
        "build_meme_potential_rows",
        lambda rows, limit, replay_calibration, **kwargs: [row],
    )
    monkeypatch.setattr(
        meme_live,
        "build_shadow_meme_rows",
        lambda rows, selected_rows, limit, replay_calibration, **kwargs: [],
    )
    snapshots = []

    meme_live.build_meme_live_report(
        {"meta": {}, "meme_rows": []},
        out_dir=out_dir,
        meme_source_limit=10,
        meme_top=10,
        meme_potential_top=10,
        meme_shadow_top=10,
        concurrency=1,
        gold_watch_confirmations=3,
        fast_publish=lambda report: snapshots.append(json.loads(json.dumps(report))),
    )

    assert snapshots[0]["meme_rows"][0]["watch_status"] == "seed_pool"
    assert snapshots[0]["meme_potential_rows"][0]["watch_ticket_stage"] == "seed"


@pytest.mark.parametrize("chain", ["bsc", "robinhood"])
def test_meme_live_refresh_replaces_only_meme_sections(tmp_path, monkeypatch, chain):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood")
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(
        json.dumps(
            {
                "meta": {"section_quality": {"alpha": "live", "dealer": "live"}},
                "alpha_rows": [{"symbol": "ALPHA"}],
                "dealer_rows": [{"symbol": "DEALER"}],
                "meme_rows": [{"symbol": "OLD"}],
            }
        ),
        encoding="utf-8",
    )

    refreshed_row = {
        "quote_observed_at": meme_live.now_iso(),
        "quote_status": "fresh",
        "quote_fingerprint": "first-observation",
        "symbol": "DOGE2",
        "name": "Doge Two",
        "chain": chain,
        "chain_id": chain,
        "contract_address": "0xabc",
        "token_address": "0xabc",
        "mcap": 120_000,
        "market_cap": 120_000,
        "price_usd": 0.001,
        "pair_age_hours": 1,
        "gold_dog_conviction_score": 90,
        "gold_dog_score": 88,
        "gold_dog_rationale": ["实时 Meme 强信号"],
        "source_labels": ["GMGN", "DS"],
        "sources": ["gmgn_live_trending", "profile_latest"],
        "source_count": 2,
        "url": "https://dexscreener.com/bsc/0xabc",
    }

    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([refreshed_row], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: [refreshed_row])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(meme_live, "optional_meme_source_status", lambda: {"gmgn": {"enabled": True}})
    monkeypatch.setattr(
        meme_live,
        "build_gold_backtest",
        lambda replay_history, gold_watch_state, *, now_iso: {
            "best_strategy": {"name": "first_discovery_probe", "label": "首次发现小仓"},
            "strategies": [{"name": "first_discovery_probe", "summary": {"count": 1}}],
        },
        raising=False,
    )

    status = meme_live.run_meme_refresh_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        gold_watch_confirmations=1,
    )

    assert status["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["alpha_rows"] == [{"symbol": "ALPHA"}]
    assert report["dealer_rows"] == [{"symbol": "DEALER"}]
    assert report["meme_rows"][0]["symbol"] == "DOGE2"
    assert isinstance(report["meme_rows"][0]["rank_score"], (int, float))
    assert report["meme_rows"][0]["rank_components"]
    assert report["meme_potential_rows"][0]["symbol"] == "DOGE2"
    assert report["meme_potential_rows"][0]["rank_score"] == report["meme_rows"][0]["rank_score"]
    assert report["gold_watch_alerts"][0]["symbol"] == "DOGE2"
    assert report["meta"]["section_quality"]["alpha"] == "live"
    assert report["meta"]["section_quality"]["dealer"] == "live"
    assert report["meta"]["section_quality"]["meme"] == "live"
    assert report["meta"]["live_refresh_mode"] == "meme_fast"
    assert report["meta"]["gold_backtest"]["best_strategy"]["name"] == "first_discovery_probe"
    assert report["meta"]["paper_trading"]["mode"] == "read_only_paper"
    assert report["meta"]["paper_trading"]["first_discovery"]["ok"] is True
    assert report["meta"]["paper_trading"]["first_discovery_shadow"]["ok"] is True
    assert report["meta"]["paper_trading"]["wallet_style"]["ok"] is True
    assert report["meta"]["paper_trading"]["wallet_style"]["entry_policy"] == "legacy_exit_only"
    assert (out_dir / "alpha-paper-trading-status.json").exists()
    assert report["meta"]["rating_v2"]["mode"] == "shadow_only_immutable_first_snapshot"
    assert report["meta"]["rating_v2"]["readiness"]["training_rows"] == 1
    assert report["meta"]["rating_v2"]["model"]["mode"] == "shadow_only_no_execution_effect"
    assert (out_dir / "alpha-rating-v2-dataset.json").exists()
    assert (out_dir / "alpha-rating-v2-model-status.json").exists()


def test_rating_v2_refresh_failure_keeps_previous_shadow_model(tmp_path, monkeypatch):
    previous = {
        "mode": "shadow_only_no_execution_effect",
        "generated_at": "2026-09-09T10:00:00+00:00",
        "ready_target_count": 1,
        "targets": {"2x": {"status": "shadow_model_ready", "model_path": "model.joblib"}},
    }
    (tmp_path / "alpha-rating-v2-model-status.json").write_text(
        json.dumps(previous), encoding="utf-8"
    )
    monkeypatch.setattr(
        meme_live,
        "refresh_rating_v2_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("incompatible dependency")),
    )

    result = meme_live.refresh_rating_v2_shadow(
        {"rows": [{"symbol": "DOG"}]},
        out_dir=tmp_path,
        now="2026-09-09T10:01:00+00:00",
    )

    assert result["ready_target_count"] == 1
    assert result["targets"] == previous["targets"]
    assert result["refresh_status"] == "degraded"
    assert result["refresh_error_type"] == "TypeError"
    saved = json.loads((tmp_path / "alpha-rating-v2-model-status.json").read_text(encoding="utf-8"))
    assert saved == result


def test_replay_followup_selects_dropped_recent_candidate_only():
    now = "2026-09-09T12:10:00+00:00"
    history = {
        "rows": {
            "bsc:0x1111111111111111111111111111111111111111": {
                "key": "bsc:0x1111111111111111111111111111111111111111",
                "chain": "bsc",
                "contract_address": "0x1111111111111111111111111111111111111111",
                "first_seen_at": "2026-09-09T12:00:00+00:00",
                "latest_seen_at": "2026-09-09T12:08:00+00:00",
                "first_price_usd": 0.001,
                "first_snapshot": {"mcap": 50_000},
                "trajectory": {"coverage_from_first": True},
            },
            "bsc:0x2222222222222222222222222222222222222222": {
                "key": "bsc:0x2222222222222222222222222222222222222222",
                "chain": "bsc",
                "contract_address": "0x2222222222222222222222222222222222222222",
                "first_seen_at": "2026-09-09T12:00:00+00:00",
                "latest_seen_at": "2026-09-09T12:08:00+00:00",
                "first_price_usd": 0.002,
                "first_snapshot": {"mcap": 60_000},
                "trajectory": {"coverage_from_first": True},
            },
            "bsc:0x3333333333333333333333333333333333333333": {
                "key": "bsc:0x3333333333333333333333333333333333333333",
                "chain": "bsc",
                "contract_address": "0x3333333333333333333333333333333333333333",
                "first_seen_at": "2026-09-07T10:00:00+00:00",
                "latest_seen_at": "2026-09-07T10:10:00+00:00",
                "first_price_usd": 0.003,
                "first_snapshot": {"mcap": 70_000},
                "trajectory": {"coverage_from_first": True},
            },
        }
    }
    current_rows = [{
        "chain": "bsc",
        "contract_address": "0x2222222222222222222222222222222222222222",
    }]

    selected = meme_live.select_replay_followup_targets(history, current_rows, now, limit=10)

    assert [row["key"] for row in selected] == [
        "bsc:0x1111111111111111111111111111111111111111"
    ]


def test_replay_followup_refreshes_dropped_candidate_with_fresh_quote():
    key = "bsc:0x1111111111111111111111111111111111111111"
    history = {
        "rows": {
            key: {
                "key": key,
                "symbol": "EARLY",
                "chain": "bsc",
                "contract_address": key.split(":", 1)[1],
                "first_seen_at": "2026-09-09T12:00:00+00:00",
                "latest_seen_at": "2026-09-09T12:08:00+00:00",
                "first_price_usd": 0.001,
                "latest_price_usd": 0.001,
                "recommendation_bucket": "shadow",
                "first_snapshot": {"mcap": 50_000, "pair_address": "0xpair"},
                "trajectory": {"coverage_from_first": True},
            }
        }
    }

    def fetcher(chain, addresses):
        assert chain == "bsc"
        assert addresses == [key.split(":", 1)[1]]
        return [{
            "chainId": "bsc",
            "pairAddress": "0xpair",
            "baseToken": {"address": key.split(":", 1)[1]},
            "priceUsd": "0.0015",
            "marketCap": 75_000,
            "fdv": 75_000,
            "liquidity": {"usd": 25_000},
            "volume": {"m5": 500, "h24": 80_000},
            "txns": {"m5": {"buys": 5, "sells": 2}},
            "priceChange": {"m5": 3, "h1": 20, "h24": 50},
            "pairCreatedAt": 1_757_419_200_000,
        }]

    result = meme_live.refresh_replay_followups(
        history,
        [],
        "2026-09-09T12:10:00+00:00",
        fetcher=fetcher,
        limit=10,
    )

    assert result["status"]["selected"] == 1
    assert result["status"]["fresh"] == 1
    assert result["rows"][0]["quote_status"] == "fresh"
    assert result["rows"][0]["price_usd"] == 0.0015


def test_meme_live_refresh_voice_alert_only_for_new_gold_ticket(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "meme_rows": []}), encoding="utf-8")

    refreshed_row = {
        "quote_observed_at": meme_live.now_iso(),
        "quote_status": "fresh",
        "quote_fingerprint": "first-observation",
        "symbol": "DOGE2",
        "name": "Doge Two",
        "chain": "bsc",
        "chain_id": "bsc",
        "contract_address": "0xabc",
        "token_address": "0xabc",
        "mcap": 120_000,
        "market_cap": 120_000,
        "pair_age_hours": 1,
        "gold_dog_conviction_score": 90,
        "gold_dog_score": 88,
        "gold_dog_rationale": ["实时 Meme 强信号"],
        "source_labels": ["GMGN", "DS"],
        "sources": ["gmgn_live_trending", "profile_latest"],
        "source_count": 2,
    }
    voice_calls = []

    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([refreshed_row], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: [refreshed_row])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(
        meme_live,
        "play_voice_alert",
        lambda message, repeat_count: voice_calls.append((message, repeat_count)) or {"ok": True, "repeat_count": repeat_count},
    )

    first = meme_live.run_meme_refresh_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        gold_watch_confirmations=1,
        voice_alert=True,
        voice_alert_text="金狗来了，注意观察",
        voice_alert_repeat=3,
    )
    second = meme_live.run_meme_refresh_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        gold_watch_confirmations=1,
        voice_alert=True,
        voice_alert_text="金狗来了，注意观察",
        voice_alert_repeat=3,
    )

    assert first["new_gold_watch_alert_count"] == 1
    assert first["voice_alert"]["ok"] is True
    assert first["voice_alert"]["triggered"] is True
    assert first["voice_alert"]["repeat_count"] == 3
    assert second["new_gold_watch_alert_count"] == 0
    assert second["voice_alert"]["enabled"] is True
    assert second["voice_alert"]["triggered"] is False
    assert second["voice_alert"]["reason"] == "waiting_for_first_confirmation"
    assert second["voice_alert"]["last_triggered_at"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["voice_alert"]["enabled"] is True
    assert report["meta"]["voice_alert"]["last_trigger_symbols"] == ["DOGE2"]
    assert voice_calls == [("D O G E 2，信号确认。", 3)]


@pytest.mark.parametrize(
    ("alert_type", "expected"),
    [
        ("discovered", "金狗来了，发现 新币。"),
        ("confirmed", "新币，信号确认。"),
        ("high_risk", "发现 新币，高风险，注意观察。"),
        ("deterioration", "新币，信号转弱。"),
        ("pullback", "新币，注意回撤。"),
        ("invalidated", "新币，信号失效。"),
    ],
)
def test_voice_alert_message_matches_event_type(alert_type, expected):
    assert meme_live.voice_alert_message([{"alert_type": alert_type}], "备用语音") == (expected, alert_type)


def test_voice_alert_message_prioritizes_new_discovery_over_invalidation():
    alerts = [
        {"alert_type": "invalidated", "symbol": "OLD"},
        {"alert_type": "early", "symbol": "NEW"},
    ]

    assert meme_live.voice_alert_message(alerts, "备用语音") == ("金狗来了，发现 N E W。", "early")


def test_meme_live_refresh_voice_alert_test_is_reported_without_new_ticket(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "meme_rows": []}), encoding="utf-8")
    voice_calls = []

    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(
        meme_live,
        "play_voice_alert",
        lambda message, repeat_count: voice_calls.append((message, repeat_count)) or {"ok": True, "repeat_count": repeat_count},
    )

    status = meme_live.run_meme_refresh_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        voice_alert_test=True,
        voice_alert_text="金狗来了，注意观察",
        voice_alert_repeat=3,
    )

    assert status["new_gold_watch_alert_count"] == 0
    assert status["voice_alert"]["triggered"] is True
    assert status["voice_alert"]["reason"] == "voice_test"
    assert status["voice_alert"]["last_tested_at"]
    assert not status["voice_alert"]["last_triggered_at"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["voice_alert"]["reason"] == "voice_test"
    assert voice_calls == [("金狗来了，注意观察。", 3)]


def test_meme_live_refresh_falls_back_to_previous_meme_rows(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(
        json.dumps(
            {
                "meta": {"section_quality": {"alpha": "live"}},
                "meme_rows": [{"symbol": "OLD", "mcap": 10_000, "pair_age_hours": 1}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([], ["source down"]))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])

    status = meme_live.run_meme_refresh_once(out_dir=out_dir, report_path=report_path, status_path=status_path)

    assert status["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meme_rows"][0]["symbol"] == "OLD"
    assert report["meta"]["used_previous_meme"] is True
    assert report["meta"]["section_quality"]["meme"] == "cached"


def test_meme_live_refresh_applies_tweet_narrative_seeds(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "alpha_rows": []}), encoding="utf-8")
    (out_dir / "alpha-narrative-tweets-manual.json").write_text(
        json.dumps(
            [
                {
                    "account": "cz_binance",
                    "text": "Happy orange cat builders. 4",
                    "created_at": "2026-08-20T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    meme_row = {
        "symbol": "OCAT",
        "name": "Orange Cat",
        "chain": "bsc",
        "contract_address": "0xcat",
        "score": 70,
        "mcap": 80_000,
        "pair_age_hours": 1,
    }

    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([meme_row], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: rows[:1])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])

    status = meme_live.run_meme_refresh_once(out_dir=out_dir, report_path=report_path, status_path=status_path)

    assert status["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meme_rows"][0]["tweet_narrative_score"] > 0
    assert report["meme_rows"][0]["sentiment_trigger"]["keyword"] == "cat"
    assert report["meta"]["tweet_narrative"]["seed_count"] >= 1


def test_meme_live_refresh_does_not_reuse_generated_tweet_output(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    (out_dir / "alpha-narrative-tweets.json").write_text(
        json.dumps(
            {
                "ok": True,
                "tweets": [
                    {
                        "account": "cz_binance",
                        "text": "Happy orange cat builders. 4",
                        "created_at": "2026-08-20T12:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(meme_live.alpha_tweet_sources, "env_list", lambda name: [])
    monkeypatch.setattr(meme_live.alpha_tweet_sources, "env_command_list", lambda name: [])

    status = meme_live.collect_tweet_sources_for_out_dir(out_dir)
    payload = json.loads((out_dir / "alpha-narrative-tweets.json").read_text(encoding="utf-8"))

    assert status["ok"] is True
    assert status["tweet_count"] == 0
    assert payload["tweets"] == []


def test_meme_live_refresh_passes_browser_commands_to_tweet_collector(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    calls = {}

    def fake_collect_tweets(*, local_files, source_urls, browser_commands):
        calls["local_files"] = local_files
        calls["source_urls"] = source_urls
        calls["browser_commands"] = browser_commands
        return {"ok": True, "updated_at": "now", "tweet_count": 0, "tweets": [], "errors": []}

    monkeypatch.setattr(meme_live.alpha_tweet_sources, "env_list", lambda name: [])
    monkeypatch.setattr(meme_live.alpha_tweet_sources, "env_command_list", lambda name: ["bb-browser fetch cz"])
    monkeypatch.setattr(meme_live.alpha_tweet_sources, "collect_tweets", fake_collect_tweets)

    status = meme_live.collect_tweet_sources_for_out_dir(out_dir)

    assert status["ok"] is True
    assert calls["browser_commands"] == ["bb-browser fetch cz"]


def test_meme_live_refresh_can_collect_tweet_sources_before_scoring(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "alpha_rows": []}), encoding="utf-8")
    meme_row = {
        "symbol": "OCAT",
        "name": "Orange Cat",
        "chain": "bsc",
        "contract_address": "0xcat",
        "score": 70,
        "mcap": 80_000,
        "pair_age_hours": 1,
    }

    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([meme_row], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: rows[:1])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])

    def fake_collect(out_dir):
        (out_dir / "alpha-narrative-tweets.json").write_text(
            json.dumps([{"account": "binance", "text": "Orange cat happy", "created_at": "2026-08-20T12:00:00Z"}]),
            encoding="utf-8",
        )
        return {"ok": True, "tweet_count": 1, "errors": []}

    status = meme_live.run_meme_refresh_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        tweet_collector=fake_collect,
    )

    assert status["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meme_rows"][0]["tweet_narrative_score"] > 0
    assert report["meta"]["tweet_source_status"]["tweet_count"] == 1


def test_meme_live_refresh_collects_noxa_launchpad_before_scoring(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood")
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "alpha_rows": []}), encoding="utf-8")
    calls = []

    def fake_refresh_noxa(refresh_out_dir):
        calls.append(refresh_out_dir)
        inbox = refresh_out_dir / "meme-source-inbox" / "noxa-launches.json"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            json.dumps(
                {
                    "source": "noxa_launchpad",
                    "chain": "robinhood",
                    "data": [
                        {
                            "chain": "robinhood",
                            "address": "0x44812a88a643b2a55bc28bf618e180728f4d3fba",
                            "symbol": "YES",
                            "name": "Abliteration AI",
                            "marketCap": 168_428,
                            "liquidity": 35_938,
                            "holders": 311,
                            "priceChange1h": 204,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {"ok": True, "row_count": 1}

    noxa_row = {
        "symbol": "YES",
        "name": "Abliteration AI",
        "chain": "robinhood",
        "chain_id": "robinhood",
        "contract_address": "0x44812a88a643b2a55bc28bf618e180728f4d3fba",
        "token_address": "0x44812a88a643b2a55bc28bf618e180728f4d3fba",
        "mcap": 168_428,
        "market_cap": 168_428,
        "liquidity": 35_938,
        "pair_age_hours": 1,
        "score": 88,
        "source_labels": ["Noxa", "DS"],
        "sources": ["noxa_launchpad"],
        "source_count": 1,
    }

    monkeypatch.setattr(meme_live, "refresh_noxa_launchpad", fake_refresh_noxa)
    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([noxa_row], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: rows[:1])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(meme_live, "optional_meme_source_status", lambda: {"noxa_launchpad": {"enabled": True, "file_count": 1}})

    status = meme_live.run_meme_refresh_once(out_dir=out_dir, report_path=report_path, status_path=status_path)

    assert status["ok"] is True
    assert calls == [out_dir]
    assert status["noxa_launchpad_status"]["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["noxa_launchpad_status"]["row_count"] == 1
    assert report["meme_potential_rows"][0]["symbol"] == "YES"


def test_meme_live_refresh_collects_proficy_trending_before_scoring(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood")
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "alpha_rows": []}), encoding="utf-8")
    calls = []

    def fake_refresh_proficy(refresh_out_dir):
        calls.append(refresh_out_dir)
        inbox = refresh_out_dir / "meme-source-inbox" / "proficy-trending.json"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            json.dumps(
                {
                    "source": "proficy_trending",
                    "data": [
                        {
                            "chain": "robinhood",
                            "address": "0x39dbed3a2bd333467115de45665cc57f813c4571",
                            "symbol": "PONS",
                            "name": "Pons",
                            "marketCap": 654_500_000,
                            "liquidity": 7_730_000,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {"ok": True, "row_count": 1}

    proficy_row = {
        "symbol": "PONS",
        "name": "Pons",
        "chain": "robinhood",
        "chain_id": "robinhood",
        "contract_address": "0x39dbed3a2bd333467115de45665cc57f813c4571",
        "token_address": "0x39dbed3a2bd333467115de45665cc57f813c4571",
        "mcap": 654_500_000,
        "market_cap": 654_500_000,
        "liquidity": 7_730_000,
        "score": 72,
        "source_labels": ["Proficy", "DS"],
        "sources": ["proficy_trending"],
        "source_count": 1,
    }

    monkeypatch.setattr(meme_live, "refresh_proficy_trending", fake_refresh_proficy)
    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([proficy_row], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: rows[:1])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(meme_live, "optional_meme_source_status", lambda: {"proficy_trending": {"enabled": True, "file_count": 1}})

    status = meme_live.run_meme_refresh_once(out_dir=out_dir, report_path=report_path, status_path=status_path)

    assert status["ok"] is True
    assert calls == [out_dir]
    assert status["proficy_trending_status"]["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["proficy_trending_status"]["row_count"] == 1
    assert report["meme_potential_rows"][0]["symbol"] == "PONS"


def test_meme_live_refresh_collects_985_monitor_before_scoring(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood")
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "alpha_rows": []}), encoding="utf-8")
    calls = []

    def fake_refresh_985(refresh_out_dir):
        calls.append(refresh_out_dir)
        inbox = refresh_out_dir / "meme-source-inbox" / "985-monitor.json"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            json.dumps(
                {
                    "source": "985_monitor",
                    "data": [
                        {
                            "chain": "robinhood",
                            "address": "0x9850000000000000000000000000000000000000",
                            "symbol": "NINE",
                            "name": "985 Pick",
                            "marketCap": 76_000,
                            "liquidity": 22_000,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {"ok": True, "row_count": 1}

    row_985 = {
        "symbol": "NINE",
        "name": "985 Pick",
        "chain": "robinhood",
        "chain_id": "robinhood",
        "contract_address": "0x9850000000000000000000000000000000000000",
        "token_address": "0x9850000000000000000000000000000000000000",
        "mcap": 76_000,
        "market_cap": 76_000,
        "liquidity": 22_000,
        "score": 74,
        "source_labels": ["985", "DS"],
        "sources": ["985_monitor"],
        "source_count": 1,
    }

    monkeypatch.setattr(meme_live, "refresh_985_monitor", fake_refresh_985)
    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([row_985], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: rows[:1])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(meme_live, "optional_meme_source_status", lambda: {"985_monitor": {"enabled": True, "file_count": 1}})

    status = meme_live.run_meme_refresh_once(out_dir=out_dir, report_path=report_path, status_path=status_path)

    assert status["ok"] is True
    assert calls == [out_dir]
    assert status["monitor985_status"]["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["monitor985_status"]["row_count"] == 1
    assert report["meme_potential_rows"][0]["symbol"] == "NINE"


def test_meme_live_refresh_collects_wind_monitor_before_scoring(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood")
    out_dir = tmp_path / "outputs"
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "alpha-meme-live-refresh-status.json"
    out_dir.mkdir()
    report_path.write_text(json.dumps({"meta": {}, "alpha_rows": []}), encoding="utf-8")
    calls = []

    def fake_refresh_wind(refresh_out_dir):
        calls.append(refresh_out_dir)
        inbox = refresh_out_dir / "meme-source-inbox" / "wind-monitor.json"
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_text(
            json.dumps(
                {
                    "source": "wind_monitor",
                    "data": [
                        {
                            "chain": "robinhood",
                            "address": "0x7777777777777777777777777777777777777777",
                            "symbol": "WIND",
                            "name": "Wind Pick",
                            "marketCap": 88_000,
                            "liquidity": 21_000,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {"ok": True, "row_count": 1}

    wind_row = {
        "symbol": "WIND",
        "name": "Wind Pick",
        "chain": "robinhood",
        "chain_id": "robinhood",
        "contract_address": "0x7777777777777777777777777777777777777777",
        "token_address": "0x7777777777777777777777777777777777777777",
        "mcap": 88_000,
        "market_cap": 88_000,
        "liquidity": 21_000,
        "score": 72,
        "source_labels": ["听风", "DS"],
        "sources": ["wind_monitor"],
        "source_count": 1,
    }

    monkeypatch.setattr(meme_live, "refresh_wind_monitor", fake_refresh_wind)
    monkeypatch.setattr(meme_live, "load_meme_candidates", lambda limit, concurrency: ([wind_row], []))
    monkeypatch.setattr(meme_live, "build_meme_potential_rows", lambda rows, limit, replay_calibration, **kwargs: rows[:1])
    monkeypatch.setattr(meme_live, "build_shadow_meme_rows", lambda rows, selected_rows, limit, replay_calibration, **kwargs: [])
    monkeypatch.setattr(meme_live, "optional_meme_source_status", lambda: {"wind_monitor": {"enabled": True, "file_count": 1}})

    status = meme_live.run_meme_refresh_once(out_dir=out_dir, report_path=report_path, status_path=status_path)

    assert status["ok"] is True
    assert calls == [out_dir]
    assert status["wind_monitor_status"]["ok"] is True
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["meta"]["wind_monitor_status"]["row_count"] == 1
    assert report["meme_potential_rows"][0]["symbol"] == "WIND"
