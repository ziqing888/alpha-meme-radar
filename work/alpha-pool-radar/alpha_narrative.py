from __future__ import annotations

import re
from typing import Any


NARRATIVE_KEYWORDS = {
    "AI": ("ai", "agent", "bot", "grok", "chatgpt", "openai", "xai", "llm", "eliza"),
    "动物": ("dog", "doge", "shib", "cat", "frog", "pepe", "bird", "monkey", "fish", "pet"),
    "马斯克系": ("elon", "tesla", "spacex", "grok", "xai"),
    "政治": ("trump", "maga", "biden", "president", "election"),
    "直播盘": ("live", "stream", "twitch", "kick", "直播"),
    "中文盘": ("中文", "龙", "华人", "中国"),
    "社区/CTO": ("cto", "community", "takeover"),
    "Pump": ("pump", "pump.fun", "pumpfun"),
    "链下热梗": (),
}

NARRATIVE_WEIGHTS = {
    "AI": 10,
    "动物": 7,
    "马斯克系": 8,
    "政治": 5,
    "直播盘": 7,
    "中文盘": 6,
    "社区/CTO": 5,
    "Pump": 3,
    "链下热梗": 14,
}

HYPE_WORDS = ("100x", "1000x", "moon", "gem", "pump", "send", "ape", "elon", "trump", "pepe", "doge", "ai")

TWEET_SOURCE_WEIGHTS = {
    "cz_binance": 24,
    "cz": 24,
    "heyibinance": 22,
    "heyi": 22,
    "yihe": 22,
    "binance": 20,
    "binancezh": 20,
    "binancewallet": 18,
    "bnbchain": 18,
}

TWEET_SEED_KEYWORDS = {
    "person": ("cz", "yi", "heyi", "binance", "bnb"),
    "animal": ("dog", "doge", "cat", "frog", "pepe", "panda", "monkey", "bird", "fish"),
    "emotion": ("happy", "angry", "crazy", "love", "sad", "fun", "开心", "生气", "快乐", "好玩"),
    "meme": ("4", "gm", "moon", "build", "builder", "builders", "cook", "cooking", "ape"),
    "ai": ("ai", "agent", "bot", "robot", "grok", "brain"),
    "chinese_meme": ("雪王", "来了", "冲", "兄弟", "发财", "土狗"),
}

TWEET_SEED_WEIGHTS = {
    "person": 8,
    "animal": 14,
    "emotion": 10,
    "meme": 11,
    "ai": 12,
    "chinese_meme": 14,
    "hashtag": 13,
    "cashtag": 14,
    "quoted": 12,
    "chinese_phrase": 11,
    "proper_keyword": 8,
}

TWEET_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "because",
    "been",
    "binance",
    "bnb",
    "but",
    "can",
    "crypto",
    "day",
    "for",
    "from",
    "have",
    "just",
    "keep",
    "more",
    "not",
    "now",
    "our",
    "that",
    "the",
    "this",
    "today",
    "was",
    "with",
    "you",
    "your",
}

CHINESE_SEED_STOPWORDS = {
    "大家",
    "今天",
    "一点",
    "我们",
    "他们",
    "这个",
    "那个",
    "不是",
    "可以",
    "没有",
}


def to_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def row_text(row: dict[str, Any]) -> str:
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
        "twitter",
        "website",
        "telegram",
    )
    return " ".join(str(row.get(key) or "") for key in fields).lower()


def social_count(row: dict[str, Any]) -> int:
    return len([key for key in ("twitter", "website", "telegram") if str(row.get(key) or "").strip()])


def keyword_matches(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii() and re.fullmatch(r"[a-z0-9_]+", keyword):
        return re.search(rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])", text) is not None
    return keyword in text


def infer_tags(row: dict[str, Any]) -> list[str]:
    explicit = [str(tag) for tag in (row.get("narrative_tags") or []) if str(tag).strip()]
    text = row_text(row)
    tags = list(explicit)
    for tag, keywords in NARRATIVE_KEYWORDS.items():
        if any(keyword_matches(text, keyword) for keyword in keywords):
            tags.append(tag)
    if re.search(r"[\u4e00-\u9fff]", text):
        tags.append("中文盘")
    if row.get("meme_seed_matches"):
        tags.append("链下热梗")
    deduped: list[str] = []
    for tag in tags:
        if tag and tag not in deduped:
            deduped.append(tag)
    return deduped[:8]


