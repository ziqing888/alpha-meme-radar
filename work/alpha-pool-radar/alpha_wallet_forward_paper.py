"""Forward-only wallet confirmation cohort; local state, no network or orders.

The fast worker is the sole writer. First receipt means first observation by this
paper worker, not the on-chain timestamp or an inferred upstream fetch timestamp.
Receipt evidence and strict execution state commit together; journals are derived.
"""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

import alpha_execution_audit as execution
from alpha_smart_money_evidence import assess_smart_money, parse_timestamp
from alpha_wallet_quality import qualified_wallets, wallet_identity


DIRECTORY = "profitable-wallet-paper"
POLICY = "wallet_forward_v1"
FRESH_SECONDS = 900


def identity(chain, address):
    result = wallet_identity(chain, address)
    if not result:
        return None
    return ({"sol": "solana", "eth": "ethereum"}.get(result[0], result[0]), result[1])


def normalize_event(event):
    wallets = event.get("wallets") or []
    if len(wallets) != 1:
        return None
    token = identity(event.get("chain"), event.get("token_address"))
    wallet = identity(event.get("chain"), wallets[0])
    if not token or not wallet:
        return None
    tx = str(event.get("tx_hash") or "")
    pattern = r"[1-9A-HJ-NP-Za-km-z]{64,88}" if token[0] == "solana" else r"0x[0-9a-fA-F]{64}"
    if not re.fullmatch(pattern, tx):
        return None
    tx = tx if token[0] == "solana" else tx.lower()
    stamp = parse_timestamp(event.get("observed_at"))
    amount = execution.number(event.get("amount_usd"))
    if not stamp or amount is None or amount <= 0 or event.get("direction") not in {"buy", "sell"}:
        return None
    normalized = {**deepcopy(event), "chain": token[0], "token_address": token[1],
                  "wallets": [wallet[1]], "tx_hash": tx, "observed_at": stamp.isoformat(),
                  "amount_usd": amount}
    core = {k: normalized[k] for k in ("chain", "token_address", "wallets", "tx_hash",
                                      "observed_at", "amount_usd", "direction")}
    key = hashlib.sha256(json.dumps([*token, wallet[1], tx]).encode()).hexdigest()
    return key, core, normalized


def collect(rows, state, now_iso, baseline, wallet_snapshot=None):
    now = execution.timestamp(now_iso)
    current_profiles = {}
    for row in rows:
        evidence = row.get("smart_money_evidence") or {}
        profiles = qualified_wallets({"wallets": evidence.get("qualified_wallet_profiles") or []}, now_iso)
        current_profiles.update({identity(p["chain"], p["address"]): p for p in profiles})
    if wallet_snapshot is not None:
        current_profiles = {identity(p["chain"], p["address"]): p
                            for p in qualified_wallets(wallet_snapshot, now_iso)}
    for row in rows:
        for event in (row.get("smart_money_evidence") or {}).get("events") or []:
            if not isinstance(event, dict) or not (normalized := normalize_event(event)):
                continue
            key, core, event = normalized
            existing = state["receipts"].get(key)
            if existing:
                existing["conflicted"] |= existing["core"] != core or bool(event.get("conflicting_reports"))
                # Newly discovered cluster links can remove apparent independence.
                links = set(existing["event"].get("linked_cluster_ids") or [])
                links.update(event.get("linked_cluster_ids") or [])
                existing["event"]["linked_cluster_ids"] = sorted(links)
                continue
            source_at = execution.timestamp(event["observed_at"])
            profile = current_profiles.get((event["chain"], event["wallets"][0]))
            eligible = bool(not baseline and execution.timestamp(state["started_at"]) <= source_at <= now
                            and (now - source_at).total_seconds() <= FRESH_SECONDS
                            and profile and event.get("flow_eligible") and event.get("provenance")
                            and not event.get("conflicting_reports"))
            state["receipts"][key] = {"core": core, "event": event, "first_received_at": now_iso,
                "baseline": baseline, "eligible_at_receipt": eligible, "profile_at_receipt": deepcopy(profile),
                "conflicted": bool(event.get("conflicting_reports")),
                "receipt_lag_seconds": (now - source_at).total_seconds()}
    return current_profiles


