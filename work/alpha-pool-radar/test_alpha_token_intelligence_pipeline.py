import ast
import json
import threading
import time
from pathlib import Path

import alpha_radar_report as report_pipeline


NOW = "2026-09-10T12:00:00+00:00"
TOKEN_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TOKEN_C = "0xcccccccccccccccccccccccccccccccccccccccc"
UI_FIELDS = {
    "status",
    "one_line_judgement",
    "project_narrative",
    "attention_evidence",
    "smart_wallets",
    "identity",
    "risks",
    "missing_evidence",
    "sources",
    "ai_narrative",
    "official_ca_status",
    "ca_conflict",
    "social_source_count",
    "wash_score",
    "flow_acceleration",
    "ai_risk_flags",
    "evidence_urls",
    "ai_analyzed_at",
    "model_generated_at",
    "generated_at",
}


def market_row(contract: str, symbol: str) -> dict:
    return {
        "chain": "bsc",
        "contract_address": contract,
        "symbol": symbol,
        "market_cap": 125_000,
        "liquidity": 48_000,
        "volume24h": 91_000,
        "quote_source": "dexscreener",
        "quote_observed_at": NOW,
        "dex_url": f"https://dexscreener.com/bsc/{contract}",
    }


def ui_record(contract: str, judgement: str) -> dict:
    return {
        "status": "partial",
        "one_line_judgement": judgement,
        "project_narrative": judgement,
        "attention_evidence": [],
        "smart_wallets": [],
        "identity": {
            "official_status": "unknown",
            "official_contract": "",
            "alternate_contracts": [],
        },
        "risks": [],
        "missing_evidence": ["official_identity"],
        "sources": [],
        "ai_narrative": judgement,
        "official_ca_status": "unknown",
        "ca_conflict": False,
        "social_source_count": 0,
        "wash_score": None,
        "flow_acceleration": None,
        "ai_risk_flags": [],
        "evidence_urls": [],
        "ai_analyzed_at": NOW,
        "model_generated_at": NOW,
        "generated_at": NOW,
        "contract_address": contract,
    }


def cache_payload(records: dict[str, dict]) -> dict:
    return {
        "schema_version": 1,
        "status": "partial",
        "generated_at": NOW,
        "records": records,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_cached_intelligence_attaches_to_every_row_collection_by_exact_identity():
    exact = ui_record(TOKEN_A, "exact-contract")
    wrong = ui_record(TOKEN_B, "same-symbol-wrong-contract")
    cache = cache_payload({f"bsc:{TOKEN_A}": exact, f"bsc:{TOKEN_B}": wrong})
    report = {
        "alpha_rows": [market_row(TOKEN_A.upper().replace("0X", "0x"), "SAME")],
        "meme_rows": [market_row(TOKEN_A, "SAME")],
        "meme_shadow_rows": [market_row(TOKEN_B, "SAME")],
        "custom_future_rows": [market_row(TOKEN_A, "SAME")],
        "meta": {},
    }

    attached = report_pipeline.attach_token_intelligence(report, cache, now=NOW)

    for name in ("alpha_rows", "meme_rows", "custom_future_rows"):
        assert report[name][0]["token_intelligence"]["one_line_judgement"] == "exact-contract"
    assert report["meme_shadow_rows"][0]["token_intelligence"]["one_line_judgement"] == "same-symbol-wrong-contract"
    assert attached == 4
    assert report["meta"]["token_intelligence"]["record_count"] == 2
    assert report["meta"]["token_intelligence"]["attached_count"] == 4


def test_refresh_emits_ui_schema_and_keeps_disappeared_watch_candidates(tmp_path):
    report = {
        "meme_rows": [market_row(TOKEN_A, "LIVE")],
        "meme_watch_universe": [market_row(TOKEN_A, "LIVE")],
        "meta": {"report_generated_at": NOW},
    }
    disappeared = market_row(TOKEN_B, "GONE")
    disappeared["latest_seen_at"] = NOW
    gold_watch_only = market_row(TOKEN_C, "GOLDOLD")
    gold_watch_only["last_seen_at"] = NOW
    write_json(tmp_path / "alpha-radar-replay-history.json", {"updated_at": NOW, "rows": {f"bsc:{TOKEN_B}": disappeared}})
    write_json(tmp_path / "alpha-gold-watch-state.json", {"updated_at": NOW, "candidates": {f"bsc:{TOKEN_C}": gold_watch_only}})
    write_json(
        tmp_path / "alpha-fast-track.json",
        {
            "updated_at": NOW,
            "quotes": {f"bsc:{TOKEN_B}": disappeared},
            "evidence": {
                f"bsc:{TOKEN_B}": {
                    "confirmation_status": "confirmed",
                    "qualified_wallet_count": 2,
                    "candidate_sources": ["gmgn_wallet_flow"],
                    "latest_evidence_at": NOW,
                }
            },
        },
    )

    result = report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=False,
    )

    saved = json.loads((tmp_path / "token-intelligence.json").read_text(encoding="utf-8"))
    assert set(saved["records"]) == {f"bsc:{TOKEN_A}", f"bsc:{TOKEN_B}", f"bsc:{TOKEN_C}"}
    assert saved["records"][f"bsc:{TOKEN_B}"]["smart_wallets"]
    intelligence = report["meme_rows"][0]["token_intelligence"]
    assert set(intelligence) == UI_FIELDS
    assert intelligence["generated_at"] == NOW
    assert intelligence["one_line_judgement"].startswith("LIVE：")
    assert result["cache_written"] is True
    assert result["auxiliary_identity_count"] == 2


