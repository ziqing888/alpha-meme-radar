from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import pytest
import alpha_token_research as research_module

from alpha_token_research import (
    assess_official_contract,
    bound_provider_results,
    build_contract_queries,
    compact_evidence_bundle,
    configured_search_provider,
    dexscreener_search_provider,
    is_cache_fresh,
    load_fresh_cache,
    parse_duckduckgo_html,
    public_page_fetcher,
    research_token,
    save_research_cache,
    summarize_grounded,
    summarize_monitor_signal,
    validate_grounded_summary,
)


TOKEN = "0x7f2673ba3d0679224a65b70aefe84f9e6a3cc376"
OTHER_TOKEN = "0x" + "b" * 40
NOW = "2026-09-10T02:00:00+00:00"


def cited(text, *evidence_ids):
    return {"text": text, "evidence_ids": list(evidence_ids or ("source-1",))}


def valid_summary():
    return {
        "one_line_judgement": cited("合约身份一致，但公开信息仍有限。"),
        "project_narrative": cited("官网公开了与观察标的一致的合约。"),
        "attention_evidence": [cited("公开检索命中项目官网。")],
        "smart_wallet_evidence": [],
        "risks": [cited("缺少更长时间窗口。")],
    }


def test_adapter_timeout_caps_orphaned_calls(monkeypatch):
    release = threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(research_module, "_ADAPTER_CALL_SLOTS", slots)

    with pytest.raises(TimeoutError, match="adapter_timeout"):
        research_module._invoke_with_timeout(lambda: release.wait(1), 0.01)
    with pytest.raises(TimeoutError, match="adapter_capacity_exhausted"):
        research_module._invoke_with_timeout(lambda: None, 0.01)

    release.set()
    assert slots.acquire(timeout=1)
    slots.release()


def test_compact_bundle_redacts_camel_case_secrets_and_url_queries():
    bundle = compact_evidence_bundle({
        "chain": "bsc",
        "contract": TOKEN,
        "market_evidence": {"clientSecret": "LEAK1", "tokenAddress": TOKEN},
        "sources": [{
            "source_id": "source-1",
            "source": "public_search",
            "observed_at": NOW,
            "chain": "bsc",
            "contract": TOKEN,
            "url": "https://example.com/?accessToken=LEAK2&ok=1",
            "snippet": TOKEN,
        }],
    })
    serialized = json.dumps(bundle)

    assert "LEAK1" not in serialized
    assert "LEAK2" not in serialized
    assert "accessToken=%5BREDACTED%5D" in serialized
    assert bundle["market_evidence"]["tokenAddress"] == TOKEN


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=-1):
        return json.dumps(self.payload).encode("utf-8")


class RawResponse(FakeResponse):
    def read(self, limit=-1):
        return self.payload if isinstance(self.payload, bytes) else str(self.payload).encode("utf-8")


def test_queries_and_provider_results_remain_contract_scoped_and_bounded():
    queries = build_contract_queries("bsc", TOKEN, symbol="CLUBY", name="Cluby")
    raw = [
        {"title": "Official", "url": "https://cluby.example", "snippet": TOKEN, "is_official": True},
        {"title": "Duplicate", "url": "https://cluby.example", "snippet": "same URL"},
        {"title": "Social", "url": "https://x.com/cluby", "snippet": f"public post {TOKEN}"},
        {"title": "Ignored", "url": "javascript:alert(1)", "snippet": "bad scheme"},
    ]
    bounded = bound_provider_results(
        raw,
        query=queries[0],
        chain="bsc",
        contract=TOKEN,
        observed_at=NOW,
        limit=2,
    )

    assert len(queries) == 3
    assert all(TOKEN in query for query in queries)
    assert queries[0] == TOKEN
    assert any("bsc" in query.lower() for query in queries[1:])
    assert any(f'"{TOKEN}"' not in query for query in queries)
    assert [item["url"] for item in bounded] == ["https://cluby.example", "https://x.com/cluby"]
    assert all(item["contract"] == TOKEN and item["observed_at"] == NOW for item in bounded)


