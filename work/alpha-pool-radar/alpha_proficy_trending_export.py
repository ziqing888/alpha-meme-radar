"""Read-only Proficy Trending exporter for Meme source aggregation."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "https://www.proficy.io/trending"
TELEGRAM_PREVIEW_URL = "https://t.me/s/ProficyTrending"
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "proficy-trending.json"
DEFAULT_STATUS = ROOT / "outputs" / "proficy-trending-export-status.json"
PRICE_BOT_PREFIX = "https://t.me/ProficyPriceBot?start="
CHAIN_ALIASES = {
    "bnb chain": "bsc",
    "bsc": "bsc",
    "robinhood": "robinhood",
    "robinhood chain": "robinhood",
    "solana": "solana",
    "base": "base",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "arbitrum": "arbitrum",
    "polygon": "polygon",
    "avalanche": "avalanche",
}


def strip_tags(fragment: str) -> str:
    normalized = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    normalized = re.sub(r"</(?:td|th|div|p|span|a|button)>", "\n", normalized, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", normalized)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def parse_money(text: str) -> float:
    cleaned = html.unescape(str(text or "")).replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned == "--":
        return 0.0
    multiplier = 1.0
    suffix = cleaned[-1:].lower()
    if suffix == "k":
        multiplier = 1_000.0
        cleaned = cleaned[:-1]
    elif suffix == "m":
        multiplier = 1_000_000.0
        cleaned = cleaned[:-1]
    elif suffix == "b":
        multiplier = 1_000_000_000.0
        cleaned = cleaned[:-1]
    try:
        return round(float(cleaned) * multiplier, 6)
    except ValueError:
        return 0.0


def parse_percent(text: str) -> float:
    cleaned = html.unescape(str(text or "")).replace("%", "").replace(",", "").strip()
    if not cleaned or cleaned == "--":
        return 0.0
    try:
        return round(float(cleaned), 6)
    except ValueError:
        return 0.0


def parse_age_hours(text: str) -> float | None:
    cleaned = html.unescape(str(text or "")).strip().lower()
    if not cleaned or cleaned == "--":
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)", cleaned)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    multipliers = {
        "m": 1 / 60,
        "min": 1 / 60,
        "mins": 1 / 60,
        "minute": 1 / 60,
        "minutes": 1 / 60,
        "h": 1,
        "hr": 1,
        "hrs": 1,
        "hour": 1,
        "hours": 1,
        "d": 24,
        "day": 24,
        "days": 24,
        "w": 24 * 7,
        "wk": 24 * 7,
        "wks": 24 * 7,
        "week": 24 * 7,
        "weeks": 24 * 7,
        "mo": 24 * 30,
        "mon": 24 * 30,
        "mos": 24 * 30,
        "month": 24 * 30,
        "months": 24 * 30,
        "y": 24 * 365,
        "yr": 24 * 365,
        "yrs": 24 * 365,
        "year": 24 * 365,
        "years": 24 * 365,
    }
    multiplier = multipliers.get(unit)
    if multiplier is None:
        return None
    return round(amount * multiplier, 4)


def normalize_chain(chain: str) -> str:
    raw = re.sub(r"\s+", " ", html.unescape(str(chain or "")).strip().lower())
    return CHAIN_ALIASES.get(raw, raw.replace(" ", "-"))


def table_cells(row_fragment: str) -> list[str]:
    return re.findall(r"<td\b[^>]*>(.*?)</td>", row_fragment, flags=re.I | re.S)


def parse_row(row_fragment: str) -> dict[str, Any] | None:
    cells = table_cells(row_fragment)
    if len(cells) < 9:
        return None
    start_match = re.search(r"https://t\.me/ProficyPriceBot\?start=([^\"'&<\s]+)", cells[1])
    if not start_match:
        return None
    address = urllib.parse.unquote(html.unescape(start_match.group(1))).strip()
    symbol_match = re.search(r'font-semibold[^"]*"[^>]*>(.*?)</span>', cells[1], flags=re.I | re.S)
    name_match = re.search(r'text-\[11px\][^"]*"[^>]*>(.*?)</span>', cells[1], flags=re.I | re.S)
    rank = int(parse_money(strip_tags(cells[0])) or 0)
    symbol = strip_tags(symbol_match.group(1)) if symbol_match else strip_tags(cells[1]).splitlines()[0]
    name = strip_tags(name_match.group(1)) if name_match else symbol
    chain = normalize_chain(strip_tags(cells[2]))
    market_cap = parse_money(strip_tags(cells[3]))
    change_1h = parse_percent(strip_tags(cells[4]))
    change_24h = parse_percent(strip_tags(cells[5]))
    volume = parse_money(strip_tags(cells[6]))
    liquidity = parse_money(strip_tags(cells[7]))
    age_text = strip_tags(cells[8])
    age_hours = parse_age_hours(age_text)
    return {
        "chain": chain,
        "chainId": chain,
        "address": address,
        "tokenAddress": address,
        "contract_address": address,
        "symbol": symbol,
        "name": name,
        "marketCap": market_cap,
        "market_cap": market_cap,
        "liquidity": liquidity,
        "liquidity_usd": liquidity,
        "volume": volume,
        "volume_24h": volume,
        "volume24h": volume,
        "priceChange1h": change_1h,
        "price_change_percent1h": change_1h,
        "priceChange24h": change_24h,
        "price_change_percent24h": change_24h,
        "rank_score": max(0, 101 - rank) if rank else 0,
        "trending_score": max(0, 101 - rank) if rank else 0,
        "proficy_rank": rank,
        "proficy_url": DEFAULT_URL,
        "telegram_scan_url": f"{PRICE_BOT_PREFIX}{urllib.parse.quote(address, safe='')}",
        "pair_age_hours": age_hours,
        "age_text": age_text,
        "source_family": "proficy_trending",
        "source_origin": "proficy_public_trending",
        "heat_signal": "telegram_group_scan_trending",
    }


def parse_telegram_row(fragment: str) -> dict[str, Any] | None:
    token_match = re.search(
        r'https://t\.me/ProficyPriceBot\?start=([^"\'&<\s]+)[^>]*>\s*<b>(.*?)</b>\s*([^<]+)</a>\s*([+-]?[0-9,.]+%)',
        fragment,
        flags=re.I | re.S,
    )
    if not token_match:
        return None
    address = urllib.parse.unquote(html.unescape(token_match.group(1))).strip()
    label = strip_tags(token_match.group(2))
    chain = normalize_chain(strip_tags(token_match.group(3)))
    change_24h = parse_percent(token_match.group(4))
    label_match = re.fullmatch(r"(.*?)\s*\(([^()]*)\)\s*", label)
    name = label_match.group(1).strip() if label_match else label
    symbol = label_match.group(2).strip() if label_match else label

    plain = strip_tags(fragment)
    rank_match = re.match(r"\s*([0-9]+)\.", plain)
    metrics = re.search(
        r"MC:\s*([^|\n]+)\|\s*Liq:\s*([^|\n]+)\|\s*Vol:\s*([^|\n]+)\|\s*Age:\s*([^\n]+)",
        plain,
        flags=re.I,
    )
    if not metrics:
        return None
    rank = int(rank_match.group(1)) if rank_match else 0
    market_cap = parse_money(metrics.group(1))
    liquidity = parse_money(metrics.group(2))
    volume = parse_money(metrics.group(3))
    age_text = metrics.group(4).strip()
    return {
        "chain": chain,
        "chainId": chain,
        "address": address,
        "tokenAddress": address,
        "contract_address": address,
        "symbol": symbol,
        "name": name,
        "marketCap": market_cap,
        "market_cap": market_cap,
        "liquidity": liquidity,
        "liquidity_usd": liquidity,
        "volume": volume,
        "volume_24h": volume,
        "volume24h": volume,
        "priceChange1h": None,
        "price_change_percent1h": None,
        "priceChange24h": change_24h,
        "price_change_percent24h": change_24h,
        "rank_score": max(0, 101 - rank) if rank else 0,
        "trending_score": max(0, 101 - rank) if rank else 0,
        "proficy_rank": rank,
        "proficy_url": TELEGRAM_PREVIEW_URL,
        "telegram_scan_url": f"{PRICE_BOT_PREFIX}{urllib.parse.quote(address, safe='')}",
        "pair_age_hours": parse_age_hours(age_text),
        "age_text": age_text,
        "source_family": "proficy_trending",
        "source_origin": "proficy_public_telegram",
        "heat_signal": "telegram_group_scan_trending",
    }


def parse_telegram_preview(html_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    fragments = re.findall(
        r'<div\b[^>]*class=["\'][^"\']*\btgme_widget_message_text\b[^"\']*["\'][^>]*>(.*?)</div>',
        html_text,
        flags=re.I | re.S,
    )
    for fragment in fragments:
        row = parse_telegram_row(fragment)
        if not row:
            continue
        key = f"{row['chain']}:{row['address']}".lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def parse_trending_page(html_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fragment in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html_text, flags=re.I | re.S):
        if "ProficyPriceBot" not in fragment:
            continue
        row = parse_row(f"<tr>{fragment}</tr>")
        if not row:
            continue
        key = f"{row['chain']}:{row['address']}".lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows or parse_telegram_preview(html_text)


def fetch_text(url: str, timeout_seconds: int) -> str:
    def request_text(target: str) -> str:
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": "Mozilla/5.0 AlphaRadar/1.0",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            return response.read().decode("utf-8", errors="replace")

    try:
        return request_text(url)
    except urllib.error.HTTPError as exc:
        if exc.code != 403 or url != DEFAULT_URL:
            raise
        return request_text(TELEGRAM_PREVIEW_URL)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_payload(rows: list[dict[str, Any]], out_path: Path) -> dict[str, Any]:
    payload = {
        "source": "proficy_trending",
        "origin": "public_trending_page",
        "url": DEFAULT_URL,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": rows,
    }
    write_json(out_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Export public Proficy Trending rows into meme-source-inbox.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--timeout-seconds", type=int, default=12)
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    started = time.time()
    status: dict[str, Any] = {
        "source": "proficy_trending",
        "ok": False,
        "url": args.url,
        "out": str(args.out),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        html_text = fetch_text(args.url, args.timeout_seconds)
        rows = parse_trending_page(html_text)[: max(1, args.limit)]
        write_payload(rows, args.out)
        status.update({"ok": True, "row_count": len(rows), "elapsed_seconds": round(time.time() - started, 3)})
    except Exception as exc:  # noqa: BLE001
        status.update({"error": str(exc), "elapsed_seconds": round(time.time() - started, 3)})
    write_json(args.status, status)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
