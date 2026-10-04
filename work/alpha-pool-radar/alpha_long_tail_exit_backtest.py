"""Compare the live 90-minute exit with convex MEME runner exits.

This is an ordered scanner-price replay. It never starts or changes a live worker.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from alpha_first_discovery_paper import classify_candidate, to_float


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPLAY = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_FAST_QUOTES = OUT_DIR / "alpha-fast-quotes.jsonl"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-long-tail-exit-backtest.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-long-tail-exit-backtest.md"
CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ExitModel:
    name: str
    label: str
    targets: tuple[tuple[float, float], ...]
    stop_multiple: float = 0.78
    trail_drawdown: float = 0.35
    max_hold_minutes: float = 90.0
    time_stop_tolerance_minutes: float = 5.0
    pre_first_target_max_hold_minutes: float | None = None
    pre_first_target_tolerance_minutes: float = 5.0


CURRENT_EXIT = ExitModel(
    name="current_90m_80_20",
    label="当前实盘：2x卖80%，尾仓35%回撤，90分钟退出",
    targets=((2.0, 0.80),),
    trail_drawdown=0.35,
    max_hold_minutes=90.0,
)

LONG_TAIL_EXIT = ExitModel(
    name="long_tail_90m_to_24h_50_10_10_30",
    label="金狗长尾：90分钟内到2x卖50%，3x卖10%，5x卖10%，30%尾仓40%回撤，最长24小时",
    targets=((2.0, 0.50), (3.0, 0.10), (5.0, 0.10)),
    trail_drawdown=0.40,
    max_hold_minutes=24.0 * 60.0,
    time_stop_tolerance_minutes=30.0,
    pre_first_target_max_hold_minutes=90.0,
    pre_first_target_tolerance_minutes=5.0,
)


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed


def normalized_points(entry_at: datetime, entry_price: float, observations: Iterable[dict[str, Any]]) -> list[tuple[datetime, float]]:
    points: list[tuple[datetime, float]] = []
    for row in observations:
        if not isinstance(row, dict):
            continue
        at = parse_time(row.get("seen_at") or row.get("quote_at") or row.get("quote_observed_at"))
        price = to_float(row.get("price_usd") or row.get("price"))
        if at is None or at <= entry_at or price <= 0:
            continue
        points.append((at, price / entry_price))
    points.sort(key=lambda item: item[0])
    deduped: list[tuple[datetime, float]] = []
    for item in points:
        if deduped and item[0] == deduped[-1][0]:
            deduped[-1] = item
        else:
            deduped.append(item)
    return deduped


def _fill(reason: str, fraction: float, multiple: float, minutes: float) -> dict[str, Any]:
    return {
        "reason": reason,
        "fraction": round(fraction, 8),
        "multiple": round(max(0.0, multiple), 8),
        "minutes": round(minutes, 4),
    }


def simulate_exit(
    *,
    entry_price: float,
    entry_at: datetime | str,
    observations: list[dict[str, Any]],
    model: ExitModel,
) -> dict[str, Any]:
    entry_time = parse_time(entry_at)
    if entry_time is None or entry_price <= 0:
        raise ValueError("entry_at and entry_price must be valid")

    points = normalized_points(entry_time, entry_price, observations)
    remaining = 1.0
    realized = 0.0
    peak_multiple = 1.0
    target_index = 0
    fills: list[dict[str, Any]] = []
    resolved = False
    exit_reason = "mark_end"
    last_multiple = 1.0

    for at, multiple in points:
        minutes = (at - entry_time).total_seconds() / 60.0
        first_target_pending = target_index == 0 and bool(model.targets)
        active_max_hold = (
            model.pre_first_target_max_hold_minutes
            if first_target_pending and model.pre_first_target_max_hold_minutes is not None
            else model.max_hold_minutes
        )
        active_tolerance = (
            model.pre_first_target_tolerance_minutes
            if first_target_pending and model.pre_first_target_max_hold_minutes is not None
            else model.time_stop_tolerance_minutes
        )
        if minutes > active_max_hold + active_tolerance:
            break
        last_multiple = multiple
        peak_multiple = max(peak_multiple, multiple)

        if multiple <= model.stop_multiple and remaining > 0:
            fills.append(_fill("stop_loss", remaining, multiple, minutes))
            realized += remaining * multiple
            remaining = 0.0
            resolved = True
            exit_reason = "stop_loss"
            break

        while target_index < len(model.targets) and multiple >= model.targets[target_index][0] and remaining > 0:
            target_multiple, fraction = model.targets[target_index]
            sold = min(remaining, fraction)
            fills.append(_fill(f"take_profit_{target_multiple:g}x", sold, target_multiple, minutes))
            realized += sold * target_multiple
            remaining -= sold
            target_index += 1

        all_targets_hit = target_index == len(model.targets)
        if all_targets_hit and remaining > 0 and multiple <= peak_multiple * (1.0 - model.trail_drawdown):
            fills.append(_fill("trailing_stop", remaining, multiple, minutes))
            realized += remaining * multiple
            remaining = 0.0
            resolved = True
            exit_reason = "trailing_stop"
            break

        first_target_pending = target_index == 0 and bool(model.targets)
        active_max_hold = (
            model.pre_first_target_max_hold_minutes
            if first_target_pending and model.pre_first_target_max_hold_minutes is not None
            else model.max_hold_minutes
        )
        if minutes >= active_max_hold and remaining > 0:
            fills.append(_fill("time_stop", remaining, multiple, minutes))
            realized += remaining * multiple
            remaining = 0.0
            resolved = True
            exit_reason = "time_stop"
            break

    marked_value = remaining * last_multiple
    exit_value = realized + marked_value
    if not points:
        last_multiple = 1.0
        marked_value = remaining
        exit_value = realized + marked_value
    conservative_value = realized
    return {
        "model": model.name,
        "resolved": resolved,
        "exit_reason": exit_reason,
        "fills": fills,
        "remaining_fraction": round(remaining, 8),
        "mark_multiple": round(last_multiple, 8),
        "gross_exit_multiple": round(exit_value, 8),
        "gross_return": round(exit_value - 1.0, 8),
        "conservative_return": round(conservative_value - 1.0 if not resolved else exit_value - 1.0, 8),
        "peak_multiple": round(peak_multiple, 8),
        "observation_count": len(points),
    }


def stress_return(
    result: dict[str, Any],
    *,
    notional_usd: float,
    roundtrip_cost_pct: float,
    gas_usd_per_swap: float,
) -> float:
    if notional_usd <= 0:
        raise ValueError("notional_usd must be positive")
    marked_exit = 1 if to_float(result.get("remaining_fraction")) > 0 else 0
    exit_count = len(result.get("fills") or []) + marked_exit
    traded_fraction = sum(to_float(fill.get("fraction")) for fill in result.get("fills") or [])
    traded_fraction += to_float(result.get("remaining_fraction"))
    friction_units = traded_fraction * roundtrip_cost_pct / 100.0
    gas_units = gas_usd_per_swap * (1 + exit_count) / notional_usd
    return to_float(result.get("gross_exit_multiple")) - 1.0 - friction_units - gas_units


def _profit_factor(values: list[float]) -> float:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses <= 0:
        return 999.0 if gains > 0 else 0.0
    return gains / losses


def _max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return abs(drawdown)


def summarize_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "win_rate": 0.0,
            "five_x_rate": 0.0,
            "ten_x_rate": 0.0,
            "unresolved_count": 0,
            "gross_average_return": 0.0,
            "stress_average_return": 0.0,
            "gross_total_units": 0.0,
            "stress_total_units": 0.0,
            "conservative_total_units": 0.0,
            "conservative_stress_total_units": 0.0,
            "gross_profit_factor": 0.0,
            "stress_profit_factor": 0.0,
            "gross_max_drawdown_units": 0.0,
            "stress_max_drawdown_units": 0.0,
            "top_1pct_profit_share": 0.0,
        }
    gross = [to_float(row.get("gross_return")) for row in rows]
    stressed = [to_float(row.get("stress_return"), to_float(row.get("gross_return"))) for row in rows]
    conservative = [
        to_float(row.get("conservative_return"), to_float(row.get("gross_return")) if row.get("resolved") else -1.0)
        for row in rows
    ]
    conservative_stressed = [
        to_float(row.get("conservative_stress_return"), conservative[index])
        for index, row in enumerate(rows)
    ]
    peaks = [to_float(row.get("peak_multiple")) for row in rows]
    positive = sorted((value for value in gross if value > 0), reverse=True)
    top_count = max(1, math.ceil(len(rows) * 0.01))
    positive_total = sum(positive)
    top_share = sum(positive[:top_count]) / positive_total if positive_total > 0 else 0.0
    return {
        "count": len(rows),
        "win_rate": sum(value > 0 for value in gross) / len(rows),
        "two_x_rate": sum(value >= 2.0 for value in peaks) / len(rows),
        "five_x_rate": sum(value >= 5.0 for value in peaks) / len(rows),
        "ten_x_rate": sum(value >= 10.0 for value in peaks) / len(rows),
        "unresolved_count": sum(not bool(row.get("resolved")) for row in rows),
        "gross_average_return": sum(gross) / len(rows),
        "gross_median_return": median(gross),
        "stress_average_return": sum(stressed) / len(rows),
        "stress_median_return": median(stressed),
        "gross_total_units": sum(gross),
        "stress_total_units": sum(stressed),
        "conservative_total_units": sum(conservative),
        "conservative_stress_total_units": sum(conservative_stressed),
        "gross_profit_factor": _profit_factor(gross),
        "stress_profit_factor": _profit_factor(stressed),
        "gross_max_drawdown_units": _max_drawdown(gross),
        "stress_max_drawdown_units": _max_drawdown(stressed),
        "top_1pct_profit_share": top_share,
    }


def _reject_bucket(reason: str) -> str:
    for prefix in (
        "首次发现市值",
        "首次发现时池龄",
        "池子太薄",
        "GMGN 硬风险偏高",
        "狙击盘比例过高",
        "筹码集中",
        "首次发现已过去",
        "价格或市值不可用",
        "没有首次发现历史",
        "大叙事突破缺少独立确认",
        "1h ",
        "5m ",
    ):
        if reason.startswith(prefix):
            return prefix
    return reason or "unknown"


def _replay_rows(replay_history: dict[str, Any]) -> list[dict[str, Any]]:
    raw = replay_history.get("rows") or {}
    if isinstance(raw, dict):
        return [row for row in raw.values() if isinstance(row, dict)]
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    return []


def _live_gate_reason(candidate: dict[str, Any], classification: dict[str, Any]) -> str | None:
    chain = str(classification.get("chain") or candidate.get("chain") or "").lower()
    if chain not in {"bsc", "robinhood"}:
        return "unsupported_live_chain"
    arm = str(classification.get("execution_arm") or "")
    first_mcap = to_float(classification.get("first_mcap_usd"))
    if chain == "bsc":
        if arm != "first_discovery":
            return "bsc_validated_first_discovery_only"
        if not 10_000 <= first_mcap <= 100_000:
            return "bsc_validated_first_mcap"
    risk_flags = candidate.get("gmgn_risk_flags") or candidate.get("risk_flags")
    if (isinstance(risk_flags, list) and risk_flags) or (not isinstance(risk_flags, list) and risk_flags):
        return "risk_flags"
    if arm == "narrative_breakout" and to_float(candidate.get("source_count")) < 2:
        return "narrative_breakout_sources"
    return None


def _score_bucket(score: float) -> str:
    if score >= 90:
        return "90+"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    if score >= 60:
        return "60-69"
    return "<60"


def select_replay_entries(replay_history: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Apply the production paper classifier to each immutable first snapshot."""
    selected: list[dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    for history_row in _replay_rows(replay_history):
        snapshot = history_row.get("first_snapshot")
        if not isinstance(snapshot, dict):
            rejected["missing_first_snapshot"] += 1
            continue
        entry_at = parse_time(history_row.get("first_seen_at"))
        entry_price = to_float(history_row.get("first_price_usd") or snapshot.get("price_usd"))
        chain = str(history_row.get("chain") or snapshot.get("chain") or "").strip().lower()
        token = str(history_row.get("contract_address") or snapshot.get("contract_address") or "").strip()
        if entry_at is None or entry_price <= 0 or not chain or not token:
            rejected["invalid_entry_identity_or_price"] += 1
            continue

        candidate = dict(snapshot)
        candidate.update(
            {
                "chain": chain,
                "contract_address": token,
                "symbol": history_row.get("symbol") or snapshot.get("symbol") or token[:10],
                "first_seen_at": entry_at.isoformat(),
                "price_usd": entry_price,
            }
        )
        classification = classify_candidate(candidate, history_row, entry_at.isoformat())
        if not classification.get("eligible"):
            rejected[_reject_bucket(str(classification.get("reject_reason") or "unknown"))] += 1
            continue
        live_reject = _live_gate_reason(candidate, classification)
        if live_reject:
            rejected[live_reject] += 1
            continue
        score = to_float(classification.get("score"))
        selected.append(
            {
                "key": f"{chain}:{token.lower()}",
                "chain": chain,
                "contract_address": token,
                "symbol": candidate["symbol"],
                "entry_at": entry_at.isoformat(),
                "entry_price": entry_price,
                "first_mcap_usd": to_float(classification.get("first_mcap_usd")),
                "arm": classification.get("execution_arm") or "first_discovery",
                "bucket": classification.get("bucket") or "unknown",
                "score": score,
                "score_bucket": _score_bucket(score),
                "liquidity_usd": to_float(candidate.get("liquidity") or candidate.get("liquidity_usd")),
                "volume24h_usd": to_float(candidate.get("volume24h") or candidate.get("dex_volume24h")),
                "change_h1_pct": to_float(candidate.get("change_h1")),
                "change_m5_pct": to_float(candidate.get("change_m5")),
                "pair_age_hours": to_float(candidate.get("pair_age_hours")),
                "top10_holder_pct": to_float(candidate.get("top10_holder_pct"), 999.0),
                "max_holder_pct": to_float(candidate.get("max_holder_pct")),
                "smart_money": to_float(candidate.get("smart_money")),
                "kol": to_float(candidate.get("kol")),
                "source_count": to_float(candidate.get("source_count")),
                "observations": history_row.get("observations") if isinstance(history_row.get("observations"), list) else [],
            }
        )
    selected.sort(key=lambda row: row["entry_at"])
    return selected, rejected


def augment_entries_with_fast_quotes(
    entries: list[dict[str, Any]],
    path: Path,
    *,
    max_horizon_minutes: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    augmented = [{**entry, "observations": list(entry.get("observations") or [])} for entry in entries]
    by_key = {str(entry.get("key")): entry for entry in augmented}
    entry_times = {key: parse_time(entry.get("entry_at")) for key, entry in by_key.items()}
    matched_entries: set[str] = set()
    lines_read = 0
    matched_quotes = 0
    invalid_lines = 0
    if not path.exists():
        return augmented, {"lines_read": 0, "matched_quotes": 0, "matched_entries": 0, "invalid_lines": 0}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines_read += 1
            try:
                quote = json.loads(line)
                chain = str(quote.get("chain") or "").strip().lower()
                token = str(quote.get("contract_address") or quote.get("token_address") or "").strip().lower()
                key = f"{chain}:{token}"
                if key not in by_key or str(quote.get("quote_status") or "").lower() != "fresh":
                    continue
                at_text = quote.get("quote_observed_at") or quote.get("quote_at")
                at = parse_time(at_text)
                entry_at = entry_times.get(key)
                price = to_float(quote.get("price_usd") or quote.get("price"))
                if at is None or entry_at is None or at <= entry_at or price <= 0:
                    continue
                if (at - entry_at).total_seconds() / 60.0 > max_horizon_minutes:
                    continue
                by_key[key]["observations"].append({"seen_at": at.isoformat(), "price_usd": price})
                matched_quotes += 1
                matched_entries.add(key)
            except (json.JSONDecodeError, TypeError, ValueError):
                invalid_lines += 1
    return augmented, {
        "lines_read": lines_read,
        "matched_quotes": matched_quotes,
        "matched_entries": len(matched_entries),
        "invalid_lines": invalid_lines,
    }


def _conservative_stress_return(
    result: dict[str, Any],
    *,
    notional_usd: float,
    roundtrip_cost_pct: float,
    gas_usd_per_swap: float,
) -> float:
    if result.get("resolved"):
        return stress_return(
            result,
            notional_usd=notional_usd,
            roundtrip_cost_pct=roundtrip_cost_pct,
            gas_usd_per_swap=gas_usd_per_swap,
        )
    fills = result.get("fills") or []
    realized_value = sum(to_float(fill.get("fraction")) * to_float(fill.get("multiple")) for fill in fills)
    sold_fraction = sum(to_float(fill.get("fraction")) for fill in fills)
    friction_units = sold_fraction * roundtrip_cost_pct / 100.0
    gas_units = gas_usd_per_swap * (1 + len(fills)) / notional_usd
    return realized_value - 1.0 - friction_units - gas_units


def _simulate_entry(
    entry: dict[str, Any],
    model: ExitModel,
    *,
    notional_usd: float,
    roundtrip_cost_pct: float,
    gas_usd_per_swap: float,
) -> dict[str, Any]:
    result = simulate_exit(
        entry_price=to_float(entry.get("entry_price")),
        entry_at=str(entry.get("entry_at") or ""),
        observations=entry.get("observations") or [],
        model=model,
    )
    result.update({key: value for key, value in entry.items() if key != "observations"})
    result["stress_return"] = stress_return(
        result,
        notional_usd=notional_usd,
        roundtrip_cost_pct=roundtrip_cost_pct,
        gas_usd_per_swap=gas_usd_per_swap,
    )
    result["conservative_stress_return"] = _conservative_stress_return(
        result,
        notional_usd=notional_usd,
        roundtrip_cost_pct=roundtrip_cost_pct,
        gas_usd_per_swap=gas_usd_per_swap,
    )
    return result


def _group_summaries(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return {key: summarize_results(values) for key, values in sorted(groups.items())}


def _period(rows: list[dict[str, Any]]) -> dict[str, Any]:
    times = [str(row.get("first_seen_at") or row.get("entry_at") or "") for row in rows]
    times = [value for value in times if value]
    return {"first_seen_min": min(times) if times else None, "first_seen_max": max(times) if times else None}


def build_backtest(
    replay_history: dict[str, Any],
    *,
    notional_usd: float = 5.0,
    roundtrip_cost_pct: float = 5.0,
    gas_usd_per_swap: float = 0.12,
    models: tuple[ExitModel, ...] = (CURRENT_EXIT, LONG_TAIL_EXIT),
    fast_quotes_path: Path | None = None,
) -> dict[str, Any]:
    entries, rejected = select_replay_entries(replay_history)
    quote_augmentation = None
    if fast_quotes_path is not None:
        max_horizon = max(model.max_hold_minutes + model.time_stop_tolerance_minutes for model in models)
        entries, quote_augmentation = augment_entries_with_fast_quotes(
            entries,
            fast_quotes_path,
            max_horizon_minutes=max_horizon,
        )
    model_rows: dict[str, list[dict[str, Any]]] = {}
    model_reports: list[dict[str, Any]] = []
    for model in models:
        rows = [
            _simulate_entry(
                entry,
                model,
                notional_usd=notional_usd,
                roundtrip_cost_pct=roundtrip_cost_pct,
                gas_usd_per_swap=gas_usd_per_swap,
            )
            for entry in entries
        ]
        model_rows[model.name] = rows
        model_reports.append(
            {
                "name": model.name,
                "label": model.label,
                "parameters": asdict(model),
                "summary": summarize_results(rows),
                "by_chain": _group_summaries(rows, "chain"),
                "by_arm": _group_summaries(rows, "arm"),
                "by_bucket": _group_summaries(rows, "bucket"),
                "by_score_bucket": _group_summaries(rows, "score_bucket"),
                "top_gross_trades": sorted(rows, key=lambda row: to_float(row.get("gross_return")), reverse=True)[:30],
                "worst_gross_trades": sorted(rows, key=lambda row: to_float(row.get("gross_return")))[:30],
            }
        )

    current = {row["key"]: row for row in model_rows.get(CURRENT_EXIT.name, [])}
    tail = {row["key"]: row for row in model_rows.get(LONG_TAIL_EXIT.name, [])}
    paired = []
    for key in sorted(current.keys() & tail.keys(), key=lambda item: current[item]["entry_at"]):
        current_row = current[key]
        tail_row = tail[key]
        paired.append(
            {
                "key": key,
                "symbol": current_row.get("symbol"),
                "chain": current_row.get("chain"),
                "entry_at": current_row.get("entry_at"),
                "peak_multiple": current_row.get("peak_multiple"),
                "current_gross_return": current_row.get("gross_return"),
                "tail_gross_return": tail_row.get("gross_return"),
                "gross_delta": to_float(tail_row.get("gross_return")) - to_float(current_row.get("gross_return")),
                "current_stress_return": current_row.get("stress_return"),
                "tail_stress_return": tail_row.get("stress_return"),
                "stress_delta": to_float(tail_row.get("stress_return")) - to_float(current_row.get("stress_return")),
            }
        )
    deltas = [to_float(row.get("stress_delta")) for row in paired]
    comparison = {
        "same_entry_count": len(paired),
        "tail_better_count": sum(value > 0 for value in deltas),
        "tail_worse_count": sum(value < 0 for value in deltas),
        "tail_equal_count": sum(value == 0 for value in deltas),
        "average_stress_delta": sum(deltas) / len(deltas) if deltas else 0.0,
        "median_stress_delta": median(deltas) if deltas else 0.0,
        "total_stress_delta_units": sum(deltas),
        "largest_tail_improvements": sorted(paired, key=lambda row: to_float(row.get("stress_delta")), reverse=True)[:30],
        "largest_tail_regressions": sorted(paired, key=lambda row: to_float(row.get("stress_delta")))[:30],
    }
    all_rows = _replay_rows(replay_history)
    return {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "source_mode": "ordered_first_snapshot_scanner_price_replay",
        "period": _period(all_rows),
        "cost_stress": {
            "notional_usd": notional_usd,
            "estimated_roundtrip_cost_pct": roundtrip_cost_pct,
            "estimated_gas_usd_per_swap": gas_usd_per_swap,
        },
        "universe": {
            "replay_rows": len(all_rows),
            "eligible_entries": len(entries),
            "rejected_entries": len(all_rows) - len(entries),
            "rejected_by_reason": dict(rejected.most_common()),
            "eligible_by_chain": dict(Counter(str(row.get("chain")) for row in entries)),
            "eligible_by_arm": dict(Counter(str(row.get("arm")) for row in entries)),
        },
        "quote_augmentation": quote_augmentation,
        "models": model_reports,
        "comparison": comparison,
        "limitations": [
            "Entry selection uses only each token's stored first snapshot; later rating/status is not used.",
            "Prices are scanner observations, not historical OKX executable reverse quotes.",
            "Sparse observations can miss intrabar stops, peaks, taxes and liquidity collapse.",
            "Unresolved rows are shown both marked to the last observation and with remaining tokens valued at zero.",
            "Unit drawdown assumes one independent equal-sized trade in chronological order; it is not a capital-constrained portfolio simulation.",
        ],
    }


def _pct(value: Any) -> str:
    return f"{to_float(value) * 100:.1f}%"


def markdown_report(result: dict[str, Any]) -> str:
    period = result.get("period") or {}
    universe = result.get("universe") or {}
    lines = [
        "# MEME 长尾退出回测",
        "",
        f"- 时间：{period.get('first_seen_min')} 至 {period.get('first_seen_max')}",
        f"- 原始项目：{universe.get('replay_rows', 0)}；按首次快照通过现行入场规则：{universe.get('eligible_entries', 0)}",
        f"- 成本压力：单笔 {result['cost_stress']['notional_usd']:.2f}U，往返摩擦 {result['cost_stress']['estimated_roundtrip_cost_pct']:.1f}%，每次 swap Gas {result['cost_stress']['estimated_gas_usd_per_swap']:.2f}U",
        "",
        "| 退出模型 | 样本 | 毛收益/笔 | 成本后/笔 | 成本后总单位 | 保守总单位 | 胜率 | PF | 最大回撤 | 5x | 10x | 未完整退出 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model in result.get("models") or []:
        summary = model.get("summary") or {}
        lines.append(
            f"| {model.get('label')} | {summary.get('count', 0)} | {_pct(summary.get('gross_average_return'))} | "
            f"{_pct(summary.get('stress_average_return'))} | {to_float(summary.get('stress_total_units')):.2f} | "
            f"{to_float(summary.get('conservative_stress_total_units')):.2f} | {_pct(summary.get('win_rate'))} | "
            f"{to_float(summary.get('stress_profit_factor')):.2f} | {to_float(summary.get('stress_max_drawdown_units')):.2f} | "
            f"{_pct(summary.get('five_x_rate'))} | {_pct(summary.get('ten_x_rate'))} | {summary.get('unresolved_count', 0)} |"
        )
    comparison = result.get("comparison") or {}
    lines.extend(
        [
            "",
            "## 同一入场对比",
            "",
            f"- 同样本：{comparison.get('same_entry_count', 0)} 笔。",
            f"- 长尾更好：{comparison.get('tail_better_count', 0)}；更差：{comparison.get('tail_worse_count', 0)}；相同：{comparison.get('tail_equal_count', 0)}。",
            f"- 长尾相对当前的成本后平均差：{_pct(comparison.get('average_stress_delta'))}；总差：{to_float(comparison.get('total_stress_delta_units')):.2f} 单位。",
            "",
            "## 分链",
            "",
            "| 模型 | 链 | 样本 | 成本后/笔 | 成本后总单位 | PF |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for model in result.get("models") or []:
        for chain, summary in (model.get("by_chain") or {}).items():
            lines.append(
                f"| {model.get('name')} | {chain} | {summary.get('count', 0)} | {_pct(summary.get('stress_average_return'))} | "
                f"{to_float(summary.get('stress_total_units')):.2f} | {to_float(summary.get('stress_profit_factor')):.2f} |"
            )
    lines.extend(
        [
            "",
            "## 按首次市值",
            "",
            "| 模型 | 市值桶 | 样本 | 成本后/笔 | PF | 5x |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for model in result.get("models") or []:
        for bucket, summary in (model.get("by_bucket") or {}).items():
            lines.append(
                f"| {model.get('name')} | {bucket} | {summary.get('count', 0)} | {_pct(summary.get('stress_average_return'))} | "
                f"{to_float(summary.get('stress_profit_factor')):.2f} | {_pct(summary.get('five_x_rate'))} |"
            )
    lines.extend(
        [
            "",
            "## 按首次评分",
            "",
            "| 模型 | 分数桶 | 样本 | 成本后/笔 | PF | 5x |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for model in result.get("models") or []:
        for bucket, summary in (model.get("by_score_bucket") or {}).items():
            lines.append(
                f"| {model.get('name')} | {bucket} | {summary.get('count', 0)} | {_pct(summary.get('stress_average_return'))} | "
                f"{to_float(summary.get('stress_profit_factor')):.2f} | {_pct(summary.get('five_x_rate'))} |"
            )
    lines.extend(["", "## 主要拒绝原因", ""])
    for reason, count in list((universe.get("rejected_by_reason") or {}).items())[:15]:
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## 局限", ""])
    lines.extend(f"- {item}" for item in result.get("limitations") or [])
    return "\n".join(lines) + "\n"


def run_from_paths(
    replay_path: Path,
    json_out: Path,
    md_out: Path,
    *,
    notional_usd: float,
    roundtrip_cost_pct: float,
    gas_usd_per_swap: float,
    fast_quotes_path: Path | None = None,
) -> dict[str, Any]:
    replay = json.loads(replay_path.read_text(encoding="utf-8-sig"))
    result = build_backtest(
        replay,
        notional_usd=notional_usd,
        roundtrip_cost_pct=roundtrip_cost_pct,
        gas_usd_per_swap=gas_usd_per_swap,
        fast_quotes_path=fast_quotes_path,
    )
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(markdown_report(result), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--fast-quotes", type=Path, default=DEFAULT_FAST_QUOTES)
    parser.add_argument("--no-fast-quotes", action="store_true")
    parser.add_argument("--notional-usd", type=float, default=5.0)
    parser.add_argument("--roundtrip-cost-pct", type=float, default=5.0)
    parser.add_argument("--gas-usd-per-swap", type=float, default=0.12)
    args = parser.parse_args()
    result = run_from_paths(
        args.replay,
        args.json_out,
        args.md_out,
        notional_usd=args.notional_usd,
        roundtrip_cost_pct=args.roundtrip_cost_pct,
        gas_usd_per_swap=args.gas_usd_per_swap,
        fast_quotes_path=None if args.no_fast_quotes else args.fast_quotes,
    )
    print(json.dumps({"universe": result["universe"], "comparison": {key: value for key, value in result["comparison"].items() if not isinstance(value, list)}}, ensure_ascii=False))
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
