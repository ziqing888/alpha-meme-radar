import importlib.util
import ast
import json
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


alpha = load_module("alpha_pool_radar", BASE / "alpha_pool_radar.py")
report = load_module("alpha_radar_report", BASE / "alpha_radar_report.py")
monitor = sys.modules["alpha_monitor_v3"]


def test_load_replay_history_caches_unchanged_file_and_reloads_atomic_update(tmp_path):
    path = tmp_path / "alpha-radar-replay-history.json"
    path.write_text(json.dumps({"rows": {"first": {"value": 1}}}), encoding="utf-8")

    first = report.load_replay_history(tmp_path)
    second = report.load_replay_history(tmp_path)
    assert second is first

    path.write_text(json.dumps({"rows": {"second": {"value": 2}}}), encoding="utf-8")
    reloaded = report.load_replay_history(tmp_path)
    assert reloaded is not first
    assert "second" in reloaded["rows"]


def minimal_report_args(out_dir: Path) -> Namespace:
    return Namespace(
        out_dir=str(out_dir), alpha_limit=10, top=10,
        max_market_cap=200_000_000, min_volume=1_000_000, chains="",
        concurrency=1, holders_enable=False, holder_provider="auto",
        holder_top_tokens=12, holder_offset=20, risk_enable=False,
        risk_top_tokens=12, meme_source_limit=10, meme_top=10,
        meme_potential_top=1, meme_shadow_top=10, dealer_top=10,
        gold_watch_confirmations=3, live_stage_enable=False,
    )


def test_full_report_publishes_raw_monitor_event_without_legacy_candidate(monkeypatch, tmp_path):
    raw_event = {
        "event_id": "raw-low-liquidity", "chain": "bsc",
        "contract_address": "0x1111111111111111111111111111111111111111",
        "provider_family": "onchain", "provider_feed": "fourmeme_launchpad",
        "event_type": "new_launch", "event_at": "2026-09-10T12:00:00+00:00",
        "observed_at": "2026-09-10T12:00:01+00:00", "evidence_role": "discovery",
        "signal_lane": "new_launch", "counts_for_resonance": True,
        "provider_event_id": "launch-1", "source_url": None,
        "raw_fingerprint": "raw-1", "upstream_provider": None,
        "event_time_basis": "event_at", "payload": {},
    }
    batch = {"candidates": [], "events": [raw_event], "rejections": [], "errors": [],
             "feed_counts": {"fourmeme_launchpad": 1}}
    calls = []

    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report, "load_meme_candidates", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy loader called")))
    monkeypatch.setattr(report, "load_meme_observation_batch", lambda limit, concurrency: calls.append((limit, concurrency)) or batch, raising=False)
    monkeypatch.setattr(
        report, "publish_monitor_v3",
        lambda **kwargs: {
            "schema_version": 3, "observed_at": kwargs["observed_at"],
            "monitor_status": "healthy", "source_health": kwargs["source_health"],
            "tokens": [{"id": "bsc:0x1111111111111111111111111111111111111111",
                        "events": kwargs["batch"]["events"]}], "rejections": [],
        }, raising=False,
    )
    monkeypatch.setattr(report, "refresh_token_intelligence", lambda *args, **kwargs: {})

    payload = report.build_report(minimal_report_args(tmp_path))

    assert calls == [(10, 1)]
    assert payload["meme_rows"] == []
    assert payload["meme_watch_universe"] == []
    assert payload["monitor_v3"]["schema_version"] == 3
    assert payload["monitor_v3"]["tokens"][0]["events"] == [raw_event]
    legacy_projection = {
        key: payload[key]
        for key in (
            "meme_rows", "meme_watch_universe", "meme_potential_rows",
            "meme_shadow_rows", "recommendation_rows", "gold_watch_alerts",
        )
    }
    assert "0x1111111111111111111111111111111111111111" not in json.dumps(legacy_projection)
    assert "0x1111111111111111111111111111111111111111" not in (tmp_path / "alpha-radar-replay-history.json").read_text(encoding="utf-8")


def test_monitor_v3_has_no_execution_or_live_worker_import_path():
    paths = {
        *BASE.glob("*execution*.py"),
        *BASE.glob("*live_worker.py"),
    }
    imported_by = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "alpha_monitor_v3" for alias in node.names):
                imported_by.append(path.name)
            if isinstance(node, ast.ImportFrom) and node.module == "alpha_monitor_v3":
                imported_by.append(path.name)

    assert imported_by == []


def test_legacy_loader_compatibility_rows_never_enter_monitor_projection(monkeypatch, tmp_path):
    legacy_row = {
        "chain": "bsc",
        "contract_address": "0x3333333333333333333333333333333333333333",
        "symbol": "LEGACY",
    }
    published = []
    monkeypatch.setattr(report, "load_meme_candidates", lambda limit, concurrency: ([legacy_row], []))
    monkeypatch.setattr(
        report,
        "publish_monitor_v3",
        lambda **kwargs: published.append(kwargs["batch"]) or {
            "schema_version": 3, "observed_at": kwargs["observed_at"],
            "monitor_status": "healthy", "source_health": {},
            "tokens": [], "rejections": [],
        },
    )

    batch = report.collect_meme_observation_batch(10, 1)
    report.publish_report_monitor_v3(
        out_dir=tmp_path,
        batch=batch,
        observed_at="2026-09-10T12:00:00+00:00",
        previous_snapshot=None,
    )

    assert batch["candidates"] == [legacy_row]
    assert published[0]["candidates"] == []


def test_monitor_health_degrades_only_the_failed_source():
    batch = {
        "events": [
            {
                "provider_family": "onchain",
                "event_at": "2026-09-10T12:00:00+00:00",
            }
        ],
        "errors": ["gmgn_trenches bsc: timeout"],
    }

    health = report.monitor_source_health(batch, "2026-09-10T12:00:01+00:00")

    assert health["onchain"]["status"] == "ok"
    assert health["onchain"]["last_event"] == "2026-09-10T12:00:00+00:00"
    assert health["gmgn"]["status"] == "error"
    assert set(health) == {"onchain", "gmgn"}


