import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_module("alpha_radar_sources", BASE / "alpha_radar_sources.py")
gold = load_module("alpha_gold_enrich", BASE / "alpha_gold_enrich.py")


def test_select_best_dex_pair_prefers_highest_liquidity_usd():
    pairs = [
        {
            "chainId": "solana",
            "pairAddress": "small",
            "url": "https://dexscreener.com/solana/small",
            "liquidity": {"usd": 12_000},
            "marketCap": 900_000,
        },
        {
            "chainId": "solana",
            "pairAddress": "deep",
            "url": "https://dexscreener.com/solana/deep",
            "liquidity": {"usd": 88_000},
            "fdv": 1_400_000,
        },
    ]

    pair = gold.select_best_pair(pairs)

    assert pair["pairAddress"] == "deep"


def test_enrich_candidate_marks_current_mcap_confirmation_without_claiming_historical_peak():
    def fake_http_json(url, timeout=15):
        return [
            {
                "chainId": "solana",
                "pairAddress": "PairA",
                "url": "https://dexscreener.com/solana/PairA",
                "priceUsd": "0.002",
                "marketCap": 1_250_000,
                "fdv": 1_300_000,
                "liquidity": {"usd": 90_000},
                "volume": {"h24": 510_000},
                "priceChange": {"h24": 80},
                "pairCreatedAt": 1735689600000,
            }
        ]

    row = gold.enrich_candidate(
        {
            "mint_address": "MintA",
            "symbol": "AAA",
            "return_pct": 1200,
        },
        http_get=fake_http_json,
    )

    assert row["current_mcap"] == 1_250_000
    assert row["current_liquidity"] == 90_000
    assert row["dex_url"] == "https://dexscreener.com/solana/PairA"
    assert row["mcap_confirmation"] == "current_over_1m"
    assert row["historical_peak_mcap_confirmed"] is False


def test_enrich_candidates_summary_counts_confirmed_and_unresolved_rows():
    rows = [
        {"mint_address": "MintA", "symbol": "A", "return_pct": 1200},
        {"mint_address": "MintB", "symbol": "B", "return_pct": 900},
    ]

    def fake_http_json(url, timeout=15):
        if "MintA" in url:
            return [{"chainId": "solana", "pairAddress": "PairA", "marketCap": 1_200_000, "liquidity": {"usd": 50_000}}]
        return []

    payload = gold.enrich_candidates(rows, http_get=fake_http_json)

    assert payload["summary"]["input_count"] == 2
    assert payload["summary"]["dex_found_count"] == 1
    assert payload["summary"]["current_over_1m_count"] == 1
    assert payload["summary"]["unresolved_count"] == 1
