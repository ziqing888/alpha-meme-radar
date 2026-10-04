from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from alpha_first_discovery_net_optimizer import (
    NetParams,
    cost_return,
    money,
    observation_points,
    parse_iso,
    passes,
    pct,
    prepared_rows,
    to_float,
)


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPLAY_HISTORY = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-first-discovery-portfolio-optimization.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-first-discovery-portfolio-optimization.md"

CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class PortfolioParams:
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
    stop_pct: float
    tp_pct: float
    tp_sell_fraction: float
    trail_drawdown_pct: float
    time_stop_minutes: float
    cost_pct: float
    gas_usd_per_swap: float
    position_usd: float
    max_open_positions: int


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def to_net_params(params: PortfolioParams) -> NetParams:
    return NetParams(
        name=params.name,
        mcap_min=params.mcap_min,
        mcap_max=params.mcap_max,
        min_signal_score=params.min_signal_score,
        min_liquidity=params.min_liquidity,
        min_volume24h=params.min_volume24h,
        min_smart_money=params.min_smart_money,
        min_kol=params.min_kol,
        max_top10_pct=params.max_top10_pct,
        min_h1_change_pct=params.min_h1_change_pct,
        max_h1_change_pct=params.max_h1_change_pct,
        min_m5_change_pct=params.min_m5_change_pct,
        max_m5_change_pct=params.max_m5_change_pct,
        max_pair_age_hours=params.max_pair_age_hours,
        max_trades_per_day=999,
        stop_pct=params.stop_pct,
        tp_pct=params.tp_pct,
        tp_sell_fraction=params.tp_sell_fraction,
        trail_drawdown_pct=params.trail_drawdown_pct,
        time_stop_minutes=params.time_stop_minutes,
        min_hold_minutes=0.0,
        cost_pct=params.cost_pct,
        gas_usd_per_swap=params.gas_usd_per_swap,
        position_usd=params.position_usd,
    )


def exit_deadline(entry_time: datetime | None, params: PortfolioParams) -> datetime | None:
    if not entry_time:
        return None
    return entry_time + timedelta(minutes=params.time_stop_minutes)


def simulate_trade(row: dict[str, Any], params: PortfolioParams) -> dict[str, Any]:
    net_params = to_net_params(params)
    points = observation_points(row)
    entry_time = parse_iso(row.get("first_seen_at"))
    deadline = exit_deadline(entry_time, params)
    if not entry_time:
        return {
            "gross_return_pct": 0.0,
            "net_return_pct": -cost_return(net_params, sell_count=1),
            "peak_multiple": 1.0,
            "path": "missing_entry_time",
            "entry_time": "",
            "exit_time": "",
            "hold_minutes": 0.0,
            "sell_count": 1,
        }
    if not points:
        exit_time = deadline or entry_time
        return {
            "gross_return_pct": 0.0,
            "net_return_pct": -cost_return(net_params, sell_count=1),
            "peak_multiple": 1.0,
            "path": "no_followup",
            "entry_time": entry_time.isoformat(timespec="seconds"),
            "exit_time": exit_time.isoformat(timespec="seconds"),
            "hold_minutes": params.time_stop_minutes,
            "sell_count": 1,
        }

    stop_multiple = 1.0 - params.stop_pct / 100.0
    tp_multiple = 1.0 + params.tp_pct / 100.0
    trail_fraction = params.trail_drawdown_pct / 100.0
    remaining = 1.0
    realized = 0.0
    sell_count = 0
    peak = 1.0
    tp_hit = False
    path: list[str] = []
    exit_time = deadline or points[-1][0] or entry_time
    last_multiple = 1.0

    for ts, multiple in points:
        if not ts:
            continue
        if deadline and ts > deadline:
            break
        last_multiple = multiple
        peak = max(peak, multiple)

        if not tp_hit and multiple <= stop_multiple:
            realized += remaining * stop_multiple
            remaining = 0.0
            sell_count += 1
            path.append("stop")
            exit_time = ts
            break

        if not tp_hit and multiple >= tp_multiple:
            sold = min(remaining, params.tp_sell_fraction)
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

    if remaining > 0:
        realized += remaining * last_multiple
        sell_count += 1
        path.append("time")

    gross = realized - 1.0
    net = gross - cost_return(net_params, sell_count=sell_count)
    hold_minutes = max(0.0, (exit_time - entry_time).total_seconds() / 60.0)
    return {
        "gross_return_pct": gross,
        "net_return_pct": net,
        "peak_multiple": peak,
        "path": "_".join(path),
        "entry_time": entry_time.isoformat(timespec="seconds"),
        "exit_time": exit_time.isoformat(timespec="seconds"),
        "hold_minutes": hold_minutes,
        "sell_count": sell_count,
    }


