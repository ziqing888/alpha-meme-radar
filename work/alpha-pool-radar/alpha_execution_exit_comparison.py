"""Frozen-entry exploratory exit comparison, never a live or portfolio worker."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from alpha_execution_audit import costs
from alpha_execution_diagnosis import stamp


def pool_identity(value):
    return value.lower() if isinstance(value, str) and value.startswith("0x") else value


def simulate(position, quotes, mode):
    entry = stamp(position["entry_at"])
    price = position["entry_price_usd"]
    original = position["quantity"]
    remaining = original
    cash = 0.0
    high = price
    partial = False
    intent = None
    fills = []
    last = entry
    for q in quotes:
        at = stamp(q["quote_at"])
        if at <= entry or at <= last or pool_identity(q["pool_address"]) != pool_identity(position["pool_address"]):
            continue
        last = at
        if at.timestamp() > entry.timestamp() + 6 * 3600:
            break
        px = q["price_usd"]
        if px <= 0 or q["liquidity_usd"] < 8000:
            continue
        if intent is not None:
            qty, reason, requested_at = intent
            notional = qty * px
            if notional / q["liquidity_usd"] > 0.01:
                continue
            fee = costs(notional, q)["total_usd"]
            cash += notional - fee
            remaining -= qty
            fills.append({"at": q["quote_at"], "reason": reason, "fraction": qty / original,
                          "price": px, "cost_usd": fee, "delay_seconds": (at-requested_at).total_seconds()})
            intent = None
            if remaining <= original * 1e-10:
                return {"closed": True, "net_usd": cash - position["debit_usd"], "fills": fills}
            partial = True
        high = max(high, px)
        ret = px / price - 1
        held = (at - entry).total_seconds()
        if ret <= -0.22:
            intent = (remaining, "stop_loss", at)
        elif not partial and ret >= 1:
            intent = (remaining if mode == "full_tp" else min(remaining, original * 0.8), "take_profit", at)
        elif partial and px / high - 1 <= -0.35:
            intent = (remaining, "trailing_stop", at)
        elif held >= 5400 and (mode == "full_tp" or not partial):
            intent = (remaining, "time_stop", at)
    return {"closed": False, "net_usd": None, "cash_received_usd": cash,
            "remaining_fraction": remaining / original, "fills": fills}


def load_quotes(path, positions):
    windows = defaultdict(list)
    for p in positions:
        windows[p["key"]].append(stamp(p["entry_at"]).timestamp())
    result = defaultdict(list)
    invalid = 0
    size = path.stat().st_size
    with path.open("rb") as f:
        while f.tell() < size:
            line = f.readline()
            if not line:
                break
            try:
                q = json.loads(line)
                chain = str(q.get("chain", "")).lower()
                token = str(q.get("contract_address") or q.get("token_address") or "").lower()
                key = f"{chain}:{token}"
                if key not in windows or q.get("quote_status") != "fresh":
                    continue
                at = q.get("quote_observed_at") or q.get("quote_at")
                ts = stamp(at).timestamp()
                if not any(start < ts <= start + 6 * 3600 for start in windows[key]):
                    continue
                result[key].append({"quote_at": at, "pool_address": q.get("pair_address") or q.get("pool_address"),
                                    "price_usd": float(q["price_usd"]),
                                    "liquidity_usd": float(q.get("liquidity_usd") or q.get("liquidity") or 0)})
            except (ValueError, TypeError, KeyError):
                invalid += 1
    for rows in result.values():
        rows.sort(key=lambda q: stamp(q["quote_at"]))
    return result, invalid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[2] / "outputs")
    args = parser.parse_args()
    state = json.loads((args.out_dir / "alpha-execution-state.json").read_text(encoding="utf-8-sig"))
    positions = state["closed"]
    quotes, invalid = load_quotes(args.out_dir / "alpha-fast-quotes.jsonl", positions)
    rows = []
    for p in positions:
        rows.append({"symbol": p["symbol"], "key": p["key"], "arm": p["arm"],
                     "entry_at": p["entry_at"], "recorded_net": p["pnl_usd"],
                     "full_tp": simulate(p, quotes[p["key"]], "full_tp"),
                     "runner": simulate(p, quotes[p["key"]], "runner")})
    groups = {}
    for arm in sorted({r["arm"] for r in rows}):
        sample = [r for r in rows if r["arm"] == arm]
        common = [r for r in sample if r["full_tp"]["closed"] and r["runner"]["closed"]]
        groups[arm] = {"recorded_closed_count": len(sample), "common_completed": len(common),
                       "full_tp_unresolved": sum(not r["full_tp"]["closed"] for r in sample),
                       "runner_unresolved": sum(not r["runner"]["closed"] for r in sample),
                       "common_full_tp_net": sum(r["full_tp"]["net_usd"] for r in common),
                       "common_runner_net": sum(r["runner"]["net_usd"] for r in common),
                       "common_recorded_net": sum(r["recorded_net"] for r in common)}
    result = {"as_of": state["last_run_at"], "groups": groups, "invalid_quote_lines": invalid,
              "mode": "exploratory_frozen_entry_exit_comparison", "trades": rows,
              "limitations": ["Recorded closed entries only; no new entries, capital scheduling or true out-of-sample test.",
                  "At most six hours of fresh recorded quotes per entry; unresolved exits remain unresolved.",
                  "Different quote cadence from original scanner; full_tp is a control, not exact ledger replication.",
                  "Depth/fees/tax are the existing assumed paper cost model; no real execution proof."]}
    (args.out_dir / "alpha-execution-exit-comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"groups": groups, "invalid_quote_lines": invalid}))


if __name__ == "__main__":
    main()
