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

from alpha_first_discovery_optimizer import money, parse_iso, prepared_rows, to_float


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPLAY_HISTORY = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-first-discovery-net-optimization.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-first-discovery-net-optimization.md"

CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class NetParams:
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
    tp_pct: float
    tp_sell_fraction: float
    trail_drawdown_pct: float
    time_stop_minutes: float
    min_hold_minutes: float
    cost_pct: float
    gas_usd_per_swap: float
    position_usd: float


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def pct(value: Any) -> str:
    return f"{to_float(value) * 100:.1f}%"


def observation_points(row: dict[str, Any]) -> list[tuple[datetime | None, float]]:
    cached = row.get("_observation_points")
    if isinstance(cached, list):
        return cached
    points: list[tuple[datetime | None, float]] = []
    first_price = to_float(row.get("first_price_usd"))
    if first_price <= 0:
        return points
    for obs in row.get("observations") or []:
        if not isinstance(obs, dict):
            continue
        price = to_float(obs.get("price_usd"))
        if price <= 0:
            continue
        points.append((parse_iso(obs.get("seen_at")), price / first_price))
    points.sort(key=lambda point: point[0] or datetime.max.replace(tzinfo=CN_TZ))
    return points


def passes(row: dict[str, Any], params: NetParams) -> bool:
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


def pick_rows(rows: list[dict[str, Any]], params: NetParams) -> list[dict[str, Any]]:
    if params.max_trades_per_day >= 999:
        return [row for row in rows if passes(row, params)]

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
                abs(row["first_mcap"] - 55_000),
                row["first_seen_at"],
            ),
        )
        selected.extend(ranked[: params.max_trades_per_day])
    return sorted(selected, key=lambda row: row["first_seen_at"])


def elapsed_minutes(start: datetime | None, current: datetime | None) -> float:
    if not start or not current:
        return 0.0
    return max(0.0, (current - start).total_seconds() / 60.0)


def cost_return(params: NetParams, sell_count: int) -> float:
    swaps = 1 + sell_count
    return params.cost_pct + swaps * params.gas_usd_per_swap / params.position_usd