def test_pipeline_failure_retains_and_reattaches_last_valid_cache(tmp_path, monkeypatch):
    previous = cache_payload({f"bsc:{TOKEN_A}": ui_record(TOKEN_A, "last-valid")})
    cache_path = tmp_path / "token-intelligence.json"
    write_json(cache_path, previous)
    original_bytes = cache_path.read_bytes()
    report = {"meme_rows": [market_row(TOKEN_A, "LIVE")], "meta": {}}

    def fail_build(*args, **kwargs):
        raise RuntimeError("deterministic builder failed")

    monkeypatch.setattr(report_pipeline, "build_token_intelligence_records", fail_build)
    result = report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=False,
    )

    assert cache_path.read_bytes() == original_bytes
    assert report["meme_rows"][0]["token_intelligence"]["one_line_judgement"] == "last-valid"
    assert report["meta"]["token_intelligence"]["status"] == "stale"
    assert result["cache_written"] is False
    assert result["error"] == "RuntimeError"


def test_provider_timeout_uses_deterministic_report_data_without_replacing_cache(tmp_path, monkeypatch):
    import alpha_token_research

    previous = cache_payload({f"bsc:{TOKEN_A}": ui_record(TOKEN_A, "researched-cache")})
    cache_path = tmp_path / "token-intelligence.json"
    write_json(cache_path, previous)
    original_bytes = cache_path.read_bytes()
    report = {"meme_rows": [market_row(TOKEN_A, "LIVE")], "meta": {}}
    monkeypatch.setattr(
        alpha_token_research,
        "research_token_from_env",
        lambda *args, **kwargs: {
            "status": "provider_timeout",
            "official_contract": {"status": "unknown", "contracts": []},
            "sources": [],
        },
    )
    monkeypatch.setattr(
        alpha_token_research,
        "summarize_grounded",
        lambda evidence, fallback, **kwargs: {"status": "fallback", "summary": fallback},
    )
    monkeypatch.setenv("TOKEN_INTELLIGENCE_RESEARCH_FORCE", "1")

    result = report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=True,
    )

    assert cache_path.read_bytes() == original_bytes
    assert result["provider_failed"] is True
    assert result["cache_written"] is False
    assert report["meme_rows"][0]["token_intelligence"]["one_line_judgement"] == "researched-cache"


def test_fresh_model_summary_is_not_researched_again(tmp_path, monkeypatch):
    import alpha_token_research

    previous = cache_payload({f"bsc:{TOKEN_A}": ui_record(TOKEN_A, "fresh model narrative")})
    write_json(tmp_path / "token-intelligence.json", previous)
    report = {"meme_rows": [market_row(TOKEN_A, "LIVE")], "meta": {}}
    calls = []
    monkeypatch.setattr(
        alpha_token_research,
        "research_token_from_env",
        lambda *args, **kwargs: calls.append(args) or {"status": "ready", "sources": []},
    )

    result = report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=True,
    )

    assert calls == []
    assert result["research_attempted"] == 0
    assert report["meme_rows"][0]["token_intelligence"]["one_line_judgement"] == "fresh model narrative"


