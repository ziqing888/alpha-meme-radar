from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from alpha_first_discovery_backtest import TradeModel, money, simulate_ordered_price_trade
from alpha_meme_potential import meme_early_conviction_annotation


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPLAY_HISTORY = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_WATCH_STATE = OUT_DIR / "alpha-gold-watch-state.json"
DEFAULT_JSON_OUT = OUT_DIR / "alpha-gold-backtest-latest.json"
DEFAULT_MD_OUT = OUT_DIR / "alpha-gold-backtest-latest.md"

CN_TZ = timezone(timedelta(hours=8))
ACTIONABLE_BUCKETS = {"ambush", "primary", "pullback", "breakout", "shadow"}
RATING_ORDER = ("S", "A", "B", "C")


@dataclass(frozen=True)
class GoldStrategy:
    name: str
    label: str
    entry: str
    mcap_min: float = 0.0
    mcap_max: float = 300_000.0
    min_conviction: float = 82.0
    min_liquidity: float = 8_000.0
    min_volume24h: float = 0.0
    min_smart_money: float = 0.0
    min_kol: float = 0.0
    max_top10_holder_pct: float = 40.0
    max_pair_age_hours: float = 24.0


DEFAULT_STRATEGIES = [
    GoldStrategy(
        name="first_discovery_probe",
        label="首次发现小仓",
        entry="first_seen",
        mcap_min=10_000,
        mcap_max=300_000,
        min_conviction=82,
        min_liquidity=8_000,
        min_volume24h=30_000,
        max_top10_holder_pct=40,
        max_pair_age_hours=24,
    ),
    GoldStrategy(
        name="early_ambush",
        label="底部早鸟",
        entry="first_seen",
        mcap_min=30_000,
        mcap_max=100_000,
        min_conviction=88,
        min_liquidity=12_000,
        min_volume24h=50_000,
        min_smart_money=15,
        min_kol=3,
        max_top10_holder_pct=32,
        max_pair_age_hours=8,
    ),
    GoldStrategy(
        name="first_confirmation",
        label="首次确认后二买",
        entry="first_confirmation",
        mcap_min=30_000,
        mcap_max=500_000,
        min_conviction=82,
        min_liquidity=8_000,
        min_volume24h=30_000,
        max_top10_holder_pct=40,
        max_pair_age_hours=24,
    ),
]


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "").replace("%", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def token_key(row: dict[str, Any]) -> str:
    key = str(row.get("key") or "").strip().lower()
    if key:
        return key
    chain = str(row.get("chain") or row.get("network") or "").strip().lower()
    address = str(row.get("contract_address") or row.get("token_address") or "").strip().lower()
    return f"{chain}:{address}" if chain and address else ""


def normalized_chain(row: dict[str, Any], snapshot: dict[str, Any]) -> str:
    return str(row.get("chain") or snapshot.get("chain") or "").strip().lower()


def replay_rows(replay_history: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in (replay_history.get("rows") or {}).values() if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get("first_seen_at") or ""))
    return rows


def watch_candidates(watch_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(key).lower(): value
        for key, value in (watch_state.get("candidates") or {}).items()
        if isinstance(value, dict)
    }


