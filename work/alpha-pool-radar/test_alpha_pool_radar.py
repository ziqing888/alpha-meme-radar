import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse


MODULE_PATH = Path(__file__).with_name("alpha_pool_radar.py")
SPEC = importlib.util.spec_from_file_location("alpha_pool_radar", MODULE_PATH)
alpha = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = alpha
SPEC.loader.exec_module(alpha)


def test_routescan_holder_provider_uses_keyless_holder_list(monkeypatch):
    captured = {}

    def fake_http_json(url, timeout=15):
        captured["url"] = url
        captured["timeout"] = timeout
        return {
            "status": "1",
            "message": "OK",
            "result": [
                {
                    "TokenHolderAddress": "0x1111111111111111111111111111111111111111",
                    "TokenHolderQuantity": "100",
                }
            ],
        }

    monkeypatch.setitem(alpha.fetch_top_holders.__globals__, "http_json", fake_http_json)

    rows = alpha.fetch_top_holders("routescan", "1", "0xabc", 20, "")

    parsed = urlparse(captured["url"])
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "api.routescan.io"
    assert parsed.path == "/v2/network/mainnet/evm/1/etherscan/api"
    assert params["module"] == ["token"]
    assert params["action"] == ["tokenholderlist"]
    assert params["contractaddress"] == ["0xabc"]
    assert params["page"] == ["1"]
    assert params["offset"] == ["20"]
    assert "apikey" not in params
    assert rows[0]["TokenHolderQuantity"] == "100"


def test_holder_metrics_supports_blockscout_value_shape():
    rows = [
        {"address": "0x1", "value": "550"},
        {"address": "0x2", "value": "150"},
        {"address": "0x3", "value": "100"},
    ]

    metrics = alpha.holder_metrics_from_rows(rows, total_supply=1000)

    assert metrics["top10_holder_pct"] == 80.0
    assert metrics["top20_holder_pct"] == 80.0
    assert metrics["max_holder_pct"] == 55.0


def test_holder_metrics_normalizes_raw_token_units():
    rows = [
        {"address": "0x1", "value": "550000000000000000000"},
        {"address": "0x2", "value": "150000000000000000000"},
        {"address": "0x3", "value": "100000000000000000000"},
    ]

    metrics = alpha.holder_metrics_from_rows(rows, total_supply=1000)

    assert metrics["top10_holder_pct"] == 80.0
    assert metrics["max_holder_pct"] == 55.0


def test_solana_holder_metrics_from_rpc_values():
    largest_accounts = [
        {"amount": "550000000", "decimals": 6, "uiAmount": 550.0},
        {"amount": "150000000", "decimals": 6, "uiAmount": 150.0},
        {"amount": "100000000", "decimals": 6, "uiAmount": 100.0},
    ]
    supply = {"amount": "1000000000", "decimals": 6, "uiAmount": 1000.0}

    metrics = alpha.solana_holder_metrics_from_rpc(largest_accounts, supply)

    assert metrics["top10_holder_pct"] == 80.0
    assert metrics["top20_holder_pct"] == 80.0
    assert metrics["max_holder_pct"] == 55.0
    assert metrics["holder_source"] == "holderlist"


def test_rugcheck_holder_metrics_uses_top_holder_percentages():
    report = {
        "topHolders": [
            {"pct": 22.5, "insider": False},
            {"pct": 12.5, "insider": True},
            {"pct": 5.0, "insider": False},
        ]
    }

    metrics = alpha.rugcheck_holder_metrics(report)

    assert metrics["top10_holder_pct"] == 40.0
    assert metrics["top20_holder_pct"] == 40.0
    assert metrics["max_holder_pct"] == 22.5
    assert metrics["holder_contract_count"] == 1
    assert metrics["holder_source"] == "rugcheck_holderlist"


def test_fetch_solana_holder_metrics_prefers_rugcheck_report(monkeypatch):
    def fake_fetch_rugcheck_report(mint):
        assert mint == "So11111111111111111111111111111111111111112"
        return {"topHolders": [{"pct": 31.0, "insider": False}]}

    def fail_rpc_request(method, params):
        raise AssertionError("Solana RPC should only be a fallback")

    monkeypatch.setitem(alpha.fetch_solana_holder_metrics.__globals__, "fetch_rugcheck_report", fake_fetch_rugcheck_report)
    monkeypatch.setitem(alpha.fetch_solana_holder_metrics.__globals__, "solana_rpc_request", fail_rpc_request)

    metrics = alpha.fetch_solana_holder_metrics("So11111111111111111111111111111111111111112")

    assert metrics["top10_holder_pct"] == 31.0
    assert metrics["holder_source"] == "rugcheck_holderlist"


def test_auto_holder_provider_prefers_blockscout_and_uses_bscscan_for_bsc():
    assert alpha.resolve_holder_provider("auto", "8453") == "blockscout"
    assert alpha.resolve_holder_provider("auto", "56") == "bscscan"
    assert alpha.resolve_holder_provider("auto", "CT_501") == "solana_rpc"


def test_parse_bscscan_holder_export_returns_holder_quantities():
    html = """
    <script>
      const quickExportTokenHolerData = '[
        ["1","0xaaa",null,"9,979,900","0.0000%",null],
        ["2","0xbbb","MEXC 13","174,501.604784094","0.0000%",null]
      ]';
    </script>
    """

    rows = alpha.parse_bscscan_holder_export(html)

    assert rows == [
        {"TokenHolderAddress": "0xaaa", "TokenHolderQuantity": "9979900", "TokenHolderAddressType": ""},
        {"TokenHolderAddress": "0xbbb", "TokenHolderQuantity": "174501.604784094", "TokenHolderAddressType": "E"},
    ]


def test_goplus_risk_metrics_detects_honeypot_and_tax():
    token = {
        "is_honeypot": "1",
        "cannot_sell_all": "1",
        "buy_tax": "0.12",
        "sell_tax": "0.21",
        "is_open_source": "0",
    }

    metrics = alpha.goplus_risk_metrics(token)

    assert metrics["risk_score"] == 100
    assert metrics["risk_level"] == "high"
    assert "honeypot" in metrics["risk_flags"]
    assert "high_sell_tax" in metrics["risk_flags"]
    assert metrics["risk_source"] == "goplus"


def test_rugcheck_risk_metrics_uses_summary_risks():
    summary = {
        "score_normalised": 72,
        "risks": [
            {"name": "Freeze authority", "level": "danger"},
            {"name": "Low Liquidity", "level": "warn"},
        ],
        "lpLockedPct": 12.5,
    }

    metrics = alpha.rugcheck_risk_metrics(summary)

    assert metrics["risk_score"] == 72
    assert metrics["risk_level"] == "high"
    assert "Freeze authority" in metrics["risk_flags"]
    assert "lp_unlocked" in metrics["risk_flags"]
    assert metrics["risk_source"] == "rugcheck"
