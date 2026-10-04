from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_WATCH_STATE = OUT_DIR / "alpha-gold-watch-state.json"
DEFAULT_REPLAY_HISTORY = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-first-discovery-backtest.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-first-discovery-backtest.md"

CN_TZ = timezone(timedelta(hours=8))

MISSING = object()


@dataclass(frozen=True)
class TradeModel:
    name: str = "tp45_tp120_stop22"
    tp1_multiple: float = 1.45
    tp1_fraction: float = 0.50
    tp2_multiple: float = 2.20
    tp2_fraction: float = 0.30
    stop_multiple: float = 0.78
    runner_floor_after_tp2: float = 1.43
    runner_trail_drawdown_pct: float = 0.35


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


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def cap_bucket(mcap: float) -> str:
    if mcap <= 10_000:
        return "<=10K"
    if mcap <= 30_000:
        return "10K-30K"
    if mcap <= 100_000:
        return "30K-100K"
    if mcap <= 300_000:
        return "100K-300K"
    if mcap <= 1_000_000:
        return "300K-1M"
    return ">1M"


def safe_multiple(numerator: Any, denominator: Any) -> float:
    base = to_float(denominator)
    if base <= 0:
        return 0.0
    return to_float(numerator) / base


def simulate_partial_tp_trade(
    peak_multiple: float,
    final_multiple: float,
    model: TradeModel = TradeModel(),
) -> dict[str, Any]:
    """Approximate tradable return from a monitor envelope, without tick order."""
    if peak_multiple <= 0:
        return {"exit_multiple": 0.0, "return_pct": -1.0, "path": "missing_peak"}

    if peak_multiple >= model.tp2_multiple:
        sold = model.tp1_fraction * model.tp1_multiple
        sold += model.tp2_fraction * model.tp2_multiple
        runner_fraction = 1.0 - model.tp1_fraction - model.tp2_fraction
        runner_exit = max(final_multiple, model.runner_floor_after_tp2)
        exit_multiple = sold + runner_fraction * runner_exit
        return {
            "exit_multiple": exit_multiple,
            "return_pct": exit_multiple - 1.0,
            "path": "tp1_tp2_runner",
        }

    if peak_multiple >= model.tp1_multiple:
        sold = model.tp1_fraction * model.tp1_multiple
        runner_fraction = 1.0 - model.tp1_fraction
        runner_exit = max(final_multiple, model.stop_multiple)
        exit_multiple = sold + runner_fraction * runner_exit
        return {
            "exit_multiple": exit_multiple,
            "return_pct": exit_multiple - 1.0,
            "path": "tp1_runner",
        }

    exit_multiple = final_multiple if final_multiple >= 1.0 else model.stop_multiple
    return {
        "exit_multiple": exit_multiple,
        "return_pct": exit_multiple - 1.0,
        "path": "mark_or_stop",
    }


