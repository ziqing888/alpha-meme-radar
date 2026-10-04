"""Dealer-proxy scoring and coverage summaries."""

from __future__ import annotations

from typing import Any

import alpha_pool_radar as alpha


def market_cap(row: dict[str, Any]) -> float:
    return alpha.to_float(row.get("market_cap") or row.get("mcap"))


def dealer_volume(row: dict[str, Any]) -> float:
    buyflow_vol = alpha.to_float(row.get("alpha_buyflow_volume_usd"))
    alpha_vol = alpha.to_float(row.get("alpha_volume24h"))
    dex_vol = alpha.to_float(row.get("dex_volume24h") or row.get("volume24h"))
    return max(buyflow_vol, alpha_vol, dex_vol)


def dealer_change(row: dict[str, Any]) -> float:
    return alpha.to_float(row.get("alpha_change24h_pct") or row.get("change_h24") or row.get("price_change_24h_pct"))


def contract_volume(row: dict[str, Any]) -> float:
    return alpha.to_float(row.get("futures_quote_volume24h"))


def dealer_profile(row: dict[str, Any]) -> dict[str, Any]:
    mcap = market_cap(row)
    volume = dealer_volume(row)
    futures_volume = contract_volume(row)
    change = dealer_change(row)
    oi = alpha.to_float(row.get("oi_change_1h_pct"))
    funding = alpha.to_float(row.get("funding_rate_pct"))
    top10 = alpha.to_float(row.get("top10_holder_pct"))
    max_holder = alpha.to_float(row.get("max_holder_pct"))
    vol_to_mcap = volume / mcap if mcap > 0 else 0.0
    contract_to_mcap = futures_volume / mcap if mcap > 0 else 0.0
    high_volume = vol_to_mcap >= 0.25
    high_holder = top10 >= 55 or max_holder >= 20
    oi_not_collapsing = oi >= -5
    contract_pump = high_holder and change >= 6 and (oi >= 4 or (contract_to_mcap >= 0.75 and oi_not_collapsing))
    futures_pump = change >= 6 and futures_volume > 0 and (oi >= 8 or (contract_to_mcap >= 1.0 and oi_not_collapsing))
    buyflow_score = alpha.to_float(row.get("alpha_buyflow_score"))
    buyflow_label = str(row.get("alpha_buyflow_label") or "")
    buyflow_explain = str(row.get("alpha_buyflow_explain") or "").strip()

    if buyflow_score >= 68 and change >= 6:
        dealer_type = "buyflow_pump"
        dealer_label = "活跃上涨型"
        explain = f"{buyflow_label}，{buyflow_explain}；上涨 {change:.1f}%，净买入金额尚未验证。"
    elif contract_pump:
        dealer_type = "controlled_contract_pump"
        dealer_label = "高控爆拉型"
        explain = (
            f"Top10 {top10:.1f}%{f'，最大钱包 {max_holder:.1f}%' if max_holder else ''}，"
            f"上涨 {change:.1f}%，OI {oi:.1f}%，合约量市比 {contract_to_mcap * 100:.1f}%，"
            "更像高控盘配合合约爆拉。"
        )
    elif futures_pump:
        dealer_type = "contract_pump"
        dealer_label = "合约爆拉型"
        explain = (
            f"上涨 {change:.1f}%，OI {oi:.1f}%，合约量市比 {contract_to_mcap * 100:.1f}%，"
            "即使暂缺Top10数据，也像合约资金在主动推。"
        )
    elif buyflow_score >= 68 and change > -8:
        dealer_type = "absorption"
        dealer_label = "活跃待确认"
        explain = f"{buyflow_label}，{buyflow_explain}；价格未明显拉开，不能仅凭成交认定吸筹。"
    elif high_volume and change >= 8:
        dealer_type = "pump"
        dealer_label = "拉盘型"
        explain = f"上涨 {change:.1f}%，量市比 {vol_to_mcap * 100:.1f}%，更像拉盘/吸引跟风。"
    elif high_volume and change <= -8:
        dealer_type = "distribution"
        dealer_label = "出货型"
        explain = f"下跌 {change:.1f}%，量市比 {vol_to_mcap * 100:.1f}%，更像放量出货/砸盘。"
    elif high_holder:
        dealer_type = "holder_risk"
        dealer_label = "高控盘型"
        explain = f"Top10 {top10:.1f}%{f'，最大钱包 {max_holder:.1f}%' if max_holder else ''}，主要是筹码集中风险。"
    elif high_volume:
        dealer_type = "active_turnover"
        dealer_label = "换手活跃"
        explain = f"量市比 {vol_to_mcap * 100:.1f}%，但涨跌方向不强，只能叫成交活跃。"
    else:
        dealer_type = "watch"
        dealer_label = "观察型"
        explain = "暂无明显拉盘/出货方向，按筹码、合约和池子继续观察。"

    return {
        "dealer_type": dealer_type,
        "dealer_label": dealer_label,
        "dealer_explain": explain,
        "dealer_volume_usd": round(volume, 2),
        "dealer_volume_to_mcap_pct": round(vol_to_mcap * 100, 2) if mcap > 0 else None,
        "dealer_contract_volume_to_mcap_pct": round(contract_to_mcap * 100, 2) if mcap > 0 and futures_volume > 0 else None,
    }