def candidate_rows(rows: list[dict[str, Any]], params: PortfolioParams) -> list[dict[str, Any]]:
    net_params = to_net_params(params)
    selected = [row for row in rows if passes(row, net_params)]
    return sorted(selected, key=lambda row: row["first_seen_at"])


def portfolio_trades(rows: list[dict[str, Any]], params: PortfolioParams) -> dict[str, Any]:
    selected = candidate_rows(rows, params)
    open_until: list[datetime] = []
    trades: list[dict[str, Any]] = []
    skipped_capacity = 0
    for row in selected:
        entry = parse_iso(row.get("first_seen_at"))
        if not entry:
            continue
        open_until = [ts for ts in open_until if ts > entry]
        if len(open_until) >= params.max_open_positions:
            skipped_capacity += 1
            continue
        sim = simulate_trade(row, params)
        exit_time = parse_iso(sim.get("exit_time"))
        if exit_time:
            open_until.append(exit_time)
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
    return {"trades": trades, "candidate_count": len(selected), "skipped_capacity": skipped_capacity}


def summarize(trades: list[dict[str, Any]], skipped_capacity: int = 0, candidate_count: int = 0) -> dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "candidate_count": candidate_count,
            "skipped_capacity": skipped_capacity,
            "net_win_rate": 0.0,
            "avg_net_return_pct": 0.0,
            "median_net_return_pct": 0.0,
            "total_net_return_units": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_units": 0.0,
            "median_hold_minutes": 0.0,
            "tp_rate": 0.0,
            "stop_rate": 0.0,
            "capacity_fill_rate": 0.0,
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
        "candidate_count": candidate_count,
        "skipped_capacity": skipped_capacity,
        "net_win_rate": len(wins) / len(trades),
        "avg_net_return_pct": sum(returns) / len(returns),
        "median_net_return_pct": median(returns),
        "total_net_return_units": sum(returns),
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else 999.0,
        "max_drawdown_units": abs(max_dd),
        "median_hold_minutes": median([to_float(trade["hold_minutes"]) for trade in trades]),
        "tp_rate": sum(1 for trade in trades if str(trade["path"]).startswith("tp")) / len(trades),
        "stop_rate": sum(1 for trade in trades if "stop" in str(trade["path"])) / len(trades),
        "capacity_fill_rate": len(trades) / candidate_count if candidate_count else 0.0,
    }


def evaluate(rows: list[dict[str, Any]], params: PortfolioParams) -> dict[str, Any]:
    result = portfolio_trades(rows, params)
    summary = summarize(result["trades"], result["skipped_capacity"], result["candidate_count"])
    score = (
        summary["avg_net_return_pct"] * 110
        + summary["net_win_rate"] * 20
        + min(summary["profit_factor"], 6) * 3
        + min(summary["count"], 80) * 0.25
        - summary["max_drawdown_units"] * 18
    )
    if summary["count"] < 20:
        score -= 100
    return {"params": asdict(params), "score": round(score, 4), "summary": summary, "trades": result["trades"]}


def split_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    days = sorted({row["day"] for row in rows})
    if len(days) <= 1:
        return rows, []
    split_index = max(1, int(len(days) * 0.67))
    train_days = set(days[:split_index])
    return [row for row in rows if row["day"] in train_days], [row for row in rows if row["day"] not in train_days]


