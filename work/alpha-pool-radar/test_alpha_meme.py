import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest


BASE = Path(__file__).parent


@pytest.mark.parametrize("chain_field", ("chain", "chain_id", "chainId", "network"))
def test_normalize_external_meme_row_normalizes_arc_chain_id_identity(monkeypatch, chain_field):
    address = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood,arc")

    normalized = meme.normalize_external_meme_row(
        {chain_field: 5042, "address": address},
        "arc_feed",
    )

    assert normalized is not None
    assert f"{normalized['chainId']}:{normalized['tokenAddress']}".lower() == f"arc:{address.lower()}"


@pytest.mark.parametrize("stamp,status", [
    ("2026-09-09T04:00:00+00:00", "fresh"),
    ("2026-09-09T03:59:30+00:00", "fresh"),
    ("2026-09-09T03:59:29+00:00", "stale"),
    ("2026-09-01T00:00:00+00:00", "stale"),
    ("2026-09-09T04:00:01+00:00", "unavailable"),
    ("2026-09-09T04:00:00", "unavailable"),
    (None, "unavailable"),
])
def test_r3_profile_fallback_preserves_observation(monkeypatch, stamp, status):
    now = "2026-09-09T04:00:00+00:00"
    item = {"chainId": "bsc", "tokenAddress": "0x" + "a" * 40,
            "sources": ["wind_monitor"], "profile": {
                "symbol": "TEST", "price": .004, "market_cap": 200_000,
                "liquidity": 20_000, "observed_at": stamp, "fetched_at": now}}
    row = meme.external_candidate_from_profile(item, int(datetime.fromisoformat(now).timestamp() * 1000))
    assert row["quote_status"] == status
    assert row["quote_observed_at"] == stamp


def test_fast_discovery_can_skip_bulk_dex_enrichment(monkeypatch):
    token = "0x" + "f" * 40
    sources = {
        f"bsc:{token}": {
            "chainId": "bsc",
            "tokenAddress": token,
            "sources": ["wind_monitor"],
            "profile": {
                "symbol": "FAST",
                "price": 0.001,
                "market_cap": 50_000,
                "liquidity": 20_000,
                "observed_at": "2026-09-11T04:00:00+00:00",
            },
        }
    }
    monkeypatch.setenv("MEME_SKIP_DEX_ENRICH", "1")
    monkeypatch.setattr(
        meme,
        "fetch_pairs_for_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("bulk Dex called")),
    )

    rows, errors = meme._build_meme_candidates(
        sources,
        [],
        8,
        observed_at="2026-09-11T04:00:00+00:00",
        events=[],
        rejections=[],
    )

    assert errors == []
    assert [row["symbol"] for row in rows] == ["FAST"]


def test_r3_profile_merge_does_not_refresh_preserved_price():
    old = {"price": .004, "market_cap": 200_000, "liquidity": 20_000,
           "observed_at": "2026-09-01T00:00:00+00:00"}
    merged = meme.merge_source_profile(old, {"market_data_pending": True,
        "observed_at": "2026-09-09T04:00:00+00:00", "smart_money": 4})
    row = meme.candidate_from_profile({"chainId": "bsc", "tokenAddress": "0x" + "a" * 40,
        "profile": {**merged, "symbol": "TEST"}}, 1788926400000)
    assert row["quote_status"] != "fresh"
    assert row["quote_observed_at"] == old["observed_at"]


def test_r3_undated_replacement_cannot_inherit_old_quote_time():
    old = {"price": .004, "market_cap": 200_000, "liquidity": 20_000,
           "quote_observed_at": "2026-09-09T04:00:00+00:00", "quote_status": "fresh"}
    merged = meme.merge_source_profile(old, {"price": .001, "market_cap": 50_000})
    row = meme.candidate_from_profile({"chainId": "bsc", "tokenAddress": "0x" + "a" * 40,
        "profile": {**merged, "symbol": "TEST"}}, 1788926400000)
    assert row["quote_status"] == "unavailable"
    assert row["quote_observed_at"] is None


def test_r3_local_inbox_fallback_cannot_create_replay_baseline(monkeypatch, tmp_path):
    from alpha_replay import update_replay_history
    now = "2026-09-09T04:00:00+00:00"
    token = "0x" + "a" * 40
    payload = {"fetched_at": now, "data": [{"chain": "bsc", "address": token,
        "symbol": "TEST", "price": .004, "market_cap": 200_000, "liquidity": 20_000,
        "observed_at": "2026-09-01T00:00:00+00:00"}]}
    inbox = tmp_path / "wind-monitor.json"
    inbox.write_text(json.dumps(payload), encoding="utf-8")
    before = inbox.read_bytes()
    monkeypatch.setattr(meme, "external_file_source_specs", lambda: [(str(inbox), "wind_monitor")])
    monkeypatch.setattr(meme, "external_source_specs", lambda *args: [])
    monkeypatch.setattr(meme, "external_command_source_specs", lambda *args: [])
    sources, errors = meme.fetch_external_meme_sources(1)
    monkeypatch.setattr(
        meme, "fetch_profile_sources", lambda *args, **_kwargs: (sources, errors)
    )
    monkeypatch.setenv("MEME_BATCH_PAIRS", "0")
    monkeypatch.setattr(meme.alpha, "utc_now_ms", lambda: int(datetime.fromisoformat(now).timestamp() * 1000))
    def timeout(*args):
        raise TimeoutError("offline fixture")
    monkeypatch.setattr(meme, "fetch_pairs_for_source", timeout)
    rows, _ = meme.load_meme_candidates(1, 1)
    assert rows[0]["quote_status"] == "stale"
    history = update_replay_history({}, rows, now)
    assert history["rows"] == {}
    fresh = {**rows[0], "price_usd": .001, "mcap": 50_000, "market_cap": 50_000,
             "quote_status": "fresh", "quote_observed_at": now}
    history = update_replay_history(history, [fresh], now)
    assert history["rows"]["bsc:" + token]["first_snapshot"]["mcap"] == 50_000
    assert inbox.read_bytes() == before


@pytest.mark.parametrize("value", [None, 0, -1, "NaN", "Infinity", True])
def test_r7_external_profile_keeps_fdv_separate(value):
    item = meme.normalize_external_meme_row({"chain": "bsc", "address": "0x" + "a" * 40,
        "symbol": "TEST", "price": .001, "liquidity": 20_000,
        "market_cap": value, "fdv": 50_000}, "wind_monitor")
    assert item["profile"]["market_cap"] is None
    row = meme.candidate_from_profile(item, 1788926400000)
    assert row["mcap"] is None and row["market_cap"] is None
    assert row["fdv"] == 50_000
    assert row["market_cap_source"] is None
    assert row["fdv_source"] == "wind_monitor.fdv"
    assert row["valuation_type"] == "fdv"


@pytest.mark.parametrize("valuation_type", ["fdv", "unavailable", "unknown", None, ""])
def test_r7_explicit_fdv_cannot_be_relabelled_market_cap(valuation_type):
    fields = meme.profile_valuation_fields({"market_cap": 50_000, "fdv": 50_000,
                                            "valuation_type": valuation_type}, "test")
    assert fields["mcap"] is None
    assert fields["valuation_type"] == "fdv"


def test_r7_dex_fdv_only_candidate_does_not_reuse_profile_market_cap(monkeypatch):
    token = "0x" + "a" * 40
    source = {"chainId": "bsc", "tokenAddress": token, "sources": ["wind_monitor"],
              "profile": {"symbol": "TEST", "market_cap": 99_000}}
    pair = {"chainId": "bsc", "pairAddress": "0x" + "b" * 40,
            "baseToken": {"address": token, "symbol": "TEST"}, "priceUsd": .001,
            "marketCap": None, "fdv": 50_000, "liquidity": {"usd": 20_000}}
    monkeypatch.setenv("MEME_BATCH_PAIRS", "0")
    monkeypatch.setattr(
        meme, "fetch_profile_sources", lambda *args, **_kwargs: ({token: source}, [])
    )
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda *args: [pair])
    monkeypatch.setattr(meme, "is_meme_like", lambda *args: True)
    rows, errors = meme.load_meme_candidates(1, 1)
    assert not errors
    assert rows[0]["mcap"] is None and rows[0]["market_cap"] is None
    assert rows[0]["fdv"] == 50_000
    assert rows[0]["valuation_type"] == "fdv"
    assert rows[0]["fdv_source"] == "dexscreener.fdv"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


alpha = load_module("alpha_pool_radar", BASE / "alpha_pool_radar.py")
meme = load_module("alpha_meme", BASE / "alpha_meme.py")


@pytest.fixture(autouse=True)
def allow_all_meme_chains_for_legacy_source_tests(monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "*")
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", "1")
    monkeypatch.setattr(meme.alpha_okx_market, "fetch", lambda *args: ([], []))
    monkeypatch.setattr(meme.alpha_okx_market, "persist_snapshot", lambda *args: None)


V3_NOW = "2026-09-10T12:00:00+00:00"
V3_TOKEN = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def v3_source_loader(*rows):
    sources = {}
    for row, source in rows:
        normalized = meme.normalize_external_meme_row(row, source, observed_at=V3_NOW)
        assert normalized is not None
        key = meme.token_key(normalized["chainId"], normalized["tokenAddress"])
        current = sources.setdefault(
            key,
            {
                "chainId": normalized["chainId"],
                "tokenAddress": normalized["tokenAddress"],
                "sources": [],
                "profile": {},
                "source_events": [],
                "audit_facts": [],
                "event_rejections": [],
            },
        )
        meme.merge_source_record(current, normalized)
    return lambda _limit: (sources, ["fixture warning"])


def test_v3_low_liquidity_event_survives_candidate_filter(monkeypatch):
    raw = {
        "chain": "bsc",
        "address": V3_TOKEN,
        "symbol": "THIN",
        "event_at": V3_NOW,
        "provider_event_id": "gmgn-thin-1",
        "market_cap": 8_000,
        "liquidity": 50,
    }
    pair = {
        "chainId": "bsc",
        "pairAddress": "0x1111111111111111111111111111111111111111",
        "pairCreatedAt": 1789041600000,
        "baseToken": {"address": V3_TOKEN, "symbol": "THIN", "name": "Thin Meme"},
        "marketCap": 8_000,
        "liquidity": {"usd": 50},
    }
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [pair])
    monkeypatch.setattr(meme, "is_meme_like", lambda *_args: True)

    batch = meme.load_meme_observation_batch(
        10,
        1,
        v3_source_loader((raw, "gmgn_trenches")),
        observed_at=V3_NOW,
    )

    assert set(batch) == {"candidates", "events", "rejections", "errors", "feed_counts"}
    assert batch["candidates"] == []
    assert "gmgn-thin-1" in [event["provider_event_id"] for event in batch["events"]]
    assert batch["feed_counts"] == {"gmgn_trenches": 1, "pair_detail": 1}
    assert batch["errors"] == ["fixture warning"]