def test_clean_monitor_batch_clears_previous_generic_source_failure():
    health = report.monitor_source_health(
        {
            "events": [{"provider_family": "gmgn", "event_at": "2026-09-10T12:00:00+00:00"}],
            "errors": [],
        },
        "2026-09-10T12:00:01+00:00",
    )

    assert health["gmgn"]["status"] == "ok"


def test_explicit_source_refresh_error_is_not_overwritten_by_stale_file_events():
    health = report.monitor_source_health(
        {
            "source_health": {
                "proficy": {
                    "status": "error",
                    "observed_at": "2026-09-10T12:00:01+00:00",
                    "error": "HTTP 403",
                }
            },
            "events": [{
                "provider_family": "proficy",
                "event_at": "2026-09-10T11:58:00+00:00",
            }],
            "errors": [],
        },
        "2026-09-10T12:00:01+00:00",
    )

    assert health["proficy"]["status"] == "error"
    assert health["proficy"]["last_event"] == "2026-09-10T11:58:00+00:00"
    assert health["meme_sources"]["status"] == "ok"
    assert health["meme_sources"]["last_success"] == "2026-09-10T12:00:01+00:00"


def test_monitor_persistence_failure_preserves_previous_tokens_and_marks_degraded(monkeypatch, tmp_path):
    previous = {
        "schema_version": 3,
        "observed_at": "2026-09-10T12:00:00+00:00",
        "monitor_status": "healthy",
        "source_health": {"gmgn": {"status": "ok"}},
        "tokens": [{"id": "bsc:0x4444444444444444444444444444444444444444", "events": [{"event_id": "old"}]}],
        "rejections": [],
    }
    monkeypatch.setattr(report, "publish_monitor_v3", lambda **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    snapshot = report.publish_report_monitor_v3(
        out_dir=tmp_path,
        batch={"candidates": [], "events": [{"event_id": "new"}], "rejections": [], "errors": []},
        observed_at="2026-09-10T12:01:00+00:00",
        previous_snapshot=previous,
    )

    assert snapshot["tokens"] == previous["tokens"]
    assert snapshot["observed_at"] == previous["observed_at"]
    assert snapshot["monitor_status"] == "degraded"
    assert snapshot["source_health"]["gmgn"] == {"status": "ok"}
    assert snapshot["source_health"]["persistence"]["status"] == "error"
    assert snapshot["source_health"]["persistence"]["error"] == "disk full"


def test_degraded_publish_recovers_previous_report_tokens_when_state_is_missing(tmp_path):
    event = {
        "schema_version": 1, "event_id": "retained-event", "chain": "bsc",
        "contract_address": "0x6666666666666666666666666666666666666666",
        "provider_family": "gmgn", "provider_feed": "gmgn_trenches",
        "event_type": "new_launch", "event_at": "2026-09-10T12:00:00+00:00",
        "observed_at": "2026-09-10T12:00:01+00:00", "evidence_role": "discovery",
        "signal_lane": "new_launch", "counts_for_resonance": True,
        "provider_event_id": "retained-1", "source_url": None,
        "raw_fingerprint": "retained-raw", "upstream_provider": None,
        "event_time_basis": "event_at", "payload": {},
    }
    previous = monitor.update_monitor_state(
        None,
        events=[event], candidates=[], rejections=[],
        source_health={
            "gmgn": {"status": "ok", "observed_at": "2026-09-10T12:00:01+00:00"},
            "onchain": {"status": "ok", "observed_at": "2026-09-10T12:00:01+00:00"},
        },
        observed_at="2026-09-10T12:00:01+00:00",
    )
    batch = {
        "candidates": [], "events": [], "rejections": [], "feed_counts": {},
        "errors": [{"source": "gmgn_trenches", "category": "timeout", "message": "request timed out"}],
    }

    snapshot = report.publish_report_monitor_v3(
        out_dir=tmp_path,
        batch=batch,
        observed_at="2026-09-10T12:01:00+00:00",
        previous_snapshot=previous,
    )

    assert [token["id"] for token in snapshot["tokens"]] == [
        "bsc:0x6666666666666666666666666666666666666666"
    ]
    assert snapshot["source_health"]["gmgn"]["status"] == "error"
    assert snapshot["source_health"]["onchain"]["status"] == "ok"


def test_structured_source_error_degrades_only_its_provider():
    batch = {
        "events": [{"provider_family": "onchain", "event_at": "2026-09-10T12:00:00+00:00"}],
        "errors": [{"source": "gmgn_trenches", "category": "timeout", "message": "request timed out"}],
    }

    health = report.monitor_source_health(batch, "2026-09-10T12:00:01+00:00")

    assert health["gmgn"]["status"] == "error"
    assert health["onchain"]["status"] == "ok"
    assert "meme_sources" not in health


def test_heat_metrics_rewards_boost_ads_cto_and_gmgn_sources():
    item = {
        "sources": ["boost_top", "ads_latest", "community_takeover", "gmgn_trending"],
        "profile": {"totalAmount": 250},
    }

    metrics = report.heat_metrics(item)

    assert metrics["heat_score"] >= 55
    assert "boost_top" in metrics["heat_flags"]
    assert "ads_latest" in metrics["heat_flags"]
    assert "community_takeover" in metrics["heat_flags"]
    assert "gmgn_trending" in metrics["heat_flags"]


def test_fetch_gmgn_sources_normalizes_common_payload(monkeypatch):
    monkeypatch.setenv("GMGN_TRENDING_URL", "https://example.invalid/gmgn")
    monkeypatch.setenv("GMGN_CLI_DISABLE", "1")

    def fake_http_json(url, **kwargs):
        assert url == "https://example.invalid/gmgn"
        return {
            "data": {
                "tokens": [
                    {
                        "chain": "bsc",
                        "token_address": "0x1111111111111111111111111111111111111111",
                        "symbol": "SOLX",
                        "score": 88,
                    }
                ]
            }
        }

    monkeypatch.setattr(report.fetch_gmgn_sources.__globals__["alpha"], "http_json", fake_http_json)

    sources, errors = report.fetch_gmgn_sources(limit=10)

    assert errors == []
    row = sources["bsc:0x1111111111111111111111111111111111111111"]
    assert row["chainId"] == "bsc"
    assert row["tokenAddress"] == "0x1111111111111111111111111111111111111111"
    assert row["sources"] == ["gmgn_trending"]
    assert row["profile"]["gmgn_score"] == 88


def test_fetch_gmgn_sources_preserves_smart_money_kol_and_potential(monkeypatch):
    monkeypatch.setenv("GMGN_TRENDING_URL", "https://example.invalid/gmgn")
    monkeypatch.setenv("GMGN_CLI_DISABLE", "1")

    def fake_http_json(url, **kwargs):
        assert url == "https://example.invalid/gmgn"
        return {
            "data": {
                "rank": [
                    {
                        "chain": "bsc",
                        "address": "MintA",
                        "symbol": "Z",
                        "score": 92,
                        "smart_money": 36,
                        "kol": 16,
                        "holder_count": 1183,
                        "potential": "10x+",
                    }
                ]
            }
        }

    monkeypatch.setattr(report.fetch_gmgn_sources.__globals__["alpha"], "http_json", fake_http_json)

    sources, errors = report.fetch_gmgn_sources(limit=10)

    assert errors == []
    row = sources["bsc:minta"]
    assert row["profile"]["smart_money"] == 36
    assert row["profile"]["kol"] == 16
    assert row["profile"]["holder_count"] == 1183
    assert row["profile"]["potential"] == "10x+"


def test_load_meme_candidates_outputs_multi_source_labels_and_gmgn_fields(monkeypatch):
    monkeypatch.setitem(
        report.load_meme_candidates.__globals__,
        "fetch_profile_sources",
        lambda limit, observed_at=None: (
            {
                "bsc:minta": {
                    "chainId": "bsc",
                    "tokenAddress": "MintA",
                    "sources": ["gmgn_trending", "boost_top"],
                    "profile": {
                        "totalAmount": 120,
                        "smart_money": 36,
                        "kol": 16,
                        "holder_count": 1183,
                        "potential": "10x+",
                    },
                }
            },
            [],
        ),
    )
    monkeypatch.setitem(
        report.load_meme_candidates.__globals__,
        "fetch_pairs_for_source",
        lambda item: [
            {
                "chainId": "bsc",
                "url": "https://dexscreener.com/bsc/MintA",
                "baseToken": {"symbol": "Z", "name": "Gen Z Meme", "address": "MintA"},
                "marketCap": 1_430_000,
                "priceUsd": "0.00143",
                "liquidity": {"usd": 127_500},
                "volume": {"h24": 66_700},
                "priceChange": {"m5": 9.3, "h1": 51.8, "h24": 88},
                "txns": {"h24": {"buys": 300, "sells": 280}},
                "pairCreatedAt": report.alpha.utc_now_ms() - 2 * 60 * 60 * 1000,
                "info": {"socials": [{"url": "https://x.com/genz"}]},
            }
        ],
    )

    rows, errors = report.load_meme_candidates(limit=10, concurrency=1)

    assert errors == []
    assert rows[0]["symbol"] == "Z"
    assert rows[0]["source_labels"] == ["GMGN", "DS", "Alpha_AI"]
    assert rows[0]["source_count"] == 2  # The local AI label is not an independent market source.
    assert rows[0]["smart_money"] == 36
    assert rows[0]["kol"] == 16
    assert rows[0]["holders"] == 1183
    assert rows[0]["potential_label"] == "10x+"


def test_cap_score_limits_meme_score_to_100():
    assert report.cap_score(119.2) == 100.0
    assert report.cap_score(-5) == 0.0


def test_holder_coverage_summary_counts_unique_tokens_with_holder_data():
    rows = [
        {"chain": "base", "symbol": "AAA", "contract_address": "0x1", "top10_holder_pct": 51.2},
        {"chain": "base", "symbol": "AAA", "contract_address": "0x1", "top10_holder_pct": 51.2},
        {"chain": "bsc", "symbol": "BBB", "contract_address": "0x2", "top10_holder_pct": None},
        {"chain": "solana", "symbol": "CCC", "token_address": "So111", "top10_holder_pct": 12.3},
    ]

    summary = report.holder_coverage_summary(rows)

    assert summary == {
        "unique_tokens": 3,
        "with_top10": 2,
        "coverage_pct": 66.67,
        "sources": {"unknown": 2},
    }


def test_apply_solana_holder_metrics_to_meme_rows_enriches_top_solana_rows(monkeypatch):
    calls = []

    def fake_fetch_solana_holder_metrics(mint):
        calls.append(mint)
        return {
            "top10_holder_pct": 42.5,
            "top20_holder_pct": 55.0,
            "max_holder_pct": 12.0,
            "holder_contract_count": 0,
            "holder_source": "holderlist",
        }

    monkeypatch.setattr(
        report.apply_solana_holder_metrics_to_meme_rows.__globals__["alpha"],
        "fetch_solana_holder_metrics",
        fake_fetch_solana_holder_metrics,
    )
    rows = [
        {"symbol": "AAA", "chain": "solana", "contract_address": "MintA"},
        {"symbol": "BBB", "chain": "bsc", "contract_address": "0x2"},
    ]

    errors = report.apply_solana_holder_metrics_to_meme_rows(rows, limit=3)

    assert errors == []
    assert calls == ["MintA"]
    assert rows[0]["top10_holder_pct"] == 42.5
    assert rows[0]["holder_source"] == "solana_rpc_holderlist"
    assert "top10_holder_pct" not in rows[1]


def test_apply_holder_metrics_to_meme_rows_enriches_evm_rows(monkeypatch):
    calls = []

    monkeypatch.setattr(
        report.apply_holder_metrics_to_meme_rows.__globals__["alpha"],
        "fetch_top_holders",
        lambda provider, chain_id, contract_address, offset, api_key: calls.append((provider, chain_id, contract_address, offset, api_key))
        or [
            {"TokenHolderQuantity": "250"},
            {"TokenHolderQuantity": "150"},
            {"TokenHolderQuantity": "100"},
        ],
    )

    rows = [
        {
            "symbol": "BASEMEME",
            "chain": "base",
            "chain_id": "base",
            "contract_address": "0xbase",
            "price_usd": 1,
            "mcap": 1_000,
        },
        {
            "symbol": "SOLMEME",
            "chain": "solana",
            "chain_id": "solana",
            "contract_address": "MintA",
        },
    ]

    errors = report.apply_holder_metrics_to_meme_rows(rows, limit=1, holder_provider="auto", holder_offset=25)

    assert errors == []
    assert calls == [("blockscout", "8453", "0xbase", 25, "")]
    assert rows[0]["top10_holder_pct"] == 50.0
    assert rows[0]["holder_source"] == "blockscout_holderlist"
    assert rows[0]["holder_chain_id"] == "8453"
    assert "top10_holder_pct" not in rows[1]


def test_build_report_keeps_dealer_radar_scoped_to_binance_alpha(monkeypatch, tmp_path):
    class FakeRadarRow:
        def __init__(self, payload):
            self.payload = payload

        def as_dict(self):
            return dict(self.payload)

    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(
        report,
        "build_gold_backtest",
        lambda replay_history, gold_watch_state, *, now_iso: {
            "best_strategy": {"name": "first_discovery_probe", "label": "首次发现小仓"},
            "strategies": [{"name": "first_discovery_probe", "summary": {"count": 1}}],
        },
        raising=False,
    )
    monkeypatch.setattr(
        report.alpha,
        "run_scan",
        lambda args: (
            [
                FakeRadarRow(
                    {
                        "symbol": "ALPHAONLY",
                        "chain": "base",
                        "contract_address": "0xalpha",
                        "market_cap": 10_000_000,
                        "alpha_volume24h": 5_000_000,
                        "top10_holder_pct": 58.0,
                    }
                )
            ],
            {"filtered_count": 1, "errors": []},
        ),
    )
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                        "symbol": "MEMEONLY",
                        "name": "Grok Dog AI",
                    "chain": "solana",
                    "contract_address": "meme",
                    "score": 82,
                    "heat_score": 45,
                    "mcap": 5_000_000,
                    "volume24h": 3_000_000,
                    "liquidity": 20_000,
                    "change_h1": 40,
                        "txns24h": 380,
                        "pair_age_hours": 4,
                            "smart_money": 26,
                            "kol": 8,
                            "holders": 900,
                            "source_labels": ["GMGN", "DS", "Alpha_AI"],
                            "source_count": 3,
                            "potential_label": "10x+",
                    }
                ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            dealer_top=10,
        )
    )

    assert [row["symbol"] for row in payload["dealer_rows"]] == ["ALPHAONLY"]
    assert payload["meme_rows"][0]["symbol"] == "MEMEONLY"
    assert payload["meme_potential_rows"][0]["symbol"] == "MEMEONLY"
    assert payload["meme_potential_rows"][0]["recommendation_action"] == "可小仓试探"
    assert payload["meta"]["meme_potential_count"] == 1
    assert payload["meta"]["holder_coverage"]["unique_tokens"] == 1