def test_fallback_text_without_ai_analysis_is_researched_again(tmp_path, monkeypatch):
    import alpha_token_research

    fallback = ui_record(TOKEN_A, "deterministic fallback")
    fallback["ai_analyzed_at"] = None
    write_json(tmp_path / "token-intelligence.json", cache_payload({f"bsc:{TOKEN_A}": fallback}))
    report = {"meme_rows": [market_row(TOKEN_A, "LIVE")], "meta": {}}
    calls = []
    monkeypatch.setattr(
        alpha_token_research,
        "research_token_from_env",
        lambda *args, **kwargs: calls.append(args) or {"status": "ready", "sources": []},
    )
    monkeypatch.setattr(
        alpha_token_research,
        "summarize_grounded",
        lambda evidence, summary, **kwargs: {"status": "fallback", "summary": summary},
    )

    result = report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=True,
    )

    assert len(calls) == 1
    assert result["research_attempted"] == 1


def test_partial_research_batch_writes_successful_records(tmp_path, monkeypatch):
    import alpha_token_research

    token_b = "0x2222222222222222222222222222222222222222"
    report = {
        "meme_potential_rows": [market_row(TOKEN_A, "FIRST"), market_row(token_b, "SECOND")],
        "meta": {},
    }

    def research(chain, contract, **kwargs):
        if contract == TOKEN_A:
            return {
                "status": "provider_timeout",
                "official_contract": {"status": "unknown", "contracts": []},
                "sources": [],
            }
        return {
            "status": "ready",
            "official_contract": {"status": "unknown", "contracts": []},
            "sources": [{"source_id": "dex-1", "title": "DexScreener", "url": "https://dexscreener.com/x"}],
        }

    monkeypatch.setenv("TOKEN_INTELLIGENCE_RESEARCH_LIMIT", "2")
    monkeypatch.setattr(alpha_token_research, "research_token_from_env", research)
    monkeypatch.setattr(
        alpha_token_research,
        "summarize_grounded",
        lambda evidence, fallback, **kwargs: {"status": "fallback", "summary": fallback},
    )

    result = report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=True,
    )

    assert result["provider_failed"] is True
    assert result["research_succeeded"] == 1
    assert result["cache_written"] is True
    payload = json.loads((tmp_path / "token-intelligence.json").read_text(encoding="utf-8"))
    assert "DexScreener" in {
        source["title"] for source in payload["records"][f"bsc:{token_b}"]["sources"]
    }


def test_research_prioritizes_meme_candidates_over_alpha_rows(tmp_path, monkeypatch):
    import alpha_token_research

    meme_token = "0x3333333333333333333333333333333333333333"
    calls = []
    report = {
        "alpha_rows": [market_row(TOKEN_A, "ALPHA")],
        "meme_potential_rows": [market_row(meme_token, "MEME")],
        "meta": {},
    }

    def research(chain, contract, **kwargs):
        calls.append(contract)
        return {"status": "ready", "official_contract": {"status": "unknown", "contracts": []}, "sources": []}

    monkeypatch.setenv("TOKEN_INTELLIGENCE_RESEARCH_LIMIT", "1")
    monkeypatch.setattr(alpha_token_research, "research_token_from_env", research)
    monkeypatch.setattr(
        alpha_token_research,
        "summarize_grounded",
        lambda evidence, fallback, **kwargs: {"status": "fallback", "summary": fallback},
    )

    report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=True,
    )

    assert calls == [meme_token]


def test_research_targets_keep_confirmed_pick_first(tmp_path):
    confirmed = "0x4444444444444444444444444444444444444444"
    ordinary = "0x5555555555555555555555555555555555555555"
    report = {
        "meme_rows": [market_row(ordinary, "LOW")],
        "gold_watch_confirmed_pick": market_row(confirmed, "HIGH"),
    }
    rows, _ = report_pipeline.collect_token_intelligence_rows(report, tmp_path, ())
    cores = report_pipeline.build_token_intelligence_records(rows, observed_at=NOW)

    targets = report_pipeline._token_intelligence_research_targets(report, cores, 2)

    assert targets == [f"bsc:{confirmed}", f"bsc:{ordinary}"]


def test_research_targets_do_not_spend_model_budget_on_auxiliary_history(tmp_path):
    historical = "0x6666666666666666666666666666666666666666"
    report = {"meme_rows": [], "meta": {}}
    cores = report_pipeline.build_token_intelligence_records(
        [market_row(historical, "HISTORY")],
        observed_at=NOW,
    )

    targets = report_pipeline._token_intelligence_research_targets(report, cores, 2)

    assert targets == []


