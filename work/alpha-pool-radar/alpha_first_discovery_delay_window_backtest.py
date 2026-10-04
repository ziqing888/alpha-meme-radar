from __future__ import annotations

import argparse
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
DEFAULT_JSON_OUT = OUT_DIR / "alpha-first-discovery-delay-window-backtest.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-first-discovery-delay-window-backtest.md"

CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class DelayParams:
    name: str
    max_entry_delay_minutes: float
    max_markup_from_first: float
    min_liquidity: float
    mcap_min: float = 10_000.0
    mcap_max: float = 100_000.0
    min_h1_change_pct: float = -20.0
    max_h1_change_pct: float = 80.0
    min_m5_change_pct: float = -25.0
    max_m5_change_pct: float = 45.0
    max_pair_age_hours: float = 6.0
    stop_pct: float = 22.0
    tp_pct: float = 100.0
    tp_sell_fraction: float = 0.8
    trail_drawdown_pct: float = 35.0
    time_stop_minutes: float = 90.0
    cost_pct: float = 0.05
    gas_usd_per_swap: float = 0.12
    position_usd: float = 35.0


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def to_net_params(params: DelayParams) -> NetParams:
    return NetParams(
        name=params.name,
        mcap_min=params.mcap_min,
        mcap_max=params.mcap_max,
        min_signal_score=0.0,
        min_liquidity=params.min_liquidity,
        min_volume24h=0.0,
        min_smart_money=0.0,
        min_kol=0.0,
        max_top10_pct=999.0,
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


def elapsed_minutes(start: datetime | None, current: datetime | None) -> float:
    if not start or not current:
        return 0.0
    return max(0.0, (current - start).total_seconds() / 60.0)


def first_entry_point(row: dict[str, Any], params: DelayParams) -> tuple[datetime, float] | None:
    first_seen = parse_iso(row.get("first_seen_at"))
    if not first_seen:
        return None
    for ts, multiple_from_first in observation_points(row):
        if not ts:
            continue
        delay = elapsed_minutes(first_seen, ts)
        if delay > params.max_entry_delay_minutes:
            break
        if multiple_from_first <= 0 or multiple_from_first > params.max_markup_from_first:
            continue
        return ts, multiple_from_first
    return None


def simulate_delayed_trade(row: dict[str, Any], params: DelayParams) -> dict[str, Any] | None:
    entry = first_entry_point(row, params)
    if not entry:
        return None
    entry_time, entry_multiple_from_first = entry
    points = [
        (ts, multiple / entry_multiple_from_first)
        for ts, multiple in observation_points(row)
        if ts and ts >= entry_time
    ]
    if not points:
        points = [(entry_time, 1.0)]

    stop_multiple = 1.0 - params.stop_pct / 100.0
    tp_multiple = 1.0 + params.tp_pct / 100.0
    trail_fraction = params.trail_drawdown_pct / 100.0
    deadline = entry_time + timedelta(minutes=params.time_stop_minutes)
    remaining = 1.0
    realized = 0.0
    sell_count = 0
    peak = 1.0
    tp_hit = False
    path: list[str] = []
    exit_time = deadline
    last_multiple = 1.0

    for ts, multiple in points:
        if ts > deadline:
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

    net_params = to_net_params(params)
    gross = realized - 1.0
    net = gross - cost_return(net_params, sell_count=sell_count)
    return {
        "symbol": row["symbol"],
        "key": row["key"],
        "first_seen_at": row["first_seen_at"],
        "entry_time": entry_time.isoformat(timespec="seconds"),
        "entry_delay_minutes": elapsed_minutes(parse_iso(row.get("first_seen_at")), entry_time),
        "first_mcap": row["first_mcap"],
        "entry_mcap": row["first_mcap"] * entry_multiple_from_first,
        "markup_from_first": entry_multiple_from_first,
        "gross_return_pct": gross,
        "net_return_pct": net,
        "peak_multiple": peak,
        "hold_minutes": elapsed_minutes(entry_time, exit_time),
        "path": "_".join(path),
        "sell_count": sell_count,
    }


def summarize(trades: list[dict[str, Any]], candidate_count: int) -> dict[str, Any]:
    if not trades:
        return {
            "candidate_count": candidate_count,
            "count": 0,
            "net_win_rate": 0.0,
            "avg_net_return_pct": 0.0,
            "median_net_return_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_units": 0.0,
            "median_delay_minutes": 0.0,
            "median_hold_minutes": 0.0,
            "tp_rate": 0.0,
            "stop_rate": 0.0,
        }
    returns = [to_float(trade["net_return_pct"]) for trade in trades]
    wins = [ret for ret in returns if ret > 0]
    losses = [ret for ret in returns if ret < 0]
    equity = 0.0
    peak_equity = 0.0
    max_dd = 0.0
    for ret in returns:
        equity += ret
        peak_equity = max(peak_equity, equity)
        max_dd = min(max_dd, equity - peak_equity)
    return {
        "candidate_count": candidate_count,
        "count": len(trades),
        "net_win_rate": len(wins) / len(trades),
        "avg_net_return_pct": sum(returns) / len(returns),
        "median_net_return_pct": median(returns),
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else 999.0,
        "max_drawdown_units": abs(max_dd),
        "median_delay_minutes": median([to_float(trade["entry_delay_minutes"]) for trade in trades]),
        "median_hold_minutes": median([to_float(trade["hold_minutes"]) for trade in trades]),
        "tp_rate": sum(1 for trade in trades if str(trade["path"]).startswith("tp")) / len(trades),
        "stop_rate": sum(1 for trade in trades if "stop" in str(trade["path"])) / len(trades),
    }


def evaluate(rows: list[dict[str, Any]], params: DelayParams) -> dict[str, Any]:
    net_params = to_net_params(params)
    candidates = [row for row in rows if passes(row, net_params)]
    trades = [trade for row in candidates if (trade := simulate_delayed_trade(row, params))]
    return {
        "params": asdict(params),
        "summary": summarize(trades, len(candidates)),
        "trades": sorted(trades, key=lambda trade: trade["entry_time"]),
    }


def run_backtest(replay_history_path: Path) -> dict[str, Any]:
    rows = prepared_rows(replay_history_path)
    for row in rows:
        row["_observation_points"] = observation_points(row)
    configs = [
        DelayParams("strict_45m_liq8k", 45, 2.2, 8_000),
        DelayParams("shadow_90m_liq8k", 90, 2.2, 8_000),
        DelayParams("shadow_180m_liq8k", 180, 2.2, 8_000),
        DelayParams("shadow_180m_no_liq_gate", 180, 2.2, 0),
        DelayParams("shadow_180m_tight_markup", 180, 1.6, 8_000),
    ]
    return {
        "generated_at": now_iso(),
        "source": str(replay_history_path),
        "universe": {"prepared_rows": len(rows)},
        "results": [evaluate(rows, params) for params in configs],
    }


def md_report(result: dict[str, Any]) -> str:
    lines = [
        "# Alpha First Discovery Delay Window Backtest",
        "",
        f"- Generated: {result['generated_at']}",
        f"- Source: `{result['source']}`",
        f"- Prepared rows: {result['universe']['prepared_rows']}",
        "- Model: enter at the first observed price inside the delay window, require markup from first discovery under the cap, then use SL22/TP100 sell80/time90 with estimated costs.",
        "",
        "| Strategy | Trades/Candidates | Win | Avg net | Median | PF | DD | Delay | Hold | TP/Stop |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in result["results"]:
        params = item["params"]
        summary = item["summary"]
        lines.append(
            f"| {params['name']} | {summary['count']}/{summary['candidate_count']} "
            f"| {pct(summary['net_win_rate'])} | {pct(summary['avg_net_return_pct'])} "
            f"| {pct(summary['median_net_return_pct'])} | {summary['profit_factor']:.2f} "
            f"| {pct(summary['max_drawdown_units'])} | {summary['median_delay_minutes']:.0f}m "
            f"| {summary['median_hold_minutes']:.0f}m | {pct(summary['tp_rate'])}/{pct(summary['stop_rate'])} |"
        )
    for item in result["results"]:
        lines.extend(["", f"## {item['params']['name']} Top Trades", "", "| Symbol | Entry | Delay | First mcap | Entry mcap | Net | Gross | Peak | Path |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])
        for trade in sorted(item["trades"], key=lambda row: row["net_return_pct"], reverse=True)[:12]:
            lines.append(
                f"| {trade['symbol']} | {trade['entry_time']} | {trade['entry_delay_minutes']:.0f}m "
                f"| {money(trade['first_mcap'])} | {money(trade['entry_mcap'])} "
                f"| {pct(trade['net_return_pct'])} | {pct(trade['gross_return_pct'])} "
                f"| {trade['peak_multiple']:.2f}x | {trade['path']} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest delayed first-discovery entry windows.")
    parser.add_argument("--replay-history", type=Path, default=DEFAULT_REPLAY_HISTORY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    args = parser.parse_args()

    result = run_backtest(args.replay_history)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_out.write_text(md_report(result), encoding="utf-8")
    for item in result["results"]:
        summary = item["summary"]
        print(
            f"{item['params']['name']} trades={summary['count']}/{summary['candidate_count']} "
            f"win={pct(summary['net_win_rate'])} avg_net={pct(summary['avg_net_return_pct'])} "
            f"pf={summary['profit_factor']:.2f}"
        )
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