def hype_word_count(text: str) -> int:
    return sum(1 for word in HYPE_WORDS if keyword_matches(text, word))


def penalty_tags(row: dict[str, Any], tags: list[str]) -> list[str]:
    text = row_text(row)
    penalties: list[str] = []
    if len(tags) >= 5 and hype_word_count(text) >= 6 and social_count(row) == 0:
        penalties.append("关键词堆砌")
    if "100x" in text and social_count(row) == 0:
        penalties.append("空喊100x")
    if not str(row.get("description") or "").strip() and social_count(row) == 0 and len(tags) <= 1:
        penalties.append("故事不完整")
    return penalties


def narrative_quality(score: float) -> str:
    if score >= 25:
        return "strong"
    if score >= 16:
        return "medium"
    return "weak"


def narrative_reasons(row: dict[str, Any], tags: list[str], penalties: list[str], score: float) -> list[str]:
    if score <= 0 and not tags:
        return ["叙事弱：没有明确热点、故事或传播载体。"]
    reasons: list[str] = []
    if tags:
        reasons.append("热点标签：" + "/".join(tags[:4]))
    socials = social_count(row)
    if socials:
        reasons.append(f"社媒完整度：{socials}/3")
    if to_float(row.get("smart_money")) >= 10 or to_float(row.get("kol")) >= 3:
        reasons.append("聪明钱/KOL共振")
    if str(row.get("description") or "").strip():
        reasons.append("有一句话故事")
    seed_matches = row.get("meme_seed_matches") or []
    if seed_matches:
        top_seed = seed_matches[0]
        reasons.append(f"链下热梗种子：{top_seed.get('keyword') or top_seed.get('term') or '--'}")
    if penalties:
        reasons.append("扣分：" + "/".join(penalties))
    return reasons[:5]


def analyze_narrative(row: dict[str, Any]) -> dict[str, Any]:
    tags = infer_tags(row)
    penalties = penalty_tags(row, tags)
    score = sum(NARRATIVE_WEIGHTS.get(tag, 3) for tag in tags)
    socials = social_count(row)
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    holders = to_float(row.get("holders"))

    if tags and str(row.get("description") or "").strip():
        score += 5
    if tags and socials >= 1:
        score += 4
    if tags and socials >= 2:
        score += 4
    if tags and socials >= 3:
        score += 3
    if tags and smart_money >= 10:
        score += 4
    if tags and kol >= 3:
        score += 3
    if tags and 300 <= holders <= 5_000:
        score += 2
    if "AI" in tags and ("动物" in tags or "直播盘" in tags):
        score += 4
    if "关键词堆砌" in penalties:
        score -= 16
    if "空喊100x" in penalties:
        score -= 8
    if "故事不完整" in penalties:
        score -= 6

    final_score = round(max(0.0, min(40.0, score)), 2)
    return {
        "score": final_score,
        "tags": tags,
        "quality": narrative_quality(final_score),
        "primary_tag": tags[0] if tags else "",
        "penalty_tags": penalties,
        "reasons": narrative_reasons(row, tags, penalties, final_score),
    }


def normalize_account(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value or "").strip().lower().lstrip("@"))


def tweet_text(tweet: dict[str, Any]) -> str:
    return str(tweet.get("text") or tweet.get("full_text") or tweet.get("content") or "")


def seed_source_weight(account: str) -> int:
    normalized = normalize_account(account)
    return TWEET_SOURCE_WEIGHTS.get(normalized, 6)


def iter_tweet_keyword_hits(text: str) -> list[tuple[str, str]]:
    lowered = text.lower()
    hits: list[tuple[str, str]] = []
    for category, keywords in TWEET_SEED_KEYWORDS.items():
        for keyword in keywords:
            if keyword_matches(lowered, keyword.lower()):
                hits.append((keyword, category))
    hits.extend(iter_dynamic_tweet_keyword_hits(text))
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for keyword, category in hits:
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((keyword, category))
    return deduped


def clean_dynamic_keyword(value: str) -> str:
    return re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+|[^0-9A-Za-z\u4e00-\u9fff]+$", "", value).strip()


