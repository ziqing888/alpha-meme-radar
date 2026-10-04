"""Meme candidate discovery for the Alpha radar report."""

from __future__ import annotations

import os
import re
import json
import hashlib
import shutil
import subprocess
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import alpha_pool_radar as alpha
import alpha_monitor_v3 as monitor_v3
from alpha_monitor_chains import normalize_monitor_chain
import alpha_okx_market
from alpha_smart_money_evidence import QUOTE_FRESH_SECONDS, parse_timestamp

try:
    import winreg  # type: ignore
except Exception:  # noqa: BLE001
    winreg = None


ROOT = Path(__file__).resolve().parents[2]
DEX_PROFILES_LATEST_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS_TOP_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_BOOSTS_LATEST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_ADS_LATEST_URL = "https://api.dexscreener.com/ads/latest/v1"
DEX_CTO_LATEST_URL = "https://api.dexscreener.com/community-takeovers/latest/v1"
MOBULA_DEFAULT_TRENDING_URL = "https://demo-api.mobula.io/api/1/metadata/trendings"
BIRDEYE_DEFAULT_TRENDING_URLS = (
    "https://public-api.birdeye.so/defi/token_trending?sort_by=rank&sort_type=asc&offset=0&limit={limit}",
    "https://public-api.birdeye.so/defi/v3/token/meme/list?sort_by=volume_24h_usd&sort_type=desc&offset=0&limit={limit}",
)
DEFAULT_MEME_SOURCE_INBOX_FILES = {
    "gmgn_cli_smartmoney": ("gmgn-wallet-flow.json",),
    "gmgn_skills_smartmoney": ("gmgn-skills-smartmoney.json",),
    "gmgn_skills_trending": ("gmgn-skills-trending.json",),
    "gmgn_skills_signal": ("gmgn-skills-signal.json",),
    "gmgn_skills_trenches": ("gmgn-skills-trenches.json",),
    "gmgn_skills_kol": ("gmgn-skills-kol.json",),
    "gmgn_skills_hot_searches": ("gmgn-skills-hot-searches.json",),
    "bsc_onchain": ("bsc-pancake-pairs.json", "bsc-onchain.json"),
    "arc_onchain": ("arc-onchain.json",),
    "fourmeme_launchpad": ("fourmeme-launches.json", "fourmeme-token-create.json"),
    "flap_launchpad": ("flap-launches.json", "flap-token-created.json"),
    "noxa_launchpad": ("noxa-launches.json", "noxa-rbh-signals.json"),
    "proficy_trending": ("proficy-trending.json",),
    "985_monitor": ("985-monitor-fast.json", "985-monitor.json"),
    "985_fomo_wallets": ("985-fomo-wallets.json",),
    "985_smartmoney": ("985-smartmoney.json",),
    "wind_monitor": ("wind-monitor.json", "wind-gmgn-subscriptions.json", "wind-ca-feed.json", "tingfeng-monitor.json"),
    "pumpfun_live": ("pumpfun-live.json", "pumpfun-new.json"),
    "pumpfun_onchain": ("pumpfun-onchain.json",),
    "okx_trenches": ("okx-trenches.json", "okx-memepump-stream.json", "okx-memepump-rest.json"),
    "okx_signal": ("okx-signal-stream.json", "okx-signal-rest.json"),
    "binance_wallet_hot": ("binance-wallet-hot.json", "binance-wallet-radar.json", "binance-wallet-meme-rush.json"),
    "binance_wallet_signal": ("binance-wallet-signals.json", "binance-wallet-smart-money.json"),
    "debot_trenches": ("debot-trenches.json",),
    "debot_signal": ("debot-signals.json", "debot-signal.json"),
    "birdeye_trending": ("birdeye-trending.json",),
}
GMGN_DEFAULT_RANK_URLS = (
    "https://gmgn.ai/defi/quotation/v1/rank/sol/swaps/1h?orderby=swaps&direction=desc&limit={limit}",
    "https://gmgn.ai/defi/quotation/v1/rank/base/swaps/1h?orderby=swaps&direction=desc&limit={limit}",
    "https://gmgn.ai/defi/quotation/v1/rank/bsc/swaps/1h?orderby=swaps&direction=desc&limit={limit}",
    "https://gmgn.ai/defi/quotation/v1/rank/eth/swaps/1h?orderby=swaps&direction=desc&limit={limit}",
)
DEFAULT_MEME_CHAINS = "bsc,robinhood"
GMGN_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://gmgn.ai/",
    "Origin": "https://gmgn.ai",
}


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def windows_environment_value(name: str) -> str:
    if os.name != "nt" or winreg is None:
        return ""
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for root_key, sub_key in locations:
        try:
            with winreg.OpenKey(root_key, sub_key) as key:
                value, _kind = winreg.QueryValueEx(key, name)
                if str(value).strip():
                    return str(value).strip()
        except OSError:
            continue
    return ""


def env_any_present(*names: str) -> bool:
    for name in names:
        if os.environ.get(name, "").strip() or windows_environment_value(name):
            return True
    return False
GMGN_CHAIN_ALIASES = {
    "sol": "solana",
    "solana": "solana",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "bsc": "bsc",
    "bnb": "bsc",
    "base": "base",
}
CHAIN_ID_ALIASES = {
    "1": "ethereum",
    "501": "solana",
    "8453": "base",
    "42161": "arbitrum",
}

HEAT_SOURCE_WEIGHTS = {
    "boost_top": 18,
    "boost_latest": 12,
    "ads_latest": 14,
    "community_takeover": 10,
    "profile_latest": 4,
    "gmgn_trending": 20,
    "gmgn_live_trending": 34,
    "gmgn_skills_trending": 36,
    "gmgn_skills_smartmoney": 38,
    "gmgn_skills_signal": 42,
    "gmgn_skills_trenches": 40,
    "gmgn_skills_kol": 28,
    "gmgn_skills_hot_searches": 18,
    "bsc_onchain": 42,
    "arc_onchain": 42,
    "fourmeme_launchpad": 48,
    "flap_launchpad": 46,
    "noxa_launchpad": 40,
    "proficy_trending": 32,
    "985_monitor": 34,
    "985_fomo_wallets": 30,
    "985_smartmoney": 38,
    "wind_monitor": 34,
    "pumpfun_live": 36,
    "pumpfun_onchain": 44,
    "gmgn_trenches": 26,
    "okx_trenches": 24,
    "okx_signal": 24,
    "binance_wallet_hot": 30,
    "binance_wallet_signal": 28,
    "debot_trenches": 28,
    "debot_signal": 30,
    "mobula_trending": 18,
    "birdeye_trending": 18,
    "birdeye_meme": 22,
}

MEME_KEYWORDS = {
    "meme",
    "doge",
    "shib",
    "pepe",
    "frog",
    "cat",
    "dog",
    "cto",
    "pump",
    "bonk",
    "fart",
    "mascot",
    "viral",
    "community",
    "sol",
    "baby",
}

NARRATIVE_KEYWORDS = {
    "AI": ("ai", "agent", "bot", "grok", "chatgpt", "openai", "xai"),
    "动物": ("dog", "doge", "shib", "cat", "frog", "pepe", "bird", "monkey", "fish"),
    "马斯克系": ("elon", "tesla", "spacex", "grok", "xai"),
    "政治": ("trump", "maga", "biden", "president"),
    "Pump": ("pump", "pump.fun", "pumpfun"),
    "社区/CTO": ("cto", "community", "takeover"),
}

DEX_CHAIN_TO_EVM_ID = {
    "robinhood": "4663",
    "ethereum": "1",
    "eth": "1",
    "bsc": "56",
    "binance": "56",
    "base": "8453",
    "optimism": "10",
    "arbitrum": "42161",
    "polygon": "137",
    "avalanche": "43114",
}


def source_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "value"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
    return []


def http_json_compat(url: str, **kwargs: Any) -> Any:
    try:
        return alpha.http_json(url, **kwargs)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return alpha.http_json(url)


def infer_narrative_tags(row: dict[str, Any]) -> list[str]:
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
    tags: list[str] = []
    for tag, keywords in NARRATIVE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            tags.append(tag)
    if re.search(r"[\u4e00-\u9fff]", text):
        tags.append("中文梗")
    deduped: list[str] = []
    for tag in tags:
        if tag not in deduped:
            deduped.append(tag)
    return deduped[:6]


def token_key(chain: str, address: str) -> str:
    return f"{chain}:{address}".lower()


def nested_rows(payload: Any) -> list[dict[str, Any]]:
    rows = source_rows(payload)
    if rows:
        return rows
    if isinstance(payload, dict):
        grouped_rows: list[dict[str, Any]] = []
        for key in ("new_creation", "near_completion", "completed"):
            value = payload.get(key)
            if isinstance(value, list):
                grouped_rows.extend(row for row in value if isinstance(row, dict))
        if grouped_rows:
            return grouped_rows
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("tokens", "rank", "list", "items", "pairs"):
                value = data.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
    return []


def meme_source_inbox_dir() -> Path:
    configured = os.environ.get("ALPHA_MEME_SOURCE_INBOX_DIR", "").strip()
    if configured:
        return Path(configured)
    return ROOT / "outputs" / "meme-source-inbox"


def json_file_has_source_rows(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 2:
            return False
        return bool(nested_rows(json.loads(path.read_text(encoding="utf-8-sig"))))
    except Exception:  # noqa: BLE001
        return False


def local_inbox_file_source_specs(*, include_expired: bool = False) -> list[tuple[str, str]]:
    if os.environ.get("ALPHA_MEME_SOURCE_INBOX_DISABLE", "").strip():
        return []
    inbox = meme_source_inbox_dir()
    specs: list[tuple[str, str]] = []
    for source_label, names in DEFAULT_MEME_SOURCE_INBOX_FILES.items():
        for name in names:
            path = inbox / name
            age_seconds = max(0, time.time() - path.stat().st_mtime) if path.exists() else float("inf")
            if json_file_has_source_rows(path) and (include_expired or age_seconds <= 300):
                specs.append((str(path), source_label))
    return specs


def iso_from_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def source_freshness(paths: list[str]) -> dict[str, Any]:
    existing = [Path(path) for path in paths if Path(path).exists()]
    if not existing:
        return {"freshness_level": "pending", "freshness_label": "无本地数据"}
    latest = max(existing, key=lambda path: path.stat().st_mtime)
    age_seconds = max(0, int(time.time() - latest.stat().st_mtime))
    if age_seconds <= 90:
        level = "green"
        label = "实时"
    elif age_seconds <= 300:
        level = "yellow"
        label = "稍旧"
    else:
        level = "red"
        label = "过期"
    return {
        "freshness_level": level,
        "freshness_label": label,
        "last_updated_at": iso_from_mtime(latest),
        "age_seconds": age_seconds,
        "freshest_file": str(latest),
    }


def poller_status_freshness(status: dict[str, Any]) -> dict[str, Any]:
    observed_at = str(status.get("observed_at") or "").strip()
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - observed).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return {"freshness_level": "yellow", "freshness_label": "状态时间缺失"}
    if age_seconds <= 90:
        level, label = "green", "实时"
    elif age_seconds <= 300:
        level, label = "yellow", "稍旧"
    else:
        level, label = "red", "过期"
    return {
        "freshness_level": level,
        "freshness_label": label,
        "last_updated_at": observed.isoformat(),
        "age_seconds": age_seconds,
    }