def test_unrelated_provider_result_requires_exact_contract_in_result_or_fetched_page():
    unrelated = [{
        "title": "Another Cluby project",
        "url": "https://unrelated.example/token",
        "snippet": "Same symbol, different contract",
        "is_official": True,
    }]
    assert bound_provider_results(
        unrelated,
        query=f'"{TOKEN}" bsc project',
        chain="bsc",
        contract=TOKEN,
        observed_at=NOW,
    ) == []

    provider_calls = []

    def provider(query, timeout):
        provider_calls.append(query)
        return unrelated if len(provider_calls) == 1 else []

    without_page = research_token("bsc", TOKEN, now=NOW, provider=provider)
    provider_calls.clear()
    with_page = research_token(
        "bsc",
        TOKEN,
        now=NOW,
        provider=provider,
        page_fetcher=lambda url, timeout: f"<main>Contract {TOKEN}</main>",
    )

    assert without_page["sources"] == []
    assert without_page["official_contract"]["status"] == "unknown"
    assert len(with_page["sources"]) == 1
    assert with_page["sources"][0]["contract"] == TOKEN
    assert with_page["sources"][0]["grounding"] == "fetched_page_exact_contract"
    assert with_page["official_contract"]["status"] == "exact_match"


def test_public_search_provider_parses_redirects_and_can_be_disabled():
    page = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcluby.cash%2Ftoken">'
        'Cluby &amp; Token</a><div class="result__snippet">Official CA ' + TOKEN + "</div>"
    )
    parsed = parse_duckduckgo_html(page)

    assert parsed == [{
        "title": "Cluby & Token",
        "url": "https://cluby.cash/token",
        "snippet": f"Official CA {TOKEN}",
        "source": "duckduckgo",
    }]
    assert configured_search_provider({"TOKEN_RESEARCH_PUBLIC_SEARCH": "0"}) is None
    assert callable(configured_search_provider({}))


def test_dexscreener_exact_contract_provider_exposes_market_and_claimed_links():
    payload = [{
        "chainId": "bsc",
        "url": "https://dexscreener.com/bsc/pair",
        "baseToken": {"address": TOKEN.upper(), "name": "Cluby Credit", "symbol": "CLUBY"},
        "marketCap": 139659,
        "liquidity": {"usd": 29510},
        "volume": {"h1": 90105},
        "txns": {"h1": {"buys": 84, "sells": 53}},
        "info": {
            "websites": [{"url": "https://cluby.example/token", "label": "Website"}],
            "socials": [{"url": "https://x.com/cluby", "type": "twitter"}],
        },
    }]
    provider = dexscreener_search_provider("bsc", TOKEN, opener=lambda request, timeout: FakeResponse(payload))

    results = provider(TOKEN, 2)

    assert results[0]["source"] == "dexscreener"
    assert TOKEN in results[0]["snippet"]
    assert "market cap 139659" in results[0]["snippet"]
    assert results[1]["official_candidate"] is True
    assert results[2]["url"] == "https://x.com/cluby"


def test_claimed_website_requires_page_level_exact_contract_for_official_match():
    provider = lambda query, timeout: [{
        "title": "Website",
        "url": "https://cluby.example/token",
        "snippet": "Website linked by the exact-contract profile.",
        "official_candidate": True,
    }]
    result = research_token(
        "bsc",
        TOKEN,
        now=NOW,
        provider=provider,
        page_fetcher=lambda url, timeout: f"Official contract {TOKEN}",
    )

    assert result["official_contract"]["status"] == "exact_match"
    assert result["sources"][0]["grounding"] == "fetched_page_exact_contract"
    assert "Official contract" in result["sources"][0]["snippet"]


def test_compact_bundle_flattens_pipeline_deterministic_and_research_layers():
    bundle = compact_evidence_bundle({
        "identity": {"chain": "bsc", "contract": TOKEN},
        "deterministic": {
            "chain": "bsc",
            "contract_address": TOKEN,
            "market_evidence": {"market_cap_usd": 139659, "sources": ["dexscreener"], "observed_at": NOW},
            "missing_evidence": ["smart_wallets"],
        },
        "research": {
            "sources": [{
                "source_id": "source-1",
                "source": "official_site",
                "chain": "bsc",
                "contract": TOKEN,
                "grounded": True,
                "observed_at": NOW,
                "title": "Official",
                "url": "https://cluby.example/token",
                "snippet": f"Contract {TOKEN}",
            }],
            "official_contract": {"status": "exact_match", "contracts": [TOKEN]},
        },
    })

    assert bundle["market_evidence"]["market_cap_usd"] == 139659
    assert bundle["official_contract"]["status"] == "exact_match"
    assert bundle["sources"][0]["source_id"] == "source-1"


