from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from statistics import median
from typing import Any, Mapping

from alpha_token_intelligence import normalize_identity

CHECKPOINTS_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
}
STAGE_RANK = {
    "aggregate_discovery": 0,
    "trend_watch": 0,
    "blocked_risk": 0,
    "aggregate_early_bird": 1,
    "smart_money": 2,
    "aggregate_confirmation": 3,
    "cooling": 3,
    "revival": 4,
}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _classification(token: Mapping[str, Any], chain: str) -> tuple[str, str, list[str]] | None:
    state = str(token.get("primary_state") or "")
    axes = token.get("ranking_axes") if isinstance(token.get("ranking_axes"), Mapping) else {}
    behavior = axes.get("market_behavior") if isinstance(axes.get("market_behavior"), Mapping) else {}
    reasons = sorted(str(item) for item in behavior.get("flags") or [])
    if state == "blocked_risk":
        risk = token.get("risk") if isinstance(token.get("risk"), Mapping) else {}
        return "blocked", "blocked_risk", sorted(str(item) for item in risk.get("hard_failures") or [])
    if state == "trend_watch" or behavior.get("disposition") == "observe":
        return "downgraded", "trend_watch", reasons
    if chain == "arc" and state == "new":
        return "discovered", "aggregate_discovery", []
    if state == "resonating":
        return ("confirmed" if chain == "arc" else "selected"), "aggregate_confirmation", []
    if state == "smart_cluster":
        return "selected", "smart_money", []
    if state == "building":
        return "selected", "aggregate_early_bird", []
    if chain == "arc" and state == "cooling":
        return "cooling", "cooling", reasons
    if chain == "arc" and state == "revival":
        return "revival", "revival", reasons
    return None


def _market_cap(token: Mapping[str, Any]) -> float | None:
    market = token.get("market") if isinstance(token.get("market"), Mapping) else {}
    return _number(market.get("market_cap_usd"))


def _summarize(records: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    cohorts: dict[str, dict[str, Any]] = {}
    stages: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for record in records.values():
        cohort = str(record.get("cohort") or "unknown")
        stages[str(record.get("entry_stage") or "unknown")] = stages.get(str(record.get("entry_stage") or "unknown"), 0) + 1
        for reason in record.get("reasons") or []:
            reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        bucket = cohorts.setdefault(cohort, {"count": 0, "checkpoint_returns": {}})
        bucket["count"] += 1
        for checkpoint, result in (record.get("checkpoints") or {}).items():
            if not isinstance(result, Mapping) or result.get("return_pct") is None:
                continue
            bucket["checkpoint_returns"].setdefault(checkpoint, []).append(float(result["return_pct"]))
    for bucket in cohorts.values():
        bucket["checkpoint_returns"] = {
            checkpoint: {
                "sample_count": len(values),
                "median_return_pct": round(median(values), 4),
                "positive_count": sum(value > 0 for value in values),
            }
            for checkpoint, values in sorted(bucket["checkpoint_returns"].items())
        }
    return {
        "cohorts": dict(sorted(cohorts.items())),
        "entry_stages": dict(sorted(stages.items())),
        "reasons": dict(sorted(reasons.items())),
    }


def update_monitor_outcomes(
    previous: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
    *,
    observed_at: str,
) -> dict[str, Any]:
    records = deepcopy((previous or {}).get("records") or {})
    now = _utc(observed_at)
    for token in snapshot.get("tokens") or []:
        if not isinstance(token, Mapping):
            continue
        token_identity = token.get("identity") if isinstance(token.get("identity"), Mapping) else {}
        try:
            chain, contract_address = normalize_identity(
                token_identity.get("chain"), token_identity.get("contract_address")
            )
        except ValueError:
            continue
        identity = f"{chain}:{contract_address}"
        classification = _classification(token, chain)
        market_cap = _market_cap(token)
        if not identity or classification is None or market_cap is None:
            continue
        cohort, stage, reasons = classification
        record = records.get(identity)
        if not isinstance(record, dict):
            record = {
                "identity": identity,
                "chain": chain,
                "contract_address": contract_address,
                "symbol": token_identity.get("symbol"),
                "cohort": cohort,
                "entry_cohort": cohort,
                "cohort_history": [{"cohort": cohort, "stage": stage, "observed_at": observed_at}],
                "entry_stage": stage,
                "best_stage": stage,
                "reasons": reasons,
                "entry_at": observed_at,
                "entry_market_cap_usd": market_cap,
                "latest_at": observed_at,
                "latest_market_cap_usd": market_cap,
                "max_market_cap_usd": market_cap,
                "checkpoints": {},
            }
            records[identity] = record
        elif record.get("cohort") != cohort:
            record.setdefault("cohort_history", []).append(
                {"cohort": cohort, "stage": stage, "observed_at": observed_at}
            )
        record["cohort"] = cohort
        record["latest_stage"] = stage
        record["reasons"] = reasons
        record["latest_at"] = observed_at
        record["latest_market_cap_usd"] = market_cap
        record["max_market_cap_usd"] = max(float(record.get("max_market_cap_usd") or market_cap), market_cap)
        if STAGE_RANK.get(stage, 0) > STAGE_RANK.get(str(record.get("best_stage") or ""), 0):
            record["best_stage"] = stage
        elapsed = max(0.0, (now - _utc(str(record["entry_at"]))).total_seconds())
        entry_cap = float(record["entry_market_cap_usd"])
        for checkpoint, seconds in CHECKPOINTS_SECONDS.items():
            if checkpoint in record["checkpoints"] or elapsed < seconds:
                continue
            record["checkpoints"][checkpoint] = {
                "observed_at": observed_at,
                "market_cap_usd": market_cap,
                "return_pct": round((market_cap / entry_cap - 1) * 100, 4),
            }
    return {
        "schema_version": 1,
        "updated_at": observed_at,
        "records": records,
        "summary": _summarize(records),
    }