def test_build_report_applies_tweet_narrative_seeds_to_meme_candidates(monkeypatch, tmp_path):
    tweets_path = tmp_path / "alpha-narrative-tweets.json"
    tweets_path.write_text(
        json.dumps(
            [
                {
                    "account": "binance",
                    "text": "Orange cat energy on BNB today. Happy community.",
                    "created_at": "2026-08-20T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ALPHA_NARRATIVE_TWEETS_FILE", str(tweets_path))
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                    "symbol": "OCAT",
                    "name": "Orange Cat",
                    "chain": "bsc",
                    "contract_address": "0xcat",
                    "score": 70,
                    "mcap": 80_000,
                    "volume24h": 60_000,
                    "liquidity": 20_000,
                    "pair_age_hours": 1,
                    "smart_money": 18,
                    "kol": 4,
                    "holders": 700,
                },
                {
                    "symbol": "PLAIN",
                    "name": "Plain Token",
                    "chain": "bsc",
                    "contract_address": "0xplain",
                    "score": 70,
                    "mcap": 80_000,
                    "volume24h": 60_000,
                    "liquidity": 20_000,
                    "pair_age_hours": 1,
                },
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            meme_potential_top=2,
            meme_shadow_top=0,
            dealer_top=10,
            gold_watch_confirmations=3,
            live_stage_enable=False,
        )
    )

    ocat = next(row for row in payload["meme_rows"] if row["symbol"] == "OCAT")
    plain = next(row for row in payload["meme_rows"] if row["symbol"] == "PLAIN")
    assert ocat["tweet_narrative_score"] > 0
    assert ocat["sentiment_trigger"]["keyword"] == "cat"
    assert plain.get("tweet_narrative_score", 0) == 0
    assert payload["meta"]["tweet_narrative"]["seed_count"] >= 1


