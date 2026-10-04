"""Scoring strategy for Alpha/Meme radar candidates."""

from typing import Any

from alpha_stage_model import best_change
from alpha_stage_model import stage_profile


def _num(row: Any, name: str, default: float = 0.0) -> float:
    try:
        value = getattr(row, name, default)
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _best_change(row: Any) -> float:
    return best_change(row)


def alpha_stage_profile(row: Any, market_cap: float, volume: float) -> dict[str, Any]:
    return stage_profile(row, market_cap=market_cap, volume=volume)


def score_row(row: Any, max_market_cap: float) -> None:
    flags: list[str] = []
    score = 0.0

    market_cap = row.market_cap or row.dex_market_cap or row.fdv
    if 0 < market_cap <= max_market_cap:
        flags.append("small_cap")
        score += 12
        if market_cap <= 50_000_000:
            score += 8
        if market_cap <= 15_000_000:
            score += 6

    vol = max(row.alpha_volume24h, row.dex_volume24h)
    if market_cap > 0:
        vol_to_mcap = vol / market_cap
        if vol_to_mcap >= 0.25:
            flags.append("high_volume_to_mcap")
            score += min(24, vol_to_mcap * 35)
        elif vol_to_mcap >= 0.08:
            score += 8

    if row.dex_volume24h >= 1_000_000:
        flags.append("dex_volume_live")
        score += 8
    if row.alpha_volume24h >= 10_000_000:
        flags.append("alpha_volume_live")
        score += 10

    change = _best_change(row)
    if change >= 20:
        flags.append("alpha_gain")
        score += min(22, change * 0.35)
    if change >= 50:
        flags.append("alpha_big_gain")
        score += 8
    if change >= 120:
        flags.append("alpha_overheated")
        score += 6
    if change <= -15 and vol >= 1_000_000:
        flags.append("alpha_dump")
        score += min(12, abs(change) * 0.25)

    if row.pair_age_hours is not None:
        if row.pair_age_hours <= 24:
            flags.append("fresh_pool_24h")
            score += 10
        elif row.pair_age_hours <= 168:
            flags.append("fresh_pool_7d")
            score += 5

    if row.futures_symbol:
        flags.append("listed_futures")
        score += 10
        if row.futures_quote_volume24h >= 25_000_000:
            flags.append("futures_volume_live")
            score += 8
        if row.oi_change_1h_pct is not None and row.oi_change_1h_pct >= 3:
            flags.append("oi_rising")
            score += min(18, row.oi_change_1h_pct * 2)
        if row.funding_rate_pct is not None and abs(row.funding_rate_pct) >= 0.03:
            flags.append("funding_abnormal")
            score += 6

    if row.hot_tag:
        flags.append("alpha_hot")
        score += 4
    if row.social_count >= 2:
        flags.append("socials_present")
        score += 3

    if row.top10_holder_pct is not None:
        if row.top10_holder_pct >= 55:
            flags.append("top10_concentrated")
            score += 16
        elif row.top10_holder_pct >= 35:
            flags.append("top10_elevated")
            score += 9
        if row.max_holder_pct is not None and row.max_holder_pct >= 18:
            flags.append("single_holder_risk")
            score += 8
        if row.top10_holder_pct >= 55 and change >= 20:
            flags.append("high_control_pump")
            score += 18
    else:
        row.notes.append("Top10 holder needs explorer adapter")

    if row.risk_level:
        flags.append(f"risk_{row.risk_level}")
        if row.risk_level == "high":
            score -= 22
        elif row.risk_level == "medium":
            score -= 10
        elif row.risk_level == "low":
            score -= 3

    row.flags = flags
    row.score = score
    for key, value in alpha_stage_profile(row, market_cap, vol).items():
        setattr(row, key, value)