def simulate_ordered_price_trade(
    first_price: float,
    observations: list[dict[str, Any]],
    model: TradeModel = TradeModel(),
) -> dict[str, Any]:
    if first_price <= 0:
        return {"exit_multiple": 0.0, "return_pct": -1.0, "path": "missing_entry_price"}

    points = sorted(
        [
            obs
            for obs in observations
            if isinstance(obs, dict) and to_float(obs.get("price_usd")) > 0
        ],
        key=lambda obs: str(obs.get("seen_at") or ""),
    )
    if not points:
        return {"exit_multiple": 1.0, "return_pct": 0.0, "path": "no_followup"}

    remaining = 1.0
    realized = 0.0
    high_multiple = 1.0
    hit_tp1 = False
    hit_tp2 = False
    exit_path: list[str] = []

    for point in points:
        multiple = to_float(point.get("price_usd")) / first_price
        high_multiple = max(high_multiple, multiple)

        if not hit_tp1 and multiple <= model.stop_multiple:
            realized += remaining * model.stop_multiple
            remaining = 0.0
            exit_path.append("stop")
            break

        if not hit_tp1 and multiple >= model.tp1_multiple:
            realized += model.tp1_fraction * model.tp1_multiple
            remaining -= model.tp1_fraction
            hit_tp1 = True
            exit_path.append("tp1")

        if hit_tp1 and not hit_tp2 and multiple >= model.tp2_multiple:
            realized += model.tp2_fraction * model.tp2_multiple
            remaining -= model.tp2_fraction
            hit_tp2 = True
            exit_path.append("tp2")

        if hit_tp2 and remaining > 0:
            trail_multiple = high_multiple * (1.0 - model.runner_trail_drawdown_pct)
            if multiple <= trail_multiple:
                realized += remaining * max(multiple, model.runner_floor_after_tp2)
                remaining = 0.0
                exit_path.append("trail")
                break

    if remaining > 0:
        final_multiple = to_float(points[-1].get("price_usd")) / first_price
        if hit_tp1:
            final_multiple = max(final_multiple, model.stop_multiple)
        realized += remaining * final_multiple
        exit_path.append("mark")

    return {
        "exit_multiple": realized,
        "return_pct": realized - 1.0,
        "peak_multiple": high_multiple,
        "path": "_".join(exit_path),
        "hit_tp1": hit_tp1,
        "hit_tp2": hit_tp2,
    }


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "win_rate": 0.0,
            "tp1_rate": 0.0,
            "two_x_rate": 0.0,
            "five_x_rate": 0.0,
            "ten_x_rate": 0.0,
            "median_peak_multiple": 0.0,
            "average_peak_multiple": 0.0,
            "median_return_pct": 0.0,
            "average_return_pct": 0.0,
            "total_return_units": 0.0,
        }

    peaks = [to_float(t["peak_multiple"]) for t in trades]
    returns = [to_float(t["return_pct"]) for t in trades]
    return {
        "count": len(trades),
        "win_rate": sum(1 for r in returns if r > 0) / len(returns),
        "tp1_rate": sum(1 for m in peaks if m >= 1.45) / len(peaks),
        "two_x_rate": sum(1 for m in peaks if m >= 2.0) / len(peaks),
        "five_x_rate": sum(1 for m in peaks if m >= 5.0) / len(peaks),
        "ten_x_rate": sum(1 for m in peaks if m >= 10.0) / len(peaks),
        "median_peak_multiple": median(peaks),
        "average_peak_multiple": sum(peaks) / len(peaks),
        "median_return_pct": median(returns),
        "average_return_pct": sum(returns) / len(returns),
        "total_return_units": sum(returns),
    }


def build_trade(row: dict[str, Any], entry_field: str, label: str) -> dict[str, Any] | None:
    entry_mcap = to_float(row.get(entry_field))
    if entry_mcap <= 0:
        return None

    max_mcap = to_float(row.get("max_seen_mcap"))
    last_mcap = to_float(row.get("last_seen_mcap"))
    peak_multiple = safe_multiple(max_mcap, entry_mcap)
    final_multiple = safe_multiple(last_mcap, entry_mcap)
    simulated = simulate_partial_tp_trade(peak_multiple, final_multiple)
    return {
        "symbol": row.get("symbol") or "",
        "chain": row.get("chain") or "",
        "contract_address": row.get("contract_address") or "",
        "status": row.get("status") or "",
        "watch_v2_tier": row.get("watch_v2_tier") or "",
        "entry_label": label,
        "entry_field": entry_field,
        "entry_at": row.get("first_seen_at") if entry_field == "first_seen_mcap" else row.get("first_confirmed_at"),
        "entry_mcap": entry_mcap,
        "first_seen_mcap": to_float(row.get("first_seen_mcap")),
        "first_confirmed_mcap": to_float(row.get("first_confirmed_mcap")),
        "last_seen_mcap": last_mcap,
        "max_seen_mcap": max_mcap,
        "peak_multiple": peak_multiple,
        "final_multiple": final_multiple,
        **simulated,
    }