def test_build_report_applies_offchain_meme_seed_terms_to_bsc_launchpad_candidates(monkeypatch, tmp_path):
    seed_path = tmp_path / "meme-seed-terms.json"
    seed_path.write_text(
        json.dumps(
            {
                "meme_seed_terms": [
                    {
                        "keyword": "MoonBiscuit",
                        "aliases": ["MBIS"],
                        "evidence_posts": [{}, {}, {}, {}],
                        "communities": ["gaming", "food"],
                        "trend": "accelerating",
                        "crypto_discovered": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SEEDS_FILE", raising=False)
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                    "symbol": "MBIS",
                    "name": "MoonBiscuit",
                    "chain": "bsc",
                    "sources": ["flap_launchpad", "gmgn_realtime"],
                    "contract_address": "0xmbis",
                    "score": 70,
                    "mcap": 80_000,
                    "volume24h": 60_000,
                    "liquidity": 20_000,
                    "pair_age_hours": 1,
                },
                {
                    "symbol": "MBIS",
                    "name": "MoonBiscuit",
                    "chain": "solana",
                    "sources": ["flap_launchpad", "gmgn_realtime"],
                    "contract_address": "SolMint",
                    "score": 70,
                    "mcap": 80_000,
                    "volume24h": 60_000,
                    "liquidity": 20_000,
                    "pair_age_hours": 1,
                },
                {
                    "symbol": "MBIS",
                    "name": "MoonBiscuit",
                    "chain": "bsc",
                    "sources": ["dexscreener_profile"],
                    "contract_address": "0xprofile",
                    "score": 70,
                    "mcap": 80_000,
                    "volume24h": 60_000,
                    "liquidity": 20_000,
                    "pair_age_hours": 1,
                },
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            meme_potential_top=3,
            meme_shadow_top=0,
            dealer_top=10,
            gold_watch_confirmations=3,
            live_stage_enable=False,
        )
    )

    launchpad = next(row for row in payload["meme_rows"] if row["contract_address"] == "0xmbis")
    solana = next(row for row in payload["meme_rows"] if row["contract_address"] == "SolMint")
    profile = next(row for row in payload["meme_rows"] if row["contract_address"] == "0xprofile")
    assert launchpad["meme_seed_candidate"] is True
    assert launchpad["meme_seed_terms"] == ["MoonBiscuit"]
    assert launchpad["meme_seed_score"] > 0
    assert solana["meme_seed_candidate"] is False
    assert profile["meme_seed_candidate"] is False
    assert payload["meta"]["meme_seed_terms"]["seed_count"] == 1
    assert payload["meta"]["meme_seed_terms"]["top_seed"]["keyword"] == "MoonBiscuit"


def test_build_report_outputs_separate_meme_holder_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {"symbol": "AAA", "chain": "base", "contract_address": "0x1", "top10_holder_pct": 31.5},
                {"symbol": "BBB", "chain": "solana", "contract_address": "MintB"},
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            dealer_top=10,
        )
    )

    assert payload["meta"]["meme_holder_coverage"]["unique_tokens"] == 2
    assert payload["meta"]["meme_holder_coverage"]["with_top10"] == 1
    assert payload["meta"]["meme_holder_coverage"]["coverage_pct"] == 50.0


