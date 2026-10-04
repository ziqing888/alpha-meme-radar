import json
import os
import threading
import time
from pathlib import Path

import alpha_meme_fast_discovery as fast
from alpha_arc_public_chain_poll import normalize_discoveries


def test_arc_adapter_inbox_loads_as_accepted_onchain_discovery_and_projects(monkeypatch, tmp_path: Path):
    observed_at = "2026-09-16T03:00:00+00:00"
    address = "0x" + "1" * 40
    inbox_dir = tmp_path / "meme-source-inbox"
    inbox_dir.mkdir()
    adapter_rows = normalize_discoveries(
        [{
            "contract_address": address,
            "source": "arc_rpc",
            "event_kind": "mint",
            "transaction_hash": "0xadapter",
            "log_index": "0x1",
            "block_number": 101,
            "name": "Arc Adapter Token",
            "symbol": "AAT",
            "decimals": 18,
            "total_supply": 1_000_000,
        }],
        [],
        observed_at,
    )
    (inbox_dir / "arc-onchain.json").write_text(
        json.dumps({
            "source": "arc_onchain",
            "observed_at": observed_at,
            "count": len(adapter_rows),
            "data": adapter_rows,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox_dir))
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood,arc")
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)

    sources, errors = fast.load_local_inbox_sources(10)
    events = [
        event
        for source in sources.values()
        for event in source.get("source_events", [])
    ]
    snapshot = fast.update_monitor_state(
        None,
        events=events,
        candidates=[],
        rejections=[],
        source_health={"arc_onchain": {"status": "ok", "observed_at": observed_at}},
        observed_at=observed_at,
    )

    assert errors == []
    assert len(events) == 1
    assert events[0]["provider_family"] == "onchain"
    assert events[0]["evidence_role"] == "discovery"
    assert events[0]["signal_lane"] == "new_launch"
    assert [token["id"] for token in snapshot["tokens"]] == [f"arc:{address}"]


def fast_monitor_event(event_id: str, observed_at: str) -> dict:
    return {
        "schema_version": 1,
        "chain": "bsc",
        "contract_address": "0x" + "a" * 40,
        "symbol": "KEEP",
        "name": "Keep State",
        "event_type": "new_launch",
        "event_at": observed_at,
        "observed_at": observed_at,
        "provider_family": "gmgn",
        "provider_feed": "gmgn_trenches",
        "evidence_role": "discovery",
        "signal_lane": "new_launch",
        "provider_event_id": event_id,
        "source_url": None,
        "raw_fingerprint": event_id,
        "upstream_provider": "gmgn",
        "event_time_basis": "observed_at",
        "counts_for_resonance": True,
        "event_id": event_id,
    }


def fast_monitor_candidate(observed_at: str) -> dict:
    return {
        "chain": "bsc",
        "contract_address": "0x" + "a" * 40,
        "symbol": "KEEP",
        "name": "Keep State",
        "observed_at": observed_at,
        "quote_source": "gmgn",
        "market_cap_usd": 40_000,
        "liquidity_usd": 12_000,
        "holders": 80,
    }


def test_fast_monitor_preserves_identity_across_a_temporary_stale_gap(tmp_path: Path):
    first_at = "2026-09-13T12:00:00+00:00"
    stale_at = "2026-09-13T12:03:00+00:00"
    resumed_at = "2026-09-13T12:03:10+00:00"

    fast.publish_fast_monitor_snapshot(
        out_dir=tmp_path,
        batch={
            "events": [fast_monitor_event("first", first_at)],
            "candidates": [fast_monitor_candidate(first_at)],
            "rejections": [],
        },
        observed_at=first_at,
        previous_snapshot=None,
    )
    stale_ui = fast.publish_fast_monitor_snapshot(
        out_dir=tmp_path,
        batch={"events": [], "candidates": [], "rejections": []},
        observed_at=stale_at,
        previous_snapshot=None,
    )
    resumed_ui = fast.publish_fast_monitor_snapshot(
        out_dir=tmp_path,
        batch={
            "events": [fast_monitor_event("resumed", resumed_at)],
            "candidates": [fast_monitor_candidate(resumed_at)],
            "rejections": [],
        },
        observed_at=resumed_at,
        previous_snapshot=None,
    )

    assert stale_ui["tokens"] == []
    assert (tmp_path / "alpha-meme-monitor-v3-fast-state.json").exists()
    assert resumed_ui["tokens"][0]["identity"]["first_seen_at"] == first_at
    assert [event["event_id"] for event in resumed_ui["tokens"][0]["events"]] == [
        "first",
        "resumed",
    ]


def test_fast_source_environment_uses_provider_market_data_without_dex_burst(monkeypatch):
    monkeypatch.setenv("MEME_SKIP_DEX_ENRICH", "0")
    monkeypatch.setenv("MEME_BATCH_PAIRS", "0")
    monkeypatch.setenv("MEME_PAIR_TOTAL_TIMEOUT_SECONDS", "99")

    fast.configure_fast_source_environment()

    assert os.environ["MEME_SKIP_DEX_ENRICH"] == "1"
    assert os.environ["MEME_BATCH_PAIRS"] == "1"
    assert os.environ["MEME_PAIR_TOTAL_TIMEOUT_SECONDS"] == "6"


def test_fast_local_source_budget_prefers_multi_source_and_caps_quote_work(monkeypatch):
    sources = {
        f"bsc:0x{index:040x}": {
            "chainId": "bsc",
            "tokenAddress": f"0x{index:040x}",
            "sources": ["gmgn_trenches"],
            "profile": {"rank_score": index},
        }
        for index in range(40)
    }
    sources.update({
        f"robinhood:0x{index + 100:040x}": {
            "chainId": "robinhood",
            "tokenAddress": f"0x{index + 100:040x}",
            "sources": ["gmgn_trenches"],
            "profile": {"rank_score": index},
        }
        for index in range(20)
    })
    important = {
        "chainId": "robinhood",
        "tokenAddress": "0x" + "f" * 40,
        "sources": ["noxa_launchpad", "wind_monitor"],
        "profile": {"rank_score": 1},
    }
    sources["robinhood:important"] = important
    monkeypatch.setattr(fast, "load_local_inbox_sources", lambda limit: (sources, []))

    selected, errors = fast.load_fast_local_sources(80, total_limit=30)

    assert errors == []
    assert len(selected) == 30
    assert important in selected.values()
    assert sum(1 for row in selected.values() if row["chainId"] == "bsc") <= 18


def test_fast_local_source_budget_reserves_recent_single_source_discoveries(monkeypatch):
    sources = {
        f"robinhood:old-{index}": {
            "chainId": "robinhood",
            "tokenAddress": f"0x{index:040x}",
            "sources": ["gmgn_trending", "wind_monitor"],
            "profile": {
                "rank_score": 90 - index,
                "observed_at": "2026-09-11T10:00:00+08:00",
            },
        }
        for index in range(35)
    }
    fresh = {
        "chainId": "robinhood",
        "tokenAddress": "0x" + "f" * 40,
        "sources": ["gmgn_skills_hot_searches"],
        "profile": {
            "rank_score": 20,
            "observed_at": "2026-09-11T10:10:00+08:00",
        },
    }
    sources["robinhood:fresh"] = fresh
    monkeypatch.setattr(fast, "load_local_inbox_sources", lambda limit: (sources, []))

    selected, errors = fast.load_fast_local_sources(
        80,
        total_limit=20,
        per_chain_limit=20,
    )

    assert errors == []
    assert fresh in selected.values()
    assert len(selected) == 20


def test_fast_local_source_budget_reserves_each_provider_family(monkeypatch):
    sources = {
        f"robinhood:proficy-{index}": {
            "chainId": "robinhood",
            "tokenAddress": f"0x{index:040x}",
            "sources": ["proficy_trending"],
            "profile": {"rank_score": 100 - index, "observed_at": "2026-09-12T10:00:00+00:00"},
        }
        for index in range(30)
    }
    for offset, source in enumerate(("985_monitor", "wind_monitor", "gmgn_skills_hot_searches"), start=100):
        sources[f"robinhood:{source}"] = {
            "chainId": "robinhood",
            "tokenAddress": f"0x{offset:040x}",
            "sources": [source],
            "profile": {"rank_score": 1, "observed_at": "2026-09-12T09:59:00+00:00"},
        }
    monkeypatch.setattr(fast, "load_local_inbox_sources", lambda limit: (sources, []))

    selected, errors = fast.load_fast_local_sources(80, total_limit=10, per_chain_limit=10)

    assert errors == []
    selected_sources = {source for row in selected.values() for source in row["sources"]}
    assert {"proficy_trending", "985_monitor", "wind_monitor", "gmgn_skills_hot_searches"}.issubset(selected_sources)