def select_rows(candidates: list[dict[str, Any]], max_first_seen_mcap: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in candidates:
        if str(row.get("chain") or "").lower() not in {"bsc", "bnb", "bsc-mainnet"}:
            continue
        first_seen_mcap = to_float(row.get("first_seen_mcap"))
        if first_seen_mcap <= 0 or first_seen_mcap > max_first_seen_mcap:
            continue
        if to_float(row.get("max_seen_mcap")) <= 0:
            continue
        rows.append(row)
    return rows


def compare_first_seen_to_confirmation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    for row in rows:
        first_seen = build_trade(row, "first_seen_mcap", "first_discovery")
        confirmed = build_trade(row, "first_confirmed_mcap", "first_confirmation")
        if not first_seen or not confirmed:
            continue
        pairs.append(
            {
                "symbol": first_seen["symbol"],
                "contract_address": first_seen["contract_address"],
                "first_seen_entry_mcap": first_seen["entry_mcap"],
                "first_confirmed_entry_mcap": confirmed["entry_mcap"],
                "confirmation_paid_up_multiple": safe_multiple(
                    confirmed["entry_mcap"], first_seen["entry_mcap"]
                ),
                "first_seen_peak_multiple": first_seen["peak_multiple"],
                "confirmed_peak_multiple": confirmed["peak_multiple"],
                "first_seen_return_pct": first_seen["return_pct"],
                "confirmed_return_pct": confirmed["return_pct"],
                "return_delta_pct": first_seen["return_pct"] - confirmed["return_pct"],
            }
        )

    if not pairs:
        return {"count": 0, "median_confirmation_paid_up_multiple": 0.0, "average_return_delta_pct": 0.0}

    return {
        "count": len(pairs),
        "median_confirmation_paid_up_multiple": median([p["confirmation_paid_up_multiple"] for p in pairs]),
        "average_confirmation_paid_up_multiple": sum(p["confirmation_paid_up_multiple"] for p in pairs) / len(pairs),
        "average_return_delta_pct": sum(p["return_delta_pct"] for p in pairs) / len(pairs),
        "pairs": sorted(pairs, key=lambda p: p["return_delta_pct"], reverse=True)[:25],
    }


def build_replay_trade(row: dict[str, Any]) -> dict[str, Any] | None:
    first_price = to_float(row.get("first_price_usd"))
    first_snapshot = row.get("first_snapshot") if isinstance(row.get("first_snapshot"), dict) else {}
    first_mcap = to_float(first_snapshot.get("mcap") or first_snapshot.get("market_cap"))
    if first_price <= 0 or first_mcap <= 0:
        return None

    observations = row.get("observations") if isinstance(row.get("observations"), list) else []
    simulation = simulate_ordered_price_trade(first_price, observations)
    return {
        "symbol": row.get("symbol") or "",
        "chain": row.get("chain") or "",
        "contract_address": row.get("contract_address") or "",
        "first_seen_at": row.get("first_seen_at") or "",
        "latest_seen_at": row.get("latest_seen_at") or "",
        "first_seen_mcap": first_mcap,
        "first_price_usd": first_price,
        "latest_price_usd": to_float(row.get("latest_price_usd")),
        "recommendation_bucket": row.get("recommendation_bucket") or first_snapshot.get("recommendation_bucket") or "",
        "first_snapshot_bucket": first_snapshot.get("recommendation_bucket") or "",
        "first_score": to_float(first_snapshot.get("score")),
        "first_entry_score": to_float(first_snapshot.get("entry_score")),
        "first_gold_score": to_float(first_snapshot.get("gold_dog_score")),
        "observation_count": len(observations),
        **simulation,
    }


def run_scan_replay_backtest(replay_history_path: Path, max_first_seen_mcap: float) -> dict[str, Any]:
    if not replay_history_path.exists():
        return {"available": False, "reason": f"missing {replay_history_path}"}

    history = load_json(replay_history_path)
    rows = list((history.get("rows") or {}).values())
    trades = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("chain") or "").lower() not in {"bsc", "bnb", "bsc-mainnet"}:
            continue
        trade = build_replay_trade(row)
        if not trade:
            continue
        if to_float(trade["first_seen_mcap"]) > max_first_seen_mcap:
            continue
        trades.append(trade)

    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_recommendation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_bucket[cap_bucket(to_float(trade["first_seen_mcap"]))].append(trade)
        by_recommendation[str(trade["recommendation_bucket"] or "unknown")].append(trade)

    return {
        "available": True,
        "source": str(replay_history_path),
        "summary": summarize(trades),
        "by_mcap_bucket": {bucket: summarize(bucket_trades) for bucket, bucket_trades in sorted(by_bucket.items())},
        "by_recommendation": {
            bucket: summarize(bucket_trades) for bucket, bucket_trades in sorted(by_recommendation.items())
        },
        "top_winners": sorted(trades, key=lambda t: t["return_pct"], reverse=True)[:20],
        "worst_model_returns": sorted(trades, key=lambda t: t["return_pct"])[:20],
    }