def param_grid(cost_pct: float, gas_usd_per_swap: float, position_usd: float) -> list[PortfolioParams]:
    grid: list[PortfolioParams] = []
    ranges = [(5_000, 100_000), (10_000, 100_000), (20_000, 100_000), (30_000, 100_000)]
    h1_ranges = [(-80, 220), (-40, 140), (-20, 80)]
    m5_ranges = [(-50, 80), (-25, 45), (-15, 25)]
    exits = [
        (10, 20, 1.0, 0, 10),
        (12, 30, 1.0, 0, 15),
        (12, 50, 1.0, 0, 20),
        (12, 80, 1.0, 0, 45),
        (16, 50, 0.8, 30, 45),
        (18, 80, 0.8, 35, 60),
        (22, 100, 0.8, 35, 90),
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
            [6, 12, 24],
            [2, 4, 6],
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
            max_open,
            exit_set,
        ) = values
        stop, tp, sell_fraction, trail, time_stop = exit_set
        grid.append(
            PortfolioParams(
                name=f"portfolio_grid_{idx}",
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
                stop_pct=stop,
                tp_pct=tp,
                tp_sell_fraction=sell_fraction,
                trail_drawdown_pct=trail,
                time_stop_minutes=time_stop,
                cost_pct=cost_pct,
                gas_usd_per_swap=gas_usd_per_swap,
                position_usd=position_usd,
                max_open_positions=max_open,
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
    grid = param_grid(cost_pct, gas_usd_per_swap, position_usd)
    results: list[dict[str, Any]] = []
    for params in grid:
        train = evaluate(train_rows, params)
        if train["summary"]["count"] < 12:
            continue
        all_result = evaluate(rows, params)
        if all_result["summary"]["count"] < 20:
            continue
        test = evaluate(test_rows, params)
        robust = (
            all_result["summary"]["avg_net_return_pct"] > 0
            and all_result["summary"]["profit_factor"] >= 1.2
            and train["summary"]["avg_net_return_pct"] > -0.03
            and (test["summary"]["count"] == 0 or test["summary"]["avg_net_return_pct"] > -0.08)
        )
        combined = all_result["score"] + train["score"] * 0.35 + test["score"] * 0.25 + (30 if robust else -40)
        results.append(
            {
                "combined_score": round(combined, 4),
                "robustness_pass": robust,
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
        "# Alpha First Discovery Portfolio Optimization",
        "",
        f"- Generated: {result['generated_at']}",
        f"- Source: `{result['source']}`",
        f"- Prepared rows: {result['universe']['prepared_rows']}",
        f"- Grid size: {result['universe']['grid_size']}; evaluated: {result['universe']['evaluated']}",
        f"- Cost: {pct(result['assumptions']['cost_pct'])} + ${result['assumptions']['gas_usd_per_swap']:.2f}/swap on ${result['assumptions']['position_usd']:.2f} position",
        "",
        "| Rank | Robust | Trades/Candidates | Net win | Avg net | Median | PF | DD | Hold | TP/Stop | Cap | Mcap | Score | Liq | Smart | Top10 | H1 | M5 | Age | Exit |",
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | --- | --- | ---: | --- |",
    ]
    for index, item in enumerate(result["top_results"], start=1):
        params = item["all"]["params"]
        summary = item["all"]["summary"]
        lines.append(
            f"| {index} | {'yes' if item['robustness_pass'] else 'no'} "
            f"| {summary['count']}/{summary['candidate_count']} | {pct(summary['net_win_rate'])} "
            f"| {pct(summary['avg_net_return_pct'])} | {pct(summary['median_net_return_pct'])} "
            f"| {summary['profit_factor']:.2f} | {pct(summary['max_drawdown_units'])} | {summary['median_hold_minutes']:.0f}m "
            f"| {pct(summary['tp_rate'])}/{pct(summary['stop_rate'])} | {params['max_open_positions']} "
            f"| {money(params['mcap_min'])}-{money(params['mcap_max'])} | {params['min_signal_score']:.0f} "
            f"| {money(params['min_liquidity'])} | {params['min_smart_money']:.0f} | {params['max_top10_pct']:.0f} "
            f"| {params['min_h1_change_pct']:.0f}..{params['max_h1_change_pct']:.0f} "
            f"| {params['min_m5_change_pct']:.0f}..{params['max_m5_change_pct']:.0f} "
            f"| {params['max_pair_age_hours']:.0f}h "
            f"| SL{params['stop_pct']:.0f}/TP{params['tp_pct']:.0f}/sell{params['tp_sell_fraction']:.1f}/time{params['time_stop_minutes']:.0f} |"
        )
    if result["top_results"]:
        best = result["top_results"][0]["all"]
        lines.extend(["", "## Best Trades", "", "| Symbol | First seen | Mcap | Net | Gross | Peak | Hold | Path |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"])
        for trade in sorted(best["trades"], key=lambda row: row["net_return_pct"], reverse=True)[:30]:
            lines.append(
                f"| {trade['symbol']} | {trade['first_seen_at']} | {money(trade['first_mcap'])} "
                f"| {pct(trade['net_return_pct'])} | {pct(trade['gross_return_pct'])} "
                f"| {trade['peak_multiple']:.2f}x | {trade['hold_minutes']:.0f}m | {trade['path']} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Optimize first-discovery portfolio profitability with capacity limits.")
    parser.add_argument("--replay-history", type=Path, default=DEFAULT_REPLAY_HISTORY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--top-n", type=int, default=40)
    parser.add_argument("--cost-pct", type=float, default=0.05)
    parser.add_argument("--gas-usd-per-swap", type=float, default=0.12)
    parser.add_argument("--position-usd", type=float, default=35.0)
    args = parser.parse_args()

    result = optimize(args.replay_history, args.top_n, args.cost_pct, args.gas_usd_per_swap, args.position_usd)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_out.write_text(md_report(result), encoding="utf-8")
    best = result["top_results"][0] if result["top_results"] else None
    if best:
        params = best["all"]["params"]
        summary = best["all"]["summary"]
        print(
            "best "
            f"trades={summary['count']} candidates={summary['candidate_count']} "
            f"win={pct(summary['net_win_rate'])} avg_net={pct(summary['avg_net_return_pct'])} "
            f"pf={summary['profit_factor']:.2f} cap={params['max_open_positions']} "
            f"exit=SL{params['stop_pct']:.0f}/TP{params['tp_pct']:.0f}/time{params['time_stop_minutes']:.0f}"
        )
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
