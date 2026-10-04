from __future__ import annotations

from datetime import datetime
import math
from typing import Any

from alpha_smart_money_evidence import QUOTE_FRESH_SECONDS, parse_timestamp


ACTIONABLE_BUCKETS = {"lead", "ambush"}
RISK_BUCKETS = {"danger"}
GOLD_RETURN_PCT = 900.0
GOLD_MIN_PEAK_MCAP = 1_000_000.0
TRAJECTORY_STOP_MULTIPLE = 0.78
TRAJECTORY_TARGET_MULTIPLES = (2, 3, 5, 10)
FIRST_SNAPSHOT_KEYS = (
    "quote_observed_at",
    "quote_status",
    "quote_source",
    "quote_time_basis",
    "market_cap_source",
    "fdv_source",
    "valuation_type",
    "price_usd",
    "pair_address",
    "pool_address",
    "source_labels",
    "sources",
    "source_groups",
    "source_count",
    "source_hit_counts",
    "source_hit_total",
    "source_repeat_score",
    "source_repeat_flags",
    "score",
    "entry_score",
    "gold_dog_score",
    "gold_dog_conviction_score",
    "gold_dog_rationale",
    "entry_stage",
    "entry_level",
    "holder_quality_score",
    "security_filter_score",
    "liquidity_age_score",
    "dev_trust_score",
    "smart_kol_score",
    "bot_manipulation_score",
    "backtest_rule_score",
    "backtest_rule_tags",
    "backtest_profile_score",
    "narrative_quality",
    "narrative_primary_tag",
    "narrative_penalty_tags",
    "narrative_reasons",
    "mcap",
    "market_cap",
    "liquidity",
    "volume24h",
    "dex_volume24h",
    "pair_age_hours",
    "change_m5",
    "change_h1",
    "change_h24",
    "txns24h",
    "smart_money",
    "kol",
    "holders",
    "top10_holder_pct",
    "top20_holder_pct",
    "max_holder_pct",
    "gmgn_risk_flags",
    "narrative_tags",
    "pro_signal_tags",
    "shadow_original_bucket",
    "shadow_original_action",
)


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def outcome_return_pct(row: dict[str, Any]) -> float:
    if row.get("peak_return_pct") is not None:
        return to_float(row.get("peak_return_pct"))
    return to_float(row.get("return_since_first_pct"))


def outcome_peak_mcap(row: dict[str, Any]) -> float:
    peak_mcap = to_float(
        row.get("peak_mcap")
        or row.get("max_mcap")
        or row.get("ath_mcap")
        or row.get("peak_market_cap")
        or row.get("max_market_cap")
    )
    if peak_mcap > 0:
        return peak_mcap
    snap = row.get("first_snapshot") or {}
    first_mcap = to_float(snap.get("mcap") or snap.get("market_cap") or snap.get("fdv") or row.get("mcap") or row.get("market_cap"))
    peak_return = outcome_return_pct(row)
    if first_mcap > 0 and peak_return > -100:
        return round(first_mcap * (1 + peak_return / 100), 2)
    return first_mcap


def is_gold_dog_outcome(row: dict[str, Any], return_pct: float | None = None) -> bool:
    outcome_return = outcome_return_pct(row) if return_pct is None else to_float(return_pct)
    return outcome_return >= GOLD_RETURN_PCT and outcome_peak_mcap(row) >= GOLD_MIN_PEAK_MCAP


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def row_key(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or row.get("name") or "").strip().lower()
    chain = str(row.get("chain") or row.get("chain_id") or row.get("chainId") or "").strip().lower()
    address = str(row.get("contract_address") or row.get("token_address") or row.get("address") or "").strip().lower()
    return f"{chain}:{address or symbol}"


def current_price(row: dict[str, Any]) -> float:
    return to_float(row.get("price_usd") or row.get("priceUsd") or row.get("price"))


def pct_change(start: float, end: float) -> float | None:
    if start <= 0 or end <= 0:
        return None
    return round((end / start - 1) * 100, 2)


def hit_status(bucket: str, return_pct: float | None) -> str:
    if bucket in ACTIONABLE_BUCKETS:
        if return_pct is None:
            return "tracking"
        if return_pct >= 15:
            return "hit"
        if return_pct <= -15:
            return "miss"
        return "tracking"
    if bucket in RISK_BUCKETS:
        if return_pct is not None and return_pct <= 0:
            return "avoided"
        return "watch_risk"
    if bucket == "reject":
        return "ignored"
    return "tracking"


def avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def horizon_win_rate(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    completed = [to_float(row.get(key)) for row in rows if row.get(key) is not None]
    wins = [value for value in completed if value > 0]
    return {
        "count": len(completed),
        "win_count": len(wins),
        "win_rate_pct": round(len(wins) / len(completed) * 100, 2) if completed else 0.0,
        "avg_return_pct": avg(completed),
    }


def first_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in FIRST_SNAPSHOT_KEYS:
        if key in row and row.get(key) not in (None, ""):
            snapshot[key] = row.get(key)
    return snapshot


def update_trajectory(
    previous: dict[str, Any] | None,
    observations: list[dict[str, Any]],
    first_price: float,
    coverage_from_first: bool,
) -> dict[str, Any]:
    trajectory = dict(previous or {})
    trajectory.setdefault("coverage_from_first", coverage_from_first)
    pending = observations[-1:] if previous else observations
    sample_count = int(to_float(trajectory.get("sample_count")))

    for observation in pending:
        price = to_float(observation.get("price_usd"))
        stamp = str(observation.get("quote_observed_at") or observation.get("seen_at") or "")
        stamp_time = parse_timestamp(stamp)
        if first_price <= 0 or price <= 0 or stamp_time is None:
            continue

        prior_stamp = parse_timestamp(trajectory.get("last_observed_at"))
        if prior_stamp is not None:
            gap_seconds = max(0.0, (stamp_time - prior_stamp).total_seconds())
            trajectory["max_gap_seconds"] = round(
                max(to_float(trajectory.get("max_gap_seconds")), gap_seconds), 2
            )

        multiple = price / first_price
        sample_count += 1
        trajectory.setdefault("first_observed_at", stamp)
        trajectory["last_observed_at"] = stamp

        if not trajectory.get("max_multiple") or multiple > to_float(trajectory.get("max_multiple")):
            trajectory["max_multiple"] = round(multiple, 8)
            trajectory["max_multiple_at"] = stamp
        if not trajectory.get("min_multiple") or multiple < to_float(trajectory.get("min_multiple")):
            trajectory["min_multiple"] = round(multiple, 8)
            trajectory["min_multiple_at"] = stamp

        if multiple <= TRAJECTORY_STOP_MULTIPLE:
            trajectory.setdefault("stop_hit_at", stamp)
        for target in TRAJECTORY_TARGET_MULTIPLES:
            if multiple >= target:
                trajectory.setdefault(f"hit_{target}x_at", stamp)

    trajectory["sample_count"] = sample_count
    first_observed = parse_timestamp(trajectory.get("first_observed_at"))
    last_observed = parse_timestamp(trajectory.get("last_observed_at"))
    if first_observed is not None and last_observed is not None:
        trajectory["observed_hours"] = round(
            max(0.0, (last_observed - first_observed).total_seconds() / 3600), 4
        )

    stop_time = parse_timestamp(trajectory.get("stop_hit_at"))
    for target in TRAJECTORY_TARGET_MULTIPLES:
        target_time = parse_timestamp(trajectory.get(f"hit_{target}x_at"))
        label = None
        if target_time is not None:
            label = stop_time is None or target_time < stop_time
        elif stop_time is not None:
            label = False
        trajectory[f"hit_{target}x_before_stop"] = label
    return trajectory


def observations_cover_first_seen(observations: list[dict[str, Any]], first_seen_at: str) -> bool:
    first_time = parse_timestamp(first_seen_at)
    stamps = [
        parse_timestamp(observation.get("quote_observed_at") or observation.get("seen_at"))
        for observation in observations
        if isinstance(observation, dict)
    ]
    observed = [stamp for stamp in stamps if stamp is not None]
    if first_time is None or not observed:
        return False
    delta = (min(observed) - first_time).total_seconds()
    return abs(delta) <= max(QUOTE_FRESH_SECONDS, 120)


def action_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        action = str(row.get("recommendation_action") or "未分组")
        grouped.setdefault(action, []).append(row)

    result: dict[str, dict[str, Any]] = {}
    for action, action_rows in grouped.items():
        hits = [row for row in action_rows if row.get("hit_status") == "hit"]
        misses = [row for row in action_rows if row.get("hit_status") == "miss"]
        avoided = [row for row in action_rows if row.get("hit_status") == "avoided"]
        returns_1h = [to_float(row.get("return_1h_pct")) for row in action_rows if row.get("return_1h_pct") is not None]
        returns_6h = [to_float(row.get("return_6h_pct")) for row in action_rows if row.get("return_6h_pct") is not None]
        returns_24h = [to_float(row.get("return_24h_pct")) for row in action_rows if row.get("return_24h_pct") is not None]
        result[action] = {
            "count": len(action_rows),
            "hit_count": len(hits),
            "miss_count": len(misses),
            "avoided_count": len(avoided),
            "hit_rate_pct": round(len(hits) / len(action_rows) * 100, 2) if action_rows else 0.0,
            "avg_return_1h_pct": avg(returns_1h),
            "avg_return_6h_pct": avg(returns_6h),
            "avg_return_24h_pct": avg(returns_24h),
            "horizon_win_rates": {
                "1h": horizon_win_rate(action_rows, "return_1h_pct"),
                "6h": horizon_win_rate(action_rows, "return_6h_pct"),
                "24h": horizon_win_rate(action_rows, "return_24h_pct"),
            },
        }
    return result


def update_replay_history(history: dict[str, Any] | None, rows: list[dict[str, Any]], now_iso: str) -> dict[str, Any]:
    payload = dict(history or {})
    existing_rows = dict(payload.get("rows") or {})
    pending_first_snapshots = dict(payload.get("pending_first_snapshots") or {})
    now = parse_time(now_iso)

    for row in rows:
        key = row_key(row)
        price = current_price(row)
        stamp = parse_timestamp(row.get("quote_observed_at"))
        valid_identity = bool(key.strip(":"))
        valid_quote = (
            valid_identity
            and math.isfinite(price)
            and price > 0
            and row.get("quote_status") == "fresh"
            and stamp is not None
            and now.tzinfo is not None
            and 0 <= (now - stamp).total_seconds() <= QUOTE_FRESH_SECONDS
        )
        raw_mcap = row.get("mcap", row.get("market_cap"))
        mcap = to_float(raw_mcap)
        valid_market_cap = (
            not isinstance(raw_mcap, bool)
            and math.isfinite(mcap)
            and mcap > 0
            and ("valuation_type" not in row or row["valuation_type"] == "market_cap")
        )
        if (
            key not in existing_rows
            and valid_identity
            and not (valid_quote and valid_market_cap)
            and key not in pending_first_snapshots
        ):
            pending_first_snapshots[key] = {
                "key": key,
                "symbol": row.get("symbol") or "",
                "chain": row.get("chain") or "",
                "contract_address": row.get("contract_address") or row.get("token_address") or "",
                "first_seen_at": now_iso,
                "first_snapshot": first_snapshot(row),
            }
        if not valid_quote:
            continue

        bucket = str(row.get("recommendation_bucket") or "")
        previous = dict(existing_rows.get(key) or {})
        if key not in existing_rows:
            if not valid_market_cap:
                continue
        elif any(field not in previous for field in ("first_seen_at", "first_price_usd", "first_snapshot")):
            # Legacy baselines need explicit review, never an automatic repair.
            continue
        pending_discovery = pending_first_snapshots.pop(key, None)
        first_seen_at = (
            previous.get("first_seen_at")
            or (pending_discovery or {}).get("first_seen_at")
            or now_iso
        )
        first_price = previous.get("first_price_usd", price)
        observations = list(previous.get("observations") or [])
        observations.append(
            {
                "seen_at": now_iso,
                "quote_observed_at": row.get("quote_observed_at"),
                "price_usd": price,
                "bucket": bucket,
                "action": row.get("recommendation_action") or "",
            }
        )
        trajectory = update_trajectory(
            previous.get("trajectory"),
            observations,
            to_float(first_price),
            observations_cover_first_seen(observations, first_seen_at),
        )
        observations = observations[-96:]

        latest_return = pct_change(to_float(first_price), price)
        first_time = parse_timestamp(first_seen_at)
        elapsed_hours = (now - first_time).total_seconds() / 3600 if first_time else 0

        merged = {
            **previous,
            "key": key,
            "symbol": row.get("symbol") or previous.get("symbol") or "",
            "chain": row.get("chain") or previous.get("chain") or "",
            "contract_address": row.get("contract_address") or previous.get("contract_address") or row.get("token_address") or "",
            "first_seen_at": first_seen_at,
            "latest_seen_at": now_iso,
            "first_price_usd": first_price,
            "first_tradeable_quote_at": previous.get("first_tradeable_quote_at") or row.get("quote_observed_at") or now_iso,
            "latest_price_usd": price,
            "recommendation_bucket": bucket or previous.get("recommendation_bucket") or "",
            "recommendation_action": row.get("recommendation_action") or previous.get("recommendation_action") or "",
            "return_since_first_pct": latest_return,
            "observations": observations,
            "trajectory": trajectory,
            "first_snapshot": previous.get("first_snapshot", first_snapshot(row)),
        }
        if pending_discovery:
            merged.setdefault("discovery_first_seen_at", pending_discovery.get("first_seen_at"))
            merged.setdefault("discovery_snapshot", pending_discovery.get("first_snapshot") or {})
        if elapsed_hours >= 1 and previous.get("return_1h_pct") is None:
            merged["return_1h_pct"] = latest_return
            trajectory["return_1h_observed_at"] = now_iso
        if elapsed_hours >= 6 and previous.get("return_6h_pct") is None:
            merged["return_6h_pct"] = latest_return
            trajectory["return_6h_observed_at"] = now_iso
        if elapsed_hours >= 24 and previous.get("return_24h_pct") is None:
            merged["return_24h_pct"] = latest_return
            trajectory["return_24h_observed_at"] = now_iso
        merged["hit_status"] = hit_status(str(merged["recommendation_bucket"]), latest_return)
        existing_rows[key] = merged

    payload["rows"] = existing_rows
    payload["pending_first_snapshots"] = pending_first_snapshots
    payload["updated_at"] = now_iso
    payload["summary"] = replay_summary(payload)
    return payload


def profile_bucket(snapshot: dict[str, Any]) -> list[str]:
    mcap = to_float(snapshot.get("mcap") or snapshot.get("market_cap"))
    age = to_float(snapshot.get("pair_age_hours"))
    smart_money = to_float(snapshot.get("smart_money"))
    kol = to_float(snapshot.get("kol"))
    top10 = to_float(snapshot.get("top10_holder_pct"))
    conviction = to_float(snapshot.get("gold_dog_conviction_score"))
    buckets: list[str] = []
    if 0 < age <= 24:
        buckets.append("fresh_pool")
    elif age > 72:
        buckets.append("stale_pool")
    if 0 < mcap <= 1_000_000:
        buckets.append("micro_cap")
    elif mcap > 5_000_000:
        buckets.append("large_for_meme_entry")
    if smart_money >= 20:
        buckets.append("strong_smart_money")
    if kol >= 5:
        buckets.append("kol_confirmed")
    if 0 < top10 <= 25:
        buckets.append("healthy_top10")
    elif top10 >= 40:
        buckets.append("concentrated_top10")
    if conviction >= 75:
        buckets.append("high_conviction")
    return buckets or ["unclassified"]


def source_combo(snapshot: dict[str, Any]) -> str:
    labels = [str(label) for label in (snapshot.get("source_labels") or []) if str(label).strip()]
    if not labels:
        labels = [str(source) for source in (snapshot.get("sources") or []) if str(source).strip()]
    labels = sorted(set(labels))
    return "+".join(labels) if labels else "unknown"


def grouped_replay_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    hits = [row for row in rows if row.get("hit_status") == "hit"]
    misses = [row for row in rows if row.get("hit_status") == "miss"]
    returns_1h = [to_float(row.get("return_1h_pct")) for row in rows if row.get("return_1h_pct") is not None]
    rows_with_returns = [
        row
        for row in rows
        if row.get("peak_return_pct") is not None or row.get("return_since_first_pct") is not None
    ]
    returns_total = [outcome_return_pct(row) for row in rows_with_returns]
    runners = [value for value in returns_total if value >= 50]
    golds = [row for row in rows_with_returns if is_gold_dog_outcome(row)]
    return {
        "count": len(rows),
        "hit_count": len(hits),
        "miss_count": len(misses),
        "hit_rate_pct": round(len(hits) / len(rows) * 100, 2) if rows else 0.0,
        "runner_count": len(runners),
        "runner_rate_pct": round(len(runners) / len(rows) * 100, 2) if rows else 0.0,
        "gold_count": len(golds),
        "gold_rate_pct": round(len(golds) / len(rows) * 100, 2) if rows else 0.0,
        "max_return_pct": round(max(returns_total), 2) if returns_total else None,
        "avg_return_1h_pct": avg(returns_1h),
        "horizon_win_rates": {"1h": horizon_win_rate(rows, "return_1h_pct")},
    }


def replay_profile_summary(history: dict[str, Any], min_count: int = 3) -> dict[str, dict[str, Any]]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    by_feature: dict[str, list[dict[str, Any]]] = {}
    for row in (history.get("rows") or {}).values():
        if row.get("recommendation_bucket") not in ACTIONABLE_BUCKETS:
            continue
        snapshot = row.get("first_snapshot") or {}
        by_source.setdefault(source_combo(snapshot), []).append(row)
        for bucket in profile_bucket(snapshot):
            by_feature.setdefault(bucket, []).append(row)

    return {
        "by_source_combo": {
            key: grouped_replay_stats(rows)
            for key, rows in sorted(by_source.items())
            if len(rows) >= min_count
        },
        "by_feature": {
            key: grouped_replay_stats(rows)
            for key, rows in sorted(by_feature.items())
            if len(rows) >= min_count
        },
    }


def replay_summary(history: dict[str, Any]) -> dict[str, Any]:
    rows = list((history.get("rows") or {}).values())
    actionable = [row for row in rows if row.get("recommendation_bucket") in ACTIONABLE_BUCKETS]
    hits = [row for row in actionable if row.get("hit_status") == "hit"]
    misses = [row for row in actionable if row.get("hit_status") == "miss"]
    avoided = [row for row in rows if row.get("hit_status") == "avoided"]
    hit_rate = round(len(hits) / len(actionable) * 100, 2) if actionable else 0.0
    return {
        "tracked_count": len(rows),
        "actionable_count": len(actionable),
        "hit_count": len(hits),
        "miss_count": len(misses),
        "hit_rate_pct": hit_rate,
        "risk_avoided_count": len(avoided),
        "by_action": action_summary(rows),
        "by_profile": replay_profile_summary(history),
    }


def replay_action_calibration(summary: dict[str, Any], min_action_count: int = 6, min_horizon_count: int = 5) -> dict[str, dict[str, Any]]:
    calibration: dict[str, dict[str, Any]] = {}
    for action, item in (summary.get("by_action") or {}).items():
        horizon_1h = ((item.get("horizon_win_rates") or {}).get("1h") or {})
        count = int(to_float(item.get("count")))
        horizon_count = int(to_float(horizon_1h.get("count")))
        avg_1h = to_float(item.get("avg_return_1h_pct"))
        win_rate = to_float(horizon_1h.get("win_rate_pct"))
        hit_rate = to_float(item.get("hit_rate_pct"))
        adjustment = 0
        risk_level = "unknown"
        reason = "样本不足，暂不校准"
        if count >= min_action_count and horizon_count >= min_horizon_count:
            if avg_1h <= -4 or (win_rate < 40 and hit_rate < 25):
                adjustment = -12
                risk_level = "weak"
                reason = f"回测拖累：1h均值{avg_1h:.2f}%，胜率{win_rate:.1f}%"
            elif avg_1h >= 2 and win_rate >= 52:
                adjustment = 3
                risk_level = "ok"
                reason = f"回测支持：1h均值{avg_1h:.2f}%，胜率{win_rate:.1f}%"
            else:
                risk_level = "neutral"
                reason = f"回测中性：1h均值{avg_1h:.2f}%，胜率{win_rate:.1f}%"
        calibration[str(action)] = {
            "score_adjustment": adjustment,
            "risk_level": risk_level,
            "reason": reason,
            "sample_count": count,
            "horizon_1h_count": horizon_count,
            "avg_return_1h_pct": avg_1h if horizon_count else None,
            "win_rate_1h_pct": win_rate if horizon_count else None,
        }
    return calibration


def replay_leaderboards(history: dict[str, Any], limit: int = 8) -> dict[str, list[dict[str, Any]]]:
    rows = list((history.get("rows") or {}).values())
    best_hits = [row for row in rows if row.get("hit_status") == "hit"]
    worst_misses = [row for row in rows if row.get("hit_status") == "miss"]
    risk_avoided = [row for row in rows if row.get("hit_status") == "avoided"]

    best_hits.sort(key=lambda row: to_float(row.get("return_since_first_pct")), reverse=True)
    worst_misses.sort(key=lambda row: to_float(row.get("return_since_first_pct")))
    risk_avoided.sort(key=lambda row: to_float(row.get("return_since_first_pct")))

    return {
        "best_hits": best_hits[:limit],
        "worst_misses": worst_misses[:limit],
        "risk_avoided": risk_avoided[:limit],
    }
