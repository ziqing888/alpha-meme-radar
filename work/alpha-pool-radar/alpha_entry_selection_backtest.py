"""Find entry segments that survive chronological and cost-stressed replay.

This script is research-only. It reads immutable first snapshots and never
starts or changes a trading worker.
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alpha_first_discovery_paper import to_float
from alpha_long_tail_exit_backtest import (
    DEFAULT_FAST_QUOTES,
    DEFAULT_REPLAY,
    LONG_TAIL_EXIT,
    _simulate_entry,
    augment_entries_with_fast_quotes,
    select_replay_entries,
    summarize_results,
)


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-entry-selection-backtest.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-entry-selection-backtest.md"
CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Selector:
    chain: str
    mcap_min: float
    mcap_max: float
    score_min: float
    score_max: float
    liquidity_min: float
    source_count_min: float
    h1_min: float
    h1_max: float
    m5_min: float
    m5_max: float


def _matches(row: dict[str, Any], selector: Selector) -> bool:
    return (
        (selector.chain == "all" or str(row.get("chain")) == selector.chain)
        and selector.mcap_min <= to_float(row.get("first_mcap_usd")) < selector.mcap_max
        and selector.score_min <= to_float(row.get("score")) < selector.score_max
        and to_float(row.get("liquidity_usd")) >= selector.liquidity_min
        and to_float(row.get("source_count")) >= selector.source_count_min
        and selector.h1_min <= to_float(row.get("change_h1_pct")) <= selector.h1_max
        and selector.m5_min <= to_float(row.get("change_m5_pct")) <= selector.m5_max
    )


def _split_days(rows: list[dict[str, Any]], ratio: float = 0.67) -> tuple[set[str], set[str]]:
    days = sorted({str(row.get("entry_at") or "")[:10] for row in rows if row.get("entry_at")})
    if len(days) <= 1:
        return set(days), set()
    split = min(len(days) - 1, max(1, int(len(days) * ratio)))
    return set(days[:split]), set(days[split:])


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize_results(rows)
    observed = sum(to_float(row.get("observation_count")) > 0 for row in rows)
    summary["observed_count"] = observed
    summary["observation_coverage"] = observed / len(rows) if rows else 0.0
    summary["resolved_rate"] = (
        (summary["count"] - summary["unresolved_count"]) / summary["count"] if summary["count"] else 0.0
    )
    return summary


def evaluate_selector(
    rows: list[dict[str, Any]],
    selector: Selector,
    train_days: set[str],
    test_days: set[str],
) -> dict[str, Any]:
    selected = [row for row in rows if _matches(row, selector)]
    train = [row for row in selected if str(row.get("entry_at") or "")[:10] in train_days]
    test = [row for row in selected if str(row.get("entry_at") or "")[:10] in test_days]
    all_summary = _summary(selected)
    train_summary = _summary(train)
    test_summary = _summary(test)
    robust = (
        train_summary["count"] >= 12
        and test_summary["count"] >= 8
        and train_summary["stress_average_return"] > 0
        and test_summary["stress_average_return"] > 0
        and train_summary["stress_profit_factor"] > 1
        and test_summary["stress_profit_factor"] > 1
    )
    weakest_average = min(
        to_float(train_summary.get("stress_average_return")),
        to_float(test_summary.get("stress_average_return")),
    )
    score = (
        weakest_average * 100
        + min(to_float(all_summary.get("stress_profit_factor")), 3.0) * 4
        + min(all_summary["count"], 80) * 0.05
        + all_summary["observation_coverage"] * 2
        - to_float(all_summary.get("stress_max_drawdown_units")) * 0.25
    )
    return {
        "selector": asdict(selector),
        "robustness_pass": robust,
        "selection_score": round(score, 6),
        "all": all_summary,
        "train": train_summary,
        "test": test_summary,
    }


def selector_grid() -> list[Selector]:
    values = itertools.product(
        ("all", "bsc", "robinhood"),
        ((10_000, 300_001), (10_000, 30_000), (30_000, 100_000), (100_000, 300_001)),
        ((0, 101), (0, 60), (60, 70), (70, 80), (80, 101)),
        (8_000, 20_000, 30_000),
        (0, 2),
        ((-20, 80), (-10, 50), (0, 50)),
        ((-25, 45), (-10, 30), (0, 30)),
    )
    return [
        Selector(
            chain=chain,
            mcap_min=mcap[0],
            mcap_max=mcap[1],
            score_min=score[0],
            score_max=score[1],
            liquidity_min=liquidity,
            source_count_min=sources,
            h1_min=h1[0],
            h1_max=h1[1],
            m5_min=m5[0],
            m5_max=m5[1],
        )
        for chain, mcap, score, liquidity, sources, h1, m5 in values
    ]


def build_selection_backtest(
    replay: dict[str, Any],
    *,
    fast_quotes_path: Path | None,
    notional_usd: float,
    roundtrip_cost_pct: float,
    gas_usd_per_swap: float,
    top_n: int,
) -> dict[str, Any]:
    entries, rejected = select_replay_entries(replay)
    quote_stats = None
    if fast_quotes_path is not None:
        entries, quote_stats = augment_entries_with_fast_quotes(
            entries,
            fast_quotes_path,
            max_horizon_minutes=LONG_TAIL_EXIT.max_hold_minutes + LONG_TAIL_EXIT.time_stop_tolerance_minutes,
        )
    rows = [
        _simulate_entry(
            entry,
            LONG_TAIL_EXIT,
            notional_usd=notional_usd,
            roundtrip_cost_pct=roundtrip_cost_pct,
            gas_usd_per_swap=gas_usd_per_swap,
        )
        for entry in entries
    ]
    train_days, test_days = _split_days(rows)
    evaluated = [evaluate_selector(rows, selector, train_days, test_days) for selector in selector_grid()]
    eligible = [item for item in evaluated if item["all"]["count"] >= 20]
    eligible.sort(
        key=lambda item: (
            not item["robustness_pass"],
            -item["selection_score"],
            -item["all"]["count"],
        )
    )
    return {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "mode": "immutable_first_snapshot_chronological_cost_stress",
        "cost_stress": {
            "notional_usd": notional_usd,
            "estimated_roundtrip_cost_pct": roundtrip_cost_pct,
            "estimated_gas_usd_per_swap": gas_usd_per_swap,
        },
        "universe": {
            "eligible_entries": len(rows),
            "train_days": sorted(train_days),
            "test_days": sorted(test_days),
            "grid_size": len(evaluated),
            "minimum_reported_count": 20,
            "rejected_by_reason": dict(rejected.most_common()),
        },
        "quote_augmentation": quote_stats,
        "nonzero_feature_counts": {
            feature: sum(to_float(row.get(feature)) != 0 for row in rows)
            for feature in (
                "liquidity_usd",
                "source_count",
                "smart_money",
                "kol",
                "change_h1_pct",
                "change_m5_pct",
                "top10_holder_pct",
            )
        },
        "baseline": _summary(rows),
        "robust_selector_count": sum(item["robustness_pass"] for item in eligible),
        "top_selectors": eligible[:top_n],
        "entry_results": rows,
        "limitations": [
            "This is a short, scanner-discovered sample rather than the full MEME market.",
            "Scanner prices are not historical executable OKX reverse quotes.",
            "Sparse or truncated observations can miss stop, peak, tax and liquidity-collapse events.",
            "Selector search is exploratory; only chronological holdout survivors are marked robust.",
            "Entry delay and markup cannot be optimized because this replay enters at the stored first snapshot.",
        ],
    }


def _pct(value: Any) -> str:
    return f"{to_float(value) * 100:.1f}%"


def _range(selector: dict[str, Any], prefix: str) -> str:
    return f"{selector[prefix + '_min']:g}..{selector[prefix + '_max']:g}"


def markdown_report(result: dict[str, Any]) -> str:
    universe = result["universe"]
    lines = [
        "# MEME 入场交叉筛选回测",
        "",
        f"- 样本：{universe['eligible_entries']}；训练日：{', '.join(universe['train_days'])}；测试日：{', '.join(universe['test_days'])}",
        f"- 网格：{universe['grid_size']}；训练和测试同时为正：{result['robust_selector_count']}",
        f"- 成本：{result['cost_stress']['notional_usd']:.2f}U/单，往返摩擦 {result['cost_stress']['estimated_roundtrip_cost_pct']:.1f}%，每次 swap Gas {result['cost_stress']['estimated_gas_usd_per_swap']:.2f}U",
        f"- 首次快照非零字段：来源数 {result['nonzero_feature_counts']['source_count']}/{universe['eligible_entries']}；聪明钱 {result['nonzero_feature_counts']['smart_money']}/{universe['eligible_entries']}；KOL {result['nonzero_feature_counts']['kol']}/{universe['eligible_entries']}",
        "",
        "| 排名 | 时序通过 | 链 | 市值 | 分数 | 流动性≥ | 来源≥ | H1 | M5 | 样本 | 全期/训练/测试收益 | PF 全/训/测 | 已退出 | Top1盈利占比 | 保守总收益 |",
        "| ---: | --- | --- | --- | --- | ---: | ---: | --- | --- | ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for index, item in enumerate(result["top_selectors"], start=1):
        selector = item["selector"]
        all_summary, train, test = item["all"], item["train"], item["test"]
        lines.append(
            f"| {index} | {'是' if item['robustness_pass'] else '否'} | {selector['chain']} "
            f"| {selector['mcap_min']/1000:g}K..{selector['mcap_max']/1000:g}K "
            f"| {_range(selector, 'score')} | {selector['liquidity_min']/1000:g}K | {selector['source_count_min']:g} "
            f"| {_range(selector, 'h1')} | {_range(selector, 'm5')} | {all_summary['count']} "
            f"| {_pct(all_summary['stress_average_return'])}/{_pct(train['stress_average_return'])}/{_pct(test['stress_average_return'])} "
            f"| {all_summary['stress_profit_factor']:.2f}/{train['stress_profit_factor']:.2f}/{test['stress_profit_factor']:.2f} "
            f"| {_pct(all_summary['resolved_rate'])} | {_pct(all_summary['top_1pct_profit_share'])} "
            f"| {all_summary['conservative_stress_total_units']:.2f} |"
        )
    lines.extend(["", "## 结论口径", ""])
    if result["robust_selector_count"]:
        lines.append("- 只有标记为‘是’的组合在按末次可见价计值时同时通过训练期和后段测试期；它不是实盘通过标签。")
    else:
        lines.append("- 当前没有任何组合同时通过训练期和后段测试期，不应把单段盈利条件直接切入实盘。")
    lines.extend(f"- {item}" for item in result["limitations"])
    if result["nonzero_feature_counts"]["source_count"] == 0:
        lines.append("- 当前首次快照没有可用来源数字段，因此本轮不能判断多源确认能否提高收益。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--fast-quotes", type=Path, default=DEFAULT_FAST_QUOTES)
    parser.add_argument("--no-fast-quotes", action="store_true")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--notional-usd", type=float, default=5.0)
    parser.add_argument("--roundtrip-cost-pct", type=float, default=5.0)
    parser.add_argument("--gas-usd-per-swap", type=float, default=0.12)
    parser.add_argument("--top-n", type=int, default=30)
    args = parser.parse_args()
    replay = json.loads(args.replay.read_text(encoding="utf-8-sig"))
    result = build_selection_backtest(
        replay,
        fast_quotes_path=None if args.no_fast_quotes else args.fast_quotes,
        notional_usd=args.notional_usd,
        roundtrip_cost_pct=args.roundtrip_cost_pct,
        gas_usd_per_swap=args.gas_usd_per_swap,
        top_n=args.top_n,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_out.write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps({
        "eligible_entries": result["universe"]["eligible_entries"],
        "grid_size": result["universe"]["grid_size"],
        "robust_selector_count": result["robust_selector_count"],
        "json": str(args.json_out),
        "markdown": str(args.md_out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