def test_public_page_fetcher_rejects_private_network_urls():
    called = []
    fetch = public_page_fetcher(opener=lambda request, timeout: called.append(request) or RawResponse(b"ok"))

    with pytest.raises(ValueError, match="non_public_page_url"):
        fetch("http://127.0.0.1/private", 1)

    assert called == []


def test_public_page_fetcher_rejects_hostnames_that_resolve_private(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    called = []
    fetch = public_page_fetcher(opener=lambda request, timeout: called.append(request) or RawResponse(b"ok"))

    with pytest.raises(ValueError, match="non_public_page_url"):
        fetch("https://project.example/token", 1)

    assert called == []


def test_public_page_fetcher_rejects_redirected_final_private_url(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    class RedirectedResponse(RawResponse):
        @staticmethod
        def geturl():
            return "http://127.0.0.1/private"

    fetch = public_page_fetcher(opener=lambda request, timeout: RedirectedResponse(b"private"))

    with pytest.raises(ValueError, match="non_public_page_url"):
        fetch("https://project.example/token", 1)


def test_public_page_fetcher_revalidates_redirect_location_before_following(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    class RedirectResponse(RawResponse):
        status = 302
        headers = {"Location": "http://127.0.0.1/private"}

        @staticmethod
        def geturl():
            return "https://project.example/token"

    calls = []
    fetch = public_page_fetcher(opener=lambda request, timeout: calls.append(request.full_url) or RedirectResponse(b""))

    with pytest.raises(ValueError, match="non_public_page_url"):
        fetch("https://project.example/token", 1)

    assert calls == ["https://project.example/token"]


def test_official_contract_extraction_distinguishes_match_mismatch_and_unknown():
    exact = assess_official_contract(TOKEN, f"<p>Contract: {TOKEN.upper()}</p>", chain="bsc")
    mismatch = assess_official_contract(TOKEN, f"<p>Contract: {OTHER_TOKEN}</p>", chain="bsc")
    unknown = assess_official_contract(TOKEN, "<p>No contract is published.</p>", chain="bsc")

    assert exact["status"] == "exact_match"
    assert exact["contracts"] == [TOKEN]
    assert mismatch == {"status": "mismatch", "contracts": [OTHER_TOKEN]}
    assert unknown == {"status": "unknown", "contracts": []}


def test_cache_freshness_rejects_future_stale_and_naive_timestamps():
    now = datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc)

    assert is_cache_fresh({"observed_at": "2026-09-10T01:59:00Z"}, now=now, ttl_seconds=120)
    assert not is_cache_fresh({"observed_at": "2026-09-10T01:50:00Z"}, now=now, ttl_seconds=120)
    assert not is_cache_fresh({"observed_at": "2026-09-10T02:01:00Z"}, now=now, ttl_seconds=120)
    assert not is_cache_fresh({"observed_at": "2026-09-10T01:59:00"}, now=now, ttl_seconds=120)


def test_research_reports_unavailable_timeout_and_partial_without_raising():
    unavailable = research_token("bsc", TOKEN, symbol="CLUBY", now=NOW)

    calls = []

    def timeout_provider(query, timeout):
        calls.append(timeout)
        raise TimeoutError("provider included a secret that must not leak")

    timed_out = research_token(
        "bsc",
        TOKEN,
        symbol="CLUBY",
        now=NOW,
        provider=timeout_provider,
        timeout_seconds=999,
    )

    assert unavailable["status"] == "provider_unavailable"
    assert timed_out["status"] == "provider_timeout"
    assert timed_out["error_code"] == "provider_timeout"
    assert timed_out.get("error") is None
    assert calls[0] == 15.0

    partial_calls = []

    def partial_provider(query, timeout):
        partial_calls.append(timeout)
        if len(partial_calls) == 1:
            return [{"title": "Cluby", "url": "https://cluby.example", "snippet": TOKEN}]
        raise RuntimeError("downstream detail")

    partial = research_token(
        "bsc",
        TOKEN,
        symbol="CLUBY",
        now=NOW,
        provider=partial_provider,
        timeout_seconds=5,
    )

    assert partial["status"] == "partial"
    assert len(partial["sources"]) == 1


def test_research_keeps_early_source_when_later_query_times_out():
    calls = []

    def provider(query, timeout):
        calls.append(query)
        if len(calls) == 1:
            return [{"title": "Exact CA", "url": "https://example.com/token", "snippet": TOKEN}]
        raise TimeoutError("later query timed out")

    result = research_token(
        "bsc",
        TOKEN,
        symbol="CLUBY",
        now=NOW,
        provider=provider,
        timeout_seconds=5,
    )

    assert result["status"] == "partial"
    assert result["error_code"] == "provider_partial"
    assert len(result["sources"]) == 1


def test_provider_and_page_fetches_share_one_total_deadline():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    provider_timeouts = []
    page_timeouts = []

    def provider(query, timeout):
        provider_timeouts.append(timeout)
        clock.value += 0.2
        return [{
            "title": "Same symbol only",
            "url": "https://candidate.example",
            "snippet": "No contract in search result",
            "is_official": True,
        }]

    def page_fetcher(url, timeout):
        page_timeouts.append(timeout)
        clock.value += 1.0
        return TOKEN

    result = research_token(
        "bsc",
        TOKEN,
        now=NOW,
        provider=provider,
        page_fetcher=page_fetcher,
        timeout_seconds=1,
        clock=clock,
    )

    assert provider_timeouts == pytest.approx([1.0, 0.8, 0.6])
    assert page_timeouts == pytest.approx([0.4])
    assert result["status"] == "provider_timeout"
    assert result["error_code"] == "provider_timeout"
    assert result["sources"] == []


def test_provider_that_ignores_timeout_cannot_block_past_total_deadline():
    def slow_provider(query, timeout):
        time.sleep(0.5)
        return [{"title": TOKEN, "url": "https://late.example", "snippet": TOKEN}]

    started = time.monotonic()
    result = research_token("bsc", TOKEN, now=NOW, provider=slow_provider, timeout_seconds=0.1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert result["status"] == "provider_timeout"
    assert result["sources"] == []


def test_cache_load_validates_schema_identity_and_uses_unique_temp_file(tmp_path):
    cache = tmp_path / "research.json"
    legacy_temp = tmp_path / "research.json.tmp"
    legacy_temp.write_text("leave-me-alone", encoding="utf-8")
    record = {
        "chain": "bsc",
        "contract": TOKEN,
        "observed_at": NOW,
        "status": "ready",
        "sources": [],
        "official_contract": {"status": "unknown", "contracts": []},
    }

    save_research_cache(cache, record)
    loaded = load_fresh_cache(cache, chain="bsc", contract=TOKEN, now=NOW)

    assert loaded is not None
    assert loaded["schema_version"] == 1
    assert legacy_temp.read_text(encoding="utf-8") == "leave-me-alone"
    assert load_fresh_cache(cache, chain="bsc", contract=OTHER_TOKEN, now=NOW) is None

    loaded["sources"] = "not-a-list"
    cache.write_text(json.dumps(loaded), encoding="utf-8")
    assert load_fresh_cache(cache, chain="bsc", contract=TOKEN, now=NOW) is None

    malformed = {**record, "schema_version": 1, "official_contract": {"status": "trusted", "contracts": TOKEN}}
    cache.write_text(json.dumps(malformed), encoding="utf-8")
    assert load_fresh_cache(cache, chain="bsc", contract=TOKEN, now=NOW) is None

    incomplete_source = {
        **record,
        "schema_version": 1,
        "sources": [{"chain": "bsc", "contract": TOKEN}],
    }
    cache.write_text(json.dumps(incomplete_source), encoding="utf-8")
    assert load_fresh_cache(cache, chain="bsc", contract=TOKEN, now=NOW) is None

    inconsistent_match = {
        **record,
        "schema_version": 1,
        "official_contract": {"status": "exact_match", "contracts": []},
    }
    cache.write_text(json.dumps(inconsistent_match), encoding="utf-8")
    assert load_fresh_cache(cache, chain="bsc", contract=TOKEN, now=NOW) is None


def test_compact_bundle_removes_secrets_and_bounds_source_text():
    evidence = {
        "chain": "bsc",
        "contract": TOKEN,
        "api_key": "must-not-appear",
        "market_evidence": {"volume_24h": 1234},
        "sources": [
            {
                "source_id": "source-1",
                "source": "public_search",
                "observed_at": NOW,
                "chain": "bsc",
                "contract": TOKEN,
                "url": "https://cluby.example",
                "snippet": "x" * 2_000,
                "authorization": "Bearer hidden",
            }
        ],
    }

    compact = compact_evidence_bundle(evidence, max_chars=800)
    serialized = json.dumps(compact, ensure_ascii=False)

    assert "must-not-appear" not in serialized
    assert "Bearer hidden" not in serialized
    assert len(serialized) <= 800
    assert compact["identity"] == {"chain": "bsc", "contract": TOKEN}


def test_compact_bundle_redacts_secret_values_and_query_params_but_keeps_token_address():
    evidence = {
        "chain": "bsc",
        "contract": TOKEN,
        "market_evidence": {
            "token_address": TOKEN,
            "access_token": "hidden-access-token",
        },
        "sources": [{
            "source_id": "source-1",
            "source": "public_search",
            "observed_at": NOW,
            "chain": "bsc",
            "contract": TOKEN,
            "url": (
                f"https://user:hidden-password@example.test/page?api_key=hidden-query&key=hidden-key"
                f"&token_address={TOKEN}&view=full#access_token=hidden-fragment"
            ),
            "snippet": TOKEN,
        }],
    }

    compact = compact_evidence_bundle(evidence)
    serialized = json.dumps(compact, ensure_ascii=False)

    assert compact["market_evidence"]["token_address"] == TOKEN
    assert "hidden-access-token" not in serialized
    assert "hidden-query" not in serialized
    assert "hidden-key" not in serialized
    assert "hidden-password" not in serialized
    assert "hidden-fragment" not in serialized
    assert "token_address=" in compact["sources"][0]["url"]
    assert TOKEN in compact["sources"][0]["url"]


def test_compact_bundle_publishes_only_grounded_evidence_ids():
    compact = compact_evidence_bundle({
        "chain": "bsc",
        "contract": TOKEN,
        "market_evidence": {
            "market_cap_usd": 100_000,
            "sources": ["dexscreener"],
            "observed_at": NOW,
            "scope": {"chain": "bsc", "contract_address": TOKEN},
        },
        "missing_evidence": ["official_identity"],
        "sources": [{
            "source_id": "source-1",
            "source": "public_search",
            "observed_at": NOW,
            "chain": "bsc",
            "contract": TOKEN,
            "url": "https://cluby.example",
        }],
    })

    assert compact["allowed_evidence_ids"] == ["market_evidence", "missing_evidence", "source-1"]


def test_summary_validation_rejects_extra_fields_and_unknown_evidence_ids():
    valid = valid_summary()

    assert validate_grounded_summary(valid, {"source-1"}) == valid
    with pytest.raises(ValueError, match="unsupported_fields"):
        validate_grounded_summary({**valid, "price_target": "10x"}, {"source-1"})
    with pytest.raises(ValueError, match="unknown_evidence_id"):
        validate_grounded_summary({**valid, "risks": [cited("身份待核实。", "invented")]}, {"source-1"})


def test_summary_requires_field_level_citations_chinese_and_non_promotional_text():
    uncited = valid_summary()
    uncited["project_narrative"] = {"text": "官网展示了该合约。", "evidence_ids": []}
    english = valid_summary()
    english["risks"] = [cited("Official identity is unknown.")]
    promotional = valid_summary()
    promotional["one_line_judgement"] = cited("这是财富密码，预计十倍上涨。")
    predictive = valid_summary()
    predictive["one_line_judgement"] = cited("该代币未来可能上涨。")

    with pytest.raises(ValueError, match="missing_field_evidence"):
        validate_grounded_summary(uncited, {"source-1"})
    with pytest.raises(ValueError, match="chinese_required"):
        validate_grounded_summary(english, {"source-1"})
    with pytest.raises(ValueError, match="promotional_or_prediction_claim"):
        validate_grounded_summary(promotional, {"source-1"})
    with pytest.raises(ValueError, match="promotional_or_prediction_claim"):
        validate_grounded_summary(predictive, {"source-1"})


def test_missing_credentials_and_invalid_model_json_use_exact_deterministic_fallback():
    deterministic = {"one_line_judgement": "本地证据不足，暂不下结论。"}
    opener_called = False

    def opener(request, timeout):
        nonlocal opener_called
        opener_called = True
        return FakeResponse({})

    absent = summarize_grounded(
        {"sources": []},
        deterministic,
        environ={},
        opener=opener,
    )

    invalid_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            **valid_summary(),
                            "price_target": "10x",
                        }
                    )
                }
            }
        ]
    }
    invalid = summarize_grounded(
        {
            "sources": [
                {
                    "source_id": "source-1",
                    "source": "public_search",
                    "observed_at": NOW,
                    "chain": "bsc",
                    "contract": TOKEN,
                    "url": "https://cluby.example",
                }
            ]
        },
        deterministic,
        environ={"OPENAI_API_KEY": "hidden", "OPENAI_MODEL": "test-model"},
        opener=lambda request, timeout: FakeResponse(invalid_payload),
    )

    assert absent == {
        "status": "fallback",
        "provider": "deterministic",
        "reason": "credentials_unavailable",
        "summary": deterministic,
    }
    assert opener_called is False
    assert invalid["status"] == "fallback"
    assert invalid["reason"] == "invalid_model_output"
    assert invalid["summary"] is deterministic


