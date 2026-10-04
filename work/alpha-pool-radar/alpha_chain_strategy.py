"""Pure chain-specific signal classification for the MEME monitor."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


CHAIN_ALIASES = {
    "56": "bsc",
    "bnb": "bsc",
    "bsc-mainnet": "bsc",
    "4663": "robinhood",
}
STRATEGY_VERSION = "chain_v2"
SUPPORTED_EVM_CHAINS = {"bsc", "robinhood"}
PROMOTED_SIGNAL_STAGES = {"aggregate_early_bird", "aggregate_confirmation"}
EVM_CONTRACT_RE = re.compile(r"0x[0-9a-f]{40}")


@dataclass(frozen=True)
class StrategyPolicy:
    chain: str
    live_signal_stage: str
    entry_route: str
    order_notional_usd: float
    first_mcap_min_usd: float
    first_mcap_max_usd: float
    max_entry_delay_seconds: float
    max_markup_from_first: float
    min_liquidity_usd: float
    max_round_trip_loss_pct: float
    max_buy_price_impact_pct: float | None
    max_sell_price_impact_pct: float | None
    min_sellable_cycles: int
    requires_observed_sell: bool
    max_open_positions: int
    max_open_notional_usd: float
    daily_realized_loss_stop_usd: float
    min_rank_score: float
    max_pair_age_hours: float
    min_independent_sources: int
    allowed_recommendation_buckets: frozenset[str]

    @classmethod
    def for_chain(cls, chain: str) -> "StrategyPolicy":
        normalized = CHAIN_ALIASES.get(_text(chain).lower(), _text(chain).lower())
        if normalized == "robinhood":
            return cls(
                chain="robinhood",
                live_signal_stage="aggregate_early_bird",
                entry_route="robinhood_aggregate_early_bird",
                order_notional_usd=2.0,
                first_mcap_min_usd=10_000.0,
                first_mcap_max_usd=300_000.0,
                max_entry_delay_seconds=600.0,
                max_markup_from_first=1.25,
                min_liquidity_usd=8_000.0,
                max_round_trip_loss_pct=25.0,
                max_buy_price_impact_pct=12.0,
                max_sell_price_impact_pct=None,
                min_sellable_cycles=1,
                requires_observed_sell=False,
                max_open_positions=3,
                max_open_notional_usd=15.0,
                daily_realized_loss_stop_usd=5.0,
                min_rank_score=45.0,
                max_pair_age_hours=6.0,
                min_independent_sources=2,
                allowed_recommendation_buckets=frozenset({"ambush", "lead"}),
            )
        if normalized == "bsc":
            return cls(
                chain="bsc",
                live_signal_stage="aggregate_early_bird",
                entry_route="bsc_aggregate_early_bird",
                order_notional_usd=1.0,
                first_mcap_min_usd=10_000.0,
                first_mcap_max_usd=100_000.0,
                max_entry_delay_seconds=600.0,
                max_markup_from_first=1.5,
                min_liquidity_usd=8_000.0,
                max_round_trip_loss_pct=25.0,
                max_buy_price_impact_pct=None,
                max_sell_price_impact_pct=12.0,
                min_sellable_cycles=2,
                requires_observed_sell=True,
                max_open_positions=2,
                max_open_notional_usd=2.0,
                daily_realized_loss_stop_usd=2.0,
                min_rank_score=60.0,
                max_pair_age_hours=6.0,
                min_independent_sources=2,
                allowed_recommendation_buckets=frozenset({"ambush", "lead"}),
            )
        raise ValueError(f"unsupported_chain:{normalized or 'missing'}")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _first_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None and row[key] != "":
            return row[key]
    return None


def _chain(row: Mapping[str, Any]) -> str:
    chain = _text(_first_value(row, "chain", "chain_id", "chainId")).lower()
    return CHAIN_ALIASES.get(chain, chain)


def _contract(row: Mapping[str, Any]) -> str:
    contract = _text(
        _first_value(row, "contract_address", "token_address", "tokenAddress", "address")
    )
    return contract.lower() if contract.lower().startswith("0x") else contract


def token_identity(row: Mapping[str, Any]) -> str:
    """Return the normalized chain-and-contract accounting identity."""
    chain = _chain(row)
    contract = _contract(row)
    if chain in SUPPORTED_EVM_CHAINS:
        if not EVM_CONTRACT_RE.fullmatch(contract) or int(contract[2:], 16) == 0:
            return ""
    return f"{chain}:{contract}" if chain and contract else ""


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _metric(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in row:
            value = _number(row[key])
            if value is not None:
                return value
    return None


def _normalized_stage(row: Mapping[str, Any]) -> str:
    stage = _text(_first_value(row, "signal_stage", "stage", "source_stage")).lower()
    return stage.replace("-", "_").replace(" ", "_")


def _independent_source_count(row: Mapping[str, Any]) -> int | None:
    values: list[Any] = []
    for field in ("source_groups", "source_labels", "sources"):
        raw = row.get(field)
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
    providers = set()
    for value in values:
        text = _text(value).lower().replace("_", " ").replace("-", " ")
        if not text:
            continue
        if text.startswith("985"):
            providers.add("985")
        elif "gmgn" in text:
            providers.add("gmgn")
        elif "okx" in text:
            providers.add("okx")
        elif text in {"ds", "dex screener", "dexscreener", "profile", "boost"}:
            providers.add("dexscreener")
        else:
            providers.add(text)
    if providers:
        return len(providers)
    stated = _metric(row, "independent_source_count", "source_count")
    return int(stated) if stated is not None and stated >= 0 else None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = _text(value).lower()
    if text in {"1", "true", "yes", "pass", "passed", "fresh", "executable", "safe"}:
        return True
    if text in {"0", "false", "no", "fail", "failed", "stale", "unavailable", "unsafe"}:
        return False
    return None


def _route_ready(row: Mapping[str, Any], side: str) -> bool | None:
    combined = _first_value(
        row,
        f"{side}_route_ready",
        f"{side}_route_ok",
    )
    if combined is not None:
        return _boolean(combined)

    route = row.get(f"{side}_route")
    if isinstance(route, Mapping):
        fresh = _boolean(_first_value(route, "fresh", "is_fresh"))
        executable = _boolean(_first_value(route, "executable", "is_executable"))
        if fresh is False or executable is False:
            return False
        if fresh is True and executable is True:
            return True
        return None

    fresh = _boolean(_first_value(
        row,
        f"{side}_route_fresh",
        f"{side}_quote_fresh",
        f"fresh_{side}_route",
    ))
    executable_value = _first_value(
        row,
        f"{side}_route_executable",
        f"{side}_executable",
        f"{side}_quote_executable",
    )
    executable = _boolean(executable_value) if executable_value is not None else None
    if fresh is False or executable is False:
        return False
    if fresh is True and executable is True:
        return True
    return None


def _hard_risks_pass(row: Mapping[str, Any]) -> bool | None:
    explicit = _first_value(
        row,
        "hard_risk_pass",
        "hard_risks_pass",
        "contract_and_tax_risks_pass",
        "risk_pass",
    )
    if explicit is not None:
        return _boolean(explicit) is True
    hard_risk = _first_value(row, "hard_risk", "has_hard_risk")
    return _boolean(hard_risk) is False if hard_risk is not None else None


def _observed_sell(row: Mapping[str, Any]) -> bool:
    explicit = _first_value(row, "real_sell_observed", "observed_sell")
    if explicit is not None:
        return _boolean(explicit) is True
    count = _metric(row, "sell_count5m", "sell_count", "sells", "sell_tx_count", "observed_sell_count")
    return count is not None and count >= 1


def _reject_reason(chain: str, checks: Mapping[str, bool], first_mcap: float | None) -> str:
    if not checks["identity"]:
        return "missing_token_identity"
    if not checks["supported_chain"]:
        return "unsupported_chain"
    if checks.get("history_present") and not checks.get("first_snapshot_identity"):
        return "first_snapshot_identity_mismatch"
    if not checks["first_snapshot"]:
        return "missing_first_snapshot"
    if not checks["current_metrics"]:
        return "current_metrics_unavailable"
    if not checks["rank_available"]:
        return f"{chain}_rank_unavailable"
    if chain == "bsc" and not checks["signal_stage"]:
        return "bsc_shadow_only"
    if not checks["signal_stage"]:
        return f"{chain}_signal_stage"
    if not checks["first_mcap"]:
        if chain == "bsc" and first_mcap is not None and 100_000 < first_mcap <= 300_000:
            return "bsc_mcap_shadow_only"
        return f"{chain}_first_mcap_range"
    for check, suffix in (
        ("entry_age", "entry_window"),
        ("markup", "markup_limit"),
        ("liquidity", "liquidity_minimum"),
        ("observed_sell", "observed_sell_required"),
    ):
        if not checks[check]:
            return f"{chain}_{suffix}"
    if checks["hard_risk"] is False or (chain == "bsc" and checks["hard_risk"] is not True):
        return f"{chain}_hard_risk"
    # Rank and the monitor recommendation bucket order candidates for review.
    # They must not veto the separate live strategy after its hard gates pass.
    if not checks["pair_age"]:
        return f"{chain}_pair_age"
    if not checks["independent_sources"]:
        return f"{chain}_independent_sources"
    return ""


def _tradeability_reason(chain: str, checks: Mapping[str, bool | None]) -> str:
    for check, suffix in (
        ("buy_route", "buy_route"),
        ("sell_route", "sell_route"),
        ("round_trip_loss", "round_trip_loss_limit"),
        ("buy_impact", "buy_impact_limit"),
        ("sell_impact", "sell_impact_limit"),
    ):
        if checks[check] is False:
            return f"{chain}_{suffix}"
    return ""


def _tradeability_status(checks: Mapping[str, bool | None]) -> str:
    if any(value is False for value in checks.values()):
        return "unavailable"
    if any(value is None for value in checks.values()):
        return "pending"
    return "passed"


def _decision_bucket_passes(chain: str, stage: str, bucket: str, policy: StrategyPolicy | None) -> bool:
    if policy is None:
        return False
    return bucket in policy.allowed_recommendation_buckets


def _signal_stage_passes(stage: str, policy: StrategyPolicy | None) -> bool:
    if policy is None:
        return False
    return stage == policy.live_signal_stage or stage in PROMOTED_SIGNAL_STAGES


def classify_signal(
    row: Mapping[str, Any],
    history_row: Mapping[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    """Classify one normalized monitor row without performing I/O."""
    history = history_row if isinstance(history_row, Mapping) else {}
    history_present = bool(history_row)
    stored_snapshot = history.get("first_snapshot")
    first_snapshot = stored_snapshot if isinstance(stored_snapshot, Mapping) else history

    first_seen_at = _first_value(history, "first_seen_at")
    if first_seen_at is None:
        first_seen_at = _first_value(first_snapshot, "first_seen_at", "seen_at")
    if first_seen_at is None and not history_present:
        first_seen_at = _first_value(row, "first_seen_at", "watch_first_seen_at")

    first_price = _metric(history, "first_price_usd")
    discovery_snapshot = history.get("discovery_snapshot")
    discovery_snapshot = discovery_snapshot if isinstance(discovery_snapshot, Mapping) else {}
    if first_price is None:
        first_price = _metric(discovery_snapshot, "first_price_usd", "price_usd", "price")
    if first_price is None:
        first_price = _metric(first_snapshot, "first_price_usd", "price_usd", "price")
    first_mcap = _metric(history, "first_mcap_usd", "first_mcap")
    if first_mcap is None:
        first_mcap = _metric(
            discovery_snapshot,
            "first_mcap_usd",
            "mcap",
            "market_cap",
            "market_cap_usd",
        )
    if first_mcap is None:
        first_mcap = _metric(
            first_snapshot,
            "first_mcap_usd",
            "mcap",
            "market_cap",
            "market_cap_usd",
        )

    current_price = _metric(row, "current_price_usd", "price_usd", "price")
    current_mcap = _metric(
        row,
        "current_mcap_usd",
        "mcap",
        "market_cap",
        "market_cap_usd",
    )
    first_time = _parse_timestamp(first_seen_at)
    current_time = _parse_timestamp(now)
    delay = (current_time - first_time).total_seconds() if first_time and current_time else None
    if current_price is not None and current_price > 0 and first_price is not None and first_price > 0:
        markup = current_price / first_price
    elif current_mcap is not None and current_mcap > 0 and first_mcap is not None and first_mcap > 0:
        markup = current_mcap / first_mcap
    else:
        markup = None

    chain = _chain(row)
    stage = _normalized_stage(row)
    identity = token_identity(row)
    stored_identity = token_identity(first_snapshot) or token_identity(history)
    first_snapshot_identity = bool(
        history_present and identity and stored_identity and stored_identity == identity
    )
    try:
        policy = StrategyPolicy.for_chain(chain)
    except ValueError:
        policy = None

    liquidity = _metric(row, "current_liquidity_usd", "liquidity_usd", "liquidity")
    round_trip_loss = _metric(
        row,
        "round_trip_loss_pct",
        "estimated_round_trip_loss_pct",
        "immediate_round_trip_loss_pct",
    )
    buy_impact = _metric(row, "buy_price_impact_pct", "buy_impact_pct")
    sell_impact = _metric(row, "sell_price_impact_pct", "sell_impact_pct")
    sellable_cycles = _metric(
        row,
        "sellable_cycles",
        "consecutive_sellable_cycles",
        "executable_sell_cycles",
    )
    rank_score = _metric(row, "rank_score")
    rank_score = round(max(0.0, min(100.0, rank_score)), 2) if rank_score is not None else None
    legacy_score = _metric(row, "legacy_score", "execution_candidate_score", "score", "entry_score")
    legacy_score = round(max(0.0, min(100.0, legacy_score)), 2) if legacy_score is not None else None
    pair_age_hours = _metric(row, "pair_age_hours")
    recommendation_bucket = _text(row.get("recommendation_bucket")).lower()
    independent_source_count = _independent_source_count(row)
    raw_components = row.get("rank_components")
    rank_components = {}
    if isinstance(raw_components, Mapping):
        for key, value in raw_components.items():
            clean_key = _text(key)
            clean_value = _number(value)
            if clean_key and clean_value is not None:
                rank_components[clean_key[:60]] = round(clean_value, 4)

    checks = {
        "identity": bool(identity),
        "supported_chain": policy is not None,
        "history_present": history_present,
        "first_snapshot_identity": first_snapshot_identity,
        "first_snapshot": history_present
        and first_snapshot_identity
        and first_time is not None
        and first_mcap is not None
        and first_mcap > 0,
        "current_metrics": current_price is not None
        and current_price > 0
        and current_mcap is not None
        and current_mcap > 0,
        "rank_available": rank_score is not None,
        "rank_threshold": policy is not None and rank_score is not None and rank_score >= policy.min_rank_score,
        "decision_bucket": _decision_bucket_passes(chain, stage, recommendation_bucket, policy),
        "pair_age": policy is not None and pair_age_hours is not None and 0 <= pair_age_hours <= policy.max_pair_age_hours,
        "independent_sources": policy is not None and independent_source_count is not None
        and independent_source_count >= policy.min_independent_sources,
        "signal_stage": _signal_stage_passes(stage, policy),
        "first_mcap": policy is not None
        and first_mcap is not None
        and policy.first_mcap_min_usd <= first_mcap <= policy.first_mcap_max_usd,
        "entry_age": policy is not None
        and delay is not None
        and 0 <= delay <= policy.max_entry_delay_seconds,
        "markup": policy is not None
        and markup is not None
        and markup <= policy.max_markup_from_first,
        "liquidity": policy is not None
        and liquidity is not None
        and liquidity >= policy.min_liquidity_usd,
        "hard_risk": _hard_risks_pass(row),
        # The OKX executor performs the exact round-trip check. Requiring that
        # result here would prevent the candidate from reaching the verifier.
        "sellability_confirmation": None,
        "observed_sell": policy is not None
        and (not policy.requires_observed_sell or _observed_sell(row)),
    }
    tradeability_checks: dict[str, bool | None] = {
        "buy_route": _route_ready(row, "buy"),
        "sell_route": _route_ready(row, "sell"),
        "round_trip_loss": (
            None
            if policy is None or round_trip_loss is None
            else 0 <= round_trip_loss <= policy.max_round_trip_loss_pct
        ),
        "buy_impact": (
            None
            if policy is None
            else True
            if policy.max_buy_price_impact_pct is None
            else None
            if buy_impact is None
            else 0 <= buy_impact <= policy.max_buy_price_impact_pct
        ),
        "sell_impact": (
            None
            if policy is None
            else True
            if policy.max_sell_price_impact_pct is None
            else None
            if sell_impact is None
            else 0 <= sell_impact <= policy.max_sell_price_impact_pct
        ),
    }
    tradeability = {
        "status": _tradeability_status(tradeability_checks),
        "reject_reason": _tradeability_reason(chain, tradeability_checks),
        "liquidity_usd": liquidity,
        "buy_route_ready": tradeability_checks["buy_route"],
        "sell_route_ready": tradeability_checks["sell_route"],
        "round_trip_loss_pct": round_trip_loss,
        "buy_impact_pct": buy_impact,
        "sell_impact_pct": sell_impact,
        "quote_at": _first_value(
            row,
            "tradeability_quote_at",
            "okx_quote_at",
            "executable_quote_at",
        ),
        "policy_checks": tradeability_checks,
    }
    reject_reason = _reject_reason(chain, checks, first_mcap)
    eligible = not reject_reason

    if chain == "bsc" and stage == "aggregate_discovery":
        entry_route = "bsc_aggregate_discovery_shadow"
    elif policy is not None and stage in PROMOTED_SIGNAL_STAGES:
        entry_route = f"{chain}_{stage}"
    elif policy is not None:
        entry_route = policy.entry_route
    else:
        entry_route = "unsupported"
    if eligible:
        execution_mode = "live_candidate"
    elif reject_reason in {"bsc_shadow_only", "bsc_mcap_shadow_only"}:
        execution_mode = "shadow"
    else:
        execution_mode = "rejected"

    return {
        "strategy_version": STRATEGY_VERSION,
        "signal_stage": stage,
        "entry_route": entry_route,
        "execution_mode": execution_mode,
        "rank_score": rank_score,
        "legacy_score": legacy_score,
        "rank_components": rank_components,
        "pair_age_hours": pair_age_hours,
        "recommendation_bucket": recommendation_bucket or None,
        "independent_source_count": independent_source_count,
        "first_seen_at": first_seen_at,
        "first_price_usd": first_price,
        "first_mcap_usd": first_mcap,
        "current_price_usd": current_price,
        "current_mcap_usd": current_mcap,
        "entry_delay_seconds": delay,
        "markup_from_first": round(markup, 4) if markup is not None else None,
        "policy_checks": checks,
        "tradeability": tradeability,
        "eligible": eligible,
        "entry_authorized": eligible,
        "reject_reason": reject_reason,
        "order_authorized": False,
        "requires_executor_tradeability_check": True,
    }