def run_backtest(
    watch_state_path: Path,
    max_first_seen_mcap: float,
    replay_history_path: Path | None = None,
) -> dict[str, Any]:
    state = load_json(watch_state_path)
    candidates = list((state.get("candidates") or {}).values())
    rows = select_rows(candidates, max_first_seen_mcap=max_first_seen_mcap)

    first_discovery_trades = [
        trade for row in rows if (trade := build_trade(row, "first_seen_mcap", "first_discovery"))
    ]
    confirmation_rows = [row for row in rows if to_float(row.get("first_confirmed_mcap")) > 0]
    confirmation_trades = [
        trade for row in confirmation_rows if (trade := build_trade(row, "first_confirmed_mcap", "first_confirmation"))
    ]

    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_status: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in first_discovery_trades:
        by_bucket[cap_bucket(to_float(trade["entry_mcap"]))].append(trade)
        by_status[str(trade["status"] or "unknown")].append(trade)

    top_winners = sorted(first_discovery_trades, key=lambda t: t["peak_multiple"], reverse=True)[:20]
    worst = sorted(first_discovery_trades, key=lambda t: t["return_pct"])[:20]

    result = {
        "generated_at": now_iso(),
        "source": str(watch_state_path),
        "method": {
            "max_first_seen_mcap": max_first_seen_mcap,
            "trade_model": TradeModel().__dict__,
            "note": "Uses monitor first_seen/first_confirmed/max_seen/last_seen envelopes, not tick-level fills.",
        },
        "universe": {
            "all_candidates": len(candidates),
            "selected_first_seen_bsc_under_cap": len(rows),
            "with_first_confirmation": len(confirmation_rows),
        },
        "first_discovery": {
            "summary": summarize(first_discovery_trades),
            "by_mcap_bucket": {bucket: summarize(trades) for bucket, trades in sorted(by_bucket.items())},
            "by_status": {status: summarize(trades) for status, trades in sorted(by_status.items())},
            "top_winners": top_winners,
            "worst_model_returns": worst,
        },
        "first_confirmation": {
            "summary": summarize(confirmation_trades),
        },
        "comparison_same_tokens": compare_first_seen_to_confirmation(rows),
    }
    if replay_history_path is not None:
        result["scan_replay"] = run_scan_replay_backtest(replay_history_path, max_first_seen_mcap)
    return result


