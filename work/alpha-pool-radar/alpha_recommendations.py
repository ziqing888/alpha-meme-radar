from __future__ import annotations

from typing import Any

from alpha_stage_model import stage_priority


BUCKET_LABELS = {
    "lead": "主推候选",
    "ambush": "埋伏候选",
    "pullback": "等回踩候选",
    "danger": "高危别碰",
    "reject": "放弃观察",
}


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def row_key(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or row.get("name") or "").strip().lower()
    chain = str(row.get("chain") or row.get("chain_id") or row.get("chainId") or "").strip().lower()
    address = str(row.get("contract_address") or row.get("token_address") or row.get("address") or "").strip().lower()
    return f"{chain}:{address or symbol}"


def score_of(row: dict[str, Any]) -> float:
    return to_float(row.get("dealer_opportunity_score", row.get("score")))


def market_cap_of(row: dict[str, Any]) -> float:
    return to_float(row.get("market_cap") or row.get("mcap") or row.get("fdv"))


def volume_of(row: dict[str, Any]) -> float:
    return to_float(row.get("alpha_volume24h") or row.get("volume24h") or row.get("dex_volume24h"))


def change_of(row: dict[str, Any]) -> float:
    return to_float(row.get("alpha_change24h_pct") or row.get("change_h1") or row.get("futures_price_change24h_pct") or row.get("change_h24"))


def pct(value: Any, digits: int = 1) -> str:
    number = to_float(value)
    prefix = "+" if number >= 0 else ""
    return f"{prefix}{number:.{digits}f}%"


def money(value: Any) -> str:
    number = to_float(value)
    if number >= 1_000_000_000:
        return f"${number / 1_000_000_000:.1f}B"
    if number >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.0f}"


def classify_row(row: dict[str, Any]) -> dict[str, str]:
    gate = risk_gate(row)
    if gate:
        return gate
    if row.get("funding_rate_8h_pct") is not None:
        row = {**row, "funding_rate_pct": row["funding_rate_8h_pct"]}
    score = score_of(row)
    stage = str(row.get("stage") or "")
    stage_action = str(row.get("action") or "")
    stage_reason = str(row.get("reason") or "")
    protection = str(row.get("protection_text") or "")
    holding = str(row.get("holding_text") or "")
    if stage_action:
        if stage_action.startswith("做多"):
            bucket = "lead" if score >= 76 or "继续拿" in stage_action else "ambush"
            return {
                "recommendation_bucket": bucket,
                "recommendation_label": BUCKET_LABELS[bucket],
                "recommendation_action": stage_action,
                "recommendation_reason": stage_reason or f"{stage} 阶段，按阶段模型跟踪。",
                "recommendation_risk": protection or "只按阶段模型观察，不自动下单。",
                "recommendation_next_step": holding or "等下一次阶段确认。",
            }
        if stage_action.startswith("多单保护"):
            return {
                "recommendation_bucket": "pullback",
                "recommendation_label": BUCKET_LABELS["pullback"],
                "recommendation_action": stage_action,
                "recommendation_reason": stage_reason or f"{stage} 阶段，先保护已有多单。",
                "recommendation_risk": protection or "不追高，等收回或破位。",
                "recommendation_next_step": holding or "看关键位能否收回。",
            }
        if stage_action.startswith("做空"):
            return {
                "recommendation_bucket": "danger",
                "recommendation_label": BUCKET_LABELS["danger"],
                "recommendation_action": stage_action,
                "recommendation_reason": stage_reason or f"{stage} 阶段，按破位试空观察。",
                "recommendation_risk": protection or "空单只适合小仓，站回失效位撤。",
                "recommendation_next_step": holding or "等反抽不过和再次破低。",
            }
        if stage in {"静默蓄势", "观察"} and score < 58:
            return {
                "recommendation_bucket": "reject",
                "recommendation_label": BUCKET_LABELS["reject"],
                "recommendation_action": stage_action,
                "recommendation_reason": stage_reason or "还没有阶段变化。",
                "recommendation_risk": protection or "不提醒，只记录。",
                "recommendation_next_step": holding or "继续后台观察。",
            }
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    risk_level = str(row.get("risk_level") or "").lower()
    risk_score = to_float(row.get("risk_score"))
    oi = to_float(row.get("oi_change_1h_pct"))
    funding_abs = abs(to_float(row.get("funding_rate_pct")))
    change = change_of(row)
    age_hours = to_float(row.get("pair_age_hours"))
    market_cap = market_cap_of(row)
    volume = volume_of(row)
    volume_to_mcap = volume / market_cap if market_cap > 0 else 0.0

    if top10 >= 55 or max_holder >= 20 or risk_level == "high" or risk_score >= 70:
        return {
            "recommendation_bucket": "danger",
            "recommendation_label": BUCKET_LABELS["danger"],
            "recommendation_action": "先别碰",
            "recommendation_reason": f"筹码或风险过高，Top10 {pct(top10)}，最大钱包 {pct(max_holder)}",
            "recommendation_risk": "容易被单边砸盘、插针，先放风险池。",
            "recommendation_next_step": "等筹码分散、流动性变厚后再看。",
        }
    if score >= 76 and oi >= 5 and funding_abs < 0.08 and change < 35:
        return {
            "recommendation_bucket": "lead",
            "recommendation_label": BUCKET_LABELS["lead"],
            "recommendation_action": "主推盯盘",
            "recommendation_reason": f"综合分 {score:.0f}，OI {pct(oi)}，费率还没过热。",
            "recommendation_risk": "不能追尖顶，放量后要等盘口确认。",
            "recommendation_next_step": "盯 5m 量能和回踩承接。",
        }
    if score >= 68 and change < 18 and age_hours >= 24 and market_cap <= 150_000_000 and volume >= 1_000_000:
        return {
            "recommendation_bucket": "ambush",
            "recommendation_label": BUCKET_LABELS["ambush"],
            "recommendation_action": "可埋伏",
            "recommendation_reason": f"热度够但还没完全拉开，市值 {money(market_cap)}。",
            "recommendation_risk": "量能断掉会降级，只适合观察池前排。",
            "recommendation_next_step": "等二次放量或 Alpha 热度继续抬头。",
        }
    if (
        score >= 58
        and market_cap <= 80_000_000
        and volume >= 2_000_000
        and volume_to_mcap >= 0.12
        and 24 <= age_hours <= 168
        and change < 15
        and funding_abs < 0.06
        and top10 < 50
        and max_holder < 18
    ):
        return {
            "recommendation_bucket": "ambush",
            "recommendation_label": BUCKET_LABELS["ambush"],
            "recommendation_action": "可埋伏",
            "recommendation_reason": f"没完全拉开但量市比 {volume_to_mcap:.2f}，市值 {money(market_cap)}。",
            "recommendation_risk": "属于早期候选，量能断掉就退回观察。",
            "recommendation_next_step": "盯二次放量、OI 是否继续抬头。",
        }
    if (
        score >= 60
        and market_cap <= 120_000_000
        and volume_to_mcap >= 0.12
        and to_float(row.get("funding_rate_pct")) <= -0.08
        and change < 12
        and top10 < 50
        and max_holder < 18
    ):
        return {
            "recommendation_bucket": "ambush",
            "recommendation_label": BUCKET_LABELS["ambush"],
            "recommendation_action": "可埋伏",
            "recommendation_reason": f"负费率 {pct(row.get('funding_rate_pct'), 3)}，涨幅没拉开，量市比 {volume_to_mcap:.2f}。",
            "recommendation_risk": "负费率不等于必涨，OI 断掉就降级。",
            "recommendation_next_step": "盯空头回补、盘口承接和二次放量。",
        }
    if score >= 58 or change >= 18 or funding_abs >= 0.05:
        return {
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "等回踩",
            "recommendation_reason": f"短线已经动过，涨幅 {pct(change)}，费率 {pct(row.get('funding_rate_pct'), 3)}。",
            "recommendation_risk": "第一波容易冲高回落，不适合直接追。",
            "recommendation_next_step": "等回踩不破或 OI 二次抬头。",
        }
    return {
        "recommendation_bucket": "reject",
        "recommendation_label": BUCKET_LABELS["reject"],
        "recommendation_action": "放弃观察",
        "recommendation_reason": f"信号太散，综合分 {score:.0f}，成交 {money(volume)}。",
        "recommendation_risk": "没有明显资金或筹码信号。",
        "recommendation_next_step": "不放首页，只保留数据记录。",
    }