def row_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = row.get("first_snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def confirmation_time(watch_row: dict[str, Any]) -> datetime | None:
    return parse_time(watch_row.get("first_confirmed_at") or watch_row.get("alerted_at"))


def sorted_observations(row: dict[str, Any]) -> list[dict[str, Any]]:
    points = [point for point in (row.get("observations") or []) if isinstance(point, dict)]
    return sorted(points, key=lambda point: str(point.get("seen_at") or ""))


def entry_from_observations(row: dict[str, Any], strategy: GoldStrategy, watch_row: dict[str, Any]) -> dict[str, Any] | None:
    points = sorted_observations(row)
    if strategy.entry == "first_seen":
        first_price = to_float(row.get("first_price_usd") or row_snapshot(row).get("price_usd"))
        if first_price <= 0:
            return None
        return {
            "entry_at": row.get("first_seen_at") or (points[0].get("seen_at") if points else ""),
            "entry_price": first_price,
            "points": points,
            "paid_up_multiple": 1.0,
        }

    confirmed_at = confirmation_time(watch_row)
    if not confirmed_at:
        return None
    for index, point in enumerate(points):
        seen_at = parse_time(point.get("seen_at"))
        price = to_float(point.get("price_usd"))
        if seen_at and seen_at >= confirmed_at and price > 0:
            first_price = to_float(row.get("first_price_usd") or row_snapshot(row).get("price_usd"))
            return {
                "entry_at": point.get("seen_at") or watch_row.get("first_confirmed_at") or "",
                "entry_price": price,
                "points": points[index:],
                "paid_up_multiple": round(price / first_price, 4) if first_price > 0 else 0.0,
            }
    return None


def passes_strategy(row: dict[str, Any], strategy: GoldStrategy, watch_row: dict[str, Any] | None) -> bool:
    snapshot = row_snapshot(row)
    chain = normalized_chain(row, snapshot)
    if chain not in {"bsc", "bnb", "bsc-mainnet", "robinhood", "4663", "solana", "sol", "base"}:
        return False

    mcap = to_float(snapshot.get("mcap") or snapshot.get("market_cap"))
    if strategy.entry == "first_confirmation":
        mcap = to_float((watch_row or {}).get("first_confirmed_mcap")) or mcap
        if not watch_row or not confirmation_time(watch_row):
            return False
        if watch_row.get("status") == "invalidated":
            return False

    bucket = str(row.get("recommendation_bucket") or snapshot.get("recommendation_bucket") or "").strip().lower()
    if bucket and bucket not in ACTIONABLE_BUCKETS:
        return False

    return (
        strategy.mcap_min <= mcap <= strategy.mcap_max
        and to_float(snapshot.get("gold_dog_conviction_score") or snapshot.get("gold_dog_score")) >= strategy.min_conviction
        and to_float(snapshot.get("liquidity") or snapshot.get("liquidity_usd")) >= strategy.min_liquidity
        and to_float(snapshot.get("volume24h") or snapshot.get("dex_volume24h")) >= strategy.min_volume24h
        and to_float(snapshot.get("smart_money")) >= strategy.min_smart_money
        and to_float(snapshot.get("kol")) >= strategy.min_kol
        and 0 <= to_float(snapshot.get("top10_holder_pct"), 999.0) <= strategy.max_top10_holder_pct
        and to_float(snapshot.get("pair_age_hours")) <= strategy.max_pair_age_hours
    )


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "win_rate": 0.0,
            "tp1_rate": 0.0,
            "two_x_rate": 0.0,
            "average_return_pct": 0.0,
            "median_return_pct": 0.0,
            "total_return_units": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_units": 0.0,
            "average_paid_up_multiple": 0.0,
        }
    returns = [to_float(trade.get("return_pct")) for trade in trades]
    peaks = [to_float(trade.get("peak_multiple")) for trade in trades]
    paid_up = [to_float(trade.get("entry_paid_up_multiple")) for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    gross_loss = abs(sum(losses))
    return {
        "count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "tp1_rate": round(sum(1 for peak_multiple in peaks if peak_multiple >= 1.45) / len(trades), 4),
        "two_x_rate": round(sum(1 for peak_multiple in peaks if peak_multiple >= 2.0) / len(trades), 4),
        "average_return_pct": round(sum(returns) / len(returns), 4),
        "median_return_pct": round(median(returns), 4),
        "total_return_units": round(sum(returns), 4),
        "profit_factor": round(sum(wins) / gross_loss, 4) if gross_loss > 0 else (999.0 if wins else 0.0),
        "max_drawdown_units": round(abs(max_drawdown), 4),
        "average_paid_up_multiple": round(sum(paid_up) / len(paid_up), 4) if paid_up else 0.0,
    }


def trade_score(summary: dict[str, Any]) -> float:
    count = to_float(summary.get("count"))
    if count <= 0:
        return -999.0
    sample_penalty = 100.0 if count < 2 else (20.0 if count < 5 else 0.0)
    non_positive_penalty = 50.0 if to_float(summary.get("average_return_pct")) <= 0 else 0.0
    return round(
        to_float(summary.get("average_return_pct")) * 100
        + to_float(summary.get("win_rate")) * 25
        + min(to_float(summary.get("profit_factor")), 8.0) * 2.5
        - to_float(summary.get("max_drawdown_units")) * 10
        - sample_penalty,
        4,
    ) - non_positive_penalty


def evaluate_strategy(
    rows: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    strategy: GoldStrategy,
    model: TradeModel,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    for row in rows:
        key = token_key(row)
        watch_row = candidates.get(key) or {}
        if not passes_strategy(row, strategy, watch_row):
            continue
        entry = entry_from_observations(row, strategy, watch_row)
        if not entry:
            continue
        sim = simulate_ordered_price_trade(entry["entry_price"], entry["points"], model)
        snapshot = row_snapshot(row)
        trades.append(
            {
                "key": key,
                "symbol": row.get("symbol") or snapshot.get("symbol") or "",
                "chain": normalized_chain(row, snapshot),
                "entry_at": entry["entry_at"],
                "entry_price_usd": entry["entry_price"],
                "entry_mcap": to_float((watch_row if strategy.entry == "first_confirmation" else snapshot).get("first_confirmed_mcap") or snapshot.get("mcap") or snapshot.get("market_cap")),
                "entry_paid_up_multiple": entry["paid_up_multiple"],
                "gold_dog_conviction_score": to_float(snapshot.get("gold_dog_conviction_score") or snapshot.get("gold_dog_score")),
                "return_pct": round(to_float(sim.get("return_pct")), 4),
                "peak_multiple": round(to_float(sim.get("peak_multiple")), 4),
                "path": sim.get("path") or "",
            }
        )
    summary = summarize_trades(trades)
    return {
        "name": strategy.name,
        "label": strategy.label,
        "entry": strategy.entry,
        "params": asdict(strategy),
        "score": trade_score(summary),
        "summary": summary,
        "trades": sorted(trades, key=lambda trade: to_float(trade.get("return_pct")), reverse=True)[:50],
    }


def early_rating(snapshot: dict[str, Any]) -> str:
    """Map the first-discovery snapshot to a testable conviction tier."""
    annotation = meme_early_conviction_annotation(snapshot)
    level = annotation.get("early_conviction_level")
    if level == "high" and annotation.get("early_conviction_data_status") == "已核验聪明钱":
        return "S"
    if level == "high":
        return "A"
    if level == "medium":
        return "B"
    return "C"


def evaluate_rating(
    rows: list[dict[str, Any]],
    rating: str,
    model: TradeModel,
    max_first_seen_mcap: float = 300_000.0,
) -> dict[str, Any]:
    """Replay first-discovery entries grouped by their first-snapshot rating."""
    trades: list[dict[str, Any]] = []
    for row in rows:
        snapshot = row_snapshot(row)
        chain = normalized_chain(row, snapshot)
        if chain not in {"bsc", "bnb", "bsc-mainnet", "robinhood", "4663", "solana", "sol", "base"}:
            continue
        first_mcap = to_float(snapshot.get("mcap") or snapshot.get("market_cap"))
        if not 0 < first_mcap <= max_first_seen_mcap or early_rating(snapshot) != rating:
            continue
        first_price = to_float(row.get("first_price_usd") or snapshot.get("price_usd"))
        if first_price <= 0:
            continue
        simulation = simulate_ordered_price_trade(first_price, sorted_observations(row), model)
        trades.append(
            {
                "key": token_key(row),
                "symbol": row.get("symbol") or snapshot.get("symbol") or "",
                "chain": chain,
                "entry_at": row.get("first_seen_at") or "",
                "entry_price_usd": first_price,
                "entry_mcap": first_mcap,
                "entry_paid_up_multiple": 1.0,
                "rating": rating,
                "return_pct": round(to_float(simulation.get("return_pct")), 4),
                "peak_multiple": round(to_float(simulation.get("peak_multiple")), 4),
                "path": simulation.get("path") or "",
            }
        )
    summary = summarize_trades(trades)
    return {
        "rating": rating,
        "summary": summary,
        "trades": sorted(trades, key=lambda trade: to_float(trade.get("return_pct")), reverse=True)[:50],
    }


def build_rating_backtest(
    rows: list[dict[str, Any]],
    model: TradeModel = TradeModel(),
    max_first_seen_mcap: float = 300_000.0,
) -> dict[str, Any]:
    return {
        "method": "first-discovery snapshot rating replay",
        "max_first_seen_mcap": max_first_seen_mcap,
        "rating_order": list(RATING_ORDER),
        "ratings": [evaluate_rating(rows, rating, model, max_first_seen_mcap) for rating in RATING_ORDER],
    }


def build_gold_backtest(
    replay_history: dict[str, Any],
    watch_state: dict[str, Any] | None = None,
    *,
    now_iso: str | None = None,
    strategies: list[GoldStrategy] | None = None,
    model: TradeModel = TradeModel(),
) -> dict[str, Any]:
    rows = replay_rows(replay_history)
    candidates = watch_candidates(watch_state or {})
    results = [evaluate_strategy(rows, candidates, strategy, model) for strategy in (strategies or DEFAULT_STRATEGIES)]
    ranked = sorted(results, key=lambda item: item["score"], reverse=True)
    best = ranked[0] if ranked and ranked[0]["summary"]["count"] else None
    data_quality = replay_data_quality(rows)
    return {
        "generated_at": now_iso or globals()["now_iso"](),
        "method": {
            "model": model.__dict__,
            "note": "本地只读回测：按扫描价格观察顺序触发止损/止盈，不代表真实成交滑点。",
        },
        "universe": {
            "replay_rows": len(rows),
            "watch_candidates": len(candidates),
        },
        "data_quality": data_quality,
        "rating_backtest": build_rating_backtest(rows, model),
        "strategies": results,
        "best_strategy": {
            "name": best["name"],
            "label": best["label"],
            "score": best["score"],
            "summary": best["summary"],
        }
        if best
        else None,
    }


def replay_data_quality(rows: list[dict[str, Any]]) -> dict[str, int]:
    trajectories = [row.get("trajectory") if isinstance(row.get("trajectory"), dict) else {} for row in rows]
    complete = sum(trajectory.get("coverage_from_first") is True for trajectory in trajectories)
    result = {
        "replay_rows": len(rows),
        "coverage_from_first": complete,
        "partial_trajectory": len(rows) - complete,
    }
    for target in (2, 3, 5, 10):
        result[f"labeled_{target}x"] = sum(
            isinstance(trajectory.get(f"hit_{target}x_before_stop"), bool)
            for trajectory in trajectories
        )
    return result


def pct(value: Any) -> str:
    return f"{to_float(value) * 100:.1f}%"


def md_report(result: dict[str, Any]) -> str:
    quality = result.get("data_quality") or {}
    lines = [
        "# Meme Gold Backtest",
        "",
        f"- Generated: {result.get('generated_at', '')}",
        f"- Replay rows: {result.get('universe', {}).get('replay_rows', 0)}",
        f"- Watch candidates: {result.get('universe', {}).get('watch_candidates', 0)}",
        f"- Complete paths from first discovery: {quality.get('coverage_from_first', 0)} / {quality.get('replay_rows', 0)}",
        f"- Causal labels: 2x={quality.get('labeled_2x', 0)}, 3x={quality.get('labeled_3x', 0)}, 5x={quality.get('labeled_5x', 0)}, 10x={quality.get('labeled_10x', 0)}",
        "",
        "| Strategy | Trades | Win | TP1 | >=2x | Avg return | Profit factor | Avg paid-up |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in result.get("strategies") or []:
        summary = item.get("summary") or {}
        lines.append(
            "| "
            f"{item.get('label')} | {summary.get('count', 0)} | "
            f"{pct(summary.get('win_rate'))} | {pct(summary.get('tp1_rate'))} | "
            f"{pct(summary.get('two_x_rate'))} | {pct(summary.get('average_return_pct'))} | "
            f"{to_float(summary.get('profit_factor')):.2f} | {to_float(summary.get('average_paid_up_multiple')):.2f}x |"
        )
    best = result.get("best_strategy") or {}
    if best:
        summary = best.get("summary") or {}
        lines.extend(
            [
                "",
                "## Current Best",
                "",
                f"- {best.get('label')}：样本 {summary.get('count', 0)}，平均模型收益 {pct(summary.get('average_return_pct'))}，胜率 {pct(summary.get('win_rate'))}。",
            ]
        )
    lines.extend(["", "## Top Trades", "", "| Strategy | Symbol | Entry cap | Return | Peak | Path |", "| --- | --- | ---: | ---: | ---: | --- |"])
    for item in result.get("strategies") or []:
        for trade in (item.get("trades") or [])[:8]:
            lines.append(
                "| "
                f"{item.get('label')} | {trade.get('symbol')} | {money(trade.get('entry_mcap'))} | "
                f"{pct(trade.get('return_pct'))} | {to_float(trade.get('peak_multiple')):.2f}x | {trade.get('path', '')} |"
            )
    lines.extend(
        [
            "",
            "## Early Rating Backtest",
            "",
            "评级只使用首次发现快照，收益使用之后的有序价格观察；S 级还要求首次快照已有核验聪明钱。",
            "",
            "| Rating | Trades | Win | TP1 | >=2x | Avg return | Profit factor | Max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in (result.get("rating_backtest") or {}).get("ratings") or []:
        summary = item.get("summary") or {}
        lines.append(
            "| "
            f"{item.get('rating')} | {summary.get('count', 0)} | {pct(summary.get('win_rate'))} | "
            f"{pct(summary.get('tp1_rate'))} | {pct(summary.get('two_x_rate'))} | "
            f"{pct(summary.get('average_return_pct'))} | {to_float(summary.get('profit_factor')):.2f} | "
            f"{pct(summary.get('max_drawdown_units'))} |"
        )
    return "\n".join(lines)


def run_from_paths(replay_history_path: Path, watch_state_path: Path, json_out: Path, md_out: Path) -> dict[str, Any]:
    result = build_gold_backtest(load_json(replay_history_path, {}), load_json(watch_state_path, {}))
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(md_report(result), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest Meme gold-dog strategy layers from local replay history.")
    parser.add_argument("--replay-history", type=Path, default=DEFAULT_REPLAY_HISTORY)
    parser.add_argument("--watch-state", type=Path, default=DEFAULT_WATCH_STATE)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--md-out", type=Path, default=DEFAULT_MD_OUT)
    args = parser.parse_args()
    result = run_from_paths(args.replay_history, args.watch_state, args.json_out, args.md_out)
    best = result.get("best_strategy") or {}
    best_summary = best.get("summary") or {}
    print(
        "gold_backtest "
        f"best={best.get('name', 'none')} "
        f"n={best_summary.get('count', 0)} "
        f"win={pct(best_summary.get('win_rate'))} "
        f"avg_return={pct(best_summary.get('average_return_pct'))}"
    )
    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
