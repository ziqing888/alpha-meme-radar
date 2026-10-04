from __future__ import annotations

import re
import importlib.util
from pathlib import Path
from typing import Any


def _load_narrative_analyzer():
    try:
        from alpha_narrative import analyze_narrative

        return analyze_narrative
    except ModuleNotFoundError:
        module_path = Path(__file__).with_name("alpha_narrative.py")
        spec = importlib.util.spec_from_file_location("alpha_narrative", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module.analyze_narrative


analyze_narrative = _load_narrative_analyzer()


BUCKET_LABELS = {
    "ambush": "上车候选",
    "pullback": "等回踩候选",
    "danger": "高危别碰",
    "reject": "放弃观察",
}

MAX_MEME_ENTRY_AGE_HOURS = 168
MAX_NEW_POOL_CANDIDATE_AGE_HOURS = 6
OLD_MEME_POOL_HOURS = 72
OLD_MEME_REVIVAL_MIN_HOURS = 72
BSC_CHAIN_ALIASES = {
    "56",
    "bsc",
    "bnb",
    "bnbchain",
    "bnb chain",
    "binance smart chain",
    "binance-smart-chain",
}

ROBINHOOD_CHAIN_ALIASES = {
    "4663",
    "robinhood",
    "robinhood chain",
    "robinhood-chain",
}

STOCK_MEME_KEYWORDS = (
    "stock",
    "stocks",
    "stonk",
    "stonks",
    "wallstreet",
    "wall street",
    "wsb",
    "gme",
    "gamestop",
    "amc",
    "tesla",
    "tsla",
    "nvidia",
    "nvda",
    "apple",
    "aapl",
    "meta",
    "google",
    "googl",
    "microsoft",
    "msft",
    "palantir",
    "pltr",
    "股票",
    "美股",
    "股",
    "纳斯达克",
    "韭菜",
)

NARRATIVE_KEYWORDS = {
    "AI": ("ai", "agent", "bot", "grok", "chatgpt", "openai", "xai"),
    "动物": ("dog", "doge", "shib", "cat", "frog", "pepe", "bird", "monkey", "fish"),
    "马斯克系": ("elon", "tesla", "spacex", "grok", "xai"),
    "政治": ("trump", "maga", "biden", "president"),
    "Pump": ("pump", "pump.fun", "pumpfun"),
    "社区/CTO": ("cto", "community", "takeover"),
}

NARRATIVE_WEIGHTS = {
    "AI": 9,
    "动物": 7,
    "马斯克系": 8,
    "中文梗": 6,
    "政治": 5,
    "Pump": 4,
    "社区/CTO": 5,
}


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def source_hit_counts(row: dict[str, Any]) -> dict[str, int]:
    raw = row.get("source_hit_counts")
    if isinstance(raw, dict):
        return {str(key).lower(): int(to_float(value)) for key, value in raw.items() if int(to_float(value)) > 0}
    counts: dict[str, int] = {}
    for source in row.get("sources") or []:
        key = str(source or "").strip().lower()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def meme_heat_annotation(row: dict[str, Any]) -> dict[str, Any]:
    """Describe a high-heat move separately from an early-entry recommendation."""
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    volume = to_float(row.get("volume24h") or row.get("volume") or row.get("dex_volume24h"))
    liquidity = to_float(row.get("liquidity_usd") or row.get("liquidity") or row.get("dex_liquidity"))
    change_m5 = to_float(row.get("change_m5") or row.get("price_change_m5"))
    change_h1 = to_float(row.get("change_h1") or row.get("price_change_h1") or row.get("price_change_1h_pct"))
    heat_score = to_float(row.get("heat_score"))
    source_count = independent_source_count(row)
    volume_to_mcap = volume / mcap if volume > 0 and mcap > 0 else 0.0
    first_seen_mcap = to_float(row.get("watch_first_seen_mcap") or row.get("first_seen_mcap"))
    mcap_multiple = mcap / first_seen_mcap if mcap > 0 and first_seen_mcap > 0 else 0.0
    risk_flags = row.get("gmgn_risk_flags") or row.get("risk_flags") or []
    if not isinstance(risk_flags, list):
        risk_flags = [risk_flags]
    hard_risk = (
        to_float(row.get("top10_holder_pct")) >= 70
        or to_float(row.get("max_holder_pct")) >= 35
        or bool(risk_flags)
    )

    score = 0.0
    reasons: list[str] = []
    if source_count >= 5:
        score += 30
        reasons.append(f"{source_count}源共振")
    elif source_count >= 4:
        score += 24
        reasons.append(f"{source_count}源共振")
    elif source_count >= 3:
        score += 16
        reasons.append(f"{source_count}源命中")
    if heat_score >= 140:
        score += 22
        reasons.append(f"热度 {heat_score:.0f}")
    elif heat_score >= 100:
        score += 14
        reasons.append(f"热度 {heat_score:.0f}")
    if change_h1 >= 120:
        score += 24
        reasons.append(f"1h +{change_h1:.0f}%")
    elif change_h1 >= 60:
        score += 16
        reasons.append(f"1h +{change_h1:.0f}%")
    elif change_h1 >= 30:
        score += 9
        reasons.append(f"1h +{change_h1:.0f}%")
    if change_m5 >= 20:
        score += 10
        reasons.append(f"5m +{change_m5:.0f}%")
    if volume_to_mcap >= 3:
        score += 18
        reasons.append(f"量市比 {volume_to_mcap:.1f}x")
    elif volume_to_mcap >= 1.5:
        score += 12
        reasons.append(f"量市比 {volume_to_mcap:.1f}x")
    if mcap_multiple >= 3:
        score += 16
        reasons.append(f"首发后 {mcap_multiple:.1f}x")
    if liquidity >= 20_000:
        score += 4
    if hard_risk:
        score -= 35

    qualifies = (
        not hard_risk
        and source_count >= 3
        and (change_h1 >= 50 or change_m5 >= 20 or volume_to_mcap >= 1.5 or mcap_multiple >= 3)
    )
    priority = "high" if qualifies and score >= 65 else "medium" if qualifies and score >= 42 else ""
    if priority == "high":
        label = "重点热度"
        action = "热度升级 · 等回踩"
        next_step = "已拉升，不追第一根；打开GMGN看买卖流，等回踩不破或二次放量。"
        risk = "这是热度升级观察，不是首次发现买入票。"
    elif priority == "medium":
        label = "热度抬头"
        action = "热度观察"
        next_step = "继续看多源是否延续，等买盘和池子同步确认。"
        risk = "单轮热度可能退潮，暂不升级为确认票。"
    else:
        label = ""
        action = ""
        next_step = ""
        risk = ""
    return {
        "heat_priority": priority,
        "heat_priority_label": label,
        "heat_priority_score": round(max(0.0, min(100.0, score)), 2),
        "heat_priority_reason": " / ".join(reasons[:6]),
        "heat_priority_action": action,
        "heat_priority_next_step": next_step,
        "heat_priority_risk": risk,
        "heat_priority_mcap_multiple": round(mcap_multiple, 2) if mcap_multiple else 0.0,
    }


def annotate_meme_heat(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, **meme_heat_annotation(row)}


def build_meme_heat_rows(
    rows: list[dict[str, Any]],
    limit: int = 12,
    chain_scope: str | None = None,
) -> list[dict[str, Any]]:
    candidates = [annotate_meme_heat(row) for row in filter_rows_by_chain(rows, chain_scope)]
    candidates = [row for row in candidates if row.get("heat_priority") in {"high", "medium"}]
    candidates.sort(
        key=lambda row: (
            0 if row.get("heat_priority") == "high" else 1,
            -to_float(row.get("heat_priority_score")),
            -to_float(row.get("heat_score")),
            -to_float(row.get("change_h1")),
        )
    )
    return candidates[: max(0, limit)]


def meme_early_conviction_annotation(row: dict[str, Any]) -> dict[str, Any]:
    """Score early thesis strength separately from momentum and entry timing."""
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = to_float(row.get("liquidity_usd") or row.get("liquidity") or row.get("dex_liquidity"))
    volume = to_float(row.get("volume24h") or row.get("volume") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    age = to_float(row.get("pair_age_hours"))
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    holders = to_float(row.get("holders"))
    source_count = max(independent_source_count(row), len(source_groups(row)))
    sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
    narrative = analyze_narrative(row)
    narrative_tags = [str(tag) for tag in narrative.get("tags") or [] if str(tag).strip()]
    theme_tags = list(narrative_tags)
    if is_bsc_row(row) and is_stock_meme_row(row):
        theme_tags.insert(0, "币股大叙事")
    if row.get("meme_seed_candidate") and "链下热梗" not in theme_tags:
        theme_tags.append("链下热梗")
    theme_tags = list(dict.fromkeys(theme_tags))[:4]
    theme_signal = bool(theme_tags) or to_float(row.get("meme_seed_score")) >= 12

    evidence = row.get("smart_money_evidence") if isinstance(row.get("smart_money_evidence"), dict) else {}
    skill = row.get("gmgn_skill_evidence") if isinstance(row.get("gmgn_skill_evidence"), dict) else {}
    qualified_wallets = max(
        int(to_float(evidence.get("qualified_wallet_count"))),
        int(to_float(evidence.get("verified_wallet_count"))),
        int(to_float(skill.get("qualified_wallet_count"))),
    )
    smart_money = to_float(row.get("smart_money"))
    wallet_mean = to_float(skill.get("wallet_screen_mean"))

    convergence = 0.0
    if source_count >= 5:
        convergence = 22.0
    elif source_count >= 4:
        convergence = 18.0
    elif source_count >= 3:
        convergence = 13.0
    elif source_count >= 2:
        convergence = 7.0

    narrative_points = min(18.0, to_float(narrative.get("score")) * 0.45)
    if is_bsc_row(row) and is_stock_meme_row(row):
        narrative_points += 12.0
    if to_float(row.get("meme_seed_score")) >= 12:
        narrative_points += 6.0
    if to_float(row.get("tweet_narrative_score")) >= 12:
        narrative_points += 5.0
    narrative_points = min(28.0, narrative_points)

    smart_points = min(12.0, smart_money * 0.8)
    if qualified_wallets:
        smart_points += min(12.0, qualified_wallets * 4.0)
    elif wallet_mean >= 65:
        smart_points += 5.0
    if skill.get("cluster_buy"):
        smart_points += 6.0
    elif "gmgn_skills_smartmoney" in sources:
        smart_points += 2.0
    smart_points = min(24.0, smart_points)

    structure = 0.0
    if 0 < mcap <= 100_000:
        structure += 10.0
    elif mcap <= 300_000:
        structure += 8.0
    elif mcap <= 1_000_000:
        structure += 5.0
    elif mcap <= 3_000_000:
        structure += 2.0
    if 0 < age <= 3:
        structure += 8.0
    elif age <= 12:
        structure += 6.0
    elif age <= 24:
        structure += 4.0
    if liquidity >= 100_000:
        structure += 10.0
    elif liquidity >= 50_000:
        structure += 8.0
    elif liquidity >= 20_000:
        structure += 6.0
    elif liquidity > 0:
        structure += 2.0
    if 0 < top10 <= 25:
        structure += 10.0
    elif top10 <= 35:
        structure += 6.0
    elif top10 < 50:
        structure += 2.0
    if 0 < max_holder <= 12:
        structure += 5.0
    elif max_holder <= 20:
        structure += 2.0
    if 200 <= holders <= 5_000:
        structure += 4.0
    elif holders >= 100:
        structure += 2.0
    if 0.25 <= volume_to_mcap <= 4:
        structure += 4.0
    structure = min(34.0, structure)

    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    risk_penalty = min(
        28.0,
        risk_value(flags, "sniper_") * 0.35
        + risk_value(flags, "bundler_") * 0.55
        + risk_value(flags, "rug_") * 0.8,
    )
    hard_risk = top10 >= 55 or max_holder >= 20 or risk_value(flags, "rug_") >= 40 or risk_value(flags, "bundler_") >= 40
    score = max(0.0, min(100.0, convergence + narrative_points + smart_points + structure - risk_penalty))

    reasons: list[str] = []
    if theme_tags:
        reasons.append("大叙事：" + "/".join(theme_tags[:2]))
    if source_count >= 2:
        reasons.append(f"{source_count}源独立命中")
    if qualified_wallets:
        reasons.append(f"已核验聪明钱 {qualified_wallets} 个")
    elif smart_money >= 1 or "gmgn_skills_smartmoney" in sources:
        reasons.append("有聪明钱入口，历史战绩待核验")
    if top10 and top10 <= 35:
        reasons.append(f"Top10 {top10:.1f}%")
    if liquidity:
        reasons.append(f"池子 {money(liquidity)}")
    if age and age <= 24:
        reasons.append(f"池龄 {age:.1f}h")
    if risk_penalty:
        reasons.append("风险标签已扣分")

    early_window = 0 < age <= 24 and 0 < mcap <= 1_000_000
    high = (
        not hard_risk
        and early_window
        and structure >= 20
        and (
            (theme_signal and source_count >= 4)
            or qualified_wallets >= 1
            or smart_points >= 14
        )
    )
    medium = not hard_risk and early_window and source_count >= 3 and (theme_signal or smart_points >= 5 or narrative_points >= 10)
    level = "high" if high else "medium" if medium else ""
    if level == "high":
        label = "早期重点 · 大叙事" if theme_signal else "早期重点"
        action = "早期重点 · 等二次确认"
        next_step = "先核验聪明钱历史胜率和独立买入；二次确认通过后，才进入首次发现小仓试探。"
        risk = "这是早期信心层，不等于无风险买入；没有盈利历史钱包时，叙事只能提高优先级。"
    elif level == "medium":
        label = "早期关注"
        action = "早期观察"
        next_step = "继续等来源延续、买盘承接和筹码数据补齐。"
        risk = "叙事或来源还不够强，不能仅凭名称和热度追入。"
    else:
        label = action = next_step = risk = ""
    data_status = "已核验聪明钱" if qualified_wallets else "聪明钱历史待核验"
    return {
        "early_conviction_level": level,
        "early_conviction_label": label,
        "early_conviction_score": round(score, 2),
        "early_conviction_reason": " / ".join(reasons[:6]),
        "early_conviction_action": action,
        "early_conviction_next_step": next_step,
        "early_conviction_risk": risk,
        "early_conviction_data_status": data_status,
        "early_conviction_theme_tags": theme_tags,
    }


def annotate_meme_early_conviction(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, **meme_early_conviction_annotation(row)}


def build_meme_conviction_rows(
    rows: list[dict[str, Any]],
    limit: int = 12,
    chain_scope: str | None = None,
) -> list[dict[str, Any]]:
    candidates = [annotate_meme_early_conviction(row) for row in filter_rows_by_chain(rows, chain_scope)]
    candidates = [row for row in candidates if row.get("early_conviction_level") in {"high", "medium"}]
    candidates.sort(
        key=lambda row: (
            0 if row.get("early_conviction_level") == "high" else 1,
            -to_float(row.get("early_conviction_score")),
            to_float(row.get("mcap") or row.get("market_cap")),
        )
    )
    return candidates[: max(0, limit)]


def source_repeat_score(row: dict[str, Any]) -> float:
    explicit = row.get("source_repeat_score")
    if explicit not in (None, ""):
        return min(32.0, max(0.0, to_float(explicit)))
    points = 0.0
    for source, count in source_hit_counts(row).items():
        if count <= 1:
            continue
        per_hit = 4.0 if source in {"okx_trenches", "gmgn_trenches", "debot_trenches", "debot_signal", "binance_wallet_hot", "binance_wallet_signal"} else 2.0
        points += min(24.0, (count - 1) * per_hit)
    return min(32.0, points)


def repeated_trenches_hits(row: dict[str, Any]) -> int:
    counts = source_hit_counts(row)
    return max(
        counts.get("okx_trenches", 0),
        counts.get("gmgn_trenches", 0),
        counts.get("debot_trenches", 0),
        counts.get("binance_wallet_hot", 0),
    )


def source_group(source: str) -> str:
    raw = str(source or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("gmgn_"):
        return "gmgn"
    if raw.startswith("okx_"):
        return "okx"
    if raw.startswith("binance_wallet_"):
        return "binance_wallet"
    if raw.startswith("debot_"):
        return "debot"
    if raw.startswith("bsc_"):
        return "bsc_onchain"
    if raw.startswith("fourmeme_"):
        return "fourmeme"
    if raw.startswith("flap_"):
        return "flap"
    if raw.startswith("noxa_"):
        return "noxa"
    if raw.startswith("pumpfun_"):
        return "pumpfun"
    if raw.startswith("proficy_"):
        return "proficy"
    if raw.startswith("mobula_"):
        return "mobula"
    if raw.startswith("birdeye_"):
        return "birdeye"
    if raw == "985_monitor":
        return "985_monitor"
    if raw == "985_fomo_wallets":
        return "985_fomo"
    if raw == "985_smartmoney":
        return "985_smartmoney"
    if raw == "wind_monitor":
        return "wind"
    if raw in {"boost_top", "boost_latest", "ads_latest", "community_takeover", "profile_latest"}:
        return "dexscreener"
    return raw


def independent_source_count(row: dict[str, Any]) -> int:
    groups = {
        group
        for source in (row.get("sources") or [])
        for group in [source_group(str(source))]
        if group
    }
    if groups:
        return len(groups)
    labels = {
        str(label).strip().lower().replace(" ", "_")
        for label in (row.get("source_labels") or [])
        if str(label).strip()
    }
    labels -= {"ds", "alpha_ai", "alpha-ai"}
    return len(labels) or int(to_float(row.get("source_count")))


def normalized_chain_value(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", raw.replace("_", " ").replace("-", " "))


def row_chain_values(row: dict[str, Any]) -> list[str]:
    keys = ("chain", "chain_id", "chainId", "network", "blockchain", "chainIndex")
    values = [normalized_chain_value(row.get(key)) for key in keys if str(row.get(key) or "").strip()]
    return [value for value in values if value]


def is_bsc_row(row: dict[str, Any]) -> bool:
    values = row_chain_values(row)
    if any(value in BSC_CHAIN_ALIASES for value in values):
        return True
    url_text = " ".join(str(row.get(key) or "") for key in ("url", "dex_url", "pair_url")).lower()
    return any(marker in url_text for marker in ("/bsc/", "chain=bsc", "bnbchain", "pancakeswap"))


def is_robinhood_row(row: dict[str, Any]) -> bool:
    values = row_chain_values(row)
    if any(value in ROBINHOOD_CHAIN_ALIASES for value in values):
        return True
    url_text = " ".join(str(row.get(key) or "") for key in ("url", "dex_url", "pair_url")).lower()
    return any(marker in url_text for marker in ("/robinhood/", "chain=robinhood", "chainid=4663"))


def source_groups(row: dict[str, Any]) -> set[str]:
    groups = {
        group
        for source in (row.get("sources") or [])
        for group in [source_group(str(source))]
        if group
    }
    if groups:
        return groups
    for label in row.get("source_labels") or []:
        label_norm = str(label or "").strip().lower().replace(" ", "_")
        if label_norm.startswith("okx"):
            groups.add("okx")
        elif label_norm.startswith("gmgn"):
            groups.add("gmgn")
        elif label_norm.startswith("binance"):
            groups.add("binance_wallet")
        elif label_norm in {"wind", "tingfeng", "听风"}:
            groups.add("wind")
        elif label_norm in {"ds", "dexscreener"}:
            groups.add("dexscreener")
    return groups


def battlefield_text(row: dict[str, Any]) -> str:
    fields = (
        "symbol",
        "name",
        "trans_symbol",
        "trans_name",
        "trans_name_zhcn",
        "description",
        "twitter_username",
        "launchpad_platform",
        "platform",
        "url",
        "dex_url",
    )
    return " ".join(str(row.get(key) or "") for key in fields).lower()


def is_stock_meme_row(row: dict[str, Any]) -> bool:
    text = battlefield_text(row)
    return any(keyword in text for keyword in STOCK_MEME_KEYWORDS)


def priority_battlefields(row: dict[str, Any]) -> list[dict[str, Any]]:
    groups = source_groups(row)
    fields: list[dict[str, Any]] = []
    if is_bsc_row(row) and is_stock_meme_row(row):
        reason = "币安币股MEME：先跑出金狗结构后，才有币安钱包/BSC扶持或上所想象；不作为首次买入信号。"
        strength = 10 + (3 if groups & {"binance_wallet", "bsc_onchain", "fourmeme", "flap"} else 0)
        fields.append(
            {
                "key": "binance_bsc_stock_meme",
                "label": "币安币股想象",
                "score": min(14, strength),
                "reason": reason,
            }
        )
    if is_robinhood_row(row) and ("okx" in groups or str(row.get("okx_source_panel") or "").strip()):
        reason = "OKX罗宾汉：先成为大金狗/高热票后，才有OKX合约或钱包扶持想象；不作为首次买入信号。"
        strength = 11 + (4 if "okx_signal" in {str(source).lower() for source in (row.get("sources") or [])} else 0)
        fields.append(
            {
                "key": "okx_robinhood_meme",
                "label": "OKX罗宾汉想象",
                "score": min(15, strength),
                "reason": reason,
            }
        )
    return fields


def battlefield_score(row: dict[str, Any]) -> float:
    # Exchange/wallet battlefield fit is a late-stage catalyst narrative, not an entry edge.
    return 0.0


def filter_rows_by_chain(rows: list[dict[str, Any]], chain_scope: str | None) -> list[dict[str, Any]]:
    scope = str(chain_scope or "").strip().lower()
    if not scope or scope in {"*", "all", "any"}:
        return rows
    if scope in {"bsc", "bnb", "56", "bnbchain"}:
        return [row for row in rows if is_bsc_row(row)]
    return rows


def pct(value: Any, digits: int = 1) -> str:
    number = to_float(value)
    prefix = "+" if number >= 0 else ""
    return f"{prefix}{number:.{digits}f}%"


def money(value: Any) -> str:
    number = to_float(value)
    if number >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.0f}"


def infer_narrative_tags(row: dict[str, Any]) -> list[str]:
    return analyze_narrative(row)["tags"][:6]
    explicit = row.get("narrative_tags") or []
    tags: list[str] = [str(tag) for tag in explicit if str(tag).strip()]
    text = " ".join(
        str(row.get(key) or "")
        for key in (
            "symbol",
            "name",
            "trans_symbol",
            "trans_name",
            "trans_name_zhcn",
            "description",
            "twitter_username",
            "launchpad_platform",
            "platform",
            "url",
        )
    ).lower()
    for tag, keywords in NARRATIVE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            tags.append(tag)
    if re.search(r"[\u4e00-\u9fff]", text):
        tags.append("中文梗")
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped[:6]


def narrative_score(row: dict[str, Any]) -> float:
    return round(min(25.0, to_float(analyze_narrative(row)["score"])), 2)
    tags = infer_narrative_tags(row)
    points = sum(NARRATIVE_WEIGHTS.get(tag, 3) for tag in tags)
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    holders = to_float(row.get("holders"))
    if tags and smart_money >= 10:
        points += 4
    if tags and kol >= 3:
        points += 3
    if tags and 300 <= holders <= 5_000:
        points += 2
    return round(min(25.0, points), 2)


def age_missing(row: dict[str, Any]) -> bool:
    return "pair_age_hours" in row and row.get("pair_age_hours") in (None, "")


def freshness_score(row: dict[str, Any]) -> float:
    if age_missing(row):
        return 0.0
    age = to_float(row.get("pair_age_hours"))
    if age <= 0:
        return 55.0
    if age <= 1:
        return 100.0
    if age <= 6:
        return 92.0
    if age <= 24:
        return 84.0
    if age <= 48:
        return 70.0
    if age <= OLD_MEME_POOL_HOURS:
        return 55.0
    if age <= MAX_MEME_ENTRY_AGE_HOURS:
        return 25.0
    return 0.0


def pro_signal_score(row: dict[str, Any]) -> float:
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    holders = to_float(row.get("holders"))
    labels = {str(label).upper() for label in (row.get("source_labels") or [])}
    sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
    source_count = independent_source_count(row)
    potential = str(row.get("potential_label") or "").lower()
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]

    points = 0.0
    points += min(28, smart_money * 0.75)
    points += min(18, kol * 1.6)
    if smart_money >= 10 and kol >= 3:
        points += 8
    if smart_money >= 50 and kol >= 15:
        points += 8
    points += min(14, narrative_score(row) * 0.45)
    if "GMGN" in labels and "DS" in labels:
        points += 6
    elif "GMGN" in labels:
        points += 4
    if "DEBOT" in labels:
        points += 6
    if "DEBOT" in labels and ("GMGN" in labels or "DS" in labels):
        points += 4
    if "okx_signal" in sources:
        points += 8
    elif "okx_trenches" in sources:
        points += 3
    elif "OKX" in labels:
        points += 2
    if "binance_wallet_signal" in sources:
        points += 8
    elif "binance_wallet_hot" in sources:
        points += 5
    if "okx_signal" in sources and ("GMGN" in labels or "DS" in labels or any(source.startswith("gmgn_") for source in sources)):
        points += 3
    if "binance_wallet_signal" in sources and (
        "GMGN" in labels
        or "DS" in labels
        or "OKX" in labels
        or any(source.startswith(("gmgn_", "okx_")) for source in sources)
    ):
        points += 3
    points += min(8, source_repeat_score(row) * 0.28)
    if source_count >= 3:
        points += 3
    if "100x" in potential:
        points += 10
    elif "10x" in potential:
        points += 4
    elif "3x" in potential:
        points += 2
    if 300 <= holders <= 5_000:
        points += 3
    if 0.3 <= volume_to_mcap <= 4:
        points += 3
    points += min(14, gmgn_skill_score(row) * 0.18)

    points -= min(12, risk_value(flags, "sniper_") / 6)
    points -= min(18, risk_value(flags, "bundler_") / 3)
    points -= min(24, risk_value(flags, "rug_") / 2)
    return round(max(0.0, min(100.0, points)), 2)


def pro_signal_tags(row: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    labels = {str(label).upper() for label in (row.get("source_labels") or [])}
    potential = str(row.get("potential_label") or "")
    if smart_money >= 10 and kol >= 3:
        tags.append("聪明钱+KOL共振")
    elif smart_money >= 10:
        tags.append("聪明钱进场")
    elif kol >= 3:
        tags.append("KOL扩散")
    if "GMGN" in labels and "DS" in labels:
        tags.append("GMGN+DS双源")
    narr = infer_narrative_tags(row)
    if narr:
        tags.append("叙事:" + "/".join(narr[:3]))
    if potential:
        tags.append(f"潜力:{potential}")
    lifecycle = launchpad_lifecycle_summary(row)
    if lifecycle:
        tags.append(lifecycle)
    tags.extend(gmgn_skill_tags(row))
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped[:6]


def gmgn_skill_categories(row: dict[str, Any]) -> list[str]:
    labels = {str(label).upper() for label in (row.get("source_labels") or []) if str(label).strip()}
    sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    skill = row.get("gmgn_skill_evidence") if isinstance(row.get("gmgn_skill_evidence"), dict) else {}
    age = to_float(row.get("pair_age_hours"))
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))

    categories: list[str] = []
    if "GMGN" in labels or any(source.startswith("gmgn_") for source in sources):
        categories.append("GMGN入口")
    if "OKX" in labels or any(source.startswith("okx_") for source in sources):
        categories.append("OKX入口")
    if any(source.startswith("binance_wallet_") for source in sources):
        categories.append("币安钱包入口")
    if priority_battlefields(row):
        categories.append("上所想象")
    if old_meme_revival_active(row):
        categories.append("老币复活")
    if sources & {"gmgn_live_trending", "gmgn_trending", "gmgn_skills_trending"}:
        categories.append("热榜趋势")
    if sources & {"gmgn_trenches", "gmgn_skills_trenches", "okx_trenches", "binance_wallet_hot"} or (0 < age <= 6 and 0 < mcap <= 100_000):
        categories.append("新币入口")
    if smart_money >= 10:
        categories.append("聪明钱")
    if skill.get("cluster_buy"):
        categories.append("Skills聚合买入")
    elif skill.get("smartmoney_buy_count"):
        categories.append("Skills聪明钱流")
    if to_float(skill.get("wallet_screen_mean")) >= 65:
        categories.append("钱包战绩筛选")
    if skill.get("cluster_exit"):
        categories.append("Skills退出聚集")
    if skill.get("positive_signal"):
        categories.append("Skills资金信号")
    if skill.get("exit_signal"):
        categories.append("Skills退出信号")
    if to_float(skill.get("kol_buy_wallet_count")) > 0:
        categories.append("Skills KOL流")
    if 0 < to_float(skill.get("hot_search_rank")) <= 20:
        categories.append("GMGN热搜")
    if kol >= 3:
        categories.append("KOL扩散")
    if 0 < top10 <= 30 or 0 < max_holder <= 12:
        categories.append("持仓检查")
    if flags:
        categories.append("风险过滤")
    if 5_000 <= mcap <= 30_000 and 0 < age <= 2:
        categories.append("种子池")
    elif 30_000 < mcap <= 100_000 and 0 < age <= 6:
        categories.append("早期候选")
    return categories[:8]


def gmgn_skill_tags(row: dict[str, Any]) -> list[str]:
    labels = {str(label).upper() for label in (row.get("source_labels") or []) if str(label).strip()}
    sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    age = to_float(row.get("pair_age_hours"))
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    top10 = to_float(row.get("top10_holder_pct"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    potential = str(row.get("potential_label") or "")
    skill = row.get("gmgn_skill_evidence") if isinstance(row.get("gmgn_skill_evidence"), dict) else {}

    tags: list[str] = []
    if sources & {"gmgn_live_trending", "gmgn_skills_trending"}:
        tags.append("GMGN实时热榜")
    elif "gmgn_trending" in sources or "GMGN" in labels:
        tags.append("GMGN热度入口")
    if sources & {"gmgn_trenches", "gmgn_skills_trenches"}:
        tags.append("Trenches新币入口")
    if "okx_trenches" in sources:
        tags.append("OKX Trenches入口")
    if "binance_wallet_hot" in sources:
        tags.append("币安钱包热榜")
    if "binance_wallet_signal" in sources:
        tags.append("币安钱包信号")
    if skill.get("cluster_buy"):
        tags.append(f"GMGN Skills聚合买入x{int(to_float(skill.get('smartmoney_buy_wallet_count')))}")
    elif skill.get("smartmoney_buy_count"):
        tags.append(f"GMGN Skills买入x{int(to_float(skill.get('smartmoney_buy_count')))}")
    if to_float(skill.get("wallet_screen_mean")) >= 65:
        tags.append(f"钱包评估{to_float(skill.get('wallet_screen_mean')):.0f}")
    if skill.get("cluster_exit"):
        tags.append("GMGN Skills退出聚集")
    if skill.get("positive_signal"):
        tags.append("GMGN Signals买入")
    if skill.get("exit_signal"):
        tags.append("GMGN Bundler卖出")
    if to_float(skill.get("kol_buy_wallet_count")) > 0:
        tags.append(f"GMGN KOL买入x{int(to_float(skill.get('kol_buy_wallet_count')))}")
    if 0 < to_float(skill.get("hot_search_rank")) <= 20:
        tags.append(f"GMGN热搜#{int(to_float(skill.get('hot_search_rank')))}")
    if old_meme_revival_active(row):
        tags.append("老池复活首爆")
    trench_hits = repeated_trenches_hits(row)
    if trench_hits >= 3:
        tags.append(f"Trenches重复命中x{trench_hits}")
    if smart_money >= 25 and kol >= 6:
        tags.append("聪明钱+KOL强共振")
    elif smart_money >= 10:
        tags.append("聪明钱持仓/买入")
    elif kol >= 3:
        tags.append("KOL持仓/扩散")
    if 5_000 <= mcap <= 30_000 and 0 < age <= 2:
        tags.append("种子池")
    elif 30_000 < mcap <= 100_000 and 0 < age <= 6:
        tags.append("早期候选")
    if 0 < top10 <= 30:
        tags.append("Top10筹码健康")
    if "100x" in potential.lower() or "10x" in potential.lower():
        tags.append("潜力标签")
    if flags:
        tags.append("GMGN风险过滤")
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped[:6]


def gmgn_skill_score(row: dict[str, Any]) -> float:
    labels = {str(label).upper() for label in (row.get("source_labels") or []) if str(label).strip()}
    sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
    source_count = independent_source_count(row)
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    age = to_float(row.get("pair_age_hours"))
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    potential = str(row.get("potential_label") or "").lower()
    skill = row.get("gmgn_skill_evidence") if isinstance(row.get("gmgn_skill_evidence"), dict) else {}

    points = 0.0
    if "GMGN" in labels or any(source.startswith("gmgn_") for source in sources):
        points += 8
    skill_buys = to_float(skill.get("smartmoney_buy_wallet_count"))
    skill_sells = to_float(skill.get("smartmoney_sell_wallet_count"))
    skill_degens = to_float(skill.get("smart_degen_count"))
    wallet_screen = to_float(skill.get("wallet_screen_mean"))
    points += min(16, skill_buys * 5)
    points += min(10, skill_degens * 1.5)
    if wallet_screen > 0:
        points += (wallet_screen - 50) * 0.16
    if skill.get("cluster_buy"):
        points += 10
    if skill.get("cluster_exit") or skill_sells > skill_buys and skill_sells >= 2:
        points -= 18
    if sources & {"gmgn_live_trending", "gmgn_skills_trending"}:
        points += 12
    if sources & {"gmgn_trenches", "gmgn_skills_trenches"}:
        points += 10
    if skill.get("positive_signal"):
        points += 10
    if skill.get("exit_signal"):
        points -= 25
    points += min(6, to_float(skill.get("kol_buy_wallet_count")) * 2)
    hot_rank = to_float(skill.get("hot_search_rank"))
    if 0 < hot_rank <= 20:
        points += max(1, 7 - hot_rank * 0.3)
    if "okx_signal" in sources:
        points += 8
    elif "okx_trenches" in sources:
        points += 5
    elif "OKX" in labels:
        points += 3
    if "binance_wallet_signal" in sources:
        points += 8
    elif "binance_wallet_hot" in sources:
        points += 7
    points += min(10, source_repeat_score(row) * 0.35)
    if "gmgn_trending" in sources:
        points += 6
    if "GMGN" in labels and "DS" in labels:
        points += 5
    if source_count >= 3:
        points += 5

    if smart_money >= 50:
        points += 22
    elif smart_money >= 25:
        points += 16
    elif smart_money >= 10:
        points += 10
    if kol >= 15:
        points += 18
    elif kol >= 8:
        points += 14
    elif kol >= 3:
        points += 8
    if smart_money >= 10 and kol >= 3:
        points += 6

    if "100x" in potential:
        points += 10
    elif "10x" in potential:
        points += 6
    if 5_000 <= mcap <= 30_000 and 0 < age <= 2:
        points += 10
    elif 30_000 < mcap <= 100_000 and 0 < age <= 6:
        points += 8
    elif 0 < age <= 24:
        points += 5
    if 0.3 <= volume_to_mcap <= 4:
        points += 6
    if 0 < top10 <= 25:
        points += 8
    elif 25 < top10 <= 40:
        points += 3
    elif top10 >= 55:
        points -= 18
    if max_holder >= 20:
        points -= 15

    points -= min(12, risk_value(flags, "sniper_") / 6)
    points -= min(16, risk_value(flags, "bundler_") / 3)
    points -= min(22, risk_value(flags, "rug_") / 2)
    return round(max(0.0, min(100.0, points)), 2)


def risk_filter_score(row: dict[str, Any]) -> float:
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    points = 100.0
    if top10 >= 55:
        points -= 45
    elif top10 >= 40:
        points -= 20
    elif top10 > 0:
        points -= max(0.0, (top10 - 25) * 0.7)
    else:
        points -= 10
    if max_holder >= 20:
        points -= 35
    elif max_holder >= 12:
        points -= 12
    points -= min(18, risk_value(flags, "sniper_") / 4)
    points -= min(30, risk_value(flags, "bundler_") / 2)
    points -= min(42, risk_value(flags, "rug_"))
    return round(max(0.0, min(100.0, points)), 2)


def absorption_score(row: dict[str, Any]) -> float:
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = to_float(row.get("liquidity") or row.get("liquidity_usd"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    change_m5 = to_float(row.get("change_m5"))
    change_h1 = to_float(row.get("change_h1"))
    txns = to_float(row.get("txns24h"))

    points = 0.0
    if 20_000 <= liquidity <= 350_000:
        points += 28
    elif liquidity > 350_000:
        points += 18
    elif liquidity >= 8_000:
        points += 12
    if 0.3 <= volume_to_mcap <= 4:
        points += 30
    elif 4 < volume_to_mcap <= 10:
        points += 14
    elif volume_to_mcap > 10:
        points += 4
    if -8 <= change_m5 <= 18:
        points += 16
    elif change_m5 > 45:
        points -= 20
    if -10 <= change_h1 <= 80:
        points += 18
    elif 80 < change_h1 <= 140:
        points += 6
    elif change_h1 > 140:
        points -= 24
    points += min(8, txns / 180)
    return round(max(0.0, min(100.0, points)), 2)


def old_meme_revival_reasons(row: dict[str, Any]) -> list[str]:
    if age_missing(row):
        return []
    age = to_float(row.get("pair_age_hours"))
    if age <= OLD_MEME_REVIVAL_MIN_HOURS:
        return []
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = to_float(row.get("liquidity") or row.get("liquidity_usd"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    change_m5 = to_float(row.get("change_m5"))
    change_h1 = to_float(row.get("change_h1"))
    change_h24 = to_float(row.get("change_h24"))
    txns = to_float(row.get("txns24h"))
    groups = source_groups(row)
    labels = {str(label).upper() for label in (row.get("source_labels") or [])}

    reasons: list[str] = []
    if 30_000 <= mcap <= 8_000_000:
        reasons.append(f"市值{money(mcap)}仍有弹性")
    if liquidity >= 20_000:
        reasons.append(f"池子{money(liquidity)}够看盘")
    if volume >= 80_000 and 0.08 <= volume_to_mcap <= 3.5:
        reasons.append(f"成交/市值{volume_to_mcap:.2f}，不是纯死水")
    if 10 <= change_m5 < 45 or 12 <= change_h1 < 140 or 40 <= change_h24 < 700:
        reasons.append(f"老池放量启动，5m {pct(change_m5)} / 1h {pct(change_h1)}")
    if txns >= 80:
        reasons.append(f"交易数{txns:.0f}，有连续盘口")
    if len(groups) >= 2 or ("GMGN" in labels and (groups & {"okx", "proficy", "985_monitor", "985_smartmoney", "wind", "debot", "dexscreener"})):
        reasons.append("多源重新点火")
    return reasons


def old_meme_revival_score(row: dict[str, Any]) -> float:
    if age_missing(row):
        return 0.0
    age = to_float(row.get("pair_age_hours"))
    if age <= OLD_MEME_REVIVAL_MIN_HOURS:
        return 0.0
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = to_float(row.get("liquidity") or row.get("liquidity_usd"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    change_m5 = to_float(row.get("change_m5"))
    change_h1 = to_float(row.get("change_h1"))
    change_h24 = to_float(row.get("change_h24"))
    txns = to_float(row.get("txns24h"))
    groups = source_groups(row)
    labels = {str(label).upper() for label in (row.get("source_labels") or [])}

    points = 0.0
    if 30_000 <= mcap <= 1_000_000:
        points += 18
    elif mcap <= 5_000_000:
        points += 14
    elif mcap <= 8_000_000:
        points += 8
    if 20_000 <= liquidity <= 350_000:
        points += 16
    elif liquidity > 350_000:
        points += 8
    if volume >= 80_000 and 0.08 <= volume_to_mcap <= 1.5:
        points += 18
    elif 1.5 < volume_to_mcap <= 3.5:
        points += 8
    if 10 <= change_m5 < 45:
        points += 18
    elif 12 <= change_h1 < 80:
        points += 18
    elif 80 <= change_h1 < 140:
        points += 8
    elif 40 <= change_h24 < 350:
        points += 10
    elif 350 <= change_h24 < 700:
        points += 5
    if txns >= 400:
        points += 10
    elif txns >= 80:
        points += 6
    if len(groups) >= 3:
        points += 14
    elif len(groups) >= 2:
        points += 10
    elif "GMGN" in labels:
        points += 5
    points += min(8, source_repeat_score(row) * 0.2)
    points += min(8, smart_kol_score(row) * 0.08)
    if change_m5 >= 45 or change_h1 >= 140 or change_h24 >= 700:
        points -= 25
    if risk_filter_score(row) < 55:
        points -= 25
    return round(max(0.0, min(100.0, points)), 2)


def old_meme_revival_active(row: dict[str, Any]) -> bool:
    return old_meme_revival_score(row) >= 58 and len(old_meme_revival_reasons(row)) >= 4


def holder_quality_score(row: dict[str, Any]) -> float:
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    holders = to_float(row.get("holders"))
    points = 100.0
    if top10 >= 50:
        points -= 35
    elif top10 >= 35:
        points -= 16
    elif 0 < top10 <= 25:
        points += 4
    else:
        points -= 8
    if max_holder >= 18:
        points -= 24
    elif max_holder >= 12:
        points -= 10
    if 100 <= holders <= 5_000:
        points += 5
    elif holders and holders < 100:
        points -= 18
    elif holders > 12_000:
        points -= 8
    return round(max(0.0, min(100.0, points)), 2)


def security_filter_score(row: dict[str, Any]) -> float:
    points = risk_filter_score(row)
    flags = [str(flag).lower() for flag in (row.get("gmgn_risk_flags") or [])]
    hard_words = ("honeypot", "mintable", "freeze", "blacklist")
    if any(any(word in flag for word in hard_words) for flag in flags):
        points = min(points, 35.0)
    return round(max(0.0, min(100.0, points)), 2)


def liquidity_age_score(row: dict[str, Any]) -> float:
    return round(absorption_score(row) * 0.55 + freshness_score(row) * 0.45, 2)


def dev_trust_score(row: dict[str, Any]) -> float:
    graduation = to_float(row.get("dev_graduation_rate"))
    ath_mcap = to_float(row.get("dev_ath_mcap") or row.get("dev_best_ath_mcap"))
    created = to_float(row.get("dev_created_token_count"))
    rug_count = to_float(row.get("dev_rug_count"))
    if not any(value > 0 for value in (graduation, ath_mcap, created, rug_count)):
        return 55.0
    points = 45.0
    points += min(22, graduation * 55)
    if ath_mcap >= 1_000_000:
        points += 16
    if 1 <= created <= 12:
        points += 8
    elif created > 30:
        points -= 18
    points -= min(35, rug_count * 12)
    return round(max(0.0, min(100.0, points)), 2)


def smart_kol_score(row: dict[str, Any]) -> float:
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    source_count = independent_source_count(row)
    points = min(40, smart_money * 1.0) + min(26, kol * 2.4)
    if smart_money >= 10 and kol >= 3:
        points += 10
    if source_count >= 3:
        points += 8
    points += min(8, narrative_score(row) * 0.25)
    return round(max(0.0, min(100.0, points)), 2)


def bot_manipulation_score(row: dict[str, Any]) -> float:
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    points = 100.0
    points -= min(20, risk_value(flags, "sniper_") / 4)
    points -= min(28, risk_value(flags, "bundler_") / 2)
    points -= min(36, risk_value(flags, "rat_") / 2)
    points -= min(42, risk_value(flags, "rug_"))
    if to_float(row.get("change_m5")) > 45:
        points -= 12
    return round(max(0.0, min(100.0, points)), 2)


def cliff_risk_score(row: dict[str, Any]) -> float:
    safety = (
        security_filter_score(row) * 0.45
        + holder_quality_score(row) * 0.25
        + bot_manipulation_score(row) * 0.30
    )
    return round(max(0.0, min(100.0, 100.0 - safety)), 2)


def risk_deduction_reasons(row: dict[str, Any], penalty_tags: list[str] | None = None) -> list[str]:
    reasons: list[str] = []
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or []) if str(flag).strip()]
    reasons.extend(flags[:2])

    narrative_penalty_tags = penalty_tags if penalty_tags is not None else row.get("narrative_penalty_tags") or []
    narrative_penalty_tags = [str(tag) for tag in narrative_penalty_tags if str(tag).strip()]
    reasons.extend(narrative_penalty_tags[:1])

    risk_score = cliff_risk_score(row)
    risk_label = cliff_risk_label(risk_score)
    if risk_score >= 35:
        reasons.append(f"风险{risk_score:.0f}/{risk_label}")

    risk_filter = risk_filter_score(row)
    if risk_filter <= 60:
        reasons.append(f"过滤{risk_filter:.0f}")

    security = security_filter_score(row)
    if security <= 55:
        reasons.append(f"安全{security:.0f}")

    holder = holder_quality_score(row)
    if holder <= 55:
        reasons.append(f"筹码{holder:.0f}")

    bot_score = bot_manipulation_score(row)
    if bot_score <= 65:
        reasons.append(f"刷量{bot_score:.0f}")

    replay_adjustment = to_float(row.get("replay_score_adjustment"))
    if replay_adjustment < 0:
        reasons.append(f"回测{replay_adjustment:.0f}")

    unique: list[str] = []
    for reason in reasons:
        if reason and reason not in unique:
            unique.append(reason)
    return unique[:4]


def risk_deduction_summary(row: dict[str, Any], penalty_tags: list[str] | None = None) -> str:
    reasons = risk_deduction_reasons(row, penalty_tags)
    return " / ".join(reasons) if reasons else "暂无硬伤"


def cliff_hype_score(row: dict[str, Any]) -> float:
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    change_m5 = max(0.0, to_float(row.get("change_m5")))
    change_h1 = max(0.0, to_float(row.get("change_h1")))
    txns = to_float(row.get("txns24h"))

    points = 0.0
    points += min(28.0, to_float(row.get("heat_score")) * 0.55)
    points += min(22.0, volume_to_mcap * 14.0)
    points += min(15.0, change_h1 / 4.0)
    points += min(10.0, change_m5 / 2.0)
    points += min(10.0, txns / 180.0)
    points += min(8.0, narrative_score(row) * 0.32)
    points += min(7.0, pro_signal_score(row) * 0.08)
    return round(max(0.0, min(100.0, points)), 2)


def cliff_gem_score(row: dict[str, Any]) -> float:
    risk = cliff_risk_score(row)
    hype = cliff_hype_score(row)
    conviction = gold_dog_conviction_score(row)
    score = conviction * 0.45 + hype * 0.25 + (100.0 - risk) * 0.30
    if risk >= 55:
        score = min(score, 45.0)
    elif risk >= 35:
        score = min(score, 70.0)
    if hard_risk_reason(row):
        score = min(score, 30.0)
    return round(max(0.0, min(100.0, score)), 2)


def cliff_risk_label(score: float) -> str:
    if score >= 70:
        return "高危"
    if score >= 45:
        return "偏高"
    if score >= 25:
        return "中等"
    return "低"


def cliff_signal_label(score: float) -> str:
    if score >= 80:
        return "强"
    if score >= 60:
        return "良好"
    if score >= 35:
        return "早期"
    return "弱"


def entry_stage(row: dict[str, Any]) -> str:
    if age_missing(row):
        return "未知"
    age = to_float(row.get("pair_age_hours"))
    if 0 < age <= 24:
        return "新池"
    if age <= OLD_MEME_POOL_HOURS:
        return "发酵"
    if old_meme_revival_active(row):
        return "老币复活"
    if age <= MAX_MEME_ENTRY_AGE_HOURS:
        return "后排"
    return "老池"


def entry_level(row: dict[str, Any]) -> str:
    conviction = gold_dog_conviction_score(row)
    if hard_risk_reason(row):
        return "高危"
    if conviction >= 82:
        return "强候选"
    if conviction >= 68:
        return "可试探"
    if conviction >= 52:
        return "等确认"
    return "放弃"


def launchpad_lifecycle_summary(row: dict[str, Any]) -> str:
    stage = str(row.get("launchpad_lifecycle_stage") or "").strip()
    label = str(row.get("launchpad_stage_label") or "").strip()
    progress = to_float(row.get("bonding_curve_progress_pct") or row.get("curve_progress_pct"))
    buys = to_float(row.get("purchase_count") or row.get("buy_count"))
    velocity = to_float(row.get("bnb_per_hour"))
    migrated = bool(row.get("migration_confirmed"))
    if not any([stage, label, progress, buys, velocity, migrated]):
        return ""
    bits: list[str] = []
    if label:
        bits.append(label)
    elif stage:
        bits.append(stage)
    if progress:
        bits.append(f"曲线{progress:.1f}%")
    if buys:
        bits.append(f"买入{buys:.0f}次")
    if velocity:
        bits.append(f"{velocity:.2f}BNB/h")
    if migrated:
        bits.append("已迁移")
    return "发射台: " + " / ".join(bits[:4])


def gold_dog_rationale(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    lifecycle = launchpad_lifecycle_summary(row)
    if lifecycle:
        reasons.append(lifecycle)
    if entry_stage(row) == "新池":
        reasons.append("池龄在早期窗口")
    if smart_kol_score(row) >= 65:
        reasons.append("聪明钱/KOL有共振")
    if holder_quality_score(row) >= 80:
        reasons.append("Top10和大户集中度可接受")
    if security_filter_score(row) >= 80 and bot_manipulation_score(row) >= 80:
        reasons.append("未触发硬风险过滤")
    tags = infer_narrative_tags(row)
    if tags:
        reasons.append("叙事: " + "/".join(tags[:3]))
    revival_reasons = old_meme_revival_reasons(row)
    if old_meme_revival_active(row) and revival_reasons:
        reasons.append("老币复活: " + " / ".join(revival_reasons[:3]))
    for battlefield in priority_battlefields(row):
        reasons.append("后验催化: " + str(battlefield["reason"]))
    skill_tags = gmgn_skill_tags(row)
    if skill_tags:
        labels = {str(label).upper() for label in (row.get("source_labels") or [])}
        sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
        has_non_gmgn_aggregator = (
            "OKX" in labels
            or "DEBOT" in labels
            or any(source.startswith("okx_") or source.startswith("debot_") for source in sources)
        )
        prefix = "聚合信号" if has_non_gmgn_aggregator else "GMGN技能"
        reasons.append(prefix + ": " + "/".join(skill_tags[:3]))
    if liquidity_age_score(row) >= 70:
        reasons.append("池子和成交有承接")
    return reasons[:6]


def gold_dog_score(row: dict[str, Any]) -> float:
    score = (
        holder_quality_score(row) * 0.25
        + security_filter_score(row) * 0.20
        + liquidity_age_score(row) * 0.15
        + dev_trust_score(row) * 0.15
        + smart_kol_score(row) * 0.15
        + bot_manipulation_score(row) * 0.10
    )
    return round(max(0.0, min(100.0, score)), 2)


def gold_dog_conviction_score(row: dict[str, Any]) -> float:
    score = gold_dog_score(row) * 0.42 + entry_score(row) * 0.32 + pro_signal_score(row) * 0.18
    if gmgn_skill_score(row) >= 70:
        score += 5
    if to_float(row.get("smart_money")) >= 25 and to_float(row.get("kol")) >= 6:
        score += 6
    if 0 < to_float(row.get("top10_holder_pct")) <= 25:
        score += 4
    if 0 < to_float(row.get("pair_age_hours")) <= 24:
        score += 4
    if -8 <= to_float(row.get("change_m5")) <= 18 and 5 <= to_float(row.get("change_h1")) <= 80:
        score += 4
    if independent_source_count(row) >= 3:
        score += 3
    score += min(4, source_repeat_score(row) * 0.12)
    return round(max(0.0, min(100.0, score)), 2)


def backtest_profile_score(row: dict[str, Any]) -> float:
    labels = {str(label).lower() for label in (row.get("source_labels") or []) if str(label).strip()}
    sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
    age = to_float(row.get("pair_age_hours"))
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    top10 = to_float(row.get("top10_holder_pct"))
    conviction = to_float(row.get("gold_dog_conviction_score")) or gold_dog_conviction_score(row)
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]

    score = 40.0
    if "gmgn" in labels or any(source.startswith("gmgn_") for source in sources):
        score += 10
    if "ds" in labels or any("profile" in source or "boost" in source for source in sources):
        score += 7
    if "birdeye" in labels or any(source.startswith("birdeye_") for source in sources):
        score += 8
    if "mobula" in labels or any(source.startswith("mobula_") for source in sources):
        score += 4
    if "debot" in labels or any(source.startswith("debot_") for source in sources):
        score += 8
    if "okx_signal" in sources:
        score += 7
    elif "okx_trenches" in sources:
        score += 4
    elif "okx" in labels:
        score += 3
    score += min(8, source_repeat_score(row) * 0.25)
    if not labels and not sources:
        score -= 18

    if 0 < age <= 24:
        score += 16
    elif 24 < age <= OLD_MEME_POOL_HOURS:
        score += 5
    elif age > OLD_MEME_POOL_HOURS:
        score -= 14

    if 0 < mcap <= 1_000_000:
        score += 14
    elif mcap <= 5_000_000:
        score += 7
    elif mcap > 5_000_000:
        score -= 8

    if smart_money >= 20:
        score += 12
    if kol >= 5:
        score += 8
    if 0 < top10 <= 25:
        score += 8
    elif top10 >= 40:
        score -= 12
    if conviction >= 75:
        score += 12
    if gmgn_skill_score(row) >= 70:
        score += 8
    score -= min(10, risk_value(flags, "sniper_") / 6)
    score -= min(14, risk_value(flags, "bundler_") / 3)
    score -= min(18, risk_value(flags, "rug_") / 3)
    return round(max(0.0, min(100.0, score)), 2)


def backtest_rule_tags(row: dict[str, Any]) -> list[str]:
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    age = to_float(row.get("pair_age_hours"))
    top10 = to_float(row.get("top10_holder_pct"))
    conviction = to_float(row.get("gold_dog_conviction_score")) or gold_dog_conviction_score(row)
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    tags: list[str] = []
    if conviction >= 85 and 0 < age <= 12 and 0 < mcap <= 300_000:
        tags.append("回测强画像:高确认+12h新池+$300K内")
    elif conviction >= 75 and 0 < age <= 12 and 0 < mcap <= 300_000:
        tags.append("回测强画像:确认75+12h新池+$300K内")
    if 0 < top10 <= 30 and 0 < age <= 12 and 0 < mcap <= 300_000:
        tags.append("回测强画像:健康Top10+新池微市值")
    if volume_to_mcap >= 0.5 and 0 < age <= 12 and 0 < mcap <= 300_000:
        tags.append("回测画像:量市比放大+新池微市值")
    if conviction >= 75 and smart_money >= 20 and kol >= 5 and 0 < age <= 24 and 0 < mcap <= 1_000_000 and 0 < top10 <= 25:
        tags.append("回测画像:严格金狗画像")
    if gmgn_skill_score(row) >= 70 and smart_money >= 10 and kol >= 3 and 0 < age <= 24 and 0 < mcap <= 1_000_000:
        tags.append("GMGN技能画像:热榜+聪明钱+早期池")
    return tags


def backtest_rule_score(row: dict[str, Any]) -> float:
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    age = to_float(row.get("pair_age_hours"))
    top10 = to_float(row.get("top10_holder_pct"))
    conviction = to_float(row.get("gold_dog_conviction_score")) or gold_dog_conviction_score(row)
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))

    score = 0.0
    if conviction >= 85 and 0 < age <= 12 and 0 < mcap <= 300_000:
        score = 100.0
    elif conviction >= 75 and 0 < age <= 12 and 0 < mcap <= 300_000:
        score = 94.0
    elif conviction >= 85 and 0 < age <= 12 and 0 < mcap <= 3_000_000:
        score = 86.0
    elif conviction >= 75 and smart_money >= 20 and kol >= 5 and 0 < age <= 24 and 0 < mcap <= 1_000_000 and 0 < top10 <= 25:
        score = 84.0
    elif gmgn_skill_score(row) >= 70 and smart_money >= 10 and kol >= 3 and 0 < age <= 24 and 0 < mcap <= 1_000_000:
        score = 82.0
    elif conviction >= 70 and 0 < age <= 12 and 0 < mcap <= 300_000:
        score = 80.0
    elif volume_to_mcap >= 0.5 and 0 < age <= 12 and 0 < mcap <= 300_000:
        score = 76.0
    elif 0 < age <= 12 and 0 < mcap <= 300_000:
        score = 68.0
    else:
        score = backtest_profile_score(row) * 0.45

    if top10 >= 40:
        score -= 18
    if age > 24:
        score -= 16
    if mcap > 3_000_000:
        score -= 10
    return round(max(0.0, min(100.0, score)), 2)


def risk_value(flags: list[str], prefix: str) -> float:
    for flag in flags:
        if not str(flag).startswith(prefix):
            continue
        match = re.search(r"([-+]?\d+(?:\.\d+)?)", str(flag))
        if match:
            return to_float(match.group(1))
    return 0.0


def hard_risk_reason(row: dict[str, Any]) -> str:
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    rug = risk_value(flags, "rug_")
    bundler = risk_value(flags, "bundler_")
    if top10 >= 55 or max_holder >= 20:
        return f"筹码太集中，Top10 {pct(top10)}，最大钱包 {pct(max_holder)}。"
    if rug >= 40:
        return f"GMGN 风险过硬，{', '.join(flags)}。"
    if bundler >= 40:
        return f"Bundler 占比太高，{', '.join(flags)}。"
    return ""


SOFT_CONFIRMATION_ONLY_SOURCES = {"proficy_trending", "985_monitor", "985_fomo_wallets", "985_smartmoney", "wind_monitor"}


def soft_confirmation_only_signal(row: dict[str, Any]) -> bool:
    sources = {str(source).lower() for source in (row.get("sources") or []) if str(source).strip()}
    if not (sources & SOFT_CONFIRMATION_ONLY_SOURCES):
        return False
    confirming_sources = [
        source
        for source in sources
        if source not in SOFT_CONFIRMATION_ONLY_SOURCES
        and (
            source.startswith("gmgn_")
            or source.startswith("okx_")
            or source.startswith("debot_")
            or source.startswith("birdeye_")
            or source.startswith("mobula_")
            or source in {"boost_top", "boost_latest", "ads_latest", "community_takeover", "profile_latest", "bsc_onchain"}
            or source.endswith("_launchpad")
            or source.endswith("_onchain")
            or source.endswith("_live")
        )
    ]
    return not confirming_sources


def entry_score(row: dict[str, Any]) -> float:
    score = to_float(row.get("score"))
    heat = to_float(row.get("heat_score"))
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = to_float(row.get("liquidity") or row.get("liquidity_usd"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    change_m5 = to_float(row.get("change_m5"))
    change_h1 = to_float(row.get("change_h1"))
    txns = to_float(row.get("txns24h"))
    age = to_float(row.get("pair_age_hours"))
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    holders = to_float(row.get("holders"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    skill = row.get("gmgn_skill_evidence") if isinstance(row.get("gmgn_skill_evidence"), dict) else {}

    points = 0.0
    points += min(26, smart_money * 0.55)
    points += min(18, kol * 1.15)
    if 300 <= holders <= 3_000:
        points += 7
    elif holders > 3_000:
        points += 3

    if 50_000 <= mcap <= 3_000_000:
        points += 16
    elif 3_000_000 < mcap <= 15_000_000:
        points += 9
    elif 15_000_000 < mcap <= 40_000_000:
        points += 3
    elif mcap > 40_000_000:
        points -= 8

    if 10_000 <= liquidity <= 500_000:
        points += 12
    elif liquidity >= 500_000:
        points += 5
    elif 0 < liquidity < 10_000:
        points -= 8

    if 0.25 <= volume_to_mcap <= 4:
        points += 16
    elif 4 < volume_to_mcap <= 8:
        points += 6
    elif volume_to_mcap > 8:
        points -= 8

    if -10 <= change_h1 <= 80:
        points += 11
    elif 80 < change_h1 <= 140:
        points += 2
    elif change_h1 > 140:
        points -= 18
    if change_m5 > 45:
        points -= 18
    elif -8 <= change_m5 <= 18:
        points += 5

    points += min(8, txns / 160)
    points += min(8, score / 12)
    points += min(6, heat / 10)
    points += min(12, to_float(skill.get("smartmoney_buy_wallet_count")) * 4)
    if skill.get("cluster_buy"):
        points += 8
    if skill.get("cluster_exit"):
        points -= 18
    points += min(18, narrative_score(row))
    if 0 < age <= 48:
        points += 5
    elif age > 168:
        points -= 5

    points -= min(12, risk_value(flags, "sniper_") / 5)
    points -= min(18, risk_value(flags, "bundler_") / 3)
    points -= min(24, risk_value(flags, "rug_") / 2)
    calibrated = points * 0.62 + min(20, pro_signal_score(row) * 0.18)
    return round(max(0.0, min(100.0, calibrated)), 2)


def classify_meme_row(row: dict[str, Any]) -> dict[str, Any]:
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = to_float(row.get("liquidity") or row.get("liquidity_usd"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_mcap = volume / mcap if mcap > 0 else 0.0
    change_m5 = to_float(row.get("change_m5"))
    change_h1 = to_float(row.get("change_h1"))
    change_h24 = to_float(row.get("change_h24"))
    txns = to_float(row.get("txns24h"))
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    age = to_float(row.get("pair_age_hours"))
    score = entry_score(row)
    tags = infer_narrative_tags(row)
    narrative = analyze_narrative(row)
    narr_score = narrative_score(row)
    narr_text = "、".join(tags)
    pro_score = pro_signal_score(row)
    pro_tags = pro_signal_tags(row)
    skill_score = gmgn_skill_score(row)
    skill_tags = gmgn_skill_tags(row)
    skill_evidence = row.get("gmgn_skill_evidence") if isinstance(row.get("gmgn_skill_evidence"), dict) else {}
    battlefields = priority_battlefields(row)
    revival_score = old_meme_revival_score(row)
    revival_reasons = old_meme_revival_reasons(row)
    pro_text = "、".join(pro_tags)
    risk_summary_score = cliff_risk_score(row)
    hype_summary_score = cliff_hype_score(row)
    gem_summary_score = cliff_gem_score(row)
    base_result = {
        "entry_score": score,
        "gold_dog_score": gold_dog_score(row),
        "cliff_risk_score": risk_summary_score,
        "cliff_hype_score": hype_summary_score,
        "cliff_gem_score": gem_summary_score,
        "cliff_risk_label": cliff_risk_label(risk_summary_score),
        "cliff_hype_label": cliff_signal_label(hype_summary_score),
        "cliff_gem_label": cliff_signal_label(gem_summary_score),
        "freshness_score": freshness_score(row),
        "narrative_score": narr_score,
        "narrative_tags": tags,
        "narrative_quality": narrative["quality"],
        "narrative_primary_tag": narrative["primary_tag"],
        "narrative_penalty_tags": narrative["penalty_tags"],
        "narrative_reasons": narrative["reasons"],
        "pro_signal_score": pro_score,
        "pro_signal_tags": pro_tags,
        "gmgn_skill_score": skill_score,
        "gmgn_skill_tags": skill_tags,
        "gmgn_skill_categories": gmgn_skill_categories(row),
        "gmgn_skill_evidence": skill_evidence,
        "priority_battlefields": battlefields,
        "priority_battlefield": battlefields[0]["key"] if battlefields else "",
        "priority_battlefield_label": battlefields[0]["label"] if battlefields else "",
        "priority_battlefield_score": battlefield_score(row),
        "priority_battlefield_reasons": [str(item["reason"]) for item in battlefields],
        "old_meme_revival_score": revival_score,
        "old_meme_revival_active": revival_score >= 58 and len(revival_reasons) >= 4,
        "old_meme_revival_reasons": revival_reasons,
        "risk_filter_score": risk_filter_score(row),
        "absorption_score": absorption_score(row),
        "holder_quality_score": holder_quality_score(row),
        "security_filter_score": security_filter_score(row),
        "liquidity_age_score": liquidity_age_score(row),
        "dev_trust_score": dev_trust_score(row),
        "smart_kol_score": smart_kol_score(row),
        "bot_manipulation_score": bot_manipulation_score(row),
        "gold_dog_conviction_score": gold_dog_conviction_score(row),
        "entry_stage": entry_stage(row),
        "entry_level": entry_level(row),
        "gold_dog_rationale": gold_dog_rationale(row),
        "backtest_profile_score": backtest_profile_score(row),
        "backtest_rule_score": backtest_rule_score(row),
        "backtest_rule_tags": backtest_rule_tags(row),
        "risk_deduction_reasons": risk_deduction_reasons(row, narrative["penalty_tags"]),
        "risk_deduction_summary": risk_deduction_summary(row, narrative["penalty_tags"]),
        **meme_early_conviction_annotation(row),
    }

    hard_risk = hard_risk_reason(row)
    if hard_risk:
        return {
            **base_result,
            "recommendation_bucket": "danger",
            "recommendation_label": BUCKET_LABELS["danger"],
            "recommendation_action": "风险太高别碰",
            "recommendation_reason": hard_risk,
            "recommendation_risk": "风险信号已经压过聪明钱和热度。",
            "recommendation_next_step": "只做风险记录，不进上车池。",
        }
    if skill_evidence.get("cluster_exit"):
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "聪明钱退出聚集",
            "recommendation_reason": "GMGN Skills 发现多地址聪明钱卖出金额超过买入，先拦截追入。",
            "recommendation_risk": "平台聪明钱退出信号优先于热度标签。",
            "recommendation_next_step": "等待卖压衰减、价格承接和新的独立买入聚合。",
        }
    if row.get("market_data_pending"):
        progress = to_float(row.get("bonding_curve_progress_pct") or row.get("curve_progress_pct"))
        buys = to_float(row.get("purchase_count") or row.get("buy_count"))
        stage = str(row.get("launchpad_lifecycle_stage") or "")
        lifecycle = launchpad_lifecycle_summary(row)
        action = "链上首发"
        reason = "发射台或链上首发事件已捕获，但市值、池子和成交还没被 Dex/GMGN 补齐。"
        next_step = "等 GMGN、DexScreener、DeBot 或发射台二次数据跟上，再进入金狗确认和语音提醒。"
        if stage == "migrated" or row.get("migration_confirmed"):
            action = "已迁移待确认"
            reason = f"{lifecycle or '发射台显示已迁移'}，但还没拿到 Dex/GMGN 的真实行情二源。"
            next_step = "等 Pancake 池子、流动性、市值、成交和 GMGN/DS 二源补齐。"
        elif progress >= 95:
            action = "曲线快毕业"
            reason = f"{lifecycle or f'曲线 {progress:.1f}%'}，进入很多早鸟雷达盯的关键区间。"
            next_step = "盯迁移上池、PairCreated、流动性和 GMGN/DS 二源确认。"
        elif progress >= 60 or buys >= 2:
            action = "曲线加速"
            reason = f"{lifecycle or '曲线开始加速'}，可进早鸟观察，但还不是金狗确认票。"
            next_step = "继续盯购买次数、曲线 95%+ 和迁移事件。"
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": action,
            "recommendation_reason": reason,
            "recommendation_risk": "这只是第一层发现流，不是确认票。",
            "recommendation_next_step": next_step,
        }
    if soft_confirmation_only_signal(row):
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "仅观察",
            "recommendation_reason": "985/Proficy/听风 代表早期热度或群体查币流，还缺 GMGN、OKX、DeBot、DexScreener 或链上二源确认。",
            "recommendation_risk": "单一热度流可能来自围观、争议、付费曝光或下跌排雷，不等于买盘确认。",
            "recommendation_next_step": "等二源命中、流动性承接和筹码信息补齐后再进入金狗候选。",
        }
    if liquidity < 5_000 or volume < 50_000 or txns < 20:
        return {
            **base_result,
            "recommendation_bucket": "reject",
            "recommendation_label": BUCKET_LABELS["reject"],
            "recommendation_action": "放弃观察",
            "recommendation_reason": f"池子或成交太弱，流动性 {money(liquidity)}，成交 {money(volume)}。",
            "recommendation_risk": "滑点和撤池风险太高。",
            "recommendation_next_step": "不进潜力榜，只留热度记录。",
        }
    if age_missing(row):
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "信息不全",
            "recommendation_reason": "池龄拿不到，不能确认是不是刚出来的新币。",
            "recommendation_risk": "信息缺口会把老池误判成新机会。",
            "recommendation_next_step": "先放热度榜观察，等补到池龄再进金狗候选。",
        }
    if change_m5 >= 45 or change_h1 >= 140 or change_h24 >= 700:
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "太热别追",
            "recommendation_reason": f"短线已经过热，5m {pct(change_m5)}，1h {pct(change_h1)}。",
            "recommendation_risk": "第一波追进去容易接尖顶。",
            "recommendation_next_step": "等回踩不破或二次放量。",
        }
    if old_meme_revival_active(row):
        revival_reasons = old_meme_revival_reasons(row)
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "老币复活盯回踩",
            "recommendation_reason": "；".join(revival_reasons[:4]),
            "recommendation_risk": "老池复活第一根容易冲高回落，不能和首发新池用同一套入场。",
            "recommendation_next_step": "打开GMGN看交易流和回踩承接，二次放量或不破启动位再单独评估。",
        }
    if age > OLD_MEME_POOL_HOURS:
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "老池不追",
            "recommendation_reason": f"池龄 {age / 24:.0f} 天，已经过了 Meme 上车雷达的早期窗口。",
            "recommendation_risk": "老池再热也更像二级热度，不适合挤占上车候选。",
            "recommendation_next_step": "放到热度榜或复盘榜看，不进上车池。",
        }
    if score >= 42 and pro_score < 45 and gold_dog_score(row) < 80:
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "等回踩",
            "recommendation_reason": f"热度够，{'叙事 ' + narr_text + '，' if narr_text else ''}但专业信号只有 {pro_score:.0f}，还没看到聪明钱/KOL共振。",
            "recommendation_risk": "这种更像热榜噪音，容易一波流。",
            "recommendation_next_step": "等聪明钱、KOL、多源命中或回踩承接再看。",
        }
    if score >= 58:
        return {
            **base_result,
            "recommendation_bucket": "ambush",
            "recommendation_label": BUCKET_LABELS["ambush"],
            "recommendation_action": "可小仓试探",
            "recommendation_reason": (
                (f"{pro_text}，" if pro_text else "")
                + (f"叙事 {narr_text}，" if narr_text and not pro_text else "")
                + f"聪明钱 {smart_money:.0f}、KOL {kol:.0f}，量市比 {volume_to_mcap:.2f}，"
                f"市值 {money(mcap)}。"
            ),
            "recommendation_risk": "Meme 波动大，只适合小仓和快进快出观察。",
            "recommendation_next_step": "盯买盘延续、池子加厚、5m 不破前低。",
        }
    if score >= 42:
        return {
            **base_result,
            "recommendation_bucket": "pullback",
            "recommendation_label": BUCKET_LABELS["pullback"],
            "recommendation_action": "等回踩",
            "recommendation_reason": f"{'叙事 ' + narr_text + '，' if narr_text else ''}有热度但上车分只有 {score:.0f}，还差确认。",
            "recommendation_risk": "没有二次放量前容易变成一波流。",
            "recommendation_next_step": "等回踩不破或聪明钱继续增加。",
        }
    return {
        **base_result,
        "recommendation_bucket": "reject",
        "recommendation_label": BUCKET_LABELS["reject"],
        "recommendation_action": "放弃观察",
        "recommendation_reason": f"热度有但上车分太低，分数 {score:.0f}，风险 {', '.join(flags) or '一般'}。",
        "recommendation_risk": "更像热闹，不像机会。",
        "recommendation_next_step": "继续放在 Meme 热度榜观察。",
    }


def apply_replay_calibration(row: dict[str, Any], calibration: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    action = str(row.get("recommendation_action") or "")
    item = (calibration or {}).get(action)
    if not item:
        return row
    adjustment = to_float(item.get("score_adjustment"))
    existing_conviction = row.get("gold_dog_conviction_score")
    conviction_score = (
        to_float(existing_conviction)
        if existing_conviction not in (None, "")
        else gold_dog_conviction_score(row)
    )
    result = {
        **row,
        "gold_dog_conviction_score": conviction_score,
        "replay_score_adjustment": adjustment,
        "replay_calibration_level": item.get("risk_level") or "unknown",
        "replay_calibration_reason": item.get("reason") or "",
    }
    if adjustment < 0:
        result["recommendation_reason"] = f"{row.get('recommendation_reason') or ''} 回测提示：{item.get('reason') or '同类动作偏弱'}"
        result["recommendation_risk"] = f"{row.get('recommendation_risk') or ''} 回测只作画像校准，不一刀切降级。"
    return result


def build_meme_potential_rows(
    rows: list[dict[str, Any]],
    limit: int | None = 8,
    include_rejects: bool = False,
    replay_calibration: dict[str, dict[str, Any]] | None = None,
    chain_scope: str | None = None,
) -> list[dict[str, Any]]:
    bucket_order = {"ambush": 0, "pullback": 1, "danger": 2, "reject": 3}
    scoped_rows = filter_rows_by_chain(rows, chain_scope)
    enriched = [apply_replay_calibration({**row, **classify_meme_row(row)}, replay_calibration) for row in scoped_rows]
    if not include_rejects:
        enriched = [row for row in enriched if row["recommendation_bucket"] != "reject"]
        enriched = [row for row in enriched if not row.get("market_data_pending")]
        enriched = [row for row in enriched if not age_missing(row)]
        enriched = [
            row for row in enriched
            if to_float(row.get("pair_age_hours")) <= MAX_NEW_POOL_CANDIDATE_AGE_HOURS
            or row.get("old_meme_revival_active")
        ]
        enriched = [
            row for row in enriched
            if not str(row.get("quote_status") or "").strip()
            or str(row.get("quote_status") or "").strip().lower() == "fresh"
        ]
    enriched.sort(
        key=lambda row: (
            bucket_order[row["recommendation_bucket"]],
            -to_float(row.get("backtest_rule_score")),
            -to_float(row.get("backtest_profile_score")),
            -to_float(row.get("gold_dog_conviction_score")),
            -to_float(row.get("gold_dog_score")),
            -to_float(row.get("entry_score")),
            -to_float(row.get("pro_signal_score")),
            -to_float(row.get("narrative_score")),
            -to_float(row.get("smart_money")),
            -to_float(row.get("kol")),
            -to_float(row.get("score")),
            row.get("symbol") or "",
        )
    )
    for index, row in enumerate(enriched, start=1):
        row["recommendation_rank"] = index
    return enriched[:limit] if limit is not None else enriched
