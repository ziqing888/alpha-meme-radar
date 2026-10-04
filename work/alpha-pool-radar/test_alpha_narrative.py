import importlib.util
import sys
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


narrative = load_module("alpha_narrative", BASE / "alpha_narrative.py")


def test_analyze_narrative_rewards_coherent_story_with_social_surface():
    row = {
        "symbol": "GROKDOG",
        "name": "Grok Dog AI",
        "description": "An AI pet agent that posts every hour and reacts to crypto Twitter.",
        "twitter": "https://x.com/grokdog",
        "website": "https://grokdog.ai",
        "telegram": "https://t.me/grokdog",
        "smart_money": 18,
        "kol": 6,
        "holders": 950,
    }

    result = narrative.analyze_narrative(row)

    assert {"AI", "动物", "马斯克系"}.issubset(set(result["tags"]))
    assert result["quality"] == "strong"
    assert result["score"] >= 30
    assert any("社媒" in reason for reason in result["reasons"])
    assert any("聪明钱/KOL" in reason for reason in result["reasons"])


def test_keyword_stuffing_is_penalized_even_when_many_hot_words_match():
    stuffed = {
        "symbol": "AIELONTRUMPPEPEDOGE100X",
        "name": "AI Elon Trump Pepe Doge 100x Pump",
        "description": "AI AI AI Elon Trump Pepe Doge moon 100x 1000x.",
    }
    focused = {
        "symbol": "AIPET",
        "name": "AI Pet",
        "description": "A tiny AI pet that replies to holders and posts daily lore.",
        "twitter": "https://x.com/aipet",
        "smart_money": 12,
        "kol": 4,
    }

    stuffed_result = narrative.analyze_narrative(stuffed)
    focused_result = narrative.analyze_narrative(focused)

    assert "关键词堆砌" in stuffed_result["penalty_tags"]
    assert focused_result["score"] > stuffed_result["score"]


def test_plain_token_has_weak_narrative_and_clear_reason():
    result = narrative.analyze_narrative({"symbol": "PLAIN", "name": "Plain Token"})

    assert result["tags"] == []
    assert result["quality"] == "weak"
    assert result["score"] == 0.0
    assert result["reasons"] == ["叙事弱：没有明确热点、故事或传播载体。"]


def test_extract_tweet_narrative_seeds_keeps_memeable_keywords_from_major_accounts():
    tweets = [
        {
            "account": "cz_binance",
            "text": "Happy builders, keep cooking. The orange cat is watching. 4",
            "created_at": "2026-08-20T12:00:00Z",
        },
        {
            "account": "heyibinance",
            "text": "雪王来了，大家今天开心一点，BNB community is fun",
            "created_at": "2026-08-20T12:01:00Z",
        },
    ]

    result = narrative.extract_tweet_narrative_seeds(tweets)
    keywords = {seed["keyword"] for seed in result["seeds"]}

    assert {"cat", "雪王", "happy", "开心", "4"}.issubset(keywords)
    assert result["source_count"] == 2
    assert result["top_seed"]["keyword"] in keywords
    assert all(seed["source_account"] in {"cz_binance", "heyibinance"} for seed in result["seeds"])


def test_extract_tweet_narrative_seeds_keeps_dynamic_terms_not_in_static_dictionary():
    tweets = [
        {
            "account": "cz_binance",
            "text": "The new #GiggleAcademy mascot is called \"MoonBiscuit\". $MBIS is funny.",
            "created_at": "2026-08-20T12:00:00Z",
        },
        {
            "account": "heyibinance",
            "text": "今天看到一个新梗：香蕉猫，真的很好玩。",
            "created_at": "2026-08-20T12:01:00Z",
        },
    ]

    result = narrative.extract_tweet_narrative_seeds(tweets)
    keywords = {seed["keyword"] for seed in result["seeds"]}

    assert {"GiggleAcademy", "MoonBiscuit", "MBIS", "香蕉猫"}.issubset(keywords)
    assert any(seed["category"] == "hashtag" for seed in result["seeds"])
    assert any(seed["category"] == "cashtag" for seed in result["seeds"])
    assert any(seed["category"] == "chinese_phrase" for seed in result["seeds"])


def test_apply_tweet_narrative_seeds_adds_trigger_to_matching_meme_candidate():
    seed_report = narrative.extract_tweet_narrative_seeds(
        [
            {
                "account": "binance",
                "text": "Orange cat energy on BNB today. Happy community.",
                "created_at": "2026-08-20T12:00:00Z",
            }
        ]
    )
    rows = [
        {"symbol": "OCAT", "name": "Orange Cat", "chain": "bsc", "score": 50},
        {"symbol": "PLAIN", "name": "Plain Token", "chain": "solana", "score": 50},
    ]

    enriched = narrative.apply_tweet_narrative_seeds(rows, seed_report["seeds"])

    matched = next(row for row in enriched if row["symbol"] == "OCAT")
    unmatched = next(row for row in enriched if row["symbol"] == "PLAIN")
    assert matched["tweet_narrative_score"] > 0
    assert matched["score"] > 50
    assert matched["sentiment_trigger"]["keyword"] == "cat"
    assert "推文种子" in matched["narrative_reasons"][-1]
    assert unmatched.get("tweet_narrative_score", 0) == 0
    assert unmatched["score"] == 50


