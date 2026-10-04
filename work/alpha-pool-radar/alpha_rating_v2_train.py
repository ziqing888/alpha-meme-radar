"""Train chronological, calibrated MEME rating models for shadow scoring only."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_DATASET = OUT_DIR / "alpha-rating-v2-dataset.json"
DEFAULT_MODEL_DIR = OUT_DIR / "alpha-rating-v2-models"
DEFAULT_STATUS = OUT_DIR / "alpha-rating-v2-model-status.json"
TARGETS = (2, 3, 5, 10)
DEFAULT_MINIMUM_LABELED = 500
DEFAULT_MINIMUM_EACH_CLASS = 50
MINIMUM_VALIDATION_AUC = 0.58
MINIMUM_BRIER_SKILL = 0.0
MINIMUM_TOP_20_LIFT = 1.1
VALIDATION_POLICY_VERSION = 1
NUMERIC_FEATURES = (
    "mcap_usd",
    "liquidity_usd",
    "volume24h_usd",
    "pair_age_hours",
    "change_m5_pct",
    "change_h1_pct",
    "change_h24_pct",
    "legacy_score",
    "entry_score",
    "gold_dog_score",
    "gold_dog_conviction_score",
    "source_count",
    "source_hit_total",
    "smart_money",
    "kol",
    "holders",
    "top10_holder_pct",
    "max_holder_pct",
    "security_filter_score",
    "holder_quality_score",
)
FEATURE_NAMES = (*NUMERIC_FEATURES, "chain_bsc", "chain_robinhood")


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def feature_vector(row: dict[str, Any]) -> list[float]:
    chain = str(row.get("chain") or "").lower()
    return [
        *(to_float(row.get(name)) for name in NUMERIC_FEATURES),
        float(chain == "bsc"),
        float(chain == "robinhood"),
    ]


def _class_counts(labels: list[int]) -> tuple[int, int]:
    return labels.count(0), labels.count(1)


def _segment_has_both_classes(labels: np.ndarray) -> bool:
    return len(np.unique(labels)) == 2


def _evaluation_metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    positive_rate = float(np.mean(labels))
    brier = float(brier_score_loss(labels, probabilities))
    baseline_brier = float(brier_score_loss(labels, np.full(len(labels), positive_rate)))
    top_count = max(1, int(np.ceil(len(labels) * 0.2)))
    top_indices = np.argsort(probabilities)[-top_count:]
    top_precision = float(np.mean(labels[top_indices]))
    return {
        "samples": int(len(labels)),
        "positive": int(np.sum(labels)),
        "negative": int(len(labels) - np.sum(labels)),
        "brier_score": round(brier, 6),
        "baseline_brier_score": round(baseline_brier, 6),
        "brier_skill": round(1 - brier / baseline_brier, 6) if baseline_brier else 0.0,
        "log_loss": round(float(log_loss(labels, probabilities, labels=[0, 1])), 6),
        "roc_auc": round(float(roc_auc_score(labels, probabilities)), 6) if _segment_has_both_classes(labels) else None,
        "positive_rate": round(positive_rate, 6),
        "mean_predicted_probability": round(float(np.mean(probabilities)), 6),
        "top_20_precision": round(top_precision, 6),
        "top_20_lift": round(top_precision / positive_rate, 6) if positive_rate else None,
    }


def _quality_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    failures = []
    if to_float(metrics.get("roc_auc")) < MINIMUM_VALIDATION_AUC:
        failures.append("roc_auc_below_threshold")
    if to_float(metrics.get("brier_skill")) <= MINIMUM_BRIER_SKILL:
        failures.append("no_brier_improvement_over_base_rate")
    if to_float(metrics.get("top_20_lift")) < MINIMUM_TOP_20_LIFT:
        failures.append("top_20_lift_below_threshold")
    return {
        "passed": not failures,
        "status": "validated" if not failures else "weak",
        "failures": failures,
        "thresholds": {
            "minimum_roc_auc": MINIMUM_VALIDATION_AUC,
            "minimum_brier_skill": MINIMUM_BRIER_SKILL,
            "minimum_top_20_lift": MINIMUM_TOP_20_LIFT,
        },
    }


def train_models(
    dataset: dict[str, Any],
    *,
    model_dir: Path,
    minimum_labeled: int = DEFAULT_MINIMUM_LABELED,
    minimum_each_class: int = DEFAULT_MINIMUM_EACH_CLASS,
) -> dict[str, Any]:
    rows = [row for row in (dataset.get("rows") or []) if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get("entry_at") or ""))
    model_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "shadow_only_no_execution_effect",
        "minimum_labeled": minimum_labeled,
        "minimum_each_class": minimum_each_class,
        "validation_policy_version": VALIDATION_POLICY_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "targets": {},
    }

    for target in TARGETS:
        label_key = f"label_{target}x_before_stop"
        labeled = [row for row in rows if row.get(label_key) in (0, 1)]
        labels = [int(row[label_key]) for row in labeled]
        negative, positive = _class_counts(labels)
        target_result: dict[str, Any] = {
            "labeled": len(labeled),
            "positive": positive,
            "negative": negative,
        }
        if len(labeled) < minimum_labeled or min(negative, positive) < minimum_each_class:
            target_result.update({"status": "collecting", "reason": "minimum_sample_not_met"})
            result["targets"][f"{target}x"] = target_result
            continue

        train_end = int(len(labeled) * 0.60)
        calibration_end = int(len(labeled) * 0.80)
        x = np.asarray([feature_vector(row) for row in labeled], dtype=float)
        y = np.asarray(labels, dtype=int)
        x_train, y_train = x[:train_end], y[:train_end]
        x_calibration, y_calibration = x[train_end:calibration_end], y[train_end:calibration_end]
        x_test, y_test = x[calibration_end:], y[calibration_end:]
        if not all(_segment_has_both_classes(segment) for segment in (y_train, y_calibration, y_test)):
            target_result.update({"status": "collecting", "reason": "chronological_split_class_imbalance"})
            result["targets"][f"{target}x"] = target_result
            continue

        base = LGBMClassifier(
            objective="binary",
            n_estimators=160,
            learning_rate=0.04,
            num_leaves=15,
            max_depth=5,
            min_child_samples=12,
            subsample=0.85,
            colsample_bytree=0.85,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
            verbosity=-1,
        )
        base.fit(x_train, y_train)
        calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="sigmoid")
        calibrated.fit(x_calibration, y_calibration)
        probabilities = calibrated.predict_proba(x_test)[:, 1]
        metrics = _evaluation_metrics(y_test, probabilities)
        metrics["test_positive_rate"] = metrics["positive_rate"]
        quality = _quality_summary(metrics)
        chain_metrics = {}
        test_rows = labeled[calibration_end:]
        for chain in ("bsc", "robinhood"):
            indices = [index for index, row in enumerate(test_rows) if str(row.get("chain") or "").lower() == chain]
            if not indices:
                chain_metrics[chain] = {"samples": 0, "reason": "no_test_samples"}
                continue
            chain_metrics[chain] = _evaluation_metrics(y_test[indices], probabilities[indices])
        importance = sorted(
            zip(FEATURE_NAMES, (int(value) for value in base.feature_importances_)),
            key=lambda item: item[1],
            reverse=True,
        )
        model_path = model_dir / f"rating-v2-{target}x.joblib"
        joblib.dump({
            "mode": "shadow_only",
            "target": f"{target}x_before_stop",
            "feature_names": list(FEATURE_NAMES),
            "estimator": calibrated,
            "trained_at": result["generated_at"],
        }, model_path)
        target_result.update({
            "status": "shadow_model_ready" if quality["passed"] else "shadow_model_weak",
            "model_path": str(model_path),
            "split": {
                "train": len(y_train),
                "calibration": len(y_calibration),
                "test": len(y_test),
                "train_through": labeled[train_end - 1].get("entry_at"),
                "calibration_through": labeled[calibration_end - 1].get("entry_at"),
                "test_through": labeled[-1].get("entry_at"),
            },
            "metrics": metrics,
            "quality": quality,
            "chain_metrics": chain_metrics,
            "top_features": [{"name": name, "importance": value} for name, value in importance[:10]],
        })
        result["targets"][f"{target}x"] = target_result

    result["ready_target_count"] = sum(
        item.get("status") == "shadow_model_ready" for item in result["targets"].values()
    )
    result["trained_target_count"] = sum(
        item.get("status") in {"shadow_model_ready", "shadow_model_weak"}
        for item in result["targets"].values()
    )
    return result


def _eligible_targets(
    dataset: dict[str, Any],
    minimum_labeled: int,
    minimum_each_class: int,
) -> set[str]:
    rows = [row for row in (dataset.get("rows") or []) if isinstance(row, dict)]
    eligible: set[str] = set()
    for target in TARGETS:
        key = f"label_{target}x_before_stop"
        labels = [int(row[key]) for row in rows if row.get(key) in (0, 1)]
        negative, positive = _class_counts(labels)
        if len(labels) >= minimum_labeled and min(negative, positive) >= minimum_each_class:
            eligible.add(f"{target}x")
    return eligible


def refresh_models(
    dataset: dict[str, Any],
    *,
    model_dir: Path,
    previous_status: dict[str, Any] | None,
    now: str,
    minimum_labeled: int = DEFAULT_MINIMUM_LABELED,
    minimum_each_class: int = DEFAULT_MINIMUM_EACH_CLASS,
    retrain_seconds: float = 6 * 3600,
) -> dict[str, Any]:
    eligible = _eligible_targets(dataset, minimum_labeled, minimum_each_class)
    previous = dict(previous_status or {})
    previous_targets = previous.get("targets") or {}
    previous_trained = {
        name
        for name, item in previous_targets.items()
        if isinstance(item, dict)
        and item.get("status") in {"shadow_model_ready", "shadow_model_weak"}
        and Path(str(item.get("model_path") or "")).exists()
    }
    try:
        age = (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            - datetime.fromisoformat(str(previous.get("generated_at") or "").replace("Z", "+00:00"))
        ).total_seconds()
    except (TypeError, ValueError):
        age = retrain_seconds + 1

    policy_matches = (
        previous.get("validation_policy_version") == VALIDATION_POLICY_VERSION
        and previous.get("minimum_labeled") == minimum_labeled
        and previous.get("minimum_each_class") == minimum_each_class
    )
    if policy_matches and eligible and eligible.issubset(previous_trained) and 0 <= age < retrain_seconds:
        previous["checked_at"] = now
        previous["reused_recent_model"] = True
        previous["dataset_training_rows"] = len(dataset.get("rows") or [])
        return previous

    result = train_models(
        dataset,
        model_dir=model_dir,
        minimum_labeled=minimum_labeled,
        minimum_each_class=minimum_each_class,
    )
    result["generated_at"] = now
    result["checked_at"] = now
    result["reused_recent_model"] = False
    result["dataset_training_rows"] = len(dataset.get("rows") or [])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--minimum-labeled", type=int, default=DEFAULT_MINIMUM_LABELED)
    parser.add_argument("--minimum-each-class", type=int, default=DEFAULT_MINIMUM_EACH_CLASS)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
    result = train_models(
        dataset,
        model_dir=args.model_dir,
        minimum_labeled=args.minimum_labeled,
        minimum_each_class=args.minimum_each_class,
    )
    args.status.parent.mkdir(parents=True, exist_ok=True)
    args.status.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "mode": result["mode"],
        "ready_target_count": result["ready_target_count"],
        "status": str(args.status),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
