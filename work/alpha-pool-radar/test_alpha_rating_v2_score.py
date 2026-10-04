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


scorer = load_module("alpha_rating_v2_score", BASE / "alpha_rating_v2_score.py")


class FakeEstimator:
    def __init__(self, probability: float):
        self.probability = probability

    def predict_proba(self, rows):
        return [[1 - self.probability, self.probability] for _ in rows]


def test_attach_shadow_scores_marks_collecting_without_ready_models(tmp_path):
    rows = [{"chain": "bsc", "contract_address": "0xabc", "score": 82}]
    scorer.attach_shadow_scores(
        rows,
        replay={"rows": {}},
        model_status={"minimum_labeled": 200, "dataset_training_rows": 7, "targets": {}},
        model_dir=tmp_path,
    )

    result = rows[0]["rating_v2"]
    assert result["status"] == "collecting"
    assert result["score"] is None
    assert result["probabilities"] == {"2x": None, "3x": None, "5x": None, "10x": None}
    assert result["legacy_score"] == 82
    assert result["sample_progress"] == {"current": 7, "required": 200}


def test_attach_shadow_scores_uses_immutable_first_snapshot_and_compares_scores(tmp_path, monkeypatch):
    rows = [{"chain": "bsc", "contract_address": "0xabc", "score": 90, "liquidity": 999_999}]
    replay = {
        "rows": {
            "bsc:0xabc": {
                "chain": "bsc",
                "contract_address": "0xabc",
                "first_snapshot": {"score": 60, "liquidity": 12_000, "mcap": 40_000},
            }
        }
    }
    probabilities = {"2x": 0.8, "3x": 0.6, "5x": 0.4, "10x": 0.2}
    monkeypatch.setattr(
        scorer.joblib,
        "load",
        lambda path: {"estimator": FakeEstimator(probabilities[path.stem.removeprefix("rating-v2-")])},
    )
    status = {
        "dataset_training_rows": 240,
        "minimum_labeled": 200,
        "targets": {
            target: {"status": "shadow_model_ready", "model_path": str(tmp_path / f"rating-v2-{target}.joblib")}
            for target in probabilities
        },
    }
    for target in probabilities:
        (tmp_path / f"rating-v2-{target}.joblib").touch()

    scorer.attach_shadow_scores(rows, replay=replay, model_status=status, model_dir=tmp_path)

    result = rows[0]["rating_v2"]
    assert result["status"] == "scored"
    assert result["probabilities"] == probabilities
    assert result["score"] == 60.6
    assert result["legacy_score"] == 90
    assert result["delta"] == -29.4
    assert result["feature_snapshot"] == "immutable_first_snapshot"
