"""Chronological, evidence-classed replay for the chain-specific MEME strategy.

The module is deliberately file-output neutral until ``run_from_paths`` is
called with explicit report paths. Scanner prices are retained for research,
but only complete OKX executable quotes can contribute to net-return metrics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from alpha_chain_strategy import StrategyPolicy, classify_signal, token_identity


CN_TZ = timezone(timedelta(hours=8))
REQUIRED_COST_COMPONENTS = (
    "gas_usd",
    "buy_tax_pct",
    "sell_tax_pct",
    "route_loss_pct",
    "buy_impact_pct",
    "sell_impact_pct",
)
EXECUTABLE_SOURCES = {"okx", "okx_dex", "okx-dex-sdk", "okx_dex_sdk"}
REFERENCE_PROVENANCE = {"imported", "synthetic", "derived"}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed


def _iso(value: Any) -> str | None:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed is not None else None


def _history_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("rows")
    if isinstance(rows, Mapping):
        return [dict(row) for row in rows.values() if isinstance(row, Mapping)]
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, Mapping)]
    return []


def _normalize_chain(value: Any) -> str:
    raw = _text(value).lower()
    try:
        return StrategyPolicy.for_chain(raw).chain
    except ValueError:
        return raw


def _row_time(row: Mapping[str, Any]) -> datetime | None:
    snapshot = row.get("first_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    return _parse_time(
        snapshot.get("signal_stage_at")
        or snapshot.get("signal_timestamp")
        or row.get("first_seen_at")
    )


def freeze_input_watermark(
    history_path: Path,
    *,
    as_of: str | None = None,
    quote_log_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the source once and return a time-bounded immutable payload."""
    raw = history_path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("history payload must be an object")
    cutoff = _parse_time(as_of or payload.get("updated_at"))
    source_rows = payload.get("rows")

    def freeze_row(row: Mapping[str, Any]) -> dict[str, Any]:
        frozen_row = dict(row)
        observations = row.get("observations")
        if isinstance(observations, list):
            frozen_row["observations"] = [
                dict(observation)
                for observation in observations
                if isinstance(observation, Mapping)
                and (
                    cutoff is None
                    or (
                        _observation_time(observation) is not None
                        and _observation_time(observation) <= cutoff
                    )
                )
            ]
        return frozen_row

    kept: dict[str, Any] | list[Any]
    if isinstance(source_rows, Mapping):
        kept = {
            str(key): freeze_row(row)
            for key, row in source_rows.items()
            if isinstance(row, Mapping)
            and (cutoff is None or (_row_time(row) is not None and _row_time(row) <= cutoff))
        }
    elif isinstance(source_rows, list):
        kept = [
            freeze_row(row)
            for row in source_rows
            if isinstance(row, Mapping)
            and (cutoff is None or (_row_time(row) is not None and _row_time(row) <= cutoff))
        ]
    else:
        kept = []
    frozen = dict(payload)
    frozen["rows"] = kept
    frozen_count = len(kept)
    source_count = len(source_rows) if isinstance(source_rows, (Mapping, list)) else 0
    quote_offset = None
    quote_sha256 = None
    if quote_log_path is not None and quote_log_path.exists():
        quote_raw = quote_log_path.read_bytes()
        quote_offset = len(quote_raw)
        quote_sha256 = hashlib.sha256(quote_raw).hexdigest()
    watermark = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "source_row_count": source_count,
        "frozen_row_count": frozen_count,
        "updated_at": payload.get("updated_at"),
        "as_of": _iso(as_of or payload.get("updated_at")),
        "quote_log_byte_offset": quote_offset,
        "quote_log_sha256": quote_sha256,
    }
    return frozen, watermark


