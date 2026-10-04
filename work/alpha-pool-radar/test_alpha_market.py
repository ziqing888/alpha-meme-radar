import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_market.py")
SPEC = importlib.util.spec_from_file_location("alpha_market", MODULE_PATH)
market = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = market
SPEC.loader.exec_module(market)


def test_fetch_alpha_tokens_filters_delisted_and_sorts_by_volume(monkeypatch):
    def fake_http_json(url):
        return {
            "success": True,
            "data": [
                {"symbol": "LOW", "volume24h": "10", "marketCap": "100"},
                {"symbol": "OFF", "volume24h": "999", "offline": True},
                {"symbol": "DEL", "volume24h": "888", "fullyDelisted": True},
                {"symbol": "HIGH", "volume24h": "20", "marketCap": "50"},
            ],
        }

    monkeypatch.setattr(market, "http_json", fake_http_json)

    tokens = market.fetch_alpha_tokens()

    assert [token["symbol"] for token in tokens] == ["HIGH", "LOW"]


def test_pick_best_pair_prefers_exact_token_then_liquidity():
    token = {"contractAddress": "0xabc"}
    pairs = [
        {
            "baseToken": {"address": "0xother"},
            "quoteToken": {"address": "0xnone"},
            "liquidity": {"usd": "999999"},
            "volume": {"h24": "1"},
        },
        {
            "baseToken": {"address": "0xabc"},
            "quoteToken": {"address": "0xquote"},
            "liquidity": {"usd": "500"},
            "volume": {"h24": "1000"},
        },
    ]

    assert market.pick_best_pair(token, pairs) is pairs[1]


def test_fetch_oi_change_returns_percent_and_latest_value(monkeypatch):
    def fake_http_json(url):
        assert "openInterestHist" in url
        return [
            {"sumOpenInterestValue": "100"},
            {"sumOpenInterestValue": "130"},
        ]

    monkeypatch.setattr(market, "http_json", fake_http_json)

    change, latest = market.fetch_oi_change("TESTUSDT")

    assert change == 30.0
    assert latest == 130.0


def test_find_futures_symbol_handles_multiplier_contracts():
    index = {"PEPE": "1000PEPEUSDT"}

    assert market.find_futures_symbol("PEPE", index) == "1000PEPEUSDT"
