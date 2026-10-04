"""Attach calibrated V2 shadow probabilities to report rows without affecting execution."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib

from alpha_rating_v2_dataset import feature_row_from_snapshot
from alpha_rating_v2_train import TARGETS, feature_vector


TARGET_WEIGHTS = {"2x": 0.45, "3x": 0.25, "5x": 0.18, "10x": 0.12}


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _identity(row: dict[str, Any]) -> str:
    chain = str(row.get("chain") or row.get("chain_id") or "").lower()
    address = str(row.get("contract_address") or row.get("token_address") or "")
    if chain in {"bsc", "bnb", "binance", "robinhood", "base", "ethereum", "eth"}:
        address = address.lower()
    return f"{chain}:{address}"


def _replay_lookup(replay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = replay.get("rows") or {}
    values = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    lookup: dict[str, dict[str, Any]] = {}
    for item in values:
        if isinstance(item, dict):
            lookup[_identity(item)] = item
    return lookup


def _load_estimators(model_status: dict[str, Any], model_dir: Path) -> dict[str, Any]:
    estimators: dict[str, Any] = {}
    statuses = model_status.get("targets") or {}
    for target in TARGETS:
        key = f"{target}x"
        status = statuses.get(key) if isinstance(statuses, dict) else None
        if not isinstance(status, dict) or status.get("status") != "shadow_model_ready":
            continue
        configured = Path(str(status.get("model_path") or ""))
        path = configured if configured.exists() else model_dir / f"rating-v2-{key}.joblib"
        if not path.exists():
            continue
        try:
            bundle = joblib.load(path)
            estimator = bundle.get("estimator") if isinstance(bundle, dict) else None
            if estimator is not None:
                estimators[key] = estimator
        except (OSError, EOFError, ValueError, TypeError):
            continue
    return estimators


def _score(probabilities: dict[str, float | None]) -> float | None:
    available = [(key, value) for key, value in probabilities.items() if value is not None]
    if not available:
        return None
    weight_sum = sum(TARGET_WEIGHTS[key] for key, _ in available)
    return round(100 * sum(TARGET_WEIGHTS[key] * float(value) for key, value in available) / weight_sum, 1)


def attach_shadow_scores(
    rows: list[dict[str, Any]],
    *,
    replay: dict[str, Any],
    model_status: dict[str, Any],
    model_dir: Path,
) -> None:
    """Mutate report rows with display-only, immutable-snapshot model output."""
    estimators = _load_estimators(model_status, model_dir)
    replay_rows = _replay_lookup(replay)
    current_samples = int(_to_float(model_status.get("dataset_training_rows")))
    required_samples = int(_to_float(model_status.get("minimum_labeled"))) or 200
    for row in rows:
        source = replay_rows.get(_identity(row)) or {}
        snapshot = source.get("first_snapshot") if isinstance(source.get("first_snapshot"), dict) else row
        normalized = feature_row_from_snapshot(
            snapshot,
            chain=source.get("chain") or row.get("chain") or row.get("chain_id"),
            symbol=source.get("symbol") or row.get("symbol"),
            contract_address=source.get("contract_address") or row.get("contract_address") or row.get("token_address"),
            entry_at=source.get("first_seen_at"),
        )
        vector = [feature_vector(normalized)]
        probabilities: dict[str, float | None] = {}
        for target in TARGETS:
            key = f"{target}x"
            estimator = estimators.get(key)
            if estimator is None:
                probabilities[key] = None
                continue
            try:
                probabilities[key] = round(float(estimator.predict_proba(vector)[0][1]), 6)
            except (IndexError, TypeError, ValueError, AttributeError):
                probabilities[key] = None
        score = _score(probabilities)
        legacy_score = _to_float(row.get("score"))
        row["rating_v2"] = {
            "status": "scored" if score is not None else "collecting",
            "score": score,
            "legacy_score": round(legacy_score, 1),
            "delta": round(score - legacy_score, 1) if score is not None else None,
            "probabilities": probabilities,
            "ready_targets": sorted(estimators),
            "sample_progress": {"current": current_samples, "required": required_samples},
            "feature_snapshot": "immutable_first_snapshot",
            "execution_effect": "none",
        }
