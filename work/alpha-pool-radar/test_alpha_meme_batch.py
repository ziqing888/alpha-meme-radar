import importlib.util
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote

import pytest


spec = importlib.util.spec_from_file_location("alpha_meme_batch_under_test", Path(__file__).with_name("alpha_meme.py"))
meme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meme)


def source(address, chain="bsc"):
    return {"chainId": chain, "tokenAddress": address, "sources": ["gmgn_trending"],
            "profile": {"chain": chain, "address": address, "symbol": address,
                        "name": "Meme", "price": 0.1, "market_cap": 50000,
                        "liquidity": 10000, "volume": 20000}}


def pair(address, chain="bsc", price="1"):
    return {"chainId": chain, "pairAddress": "pool-" + address,
            "baseToken": {"address": address, "symbol": "Meme", "name": "Meme"},
            "quoteToken": {"address": "0xquote"}, "priceUsd": price,
            "marketCap": 50000, "liquidity": {"usd": 10000},
            "volume": {"h24": 20000}, "priceChange": {}, "txns": {}}


@pytest.fixture(autouse=True)
def offline_batch(monkeypatch):
    monkeypatch.setenv("MEME_BATCH_PAIRS", "1")
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "*")
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda item: pytest.fail("unexpected per-token request"))
    monkeypatch.setattr(meme, "http_json_compat", lambda *a, **kw: pytest.fail("unmocked request"))


def install_sources(monkeypatch, items):
    monkeypatch.setattr(
        meme,
        "fetch_profile_sources",
        lambda limit, observed_at=None: ({str(i): item for i, item in enumerate(items)}, []),
    )


def test_300_tokens_use_ten_batches_and_retain_profile_fallback(monkeypatch):
    install_sources(monkeypatch, [source(f"0x{i:040x}") for i in range(300)])
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        assert "/tokens/v1/bsc/" in url
        assert len(url.rsplit("/", 1)[1].split(",")) == 30
        return []

    monkeypatch.setattr(meme, "http_json_compat", fetch)
    rows, errors = meme.load_meme_candidates(300, 4)
    assert len(calls) == 10
    assert len(rows) == 300
    assert errors == []


def test_batches_group_canonical_chain_deduplicate_and_chunk(monkeypatch):
    items = [source(f"0x{i:040x}") for i in range(31)]
    items += [source("0x0000000000000000000000000000000000000000", "56"), source("MintA", "sol"), source("minta", "solana")]
    install_sources(monkeypatch, items)
    calls = []
    monkeypatch.setattr(meme, "http_json_compat", lambda url, **kwargs: calls.append(unquote(url)) or [])
    meme.load_meme_candidates(40, 2)
    bsc = [url.rsplit("/", 1)[1].split(",") for url in calls if "/bsc/" in url]
    sol = [url for url in calls if "/solana/" in url]
    assert sorted(map(len, bsc)) == [1, 30]
    assert len(sol) == 1 and "MintA,minta" in sol[0]


def test_exact_base_token_mapping_excludes_quote_side_wrong_chain_and_case(monkeypatch):
    payload = [pair("0xABC"), pair("0xother"), pair("0xabc", "base"),
               {**pair("0xother"), "quoteToken": {"address": "0xabc"}}, None]
    monkeypatch.setattr(meme, "http_json_compat", lambda *a, **kw: payload)
    result = meme.fetch_pairs_for_batch("bsc", ["0xabc"])
    assert result[("bsc", "0xabc")] == [payload[0]]
    monkeypatch.setattr(meme, "http_json_compat", lambda *a, **kw: [pair("MintA", "solana"), pair("minta", "solana")])
    assert len(meme.fetch_pairs_for_batch("solana", ["MintA"])[("solana", "MintA")]) == 1


def test_loader_uses_matched_batch_price_not_other_tokens(monkeypatch):
    install_sources(monkeypatch, [source("0xabc"), source("0xdef")])
    monkeypatch.setattr(meme, "http_json_compat", lambda *a, **kw: [pair("0xdef", price="2"), pair("0xabc", price="1"), pair("0xother", price="999")])
    rows, errors = meme.load_meme_candidates(2, 1)
    assert errors == []
    assert {row["contract_address"]: row["price_usd"] for row in rows} == {"0xabc": 1, "0xdef": 2}


@pytest.mark.parametrize("error", [HTTPError("https://api.dexscreener.com", 429, "Too Many Requests", {}, None), RuntimeError("server down")])
def test_batch_error_uses_local_profiles_without_per_token_retry(monkeypatch, error):
    install_sources(monkeypatch, [source("0xabc"), source("0xdef")])
    calls = []

    def fail(url, **kwargs):
        calls.append(url)
        raise error

    monkeypatch.setattr(meme, "http_json_compat", fail)
    rows, errors = meme.load_meme_candidates(2, 1)
    assert len(calls) == 1
    assert len(rows) == 2
    assert all(row["price_usd"] == 0.1 for row in rows)
    assert any("dex_batch" in error for error in errors)


def test_bad_payload_and_partial_result_fall_back_without_token_requests(monkeypatch):
    install_sources(monkeypatch, [source("0xabc"), source("0xdef")])
    monkeypatch.setattr(meme, "http_json_compat", lambda *a, **kw: {"error": "bad response"})
    rows, errors = meme.load_meme_candidates(2, 1)
    assert len(rows) == 2 and errors
    monkeypatch.setattr(meme, "http_json_compat", lambda *a, **kw: [pair("0xabc", price="3")])
    rows, errors = meme.load_meme_candidates(2, 1)
    assert {r["contract_address"]: r["price_usd"] for r in rows} == {"0xabc": 3, "0xdef": 0.1}
    assert not errors


def test_disabled_default_keeps_original_mock_contract(monkeypatch):
    monkeypatch.delenv("MEME_BATCH_PAIRS", raising=False)
    install_sources(monkeypatch, [source("0xabc")])
    calls = []
    monkeypatch.setattr(meme, "fetch_pairs_for_source", lambda item: calls.append(item) or [])
    rows, errors = meme.load_meme_candidates(1, 1)
    assert len(calls) == 1 and len(rows) == 1 and not errors


def test_allowlist_filters_before_batch_http(monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc")
    install_sources(monkeypatch, [source("0xabc"), source("MintA", "solana")])
    calls = []
    monkeypatch.setattr(meme, "http_json_compat", lambda url, **kwargs: calls.append(url) or [])
    rows, _ = meme.load_meme_candidates(2, 1)
    assert len(calls) == 1 and "/bsc/" in calls[0]
    assert len(rows) == 1