def test_v3_research_targets_ignore_raw_discoveries_and_legacy_fallbacks(tmp_path):
    raw = market_row(TOKEN_A, "RAW")
    report = {
        "monitor_v3": {
            "schema_version": 3,
            "tokens": [{
                **raw,
                "active_states": ["new"],
                "freshness": {"status": "fresh"},
            }],
        },
        "meme_rows": [raw],
    }
    cores = report_pipeline.build_token_intelligence_records([raw], observed_at=NOW)

    targets = report_pipeline._token_intelligence_research_targets(report, cores, 2)

    assert targets == []


def test_v3_research_targets_only_use_fresh_unblocked_selected_states(tmp_path):
    building = market_row(TOKEN_A, "BUILD")
    smart_token = "0x7777777777777777777777777777777777777777"
    smart = market_row(smart_token, "SMART")
    stale_token = "0x8888888888888888888888888888888888888888"
    stale = market_row(stale_token, "STALE")
    blocked_token = "0x9999999999999999999999999999999999999999"
    blocked = market_row(blocked_token, "BLOCKED")
    downgraded_token = "0xdddddddddddddddddddddddddddddddddddddddd"
    downgraded = market_row(downgraded_token, "DOWNGRADED")
    report = {
        "monitor_v3": {
            "schema_version": 3,
            "tokens": [
                {**building, "active_states": ["building"], "freshness": {"status": "fresh"}},
                {**smart, "active_states": ["smart_cluster"], "freshness": {"status": "fresh"}},
                {**stale, "active_states": ["resonating"], "freshness": {"status": "stale"}},
                {**blocked, "active_states": ["building", "blocked_risk"], "freshness": {"status": "fresh"}},
                {**downgraded, "active_states": ["smart_cluster", "trend_watch"], "freshness": {"status": "fresh"}},
            ],
        },
    }
    cores = report_pipeline.build_token_intelligence_records(
        [building, smart, stale, blocked, downgraded],
        observed_at=NOW,
    )

    targets = report_pipeline._token_intelligence_research_targets(report, cores, 8)

    assert targets == [f"bsc:{smart_token}", f"bsc:{TOKEN_A}"]


def test_research_exact_match_normalizes_to_ui_match(tmp_path, monkeypatch):
    import alpha_token_research

    report = {"meme_rows": [market_row(TOKEN_A, "LIVE")], "meta": {}}
    monkeypatch.setattr(
        alpha_token_research,
        "research_token_from_env",
        lambda *args, **kwargs: {
            "status": "ready",
            "official_contract": {"status": "exact_match", "contracts": [TOKEN_A]},
            "sources": [],
        },
    )
    monkeypatch.setattr(
        alpha_token_research,
        "summarize_grounded",
        lambda evidence, fallback, **kwargs: {"status": "fallback", "summary": fallback},
    )

    report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=True,
    )

    identity = report["meme_rows"][0]["token_intelligence"]["identity"]
    assert identity["official_status"] == "match"
    assert identity["official_contract"] == TOKEN_A


def test_ui_record_unwraps_grounded_model_statements():
    core = {
        "status": "partial",
        "chain": "bsc",
        "contract_address": TOKEN_A,
        "fallback_summary": "fallback",
        "missing_evidence": [],
        "same_symbol_contracts": [],
        "source_references": [],
    }
    summary = {
        "status": "ready",
        "summary": {
            "one_line_judgement": {"text": "一句话结论", "evidence_ids": ["e1"]},
            "project_narrative": {"text": "项目叙事", "evidence_ids": ["e1"]},
            "attention_evidence": [],
            "smart_wallet_evidence": [],
            "risks": [],
        },
    }

    intelligence = report_pipeline._ui_record(
        core,
        NOW,
        research={"status": "ready", "sources": [], "official_contract": {"status": "unknown"}},
        summary=summary,
    )

    assert intelligence["one_line_judgement"] == "一句话结论"
    assert intelligence["project_narrative"] == "项目叙事"
    assert intelligence["ai_narrative"] == "项目叙事"
    assert intelligence["ai_analyzed_at"] == NOW
    assert intelligence["status"] == "ready"


