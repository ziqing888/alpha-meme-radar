"""Independent public-data paper challenger. No network, history or secret reads.

Call run_once(rows, Path("outputs"), now_iso); only execution-challenger/ is
written. Input is native execution quotes or fast-worker rows. volume5m must be
the source's actual five-minute volume; no other interval is converted to it.
Every observation needs its own fresh, advancing source timestamp and pinned
token/pool identity. Repeated fingerprints never provide confirmation.
Pending-data and risk flags retain only boolean presence, never raw payloads.
Fresh vetoes cancel pending buys before execution; exits still receive quotes.

Rules freeze on first use. Warmup is 30 minutes of experiment wall time. Evidence
uses only quotes received since startup, retained for one hour. Revival compares
against minute-spaced observations ending at least five minutes before signal.
Pullback requires peak -> low -> bounce -> higher low -> three rising samples,
with the final price reclaiming the bounce. Three samples means two rises,
selected backwards from the latest quote at least 20 seconds apart. No observed
price drop between those samples is allowed; intervening flat prices are OK.
This deliberately conservative pattern is not a profitability claim; reported
buy/sell counts do not establish dollar net inflow. Costs, exits, missing marks
and next-quote fills remain those of alpha_execution_audit.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from statistics import median

import alpha_execution_audit as execution
from alpha_wallet_forward_paper import exclusive_lock


DIRECTORY = "execution-challenger"
CONFIG = {
    "policy": "execution_challenger_v1", "starting_cash_usd": 1000.0,
    "buy_notional_usd": 35.0, "warmup_seconds": 1800,
    "history_seconds": 3600, "volume_spacing_seconds": 60,
    "volume_min_observations": 6, "volume_min_span_seconds": 900,
    "volume_exclude_recent_seconds": 300, "volume_multiplier": 2.0,
    "rising_samples": 3, "rising_spacing_seconds": 20,
    "old_age_hours": 168, "drawdown_min_pct": 10, "drawdown_max_pct": 40,
    "pullback_confirmation": "higher_low_and_bounce_reclaim",
    "min_buys": 5, "cooldown_seconds": 21600, "max_entry_drift": 0.05,
}
TEXT_FIELDS = (
    "chain", "chain_id", "contract_address", "token_address", "symbol",
    "pool_address", "pair_address", "quote_at", "quote_observed_at",
    "quote_status", "quote_fingerprint",
)
NUMBER_FIELDS = (
    "price_usd", "liquidity_usd", "liquidity", "pair_age_hours",
    "volume5m", "buy_count5m", "sell_count5m", "change_m5", "change_h1",
)
VETO_FIELDS = ("market_data_pending", "gmgn_risk_flags")


def public_quote(raw):
    """Whitelist scalar fields before anything can reach state or event snapshots."""
    row = {k: raw[k] for k in TEXT_FIELDS if isinstance(raw.get(k), str)}
    row.update({k: bool(raw.get(k)) for k in VETO_FIELDS})
    for k in NUMBER_FIELDS:
        if not isinstance(raw.get(k), bool):
            row[k] = execution.number(raw.get(k))
    for target, alias in (("pool_address", "pair_address"),
                          ("quote_at", "quote_observed_at"),
                          ("liquidity_usd", "liquidity")):
        if row.get(target) is None:
            row[target] = row.get(alias)
    key = execution.token_key(row)
    for primary, alias in (("chain", "chain_id"),
                           ("contract_address", "token_address")):
        if row.get(primary) and row.get(alias):
            other = {**row, primary: row[alias]}
            if execution.token_key(other) != key:
                row["quote_status"] = "conflicting_identity"
    if row.get("pool_address") and row.get("pair_address"):
        if execution.pool_key(row) != execution.pool_key({**row, "pool_address": row["pair_address"]}):
            row["quote_status"] = "conflicting_identity"
    if raw.get("quote_at") and raw.get("quote_observed_at"):
        if execution.timestamp(row.get("quote_at")) != execution.timestamp(row.get("quote_observed_at")):
            row["quote_status"] = "conflicting_timestamp"
    if key:
        row["chain"], row["contract_address"] = key.split(":", 1)
    row["pool_address"] = execution.pool_key(row)
    row.setdefault("quote_status", "fresh" if raw.get("quote_at") else "unavailable")
    proof = raw.get("verification")
    if isinstance(proof, dict):
        row["verification"] = {k: proof[k] for k in
            ("quote_at", "pool_address", "confirmed_at", "source")
            if isinstance(proof.get(k), str)}
    return row


def quote_input(rows):
    quotes = [public_quote(r) for r in rows if isinstance(r, dict)]
    groups = {}
    for q in quotes:
        identity = (execution.token_key(q), q["pool_address"], execution.timestamp(q.get("quote_at")))
        groups.setdefault(identity, []).append(q)
    for group in groups.values():
        # Duplicate public observations cannot hide a veto by input ordering.
        for field in VETO_FIELDS:
            present = any(q[field] for q in group)
            for q in group:
                q[field] = present
        values = {tuple(q.get(k) for k in NUMBER_FIELDS) for q in group}
        if len(values) > 1:
            for q in group:
                q["quote_status"] = "conflicting_observation"
    return {"execution_quotes": quotes, "execution_candidates": []}


def collect(state, accepted, now, exclusions):
    fresh = {}
    start = execution.timestamp(state["started_at"])
    for key, q in accepted.items():
        at = execution.timestamp(q["quote_at"])
        if q.get("quote_fingerprint"):
            state["fingerprints"].setdefault(key, {})[q["quote_fingerprint"]] = q["quote_at"]
        if at < start:
            exclusions["quote_before_experiment"] += 1
            continue
        history = state["observations"].setdefault(key, [])
        observation = {k: q.get(k) for k in
            ("quote_at", "price_usd", "pool_address", "volume5m")}
        observation["received_at"] = now.isoformat()
        history.append(observation)
        fresh[key] = q
    for key, history in list(state["observations"].items()):
        retained = [o for o in history if
            (now - execution.timestamp(o["quote_at"])).total_seconds() <= CONFIG["history_seconds"]]
        if retained:
            state["observations"][key] = retained
        else:
            del state["observations"][key]
    return fresh


def signal(q, history):
    """Return explicit candidate, evidence and first unmet requirement."""
    age = execution.number(q.get("pair_age_hours"))
    info = {"key": execution.token_key(q), "signal_at": q["quote_at"],
            "signal_price_usd": q["price_usd"]}
    if age is None or age < 0:
        return None, info, "missing_age"
    arm = "old_revival" if age >= CONFIG["old_age_hours"] else "pullback"
    info["arm"] = arm
    indices = []
    for i in range(len(history) - 1, -1, -1):
        if not indices or (execution.timestamp(history[indices[-1]]["quote_at"]) -
                           execution.timestamp(history[i]["quote_at"])).total_seconds() >= CONFIG["rising_spacing_seconds"]:
            indices.append(i)
            if len(indices) == CONFIG["rising_samples"]:
                break
    if len(indices) < CONFIG["rising_samples"]:
        return None, info, "missing_consecutive_rises"
    indices.reverse()
    tail = [history[i] for i in indices]
    # Sampling must not hide a lower low or an intervening spike and reversal.
    path = history[indices[0]:]
    if (not all(a["price_usd"] < b["price_usd"] for a, b in zip(tail, tail[1:]))
            or any(a["price_usd"] > b["price_usd"] for a, b in zip(path, path[1:]))):
        return None, info, "missing_consecutive_rises"
    info["rising_quotes"] = deepcopy(tail)
    buys, sells = q.get("buy_count5m"), q.get("sell_count5m")
    if buys is None or sells is None or sells < 0 or buys < CONFIG["min_buys"] or buys <= sells:
        return None, info, "missing_positive_buy_flow"
    candidate = {**q, "execution_arm": arm, "signal_at": q["quote_at"],
                 "signal_price_usd": q["price_usd"]}
    if arm == "old_revival":
        current_at = execution.timestamp(q["quote_at"])
        baseline = []
        for o in history[:-1]:
            at = execution.timestamp(o["quote_at"])
            if (current_at - at).total_seconds() < CONFIG["volume_exclude_recent_seconds"]:
                continue
            if o["volume5m"] is None or o["volume5m"] < 0:
                continue
            if not baseline or (at - execution.timestamp(baseline[-1]["quote_at"])).total_seconds() >= CONFIG["volume_spacing_seconds"]:
                baseline.append(o)
        span = ((execution.timestamp(baseline[-1]["quote_at"]) -
                 execution.timestamp(baseline[0]["quote_at"])).total_seconds() if baseline else 0)
        info.update(volume_observations=len(baseline), volume_span_seconds=span)
        if len(baseline) < CONFIG["volume_min_observations"] or span < CONFIG["volume_min_span_seconds"]:
            return None, info, "missing_volume_baseline"
        reference = median(o["volume5m"] for o in baseline)
        info.update(volume_median=reference, volume5m=q.get("volume5m"),
                    volume_baseline=deepcopy(baseline))
        if reference <= 0 or q.get("volume5m") is None or q["volume5m"] < CONFIG["volume_multiplier"] * reference:
            return None, info, "missing_volume_expansion"
        if q.get("change_h1") is None or q["change_h1"] <= 0:
            return None, info, "missing_positive_h1"
        candidate["old_meme_revival_active"] = True
    else:
        # Structure must precede the earliest selected confirmation sample.
        preceding = history[:indices[0]]
        if len(preceding) < 3:
            return None, info, "missing_pullback_structure"
        peak_index = max(range(len(preceding)), key=lambda i: (preceding[i]["price_usd"], i))
        peak = preceding[peak_index]
        dd = round((1 - q["price_usd"] / peak["price_usd"]) * 100, 10)
        info["drawdown_pct"] = dd
        if not CONFIG["drawdown_min_pct"] <= dd <= CONFIG["drawdown_max_pct"]:
            return None, info, "pullback_drawdown"
        after_peak = preceding[peak_index + 1:]
        if len(after_peak) < 2:
            return None, info, "missing_pullback_structure"
        low_index = min(range(len(after_peak)), key=lambda i: after_peak[i]["price_usd"])
        low = after_peak[low_index]
        bounces = after_peak[low_index + 1:]
        bounce = max(bounces, key=lambda o: o["price_usd"]) if bounces else None
        higher_low = tail[0]
        if not (bounce and low["price_usd"] < higher_low["price_usd"] < bounce["price_usd"]
                and q["price_usd"] >= bounce["price_usd"]):
            return None, info, "missing_higher_low_reclaim"
        info.update(peak=deepcopy(peak), low=deepcopy(low), bounce=deepcopy(bounce),
                    higher_low=deepcopy(higher_low))
        candidate["drawdown_from_peak_pct"] = -dd
    # Existing flow, momentum and execution gates remain additional constraints.
    reason = execution.candidate_gate(candidate, execution.timestamp(q["quote_at"]))
    if reason:
        return None, info, reason
    candidate["challenger_signal"] = deepcopy(info)
    return candidate, info, None


def cooling_down(engine, key, arm, now):
    for closed in engine["closed"]:
        if closed["key"] == key and closed["arm"] == arm:
            at = execution.timestamp(closed.get("exit_at"))
            if at is None or (now - at).total_seconds() < CONFIG["cooldown_seconds"]:
                return True
    return False


def advance(rows, state, now):
    now_iso = now.isoformat()
    engine = state["execution"]
    inputs = quote_input(rows)
    for q in inputs["execution_quotes"]:
        key, fingerprint = execution.token_key(q), q.get("quote_fingerprint")
        if (fingerprint and fingerprint in state["fingerprints"].get(key, {})
                and fingerprint != engine["quotes"].get(key, {}).get("quote_fingerprint")
                and q["quote_status"] == "fresh"):
            q["quote_status"] = "reused_quote_fingerprint"
    # Preview on quote-only copies; step must still see the original prior quotes
    # to make the identical selection and enforce next-observation fills.
    preview = {k: deepcopy(engine[k]) for k in ("quotes", "quarantine")}
    accepted, issues = execution.select_quotes(inputs, preview, now)
    exclusions = Counter(issues.values())
    fresh = collect(state, accepted, now, exclusions)
    warmup = (now - execution.timestamp(state["started_at"])).total_seconds() < CONFIG["warmup_seconds"]
    cancellations, blocked = [], set()
    for oid, order in list(engine["orders"].items()):
        if order["side"] != "buy":
            continue
        q = accepted.get(order["key"])
        price = execution.number(order.get("signal_snapshot", {}).get("signal_price_usd"))
        reason = "missing_intent_signal_price" if price is None or price <= 0 else None
        drift = None
        if q and q["pool_address"] == order["pool_address"] and execution.timestamp(q["quote_at"]) > execution.timestamp(order["created_at"]):
            drift = q["price_usd"] / price - 1 if price and price > 0 else None
            if drift is not None and q["price_usd"] > price * (1 + CONFIG["max_entry_drift"]):
                reason = "entry_price_drift"
        if (q and q["pool_address"] == order["pool_address"]
                and any(q[field] for field in VETO_FIELDS)):
            reason = "pending_data_or_risk_flags"
        if cooling_down(engine, order["key"], order["arm"], now):
            reason = "reentry_cooldown"
        if reason:
            del engine["orders"][oid]
            blocked.add(oid)
            exclusions[reason] += 1
            execution.emit(engine, cancellations, "cancel", now_iso, **order,
                           reason=reason, entry_drift=drift, signal_price_usd=price)
    signals = []
    for key, q in fresh.items():
        if warmup:
            exclusions["warmup"] += 1
            continue
        candidate, info, reason = signal(q, state["observations"][key])
        oid = f"{info.get('arm')}|{key}"
        if not reason and cooling_down(engine, key, info["arm"], now):
            reason = "reentry_cooldown"
        if not reason and oid in blocked:
            reason = "cancelled_this_observation"
        if not reason and (oid in engine["positions"] or oid in engine["orders"]):
            reason = "already_active"
        info["reason"] = reason
        info["eligible"] = reason is None
        signals.append(info)
        if reason:
            exclusions[reason] += 1
        else:
            inputs["execution_candidates"].append(candidate)
    result = execution.step(inputs, engine, now_iso)
    exclusions.update(g["reason"] for g in result["candidate_gates"])
    return {**result, "events": cancellations + result["events"],
            "policy": CONFIG["policy"], "config": deepcopy(state["config"]),
            "started_at": state["started_at"], "warmup": warmup,
            "observations_added": len(fresh),
            "observation_count": sum(map(len, state["observations"].values())),
            "signals": signals, "candidate_count": len(inputs["execution_candidates"]),
            "exclusion_counts": dict(exclusions),
            "profitability_conclusion": "insufficient_forward_evidence"}


def run_once(rows: list[dict], out_dir: Path, now_iso: str) -> dict:
    now = execution.timestamp(now_iso)
    if now is None:
        raise ValueError("now_iso requires a timezone-aware ISO timestamp")
    directory = Path(out_dir) / DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(directory / "writer.lock"):
        path = directory / "state.json"
        state = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                 {"schema_version": 1, "config": deepcopy(CONFIG),
                  "started_at": now.isoformat(), "observations": {}, "fingerprints": {},
                  "execution": execution.new_state()})
        if (state.get("schema_version") != 1 or state.get("config") != CONFIG
                or state["execution"]["assumptions"] != execution.ASSUMPTIONS
                or any(execution.ASSUMPTIONS[k] != CONFIG[k] for k in
                       ("starting_cash_usd", "buy_notional_usd"))):
            raise ValueError("Challenger fixed configuration mismatch; use a new experiment directory")
        last = execution.timestamp(state["execution"].get("last_run_at"))
        if last and now <= last:
            return {**state["last_report"], "events": [], "signals": [],
                    "observations_added": 0, "candidate_count": 0,
                    "status": "ignored_non_advancing_observation",
                    "exclusion_counts": {"non_advancing_run": 1}}
        result = advance(rows, state, now)
        state["last_report"] = result
        execution._atomic_json(path, state)
        execution._atomic_json(directory / "report.json", result)
        return result
