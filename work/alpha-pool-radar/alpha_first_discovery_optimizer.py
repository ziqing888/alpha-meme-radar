from __future__ import annotations

import argparse
import itertools
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from alpha_first_discovery_backtest import TradeModel, simulate_ordered_price_trade


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPLAY_HISTORY = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-first-discovery-optimization.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-first-discovery-optimization.md"

CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class StrategyParams:
    name: str
    mcap_min: float
    mcap_max: float
    min_signal_score: float
    min_liquidity: float
    min_volume24h: float
    min_smart_money: float
    min_kol: float
    max_top10_pct: float
    min_h1_change_pct: float
    max_h1_change_pct: float
    min_m5_change_pct: float
    max_m5_change_pct: float
    max_pair_age_hours: float
    max_trades_per_day: int
    stop_pct: float
    tp1_pct: float
    tp2_pct: float


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "").replace("%", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    number = to_float(value)
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"
    if abs(number) >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.2f}"


def pct(value: Any) -> str:
    return f"{to_float(value) * 100:.1f}%"


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def signal_score(snapshot: dict[str, Any]) -> float:
    base = (
        to_float(snapshot.get("score")) * 0.24
        + to_float(snapshot.get("entry_score")) * 0.22
        + to_float(snapshot.get("gold_dog_score")) * 0.16
        + to_float(snapshot.get("gold_dog_conviction_score")) * 0.16
        + to_float(snapshot.get("backtest_rule_score")) * 0.12
        + to_float(snapshot.get("security_filter_score")) * 0.05
        + to_float(snapshot.get("holder_quality_score")) * 0.05
    )
    sources = snapshot.get("source_labels") or snapshot.get("sources") or []
    if isinstance(sources, list) and len(sources) >= 2:
        base += 4
    if to_float(snapshot.get("smart_money")) >= 30:
        base += 3
    if to_float(snapshot.get("kol")) >= 8:
        base += 2
    return max(0.0, min(100.0, base))


def prepared_rows(replay_history_path: Path) -> list[dict[str, Any]]:
    history = load_json(replay_history_path)
    rows: list[dict[str, Any]] = []
    for item in (history.get("rows") or {}).values():
        if not isinstance(item, dict):
            continue
        if str(item.get("chain") or "").lower() not in {"bsc", "bnb", "bsc-mainnet"}:
            continue
        first_seen_at = parse_iso(item.get("first_seen_at"))
        snapshot = item.get("first_snapshot") if isinstance(item.get("first_snapshot"), dict) else {}
        observations = item.get("observations") if isinstance(item.get("observations"), list) else []
        first_price = to_float(item.get("first_price_usd") or snapshot.get("price_usd"))
        first_mcap = to_float(snapshot.get("mcap") or snapshot.get("market_cap"))
        if not first_seen_at or not snapshot or first_price <= 0 or first_mcap <= 0 or not observations:
            continue
        rows.append(
            {
                "key": item.get("key") or "",
                "symbol": item.get("symbol") or "",
                "first_seen_at": first_seen_at.isoformat(timespec="seconds"),
                "day": first_seen_at.date().isoformat(),
                "first_price_usd": first_price,
                "first_mcap": first_mcap,
                "liquidity": to_float(snapshot.get("liquidity")),
                "volume24h": to_float(snapshot.get("volume24h") or snapshot.get("dex_volume24h")),
                "smart_money": to_float(snapshot.get("smart_money")),
                "kol": to_float(snapshot.get("kol")),
                "top10_holder_pct": to_float(snapshot.get("top10_holder_pct"), 999.0),
                "change_h1": to_float(snapshot.get("change_h1")),
                "change_m5": to_float(snapshot.get("change_m5")),
                "pair_age_hours": to_float(snapshot.get("pair_age_hours")),
                "signal_score": signal_score(snapshot),
                "recommendation_bucket": item.get("recommendation_bucket") or snapshot.get("recommendation_bucket") or "",
                "observations": observations,
            }
        )
    rows.sort(key=lambda row: row["first_seen_at"])
    return rows


def trade_model_for(params: StrategyParams) -> TradeModel:
    return TradeModel(
        name=f"tp{params.tp1_pct:g}_tp{params.tp2_pct:g}_stop{params.stop_pct:g}",
        tp1_multiple=1.0 + params.tp1_pct / 100.0,
        tp1_fraction=0.50,
        tp2_multiple=1.0 + params.tp2_pct / 100.0,
        tp2_fraction=0.30,
        stop_multiple=1.0 - params.stop_pct / 100.0,
        runner_floor_after_tp2=1.0 + params.tp1_pct / 100.0,
        runner_trail_drawdown_pct=0.35,
    )