def test_ui_record_exposes_shadow_features_without_changing_entry_state():
    core = {
        "status": "partial",
        "chain": "bsc",
        "contract_address": TOKEN_A,
        "fallback_summary": "fallback",
        "missing_evidence": [],
        "same_symbol_contracts": [],
        "source_references": [],
        "market_evidence": {
            "is_wash_trading": True,
            "flow_acceleration": 1.8,
        },
    }
    research = {
        "status": "ready",
        "official_contract": {"status": "mismatch", "contracts": [TOKEN_A, TOKEN_B]},
        "sources": [{
            "source_id": "social-1",
            "source": "public_search",
            "title": "X post",
            "url": "https://x.com/example/status/1",
            "observed_at": NOW,
        }],
    }
    summary = {
        "status": "ready",
        "summary": {
            "one_line_judgement": {"text": "一句话结论", "evidence_ids": ["social-1"]},
            "project_narrative": {"text": "项目叙事", "evidence_ids": ["social-1"]},
            "attention_evidence": [],
            "smart_wallet_evidence": [],
            "risks": [{"text": "存在多合约冲突", "evidence_ids": ["social-1"]}],
        },
    }

    intelligence = report_pipeline._ui_record(core, NOW, research=research, summary=summary)

    assert intelligence["official_ca_status"] == "mismatch"
    assert intelligence["ca_conflict"] is True
    assert intelligence["social_source_count"] == 1
    assert intelligence["wash_score"] == 100.0
    assert intelligence["flow_acceleration"] == 1.8
    assert intelligence["ai_risk_flags"] == ["存在多合约冲突"]
    assert intelligence["evidence_urls"] == ["https://x.com/example/status/1"]


def test_conflicting_nested_identity_never_attaches_cached_contract():
    report = {
        "meme_rows": [{
            **market_row(TOKEN_A, "SAME"),
            "identity": {"chain": "robinhood", "contract_address": TOKEN_B},
        }],
        "meta": {},
    }
    cache = cache_payload({f"bsc:{TOKEN_A}": ui_record(TOKEN_A, "must-not-attach")})

    attached = report_pipeline.attach_token_intelligence(report, cache, now=NOW)

    assert attached == 0
    assert "token_intelligence" not in report["meme_rows"][0]


def test_cache_validation_filters_bad_sources_and_sensitive_query_values(tmp_path):
    valid = ui_record(TOKEN_A, "safe")
    valid["sources"] = [
        None,
        {"source_id": "s1", "title": "source", "url": "https://example.com/x?q=ok&api_key=secret#token=bad"},
    ]
    invalid = ui_record(TOKEN_B, "bad")
    invalid["one_line_judgement"] = {"text": "not-renderable"}
    write_json(tmp_path / "token-intelligence.json", cache_payload({
        f"bsc:{TOKEN_A}": valid,
        f"bsc:{TOKEN_B}": invalid,
    }))

    loaded = report_pipeline.load_token_intelligence_cache(tmp_path / "token-intelligence.json")

    assert list(loaded["records"]) == [f"bsc:{TOKEN_A}"]
    assert loaded["records"][f"bsc:{TOKEN_A}"]["sources"] == [{
        "source_id": "s1",
        "title": "source",
        "url": "https://example.com/x?q=ok",
        "observed_at": None,
    }]


def test_deterministic_refresh_preserves_previous_model_text(tmp_path):
    previous = ui_record(TOKEN_A, "previous model narrative")
    previous.update({
        "ai_narrative": "previous AI narrative",
        "official_ca_status": "mismatch",
        "ca_conflict": True,
        "social_source_count": 3,
        "wash_score": 72.0,
        "flow_acceleration": 1.8,
        "ai_risk_flags": ["多合约冲突"],
        "evidence_urls": ["https://example.com/evidence"],
        "ai_analyzed_at": NOW,
    })
    previous["identity"]["official_status"] = "mismatch"
    write_json(tmp_path / "token-intelligence.json", cache_payload({f"bsc:{TOKEN_A}": previous}))
    report = {"meme_rows": [market_row(TOKEN_A, "LIVE")], "meta": {}}

    result = report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=False,
    )

    assert result["cache_written"] is True
    intelligence = report["meme_rows"][0]["token_intelligence"]
    assert intelligence["one_line_judgement"] == "previous model narrative"
    assert intelligence["ai_narrative"] == "previous AI narrative"
    assert intelligence["official_ca_status"] == "mismatch"
    assert intelligence["ca_conflict"] is True
    assert intelligence["social_source_count"] == 3
    assert intelligence["wash_score"] == 72.0
    assert intelligence["flow_acceleration"] == 1.8
    assert intelligence["ai_risk_flags"] == ["多合约冲突"]
    assert intelligence["evidence_urls"] == ["https://example.com/evidence"]
    assert intelligence["ai_analyzed_at"] == NOW


