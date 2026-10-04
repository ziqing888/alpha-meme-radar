import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


net = load_module("alpha_first_discovery_net_optimizer", BASE / "alpha_first_discovery_net_optimizer.py")


def params(**overrides):
    item = {
        "name": "test",
        "mcap_min": 10_000,
        "mcap_max": 100_000,
        "min_signal_score": 0,
        "min_liquidity": 0,
        "min_volume24h": 0,
        "min_smart_money": 0,
        "min_kol": 0,
        "max_top10_pct": 999,
        "min_h1_change_pct": -80,
        "max_h1_change_pct": 220,
        "min_m5_change_pct": -50,
        "max_m5_change_pct": 80,
        "max_pair_age_hours": 999,
        "max_trades_per_day": 999,
        "stop_pct": 12,
        "tp_pct": 50,
        "tp_sell_fraction": 1.0,
        "trail_drawdown_pct": 0,
        "time_stop_minutes": 20,
        "min_hold_minutes": 0,
        "cost_pct": 0.05,
        "gas_usd_per_swap": 0.12,
        "position_usd": 35.0,
    }
    item.update(overrides)
    return net.NetParams(**item)


def row(**overrides):
    item = {
        "key": "bsc:0x1",
        "symbol": "TEST",
        "first_seen_at": "2026-09-01T00:00:00+08:00",
        "day": "2026-09-01",
        "first_price_usd": 1.0,
        "first_mcap": 50_000,
        "signal_score": 10,
        "liquidity": 10_000,
        "volume24h": 0,
        "smart_money": 0,
        "kol": 0,
        "top10_holder_pct": 20,
        "change_h1": 10,
        "change_m5": 5,
        "pair_age_hours": 1,
        "observations": [
            {"seen_at": "2026-09-01T00:05:00+08:00", "price_usd": 1.55},
            {"seen_at": "2026-09-01T00:30:00+08:00", "price_usd": 2.0},
        ],
    }
    item.update(overrides)
    return item


def test_full_tp_exits_and_subtracts_costs():
    result = net.simulate_trade(row(), params(tp_pct=50, tp_sell_fraction=1.0))

    assert result["path"] == "tp"
    assert result["gross_return_pct"] == 0.5
    assert result["net_return_pct"] < result["gross_return_pct"]


def test_time_stop_ignores_first_observation_after_window():
    result = net.simulate_trade(
        row(observations=[{"seen_at": "2026-09-01T01:00:00+08:00", "price_usd": 3.0}]),
        params(time_stop_minutes=20),
    )

    assert result["path"] == "time"
    assert result["gross_return_pct"] == 0.0


def test_pick_rows_can_keep_all_daily_signals():
    rows = [
        row(key="bsc:0x1", symbol="A"),
        row(key="bsc:0x2", symbol="B"),
    ]

    assert len(net.pick_rows(rows, params(max_trades_per_day=999))) == 2


def test_summary_uses_net_returns():
    trades = [
        {"net_return_pct": 0.2, "hold_minutes": 5, "path": "tp"},
        {"net_return_pct": -0.1, "hold_minutes": 10, "path": "stop"},
    ]
    summary = net.summarize(trades)

    assert summary["count"] == 2
    assert summary["net_win_rate"] == 0.5
    assert summary["profit_factor"] == 2.0