def test_apply_tweet_narrative_seeds_matches_dynamic_terms_to_bsc_candidates():
    seed_report = narrative.extract_tweet_narrative_seeds(
        [
            {
                "account": "cz_binance",
                "text": "The new #GiggleAcademy mascot is called \"MoonBiscuit\".",
                "created_at": "2026-08-20T12:00:00Z",
            }
        ]
    )
    rows = [
        {"symbol": "MBIS", "name": "MoonBiscuit", "chain": "bsc", "score": 50},
        {"symbol": "OTHER", "name": "Other Meme", "chain": "bsc", "score": 50},
    ]

    enriched = narrative.apply_tweet_narrative_seeds(rows, seed_report["seeds"])

    matched = next(row for row in enriched if row["symbol"] == "MBIS")
    unmatched = next(row for row in enriched if row["symbol"] == "OTHER")
    assert matched["tweet_narrative_score"] > 0
    assert matched["sentiment_trigger"]["keyword"] == "MoonBiscuit"
    assert unmatched["tweet_narrative_score"] == 0.0


def test_apply_tweet_narrative_seeds_clears_stale_trigger_when_keyword_disappears():
    rows = [
        {
            "symbol": "OCAT",
            "name": "Orange Cat",
            "score": 76.33,
            "tweet_narrative_score": 6.33,
            "tweet_narrative_matches": [{"keyword": "cat"}],
            "sentiment_trigger": {"keyword": "cat"},
            "narrative_reasons": ["推文种子：cz_binance / cat", "链上成交确认"],
        }
    ]

    enriched = narrative.apply_tweet_narrative_seeds(rows, [])

    assert enriched[0]["tweet_narrative_score"] == 0.0
    assert enriched[0]["tweet_narrative_matches"] == []
    assert enriched[0]["sentiment_trigger"] is None
    assert enriched[0]["score"] == 70.0
    assert enriched[0]["narrative_reasons"] == ["链上成交确认"]


def test_normalize_meme_seed_terms_accepts_grok_payload():
    payload = {
        "meme_seed_terms": [
                {
                    "keyword": "MoonBiscuit",
                    "aliases": ["MoonSnack", "MBIS"],
                    "pumpfun_terms": ["BISCUITMOON"],
                "evidence_posts": [{}, {}, {}, {}, {}],
                "communities": ["gaming", "food", "tiktok"],
                "trend": "accelerating",
                "crypto_discovered": False,
                "origin": "public X meme thread",
                "reason": "跨圈层开始模仿",
            }
        ]
    }

    result = narrative.normalize_meme_seed_terms(payload)
    seed = result["seeds"][0]

    assert result["seed_count"] == 1
    assert seed["keyword"] == "MoonBiscuit"
    assert {"MoonSnack", "MBIS", "BISCUITMOON"}.issubset(set(seed["search_terms"]))
    assert seed["score"] > 30
    assert seed["crypto_discovered"] is False


def test_apply_meme_seed_terms_matches_only_bsc_allowed_sources():
    seed_report = narrative.normalize_meme_seed_terms(
        {
            "meme_seed_terms": [
                {
                    "keyword": "MoonBiscuit",
                    "aliases": ["MBIS"],
                    "evidence_posts": [{}, {}, {}],
                    "communities": ["gaming"],
                    "trend": "accelerating",
                    "crypto_discovered": False,
                }
            ]
        }
    )
    rows = [
        {"symbol": "MBIS", "name": "MoonBiscuit", "chain": "bsc", "sources": ["flap_launchpad"], "score": 50},
        {"symbol": "MBIS", "name": "MoonBiscuit", "chain": "solana", "sources": ["flap_launchpad"], "score": 50},
        {"symbol": "MBIS", "name": "MoonBiscuit", "chain": "bsc", "sources": ["profile_latest"], "score": 50},
    ]

    enriched = narrative.apply_meme_seed_terms(rows, seed_report["seeds"])

    assert enriched[0]["meme_seed_candidate"] is True
    assert enriched[0]["meme_seed_gate"] == "seed_only"
    assert enriched[0]["meme_seed_score"] > 0
    assert enriched[0]["score"] > 50
    assert enriched[1]["meme_seed_candidate"] is False
    assert enriched[1]["score"] == 50
    assert enriched[2]["meme_seed_candidate"] is False
    assert enriched[2]["score"] == 50