def test_deterministic_refresh_does_not_renew_stale_model_text(tmp_path):
    previous = ui_record(TOKEN_A, "stale model narrative")
    previous["generated_at"] = "2020-01-01T00:00:00+00:00"
    previous["model_generated_at"] = "2020-01-01T00:00:00+00:00"
    write_json(tmp_path / "token-intelligence.json", cache_payload({f"bsc:{TOKEN_A}": previous}))
    report = {"meme_rows": [market_row(TOKEN_A, "LIVE")], "meta": {}}

    report_pipeline.refresh_token_intelligence(
        report,
        out_dir=tmp_path,
        now=NOW,
        research_enabled=False,
    )

    intelligence = report["meme_rows"][0]["token_intelligence"]
    assert intelligence["one_line_judgement"].startswith("LIVE：")
    assert intelligence["model_generated_at"] is None


def test_source_url_redacts_secret_suffixes():
    value = report_pipeline._sanitize_source_url(
        "https://example.com/path?ok=1&client_secret=a&password=b&x-api-key=c&clientSecret=d&accessToken=e&privateKey=f#token=g"
    )

    assert value == "https://example.com/path?ok=1"


def test_cache_validation_sanitizes_shadow_urls_and_tolerates_bad_counts(tmp_path):
    valid = ui_record(TOKEN_A, "safe")
    valid["social_source_count"] = "not-a-number"
    valid["evidence_urls"] = ["https://example.com/proof?ok=1&api_key=secret#token=bad"]
    write_json(tmp_path / "token-intelligence.json", cache_payload({f"bsc:{TOKEN_A}": valid}))

    loaded = report_pipeline.load_token_intelligence_cache(tmp_path / "token-intelligence.json")
    intelligence = loaded["records"][f"bsc:{TOKEN_A}"]

    assert intelligence["social_source_count"] is None
    assert intelligence["evidence_urls"] == ["https://example.com/proof?ok=1"]


def test_future_intelligence_timestamp_is_not_fresh():
    assert report_pipeline._intelligence_freshness(
        "2030-01-01T00:00:00+00:00",
        NOW,
    ) == ("unknown", None)


def test_background_research_schedule_returns_without_waiting(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()

    def slow_refresh(*args, **kwargs):
        started.set()
        release.wait(1)

    monkeypatch.setenv("TOKEN_INTELLIGENCE_RESEARCH_ENABLED", "1")
    monkeypatch.setattr(report_pipeline, "refresh_token_intelligence", slow_refresh)
    before = time.monotonic()
    scheduled = report_pipeline.schedule_token_intelligence_research({}, out_dir=tmp_path, now=NOW)
    elapsed = time.monotonic() - before

    assert scheduled is True
    assert elapsed < 0.1
    assert started.wait(0.5)
    release.set()
    assert report_pipeline.wait_for_token_intelligence_research(1)


def test_background_research_publishes_completed_cache(monkeypatch, tmp_path):
    published = threading.Event()
    report_path = tmp_path / "report.json"
    monkeypatch.setenv("TOKEN_INTELLIGENCE_RESEARCH_ENABLED", "1")
    monkeypatch.setattr(report_pipeline, "refresh_token_intelligence", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        report_pipeline,
        "publish_token_intelligence_cache",
        lambda path, out_dir, now: published.set() or 1,
    )

    scheduled = report_pipeline.schedule_token_intelligence_research(
        {},
        out_dir=tmp_path,
        now=NOW,
        report_path=report_path,
    )

    assert scheduled is True
    assert published.wait(0.5)
    assert report_pipeline.wait_for_token_intelligence_research(1)


def test_execution_and_trade_worker_modules_do_not_import_or_await_research():
    base = Path(__file__).parent
    protected = (
        "alpha_fast_track.py",
        "alpha_bsc_live_adapter.py",
        "alpha_okx_live_worker.py",
        "alpha_gmgn_live_worker.py",
    )
    for name in protected:
        tree = ast.parse((base / name).read_text(encoding="utf-8"), filename=name)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        assert "alpha_token_research" not in imports, name
        awaited_names = {
            child.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            for child in ast.walk(node.value)
            if isinstance(child, ast.Name)
        }
        assert not {"research_token", "research_token_async", "summarize_grounded"} & awaited_names, name