def evidence_for(row, state, current_profiles, now_iso):
    token = identity(row.get("chain"), row.get("contract_address"))
    now = execution.timestamp(now_iso)
    events, profiles, receipts = [], {}, {}
    for key, receipt in state["receipts"].items():
        event = receipt["event"]
        if (event["chain"], event["token_address"]) != token:
            continue
        source_at = execution.timestamp(event["observed_at"])
        if not 0 <= (now - source_at).total_seconds() <= FRESH_SECONDS:
            continue
        event = deepcopy(event)
        event["conflicting_reports"] = receipt["conflicted"]
        events.append(event)
        wallet_key = (event["chain"], event["wallets"][0])
        if (receipt["eligible_at_receipt"] and not receipt["conflicted"]
                and wallet_key in current_profiles):
            # First eligibility stays frozen; current qualification is a separate check.
            profiles[wallet_key] = current_profiles[wallet_key]
            if event["direction"] == "buy":
                receipts[key] = receipt
        elif event["direction"] == "buy":
            # A later profile must never qualify an earlier unqualified buy.
            event["flow_eligible"] = False
    enriched = {**row, "smart_money_evidence": {"events": events,
                "qualified_wallet_profiles": list(profiles.values())}}
    return enriched, receipts


def concentration_ok(row):
    top10 = execution.number(row.get("top10_holder_pct"))
    largest = execution.number(row.get("max_holder_pct"))
    return top10 is not None and largest is not None and 0 < largest <= top10 < 40 and largest < 18


def step(rows, state, now_iso, baseline=False, wallet_snapshot=None):
    current = collect(rows, state, now_iso, baseline, wallet_snapshot)
    candidates, checks, gates, normalized_rows = [], {}, [], []
    active_tokens = {p["key"] for p in [*state["execution"]["positions"].values(),
                                      *state["execution"]["orders"].values()]}
    for raw in rows:
        token = identity(raw.get("chain"), raw.get("contract_address"))
        if not token:
            continue
        row = {**raw, "chain": token[0], "contract_address": token[1]}
        normalized_rows.append(row)
        row, receipts = evidence_for(row, state, current, now_iso)
        verdict = assess_smart_money(row, now_iso)
        key = execution.token_key(row)
        state.setdefault("token_first_seen", {}).setdefault(key, now_iso)
        anchors = [state["token_first_seen"][key], state.setdefault("discovery_anchors", {}).get(key),
                   row.get("first_seen_at"), (row.get("replay") or {}).get("first_seen_at"), row.get("watch_first_seen_at")]
        state["discovery_anchors"][key] = min(stamp for value in anchors
            if (stamp := execution.timestamp(value)) and stamp <= execution.timestamp(now_iso)).isoformat()
        checks[key] = verdict
        reason = ""
        if not verdict["buy_support"]:
            reason = verdict["confirmation_status"]
        elif not concentration_ok(row):
            reason = "missing_or_high_concentration"
        if reason:
            gates.append({"key": key, "reason": reason})
            continue
        relevant = {k: r for k, r in receipts.items() if r["event"]["wallets"][0] in verdict["buy_wallets"]}
        if key in active_tokens or not relevant or not any(not r.get("consumed_by") for r in relevant.values()):
            continue
        # Use real receipt time, never renew a cached signal with a new quote time.
        row["signal_at"] = max(r["first_received_at"] for r in relevant.values())
        row["wallet_receipt_ids"] = sorted(relevant)
        row["entry_policy"] = POLICY
        row.pop("entry_score", None)
        row.pop("old_meme_revival_score", None)
        price = execution.number(row.get("price_usd"), 0)
        peak = max(state["execution"]["signal_peaks"].get(key, 0), price)
        row["drawdown_from_peak_pct"] = (price / peak - 1) * 100 if peak > 0 else 0
        row["first_seen_at"] = state["discovery_anchors"][key]
        row["paper_first_seen_at"] = state["token_first_seen"][key]
        age = execution.number(row.get("pair_age_hours"), -1)
        row["execution_arm"] = "old_revival" if age >= 168 else "pullback" if row["drawdown_from_peak_pct"] <= -10 else "first_discovery"
        row["old_meme_revival_active"] = age >= 168
        candidates.append(row)
    cancelled = []
    row_map = {execution.token_key(r): r for r in normalized_rows}
    for oid, order in list(state["execution"]["orders"].items()):
        if order["side"] != "buy":
            continue
        check = checks.get(order["key"], {})
        # Quote outages stay pending, but disappearing wallet support or fresh
        # adverse flow cannot fill yesterday's intent from its frozen snapshot.
        supported = (len(check.get("non_overlapping_buy_groups", [])) >= 2
                     and check.get("buy_usd", 0) > check.get("sell_usd", 0))
        row = row_map.get(order["key"], {})
        current_signal = {**order["signal_snapshot"], **{k: row.get(k) for k in (
            "change_m5", "change_h1", "buy_count5m", "sell_count5m", "volume5m", "mcap", "pair_age_hours")}}
        fresh_gate = execution.candidate_gate(current_signal, execution.timestamp(now_iso)) if check.get("market_fresh") else None
        if (not supported or check.get("sell_dominance") or not concentration_ok(row)
                or row.get("gmgn_risk_flags") or row.get("market_data_pending")
                or row.get("watch_status") in {"invalidated", "expired"} or fresh_gate
                or (check.get("market_fresh") and not check.get("market_liquid"))):
            del state["execution"]["orders"][oid]
            execution.emit(state["execution"], cancelled, "cancel", now_iso, **order,
                           reason="wallet_support_or_risk_invalidated")
    result = execution.step({"meme_rows": normalized_rows, "execution_candidates": candidates}, state["execution"], now_iso)
    result["events"] = cancelled + result["events"]
    for event in result["events"]:
        if event["type"] == "intent" and event["side"] == "buy":
            for key in event["signal_snapshot"].get("wallet_receipt_ids", []):
                state["receipts"][key].setdefault("consumed_by", event["id"])
    state["execution"]["realized_pnl_usd"] = result["realized_pnl_usd"]
    receipts = list(state["receipts"].values())
    return {**result, "policy": POLICY, "mode": "forward_spot_paper_only", "started_at": state["started_at"],
            "receipt_time_basis": "first observation by paper worker; upper bound on upstream receipt",
            "receipt_count": len(receipts), "baseline_count": sum(r["baseline"] for r in receipts),
            "eligible_buy_receipts": sum(r["eligible_at_receipt"] and not r["conflicted"] and r["event"]["direction"] == "buy" for r in receipts),
            "closed_count": len(state["execution"]["closed"]),
            "wallet_gates": dict(Counter(g["reason"] for g in gates)),
            "candidate_count": len(candidates), "profitability_conclusion": "insufficient_forward_evidence"}