def test_v3_normalizes_every_pair_detail_before_best_pair_selection(monkeypatch):
    source = {
        "chain": "bsc",
        "address": V3_TOKEN,
        "symbol": "MULTI",
        "event_at": V3_NOW,
        "provider_event_id": "gmgn-multi-1",
    }
    low_pair = {
        "chainId": "bsc",
        "pairAddress": "0x1111111111111111111111111111111111111111",
        "pairCreatedAt": 1789041540000,
        "baseToken": {"address": V3_TOKEN, "symbol": "MULTI", "name": "Multi Meme"},
        "marketCap": 40_000,
        "priceUsd": 0.001,
        "liquidity": {"usd": 3_000},
        "volume": {"h24": 5_000},
    }
    high_pair = {
        **low_pair,
        "pairAddress": "0x2222222222222222222222222222222222222222",
        "pairCreatedAt": 1789041600000,
        "liquidity": {"usd": 30_000},
        "volume": {"h24": 50_000},
    }
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [low_pair, high_pair])
    monkeypatch.setattr(meme, "is_meme_like", lambda *_args: True)

    batch = meme.load_meme_observation_batch(
        10,
        1,
        v3_source_loader((source, "gmgn_trenches")),
        observed_at=V3_NOW,
    )

    pair_events = [
        event for event in batch["events"] if event["provider_feed"] == "pair_detail"
    ]
    assert {event["provider_event_id"] for event in pair_events} == {
        low_pair["pairAddress"],
        high_pair["pairAddress"],
    }
    assert batch["feed_counts"]["pair_detail"] == 2
    assert batch["candidates"][0]["pair_address"] == high_pair["pairAddress"]
    assert batch["candidates"][0]["liquidity"] == 30_000


def test_v3_missing_symbol_event_survives_when_contract_identity_is_resolved(monkeypatch):
    raw = {
        "chain": "bsc",
        "address": V3_TOKEN,
        "pool_address": "0x1111111111111111111111111111111111111111",
        "event_at": V3_NOW,
        "provider_event_id": "pair-without-symbol",
        "market_cap": 25_000,
        "liquidity": 9_000,
    }
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [])

    batch = meme.load_meme_observation_batch(
        10,
        1,
        v3_source_loader((raw, "fourmeme_launchpad")),
        observed_at=V3_NOW,
    )

    assert batch["candidates"] == []
    assert len(batch["events"]) == 1
    assert batch["events"][0]["contract_address"] == V3_TOKEN
    assert batch["events"][0]["evidence_role"] == "discovery"


@pytest.mark.parametrize(
    ("source", "family", "role", "lane"),
    [
        ("gmgn_trending", "gmgn", "ranking", "trending"),
        ("okx_trenches", "okx", "discovery", "new_launch"),
        ("985_monitor", "985", "wallet", "smart_money"),
        ("debot_signal", "debot", "market", "market_flow"),
        ("wind_monitor", "wind", "ranking", "kol_social"),
        ("bsc_onchain", "onchain", "discovery", "new_launch"),
        ("boost_top", "dexscreener", "ranking", "trending"),
    ],
)
def test_v3_source_rows_map_to_task_1_contract(source, family, role, lane):
    raw = {
        "chain": "bsc",
        "address": V3_TOKEN,
        "event_at": V3_NOW,
        "provider_event_id": f"{source}-1",
        "lookup_by_ca": False,
    }

    normalized = meme.normalize_external_meme_row(raw, source, observed_at=V3_NOW)

    assert normalized["source_events"][0]["provider_family"] == family
    assert normalized["source_events"][0]["evidence_role"] == role
    assert normalized["source_events"][0]["signal_lane"] == lane


def test_v3_duplicate_source_rows_keep_one_stable_event_and_audit_fact(monkeypatch):
    raw = {
        "chain": "bsc",
        "address": V3_TOKEN,
        "symbol": "DUPE",
        "event_at": V3_NOW,
        "provider_event_id": "same-event",
        "source_url": "https://example.test/events/same-event",
        "is_honeypot": False,
    }
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [])

    batch = meme.load_meme_observation_batch(
        10,
        1,
        v3_source_loader((raw, "gmgn_trending"), (dict(raw), "gmgn_trending")),
        observed_at=V3_NOW,
    )

    assert len(batch["events"]) == 1
    assert batch["events"][0]["event_at"] == V3_NOW
    assert batch["events"][0]["observed_at"] == V3_NOW
    assert batch["events"][0]["source_url"] == "https://example.test/events/same-event"
    assert len(batch["events"][0]["raw_fingerprint"]) == 64
    assert len(next(iter(v3_source_loader((raw, "gmgn_trending"))(10)[0].values()))["audit_facts"]) == 1


def test_v3_ca_triggered_dexscreener_row_is_enrichment(monkeypatch):
    raw = {
        "chain": "bsc",
        "address": V3_TOKEN,
        "event_at": V3_NOW,
        "provider_event_id": "dex-lookup-1",
        "lookup_by_ca": True,
    }
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [])

    batch = meme.load_meme_observation_batch(
        10,
        1,
        v3_source_loader((raw, "pair_detail")),
        observed_at=V3_NOW,
    )

    assert batch["events"][0]["provider_family"] == "dexscreener"
    assert batch["events"][0]["evidence_role"] == "enrichment"
    assert batch["events"][0]["counts_for_resonance"] is False


def test_v3_does_not_invent_events_from_presentation_labels(monkeypatch):
    source = {
        "chainId": "bsc",
        "tokenAddress": V3_TOKEN,
        "sources": ["DS", "Alpha_AI"],
        "profile": {"symbol": "LABELS", "market_data_pending": True},
    }
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [])

    batch = meme.load_meme_observation_batch(
        10, 1, lambda _limit: ({"bsc:labels": source}, []), observed_at=V3_NOW
    )

    assert batch["events"] == []
    assert batch["feed_counts"] == {}


def test_v3_legacy_candidate_wrapper_matches_batch_exactly(monkeypatch):
    raw = {
        "chain": "bsc",
        "address": V3_TOKEN,
        "symbol": "LEGACY",
        "event_at": V3_NOW,
        "provider_event_id": "legacy-1",
        "market_data_pending": True,
    }
    loader = v3_source_loader((raw, "gmgn_trenches"))
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [])
    monkeypatch.setattr(meme.alpha, "utc_now_ms", lambda: 1789041600000)

    batch = meme.load_meme_observation_batch(10, 1, loader, observed_at=V3_NOW)
    rows, errors = meme.load_meme_candidates(10, 1, loader)

    assert rows == batch["candidates"]
    assert errors == batch["errors"]


def runtime_file_state(path):
    return None if not path.exists() else (path.stat().st_size, path.stat().st_mtime_ns)


def test_v3_default_loader_preserves_supplied_observed_at(monkeypatch, tmp_path):
    raw = {
        "chainId": "bsc",
        "tokenAddress": V3_TOKEN,
        "event_at": "2026-09-10T11:59:00+00:00",
        "provider_event_id": "dex-default-1",
    }

    def fake_http(url, **_kwargs):
        return [raw] if url == meme.DEX_BOOSTS_TOP_URL else []

    monkeypatch.setattr(meme, "http_json_compat", fake_http)
    monkeypatch.setattr(meme, "fetch_gmgn_sources", lambda *_args, **_kwargs: ({}, []))
    monkeypatch.setattr(meme, "external_source_specs", lambda _limit: [])
    monkeypatch.setattr(meme, "external_file_source_specs", lambda: [])
    monkeypatch.setattr(meme, "external_command_source_specs", lambda _limit: [])
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [])
    persisted = []
    monkeypatch.setattr(
        meme.alpha_okx_market,
        "persist_snapshot",
        lambda out_dir, rows, errors: persisted.append((out_dir, rows, errors)),
    )
    real_paths = [
        meme.ROOT / "outputs" / "meme-source-inbox" / "okx-signal-rest.json",
        meme.ROOT / "outputs" / "meme-source-inbox" / "okx-memepump-rest.json",
    ]
    before = {path: runtime_file_state(path) for path in real_paths}
    monkeypatch.setattr(meme, "ROOT", tmp_path)

    batch = meme.load_meme_observation_batch(1, 1, observed_at=V3_NOW)

    assert len(batch["events"]) == 1
    assert batch["events"][0]["provider_feed"] == "boost_top"
    assert batch["events"][0]["event_at"] == "2026-09-10T11:59:00+00:00"
    assert batch["events"][0]["observed_at"] == V3_NOW
    assert persisted == [(tmp_path / "outputs" / "meme-source-inbox", [], [])]
    assert {path: runtime_file_state(path) for path in real_paths} == before


def test_v3_default_loader_computes_one_implicit_observed_at(monkeypatch, tmp_path):
    raw = {
        "chainId": "bsc",
        "tokenAddress": V3_TOKEN,
        "event_at": "2026-09-10T11:59:00+00:00",
        "provider_event_id": "dex-one-clock-1",
    }
    implicit_calls = []

    def fake_observed_at(value=None):
        if value is not None:
            return value
        implicit_calls.append(value)
        return V3_NOW if len(implicit_calls) == 1 else "2026-09-10T12:00:01+00:00"

    monkeypatch.setattr(meme, "evidence_observed_at", fake_observed_at)
    monkeypatch.setattr(
        meme,
        "http_json_compat",
        lambda url, **_kwargs: [raw] if url == meme.DEX_BOOSTS_TOP_URL else [],
    )
    monkeypatch.setattr(meme, "fetch_gmgn_sources", lambda *_args, **_kwargs: ({}, []))
    monkeypatch.setattr(meme, "fetch_external_meme_sources", lambda *_args, **_kwargs: ({}, []))
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda _item: [])
    monkeypatch.setattr(meme, "ROOT", tmp_path)

    batch = meme.load_meme_observation_batch(1, 1)

    assert implicit_calls == [None]
    assert batch["events"][0]["observed_at"] == V3_NOW


def test_meme_discovery_defaults_to_bsc_and_robinhood(monkeypatch):
    monkeypatch.delenv("ALPHA_MEME_CHAINS", raising=False)
    monkeypatch.delenv("GMGN_TRENDING_URL", raising=False)
    monkeypatch.delenv("GMGN_TRENDING_URLS", raising=False)
    monkeypatch.delenv("GMGN_CLI_CHAINS", raising=False)
    monkeypatch.delenv("GMGN_CLI_DISABLE", raising=False)

    urls = meme.gmgn_source_urls(limit=7)
    cli_specs = meme.gmgn_cli_specs(limit=7)
    assert meme.DEFAULT_MEME_CHAINS == "bsc,robinhood"
    assert meme.meme_chain_allowlist() == {"bsc", "robinhood"}
    assert meme.meme_chain_allowed("4663")
    assert not meme.meme_chain_allowed("46630")

    assert urls == ["https://gmgn.ai/defi/quotation/v1/rank/bsc/swaps/1h?orderby=swaps&direction=desc&limit=7"]
    assert {chain for chain, _interval, _limit in cli_specs} == {"bsc"}


