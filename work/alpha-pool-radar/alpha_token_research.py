"""Optional, contract-scoped token research and grounded summary adapters."""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import math
import os
import queue
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


DEFAULT_TIMEOUT_SECONDS = 6.0
MAX_TIMEOUT_SECONDS = 15.0
DEFAULT_CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_PROVIDER_RESULTS = 6
MAX_PROVIDER_RESPONSE_BYTES = 1_000_000
MAX_EVIDENCE_CHARS = 12_000
CACHE_SCHEMA_VERSION = 1
MAX_OFFICIAL_CONTRACTS = 32
MAX_ORPHANED_ADAPTER_CALLS = 4
_ADAPTER_CALL_SLOTS = threading.BoundedSemaphore(MAX_ORPHANED_ADAPTER_CALLS)
ROOT = Path(__file__).resolve().parents[2]
MODEL_ENV_FILES = (ROOT / ".env", ROOT / ".env.local")
MODEL_ENV_KEYS = {
    "TOKEN_RESEARCH_OPENAI_API_KEY",
    "TOKEN_RESEARCH_OPENAI_MODEL",
    "TOKEN_RESEARCH_OPENAI_BASE_URL",
    "TOKEN_RESEARCH_OPENAI_PROXY_URL",
    "TOKEN_RESEARCH_OPENROUTER_ZDR",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
}

SUMMARY_FIELDS = {
    "one_line_judgement",
    "project_narrative",
    "attention_evidence",
    "smart_wallet_evidence",
    "risks",
}
STATEMENT_FIELDS = {"text", "evidence_ids"}
STATEMENT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "evidence_ids"],
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "evidence_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {"type": "string"},
        },
    },
}
SUMMARY_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(SUMMARY_FIELDS),
    "properties": {
        "one_line_judgement": STATEMENT_JSON_SCHEMA,
        "project_narrative": STATEMENT_JSON_SCHEMA,
        "attention_evidence": {"type": "array", "maxItems": 4, "items": STATEMENT_JSON_SCHEMA},
        "smart_wallet_evidence": {"type": "array", "maxItems": 4, "items": STATEMENT_JSON_SCHEMA},
        "risks": {"type": "array", "maxItems": 5, "items": STATEMENT_JSON_SCHEMA},
    },
}
SECRET_FIELD_NAMES = {
    "api_key", "apikey", "key", "authorization", "auth", "password", "passwd",
    "secret", "token", "access_token", "refresh_token", "id_token",
    "bearer_token", "api_token", "signature", "sig", "private_key", "credential",
}
SAFE_TOKEN_FIELDS = {"token_address", "token_contract", "token_name", "token_symbol", "token_id"}
VALID_RESEARCH_STATUSES = {
    "ready", "partial", "no_results", "provider_unavailable",
    "provider_timeout", "provider_error",
}
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
PROMOTIONAL_OR_PREDICTION_RE = re.compile(
    r"(?:稳赚|必涨|暴涨|翻倍|十倍|百倍|千倍|财富密码|梭哈|上车|价格目标|目标价|"
    r"预计.{0,12}(?:上涨|涨到|翻倍)|将会?.{0,8}(?:上涨|暴涨|涨到)|看涨到|"
    r"未来.{0,12}(?:可能|预计|预期|将).{0,8}(?:上涨|下跌|涨到|跌到)|"
    r"\b(?:buy\s+now|guaranteed|price\s+target|will\s+(?:rise|pump)|moon|\d+x)\b)",
    re.IGNORECASE,
)
EVM_CONTRACT_RE = re.compile(r"(?<![0-9a-f])0x[0-9a-f]{40}(?![0-9a-f])", re.IGNORECASE)
SOLANA_CONTRACT_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")


def _bounded_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(timeout):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(0.1, min(timeout, MAX_TIMEOUT_SECONDS))


def _model_environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    if environ is not None:
        return environ
    values: dict[str, str] = {}
    for path in MODEL_ENV_FILES:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in MODEL_ENV_KEYS:
                values[key] = value.strip().strip('"').strip("'")
    for key in MODEL_ENV_KEYS:
        if str(os.environ.get(key) or "").strip():
            values[key] = str(os.environ[key]).strip()
    return values


def _is_openrouter_url(base_url: str) -> bool:
    hostname = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def _observed_at(value: datetime | str | None) -> str:
    parsed = _parse_time(value) if value is not None else datetime.now(timezone.utc)
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_contract(chain: str, contract: Any) -> str:
    value = str(contract or "").strip()
    return value.lower() if str(chain).strip().lower() not in {"solana", "sol"} else value


def build_contract_queries(
    chain: str,
    contract: str,
    *,
    symbol: str = "",
    name: str = "",
) -> list[str]:
    """Build a small stable query set with the contract in every query."""
    normalized_chain = str(chain or "").strip().lower()
    normalized_contract = _normalize_contract(normalized_chain, contract)
    if not normalized_chain or not normalized_contract:
        raise ValueError("chain_and_contract_required")
    context = " ".join(part for part in (str(symbol).strip(), str(name).strip()) if part)
    return [
        normalized_contract,
        f"{normalized_contract} {normalized_chain} {context} project".replace("  ", " ").strip(),
        f'"{normalized_contract}" {normalized_chain} contract public research',
    ]


build_research_queries = build_contract_queries