def md_summary(result: dict[str, Any]) -> str:
    def row(name: str, summary: dict[str, Any]) -> str:
        return (
            f"| {name} | {summary['count']} | {pct(summary['win_rate'])} | "
            f"{pct(summary['tp1_rate'])} | {pct(summary['two_x_rate'])} | "
            f"{pct(summary['five_x_rate'])} | {summary['median_peak_multiple']:.2f}x | "
            f"{summary['average_peak_multiple']:.2f}x | {pct(summary['average_return_pct'])} |"
        )

    discovery = result["first_discovery"]["summary"]
    confirmation = result["first_confirmation"]["summary"]
    comparison = result["comparison_same_tokens"]
    lines = [
        "# Alpha First Discovery Backtest",
        "",
        f"- Generated: {result['generated_at']}",
        f"- Source: `{result['source']}`",
        f"- Universe: BSC candidates with first seen market cap <= {money(result['method']['max_first_seen_mcap'])}",
        f"- Model: {result['method']['trade_model']['name']}; TP1 45% sells 50%, TP2 120% sells 30%, unresolved losers stop at -22%.",
        "- Limitation: monitor-envelope backtest, not tick-level fill replay.",
        "",
        "## Headline",
        "",
        "| Entry | Trades | Win | TP1 hit | Peak >=2x | Peak >=5x | Median peak | Avg peak | Avg modeled return |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        row("First discovery", discovery),
        row("First confirmation", confirmation),
        "",
        "## First Discovery By Market Cap",
        "",
        "| First seen cap | Trades | Win | TP1 hit | Peak >=2x | Peak >=5x | Median peak | Avg peak | Avg modeled return |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for bucket, summary in result["first_discovery"]["by_mcap_bucket"].items():
        lines.append(row(bucket, summary))

    lines.extend(
        [
            "",
            "## Same Token Comparison",
            "",
            f"- Tokens with both first discovery and first confirmation: {comparison.get('count', 0)}",
            f"- Median markup paid by waiting for confirmation: {comparison.get('median_confirmation_paid_up_multiple', 0.0):.2f}x",
            f"- Average modeled return advantage of first discovery: {pct(comparison.get('average_return_delta_pct', 0.0))}",
            "",
        ]
    )
    replay = result.get("scan_replay") or {}
    if replay.get("available"):
        replay_summary = replay["summary"]
        lines.extend(
            [
                "## Scan Replay",
                "",
                "This replays ordered price observations from first discovery and triggers stop/TP in sequence.",
                "",
                "| Entry | Trades | Win | TP1 hit | Peak >=2x | Peak >=5x | Median peak | Avg peak | Avg modeled return |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
                row("First discovery scan replay", replay_summary),
                "",
                "| First seen cap | Trades | Win | TP1 hit | Peak >=2x | Peak >=5x | Median peak | Avg peak | Avg modeled return |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for bucket, summary in replay["by_mcap_bucket"].items():
            lines.append(row(bucket, summary))
        lines.append("")

    lines.extend(
        [
            "## Biggest First-Discovery Winners",
            "",
            "| Symbol | First seen | First confirmed | Peak | Current | Peak multiple | Status |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for trade in result["first_discovery"]["top_winners"][:12]:
        lines.append(
            "| "
            f"{trade['symbol']} | {money(trade['first_seen_mcap'])} | "
            f"{money(trade['first_confirmed_mcap']) if trade['first_confirmed_mcap'] else '-'} | "
            f"{money(trade['max_seen_mcap'])} | {money(trade['last_seen_mcap'])} | "
            f"{trade['peak_multiple']:.2f}x | {trade['status']} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- If first discovery beats first confirmation, live paper should probe earlier with smaller size.",
            "- Confirmation should become add/hold evidence, not the first entry gate.",
            "- The next stricter version needs tick or scan-history replay to test stop order and max drawdown.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest Alpha first-discovery entries from local watch state.")
    parser.add_argument("--watch-state", type=Path, default=DEFAULT_WATCH_STATE)
    parser.add_argument("--replay-history", type=Path, default=DEFAULT_REPLAY_HISTORY)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--max-first-seen-mcap", type=float, default=300_000.0)
    args = parser.parse_args()

    result = run_backtest(
        args.watch_state,
        max_first_seen_mcap=args.max_first_seen_mcap,
        replay_history_path=args.replay_history,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    args.md_out.write_text(md_summary(result), encoding="utf-8")

    discovery = result["first_discovery"]["summary"]
    confirmation = result["first_confirmation"]["summary"]
    print(
        "first_discovery "
        f"n={discovery['count']} win={pct(discovery['win_rate'])} "
        f"tp1={pct(discovery['tp1_rate'])} 2x={pct(discovery['two_x_rate'])} "
        f"5x={pct(discovery['five_x_rate'])} avg_return={pct(discovery['average_return_pct'])}"
    )
    print(
        "first_confirmation "
        f"n={confirmation['count']} win={pct(confirmation['win_rate'])} "
        f"tp1={pct(confirmation['tp1_rate'])} 2x={pct(confirmation['two_x_rate'])} "
        f"5x={pct(confirmation['five_x_rate'])} avg_return={pct(confirmation['average_return_pct'])}"
    )
    replay = result.get("scan_replay") or {}
    if replay.get("available"):
        summary = replay["summary"]
        print(
            "scan_replay "
            f"n={summary['count']} win={pct(summary['win_rate'])} "
            f"tp1={pct(summary['tp1_rate'])} 2x={pct(summary['two_x_rate'])} "
            f"5x={pct(summary['five_x_rate'])} avg_return={pct(summary['average_return_pct'])}"
        )
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
