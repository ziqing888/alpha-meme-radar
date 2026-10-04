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


gold_backtest = load_module("alpha_gold_backtest", BASE / "alpha_gold_backtest.py")


def replay_row(
    key: str,
    symbol: str,
    *,
    first_mcap: float,
    conviction: float,
    observations: list[tuple[str, float]],
    top10: float = 18,
    sources: list[str] | None = None,
) -> dict:
    return {
        "key": key,
        "symbol": symbol,
        "chain": key.split(":", 1)[0],
        "contract_address": key.split(":", 1)[1],
        "first_seen_at": observations[0][0],
        "latest_seen_at": observations[-1][0],
        "first_price_usd": observations[0][1],
        "latest_price_usd": observations[-1][1],
        "recommendation_bucket": "ambush",
        "observations": [{"seen_at": seen_at, "price_usd": price} for seen_at, price in observations],
        "first_snapshot": {
            "symbol": symbol,
            "chain": key.split(":", 1)[0],
            "contract_address": key.split(":", 1)[1],
            "mcap": first_mcap,
            "price_usd": observations[0][1],
            "pair_age_hours": 1.5,
            "liquidity": 24_000,
            "volume24h": 95_000,
            "smart_money": 32,
            "kol": 9,
            "top10_holder_pct": top10,
            "source_labels": sources or ["GMGN", "DS"],
            "gold_dog_conviction_score": conviction,
            "gold_dog_score": conviction - 4,
            "recommendation_bucket": "ambush",
            "recommendation_action": "可小仓试探",
        },
    }


def test_gold_backtest_compares_first_discovery_with_later_confirmation():
    replay_history = {
        "rows": {
            "bsc:0xdog": replay_row(
                "bsc:0xdog",
                "DOG",
                first_mcap=42_000,
                conviction=91,
                observations=[
                    ("2026-09-01T10:00:00+08:00", 1.0),
                    ("2026-09-01T10:05:00+08:00", 1.6),
                    ("2026-09-01T10:20:00+08:00", 2.4),
                ],
            ),
            "bsc:0xbad": replay_row(
                "bsc:0xbad",
                "BAD",
                first_mcap=66_000,
                conviction=87,
                observations=[
                    ("2026-09-01T11:00:00+08:00", 1.0),
                    ("2026-09-01T11:08:00+08:00", 0.74),
                ],
            ),
            "bsc:0xlate": replay_row(
                "bsc:0xlate",
                "LATE",
                first_mcap=820_000,
                conviction=94,
                observations=[
                    ("2026-09-01T12:00:00+08:00", 1.0),
                    ("2026-09-01T12:08:00+08:00", 1.5),
                ],
            ),
        }
    }
    watch_state = {
        "candidates": {
            "bsc:0xdog": {
                "status": "strong_candidate",
                "first_confirmed_at": "2026-09-01T10:05:00+08:00",
                "first_confirmed_mcap": 67_200,
                "source_confirmation_ok": True,
            }
        }
    }

    result = gold_backtest.build_gold_backtest(replay_history, watch_state, now_iso="2026-09-01T13:00:00+08:00")

    by_name = {item["name"]: item for item in result["strategies"]}
    assert by_name["first_discovery_probe"]["summary"]["count"] == 2
    assert by_name["first_confirmation"]["summary"]["count"] == 1
    assert by_name["first_confirmation"]["trades"][0]["entry_paid_up_multiple"] == 1.6
    assert by_name["first_discovery_probe"]["summary"]["average_return_pct"] > 0
    assert by_name["first_confirmation"]["summary"]["average_return_pct"] < by_name["first_discovery_probe"]["trades"][0]["return_pct"]
    assert result["best_strategy"]["name"] == "first_discovery_probe"
    rating_rows = {item["rating"]: item for item in result["rating_backtest"]["ratings"]}
    assert set(rating_rows) == {"S", "A", "B", "C"}
    assert sum(item["summary"]["count"] for item in rating_rows.values()) == 2
    assert result["data_quality"] == {
        "replay_rows": 3,
        "coverage_from_first": 0,
        "partial_trajectory": 3,
        "labeled_2x": 0,
        "labeled_3x": 0,
        "labeled_5x": 0,
        "labeled_10x": 0,
    }
