import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


portfolio = load_module(
    "alpha_first_discovery_portfolio_optimizer",
    BASE / "alpha_first_discovery_portfolio_optimizer.py",
)


def params(**overrides):
    values = {
        "name": "test",
        "mcap_min": 10_000,
        "mcap_max": 100_000,
        "min_signal_score": 0,
        "min_liquidity": 0,
        "min_volume24h": 0,
        "min_smart_money": 0,
        "min_kol": 0,
        "max_top10_pct": 999,
        "min_h1_change_pct": -20,
        "max_h1_change_pct": 80,
        "min_m5_change_pct": -25,
        "max_m5_change_pct": 45,
        "max_pair_age_hours": 6,
        "stop_pct": 22,
        "tp_pct": 100,
        "tp_sell_fraction": 0.8,
        "trail_drawdown_pct": 35,
        "time_stop_minutes": 90,
        "cost_pct": 0.05,
        "gas_usd_per_swap": 0.12,
        "position_usd": 35.0,
        "max_open_positions": 4,
    }
    values.update(overrides)
    return portfolio.PortfolioParams(**values)


def row(symbol, first_seen_at="2026-09-02T14:00:00+08:00", **overrides):
    values = {
        "symbol": symbol,
        "key": f"bsc:{symbol.lower()}",
        "first_seen_at": first_seen_at,
        "first_mcap": 50_000,
        "signal_score": 40,
        "liquidity": 20_000,
        "volume24h": 100_000,
        "smart_money": 0,
        "kol": 0,
        "top10_holder_pct": 999,
        "change_h1": 20,
        "change_m5": 5,
        "pair_age_hours": 1,
        "first_price_usd": 0.0001,
        "observations": [],
    }
    values.update(overrides)
    return values


def test_portfolio_capacity_skips_overlapping_candidates():
    rows = [
        row("A", "2026-09-02T14:00:00+08:00"),
        row("B", "2026-09-02T14:10:00+08:00"),
        row("C", "2026-09-02T14:20:00+08:00"),
    ]

    result = portfolio.portfolio_trades(rows, params(max_open_positions=1))

    assert result["candidate_count"] == 3
    assert result["skipped_capacity"] == 2
    assert [trade["symbol"] for trade in result["trades"]] == ["A"]


def test_time_stop_ignores_observations_after_deadline():
    trade = portfolio.simulate_trade(
        row(
            "LATE",
            observations=[
                {"seen_at": "2026-09-02T16:00:00+08:00", "price_usd": 0.001},
            ],
        ),
        params(),
    )

    assert trade["path"] == "time"
    assert trade["gross_return_pct"] == 0.0
    assert trade["hold_minutes"] == 90.0
    assert trade["exit_time"] == "2026-09-02T15:30:00+08:00"
    assert trade["net_return_pct"] < 0


def test_tp_then_trail_records_two_sells():
    trade = portfolio.simulate_trade(
        row(
            "TRAIL",
            observations=[
                {"seen_at": "2026-09-02T14:05:00+08:00", "price_usd": 0.0002},
                {"seen_at": "2026-09-02T14:20:00+08:00", "price_usd": 0.00013},
            ],
        ),
        params(),
    )

    assert trade["path"] == "tp_trail"
    assert trade["sell_count"] == 2
    assert trade["gross_return_pct"] > 0
