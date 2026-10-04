"""Independent, explainable discovery ranking for MEME candidates.

This module deliberately has no execution policy.  Its output can order
simultaneous discoveries, but it cannot authorize or reject an order.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping


COMPONENT_CAPS = {
    "timing": 20.0,
    "source_evidence": 20.0,
    "liquidity": 15.0,
    "transaction_acceleration": 15.0,
    "narrative": 10.0,
    "smart_money": 10.0,
    "holder_structure": 10.0,
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _metric(row: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if name in row:
            value = _number(row[name])
            if value is not None:
                return value
    return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _clamp(value: float, maximum: float) -> float:
    return max(0.0, min(maximum, value))


def _timing_score(
    row: Mapping[str, Any],
    first_snapshot: Mapping[str, Any],
    now: datetime,
) -> float:
    first_seen = None
    for source in (first_snapshot, row):
        first_seen = _parse_time(
            source.get("first_seen_at")
            or source.get("discovered_at")
            or source.get("first_observed_at")
        )
        if first_seen is not None:
            break
    if first_seen is None:
        return 0.0
    age_seconds = (now.astimezone(timezone.utc) - first_seen.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0:
        return 0.0
    return _clamp(20.0 * (1.0 - age_seconds / 600.0), COMPONENT_CAPS["timing"])


def _canonical_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "okx" in text:
        return "okx"
    if "gmgn" in text:
        return "gmgn"
    if text == "ds" or "dexscreener" in text:
        return "dexscreener"
    if "noxa" in text:
        return "noxa"
    if "binance" in text:
        return "binance"
    return text


def _source_score(row: Mapping[str, Any]) -> float:
    raw_sources: list[Any] = []
    # Prefer provenance fields. Display labels can also contain enrichment or
    # processing stages such as DS and Alpha_AI, which are not independent
    # discovery evidence and must not increase the live rank.
    for name in ("source_groups", "sources", "source_names", "source_labels"):
        value = row.get(name)
        if isinstance(value, (list, tuple, set)):
            raw_sources.extend(value)
        elif value:
            raw_sources.append(value)
        if raw_sources:
            break
    groups = {_canonical_source(value) for value in raw_sources}
    groups.discard("")
    if groups:
        count = len(groups)
    else:
        count_value = _metric(row, "source_count", "independent_source_count")
        count = max(0, int(count_value)) if count_value is not None else 0
    return _clamp(count * 5.0, COMPONENT_CAPS["source_evidence"])


def _liquidity_score(row: Mapping[str, Any]) -> float:
    liquidity = _metric(row, "liquidity_usd", "current_liquidity_usd", "liquidity")
    if liquidity is None or liquidity < 5_000:
        return 0.0
    if liquidity < 10_000:
        return 3.0
    if liquidity < 20_000:
        return 6.0
    if liquidity < 50_000:
        return 10.0
    if liquidity < 100_000:
        return 13.0
    return 15.0


def _transaction_count(row: Mapping[str, Any]) -> float | None:
    direct = _metric(row, "transaction_count5m", "tx_count5m", "transactions_5m")
    if direct is not None:
        return max(0.0, direct)
    buys = _metric(row, "buy_count5m", "buys5m", "purchase_count5m")
    sells = _metric(row, "sell_count5m", "sells5m")
    if buys is None and sells is None:
        return None
    return max(0.0, buys or 0.0) + max(0.0, sells or 0.0)


def _transaction_score(row: Mapping[str, Any]) -> float:
    current = _transaction_count(row)
    if current is None:
        return 0.0
    activity = _clamp(current / 5.0, 10.0)
    previous = _metric(
        row,
        "previous_tx_count5m",
        "prior_tx_count5m",
        "previous_transaction_count5m",
    )
    ratio = _metric(row, "transaction_acceleration_ratio", "tx_acceleration_ratio")
    if previous is not None and previous > 0:
        ratio = current / previous
    acceleration = _clamp(((ratio or 0.0) - 1.0) * 5.0, 5.0)
    return _clamp(activity + acceleration, COMPONENT_CAPS["transaction_acceleration"])


def _scaled_score(row: Mapping[str, Any], maximum: float, *names: str) -> float:
    value = _metric(row, *names)
    if value is None:
        return 0.0
    return _clamp(value / 100.0 * maximum, maximum)


def _smart_money_score(row: Mapping[str, Any]) -> float:
    explicit = _metric(row, "smart_money_score", "smartmoney_score")
    if explicit is not None:
        return _clamp(explicit / 10.0, COMPONENT_CAPS["smart_money"])
    count = _metric(row, "smart_money", "smart_money_count", "smartmoney_buy_count")
    return _clamp((count or 0.0) / 2.5, COMPONENT_CAPS["smart_money"])


def _top10_quality(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    if value <= 25:
        return 10.0
    if value <= 40:
        return 10.0 - (value - 25.0) * 0.4
    if value <= 55:
        return 4.0 - (value - 40.0) * (4.0 / 15.0)
    return 0.0


def _largest_holder_quality(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    if value <= 10:
        return 10.0
    if value <= 20:
        return 20.0 - value
    return 0.0


def _holder_score(row: Mapping[str, Any]) -> float:
    explicit = _metric(row, "holder_quality_score")
    if explicit is not None:
        return _clamp(explicit / 10.0, COMPONENT_CAPS["holder_structure"])
    evidence = [
        value
        for value in (
            _top10_quality(_metric(row, "top10_holder_pct", "top10_pct")),
            _largest_holder_quality(_metric(row, "max_holder_pct", "largest_holder_pct")),
        )
        if value is not None
    ]
    return min(evidence) if evidence else 0.0


def compute_discovery_rank(
    candidate: Mapping[str, Any],
    *,
    first_snapshot: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return ranking evidence only; never return an execution decision."""
    if not isinstance(candidate, Mapping):
        raise TypeError("candidate_must_be_mapping")
    snapshot = first_snapshot if isinstance(first_snapshot, Mapping) else {}
    evaluated_at = now or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None:
        raise ValueError("now_must_be_timezone_aware")
    evidence = dict(candidate)
    evidence.update(snapshot)

    components = {
        "timing": _timing_score(candidate, snapshot, evaluated_at),
        "source_evidence": _source_score(evidence),
        "liquidity": _liquidity_score(evidence),
        "transaction_acceleration": _transaction_score(evidence),
        "narrative": _scaled_score(evidence, COMPONENT_CAPS["narrative"], "narrative_score"),
        "smart_money": _smart_money_score(evidence),
        "holder_structure": _holder_score(evidence),
    }
    rounded = {name: round(_clamp(value, COMPONENT_CAPS[name]), 2) for name, value in components.items()}
    return {
        "rank_score": round(_clamp(sum(rounded.values()), 100.0), 2),
        "rank_components": rounded,
    }