def test_unscoped_source_cannot_support_model_prose():
    deterministic = "只使用本地确定性摘要。"

    result = summarize_grounded(
        {
            "chain": "bsc",
            "contract": TOKEN,
            "sources": [
                {
                    "source_id": "source-1",
                    "source": "public_search",
                    "chain": "bsc",
                    "contract": OTHER_TOKEN,
                    "url": "https://unrelated.example",
                }
            ],
        },
        deterministic,
        environ={"OPENAI_API_KEY": "hidden", "OPENAI_MODEL": "test-model"},
        opener=lambda request, timeout: pytest.fail("unscoped evidence must not reach the model"),
    )

    assert result == {
        "status": "fallback",
        "provider": "deterministic",
        "reason": "evidence_unavailable",
        "summary": deterministic,
    }


def test_openai_compatible_summary_is_json_only_grounded_and_timeout_bounded():
    summary = valid_summary()
    captured = {}

    def opener(request, timeout):
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"choices": [{"message": {"content": f"```json\n{json.dumps(summary)}\n```"}}]})

    result = summarize_grounded(
        {
            "chain": "bsc",
            "contract": TOKEN,
            "sources": [
                {
                    "source_id": "source-1",
                    "source": "official_site",
                    "observed_at": NOW,
                    "chain": "bsc",
                    "contract": TOKEN,
                    "url": "https://cluby.example",
                    "snippet": TOKEN,
                }
            ],
        },
        "fallback",
        environ={
            "OPENAI_API_KEY": "hidden-key",
            "OPENAI_MODEL": "test-model",
            "OPENAI_BASE_URL": "https://models.example/v1",
        },
        opener=opener,
        timeout_seconds=999,
    )

    assert result == {"status": "ready", "provider": "openai_compatible", "summary": summary}
    assert captured["timeout"] == 15.0
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["temperature"] == 0
    assert captured["body"]["max_tokens"] == 700
    assert "hidden-key" not in json.dumps(result)