def passes(row: dict[str, Any], params: StrategyParams) -> bool:
    return (
        params.mcap_min <= row["first_mcap"] <= params.mcap_max
        and row["signal_score"] >= params.min_signal_score
        and row["liquidity"] >= params.min_liquidity
        and row["volume24h"] >= params.min_volume24h
        and row["smart_money"] >= params.min_smart_money
        and row["kol"] >= params.min_kol
        and row["top10_holder_pct"] <= params.max_top10_pct
        and params.min_h1_change_pct <= row["change_h1"] <= params.max_h1_change_pct
        and params.min_m5_change_pct <= row["change_m5"] <= params.max_m5_change_pct
        and row["pair_age_hours"] <= params.max_pair_age_hours
    )


def pick_daily(rows: list[dict[str, Any]], params: StrategyParams) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if passes(row, params):
            by_day[row["day"]].append(row)

    selected: list[dict[str, Any]] = []
    for day in sorted(by_day):
        ranked = sorted(
            by_day[day],
            key=lambda row: (
                -row["signal_score"],
                abs(row["first_mcap"] - 65_000),
                row["first_seen_at"],
            ),
        )
        selected.extend(ranked[: params.max_trades_per_day])
    selected.sort(key=lambda row: row["first_seen_at"])
    return selected


def summarize_returns(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {
            "count": 0,
            "win_rate": 0.0,
            "average_return_pct": 0.0,
            "median_return_pct": 0.0,
            "total_return_units": 0.0,
            "max_drawdown_units": 0.0,
            "profit_factor": 0.0,
        }
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for ret in returns:
        equity += ret
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "count": len(returns),
        "win_rate": len(wins) / len(returns),
        "average_return_pct": sum(returns) / len(returns),
        "median_return_pct": median(returns),
        "total_return_units": sum(returns),
        "max_drawdown_units": abs(max_dd),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else 999.0,
    }


def evaluate(rows: list[dict[str, Any]], params: StrategyParams) -> dict[str, Any]:
    selected = pick_daily(rows, params)
    model = trade_model_for(params)
    trades: list[dict[str, Any]] = []
    for row in selected:
        sim = simulate_ordered_price_trade(row["first_price_usd"], row["observations"], model)
        trades.append(
            {
                "symbol": row["symbol"],
                "key": row["key"],
                "first_seen_at": row["first_seen_at"],
                "first_mcap": row["first_mcap"],
                "signal_score": row["signal_score"],
                "return_pct": sim["return_pct"],
                "peak_multiple": sim.get("peak_multiple", 0.0),
                "path": sim.get("path", ""),
            }
        )
    returns = [to_float(trade["return_pct"]) for trade in trades]
    summary = summarize_returns(returns)
    score = (
        summary["average_return_pct"] * 100
        + summary["win_rate"] * 35
        + min(summary["count"], 20) * 0.8
        + min(summary["profit_factor"], 8) * 3
        - summary["max_drawdown_units"] * 12
    )
    if summary["count"] < 4:
        score -= 100
    return {
        "params": asdict(params),
        "score": round(score, 4),
        "summary": summary,
        "trades": trades,
    }


def split_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    days = sorted({row["day"] for row in rows})
    split_index = max(1, int(len(days) * 0.67))
    train_days = set(days[:split_index])
    return [row for row in rows if row["day"] in train_days], [row for row in rows if row["day"] not in train_days]


def param_grid() -> list[StrategyParams]:
    grid: list[StrategyParams] = []
    ranges = [
        (10_000, 100_000),
        (20_000, 100_000),
        (30_000, 100_000),
        (30_000, 150_000),
        (30_000, 300_000),
        (50_000, 150_000),
    ]
    h1_ranges = [(-40, 80), (-40, 140), (-20, 140), (-20, 220)]
    m5_ranges = [(-25, 25), (-15, 25), (-15, 45)]
    exit_sets = [(16, 35, 100), (18, 45, 100), (22, 45, 120)]
    for idx, values in enumerate(
        itertools.product(
            ranges,
            [78, 84, 90],
            [0, 8_000, 20_000],
            [0, 40_000, 120_000],
            [0, 30],
            [0, 8],
            [25, 32],
            h1_ranges,
            m5_ranges,
            [6, 12, 24],
            [999],
            exit_sets,
        )
    ):
        (
            mcap_range,
            min_score,
            min_liq,
            min_vol,
            min_smart,
            min_kol,
            max_top10,
            h1_range,
            m5_range,
            max_age,
            max_per_day,
            exit_set,
        ) = values
        stop, tp1, tp2 = exit_set
        grid.append(
            StrategyParams(
                name=f"grid_{idx}",
                mcap_min=mcap_range[0],
                mcap_max=mcap_range[1],
                min_signal_score=min_score,
                min_liquidity=min_liq,
                min_volume24h=min_vol,
                min_smart_money=min_smart,
                min_kol=min_kol,
                max_top10_pct=max_top10,
                min_h1_change_pct=h1_range[0],
                max_h1_change_pct=h1_range[1],
                min_m5_change_pct=m5_range[0],
                max_m5_change_pct=m5_range[1],
                max_pair_age_hours=max_age,
                max_trades_per_day=max_per_day,
                stop_pct=stop,
                tp1_pct=tp1,
                tp2_pct=tp2,
            )
        )
    return grid