def _stage(row: Mapping[str, Any]) -> tuple[str, str | None]:
    snapshot = row.get("first_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    stage = _text(snapshot.get("signal_stage")).lower()
    stage = stage.replace("-", "_").replace(" ", "_")
    at = _iso(snapshot.get("signal_stage_at") or snapshot.get("signal_timestamp"))
    return stage, at


def _normalized_identity(row: Mapping[str, Any], stage: str) -> str:
    identity = token_identity(row)
    return f"{identity}:{stage}" if identity and stage else ""


def chronological_split(
    rows: Iterable[Mapping[str, Any]],
    *,
    holdout_fraction: float = 0.25,
) -> dict[str, Any]:
    """Deduplicate at first decision and isolate full calendar days."""
    if not 0 < holdout_fraction < 1:
        raise ValueError("holdout_fraction must be between zero and one")
    earliest: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for raw in rows:
        row = dict(raw)
        stage, stage_at = _stage(row)
        identity = _normalized_identity(row, stage)
        at = _parse_time(stage_at)
        if not identity or at is None:
            identity = f"not-evaluable:{len(earliest)}:{id(raw)}"
        prior = earliest.get(identity)
        if prior is not None:
            duplicate_count += 1
            if (_row_time(prior) or at) <= at:
                continue
        row["identity"] = token_identity(row)
        row["signal_stage"] = stage
        row["signal_stage_at"] = stage_at
        earliest[identity] = row
    ordered = sorted(earliest.values(), key=lambda row: _row_time(row) or datetime.max.replace(tzinfo=timezone.utc))
    days = sorted({_row_time(row).date().isoformat() for row in ordered if _row_time(row) is not None})
    holdout_days = max(1, math.ceil(len(days) * holdout_fraction)) if len(days) > 1 else 0
    test_days = set(days[-holdout_days:]) if holdout_days else set()
    train = [row for row in ordered if _row_time(row) and _row_time(row).date().isoformat() not in test_days]
    test = [row for row in ordered if _row_time(row) and _row_time(row).date().isoformat() in test_days]
    return {
        "train": train,
        "test": test,
        "train_days": sorted(set(days) - test_days),
        "test_days": sorted(test_days),
        "duplicate_count": duplicate_count,
    }


def _costs(mapping: Mapping[str, Any] | None) -> tuple[dict[str, float], list[str]]:
    values: dict[str, float] = {}
    missing: list[str] = []
    source = mapping if isinstance(mapping, Mapping) else {}
    for key in REQUIRED_COST_COMPONENTS:
        value = _number(source.get(key))
        if value is None or value < 0:
            missing.append(key)
        else:
            values[key] = value
    return values, missing


def _entry_fill(quote: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
    fields = {
        "buy_price_usd": _number(quote.get("buy_price_usd")),
        "bought_token_amount": _number(
            quote.get("bought_token_amount") or quote.get("actual_bought_token_amount")
        ),
        "spent_usd": _number(quote.get("spent_usd") or quote.get("actual_spent_usd")),
    }
    missing = [name for name, value in fields.items() if value is None or value <= 0]
    if not missing and not math.isclose(
        fields["buy_price_usd"] * fields["bought_token_amount"],
        fields["spent_usd"],
        rel_tol=1e-6,
        abs_tol=1e-9,
    ):
        missing.append("entry_fill_inconsistent")
    return ({name: float(value) for name, value in fields.items() if value is not None}, missing)


def _exit_proceeds(
    sell_price_usd: float,
    token_amount: float,
    costs: Mapping[str, float],
) -> float:
    percent = costs["sell_tax_pct"] + costs["route_loss_pct"] + costs["sell_impact_pct"]
    return max(0.0, sell_price_usd * token_amount * (1.0 - percent / 100.0) - costs["gas_usd"])


def _market_cap_bucket(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "unknown"
    if number < 10_000:
        return "under_10k"
    if number <= 100_000:
        return "10k_100k"
    if number <= 300_000:
        return "100k_300k"
    return "over_300k"


def _exit_policy(chain: str) -> dict[str, Any]:
    if chain == "bsc":
        return {"targets": ((2.0, 0.5),), "runner": 0.5, "pre_target_minutes": 90.0, "runner_minutes": 1440.0}
    return {
        "targets": ((2.0, 0.5), (3.0, 0.1), (5.0, 0.1)),
        "runner": 0.3,
        "pre_target_minutes": 90.0,
        "runner_minutes": 1440.0,
    }


def _entry_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = row.get("first_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    entry_quote = snapshot.get("entry_quote")
    entry_quote = entry_quote if isinstance(entry_quote, Mapping) else {}
    return {
        "snapshot_provenance": _text(snapshot.get("snapshot_provenance") or snapshot.get("provenance") or "unknown").lower(),
        "entry_source": _text(entry_quote.get("quote_source") or snapshot.get("quote_source") or "unknown").lower(),
        "entry_time_basis": _text(entry_quote.get("quote_time_basis") or snapshot.get("quote_time_basis") or "unknown").lower(),
    }


def _observation_time(row: Mapping[str, Any]) -> datetime | None:
    quote = row.get("executable_quote")
    quote = quote if isinstance(quote, Mapping) else {}
    return _parse_time(quote.get("quote_observed_at") or row.get("quote_observed_at") or row.get("seen_at"))


def _quote_binding_matches(
    quote: Mapping[str, Any],
    *,
    chain: str,
    contract_address: str,
    pool_address: str,
    notional_usd: float,
    expected_at: Any,
) -> bool:
    quote_chain = _normalize_chain(quote.get("chain") or quote.get("chain_id"))
    quote_contract = _text(
        quote.get("contract_address") or quote.get("token_address") or quote.get("address")
    ).lower()
    quote_pool = _text(quote.get("pool_address") or quote.get("pair_address")).lower()
    quote_notional = _number(quote.get("notional_usd"))
    quote_at = _iso(quote.get("quote_observed_at") or quote.get("quote_at"))
    expected_time = _iso(expected_at)
    return (
        quote_chain == chain
        and quote_contract == contract_address.lower()
        and (not pool_address or quote_pool == pool_address.lower())
        and quote_notional is not None
        and math.isclose(quote_notional, notional_usd, rel_tol=0.0, abs_tol=1e-9)
        and quote_at is not None
        and quote_at == expected_time
    )


def _quote_identity_matches(
    quote: Mapping[str, Any],
    *,
    chain: str,
    contract_address: str,
    pool_address: str,
    expected_at: Any,
) -> bool:
    quote_chain = _normalize_chain(quote.get("chain") or quote.get("chain_id"))
    quote_contract = _text(
        quote.get("contract_address") or quote.get("token_address") or quote.get("address")
    ).lower()
    quote_pool = _text(quote.get("pool_address") or quote.get("pair_address")).lower()
    quote_at = _iso(quote.get("quote_observed_at") or quote.get("quote_at"))
    return (
        quote_chain == chain
        and quote_contract == contract_address.lower()
        and (not pool_address or quote_pool == pool_address.lower())
        and quote_at is not None
        and quote_at == _iso(expected_at)
    )


def _quoted_exit_fraction(
    quote: Mapping[str, Any],
    *,
    original_token_amount: float,
    order_notional_usd: float,
) -> float | None:
    token_amount = _number(
        quote.get("sell_token_amount") or quote.get("token_amount") or quote.get("amount_token")
    )
    quoted_notional = _number(quote.get("notional_usd"))
    token_fraction = token_amount / original_token_amount if token_amount is not None and token_amount > 0 else None
    notional_fraction = quoted_notional / order_notional_usd if quoted_notional is not None and quoted_notional > 0 else None
    if token_fraction is not None and notional_fraction is not None and not math.isclose(
        token_fraction, notional_fraction, rel_tol=1e-6, abs_tol=1e-9
    ):
        return None
    fraction = token_fraction if token_fraction is not None else notional_fraction
    return fraction if fraction is not None and fraction > 0 else None


def replay_candidate(
    row: Mapping[str, Any],
    *,
    as_of: str | None = None,
    max_gap_seconds: float = 600.0,
) -> dict[str, Any]:
    """Replay one immutable signal using chronological executable evidence."""
    chain = _normalize_chain(row.get("chain") or row.get("chain_id"))
    stage, entry_at_text = _stage(row)
    identity = token_identity(row)
    snapshot = row.get("first_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    base = {
        "identity": identity,
        "chain": chain,
        "signal_stage": stage,
        "entry_at": entry_at_text,
        "first_mcap_usd": _number(snapshot.get("mcap") or snapshot.get("market_cap") or row.get("first_mcap_usd")),
        "market_cap_bucket": _market_cap_bucket(snapshot.get("mcap") or snapshot.get("market_cap") or row.get("first_mcap_usd")),
        "quote_provenance": _entry_provenance(row),
    }
    if not stage or not entry_at_text:
        return {
            **base,
            "evidence_class": "not_evaluable",
            "not_evaluable_reason": "missing_signal_stage_timestamp",
            "resolved": False,
            "net_return": None,
            "fills": [],
            "missing_cost_components": [],
        }
    entry_at = _parse_time(entry_at_text)
    first_seen = _iso(row.get("first_seen_at"))
    history_for_classification = {
        "first_seen_at": first_seen,
        "first_snapshot": {
            **snapshot,
            "price_usd": snapshot.get("price_usd") or row.get("first_price_usd"),
            "mcap": snapshot.get("mcap") or snapshot.get("market_cap") or row.get("first_mcap_usd"),
        },
    }
    decision = classify_signal({**snapshot, "chain": chain, "contract_address": row.get("contract_address"), "signal_stage": stage}, history_for_classification, entry_at_text)
    policy = StrategyPolicy.for_chain(chain)
    notional = policy.order_notional_usd
    entry_quote = snapshot.get("entry_quote")
    entry_quote = entry_quote if isinstance(entry_quote, Mapping) else {}
    entry_costs, entry_missing = _costs(entry_quote or snapshot.get("entry_costs"))
    entry_fill, entry_fill_missing = _entry_fill(entry_quote)
    entry_spent = entry_fill.get("spent_usd")
    original_token_amount = entry_fill.get("bought_token_amount")
    entry_total_cost = (
        entry_spent + entry_costs["gas_usd"]
        if entry_spent is not None and not entry_missing
        else None
    )
    entry_cost_units = (
        (entry_total_cost - notional) / notional
        if entry_total_cost is not None
        else None
    )
    provenance = base["quote_provenance"]
    reference_reasons: list[str] = []
    if provenance["snapshot_provenance"] == "unknown":
        reference_reasons.append("missing_provenance")
    if provenance["snapshot_provenance"] in REFERENCE_PROVENANCE:
        reference_reasons.append(f"{provenance['snapshot_provenance']}_snapshot")
    if provenance["entry_source"] not in EXECUTABLE_SOURCES or provenance["entry_time_basis"] != "executable_quote":
        reference_reasons.append("entry_not_executable")
    if entry_missing:
        reference_reasons.append("missing_entry_costs")
    if entry_fill_missing:
        reference_reasons.append("missing_or_invalid_entry_fill")
    pool_address = _text(snapshot.get("pool_address") or snapshot.get("pair_address")).lower()
    contract_address = _text(
        row.get("contract_address") or row.get("token_address") or row.get("address")
    ).lower()
    if not _quote_binding_matches(
        entry_quote,
        chain=chain,
        contract_address=contract_address,
        pool_address=pool_address,
        notional_usd=notional,
        expected_at=entry_at_text,
    ):
        reference_reasons.append("entry_quote_binding_mismatch")
    entry_executable = bool(decision.get("eligible")) and not reference_reasons

    raw_observations = [obs for obs in row.get("observations") or [] if isinstance(obs, Mapping)]
    ordered: list[tuple[datetime, Mapping[str, Any]]] = []
    for observation in raw_observations:
        at = _observation_time(observation)
        if at is not None and entry_at is not None and at >= entry_at:
            ordered.append((at, observation))
    ordered.sort(key=lambda item: item[0])
    deduped: list[tuple[datetime, Mapping[str, Any]]] = []
    for item in ordered:
        if deduped and item[0] == deduped[-1][0]:
            deduped[-1] = item
        else:
            deduped.append(item)
    gaps = [(right[0] - left[0]).total_seconds() for left, right in zip(deduped, deduped[1:])]
    observed_max_gap = max(gaps) if gaps else None
    if observed_max_gap is not None and observed_max_gap > max_gap_seconds:
        reference_reasons.append("observation_gap")

    exit_policy = _exit_policy(chain)
    remaining = 1.0
    realized_proceeds_usd = 0.0
    fills: list[dict[str, Any]] = []
    target_index = 0
    peak = 1.0
    resolved = False
    exit_reason = "censored"
    missing_costs: set[str] = set(entry_missing + entry_fill_missing)
    ambiguous_sparse_jump = False
    shadow_hits = {"3x": False, "5x": False} if chain == "bsc" else {}
    first_usable_at: datetime | None = None
    last_usable_at: datetime | None = None
    unsellable_after_entry = False

    for at, observation in deduped:
        quote = observation.get("executable_quote")
        quote = quote if isinstance(quote, Mapping) else {}
        source = _text(quote.get("quote_source") or observation.get("quote_source")).lower()
        time_basis = _text(quote.get("quote_time_basis") or observation.get("quote_time_basis")).lower()
        if source not in EXECUTABLE_SOURCES or time_basis != "executable_quote":
            reference_reasons.append("dexscreener_only_path")
            unsellable_after_entry = unsellable_after_entry or entry_executable
            continue
        expected_observation_at = observation.get("quote_observed_at") or observation.get("seen_at")
        if not _quote_identity_matches(
            quote,
            chain=chain,
            contract_address=contract_address,
            pool_address=pool_address,
            expected_at=expected_observation_at,
        ):
            reference_reasons.append("quote_binding_mismatch")
            unsellable_after_entry = unsellable_after_entry or entry_executable
            continue
        sell_price = _number(quote.get("sell_price_usd"))
        if sell_price is None or sell_price <= 0 or original_token_amount is None or entry_total_cost is None:
            reference_reasons.append("missing_executable_price")
            unsellable_after_entry = unsellable_after_entry or entry_executable
            continue
        costs, missing = _costs(quote)
        missing_costs.update(missing)
        if missing:
            reference_reasons.append("missing_exit_costs")
            unsellable_after_entry = unsellable_after_entry or entry_executable
            continue
        quoted_fraction = _quoted_exit_fraction(
            quote,
            original_token_amount=original_token_amount,
            order_notional_usd=notional,
        )
        if quoted_fraction is None or quoted_fraction > remaining + 1e-9:
            reference_reasons.append("quote_binding_mismatch")
            unsellable_after_entry = unsellable_after_entry or entry_executable
            continue
        quoted_token_amount = original_token_amount * quoted_fraction
        quote_proceeds = _exit_proceeds(sell_price, quoted_token_amount, costs)
        quoted_cost_basis = entry_total_cost * quoted_fraction
        multiple = quote_proceeds / quoted_cost_basis if quoted_cost_basis > 0 else 0.0
        first_usable_at = first_usable_at or at
        last_usable_at = at
        elapsed = (at - entry_at).total_seconds() / 60.0
        peak = max(peak, multiple)
        if chain == "bsc":
            shadow_hits["3x"] = shadow_hits["3x"] or multiple >= 3.0
            shadow_hits["5x"] = shadow_hits["5x"] or multiple >= 5.0
        if multiple <= 0.78 and remaining > 0:
            if not math.isclose(quoted_fraction, remaining, rel_tol=1e-6, abs_tol=1e-9):
                reference_reasons.append("exit_quote_binding_mismatch")
                unsellable_after_entry = unsellable_after_entry or entry_executable
                continue
            fills.append({"reason": "stop_loss", "fraction": round(remaining, 8), "token_amount": round(quoted_token_amount, 12), "multiple": round(multiple, 8), "proceeds_usd": round(quote_proceeds, 8), "at": at.isoformat()})
            realized_proceeds_usd += quote_proceeds
            remaining = 0.0
            resolved = True
            exit_reason = "stop_loss"
            break
        pending_targets = exit_policy["targets"][target_index:]
        crossed = [target for target in pending_targets if multiple >= target[0]]
        if crossed and remaining > 0:
            target, fraction = crossed[0]
            if len(crossed) > 1:
                ambiguous_sparse_jump = True
                reference_reasons.append("sparse_multi_target_jump")
            sold = min(remaining, fraction)
            if not math.isclose(quoted_fraction, sold, rel_tol=1e-6, abs_tol=1e-9):
                reference_reasons.append("partial_exit_quote_binding_mismatch")
                unsellable_after_entry = unsellable_after_entry or entry_executable
                continue
            fills.append({"reason": f"take_profit_{target:g}x", "fraction": round(sold, 8), "token_amount": round(quoted_token_amount, 12), "multiple": round(multiple, 8), "proceeds_usd": round(quote_proceeds, 8), "at": at.isoformat()})
            realized_proceeds_usd += quote_proceeds
            remaining -= sold
            target_index += 1
            continue
        if target_index > 0 and remaining > 0 and multiple <= peak * 0.60:
            if not math.isclose(quoted_fraction, remaining, rel_tol=1e-6, abs_tol=1e-9):
                reference_reasons.append("exit_quote_binding_mismatch")
                unsellable_after_entry = unsellable_after_entry or entry_executable
                continue
            fills.append({"reason": "trailing_stop", "fraction": round(remaining, 8), "token_amount": round(quoted_token_amount, 12), "multiple": round(multiple, 8), "proceeds_usd": round(quote_proceeds, 8), "at": at.isoformat()})
            realized_proceeds_usd += quote_proceeds
            remaining = 0.0
            resolved = True
            exit_reason = "trailing_stop"
            break
        max_minutes = exit_policy["pre_target_minutes"] if target_index == 0 else exit_policy["runner_minutes"]
        if elapsed >= max_minutes and remaining > 0:
            if not math.isclose(quoted_fraction, remaining, rel_tol=1e-6, abs_tol=1e-9):
                reference_reasons.append("exit_quote_binding_mismatch")
                unsellable_after_entry = unsellable_after_entry or entry_executable
                continue
            fills.append({"reason": "time_stop", "fraction": round(remaining, 8), "token_amount": round(quoted_token_amount, 12), "multiple": round(multiple, 8), "proceeds_usd": round(quote_proceeds, 8), "at": at.isoformat()})
            realized_proceeds_usd += quote_proceeds
            remaining = 0.0
            resolved = True
            exit_reason = "time_stop"
            break

    cutoff = _parse_time(as_of)
    horizon_minutes = exit_policy["pre_target_minutes"] if target_index == 0 else exit_policy["runner_minutes"]
    horizon_mature = resolved or (cutoff is not None and entry_at is not None and (cutoff - entry_at).total_seconds() / 60.0 >= horizon_minutes)
    near_entry = first_usable_at is not None and entry_at is not None and (first_usable_at - entry_at).total_seconds() <= max_gap_seconds
    if not near_entry:
        reference_reasons.append("missing_near_entry_quote")
    if entry_executable and first_usable_at is None:
        unsellable_after_entry = True
    if not horizon_mature:
        reference_reasons.append("horizon_not_mature")
    if not resolved:
        reference_reasons.append("censored_path")
    evidence_class = "executable" if not reference_reasons and not missing_costs and resolved else "reference_only"
    net_return = (
        round((realized_proceeds_usd - entry_total_cost) / notional, 8)
        if evidence_class == "executable" and entry_total_cost is not None
        else None
    )
    return {
        **base,
        "strategy_version": decision.get("strategy_version"),
        "entry_route": decision.get("entry_route"),
        "eligible": bool(decision.get("eligible")),
        "reject_reason": decision.get("reject_reason"),
        "evidence_class": evidence_class,
        "reference_reasons": sorted(set(reference_reasons)),
        "missing_cost_components": sorted(missing_costs),
        "entry_cost_units": round(entry_cost_units, 8) if entry_cost_units is not None else None,
        "entry_executable": entry_executable,
        "entry_buy_price_usd": entry_fill.get("buy_price_usd"),
        "entry_bought_token_amount": original_token_amount,
        "entry_spent_usd": entry_spent,
        "entry_total_cost_usd": entry_total_cost,
        "unsellable_after_entry": unsellable_after_entry,
        "resolved": resolved,
        "exit_reason": exit_reason,
        "fills": fills,
        "remaining_fraction": round(remaining, 8),
        "net_return": net_return,
        "peak_multiple": round(peak, 8),
        "hit_2x": peak >= 2.0,
        "hit_5x": peak >= 5.0,
        "hit_10x": peak >= 10.0,
        "shadow_hits": shadow_hits,
        "ambiguous_sparse_jump": ambiguous_sparse_jump,
        "observation_count": len(deduped),
        "max_observation_gap_seconds": observed_max_gap,
        "horizon_mature": horizon_mature,
    }


def _max_drawdown(values: Iterable[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 8)


def summarize_results(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    all_rows = list(rows)
    executable = [row for row in all_rows if row.get("evidence_class") == "executable" and row.get("resolved") is True and _number(row.get("net_return")) is not None]
    returns = [_number(row.get("net_return")) or 0.0 for row in sorted(executable, key=lambda row: _text(row.get("entry_at")))]
    gains = sum(value for value in returns if value > 0)
    losses = abs(sum(value for value in returns if value < 0))
    positive = sum(value > 0 for value in returns)
    peaks = [_number(row.get("peak_multiple")) or 0.0 for row in executable]
    winners = sorted((value for value in returns if value > 0), reverse=True)
    entered = [row for row in all_rows if row.get("entry_executable") is True]
    unsellable = [row for row in entered if row.get("unsellable_after_entry") is True]
    stress_rows = [
        row for row in entered
        if row.get("unsellable_after_entry") is True
        or (row.get("evidence_class") == "executable" and row.get("resolved") is True and _number(row.get("net_return")) is not None)
    ]
    stress_returns = [
        -1.0 if row.get("unsellable_after_entry") is True else (_number(row.get("net_return")) or 0.0)
        for row in sorted(stress_rows, key=lambda row: _text(row.get("entry_at")))
    ]
    return {
        "total_candidates": len(all_rows),
        "executable_resolved_count": len(executable),
        "resolved_coverage": round(len(executable) / len(all_rows), 8) if all_rows else 0.0,
        "reference_only_count": sum(row.get("evidence_class") == "reference_only" for row in all_rows),
        "not_evaluable_count": sum(row.get("evidence_class") == "not_evaluable" for row in all_rows),
        "unresolved_count": sum(
            row.get("evidence_class") != "not_evaluable" and row.get("resolved") is not True
            for row in all_rows
        ),
        "win_rate": round(positive / len(returns), 8) if returns else None,
        "two_x_rate": round(sum(value >= 2 for value in peaks) / len(peaks), 8) if peaks else None,
        "five_x_rate": round(sum(value >= 5 for value in peaks) / len(peaks), 8) if peaks else None,
        "ten_x_rate": round(sum(value >= 10 for value in peaks) / len(peaks), 8) if peaks else None,
        "net_return_units": round(sum(returns), 8) if returns else None,
        "profit_factor": round(gains / losses, 8) if losses > 0 else None,
        "maximum_drawdown_units": _max_drawdown(returns) if returns else None,
        "catastrophic_loss_rate": round(sum(value <= -0.8 for value in returns) / len(returns), 8) if returns else None,
        "top_winner_contribution": round(winners[0] / gains, 8) if winners and gains > 0 else None,
        "entry_executable_count": len(entered),
        "unsellable_after_entry_count": len(unsellable),
        "unsellable_after_entry_rate": round(len(unsellable) / len(entered), 8) if entered else None,
        "stress_evaluated_count": len(stress_returns),
        "stress_net_return_units": round(sum(stress_returns), 8) if stress_returns else None,
        "stress_loss_rate": round(sum(value < 0 for value in stress_returns) / len(stress_returns), 8) if stress_returns else None,
        "stress_catastrophic_loss_rate": round(sum(value <= -0.8 for value in stress_returns) / len(stress_returns), 8) if stress_returns else None,
        "stress_maximum_drawdown_units": _max_drawdown(stress_returns) if stress_returns else None,
    }


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_text(row.get(key)) or "unknown"].append(row)
    return {name: summarize_results(values) for name, values in sorted(grouped.items())}


def build_backtest(
    history: Mapping[str, Any],
    *,
    as_of: str | None = None,
    watermark: Mapping[str, Any] | None = None,
    holdout_fraction: float = 0.25,
) -> dict[str, Any]:
    rows = _history_rows(history)
    split = chronological_split(rows, holdout_fraction=holdout_fraction)
    deduped = split["train"] + split["test"]
    replayed = [replay_candidate(row, as_of=as_of or history.get("updated_at")) for row in deduped]
    chain_v2_rows = [row for row in replayed if row.get("eligible")]
    payload_bytes = json.dumps(history, sort_keys=True, ensure_ascii=False).encode("utf-8")
    effective_watermark = dict(watermark or {
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "byte_length": len(payload_bytes),
        "source_row_count": len(rows),
        "frozen_row_count": len(rows),
        "updated_at": history.get("updated_at"),
        "as_of": _iso(as_of or history.get("updated_at")),
        "quote_log_byte_offset": None,
        "quote_log_sha256": None,
    })
    return {
        "generated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "watermark": effective_watermark,
        "universe": summarize_results(replayed),
        "comparison": {
            "current": {
                "status": "not_evaluable",
                "reason": "policy_not_implemented",
            },
            "aggregate_discovery": {
                "status": "not_evaluable",
                "reason": "policy_not_implemented",
            },
            "chain_v2": {
                "status": "implemented",
                "summary": summarize_results(chain_v2_rows),
            },
        },
        "groups": {
            "by_chain": _group(replayed, "chain"),
            "by_signal_stage": _group(replayed, "signal_stage"),
            "by_market_cap_bucket": _group(replayed, "market_cap_bucket"),
        },
        "holdout": {
            "policy_selection_source": "train_only",
            "train_days": split["train_days"],
            "test_days": split["test_days"],
            "train": summarize_results([row for row in replayed if _text(row.get("entry_at"))[:10] in set(split["train_days"])]),
            "test": summarize_results([row for row in replayed if _text(row.get("entry_at"))[:10] in set(split["test_days"])]),
            "duplicate_count": split["duplicate_count"],
        },
        "rejection_reasons": dict(Counter(_text(row.get("reject_reason")) or "none" for row in replayed)),
        "evidence_classes": dict(Counter(_text(row.get("evidence_class")) or "unknown" for row in replayed)),
        "rows": replayed,
        "limitations": [
            "Missing immutable signal stage or stage timestamp is not evaluable.",
            "Imported, synthetic and DexScreener-only paths are reference-only.",
            "Executable metrics require complete gas, tax, route-loss and impact components.",
            "A sparse observation crossing multiple targets is reference-only and fills at most one target.",
            "Censored and reference-only rows never contribute to headline net-return metrics.",
        ],
    }


def markdown_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Chain-specific MEME strategy backtest",
        "",
        "## Evidence classes",
        "",
    ]
    for name, count in sorted((result.get("evidence_classes") or {}).items()):
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Comparison", "", "| Policy | Candidates | Executable resolved | Net units | Unsellable after entry | Stress net units |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for name, policy_result in (result.get("comparison") or {}).items():
        if policy_result.get("status") != "implemented":
            lines.append(f"| {name} | not evaluable | not evaluable | not evaluable | not evaluable | not evaluable |")
            continue
        summary = policy_result.get("summary") or {}
        lines.append(f"| {name} | {summary.get('total_candidates', 0)} | {summary.get('executable_resolved_count', 0)} | {summary.get('net_return_units')} | {summary.get('unsellable_after_entry_rate')} | {summary.get('stress_net_return_units')} |")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in result.get("limitations") or [])
    return "\n".join(lines) + "\n"


def run_from_paths(
    history_path: Path,
    json_out: Path,
    md_out: Path,
    *,
    as_of: str | None = None,
    quote_log_path: Path | None = None,
) -> dict[str, Any]:
    frozen, watermark = freeze_input_watermark(history_path, as_of=as_of, quote_log_path=quote_log_path)
    result = build_backtest(frozen, as_of=watermark["as_of"], watermark=watermark)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(markdown_report(result), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument("--quote-log", type=Path)
    args = parser.parse_args()
    markdown_out = args.markdown_out or args.out.with_suffix(".md")
    result = run_from_paths(args.history, args.out, markdown_out, as_of=args.as_of, quote_log_path=args.quote_log)
    print(json.dumps({"watermark": result["watermark"], "evidence_classes": result["evidence_classes"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