def test_build_report_includes_optional_meme_source_status(monkeypatch, tmp_path):
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(report, "load_meme_candidates", lambda limit, concurrency: ([], []))
    monkeypatch.setenv("GMGN_TRENCHES_URLS", "https://example.invalid/gmgn")
    monkeypatch.setenv("BIRDEYE_API_KEY", "birdeye-key")

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            meme_potential_top=8,
            dealer_top=10,
        )
    )

    status = payload["meta"]["meme_source_status"]
    assert status["gmgn_trenches"]["enabled"] is True
    assert status["birdeye"]["enabled"] is True
    assert status["birdeye"]["url_count"] == 2
    assert status["birdeye"]["key_present"] is True


def test_build_report_can_use_cached_meme_rows_without_fetching_sources(monkeypatch, tmp_path):
    previous = {
        "meta": {},
        "meme_rows": [
            {
                "symbol": "CACHE",
                "name": "Cached Meme",
                "chain": "bsc",
                "contract_address": "0xcache",
                "token_address": "0xcache",
                "mcap": 80_000,
                "market_cap": 80_000,
                "liquidity": 20_000,
                "volume24h": 200_000,
                "pair_age_hours": 2,
                "score": 88,
                "gold_dog_conviction_score": 78,
            }
        ],
    }
    (tmp_path / "alpha-radar-report-latest.json").write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setenv("ALPHA_MEME_CACHE_ONLY", "1")
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))

    def fail_load_meme_candidates(limit, concurrency):
        raise AssertionError("cache-only full refresh must not fetch Meme sources")

    monkeypatch.setattr(report, "load_meme_candidates", fail_load_meme_candidates)

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            meme_potential_top=1,
            meme_shadow_top=40,
            dealer_top=10,
        )
    )

    assert payload["meme_rows"][0]["symbol"] == "CACHE"
    assert payload["meta"]["used_previous_meme"] is True
    assert payload["meta"]["section_quality"]["meme"] == "cached"