def optimize(replay_history_path: Path, top_n: int) -> dict[str, Any]:
    rows = prepared_rows(replay_history_path)
    train_rows, test_rows = split_rows(rows)
    candidates: list[dict[str, Any]] = []
    for params in param_grid():
        train = evaluate(train_rows, params)
        if train["summary"]["count"] < 8:
            continue
        test = evaluate(test_rows, params)
        if test["summary"]["count"] < 1:
            continue
        robustness = (
            train["summary"]["average_return_pct"] > 0
            and test["summary"]["average_return_pct"] > -0.05
            and train["summary"]["profit_factor"] >= 1.15
        )
        combined_score = train["score"] + test["score"] * 0.65 + (20 if robustness else -20)
        candidates.append(
            {
                "combined_score": round(combined_score, 4),
                "robustness_pass": robustness,
                "train": {k: v for k, v in train.items() if k != "trades"},
                "test": {k: v for k, v in test.items() if k != "trades"},
                "all": evaluate(rows, params),
            }
        )
    candidates.sort(
        key=lambda result: (
            not result["robustness_pass"],
            -result["combined_score"],
            -result["test"]["summary"]["count"],
        )
    )
    return {
        "generated_at": now_iso(),
        "source": str(replay_history_path),
        "universe": {
            "prepared_rows": len(rows),
            "train_rows": len(train_rows),
            "test_rows": len(test_rows),
            "grid_size": len(param_grid()),
            "evaluated": len(candidates),
        },
        "top_results": candidates[:top_n],
    }


def md_report(result: dict[str, Any]) -> str:
    lines = [
        "# Alpha First Discovery Optimization",
        "",
        f"- Generated: {result['generated_at']}",
        f"- Source: `{result['source']}`",
        f"- Prepared rows: {result['universe']['prepared_rows']}",
        f"- Grid size: {result['universe']['grid_size']}; evaluated: {result['universe']['evaluated']}",
        "",
        "| Rank | Robust | All trades | All win | All avg | All PF | Test trades | Test win | Test avg | Mcap | Score | Liq | Vol | Smart/KOL | H1 | M5 | Age | Stop/TP |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | --- | --- | ---: | --- |",
    ]
    for index, item in enumerate(result["top_results"], start=1):
        params = item["all"]["params"]
        all_summary = item["all"]["summary"]
        test_summary = item["test"]["summary"]
        lines.append(
            f"| {index} | {'yes' if item['robustness_pass'] else 'no'} "
            f"| {all_summary['count']} | {pct(all_summary['win_rate'])} | {pct(all_summary['average_return_pct'])} | {all_summary['profit_factor']:.2f} "
            f"| {test_summary['count']} | {pct(test_summary['win_rate'])} | {pct(test_summary['average_return_pct'])} "
            f"| {money(params['mcap_min'])}-{money(params['mcap_max'])} | {params['min_signal_score']:.0f} "
            f"| {money(params['min_liquidity'])} | {money(params['min_volume24h'])} "
            f"| {params['min_smart_money']:.0f}/{params['min_kol']:.0f} "
            f"| {params['min_h1_change_pct']:.0f}..{params['max_h1_change_pct']:.0f} "
            f"| {params['min_m5_change_pct']:.0f}..{params['max_m5_change_pct']:.0f} "
            f"| {params['max_pair_age_hours']:.0f}h "
            f"| -{params['stop_pct']:.0f}%/+{params['tp1_pct']:.0f}%/+{params['tp2_pct']:.0f}% |"
        )
    if result["top_results"]:
        best = result["top_results"][0]["all"]
        lines.extend(["", "## Best Trades", "", "| Symbol | First seen | Mcap | Score | Return | Peak | Path |", "| --- | --- | ---: | ---: | ---: | ---: | --- |"])
        for trade in sorted(best["trades"], key=lambda row: row["return_pct"], reverse=True)[:20]:
            lines.append(
                f"| {trade['symbol']} | {trade['first_seen_at']} | {money(trade['first_mcap'])} "
                f"| {trade['signal_score']:.1f} | {pct(trade['return_pct'])} | {trade['peak_multiple']:.2f}x | {trade['path']} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid-search first-discovery paper strategy against scan replay history.")
    parser.add_argument("--replay-history", type=Path, default=DEFAULT_REPLAY_HISTORY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--top-n", type=int, default=30)
    args = parser.parse_args()

    result = optimize(args.replay_history, top_n=args.top_n)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_out.write_text(md_report(result), encoding="utf-8")
    best = result["top_results"][0] if result["top_results"] else None
    if best:
        all_summary = best["all"]["summary"]
        params = best["all"]["params"]
        print(
            "best "
            f"trades={all_summary['count']} win={pct(all_summary['win_rate'])} "
            f"avg={pct(all_summary['average_return_pct'])} pf={all_summary['profit_factor']:.2f} "
            f"mcap={money(params['mcap_min'])}-{money(params['mcap_max'])} "
            f"score>={params['min_signal_score']}"
        )
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