def test_monitor_signal_summary_uses_small_json_response_for_fast_selected_tokens():
    captured = {}
    model_output = {
        "one_line_judgement": "多源热度同步，但筹码集中度偏高。",
        "project_narrative": "当前关注来自多源共振。",
        "attention_evidence": ["市值与成交同步抬升"],
        "smart_wallet_evidence": ["已核验钱包 2 个"],
        "risks": ["Top10 集中度偏高"],
    }

    def opener(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse({"choices": [{"message": {"content": json.dumps(model_output)}}]})

    result = summarize_monitor_signal(
        {"market_evidence": {"market_cap_usd": 50_000}, "missing_evidence": []},
        "fallback",
        environ={
            "TOKEN_RESEARCH_OPENAI_API_KEY": "hidden",
            "TOKEN_RESEARCH_OPENAI_MODEL": "fast-model",
            "TOKEN_RESEARCH_OPENAI_BASE_URL": "https://models.example/v1",
        },
        opener=opener,
        timeout_seconds=99,
    )

    assert result == {"status": "ready", "provider": "openai_compatible", "summary": model_output}
    assert captured["timeout"] == 15.0
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["max_tokens"] == 400
    assert captured["body"]["reasoning"] == {"effort": "none", "exclude": True}


def test_default_model_environment_loads_project_env(monkeypatch, tmp_path):
    summary = valid_summary()
    captured = {}
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TOKEN_RESEARCH_OPENAI_API_KEY=project-key\n"
        "TOKEN_RESEARCH_OPENAI_MODEL=openai/gpt-5-nano\n"
        "TOKEN_RESEARCH_OPENAI_BASE_URL=https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )

    def opener(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"choices": [{"message": {"content": json.dumps(summary)}}]})

    monkeypatch.setattr(research_module, "MODEL_ENV_FILES", (env_file,))
    monkeypatch.setattr(research_module.urllib.request, "urlopen", opener)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "old-process-key")

    result = summarize_grounded(
        {
            "chain": "bsc",
            "contract": TOKEN,
            "sources": [{
                "source_id": "source-1",
                "source": "official_site",
                "observed_at": NOW,
                "chain": "bsc",
                "contract": TOKEN,
                "url": "https://cluby.example",
                "snippet": TOKEN,
            }],
        },
        "fallback",
    )

    assert result["status"] == "ready"
    assert captured["authorization"] == "Bearer project-key"
    assert captured["body"]["model"] == "openai/gpt-5-nano"
    assert captured["body"]["max_completion_tokens"] == 500
    assert captured["body"]["reasoning"] == {"effort": "minimal", "exclude": True}
    response_format = captured["body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert "project-key" not in json.dumps(result)


def test_openrouter_summary_enforces_private_provider_routing():
    captured = {}
    summary = valid_summary()

    def opener(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"choices": [{"message": {"content": json.dumps(summary)}}]})

    result = summarize_grounded(
        {"chain": "bsc", "contract": TOKEN, "sources": [{
            "source_id": "source-1", "source": "public_search", "observed_at": NOW,
            "chain": "bsc", "contract": TOKEN, "url": "https://cluby.example",
        }]},
        "fallback",
        environ={
            "TOKEN_RESEARCH_OPENAI_API_KEY": "hidden",
            "TOKEN_RESEARCH_OPENAI_MODEL": "openai/gpt-5-nano",
            "TOKEN_RESEARCH_OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
        },
        opener=opener,
    )

    assert result["status"] == "ready"
    assert "temperature" not in captured["body"]
    assert "max_tokens" not in captured["body"]
    assert captured["body"]["max_completion_tokens"] == 500
    assert captured["body"]["reasoning"] == {"effort": "minimal", "exclude": True}
    assert captured["body"]["provider"] == {
        "data_collection": "deny",
        "zdr": True,
        "allow_fallbacks": True,
        "require_parameters": True,
    }
    assert captured["body"]["usage"] == {"include": True}