def test_meme_chain_override_can_keep_arc_disabled_for_rollback(monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood")

    assert meme.meme_chain_allowlist() == {"bsc", "robinhood"}
    assert meme.meme_chain_allowed("bsc")
    assert meme.meme_chain_allowed("4663")
    assert not meme.meme_chain_allowed("arc")
    assert not meme.meme_chain_allowed("5042")


def test_load_meme_candidates_filters_non_bsc_by_default(monkeypatch):
    monkeypatch.delenv("ALPHA_MEME_CHAINS", raising=False)
    monkeypatch.setitem(
        meme.load_meme_candidates.__globals__,
        "fetch_profile_sources",
        lambda limit, **_kwargs: (
            {
                "bsc:0xbsc": {
                    "chainId": "bsc",
                    "tokenAddress": "0xBsc",
                    "sources": ["gmgn_trending"],
                    "profile": {
                        "chain": "bsc",
                        "address": "0xBsc",
                        "symbol": "BSCG",
                        "name": "BSC Gold",
                        "price": 0.00021,
                        "market_cap": 42_000,
                        "liquidity": 18_000,
                        "volume": 160_000,
                        "swaps": 600,
                    },
                },
                "solana:mintsol": {
                    "chainId": "solana",
                    "tokenAddress": "MintSol",
                    "sources": ["gmgn_trending"],
                    "profile": {
                        "chain": "sol",
                        "address": "MintSol",
                        "symbol": "SOLG",
                        "name": "Sol Gold",
                        "price": 0.00021,
                        "market_cap": 42_000,
                        "liquidity": 18_000,
                        "volume": 160_000,
                        "swaps": 600,
                    },
                },
            },
            [],
        ),
    )

    def fake_fetch_pairs_for_source(item):
        raise RuntimeError("dex rate limited")

    monkeypatch.setitem(meme.load_meme_candidates.__globals__, "fetch_pairs_for_source", fake_fetch_pairs_for_source)

    rows, errors = meme.load_meme_candidates(limit=10, concurrency=1)

    assert errors == []
    assert [row["symbol"] for row in rows] == ["BSCG"]
    assert rows[0]["chain"] == "bsc"


def test_heat_metrics_rewards_boost_ads_cto_and_gmgn_sources():
    item = {
        "sources": ["boost_top", "ads_latest", "community_takeover", "gmgn_trending"],
        "profile": {"totalAmount": 250},
    }

    metrics = meme.heat_metrics(item)

    assert metrics["heat_score"] >= 55
    assert "boost_top" in metrics["heat_flags"]
    assert "ads_latest" in metrics["heat_flags"]
    assert "community_takeover" in metrics["heat_flags"]
    assert "gmgn_trending" in metrics["heat_flags"]


def test_repeated_okx_trenches_hits_boost_heat_without_inflating_labels():
    sources = ["okx_trenches"] * 8

    metrics = meme.heat_metrics({"sources": sources, "profile": {}})

    assert metrics["source_hit_counts"]["okx_trenches"] == 8
    assert metrics["source_repeat_score"] == 24
    assert "okx_trenches_x8" in metrics["heat_flags"]
    assert metrics["heat_score"] == 48
    assert meme.source_signal_fields(sources)["source_groups"] == ["okx"]
    assert meme.source_signal_fields(sources)["source_count"] == 1
    assert meme.source_labels(sources, {}) == ["OKX", "DS", "Alpha_AI"]


def test_okx_signal_has_dedicated_display_label():
    assert meme.source_labels(["okx_signal"], {}) == ["OKX信号", "DS", "Alpha_AI"]


def test_pumpfun_live_source_is_first_layer_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["pumpfun_live"], "profile": {}})

    assert metrics["heat_score"] == 36
    assert metrics["heat_flags"] == ["pumpfun_live"]
    assert meme.source_labels(["pumpfun_live"], {}) == ["Pump.fun", "DS", "Alpha_AI"]


def test_pumpfun_onchain_source_is_stronger_first_layer_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["pumpfun_onchain"], "profile": {}})

    assert metrics["heat_score"] == 44
    assert metrics["heat_flags"] == ["pumpfun_onchain"]
    assert meme.source_labels(["pumpfun_onchain"], {}) == ["Pump.fun", "DS", "Alpha_AI"]


def test_noxa_launchpad_source_is_robinhood_first_layer_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["noxa_launchpad"], "profile": {}})

    assert metrics["heat_score"] == 40
    assert metrics["heat_flags"] == ["noxa_launchpad"]
    assert meme.source_labels(["noxa_launchpad"], {}) == ["Noxa", "DS", "Alpha_AI"]
    assert meme.source_labels(["noxa_launchpad"], {"market_data_pending": True}) == ["Noxa"]


def test_proficy_trending_source_is_group_scan_confirmation_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["proficy_trending"], "profile": {}})

    assert metrics["heat_score"] == 32
    assert metrics["heat_flags"] == ["proficy_trending"]
    assert meme.source_labels(["proficy_trending"], {}) == ["Proficy", "DS", "Alpha_AI"]


def test_985_monitor_source_is_realtime_discovery_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["985_monitor"], "profile": {}})

    assert metrics["heat_score"] == 34
    assert metrics["heat_flags"] == ["985_monitor"]
    assert meme.source_labels(["985_monitor"], {}) == ["985", "DS", "Alpha_AI"]


def test_985_wallet_sources_are_soft_confirmation_heat_and_labels():
    fomo_metrics = meme.heat_metrics({"sources": ["985_fomo_wallets"], "profile": {}})
    smart_metrics = meme.heat_metrics({"sources": ["985_smartmoney"], "profile": {}})

    assert fomo_metrics["heat_score"] == 30
    assert fomo_metrics["heat_flags"] == ["985_fomo_wallets"]
    assert smart_metrics["heat_score"] == 38
    assert smart_metrics["heat_flags"] == ["985_smartmoney"]
    assert meme.source_labels(["985_fomo_wallets"], {}) == ["985 FOMO", "DS", "Alpha_AI"]
    assert meme.source_labels(["985_smartmoney"], {}) == ["985 SM", "DS", "Alpha_AI"]
    assert meme.source_labels(["985_fomo_wallets"], {"market_data_pending": True}) == ["985 FOMO"]


def test_wind_monitor_source_is_social_ca_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["wind_monitor"], "profile": {}})

    assert metrics["heat_score"] == 34
    assert metrics["heat_flags"] == ["wind_monitor"]
    assert meme.source_labels(["wind_monitor"], {}) == ["听风", "DS", "Alpha_AI"]
    assert meme.source_groups(["wind_monitor", "gmgn_trenches"]) == ["wind", "gmgn"]


def test_soft_source_profile_merge_preserves_market_data():
    profile = meme.merge_source_profile(
        {"liquidity": 42_000, "volume24h": 180_000, "marketCap": 90_000, "market_data_pending": False},
        {"liquidity": 0, "volume24h": 250, "marketCap": 0, "market_data_pending": True, "smart_money": 12},
    )

    assert profile["liquidity"] == 42_000
    assert profile["volume24h"] == 180_000
    assert profile["marketCap"] == 90_000
    assert profile["market_data_pending"] is False
    assert profile["smart_money"] == 12


def test_older_paid_event_cannot_replace_newer_gmgn_market_snapshot():
    gmgn = {
        "price": 0.00282351,
        "market_cap": 2_239_710,
        "market_cap_source": "gmgn_skills_hot_searches.market_cap",
        "liquidity": 182_214,
        "volume": 173_239,
        "observed_at": "2026-09-13T16:48:37+08:00",
    }
    proficy = {
        "market_cap": 1_970_000,
        "market_cap_source": "proficy_trending.market_cap",
        "liquidity": 146_000,
        "volume": 3_180_000,
        "observed_at": "2026-09-13T16:51:00+08:00",
        "source_family": "proficy_trending",
    }
    old_985 = {
        "price": 0.00004001,
        "market_cap": 40_011,
        "market_cap_source": "985_monitor.market_cap",
        "liquidity": 18_603.37,
        "volume": 6_050.44,
        "observed_at": "2026-09-13T05:35:38.395Z",
        "source_family": "985_monitor",
    }

    merged = meme.merge_source_profile(gmgn, proficy)
    merged = meme.merge_source_profile(merged, old_985)

    assert merged["price"] == gmgn["price"]
    assert merged["market_cap"] == proficy["market_cap"]
    assert merged["market_cap_source"] == proficy["market_cap_source"]
    assert merged["liquidity"] == proficy["liquidity"]
    assert merged["volume"] == proficy["volume"]
    assert merged["quote_observed_at"] == gmgn["observed_at"]


def test_intermediate_market_snapshot_cannot_replace_latest_partial_market_update():
    initial = {
        "price": 0.0028,
        "market_cap": 2_200_000,
        "liquidity": 180_000,
        "observed_at": "2026-09-13T16:48:00+08:00",
        "quote_observed_at": "2026-09-13T16:48:00+08:00",
    }
    latest_market = {
        "market_cap": 2_600_000,
        "liquidity": 190_000,
        "observed_at": "2026-09-13T16:51:00+08:00",
    }
    intermediate = {
        "market_cap": 2_300_000,
        "liquidity": 175_000,
        "observed_at": "2026-09-13T16:49:00+08:00",
    }

    merged = meme.merge_source_profile(initial, latest_market)
    merged = meme.merge_source_profile(merged, intermediate)

    assert merged["market_cap"] == latest_market["market_cap"]
    assert merged["liquidity"] == latest_market["liquidity"]
    assert merged["observed_at"] == latest_market["observed_at"]
    assert merged["price"] == initial["price"]
    assert merged["quote_observed_at"] == initial["quote_observed_at"]


def test_bsc_onchain_source_is_first_layer_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["bsc_onchain"], "profile": {}})

    assert metrics["heat_score"] == 42
    assert metrics["heat_flags"] == ["bsc_onchain"]
    assert meme.source_labels(["bsc_onchain"], {}) == ["BSC链上", "DS", "Alpha_AI"]


def test_arc_onchain_source_matches_bsc_onchain_heat_label_and_group():
    metrics = meme.heat_metrics({"sources": ["arc_onchain"], "profile": {}})

    assert metrics["heat_score"] == 42
    assert metrics["heat_flags"] == ["arc_onchain"]
    assert meme.source_labels(["arc_onchain"], {}) == ["ARC链上", "DS", "Alpha_AI"]
    assert meme.source_groups(["arc_onchain"]) == ["arc_onchain"]


def test_arc_onchain_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "arc-onchain.json").write_text(
        json.dumps({"data": [{"chain": "arc", "address": "0x1234567890abcdef1234567890abcdef12345678"}]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()

    assert (str(inbox / "arc-onchain.json"), "arc_onchain") in specs


def test_fourmeme_launchpad_source_is_first_layer_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["fourmeme_launchpad"], "profile": {}})

    assert metrics["heat_score"] == 48
    assert metrics["heat_flags"] == ["fourmeme_launchpad"]
    assert meme.source_labels(["fourmeme_launchpad"], {}) == ["Four.meme", "DS", "Alpha_AI"]


def test_flap_launchpad_source_is_first_layer_heat_and_label():
    metrics = meme.heat_metrics({"sources": ["flap_launchpad"], "profile": {}})

    assert metrics["heat_score"] == 46
    assert metrics["heat_flags"] == ["flap_launchpad"]
    assert meme.source_labels(["flap_launchpad"], {}) == ["Flap", "DS", "Alpha_AI"]


def test_launchpad_pending_source_label_does_not_claim_ds_confirmation():
    assert meme.source_labels(["fourmeme_launchpad"], {"market_data_pending": True}) == ["Four.meme"]
    assert meme.source_labels(["flap_launchpad"], {"market_data_pending": True}) == ["Flap"]


def test_pumpfun_live_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "pumpfun-live.json").write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "solana",
                        "mint": "MintPump",
                        "symbol": "PUMPY",
                        "marketCap": 22_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "pumpfun-live.json"), "pumpfun_live") in specs
    assert status["pumpfun_live"]["enabled"] is True
    assert status["pumpfun_live"]["file_count"] == 1
    assert status["pumpfun_live"]["freshness_level"] == "green"
    assert status["pumpfun_live"]["age_seconds"] >= 0


