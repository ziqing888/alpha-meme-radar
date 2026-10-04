#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any

from alpha_history_import import load_history_file
from alpha_history_import import load_melt_dataset_dir
from alpha_history_import import merge_histories
from alpha_replay import ACTIONABLE_BUCKETS
from alpha_replay import grouped_replay_stats


GOLD_RETURN_PCT = 900.0
GOLD_MIN_PEAK_MCAP = 1_000_000.0
RULE_SEARCH_BUCKETS = ACTIONABLE_BUCKETS | {"shadow"}


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def edge_score(row: dict[str, Any]) -> float:
    return round(
        to_float(row.get("gold_rate_pct")) * 2.2
        + to_float(row.get("runner_rate_pct")) * 1.1
        + to_float(row.get("hit_rate_pct")) * 0.55
        + max(-30.0, min(60.0, to_float(row.get("avg_return_1h_pct")))) * 0.4,
        2,
    )


def edge_rows(groups: dict[str, dict[str, Any]], kind: str, min_count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, item in groups.items():
        if int(to_float(item.get("count"))) < min_count:
            continue
        rows.append(
            {
                "kind": kind,
                "name": name,
                "count": int(to_float(item.get("count"))),
                "hit_rate_pct": to_float(item.get("hit_rate_pct")),
                "runner_rate_pct": to_float(item.get("runner_rate_pct")),
                "gold_rate_pct": to_float(item.get("gold_rate_pct")),
                "avg_return_1h_pct": item.get("avg_return_1h_pct"),
                "max_return_pct": item.get("max_return_pct"),
                "edge_score": edge_score(item),
            }
        )
    return rows


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def history_window_summary(history: dict[str, Any], target_days: int = 365) -> dict[str, Any]:
    times: list[datetime] = []
    for row in (history.get("rows") or {}).values():
        for key in ("first_seen_at", "latest_seen_at"):
            item = parse_time(row.get(key))
            if item:
                times.append(item)
    if not times:
        return {
            "target_days": target_days,
            "coverage_days": 0.0,
            "coverage_ok": False,
            "first_seen_at": None,
            "latest_seen_at": None,
            "status": "没有历史样本，不能做一年回测",
        }
    first_seen = min(times)
    latest_seen = max(times)
    coverage_days = round((latest_seen - first_seen).total_seconds() / 86400, 2)
    coverage_ok = coverage_days >= target_days * 0.9
    return {
        "target_days": target_days,
        "coverage_days": coverage_days,
        "coverage_ok": coverage_ok,
        "first_seen_at": first_seen.isoformat(),
        "latest_seen_at": latest_seen.isoformat(),
        "status": "覆盖足够，可以参考一年回测" if coverage_ok else f"历史覆盖 {coverage_days:.2f} 天，不足一年，当前只能做小样本回测",
    }


def snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("first_snapshot") or {}


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
    snap = snapshot(row)
    first_mcap = to_float(
        snap.get("mcap")
        or snap.get("market_cap")
        or snap.get("fdv")
        or row.get("mcap")
        or row.get("market_cap")
        or row.get("fdv")
    )
    peak_return = outcome_return_pct(row)
    if first_mcap > 0 and peak_return > -100:
        return round(first_mcap * (1 + peak_return / 100), 2)
    return first_mcap


def is_gold_dog_outcome(
    row: dict[str, Any],
    gold_return_pct: float = GOLD_RETURN_PCT,
    min_peak_mcap: float = GOLD_MIN_PEAK_MCAP,
) -> bool:
    return outcome_return_pct(row) >= gold_return_pct and outcome_peak_mcap(row) >= min_peak_mcap


def source_count(value: Any) -> float:
    if isinstance(value, list):
        return float(len([item for item in value if str(item).strip()]))
    return 0.0


def snapshot_metric(row: dict[str, Any], key: str) -> float:
    snap = snapshot(row)
    mcap = to_float(snap.get("mcap") or snap.get("market_cap") or snap.get("fdv"))
    volume = to_float(snap.get("volume24h") or snap.get("dex_volume24h"))
    liquidity = to_float(snap.get("liquidity") or snap.get("liquidity_usd"))

    if key == "mcap":
        return mcap
    if key == "volume_to_mcap":
        return volume / mcap if mcap > 0 else 0.0
    if key == "liquidity_to_mcap":
        return liquidity / mcap if mcap > 0 else 0.0
    if key == "source_count":
        return max(source_count(snap.get("source_labels")), source_count(snap.get("sources")))
    if key == "risk_flag_count":
        return source_count(snap.get("gmgn_risk_flags"))
    if key == "narrative_count":
        return source_count(snap.get("narrative_tags"))
    return to_float(snap.get(key))


def rule_ok(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    if row.get("recommendation_bucket") not in RULE_SEARCH_BUCKETS:
        return False
    if not snapshot(row):
        return False
    for key, threshold in (rule.get("max") or {}).items():
        value = snapshot_metric(row, key)
        if value <= 0 or value > to_float(threshold):
            return False
    for key, threshold in (rule.get("min") or {}).items():
        value = snapshot_metric(row, key)
        if value < to_float(threshold):
            return False
    return True


def miss_reason(row: dict[str, Any]) -> str:
    reasons: list[str] = []
    bucket = str(row.get("recommendation_bucket") or "")
    if bucket not in ACTIONABLE_BUCKETS:
        reasons.append("没有进入唯一主推")
    if not snapshot(row):
        reasons.append("缺少首次快照")
        return "；".join(reasons)
    age = snapshot_metric(row, "pair_age_hours")
    mcap = snapshot_metric(row, "mcap")
    conviction = snapshot_metric(row, "gold_dog_conviction_score")
    top10 = snapshot_metric(row, "top10_holder_pct")
    smart_money = snapshot_metric(row, "smart_money")
    kol = snapshot_metric(row, "kol")
    source_num = snapshot_metric(row, "source_count")
    risk_num = snapshot_metric(row, "risk_flag_count")
    volume_to_mcap = snapshot_metric(row, "volume_to_mcap")
    if age <= 0:
        reasons.append("缺池龄")
    elif age > 12:
        reasons.append("池龄超过12h强窗口")
    if mcap <= 0:
        reasons.append("缺市值")
    elif mcap > 300_000:
        reasons.append("市值超过$300K强窗口")
    if conviction < 75:
        reasons.append("确认分太低")
    if top10 >= 40:
        reasons.append("Top10偏高")
    if smart_money < 20 or kol < 5:
        reasons.append("聪明钱/KOL不足")
    if source_num < 2:
        reasons.append("多源不足")
    if risk_num > 0:
        reasons.append("有风险旗标")
    if volume_to_mcap < 0.3:
        reasons.append("量市比不足")
    return "；".join(reasons or ["命中规则但排序输给更强画像"])


def missed_gold_diagnostics(rows: list[dict[str, Any]], gold_return_pct: float = GOLD_RETURN_PCT) -> list[dict[str, Any]]:
    missed: list[dict[str, Any]] = []
    for row in rows:
        outcome_return = outcome_return_pct(row)
        if not is_gold_dog_outcome(row, gold_return_pct=gold_return_pct):
            continue
        if row.get("recommendation_bucket") in ACTIONABLE_BUCKETS:
            continue
        missed.append(
            {
                "symbol": row.get("symbol") or "",
                "chain": row.get("chain") or "",
                "return_since_first_pct": outcome_return,
                "peak_mcap": outcome_peak_mcap(row),
                "bucket": row.get("recommendation_bucket") or "",
                "miss_reason": miss_reason(row),
                "first_snapshot": snapshot(row),
            }
        )
    missed.sort(key=lambda row: to_float(row.get("return_since_first_pct")), reverse=True)
    return missed


def gold_pick_audit(rows: list[dict[str, Any]], gold_return_pct: float = GOLD_RETURN_PCT) -> dict[str, Any]:
    searchable = searchable_rule_rows(rows)
    actionable = [row for row in searchable if row.get("recommendation_bucket") in ACTIONABLE_BUCKETS]
    ten_x_rows = [row for row in searchable if outcome_return_pct(row) >= gold_return_pct]
    ten_x_mcap_unknown = [row for row in ten_x_rows if outcome_peak_mcap(row) <= 0]
    ten_x_below_mcap = [row for row in ten_x_rows if 0 < outcome_peak_mcap(row) < GOLD_MIN_PEAK_MCAP]
    gold_rows = [row for row in searchable if is_gold_dog_outcome(row, gold_return_pct=gold_return_pct)]
    caught_gold = [row for row in actionable if is_gold_dog_outcome(row, gold_return_pct=gold_return_pct)]
    wrong_gold_picks = [row for row in actionable if not is_gold_dog_outcome(row, gold_return_pct=gold_return_pct)]
    missed_gold = [row for row in gold_rows if row.get("recommendation_bucket") not in ACTIONABLE_BUCKETS]
    return {
        "gold_return_pct": gold_return_pct,
        "gold_min_peak_mcap": GOLD_MIN_PEAK_MCAP,
        "searchable_count": len(searchable),
        "actionable_count": len(actionable),
        "ten_x_count": len(ten_x_rows),
        "ten_x_mcap_unknown_count": len(ten_x_mcap_unknown),
        "ten_x_below_mcap_count": len(ten_x_below_mcap),
        "total_gold_count": len(gold_rows),
        "caught_gold_count": len(caught_gold),
        "missed_gold_count": len(missed_gold),
        "wrong_gold_pick_count": len(wrong_gold_picks),
        "gold_capture_rate_pct": round(len(caught_gold) / len(gold_rows) * 100, 2) if gold_rows else 0.0,
        "gold_pick_precision_pct": round(len(caught_gold) / len(actionable) * 100, 2) if actionable else 0.0,
    }


def rule_key(rule: dict[str, Any]) -> str:
    return json.dumps({"min": rule.get("min") or {}, "max": rule.get("max") or {}}, sort_keys=True)


def manual_rule_grid() -> list[dict[str, Any]]:
    return [
        {"name": "高确认+新池", "min": {"gold_dog_conviction_score": 75}, "max": {"pair_age_hours": 24}},
        {"name": "微市值+新池", "max": {"mcap": 1_000_000, "pair_age_hours": 24}},
        {"name": "聪明钱+KOL", "min": {"smart_money": 20, "kol": 5}},
        {"name": "健康筹码+新池", "max": {"top10_holder_pct": 25, "pair_age_hours": 24}},
        {"name": "高确认+微市值", "min": {"gold_dog_conviction_score": 75}, "max": {"mcap": 1_000_000}},
        {"name": "微市值+聪明钱", "min": {"smart_money": 20}, "max": {"mcap": 1_000_000}},
        {
            "name": "严格金狗画像",
            "min": {"gold_dog_conviction_score": 75, "smart_money": 20, "kol": 5},
            "max": {"mcap": 1_000_000, "pair_age_hours": 24, "top10_holder_pct": 25},
        },
    ]


def auto_rule_grid() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []

    for age, mcap in product([6, 12, 24, 48], [300_000, 1_000_000, 3_000_000, 5_000_000]):
        rules.append({"name": f"新池<= {age}h + 市值<= {short_money(mcap)}", "max": {"pair_age_hours": age, "mcap": mcap}})
        for conviction in [65, 70, 75, 85]:
            rules.append(
                {
                    "name": f"确认>={conviction} + 新池<= {age}h + 市值<= {short_money(mcap)}",
                    "min": {"gold_dog_conviction_score": conviction},
                    "max": {"pair_age_hours": age, "mcap": mcap},
                }
            )
        for volume_to_mcap in [0.3, 0.5, 1.0, 2.0]:
            rules.append(
                {
                    "name": f"量市比>={volume_to_mcap:g} + 新池<= {age}h + 市值<= {short_money(mcap)}",
                    "min": {"volume_to_mcap": volume_to_mcap},
                    "max": {"pair_age_hours": age, "mcap": mcap},
                }
            )
        for top10 in [20, 25, 30, 35]:
            rules.append(
                {
                    "name": f"Top10<={top10}% + 新池<= {age}h + 市值<= {short_money(mcap)}",
                    "max": {"top10_holder_pct": top10, "pair_age_hours": age, "mcap": mcap},
                }
            )

    for smart, kol in product([10, 20, 30, 50], [3, 5, 10]):
        rules.append({"name": f"聪明钱>={smart} + KOL>={kol}", "min": {"smart_money": smart, "kol": kol}})
        rules.append(
            {
                "name": f"聪明钱>={smart} + KOL>={kol} + 新池",
                "min": {"smart_money": smart, "kol": kol},
                "max": {"pair_age_hours": 24},
            }
        )
        rules.append(
            {
                "name": f"聪明钱>={smart} + KOL>={kol} + 多源",
                "min": {"smart_money": smart, "kol": kol, "source_count": 2},
            }
        )

    for source_min, conviction, age in product([2, 3], [65, 70, 75], [12, 24]):
        rules.append(
            {
                "name": f"多源>={source_min} + 确信>={conviction} + 新池<= {age}h",
                "min": {"source_count": source_min, "gold_dog_conviction_score": conviction},
                "max": {"pair_age_hours": age},
            }
        )

    for top10, mcap in product([20, 25, 30], [1_000_000, 3_000_000]):
        rules.append(
            {
                "name": f"无GMGN风险 + Top10<={top10}% + 市值<= {short_money(mcap)}",
                "max": {"risk_flag_count": 0, "top10_holder_pct": top10, "mcap": mcap},
            }
        )

    unique: dict[str, dict[str, Any]] = {}
    for rule in rules:
        unique.setdefault(rule_key(rule), rule)
    return list(unique.values())


def candidate_rule_grid() -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for rule in [*manual_rule_grid(), *auto_rule_grid()]:
        unique.setdefault(rule_key(rule), rule)
    return list(unique.values())


def sample_weight(count: float) -> float:
    if count >= 20:
        return 12.0
    if count >= 10:
        return 8.0
    if count >= 5:
        return 4.0
    return 0.0


def rule_score(stats: dict[str, Any]) -> float:
    count = to_float(stats.get("count"))
    return round(
        to_float(stats.get("gold_rate_pct")) * 3.0
        + to_float(stats.get("runner_rate_pct")) * 1.4
        + to_float(stats.get("gold_capture_rate_pct")) * 1.1
        + to_float(stats.get("hit_rate_pct")) * 0.65
        + max(-30.0, min(80.0, to_float(stats.get("avg_return_1h_pct")))) * 0.35
        - to_float(stats.get("false_positive_rate_pct")) * 0.35
        - to_float(stats.get("candidate_pressure_pct")) * 0.15
        + sample_weight(count),
        2,
    )


def searchable_rule_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("recommendation_bucket") in RULE_SEARCH_BUCKETS and snapshot(row)
    ]


def enrich_rule_stats(stats: dict[str, Any], matched_rows: list[dict[str, Any]], searchable_rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_gold = [row for row in searchable_rows if is_gold_dog_outcome(row)]
    matched_gold = [row for row in matched_rows if is_gold_dog_outcome(row)]
    false_positive = [row for row in matched_rows if outcome_return_pct(row) < 50]
    return {
        **stats,
        "total_gold_count": len(total_gold),
        "gold_capture_count": len(matched_gold),
        "gold_capture_rate_pct": round(len(matched_gold) / len(total_gold) * 100, 2) if total_gold else 0.0,
        "false_positive_count": len(false_positive),
        "false_positive_rate_pct": round(len(false_positive) / len(matched_rows) * 100, 2) if matched_rows else 0.0,
        "candidate_pressure_pct": round(len(matched_rows) / len(searchable_rows) * 100, 2) if searchable_rows else 0.0,
    }


def evaluate_candidate_rules(rows: list[dict[str, Any]], min_count: int = 3) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    searchable_rows = searchable_rule_rows(rows)
    for rule in candidate_rule_grid():
        matched = [(idx, row) for idx, row in enumerate(rows) if rule_ok(row, rule)]
        if len(matched) < min_count:
            continue
        matched_rows = [row for _, row in matched]
        stats = enrich_rule_stats(grouped_replay_stats(matched_rows), matched_rows, searchable_rows)
        signature = "|".join(str(row.get("key") or idx) for idx, row in matched)
        results.append({**rule, **stats, "rule_score": rule_score(stats), "_match_signature": signature})
    results.sort(
        key=lambda row: (
            to_float(row.get("rule_score")),
            to_float(row.get("gold_rate_pct")),
            to_float(row.get("runner_rate_pct")),
            to_float(row.get("count")),
        ),
        reverse=True,
    )
    deduped: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    for row in results:
        signature = str(row.get("_match_signature") or "")
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        public_row = dict(row)
        public_row.pop("_match_signature", None)
        deduped.append(public_row)
    return deduped


def strategy_optimization(candidate_rules: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    searchable_rows = searchable_rule_rows(rows)
    total_gold_count = len([row for row in searchable_rows if is_gold_dog_outcome(row)])
    viable = [
        rule
        for rule in candidate_rules
        if to_float(rule.get("gold_rate_pct")) >= 10 and to_float(rule.get("false_positive_rate_pct")) <= 70
    ]
    primary = viable[0] if viable else (candidate_rules[0] if candidate_rules else None)
    recommendations: list[str] = []
    if primary:
        recommendations.append(
            f"主推优先用 `{primary.get('name')}`：金狗率 {to_float(primary.get('gold_rate_pct')):.2f}%，"
            f"捕获 {to_float(primary.get('gold_capture_rate_pct')):.2f}%，误报 {to_float(primary.get('false_positive_rate_pct')):.2f}%。"
        )
        if to_float(primary.get("gold_capture_rate_pct")) < 30:
            recommendations.append("捕获率偏低：保留一个影子观察池继续学习漏网金狗，不要只记录主推。")
        if to_float(primary.get("false_positive_rate_pct")) > 50:
            recommendations.append("误报偏高：下一轮提高确认分、压低市值窗口或增加风险过滤。")
    else:
        recommendations.append("可用样本不足，先继续导入历史特征或扩大影子观察池。")
    return {
        "target": "single_pick_gold_dog",
        "searchable_count": len(searchable_rows),
        "total_gold_count": total_gold_count,
        "primary_rule": primary,
        "recommendations": recommendations,
    }


def next_rules(best_edges: list[dict[str, Any]], weak_edges: list[dict[str, Any]]) -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    for edge in best_edges[:5]:
        if to_float(edge.get("gold_rate_pct")) >= 10 or to_float(edge.get("runner_rate_pct")) >= 25:
            rules.append(
                {
                    "action": "加权",
                    "name": str(edge.get("name") or ""),
                    "reason": f"金狗率 {to_float(edge.get('gold_rate_pct')):.2f}%，runner {to_float(edge.get('runner_rate_pct')):.2f}%",
                }
            )
    for edge in weak_edges[:5]:
        if to_float(edge.get("hit_rate_pct")) <= 10 and to_float(edge.get("avg_return_1h_pct")) < 0:
            rules.append(
                {
                    "action": "降权",
                    "name": str(edge.get("name") or ""),
                    "reason": f"命中率 {to_float(edge.get('hit_rate_pct')):.2f}%，1h均值 {to_float(edge.get('avg_return_1h_pct')):.2f}%",
                }
            )
    return rules[:8]


def build_backtest_payload(history: dict[str, Any], min_count: int = 3, target_days: int = 365) -> dict[str, Any]:
    summary = history.get("summary") or {}
    profile = summary.get("by_profile") or {}
    source_edges = edge_rows(profile.get("by_source_combo") or {}, "source", min_count)
    feature_edges = edge_rows(profile.get("by_feature") or {}, "feature", min_count)
    all_edges = [*source_edges, *feature_edges]
    best_edges = sorted(all_edges, key=lambda row: (to_float(row.get("edge_score")), to_float(row.get("count"))), reverse=True)[:12]
    weak_edges = sorted(all_edges, key=lambda row: (to_float(row.get("edge_score")), -to_float(row.get("count"))))[:12]
    history_rows = list((history.get("rows") or {}).values())
    candidate_rules = evaluate_candidate_rules(history_rows, min_count=min_count)
    missed_gold = missed_gold_diagnostics(history_rows)
    strategy = strategy_optimization(candidate_rules, history_rows)
    audit = gold_pick_audit(history_rows)
    return {
        "summary": {
            "tracked_count": summary.get("tracked_count", 0),
            "actionable_count": summary.get("actionable_count", 0),
            "hit_count": summary.get("hit_count", 0),
            "miss_count": summary.get("miss_count", 0),
            "hit_rate_pct": summary.get("hit_rate_pct", 0),
        },
        "history_window": history_window_summary(history, target_days=target_days),
        "missed_gold": {
            "gold_return_pct": GOLD_RETURN_PCT,
            "gold_min_peak_mcap": GOLD_MIN_PEAK_MCAP,
            "count": len(missed_gold),
            "examples": missed_gold[:20],
        },
        "gold_pick_audit": audit,
        "best_edges": best_edges,
        "weak_edges": weak_edges,
        "candidate_rules": candidate_rules[:30],
        "strategy_optimization": strategy,
        "searched_rule_count": len(candidate_rule_grid()),
        "next_rules": next_rules(best_edges, weak_edges),
    }


def load_backtest_history(
    out_dir: Path,
    import_paths: list[str | Path] | None = None,
    melt_data_dirs: list[str | Path] | None = None,
) -> dict[str, Any]:
    history_path = out_dir / "alpha-radar-replay-history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text(encoding="utf-8"))
    else:
        history = {"rows": {}}
    for import_path in import_paths or []:
        history = merge_histories(history, load_history_file(import_path))
    for melt_dir in melt_data_dirs or []:
        history = merge_histories(history, load_melt_dataset_dir(melt_dir))
    return history


def fmt_pct(value: Any) -> str:
    if value is None:
        return "--"
    return f"{to_float(value):.2f}%"


def short_money(value: Any) -> str:
    n = to_float(value)
    if n >= 1_000_000:
        return f"${n / 1_000_000:g}M"
    if n >= 1_000:
        return f"${n / 1_000:g}K"
    return f"${n:g}"


def render_edge(row: dict[str, Any]) -> str:
    return (
        f"- {row['kind']} `{row['name']}`：样本 {row['count']}，"
        f"命中 {fmt_pct(row['hit_rate_pct'])}，runner {fmt_pct(row['runner_rate_pct'])}，"
        f"金狗 {fmt_pct(row['gold_rate_pct'])}，1h均值 {fmt_pct(row['avg_return_1h_pct'])}，"
        f"最大 {fmt_pct(row['max_return_pct'])}"
    )


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    window = payload.get("history_window") or {}
    missed_gold = payload.get("missed_gold") or {}
    audit = payload.get("gold_pick_audit") or {}
    strategy = payload.get("strategy_optimization") or {}
    primary_rule = strategy.get("primary_rule") or {}
    lines = [
        "# 金狗回测报告",
        "",
        (
            f"样本：跟踪 {summary.get('tracked_count', 0)}，可行动 {summary.get('actionable_count', 0)}，"
            f"命中率 {fmt_pct(summary.get('hit_rate_pct'))}。"
        ),
        (
            f"金狗标准：发现后峰值至少 10x（涨幅 {fmt_pct(audit.get('gold_return_pct', GOLD_RETURN_PCT))}），"
            f"且峰值市值至少 {short_money(audit.get('gold_min_peak_mcap', GOLD_MIN_PEAK_MCAP))}。"
        ),
        (
            f"抓到金狗：{audit.get('caught_gold_count', 0)} / {audit.get('total_gold_count', 0)}，"
            f"捕获率 {fmt_pct(audit.get('gold_capture_rate_pct'))}；"
            f"抓错主推：{audit.get('wrong_gold_pick_count', 0)}，"
            f"主推精度 {fmt_pct(audit.get('gold_pick_precision_pct'))}。"
        ),
        (
            f"10x待确认：{audit.get('ten_x_mcap_unknown_count', 0)} 个缺峰值市值；"
            f"{audit.get('ten_x_below_mcap_count', 0)} 个峰值市值未到 {short_money(audit.get('gold_min_peak_mcap', GOLD_MIN_PEAK_MCAP))}。"
        ),
        f"历史覆盖：{window.get('coverage_days', 0)} 天 / 目标 {window.get('target_days', 365)} 天，{window.get('status', '')}。",
        "",
        "## 最强画像",
    ]
    best = payload.get("best_edges") or []
    lines.extend(render_edge(row) for row in best[:10])
    if not best:
        lines.append("- 样本不足，先继续收集。")
    lines.extend(["", "## 最弱画像"])
    weak = payload.get("weak_edges") or []
    lines.extend(render_edge(row) for row in weak[:10])
    if not weak:
        lines.append("- 样本不足，先继续收集。")
    lines.extend(["", "## 下一轮调参"])
    rules = payload.get("next_rules") or []
    lines.extend(f"- {rule['action']} `{rule['name']}`：{rule['reason']}" for rule in rules)
    if not rules:
        lines.append("- 暂时不调权重，先扩大样本。")
    lines.extend(["", "## 策略优化"])
    if primary_rule:
        lines.append(
            f"- 主推规则 `{primary_rule.get('name')}`：样本 {primary_rule.get('count', 0)}，"
            f"金狗率 {fmt_pct(primary_rule.get('gold_rate_pct'))}，"
            f"金狗捕获 {fmt_pct(primary_rule.get('gold_capture_rate_pct'))}，"
            f"误报 {fmt_pct(primary_rule.get('false_positive_rate_pct'))}。"
        )
    for item in strategy.get("recommendations") or []:
        lines.append(f"- {item}")
    if not primary_rule and not (strategy.get("recommendations") or []):
        lines.append("- 暂时没有足够样本决定主推规则。")
    lines.extend(["", "## 漏网金狗"])
    if missed_gold.get("count"):
        for row in (missed_gold.get("examples") or [])[:10]:
            lines.append(
                f"- `{row.get('symbol') or '--'}`：涨幅 {fmt_pct(row.get('return_since_first_pct'))}，"
                f"峰值市值 {short_money(row.get('peak_mcap'))}，"
                f"漏掉原因：{row.get('miss_reason') or '--'}"
            )
    else:
        lines.append("- 当前样本里没有发现“10x 且峰值市值超过 $1M 但没进主推”的漏网金狗。")
    lines.extend(["", "## 自动阈值搜索"])
    candidate_rules = payload.get("candidate_rules") or []
    for rule in candidate_rules[:20]:
        lines.append(
            f"- `{rule['name']}`：样本 {rule['count']}，命中 {fmt_pct(rule['hit_rate_pct'])}，"
            f"runner {fmt_pct(rule['runner_rate_pct'])}，金狗 {fmt_pct(rule['gold_rate_pct'])}，"
            f"捕获 {fmt_pct(rule.get('gold_capture_rate_pct'))}，误报 {fmt_pct(rule.get('false_positive_rate_pct'))}，"
            f"1h均值 {fmt_pct(rule['avg_return_1h_pct'])}，规则分 {to_float(rule.get('rule_score')):.2f}"
        )
    if not candidate_rules:
        lines.append("- 样本不足，暂时没有可用阈值组合。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build local gold-dog backtest report.")
    parser.add_argument("--out-dir", default="outputs")
    parser.add_argument("--min-count", type=int, default=3)
    parser.add_argument("--target-days", type=int, default=365)
    parser.add_argument("--history-import", action="append", default=[])
    parser.add_argument("--melt-data-dir", action="append", default=[])
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    history = load_backtest_history(out_dir, args.history_import, args.melt_data_dir)
    if not (history.get("rows") or {}):
        raise SystemExit(f"missing replay history: {out_dir / 'alpha-radar-replay-history.json'}")
    payload = build_backtest_payload(history, min_count=args.min_count, target_days=args.target_days)
    (out_dir / "alpha-radar-backtest-latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "alpha-radar-backtest-latest.md").write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({"ok": True, "json": str(out_dir / "alpha-radar-backtest-latest.json"), "md": str(out_dir / "alpha-radar-backtest-latest.md")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