def _journals(directory, state):
    for name, records in (("receipts", [{"id": k, **r} for k, r in state["receipts"].items()]),
                          ("events", state["execution"]["events"])):
        path = directory / (name + ".jsonl")
        temporary = path.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(r, ensure_ascii=True, allow_nan=False) + "\n" for r in records), encoding="utf-8")
        temporary.replace(path)


@contextmanager
def exclusive_lock(path):
    # The OS releases the byte lock even on process termination. The file stays.
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


def run_once(rows, out_dir, now_iso, *, wallet_snapshot=None):
    now = execution.timestamp(now_iso)
    if not now:
        raise ValueError("now_iso must have a timezone")
    now_iso = now.isoformat()
    directory = Path(out_dir) / DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(directory / "writer.lock"):
        path = directory / "state.json"
        baseline = not path.exists()
        if baseline:
            state = {"schema_version": 1, "policy": POLICY, "started_at": now_iso,
                     "receipts": {}, "execution": execution.new_state()}
        else:
            state = json.loads(path.read_text(encoding="utf-8"))
        if (state.get("schema_version") != 1 or state.get("policy") != POLICY
                or state["execution"].get("assumptions") != execution.ASSUMPTIONS):
            raise ValueError("Forward paper state or fixed assumptions mismatch")
        last = execution.timestamp(state["execution"].get("last_run_at"))
        if last and now <= last:
            _journals(directory, state)
            execution._atomic_json(directory / "report.json", state["last_report"])
            return {**state["last_report"], "events": [], "status": "ignored_non_advancing_observation"}
        result = step(deepcopy(rows), state, now_iso, baseline, wallet_snapshot)
        state["last_report"] = result
        execution._atomic_json(path, state)
        _journals(directory, state)
        execution._atomic_json(directory / "report.json", result)
        return result