def test_pumpfun_onchain_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "pumpfun-onchain.json").write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "solana",
                        "mint": "MintPump",
                        "symbol": "MINT",
                        "market_data_pending": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "pumpfun-onchain.json"), "pumpfun_onchain") in specs
    assert status["pumpfun_onchain"]["enabled"] is True
    assert status["pumpfun_onchain"]["file_count"] == 1
    assert status["pumpfun_onchain"]["freshness_level"] == "green"


def test_noxa_launchpad_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "noxa-launches.json").write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0x1234567890abcdef1234567890abcdef12345678",
                        "symbol": "NOXANEW",
                        "marketCap": 52_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "noxa-launches.json"), "noxa_launchpad") in specs
    assert status["noxa_launchpad"]["enabled"] is True
    assert status["noxa_launchpad"]["file_count"] == 1
    assert status["noxa_launchpad"]["freshness_level"] == "green"


def test_proficy_trending_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "proficy-trending.json").write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0x1234567890abcdef1234567890abcdef12345678",
                        "symbol": "PONS",
                        "marketCap": 654_500_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "proficy-trending.json"), "proficy_trending") in specs
    assert status["proficy_trending"]["enabled"] is True
    assert status["proficy_trending"]["file_count"] == 1
    assert status["proficy_trending"]["freshness_level"] == "green"


def test_expired_inbox_snapshot_is_reported_but_not_loaded_as_live_source(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    snapshot = inbox / "proficy-trending.json"
    snapshot.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0x1234567890abcdef1234567890abcdef12345678",
                        "symbol": "STALE",
                        "marketCap": 100_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stale_time = time.time() - 301
    os.utime(snapshot, (stale_time, stale_time))
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    assert (str(snapshot), "proficy_trending") not in meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()
    assert status["proficy_trending"]["enabled"] is True
    assert status["proficy_trending"]["freshness_level"] == "red"


def test_985_monitor_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "985-monitor.json").write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0x1234567890abcdef1234567890abcdef12345678",
                        "symbol": "RBH",
                        "marketCap": 98_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "985-monitor.json"), "985_monitor") in specs
    assert status["985_monitor"]["enabled"] is True
    assert status["985_monitor"]["file_count"] == 1
    assert status["985_monitor"]["freshness_level"] == "green"


def test_985_wallet_inbox_files_are_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "985-fomo-wallets.json").write_text(
        json.dumps({"data": [{"chain": "bsc", "address": "0xFomo", "symbol": "FOMO"}]}),
        encoding="utf-8",
    )
    (inbox / "985-smartmoney.json").write_text(
        json.dumps({"data": [{"chain": "base", "address": "0xSmart", "symbol": "SMART"}]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "985-fomo-wallets.json"), "985_fomo_wallets") in specs
    assert (str(inbox / "985-smartmoney.json"), "985_smartmoney") in specs
    assert status["985_fomo_wallets"]["enabled"] is True
    assert status["985_fomo_wallets"]["file_count"] == 1
    assert status["985_smartmoney"]["enabled"] is True
    assert status["985_smartmoney"]["file_count"] == 1


def test_wind_monitor_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "wind-monitor.json").write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0x1234567890abcdef1234567890abcdef12345678",
                        "symbol": "WIND",
                        "marketCap": 88_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "wind-monitor.json"), "wind_monitor") in specs
    assert status["wind_monitor"]["enabled"] is True
    assert status["wind_monitor"]["file_count"] == 1
    assert status["wind_monitor"]["freshness_level"] == "green"


def test_fast_985_monitor_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "985-monitor-fast.json").write_text(
        json.dumps({
            "fetched_at": datetime.now().astimezone().isoformat(),
            "data": [{
                "chain": "robinhood",
                "address": "0x1234567890abcdef1234567890abcdef12345678",
                "symbol": "FAST985",
            }],
        }),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()

    assert (str(inbox / "985-monitor-fast.json"), "985_monitor") in specs


def test_bsc_onchain_source_status_is_default_bsc_first_layer(monkeypatch):
    monkeypatch.delenv("BSC_WSS_URL", raising=False)
    monkeypatch.delenv("BNBCHAIN_WSS_URL", raising=False)
    monkeypatch.delenv("BSC_PANCAKE_WSS_URL", raising=False)
    monkeypatch.delenv("QUICKNODE_BSC_WSS_URL", raising=False)
    monkeypatch.delenv("ALPHA_SHOW_SOLANA_SOURCES", raising=False)

    status = meme.optional_meme_source_status()

    assert status["bsc_onchain"]["enabled"] is False
    assert "BSC_WSS_URL" in status["bsc_onchain"]["needs"]
    assert "pumpfun_live" not in status
    assert "pumpfun_onchain" not in status


def test_launchpad_source_status_needs_bitquery_token(monkeypatch):
    monkeypatch.delenv("BITQUERY_API_KEY", raising=False)
    monkeypatch.delenv("BITQUERY_TOKEN", raising=False)
    monkeypatch.setattr(meme, "windows_environment_value", lambda _name: "")

    status = meme.optional_meme_source_status()

    assert status["fourmeme_launchpad"]["enabled"] is False
    assert "BITQUERY_API_KEY" in status["fourmeme_launchpad"]["needs"]
    assert status["flap_launchpad"]["enabled"] is False
    assert "BITQUERY_API_KEY" in status["flap_launchpad"]["needs"]


def test_bsc_onchain_inbox_file_is_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "bsc-pancake-pairs.json").write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "bsc",
                        "address": "0x1234567890abcdef1234567890abcdef12345678",
                        "symbol": "BSCNEW",
                        "market_data_pending": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "bsc-pancake-pairs.json"), "bsc_onchain") in specs
    assert status["bsc_onchain"]["enabled"] is True
    assert status["bsc_onchain"]["file_count"] == 1
    assert status["bsc_onchain"]["freshness_level"] == "green"


def test_arc_onchain_inbox_and_degraded_poller_health_are_visible(monkeypatch, tmp_path):
    inbox = tmp_path / "outputs" / "meme-source-inbox"
    inbox.mkdir(parents=True)
    arc_file = inbox / "arc-onchain.json"
    arc_file.write_text(
        json.dumps({"data": [{
            "chain": "arc",
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "symbol": "ARCNEW",
        }]}),
        encoding="utf-8",
    )
    (tmp_path / "outputs" / "arc-public-chain-poll-status.json").write_text(
        json.dumps({"ok": False, "status": "degraded", "arcscan_errors": ["endpoint_timeout"]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))
    monkeypatch.setattr(meme, "ROOT", tmp_path)

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(arc_file), "arc_onchain") in specs
    assert status["arc_onchain"]["enabled"] is True
    assert status["arc_onchain"]["file_count"] == 1
    assert status["arc_onchain"]["poller"]["status"] == "degraded"
    assert status["arc_onchain"]["freshness_level"] == "red"


