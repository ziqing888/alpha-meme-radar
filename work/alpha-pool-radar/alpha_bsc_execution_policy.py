"""Pure, read-only policy for the scaled BSC first-discovery strategy."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


CN_TZ = timezone(timedelta(hours=8))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _risk_number(value: Any) -> float | None:
    """Parse a non-negative risk counter; invalid input must stop execution."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _risk_field(row: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        if name not in row:
            continue
        value = row[name]
        return _risk_number(value)
    return 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (result if result.tzinfo else result.replace(tzinfo=CN_TZ)).astimezone(timezone.utc)


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _time(value)
    if not parsed:
        return None
    current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=CN_TZ).astimezone(timezone.utc)
    return (current - parsed).total_seconds()


def token_key(row: Mapping[str, Any]) -> str:
    """Return a canonical chain/address identity, never a symbol identity."""
    chain = _text(row.get("chain") or row.get("network") or row.get("chain_id")).lower()
    address = _text(row.get("contract_address") or row.get("token_address") or row.get("address")).lower()
    return f"{chain}:{address}" if chain and address else ""


def _pool_key(row: Mapping[str, Any]) -> str:
    return _text(row.get("pool_address") or row.get("pair_address")).lower()


def scaled_notional(capital_usd: float, base_usd: float = 35.0, max_usd: float = 55.0) -> float:
    """Scale the legacy 100U reference to the old 35-55U paper range."""
    capital = max(0.0, _number(capital_usd))
    base = max(0.0, _number(base_usd))
    ceiling = max(base, _number(max_usd, base))
    return round(min(ceiling, base * capital / 100.0), 8)


@dataclass(frozen=True)
class ExecutionConfig:
    reference_capital_usd: float = 100.0
    order_notional_usd: float = 5.0
    max_open_positions: int = 3
    max_exposure_usd: float = 15.0
    daily_loss_limit_usd: float = 8.0
    min_liquidity_usd: float = 8_000.0
    first_mcap_min_usd: float = 10_000.0
    first_mcap_max_usd: float = 300_000.0
    narrative_mcap_min_usd: float = 300_000.0
    narrative_mcap_max_usd: float = 1_000_000.0
    narrative_min_liquidity_usd: float = 30_000.0
    narrative_max_h1_change_pct: float = 100.0
    max_first_observation_minutes: float = 45.0
    max_signal_age_seconds: float = 2_700.0
    max_quote_age_seconds: float = 30.0
    stop_loss_pct: float = 22.0
    tp1_return_pct: float = 100.0
    tp1_sell_fraction: float = 0.8
    time_stop_minutes: float = 90.0
    trail_drawdown_pct: float = 35.0
    profile: str = "strict_45m"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExecutionConfig":
        values = os.environ if env is None else env
        capital = _number(values.get("BSC_EXECUTION_CAPITAL_USD"), 100.0)
        scaled = scaled_notional(capital)
        return cls(
            reference_capital_usd=capital,
            order_notional_usd=min(5.0, scaled),
            max_open_positions=3,
            max_exposure_usd=15.0,
            daily_loss_limit_usd=8.0,
        )

    @classmethod
    def paper(cls) -> "ExecutionConfig":
        return cls.from_env({"BSC_EXECUTION_CAPITAL_USD": "100"})


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str
    arm: str | None = None
    token: str = ""
    notional_usd: float = 0.0


@dataclass(frozen=True)
class ExitDecision:
    exit: bool
    reason: str = "hold"
    sell_fraction: float = 0.0
    return_pct: float | None = None


def _reject(reason: str, row: Mapping[str, Any], config: ExecutionConfig) -> Decision:
    return Decision(False, reason, _text(row.get("execution_arm")) or None, token_key(row), config.order_notional_usd)


def _source_groups(row: Mapping[str, Any]) -> set[str]:
    values: list[str] = []
    for field in ("source_labels", "sources", "source_groups", "source_names"):
        value = row.get(field)
        if isinstance(value, list):
            values.extend(_text(item).lower() for item in value if _text(item))
        elif value:
            values.append(_text(value).lower())
    counts = row.get("source_hit_counts")
    if isinstance(counts, Mapping):
        values.extend(_text(item).lower() for item in counts if _text(item))
    groups: set[str] = set()
    for value in values:
        if "okx" in value:
            groups.add("okx")
        if "gmgn" in value:
            groups.add("gmgn")
        if "dexscreener" in value or value == "ds":
            groups.add("ds")
    return groups