def add_dynamic_hit(hits: list[tuple[str, str]], keyword: str, category: str) -> None:
    cleaned = clean_dynamic_keyword(keyword)
    if not cleaned:
        return
    lowered = cleaned.lower()
    if lowered in TWEET_STOPWORDS or cleaned in CHINESE_SEED_STOPWORDS:
        return
    if cleaned.isascii() and len(cleaned) < 3 and not cleaned.isupper():
        return
    if len(cleaned) > 32:
        return
    hits.append((cleaned, category))


def iter_dynamic_tweet_keyword_hits(text: str) -> list[tuple[str, str]]:
    cleaned_text = re.sub(r"https?://\S+", " ", text)
    hits: list[tuple[str, str]] = []
    for match in re.finditer(r"(?<!\w)#([A-Za-z0-9_\u4e00-\u9fff]{2,24})", cleaned_text):
        add_dynamic_hit(hits, match.group(1), "hashtag")
    for match in re.finditer(r"(?<!\w)\$([A-Z][A-Z0-9]{1,12})\b", cleaned_text):
        add_dynamic_hit(hits, match.group(1), "cashtag")
    for match in re.finditer(r"[\"“”'‘’]([^\"“”'‘’]{2,24})[\"“”'‘’]", cleaned_text):
        add_dynamic_hit(hits, match.group(1), "quoted")
    for match in re.finditer(r"[\u4e00-\u9fff]{2,6}", cleaned_text):
        add_dynamic_hit(hits, match.group(0), "chinese_phrase")
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9]{2,18}\b", cleaned_text):
        add_dynamic_hit(hits, match.group(0), "proper_keyword")
    return hits


def seed_reason(account: str, keyword: str, category: str) -> str:
    label = account or "unknown"
    if category in ("animal", "chinese_meme", "meme"):
        return f"{label} 推文出现可造梗关键词 {keyword}"
    if category == "emotion":
        return f"{label} 推文带情绪词 {keyword}"
    return f"{label} 推文出现叙事关键词 {keyword}"


def extract_tweet_narrative_seeds(tweets: list[dict[str, Any]], *, limit: int = 40) -> dict[str, Any]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for tweet in tweets:
        account = normalize_account(tweet.get("account") or tweet.get("username") or tweet.get("author") or "")
        text = tweet_text(tweet)
        for keyword, category in iter_tweet_keyword_hits(text):
            key = (keyword.lower(), account)
            source_weight = seed_source_weight(account)
            score = source_weight + TWEET_SEED_WEIGHTS.get(category, 5)
            current = merged.get(key)
            if current:
                current["score"] = max(to_float(current.get("score")), score)
                current["mentions"] = int(current.get("mentions") or 1) + 1
                continue
            merged[key] = {
                "keyword": keyword,
                "category": category,
                "source_account": account,
                "source_weight": source_weight,
                "score": score,
                "mentions": 1,
                "created_at": tweet.get("created_at") or "",
                "reason": seed_reason(account, keyword, category),
            }
    seeds = sorted(
        merged.values(),
        key=lambda seed: (
            to_float(seed.get("score")),
            int(seed.get("mentions") or 0),
            str(seed.get("keyword") or ""),
        ),
        reverse=True,
    )[:limit]
    return {
        "source_count": len({normalize_account(tweet.get("account") or tweet.get("username") or tweet.get("author") or "") for tweet in tweets}),
        "seed_count": len(seeds),
        "top_seed": seeds[0] if seeds else None,
        "seeds": seeds,
    }


def token_match_text(row: dict[str, Any]) -> str:
    values = [row_text(row)]
    tags = row.get("narrative_tags") or []
    values.extend(str(tag).lower() for tag in tags)
    return " ".join(values)