def test_fast_local_source_provider_reserves_do_not_consume_chain_budget_twice(monkeypatch):
    sources = {}
    for chain_index, chain in enumerate(("bsc", "robinhood")):
        for index in range(10):
            sources[f"{chain}:token-{index}"] = {
                "chainId": chain,
                "tokenAddress": f"0x{chain_index * 100 + index:040x}",
                "sources": ["985_monitor" if index == 0 else "proficy_trending"],
                "profile": {"rank_score": 100 - index, "observed_at": f"2026-09-12T10:00:{index:02d}+00:00"},
            }
    monkeypatch.setattr(fast, "load_local_inbox_sources", lambda limit: (sources, []))

    selected, _ = fast.load_fast_local_sources(80, total_limit=10, per_chain_limit=5)

    assert len(selected) == 10
    assert sum(row["chainId"] == "bsc" for row in selected.values()) == 5
    assert sum(row["chainId"] == "robinhood" for row in selected.values()) == 5


def test_fast_source_refresh_includes_throttled_gmgn_hot_search(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(fast, "refresh_bsc_public_pairs", lambda out_dir: calls.append("bsc") or {"ok": True})
    monkeypatch.setattr(fast, "refresh_noxa_launchpad", lambda out_dir: calls.append("noxa") or {"ok": True})
    monkeypatch.setattr(fast, "refresh_proficy_trending", lambda out_dir: calls.append("proficy") or {"ok": True})
    monkeypatch.setattr(fast, "refresh_985_core", lambda out_dir: calls.append("985") or {"ok": True})
    monkeypatch.setattr(fast, "refresh_wind_monitor", lambda out_dir: calls.append("wind") or {"ok": True})
    monkeypatch.setattr(fast, "refresh_gmgn_hot_search", lambda out_dir: calls.append("gmgn") or {"ok": True})
    monkeypatch.setattr(fast, "refresh_okx_market", lambda out_dir: calls.append("okx") or {"ok": True})
    monkeypatch.setattr(
        fast,
        "refresh_arc_public_chain",
        lambda out_dir: calls.append("arc") or {"source": "arc_onchain", "ok": False, "error": "rpc_unavailable"},
        raising=False,
    )
    statuses = fast.refresh_fast_sources(tmp_path)

    assert sorted(calls) == ["985", "arc", "bsc", "gmgn", "noxa", "okx", "proficy", "wind"]
    assert len(statuses) == 8
    assert next(status for status in statuses if status.get("source") == "arc_onchain")["ok"] is False


def test_refresh_arc_public_chain_returns_the_single_adapter_status(monkeypatch, tmp_path):
    adapter_status = {"status": "ok", "ok": True, "row_count": 2}
    calls = []

    assert callable(getattr(fast, "refresh_arc_public_chain", None))
    monkeypatch.setattr(
        fast,
        "run_arc_public_chain_once",
        lambda *, out_dir, rpc_urls, arcscan_base_url: calls.append(
            (out_dir, rpc_urls, arcscan_base_url)
        ) or adapter_status,
        raising=False,
    )

    status = fast.refresh_arc_public_chain(tmp_path)

    assert status == {**adapter_status, "source": "arc_onchain"}
    assert calls == [(tmp_path, list(fast.DEFAULT_ARC_RPC_URLS), fast.DEFAULT_ARCSCAN_BASE_URL)]


def test_arc_onchain_job_has_a_two_second_cadence():
    assert fast.FAST_SOURCE_MIN_INTERVAL_SECONDS["arc_onchain"] == 2


def test_fast_source_refresh_does_not_double_throttle_gmgn_backoff(monkeypatch, tmp_path):
    gmgn_calls = []
    monkeypatch.setattr(fast, "refresh_bsc_public_pairs", lambda out_dir: {"source": "onchain", "ok": True})
    monkeypatch.setattr(fast, "refresh_noxa_launchpad", lambda out_dir: {"source": "noxa_launchpad", "ok": True})
    monkeypatch.setattr(fast, "refresh_proficy_trending", lambda out_dir: {"source": "proficy_trending", "ok": True})
    monkeypatch.setattr(fast, "refresh_985_core", lambda out_dir: {"source": "985_monitor_fast", "ok": True})
    monkeypatch.setattr(fast, "refresh_wind_monitor", lambda out_dir: {"source": "wind_monitor", "ok": True})
    monkeypatch.setattr(fast, "refresh_okx_market", lambda out_dir: {"source": "okx_market_fast", "ok": True})
    monkeypatch.setattr(fast, "refresh_arc_public_chain", lambda out_dir: {"source": "arc_onchain", "ok": True})

    def refresh_gmgn(out_dir):
        gmgn_calls.append(out_dir)
        if len(gmgn_calls) == 1:
            return {"source": "gmgn_skills_fast_hot_search", "ok": False, "reason": "rate_limit_backoff"}
        return {"source": "gmgn_skills_fast_hot_search", "ok": True, "row_count": 100}

    monkeypatch.setattr(fast, "refresh_gmgn_hot_search", refresh_gmgn)

    fast.refresh_fast_sources(tmp_path)
    statuses = fast.refresh_fast_sources(tmp_path)

    assert len(gmgn_calls) == 2
    assert next(status for status in statuses if status.get("source", "").startswith("gmgn_"))["ok"] is True


def test_fast_source_cadence_reuses_recent_success(tmp_path):
    calls = []

    def refresh(out_dir):
        calls.append(out_dir)
        return {"source": "wind_monitor", "ok": True, "row_count": 4}

    first = fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="wind",
        refresher=refresh,
        min_interval_seconds=30,
        now_monotonic=100,
    )
    cached = fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="wind",
        refresher=refresh,
        min_interval_seconds=30,
        now_monotonic=110,
    )
    refreshed = fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="wind",
        refresher=refresh,
        min_interval_seconds=30,
        now_monotonic=131,
    )

    assert len(calls) == 2
    assert first["ok"] is True
    assert cached["skipped"] is True
    assert cached["reason"] == "min_interval"
    assert cached["row_count"] == 4
    assert refreshed.get("skipped") is None


def test_fast_source_cadence_backs_off_after_rate_limit(tmp_path):
    calls = []

    def refresh(out_dir):
        calls.append(out_dir)
        return {"source": "wind_monitor", "ok": True, "row_count": 3, "feed_error": "http_429"}

    fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="wind-rate-limit",
        refresher=refresh,
        min_interval_seconds=30,
        rate_limit_backoff_seconds=120,
        now_monotonic=100,
    )
    cached = fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="wind-rate-limit",
        refresher=refresh,
        min_interval_seconds=30,
        rate_limit_backoff_seconds=120,
        now_monotonic=140,
    )
    refreshed = fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="wind-rate-limit",
        refresher=refresh,
        min_interval_seconds=30,
        rate_limit_backoff_seconds=120,
        now_monotonic=221,
    )

    assert len(calls) == 2
    assert cached["ok"] is True
    assert cached["skipped"] is True
    assert cached["reason"] == "rate_limit_backoff"
    assert refreshed.get("skipped") is None


