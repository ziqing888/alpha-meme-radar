import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


trainer = load_module("alpha_rating_v2_train", BASE / "alpha_rating_v2_train.py")


def synthetic_rows(count: int) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        positive = index % 2
        rows.append({
            "entry_at": (start + timedelta(minutes=index)).isoformat(),
            "chain": "robinhood" if index % 3 else "bsc",
            "mcap_usd": 20_000 + index * 500,
            "liquidity_usd": 10_000 + positive * 20_000,
            "volume24h_usd": 30_000 + index * 100,
            "pair_age_hours": 1 + index % 12,
            "change_m5_pct": positive * 10,
            "change_h1_pct": positive * 30,
            "legacy_score": 60 + index % 20,
            "source_count": 1 + positive,
            "smart_money": positive * 5,
            "kol": positive,
            "top10_holder_pct": 20 + (1 - positive) * 20,
            "label_2x_before_stop": positive,
            "label_3x_before_stop": None,
            "label_5x_before_stop": None,
            "label_10x_before_stop": None,
        })
    return rows


def uninformative_rows(count: int) -> list[dict]:
    rows = synthetic_rows(count)
    for row in rows:
        row["chain"] = "bsc"
        for feature in trainer.NUMERIC_FEATURES:
            row[feature] = 1
    return rows


def test_train_models_stays_collecting_when_samples_are_insufficient(tmp_path):
    result = trainer.train_models(
        {"rows": synthetic_rows(20)},
        model_dir=tmp_path,
        minimum_labeled=40,
        minimum_each_class=10,
    )

    assert result["targets"]["2x"]["status"] == "collecting"
    assert not list(tmp_path.glob("*.joblib"))


def test_train_models_writes_calibrated_chronological_shadow_model(tmp_path):
    result = trainer.train_models(
        {"rows": synthetic_rows(120)},
        model_dir=tmp_path,
        minimum_labeled=100,
        minimum_each_class=20,
    )

    target = result["targets"]["2x"]
    assert target["status"] == "shadow_model_ready"
    assert target["split"]["train"] == 72
    assert target["split"]["calibration"] == 24
    assert target["split"]["test"] == 24
    assert 0 <= target["metrics"]["brier_score"] <= 1
    assert target["quality"]["passed"] is True
    assert target["chain_metrics"]["bsc"]["samples"] > 0
    assert target["chain_metrics"]["robinhood"]["samples"] > 0
    bundle = joblib.load(tmp_path / "rating-v2-2x.joblib")
    assert bundle["mode"] == "shadow_only"
    assert "legacy_score" in bundle["feature_names"]


def test_train_models_marks_uninformative_model_weak(tmp_path):
    result = trainer.train_models(
        {"rows": uninformative_rows(120)},
        model_dir=tmp_path,
        minimum_labeled=100,
        minimum_each_class=20,
    )

    target = result["targets"]["2x"]
    assert target["status"] == "shadow_model_weak"
    assert target["quality"]["passed"] is False
    assert target["metrics"]["roc_auc"] == 0.5
    assert result["trained_target_count"] == 1
    assert result["ready_target_count"] == 0
    assert (tmp_path / "rating-v2-2x.joblib").exists()


def test_train_models_uses_formal_sample_thresholds_by_default(tmp_path):
    result = trainer.train_models({"rows": synthetic_rows(300)}, model_dir=tmp_path)

    assert result["minimum_labeled"] == 500
    assert result["minimum_each_class"] == 50
    assert result["targets"]["2x"]["status"] == "collecting"


def test_refresh_models_reuses_recent_shadow_model(tmp_path, monkeypatch):
    dataset = {"rows": synthetic_rows(120)}
    first = trainer.refresh_models(
        dataset,
        model_dir=tmp_path,
        previous_status=None,
        now="2026-09-09T00:00:00+00:00",
        minimum_labeled=100,
        minimum_each_class=20,
    )
    assert first["ready_target_count"] == 1

    monkeypatch.setattr(trainer, "train_models", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("retrained")))
    reused = trainer.refresh_models(
        dataset,
        model_dir=tmp_path,
        previous_status=first,
        now="2026-09-09T01:00:00+00:00",
        minimum_labeled=100,
        minimum_each_class=20,
    )

    assert reused["reused_recent_model"] is True
    assert reused["checked_at"] == "2026-09-09T01:00:00+00:00"


def test_refresh_models_retrains_when_validation_policy_changed(tmp_path, monkeypatch):
    dataset = {"rows": synthetic_rows(120)}
    previous = {
        "generated_at": "2026-09-09T00:00:00+00:00",
        "minimum_labeled": 100,
        "minimum_each_class": 20,
        "targets": {
            "2x": {"status": "shadow_model_ready", "model_path": str(tmp_path / "rating-v2-2x.joblib")},
        },
    }
    (tmp_path / "rating-v2-2x.joblib").touch()
    calls = []
    original = trainer.train_models
    monkeypatch.setattr(trainer, "train_models", lambda *args, **kwargs: calls.append(True) or original(*args, **kwargs))

    refreshed = trainer.refresh_models(
        dataset,
        model_dir=tmp_path,
        previous_status=previous,
        now="2026-09-09T01:00:00+00:00",
        minimum_labeled=100,
        minimum_each_class=20,
    )

    assert calls == [True]
    assert refreshed["validation_policy_version"] == trainer.VALIDATION_POLICY_VERSION
    assert refreshed["reused_recent_model"] is False
