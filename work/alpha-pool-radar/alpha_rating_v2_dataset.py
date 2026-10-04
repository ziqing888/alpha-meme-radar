"""Build leakage-safe MEME rating labels from immutable first snapshots.

This module is research-only. It never starts or changes an execution worker.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPLAY = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-rating-v2-dataset.json"
DEFAULT_CSV_OUT = OUT_DIR / "alpha-rating-v2-dataset.csv"
TARGETS = (2, 3, 5, 10)


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _value(snapshot: dict[str, Any], *keys: str) -> float:
    for key in keys:
        if snapshot.get(key) not in (None, ""):
            return to_float(snapshot.get(key))
    return 0.0


def _label(trajectory: dict[str, Any], target: int) -> int | None:
    value = trajectory.get(f"hit_{target}x_before_stop")
    if isinstance(value, bool):
        return int(value)
    if to_float(trajectory.get("observed_hours")) >= 24:
        return 0
    return None


def _raw_rows(replay: dict[str, Any]) -> list[dict[str, Any]]:
    raw = replay.get("rows") or {}
    if isinstance(raw, dict):
        return [row for row in raw.values() if isinstance(row, dict)]
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    return []


def feature_row_from_snapshot(
    snapshot: dict[str, Any],
    *,
    chain: Any = "",
    symbol: Any = "",
    contract_address: Any = "",
    entry_at: Any = "",
) -> dict[str, Any]:
    """Normalize one immutable observation into the V2 feature contract."""
    return {
        "entry_at": entry_at or "",
        "chain": str(chain or snapshot.get("chain") or snapshot.get("chain_id") or "").lower(),
        "symbol": symbol or snapshot.get("symbol") or "",
        "contract_address": contract_address or snapshot.get("contract_address") or snapshot.get("token_address") or "",
        "mcap_usd": _value(snapshot, "mcap", "market_cap"),
        "liquidity_usd": _value(snapshot, "liquidity", "liquidity_usd"),
        "volume24h_usd": _value(snapshot, "volume24h", "dex_volume24h", "volume24h_usd"),
        "pair_age_hours": _value(snapshot, "pair_age_hours"),
        "change_m5_pct": _value(snapshot, "change_m5", "change_m5_pct"),
        "change_h1_pct": _value(snapshot, "change_h1", "change_h1_pct"),
        "change_h24_pct": _value(snapshot, "change_h24", "change_h24_pct"),
        "legacy_score": _value(snapshot, "score"),
        "entry_score": _value(snapshot, "entry_score"),
        "gold_dog_score": _value(snapshot, "gold_dog_score"),
        "gold_dog_conviction_score": _value(snapshot, "gold_dog_conviction_score"),
        "source_count": _value(snapshot, "source_count"),
        "source_hit_total": _value(snapshot, "source_hit_total"),
        "smart_money": _value(snapshot, "smart_money"),
        "kol": _value(snapshot, "kol"),
        "holders": _value(snapshot, "holders"),
        "top10_holder_pct": _value(snapshot, "top10_holder_pct"),
        "max_holder_pct": _value(snapshot, "max_holder_pct"),
        "security_filter_score": _value(snapshot, "security_filter_score"),
        "holder_quality_score": _value(snapshot, "holder_quality_score"),
        "narrative_quality": str(snapshot.get("narrative_quality") or "unknown"),
    }


def build_training_rows(replay: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source in _raw_rows(replay):
        snapshot = source.get("first_snapshot") or {}
        trajectory = source.get("trajectory") or {}
        if not isinstance(snapshot, dict) or not snapshot or not isinstance(trajectory, dict) or not trajectory:
            continue
        if trajectory.get("coverage_from_first") is not True:
            continue
        row = {
            **feature_row_from_snapshot(
                snapshot,
                chain=source.get("chain"),
                symbol=source.get("symbol"),
                contract_address=source.get("contract_address"),
                entry_at=source.get("first_seen_at"),
            ),
            "sample_count": int(to_float(trajectory.get("sample_count"))),
            "observed_hours": to_float(trajectory.get("observed_hours")),
            "max_multiple": to_float(trajectory.get("max_multiple")),
            "min_multiple": to_float(trajectory.get("min_multiple")),
        }
        for target in TARGETS:
            row[f"label_{target}x_before_stop"] = _label(trajectory, target)
        result.append(row)
    result.sort(key=lambda row: str(row.get("entry_at") or ""))
    return result


def readiness_summary(
    rows: list[dict[str, Any]],
    *,
    minimum_labeled: int = 500,
    minimum_each_class: int = 50,
) -> dict[str, Any]:
    targets: dict[str, Any] = {}
    for target in TARGETS:
        key = f"label_{target}x_before_stop"
        labels = [row.get(key) for row in rows if row.get(key) in (0, 1)]
        positive = sum(value == 1 for value in labels)
        negative = sum(value == 0 for value in labels)
        targets[f"{target}x"] = {
            "labeled": len(labels),
            "positive": positive,
            "negative": negative,
            "positive_rate": round(positive / len(labels), 6) if labels else None,
            "ready": len(labels) >= minimum_labeled and min(positive, negative) >= minimum_each_class,
        }
    return {
        "training_rows": len(rows),
        "minimum_labeled": minimum_labeled,
        "minimum_each_class": minimum_each_class,
        "targets": targets,
        "any_target_ready": any(item["ready"] for item in targets.values()),
    }


def build_dataset(replay: dict[str, Any]) -> dict[str, Any]:
    rows = build_training_rows(replay)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "shadow_only_immutable_first_snapshot",
        "stop_multiple": 0.78,
        "label_horizon_hours": 24,
        "readiness": readiness_summary(rows),
        "rows": rows,
        "next_model": "LightGBM chronological split plus sigmoid probability calibration",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--csv-out", type=Path, default=DEFAULT_CSV_OUT)
    args = parser.parse_args()
    replay = json.loads(args.replay.read_text(encoding="utf-8-sig"))
    result = build_dataset(replay)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(args.csv_out, result["rows"])
    print(json.dumps({"readiness": result["readiness"], "json": str(args.json_out), "csv": str(args.csv_out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