def _provider_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    for key in ("results", "items", "organic_results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, Mapping):
            nested = _provider_items(value)
            if nested:
                return nested
    return []


def _short_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _page_excerpt(value: Any, limit: int = 1_500) -> str:
    raw = str(value or "")
    title = re.findall(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
    descriptions = re.findall(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']',
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    without_noise = re.sub(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    visible = re.sub(r"<[^>]+>", " ", without_noise)
    return _short_text(" ".join([*title, *descriptions, visible]), limit)


def _contains_exact_contract(value: Any, chain: str, contract: str) -> bool:
    expected = _normalize_contract(chain, contract)
    if not expected:
        return False
    decoded = html.unescape(urllib.parse.unquote(str(value or "")))
    return expected in (_normalize_contract(chain, decoded))


def bound_provider_results(
    payload: Any,
    *,
    query: str,
    chain: str,
    contract: str,
    observed_at: str,
    limit: int = MAX_PROVIDER_RESULTS,
    include_ungrounded: bool = False,
) -> list[dict[str, Any]]:
    """Normalize results, grounding only exact contract matches."""
    bounded_limit = max(0, min(int(limit), MAX_PROVIDER_RESULTS))
    normalized_chain = str(chain or "").strip().lower()
    normalized_contract = _normalize_contract(normalized_chain, contract)
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for raw in _provider_items(payload):
        if len(results) >= bounded_limit or not isinstance(raw, Mapping):
            break
        url = _short_text(raw.get("url") or raw.get("link"), 1_000)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or url in seen_urls:
            continue
        seen_urls.add(url)
        digest = hashlib.sha256(f"{url}\n{query}".encode("utf-8")).hexdigest()[:16]
        source_id = _short_text(raw.get("source_id") or f"research-{digest}", 100)
        title = _short_text(raw.get("title") or raw.get("name"), 300)
        snippet = _short_text(raw.get("snippet") or raw.get("description") or raw.get("content"), 1_200)
        grounded = _contains_exact_contract(f"{title}\n{snippet}\n{url}", normalized_chain, normalized_contract)
        if not grounded and not include_ungrounded:
            continue
        item: dict[str, Any] = {
            "source_id": source_id,
            "source": _short_text(raw.get("source") or "public_search", 80),
            "title": title,
            "url": url,
            "snippet": snippet,
            "query": _short_text(query, 500),
            "is_official": raw.get("is_official") is True,
            "official_candidate": raw.get("official_candidate") is True,
            "grounded": grounded,
        }
        if grounded:
            item.update({
                "observed_at": observed_at,
                "chain": normalized_chain,
                "contract": normalized_contract,
                "grounding": "result_exact_contract",
            })
        results.append(item)
    return results


def parse_duckduckgo_html(page: Any, *, limit: int = MAX_PROVIDER_RESULTS) -> list[dict[str, str]]:
    """Extract a bounded result list from DuckDuckGo's lightweight HTML page."""
    raw = str(page or "")
    anchors = re.findall(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    snippets = re.findall(
        r'<(?:a|div)[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    results: list[dict[str, str]] = []
    for index, (href, title) in enumerate(anchors[: max(0, min(int(limit), MAX_PROVIDER_RESULTS))]):
        decoded = html.unescape(href)
        parsed = urllib.parse.urlparse(decoded if decoded.startswith("http") else f"https:{decoded}")
        query = urllib.parse.parse_qs(parsed.query)
        target = urllib.parse.unquote(query.get("uddg", [decoded])[0])
        clean_title = _short_text(html.unescape(re.sub(r"<[^>]+>", " ", title)), 300)
        snippet = snippets[index] if index < len(snippets) else ""
        clean_snippet = _short_text(html.unescape(re.sub(r"<[^>]+>", " ", snippet)), 1_200)
        results.append({"title": clean_title, "url": target, "snippet": clean_snippet, "source": "duckduckgo"})
    return results


def duckduckgo_search_provider(
    *, opener: Callable[..., Any] = urllib.request.urlopen,
) -> Callable[[str, float], Any]:
    """Build a no-key public-search adapter for the asynchronous research path."""

    def search(query: str, timeout: float) -> Any:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        request = urllib.request.Request(
            url,
            headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 alpha-token-research/1"},
        )
        with opener(request, timeout=_bounded_timeout(timeout)) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ValueError("provider_response_too_large")
        return parse_duckduckgo_html(raw.decode("utf-8", errors="replace"))

    return search


def _public_http_url(value: Any) -> str:
    url = _short_text(value, 1_000)
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not host or host == "localhost" or host.endswith(".local"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    return "" if not address.is_global else url


def _resolved_public_http_url(value: Any) -> str:
    url = _public_http_url(value)
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return ""
    resolved = []
    for address in addresses:
        try:
            resolved.append(ipaddress.ip_address(address[4][0]))
        except (ValueError, IndexError, TypeError):
            return ""
    return url if resolved and all(address.is_global for address in resolved) else ""


def public_page_fetcher(
    *, opener: Callable[..., Any] | None = None,
) -> Callable[[str, float], str]:
    """Build a bounded page reader for public URLs discovered from exact-CA data."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, file_pointer, code, message, headers, new_url):
            return None

    open_request = opener or urllib.request.build_opener(NoRedirect()).open

    def fetch(url: str, timeout: float) -> str:
        current_url = url
        for _ in range(4):
            safe_url = _resolved_public_http_url(current_url)
            if not safe_url:
                raise ValueError("non_public_page_url")
            request = urllib.request.Request(
                safe_url,
                headers={"Accept": "text/html,text/plain", "User-Agent": "Mozilla/5.0 alpha-token-research/1"},
            )
            try:
                response_context = open_request(request, timeout=_bounded_timeout(timeout))
            except urllib.error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise
                response_context = exc
            with response_context as response:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location") if getattr(response, "headers", None) else None
                    next_url = urllib.parse.urljoin(safe_url, str(location or ""))
                    if not _resolved_public_http_url(next_url):
                        raise ValueError("non_public_page_url")
                    current_url = next_url
                    continue
                final_url = response.geturl() if callable(getattr(response, "geturl", None)) else safe_url
                if not _resolved_public_http_url(final_url):
                    raise ValueError("non_public_page_url")
                raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
                raise ValueError("page_response_too_large")
            return raw.decode("utf-8", errors="replace")
        raise ValueError("too_many_page_redirects")

    return fetch


def dexscreener_search_provider(
    chain: str,
    contract: str,
    *,
    fallback: Callable[[str, float], Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Callable[[str, float], Any]:
    """Resolve an exact contract to current market data and its claimed public links."""

    normalized_chain = str(chain or "").strip().lower()
    normalized_contract = _normalize_contract(normalized_chain, contract)
    cached: list[dict[str, Any]] | None = None

    def load(timeout: float) -> list[dict[str, Any]]:
        nonlocal cached
        if cached is not None:
            return cached
        endpoint = (
            "https://api.dexscreener.com/tokens/v1/"
            f"{urllib.parse.quote(normalized_chain, safe='')}/{urllib.parse.quote(normalized_contract, safe='')}"
        )
        request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": "alpha-token-research/1"},
        )
        with opener(request, timeout=_bounded_timeout(timeout)) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ValueError("provider_response_too_large")
        payload = json.loads(raw)
        pairs = payload if isinstance(payload, list) else []
        results: list[dict[str, Any]] = []
        for pair in pairs:
            if not isinstance(pair, Mapping):
                continue
            pair_chain = str(pair.get("chainId") or "").strip().lower()
            base = pair.get("baseToken") if isinstance(pair.get("baseToken"), Mapping) else {}
            pair_contract = _normalize_contract(pair_chain, base.get("address"))
            if pair_chain != normalized_chain or pair_contract != normalized_contract:
                continue
            liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), Mapping) else {}
            volume = pair.get("volume") if isinstance(pair.get("volume"), Mapping) else {}
            txns = pair.get("txns") if isinstance(pair.get("txns"), Mapping) else {}
            hour = txns.get("h1") if isinstance(txns.get("h1"), Mapping) else {}
            name = _short_text(base.get("name") or "Unknown token", 160)
            symbol = _short_text(base.get("symbol"), 80)
            snippet = (
                f"Exact contract {normalized_contract}; market cap {pair.get('marketCap')}; "
                f"liquidity {liquidity.get('usd')}; 1h volume {volume.get('h1')}; "
                f"1h buys {hour.get('buys')}; 1h sells {hour.get('sells')}."
            )
            results.append({
                "title": f"{name} ({symbol}) on DexScreener" if symbol else f"{name} on DexScreener",
                "url": pair.get("url") or endpoint,
                "snippet": snippet,
                "source": "dexscreener",
            })
            info = pair.get("info") if isinstance(pair.get("info"), Mapping) else {}
            for website in info.get("websites") or []:
                if not isinstance(website, Mapping) or not _public_http_url(website.get("url")):
                    continue
                results.append({
                    "title": _short_text(website.get("label") or f"{name} website", 300),
                    "url": website.get("url"),
                    "snippet": "Website linked by the exact-contract DexScreener profile.",
                    "source": "dexscreener_profile",
                    "official_candidate": True,
                })
            for social in info.get("socials") or []:
                if not isinstance(social, Mapping) or not _public_http_url(social.get("url")):
                    continue
                results.append({
                    "title": _short_text(f"{name} {social.get('type') or 'social'}", 300),
                    "url": social.get("url"),
                    "snippet": "Social link listed by the exact-contract DexScreener profile.",
                    "source": "dexscreener_profile",
                })
            break
        cached = results
        return results

    def search(query: str, timeout: float) -> Any:
        try:
            results = load(timeout)
        except Exception:
            if fallback is None:
                raise
            return fallback(query, timeout)
        return results or (fallback(query, timeout) if fallback is not None else [])

    return search


def extract_official_contracts(page_text: Any, chain: str = "") -> list[str]:
    """Extract unique chain-shaped contract addresses from public page text."""
    text = html.unescape(str(page_text or ""))
    normalized_chain = str(chain or "").strip().lower()
    pattern = SOLANA_CONTRACT_RE if normalized_chain in {"sol", "solana"} else EVM_CONTRACT_RE
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.findall(text):
        value = match if pattern is SOLANA_CONTRACT_RE else match.lower()
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values


def assess_official_contract(expected_contract: str, page_text: Any, *, chain: str = "") -> dict[str, Any]:
    expected = _normalize_contract(chain, expected_contract)
    contracts = extract_official_contracts(page_text, chain)
    comparable = {value if str(chain).lower() in {"sol", "solana"} else value.lower() for value in contracts}
    if expected and expected in comparable:
        status = "exact_match"
    elif contracts:
        status = "mismatch"
    else:
        status = "unknown"
    if expected in comparable:
        contracts = [expected, *(item for item in contracts if item != expected)]
    return {"status": status, "contracts": contracts[:MAX_OFFICIAL_CONTRACTS]}


def is_cache_fresh(
    cache_entry: Mapping[str, Any] | None,
    *,
    now: datetime | str | None = None,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> bool:
    if not isinstance(cache_entry, Mapping) or ttl_seconds < 0:
        return False
    observed = _parse_time(
        cache_entry.get("observed_at")
        or cache_entry.get("fetched_at")
        or cache_entry.get("updated_at")
    )
    current = _parse_time(now) if now is not None else datetime.now(timezone.utc)
    if observed is None or current is None:
        return False
    age = (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    return 0 <= age <= ttl_seconds


def load_fresh_cache(
    path: Path,
    *,
    chain: str = "",
    contract: str = "",
    now: datetime | str | None = None,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not _valid_cache_record(payload, chain=chain, contract=contract):
        return None
    return payload if is_cache_fresh(payload, now=now, ttl_seconds=ttl_seconds) else None


def _valid_contract_shape(chain: str, contract: str) -> bool:
    if chain in {"sol", "solana"}:
        return SOLANA_CONTRACT_RE.fullmatch(contract) is not None
    return EVM_CONTRACT_RE.fullmatch(contract) is not None


def _valid_cache_record(payload: Mapping[str, Any], *, chain: str = "", contract: str = "") -> bool:
    payload_chain = str(payload.get("chain") or "").strip().lower()
    payload_contract = _normalize_contract(payload_chain, payload.get("contract"))
    expected_chain = str(chain or "").strip().lower()
    expected_contract = _normalize_contract(expected_chain or payload_chain, contract)
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        return False
    if not payload_chain or not _valid_contract_shape(payload_chain, payload_contract):
        return False
    if expected_chain and payload_chain != expected_chain:
        return False
    if expected_contract and payload_contract != expected_contract:
        return False
    if payload.get("status") not in VALID_RESEARCH_STATUSES:
        return False
    official = payload.get("official_contract")
    if not isinstance(payload.get("sources"), list) or not isinstance(official, Mapping):
        return False
    if set(official) != {"status", "contracts"}:
        return False
    if official.get("status") not in {"exact_match", "mismatch", "unknown"}:
        return False
    official_contracts = official.get("contracts")
    if not isinstance(official_contracts, list):
        return False
    if any(not isinstance(item, str) or not _valid_contract_shape(payload_chain, item) for item in official_contracts):
        return False
    normalized_official = {_normalize_contract(payload_chain, item) for item in official_contracts}
    if official.get("status") == "exact_match" and payload_contract not in normalized_official:
        return False
    if official.get("status") == "mismatch" and (not normalized_official or payload_contract in normalized_official):
        return False
    if official.get("status") == "unknown" and normalized_official:
        return False
    if _parse_time(payload.get("observed_at")) is None:
        return False
    for source in payload["sources"]:
        if not isinstance(source, Mapping):
            return False
        if not all(source.get(field) for field in ("source_id", "source", "url", "observed_at", "grounding")):
            return False
        if source.get("grounded") is not True or _parse_time(source.get("observed_at")) is None:
            return False
        parsed_url = urllib.parse.urlparse(str(source.get("url")))
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return False
        source_chain = str(source.get("chain") or "").strip().lower()
        source_contract = _normalize_contract(source_chain, source.get("contract"))
        if source_chain != payload_chain or source_contract != payload_contract:
            return False
    return True


def save_research_cache(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist a valid research record atomically without serializing provider config."""
    value = {**dict(payload), "schema_version": CACHE_SCHEMA_VERSION}
    if not _valid_cache_record(value):
        raise ValueError("invalid_cache_record")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _call_search_provider(provider: Any, query: str, timeout: float) -> Any:
    if callable(provider):
        return provider(query, timeout)
    search = getattr(provider, "search", None)
    if callable(search):
        return search(query=query, timeout=timeout)
    raise TypeError("provider_not_callable")


def _invoke_with_timeout(operation: Callable[[], Any], timeout: float) -> Any:
    """Bound an optional blocking adapter even when it ignores its timeout."""
    if not _ADAPTER_CALL_SLOTS.acquire(blocking=False):
        raise TimeoutError("adapter_capacity_exhausted")
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put((True, operation()))
        except Exception as exc:  # noqa: BLE001
            result_queue.put((False, exc))
        finally:
            _ADAPTER_CALL_SLOTS.release()

    thread = threading.Thread(target=run, name="alpha-token-research-call", daemon=True)
    try:
        thread.start()
    except Exception:
        _ADAPTER_CALL_SLOTS.release()
        raise
    try:
        succeeded, value = result_queue.get(timeout=max(0.001, timeout))
    except queue.Empty as exc:
        raise TimeoutError("adapter_timeout") from exc
    if succeeded:
        return value
    raise value


def research_token(
    chain: str,
    contract: str,
    *,
    symbol: str = "",
    name: str = "",
    now: datetime | str | None = None,
    provider: Any = None,
    page_fetcher: Callable[[str, float], Any] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_results: int = MAX_PROVIDER_RESULTS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run best-effort public research; failures become deterministic state."""
    normalized_chain = str(chain or "").strip().lower()
    normalized_contract = _normalize_contract(normalized_chain, contract)
    queries = build_contract_queries(normalized_chain, normalized_contract, symbol=symbol, name=name)
    timestamp = _observed_at(now)
    base: dict[str, Any] = {
        "chain": normalized_chain,
        "contract": normalized_contract,
        "observed_at": timestamp,
        "queries": queries,
        "sources": [],
        "official_contract": {"status": "unknown", "contracts": []},
    }
    if provider is None:
        return {**base, "status": "provider_unavailable", "error_code": "provider_unavailable"}

    timeout = _bounded_timeout(timeout_seconds)
    deadline = clock() + timeout
    sources: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    had_timeout = False
    had_error = False
    deadline_exhausted = False
    result_limit = max(0, min(int(max_results), MAX_PROVIDER_RESULTS))
    for query in queries:
        if len(sources) >= result_limit:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            deadline_exhausted = True
            break
        try:
            raw = _invoke_with_timeout(lambda: _call_search_provider(provider, query, remaining), remaining)
            if clock() >= deadline:
                deadline_exhausted = True
                break
            normalized = bound_provider_results(
                raw,
                query=query,
                chain=normalized_chain,
                contract=normalized_contract,
                observed_at=timestamp,
                limit=result_limit,
                include_ungrounded=page_fetcher is not None,
            )
            for item in normalized:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                if item.get("grounded"):
                    sources.append(item)
                else:
                    candidates.append(item)
        except (TimeoutError, asyncio.TimeoutError):
            had_timeout = True
            deadline_exhausted = True
            break
        except Exception:  # noqa: BLE001 - provider details must not leak into state
            had_error = True

    official_pages: list[str] = []
    for source in sources:
        if source.get("is_official"):
            official_pages.append(f"{source.get('title', '')}\n{source.get('snippet', '')}\n{source.get('url', '')}")

    fetch_candidates = candidates + [
        source for source in sources if source.get("is_official") or source.get("official_candidate")
    ]
    fetched_urls: set[str] = set()
    if page_fetcher is not None and not deadline_exhausted:
        for candidate in fetch_candidates:
            url = str(candidate.get("url") or "")
            if not url or url in fetched_urls:
                continue
            fetched_urls.add(url)
            remaining = deadline - clock()
            if remaining <= 0:
                deadline_exhausted = True
                break
            try:
                page_text = _invoke_with_timeout(lambda: page_fetcher(url, remaining), remaining)
                if clock() >= deadline:
                    deadline_exhausted = True
                    break
            except (TimeoutError, asyncio.TimeoutError):
                had_timeout = True
                deadline_exhausted = True
                break
            except Exception:  # noqa: BLE001
                had_error = True
                continue
            exact_match = _contains_exact_contract(page_text, normalized_chain, normalized_contract)
            if exact_match and not candidate.get("grounded") and len(sources) < result_limit:
                promoted = dict(candidate)
                promoted.update({
                    "grounded": True,
                    "grounding": "fetched_page_exact_contract",
                    "observed_at": timestamp,
                    "chain": normalized_chain,
                    "contract": normalized_contract,
                    "snippet": _page_excerpt(page_text) or candidate.get("snippet"),
                })
                sources.append(promoted)
            if candidate.get("is_official") or candidate.get("official_candidate"):
                official_pages.append(str(page_text or ""))

    official = assess_official_contract(normalized_contract, "\n".join(official_pages), chain=normalized_chain)
    if sources and (deadline_exhausted or had_timeout or had_error):
        status = "partial"
    elif deadline_exhausted or had_timeout:
        status = "provider_timeout"
    elif sources:
        status = "ready"
    elif had_error:
        status = "provider_error"
    else:
        status = "no_results"
    result = {**base, "status": status, "sources": sources, "official_contract": official}
    if status in {"provider_timeout", "provider_error", "partial"}:
        result["error_code"] = status if status != "partial" else "provider_partial"
    return result


async def research_token_async(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Async wrapper with an overall deadline in addition to per-call timeouts."""
    overall_timeout = _bounded_timeout(kwargs.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    try:
        return await asyncio.wait_for(asyncio.to_thread(research_token, *args, **kwargs), timeout=overall_timeout)
    except asyncio.TimeoutError:
        chain, contract = str(args[0]).lower(), _normalize_contract(str(args[0]), args[1])
        return {
            "chain": chain,
            "contract": contract,
            "observed_at": _observed_at(kwargs.get("now")),
            "queries": build_contract_queries(chain, contract, symbol=kwargs.get("symbol", ""), name=kwargs.get("name", "")),
            "sources": [],
            "official_contract": {"status": "unknown", "contracts": []},
            "status": "provider_timeout",
            "error_code": "provider_timeout",
        }


def configured_search_provider(
    environ: Mapping[str, str] | None = None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Callable[[str, float], Any] | None:
    """Create a generic JSON search provider only when its endpoint is configured."""
    env = _model_environment(environ)
    endpoint = str(env.get("TOKEN_RESEARCH_PROVIDER_URL") or "").strip()
    if not endpoint:
        enabled = str(env.get("TOKEN_RESEARCH_PUBLIC_SEARCH", "1")).strip().lower()
        return None if enabled in {"0", "false", "no", "off"} else duckduckgo_search_provider(opener=opener)
    api_key = str(env.get("TOKEN_RESEARCH_PROVIDER_API_KEY") or "").strip()

    def search(query: str, timeout: float) -> Any:
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}{urllib.parse.urlencode({'q': query})}"
        headers = {"Accept": "application/json", "User-Agent": "alpha-token-research/1"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, headers=headers)
        with opener(request, timeout=_bounded_timeout(timeout)) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ValueError("provider_response_too_large")
        return json.loads(raw)

    return search


def research_token_from_env(*args: Any, environ: Mapping[str, str] | None = None, **kwargs: Any) -> dict[str, Any]:
    if len(args) < 2:
        raise ValueError("chain_and_contract_required")
    kwargs["provider"] = dexscreener_search_provider(
        str(args[0]),
        str(args[1]),
        fallback=configured_search_provider(environ),
    )
    kwargs.setdefault("page_fetcher", public_page_fetcher())
    return research_token(*args, **kwargs)


def _is_secret_field(key: Any) -> bool:
    raw = str(key).strip()
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
    if normalized in SAFE_TOKEN_FIELDS:
        return False
    if (
        normalized in SECRET_FIELD_NAMES
        or normalized.endswith(("_api_key", "_password", "_secret", "_private_key", "_credential"))
        or normalized.endswith(("_token", "_authorization", "_signature"))
    ):
        return True
    compact = normalized.replace("_", "")
    return compact.endswith((
        "apikey",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
        "password",
        "privatekey",
        "credential",
        "authorization",
        "signature",
    ))


def _redact_url_query(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value

    def redact_pairs(raw: str) -> str:
        pairs = []
        for key, item in urllib.parse.parse_qsl(raw, keep_blank_values=True):
            pairs.append((key, "[REDACTED]" if _is_secret_field(key) else item))
        return urllib.parse.urlencode(pairs)

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    netloc = f"{hostname}{port}"
    fragment = redact_pairs(parsed.fragment) if "=" in parsed.fragment else parsed.fragment
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, redact_pairs(parsed.query), fragment))


def _redact_text(value: str) -> str:
    text = _redact_url_query(value)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    return text


def _compact_value(value: Any, *, depth: int = 0, field_name: str = "") -> Any:
    if depth >= 4:
        return None
    if _is_secret_field(field_name):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        result = {}
        for key, item in list(value.items())[:30]:
            compacted = _compact_value(item, depth=depth + 1, field_name=str(key))
            if compacted is not None:
                result[str(key)] = compacted
        return result
    if isinstance(value, (list, tuple)):
        return [item for item in (_compact_value(item, depth=depth + 1) for item in value[:8]) if item is not None]
    if isinstance(value, str):
        return _redact_text(value)[:800]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:300]


def compact_evidence_bundle(evidence: Mapping[str, Any], *, max_chars: int = MAX_EVIDENCE_CHARS) -> dict[str, Any]:
    """Create a bounded, secret-free model input from recorded evidence only."""
    max_chars = max(256, int(max_chars))
    deterministic = evidence.get("deterministic") if isinstance(evidence.get("deterministic"), Mapping) else {}
    research = evidence.get("research") if isinstance(evidence.get("research"), Mapping) else {}
    identity = evidence.get("identity") if isinstance(evidence.get("identity"), Mapping) else {}
    source_identity: Mapping[str, Any] = {}
    raw_sources = evidence.get("sources") or research.get("sources")
    if isinstance(raw_sources, list):
        source_identity = next((item for item in raw_sources if isinstance(item, Mapping)), {})
    identity_chain = str(
        identity.get("chain")
        or evidence.get("chain")
        or deterministic.get("chain")
        or research.get("chain")
        or source_identity.get("chain")
        or ""
    )
    identity = {
        "chain": identity_chain.lower(),
        "contract": _normalize_contract(
            identity_chain,
            identity.get("contract")
            or evidence.get("contract")
            or evidence.get("contract_address")
            or deterministic.get("contract_address")
            or research.get("contract")
            or source_identity.get("contract")
            or source_identity.get("contract_address"),
        ),
    }
    bundle: dict[str, Any] = {"identity": identity}
    for field in (
        "market_evidence",
        "smart_wallet_evidence",
        "official_identity",
        "official_contract",
        "same_symbol_contracts",
        "risks",
        "missing_evidence",
        "sources",
    ):
        value = evidence.get(field)
        if value is None and field in deterministic:
            value = deterministic.get(field)
        if value is None and field in research:
            value = research.get(field)
        if value is not None:
            bundle[field] = _compact_value(value)
    sources = bundle.get("sources")
    if isinstance(sources, list):
        bundle["sources"] = [source for source in sources if _source_is_grounded(source, identity)]

    def serialized_length() -> int:
        return len(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")))

    bundle["allowed_evidence_ids"] = sorted(_evidence_ids(bundle))
    while serialized_length() > max_chars:
        sources = bundle.get("sources")
        if isinstance(sources, list) and sources:
            longest = max(
                (item for item in sources if isinstance(item, dict) and item.get("snippet")),
                key=lambda item: len(str(item.get("snippet"))),
                default=None,
            )
            if longest is not None and len(str(longest["snippet"])) > 80:
                longest["snippet"] = str(longest["snippet"])[: len(str(longest["snippet"])) // 2]
                continue
            sources.pop()
            continue
        removable = next(
            (field for field in reversed(tuple(bundle)) if field not in {"identity", "allowed_evidence_ids"}),
            None,
        )
        if removable is None:
            break
        bundle.pop(removable)
    bundle["allowed_evidence_ids"] = sorted(_evidence_ids(bundle))
    return bundle


def _source_is_grounded(value: Any, identity: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping):
        return False
    source_chain = str(value.get("chain") or "").strip().lower()
    source_contract = _normalize_contract(source_chain, value.get("contract") or value.get("contract_address"))
    identity_chain = str(identity.get("chain") or "").strip().lower()
    identity_contract = _normalize_contract(identity_chain, identity.get("contract"))
    return bool(
        (value.get("source_id") or value.get("evidence_id"))
        and str(value.get("source") or "").strip()
        and _parse_time(value.get("observed_at")) is not None
        and source_chain == identity_chain
        and source_contract == identity_contract
    )


def _evidence_ids(bundle: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    identity = bundle.get("identity") if isinstance(bundle.get("identity"), Mapping) else {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            source_id = value.get("source_id") or value.get("evidence_id")
            if source_id and _source_is_grounded(value, identity):
                found.add(str(source_id))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(bundle)
    for field in ("market_evidence", "smart_wallet_evidence"):
        evidence = bundle.get(field)
        if not isinstance(evidence, Mapping):
            continue
        scope = evidence.get("scope") if isinstance(evidence.get("scope"), Mapping) else {}
        scoped = {
            "chain": scope.get("chain") or evidence.get("chain"),
            "contract": scope.get("contract_address") or evidence.get("contract_address"),
        }
        if (
            _parse_time(evidence.get("observed_at")) is not None
            and evidence.get("sources")
            and str(scoped.get("chain") or "").lower() == str(identity.get("chain") or "").lower()
            and _normalize_contract(scoped.get("chain"), scoped.get("contract"))
            == _normalize_contract(identity.get("chain"), identity.get("contract"))
        ):
            found.add(field)
    same_symbol = bundle.get("same_symbol_contracts")
    if isinstance(same_symbol, list) and any(
        isinstance(item, Mapping) and item.get("sources") and _parse_time(item.get("observed_at")) is not None
        for item in same_symbol
    ):
        found.add("same_symbol_contracts")
    if isinstance(bundle.get("missing_evidence"), list) and bundle.get("missing_evidence"):
        found.add("missing_evidence")
    return found


def validate_grounded_summary(payload: Any, evidence_ids: set[str]) -> dict[str, Any]:
    """Require Chinese, non-promotional prose with citations on every statement."""
    if not isinstance(payload, dict):
        raise ValueError("summary_not_object")
    unsupported = set(payload) - SUMMARY_FIELDS
    if unsupported:
        raise ValueError("unsupported_fields")
    missing = SUMMARY_FIELDS - set(payload)
    if missing:
        raise ValueError("missing_fields")

    def validate_statement(value: Any, field: str, limit: int, *, one_line: bool = False) -> None:
        if not isinstance(value, dict) or set(value) != STATEMENT_FIELDS:
            raise ValueError(f"invalid_{field}")
        text = value.get("text")
        citations = value.get("evidence_ids")
        if not isinstance(text, str) or not text.strip() or len(text) > limit or (one_line and "\n" in text):
            raise ValueError(f"invalid_{field}")
        if not CHINESE_RE.search(text):
            raise ValueError("chinese_required")
        if PROMOTIONAL_OR_PREDICTION_RE.search(text):
            raise ValueError("promotional_or_prediction_claim")
        if not isinstance(citations, list) or not citations or len(citations) > 12:
            raise ValueError("missing_field_evidence")
        if any(not isinstance(item, str) or item not in evidence_ids for item in citations):
            raise ValueError("unknown_evidence_id")

    validate_statement(payload["one_line_judgement"], "one_line_judgement", 180, one_line=True)
    validate_statement(payload["project_narrative"], "project_narrative", 600)
    for field, limit in (("attention_evidence", 4), ("smart_wallet_evidence", 4), ("risks", 5)):
        values = payload[field]
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"invalid_{field}")
        for value in values:
            validate_statement(value, field, 240)
    return payload


def _fallback(deterministic_summary: Any, reason: str) -> dict[str, Any]:
    return {
        "status": "fallback",
        "provider": "deterministic",
        "reason": reason,
        "summary": deterministic_summary,
    }


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def _parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("summary_not_object")
    return parsed


def summarize_grounded(
    evidence: Mapping[str, Any],
    deterministic_summary: Any,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Use an optional OpenAI-compatible JSON model, otherwise return fallback."""
    env = _model_environment(environ)
    deepseek_key = str(env.get("DEEPSEEK_API_KEY") or "").strip()
    api_key = str(env.get("TOKEN_RESEARCH_OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or deepseek_key).strip()
    model = str(
        env.get("TOKEN_RESEARCH_OPENAI_MODEL")
        or env.get("OPENAI_MODEL")
        or env.get("DEEPSEEK_MODEL")
        or ("deepseek-chat" if deepseek_key else "")
    ).strip()
    if not api_key or not model:
        return _fallback(deterministic_summary, "credentials_unavailable")

    bundle = compact_evidence_bundle(evidence)
    evidence_ids = _evidence_ids(bundle)
    if not evidence_ids:
        return _fallback(deterministic_summary, "evidence_unavailable")

    base_url = str(
        env.get("TOKEN_RESEARCH_OPENAI_BASE_URL")
        or env.get("OPENAI_BASE_URL")
        or env.get("DEEPSEEK_BASE_URL")
        or ("https://api.deepseek.com/v1" if deepseek_key else "https://api.openai.com/v1")
    ).strip()
    body: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是代币证据摘要器。只能使用用户提供的证据，必须使用中文，不得包含宣传、涨跌预测、"
                    "收益承诺、价格目标或未记录事实。只返回 JSON，顶层字段必须恰好为 "
                    "one_line_judgement、project_narrative、attention_evidence、smart_wallet_evidence、risks。"
                    "前两项必须是 {text,evidence_ids} 对象，后三项必须是该对象的数组。"
                    "每个非空陈述必须单独列出支持它的已有 evidence_ids，不得使用全局引用。"
                    "evidence_ids 只能逐字选自用户数据中的 allowed_evidence_ids；不得自行拼接、改名或新增。"
                    "one_line_judgement 和 project_narrative 的 text 不得为空；证据不足时也要明确写出"
                    "公开证据有限，并引用支持该判断的 evidence_ids，不得把第三方页面写成官方来源。"
                ),
            },
            {"role": "user", "content": json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))},
        ],
    }
    if model.startswith("openai/gpt-5"):
        body["max_completion_tokens"] = 500
        body["reasoning"] = {"effort": "minimal", "exclude": True}
    else:
        body["temperature"] = 0
        body["max_tokens"] = 700
    if _is_openrouter_url(base_url):
        require_zdr = str(env.get("TOKEN_RESEARCH_OPENROUTER_ZDR", "1")).strip().lower() not in {
            "0", "false", "no", "off",
        }
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "token_evidence_summary",
                "strict": True,
                "schema": SUMMARY_JSON_SCHEMA,
            },
        }
        body["provider"] = {
            "data_collection": "deny",
            "zdr": require_zdr,
            "allow_fallbacks": True,
            "require_parameters": True,
        }
        body["usage"] = {"include": True}
    request = urllib.request.Request(
        _chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "alpha-token-research/1",
        },
        method="POST",
    )
    try:
        timeout = _bounded_timeout(timeout_seconds)
        invoke = opener or urllib.request.urlopen
        proxy_url = str(env.get("TOKEN_RESEARCH_OPENAI_PROXY_URL") or "").strip()
        socks_proxy = opener is None and proxy_url.lower().startswith(("socks5://", "socks5h://"))
        if opener is None and proxy_url and not socks_proxy:
            proxy = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            invoke = urllib.request.build_opener(proxy).open

        def invoke_model() -> bytes:
            if socks_proxy:
                import requests

                response = requests.post(
                    request.full_url,
                    data=request.data,
                    headers=dict(request.header_items()),
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=timeout,
                )
                response.raise_for_status()
                return response.content[: MAX_PROVIDER_RESPONSE_BYTES + 1]
            with invoke(request, timeout=timeout) as response:
                return response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)

        raw = _invoke_with_timeout(invoke_model, timeout)
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            return _fallback(deterministic_summary, "model_response_too_large")
        response_payload = json.loads(raw.decode("utf-8"))
        content = response_payload["choices"][0]["message"]["content"]
        summary = validate_grounded_summary(_parse_json_content(content), evidence_ids)
    except (TimeoutError, asyncio.TimeoutError):
        return _fallback(deterministic_summary, "model_timeout")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
        return _fallback(deterministic_summary, "invalid_model_output")
    except Exception:  # noqa: BLE001 - never return provider or credential details
        return _fallback(deterministic_summary, "model_unavailable")
    return {"status": "ready", "provider": "openai_compatible", "summary": summary}


def summarize_monitor_signal(
    evidence: Mapping[str, Any],
    deterministic_summary: Any,
    *,
    environ: Mapping[str, str] | None = None,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Produce a small, low-latency AI judgement for already-selected monitor tokens."""
    env = _model_environment(environ)
    api_key = str(env.get("TOKEN_RESEARCH_OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or "").strip()
    model = str(env.get("TOKEN_RESEARCH_OPENAI_MODEL") or env.get("OPENAI_MODEL") or "").strip()
    if not api_key or not model:
        return _fallback(deterministic_summary, "credentials_unavailable")
    base_url = str(env.get("TOKEN_RESEARCH_OPENAI_BASE_URL") or env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 MEME 监控信号分析器。只依据输入数据，用简洁中文返回 JSON。"
                    "字段必须是 one_line_judgement、project_narrative、attention_evidence、"
                    "smart_wallet_evidence、risks；前两项是字符串，后三项是字符串数组。"
                    "不要预测价格、不要给买入建议；重点说明多源强度、资金、筹码和主要风险。"
                ),
            },
            {"role": "user", "content": json.dumps(compact_evidence_bundle(evidence), ensure_ascii=False, separators=(",", ":"))},
        ],
        "temperature": 0,
        "reasoning": {"effort": "none", "exclude": True},
        "max_tokens": 400,
    }
    request = urllib.request.Request(
        _chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "alpha-token-monitor/1",
        },
        method="POST",
    )
    timeout = _bounded_timeout(timeout_seconds)
    invoke = opener or urllib.request.urlopen
    proxy_url = str(env.get("TOKEN_RESEARCH_OPENAI_PROXY_URL") or "").strip()
    if opener is None and proxy_url:
        proxy = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        invoke = urllib.request.build_opener(proxy).open

    def invoke_model() -> bytes:
        with invoke(request, timeout=timeout) as response:
            return response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)

    try:
        raw = _invoke_with_timeout(invoke_model, timeout)
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            return _fallback(deterministic_summary, "model_response_too_large")
        content = json.loads(raw.decode("utf-8"))["choices"][0]["message"]["content"]
        summary = _parse_json_content(content)
        required = {
            "one_line_judgement",
            "project_narrative",
            "attention_evidence",
            "smart_wallet_evidence",
            "risks",
        }
        if set(summary) != required:
            raise ValueError("monitor_summary_fields")
        if not all(isinstance(summary[field], str) and summary[field].strip() for field in ("one_line_judgement", "project_narrative")):
            raise ValueError("monitor_summary_text")
        if not all(isinstance(summary[field], list) and all(isinstance(item, str) for item in summary[field]) for field in ("attention_evidence", "smart_wallet_evidence", "risks")):
            raise ValueError("monitor_summary_lists")
    except (TimeoutError, asyncio.TimeoutError):
        return _fallback(deterministic_summary, "model_timeout")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
        return _fallback(deterministic_summary, "invalid_model_output")
    except Exception:  # noqa: BLE001
        return _fallback(deterministic_summary, "model_unavailable")
    return {"status": "ready", "provider": "openai_compatible", "summary": summary}


summarize_with_openai = summarize_grounded
