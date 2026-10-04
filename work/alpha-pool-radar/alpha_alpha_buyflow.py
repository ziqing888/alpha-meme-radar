"""Binance Alpha buy-pressure enrichment from public read-only fields."""

from __future__ import annotations

from typing import Any

import alpha_pool_radar as alpha


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _first_positive(*values: Any) -> float:
    for value in values:
        number = alpha.to_float(value)
        if number > 0:
            return number
    return 0.0


def buyflow_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Score Alpha-side buy pressure using the public fields already collected.

    This is a confirmation proxy: Binance Alpha volume, DexScreener buy/sell
    ratio, transaction count, and volume-to-market-cap. It does not use account,
    key, or order endpoints.
    """

    market_cap = _first_positive(row.get("market_cap"), row.get("mcap"), row.get("dex_market_cap"), row.get("fdv"))
    alpha_volume = alpha.to_float(row.get("alpha_volume24h"))
    dex_volume = alpha.to_float(row.get("dex_volume24h") or row.get("volume24h"))
    volume = max(alpha_volume, dex_volume)
    volume_to_mcap = volume / market_cap if market_cap > 0 else 0.0
    buy_sell_ratio = alpha.to_float(row.get("buy_sell_ratio24h"))
    txns24h = alpha.to_float(row.get("txns24h"))
    change = alpha.to_float(row.get("alpha_change24h_pct") or row.get("change_h24") or row.get("price_change_24h_pct"))

    score = 0.0
    reasons: list[str] = []

    if volume_to_mcap >= 0.45:
        score += 30
        reasons.append(f"量市比{_pct(volume_to_mcap)}")
    elif volume_to_mcap >= 0.25:
        score += 22
        reasons.append(f"量市比{_pct(volume_to_mcap)}")
    elif volume_to_mcap >= 0.12:
        score += 12
        reasons.append(f"量市比{_pct(volume_to_mcap)}")

    if buy_sell_ratio >= 2.0 and txns24h >= 60:
        score += 28
        reasons.append(f"买卖比{buy_sell_ratio:.2f}")
    elif buy_sell_ratio >= 1.45 and txns24h >= 40:
        score += 20
        reasons.append(f"买卖比{buy_sell_ratio:.2f}")
    elif buy_sell_ratio >= 1.15 and txns24h >= 25:
        score += 10
        reasons.append(f"买卖比{buy_sell_ratio:.2f}")

    if txns24h >= 800:
        score += 14
        reasons.append(f"成交笔数{txns24h:.0f}")
    elif txns24h >= 300:
        score += 10
        reasons.append(f"成交笔数{txns24h:.0f}")
    elif txns24h >= 100:
        score += 6
        reasons.append(f"成交笔数{txns24h:.0f}")

    if alpha_volume > 0 and dex_volume > 0:
        score += 8
        reasons.append("Alpha与DEX均有成交，方向未验证")
    elif alpha_volume > 0 or dex_volume > 0:
        score += 4

    if change >= 18:
        score += 12
        reasons.append(f"涨幅{change:.1f}%")
    elif change >= 6:
        score += 7
        reasons.append(f"涨幅{change:.1f}%")
    elif change <= -12 and score >= 18:
        score -= 8
        reasons.append(f"价格承压{change:.1f}%")

    score = round(max(0.0, min(100.0, score)), 2)
    if score >= 68:
        label = "高度活跃"
    elif score >= 48:
        label = "成交活跃"
    elif score >= 28:
        label = "成交升温"
    else:
        label = "未确认"

    explain = " / ".join(reasons) if reasons else "公开成交、买卖比和量市比暂未给出明显买盘。"

    return {
        "alpha_buyflow_score": score,
        "alpha_buyflow_label": label,
        "alpha_buyflow_explain": explain,
        "alpha_buyflow_volume_usd": round(volume, 2),
        "alpha_buyflow_volume_to_mcap_pct": round(volume_to_mcap * 100, 2) if market_cap > 0 else None,
        "alpha_buyflow_buy_sell_ratio": round(buy_sell_ratio, 4) if buy_sell_ratio > 0 else None,
        "alpha_buyflow_txns24h": int(txns24h) if txns24h > 0 else None,
        "alpha_buyflow_confirmed": score >= 48,
        "alpha_buyflow_source": "binance_alpha_dex_public_proxy",
    }


def enrich_alpha_buyflow(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row.update(buyflow_metrics(row))


def buyflow_coverage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    with_ratio = [row for row in rows if row.get("alpha_buyflow_buy_sell_ratio") is not None]
    confirmed = [row for row in rows if row.get("alpha_buyflow_confirmed")]
    strong = [row for row in rows if alpha.to_float(row.get("alpha_buyflow_score")) >= 68]
    return {
        "total": total,
        "with_buy_sell_ratio": len(with_ratio),
        "confirmed_count": len(confirmed),
        "strong_count": len(strong),
        "coverage_pct": round((len(with_ratio) / total) * 100, 2) if total else 0.0,
    }
