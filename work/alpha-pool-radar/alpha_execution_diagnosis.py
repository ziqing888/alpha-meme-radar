"""Read-only ledger diagnosis. Candidate filters are not a portfolio backtest."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


CN = timezone(timedelta(hours=8))


def seconds(end, start):
    return (stamp(end) - stamp(start)).total_seconds() if end and start else None


def metrics(rows):
    pnl = [r["net_usd"] for r in rows]
    wins = sum(x > 0 for x in pnl)
    gain, loss = sum(max(0, x) for x in pnl), -sum(min(0, x) for x in pnl)
    return {"closed": len(rows), "wins": wins,
            "win_pct": round(100 * wins / len(rows), 2) if rows else None,
            "net_usd": round(sum(pnl), 4),
            "gross_usd": round(sum(r["gross_usd"] for r in rows), 4),
            "costs_usd": round(sum(r["costs_usd"] for r in rows), 4),
            "profit_factor": round(gain / loss, 3) if loss else None,
            "net_without_best": round(sum(pnl) - max(pnl), 4) if pnl else None}


def diagnose(state, report):
    events = state["events"]
    buys = {(e["arm"], e["key"], e["time"]): e for e in events
            if e["type"] == "fill" and e.get("side") == "buy"}
    sells = {(e["arm"], e["key"], e["time"]): e for e in events
             if e["type"] == "fill" and e.get("side") == "sell"}
    intents = {(e["arm"], e["key"], e["time"]): e for e in events
               if e["type"] == "intent" and e.get("side") == "sell"}
    rows = []
    for p in state["closed"]:
        buy = buys.get((p["arm"], p["key"], p["entry_at"]), {})
        sell = sells.get((p["arm"], p["key"], p["exit_at"]), {})
        intent = intents.get((p["arm"], p["key"], sell.get("created_at")), {})
        signal = buy.get("signal_snapshot", {})
        entry_q = buy.get("quote", {})
        exit_price = p["exit_quote"]["price_usd"]
        gross = p["quantity"] * (exit_price - p["entry_price_usd"])
        costs = p["buy_costs"]["total_usd"] + p["sell_costs"]["total_usd"]
        signal_price = signal.get("price_usd")
        row = {"arm": p["arm"], "chain": p["key"].split(":")[0], "key": p["key"],
               "symbol": p["symbol"], "entry_at": p["entry_at"], "exit_at": p["exit_at"],
               "net_usd": p["pnl_usd"], "gross_usd": gross, "costs_usd": costs,
               "reconciles": abs(gross - costs - p["pnl_usd"]) < 1e-6,
               "entry_price": p["entry_price_usd"], "exit_price": exit_price,
               "return_pct": (exit_price / p["entry_price_usd"] - 1) * 100,
               "holding_minutes": seconds(p["exit_at"], p["entry_at"]) / 60,
               "buy_delay_seconds": seconds(p["entry_at"], buy.get("created_at")),
               "sell_delay_seconds": seconds(p["exit_at"], sell.get("created_at")),
               "entry_vs_signal_pct": (p["entry_price_usd"] / signal_price - 1) * 100 if signal_price else None,
               "exit_reason": intent.get("reason", "unknown"),
               "liquidity": entry_q.get("liquidity_usd"), "mcap": signal.get("mcap"),
               "score": signal.get("entry_score"), "m5": signal.get("change_m5"),
               "h1": signal.get("change_h1"), "pair_age_hours": signal.get("pair_age_hours"),
               "buy_count5m": signal.get("buy_count5m"), "sell_count5m": signal.get("sell_count5m"),
               "day": stamp(p["entry_at"]).astimezone(CN).strftime("%Y-%m-%d")}
        rows.append(row)
    groups = {}
    for field in ("arm", "chain", "exit_reason", "day"):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[field]].append(row)
        groups[field] = {k: metrics(v) for k, v in grouped.items()}
    filters = {
        "baseline": lambda r: True,
        "liquidity_ge_30k": lambda r: (r["liquidity"] or 0) >= 30_000,
        "score_ge_80": lambda r: (r["score"] or 0) >= 80,
        "m5_le_15": lambda r: r["m5"] is not None and r["m5"] <= 15,
        "entry_drift_le_5pct": lambda r: r["entry_vs_signal_pct"] is not None and r["entry_vs_signal_pct"] <= 5,
        "buy_count_gt_sell_count": lambda r: r["buy_count5m"] is not None and r["sell_count5m"] is not None and r["buy_count5m"] > r["sell_count5m"],
    }
    comparisons = {}
    for arm in sorted({r["arm"] for r in rows}):
        sample = [r for r in rows if r["arm"] == arm]
        comparisons[arm] = {}
        for name, predicate in filters.items():
            selected = [r for r in sample if predicate(r)]
            comparisons[arm][name] = {"all": metrics(selected),
                "by_entry_day": {day: metrics([r for r in selected if r["day"] == day])
                                 for day in sorted({r["day"] for r in sample})}}
    delays = {}
    for field in ("buy_delay_seconds", "sell_delay_seconds", "entry_vs_signal_pct"):
        values = sorted(r[field] for r in rows if r[field] is not None)
        delays[field] = {"median": median(values) if values else None,
                         "max": max(values) if values else None,
                         "over_30": sum(v > 30 for v in values)}
    result = {"as_of": state["last_run_at"], "started_at": events[0]["time"],
              "mode": "read_only_diagnosis", "summary": metrics(rows), "groups": groups,
              "delays": delays, "filter_comparisons": comparisons,
              "worst": sorted(rows, key=lambda r: r["net_usd"])[:8],
              "best": sorted(rows, key=lambda r: -r["net_usd"])[:8],
              "open_count": len(state["positions"]),
              "unvalued_open": [{"symbol": p["symbol"], "arm": p["arm"], "entry_at": p["entry_at"],
                                  "debit_usd": p["debit_usd"], "status": p.get("valuation_status")}
                                 for p in state["positions"].values() if p.get("net_value_usd") is None],
              "quote_issue_counts": dict(Counter(report.get("quote_issues", {}).values())),
              "accounting_mismatches": sum(not r["reconciles"] for r in rows),
              "limitations": ["Closed trades only; missing valuations are not zero losses.",
                  "Filter comparisons reuse recorded exits, omit replacement trades and portfolio constraints.",
                  "Same-sample exploratory diagnosis, not out-of-sample profitability proof.",
                  "Cost assumptions are model inputs, not real measured execution fees."]}
    return result, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[2] / "outputs")
    args = parser.parse_args()
    state = json.loads((args.out_dir / "alpha-execution-state.json").read_text(encoding="utf-8-sig"))
    report = json.loads((args.out_dir / "alpha-execution-report.json").read_text(encoding="utf-8-sig"))
    result, rows = diagnose(state, report)
    target = args.out_dir / "alpha-execution-diagnosis.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    with (args.out_dir / "alpha-execution-diagnosis-trades.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["symbol"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({k: result[k] for k in ("as_of", "summary", "groups", "delays", "accounting_mismatches", "unvalued_open")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