def test_arc_onchain_stale_successful_poller_status_is_not_reported_live(monkeypatch, tmp_path):
    output = tmp_path / "outputs"
    output.mkdir()
    (output / "arc-public-chain-poll-status.json").write_text(
        json.dumps({"ok": True, "status": "ok", "observed_at": "2020-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(output / "meme-source-inbox"))
    monkeypatch.setattr(meme, "ROOT", tmp_path)

    status = meme.optional_meme_source_status()

    assert status["arc_onchain"]["enabled"] is True
    assert status["arc_onchain"]["freshness_level"] == "red"
    assert status["arc_onchain"]["last_updated_at"] == "2020-01-01T00:00:00+00:00"


def test_arc_onchain_without_poller_or_inbox_remains_pending(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(tmp_path / "meme-source-inbox"))
    monkeypatch.setattr(meme, "ROOT", tmp_path)

    status = meme.optional_meme_source_status()

    assert status["arc_onchain"]["enabled"] is False
    assert status["arc_onchain"]["freshness_level"] == "pending"


def test_launchpad_inbox_files_are_auto_detected(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "fourmeme-launches.json").write_text(
        json.dumps({"data": [{"chain": "bsc", "address": "0x1234567890abcdef1234567890abcdef12345678", "symbol": "FOUR", "market_data_pending": True}]}),
        encoding="utf-8",
    )
    (inbox / "flap-launches.json").write_text(
        json.dumps({"data": [{"chain": "bsc", "address": "0x1234567890abcdef1234567890abcdef12348888", "symbol": "FLAP", "market_data_pending": True}]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    specs = meme.local_inbox_file_source_specs()
    status = meme.optional_meme_source_status()

    assert (str(inbox / "fourmeme-launches.json"), "fourmeme_launchpad") in specs
    assert (str(inbox / "flap-launches.json"), "flap_launchpad") in specs
    assert status["fourmeme_launchpad"]["enabled"] is True
    assert status["fourmeme_launchpad"]["freshness_level"] == "green"
    assert status["flap_launchpad"]["enabled"] is True
    assert status["flap_launchpad"]["freshness_level"] == "green"


def test_source_status_marks_stale_inbox_files(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    old_file = inbox / "debot-signals.json"
    old_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "solana",
                        "address": "DebotMint",
                        "symbol": "DEBOT",
                        "marketCap": 42_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    old_stamp = 1_700_000_000
    os.utime(old_file, (old_stamp, old_stamp))
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(inbox))

    status = meme.optional_meme_source_status()

    assert status["debot"]["enabled"] is True
    assert status["debot"]["freshness_level"] == "red"
    assert status["debot"]["freshness_label"] == "过期"
    assert status["debot"]["age_seconds"] > 300


def test_fetch_gmgn_sources_normalizes_common_payload(monkeypatch):
    monkeypatch.setenv("GMGN_TRENDING_URL", "https://example.invalid/gmgn")
    monkeypatch.setenv("GMGN_CLI_DISABLE", "1")

    def fake_http_json(url, **kwargs):
        assert url == "https://example.invalid/gmgn"
        return {
            "data": {
                "tokens": [
                    {
                        "chain": "solana",
                        "token_address": "So11111111111111111111111111111111111111112",
                        "symbol": "SOLX",
                        "score": 88,
                    }
                ]
            }
        }

    monkeypatch.setattr(alpha, "http_json", fake_http_json)

    sources, errors = meme.fetch_gmgn_sources(limit=10)

    assert errors == []
    row = sources["solana:so11111111111111111111111111111111111111112"]
    assert row["chainId"] == "solana"
    assert row["tokenAddress"] == "So11111111111111111111111111111111111111112"
    assert row["sources"] == ["gmgn_trending"]
    assert row["profile"]["gmgn_score"] == 88


def test_fetch_gmgn_sources_sends_browser_headers(monkeypatch):
    monkeypatch.setenv("GMGN_TRENDING_URL", "https://example.invalid/gmgn")
    monkeypatch.setenv("GMGN_CLI_DISABLE", "1")
    seen_headers = {}

    def fake_http_json(url, **kwargs):
        seen_headers.update(kwargs.get("headers") or {})
        return {
            "data": {
                "rank": [
                    {
                        "chain": "solana",
                        "address": "MintA",
                        "score": 90,
                    }
                ]
            }
        }

    monkeypatch.setattr(alpha, "http_json", fake_http_json)

    sources, errors = meme.fetch_gmgn_sources(limit=1)

    assert errors == []
    assert "solana:minta" in sources
    assert seen_headers["Referer"].startswith("https://gmgn.ai")
    assert "Mozilla/5.0" in seen_headers["User-Agent"]


def test_gmgn_profile_fields_translate_rank_metrics_to_recommendation_fields():
    fields = meme.gmgn_profile_fields(
        {
            "smart_degen_count": 40,
            "renowned_count": 9,
            "holder_count": 1101,
            "market_cap": 96_000,
            "liquidity": 23_000,
            "volume": 429_000,
            "top_10_holder_rate": 0.1715,
            "sniper_count": 42,
            "bundler_rate": 0.2182,
            "rug_ratio": 0.13,
        }
    )

    assert fields["smart_money"] == 40
    assert fields["kol"] == 9
    assert fields["holders"] == 1101
    assert fields["potential_label"] == "100x+"
    assert fields["top10_holder_pct"] == 17.15
    assert fields["gmgn_risk_flags"] == ["sniper_42", "bundler_21.8%"]


def test_load_meme_candidates_keeps_gmgn_only_rows_when_dex_pairs_fail(monkeypatch):
    monkeypatch.setitem(
        meme.load_meme_candidates.__globals__,
        "fetch_profile_sources",
        lambda limit, **_kwargs: (
            {
                "solana:minta": {
                    "chainId": "solana",
                    "tokenAddress": "MintA",
                    "sources": ["gmgn_trending"],
                    "profile": {
                        "chain": "sol",
                        "address": "MintA",
                        "symbol": "BOB",
                        "name": "Bob Bot",
                        "price": 0.000113,
                        "market_cap": 96_000,
                        "liquidity": 23_000,
                        "volume": 429_000,
                        "price_change_percent1m": 0.57,
                        "price_change_percent5m": 11.34,
                        "price_change_percent1h": 127.76,
                        "creation_timestamp": 1_800_000_000,
                        "swaps": 10_527,
                        "smart_degen_count": 40,
                        "renowned_count": 9,
                        "holder_count": 1101,
                        "score": 2.4,
                    },
                }
            },
            [],
        ),
    )

    def fake_fetch_pairs_for_source(item):
        raise RuntimeError("dex rate limited")

    monkeypatch.setitem(meme.load_meme_candidates.__globals__, "fetch_pairs_for_source", fake_fetch_pairs_for_source)
    monkeypatch.setattr(meme.alpha, "utc_now_ms", lambda: 1_800_000_000 * 1000 + 3_600_000)

    rows, errors = meme.load_meme_candidates(limit=10, concurrency=1)

    assert errors == []
    assert rows[0]["symbol"] == "BOB"
    assert rows[0]["source_labels"] == ["GMGN", "DS", "Alpha_AI"]
    assert rows[0]["smart_money"] == 40
    assert rows[0]["kol"] == 9
    assert rows[0]["potential_label"] == "100x+"
    assert rows[0]["url"] == "https://gmgn.ai/sol/token/MintA"


def test_gmgn_candidate_from_profile_outputs_narrative_tags():
    row = meme.gmgn_candidate_from_profile(
        {
            "chainId": "solana",
            "tokenAddress": "MintA",
            "sources": ["gmgn_trending"],
            "profile": {
                "chain": "sol",
                "address": "MintA",
                "symbol": "GROKDOG",
                "name": "Grok Dog AI",
                "launchpad_platform": "Pump.fun",
                "price": 0.000113,
                "market_cap": 960_000,
                "liquidity": 73_000,
                "volume": 429_000,
            },
        },
        now_ms=1_800_000_000 * 1000,
    )

    assert row is not None
    assert {"AI", "动物", "马斯克系", "Pump"}.issubset(set(row["narrative_tags"]))


def test_launchpad_lifecycle_fields_are_kept_on_pending_bsc_candidate():
    row = meme.candidate_from_profile(
        {
            "chainId": "bsc",
            "tokenAddress": "0x1234567890abcdef1234567890abcdef12345678",
            "sources": ["fourmeme_launchpad"],
            "profile": {
                "chain": "bsc",
                "tokenAddress": "0x1234567890abcdef1234567890abcdef12345678",
                "symbol": "FAST",
                "name": "Fast Curve",
                "market_data_pending": True,
                "bonding_curve_progress_pct": 96.2,
                "purchase_count": 7,
                "bnb_per_hour": 3.4,
                "launchpad_lifecycle_stage": "near_graduation",
                "launchpad_stage_label": "曲线快毕业",
            },
        },
        now_ms=1_800_000_000 * 1000,
    )

    assert row is not None
    assert row["market_data_pending"] is True
    assert row["pair_age_hours"] is None
    assert row["source_labels"] == ["Four.meme"]
    assert row["bonding_curve_progress_pct"] == 96.2
    assert row["purchase_count"] == 7
    assert row["launchpad_lifecycle_stage"] == "near_graduation"
    assert "curve_near_graduation" in row["heat_flags"]


def test_fetch_gmgn_sources_uses_default_rank_urls_without_manual_env(monkeypatch):
    monkeypatch.delenv("GMGN_TRENDING_URL", raising=False)
    monkeypatch.delenv("GMGN_TRENDING_URLS", raising=False)
    monkeypatch.delenv("GMGN_TRENCHES_URLS", raising=False)
    monkeypatch.setenv("GMGN_CLI_DISABLE", "1")
    called_urls = []

    def fake_http_json(url, **kwargs):
        called_urls.append(url)
        if "/rank/sol/swaps/1h" in url:
            return {
                "data": {
                    "rank": [
                        {
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
        return {"data": {"rank": []}}

    monkeypatch.setattr(alpha, "http_json", fake_http_json)

    sources, errors = meme.fetch_gmgn_sources(limit=5)

    assert errors == []
    assert any("/rank/sol/swaps/1h" in url for url in called_urls)
    row = sources["solana:minta"]
    assert row["chainId"] == "solana"
    assert row["tokenAddress"] == "MintA"
    assert row["profile"]["gmgn_score"] == 92
    assert row["profile"]["smart_money"] == 36


def test_gmgn_source_urls_include_optional_trenches_urls(monkeypatch):
    monkeypatch.delenv("GMGN_TRENDING_URL", raising=False)
    monkeypatch.delenv("GMGN_TRENDING_URLS", raising=False)
    monkeypatch.setenv("GMGN_TRENCHES_URLS", "https://example.invalid/trenches?limit={limit}")

    urls = meme.gmgn_source_urls(limit=7)

    assert any("/rank/sol/swaps/1h" in url for url in urls)
    assert "https://example.invalid/trenches?limit=7" in urls


def test_fetch_gmgn_sources_labels_trenches_rows(monkeypatch):
    monkeypatch.delenv("GMGN_TRENDING_URL", raising=False)
    monkeypatch.delenv("GMGN_TRENDING_URLS", raising=False)
    monkeypatch.setenv("GMGN_CLI_DISABLE", "1")
    monkeypatch.setenv("GMGN_TRENCHES_URLS", "https://example.invalid/trenches?limit={limit}")

    def fake_http_json(url, **kwargs):
        if "trenches" not in url:
            return {"data": {"rank": []}}
        return {
            "data": {
                "rank": [
                    {
                        "chain": "solana",
                        "address": "MintT",
                        "symbol": "TRENCH",
                        "score": 77,
                    }
                ]
            }
        }

    monkeypatch.setattr(alpha, "http_json", fake_http_json)

    sources, errors = meme.fetch_gmgn_sources(limit=3)

    assert errors == []
    row = sources["solana:mintt"]
    assert row["sources"] == ["gmgn_trenches"]
    assert row["profile"]["source_family"] == "gmgn_trenches"


def test_optional_meme_source_status_reports_configured_sources(monkeypatch):
    monkeypatch.setenv("GMGN_TRENCHES_URLS", "https://example.invalid/gmgn")
    monkeypatch.setenv("GMGN_TRENCHES_COMMANDS", "bb-browser gmgn {limit}")
    monkeypatch.setenv("OKX_TRENCHES_URLS", "https://example.invalid/okx-a, https://example.invalid/okx-b")
    monkeypatch.setenv("OKX_TRENCHES_FILES", "C:\\tmp\\okx.json")
    monkeypatch.setenv("PROFICY_TRENDING_FILES", "C:\\tmp\\proficy.json")
    monkeypatch.setenv("MONITOR985_FILES", "C:\\tmp\\985.json")
    monkeypatch.setenv("MONITOR985_FOMO_FILES", "C:\\tmp\\985-fomo.json")
    monkeypatch.setenv("MONITOR985_SMARTMONEY_FILES", "C:\\tmp\\985-sm.json")
    monkeypatch.setenv("BINANCE_WALLET_HOT_FILES", "C:\\tmp\\binance-wallet-hot.json")
    monkeypatch.setenv("BINANCE_WALLET_SIGNAL_COMMANDS", "binance-wallet-signals {limit}")
    monkeypatch.setenv("DEBOT_TRENCHES_URLS", "https://example.invalid/debot-bsc?limit={limit}")
    monkeypatch.setenv("DEBOT_SIGNAL_COMMANDS", "bb-browser debot")
    monkeypatch.setenv("MOBULA_API_KEY", "mobula-key")
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    monkeypatch.delenv("BIRDEYE_TRENDING_URLS", raising=False)
    monkeypatch.setenv("BIRDEYE_TRENDING_COMMANDS", "bb-browser birdeye")

    status = meme.optional_meme_source_status()

    assert status["gmgn_trenches"]["enabled"] is True
    assert status["gmgn_trenches"]["url_count"] == 1
    assert status["gmgn_trenches"]["command_count"] == 2
    assert status["gmgn_trenches"]["file_count"] == 0
    assert status["gmgn_trenches"]["mode"] == "gmgn-cli/url/browser/file"
    assert status["okx_trenches"]["enabled"] is True
    assert status["okx_trenches"]["url_count"] == 2
    assert status["okx_trenches"]["file_count"] == 1
    assert status["proficy_trending"]["enabled"] is True
    assert status["proficy_trending"]["file_count"] == 1
    assert status["proficy_trending"]["mode"] == "public-trending/file/inbox"
    assert status["985_monitor"]["enabled"] is True
    assert status["985_monitor"]["file_count"] == 1
    assert status["985_monitor"]["mode"] == "public-api/file/inbox"
    assert status["985_fomo_wallets"]["enabled"] is True
    assert status["985_fomo_wallets"]["file_count"] == 1
    assert status["985_fomo_wallets"]["mode"] == "public-fomo-profile/file/inbox"
    assert status["985_smartmoney"]["enabled"] is True
    assert status["985_smartmoney"]["file_count"] == 1
    assert status["985_smartmoney"]["mode"] == "public-smartmoney/file/inbox"
    assert status["binance_wallet_hot"]["enabled"] is True
    assert status["binance_wallet_hot"]["file_count"] == 1
    assert status["binance_wallet_hot"]["mode"] == "wallet-market-rank/meme-rush/url/command/file/inbox"
    assert status["binance_wallet_signal"]["enabled"] is True
    assert status["binance_wallet_signal"]["command_count"] == 1
    assert status["binance_wallet_signal"]["mode"] == "wallet-smart-money-signal/url/command/file/inbox"
    assert status["debot"]["enabled"] is True
    assert status["debot"]["url_count"] == 1
    assert status["debot"]["command_count"] == 1
    assert status["debot"]["mode"] == "url/browser/file/inbox"
    assert status["mobula"]["enabled"] is True
    assert status["mobula"]["url_count"] == 1
    assert status["mobula"]["key_present"] is True
    assert status["mobula"]["freshness_level"] == "green"
    assert status["birdeye"]["enabled"] is True
    assert status["birdeye"]["url_count"] == 0
    assert status["birdeye"]["command_count"] == 1
    assert status["birdeye"]["key_present"] is False
    assert status["birdeye"]["needs"] == ""


def test_optional_meme_source_status_marks_mobula_default_enabled(monkeypatch):
    monkeypatch.delenv("MOBULA_TRENDING_URLS", raising=False)
    monkeypatch.delenv("MOBULA_API_KEY", raising=False)
    monkeypatch.delenv("MOBULA_DISABLE_DEFAULT_TRENDING", raising=False)

    status = meme.optional_meme_source_status()

    assert status["mobula"]["enabled"] is True
    assert status["mobula"]["url_count"] == 1
    assert status["mobula"]["key_present"] is False
    assert status["mobula"]["freshness_level"] == "green"


def test_fetch_external_meme_sources_normalizes_configured_url_rows(monkeypatch):
    monkeypatch.setenv("MOBULA_DISABLE_DEFAULT_TRENDING", "1")
    monkeypatch.setenv("MOBULA_TRENDING_URLS", "https://example.invalid/mobula?limit={limit}")
    monkeypatch.setenv("BIRDEYE_TRENDING_URLS", "https://example.invalid/birdeye?limit={limit}")
    monkeypatch.setenv("DEBOT_SIGNAL_URLS", "https://example.invalid/debot?limit={limit}")
    monkeypatch.setenv("BIRDEYE_API_KEY", "bird-key")
    seen_headers = {}

    def fake_http_json(url, **kwargs):
        if "birdeye" in url:
            seen_headers.update(kwargs.get("headers") or {})
            return {
                "data": {
                    "items": [
                        {
                            "chain": "solana",
                            "address": "BirdMint",
                            "symbol": "BIRD",
                            "name": "Bird Meme",
                            "mc": 210_000,
                            "liquidity": 41_000,
                            "volume24h": 320_000,
                        }
                    ]
                }
            }
        if "debot" in url:
            return {
                "data": {
                    "items": [
                        {
                            "chain": "bsc",
                            "address": "0xDebot",
                            "symbol": "DEB",
                            "name": "Debot Pick",
                            "marketCap": 88_000,
                            "liquidityUsd": 33_000,
                            "volume24hUsd": 260_000,
                            "score": 82,
                        }
                    ]
                }
            }
        return {
            "data": [
                {
                    "chainId": "base",
                    "tokenAddress": "0xMobula",
                    "symbol": "MOBU",
                    "name": "Mobula Meme",
                    "market_cap": 120_000,
                    "liquidity": 25_000,
                    "volume": 190_000,
                    "score": 61,
                }
            ]
        }

    monkeypatch.setattr(alpha, "http_json", fake_http_json)

    sources, errors = meme.fetch_external_meme_sources(limit=5)

    assert errors == []
    assert seen_headers["X-API-KEY"] == "bird-key"
    mobula = sources["base:0xmobula"]
    birdeye = sources["solana:birdmint"]
    debot = sources["bsc:0xdebot"]
    assert mobula["sources"] == ["mobula_trending"]
    assert mobula["profile"]["market_cap"] == 120_000
    assert birdeye["sources"] == ["birdeye_trending"]
    assert birdeye["profile"]["market_cap"] == 210_000
    assert debot["sources"] == ["debot_signal"]
    assert debot["profile"]["market_cap"] == 88_000


def test_fetch_external_meme_sources_reads_file_and_browser_command_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("MOBULA_DISABLE_DEFAULT_TRENDING", "1")
    okx_file = tmp_path / "okx-trenches.json"
    proficy_file = tmp_path / "proficy-trending.json"
    monitor985_file = tmp_path / "985-monitor.json"
    monitor985_fomo_file = tmp_path / "985-fomo-wallets.json"
    monitor985_smartmoney_file = tmp_path / "985-smartmoney.json"
    wind_file = tmp_path / "wind-monitor.json"
    binance_hot_file = tmp_path / "binance-wallet-hot.json"
    binance_signal_file = tmp_path / "binance-wallet-signals.json"
    okx_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "bsc",
                        "address": "0xOkxPick",
                        "symbol": "OKXP",
                        "name": "OKX Pick",
                        "marketCap": 42_000,
                        "liquidityUsd": 12_000,
                        "volume24hUsd": 95_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OKX_TRENCHES_FILES", str(okx_file))
    proficy_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0xProficyPick",
                        "symbol": "PROF",
                        "name": "Proficy Pick",
                        "marketCap": 92_000,
                        "liquidity": 31_000,
                        "volume24h": 140_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROFICY_TRENDING_FILES", str(proficy_file))
    monitor985_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0x985Pick",
                        "symbol": "NINE",
                        "name": "985 Pick",
                        "marketCap": 76_000,
                        "liquidity": 22_000,
                        "volume24h": 110_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MONITOR985_FILES", str(monitor985_file))
    monitor985_fomo_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "bsc",
                        "address": "0x985Fomo",
                        "symbol": "FOMO",
                        "name": "985 Fomo Pick",
                        "marketCap": 55_000,
                        "market_data_pending": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MONITOR985_FOMO_FILES", str(monitor985_fomo_file))
    monitor985_smartmoney_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "base",
                        "address": "0x985Smart",
                        "symbol": "SMRT",
                        "name": "985 Smart Pick",
                        "marketCap": 85_000,
                        "market_data_pending": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MONITOR985_SMARTMONEY_FILES", str(monitor985_smartmoney_file))
    wind_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0xWindPick",
                        "symbol": "WIND",
                        "name": "Wind Pick",
                        "marketCap": 66_000,
                        "liquidity": 24_000,
                        "volume24h": 125_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WIND_MONITOR_FILES", str(wind_file))
    binance_hot_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "robinhood",
                        "address": "0xBinanceHot",
                        "symbol": "BHOT",
                        "name": "Binance Wallet Hot",
                        "marketCap": 64_000,
                        "liquidity": 21_000,
                        "volume24h": 180_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BINANCE_WALLET_HOT_FILES", str(binance_hot_file))
    binance_signal_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "bsc",
                        "address": "0xBinanceSignal",
                        "symbol": "BSIG",
                        "name": "Binance Wallet Signal",
                        "marketCap": 96_000,
                        "liquidity": 38_000,
                        "volume24h": 240_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BINANCE_WALLET_SIGNAL_FILES", str(binance_signal_file))
    monkeypatch.setenv("GMGN_TRENCHES_COMMANDS", "bb-browser gmgn {limit}")

    def fake_command_json(command):
        assert command == "bb-browser gmgn 5"
        return {
            "data": [
                {
                    "chain": "bsc",
                    "address": "0xGmgnPick",
                    "symbol": "GMP",
                    "name": "GMGN Pick",
                    "market_cap": 36_000,
                    "liquidity": 15_000,
                    "volume": 80_000,
                }
            ]
        }

    monkeypatch.setattr(meme, "command_json", fake_command_json)

    sources, errors = meme.fetch_external_meme_sources(limit=5)

    assert errors == []
    assert sources["bsc:0xokxpick"]["sources"] == ["okx_trenches"]
    assert sources["bsc:0xokxpick"]["profile"]["source_origin"] == str(okx_file)
    assert sources["robinhood:0xproficypick"]["sources"] == ["proficy_trending"]
    assert sources["robinhood:0xproficypick"]["profile"]["source_origin"] == str(proficy_file)
    assert sources["robinhood:0x985pick"]["sources"] == ["985_monitor"]
    assert sources["robinhood:0x985pick"]["profile"]["source_origin"] == str(monitor985_file)
    assert sources["bsc:0x985fomo"]["sources"] == ["985_fomo_wallets"]
    assert sources["bsc:0x985fomo"]["profile"]["source_origin"] == str(monitor985_fomo_file)
    assert sources["base:0x985smart"]["sources"] == ["985_smartmoney"]
    assert sources["base:0x985smart"]["profile"]["source_origin"] == str(monitor985_smartmoney_file)
    assert sources["robinhood:0xwindpick"]["sources"] == ["wind_monitor"]
    assert sources["robinhood:0xwindpick"]["profile"]["source_origin"] == str(wind_file)
    assert sources["robinhood:0xbinancehot"]["sources"] == ["binance_wallet_hot"]
    assert sources["robinhood:0xbinancehot"]["profile"]["source_origin"] == str(binance_hot_file)
    assert sources["bsc:0xbinancesignal"]["sources"] == ["binance_wallet_signal"]
    assert sources["bsc:0xbinancesignal"]["profile"]["source_origin"] == str(binance_signal_file)
    assert sources["bsc:0xgmgnpick"]["sources"] == ["gmgn_trenches"]
    assert sources["bsc:0xgmgnpick"]["profile"]["source_origin"] == "bb-browser gmgn 5"


def test_external_file_source_specs_auto_reads_nonempty_local_inbox(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.delenv("DEBOT_SIGNAL_FILES", raising=False)
    monkeypatch.delenv("OKX_TRENCHES_FILES", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(tmp_path))
    debot_file = tmp_path / "debot-signals.json"
    debot_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "bsc",
                        "address": "0xDebotInbox",
                        "symbol": "DBI",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "okx-trenches.json").write_text("[]", encoding="utf-8")

    specs = meme.external_file_source_specs()

    assert (str(debot_file), "debot_signal") in specs
    assert all(label != "okx_trenches" for _path, label in specs)


def test_fetch_external_meme_sources_reads_local_debot_inbox(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("MOBULA_DISABLE_DEFAULT_TRENDING", "1")
    monkeypatch.delenv("DEBOT_SIGNAL_FILES", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(tmp_path))
    debot_file = tmp_path / "debot-signals.json"
    debot_file.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "bsc",
                        "address": "0xDebotInbox",
                        "symbol": "DBI",
                        "name": "DeBot Inbox",
                        "marketCap": 77_000,
                        "liquidityUsd": 29_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    sources, errors = meme.fetch_external_meme_sources(limit=5)

    assert errors == []
    row = sources["bsc:0xdebotinbox"]
    assert row["sources"] == ["debot_signal"]
    assert row["profile"]["source_origin"] == str(debot_file)
    assert row["profile"]["market_cap"] == 77_000


def test_birdeye_api_key_enables_default_discovery_urls(monkeypatch):
    monkeypatch.delenv("BIRDEYE_TRENDING_URLS", raising=False)
    monkeypatch.delenv("BIRDEYE_DISABLE_DEFAULT_TRENDING", raising=False)
    monkeypatch.setenv("BIRDEYE_API_KEY", "bird-key")

    specs = meme.external_source_specs(limit=25)

    urls = [url for url, label in specs if label.startswith("birdeye_")]
    assert any("/defi/token_trending" in url and "limit=25" in url for url in urls)
    assert any("/defi/v3/token/meme/list" in url and "limit=25" in url for url in urls)


def test_normalize_external_meme_row_reads_birdeye_market_fields():
    normalized = meme.normalize_external_meme_row(
        {
            "chain": "solana",
            "address": "BirdMint",
            "symbol": "BIRD",
            "market_cap": 420_000,
            "liquidity_usd": 81_000,
            "volume_24h_usd": 730_000,
            "price_change_1h_percent": 34,
            "price_change_5m_percent": 7,
            "price_change_24h_percent": 88,
        },
        "birdeye_trending",
    )

    assert normalized is not None
    profile = normalized["profile"]
    assert profile["market_cap"] == 420_000
    assert profile["liquidity"] == 81_000
    assert profile["volume"] == 730_000
    assert profile["price_change_percent1h"] == 34
    assert profile["price_change_percent5m"] == 7
    assert profile["price_change_percent24h"] == 88


def test_binance_wallet_sources_share_one_independent_group():
    sources = ["binance_wallet_hot", "binance_wallet_signal"]

    labels = meme.source_labels(sources, {})
    assert "币安钱包" in labels
    assert "币安钱包信号" in labels
    assert "DS" in labels
    assert "Alpha_AI" in labels
    assert meme.source_signal_fields(sources)["source_groups"] == ["binance_wallet"]
    assert meme.source_signal_fields(sources)["source_count"] == 1


def test_optional_meme_source_status_includes_default_enabled_sources(monkeypatch):
    monkeypatch.delenv("GMGN_TRENDING_URL", raising=False)
    monkeypatch.delenv("GMGN_TRENDING_URLS", raising=False)
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    monkeypatch.delenv("BIRDEYE_TRENDING_URLS", raising=False)

    status = meme.optional_meme_source_status()

    assert status["dexscreener_discovery"]["enabled"] is True
    assert status["gmgn_trending"]["enabled"] is True
    assert status["gmgn_trending"]["url_count"] >= 4
    assert status["gmgn_live_trending"]["enabled"] is True
    assert status["gmgn_live_trending"]["mode"] == "gmgn-cli"
    assert status["gmgn_trenches"]["enabled"] is True
    assert status["gmgn_trenches"]["command_count"] >= 1
    assert status["gmgn_trenches"]["mode"] == "gmgn-cli/url/browser/file"
    assert status["birdeye"]["enabled"] is False
    assert "BIRDEYE_API_KEY" in status["birdeye"]["needs"]


def test_external_source_specs_uses_mobula_demo_by_default(monkeypatch):
    monkeypatch.delenv("MOBULA_TRENDING_URLS", raising=False)
    monkeypatch.delenv("MOBULA_DISABLE_DEFAULT_TRENDING", raising=False)

    specs = meme.external_source_specs(limit=9)

    assert ("https://demo-api.mobula.io/api/1/metadata/trendings", "mobula_trending") in specs


def test_wind_monitor_env_sources_are_registered(monkeypatch, tmp_path):
    wind_file = tmp_path / "wind.json"
    monkeypatch.setenv("WIND_MONITOR_URLS", "https://example.invalid/wind?limit={limit}")
    monkeypatch.setenv("TINGFENG_MONITOR_COMMANDS", "wind-export --limit {limit}")
    monkeypatch.setenv("WIND_MONITOR_FILES", str(wind_file))

    assert ("https://example.invalid/wind?limit=7", "wind_monitor") in meme.external_source_specs(limit=7)
    assert ("wind-export --limit 7", "wind_monitor") in meme.external_command_source_specs(limit=7)
    assert (str(wind_file), "wind_monitor") in meme.external_file_source_specs()

    status = meme.optional_meme_source_status()
    assert status["wind_monitor"]["enabled"] is True
    assert status["wind_monitor"]["url_count"] == 1
    assert status["wind_monitor"]["command_count"] == 1
    assert status["wind_monitor"]["file_count"] == 1


def test_wind_monitor_export_status_marks_source_checked(monkeypatch, tmp_path):
    monkeypatch.delenv("WIND_MONITOR_URLS", raising=False)
    monkeypatch.delenv("TINGFENG_MONITOR_URLS", raising=False)
    monkeypatch.delenv("WIND_MONITOR_COMMANDS", raising=False)
    monkeypatch.delenv("TINGFENG_MONITOR_COMMANDS", raising=False)
    monkeypatch.delenv("WIND_MONITOR_FILES", raising=False)
    monkeypatch.delenv("TINGFENG_MONITOR_FILES", raising=False)
    monkeypatch.setattr(meme, "ROOT", tmp_path)
    status_path = tmp_path / "outputs" / "wind-monitor-export-status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(
        json.dumps({"ok": True, "row_count": 0, "feed_error": "http_429"}),
        encoding="utf-8",
    )

    status = meme.optional_meme_source_status()

    assert status["wind_monitor"]["enabled"] is True
    assert status["wind_monitor"]["freshness_level"] == "yellow"
    assert status["wind_monitor"]["freshness_label"] == "听风feed限流"
    assert status["wind_monitor"]["exporter"]["feed_error"] == "http_429"


def test_fetch_gmgn_cli_sources_reads_official_live_trending(monkeypatch, tmp_path):
    monkeypatch.delenv("GMGN_CLI_DISABLE", raising=False)
    monkeypatch.setenv("GMGN_TRENCHES_CLI_DISABLE", "1")
    monkeypatch.setenv("GMGN_CLI_CHAINS", "bsc")
    monkeypatch.setenv("GMGN_CLI_INTERVALS", "1m")
    monkeypatch.setenv("GMGN_CLI_LIMIT", "5")
    monkeypatch.setattr(meme.shutil, "which", lambda name: "npx")
    monkeypatch.setattr(meme, "ROOT", tmp_path)
    monkeypatch.setattr(meme, "read_gmgn_skills_status", lambda: {})

    class Result:
        returncode = 0
        stderr = ""
        stdout = (
            '{"code":0,"data":{"rank":[{'
            '"chain":"bsc","address":"0xdd7960463036f3919f9dbd0b26fe06fd4da67777",'
            '"symbol":"雪王来了","name":"The Snow King is here","price":0.000013,'
            '"price_change_percent1m":18,"price_change_percent5m":45,"price_change_percent1h":120,'
            '"volume":66000,"liquidity":12000,"market_cap":13370,"swaps":900,'
            '"holder_count":188,"smart_degen_count":12,"renowned_count":3,'
            '"top_10_holder_rate":0.19,"hot_level":3'
            '}]}}'
        )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert "GMGN_PRIVATE_KEY" not in kwargs["env"]
        return Result()

    monkeypatch.setattr(meme.subprocess, "run", fake_run)

    sources, errors = meme.fetch_gmgn_cli_sources(limit=20)

    assert errors == []
    assert calls[0][:3] == ["npx", "--yes", "gmgn-cli"]
    row = sources["bsc:0xdd7960463036f3919f9dbd0b26fe06fd4da67777"]
    assert row["sources"] == ["gmgn_live_trending"]
    assert row["profile"]["symbol"] == "雪王来了"
    assert row["profile"]["smart_degen_count"] == 12
    assert row["profile"]["gmgn_cli_interval"] == "1m"


def test_fetch_gmgn_cli_sources_reads_official_trenches(monkeypatch, tmp_path):
    monkeypatch.delenv("GMGN_CLI_DISABLE", raising=False)
    monkeypatch.delenv("GMGN_TRENCHES_CLI_DISABLE", raising=False)
    monkeypatch.setenv("GMGN_CLI_INTERVALS", "")
    monkeypatch.setenv("GMGN_TRENCHES_CLI_CHAINS", "bsc")
    monkeypatch.setenv("GMGN_TRENCHES_CLI_TYPES", "new_creation,near_completion")
    monkeypatch.setenv("GMGN_TRENCHES_CLI_LIMIT", "5")
    monkeypatch.setattr(meme.shutil, "which", lambda name: "npx")
    monkeypatch.setattr(meme, "ROOT", tmp_path)
    monkeypatch.setattr(meme, "read_gmgn_skills_status", lambda: {})

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "new_creation": [
                    {
                        "chain": "bsc",
                        "address": "0xFresh7777",
                        "symbol": "FRESH",
                        "name": "Fresh BSC Meme",
                        "market_cap": 6200,
                        "liquidity": 2100,
                        "volume_24h": 8900,
                        "smart_degen_count": 4,
                        "renowned_count": 1,
                        "top_10_holder_rate": 0.16,
                    }
                ],
                "near_completion": [
                    {
                        "chain": "bsc",
                        "address": "0xCurve7777",
                        "symbol": "CURVE",
                        "name": "Near Completion",
                        "market_cap": 28_000,
                        "liquidity": 9_000,
                        "volume_24h": 44_000,
                        "smart_degen_count": 12,
                        "renowned_count": 3,
                        "top_10_holder_rate": 0.21,
                    }
                ],
            }
        )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert "GMGN_PRIVATE_KEY" not in kwargs["env"]
        return Result()

    monkeypatch.setattr(meme.subprocess, "run", fake_run)

    sources, errors = meme.fetch_gmgn_cli_sources(limit=20)

    assert errors == []
    assert calls[0][:5] == ["npx", "--yes", "gmgn-cli", "market", "trenches"]
    assert "--type" in calls[0]
    assert sources["bsc:0xfresh7777"]["sources"] == ["gmgn_trenches"]
    assert sources["bsc:0xfresh7777"]["profile"]["market_cap"] == 6200
    assert sources["bsc:0xfresh7777"]["profile"]["volume_24h"] == 8900
    assert sources["bsc:0xcurve7777"]["profile"]["gmgn_trenches_types"] == "new_creation,near_completion"


def test_fresh_skills_cache_disables_all_legacy_gmgn_cli_calls(monkeypatch, tmp_path):
    monkeypatch.delenv("GMGN_CLI_DISABLE", raising=False)
    monkeypatch.delenv("GMGN_TRENCHES_CLI_DISABLE", raising=False)
    monkeypatch.setenv("GMGN_CLI_CHAINS", "bsc")
    monkeypatch.setenv("GMGN_TRENCHES_CLI_CHAINS", "bsc")
    monkeypatch.setattr(meme, "ROOT", tmp_path)
    inbox = tmp_path / "outputs" / "meme-source-inbox"
    inbox.mkdir(parents=True)
    for name in ("gmgn-skills-trending.json", "gmgn-skills-trenches.json"):
        (inbox / name).write_text(json.dumps({"rows": [{"chain": "bsc", "address": "0x" + "a" * 40}]}))
    monkeypatch.setattr(meme.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy GMGN CLI must not run")))

    sources, errors = meme.fetch_gmgn_cli_sources(limit=20)

    assert sources == {}
    assert errors == []


def test_gmgn_skills_all_feed_files_are_local_sources(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    monkeypatch.setenv("ALPHA_MEME_SOURCE_INBOX_DIR", str(tmp_path))
    expected = {
        "gmgn-skills-signal.json": "gmgn_skills_signal",
        "gmgn-skills-trenches.json": "gmgn_skills_trenches",
        "gmgn-skills-kol.json": "gmgn_skills_kol",
        "gmgn-skills-hot-searches.json": "gmgn_skills_hot_searches",
    }
    for name in expected:
        (tmp_path / name).write_text(json.dumps({"rows": [{"chain": "bsc", "address": "0x" + "a" * 40}]}))

    specs = set(meme.local_inbox_file_source_specs())

    assert {(str(tmp_path / name), source) for name, source in expected.items()} <= specs


def test_heat_score_uses_only_strongest_signal_from_same_provider_group():
    item = {
        "sources": ["gmgn_skills_signal", "gmgn_skills_trending", "gmgn_skills_kol"],
        "profile": {},
    }

    metrics = meme.heat_metrics(item)

    assert metrics["heat_score"] == max(
        meme.HEAT_SOURCE_WEIGHTS["gmgn_skills_signal"],
        meme.HEAT_SOURCE_WEIGHTS["gmgn_skills_trending"],
        meme.HEAT_SOURCE_WEIGHTS["gmgn_skills_kol"],
    )

    okx_metrics = meme.heat_metrics({"sources": ["okx_signal", "okx_trenches"], "profile": {}})
    assert okx_metrics["heat_score"] == max(
        meme.HEAT_SOURCE_WEIGHTS["okx_signal"],
        meme.HEAT_SOURCE_WEIGHTS["okx_trenches"],
    )


def test_hidden_subprocess_kwargs_hide_windows_console(monkeypatch):
    monkeypatch.setattr(meme.os, "name", "nt")
    monkeypatch.setattr(meme.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    assert meme.hidden_subprocess_kwargs() == {"creationflags": 0x08000000}


def test_normalize_external_meme_row_reads_mobula_contracts_array():
    normalized = meme.normalize_external_meme_row(
        {
            "name": "DINOSOL",
            "symbol": "DINO",
            "contracts": [{"address": "MintDino", "blockchain": "Solana", "decimals": 6}],
            "price": 0.000015,
            "price_change_24h": -1.4,
            "trending_score": 1.5,
        },
        "mobula_trending",
    )

    assert normalized is not None
    assert normalized["chainId"] == "solana"
    assert normalized["tokenAddress"] == "MintDino"
    assert normalized["profile"]["price_change_percent24h"] == -1.4
    assert normalized["profile"]["gmgn_score"] == 1.5


def test_external_source_row_limit_caps_demo_sources(monkeypatch):
    monkeypatch.delenv("MOBULA_SOURCE_LIMIT", raising=False)
    monkeypatch.delenv("DEBOT_SOURCE_LIMIT", raising=False)
    monkeypatch.delenv("WIND_MONITOR_SOURCE_LIMIT", raising=False)

    assert meme.external_source_row_limit("mobula_trending", requested_limit=60) == 15
    assert meme.external_source_row_limit("debot_signal", requested_limit=60) == 20
    assert meme.external_source_row_limit("wind_monitor", requested_limit=60) == 45
    assert meme.external_source_row_limit("gmgn_trending", requested_limit=60) == 60


def test_fetch_external_meme_sources_caps_rows_before_dex_confirmation(monkeypatch):
    monkeypatch.setenv("MOBULA_DISABLE_DEFAULT_TRENDING", "1")
    monkeypatch.setenv("MOBULA_TRENDING_URLS", "https://example.invalid/mobula")
    rows = [
        {"chainId": "solana", "tokenAddress": f"Mint{i}", "symbol": f"M{i}", "market_cap": 100_000, "liquidity": 20_000}
        for i in range(30)
    ]
    monkeypatch.setattr(alpha, "http_json", lambda url, **kwargs: rows)

    sources, errors = meme.fetch_external_meme_sources(limit=60)

    assert errors == []
    assert len(sources) == 15


def test_fetch_profile_sources_merges_external_sources(monkeypatch):
    monkeypatch.setattr(meme, "load_local_inbox_sources", lambda limit: ({}, []))
    monkeypatch.setitem(meme.fetch_profile_sources.__globals__, "fetch_gmgn_sources", lambda limit: ({}, []))
    monkeypatch.setitem(
        meme.fetch_profile_sources.__globals__,
        "fetch_external_meme_sources",
        lambda limit, **_kwargs: (
            {
                "solana:mintx": {
                    "chainId": "solana",
                    "tokenAddress": "MintX",
                    "sources": ["mobula_trending"],
                    "profile": {"symbol": "MOBX"},
                }
            },
            [],
        ),
    )
    monkeypatch.setattr(alpha, "http_json", lambda url: [])

    sources, errors = meme.fetch_profile_sources(limit=3)

    assert errors == []
    assert sources["solana:mintx"]["sources"] == ["mobula_trending"]
    assert sources["solana:mintx"]["profile"]["symbol"] == "MOBX"


def test_fetch_profile_sources_does_not_reread_local_inbox_as_optional_source(monkeypatch):
    token = "0x" + "a" * 40
    external_calls = []

    monkeypatch.setattr(
        meme,
        "load_local_inbox_sources",
        lambda limit: (
            {
                f"bsc:{token}": {
                    "chainId": "bsc",
                    "tokenAddress": token,
                    "sources": ["985_monitor"],
                    "profile": {"symbol": "LOCAL"},
                }
            },
            [],
        ),
    )
    monkeypatch.setitem(
        meme.fetch_profile_sources.__globals__,
        "fetch_gmgn_sources",
        lambda limit, **_kwargs: ({}, []),
    )

    def fake_external(limit, **kwargs):
        external_calls.append(kwargs)
        if kwargs.get("include_local_files", True):
            return (
                {
                    f"bsc:{token}": {
                        "chainId": "bsc",
                        "tokenAddress": token,
                        "sources": ["985_monitor"],
                        "profile": {"symbol": "LOCAL"},
                    }
                },
                [],
            )
        return {}, []

    monkeypatch.setitem(
        meme.fetch_profile_sources.__globals__,
        "fetch_external_meme_sources",
        fake_external,
    )
    monkeypatch.setattr(alpha, "http_json", lambda url, **kwargs: [])

    sources, errors = meme.fetch_profile_sources(limit=3, observed_at="2026-09-11T00:00:00+08:00")

    assert errors == []
    assert external_calls == [
        {"observed_at": "2026-09-11T00:00:00+08:00", "include_local_files": False}
    ]
    assert sources[f"bsc:{token}"]["sources"] == ["985_monitor"]


def test_fetch_profile_sources_keeps_base_sources_when_optional_source_times_out(monkeypatch):
    local_token = "0x" + "a" * 40
    class ImmediateFuture:
        def cancel(self):
            return False

        def result(self):
            return (
                {
                    "solana:mintx": {
                        "chainId": "solana",
                        "tokenAddress": "MintX",
                        "sources": ["gmgn_trending"],
                        "profile": {"symbol": "MOBX"},
                    }
                },
                [],
            )

    class NeverFinishedFuture:
        def cancel(self):
            return True

        def result(self):
            raise AssertionError("timed out future should not be read")

    futures = [ImmediateFuture(), NeverFinishedFuture()]

    class FakePool:
        def __init__(self, max_workers):
            self.max_workers = max_workers
            self.shutdown_calls = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.shutdown(wait=True)
            return False

        def shutdown(self, wait=True, cancel_futures=False):
            self.shutdown_calls.append({"wait": wait, "cancel_futures": cancel_futures})

        def submit(self, fn, *args, **kwargs):
            if self.max_workers == 2:
                return futures.pop(0)

            class DoneFuture:
                def cancel(self_inner):
                    return False

                def result(self_inner):
                    return fn(*args, **kwargs)

            return DoneFuture()

    def fake_as_completed(items, timeout=None):
        if timeout == 30:
            yield items[0]
            raise TimeoutError("1 (of 2) futures unfinished")
        yield from items

    monkeypatch.setattr(meme, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(meme, "as_completed", fake_as_completed)
    monkeypatch.setattr(alpha, "http_json", lambda url, **kwargs: [])
    monkeypatch.setattr(
        meme,
        "load_local_inbox_sources",
        lambda limit: (
            {
                f"bsc:{local_token}": {
                    "chainId": "bsc",
                    "tokenAddress": local_token,
                    "sources": ["985_monitor"],
                    "profile": {"symbol": "LOCAL"},
                }
            },
            [],
        ),
    )

    sources, errors = meme.fetch_profile_sources(limit=3)

    assert sources["solana:mintx"]["profile"]["symbol"] == "MOBX"
    assert sources[f"bsc:{local_token}"]["sources"] == ["985_monitor"]
    assert any("optional meme source timeout" in error for error in errors)


def test_load_local_inbox_sources_merges_same_token_without_network(tmp_path, monkeypatch):
    token = "0x" + "a" * 40
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"data": [{
        "chain": "robinhood", "address": token, "symbol": "FAST",
        "market_cap": 50_000, "liquidity": 25_000, "volume": 60_000,
    }]}), encoding="utf-8")
    second.write_text(json.dumps({"data": [{
        "chain": "robinhood", "address": token, "symbol": "FAST",
        "market_cap": 52_000, "liquidity": 26_000, "volume": 70_000,
    }]}), encoding="utf-8")
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc,robinhood")
    monkeypatch.setattr(
        meme,
        "external_file_source_specs",
        lambda: [(str(first), "noxa_launchpad"), (str(second), "wind_monitor")],
    )

    sources, errors = meme.load_local_inbox_sources(20)

    assert errors == []
    assert list(sources) == [f"robinhood:{token}"]
    assert sources[f"robinhood:{token}"]["sources"] == ["noxa_launchpad", "wind_monitor"]
    assert sources[f"robinhood:{token}"]["profile"]["market_cap"] == 52_000
    events = sources[f"robinhood:{token}"]["source_events"]
    assert {event["provider_family"] for event in events} == {"noxa", "wind"}
    assert all(event["event_time_basis"] == "snapshot_observed_at" for event in events)


def test_load_meme_candidates_keeps_external_rows_when_dex_pairs_fail(monkeypatch):
    monkeypatch.setitem(
        meme.load_meme_candidates.__globals__,
        "fetch_profile_sources",
        lambda limit, **_kwargs: (
            {
                "solana:birdmint": {
                    "chainId": "solana",
                    "tokenAddress": "BirdMint",
                    "sources": ["birdeye_trending"],
                    "profile": {
                        "chain": "solana",
                        "address": "BirdMint",
                        "symbol": "BIRD",
                        "name": "Bird Meme",
                        "market_cap": 210_000,
                        "liquidity": 41_000,
                        "volume": 320_000,
                        "price": 0.00021,
                        "price_change_percent1h": 36,
                        "price_change_percent5m": 8,
                        "holder_count": 780,
                        "source_family": "birdeye_trending",
                    },
                }
            },
            [],
        ),
    )
    monkeypatch.setitem(meme.load_meme_candidates.__globals__, "fetch_pairs_for_source", lambda item: (_ for _ in ()).throw(RuntimeError("dex fail")))

    rows, errors = meme.load_meme_candidates(limit=10, concurrency=1)

    assert errors == []
    assert rows[0]["symbol"] == "BIRD"
    assert rows[0]["source_labels"] == ["Birdeye", "DS", "Alpha_AI"]
    assert rows[0]["mcap"] == 210_000
    assert rows[0]["url"] == ""


def test_cap_score_limits_meme_score_to_100():
    assert meme.cap_score(119.2) == 100.0
    assert meme.cap_score(-5) == 0.0


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

    monkeypatch.setattr(alpha, "fetch_solana_holder_metrics", fake_fetch_solana_holder_metrics)
    rows = [
        {"symbol": "AAA", "chain": "solana", "contract_address": "MintA"},
        {"symbol": "BBB", "chain": "bsc", "contract_address": "0x2"},
    ]

    errors = meme.apply_solana_holder_metrics_to_meme_rows(rows, limit=3)

    assert errors == []
    assert calls == ["MintA"]
    assert rows[0]["top10_holder_pct"] == 42.5
    assert rows[0]["holder_source"] == "solana_rpc_holderlist"
    assert "top10_holder_pct" not in rows[1]