def live_source_freshness() -> dict[str, Any]:
    return {"freshness_level": "green", "freshness_label": "本轮检查", "last_checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def configured_source_freshness(enabled: bool, files: list[str] | None = None) -> dict[str, Any]:
    source_files = files or []
    if source_files:
        return source_freshness(source_files)
    if enabled:
        return live_source_freshness()
    return {"freshness_level": "pending", "freshness_label": "未接入"}


def normalize_gmgn_chain(chain: str) -> str:
    raw = str(chain or "").strip().lower()
    return GMGN_CHAIN_ALIASES.get(raw, raw)


def chain_from_gmgn(row: dict[str, Any], fallback: str = "") -> str:
    chain = str(row.get("chainId") or row.get("chain") or row.get("network") or fallback).strip()
    return normalize_gmgn_chain(chain)


def normalize_chain(chain: Any) -> str:
    raw = str(chain or "").strip().lower()
    if not raw:
        return ""
    normalized = normalize_monitor_chain(raw)
    return CHAIN_ID_ALIASES.get(normalized, normalize_gmgn_chain(normalized))


def meme_chain_allowlist() -> set[str]:
    raw = os.environ.get("ALPHA_MEME_CHAINS", DEFAULT_MEME_CHAINS).strip().lower()
    if raw in {"*", "all"}:
        return set()
    return {chain for chain in (normalize_chain(item) for item in raw.split(",")) if chain}


def meme_chain_allowed(chain: Any) -> bool:
    allowed = meme_chain_allowlist()
    return not allowed or normalize_chain(chain) in allowed


def gmgn_cli_chain_name(chain: str) -> str:
    normalized = normalize_chain(chain)
    if normalized == "solana":
        return "sol"
    if normalized == "ethereum":
        return "eth"
    return normalized


def address_from_gmgn(row: dict[str, Any]) -> str:
    return str(
        row.get("tokenAddress")
        or row.get("token_address")
        or row.get("address")
        or row.get("mint")
        or row.get("base_address")
        or ""
    ).strip()


def gmgn_chain_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        rank_index = parts.index("rank")
    except ValueError:
        return ""
    if len(parts) <= rank_index + 1:
        return ""
    return normalize_gmgn_chain(parts[rank_index + 1])


def env_url_list(name: str, limit: int | None = None) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    urls: list[str] = []
    for url in raw.split(","):
        cleaned = url.strip()
        if not cleaned:
            continue
        urls.append(cleaned.format(limit=limit) if limit is not None else cleaned)
    return urls


def env_command_list(name: str, limit: int | None = None) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    commands: list[str] = []
    for command in raw.replace("||", "\n").splitlines():
        cleaned = command.strip()
        if cleaned:
            commands.append(cleaned.format(limit=limit) if limit is not None else cleaned)
    return commands


def env_path_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def gmgn_source_specs(limit: int) -> list[tuple[str, str]]:
    single_url = os.environ.get("GMGN_TRENDING_URL", "").strip()
    if single_url:
        trending_urls = [single_url.format(limit=limit)]
    else:
        multi_urls = env_url_list("GMGN_TRENDING_URLS", limit)
        trending_urls = multi_urls or [url.format(limit=limit) for url in GMGN_DEFAULT_RANK_URLS]
    trending_urls = [
        url for url in trending_urls
        if not gmgn_chain_from_url(url) or meme_chain_allowed(gmgn_chain_from_url(url))
    ]
    specs = [(url, "gmgn_trending") for url in trending_urls]
    specs.extend((url, "gmgn_trenches") for url in env_url_list("GMGN_TRENCHES_URLS", limit))
    return specs


def gmgn_source_urls(limit: int) -> list[str]:
    return [url for url, _source in gmgn_source_specs(limit)]


def env_csv(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default).strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def gmgn_cli_enabled() -> bool:
    return not os.environ.get("GMGN_CLI_DISABLE", "").strip()


def gmgn_cli_specs(limit: int) -> list[tuple[str, str, int]]:
    if not gmgn_cli_enabled():
        return []
    allowed = meme_chain_allowlist()
    default_chains = "sol,bsc,base,eth" if not allowed else ",".join(gmgn_cli_chain_name(chain) for chain in sorted(allowed))
    chains = [chain for chain in env_csv("GMGN_CLI_CHAINS", default_chains)
              if meme_chain_allowed(chain) and chain in {"sol", "bsc", "base", "eth", "monad", "tron"}]
    intervals = env_csv("GMGN_CLI_INTERVALS", "1m,5m,1h")
    per_query_limit = max(1, min(limit, alpha.to_int(os.environ.get("GMGN_CLI_LIMIT"), default=20)))
    return [(chain, interval, per_query_limit) for chain in chains for interval in intervals]


def gmgn_trenches_cli_specs(limit: int) -> list[tuple[str, list[str], int]]:
    if not gmgn_cli_enabled() or os.environ.get("GMGN_TRENCHES_CLI_DISABLE", "").strip():
        return []
    allowed = meme_chain_allowlist()
    default_chains = "bsc" if not allowed else ",".join(gmgn_cli_chain_name(chain) for chain in sorted(allowed))
    chains = [chain for chain in env_csv("GMGN_TRENCHES_CLI_CHAINS", default_chains)
              if meme_chain_allowed(chain) and chain in {"sol", "bsc", "base", "eth", "monad", "tron"}]
    types = env_csv("GMGN_TRENCHES_CLI_TYPES", "new_creation,near_completion,completed")
    allowed_types = {"new_creation", "near_completion", "completed"}
    types = [item for item in types if item in allowed_types] or ["new_creation", "near_completion", "completed"]
    per_query_limit = max(1, min(limit, alpha.to_int(os.environ.get("GMGN_TRENCHES_CLI_LIMIT"), default=30)))
    return [(chain, types, per_query_limit) for chain in chains]


def external_source_specs(limit: int) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    specs.extend((url, "okx_trenches") for url in env_url_list("OKX_TRENCHES_URLS", limit))
    specs.extend((url, "debot_trenches") for url in env_url_list("DEBOT_TRENCHES_URLS", limit))
    specs.extend((url, "debot_signal") for url in env_url_list("DEBOT_SIGNAL_URLS", limit))
    specs.extend((url, "proficy_trending") for url in env_url_list("PROFICY_TRENDING_URLS", limit))
    specs.extend((url, "985_monitor") for url in env_url_list("MONITOR985_URLS", limit))
    specs.extend((url, "985_fomo_wallets") for url in env_url_list("MONITOR985_FOMO_URLS", limit))
    specs.extend((url, "985_smartmoney") for url in env_url_list("MONITOR985_SMARTMONEY_URLS", limit))
    specs.extend((url, "wind_monitor") for url in env_url_list("WIND_MONITOR_URLS", limit))
    specs.extend((url, "wind_monitor") for url in env_url_list("TINGFENG_MONITOR_URLS", limit))
    specs.extend((url, "binance_wallet_hot") for url in env_url_list("BINANCE_WALLET_HOT_URLS", limit))
    specs.extend((url, "binance_wallet_signal") for url in env_url_list("BINANCE_WALLET_SIGNAL_URLS", limit))
    mobula_urls = env_url_list("MOBULA_TRENDING_URLS", limit)
    if not mobula_urls and not os.environ.get("MOBULA_DISABLE_DEFAULT_TRENDING", "").strip():
        mobula_urls = [MOBULA_DEFAULT_TRENDING_URL]
    specs.extend((url, "mobula_trending") for url in mobula_urls)
    birdeye_urls = env_url_list("BIRDEYE_TRENDING_URLS", limit)
    if (
        not birdeye_urls
        and os.environ.get("BIRDEYE_API_KEY", "").strip()
        and not os.environ.get("BIRDEYE_DISABLE_DEFAULT_TRENDING", "").strip()
    ):
        birdeye_urls = [url.format(limit=limit) for url in BIRDEYE_DEFAULT_TRENDING_URLS]
    for url in birdeye_urls:
        label = "birdeye_meme" if "/token/meme/" in url else "birdeye_trending"
        specs.append((url, label))
    return specs


def external_command_source_specs(limit: int) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    specs.extend((command, "gmgn_trenches") for command in env_command_list("GMGN_TRENCHES_COMMANDS", limit))
    specs.extend((command, "okx_trenches") for command in env_command_list("OKX_TRENCHES_COMMANDS", limit))
    specs.extend((command, "debot_trenches") for command in env_command_list("DEBOT_TRENCHES_COMMANDS", limit))
    specs.extend((command, "debot_signal") for command in env_command_list("DEBOT_SIGNAL_COMMANDS", limit))
    specs.extend((command, "proficy_trending") for command in env_command_list("PROFICY_TRENDING_COMMANDS", limit))
    specs.extend((command, "985_monitor") for command in env_command_list("MONITOR985_COMMANDS", limit))
    specs.extend((command, "985_fomo_wallets") for command in env_command_list("MONITOR985_FOMO_COMMANDS", limit))
    specs.extend((command, "985_smartmoney") for command in env_command_list("MONITOR985_SMARTMONEY_COMMANDS", limit))
    specs.extend((command, "wind_monitor") for command in env_command_list("WIND_MONITOR_COMMANDS", limit))
    specs.extend((command, "wind_monitor") for command in env_command_list("TINGFENG_MONITOR_COMMANDS", limit))
    specs.extend((command, "binance_wallet_hot") for command in env_command_list("BINANCE_WALLET_HOT_COMMANDS", limit))
    specs.extend((command, "binance_wallet_signal") for command in env_command_list("BINANCE_WALLET_SIGNAL_COMMANDS", limit))
    specs.extend((command, "birdeye_trending") for command in env_command_list("BIRDEYE_TRENDING_COMMANDS", limit))
    specs.extend((command, "bsc_onchain") for command in env_command_list("BSC_ONCHAIN_COMMANDS", limit))
    specs.extend((command, "fourmeme_launchpad") for command in env_command_list("FOURMEME_LIVE_COMMANDS", limit))
    specs.extend((command, "flap_launchpad") for command in env_command_list("FLAP_LIVE_COMMANDS", limit))
    specs.extend((command, "noxa_launchpad") for command in env_command_list("NOXA_LAUNCHPAD_COMMANDS", limit))
    specs.extend((command, "pumpfun_live") for command in env_command_list("PUMPFUN_LIVE_COMMANDS", limit))
    specs.extend((command, "pumpfun_onchain") for command in env_command_list("PUMPFUN_ONCHAIN_COMMANDS", limit))
    return specs


def external_file_source_specs() -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    specs.extend((path, "gmgn_trenches") for path in env_path_list("GMGN_TRENCHES_FILES"))
    specs.extend((path, "okx_trenches") for path in env_path_list("OKX_TRENCHES_FILES"))
    specs.extend((path, "debot_trenches") for path in env_path_list("DEBOT_TRENCHES_FILES"))
    specs.extend((path, "debot_signal") for path in env_path_list("DEBOT_SIGNAL_FILES"))
    specs.extend((path, "proficy_trending") for path in env_path_list("PROFICY_TRENDING_FILES"))
    specs.extend((path, "985_monitor") for path in env_path_list("MONITOR985_FILES"))
    specs.extend((path, "985_fomo_wallets") for path in env_path_list("MONITOR985_FOMO_FILES"))
    specs.extend((path, "985_smartmoney") for path in env_path_list("MONITOR985_SMARTMONEY_FILES"))
    specs.extend((path, "wind_monitor") for path in env_path_list("WIND_MONITOR_FILES"))
    specs.extend((path, "wind_monitor") for path in env_path_list("TINGFENG_MONITOR_FILES"))
    specs.extend((path, "binance_wallet_hot") for path in env_path_list("BINANCE_WALLET_HOT_FILES"))
    specs.extend((path, "binance_wallet_signal") for path in env_path_list("BINANCE_WALLET_SIGNAL_FILES"))
    specs.extend((path, "birdeye_trending") for path in env_path_list("BIRDEYE_TRENDING_FILES"))
    specs.extend((path, "bsc_onchain") for path in env_path_list("BSC_ONCHAIN_FILES"))
    specs.extend((path, "fourmeme_launchpad") for path in env_path_list("FOURMEME_LIVE_FILES"))
    specs.extend((path, "flap_launchpad") for path in env_path_list("FLAP_LIVE_FILES"))
    specs.extend((path, "noxa_launchpad") for path in env_path_list("NOXA_LAUNCHPAD_FILES"))
    specs.extend((path, "pumpfun_live") for path in env_path_list("PUMPFUN_LIVE_FILES"))
    specs.extend((path, "pumpfun_onchain") for path in env_path_list("PUMPFUN_ONCHAIN_FILES"))
    env_paths = {str(Path(path)) for path, _source in specs}
    specs.extend((path, source) for path, source in local_inbox_file_source_specs() if str(Path(path)) not in env_paths)
    return specs


def external_source_headers(source_label: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if source_label == "birdeye_trending":
        api_key = os.environ.get("BIRDEYE_API_KEY", "").strip()
        if api_key:
            headers["X-API-KEY"] = api_key
    if source_label == "mobula_trending":
        api_key = os.environ.get("MOBULA_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = api_key
    return headers


def command_json(command: str) -> Any:
    timeout = max(5, alpha.to_int(os.environ.get("EXTERNAL_MEME_COMMAND_TIMEOUT_SECONDS"), default=25))
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **hidden_subprocess_kwargs(),
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(message[-1] if message else f"command exited {proc.returncode}")
    return json.loads(proc.stdout)


def external_source_row_limit(source_label: str, requested_limit: int) -> int:
    if source_label.startswith("mobula_"):
        env_value = alpha.to_int(os.environ.get("MOBULA_SOURCE_LIMIT"), default=15)
        return max(1, min(requested_limit, env_value))
    if source_label.startswith("birdeye_"):
        env_value = alpha.to_int(os.environ.get("BIRDEYE_SOURCE_LIMIT"), default=20)
        return max(1, min(requested_limit, env_value))
    if source_label.startswith("okx_"):
        env_value = alpha.to_int(os.environ.get("OKX_SOURCE_LIMIT"), default=20)
        return max(1, min(requested_limit, env_value))
    if source_label.startswith("debot_"):
        env_value = alpha.to_int(os.environ.get("DEBOT_SOURCE_LIMIT"), default=20)
        return max(1, min(requested_limit, env_value))
    if source_label.startswith("pumpfun_"):
        env_value = alpha.to_int(os.environ.get("PUMPFUN_SOURCE_LIMIT"), default=40)
        return max(1, min(requested_limit, env_value))
    if source_label.startswith("proficy_"):
        env_value = alpha.to_int(os.environ.get("PROFICY_SOURCE_LIMIT"), default=25)
        return max(1, min(requested_limit, env_value))
    if source_label == "985_monitor":
        env_value = alpha.to_int(os.environ.get("MONITOR985_SOURCE_LIMIT"), default=45)
        return max(1, min(requested_limit, env_value))
    if source_label == "985_fomo_wallets":
        env_value = alpha.to_int(os.environ.get("MONITOR985_FOMO_SOURCE_LIMIT"), default=40)
        return max(1, min(requested_limit, env_value))
    if source_label == "985_smartmoney":
        env_value = alpha.to_int(os.environ.get("MONITOR985_SMARTMONEY_SOURCE_LIMIT"), default=40)
        return max(1, min(requested_limit, env_value))
    if source_label == "wind_monitor":
        env_value = alpha.to_int(os.environ.get("WIND_MONITOR_SOURCE_LIMIT"), default=45)
        return max(1, min(requested_limit, env_value))
    return requested_limit


def profile_valuation_fields(row: dict[str, Any], source: str) -> dict[str, Any]:
    def positive(value: Any) -> float | None:
        number = alpha.to_float(value)
        return number if not isinstance(value, bool) and number > 0 else None

    field = next((key for key in ("market_cap", "marketCap", "mc", "mcap")
                  if row.get(key) is not None), None)
    mcap = positive(row.get(field)) if field else None
    if "valuation_type" in row and row["valuation_type"] != "market_cap":
        mcap = None
    fdv = positive(row.get("fdv"))
    return {
        "mcap": mcap, "market_cap": mcap, "fdv": fdv,
        "market_cap_source": (row.get("market_cap_source") or f"{source}.{field}") if mcap else None,
        "fdv_source": (row.get("fdv_source") or f"{source}.fdv") if fdv else None,
        "valuation_type": "market_cap" if mcap else "fdv" if fdv else "unavailable",
    }


def evidence_observed_at(value: str | None = None) -> str:
    return value or datetime.now(timezone.utc).isoformat()


def source_evidence(
    raw: dict[str, Any],
    *,
    source: str,
    chain: str,
    contract_address: str,
    observed_at: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    stamp = evidence_observed_at(observed_at)
    event, rejection = monitor_v3.normalize_provider_event(
        raw,
        source=source,
        chain=chain,
        contract_address=contract_address,
        observed_at=stamp,
    )
    audit_facts: list[dict[str, Any]] = []
    try:
        descriptor = monitor_v3.source_descriptor(source, raw)
        audit_facts = monitor_v3.normalize_audit_facts(
            raw,
            provider_family=descriptor.provider_family,
            observed_at=stamp,
            identity_key=token_key(chain, contract_address),
        )
    except (TypeError, ValueError):
        pass
    return {
        "source_events": [event] if event else [],
        "audit_facts": audit_facts,
        "event_rejections": [rejection] if rejection else [],
    }


def _extend_unique(
    target: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    identity_field: str,
) -> None:
    seen = {str(item.get(identity_field) or "") for item in target}
    for item in incoming:
        identity = str(item.get(identity_field) or "")
        if identity and identity not in seen:
            target.append(item)
            seen.add(identity)


def merge_source_record(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    current["sources"].extend(incoming.get("sources") or [])
    current["profile"] = merge_source_profile(
        current.get("profile", {}), incoming.get("profile", {})
    )
    _extend_unique(
        current.setdefault("source_events", []),
        incoming.get("source_events") or [],
        "event_id",
    )
    _extend_unique(
        current.setdefault("audit_facts", []),
        incoming.get("audit_facts") or [],
        "evidence_id",
    )
    _extend_unique(
        current.setdefault("event_rejections", []),
        incoming.get("event_rejections") or [],
        "raw_fingerprint",
    )
    return current


def source_record(
    raw: dict[str, Any],
    *,
    source: str,
    chain: str,
    contract_address: str,
    profile: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "chainId": chain,
        "tokenAddress": contract_address,
        "sources": [source],
        "profile": profile if profile is not None else raw,
        **source_evidence(
            raw,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
        ),
    }


def normalize_external_meme_row(
    row: dict[str, Any],
    source_label: str,
    *,
    observed_at: str | None = None,
) -> dict[str, Any] | None:
    if observed_at and not any(
        row.get(field) not in (None, "")
        for field in (
            "event_at", "timestamp", "created_at", "createdAt", "pair_created_at",
            "pairCreatedAt", "block_timestamp", "blockTime", "time", "observed_at",
        )
    ):
        row = {
            **row,
            "observed_at": observed_at,
            "event_time_basis": "snapshot_observed_at",
        }
    if source_label.startswith("okx_") and not row.get("okx_kind") and (
        "tokenContractAddress" in row
        or "tokenAddress" in row
        or isinstance(row.get("token"), dict)
    ):
        okx_kind = "SIGNAL" if source_label == "okx_signal" or isinstance(row.get("token"), dict) else "TRENCHES"
        row = {**row, **alpha_okx_market.normalize(row, normalize_chain(row.get("chainIndex")), okx_kind)}
    contracts = row.get("contracts")
    first_contract = contracts[0] if isinstance(contracts, list) and contracts and isinstance(contracts[0], dict) else {}
    chain = normalize_chain(
        first_present(row, ("chainId", "chain_id", "chain", "network", "blockchain", "chainIndex"))
        or first_present(first_contract, ("chainId", "chain_id", "chain", "network", "blockchain", "chainIndex"))
    )
    address = str(
        first_present(
            row,
            (
                "tokenAddress",
                "token_address",
                "contractAddress",
                "contract_address",
                "address",
                "mint",
                "base_address",
            ),
        )
        or first_present(first_contract, ("address", "tokenAddress", "token_address", "contractAddress", "contract_address", "mint"))
        or ""
    ).strip()
    if not chain or not address:
        return None
    if not meme_chain_allowed(chain):
        return None
    valuation = profile_valuation_fields(row, source_label)
    volume = alpha.to_float(
        first_present(row, ("volume", "volume24h", "volume_24h", "v24h", "volume_24h_usd", "volume24hUsd", "volume24hUSD"))
    )
    profile = {
        **row,
        "chain": chain,
        "address": address,
        **valuation,
        "liquidity": alpha.to_float(first_present(row, ("liquidity", "liquidity_usd", "liquidityUsd", "liquidityUSD"))),
        "volume": volume,
        "price_change_percent5m": alpha.to_float(
            first_present(row, ("price_change_percent5m", "price_change_5m", "priceChange5m", "price_change_5m_percent"))
        ),
        "price_change_percent1h": alpha.to_float(
            first_present(row, ("price_change_percent1h", "price_change_1h", "priceChange1h", "price_change_1h_percent"))
        ),
        "price_change_percent24h": alpha.to_float(
            first_present(row, ("price_change_percent24h", "price_change_24h", "priceChange24h", "price_change_24h_percent"))
        ),
        "gmgn_score": alpha.to_float(first_present(row, ("score", "rank_score", "hot_score", "trending_score"))),
        "source_family": source_label,
    }
    return source_record(
        row,
        source=source_label,
        chain=chain,
        contract_address=address,
        profile=profile,
        observed_at=observed_at,
    )


def read_okx_stream_status() -> dict[str, Any]:
    path = ROOT / "outputs" / "okx-stream-export-status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_wind_monitor_status() -> dict[str, Any]:
    path = ROOT / "outputs" / "wind-monitor-export-status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_arc_public_chain_poll_status() -> dict[str, Any]:
    path = ROOT / "outputs" / "arc-public-chain-poll-status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_gmgn_skills_status() -> dict[str, Any]:
    path = ROOT / "outputs" / "gmgn-skills-status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def optional_meme_source_status() -> dict[str, dict[str, Any]]:
    inbox_specs = local_inbox_file_source_specs(include_expired=True)

    def inbox_files(source_label: str) -> list[str]:
        return [path for path, label in inbox_specs if label == source_label]

    gmgn_trenches_urls = env_url_list("GMGN_TRENCHES_URLS")
    gmgn_trenches_commands = env_command_list("GMGN_TRENCHES_COMMANDS")
    gmgn_trenches_files = env_path_list("GMGN_TRENCHES_FILES")
    okx_urls = env_url_list("OKX_TRENCHES_URLS")
    okx_commands = env_command_list("OKX_TRENCHES_COMMANDS")
    okx_files = env_path_list("OKX_TRENCHES_FILES") + inbox_files("okx_trenches")
    okx_signal_files = env_path_list("OKX_SIGNAL_FILES") + inbox_files("okx_signal")
    debot_trenches_urls = env_url_list("DEBOT_TRENCHES_URLS")
    debot_signal_urls = env_url_list("DEBOT_SIGNAL_URLS")
    debot_trenches_commands = env_command_list("DEBOT_TRENCHES_COMMANDS")
    debot_signal_commands = env_command_list("DEBOT_SIGNAL_COMMANDS")
    debot_trenches_files = env_path_list("DEBOT_TRENCHES_FILES") + inbox_files("debot_trenches")
    debot_signal_files = env_path_list("DEBOT_SIGNAL_FILES") + inbox_files("debot_signal")
    bsc_onchain_commands = env_command_list("BSC_ONCHAIN_COMMANDS")
    bsc_onchain_files = env_path_list("BSC_ONCHAIN_FILES") + inbox_files("bsc_onchain")
    bsc_onchain_wss = env_any_present("BSC_WSS_URL", "BNBCHAIN_WSS_URL", "BSC_PANCAKE_WSS_URL", "QUICKNODE_BSC_WSS_URL")
    arc_onchain_files = inbox_files("arc_onchain")
    fourmeme_commands = env_command_list("FOURMEME_LIVE_COMMANDS")
    fourmeme_files = env_path_list("FOURMEME_LIVE_FILES") + inbox_files("fourmeme_launchpad")
    fourmeme_key = env_any_present("BITQUERY_API_KEY", "BITQUERY_TOKEN")
    flap_commands = env_command_list("FLAP_LIVE_COMMANDS")
    flap_files = env_path_list("FLAP_LIVE_FILES") + inbox_files("flap_launchpad")
    flap_key = env_any_present("BITQUERY_API_KEY", "BITQUERY_TOKEN")
    noxa_commands = env_command_list("NOXA_LAUNCHPAD_COMMANDS")
    noxa_files = env_path_list("NOXA_LAUNCHPAD_FILES") + inbox_files("noxa_launchpad")
    proficy_urls = env_url_list("PROFICY_TRENDING_URLS")
    proficy_commands = env_command_list("PROFICY_TRENDING_COMMANDS")
    proficy_files = env_path_list("PROFICY_TRENDING_FILES") + inbox_files("proficy_trending")
    monitor985_urls = env_url_list("MONITOR985_URLS")
    monitor985_commands = env_command_list("MONITOR985_COMMANDS")
    monitor985_files = env_path_list("MONITOR985_FILES") + inbox_files("985_monitor")
    monitor985_fomo_urls = env_url_list("MONITOR985_FOMO_URLS")
    monitor985_fomo_commands = env_command_list("MONITOR985_FOMO_COMMANDS")
    monitor985_fomo_files = env_path_list("MONITOR985_FOMO_FILES") + inbox_files("985_fomo_wallets")
    monitor985_smartmoney_urls = env_url_list("MONITOR985_SMARTMONEY_URLS")
    monitor985_smartmoney_commands = env_command_list("MONITOR985_SMARTMONEY_COMMANDS")
    monitor985_smartmoney_files = env_path_list("MONITOR985_SMARTMONEY_FILES") + inbox_files("985_smartmoney")
    wind_urls = env_url_list("WIND_MONITOR_URLS") + env_url_list("TINGFENG_MONITOR_URLS")
    wind_commands = env_command_list("WIND_MONITOR_COMMANDS") + env_command_list("TINGFENG_MONITOR_COMMANDS")
    wind_files = (
        env_path_list("WIND_MONITOR_FILES")
        + env_path_list("TINGFENG_MONITOR_FILES")
        + inbox_files("wind_monitor")
    )
    binance_wallet_hot_urls = env_url_list("BINANCE_WALLET_HOT_URLS")
    binance_wallet_hot_commands = env_command_list("BINANCE_WALLET_HOT_COMMANDS")
    binance_wallet_hot_files = env_path_list("BINANCE_WALLET_HOT_FILES") + inbox_files("binance_wallet_hot")
    binance_wallet_signal_urls = env_url_list("BINANCE_WALLET_SIGNAL_URLS")
    binance_wallet_signal_commands = env_command_list("BINANCE_WALLET_SIGNAL_COMMANDS")
    binance_wallet_signal_files = env_path_list("BINANCE_WALLET_SIGNAL_FILES") + inbox_files("binance_wallet_signal")
    pumpfun_commands = env_command_list("PUMPFUN_LIVE_COMMANDS")
    pumpfun_files = env_path_list("PUMPFUN_LIVE_FILES") + inbox_files("pumpfun_live")
    pumpfun_key = bool(os.environ.get("APIFY_TOKEN", "").strip() or os.environ.get("APIFY_API_TOKEN", "").strip())
    pumpfun_onchain_commands = env_command_list("PUMPFUN_ONCHAIN_COMMANDS")
    pumpfun_onchain_files = env_path_list("PUMPFUN_ONCHAIN_FILES") + inbox_files("pumpfun_onchain")
    pumpfun_onchain_wss = bool(
        os.environ.get("SOLANA_PUMPFUN_WSS_URL", "").strip()
        or os.environ.get("SOLANA_WSS_URL", "").strip()
        or os.environ.get("HELIUS_WSS_URL", "").strip()
        or os.environ.get("QUICKNODE_WSS_URL", "").strip()
        or os.environ.get("HELIUS_API_KEY", "").strip()
    )
    mobula_urls = env_url_list("MOBULA_TRENDING_URLS")
    if not mobula_urls and not os.environ.get("MOBULA_DISABLE_DEFAULT_TRENDING", "").strip():
        mobula_urls = [MOBULA_DEFAULT_TRENDING_URL]
    birdeye_urls = env_url_list("BIRDEYE_TRENDING_URLS")
    birdeye_commands = env_command_list("BIRDEYE_TRENDING_COMMANDS")
    birdeye_files = env_path_list("BIRDEYE_TRENDING_FILES") + inbox_files("birdeye_trending")
    birdeye_default_urls = []
    if (
        not birdeye_urls
        and os.environ.get("BIRDEYE_API_KEY", "").strip()
        and not os.environ.get("BIRDEYE_DISABLE_DEFAULT_TRENDING", "").strip()
    ):
        birdeye_default_urls = list(BIRDEYE_DEFAULT_TRENDING_URLS)
    okx_key = bool(alpha_okx_market.credentials()[0])
    mobula_key = bool(os.environ.get("MOBULA_API_KEY", "").strip())
    birdeye_key = bool(os.environ.get("BIRDEYE_API_KEY", "").strip())
    gmgn_cli = gmgn_cli_specs(20)
    gmgn_trenches_cli = gmgn_trenches_cli_specs(20)
    gmgn_trenches_enabled = bool(gmgn_trenches_cli or gmgn_trenches_urls or gmgn_trenches_commands or gmgn_trenches_files)
    okx_api_status = alpha_okx_market.status()
    okx_stream_status = read_okx_stream_status()
    wind_export_status = read_wind_monitor_status()
    arc_poller_status = read_arc_public_chain_poll_status()
    gmgn_skills_export_status = read_gmgn_skills_status()
    okx_enabled = bool(okx_urls or okx_commands or okx_files or okx_api_status["configured"])
    okx_signal_enabled = bool(okx_signal_files or okx_api_status["configured"])
    debot_enabled = bool(debot_trenches_urls or debot_signal_urls or debot_trenches_commands or debot_signal_commands or debot_trenches_files or debot_signal_files)
    bsc_onchain_enabled = bool(bsc_onchain_files or bsc_onchain_commands or bsc_onchain_wss)
    arc_onchain_enabled = bool(arc_onchain_files or arc_poller_status)
    fourmeme_enabled = bool(fourmeme_files or fourmeme_commands or fourmeme_key)
    flap_enabled = bool(flap_files or flap_commands or flap_key)
    noxa_enabled = bool(noxa_files or noxa_commands)
    proficy_enabled = bool(proficy_urls or proficy_commands or proficy_files)
    monitor985_enabled = bool(monitor985_urls or monitor985_commands or monitor985_files)
    monitor985_fomo_enabled = bool(monitor985_fomo_urls or monitor985_fomo_commands or monitor985_fomo_files)
    monitor985_smartmoney_enabled = bool(monitor985_smartmoney_urls or monitor985_smartmoney_commands or monitor985_smartmoney_files)
    wind_enabled = bool(wind_urls or wind_commands or wind_files or wind_export_status)
    binance_wallet_hot_enabled = bool(binance_wallet_hot_urls or binance_wallet_hot_commands or binance_wallet_hot_files)
    binance_wallet_signal_enabled = bool(binance_wallet_signal_urls or binance_wallet_signal_commands or binance_wallet_signal_files)
    pumpfun_enabled = bool(pumpfun_files or pumpfun_commands or pumpfun_key)
    pumpfun_onchain_enabled = bool(pumpfun_onchain_files or pumpfun_onchain_commands or pumpfun_onchain_wss)
    mobula_enabled = bool(mobula_urls or mobula_key)
    birdeye_enabled = bool(birdeye_urls or birdeye_default_urls or birdeye_commands or birdeye_files)
    status = {
        "dexscreener_discovery": {
            "enabled": True,
            "url_count": 5,
            "key_present": False,
            "mode": "default",
            **live_source_freshness(),
        },
        "gmgn_trending": {
            "enabled": True,
            "url_count": len(gmgn_source_urls(20)),
            "key_present": False,
            "mode": "default",
            **live_source_freshness(),
        },
        "gmgn_live_trending": {
            "enabled": bool(gmgn_cli),
            "url_count": len(gmgn_cli),
            "key_present": bool(os.environ.get("GMGN_API_KEY", "").strip()) or bool((gmgn_skills_export_status.get("env") or {}).get("configured")),
            "mode": "gmgn-cli",
            "needs": "" if gmgn_cli else "gmgn-cli",
            **live_source_freshness(),
        },
        "gmgn_skills_market": {
            "enabled": True,
            "url_count": 0,
            "key_present": bool(os.environ.get("GMGN_API_KEY", "").strip()),
            "mode": "local gmgn-cli skills market",
            "needs": "gmgn-cli + GMGN_API_KEY",
            "status": gmgn_skills_export_status,
            **source_freshness([
                str(ROOT / "outputs" / "meme-source-inbox" / "gmgn-skills-smartmoney.json"),
                str(ROOT / "outputs" / "meme-source-inbox" / "gmgn-skills-trending.json"),
            ]),
        },
        "gmgn_trenches": {
            "enabled": gmgn_trenches_enabled,
            "url_count": len(gmgn_trenches_urls),
            "command_count": len(gmgn_trenches_commands) + len(gmgn_trenches_cli),
            "file_count": len(gmgn_trenches_files),
            "key_present": bool(os.environ.get("GMGN_API_KEY", "").strip()),
            "mode": "gmgn-cli/url/browser/file",
            "needs": "" if gmgn_trenches_cli or gmgn_trenches_urls or gmgn_trenches_commands or gmgn_trenches_files else "GMGN_TRENCHES_CLI or GMGN_TRENCHES_URLS",
            **configured_source_freshness(gmgn_trenches_enabled, gmgn_trenches_files),
        },
        "okx_trenches": {
            "enabled": okx_enabled,
            "url_count": len(okx_urls),
            "command_count": len(okx_commands),
            "file_count": len(okx_files),
            "key_present": okx_key,
            "mode": "official-api/ws-inbox/url/file",
            "official_api": okx_api_status,
            "websocket": okx_stream_status,
            "needs": "" if okx_enabled else "OKX_API_KEY + OKX_SECRET_KEY + OKX_PASSPHRASE (market API) or OKX_TRENCHES_FILES",
            **configured_source_freshness(okx_enabled, okx_files),
            **({"freshness_level": "red", "freshness_label": "OKX API未就绪"}
               if okx_api_status["configured"] and not okx_api_status["ok"] else {}),
            **({"freshness_level": "yellow", "freshness_label": "OKX WS需白名单/套餐"}
               if "ws_channel_not_whitelisted" in str(okx_stream_status.get("reason") or "") else {}),
        },
        "okx_signal": {
            "enabled": okx_signal_enabled,
            "url_count": 0,
            "command_count": 0,
            "file_count": len(okx_signal_files),
            "key_present": okx_key,
            "mode": "official-api/ws-inbox/file",
            "official_api": {**okx_api_status, "row_count": okx_api_status.get("signal_row_count", 0)},
            "websocket": okx_stream_status,
            "needs": "" if okx_signal_enabled else "OKX_API_KEY + OKX_SECRET_KEY + OKX_PASSPHRASE (market API) or outputs/meme-source-inbox/okx-signal-stream.json",
            **configured_source_freshness(okx_signal_enabled, okx_signal_files),
            **({"freshness_level": "red", "freshness_label": "OKX信号未就绪"}
               if okx_api_status["configured"] and not okx_api_status["ok"] else {}),
            **({"freshness_level": "yellow", "freshness_label": "OKX WS需白名单/套餐"}
               if "ws_channel_not_whitelisted" in str(okx_stream_status.get("reason") or "") else {}),
        },
        "debot": {
            "enabled": debot_enabled,
            "url_count": len(debot_trenches_urls) + len(debot_signal_urls),
            "command_count": len(debot_trenches_commands) + len(debot_signal_commands),
            "file_count": len(debot_trenches_files) + len(debot_signal_files),
            "key_present": bool(os.environ.get("DEBOT_API_KEY", "").strip()),
            "mode": "url/browser/file/inbox",
            "needs": "" if debot_trenches_urls or debot_signal_urls or debot_trenches_commands or debot_signal_commands or debot_trenches_files or debot_signal_files else "DEBOT_TRENCHES_URLS or DEBOT_SIGNAL_URLS or DEBOT_TRENCHES_COMMANDS or DEBOT_SIGNAL_COMMANDS or DEBOT_TRENCHES_FILES or DEBOT_SIGNAL_FILES or outputs/meme-source-inbox/debot-signals.json",
            **configured_source_freshness(debot_enabled, debot_trenches_files + debot_signal_files),
        },
        "binance_wallet_hot": {
            "enabled": binance_wallet_hot_enabled,
            "url_count": len(binance_wallet_hot_urls),
            "command_count": len(binance_wallet_hot_commands),
            "file_count": len(binance_wallet_hot_files),
            "key_present": env_any_present("BINANCE_WALLET_API_KEY", "BINANCE_SKILLS_API_KEY"),
            "mode": "wallet-market-rank/meme-rush/url/command/file/inbox",
            "needs": "" if binance_wallet_hot_enabled else "BINANCE_WALLET_HOT_URLS or BINANCE_WALLET_HOT_COMMANDS or BINANCE_WALLET_HOT_FILES or outputs/meme-source-inbox/binance-wallet-hot.json",
            **configured_source_freshness(binance_wallet_hot_enabled, binance_wallet_hot_files),
        },
        "binance_wallet_signal": {
            "enabled": binance_wallet_signal_enabled,
            "url_count": len(binance_wallet_signal_urls),
            "command_count": len(binance_wallet_signal_commands),
            "file_count": len(binance_wallet_signal_files),
            "key_present": env_any_present("BINANCE_WALLET_API_KEY", "BINANCE_SKILLS_API_KEY"),
            "mode": "wallet-smart-money-signal/url/command/file/inbox",
            "needs": "" if binance_wallet_signal_enabled else "BINANCE_WALLET_SIGNAL_URLS or BINANCE_WALLET_SIGNAL_COMMANDS or BINANCE_WALLET_SIGNAL_FILES or outputs/meme-source-inbox/binance-wallet-signals.json",
            **configured_source_freshness(binance_wallet_signal_enabled, binance_wallet_signal_files),
        },
        "bsc_onchain": {
            "enabled": bsc_onchain_enabled,
            "command_count": len(bsc_onchain_commands),
            "file_count": len(bsc_onchain_files),
            "key_present": bsc_onchain_wss,
            "mode": "bsc-eth_subscribe/file/inbox",
            "needs": "" if bsc_onchain_enabled else "BSC_WSS_URL or BNBCHAIN_WSS_URL or QUICKNODE_BSC_WSS_URL or outputs/meme-source-inbox/bsc-pancake-pairs.json",
            **configured_source_freshness(bsc_onchain_enabled, bsc_onchain_files),
        },
        "arc_onchain": {
            "enabled": arc_onchain_enabled,
            "command_count": 0,
            "file_count": len(arc_onchain_files),
            "key_present": False,
            "mode": "arc-public-rpc-poll/file/inbox",
            "poller": arc_poller_status,
            "needs": "" if arc_onchain_enabled else "outputs/meme-source-inbox/arc-onchain.json",
            **(
                source_freshness(arc_onchain_files)
                if arc_onchain_files
                else poller_status_freshness(arc_poller_status)
                if arc_poller_status
                else configured_source_freshness(False)
            ),
            **({"freshness_level": "red", "freshness_label": "ARC轮询异常"}
               if arc_poller_status and not arc_poller_status.get("ok") else {}),
            **({"freshness_level": "yellow", "freshness_label": "ARC轮询降级"}
               if arc_poller_status.get("status") == "degraded" and arc_poller_status.get("ok") else {}),
        },
        "fourmeme_launchpad": {
            "enabled": fourmeme_enabled,
            "command_count": len(fourmeme_commands),
            "file_count": len(fourmeme_files),
            "key_present": fourmeme_key,
            "mode": "bitquery-ws/file/inbox",
            "needs": "" if fourmeme_enabled else "BITQUERY_API_KEY or BITQUERY_TOKEN or outputs/meme-source-inbox/fourmeme-launches.json",
            **configured_source_freshness(fourmeme_enabled, fourmeme_files),
        },
        "flap_launchpad": {
            "enabled": flap_enabled,
            "command_count": len(flap_commands),
            "file_count": len(flap_files),
            "key_present": flap_key,
            "mode": "bitquery-ws/file/inbox",
            "needs": "" if flap_enabled else "BITQUERY_API_KEY or BITQUERY_TOKEN or outputs/meme-source-inbox/flap-launches.json",
            **configured_source_freshness(flap_enabled, flap_files),
        },
        "noxa_launchpad": {
            "enabled": noxa_enabled,
            "command_count": len(noxa_commands),
            "file_count": len(noxa_files),
            "key_present": False,
            "mode": "public-telegram/file/inbox",
            "needs": "" if noxa_enabled else "NOXA_LAUNCHPAD_COMMANDS or NOXA_LAUNCHPAD_FILES or outputs/meme-source-inbox/noxa-launches.json",
            **configured_source_freshness(noxa_enabled, noxa_files),
        },
        "proficy_trending": {
            "enabled": proficy_enabled,
            "url_count": len(proficy_urls),
            "command_count": len(proficy_commands),
            "file_count": len(proficy_files),
            "key_present": False,
            "mode": "public-trending/file/inbox",
            "needs": "" if proficy_enabled else "PROFICY_TRENDING_URLS or PROFICY_TRENDING_COMMANDS or PROFICY_TRENDING_FILES or outputs/meme-source-inbox/proficy-trending.json",
            **configured_source_freshness(proficy_enabled, proficy_files),
        },
        "985_monitor": {
            "enabled": monitor985_enabled,
            "url_count": len(monitor985_urls),
            "command_count": len(monitor985_commands),
            "file_count": len(monitor985_files),
            "key_present": False,
            "mode": "public-api/file/inbox",
            "needs": "" if monitor985_enabled else "MONITOR985_URLS or MONITOR985_COMMANDS or MONITOR985_FILES or outputs/meme-source-inbox/985-monitor.json",
            **configured_source_freshness(monitor985_enabled, monitor985_files),
        },
        "985_fomo_wallets": {
            "enabled": monitor985_fomo_enabled,
            "url_count": len(monitor985_fomo_urls),
            "command_count": len(monitor985_fomo_commands),
            "file_count": len(monitor985_fomo_files),
            "key_present": False,
            "mode": "public-fomo-profile/file/inbox",
            "needs": "" if monitor985_fomo_enabled else "MONITOR985_FOMO_URLS or MONITOR985_FOMO_FILES or outputs/meme-source-inbox/985-fomo-wallets.json",
            **configured_source_freshness(monitor985_fomo_enabled, monitor985_fomo_files),
        },
        "985_smartmoney": {
            "enabled": monitor985_smartmoney_enabled,
            "url_count": len(monitor985_smartmoney_urls),
            "command_count": len(monitor985_smartmoney_commands),
            "file_count": len(monitor985_smartmoney_files),
            "key_present": False,
            "mode": "public-smartmoney/file/inbox",
            "needs": "" if monitor985_smartmoney_enabled else "MONITOR985_SMARTMONEY_URLS or MONITOR985_SMARTMONEY_FILES or outputs/meme-source-inbox/985-smartmoney.json",
            **configured_source_freshness(monitor985_smartmoney_enabled, monitor985_smartmoney_files),
        },
        "wind_monitor": {
            "enabled": wind_enabled,
            "url_count": len(wind_urls),
            "command_count": len(wind_commands),
            "file_count": len(wind_files),
            "key_present": False,
            "mode": "public-api/gmgn-subscription/ca-feed/url/command/file/inbox",
            "exporter": wind_export_status,
            "needs": "" if wind_enabled else "WIND_MONITOR_URLS or WIND_MONITOR_COMMANDS or WIND_MONITOR_FILES or outputs/meme-source-inbox/wind-monitor.json",
            **configured_source_freshness(wind_enabled, wind_files),
            **({"freshness_level": "green", "freshness_label": "本轮检查"}
               if wind_export_status.get("ok") and not wind_files else {}),
            **({"freshness_level": "yellow", "freshness_label": "听风feed限流"}
               if str(wind_export_status.get("feed_error") or "").startswith("http_429") and not wind_files else {}),
            **({"freshness_level": "red", "freshness_label": "听风接口异常"}
               if wind_export_status and not wind_export_status.get("ok") and not wind_files else {}),
        },
        "pumpfun_live": {
            "enabled": pumpfun_enabled,
            "command_count": len(pumpfun_commands),
            "file_count": len(pumpfun_files),
            "key_present": pumpfun_key,
            "mode": "sse/file/inbox",
            "needs": "" if pumpfun_files or pumpfun_commands or pumpfun_key else "APIFY_TOKEN or PUMPFUN_LIVE_COMMANDS or PUMPFUN_LIVE_FILES or outputs/meme-source-inbox/pumpfun-live.json",
            **configured_source_freshness(pumpfun_enabled, pumpfun_files),
        },
        "pumpfun_onchain": {
            "enabled": pumpfun_onchain_enabled,
            "command_count": len(pumpfun_onchain_commands),
            "file_count": len(pumpfun_onchain_files),
            "key_present": pumpfun_onchain_wss,
            "mode": "solana-logsSubscribe/file/inbox",
            "needs": "" if pumpfun_onchain_enabled else "HELIUS_API_KEY or SOLANA_PUMPFUN_WSS_URL or QUICKNODE_WSS_URL or outputs/meme-source-inbox/pumpfun-onchain.json",
            **configured_source_freshness(pumpfun_onchain_enabled, pumpfun_onchain_files),
        },
        "mobula": {
            "enabled": mobula_enabled,
            "url_count": len(mobula_urls),
            "key_present": mobula_key,
            **configured_source_freshness(mobula_enabled),
        },
        "birdeye": {
            "enabled": birdeye_enabled,
            "url_count": len(birdeye_urls) + len(birdeye_default_urls),
            "command_count": len(birdeye_commands),
            "file_count": len(birdeye_files),
            "key_present": birdeye_key,
            "mode": "url/browser/file/inbox",
            "needs": "" if birdeye_key or birdeye_urls or birdeye_commands or birdeye_files else "BIRDEYE_API_KEY or BIRDEYE_TRENDING_URLS or BIRDEYE_TRENDING_COMMANDS or BIRDEYE_TRENDING_FILES or outputs/meme-source-inbox/birdeye-trending.json",
            **configured_source_freshness(birdeye_enabled, birdeye_files),
        },
    }
    if not os.environ.get("ALPHA_SHOW_SOLANA_SOURCES", "").strip():
        for key in ("pumpfun_live", "pumpfun_onchain"):
            item = status.get(key) or {}
            if not item.get("enabled"):
                status.pop(key, None)
    return status


def is_gmgn_source(source: str) -> bool:
    return source.startswith("gmgn_")


def external_source_label(source: str) -> str:
    if source.startswith("bsc_"):
        return "BSC链上"
    if source.startswith("arc_"):
        return "ARC链上"
    if source.startswith("fourmeme_"):
        return "Four.meme"
    if source.startswith("flap_"):
        return "Flap"
    if source.startswith("noxa_"):
        return "Noxa"
    if source.startswith("proficy_"):
        return "Proficy"
    if source == "985_monitor":
        return "985"
    if source == "985_fomo_wallets":
        return "985 FOMO"
    if source == "985_smartmoney":
        return "985 SM"
    if source == "wind_monitor":
        return "听风"
    if source.startswith("pumpfun_"):
        return "Pump.fun"
    if source == "okx_signal":
        return "OKX信号"
    if source.startswith("okx_"):
        return "OKX"
    if source == "binance_wallet_signal":
        return "币安钱包信号"
    if source.startswith("binance_wallet_"):
        return "币安钱包"
    if source.startswith("mobula_"):
        return "Mobula"
    if source.startswith("birdeye_"):
        return "Birdeye"
    if source.startswith("debot_"):
        return "DeBot"
    return ""


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
    if raw.startswith("arc_"):
        return "arc_onchain"
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


def source_groups(sources: list[str] | None) -> list[str]:
    groups: list[str] = []
    for source in sources or []:
        group = source_group(source)
        if group and group not in groups:
            groups.append(group)
    return groups


def independent_source_count(sources: list[str] | None) -> int:
    return len(source_groups(sources))


def source_hit_counts(sources: list[str] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources or []:
        key = str(source or "").strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def source_repeat_metrics(sources: list[str] | None) -> dict[str, Any]:
    counts = source_hit_counts(sources)
    repeat_score = 0.0
    repeat_flags: list[str] = []
    for source, count in sorted(counts.items()):
        if count <= 1:
            continue
        per_hit = 4.0 if source in {"okx_trenches", "gmgn_trenches", "debot_trenches", "debot_signal", "binance_wallet_hot", "binance_wallet_signal"} else 2.0
        repeat_score += min(24.0, (count - 1) * per_hit)
        repeat_flags.append(f"{source}_x{count}")
    return {
        "source_hit_counts": counts,
        "source_hit_total": sum(counts.values()),
        "source_repeat_score": round(min(32.0, repeat_score), 2),
        "source_repeat_flags": repeat_flags[:6],
    }


def heat_metrics(item: dict[str, Any]) -> dict[str, Any]:
    raw_sources = [str(source) for source in (item.get("sources") or []) if source]
    sources = sorted(set(raw_sources))
    repeat = source_repeat_metrics(raw_sources)
    profile = item.get("profile") or {}
    heat_score = 0.0
    heat_flags: list[str] = []
    strongest_grouped: dict[str, tuple[int, str]] = {}
    for source in sources:
        weight = HEAT_SOURCE_WEIGHTS.get(source, 0)
        group = source_group(source)
        if not weight:
            continue
        if group in {"gmgn", "okx"}:
            strongest = strongest_grouped.get(group)
            if strongest is None or weight > strongest[0]:
                strongest_grouped[group] = (weight, source)
            continue
        heat_score += weight
        heat_flags.append(source)
    for weight, source in strongest_grouped.values():
        heat_score += weight
        heat_flags.append(source)
    boost = alpha.to_float(profile.get("totalAmount") or profile.get("amount"))
    if boost > 0:
        heat_score += min(18, boost / 25)
        heat_flags.append(f"boost_amount_{boost:.0f}")
    gmgn_score = alpha.to_float(profile.get("gmgn_score"))
    if gmgn_score > 0:
        heat_score += min(15, gmgn_score / 8)
        heat_flags.append(f"gmgn_score_{gmgn_score:.0f}")
    lifecycle = launchpad_lifecycle_fields(profile)
    progress = alpha.to_float(lifecycle.get("bonding_curve_progress_pct"))
    purchase_count = alpha.to_float(lifecycle.get("purchase_count"))
    if progress >= 95:
        heat_score += 24
        heat_flags.append("curve_near_graduation")
    elif progress >= 60:
        heat_score += 15
        heat_flags.append("curve_accelerating")
    if purchase_count > 0:
        heat_score += min(10, purchase_count * 1.5)
        heat_flags.append(f"launchpad_buys_{purchase_count:.0f}")
    if lifecycle.get("migration_confirmed"):
        heat_score += 18
        heat_flags.append("launchpad_migrated")
    repeat_score = alpha.to_float(repeat.get("source_repeat_score"))
    if repeat_score > 0:
        heat_score += repeat_score
        heat_flags.extend(repeat.get("source_repeat_flags") or [])
    return {"heat_score": round(heat_score, 2), "heat_flags": heat_flags[:8], **repeat}


def source_signal_fields(sources: list[str] | None) -> dict[str, Any]:
    return {
        **source_repeat_metrics(sources),
        "source_groups": source_groups(sources),
        "source_count": independent_source_count(sources),
    }


NUMERIC_PROFILE_PRESERVE_KEYS = {
    "market_cap",
    "marketCap",
    "mcap",
    "fdv",
    "liquidity",
    "liquidity_usd",
    "liquidityUsd",
    "volume",
    "volume24h",
    "volume_24h",
    "price",
    "price_usd",
    "priceUsd",
}
PRICE_PROFILE_KEYS = {"price", "price_usd", "priceUsd"}
MARKET_PROFILE_METADATA_KEYS = {
    "market_cap_source",
    "fdv_source",
    "valuation_type",
    "observed_at",
    "pair_age_hours",
}
QUOTE_PROFILE_METADATA_KEYS = {
    "quote_observed_at",
    "quote_status",
    "quote_time_basis",
    "quote_source",
}


def _profile_has_market(profile: dict[str, Any]) -> bool:
    return any(alpha.to_float(profile.get(key)) > 0 for key in NUMERIC_PROFILE_PRESERVE_KEYS)


def _profile_has_price(profile: dict[str, Any]) -> bool:
    return any(alpha.to_float(profile.get(key)) > 0 for key in PRICE_PROFILE_KEYS)


def _profile_market_timestamp(profile: dict[str, Any]) -> datetime | None:
    timestamps = [
        parsed
        for parsed in (
            parse_timestamp(profile.get("observed_at")),
            parse_timestamp(profile.get("quote_observed_at")),
        )
        if parsed is not None
    ]
    return max(timestamps, default=None)


def _profile_price_timestamp(profile: dict[str, Any]) -> datetime | None:
    return parse_timestamp(profile.get("quote_observed_at") or profile.get("observed_at"))


def merge_source_profile(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    incoming_pending = bool(incoming.get("market_data_pending"))
    current_has_market = _profile_has_market(current)
    current_has_price = _profile_has_price(current)
    incoming_has_market = _profile_has_market(incoming)
    incoming_has_price = _profile_has_price(incoming)
    current_market_at = _profile_market_timestamp(current)
    incoming_market_at = _profile_market_timestamp(incoming)
    current_price_at = _profile_price_timestamp(current)
    incoming_price_at = _profile_price_timestamp(incoming)
    incoming_market_preferred = bool(
        not incoming_pending
        and incoming_has_market
        and (
            not current_has_market
            or incoming_market_at is not None
            and (current_market_at is None or incoming_market_at >= current_market_at)
        )
    )
    incoming_price_preferred = False
    if not incoming_pending and incoming_has_price:
        incoming_price_preferred = (
            incoming_price_at is None
            or current_price_at is None
            or incoming_price_at >= current_price_at
        )
        incoming_market_preferred = incoming_market_preferred or incoming_price_preferred

    for key, value in incoming.items():
        if value in (None, "", []):
            continue
        if key == "market_data_pending" and value and alpha.to_float(merged.get("liquidity")) > 0:
            continue
        if key in PRICE_PROFILE_KEYS and current_has_price and not incoming_price_preferred:
            continue
        if key in (NUMERIC_PROFILE_PRESERVE_KEYS - PRICE_PROFILE_KEYS) | MARKET_PROFILE_METADATA_KEYS:
            if current_has_market and not incoming_market_preferred:
                continue
            if key in NUMERIC_PROFILE_PRESERVE_KEYS and alpha.to_float(merged.get(key)) > 0:
                if incoming_pending or alpha.to_float(value) <= 0:
                    continue
        if key in QUOTE_PROFILE_METADATA_KEYS and current_has_price and not incoming_price_preferred:
            continue
        merged[key] = value
    if incoming_price_preferred:
        if incoming_market_at is not None:
            merged["quote_observed_at"] = incoming.get("quote_observed_at", incoming.get("observed_at"))
            merged["quote_status"] = incoming.get("quote_status")
        else:
            merged["quote_observed_at"] = None
            merged["quote_status"] = "unavailable"
    elif current_has_price:
        merged["quote_observed_at"] = current.get("quote_observed_at", current.get("observed_at"))
        if "quote_status" in current:
            merged["quote_status"] = current.get("quote_status")
        else:
            merged.pop("quote_status", None)
    return merged


def launchpad_lifecycle_fields(profile: dict[str, Any]) -> dict[str, Any]:
    progress = alpha.to_float(
        first_present(
            profile,
            (
                "bonding_curve_progress_pct",
                "curve_progress_pct",
                "bondingCurveProgress",
                "curveProgress",
                "progress",
                "completion",
                "completeRate",
            ),
        )
    )
    if 0 < progress <= 1:
        progress *= 100
    purchase_count = alpha.to_int(
        first_present(profile, ("purchase_count", "buy_count", "quote_count", "buys", "swaps", "txns24h"))
    )
    bnb_per_hour = alpha.to_float(first_present(profile, ("bnb_per_hour", "buy_velocity_bnb_h", "bnbPerHour")))
    migrated = bool(first_present(profile, ("migration_confirmed", "migrated", "is_migrated", "isMigrated")))
    explicit_stage = str(first_present(profile, ("launchpad_lifecycle_stage", "lifecycle_stage", "stage")) or "").strip()
    stage = explicit_stage or "launchpad_new"
    if migrated:
        stage = "migrated"
    elif progress >= 95:
        stage = "near_graduation"
    elif progress >= 60 or bnb_per_hour > 0 or purchase_count >= 2:
        stage = "curve_accelerating"
    labels = {
        "launchpad_new": "发射台首发",
        "curve_accelerating": "曲线加速",
        "near_graduation": "曲线快毕业",
        "migrated": "已迁移待行情",
        "confirmed_market": "行情确认",
    }
    rationale: list[str] = []
    if progress > 0:
        rationale.append(f"曲线{progress:.1f}%")
    if purchase_count:
        rationale.append(f"买入{purchase_count}次")
    if bnb_per_hour > 0:
        rationale.append(f"速度{bnb_per_hour:.2f}BNB/h")
    if migrated:
        rationale.append("已迁移")
    return {
        "launchpad_lifecycle_stage": stage,
        "launchpad_stage_label": labels.get(stage, stage or "发射台观察"),
        "bonding_curve_progress_pct": round(max(0.0, min(100.0, progress)), 4),
        "curve_progress_pct": round(max(0.0, min(100.0, progress)), 4),
        "purchase_count": purchase_count,
        "buy_count": purchase_count,
        "bnb_per_hour": bnb_per_hour,
        "migration_confirmed": migrated,
        "lifecycle_rationale": rationale[:4],
    }


def source_labels(sources: list[str] | None, profile: dict[str, Any] | None = None) -> list[str]:
    source_set = {str(source) for source in (sources or []) if source}
    market_data_pending = bool((profile or {}).get("market_data_pending"))
    labels: list[str] = [] if market_data_pending else ["DS"]
    if any(is_gmgn_source(source) for source in source_set):
        labels.insert(0, "GMGN")
    for source in sorted(source_set):
        label = external_source_label(source)
        if label and label not in labels:
            labels.insert(0, label)
    if profile and alpha.to_float(profile.get("gmgn_score")) > 0 and "GMGN" not in labels:
        labels.insert(0, "GMGN")
    if not market_data_pending:
        labels.append("Alpha_AI")
    deduped: list[str] = []
    for label in labels:
        if label not in deduped:
            deduped.append(label)
    return deduped


def first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def ratio_pct(value: Any) -> float | None:
    amount = alpha.to_float(value, default=-1)
    if amount < 0:
        return None
    return round(amount * 100 if amount <= 1 else amount, 4)


def gmgn_potential_label(profile: dict[str, Any]) -> str:
    explicit = first_present(profile, ("potential", "potential_label", "potentialLabel", "x_potential"))
    if explicit:
        return str(explicit)
    market_cap = alpha.to_float(profile.get("market_cap") or profile.get("marketCap"))
    liquidity = alpha.to_float(profile.get("liquidity"))
    volume = alpha.to_float(
        profile.get("volume")
        or profile.get("volume24h")
        or profile.get("volume_24h")
        or profile.get("volume_24h_usd")
        or profile.get("volume24hUsd")
    )
    smart_money = alpha.to_int(first_present(profile, ("smart_money", "smartMoney", "smart_degen_count", "smart_money_count", "smartMoneyCount")))
    kol = alpha.to_int(first_present(profile, ("kol", "kol_count", "kolCount", "kols", "renowned_count")))
    holders = alpha.to_int(first_present(profile, ("holder_count", "holders", "holderCount")))
    volume_to_cap = volume / max(1, market_cap)
    if market_cap <= 120_000 and liquidity >= 10_000 and volume_to_cap >= 2 and smart_money >= 25 and kol >= 5 and holders >= 300:
        return "100x+"
    if market_cap <= 1_500_000 and liquidity >= 20_000 and volume_to_cap >= 0.5 and smart_money >= 10 and kol >= 2:
        return "10x+"
    if market_cap <= 5_000_000 and smart_money >= 5:
        return "3x+"
    return ""


def gmgn_risk_flags(profile: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    sniper_count = alpha.to_int(profile.get("sniper_count"))
    bundler_pct = ratio_pct(profile.get("bundler_rate"))
    rug_pct = ratio_pct(profile.get("rug_ratio"))
    top10_pct = ratio_pct(profile.get("top_10_holder_rate"))
    if sniper_count >= 30:
        flags.append(f"sniper_{sniper_count}")
    if bundler_pct is not None and bundler_pct >= 20:
        flags.append(f"bundler_{bundler_pct:.1f}%")
    if rug_pct is not None and rug_pct >= 40:
        flags.append(f"rug_{rug_pct:.1f}%")
    if top10_pct is not None and top10_pct >= 35:
        flags.append(f"top10_{top10_pct:.1f}%")
    return flags[:6]


def gmgn_profile_fields(profile: dict[str, Any]) -> dict[str, Any]:
    smart_money = first_present(profile, ("smart_money", "smartMoney", "smart_degen_count", "smart_money_count", "smartMoneyCount"))
    kol = first_present(profile, ("kol", "kol_count", "kolCount", "kols", "renowned_count"))
    holders = first_present(profile, ("holder_count", "holders", "holderCount"))
    potential = gmgn_potential_label(profile)
    top10_pct = ratio_pct(profile.get("top_10_holder_rate"))
    return {
        "smart_money": alpha.to_int(smart_money),
        "kol": alpha.to_int(kol),
        "holders": alpha.to_int(holders),
        "potential_label": str(potential or ""),
        "top10_holder_pct": top10_pct,
        "gmgn_risk_flags": gmgn_risk_flags(profile),
    }


def okx_profile_fields(profile: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "okx_kind",
        "okx_source_panel",
        "okx_signal_panel",
        "okx_signal_timestamp",
        "okx_signal_type",
        "okx_signal_type_label",
        "okx_wallet_type",
        "okx_trigger_wallet_count",
        "okx_signal_amount_usd",
        "okx_signal_sold_ratio_pct",
        "okx_signal_price_usd",
        "okx_signal_wallet_addresses",
        "okx_volume_1h",
        "okx_tx_count_1h",
        "okx_buy_tx_count_1h",
        "okx_sell_tx_count_1h",
        "okx_bonding_percent",
        "okx_created_timestamp",
        "okx_protocol_id",
        "okx_tags",
        "okx_social",
        "okx_dev_holdings_pct",
        "okx_insiders_pct",
        "okx_bundlers_pct",
        "okx_snipers_pct",
        "okx_fresh_wallets_pct",
        "okx_suspected_phishing_pct",
    )
    return {key: profile.get(key) for key in keys if profile.get(key) not in (None, "", [])}


def cap_score(value: float) -> float:
    return round(max(0.0, min(100.0, alpha.to_float(value))), 2)


def profile_text(item: dict[str, Any]) -> str:
    links = item.get("links") or []
    link_text = " ".join(str(link.get("url") or "") for link in links if isinstance(link, dict))
    return " ".join(
        [
            str(item.get("description") or ""),
            str(item.get("url") or ""),
            link_text,
        ]
    ).lower()


def pair_text(pair: dict[str, Any], profile: dict[str, Any] | None = None) -> str:
    base = pair.get("baseToken") or {}
    info = pair.get("info") or {}
    socials = info.get("socials") or []
    websites = info.get("websites") or []
    bits = [
        str(base.get("name") or ""),
        str(base.get("symbol") or ""),
        str(pair.get("dexId") or ""),
        str(pair.get("url") or ""),
        " ".join(str(item.get("url") or "") for item in socials + websites if isinstance(item, dict)),
    ]
    if profile:
        bits.append(profile_text(profile))
    return " ".join(bits).lower()


def is_meme_like(pair: dict[str, Any], profile: dict[str, Any] | None = None) -> bool:
    text = pair_text(pair, profile)
    return any(keyword in text for keyword in MEME_KEYWORDS)


def fetch_profile_sources(
    limit: int,
    *,
    observed_at: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    sources: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    specs = (
        ("boost_top", DEX_BOOSTS_TOP_URL),
        ("boost_latest", DEX_BOOSTS_LATEST_URL),
        ("ads_latest", DEX_ADS_LATEST_URL),
        ("community_takeover", DEX_CTO_LATEST_URL),
        ("profile_latest", DEX_PROFILES_LATEST_URL),
    )

    def fetch_source(label: str, url: str) -> tuple[str, list[dict[str, Any]], str]:
        try:
            return label, source_rows(http_json_compat(url, timeout=4)), ""
        except Exception as exc:  # noqa: BLE001
            return label, [], str(exc)

    pool = ThreadPoolExecutor(max_workers=len(specs))
    timed_out = False
    futures = [pool.submit(fetch_source, label, url) for label, url in specs]
    try:
        for future in as_completed(futures, timeout=25):
            label, rows, error = future.result()
            if error:
                errors.append(f"{label}: {error}")
                continue
            for row in rows[:limit]:
                chain = str(row.get("chainId") or "").strip()
                address = str(row.get("tokenAddress") or "").strip()
                if not chain or not address:
                    continue
                if not meme_chain_allowed(chain):
                    continue
                key = token_key(chain, address)
                current = sources.setdefault(
                    key,
                    {"chainId": chain, "tokenAddress": address, "sources": [], "profile": {}},
                )
                event_raw = row if "lookup_by_ca" in row else {**row, "lookup_by_ca": False}
                merge_source_record(
                    current,
                    source_record(
                        event_raw,
                        source=label,
                        chain=chain,
                        contract_address=address,
                        profile=row,
                        observed_at=observed_at,
                    ),
                )
    except TimeoutError as exc:
        timed_out = True
        errors.append(f"dexscreener discovery timeout: {exc}")
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    finally:
        if not timed_out:
            pool.shutdown(wait=True)

    def merge_source_rows(source_rows_by_key: dict[str, dict[str, Any]]) -> None:
        for key, row in source_rows_by_key.items():
            if not meme_chain_allowed(row.get("chainId")):
                continue
            current = sources.setdefault(key, {"chainId": row["chainId"], "tokenAddress": row["tokenAddress"], "sources": []})
            merge_source_record(current, row)

    # Inbox snapshots are refreshed before this scan and must not be held hostage
    # by slower optional network adapters.
    local_sources, local_errors = load_local_inbox_sources(limit)
    errors.extend(local_errors)
    merge_source_rows(local_sources)

    optional_timeout = max(
        3,
        alpha.to_int(os.environ.get("MEME_OPTIONAL_SOURCE_TIMEOUT_SECONDS"), default=30),
    )
    pool = ThreadPoolExecutor(max_workers=2)
    timed_out = False
    if observed_at is None:
        optional_futures = [
            pool.submit(fetch_gmgn_sources, limit),
            pool.submit(fetch_external_meme_sources, limit, include_local_files=False),
        ]
    else:
        optional_futures = [
            pool.submit(fetch_gmgn_sources, limit, observed_at=observed_at),
            pool.submit(
                fetch_external_meme_sources,
                limit,
                observed_at=observed_at,
                include_local_files=False,
            ),
        ]
    try:
        for future in as_completed(optional_futures, timeout=optional_timeout):
            source_rows_by_key, source_errors = future.result()
            errors.extend(source_errors)
            merge_source_rows(source_rows_by_key)
    except TimeoutError as exc:
        timed_out = True
        errors.append(f"optional meme source timeout: {exc}")
        for future in optional_futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    finally:
        if not timed_out:
            pool.shutdown(wait=True)
    return sources, errors


def fetch_gmgn_sources(
    limit: int,
    *,
    observed_at: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    sources: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for url, source_label in gmgn_source_specs(limit):
        fallback_chain = gmgn_chain_from_url(url)
        try:
            rows = nested_rows(http_json_compat(url, timeout=6, headers=GMGN_REQUEST_HEADERS))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_label} {url}: {exc}")
            continue
        for row in rows[:limit]:
            chain = chain_from_gmgn(row, fallback=fallback_chain)
            address = address_from_gmgn(row)
            if not chain or not address:
                continue
            if not meme_chain_allowed(chain):
                continue
            key = token_key(chain, address)
            incoming = source_record(
                row,
                source=source_label,
                chain=chain,
                contract_address=address,
                profile={
                    **row,
                    "source_family": source_label,
                    "gmgn_score": alpha.to_float(row.get("score") or row.get("rank_score") or row.get("hot_score")),
                },
                observed_at=observed_at,
            )
            previous = sources.get(key)
            if previous:
                _extend_unique(incoming["source_events"], previous.get("source_events") or [], "event_id")
                _extend_unique(incoming["audit_facts"], previous.get("audit_facts") or [], "evidence_id")
                _extend_unique(
                    incoming["event_rejections"],
                    previous.get("event_rejections") or [],
                    "raw_fingerprint",
                )
            sources[key] = incoming
    cli_sources, cli_errors = (
        fetch_gmgn_cli_sources(limit)
        if observed_at is None
        else fetch_gmgn_cli_sources(limit, observed_at=observed_at)
    )
    errors.extend(cli_errors)
    for key, row in cli_sources.items():
        current = sources.setdefault(key, {"chainId": row["chainId"], "tokenAddress": row["tokenAddress"], "sources": []})
        merge_source_record(current, row)
    return sources, errors


def fetch_gmgn_cli_sources(
    limit: int,
    *,
    observed_at: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    sources: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    specs = gmgn_cli_specs(limit)
    # The live refresh already calls the local Skills Market adapter. Reuse its
    # fresh file so the legacy discovery path does not duplicate API calls.
    skill_status = read_gmgn_skills_status()
    retry_after = alpha.to_float(skill_status.get("retry_after_epoch"))
    skill_inbox = ROOT / "outputs" / "meme-source-inbox"
    trend_cache = skill_inbox / "gmgn-skills-trending.json"
    trenches_cache = skill_inbox / "gmgn-skills-trenches.json"
    if retry_after > time.time() or (trend_cache.exists() and time.time() - trend_cache.stat().st_mtime <= 90):
        specs = []
    trenches_specs = (
        []
        if retry_after > time.time() or (trenches_cache.exists() and time.time() - trenches_cache.stat().st_mtime <= 90)
        else gmgn_trenches_cli_specs(limit)
    )
    if not specs and not trenches_specs:
        return sources, errors
    npx = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
    timeout = max(5, alpha.to_int(os.environ.get("GMGN_CLI_TIMEOUT_SECONDS"), default=20))
    env = os.environ.copy()
    env.setdefault("GMGN_API_KEY", "gmgn_solbscbaseethmonadtron")
    env.pop("GMGN_PRIVATE_KEY", None)
    for chain, interval, per_query_limit in specs:
        cmd = [
            npx,
            "--yes",
            "gmgn-cli",
            "market",
            "trending",
            "--chain",
            chain,
            "--interval",
            interval,
            "--order-by",
            "volume",
            "--limit",
            str(per_query_limit),
            "--raw",
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gmgn_live_trending {chain}/{interval}: {exc}")
            continue
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip().splitlines()
            errors.append(f"gmgn_live_trending {chain}/{interval}: {message[-1] if message else 'command failed'}")
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"gmgn_live_trending {chain}/{interval}: invalid json {exc}")
            continue
        for row in nested_rows(payload)[:per_query_limit]:
            normalized_chain = chain_from_gmgn(row, fallback=chain)
            address = address_from_gmgn(row)
            if not normalized_chain or not address:
                continue
            if not meme_chain_allowed(normalized_chain):
                continue
            key = token_key(normalized_chain, address)
            profile = {
                **row,
                "chain": normalized_chain,
                "source_family": "gmgn_live_trending",
                "gmgn_score": alpha.to_float(row.get("hot_level") or row.get("rank")),
                "gmgn_cli_interval": interval,
            }
            current = sources.setdefault(
                key,
                {"chainId": normalized_chain, "tokenAddress": address, "sources": [], "profile": {}},
            )
            merge_source_record(
                current,
                source_record(
                    row,
                    source="gmgn_live_trending",
                    chain=normalized_chain,
                    contract_address=address,
                    profile=profile,
                    observed_at=observed_at,
                ),
            )
    for chain, types, per_query_limit in trenches_specs:
        cmd = [
            npx,
            "--yes",
            "gmgn-cli",
            "market",
            "trenches",
            "--chain",
            chain,
            "--limit",
            str(per_query_limit),
            "--raw",
        ]
        for item in types:
            cmd.extend(["--type", item])
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
                **hidden_subprocess_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gmgn_trenches {chain}: {exc}")
            continue
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip().splitlines()
            errors.append(f"gmgn_trenches {chain}: {message[-1] if message else 'command failed'}")
            continue
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"gmgn_trenches {chain}: invalid json {exc}")
            continue
        for row in nested_rows(payload)[: per_query_limit * max(1, len(types))]:
            normalized_chain = chain_from_gmgn(row, fallback=chain)
            address = address_from_gmgn(row)
            if not normalized_chain or not address:
                continue
            if not meme_chain_allowed(normalized_chain):
                continue
            key = token_key(normalized_chain, address)
            profile = {
                **row,
                "chain": normalized_chain,
                "source_family": "gmgn_trenches",
                "gmgn_score": alpha.to_float(row.get("smart_degen_count")) * 2
                + alpha.to_float(row.get("renowned_count")) * 3
                + alpha.to_float(row.get("visiting_count")) * 0.1,
                "gmgn_trenches_types": ",".join(types),
            }
            current = sources.setdefault(
                key,
                {"chainId": normalized_chain, "tokenAddress": address, "sources": [], "profile": {}},
            )
            merge_source_record(
                current,
                source_record(
                    row,
                    source="gmgn_trenches",
                    chain=normalized_chain,
                    contract_address=address,
                    profile=profile,
                    observed_at=observed_at,
                ),
            )
    return sources, errors


def fetch_external_meme_sources(
    limit: int,
    *,
    observed_at: str | None = None,
    include_local_files: bool = True,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    sources: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def merge_payload_rows(payload: Any, source_label: str, source_name: str) -> None:
        for row in nested_rows(payload)[: external_source_row_limit(source_label, limit)]:
            normalized = normalize_external_meme_row(
                row, source_label, observed_at=observed_at
            )
            if not normalized:
                continue
            key = token_key(normalized["chainId"], normalized["tokenAddress"])
            current = sources.setdefault(key, {"chainId": normalized["chainId"], "tokenAddress": normalized["tokenAddress"], "sources": [], "profile": {}})
            normalized["profile"] = {
                **normalized.get("profile", {}),
                "source_origin": source_name,
            }
            merge_source_record(current, normalized)

    okx_rows, okx_errors = alpha_okx_market.fetch(meme_chain_allowlist(), external_source_row_limit("okx_trenches", limit))
    errors.extend(f"okx_market {error}" for error in okx_errors)
    alpha_okx_market.persist_snapshot(ROOT / "outputs" / "meme-source-inbox", okx_rows, okx_errors)
    for row in okx_rows:
        label = "okx_signal" if row.get("okx_kind") == "SIGNAL" else "okx_trenches"
        merge_payload_rows([row], label, "OKX official market API")

    for url, source_label in external_source_specs(limit):
        try:
            merge_payload_rows(http_json_compat(url, timeout=6, headers=external_source_headers(source_label)), source_label, url)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_label} {url}: {exc}")
            continue
    if include_local_files:
        for path, source_label in external_file_source_specs():
            try:
                merge_payload_rows(json.loads(Path(path).read_text(encoding="utf-8-sig")), source_label, path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source_label} file {path}: {exc}")
                continue
    for command, source_label in external_command_source_specs(limit):
        try:
            merge_payload_rows(command_json(command), source_label, command[:120])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_label} command: {exc}")
            continue
    return sources, errors


def load_local_inbox_sources(limit: int) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load only normalized local source snapshots for the low-latency path."""
    sources: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for path, source_label in external_file_source_specs():
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source_label} file {path}: {exc}")
            continue
        snapshot_observed_at = (
            first_present(payload, ("observed_at", "updated_at", "fetched_at"))
            if isinstance(payload, dict)
            else None
        )
        if not snapshot_observed_at:
            snapshot_observed_at = datetime.fromtimestamp(
                Path(path).stat().st_mtime,
                timezone.utc,
            ).isoformat()
        for row in nested_rows(payload)[: external_source_row_limit(source_label, limit)]:
            normalized = normalize_external_meme_row(
                row,
                source_label,
                observed_at=str(snapshot_observed_at),
            )
            if not normalized:
                continue
            key = token_key(normalized["chainId"], normalized["tokenAddress"])
            current = sources.setdefault(
                key,
                {
                    "chainId": normalized["chainId"],
                    "tokenAddress": normalized["tokenAddress"],
                    "sources": [],
                    "profile": {},
                },
            )
            normalized["profile"] = {
                **normalized.get("profile", {}),
                "source_origin": path,
            }
            merge_source_record(current, normalized)
    for item in sources.values():
        item["sources"] = sorted(set(item.get("sources") or []))
    return sources, errors


def fetch_pairs_for_source(item: dict[str, Any]) -> list[dict[str, Any]]:
    url = alpha.DEX_TOKEN_PAIRS_URL.format(
        chain=urllib.parse.quote(str(item["chainId"]), safe=""),
        address=urllib.parse.quote(str(item["tokenAddress"]), safe=""),
    )
    payload = http_json_compat(url, timeout=6)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("pairs"), list):
        return payload["pairs"]
    return []


def batch_pair_key(chain: str, address: str) -> tuple[str, str]:
    chain = normalize_chain(chain)
    address = str(address or "").strip()
    # Solana mint addresses are case-sensitive; EVM hex addresses are not.
    return chain, address.lower() if address.startswith(("0x", "0X")) and chain != "solana" else address


def fetch_pairs_for_batch(chain: str, addresses: list[str]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    if not 1 <= len(addresses) <= 30:
        raise ValueError("DEX token batches require 1-30 addresses")
    url = "https://api.dexscreener.com/tokens/v1/{}/{}".format(
        urllib.parse.quote(chain, safe=""),
        ",".join(urllib.parse.quote(address, safe="") for address in addresses),
    )
    payload = http_json_compat(url, timeout=6)
    if not isinstance(payload, list):
        raise ValueError("DEX batch response must be a list of pairs")
    matched = {batch_pair_key(chain, address): [] for address in addresses}
    for pair in payload:
        if not isinstance(pair, dict) or not isinstance(pair.get("baseToken"), dict):
            continue
        key = batch_pair_key(pair.get("chainId", ""), pair["baseToken"].get("address", ""))
        if key in matched:
            matched[key].append(pair)
    return matched


def batch_pair_futures(pool, source_items, pair_timeout, errors):
    grouped = {}
    for item in source_items:
        chain, address = batch_pair_key(item["chainId"], item["tokenAddress"])
        grouped.setdefault(chain, {}).setdefault(address, []).append(item)
    jobs = {}
    for chain, by_address in grouped.items():
        addresses = list(by_address)
        for offset in range(0, len(addresses), 30):
            batch = addresses[offset:offset + 30]
            items = [item for address in batch for item in by_address[address]]
            jobs[pool.submit(fetch_pairs_for_batch, chain, batch)] = items
    results, finished = {}, set()

    def publish(items, pairs=None, error=None):
        # Reuse the existing per-profile processing/fallback without scheduling
        # any per-token HTTP calls, including after 429 or partial responses.
        for item in items:
            future = Future()
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(pairs.get(batch_pair_key(item["chainId"], item["tokenAddress"]), []))
            results[future] = item

    try:
        for job in as_completed(jobs, timeout=pair_timeout):
            finished.add(job)
            try:
                pairs = job.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dex_batch {normalize_chain(jobs[job][0]['chainId'])}: {exc}")
                publish(jobs[job], error=exc)
            else:
                publish(jobs[job], pairs=pairs)
    except TimeoutError:
        errors.append("dex_batch total timeout; using profile fallbacks")
    for job, items in jobs.items():
        if job not in finished:
            job.cancel()
            publish(items, error=TimeoutError("DEX batch timeout"))
    return results


def gmgn_url_chain(chain: str) -> str:
    normalized = normalize_gmgn_chain(chain)
    if normalized == "solana":
        return "sol"
    if normalized == "ethereum":
        return "eth"
    return normalized


def gmgn_pair_age_hours(profile: dict[str, Any], now_ms: int) -> float | None:
    created = alpha.to_float(
        first_present(profile, ("creation_timestamp", "open_timestamp", "start_live_timestamp", "pool_created_at"))
    )
    if created <= 0:
        return None
    if created > 10_000_000_000:
        created_ms = created
    else:
        created_ms = created * 1000
    return max(0.0, (now_ms - created_ms) / 3_600_000)


def gmgn_candidate_from_profile(item: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
    if not any(is_gmgn_source(str(source)) for source in (item.get("sources") or [])):
        return None
    return candidate_from_profile(item, now_ms)


def candidate_from_profile(item: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
    profile = item.get("profile") or {}
    chain = chain_from_gmgn(profile, fallback=str(item.get("chainId") or ""))
    address = address_from_gmgn(profile) or str(item.get("tokenAddress") or "")
    symbol = str(profile.get("symbol") or profile.get("trans_symbol") or "")
    name = str(profile.get("name") or profile.get("trans_name") or "")
    valuation = profile_valuation_fields(profile, str(profile.get("source_family") or "gmgn_profile"))
    mcap = valuation["mcap"]
    liquidity = alpha.to_float(profile.get("liquidity"))
    volume = alpha.to_float(profile.get("volume") or profile.get("volume24h"))
    market_data_pending = bool(profile.get("market_data_pending"))
    if not meme_chain_allowed(chain):
        return None
    if not chain or not address or not symbol:
        return None
    if not market_data_pending and (not (mcap or valuation["fdv"]) or liquidity <= 0):
        return None
    gmgn_fields = gmgn_profile_fields(profile)
    heat = heat_metrics(item)
    lifecycle = launchpad_lifecycle_fields(profile)
    change_h1 = alpha.to_float(profile.get("price_change_percent1h") or profile.get("price_change_percent"))
    change_m5 = alpha.to_float(profile.get("price_change_percent5m"))
    quote_observed_at = profile.get("quote_observed_at", profile.get("observed_at"))
    stamp = parse_timestamp(quote_observed_at)
    age_seconds = now_ms / 1000 - stamp.timestamp() if stamp else None
    quote_status = "unavailable"
    if (not market_data_pending and alpha.to_float(profile.get("price")) > 0
            and age_seconds is not None and age_seconds >= 0):
        quote_status = "fresh" if age_seconds <= QUOTE_FRESH_SECONDS else "stale"
        if profile.get("quote_status") not in (None, "", "fresh"):
            quote_status = str(profile["quote_status"])
    quote_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "price_usd": alpha.to_float(profile.get("price")),
                "mcap": mcap,
                "liquidity": liquidity,
                "volume24h": volume,
                "txns24h": alpha.to_int(profile.get("swaps") or profile.get("txns24h")),
                "token_address": address,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()[:20]
    score = 0.0
    score += min(28, volume / max(1, mcap) * 10) if mcap else 0
    score += min(20, change_h1 / 30) if change_h1 > 0 else 0
    score += min(10, change_m5 / 8) if change_m5 > 0 else 0
    score += min(14, alpha.to_float(profile.get("swaps")) / 800)
    score += min(12, alpha.to_float(gmgn_fields.get("smart_money")) / 4)
    score += min(8, alpha.to_float(gmgn_fields.get("kol")))
    score += min(32, heat["heat_score"])
    age = gmgn_pair_age_hours(profile, now_ms)
    if age is not None and age <= 24:
        score += 8
    return {
        "score": cap_score(score),
        "symbol": symbol,
        "name": name,
        "image_url": str(
            profile.get("logo")
            or profile.get("logo_url")
            or profile.get("image")
            or profile.get("image_url")
            or profile.get("imageUrl")
            or ""
        ),
        "narrative_tags": infer_narrative_tags(profile),
        "chain": chain,
        "chain_id": chain,
        "contract_address": address,
        "token_address": address,
        "price_usd": alpha.to_float(profile.get("price")),
        **valuation,
        "liquidity": liquidity,
        "volume": volume,
        "volume24h": volume,
        "change_m5": change_m5,
        "change_h1": change_h1,
        "change_h24": alpha.to_float(profile.get("price_change_percent24h") or profile.get("price_change_percent")),
        "txns24h": alpha.to_int(profile.get("swaps") or profile.get("txns24h")),
        "pair_age_hours": age,
        "quote_observed_at": quote_observed_at,
        "quote_source": "gmgn_profile",
        "quote_status": quote_status,
        "quote_time_basis": "source_observation_not_refresh_time",
        "quote_fingerprint": quote_fingerprint if quote_status == "fresh" else "",
        "pair_address": "",
        "market_data_pending": market_data_pending,
        "boost_amount": alpha.to_float(profile.get("dexscr_boost_fee")),
        "heat_score": heat["heat_score"],
        "heat_flags": heat["heat_flags"],
        "sources": sorted(set(item.get("sources") or [])),
        "source_labels": source_labels(item.get("sources") or [], profile),
        **source_signal_fields(item.get("sources") or []),
        **okx_profile_fields(profile),
        **lifecycle,
        **gmgn_fields,
        "url": f"https://gmgn.ai/{gmgn_url_chain(chain)}/token/{address}",
    }


def external_candidate_from_profile(item: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
    if any(is_gmgn_source(str(source)) for source in (item.get("sources") or [])):
        return None
    candidate = candidate_from_profile(item, now_ms)
    if not candidate:
        return None
    candidate["url"] = ""
    return candidate


def _build_meme_candidates(
    sources: dict[str, dict[str, Any]],
    errors: list[str],
    concurrency: int,
    *,
    observed_at: str,
    events: list[dict[str, Any]],
    rejections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    candidates: dict[str, dict[str, Any]] = {}

    pair_timeout = max(5, alpha.to_int(os.environ.get("MEME_PAIR_TOTAL_TIMEOUT_SECONDS"), default=30))
    source_items = [item for item in sources.values() if meme_chain_allowed(item.get("chainId"))]
    if os.environ.get("MEME_SKIP_DEX_ENRICH", "").strip() == "1":
        for item in source_items:
            candidate = (
                gmgn_candidate_from_profile(item, alpha.utc_now_ms())
                or external_candidate_from_profile(item, alpha.utc_now_ms())
            )
            if candidate:
                candidates[token_key(item["chainId"], item["tokenAddress"])] = candidate
        return sorted(candidates.values(), key=lambda row: row["score"], reverse=True), errors

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        if os.environ.get("MEME_BATCH_PAIRS", "").strip() == "1":
            futures = batch_pair_futures(pool, source_items, pair_timeout, errors)
        else:
            futures = {pool.submit(fetch_pairs_for_source, item): item for item in source_items}
        try:
            completed = as_completed(futures, timeout=pair_timeout)
            for future in completed:
                item = futures[future]
                key = token_key(item["chainId"], item["tokenAddress"])
                try:
                    pairs = future.result()
                except Exception as exc:  # noqa: BLE001
                    fallback = gmgn_candidate_from_profile(item, alpha.utc_now_ms()) or external_candidate_from_profile(item, alpha.utc_now_ms())
                    if fallback:
                        candidates[key] = fallback
                        continue
                    errors.append(f"dex_pairs {key}: {exc}")
                    continue
                for pair in pairs:
                    pair_raw = {**pair, "lookup_by_ca": True}
                    pair_evidence = source_evidence(
                        pair_raw,
                        source="pair_detail",
                        chain=str(pair.get("chainId") or item["chainId"]),
                        contract_address=str(item["tokenAddress"]),
                        observed_at=observed_at,
                    )
                    _extend_unique(events, pair_evidence["source_events"], "event_id")
                    _extend_unique(
                        rejections,
                        pair_evidence["event_rejections"],
                        "raw_fingerprint",
                    )
                best = alpha.pick_best_pair(
                    {"contractAddress": item["tokenAddress"]},
                    pairs,
                )
                if best and not meme_chain_allowed(best.get("chainId") or item.get("chainId")):
                    continue
                if not best or not is_meme_like(best, item.get("profile")):
                    fallback = gmgn_candidate_from_profile(item, alpha.utc_now_ms()) or external_candidate_from_profile(item, alpha.utc_now_ms())
                    if fallback:
                        candidates[key] = fallback
                    continue
                metrics = alpha.pair_metrics(best, alpha.utc_now_ms())
                quote_observed_at = datetime.now().astimezone().isoformat(timespec="seconds")
                valuation = profile_valuation_fields(best, "dexscreener")
                mcap = valuation["mcap"]
                liq = alpha.to_float((best.get("liquidity") or {}).get("usd"))
                if not (mcap or valuation["fdv"]) or (mcap or valuation["fdv"]) > 80_000_000 or liq < 2_000:
                    continue
                base = best.get("baseToken") or {}
                price_change = best.get("priceChange") or {}
                volume = best.get("volume") or {}
                buys = alpha.to_int(((best.get("txns") or {}).get("h24") or {}).get("buys"))
                sells = alpha.to_int(((best.get("txns") or {}).get("h24") or {}).get("sells"))
                profile = item.get("profile") or {}
                vol24 = alpha.to_float(volume.get("h24"))
                quote_fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "price_usd": alpha.to_float(best.get("priceUsd")),
                            "liquidity": liq,
                            "volume24h": vol24,
                            "buys24h": buys,
                            "sells24h": sells,
                            "pair_address": best.get("pairAddress"),
                        },
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest()[:20]
                boost = alpha.to_float(profile.get("totalAmount"))
                heat = heat_metrics(item)
                age = metrics.get("pair_age_hours")
                gmgn_fields = gmgn_profile_fields(profile)
                narrative_tags = infer_narrative_tags(
                    {
                        **profile,
                        "symbol": base.get("symbol"),
                        "name": base.get("name"),
                        "url": best.get("url"),
                        "platform": best.get("dexId"),
                    }
                )
                score = 0.0
                score += min(25, vol24 / max(1, mcap) * 80) if mcap else 0
                score += min(18, alpha.to_float(price_change.get("h1")) / 5) if alpha.to_float(price_change.get("h1")) > 0 else 0
                score += min(12, alpha.to_float(price_change.get("m5")) / 4) if alpha.to_float(price_change.get("m5")) > 0 else 0
                score += min(12, (buys + sells) / 80)
                score += min(10, boost / 100)
                score += min(32, heat["heat_score"])
                if age is not None and age <= 24:
                    score += 14
                elif age is not None and age <= 168:
                    score += 7
                if metrics.get("social_count", 0) >= 2:
                    score += 5

                candidates[key] = {
                    "score": cap_score(score),
                    "symbol": str(base.get("symbol") or ""),
                    "name": str(base.get("name") or ""),
                    "image_url": str((best.get("info") or {}).get("imageUrl") or ""),
                    "narrative_tags": narrative_tags,
                    "chain": str(best.get("chainId") or item["chainId"]),
                    "chain_id": str(best.get("chainId") or item["chainId"]),
                    "contract_address": str(item["tokenAddress"]),
                    "token_address": str(item["tokenAddress"]),
                    "price_usd": alpha.to_float(best.get("priceUsd")),
                    **valuation,
                    "liquidity": liq,
                    "volume24h": vol24,
                    "change_m5": alpha.to_float(price_change.get("m5")),
                    "change_h1": alpha.to_float(price_change.get("h1")),
                    "change_h24": alpha.to_float(price_change.get("h24")),
                    "txns24h": buys + sells,
                    "pair_age_hours": age,
                    "quote_observed_at": quote_observed_at,
                    "quote_source": "dexscreener",
                    "quote_status": "fresh",
                    "quote_time_basis": "http_observation_not_trade_timestamp",
                    "quote_fingerprint": quote_fingerprint,
                    "pair_address": best.get("pairAddress") or "",
                    "boost_amount": boost,
                    "heat_score": heat["heat_score"],
                    "heat_flags": heat["heat_flags"],
                    "sources": sorted(set(item.get("sources") or [])),
                    "source_labels": source_labels(item.get("sources") or [], profile),
                    **source_signal_fields(item.get("sources") or []),
                    **okx_profile_fields(profile),
                    **gmgn_fields,
                    "url": best.get("url") or "",
                }
        except TimeoutError:
            pending = [future for future in futures if not future.done()]
            errors.append(f"dex_pairs timeout: {len(pending)} pending after {pair_timeout}s")
            for future in pending:
                future.cancel()
                item = futures[future]
                key = token_key(item["chainId"], item["tokenAddress"])
                fallback = gmgn_candidate_from_profile(item, alpha.utc_now_ms()) or external_candidate_from_profile(item, alpha.utc_now_ms())
                if fallback:
                    candidates[key] = fallback
    return sorted(candidates.values(), key=lambda row: row["score"], reverse=True), errors


def load_meme_observation_batch(
    limit: int,
    concurrency: int,
    source_loader: Any = None,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    stamp = evidence_observed_at(observed_at)
    if source_loader is not None:
        sources, source_errors = source_loader(limit)
    else:
        sources, source_errors = fetch_profile_sources(limit, observed_at=stamp)
    errors = list(source_errors)
    events: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for item in sources.values():
        _extend_unique(events, item.get("source_events") or [], "event_id")
        _extend_unique(
            rejections,
            item.get("event_rejections") or [],
            "raw_fingerprint",
        )

    candidates, errors = _build_meme_candidates(
        sources,
        errors,
        concurrency,
        observed_at=stamp,
        events=events,
        rejections=rejections,
    )
    feed_counts: dict[str, int] = {}
    for event in events:
        feed = str(event["provider_feed"])
        feed_counts[feed] = feed_counts.get(feed, 0) + 1
    return {
        "candidates": candidates,
        "events": events,
        "rejections": rejections,
        "errors": errors,
        "feed_counts": feed_counts,
    }


def load_meme_candidates(
    limit: int,
    concurrency: int,
    source_loader: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    batch = load_meme_observation_batch(limit, concurrency, source_loader)
    return batch["candidates"], batch["errors"]


def meme_holder_chain_id(row: dict[str, Any]) -> str:
    raw = str(row.get("chain_id") or row.get("chain") or "").strip().lower()
    if raw.isdigit() or raw == "solana":
        return raw
    return DEX_CHAIN_TO_EVM_ID.get(raw, raw)


def inferred_meme_total_supply(row: dict[str, Any]) -> float:
    price = alpha.to_float(row.get("price_usd"))
    if price <= 0:
        return 0.0
    value = alpha.to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    return value / price if value > 0 else 0.0


def apply_holder_metrics_to_meme_rows(
    rows: list[dict[str, Any]],
    limit: int,
    holder_provider: str = "auto",
    holder_offset: int = 20,
) -> list[str]:
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        address = str(row.get("contract_address") or row.get("token_address") or "")
        chain_id = meme_holder_chain_id(row)
        if (chain_id == "solana" and address) or (alpha.is_evm_chain_id(chain_id) and address.startswith("0x")):
            candidates.append(row)
        if len(candidates) >= max(0, limit):
            break

    configured_provider = alpha.normalize_holder_provider(holder_provider)
    for row in candidates:
        address = str(row.get("contract_address") or row.get("token_address") or "")
        chain_id = meme_holder_chain_id(row)
        provider = alpha.resolve_holder_provider(configured_provider, chain_id)
        if not provider:
            errors.append(f"Meme holder adapter skipped {row.get('symbol') or address}: no free provider for chain {chain_id}.")
            continue
        try:
            if provider == "solana_rpc":
                metrics = alpha.fetch_solana_holder_metrics(address)
            else:
                total_supply = inferred_meme_total_supply(row)
                if total_supply <= 0:
                    errors.append(f"Meme holder adapter skipped {row.get('symbol') or address}: no inferred supply from price/mcap.")
                    continue
                holders = alpha.fetch_top_holders(
                    provider,
                    chain_id,
                    address,
                    max(20, holder_offset),
                    alpha.holder_api_key(provider),
                )
                metrics = alpha.holder_metrics_from_rows(holders, total_supply)
            if not metrics:
                errors.append(f"Meme holder adapter empty {row.get('symbol') or address}: no holder quantities.")
                continue
            row["top10_holder_pct"] = metrics["top10_holder_pct"]
            row["top20_holder_pct"] = metrics["top20_holder_pct"]
            row["max_holder_pct"] = metrics["max_holder_pct"]
            row["holder_contract_count"] = metrics["holder_contract_count"]
            row["holder_source"] = f"{provider}_{metrics['holder_source']}"
            row["holder_chain_id"] = chain_id
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Meme holder adapter {row.get('symbol') or address} {chain_id}:{address}: {exc}")
        time.sleep(0.25)
    return errors


def apply_solana_holder_metrics_to_meme_rows(rows: list[dict[str, Any]], limit: int) -> list[str]:
    solana_rows = [
        row
        for row in rows
        if str(row.get("chain") or row.get("chain_id") or "").lower() == "solana"
    ]
    return apply_holder_metrics_to_meme_rows(solana_rows, limit=limit, holder_provider="solana_rpc")
