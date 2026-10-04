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


load_module("alpha_pool_radar", BASE / "alpha_pool_radar.py")
buyflow = load_module("alpha_alpha_buyflow", BASE / "alpha_alpha_buyflow.py")


def test_buyflow_metrics_marks_strong_public_buy_pressure():
    row = {
        "market_cap": 10_000_000,
        "alpha_volume24h": 3_000_000,
        "dex_volume24h": 4_000_000,
        "buy_sell_ratio24h": 2.2,
        "txns24h": 420,
        "alpha_change24h_pct": 15,
    }

    metrics = buyflow.buyflow_metrics(row)

    assert metrics["alpha_buyflow_label"] == "高度活跃"
    assert metrics["alpha_buyflow_confirmed"] is True
    assert metrics["alpha_buyflow_volume_to_mcap_pct"] == 40.0
    assert metrics["alpha_buyflow_buy_sell_ratio"] == 2.2
    assert "买卖比2.20" in metrics["alpha_buyflow_explain"]


def test_buyflow_metrics_keeps_weak_flow_unconfirmed():
    row = {
        "market_cap": 50_000_000,
        "alpha_volume24h": 1_000_000,
        "buy_sell_ratio24h": 0.9,
        "txns24h": 12,
        "alpha_change24h_pct": -3,
    }

    metrics = buyflow.buyflow_metrics(row)

    assert metrics["alpha_buyflow_label"] == "未确认"
    assert metrics["alpha_buyflow_confirmed"] is False


def test_enrich_alpha_buyflow_and_coverage_summary():
    rows = [
        {
            "symbol": "A",
            "market_cap": 10_000_000,
            "dex_volume24h": 4_000_000,
            "buy_sell_ratio24h": 2.1,
            "txns24h": 500,
        },
        {"symbol": "B", "market_cap": 20_000_000, "dex_volume24h": 100_000},
    ]

    buyflow.enrich_alpha_buyflow(rows)
    summary = buyflow.buyflow_coverage_summary(rows)

    assert rows[0]["alpha_buyflow_confirmed"] is True
    assert rows[1]["alpha_buyflow_confirmed"] is False
    assert summary["total"] == 2
    assert summary["with_buy_sell_ratio"] == 1
    assert summary["confirmed_count"] == 1
