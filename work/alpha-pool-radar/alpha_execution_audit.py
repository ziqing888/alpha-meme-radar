"""Independent, offline strict spot-paper execution and causal ledger audit.

API: run_once(report, out_dir, now_iso). Only alpha-execution-* files are written.
Single writer; a lock fails closed. The state is authoritative; its event journal
rebuilds the separate JSONL and report after an interrupted write.

Input contract:
  execution_candidates (or meme_potential_rows/meme_shadow_rows): token rows with
    chain, contract_address, signal_at (timezone-aware ISO), execution_arm
    (first_discovery/pullback/old_revival), entry_score, pair_age_hours, mcap,
    change_m5, change_h1. First discovery also needs first_seen_at (or
    replay.first_seen_at); pullback needs drawdown_from_peak_pct (negative);
    revival needs old_meme_revival_active and old_meme_revival_score.
  execution_quotes: list of {chain, contract_address, pool_address, quote_at,
    price_usd, liquidity_usd}. quote_at must be the source quote timestamp,
    NEVER report generation/scan time. Price and liquidity must describe that
    same token, pool and observation. Symbols never identify a quote.
  A jump >= 3x in either direction needs a later quote with verification:
    {quote_at: <quarantined quote timestamp>, pool_address: <same pool>,
     confirmed_at: <later timezone-aware ISO>, source: <independent evidence>}.
    Price must agree within 10%. Repeated anomalous quotes alone do not verify.
  legacy_ledger: optional read-only old state dict, audited in the report.
  Fast-worker adapter: meme_rows quotes map quote_observed_at -> quote_at,
    pair_address -> pool_address, liquidity -> liquidity_usd, and require
    quote_status='fresh'. quote_fingerprint must identify a source observation;
    an unchanged fingerprint is never a new execution opportunity.
    Without execution_candidates, meme_rows can generate independent signals:
    >=5 buys, buys>sells and >=$500 volume5m; positive 5m momentum. Historical
    radar scores are not refreshed merely by receiving a new quote. Discovery
    requires first_seen_at/replay.first_seen_at/watch_first_seen_at. Pullbacks
    use only the strict module's previously accepted price peaks. Old revival
    requires >=7 days pool age and fresh positive flow/momentum.

Assumptions are fixed, recorded in state, and not calibrated on future returns.
Both sides pay 0.3% fee + 1% assumed token tax + 0.5% base slippage, plus
notional/(liquidity/2) depth impact and $0.12 gas. Orders over 1% of liquidity
remain pending. These are execution stress assumptions, not measured pool fees.
Each $35 buy notional plus maximum costs reserves full-loss capital: $300 total,
$100 per arm. No daily order cap. Exit: -22%, +100%, or 90 minutes, observed
only; ALL fills require a strictly later quote than the decision observation.
Duplicate observations may reuse the last accepted quote for valuation only,
with its original timestamp and <=30s age. Missing/stale/quarantined valuations
are null; cached marks never create fills or price-triggered exit intents.

CLI --ledger STATE [--history HISTORY] prints an audit/causal replay to stdout.
No network, account, refresh, UI, messaging, or legacy-state dependencies.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path


ARMS = ("first_discovery", "pullback", "old_revival")
ASSUMPTIONS = {
    "starting_cash_usd": 1000.0, "buy_notional_usd": 35.0,
    "full_loss_budget_usd": 300.0, "arm_full_loss_budget_usd": 100.0,
    "fee_rate": 0.003, "tax_rate": 0.01, "base_slippage_rate": 0.005,
    "gas_usd": 0.12, "max_depth_fraction": 0.01,
    "min_liquidity_usd": 8000.0, "quote_max_age_seconds": 30,
    "signal_max_age_seconds": 300, "buy_intent_ttl_seconds": 900,
    "jump_ratio": 3.0, "stop_return": -0.22, "take_profit_return": 1.0,
    "hold_seconds": 5400, "risk_basis": "full buy debit, including pending orders",
    "depth_model": "notional / (liquidity_usd / 2), applied each side",
    "tax_model": "assumed 1% each side; not measured", "daily_order_cap": None,
}
PREFIX = "alpha-execution"
EXECUTION_QUOTE_RETENTION_SECONDS = 6 * 60 * 60
MAX_PERSISTED_QUOTES = 2000
PERSISTED_QUOTE_FIELDS = (
    "chain", "contract_address", "pool_address", "pair_address", "quote_at",
    "price_usd", "liquidity_usd", "quote_fingerprint", "quote_status",
)
ALIASES = {"bnb": "bsc", "56": "bsc", "bsc-mainnet": "bsc",
           "sol": "solana", "501": "solana", "base-mainnet": "base",
           "8453": "base", "eth": "ethereum", "1": "ethereum"}
EVM = {"bsc", "base", "ethereum", "robinhood", "arbitrum", "polygon"}


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def number(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def token_key(row):
    chain = str(row.get("chain") or row.get("chain_id") or "").strip().lower()
    chain = ALIASES.get(chain, chain)
    address = str(row.get("contract_address") or row.get("token_address") or "").strip()
    if chain in EVM:
        address = address.lower()
    return f"{chain}:{address}" if chain and address else ""


def pool_key(row):
    pool = str(row.get("pool_address") or "").strip()
    return pool.lower() if token_key(row).split(":")[0] in EVM else pool


def quote_error(row, now):
    if not token_key(row):
        return "missing_token_identity"
    at = timestamp(row.get("quote_at"))
    if not at:
        return "missing_quote_timestamp"
    if not pool_key(row):
        return "missing_pool_identity"
    age = (now - at).total_seconds()
    if age < 0 or age > ASSUMPTIONS["quote_max_age_seconds"]:
        return "future_quote" if age < 0 else "stale_quote"
    if number(row.get("price_usd"), 0) <= 0:
        return "invalid_price"
    if number(row.get("liquidity_usd"), 0) < ASSUMPTIONS["min_liquidity_usd"]:
        return "missing_or_low_liquidity"
    return None


def quote_overlay(report):
    overlay = report.get("execution_quotes")
    if overlay is None:
        overlay = [{**row, "quote_at": row.get("quote_observed_at"),
                    "pool_address": row.get("pair_address"),
                    "liquidity_usd": row.get("liquidity"),
                    "quote_status": row.get("quote_status", "unavailable")}
                   for row in report.get("meme_rows") or []]
    return overlay


def select_quotes(report, state, now):
    groups, errors = {}, {}
    overlay = quote_overlay(report)
    for raw in overlay:
        key = token_key(raw)
        status = raw.get("quote_status", "fresh")
        error = f"source_quote_{status}" if status != "fresh" else quote_error(raw, now)
        if error:
            errors[key or "unknown"] = error
        else:
            q = {**raw, "pool_address": pool_key(raw),
                 "price_usd": number(raw["price_usd"]),
                 "liquidity_usd": number(raw["liquidity_usd"])}
            groups.setdefault(key, []).append(q)
    accepted = {}
    for key, quotes in groups.items():
        previous = state["quotes"].get(key)
        if previous:
            quotes = [q for q in quotes if q["pool_address"] == previous["pool_address"]]
            if not quotes:
                errors[key] = "pool_changed_pending_verification"
                continue
        # Choose by identity, then freshness/depth; never by price or ticker.
        quotes.sort(key=lambda q: (timestamp(q["quote_at"]), q["liquidity_usd"],
                                   q["pool_address"]), reverse=True)
        q = quotes[0]
        if any(x["pool_address"] == q["pool_address"] and
               timestamp(x["quote_at"]) == timestamp(q["quote_at"]) and
               x["price_usd"] != q["price_usd"] for x in quotes):
            errors[key] = "conflicting_quote"
            continue
        if previous and q.get("quote_fingerprint") and q["quote_fingerprint"] == previous.get("quote_fingerprint"):
            errors[key] = ("duplicate_quote_fingerprint" if
                           (q["price_usd"], q["liquidity_usd"]) ==
                           (previous["price_usd"], previous["liquidity_usd"])
                           else "conflicting_quote")
            continue
        if previous and timestamp(q["quote_at"]) == timestamp(previous["quote_at"]) and q["price_usd"] != previous["price_usd"]:
            errors[key] = "conflicting_quote"
            continue
        if previous and timestamp(q["quote_at"]) <= timestamp(previous["quote_at"]):
            errors[key] = "non_advancing_quote"
            continue
        if previous:
            ratio = q["price_usd"] / previous["price_usd"]
            if max(ratio, 1 / ratio) >= ASSUMPTIONS["jump_ratio"]:
                suspect = state["quarantine"].get(key)
                proof = q.get("verification") or {}
                confirmed = timestamp(proof.get("confirmed_at"))
                verified = (suspect and proof.get("quote_at") == suspect["quote_at"]
                            and proof.get("pool_address") == q["pool_address"]
                            and bool(proof.get("source")) and confirmed
                            and timestamp(suspect["quote_at"]) < confirmed <= now
                            and timestamp(q["quote_at"]) >= confirmed
                            and abs(q["price_usd"] / suspect["price_usd"] - 1) <= 0.1)
                if not verified:
                    state["quarantine"].setdefault(key, q)
                    errors[key] = "abnormal_jump_pending_verification"
                    continue
        state["quarantine"].pop(key, None)
        state["quotes"][key] = q
        accepted[key] = q
        errors.pop(key, None)
    return accepted, errors


def candidate_gate(row, now):
    arm = row.get("execution_arm")
    if arm not in ARMS:
        return "missing_execution_arm"
    at = timestamp(row.get("signal_at"))
    if not at or not 0 <= (now - at).total_seconds() <= ASSUMPTIONS["signal_max_age_seconds"]:
        return "missing_or_stale_signal_timestamp"
    if row.get("market_data_pending") or row.get("gmgn_risk_flags"):
        return "pending_data_or_risk_flags"
    if row.get("watch_status") in {"expired", "invalidated"} and arm == "first_discovery":
        return "expired_first_discovery"
    flow = (number(row.get("buy_count5m"), 0) >= 5 and
            number(row.get("buy_count5m"), 0) > number(row.get("sell_count5m"), math.inf) and
            number(row.get("volume5m"), 0) >= 500 and number(row.get("change_m5"), 0) > 0)
    if number(row.get("entry_score"), -1) < 60 and not flow:
        return "insufficient_entry_score"
    age = number(row.get("pair_age_hours"), -1)
    m5, h1 = number(row.get("change_m5")), number(row.get("change_h1"))
    if m5 is None or h1 is None or not -10 <= m5 <= 30 or not -20 <= h1 <= 80:
        return "momentum_gate"
    if arm == "first_discovery":
        first = timestamp(row.get("first_seen_at") or (row.get("replay") or {}).get("first_seen_at"))
        if not first or not 0 <= (now - first).total_seconds() <= 2700:
            return "first_discovery_window"
        if not 0 <= age <= 6 or not 10000 <= number(row.get("mcap"), 0) <= 300000:
            return "first_discovery_age_or_mcap"
        if row.get("old_meme_revival_active") or row.get("watch_ticket_stage") == "late":
            return "wrong_first_discovery_stage"
    elif arm == "pullback":
        dd = number(row.get("drawdown_from_peak_pct"), 0)
        if not 0 <= age < 168 or not -40 <= dd <= -10 or m5 <= 0:
            return "pullback_reclaim_gate"
    elif not (age >= 168 and row.get("old_meme_revival_active") is True
              and (number(row.get("old_meme_revival_score"), 0) >= 60 or flow) and h1 > 0):
        return "old_revival_gate"
    return None


def costs(notional, q):
    impact = notional / (q["liquidity_usd"] / 2)
    rate = sum(ASSUMPTIONS[k] for k in ("fee_rate", "tax_rate", "base_slippage_rate"))
    return {"notional_usd": notional, "fee_usd": notional * ASSUMPTIONS["fee_rate"],
            "tax_usd": notional * ASSUMPTIONS["tax_rate"],
            "slippage_usd": notional * ASSUMPTIONS["base_slippage_rate"],
            "depth_usd": notional * impact, "gas_usd": ASSUMPTIONS["gas_usd"],
            "total_usd": notional * (rate + impact) + ASSUMPTIONS["gas_usd"]}


def reserve():
    n = ASSUMPTIONS["buy_notional_usd"]
    rate = sum(ASSUMPTIONS[k] for k in ("fee_rate", "tax_rate", "base_slippage_rate"))
    return n * (1 + rate + 2 * ASSUMPTIONS["max_depth_fraction"]) + ASSUMPTIONS["gas_usd"]


def new_state():
    return {"schema_version": 1, "assumptions": deepcopy(ASSUMPTIONS),
            "cash_usd": ASSUMPTIONS["starting_cash_usd"], "realized_pnl_usd": 0.0, "positions": {},
            "orders": {}, "quotes": {}, "quarantine": {}, "events": [],
            "closed": [], "signal_peaks": {}, "last_run_at": None}


def compact_quote(row):
    key = token_key(row)
    chain, address = key.split(":", 1) if ":" in key else ("", "")
    pool = pool_key(row)
    compact = {field: row.get(field) for field in PERSISTED_QUOTE_FIELDS
               if row.get(field) is not None}
    compact.update({"chain": chain, "contract_address": address,
                    "pool_address": pool, "price_usd": number(row.get("price_usd")),
                    "liquidity_usd": number(row.get("liquidity_usd"))})
    if row.get("pair_address") or pool:
        compact["pair_address"] = row.get("pair_address") or pool
    return compact


def compact_execution_state(state, now_iso):
    """Bound rebuildable quote snapshots while retaining every active token."""
    now = timestamp(now_iso)
    if not now:
        raise ValueError("now_iso requires a timezone-aware ISO timestamp")
    pinned = set((state.get("quarantine") or {}).keys())
    for section in ("positions", "orders"):
        pinned.update(item.get("key") for item in (state.get(section) or {}).values()
                      if item.get("key"))
    ranked = []
    for key, raw in (state.get("quotes") or {}).items():
        observed = timestamp(raw.get("quote_at"))
        age = (now - observed).total_seconds() if observed else float("inf")
        if key in pinned or 0 <= age <= EXECUTION_QUOTE_RETENTION_SECONDS:
            ranked.append((key, raw, observed))
    ranked.sort(key=lambda item: item[2] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    pinned_rows = [item for item in ranked if item[0] in pinned]
    ordinary_rows = [item for item in ranked if item[0] not in pinned]
    remaining = max(0, MAX_PERSISTED_QUOTES - len(pinned_rows))
    state["quotes"] = {key: compact_quote(raw)
                       for key, raw, _ in [*pinned_rows, *ordinary_rows[:remaining]]}
    state["quarantine"] = {key: compact_quote(raw)
                           for key, raw in (state.get("quarantine") or {}).items()}
    return state


def signal_rows(report, state, quotes):
    explicit = report.get("execution_candidates")
    if explicit is not None:
        return explicit
    rows = report.get("meme_rows")
    if rows is None:
        return (report.get("meme_potential_rows") or []) + (report.get("meme_shadow_rows") or [])
    candidates = []
    for raw in rows:
        q = quotes.get(token_key(raw))
        if not q:
            continue
        row = deepcopy(raw)
        if row.get("signal_at"):
            candidates.append(row)
            continue
        # These are new flow decisions on fresh source observations, not reused
        # radar scores given a fabricated current signal timestamp.
        for field in ("entry_score", "old_meme_revival_score"):
            row.pop(field, None)
        row["signal_at"] = q["quote_at"]
        row["first_seen_at"] = (row.get("first_seen_at") or
            (row.get("replay") or {}).get("first_seen_at") or row.get("watch_first_seen_at"))
        peak = state["signal_peaks"][token_key(raw)]
        row["drawdown_from_peak_pct"] = (q["price_usd"] / peak - 1) * 100
        age = number(row.get("pair_age_hours"), -1)
        if age >= 168:
            row["execution_arm"] = "old_revival"
            row["old_meme_revival_active"] = True
        elif row["drawdown_from_peak_pct"] <= -10:
            row["execution_arm"] = "pullback"
        else:
            row["execution_arm"] = "first_discovery"
        candidates.append(row)
    return candidates


def emit(state, events, kind, now, **fields):
    event = {"id": len(state["events"]) + 1, "type": kind, "time": now, **fields}
    state["events"].append(event)
    events.append(event)


def summary(state):
    arms = {}
    for arm in ARMS:
        positions = [p for p in state["positions"].values() if p["arm"] == arm]
        orders = [o for o in state["orders"].values() if o["arm"] == arm and o["side"] == "buy"]
        arms[arm] = {"open": len(positions), "pending_buys": len(orders),
                     "closed": sum(p["arm"] == arm for p in state["closed"]),
                     "realized_pnl_usd": sum(p["pnl_usd"] for p in state["closed"] if p["arm"] == arm),
                     "risk_reserved_usd": sum(p["debit_usd"] for p in positions) + len(orders) * reserve()}
    pending = sum(p.get("net_value_usd") is None for p in state["positions"].values())
    valued = sum(p.get("net_value_usd") or 0 for p in state["positions"].values())
    return {"cash_usd": state["cash_usd"], "arms": arms, "pending_valuations": pending,
            "equity_usd": None if pending else state["cash_usd"] + valued,
            "known_net_position_value_usd": valued,
            "realized_pnl_usd": sum(p["pnl_usd"] for p in state["closed"]),
            "positions": deepcopy(list(state["positions"].values())),
            "pending_orders": deepcopy(list(state["orders"].values()))}


def step(report, state, now_iso):
    now = timestamp(now_iso)
    if not now:
        raise ValueError("now_iso requires a timezone-aware ISO timestamp")
    now_iso = now.isoformat()
    last = timestamp(state["last_run_at"])
    if last and now <= last:
        return {"status": "ignored_non_advancing_observation", "events": [], **summary(state)}
    events = []
    quotes, errors = select_quotes(report, state, now)
    for key, q in quotes.items():
        state["signal_peaks"][key] = max(state["signal_peaks"].get(key, 0), q["price_usd"])
    # Only intents from earlier observations may fill in this pass.
    for oid, order in list(state["orders"].items()):
        if order["side"] == "buy" and (now - timestamp(order["created_at"])).total_seconds() > ASSUMPTIONS["buy_intent_ttl_seconds"]:
            del state["orders"][oid]
            emit(state, events, "cancel", now_iso, **order, reason="buy_intent_expired")
            continue
        q = quotes.get(order["key"])
        if not q or timestamp(q["quote_at"]) <= timestamp(order["created_at"]):
            continue
        if q["pool_address"] != order["pool_address"]:
            continue
        if order["side"] == "buy":
            gate = candidate_gate(order["signal_snapshot"], now)
            if gate:
                del state["orders"][oid]
                emit(state, events, "cancel", now_iso, **order, reason=gate)
                continue
        p = state["positions"].get(oid)
        notional = ASSUMPTIONS["buy_notional_usd"] if order["side"] == "buy" else p["quantity"] * q["price_usd"]
        if notional / q["liquidity_usd"] > ASSUMPTIONS["max_depth_fraction"]:
            errors[order["key"]] = "depth_limit_pending"
            continue
        cost = costs(notional, q)
        fill_details = {}
        if order["side"] == "buy":
            debit = notional + cost["total_usd"]
            if debit > state["cash_usd"]:
                continue
            state["cash_usd"] -= debit
            fill_details = {"debit_usd": debit, "quantity": notional / q["price_usd"]}
            state["positions"][oid] = {"key": order["key"], "arm": order["arm"],
                "symbol": order["symbol"], "quantity": notional / q["price_usd"],
                "entry_price_usd": q["price_usd"], "entry_at": now_iso,
                "debit_usd": debit, "buy_costs": cost, "pool_address": q["pool_address"]}
        else:
            proceeds = notional - cost["total_usd"]
            state["cash_usd"] += proceeds
            fill_details = {"proceeds_usd": proceeds, "pnl_usd": proceeds - p["debit_usd"],
                            "quantity": p["quantity"]}
            state["closed"].append({**p, "exit_at": now_iso, "exit_quote": q,
                                    "sell_costs": cost, "pnl_usd": proceeds - p["debit_usd"]})
            del state["positions"][oid]
        del state["orders"][oid]
        emit(state, events, "fill", now_iso, **order, quote=q, costs=cost, **fill_details)
    for oid, p in state["positions"].items():
        q = quotes.get(p["key"])
        mark = q
        if not mark and errors.get(p["key"]) in {"duplicate_quote_fingerprint", "non_advancing_quote"}:
            cached = state["quotes"].get(p["key"])
            if (cached and p["key"] not in state["quarantine"] and
                    cached["pool_address"] == p["pool_address"] and not quote_error(cached, now)):
                mark = cached
        p["net_value_usd"] = None
        p["valuation_quote_at"] = None
        p["valuation_quote_age_seconds"] = None
        p["valuation_price_usd"] = None
        p["valuation_quote_reused"] = False
        p["valuation_status"] = errors.get(p["key"], "missing_quote_pending_valuation")
        if mark:
            notional = p["quantity"] * mark["price_usd"]
            if notional / mark["liquidity_usd"] <= ASSUMPTIONS["max_depth_fraction"]:
                p["net_value_usd"] = notional - costs(notional, mark)["total_usd"]
                p["valuation_status"] = "valued"
                p["valuation_quote_at"] = mark["quote_at"]
                p["valuation_quote_age_seconds"] = (now - timestamp(mark["quote_at"])).total_seconds()
                p["valuation_price_usd"] = mark["price_usd"]
                p["valuation_quote_reused"] = q is None
            else:
                p["valuation_status"] = "depth_limit_pending"
        if oid in state["orders"]:
            continue
        held = (now - timestamp(p["entry_at"])).total_seconds()
        ret = q["price_usd"] / p["entry_price_usd"] - 1 if q else None
        reason = "time_stop" if held >= ASSUMPTIONS["hold_seconds"] else None
        if ret is not None and ret <= ASSUMPTIONS["stop_return"]:
            reason = "stop_loss"
        elif ret is not None and ret >= ASSUMPTIONS["take_profit_return"]:
            reason = "take_profit"
        if reason:
            order = {"key": p["key"], "arm": p["arm"], "symbol": p["symbol"],
                     "side": "sell", "created_at": now_iso, "pool_address": p["pool_address"]}
            state["orders"][oid] = order
            emit(state, events, "intent", now_iso, **order, reason=reason)
    rows = signal_rows(report, state, quotes)
    gates = []
    for row in rows:
        key, arm = token_key(row), row.get("execution_arm")
        error = candidate_gate(row, now)
        q = quotes.get(key)
        if not key or error or not q:
            gates.append({"key": key, "arm": arm, "reason": error or errors.get(key, "missing_quote")})
            continue
        if timestamp(row["signal_at"]) > timestamp(q["quote_at"]):
            gates.append({"key": key, "arm": arm, "reason": "quote_predates_signal"})
            continue
        oid = f"{arm}|{key}"
        # One active position per token per arm, and no same-observation re-entry.
        if oid in state["orders"] or oid in state["positions"] or any(
                p["key"] == key and p["arm"] == arm and
                (now - timestamp(p["exit_at"])).total_seconds() < 21600 for p in state["closed"]):
            continue
        budgets = summary(state)["arms"]
        risk = sum(a["risk_reserved_usd"] for a in budgets.values())
        pending_cash = sum(reserve() for o in state["orders"].values() if o["side"] == "buy")
        if (risk + reserve() > ASSUMPTIONS["full_loss_budget_usd"] or
                budgets[arm]["risk_reserved_usd"] + reserve() > ASSUMPTIONS["arm_full_loss_budget_usd"] or
                pending_cash + reserve() > state["cash_usd"]):
            gates.append({"key": key, "arm": arm, "reason": "risk_budget"})
            continue
        order = {"key": key, "arm": arm, "symbol": row.get("symbol", ""),
                 "side": "buy", "created_at": now_iso, "pool_address": q["pool_address"],
                 "signal_snapshot": deepcopy(row)}
        state["orders"][oid] = order
        emit(state, events, "intent", now_iso, **order, signal_at=row["signal_at"])
    state["last_run_at"] = now_iso
    compact_execution_state(state, now_iso)
    return {"status": "ok" if quotes else "pending_quotes", "at": now_iso,
            "accepted_quotes": len(quotes), "quote_issues": errors, "candidate_gates": gates,
            "events": events, "assumptions": state["assumptions"], **summary(state)}


def audit_ledger(ledger, history=None):
    """Rank original claims; missing evidence never becomes a validated return."""
    positions = (ledger.get("closed_positions") or []) + (ledger.get("open_positions") or [])
    biggest = sorted(positions, key=lambda p: abs(number(p.get("realized_pnl_usd"), 0)), reverse=True)[:10]
    findings, previous = [], {}
    for event in sorted(ledger.get("events") or [], key=lambda e: timestamp(e.get("time")) or datetime.min.replace(tzinfo=timezone.utc)):
        kind = event.get("type", "")
        if kind == "open":
            previous[event.get("key")] = number(event.get("price_usd"), 0)
            continue
        if not ("exit" in kind or "profit" in kind or "loss" in kind or "trailing" in kind or event.get("sold_fraction")):
            continue
        flags = []
        at = timestamp(event.get("time"))
        quote = event.get("quote") or {}
        if not at or quote_error(quote, at):
            flags.append("exit_without_valid_quote_evidence")
        if any(word in str(event.get("reason", "")) for word in ("missing", "last", "\u7f3a\u5931", "\u4e0d\u53ef\u7528", "\u6700\u540e")):
            flags.append("missing_quote_fallback_exit")
        price = number(event.get("price_usd"), 0)
        prior = previous.get(event.get("key"), 0)
        if price > 0 and prior > 0 and max(price / prior, prior / price) >= ASSUMPTIONS["jump_ratio"]:
            flags.append("abnormal_execution_jump")
        if price > 0:
            previous[event.get("key")] = price
        if flags:
            findings.append({"key": event.get("key"), "time": event.get("time"),
                             "pnl_usd": event.get("pnl_usd"), "flags": flags})
    history_jumps = []
    for key, row in ((history or {}).get("rows") or {}).items():
        prior = None
        for obs in sorted(row.get("observations") or [], key=lambda o: timestamp(o.get("seen_at")) or datetime.min.replace(tzinfo=timezone.utc)):
            price = number(obs.get("price_usd"), 0)
            if price > 0 and timestamp(obs.get("seen_at")):
                if prior and max(price / prior, prior / price) >= ASSUMPTIONS["jump_ratio"]:
                    history_jumps.append({"key": key, "seen_at": obs["seen_at"], "ratio": price / prior})
                prior = price
    return {"status": "audit_only_unverified_legacy_claims", "position_count": len(positions),
            "largest_pnl_claims": [{k: p.get(k) for k in ("key", "symbol", "realized_pnl_usd", "entry_price_usd", "high_price_usd", "closed_at")} for p in biggest],
            "findings": sorted(findings, key=lambda f: abs(number(f["pnl_usd"], 0)), reverse=True),
            "history_jumps": sorted(history_jumps, key=lambda j: max(j["ratio"], 1 / j["ratio"]), reverse=True)[:50],
            "validated_pnl_usd": None}


def causal_replay(history):
    """Frozen-rule walk-forward, in memory. Never backfill latest features.

    Accept a list of {observed_at, report} or legacy {rows: {key: ...}}.
    Legacy observations require their own quote_at/pool/liquidity and optional
    `candidate` snapshot with signal_at. seen_at is availability time only.
    """
    frames, skipped = [], Counter()
    if isinstance(history, list):
        frames = history
    else:
        grouped = {}
        for row in (history.get("rows") or {}).values():
            for obs in row.get("observations") or []:
                at = obs.get("seen_at")
                if not timestamp(at):
                    skipped["missing_observation_timestamp"] += 1
                    continue
                frame = grouped.setdefault(at, {"execution_quotes": [], "execution_candidates": []})
                frame["execution_quotes"].append({"chain": row.get("chain"),
                    "contract_address": row.get("contract_address"), **obs})
                if isinstance(obs.get("candidate"), dict):
                    frame["execution_candidates"].append(obs["candidate"])
        frames = [{"observed_at": at, "report": r} for at, r in grouped.items()]
    groups = {}
    for frame in frames:
        at = timestamp(frame.get("observed_at"))
        if not at:
            skipped["missing_observation_timestamp"] += 1
            continue
        combined = groups.setdefault(at, {"execution_quotes": []})
        report = frame.get("report") or {}
        combined["execution_quotes"].extend(quote_overlay(report))
        for section in ("execution_candidates", "meme_rows", "meme_potential_rows", "meme_shadow_rows"):
            if section in report and report[section] is not None:
                combined.setdefault(section, []).extend(report[section])
    state, accepted, checkpoints = new_state(), 0, []
    for at, report in sorted(groups.items()):
        for q in report["execution_quotes"]:
            error = quote_error(q, at)
            if error:
                skipped[error] += 1
        result = step(report, state, at.isoformat())
        accepted += result.get("accepted_quotes", 0)
        if result["events"]:
            checkpoints.append({"at": at.isoformat(), **summary(state)})
    return {"status": "ok" if accepted else "no_usable_data", "accepted_quotes": accepted,
            "skipped": dict(skipped), "observation_count": len(groups),
            "method": "chronological frozen-rule walk-forward; no interpolation or forced final exit",
            "checkpoints": checkpoints, "events": state["events"], **summary(state)}


def _atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


@contextmanager
def exclusive_lock(path):
    """Process-scoped writer lock; stale lock files are harmless after a crash."""
    with path.open("a+b") as handle:
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_once(report: dict, out_dir: Path, now_iso: str) -> dict:
    """Consume caller-supplied fresh quotes, persist only independent artifacts."""
    if not timestamp(now_iso):
        raise ValueError("now_iso requires a timezone-aware ISO timestamp")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = out_dir / f"{PREFIX}.lock"
    with exclusive_lock(lock):
        path = out_dir / f"{PREFIX}-state.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else new_state()
        if state.get("schema_version") != 1 or state.get("assumptions") != ASSUMPTIONS:
            raise ValueError("Independent state schema/assumptions mismatch; use a new out_dir")
        result = step(report, state, now_iso)
        state["realized_pnl_usd"] = result["realized_pnl_usd"]
        if isinstance(report.get("legacy_ledger"), dict):
            result["legacy_audit"] = audit_ledger(report["legacy_ledger"])
        _atomic_json(path, state)
        event_path = out_dir / f"{PREFIX}-events.jsonl"
        temp = event_path.with_suffix(".jsonl.tmp")
        temp.write_text("".join(json.dumps(e, allow_nan=False) + "\n" for e in state["events"]), encoding="utf-8")
        temp.replace(event_path)
        _atomic_json(out_dir / f"{PREFIX}-report.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--history", type=Path)
    args = parser.parse_args()
    history = json.loads(args.history.read_text(encoding="utf-8")) if args.history else None
    result = {}
    if args.ledger:
        ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
        if args.events:
            ledger["events"] = [json.loads(line) for line in args.events.read_text(encoding="utf-8").splitlines() if line.strip()]
        result["audit"] = audit_ledger(ledger, history if isinstance(history, dict) else None)
    if history is not None:
        result["replay"] = causal_replay(history)
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
