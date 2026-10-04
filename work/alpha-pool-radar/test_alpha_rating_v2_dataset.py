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


rating = load_module("alpha_rating_v2_dataset", BASE / "alpha_rating_v2_dataset.py")


def test_build_training_rows_uses_only_immutable_first_snapshot_features():
    replay = {
        "rows": {
            "robinhood:0xabc": {
                "symbol": "DOG",
                "chain": "robinhood",
                "first_seen_at": "2026-09-09T00:00:00+00:00",
                "latest_price_usd": 999,
                "first_snapshot": {
                    "mcap": 50_000,
                    "liquidity": 25_000,
                    "score": 66,
                    "source_count": 2,
                    "change_h1": 30,
                    "change_m5": 5,
                },
                "trajectory": {
                    "coverage_from_first": True,
                    "observed_hours": 24.1,
                    "sample_count": 100,
                    "hit_2x_before_stop": True,
                    "hit_3x_before_stop": False,
                    "hit_5x_before_stop": False,
                    "hit_10x_before_stop": False,
                    "max_multiple": 2.4,
                    "min_multiple": 0.75,
                },
            }
        }
    }

    rows = rating.build_training_rows(replay)

    assert len(rows) == 1
    row = rows[0]
    assert row["mcap_usd"] == 50_000
    assert row["liquidity_usd"] == 25_000
    assert row["legacy_score"] == 66
    assert row["source_count"] == 2
    assert row["label_2x_before_stop"] == 1
    assert row["label_5x_before_stop"] == 0
    assert "latest_price_usd" not in row


def test_unresolved_target_stays_unlabeled_until_24h_coverage():
    base = {
        "symbol": "DOG",
        "chain": "bsc",
        "first_seen_at": "2026-09-09T00:00:00+00:00",
        "first_snapshot": {"mcap": 50_000, "liquidity": 20_000},
    }
    replay = {
        "rows": {
            "bsc:a": {**base, "trajectory": {"coverage_from_first": True, "observed_hours": 2, "sample_count": 3}},
            "bsc:b": {**base, "trajectory": {"coverage_from_first": True, "observed_hours": 24, "sample_count": 25}},
        }
    }

    rows = rating.build_training_rows(replay)

    assert rows[0]["label_2x_before_stop"] is None
    assert rows[1]["label_2x_before_stop"] == 0


def test_partial_legacy_trajectory_is_excluded_from_training():
    replay = {
        "rows": {
            "bsc:a": {
                "chain": "bsc",
                "first_seen_at": "2026-09-01T00:00:00+00:00",
                "first_snapshot": {"mcap": 50_000},
                "trajectory": {
                    "coverage_from_first": False,
                    "observed_hours": 24,
                    "hit_2x_before_stop": True,
                },
            }
        }
    }

    assert rating.build_training_rows(replay) == []


def test_readiness_requires_enough_positive_and_negative_examples():
    rows = [
        {"label_2x_before_stop": 1 if index % 2 else 0}
        for index in range(40)
    ]
    not_ready = rating.readiness_summary(rows, minimum_labeled=50, minimum_each_class=10)
    ready = rating.readiness_summary(rows, minimum_labeled=40, minimum_each_class=10)

    assert not_ready["targets"]["2x"]["ready"] is False
    assert ready["targets"]["2x"]["ready"] is True