def dealer_score(row: dict[str, Any]) -> tuple[float, list[str]]:
    mcap = market_cap(row)
    volume = dealer_volume(row)
    oi = row.get("oi_change_1h_pct")
    funding = row.get("funding_rate_pct")
    futures_volume = contract_volume(row)
    liq = alpha.to_float(row.get("dex_liquidity") or row.get("liquidity"))
    age = row.get("pair_age_hours")
    flags: list[str] = []
    score = 0.0

    if 0 < mcap <= 80_000_000:
        score += 18
        flags.append("小市值")
    if mcap > 0 and volume / mcap >= 0.25:
        score += min(22, volume / mcap * 30)
        flags.append("量/市值高")
    if oi is not None and alpha.to_float(oi) >= 5:
        score += min(24, alpha.to_float(oi) * 1.4)
        flags.append(f"OI+{alpha.to_float(oi):.1f}%")
    if mcap > 0 and futures_volume / mcap >= 1.0:
        score += min(18, futures_volume / mcap * 4)
        flags.append("合约量/市值高")
    if funding is not None and abs(alpha.to_float(funding)) >= 0.05:
        score += 12
        flags.append(f"费率{alpha.to_float(funding):.3f}%")
    if 0 < liq < max(50_000, mcap * 0.02):
        score += 8
        flags.append("浅池")
    if age is not None and alpha.to_float(age) <= 168:
        score += 7
        flags.append(f"新池{alpha.to_float(age) / 24:.1f}天")
    top10 = row.get("top10_holder_pct")
    max_holder = row.get("max_holder_pct")
    if top10 is not None:
        top10_value = alpha.to_float(top10)
        if top10_value >= 55:
            score += 26
            flags.append(f"Top10 {top10_value:.1f}%")
        elif top10_value >= 35:
            score += 16
            flags.append(f"Top10 {top10_value:.1f}%")
    if max_holder is not None and alpha.to_float(max_holder) >= 18:
        score += 10
        flags.append(f"Max {alpha.to_float(max_holder):.1f}%")
    top10_value = alpha.to_float(top10)
    max_holder_value = alpha.to_float(max_holder)
    high_holder = top10_value >= 55 or max_holder_value >= 20
    oi_value = alpha.to_float(oi)
    futures_to_mcap = futures_volume / mcap if mcap > 0 else 0.0
    if high_holder and dealer_change(row) >= 6 and (oi_value >= 4 or (futures_to_mcap >= 0.75 and oi_value >= -5)):
        score += 22
        flags.append("高控爆拉")
    elif dealer_change(row) >= 6 and futures_volume > 0 and (oi_value >= 8 or (futures_to_mcap >= 1.0 and oi_value >= -5)):
        score += 14
        flags.append("合约爆拉")
    buyflow_score = alpha.to_float(row.get("alpha_buyflow_score"))
    if buyflow_score >= 68:
        score += 18
        flags.append("高度活跃")
    elif buyflow_score >= 48:
        score += 12
        flags.append("成交活跃")
    elif buyflow_score >= 28:
        score += 6
        flags.append("成交升温")
    risk_level = str(row.get("risk_level") or "")
    risk_score = row.get("risk_score")
    if risk_level:
        flags.append(f"Risk {risk_level} {alpha.to_float(risk_score):.0f}")
    return round(score, 2), flags


def holder_coverage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tokens: dict[str, dict[str, Any]] = {}
    for row in rows:
        chain = str(row.get("chain") or row.get("chain_id") or "").lower()
        address = str(row.get("contract_address") or row.get("token_address") or row.get("pair_address") or row.get("symbol") or "").lower()
        key = f"{chain}:{address}"
        if key == ":":
            continue
        tokens.setdefault(key, row)

    with_top10 = [row for row in tokens.values() if row.get("top10_holder_pct") is not None]
    sources: dict[str, int] = {}
    for row in with_top10:
        source = str(row.get("holder_source") or "unknown")
        sources[source] = sources.get(source, 0) + 1

    total = len(tokens)
    return {
        "unique_tokens": total,
        "with_top10": len(with_top10),
        "coverage_pct": round((len(with_top10) / total) * 100, 2) if total else 0.0,
        "sources": sources,
    }


def dealer_dimensions(row: dict[str, Any]) -> dict[str, Any]:
    # Reuse the existing activity weights without rewarding concentration,
    # thin liquidity or either sign of extreme funding as a long opportunity.
    clean = {**row, "top10_holder_pct": None, "max_holder_pct": None,
             "dex_liquidity": 0, "liquidity": 0, "funding_rate_pct": None}
    activity, _ = dealer_score(clean)
    from alpha_recommendations import risk_gate
    gate = risk_gate(row)
    blocked = bool(gate and gate["recommendation_bucket"] == "danger")
    incomplete = bool(gate and not blocked)
    return {
        "dealer_opportunity_score": None if incomplete else (0.0 if blocked else min(100.0, activity)),
        "dealer_activity_score": min(100.0, activity),
        "dealer_risk_status": "blocked" if blocked else "unknown" if incomplete else "checked",
        "dealer_structure_status": str(row.get("stage") or "待确认"),
        "dealer_model_version": "risk-separated-v1",
    }
