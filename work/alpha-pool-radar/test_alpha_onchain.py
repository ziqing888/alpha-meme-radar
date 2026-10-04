import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


MODULE_PATH = Path(__file__).with_name("alpha_onchain.py")
SPEC = importlib.util.spec_from_file_location("alpha_onchain", MODULE_PATH)
onchain = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = onchain
SPEC.loader.exec_module(onchain)


def test_routescan_holder_provider_uses_keyless_holder_list(monkeypatch):
    captured = {}

    def fake_http_json(url, timeout=15):
        captured["url"] = url
        captured["timeout"] = timeout
        return {
            "status": "1",
            "message": "OK",
            "result": [{"TokenHolderAddress": "0x1", "TokenHolderQuantity": "100"}],
        }

    monkeypatch.setattr(onchain, "http_json", fake_http_json)

    rows = onchain.fetch_top_holders("routescan", "1", "0xabc", 20, "")

    parsed = urlparse(captured["url"])
    params = parse_qs(parsed.query)
    assert parsed.netloc == "api.routescan.io"
    assert params["action"] == ["tokenholderlist"]
    assert params["contractaddress"] == ["0xabc"]
    assert "apikey" not in params
    assert rows[0]["TokenHolderQuantity"] == "100"


def test_holder_metrics_normalizes_raw_token_units():
    rows = [
        {"address": "0x1", "value": "550000000000000000000"},
        {"address": "0x2", "value": "150000000000000000000"},
        {"address": "0x3", "value": "100000000000000000000"},
    ]

    metrics = onchain.holder_metrics_from_rows(rows, total_supply=1000)

    assert metrics["top10_holder_pct"] == 80.0
    assert metrics["max_holder_pct"] == 55.0


def test_goplus_risk_metrics_detects_honeypot_and_tax():
    token = {
        "is_honeypot": "1",
        "cannot_sell_all": "1",
        "buy_tax": "0.12",
        "sell_tax": "0.21",
        "is_open_source": "0",
    }

    metrics = onchain.goplus_risk_metrics(token)

    assert metrics["risk_score"] == 100
    assert metrics["risk_level"] == "high"
    assert "honeypot" in metrics["risk_flags"]
    assert "high_sell_tax" in metrics["risk_flags"]
    assert metrics["risk_source"] == "goplus"


def test_goplus_holders_are_fractions_and_include_contracts():
    token = {"holder_count": "30", "holders": [
        {"address": f"0x{i}", "percent": "0.02", "is_contract": int(i == 0)} for i in range(10)
    ]}
    metrics = onchain.goplus_holder_metrics(token)
    assert metrics["top10_holder_pct"] == 20
    assert metrics["max_holder_pct"] == 2
    assert metrics["top20_holder_pct"] is None
    assert metrics["holder_contract_count"] == 1
    assert onchain.goplus_holder_metrics({**token, "holders": token["holders"][:9]}) == {}
    for invalid in (None, "NaN", "-0.1", "1.1", ""):
        broken = {**token, "holders": [dict(h) for h in token["holders"]]}
        broken["holders"][0]["percent"] = invalid
        assert onchain.goplus_holder_metrics(broken) == {}


def test_goplus_small_holder_population_and_duplicates():
    holders = [{"address": "0x1", "percent": "0.7"}, {"address": "0x2", "percent": "0.3"}]
    assert onchain.goplus_holder_metrics({"holder_count": 2, "holders": holders})["top10_holder_pct"] == 100
    assert onchain.goplus_holder_metrics({"holder_count": 2, "holders": [holders[0], holders[0]]}) == {}


def test_goplus_adapter_populates_row_without_inferred_supply(monkeypatch):
    from types import SimpleNamespace
    row = SimpleNamespace(chain_id="56", chain="BSC", contract_address="0xabc", symbol="TEST", notes=[])
    monkeypatch.setattr(onchain.time, "sleep", lambda _: None)
    monkeypatch.setattr(onchain, "score_row", lambda *a, **kw: None)
    monkeypatch.setattr(onchain, "fetch_live_goplus_holders", lambda *a: {
        "top10_holder_pct": 30, "top20_holder_pct": None, "max_holder_pct": 5,
        "holder_contract_count": 1, "holder_source": "top10_addresses_including_contracts",
    })
    args = SimpleNamespace(holders_enable=True, holder_provider="goplus", holder_top_tokens=30)
    assert onchain.apply_holder_metrics([row], args) == []
    assert row.top10_holder_pct == 30
    assert row.holder_source.startswith("goplus_")
    assert row.holder_observed_at.endswith("Z")


def test_rugcheck_risk_metrics_uses_summary_risks():
    summary = {
        "score_normalised": 72,
        "risks": [
            {"name": "Freeze authority", "level": "danger"},
            {"name": "Low Liquidity", "level": "warn"},
        ],
        "lpLockedPct": 12.5,
    }

    metrics = onchain.rugcheck_risk_metrics(summary)

    assert metrics["risk_score"] == 72
    assert metrics["risk_level"] == "high"
    assert "Freeze authority" in metrics["risk_flags"]
    assert "lp_unlocked" in metrics["risk_flags"]
    assert metrics["risk_source"] == "rugcheck"
