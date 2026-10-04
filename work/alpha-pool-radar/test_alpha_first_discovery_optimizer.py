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


opt = load_module("alpha_first_discovery_optimizer", BASE / "alpha_first_discovery_optimizer.py")


def row(**overrides):
    item = {
        "key": "bsc:0x1",
        "symbol": "GOOD",
        "first_seen_at": "2026-09-01T00:00:00+08:00",
        "day": "2026-09-01",
        "first_price_usd": 1.0,
        "first_mcap": 60_000,
        "liquidity": 25_000,
        "volume24h": 150_000,
        "smart_money": 40,
        "kol": 10,
        "top10_holder_pct": 20,
        "change_h1": 40,
        "change_m5": 5,
        "pair_age_hours": 2,
        "signal_score": 92,
        "recommendation_bucket": "shadow",
        "observations": [
            {"seen_at": "2026-09-01T00:10:00+08:00", "price_usd": 1.5},
            {"seen_at": "2026-09-01T00:20:00+08:00", "price_usd": 2.3},
        ],
    }
    item.update(overrides)
    return item


def params(**overrides):
    item = {
        "name": "test",
        "mcap_min": 30_000,
        "mcap_max": 100_000,
        "min_signal_score": 80,
        "min_liquidity": 10_000,
        "min_volume24h": 40_000,
        "min_smart_money": 30,
        "min_kol": 8,
        "max_top10_pct": 25,
        "min_h1_change_pct": -20,
        "max_h1_change_pct": 140,
        "min_m5_change_pct": -15,
        "max_m5_change_pct": 25,
        "max_pair_age_hours": 6,
        "max_trades_per_day": 1,
        "stop_pct": 22,
        "tp1_pct": 45,
        "tp2_pct": 120,
    }
    item.update(overrides)
    return opt.StrategyParams(**item)


def test_passes_accepts_good_row() -> None:
    assert opt.passes(row(), params())


def test_passes_rejects_overheated_h1() -> None:
    assert not opt.passes(row(change_h1=300), params())


def test_pick_daily_keeps_only_best_per_day() -> None:
    rows = [
        row(key="bsc:0x1", symbol="LOW", signal_score=82),
        row(key="bsc:0x2", symbol="HIGH", signal_score=95),
    ]
    selected = opt.pick_daily(rows, params())

    assert len(selected) == 1
    assert selected[0]["symbol"] == "HIGH"


def test_evaluate_summarizes_ordered_trade_return() -> None:
    result = opt.evaluate([row()], params())

    assert result["summary"]["count"] == 1
    assert result["summary"]["average_return_pct"] > 0
    assert result["trades"][0]["path"].startswith("tp1")


def test_split_rows_uses_later_days_as_test() -> None:
    rows = [
        row(day="2026-09-01", first_seen_at="2026-09-01T00:00:00+08:00"),
        row(day="2026-09-02", first_seen_at="2026-09-02T00:00:00+08:00"),
        row(day="2026-09-03", first_seen_at="2026-09-03T00:00:00+08:00"),
    ]
    train, test = opt.split_rows(rows)

    assert {x["day"] for x in train} == {"2026-09-01", "2026-09-02"}
    assert {x["day"] for x in test} == {"2026-09-03"}