def test_openrouter_summary_can_relax_zdr_for_public_market_evidence():
    captured = {}

    def opener(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"choices": [{"message": {"content": json.dumps(valid_summary())}}]})

    result = summarize_grounded(
        {"chain": "bsc", "contract": TOKEN, "sources": [{
            "source_id": "source-1", "source": "public_search", "observed_at": NOW,
            "chain": "bsc", "contract": TOKEN, "url": "https://cluby.example",
        }]},
        "fallback",
        environ={
            "TOKEN_RESEARCH_OPENAI_API_KEY": "hidden",
            "TOKEN_RESEARCH_OPENAI_MODEL": "openai/gpt-4.1-nano",
            "TOKEN_RESEARCH_OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
            "TOKEN_RESEARCH_OPENROUTER_ZDR": "0",
        },
        opener=opener,
    )

    assert result["status"] == "ready"
    assert captured["body"]["provider"]["data_collection"] == "deny"
    assert captured["body"]["provider"]["zdr"] is False


def test_configured_proxy_is_used_for_default_model_opener(monkeypatch):
    captured = {}
    summary = valid_summary()

    class ProxyOpener:
        def open(self, request, timeout):
            captured["timeout"] = timeout
            return FakeResponse({"choices": [{"message": {"content": json.dumps(summary)}}]})

    def build_opener(handler):
        captured["proxies"] = handler.proxies
        return ProxyOpener()

    monkeypatch.setattr("alpha_token_research.urllib.request.build_opener", build_opener)
    result = summarize_grounded(
        {"chain": "bsc", "contract": TOKEN, "sources": [{
            "source_id": "source-1", "source": "public_search", "observed_at": NOW,
            "chain": "bsc", "contract": TOKEN, "url": "https://cluby.example",
        }]},
        "fallback",
        environ={
            "TOKEN_RESEARCH_OPENAI_API_KEY": "hidden",
            "TOKEN_RESEARCH_OPENAI_MODEL": "openai/gpt-5-nano",
            "TOKEN_RESEARCH_OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
            "TOKEN_RESEARCH_OPENAI_PROXY_URL": "http://127.0.0.1:7897",
        },
    )

    assert result["status"] == "ready"
    assert captured["proxies"] == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }


def test_socks_proxy_uses_request_scoped_transport(monkeypatch):
    import requests

    captured = {}
    summary = valid_summary()

    class Response:
        content = json.dumps({"choices": [{"message": {"content": json.dumps(summary)}}]}).encode()

        @staticmethod
        def raise_for_status():
            return None

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(requests, "post", post)
    result = summarize_grounded(
        {"chain": "bsc", "contract": TOKEN, "sources": [{
            "source_id": "source-1", "source": "public_search", "observed_at": NOW,
            "chain": "bsc", "contract": TOKEN, "url": "https://cluby.example",
        }]},
        "fallback",
        environ={
            "TOKEN_RESEARCH_OPENAI_API_KEY": "hidden",
            "TOKEN_RESEARCH_OPENAI_MODEL": "openai/gpt-5-nano",
            "TOKEN_RESEARCH_OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
            "TOKEN_RESEARCH_OPENAI_PROXY_URL": "socks5h://127.0.0.1:7898",
        },
    )

    assert result["status"] == "ready"
    assert captured["proxies"] == {
        "http": "socks5h://127.0.0.1:7898",
        "https": "socks5h://127.0.0.1:7898",
    }


def test_model_opener_that_ignores_timeout_falls_back_on_deadline():
    def slow_opener(request, timeout):
        time.sleep(0.5)
        return FakeResponse({})

    started = time.monotonic()
    result = summarize_grounded(
        {"chain": "bsc", "contract": TOKEN, "sources": [{
            "source_id": "source-1", "source": "public_search", "observed_at": NOW,
            "chain": "bsc", "contract": TOKEN, "url": f"https://example.test/{TOKEN}",
        }]},
        "fallback",
        environ={"OPENAI_API_KEY": "hidden", "OPENAI_MODEL": "test-model"},
        opener=slow_opener,
        timeout_seconds=0.1,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert result["reason"] == "model_timeout"


def test_deepseek_environment_is_an_openai_compatible_default():
    captured = {}
    summary = valid_summary()

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"choices": [{"message": {"content": json.dumps(summary)}}]})

    result = summarize_grounded(
        {"chain": "bsc", "contract": TOKEN, "sources": [{
            "source_id": "source-1", "source": "public_search", "observed_at": NOW,
            "chain": "bsc", "contract": TOKEN, "url": "https://cluby.example",
        }]},
        "fallback",
        environ={"DEEPSEEK_API_KEY": "hidden"},
        opener=opener,
    )

    assert result["status"] == "ready"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["body"]["model"] == "deepseek-chat"