def simulate_trade(row: dict[str, Any], params: NetParams) -> dict[str, Any]:
    points = observation_points(row)
    start = parse_iso(row.get("first_seen_at"))
    if not points:
        return {
            "gross_return_pct": 0.0,
            "net_return_pct": -cost_return(params, sell_count=1),
            "peak_multiple": 1.0,
            "path": "no_followup",
            "hold_minutes": 0.0,
            "sell_count": 1,
        }

    stop_multiple = 1.0 - params.stop_pct / 100.0
    tp_multiple = 1.0 + params.tp_pct / 100.0
    trail_fraction = params.trail_drawdown_pct / 100.0
    tp_fraction = params.tp_sell_fraction
    remaining = 1.0
    realized = 0.0
    sell_count = 0
    peak = 1.0
    tp_hit = False
    path: list[str] = []
    exit_time = points[-1][0]

    last_seen_before_timeout: tuple[datetime | None, float] = (start, 1.0)
    timed_out = False
    for ts, multiple in points:
        minutes = elapsed_minutes(start, ts)
        if params.time_stop_minutes > 0 and minutes > params.time_stop_minutes:
            timed_out = True
            break
        last_seen_before_timeout = (ts, multiple)
        peak = max(peak, multiple)

        if not tp_hit and minutes >= params.min_hold_minutes and multiple <= stop_multiple:
            realized += remaining * stop_multiple
            remaining = 0.0
            sell_count += 1
            path.append("stop")
            exit_time = ts
            break

        if not tp_hit and multiple >= tp_multiple:
            sold = min(remaining, tp_fraction)
            realized += sold * tp_multiple
            remaining -= sold
            sell_count += 1
            tp_hit = True
            path.append("tp")
            if remaining <= 0:
                exit_time = ts
                break

        if tp_hit and remaining > 0 and trail_fraction > 0:
            trail_multiple = peak * (1.0 - trail_fraction)
            if multiple <= max(trail_multiple, 1.0):
                realized += remaining * multiple
                remaining = 0.0
                sell_count += 1
                path.append("trail")
                exit_time = ts
                break

        if params.time_stop_minutes > 0 and minutes >= params.time_stop_minutes:
            realized += remaining * multiple
            remaining = 0.0
            sell_count += 1
            path.append("time")
            exit_time = ts
            break

    if remaining > 0:
        final_ts, final_multiple = last_seen_before_timeout if timed_out else points[-1]
        exit_time = final_ts
        if timed_out:
            path.append("time")
        else:
            path.append("mark")
        realized += remaining * final_multiple
        sell_count += 1

    gross = realized - 1.0
    net = gross - cost_return(params, sell_count=sell_count)
    return {
        "gross_return_pct": gross,
        "net_return_pct": net,
        "peak_multiple": peak,
        "path": "_".join(path),
        "hold_minutes": elapsed_minutes(start, exit_time),
        "sell_count": sell_count,
    }


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "net_win_rate": 0.0,
            "avg_net_return_pct": 0.0,
            "median_net_return_pct": 0.0,
            "total_net_return_units": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_units": 0.0,
            "median_hold_minutes": 0.0,
            "tp_rate": 0.0,
            "stop_rate": 0.0,
        }
    returns = [to_float(trade["net_return_pct"]) for trade in trades]
    wins = [ret for ret in returns if ret > 0]
    losses = [ret for ret in returns if ret < 0]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for ret in returns:
        equity += ret
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "count": len(trades),
        "net_win_rate": len(wins) / len(trades),
        "avg_net_return_pct": sum(returns) / len(returns),
        "median_net_return_pct": median(returns),
        "total_net_return_units": sum(returns),
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else 999.0,
        "max_drawdown_units": abs(max_dd),
        "median_hold_minutes": median([to_float(trade["hold_minutes"]) for trade in trades]),
        "tp_rate": sum(1 for trade in trades if str(trade["path"]).startswith("tp")) / len(trades),
        "stop_rate": sum(1 for trade in trades if "stop" in str(trade["path"])) / len(trades),
    }


def evaluate(rows: list[dict[str, Any]], params: NetParams) -> dict[str, Any]:
    selected = pick_rows(rows, params)
    trades: list[dict[str, Any]] = []
    for row in selected:
        sim = simulate_trade(row, params)
        trades.append(
            {
                "symbol": row["symbol"],
                "key": row["key"],
                "first_seen_at": row["first_seen_at"],
                "first_mcap": row["first_mcap"],
                "signal_score": row["signal_score"],
                **sim,
            }
        )
    summary = summarize(trades)
    score = (
        summary["avg_net_return_pct"] * 120
        + summary["net_win_rate"] * 18
        + min(summary["profit_factor"], 6) * 3
        + min(summary["count"], 80) * 0.25
        - summary["max_drawdown_units"] * 20
    )
    if summary["count"] < 15:
        score -= 100
    return {"params": asdict(params), "score": round(score, 4), "summary": summary, "trades": trades}


def split_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    days = sorted({row["day"] for row in rows})
    if len(days) <= 1:
        return rows, []
    split_index = max(1, int(len(days) * 0.67))
    train_days = set(days[:split_index])
    return [row for row in rows if row["day"] in train_days], [row for row in rows if row["day"] not in train_days]


