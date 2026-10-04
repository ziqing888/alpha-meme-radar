"""Read-only Tingfeng/Wind public feed exporter for Meme source aggregation."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "https://wind.jokkimon.club"
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "wind-monitor.json"
DEFAULT_STATUS = ROOT / "outputs" / "wind-monitor-export-status.json"
DEFAULT_HANDLES = "fomo:unipcs,fomo:traderpow,fomo:ed_x0101,pumpfun:0xsun,pumpfun:traderpow,pumpfun:hexiecs"
DEFAULT_FEED_ACTIONS = "tweet,quote,repost,reply,pump_callout,pump_update,pump_buy,pump_sell"
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
SOLANA_RE = re.compile(r"(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
CHAIN_ALIASES = {
    "56": "bsc",
    "bnb": "bsc",
    "bnb chain": "bsc",
    "bsc": "bsc",
    "binance smart chain": "bsc",
    "sol": "solana",
    "solana": "solana",
    "4663": "robinhood",
    "robinhood": "robinhood",
    "robinhood chain": "robinhood",
    "8453": "base",
    "base": "base",
    "1": "ethereum",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "evm": "",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_chain(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return CHAIN_ALIASES.get(raw, raw.replace(" ", "-"))


def first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def json_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            values.extend(json_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(json_values(item))
    return values


def endpoint_url(base_url: str, path: str, params: dict[str, Any]) -> str:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}?{query}"


def fetch_json(url: str, timeout_seconds: int) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AlphaRadar/1.0",
            "Accept": "application/json",
            "Referer": f"{DEFAULT_BASE_URL}/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8", errors="replace"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def event_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return [event for event in payload["events"] if isinstance(event, dict)]
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    return []


def default_handles_from_catalog(base_url: str, max_handles: int, timeout_seconds: int) -> list[str]:
    payload = fetch_json(f"{base_url.rstrip('/')}/api/catalog", timeout_seconds)
    accounts = payload.get("accounts") if isinstance(payload, dict) and isinstance(payload.get("accounts"), list) else []
    preferred = {"fomo", "pumpfun", "telegram", "binance_square"}
    handles: list[str] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        platform = str(account.get("platform") or "").strip().lower()
        handle = str(account.get("handle") or "").strip().lower()
        if not platform or not handle or platform not in preferred:
            continue
        pair = f"{platform}:{handle}"
        if pair not in handles:
            handles.append(pair)
        if len(handles) >= max_handles:
            break
    return handles


def configured_handles(base_url: str, max_handles: int, timeout_seconds: int, use_catalog: bool) -> list[str]:
    raw = os.environ.get("WIND_MONITOR_HANDLES") or os.environ.get("TINGFENG_MONITOR_HANDLES") or DEFAULT_HANDLES
    handles = [item.strip().lower() for item in raw.split(",") if item.strip()]
    handles = [item if ":" in item else f"fomo:{item}" for item in handles]
    if handles:
        return handles[:max_handles]
    if use_catalog:
        return default_handles_from_catalog(base_url, max_handles, timeout_seconds)
    return []


def candidate_ca_items(event: dict[str, Any]) -> list[dict[str, str]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    reference = payload.get("reference") if isinstance(payload.get("reference"), dict) else {}
    pools: list[Any] = [
        event.get("ca_info"),
        payload.get("ca_info"),
        payload.get("reference_bio_ca"),
        reference.get("bio_ca"),
        extra.get("ca_info"),
    ]
    pump = extra.get("pump") if isinstance(extra.get("pump"), dict) else {}
    fomo = extra.get("fomo") if isinstance(extra.get("fomo"), dict) else {}
    for obj in (payload, extra, pump, fomo):
        if isinstance(obj, dict):
            pools.append(
                {
                    "address": first_present(obj, "ca", "address", "tokenAddress", "token_address", "contract", "mint"),
                    "chain": first_present(obj, "chain", "chain_slug", "chainName", "network", "networkId", "chainId"),
                }
            )
    found: list[dict[str, str]] = []
    for pool in pools:
        items = pool if isinstance(pool, list) else [pool]
        for item in items:
            if not isinstance(item, dict):
                continue
            address = str(first_present(item, "address", "ca", "tokenAddress", "token_address", "contract", "mint") or "").strip()
            if not address:
                continue
            chain = normalize_chain(first_present(item, "resolved_chain", "chain_slug", "chain", "chainName", "network", "networkId", "chainId"))
            found.append({"address": address, "chain": chain})
    return found


def _joined_text(*values: Any) -> str:
    return " ".join(str(value).strip() for value in values if str(value or "").strip())


def original_post_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return _joined_text(
        event.get("search_text"),
        payload.get("content_text"),
        payload.get("translation"),
    )


def referenced_post_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    reference = payload.get("reference") if isinstance(payload.get("reference"), dict) else {}
    return _joined_text(
        payload.get("reference_text"),
        payload.get("reference_translation"),
        reference.get("content_text"),
        reference.get("translation"),
    )


def event_text(event: dict[str, Any]) -> str:
    return _joined_text(original_post_text(event), referenced_post_text(event))


def author_metadata(event: dict[str, Any], payload: dict[str, Any], *, handle: str, platform: str) -> dict[str, Any]:
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    return {
        "id": first_present(author, "id", "user_id") or first_present(payload, "author_id", "user_id"),
        "handle": handle or first_present(author, "handle", "username") or "",
        "name": first_present(author, "name", "display_name") or first_present(payload, "author_name", "display_name"),
        "followers": first_present(author, "followers", "followers_count") or first_present(payload, "author_followers", "followers_count"),
        "verified": first_present(author, "verified", "is_verified"),
        "platform": platform,
    }


def referenced_url(payload: dict[str, Any]) -> str:
    reference = payload.get("reference") if isinstance(payload.get("reference"), dict) else {}
    return str(
        first_present(payload, "referenced_url", "reference_url", "quoted_url")
        or first_present(reference, "tweet_url", "url", "source_url")
        or ""
    )


def infer_chain_from_text(text: str, address: str) -> str | None:
    lowered = text.lower()
    patterns = (
        (r"\b(?:bsc|bnb\s*chain|binance\s*smart\s*chain)\b", "bsc"),
        (r"\brobinhood(?:\s*chain)?\b", "robinhood"),
        (r"\bbase(?:\s*chain)?\b", "base"),
        (r"\b(?:ethereum|eth)\b", "ethereum"),
        (r"\b(?:solana|sol|pump\.fun|pumpfun|pump)\b", "solana"),
    )
    for pattern, chain in patterns:
        if re.search(pattern, lowered):
            return chain
    return None if address.lower().startswith("0x") else "solana"


def statement_scopes(text: str) -> list[dict[str, Any]]:
    scopes: list[dict[str, Any]] = []
    for raw_statement in re.split(r"[,，;\n\r!?。！？]+", text):
        statement = raw_statement.strip()
        if not statement:
            continue
        chain_markers: list[tuple[int, int, str]] = []
        patterns = (
            (r"\b(?:bsc|bnb\s*chain|binance\s*smart\s*chain)\b", "bsc"),
            (r"\brobinhood(?:\s*chain)?\b", "robinhood"),
            (r"\bbase(?:\s*chain)?\b", "base"),
            (r"\b(?:ethereum|eth)\b", "ethereum"),
            (r"\b(?:solana|sol|pump\.fun|pumpfun|pump)\b", "solana"),
        )
        for pattern, chain in patterns:
            chain_markers.extend(
                (match.start(), match.end(), chain)
                for match in re.finditer(pattern, statement, re.IGNORECASE)
            )
        chain_markers.sort()
        address_matches = list(EVM_RE.finditer(statement))
        if any(marker[2] == "solana" for marker in chain_markers):
            address_matches.extend(SOLANA_RE.finditer(statement))
        address_matches.sort(key=lambda match: match.start())
        suffix_labeled = bool(
            chain_markers
            and address_matches
            and address_matches[0].start() < chain_markers[0][0]
        )
        for index, match in enumerate(address_matches):
            address = match.group(1) if match.lastindex else match.group(0)
            previous_end = address_matches[index - 1].end() if index else 0
            next_start = address_matches[index + 1].start() if index + 1 < len(address_matches) else len(statement)
            preceding = [
                marker
                for marker in chain_markers
                if previous_end <= marker[0] and marker[1] <= match.start()
            ]
            following = [
                marker
                for marker in chain_markers
                if match.end() <= marker[0] and marker[1] <= next_start
            ]
            candidates = following if suffix_labeled else preceding
            candidate_chains = {marker[2] for marker in candidates}
            chain = next(iter(candidate_chains)) if len(candidate_chains) == 1 else None
            if not address.lower().startswith("0x") and chain is None:
                chain = "solana"
            scopes.append({"address": address, "chain": chain, "text": statement})
    return scopes


def text_contracts(text: str) -> list[dict[str, Any]]:
    return [
        {"address": scope["address"], "chain": scope["chain"]}
        for scope in statement_scopes(text)
    ]


def scoped_text(scopes: list[dict[str, Any]], chain: str | None, address: str) -> str | None:
    matches: list[str] = []
    for scope in scopes:
        if scope["chain"] != chain or scope["address"].lower() != address.lower():
            continue
        if scope["text"] not in matches:
            matches.append(scope["text"])
    return " ".join(matches) or None


def extract_contracts(event: dict[str, Any]) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = [
        *candidate_ca_items(event),
        *text_contracts(original_post_text(event)),
        *text_contracts(referenced_post_text(event)),
    ]
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in contracts:
        address = str(item.get("address") or "").strip()
        chain = normalize_chain(item.get("chain")) or None
        if not address:
            continue
        key = f"{chain}:{address}".lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"chain": chain, "address": address})
    return deduped


def symbol_from_event(event: dict[str, Any], address: str) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    for obj in (payload, extra, extra.get("pump"), extra.get("fomo"), event):
        if not isinstance(obj, dict):
            continue
        value = first_present(obj, "symbol", "token_symbol", "ticker", "coinSymbol", "name")
        if value:
            return str(value).strip().replace("$", "")[:32]
    text = " ".join(json_values(event))
    cashtag = re.search(r"\$([A-Za-z][A-Za-z0-9_]{1,20})", text)
    if cashtag:
        return cashtag.group(1)
    return address[:6]


def normalize_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    pump = extra.get("pump") if isinstance(extra.get("pump"), dict) else {}
    fomo = extra.get("fomo") if isinstance(extra.get("fomo"), dict) else {}
    platform = str(event.get("platform") or payload.get("platform") or payload.get("platform_name") or "").strip().lower()
    action = str(event.get("action") or payload.get("action") or "").strip().lower()
    handle = str(event.get("handle") or payload.get("author_handle") or payload.get("handle") or "").strip().lower()
    contracts = extract_contracts(event)
    original_text = original_post_text(event) or None
    reference_text = referenced_post_text(event) or None
    original_scopes = statement_scopes(original_text or "")
    reference_scopes = statement_scopes(reference_text or "")
    original_url = str(payload.get("tweet_url") or payload.get("url") or event.get("url") or "")
    reference_url = referenced_url(payload)
    provider_event_id = event.get("id") or event.get("key") or event.get("seq") or ""
    event_at = payload.get("created_at") or event.get("created_at") or event.get("ts") or None
    observed_at = now_iso()
    extracted_contracts = [item["address"] for item in contracts]
    extracted_contract_details = [
        {"chain": item["chain"], "address": item["address"]}
        for item in contracts
    ]
    author = author_metadata(event, payload, handle=handle, platform=platform)
    rows: list[dict[str, Any]] = []
    for contract in contracts:
        address = contract["address"]
        chain = contract["chain"]
        row_original_text = scoped_text(original_scopes, chain, address)
        row_reference_text = scoped_text(reference_scopes, chain, address)
        post_text = _joined_text(row_original_text, row_reference_text) or None
        row_source_url = (
            original_url if row_original_text
            else reference_url if row_reference_text
            else original_url or reference_url
        )
        symbol = symbol_from_event(event, address)
        amount_usd = to_float(first_present(fomo, "amount_usd", "usd", "netAmountUsd") or first_present(pump, "amount_usd", "usd"))
        market_cap = to_float(
            first_present(fomo, "market_cap", "marketCap", "mcap", "mc")
            or first_present(pump, "market_cap", "marketCap", "mcap", "mc")
        )
        score = 34.0
        if action in {"fomo_buy", "pump_buy"}:
            score += 16
        if action in {"pump_callout", "tweet", "quote"}:
            score += 10
        if amount_usd is not None and amount_usd > 0:
            score += min(18.0, amount_usd / 500)
        rows.append(
            {
                "chain": chain,
                "chainId": chain,
                "address": address,
                "tokenAddress": address,
                "contract_address": address,
                "symbol": symbol,
                "name": str(first_present(pump, "name") or first_present(fomo, "name") or symbol),
                "marketCap": market_cap,
                "market_cap": market_cap,
                "volume": amount_usd,
                "volume24h": amount_usd,
                "rank_score": round(min(100.0, score), 2),
                "market_data_pending": market_cap is None,
                "source_family": "wind_monitor",
                "source_origin": "wind_public_api",
                "provider_family": "wind",
                "provider_feed": "wind_monitor",
                "provider_event_id": provider_event_id,
                "event_at": event_at,
                "source_url": row_source_url or None,
                "provenance_status": "known" if event_at else "unknown",
                "wind_action": action,
                "wind_platform": platform,
                "wind_handle": handle,
                "wind_seq": event.get("seq"),
                "wind_event_id": event.get("id") or event.get("key") or "",
                "wind_url": original_url,
                "wind_post_text": post_text,
                "wind_original_post_text": row_original_text,
                "wind_referenced_post_text": row_reference_text,
                "wind_author": author,
                "wind_original_url": original_url or None,
                "wind_referenced_url": reference_url or None,
                "wind_extracted_contracts": extracted_contracts,
                "wind_extracted_contract_details": extracted_contract_details,
                "wind_extracted_identity": {"chain": chain, "address": address},
                "wind_extracted_ca": address,
                "wind_attention_only": post_text is None,
                "observed_at": observed_at,
                "identity_status": "bound" if chain else "unbound",
                "heat_signal": "tingfeng_social_ca",
                "smart_money": 1 if action in {"fomo_buy", "pump_buy"} else 0,
                "kol": 1 if handle else 0,
            }
        )
    return rows


def collect_rows(base_url: str, handles: list[str], limit: int, timeout_seconds: int, feed_actions: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not handles:
        return [], {"handles": 0, "events": 0, "feed_error": "no_handles"}
    params = {"handles": ",".join(handles), "limit": max(1, limit)}
    activity_payload = fetch_json(endpoint_url(base_url, "/api/activities", params), timeout_seconds)
    events = event_list(activity_payload)
    feed_error = ""
    if feed_actions:
        try:
            feed_payload = fetch_json(endpoint_url(base_url, "/api/feed", {**params, "actions": feed_actions}), timeout_seconds)
            events.extend(event_list(feed_payload))
        except urllib.error.HTTPError as exc:
            feed_error = f"http_{exc.code}"
        except Exception as exc:  # noqa: BLE001
            feed_error = str(exc)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        for row in normalize_event(event):
            key = f"{row['chain']}:{row['address']}:{row.get('wind_event_id') or row.get('wind_seq')}".lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows, {"handles": len(handles), "events": len(events), "rows": len(rows), "feed_error": feed_error}


def write_payload(rows: list[dict[str, Any]], out_path: Path, *, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    payload = {
        "source": "wind_monitor",
        "origin": "wind_public_api",
        "base_url": base_url,
        "fetched_at": now_iso(),
        "data": rows,
    }
    write_json(out_path, payload)
    return payload


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    status: dict[str, Any] = {
        "source": "wind_monitor",
        "ok": False,
        "base_url": args.base_url,
        "out": str(args.out),
        "checked_at": now_iso(),
    }
    try:
        handles = configured_handles(args.base_url, max(1, args.max_handles), max(3, args.timeout_seconds), bool(args.use_catalog))
        rows, counts = collect_rows(args.base_url, handles, max(1, args.limit), max(3, args.timeout_seconds), args.feed_actions)
        if rows or args.overwrite_empty:
            write_payload(rows, args.out, base_url=args.base_url)
        status.update({"ok": True, "row_count": len(rows), "event_counts": counts, "handles": handles})
        if counts.get("feed_error"):
            status["feed_error"] = counts["feed_error"]
    except Exception as exc:  # noqa: BLE001
        status.update({"error": str(exc)})
    status["elapsed_seconds"] = round(time.time() - started, 3)
    write_json(args.status, status)
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Tingfeng/Wind CA events into meme-source-inbox.")
    parser.add_argument("--base-url", default=os.environ.get("WIND_MONITOR_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("WIND_MONITOR_TIMEOUT_SECONDS", "10")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("WIND_MONITOR_LIMIT", "40")))
    parser.add_argument("--max-handles", type=int, default=int(os.environ.get("WIND_MONITOR_MAX_HANDLES", "12")))
    parser.add_argument("--feed-actions", default=os.environ.get("WIND_MONITOR_FEED_ACTIONS", DEFAULT_FEED_ACTIONS))
    parser.add_argument("--use-catalog", action="store_true", default=bool(os.environ.get("WIND_MONITOR_USE_CATALOG_DEFAULT", "").strip()))
    parser.add_argument("--overwrite-empty", action="store_true")
    return parser.parse_args()


def main() -> int:
    status = run_once(parse_args())
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