def test_build_report_outputs_chinese_recommendation_rows(monkeypatch, tmp_path):
    class FakeRadarRow:
        def __init__(self, payload):
            self.payload = payload

        def as_dict(self):
            return dict(self.payload)

    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(
        report.alpha,
        "run_scan",
        lambda args: (
            [
                FakeRadarRow(
                    {
                        "symbol": "LEAD",
                        "chain": "base",
                        "futures_symbol": "LEADUSDT",
                        "score": 82,
                        "market_cap": 45_000_000,
                        "alpha_volume24h": 16_000_000,
                        "oi_change_1h_pct": 8.2,
                        "funding_rate_pct": 0.018,
                        "alpha_change24h_pct": 12,
                        "pair_age_hours": 72,
                    }
                )
            ],
            {"filtered_count": 1, "errors": []},
        ),
    )
    monkeypatch.setattr(report, "load_meme_candidates", lambda limit, concurrency: ([], []))

    def fake_attach_live_stage_profiles(rows):
        for row in rows:
            if row.get("symbol") == "LEAD":
                row.update(
                    {
                        "stage": "早期试多",
                        "direction": "做多",
                        "action": "做多-小仓试",
                        "reason": "量先动价格被托",
                        "key_level": 1.2,
                        "invalid_level": 0.9,
                        "holding_text": "LEAD 多单小仓，破0.9走",
                        "protection_text": "冲2%-4%先减，破0.9走",
                        "alpha_stage": "early_long",
                        "alpha_stage_label": "早期试多",
                    }
                )

    monkeypatch.setattr(report, "attach_live_stage_profiles", fake_attach_live_stage_profiles)

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            dealer_top=10,
        )
    )

    assert payload["recommendation_rows"][0]["symbol"] == "LEAD"
    assert payload["recommendation_rows"][0]["recommendation_label"] == "数据待补"
    assert payload["recommendation_rows"][0]["stage"] == "早期试多"
    assert payload["recommendation_rows"][0]["direction"] == "做多"
    assert payload["recommendation_rows"][0]["recommendation_action"] == "仅观察"
    assert payload["recommendation_rows"][0]["key_level"] == 1.2
    assert payload["recommendation_rows"][0]["holding_text"]
    assert payload["meta"]["recommendation_count"] == 1


def test_attach_stage_profile_does_not_create_action_without_full_futures_context():
    rows = [
        {
            "symbol": "NOFUT",
            "market_cap": 20_000_000,
            "alpha_volume24h": 10_000_000,
            "top10_holder_pct": 80.0,
            "oi_change_1h_pct": None,
            "funding_rate_pct": None,
        }
    ]

    report.attach_stage_profile(rows, enable_live_stage=True)

    assert rows[0]["stage"] == "高控盘风险"
    assert rows[0]["direction"] == "观察"
    assert rows[0]["action"] == ""
    assert rows[0]["holding_text"] == ""
    assert rows[0]["protection_text"] == ""


def test_attach_stage_profile_ignores_silent_stage_as_trade_action(monkeypatch):
    rows = [
        {
            "symbol": "SILENT",
            "futures_symbol": "SILENTUSDT",
            "market_cap": 20_000_000,
            "alpha_volume24h": 10_000_000,
            "oi_change_1h_pct": 1.2,
            "funding_rate_pct": 0.01,
        }
    ]

    def fake_attach_live_stage_profiles(items):
        items[0].update(
            {
                "stage": "静默蓄势",
                "direction": "观察",
                "action": "",
                "reason": "横盘放量低点稳",
                "alpha_stage": "silent_build",
                "alpha_stage_label": "静默蓄势",
            }
        )

    monkeypatch.setattr(report, "attach_live_stage_profiles", fake_attach_live_stage_profiles)
    report.attach_stage_profile(rows, enable_live_stage=True)

    assert rows[0]["stage"] == "静默蓄势"
    assert rows[0]["action"] == ""
    assert rows[0]["direction"] == "观察"


def test_build_report_uses_separate_small_meme_potential_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                    "symbol": f"MEME{i}",
                    "score": 90 - i,
                    "heat_score": 45,
                    "mcap": 4_000_000,
                    "liquidity": 100_000,
                    "volume24h": 2_000_000,
                    "change_h1": 35,
                    "txns24h": 600,
                        "pair_age_hours": 1,
                }
                for i in range(10)
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=40,
            meme_potential_top=3,
            dealer_top=10,
        )
    )

    assert [row["symbol"] for row in payload["meme_potential_rows"]] == ["MEME0", "MEME1", "MEME2"]
    assert payload["meta"]["meme_potential_count"] == 3
    assert payload["meta"]["meme_count"] == 10


def test_parse_args_defaults_to_one_gold_dog_pick(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["alpha_radar_report.py"])

    args = report.parse_args()

    assert args.meme_potential_top == 1


def test_build_report_defaults_to_single_gold_dog_pick(monkeypatch, tmp_path):
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                    "symbol": "TRUEGOLD",
                    "score": 68,
                    "heat_score": 28,
                    "mcap": 260_000,
                    "liquidity": 58_000,
                    "volume24h": 360_000,
                    "change_m5": 4,
                    "change_h1": 26,
                    "txns24h": 900,
                    "pair_age_hours": 4,
                    "smart_money": 34,
                    "kol": 8,
                    "holders": 760,
                    "top10_holder_pct": 19,
                    "source_labels": ["GMGN", "DS", "Alpha_AI"],
                    "source_count": 3,
                    "gold_dog_conviction_score": 88,
                },
                {
                    "symbol": "HOTNOISE",
                    "score": 92,
                    "heat_score": 70,
                    "mcap": 5_000_000,
                    "liquidity": 500_000,
                    "volume24h": 7_000_000,
                    "change_m5": 12,
                    "change_h1": 70,
                    "txns24h": 5000,
                    "pair_age_hours": 20,
                    "smart_money": 8,
                    "kol": 1,
                    "holders": 5000,
                    "top10_holder_pct": 38,
                },
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
                meme_source_limit=10,
                meme_top=40,
                dealer_top=10,
                gold_watch_confirmations=1,
            )
        )

    assert [row["symbol"] for row in payload["meme_potential_rows"]] == ["TRUEGOLD"]
    assert payload["gold_dog_pick"] is None  # Score alone has no fresh evidence timestamp.
    assert payload["meta"]["meme_potential_count"] == 1