def matching_tweet_seeds(row: dict[str, Any], seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = token_match_text(row)
    matches = []
    for seed in seeds:
        keyword = str(seed.get("keyword") or "").strip()
        if keyword and keyword_matches(text, keyword.lower()):
            matches.append(seed)
    matches.sort(key=lambda seed: to_float(seed.get("score")), reverse=True)
    return matches


def apply_tweet_narrative_seeds(rows: list[dict[str, Any]], seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        next_row = dict(row)
        matches = matching_tweet_seeds(next_row, seeds)
        if not matches:
            stale_score = to_float(next_row.get("tweet_narrative_score"))
            if stale_score:
                next_row["score"] = round(max(0.0, to_float(next_row.get("score")) - stale_score), 2)
            next_row["tweet_narrative_score"] = 0.0
            next_row["tweet_narrative_matches"] = []
            next_row["sentiment_trigger"] = None
            if next_row.get("narrative_reasons"):
                next_row["narrative_reasons"] = [
                    reason
                    for reason in list(next_row.get("narrative_reasons") or [])
                    if not str(reason).startswith("推文种子：")
                ]
            enriched.append(next_row)
            continue
        score = round(min(18.0, sum(to_float(seed.get("score")) for seed in matches[:3]) / 6), 2)
        next_row["tweet_narrative_score"] = score
        next_row["tweet_narrative_matches"] = matches[:5]
        next_row["sentiment_trigger"] = matches[0]
        next_row["score"] = round(to_float(next_row.get("score")) + score, 2)
        reasons = list(next_row.get("narrative_reasons") or [])
        reasons.append(f"推文种子：{matches[0].get('source_account')} / {matches[0].get('keyword')}")
        next_row["narrative_reasons"] = reasons[-6:]
        enriched.append(next_row)
    return enriched


def list_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def first_text_value(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_meme_seed_item(value: Any, index: int) -> dict[str, Any] | None:
    if isinstance(value, str):
        keyword = value.strip()
        if not keyword:
            return None
        return {
            "keyword": keyword,
            "aliases": [],
            "search_terms": [keyword],
            "category": "offchain_meme",
            "source_account": "manual",
            "score": 18,
            "mentions": 1,
            "evidence_count": 0,
            "community_count": 0,
            "trend": "unknown",
            "crypto_discovered": None,
            "reason": f"链下热梗：{keyword}",
        }
    if not isinstance(value, dict):
        return None

    keyword = first_text_value(value, ("keyword", "term", "phrase", "name", "meme", "title"))
    if not keyword:
        return None
    aliases = list_values(value.get("aliases") or value.get("alias") or value.get("names"))
    pumpfun_terms = list_values(value.get("pumpfun_terms") or value.get("pumpfun_search_terms") or value.get("search_terms"))
    evidence = value.get("evidence_posts") or value.get("evidence") or value.get("posts") or []
    communities = value.get("communities") or value.get("source_communities") or value.get("audiences") or []
    evidence_count = len(evidence) if isinstance(evidence, list) else to_float(value.get("evidence_count"))
    community_count = len(communities) if isinstance(communities, list) else to_float(value.get("community_count"))
    trend = str(value.get("trend") or value.get("trend_status") or value.get("acceleration") or "unknown").strip().lower()
    crypto_discovered = value.get("crypto_discovered")
    if isinstance(crypto_discovered, str):
        crypto_discovered = crypto_discovered.strip().lower() in {"1", "true", "yes", "on", "discovered", "已发现"}

    score = 14.0 + min(18.0, evidence_count * 2.5) + min(12.0, community_count * 4.0)
    if trend in {"accelerating", "加速", "rising", "up"}:
        score += 8
    elif trend in {"fading", "weakening", "减弱", "down"}:
        score -= 8
    if crypto_discovered is False:
        score += 5
    elif crypto_discovered is True:
        score -= 5

    search_terms = [keyword, *aliases, *pumpfun_terms]
    deduped_terms: list[str] = []
    for term in search_terms:
        cleaned = str(term).strip()
        if cleaned and cleaned.lower() not in {item.lower() for item in deduped_terms}:
            deduped_terms.append(cleaned)

    return {
        "keyword": keyword,
        "aliases": aliases,
        "search_terms": deduped_terms[:12],
        "category": str(value.get("category") or "offchain_meme"),
        "source_account": str(value.get("source") or value.get("source_account") or "grok"),
        "score": round(max(0.0, min(60.0, to_float(value.get("score")) or score)), 2),
        "mentions": int(to_float(value.get("mentions")) or max(1, evidence_count)),
        "evidence_count": int(evidence_count),
        "community_count": int(community_count),
        "trend": trend,
        "origin": value.get("origin") or value.get("first_seen") or "",
        "crypto_discovered": crypto_discovered,
        "reason": value.get("reason") or f"链下热梗：{keyword}",
        "rank": index + 1,
    }


def normalize_meme_seed_terms(payload: Any, *, limit: int = 80) -> dict[str, Any]:
    if isinstance(payload, dict):
        raw_terms = (
            payload.get("meme_seed_terms")
            or payload.get("offchain_meme_seeds")
            or payload.get("seeds")
            or payload.get("terms")
            or []
        )
    else:
        raw_terms = payload
    if not isinstance(raw_terms, list):
        return {"enabled": True, "source_count": 0, "seed_count": 0, "top_seed": None, "seeds": [], "error": "meme_seed_terms must be a list"}

    seeds = [seed for index, item in enumerate(raw_terms) if (seed := normalize_meme_seed_item(item, index))]
    seeds.sort(
        key=lambda seed: (
            to_float(seed.get("score")),
            int(seed.get("evidence_count") or 0),
            int(seed.get("community_count") or 0),
            str(seed.get("keyword") or ""),
        ),
        reverse=True,
    )
    seeds = seeds[:limit]
    return {
        "enabled": True,
        "source_count": len({str(seed.get("source_account") or "") for seed in seeds}),
        "seed_count": len(seeds),
        "top_seed": seeds[0] if seeds else None,
        "seeds": seeds,
    }


def is_bsc_row(row: dict[str, Any]) -> bool:
    values = {
        str(row.get("chain") or "").strip().lower(),
        str(row.get("chain_id") or "").strip().lower(),
        str(row.get("chainId") or "").strip().lower(),
    }
    return bool(values & {"bsc", "56", "bnb", "bnb chain", "binance smart chain", "binance-smart-chain"})


def row_seed_source_text(row: dict[str, Any]) -> str:
    values: list[str] = []
    for key in ("source", "source_family", "source_origin", "holder_source", "launchpad_platform", "platform"):
        value = row.get(key)
        if value:
            values.append(str(value))
    values.extend(str(source) for source in (row.get("sources") or []))
    values.extend(str(label) for label in (row.get("source_labels") or []))
    return " ".join(values).lower()


def is_allowed_meme_seed_source(row: dict[str, Any]) -> bool:
    source_text = row_seed_source_text(row)
    return any(
        marker in source_text
        for marker in (
            "fourmeme",
            "four.meme",
            "flap",
            "pancake",
            "bsc_onchain",
            "pair_created",
            "gmgn",
            "debot",
        )
    )


def matching_meme_seed_terms(row: dict[str, Any], seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not is_bsc_row(row) or not is_allowed_meme_seed_source(row):
        return []
    text = token_match_text(row)
    matches: list[dict[str, Any]] = []
    for seed in seeds:
        terms = seed.get("search_terms") or [seed.get("keyword")]
        if any(keyword_matches(text, str(term).strip().lower()) for term in terms if str(term).strip()):
            matches.append(seed)
    matches.sort(key=lambda seed: to_float(seed.get("score")), reverse=True)
    return matches


def apply_meme_seed_terms(rows: list[dict[str, Any]], seeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        next_row = dict(row)
        matches = matching_meme_seed_terms(next_row, seeds)
        stale_score = to_float(next_row.get("meme_seed_score"))
        if not matches:
            if stale_score:
                next_row["score"] = round(max(0.0, to_float(next_row.get("score")) - stale_score), 2)
            next_row["meme_seed_score"] = 0.0
            next_row["meme_seed_matches"] = []
            next_row["meme_seed_terms"] = []
            next_row["meme_seed_candidate"] = False
            next_row["meme_seed_gate"] = "no_seed_match"
            if next_row.get("narrative_reasons"):
                next_row["narrative_reasons"] = [
                    reason
                    for reason in list(next_row.get("narrative_reasons") or [])
                    if not str(reason).startswith("链下热梗种子：")
                ]
            enriched.append(next_row)
            continue

        score = round(min(22.0, sum(to_float(seed.get("score")) for seed in matches[:3]) / 7), 2)
        next_row["meme_seed_score"] = score
        next_row["meme_seed_matches"] = matches[:5]
        next_row["meme_seed_terms"] = [str(seed.get("keyword") or "") for seed in matches[:5] if str(seed.get("keyword") or "").strip()]
        next_row["meme_seed_candidate"] = True
        next_row["meme_seed_gate"] = "seed_only"
        next_row["score"] = round(max(0.0, to_float(next_row.get("score")) - stale_score) + score, 2)
        reasons = [
            reason
            for reason in list(next_row.get("narrative_reasons") or [])
            if not str(reason).startswith("链下热梗种子：")
        ]
        reasons.append(f"链下热梗种子：{matches[0].get('keyword')}")
        next_row["narrative_reasons"] = reasons[-6:]
        enriched.append(next_row)
    return enriched