def param_grid(cost_pct: float, gas_usd_per_swap: float, position_usd: float) -> list[NetParams]:
    grid: list[NetParams] = []
    ranges = [(5_000, 100_000), (10_000, 100_000), (30_000, 100_000)]
    h1_ranges = [(-80, 220), (-40, 140), (-20, 80)]
    m5_ranges = [(-50, 80), (-25, 45), (-15, 25)]
    exits = [
        (10, 20, 1.0, 0, 10, 0),
        (12, 30, 1.0, 0, 15, 0),
        (12, 50, 1.0, 0, 20, 0),
        (12, 80, 1.0, 0, 45, 0),
        (16, 50, 0.8, 30, 45, 0),
        (18, 80, 0.8, 35, 60, 0),
        (22, 100, 0.8, 35, 90, 0),
    ]
    for idx, values in enumerate(
        itertools.product(
            ranges,
            [0, 70, 78, 84],
            [0, 8_000],
            [0],
            [0, 20, 30],
            [0],
            [25, 32, 999],
            h1_ranges,
            m5_ranges,
            [6, 12, 24, 999],
            [999],
            exits,
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
        stop, tp, sell_fraction, trail, time_stop, min_hold = exit_set
        grid.append(
            NetParams(
                name=f"net_grid_{idx}",
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
                tp_pct=tp,
                tp_sell_fraction=sell_fraction,
                trail_drawdown_pct=trail,
                time_stop_minutes=time_stop,
                min_hold_minutes=min_hold,
                cost_pct=cost_pct,
                gas_usd_per_swap=gas_usd_per_swap,
                position_usd=position_usd,
            )
        )
    return grid


def optimize(
    replay_history_path: Path,
    top_n: int,
    cost_pct: float,
    gas_usd_per_swap: float,
    position_usd: float,
) -> dict[str, Any]:
    rows = prepared_rows(replay_history_path)
    for row in rows:
        row["_observation_points"] = observation_points(row)
    train_rows, test_rows = split_rows(rows)
    results: list[dict[str, Any]] = []
    grid = param_grid(cost_pct=cost_pct, gas_usd_per_swap=gas_usd_per_swap, position_usd=position_usd)
    for params in grid:
        train = evaluate(train_rows, params)
        if train["summary"]["count"] < 12:
            continue
        all_result = evaluate(rows, params)
        if all_result["summary"]["count"] < 20:
            continue
        test = evaluate(test_rows, params)
        robustness = (
            all_result["summary"]["avg_net_return_pct"] > 0
            and all_result["summary"]["profit_factor"] >= 1.15
            and train["summary"]["avg_net_return_pct"] > -0.03
            and (test["summary"]["count"] == 0 or test["summary"]["avg_net_return_pct"] > -0.08)
        )
        combined = all_result["score"] + train["score"] * 0.35 + test["score"] * 0.25 + (30 if robustness else -40)
        results.append(
            {
                "combined_score": round(combined, 4),
                "robustness_pass": robustness,
                "train": {k: v for k, v in train.items() if k != "trades"},
                "test": {k: v for k, v in test.items() if k != "trades"},
                "all": all_result,
            }
        )
    results.sort(
        key=lambda item: (
            not item["robustness_pass"],
            -item["combined_score"],
            -item["all"]["summary"]["count"],
        )
    )
    return {
        "generated_at": now_iso(),
        "source": str(replay_history_path),
        "assumptions": {
            "cost_pct": cost_pct,
            "gas_usd_per_swap": gas_usd_per_swap,
            "position_usd": position_usd,
        },
        "universe": {
            "prepared_rows": len(rows),
            "train_rows": len(train_rows),
            "test_rows": len(test_rows),
            "grid_size": len(grid),
            "evaluated": len(results),
        },
        "top_results": results[:top_n],
    }


def md_report(result: dict[str, Any]) -> str:
    lines = [
        "# Alpha First Discovery Net Optimization",
        "",
        f"- Generated: {result['generated_at']}",
        f"- Source: `{result['source']}`",
        f"- Prepared rows: {result['universe']['prepared_rows']}",
        f"- Grid size: {result['universe']['grid_size']}; evaluated: {result['universe']['evaluated']}",
        f"- Cost: {pct(result['assumptions']['cost_pct'])} + ${result['assumptions']['gas_usd_per_swap']:.2f}/swap on ${result['assumptions']['position_usd']:.2f} position",
        "",
        "| Rank | Robust | Trades | Net win | Avg net | Total net | PF | DD | Hold | TP/Stop | Mcap | Score | Liq | Vol | Smart/KOL | Top10 | H1 | M5 | Age | Exit |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- | ---: | --- |",
    ]
    for index, item in enumerate(result["top_results"], start=1):
        params = item["all"]["params"]
        summary = item["all"]["summary"]
        lines.append(
            f"| {index} | {'yes' if item['robustness_pass'] else 'no'} | {summary['count']} "
            f"| {pct(summary['net_win_rate'])} | {pct(summary['avg_net_return_pct'])} | {pct(summary['total_net_return_units'])} "
            f"| {summary['profit_factor']:.2f} | {pct(summary['max_drawdown_units'])} | {summary['median_hold_minutes']:.0f}m "
            f"| {pct(summary['tp_rate'])}/{pct(summary['stop_rate'])} "
            f"| {money(params['mcap_min'])}-{money(params['mcap_max'])} | {params['min_signal_score']:.0f} "
            f"| {money(params['min_liquidity'])} | {money(params['min_volume24h'])} "
            f"| {params['min_smart_money']:.0f}/{params['min_kol']:.0f} | {params['max_top10_pct']:.0f} "
            f"| {params['min_h1_change_pct']:.0f}..{params['max_h1_change_pct']:.0f} "
            f"| {params['min_m5_change_pct']:.0f}..{params['max_m5_change_pct']:.0f} "
            f"| {params['max_pair_age_hours']:.0f}h "
            f"| SL{params['stop_pct']:.0f}/TP{params['tp_pct']:.0f}/sell{params['tp_sell_fraction']:.1f}/time{params['time_stop_minutes']:.0f} |"
        )
    if result["top_results"]:
        best = result["top_results"][0]["all"]
        lines.extend(["", "## Best Result Trades", "", "| Symbol | First seen | Mcap | Score | Net | Gross | Peak | Hold | Path |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])
        for trade in sorted(best["trades"], key=lambda row: row["net_return_pct"], reverse=True)[:30]:
            lines.append(
                f"| {trade['symbol']} | {trade['first_seen_at']} | {money(trade['first_mcap'])} "
                f"| {trade['signal_score']:.1f} | {pct(trade['net_return_pct'])} | {pct(trade['gross_return_pct'])} "
                f"| {trade['peak_multiple']:.2f}x | {trade['hold_minutes']:.0f}m | {trade['path']} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Optimize first-discovery entries for net profitability.")
    parser.add_argument("--replay-history", type=Path, default=DEFAULT_REPLAY_HISTORY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--cost-pct", type=float, default=0.05)
    parser.add_argument("--gas-usd-per-swap", type=float, default=0.12)
    parser.add_argument("--position-usd", type=float, default=35.0)
    args = parser.parse_args()

    result = optimize(
        args.replay_history,
        top_n=args.top_n,
        cost_pct=args.cost_pct,
        gas_usd_per_swap=args.gas_usd_per_swap,
        position_usd=args.position_usd,
    )
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_out.write_text(md_report(result), encoding="utf-8")
    best = result["top_results"][0] if result["top_results"] else None
    if best:
        params = best["all"]["params"]
        summary = best["all"]["summary"]
        print(
            "best "
            f"trades={summary['count']} win={pct(summary['net_win_rate'])} "
            f"avg_net={pct(summary['avg_net_return_pct'])} pf={summary['profit_factor']:.2f} "
            f"mcap={money(params['mcap_min'])}-{money(params['mcap_max'])} "
            f"exit=SL{params['stop_pct']:.0f}/TP{params['tp_pct']:.0f}/time{params['time_stop_minutes']:.0f}"
        )
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