def test_build_report_exposes_early_gold_watch_pick_and_first_mcap(monkeypatch, tmp_path):
    previous_state = {
        "candidates": {
            "bsc:0xearly": {
                "key": "bsc:0xearly",
                "symbol": "EARLY",
                "chain": "bsc",
                "contract_address": "0xearly",
                    "first_seen_at": "2026-08-14T16:00:00+08:00",
                    "scan_count": 1,
                    "strong_scan_count": 1,
                    "consecutive_strong_scan_count": 1,
                    "lifetime_strong_scan_count": 1,
                    "first_seen_mcap": 28_000,
                    "max_seen_mcap": 28_000,
            }
        }
    }
    (tmp_path / "alpha-gold-watch-state.json").write_text(json.dumps(previous_state), encoding="utf-8")
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                    "symbol": "EARLY",
                    "chain": "bsc",
                    "contract_address": "0xearly",
                    "score": 82,
                    "heat_score": 40,
                    "mcap": 84_000,
                    "liquidity": 40_000,
                    "volume24h": 180_000,
                    "change_m5": 6,
                    "change_h1": 80,
                    "txns24h": 900,
                    "pair_age_hours": 2,
                    "smart_money": 32,
                    "kol": 8,
                    "holders": 620,
                    "top10_holder_pct": 18,
                    "source_labels": ["GMGN", "DS"],
                    "source_count": 2,
                    "gold_dog_conviction_score": 90,
                },
                {
                    "symbol": "LATE",
                    "chain": "bsc",
                    "contract_address": "0xlate",
                    "score": 98,
                    "heat_score": 70,
                    "mcap": 1_300_000,
                    "liquidity": 180_000,
                    "volume24h": 2_000_000,
                    "change_m5": 12,
                    "change_h1": 350,
                    "txns24h": 6000,
                    "pair_age_hours": 18,
                    "smart_money": 60,
                    "kol": 20,
                    "holders": 4000,
                    "top10_holder_pct": 20,
                    "source_labels": ["GMGN", "DS"],
                    "source_count": 2,
                    "gold_dog_conviction_score": 96,
                },
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=40,
            dealer_top=10,
        )
    )

    early_row = next(row for row in payload["meme_potential_rows"] + payload["meme_shadow_rows"] if row["symbol"] == "EARLY")
    assert payload["gold_watch_early_pick"] is None  # Old scan counters cannot confirm cached data.
    assert early_row["watch_first_seen_mcap"] == 28_000
    assert early_row["watch_mcap_multiple_from_first"] == 3


def test_shadow_meme_rows_skip_market_data_pending_launchpad_rows():
    rows = [
        {
            "symbol": "WAIT",
            "name": "Four.meme WAIT",
            "chain": "bsc",
            "contract_address": "0x1111111111111111111111111111111111111111",
            "score": 48,
            "heat_score": 48,
            "mcap": 0,
            "liquidity": 0,
            "volume24h": 0,
            "txns24h": 0,
            "pair_age_hours": 0.001,
            "sources": ["fourmeme_launchpad"],
            "source_labels": ["Four.meme"],
            "source_count": 1,
            "market_data_pending": True,
        },
        {
            "symbol": "LIVE",
            "name": "Live BSC Dog",
            "chain": "bsc",
            "contract_address": "0x2222222222222222222222222222222222222222",
            "score": 72,
            "heat_score": 40,
            "mcap": 120_000,
            "liquidity": 35_000,
            "volume24h": 180_000,
            "change_h1": 15,
            "txns24h": 300,
            "pair_age_hours": 3,
            "top10_holder_pct": 18,
            "sources": ["gmgn_live_trending", "profile_latest"],
            "source_labels": ["GMGN", "DS"],
            "source_count": 2,
        },
    ]

    shadow = report.build_shadow_meme_rows(rows, selected_rows=[], limit=5, replay_calibration=None, chain_scope="bsc")

    assert [row["symbol"] for row in shadow] == ["LIVE"]


def test_shadow_meme_rows_keep_low_score_noxa_launchpad_rows_visible():
    rows = [
        {
            "symbol": f"HIGH{i}",
            "name": f"High Score {i}",
            "chain": "robinhood",
            "contract_address": f"0x{i:040x}",
            "score": 80 - i,
            "heat_score": 40,
            "mcap": 500_000 + i,
            "liquidity": 80_000,
            "volume24h": 200_000,
            "change_h1": 8,
            "txns24h": 400,
            "pair_age_hours": 2,
            "top10_holder_pct": 20,
            "sources": ["gmgn_live_trending", "profile_latest"],
            "source_labels": ["GMGN", "DS"],
            "source_count": 2,
        }
        for i in range(5)
    ]
    rows.append(
        {
            "symbol": "BID",
            "name": "PocketBid",
            "chain": "robinhood",
            "contract_address": "0x397de4ed08b189039ba732774e677cbf549ac63b",
            "score": 34,
            "heat_score": 40,
            "mcap": 198_929,
            "market_cap": 198_929,
            "liquidity": 41_654,
            "volume24h": 61_000,
            "change_h1": 12,
            "txns24h": 120,
            "pair_age_hours": 0.2,
            "holders": 209,
            "top10_holder_pct": 0,
            "sources": ["noxa_launchpad"],
            "source_labels": ["Noxa", "DS"],
            "source_count": 1,
        }
    )

    shadow = report.build_shadow_meme_rows(rows, selected_rows=[], limit=3, replay_calibration=None, chain_scope="robinhood")

    assert "BID" in [row["symbol"] for row in shadow]
    noxa = next(row for row in shadow if row["symbol"] == "BID")
    assert noxa["recommendation_action"] == "Noxa 观察"
    assert noxa["recommendation_reason"].startswith("Noxa 发射源保底观察")