def test_985_fast_refresh_uses_five_second_default_timeout(monkeypatch, tmp_path):
    observed = {}

    def collect_rows(base_url, limit, timeout):
        observed["timeout"] = timeout
        return [], {}

    monkeypatch.delenv("MONITOR985_FAST_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(fast.alpha_985_monitor_export, "collect_rows", collect_rows)
    monkeypatch.setattr(fast.alpha_985_monitor_export, "write_payload", lambda *args, **kwargs: None)
    monkeypatch.setattr(fast.alpha_985_monitor_export, "write_json", lambda *args, **kwargs: None)

    status = fast.refresh_985_core(tmp_path)

    assert status["ok"] is True
    assert observed["timeout"] == 5


def test_okx_market_refresh_persists_read_only_rows(monkeypatch, tmp_path):
    rows = [{"chain": "bsc", "address": "0x" + "1" * 40, "okx_kind": "SIGNAL"}]
    persisted = []
    monkeypatch.setattr(fast.alpha_okx_market, "status", lambda: {"configured": True})
    monkeypatch.setattr(fast.alpha_okx_market, "fetch", lambda chains, limit: (rows, []))
    monkeypatch.setattr(
        fast.alpha_okx_market,
        "persist_snapshot",
        lambda out_dir, fetched, errors: persisted.append((out_dir, fetched, errors)) or {"ok": True},
    )

    status = fast.refresh_okx_market(tmp_path)

    assert status["source"] == "okx_market_fast"
    assert status["ok"] is True
    assert status["row_count"] == 1
    assert persisted == [(tmp_path / "meme-source-inbox", rows, [])]


def test_okx_market_refresh_uses_recent_success_without_another_api_burst(monkeypatch, tmp_path):
    status_path = tmp_path / "okx-market-fast-status.json"
    status_path.write_text(json.dumps({
        "source": "okx_market_fast",
        "ok": True,
        "checked_at": fast.now_iso(),
        "row_count": 160,
    }), encoding="utf-8")
    monkeypatch.setattr(
        fast.alpha_okx_market,
        "fetch",
        lambda chains, limit: (_ for _ in ()).throw(AssertionError("unexpected OKX request")),
    )

    status = fast.refresh_okx_market(tmp_path)

    assert status["ok"] is True
    assert status["skipped"] is True
    assert status["reason"] == "min_interval"
    assert status["row_count"] == 160


def test_okx_market_refresh_keeps_request_failure_attributed_to_okx(monkeypatch, tmp_path):
    monkeypatch.setattr(fast.alpha_okx_market, "status", lambda: {"configured": True})
    monkeypatch.setattr(
        fast.alpha_okx_market,
        "fetch",
        lambda chains, limit: (_ for _ in ()).throw(RuntimeError("http_429")),
    )

    status = fast.refresh_okx_market(tmp_path)

    assert status["source"] == "okx_market_fast"
    assert status["ok"] is False
    assert status["row_count"] == 0
    assert status["error"] == "RuntimeError: http_429"


def test_fast_source_health_keeps_provider_refresh_failures_explicit():
    health = fast.fast_source_health([
        {"source": "proficy_trending", "ok": False, "checked_at": "2026-09-12T10:00:00+00:00", "error": "HTTP 403"},
        {"source": "985_monitor_fast", "ok": True, "checked_at": "2026-09-12T10:00:00+00:00", "row_count": 60},
        {"mode": "public_rpc_poll", "ok": True, "finished_at": "2026-09-12T10:00:00+00:00", "rows": 2},
    ])

    assert health["proficy"]["status"] == "error"
    assert health["985"]["status"] == "ok"
    assert health["onchain"]["row_count"] == 2


def test_fast_source_health_keeps_arc_status_separate_when_degraded():
    health = fast.fast_source_health([{
        "source": "arc_onchain",
        "ok": False,
        "observed_at": "2026-09-12T10:00:00+00:00",
        "error": "rpc_unavailable",
    }])

    assert health["arc_onchain"]["status"] == "error"
    assert health["arc_onchain"]["error"] == "rpc_unavailable"
    assert "onchain" not in health


def test_fast_source_health_treats_throttled_cached_feed_as_available():
    health = fast.fast_source_health([{
        "source": "gmgn_skills_fast_hot_search",
        "ok": False,
        "skipped": True,
        "reason": "min_interval",
        "row_count": 100,
        "last_request_at": "2026-09-12T10:00:00+00:00",
    }])

    assert health["gmgn"]["status"] == "ok"
    assert health["gmgn"]["row_count"] == 100


def test_fast_source_health_reports_rate_limit_backoff_as_degraded():
    health = fast.fast_source_health([{
        "source": "gmgn_skills_fast_hot_search",
        "ok": False,
        "skipped": True,
        "reason": "rate_limit_backoff",
        "row_count": 100,
        "last_request_at": "2026-09-12T10:00:00+00:00",
    }])

    assert health["gmgn"]["status"] == "degraded"
    assert health["gmgn"]["row_count"] == 100
    assert health["gmgn"]["error"] == "rate_limit_backoff"
    assert "last_success" not in health["gmgn"]


def test_background_source_refresh_does_not_block_monitor_projection(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def slow_refresh(out_dir):
        started.set()
        release.wait(2)
        return [{"source": "wind_monitor", "ok": True, "row_count": 1}]

    before = time.monotonic()
    status = fast.schedule_fast_source_refresh(
        tmp_path,
        refresher=slow_refresh,
        min_interval_seconds=0,
    )
    elapsed = time.monotonic() - before

    assert started.wait(0.5)
    assert elapsed < 0.2
    assert status[-1]["source"] == "refresh_scheduler"
    assert status[-1]["running"] is True
    release.set()
    assert fast.wait_for_fast_source_refresh(tmp_path, timeout_seconds=1)


def test_monitor_enrichment_targets_only_fresh_resonance_with_resolvable_market_gaps():
    fresh_address = "0x" + "1" * 40
    expired_address = "0x" + "2" * 40
    single_address = "0x" + "3" * 40
    rows = [
        {"chain": "bsc", "contract_address": fresh_address, "symbol": "FRESH"},
        {"chain": "bsc", "contract_address": expired_address, "symbol": "OLD"},
        {"chain": "bsc", "contract_address": single_address, "symbol": "SINGLE"},
    ]
    monitor = {
        "tokens": [
            {
                "id": f"bsc:{fresh_address}",
                "active_states": ["new"],
                "resonance": {
                    "subtype": "cross_provider",
                    "provider_families": ["985", "wind"],
                    "confirmation_gate": {"failures": ["market_cap_unknown", "liquidity_unknown"]},
                },
                "ranking_axes": {"timing": {"latest_event_age_seconds": 8}, "market_behavior": {"disposition": "unknown"}},
                "risk": {"hard_blocked": False},
            },
            {
                "id": f"bsc:{expired_address}",
                "active_states": ["new"],
                "resonance": {
                    "subtype": "cross_provider",
                    "provider_families": ["gmgn", "985"],
                    "confirmation_gate": {"failures": ["discovery_window_expired"]},
                },
                "ranking_axes": {"timing": {"latest_event_age_seconds": 5}, "market_behavior": {"disposition": "unknown"}},
                "risk": {"hard_blocked": False},
            },
            {
                "id": f"bsc:{single_address}",
                "active_states": ["new"],
                "resonance": {"subtype": None, "provider_families": ["985"], "confirmation_gate": {"failures": []}},
                "ranking_axes": {"timing": {"latest_event_age_seconds": 2}, "market_behavior": {"disposition": "unknown"}},
                "risk": {"hard_blocked": False},
            },
        ]
    }

    targets = fast.monitor_quote_enrichment_targets(rows, monitor, limit=6)

    assert [row["symbol"] for row in targets] == ["FRESH"]


def test_monitor_quote_cache_applies_fresh_pair_age_and_market_fields():
    now = "2026-09-12T10:00:10+00:00"
    address = "0x" + "4" * 40
    rows = [{"chain": "bsc", "contract_address": address, "symbol": "DOG"}]
    cache = {
        "quotes": {
            f"bsc:{address}": {
                "chain": "bsc",
                "contract_address": address,
                "quote_status": "fresh",
                "quote_observed_at": "2026-09-12T10:00:05+00:00",
                "price_usd": 0.001,
                "market_cap": 25_000,
                "mcap": 25_000,
                "liquidity": 12_000,
                "pair_address": "0xpool",
                "pair_age_hours": 0.25,
                "holder_count": 88,
                "top10_holder_pct": 14.2,
                "holder_source": "gmgn",
                "holder_observed_at": "2026-09-12T10:00:04+00:00",
            }
        }
    }

    enriched = fast.apply_monitor_quote_cache(rows, cache, now)

    assert enriched[0]["quote_status"] == "fresh"
    assert enriched[0]["market_cap"] == 25_000
    assert enriched[0]["pair_age_hours"] == 0.25
    assert enriched[0]["holder_count"] == 88
    assert enriched[0]["holder_observed_at"] == "2026-09-12T10:00:04+00:00"


def test_monitor_quote_refresh_persists_public_market_and_live_holder_evidence(tmp_path):
    now = "2026-09-12T10:00:10+00:00"
    address = "0x" + "5" * 40
    row = {"chain": "bsc", "contract_address": address, "symbol": "FAST"}
    seen = []

    def fetcher(chain, addresses):
        seen.append((chain, addresses))
        return [{
            "chainId": "bsc",
            "pairAddress": "0xpair",
            "pairCreatedAt": 1789206310000,
            "baseToken": {"address": address},
            "priceUsd": "0.001",
            "marketCap": 25_000,
            "liquidity": {"usd": 12_000},
            "volume": {"m5": 4_000, "h24": 80_000},
            "txns": {"m5": {"buys": 20, "sells": 8}},
            "priceChange": {"m5": 8, "h1": 25, "h24": 40},
        }]

    def gmgn_fetcher(chain, token_address, **_kwargs):
        assert chain == "bsc"
        assert token_address == address
        return {
            "chain": chain,
            "contract_address": token_address,
            "quote_status": "fresh",
            "quote_source": "gmgn_skill_token_info",
            "quote_observed_at": now,
            "gmgn_observed_at": now,
            "price_usd": 0.001,
            "market_cap": 25_000,
            "mcap": 25_000,
            "liquidity": 12_000,
            "pair_address": "0xpair",
            "holder_count": 321,
            "top10_holder_pct": 12.5,
        }

    status = fast.refresh_monitor_quotes(
        tmp_path,
        [row],
        now,
        fetcher=fetcher,
        gmgn_fetcher=gmgn_fetcher,
    )
    cache = json.loads((tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).read_text(encoding="utf-8"))

    assert seen == [("bsc", [address])]
    assert status["fresh_count"] == 1
    quote = cache["quotes"][f"bsc:{address}"]
    assert quote["quote_status"] == "fresh"
    assert quote["pair_age_hours"] >= 0
    assert quote["holder_count"] == 321
    assert quote["holders"] == 321
    assert quote["top10_holder_pct"] == 12.5
    assert quote["holder_source"] == "gmgn"
    assert quote["holder_observed_at"] == now


def test_monitor_quote_refresh_staggers_selected_gmgn_holder_requests(tmp_path):
    now = "2026-09-12T10:00:10+00:00"
    addresses = [f"0x{index:040x}" for index in range(1, 4)]
    rows = [{"chain": "bsc", "contract_address": address, "symbol": f"FAST{index}"}
            for index, address in enumerate(addresses, start=1)]
    calls = []

    def fetcher(_chain, _addresses):
        return [{
            "chainId": "bsc",
            "pairAddress": f"pair-{address}",
            "baseToken": {"address": address},
            "priceUsd": "0.001",
            "marketCap": 25_000,
            "liquidity": {"usd": 12_000},
        } for address in _addresses]

    def gmgn_fetcher(chain, token_address, **_kwargs):
        calls.append((chain, token_address))
        return {
            "chain": chain,
            "contract_address": token_address,
            "quote_status": "fresh",
            "quote_source": "gmgn_skill_token_info",
            "quote_observed_at": now,
            "gmgn_observed_at": now,
            "price_usd": 0.001,
            "market_cap": 25_000,
            "mcap": 25_000,
            "liquidity": 12_000,
            "pair_address": f"pair-{token_address}",
            "holder_count": 200,
            "top10_holder_pct": 12.5,
        }

    fast.refresh_monitor_quotes(
        tmp_path,
        rows,
        now,
        fetcher=fetcher,
        gmgn_fetcher=gmgn_fetcher,
    )

    assert calls == [("bsc", addresses[0])]


def test_monitor_quote_refresh_respects_persisted_gmgn_cooldown(tmp_path):
    now = "2026-09-12T10:00:10+00:00"
    address = "0x" + "7" * 40
    key = f"bsc:{address}"
    (tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).write_text(json.dumps({
        "gmgn_retry_after_epoch": time.time() + 60,
        "quotes": {
            key: {
                "chain": "bsc",
                "contract_address": address,
                "holders": 222,
                "holder_count": 222,
                "top10_holder_pct": 12.5,
                "holder_source": "gmgn",
                "holder_observed_at": "2026-09-12T09:59:50+00:00",
            },
        },
    }), encoding="utf-8")

    def fetcher(_chain, _addresses):
        return [{
            "chainId": "bsc",
            "pairAddress": "pair-1",
            "baseToken": {"address": address},
            "priceUsd": "0.001",
            "marketCap": 25_000,
            "liquidity": {"usd": 12_000},
        }]

    def gmgn_fetcher(*_args, **_kwargs):
        raise AssertionError("GMGN must not be called during cooldown")

    status = fast.refresh_monitor_quotes(
        tmp_path,
        [{"chain": "bsc", "contract_address": address, "symbol": "COOL"}],
        now,
        fetcher=fetcher,
        gmgn_fetcher=gmgn_fetcher,
    )
    cache = json.loads((tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).read_text(encoding="utf-8"))

    assert status["gmgn_cooldown"] is True
    assert status["errors"] == []
    assert cache["quotes"][key]["holders"] == 222
    assert cache["gmgn_retry_after_epoch"] > time.time()


def test_selected_quote_scheduler_defaults_to_three_seconds():
    assert fast.schedule_monitor_quote_enrichment.__kwdefaults__["min_interval_seconds"] == 3.0


def test_monitor_intelligence_report_contains_only_selected_live_states():
    selected = {
        "id": "bsc:0x" + "1" * 40,
        "identity": {"chain": "bsc", "contract_address": "0x" + "1" * 40, "symbol": "EARLY", "name": "Early Coin"},
        "active_states": ["new", "building"],
        "freshness": {"status": "fresh"},
        "market": {"market_cap_usd": 44_000, "liquidity_usd": 18_000, "holders": 120, "top10_holder_pct": 14.5, "observed_at": "2026-09-12T10:00:00+00:00"},
        "resonance": {"provider_families": ["gmgn", "985"]},
        "risk": {"soft_flags": ["unlocked_lp"]},
        "wallet_evidence": {"verified_buyers": 2},
    }
    trend = {
        "id": "bsc:0x" + "2" * 40,
        "active_states": ["new", "trend_watch"],
        "freshness": {"status": "fresh"},
    }
    blocked = {
        "id": "bsc:0x" + "3" * 40,
        "active_states": ["resonating", "blocked_risk"],
        "freshness": {"status": "fresh"},
    }

    report = fast.monitor_intelligence_report({
        "schema_version": 3,
        "tokens": [selected, trend, blocked],
    })

    assert [token["id"] for token in report["monitor_v3"]["tokens"]] == [selected["id"]]
    assert report["token_intelligence_selected_only"] is True
    assert report["monitor_selected_rows"] == [{
        "chain": "bsc",
        "contract_address": "0x" + "1" * 40,
        "symbol": "EARLY",
        "name": "Early Coin",
        "market_cap": 44_000,
        "liquidity": 18_000,
        "holders": 120,
        "top10_holder_pct": 14.5,
        "quote_observed_at": "2026-09-12T10:00:00+00:00",
        "quote_source": "gmgn",
        "provider_feed": "gmgn_token_info",
        "observed_at": "2026-09-12T10:00:00+00:00",
        "source_labels": ["gmgn", "985"],
        "source_count": 2,
        "risk_flags": ["unlocked_lp"],
        "volume24h": None,
        "smart_money_evidence": {
            "qualified_wallet_count": 2,
            "candidate_buy_wallet_count": None,
            "sources": ["gmgn", "985"],
            "observed_at": "2026-09-12T10:00:00+00:00",
        },
    }]
    assert set(report["monitor_v3"]) == {"schema_version", "observed_at", "tokens"}


def test_potential_selection_reserves_breadth_per_chain(monkeypatch):
    seen = []

    def select(rows, *, limit, replay_calibration):
        seen.append({row["chain"] for row in rows})
        return rows[:limit]

    monkeypatch.setattr(fast, "build_meme_potential_rows", select)
    rows = [
        {"chain": "bsc", "symbol": "BSC-1"},
        {"chain": "bsc", "symbol": "BSC-2"},
        {"chain": "robinhood", "symbol": "RBH-1"},
    ]

    selected = fast.per_chain_potential_rows(rows, replay_calibration={})

    assert [row["symbol"] for row in selected] == ["BSC-1", "BSC-2", "RBH-1"]
    assert seen == [{"bsc"}, {"robinhood"}]


def test_potential_selection_drops_terminal_watch_rows_before_ranking(monkeypatch):
    def select(rows, *, limit, replay_calibration):
        return rows[:limit]

    monkeypatch.setattr(fast, "build_meme_potential_rows", select)
    rows = [
        {"chain": "bsc", "symbol": "OLD-HIGH", "watch_status": "invalidated"},
        {"chain": "bsc", "symbol": "NEW", "watch_status": "early_candidate"},
        {"chain": "bsc", "symbol": "EXPIRED", "watch_status": "expired"},
    ]

    selected = fast.per_chain_potential_rows(rows, replay_calibration={}, limit_per_chain=1)

    assert [row["symbol"] for row in selected] == ["NEW"]


def test_potential_selection_drops_non_entry_states_and_late_candidates(monkeypatch):
    monkeypatch.setattr(
        fast,
        "build_meme_potential_rows",
        lambda rows, *, limit, replay_calibration: rows[:limit],
    )
    rows = [
        {
            "chain": "bsc", "symbol": "FRESH", "watch_status": "candidate",
            "watch_first_seen_at": "2026-09-11T01:55:00+08:00",
        },
        {
            "chain": "bsc", "symbol": "OLD", "watch_status": "candidate",
            "watch_first_seen_at": "2026-09-11T01:30:00+08:00",
        },
        {
            "chain": "bsc", "symbol": "PULLBACK", "watch_status": "pullback",
            "watch_first_seen_at": "2026-09-11T01:59:00+08:00",
        },
    ]

    selected = fast.per_chain_potential_rows(
        rows,
        replay_calibration={},
        evaluated_at="2026-09-11T02:00:00+08:00",
    )

    assert [row["symbol"] for row in selected] == ["FRESH"]


def test_fast_discovery_publishes_local_candidates_without_full_report_pipeline(tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "report.json"
    status_path = out_dir / "status.json"
    report_path.write_text(json.dumps({"meta": {"kept": True}, "alpha_rows": [{"symbol": "ALPHA"}]}))
    token = "0x" + "a" * 40
    candidate = {
        "chain": "robinhood",
        "chain_id": "robinhood",
        "contract_address": token,
        "token_address": token,
        "symbol": "FAST",
        "name": "Fast candidate",
        "mcap": 50_000,
        "market_cap": 50_000,
        "price_usd": 0.001,
        "liquidity": 25_000,
        "volume24h": 60_000,
        "pair_age_hours": 0.1,
        "sources": ["noxa_launchpad", "wind_monitor"],
        "source_count": 2,
    }

    status = fast.run_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        source_refresher=lambda out_dir: [{"ok": True}],
        candidate_loader=lambda limit, concurrency, source_loader: ([candidate], []),
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    snapshot = json.loads((out_dir / "alpha-meme-fast-latest.json").read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert report["alpha_rows"] == [{"symbol": "ALPHA"}]
    assert report["meta"]["kept"] is True
    assert snapshot["meta"]["live_refresh_mode"] == "meme_fast_discovery"
    assert snapshot["meme_rows"][0]["symbol"] == "FAST"
    assert snapshot["meme_rows"][0]["rank_score"] >= 0
    assert snapshot["meme_rows"][0]["watch_status"]
    assert snapshot["meme_rows"][0]["watch_v2_tier"] in {"discovered", "early", "confirmed", "review"}


def test_fast_discovery_publishes_selected_candidates_to_live_execution(monkeypatch, tmp_path):
    now = "2026-09-11T02:00:00+00:00"
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "report.json"
    status_path = out_dir / "status.json"
    report_path.write_text("{}", encoding="utf-8")
    token = "0x" + "d" * 40
    pool = "0x" + "e" * 64
    candidate = {
        "chain": "robinhood",
        "chain_id": "robinhood",
        "contract_address": token,
        "token_address": token,
        "symbol": "FAST-LIVE",
        "mcap": 50_000,
        "market_cap": 50_000,
        "price_usd": 0.001,
        "liquidity": 25_000,
        "liquidity_usd": 25_000,
        "pair_address": pool,
        "pool_address": pool,
        "valuation_type": "market_cap",
        "pair_age_hours": 0.02,
        "quote_status": "fresh",
        "quote_observed_at": now,
        "sources": ["gmgn_hot", "dexscreener"],
        "source_labels": ["GMGN", "DS"],
        "source_count": 2,
        "hard_risk_pass": True,
    }

    monkeypatch.setattr(fast, "now_iso", lambda: now)
    monkeypatch.setattr(
        fast,
        "compute_discovery_rank",
        lambda row, **kwargs: {"rank_score": 92, "rank_components": {"sources": 30, "timing": 30}},
    )
    monkeypatch.setattr(
        fast,
        "build_meme_potential_rows",
        lambda rows, *, limit, replay_calibration: [
            {
                **row,
                "recommendation_bucket": "ambush",
                "signal_stage": "aggregate_early_bird",
                "rank_score": 92,
                "rank_components": {"sources": 30, "timing": 30},
            }
            for row in rows[:limit]
        ],
    )

    status = fast.run_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        source_refresher=lambda out_dir: [{"ok": True}],
        candidate_loader=lambda limit, concurrency, source_loader: ([candidate], []),
    )

    payload = json.loads((out_dir / "robinhood-execution-input.json").read_text(encoding="utf-8"))
    bsc_payload = json.loads((out_dir / "bsc-execution-input.json").read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert status["execution_input"]["robinhood_signal_count"] == 1
    assert payload["signals"][0]["symbol"] == "FAST-LIVE"
    assert payload["signals"][0]["signal_stage"] == "aggregate_early_bird"
    assert payload["signals"][0]["entry_route"] == "robinhood_aggregate_early_bird"
    assert payload["quotes"][0]["contract_address"] == token
    assert bsc_payload["signals"] == []


def test_fast_execution_publish_does_not_clear_existing_inputs_when_no_candidate(tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    robinhood_path = out_dir / "robinhood-execution-input.json"
    bsc_path = out_dir / "bsc-execution-input.json"
    robinhood_existing = {"updated_at": "old", "signals": [{"symbol": "KEEP-RH"}]}
    bsc_existing = {"updated_at": "old", "signals": [{"symbol": "KEEP-BSC"}]}
    robinhood_path.write_text(json.dumps(robinhood_existing), encoding="utf-8")
    bsc_path.write_text(json.dumps(bsc_existing), encoding="utf-8")

    result = fast.publish_execution_inputs_from_fast_snapshot(
        out_dir=out_dir,
        potential=[],
        replay={},
        generated_at="2026-09-11T02:00:00+00:00",
    )

    assert result["published"] is False
    assert json.loads(robinhood_path.read_text(encoding="utf-8")) == robinhood_existing
    assert json.loads(bsc_path.read_text(encoding="utf-8")) == bsc_existing


def test_arc_never_enters_execution_inputs(tmp_path):
    generated_at = "2026-09-16T03:00:00+00:00"
    arc_candidate = {
        "chain": "5042",
        "contract_address": "0x" + "a" * 40,
        "symbol": "ARC",
    }
    bsc_candidate = {
        "chain": "bsc",
        "contract_address": "0x" + "b" * 40,
        "symbol": "BSC",
    }
    robinhood_candidate = {
        "chain": "robinhood",
        "contract_address": "0x" + "c" * 40,
        "symbol": "RH",
    }

    fast.publish_execution_inputs_from_fast_snapshot(
        out_dir=tmp_path,
        potential=[arc_candidate, bsc_candidate, robinhood_candidate],
        replay={},
        generated_at=generated_at,
    )

    assert arc_candidate["contract_address"] not in (
        tmp_path / "bsc-execution-input.json"
    ).read_text(encoding="utf-8")
    assert arc_candidate["contract_address"] not in (
        tmp_path / "robinhood-execution-input.json"
    ).read_text(encoding="utf-8")
    assert not (tmp_path / "arc-execution-input.json").exists()

    baseline_dir = tmp_path / "without-arc"
    baseline_dir.mkdir()
    fast.publish_execution_inputs_from_fast_snapshot(
        out_dir=baseline_dir,
        potential=[bsc_candidate, robinhood_candidate],
        replay={},
        generated_at=generated_at,
    )
    assert json.loads((tmp_path / "bsc-execution-input.json").read_text(encoding="utf-8")) == json.loads(
        (baseline_dir / "bsc-execution-input.json").read_text(encoding="utf-8")
    )
    assert json.loads((tmp_path / "robinhood-execution-input.json").read_text(encoding="utf-8")) == json.loads(
        (baseline_dir / "robinhood-execution-input.json").read_text(encoding="utf-8")
    )


def test_execution_allowlist_filters_arc_before_strategy_annotation(monkeypatch, tmp_path):
    observed = []

    def capture(rows, out_dir, now, replay_history):
        observed.extend(rows)
        return []

    monkeypatch.setattr(fast, "annotate_new_execution_candidates", capture)
    fast.publish_execution_inputs_from_fast_snapshot(
        out_dir=tmp_path,
        potential=[
            {"chain": "arc", "contract_address": "0x" + "a" * 40},
            {"chain": "bsc", "contract_address": "0x" + "b" * 40},
            {"chain": "robinhood", "contract_address": "0x" + "c" * 40},
        ],
        replay={},
        generated_at="2026-09-16T03:00:00+00:00",
    )

    assert {row["chain"] for row in observed} == {"bsc", "robinhood"}


def test_arc_market_refresh_skips_unsupported_gmgn_holder_lookup(tmp_path):
    address = "0x" + "e" * 40
    gmgn_calls = []

    result = fast.refresh_monitor_quotes(
        tmp_path,
        [{"chain": "5042", "contract_address": address}],
        "2026-09-16T03:00:00+00:00",
        fetcher=lambda chain, addresses: [{
            "chainId": "arc",
            "baseToken": {"address": address},
            "pairAddress": "0x" + "f" * 40,
            "priceUsd": 0.001,
            "liquidity": {"usd": 10_000},
            "volume": {"m5": 50, "h24": 500},
            "txns": {"m5": {"buys": 2, "sells": 1}},
            "marketCap": 25_000,
        }],
        gmgn_fetcher=lambda *args, **kwargs: gmgn_calls.append((args, kwargs)) or {},
    )

    cache = json.loads((tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).read_text(encoding="utf-8"))
    assert result["fresh_count"] == 1
    assert gmgn_calls == []
    assert cache["quotes"][f"arc:{address}"]["gmgn_holder_status"] == "provider_unavailable"


def test_arc_discovery_without_pair_remains_visible_as_market_pending(monkeypatch, tmp_path):
    observed_at = "2026-09-16T03:00:00+00:00"
    address = "0x" + "d" * 40
    candidate = {
        "chain": "arc",
        "chain_id": "arc",
        "contract_address": address,
        "token_address": address,
        "symbol": "PENDING",
        "name": "Pending market",
        "observed_at": observed_at,
        "market_data_pending": True,
        "sources": ["arc_rpc"],
        "provider_feed": "arc_rpc",
    }
    monkeypatch.setattr(fast, "now_iso", lambda: observed_at)

    event = {
        **fast_monitor_event("arc-pending", observed_at),
        "chain": "arc",
        "contract_address": address,
        "symbol": "PENDING",
        "name": "Pending market",
        "provider_family": "onchain",
        "provider_feed": "arc_rpc",
        "upstream_provider": "arc_rpc",
    }

    fast.run_once(
        out_dir=tmp_path,
        report_path=tmp_path / "report.json",
        status_path=tmp_path / "status.json",
        source_refresher=lambda out_dir: [{"source": "arc_onchain", "ok": True}],
        candidate_loader=fast.load_meme_candidates,
        observation_loader=lambda *args, **kwargs: {
            "candidates": [candidate],
            "events": [event],
            "rejections": [],
            "errors": [],
            "feed_counts": {"arc_rpc": 1},
        },
        execution_handoff_enabled=False,
    )

    snapshot = json.loads((tmp_path / fast.FAST_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert snapshot["meme_rows"][0]["contract_address"] == address
    assert snapshot["meme_rows"][0]["market_data_pending"] is True
    assert snapshot["monitor_v3"]["tokens"][0]["id"] == f"arc:{address}"


def test_monitor_stage_filter_only_hands_early_and_confirmed_tokens_to_execution():
    rows = [
        {"chain": "bsc", "contract_address": "0x" + "a" * 40, "symbol": "EARLY"},
        {"chain": "bsc", "contract_address": "0x" + "b" * 40, "symbol": "CONFIRMED"},
        {"chain": "bsc", "contract_address": "0x" + "c" * 40, "symbol": "WATCH"},
        {"chain": "bsc", "contract_address": "0x" + "d" * 40, "symbol": "RAW"},
    ]
    snapshot = {
        "tokens": [
            {
                "id": "bsc:0x" + "a" * 40,
                "primary_state": "building",
                "active_states": ["new", "building"],
                "ranking_axes": {"market_behavior": {"disposition": "pass", "flags": []}},
            },
            {
                "id": "bsc:0x" + "b" * 40,
                "primary_state": "resonating",
                "active_states": ["new", "building", "resonating"],
                "ranking_axes": {"market_behavior": {"disposition": "pass", "flags": []}},
            },
            {
                "id": "bsc:0x" + "c" * 40,
                "primary_state": "trend_watch",
                "active_states": ["new", "trend_watch"],
                "ranking_axes": {"market_behavior": {"disposition": "observe", "flags": ["sell_pressure"]}},
            },
            {
                "id": "bsc:0x" + "d" * 40,
                "primary_state": "new",
                "active_states": ["new"],
                "ranking_axes": {"market_behavior": {"disposition": "unknown", "flags": []}},
            },
        ]
    }

    selected = fast.monitor_selected_execution_candidates(rows, snapshot)

    assert [(row["symbol"], row["signal_stage"]) for row in selected] == [
        ("EARLY", "aggregate_early_bird"),
        ("CONFIRMED", "aggregate_confirmation"),
    ]


def test_fast_discovery_uses_monitor_promotion_for_execution_and_writes_outcomes(monkeypatch, tmp_path):
    now = "2026-09-11T02:00:00+00:00"
    token = "0x" + "e" * 40
    candidate = {
        "chain": "bsc",
        "chain_id": "bsc",
        "contract_address": token,
        "token_address": token,
        "symbol": "PROMOTED",
        "mcap": 30_000,
        "market_cap": 30_000,
        "price_usd": 0.001,
        "liquidity": 12_000,
        "volume24h": 80_000,
        "pair_age_hours": 0.02,
        "sources": ["gmgn", "okx"],
        "source_count": 2,
    }
    monitor_snapshot = {
        "tokens": [{
            "id": f"bsc:{token}",
            "identity": {"chain": "bsc", "contract_address": token, "symbol": "PROMOTED"},
            "primary_state": "building",
            "active_states": ["new", "building"],
            "state_updated_at": now,
            "market": {"market_cap_usd": 30_000, "price_usd": 0.001},
            "ranking_axes": {"market_behavior": {"disposition": "pass", "flags": []}},
            "resonance": {"provider_families": ["gmgn", "okx"]},
        }]
    }
    handed_to_execution = []
    monkeypatch.setattr(fast, "now_iso", lambda: now)
    monkeypatch.setattr(fast, "publish_fast_monitor_snapshot", lambda **kwargs: monitor_snapshot)
    monkeypatch.setattr(
        fast,
        "build_meme_potential_rows",
        lambda rows, *, limit, replay_calibration: [{**rows[0], "recommendation_bucket": "ambush"}],
    )
    monkeypatch.setattr(
        fast,
        "publish_execution_inputs_from_fast_snapshot",
        lambda **kwargs: handed_to_execution.extend(kwargs["potential"]) or {"published": True},
    )

    fast.run_once(
        out_dir=tmp_path,
        report_path=tmp_path / "report.json",
        status_path=tmp_path / "status.json",
        source_refresher=lambda out_dir: [{"ok": True}],
        candidate_loader=fast.load_meme_candidates,
        observation_loader=lambda *args, **kwargs: {
            "candidates": [candidate],
            "events": [],
            "rejections": [],
            "errors": [],
            "feed_counts": {},
        },
    )

    assert handed_to_execution[0]["signal_stage"] == "aggregate_early_bird"
    outcome = json.loads((tmp_path / "alpha-monitor-outcomes-latest.json").read_text(encoding="utf-8"))
    assert outcome["records"][f"bsc:{token}"]["cohort"] == "selected"


def test_fast_discovery_publishes_normalized_batch_to_monitor_v3(monkeypatch, tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "report.json"
    status_path = out_dir / "status.json"
    report_path.write_text(json.dumps({"monitor_v3": {"tokens": [{"id": "old"}]}}))
    token = "0x" + "b" * 40
    candidate = {
        "chain": "bsc",
        "contract_address": token,
        "symbol": "FAST-V3",
        "mcap": 20_000,
        "price_usd": 0.001,
        "liquidity": 10_000,
        "volume24h": 80_000,
        "txns24h": 40,
        "pair_age_hours": 0.02,
        "quote_status": "fresh",
    }
    batch = {
        "candidates": [candidate],
        "events": [{"event_id": "fast-event"}],
        "rejections": [],
        "errors": [],
        "feed_counts": {"bsc_new_pair": 1},
    }
    published = []
    monkeypatch.setattr(
        fast,
        "publish_fast_monitor_snapshot",
        lambda **kwargs: published.append(kwargs) or {
            "observed_at": kwargs["observed_at"],
            "tokens": [{"id": "fresh"}],
        },
    )

    status = fast.run_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        source_refresher=lambda out_dir: [{"ok": True}],
        observation_loader=lambda *args, **kwargs: batch,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    snapshot = json.loads((out_dir / "alpha-meme-fast-latest.json").read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert published[0]["batch"] is batch
    assert published[0]["previous_snapshot"] is None
    assert report["monitor_v3"]["tokens"] == [{"id": "old"}]
    assert snapshot["monitor_v3"]["tokens"] == [{"id": "fresh"}]


def test_fast_discovery_writes_sidecar_without_mutating_slow_report(tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "status.json"
    slow_report = {
        "meta": {"report_generated_at": "2026-09-11T09:00:00+08:00"},
        "alpha_rows": [{"symbol": "KEEP"}],
    }
    report_path.write_text(json.dumps(slow_report), encoding="utf-8")
    token = "0x" + "c" * 40
    candidate = {
        "chain": "bsc",
        "contract_address": token,
        "symbol": "FAST-SIDECAR",
        "mcap": 25_000,
        "price_usd": 0.001,
        "liquidity": 12_000,
        "volume24h": 80_000,
        "pair_age_hours": 0.02,
        "quote_status": "fresh",
        "quote_fingerprint": "q-1",
        "sources": ["gmgn_hot", "okx_signal"],
        "source_count": 2,
    }

    status = fast.run_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        source_refresher=lambda out_dir: [{"ok": True}],
        candidate_loader=lambda limit, concurrency, source_loader: ([candidate], []),
    )

    assert status["ok"] is True
    assert json.loads(report_path.read_text(encoding="utf-8")) == slow_report
    snapshot = json.loads((out_dir / "alpha-meme-fast-latest.json").read_text(encoding="utf-8"))
    assert snapshot["meta"]["live_refresh_mode"] == "meme_fast_discovery"
    assert snapshot["meme_rows"][0]["symbol"] == "FAST-SIDECAR"


def test_fast_discovery_never_reads_slow_report(monkeypatch, tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "alpha-radar-report-latest.json"
    status_path = out_dir / "status.json"
    report_path.write_text("not-json", encoding="utf-8")
    token = "0x" + "9" * 40
    candidate = {
        "chain": "bsc",
        "contract_address": token,
        "symbol": "NO-SLOW-READ",
        "mcap": 25_000,
        "price_usd": 0.001,
        "liquidity": 12_000,
        "volume24h": 80_000,
        "pair_age_hours": 0.02,
        "quote_status": "fresh",
        "quote_fingerprint": "q-no-slow-read",
        "sources": ["gmgn_hot", "okx_signal"],
        "source_count": 2,
    }
    original_read_json = fast.read_json

    def reject_slow_report(path):
        if path == report_path:
            raise AssertionError("slow report read")
        return original_read_json(path)

    monkeypatch.setattr(fast, "read_json", reject_slow_report)

    status = fast.run_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=status_path,
        source_refresher=lambda out_dir: [{"ok": True}],
        candidate_loader=lambda limit, concurrency, source_loader: ([candidate], []),
    )

    assert status["ok"] is True
    assert (out_dir / "alpha-meme-monitor-v3-latest.json").exists()


def test_fast_discovery_writes_monitor_baseline_sidecar(monkeypatch, tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    token = "0x" + "8" * 40
    candidate = {
        "chain": "robinhood",
        "contract_address": token,
        "symbol": "BASELINE",
        "mcap": 42_000,
        "price_usd": 0.00042,
        "liquidity": 18_000,
        "volume24h": 90_000,
        "pair_age_hours": 0.02,
        "quote_status": "fresh",
        "quote_fingerprint": "q-baseline",
        "sources": ["noxa_launchpad", "wind_monitor"],
        "source_count": 2,
        "watch_first_seen_at": "2026-09-12T09:00:00+08:00",
        "watch_first_seen_mcap": 31_000,
        "watch_max_seen_mcap": 55_000,
    }

    generated_at = "2026-09-12T09:00:00+08:00"
    monkeypatch.setattr(fast, "now_iso", lambda: generated_at)
    status = fast.run_once(
        out_dir=out_dir,
        report_path=out_dir / "missing-report.json",
        status_path=out_dir / "status.json",
        source_refresher=lambda out_dir: [{"ok": True}],
        candidate_loader=lambda limit, concurrency, source_loader: ([candidate], []),
    )

    baselines = json.loads(
        (out_dir / "alpha-monitor-baselines-latest.json").read_text(encoding="utf-8")
    )
    assert status["ok"] is True
    assert baselines[f"robinhood:{token}"] == {
        "first_seen_at": "2026-09-12T09:00:00+08:00",
        "first_market_cap_usd": 42_000,
        "first_price_usd": None,
        "peak_market_cap_usd": 42_000,
    }


def test_pending_quote_rows_keep_recent_multi_source_candidates_out_of_rejects():
    rows = [
        {
            "chain": "bsc",
            "symbol": "RESONATING",
            "contract_address": "0x" + "d" * 40,
            "pair_age_hours": 0.05,
            "source_count": 2,
            "sources": ["gmgn_hot", "okx_signal"],
            "quote_status": "stale",
            "market_data_pending": True,
            "recommendation_bucket": "pullback",
            "rank_score": 71,
        },
        {
            "chain": "bsc",
            "symbol": "ONE-SOURCE",
            "contract_address": "0x" + "e" * 40,
            "pair_age_hours": 0.05,
            "source_count": 1,
            "sources": ["gmgn_hot"],
            "quote_status": "unavailable",
            "market_data_pending": True,
            "recommendation_bucket": "pullback",
            "rank_score": 80,
        },
        {
            "chain": "bsc",
            "symbol": "HARD-FAIL",
            "contract_address": "0x" + "f" * 40,
            "pair_age_hours": 0.05,
            "source_count": 3,
            "quote_status": "unavailable",
            "market_data_pending": True,
            "recommendation_bucket": "reject",
            "rank_score": 99,
        },
    ]

    pending = fast.build_pending_quote_rows(rows, limit=8)

    assert [row["symbol"] for row in pending] == ["RESONATING"]
    assert pending[0]["selection_state"] == "pending_quote"
    assert pending[0]["selection_reason"] == "多源共振，等待新鲜报价"


def test_fast_watch_uses_two_quick_observations_for_confirmation(monkeypatch, tmp_path):
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()
    report_path = out_dir / "report.json"
    report_path.write_text("{}", encoding="utf-8")
    captured = {}
    token = "0x" + "1" * 40
    candidate = {
        "chain": "bsc",
        "contract_address": token,
        "symbol": "QUICK",
        "mcap": 25_000,
        "price_usd": 0.001,
        "liquidity": 12_000,
        "volume24h": 80_000,
        "pair_age_hours": 0.02,
        "quote_status": "fresh",
        "quote_fingerprint": "q-1",
        "sources": ["gmgn_hot", "okx_signal"],
        "source_count": 2,
    }

    def capture_watch(state, rows, now, **kwargs):
        captured.update(kwargs)
        return {"candidates": {}}

    monkeypatch.setattr(fast, "update_watch_state", capture_watch)
    monkeypatch.setattr(fast, "attach_gold_watch_fields", lambda rows, state: None)

    status = fast.run_once(
        out_dir=out_dir,
        report_path=report_path,
        status_path=out_dir / "status.json",
        source_refresher=lambda out_dir: [{"ok": True}],
        candidate_loader=lambda limit, concurrency, source_loader: ([candidate], []),
    )

    assert status["ok"] is True
    assert captured["min_confirmations"] == 2


def test_monitor_quote_cache_strips_arc_gmgn_market_and_holders_but_keeps_bsc_cache():
    now = "2026-09-12T10:00:10+00:00"
    arc_address = "0x" + "a" * 40
    bsc_address = "0x" + "b" * 40
    cached_quote = {
        "quote_status": "fresh",
        "quote_observed_at": "2026-09-12T10:00:05+00:00",
        "quote_source": "gmgn_skill_token_info",
        "price_usd": 0.001,
        "market_cap": 25_000,
        "liquidity": 12_000,
        "holders": 88,
        "holder_count": 88,
        "top10_holder_pct": 14.2,
        "holder_source": "gmgn",
        "holder_observed_at": "2026-09-12T10:00:04+00:00",
    }
    cache = {
        "quotes": {
            f"arc:{arc_address}": {**cached_quote, "chain": "arc", "contract_address": arc_address},
            f"bsc:{bsc_address}": {**cached_quote, "chain": "bsc", "contract_address": bsc_address},
        }
    }

    arc_row, bsc_row = fast.apply_monitor_quote_cache(
        [
            {"chain": "arc", "contract_address": arc_address},
            {"chain": "bsc", "contract_address": bsc_address},
        ],
        cache,
        now,
    )

    assert arc_row["quote_status"] == "unavailable"
    assert "market_cap" not in arc_row
    assert "holder_count" not in arc_row
    assert "top10_holder_pct" not in arc_row
    assert bsc_row["market_cap"] == 25_000
    assert bsc_row["holder_count"] == 88


def test_arc_fresh_dex_refresh_replaces_persisted_gmgn_market_and_drops_holders(tmp_path):
    now = "2026-09-12T10:00:10+00:00"
    arc_address = "0x" + "c" * 40
    bsc_address = "0x" + "d" * 40
    previous = {
        "quote_status": "fresh",
        "quote_observed_at": "2026-09-12T10:00:05+00:00",
        "quote_source": "gmgn_skill_token_info",
        "price_usd": 9.0,
        "market_cap": 900_000,
        "liquidity": 90_000,
        "holders": 88,
        "holder_count": 88,
        "top10_holder_pct": 14.2,
        "holder_source": "gmgn",
        "holder_observed_at": "2026-09-12T10:00:05+00:00",
    }
    (tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).write_text(json.dumps({
        "quotes": {
            f"arc:{arc_address}": {**previous, "chain": "arc", "contract_address": arc_address},
            f"bsc:{bsc_address}": {**previous, "chain": "bsc", "contract_address": bsc_address},
        }
    }))

    def fetcher(chain, addresses):
        address = addresses[0]
        return [{
            "chainId": chain,
            "pairAddress": f"0x{chain}pair",
            "baseToken": {"address": address},
            "priceUsd": "0.002",
            "marketCap": 30_000,
            "liquidity": {"usd": 15_000},
            "volume": {"m5": 4_000, "h24": 80_000},
            "txns": {"m5": {"buys": 20, "sells": 8}},
            "priceChange": {"m5": 8, "h1": 25, "h24": 40},
        }]

    fast.refresh_monitor_quotes(
        tmp_path,
        [
            {"chain": "arc", "contract_address": arc_address},
            {"chain": "bsc", "contract_address": bsc_address},
        ],
        now,
        fetcher=fetcher,
        gmgn_fetcher=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected GMGN refresh")),
    )
    quotes = json.loads((tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).read_text())["quotes"]

    assert quotes[f"arc:{arc_address}"]["quote_source"] == "dexscreener"
    assert quotes[f"arc:{arc_address}"]["market_cap"] == 30_000
    assert "holder_count" not in quotes[f"arc:{arc_address}"]
    assert "top10_holder_pct" not in quotes[f"arc:{arc_address}"]
    assert quotes[f"bsc:{bsc_address}"]["holder_count"] == 88
    assert quotes[f"bsc:{bsc_address}"]["top10_holder_pct"] == 14.2


def test_arc_no_dex_refresh_does_not_retain_persisted_gmgn_market_or_holders(tmp_path):
    now = "2026-09-12T10:00:10+00:00"
    address = "0x" + "e" * 40
    (tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).write_text(json.dumps({
        "quotes": {f"arc:{address}": {
            "chain": "arc",
            "contract_address": address,
            "quote_status": "fresh",
            "quote_observed_at": "2026-09-12T10:00:05+00:00",
            "quote_source": "gmgn_skill_token_info",
            "price_usd": 9.0,
            "market_cap": 900_000,
            "liquidity": 90_000,
            "holders": 88,
            "holder_count": 88,
            "top10_holder_pct": 14.2,
            "holder_source": "gmgn",
        }}
    }))

    fast.refresh_monitor_quotes(
        tmp_path,
        [{"chain": "arc", "contract_address": address}],
        now,
        fetcher=lambda chain, addresses: [],
        gmgn_fetcher=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected GMGN refresh")),
    )
    quote = json.loads((tmp_path / fast.MONITOR_QUOTE_CACHE_NAME).read_text())["quotes"][f"arc:{address}"]

    assert quote["quote_status"] == "unavailable"
    assert quote["gmgn_holder_status"] == "provider_unavailable"
    for field in ("price_usd", "market_cap", "liquidity", "holders", "holder_count", "top10_holder_pct"):
        assert field not in quote


def test_arc_fast_source_health_preserves_degraded_success_observation_and_endpoint_errors():
    observed_at = "2026-09-12T10:00:00+00:00"
    rpc_errors = [{"url": "https://primary", "error": "timeout"}]
    arcscan_errors = [{"url": "https://scan", "error": "arcscan_lag"}]
    health = fast.fast_source_health([{
        "source": "arc_onchain",
        "status": "degraded",
        "ok": True,
        "observed_at": observed_at,
        "row_count": 2,
        "rpc_errors": rpc_errors,
        "arcscan_errors": arcscan_errors,
    }])

    assert health["arc_onchain"]["status"] == "degraded"
    assert health["arc_onchain"]["observed_at"] == observed_at
    assert health["arc_onchain"]["last_success"] == observed_at
    assert health["arc_onchain"]["rpc_errors"] == rpc_errors
    assert health["arc_onchain"]["arcscan_errors"] == arcscan_errors


def test_arc_fast_source_health_preserves_explicit_stale_status_and_adapter_time():
    observed_at = "2026-09-12T09:55:00+00:00"
    health = fast.fast_source_health([{
        "source": "arc_onchain",
        "status": "stale",
        "ok": False,
        "observed_at": observed_at,
        "row_count": 1,
        "error": "checkpoint_stale",
    }])

    assert health["arc_onchain"]["status"] == "stale"
    assert health["arc_onchain"]["observed_at"] == observed_at
    assert health["arc_onchain"]["error"] == "checkpoint_stale"


def test_nested_arc_rpc_http_429_activates_source_backoff(tmp_path):
    calls = []

    def refresher(_out_dir):
        calls.append(True)
        return {
            "source": "arc_onchain",
            "status": "degraded",
            "ok": True,
            "observed_at": "2026-09-12T10:00:00+00:00",
            "rpc_errors": [{"url": "https://primary", "error": "HTTP Error 429: Too Many Requests"}],
            "arcscan_errors": [],
        }

    fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="arc_onchain",
        refresher=refresher,
        min_interval_seconds=2,
        rate_limit_backoff_seconds=120,
        now_monotonic=100,
    )
    second = fast.refresh_fast_source_on_cadence(
        tmp_path,
        source="arc_onchain",
        refresher=refresher,
        min_interval_seconds=2,
        rate_limit_backoff_seconds=120,
        now_monotonic=105,
    )

    assert len(calls) == 1
    assert second["skipped"] is True
    assert second["reason"] == "rate_limit_backoff"