def entry_decision(row: Mapping[str, Any], now: datetime, config: ExecutionConfig) -> Decision:
    """Evaluate one already-normalized candidate without mutating any state."""
    key = token_key(row)
    capital = _risk_number(config.reference_capital_usd)
    order_notional = _risk_number(config.order_notional_usd)
    if capital is None or capital <= 0 or order_notional is None or order_notional <= 0:
        return _reject("invalid_capital", row, config)
    if _text(row.get("chain")).lower() != "bsc":
        return _reject("bsc_only", row, config)
    if not key:
        return _reject("missing_token_identity", row, config)
    if not _pool_key(row):
        return _reject("missing_pool_identity", row, config)
    arm = _text(row.get("execution_arm"))
    if arm not in {"first_discovery", "narrative_breakout"} or config.profile != "strict_45m":
        return _reject("strict_first_discovery_only", row, config)

    signal_age = _age_seconds(row.get("signal_at"), now)
    if signal_age is None or signal_age < 0 or signal_age > config.max_signal_age_seconds:
        return _reject("stale_signal", row, config)
    quote_age = _age_seconds(row.get("quote_at") or row.get("quote_observed_at"), now)
    if quote_age is None or quote_age < 0 or quote_age > config.max_quote_age_seconds:
        return _reject("stale_quote", row, config)
    if _text(row.get("quote_status")).lower() != "fresh":
        return _reject("quote_not_fresh", row, config)
    if _number(row.get("price_usd") or row.get("price")) <= 0:
        return _reject("invalid_price", row, config)
    if _number(row.get("liquidity_usd") or row.get("liquidity")) < config.min_liquidity_usd:
        return _reject("low_liquidity", row, config)

    first_age = _age_seconds(row.get("first_seen_at"), now)
    if first_age is None or first_age < 0 or first_age > config.max_first_observation_minutes * 60:
        return _reject("first_discovery_window", row, config)
    mcap = _number(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    if arm == "narrative_breakout":
        if not config.narrative_mcap_min_usd < mcap <= config.narrative_mcap_max_usd:
            return _reject("narrative_breakout_mcap", row, config)
        if _number(row.get("liquidity_usd") or row.get("liquidity")) < config.narrative_min_liquidity_usd:
            return _reject("narrative_breakout_liquidity", row, config)
        groups = _source_groups(row)
        if "okx" not in groups or not groups.intersection({"gmgn", "ds"}):
            return _reject("narrative_breakout_sources", row, config)
    elif not config.first_mcap_min_usd <= mcap <= config.first_mcap_max_usd:
        return _reject("first_discovery_mcap", row, config)
    if _text(row.get("watch_status")).lower() == "invalidated":
        return _reject("invalidated_status", row, config)
    if row.get("gmgn_risk_flags") or row.get("risk_flags"):
        return _reject("risk_flags", row, config)

    # Discovery classification owns ranking; execution must not add a score gate.
    h1_max = config.narrative_max_h1_change_pct if arm == "narrative_breakout" else 80.0
    if not -25 <= _number(row.get("change_m5"), -999) <= 45 or not -20 <= _number(row.get("change_h1"), -999) <= h1_max:
        return _reject("momentum_gate", row, config)

    existing = row.get("existing_token_keys") or row.get("open_token_keys") or []
    if key in {str(item).lower() for item in existing}:
        return _reject("duplicate_token", row, config)
    open_count = _risk_field(row, "open_positions", "open_count")
    exposure = _risk_field(row, "current_exposure_usd", "open_exposure_usd")
    daily_loss = _risk_field(row, "daily_loss_usd", "realized_loss_usd")
    if open_count is None or exposure is None or daily_loss is None:
        return _reject("invalid_risk_state", row, config)
    if open_count >= config.max_open_positions or exposure + config.order_notional_usd > config.max_exposure_usd:
        return _reject("exposure_limit", row, config)
    if daily_loss >= config.daily_loss_limit_usd:
        return _reject("daily_loss_circuit_breaker", row, config)
    return Decision(True, "accepted", arm, key, config.order_notional_usd)


def exit_decision(position: Mapping[str, Any], quote: Mapping[str, Any], now: datetime, config: ExecutionConfig) -> ExitDecision:
    if _text(position.get("chain")).lower() != "bsc" or _text(quote.get("chain")).lower() != "bsc":
        return ExitDecision(False, "bsc_only")
    position_key = token_key(position)
    quote_key = token_key(quote)
    if not position_key or not quote_key:
        return ExitDecision(False, "missing_token_identity")
    if position_key != quote_key:
        return ExitDecision(False, "token_identity_mismatch")
    position_pool = _pool_key(position)
    quote_pool = _pool_key(quote)
    if not position_pool or not quote_pool:
        return ExitDecision(False, "missing_pool_identity")
    if position_pool != quote_pool:
        return ExitDecision(False, "pool_identity_mismatch")
    quote_age = _age_seconds(quote.get("quote_at") or quote.get("quote_observed_at"), now)
    if quote_age is None or quote_age < 0 or quote_age > config.max_quote_age_seconds:
        return ExitDecision(False, "stale_quote")
    if _text(quote.get("quote_status")).lower() != "fresh":
        return ExitDecision(False, "quote_not_fresh")
    entry = _number(position.get("entry_price_usd") or position.get("entry_price"))
    price = _number(quote.get("price_usd") or quote.get("price"))
    if entry <= 0 or price <= 0:
        return ExitDecision(False, "invalid_price")
    ret = (price / entry - 1.0) * 100.0
    remaining = max(0.0, min(1.0, _number(position.get("remaining_fraction"), 1.0)))
    if remaining <= 0:
        return ExitDecision(False, "already_closed", 0.0, ret)
    if ret <= -config.stop_loss_pct + 1e-9:
        return ExitDecision(True, "stop_loss", remaining, ret)
    if not position.get("tp1_hit") and ret >= config.tp1_return_pct:
        return ExitDecision(True, "take_profit_1", min(remaining, config.tp1_sell_fraction), ret)
    high = max(entry, _number(position.get("high_price_usd"), entry), price)
    if position.get("tp1_hit") and (price / high - 1.0) * 100.0 <= -config.trail_drawdown_pct:
        return ExitDecision(True, "trailing_stop", remaining, ret)
    held = _age_seconds(position.get("entry_at") or position.get("entry_time"), now)
    if held is not None and held >= config.time_stop_minutes * 60:
        return ExitDecision(True, "time_stop", remaining, ret)
    return ExitDecision(False, "hold", 0.0, ret)