def test_build_report_tracks_shadow_meme_rows_without_showing_extra_picks(monkeypatch, tmp_path):
    quote_observed_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                    "symbol": "TRUEGOLD",
                    "chain": "solana",
                    "contract_address": "MintTrue",
                        "price_usd": 0.001,
                        "quote_status": "fresh",
                        "quote_observed_at": quote_observed_at,
                    "score": 70,
                    "heat_score": 30,
                    "mcap": 220_000,
                    "liquidity": 60_000,
                    "volume24h": 380_000,
                    "change_m5": 4,
                    "change_h1": 28,
                    "txns24h": 920,
                    "pair_age_hours": 5,
                    "smart_money": 36,
                    "kol": 9,
                    "holders": 780,
                    "top10_holder_pct": 18,
                    "source_labels": ["GMGN", "DS", "Alpha_AI"],
                    "source_count": 3,
                    "gold_dog_conviction_score": 90,
                },
                {
                    "symbol": "LATERGOLD",
                    "chain": "solana",
                    "contract_address": "MintLater",
                        "price_usd": 0.002,
                        "quote_status": "fresh",
                        "quote_observed_at": quote_observed_at,
                    "score": 58,
                    "heat_score": 24,
                    "mcap": 280_000,
                    "liquidity": 55_000,
                    "volume24h": 330_000,
                    "change_m5": 3,
                    "change_h1": 16,
                    "txns24h": 710,
                    "pair_age_hours": 7,
                    "smart_money": 28,
                    "kol": 7,
                    "holders": 640,
                    "top10_holder_pct": 24,
                    "source_labels": ["GMGN", "DS", "Birdeye"],
                    "source_count": 3,
                    "gold_dog_conviction_score": 78,
                },
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            dealer_top=10,
        )
    )

    assert [row["symbol"] for row in payload["meme_potential_rows"]] == ["TRUEGOLD"]
    assert payload["meta"]["meme_shadow_count"] == 1
    history = json.loads((tmp_path / "alpha-radar-replay-history.json").read_text(encoding="utf-8"))
    shadow = history["rows"]["solana:mintlater"]
    assert shadow["recommendation_bucket"] == "shadow"
    assert shadow["recommendation_action"] == "影子观察"
    assert shadow["first_snapshot"]["shadow_original_bucket"] == "ambush"
    assert shadow["first_snapshot"]["backtest_rule_score"] > 0


def test_build_report_updates_replay_history_summary(monkeypatch, tmp_path):
    quote_observed_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    class FakeRadarRow:
        def __init__(self, payload):
            self.payload = payload

        def as_dict(self):
            return dict(self.payload)

    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(
        report.alpha,
        "run_scan",
        lambda args: (
            [
                FakeRadarRow(
                    {
                        "symbol": "LEAD",
                        "chain": "base",
                        "contract_address": "0xlead",
                        "score": 82,
                            "price_usd": 1.0,
                            "quote_status": "fresh",
                            "quote_observed_at": quote_observed_at,
                        "market_cap": 45_000_000,
                        "alpha_volume24h": 16_000_000,
                        "oi_change_1h_pct": 8.2,
                        "funding_rate_pct": 0.018,
                        "alpha_change24h_pct": 12,
                        "pair_age_hours": 72,
                    }
                )
            ],
            {"filtered_count": 1, "errors": []},
        ),
    )
    monkeypatch.setattr(report, "load_meme_candidates", lambda limit, concurrency: ([], []))

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            dealer_top=10,
        )
    )

    assert (tmp_path / "alpha-radar-replay-history.json").exists()
    assert payload["meta"]["replay"]["tracked_count"] == 1
    assert "best_hits" in payload["meta"]["replay_boards"]
    assert "worst_misses" in payload["meta"]["replay_boards"]
    assert "risk_avoided" in payload["meta"]["replay_boards"]
    assert "gold_backtest" in payload["meta"]
    assert "strategies" in payload["meta"]["gold_backtest"]
    assert payload["recommendation_rows"][0]["replay"]["first_price_usd"] == 1.0
    assert payload["recommendation_rows"][0]["replay"]["first_mcap_usd"] == 45_000_000


def test_build_report_applies_replay_calibration_to_meme_potential(monkeypatch, tmp_path):
    history = {
        "summary": {
            "by_action": {
                "可小仓试探": {
                    "count": 12,
                    "hit_rate_pct": 16.67,
                    "avg_return_1h_pct": -5.8,
                    "horizon_win_rates": {"1h": {"count": 8, "win_rate_pct": 37.5, "avg_return_pct": -5.8}},
                }
            }
        },
        "rows": {},
    }
    (tmp_path / "alpha-radar-replay-history.json").write_text(__import__("json").dumps(history), encoding="utf-8")
    monkeypatch.setattr(report.alpha, "configure_cache", lambda out_dir: None)
    monkeypatch.setattr(report.alpha, "data_quality_summary", lambda: {"overall": "live", "by_source": {}})
    monkeypatch.setattr(report.alpha, "run_scan", lambda args: ([], {"filtered_count": 0, "errors": []}))
    monkeypatch.setattr(
        report,
        "load_meme_candidates",
        lambda limit, concurrency: (
            [
                {
                    "symbol": "SMART",
                    "score": 72,
                    "heat_score": 32,
                    "mcap": 680_000,
                    "liquidity": 72_000,
                    "volume24h": 1_300_000,
                    "change_m5": 6,
                    "change_h1": 48,
                    "txns24h": 1400,
                    "pair_age_hours": 6,
                    "smart_money": 54,
                    "kol": 12,
                    "holders": 860,
                    "gmgn_risk_flags": [],
                    "top10_holder_pct": 18,
                    "price_usd": 0.001,
                    "chain": "solana",
                    "contract_address": "MintSmart",
                }
            ],
            [],
        ),
    )

    payload = report.build_report(
        Namespace(
            out_dir=str(tmp_path),
            alpha_limit=10,
            top=10,
            max_market_cap=200_000_000,
            min_volume=1_000_000,
            chains="",
            concurrency=1,
            holders_enable=False,
            holder_provider="auto",
            holder_top_tokens=12,
            holder_offset=20,
            risk_enable=False,
            risk_top_tokens=12,
            meme_source_limit=10,
            meme_top=10,
            meme_potential_top=8,
            dealer_top=10,
        )
    )

    row = payload["meme_potential_rows"][0]
    assert row["recommendation_bucket"] == "ambush"
    assert row["gold_dog_conviction_score"] >= 70
    assert row["replay_score_adjustment"] == -12
    assert "回测" in row["recommendation_reason"]
    assert "可小仓试探" in payload["meta"]["replay_action_calibration"]