def risk_gate(row: dict[str, Any]) -> dict[str, str] | None:
    high = (to_float(row.get("top10_holder_pct")) >= 55
            or to_float(row.get("max_holder_pct")) >= 20
            or str(row.get("risk_level") or "").lower() == "high"
            or to_float(row.get("risk_score")) >= 70)
    import math
    def known(key: str) -> bool:
        try:
            return math.isfinite(float(row.get(key)))
        except (TypeError, ValueError):
            return False
    missing = [label for key, label in (("top10_holder_pct", "Top10筹码"), ("max_holder_pct", "最大钱包")) if not known(key)]
    if row.get("oi_basis") == "quantity" and row.get("futures_symbol"):
        missing += [label for key, label in (("oi_change_1h_pct", "数量OI"), ("funding_rate_8h_pct", "费率周期")) if not known(key)]
    if not high and not missing:
        return None
    return {
        "recommendation_bucket": "danger" if high else "reject",
        "recommendation_label": "风险拦截" if high else "数据待补",
        "recommendation_action": "暂停新机会判断" if high else "仅观察",
        "recommendation_reason": "筹码集中或风险过高" if high else "缺少" + "、".join(missing),
        "recommendation_risk": "阶段信号不覆盖风险检查" if high else "缺失不等于健康，不生成入场建议",
        "recommendation_next_step": "等待风险解除并重新确认" if high else "补齐数据后重新评估",
    }


def build_recommendation_rows(alpha_rows: list[dict[str, Any]], dealer_rows: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*alpha_rows, *dealer_rows]:
        key = row_key(row)
        if not key.strip(":"):
            continue
        merged[key] = {**merged.get(key, {}), **row}

    bucket_order = {"lead": 0, "ambush": 1, "pullback": 2, "danger": 3, "reject": 4}
    enriched = [{**row, **classify_row(row)} for row in merged.values()]
    enriched.sort(
        key=lambda row: (
            bucket_order[row["recommendation_bucket"]],
            -stage_priority(row),
            -score_of(row),
            row.get("symbol") or "",
        )
    )

    for index, row in enumerate(enriched, start=1):
        row["recommendation_rank"] = index

    return enriched[:limit] if limit else enriched
