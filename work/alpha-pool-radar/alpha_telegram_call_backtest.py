"""Backtest public Telegram meme-call channels with read-only DexScreener data."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "outputs" / "telegram-call-channel-backtest.json"
DEFAULT_MD = ROOT / "outputs" / "telegram-call-channel-backtest.md"
DEFAULT_REPORT = ROOT / "outputs" / "alpha-radar-report-latest.json"

EVM_RE = re.compile(r"(?<![a-zA-Z0-9])0x[a-fA-F0-9]{40}(?![a-zA-Z0-9])")
SOL_PUMP_RE = re.compile(r"(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44}pump)(?![A-Za-z0-9])")
SOL_RE = re.compile(r"(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{38,44})(?![A-Za-z0-9])")
GMGN_SOL_RE = re.compile(r"gmgn\.ai/sol/token/([1-9A-HJ-NP-Za-km-z]{32,44}(?:pump)?)", re.I)
GMGN_EVM_RE = re.compile(r"gmgn\.ai/(?:bsc|eth|base|robinhood)/token/(0x[a-fA-F0-9]{40})", re.I)
DEX_URL_RE = re.compile(r"dexscreener\.com/([a-z0-9_-]+)/([a-zA-Z0-9]{20,80})", re.I)
POST_RE = re.compile(r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"(.*?)</div>\s*</div>', re.S)
TIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"', re.I)


CHANNELS = {
    "inside_calls": "https://t.me/s/inside_calls",
    "EthansCrypto": "https://t.me/s/EthansCrypto",
    "Mrbigbagcalls": "https://t.me/s/Mrbigbagcalls",
    "Kingdom_X100_CALLS": "https://t.me/s/Kingdom_X100_CALLS",
    "ogamdopamine": "https://t.me/s/ogamdopamine",
    "Cabal777xbt": "https://t.me/s/Cabal777xbt",
    "SHIFTERFREECALLS": "https://t.me/s/SHIFTERFREECALLS",
    "CriptoGemas_Anuncios": "https://t.me/s/CriptoGemas_Anuncios",
    "MoonpumpWXC": "https://t.me/s/MoonpumpWXC",
    "NeoCallss": "https://t.me/s/NeoCallss",
}


@dataclass
class Call:
    channel: str
    post_url: str
    observed_at: str
    address: str
    chain_hint: str
    symbol_hint: str
    entry_market_cap: float
    text: str


def fetch_text(url: str, timeout_seconds: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AlphaRadar/1.0",
            "Accept": "text/html,application/xhtml+xml,application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def strip_tags(fragment: str) -> str:
    normalized = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    normalized = re.sub(r"</(?:div|p|span|a|code|b|strong|i)>", "\n", normalized, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", normalized)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def parse_money(raw: str) -> float:
    cleaned = html.unescape(str(raw or "")).replace("$", "").replace(",", "").strip()
    cleaned = re.sub(r"\s+", "", cleaned)
    if not cleaned:
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
        return float(cleaned) * multiplier
    except ValueError:
        return 0.0


def parse_entry_market_cap(text: str) -> float:
    patterns = [
        r"(?:market\s*cap|mcap|mc|市值)\s*[:：]?\s*\$?\s*([0-9][0-9,.]*\s*[kKmMbB]?)",
        r"(?:sub|under|around|at|entry|called|buy|aped|bought)\s*\$?\s*([0-9][0-9,.]*\s*[kKmMbB]?)\s*(?:mc|mcap|market\s*cap)",
        r"\$?\s*([0-9][0-9,.]*\s*[kKmMbB])\s*(?:mc|mcap|market\s*cap|市值)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = parse_money(match.group(1))
            if value >= 1_000:
                return value
    return 0.0


def parse_entry_market_cap_near_address(text: str, address: str) -> float:
    idx = text.lower().find(address.lower())
    if idx < 0:
        return 0.0
    after = text[idx + len(address) : idx + len(address) + 220]
    value = parse_entry_market_cap(after)
    if value:
        return value
    before = text[max(0, idx - 160) : idx]
    return parse_entry_market_cap(before)


def parse_symbol(text: str) -> str:
    match = re.search(r"\$([A-Za-z0-9_\u4e00-\u9fff]{1,24})", text)
    return match.group(1) if match else ""


def parse_symbol_near_address(text: str, address: str) -> str:
    idx = text.lower().find(address.lower())
    if idx < 0:
        return parse_symbol(text)
    before = text[max(0, idx - 180) : idx]
    lines = [line.strip(" ✅·-:") for line in before.splitlines() if line.strip(" ✅·-:")]
    blocked = {"ca", "mc", "mcap", "marketcap", "liq", "liquidity", "buy", "sell", "solana", "bsc", "bnb", "base", "robinhood"}
    for line in reversed(lines[-4:]):
        if line.isdigit():
            continue
        if line.lower() in blocked:
            continue
        if re.search(r"\b(?:mc|mcap|liq|liquidity|holders?)\b", line, flags=re.I):
            continue
        if re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{1,24}", line):
            return line
    symbol = parse_symbol(before)
    if symbol and not re.fullmatch(r"[0-9.]+[kKmMbB]?", symbol):
        return symbol
    return ""


def normalize_chain(raw: str) -> str:
    chain = (raw or "").strip().lower()
    aliases = {
        "bnb": "bsc",
        "bnbchain": "bsc",
        "bnb-chain": "bsc",
        "binance": "bsc",
        "ethereum": "ethereum",
        "eth": "ethereum",
        "sol": "solana",
        "solana": "solana",
        "base": "base",
        "robinhood": "robinhood",
    }
    return aliases.get(chain, chain)


def chain_hint_from_text(text: str, fragment: str, address: str) -> str:
    for match in DEX_URL_RE.finditer(fragment):
        token = match.group(2)
        if token.lower() == address.lower():
            return normalize_chain(match.group(1))
    idx = text.lower().find(address.lower())
    scoped = text if idx < 0 else text[max(0, idx - 500) : idx + len(address) + 120]
    last_hits: list[tuple[int, str]] = []
    for raw, chain in (("robinhood", "robinhood"), ("solana", "solana"), (" bsc", "bsc"), (" bnb", "bsc"), ("base", "base")):
        pos = scoped.lower().rfind(raw)
        if pos >= 0:
            last_hits.append((pos, chain))
    if last_hits:
        return max(last_hits)[1]
    lower = text.lower()
    if "robinhood" in lower:
        return "robinhood"
    if "solana" in lower or " raydium" in lower or address.endswith("pump"):
        return "solana"
    if "bsc" in lower or "bnb" in lower or "pancake" in lower:
        return "bsc"
    if "base" in lower:
        return "base"
    return ""


def extract_addresses(fragment: str, text: str) -> list[str]:
    candidates: list[tuple[int, str]] = []
    candidates.extend((match.start(1), match.group(1)) for match in GMGN_SOL_RE.finditer(fragment))
    candidates.extend((match.start(1), match.group(1)) for match in GMGN_EVM_RE.finditer(fragment))
    candidates.extend((match.start(0), match.group(0)) for match in EVM_RE.finditer(text))
    candidates.extend((match.start(1), match.group(1)) for match in SOL_PUMP_RE.finditer(text))
    if re.search(r"\b(sol|solana|raydium|pumpfun|pump\.fun)\b", text, flags=re.I):
        candidates.extend((match.start(1), match.group(1)) for match in SOL_RE.finditer(text))
    seen: set[str] = set()
    result: list[str] = []
    for _, address in sorted(candidates, key=lambda item: item[0]):
        normalized = address.strip().strip("`.,:;)")
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def parse_telegram_page(channel: str, html_text: str) -> list[Call]:
    calls: list[Call] = []
    for post_id, fragment in POST_RE.findall(html_text):
        text = strip_tags(fragment)
        if not text:
            continue
        time_match = TIME_RE.search(fragment)
        observed_at = time_match.group(1) if time_match else ""
        post_url = f"https://t.me/{post_id}"
        addresses = extract_addresses(fragment, text)
        global_entry_market_cap = parse_entry_market_cap(text) if len(addresses) == 1 else 0.0
        global_symbol = parse_symbol(text) if len(addresses) == 1 else ""
        for address in addresses:
            entry_market_cap = parse_entry_market_cap_near_address(text, address) or global_entry_market_cap
            symbol = parse_symbol_near_address(text, address) or global_symbol
            calls.append(
                Call(
                    channel=channel,
                    post_url=post_url,
                    observed_at=observed_at,
                    address=address,
                    chain_hint=chain_hint_from_text(text, fragment, address),
                    symbol_hint=symbol,
                    entry_market_cap=entry_market_cap,
                    text=text[:1200],
                )
            )
    return dedupe_calls(calls)


def dedupe_calls(calls: list[Call]) -> list[Call]:
    seen: set[tuple[str, str]] = set()
    result: list[Call] = []
    for call in calls:
        key = (call.channel, call.address.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(call)
    return result


def best_pair_for_call(call: Call, pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pairs:
        return None
    hint = normalize_chain(call.chain_hint)
    filtered = [pair for pair in pairs if not hint or str(pair.get("chainId", "")).lower() == hint]
    if not filtered:
        filtered = pairs
    return max(filtered, key=lambda p: float(((p.get("liquidity") or {}).get("usd") or 0) or 0))


def fetch_dex_pairs(address: str, timeout_seconds: int = 12) -> list[dict[str, Any]]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{urllib.parse.quote(address, safe='')}"
    payload = json.loads(fetch_text(url, timeout_seconds))
    return list(payload.get("pairs") or [])


def load_local_quote_map(report_path: Path) -> dict[str, dict[str, Any]]:
    if not report_path.exists():
        return {}
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows: list[dict[str, Any]] = []
    for key in ("meme_potential_rows", "meme_rows", "meme_shadow_rows", "alpha_rows", "dealer_rows"):
        rows.extend([row for row in payload.get(key) or [] if isinstance(row, dict)])
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        address = str(row.get("contract_address") or row.get("token_address") or row.get("address") or "").lower()
        if not address:
            continue
        current_mcap = float(row.get("market_cap") or row.get("mcap") or row.get("marketCap") or row.get("fdv") or 0)
        liquidity = float(row.get("liquidity") or row.get("liquidity_usd") or row.get("dex_liquidity") or 0)
        quote = {
            "chainId": row.get("chain") or row.get("chain_id") or row.get("chainId") or "",
            "marketCap": current_mcap,
            "fdv": float(row.get("fdv") or current_mcap or 0),
            "liquidity": {"usd": liquidity},
            "volume": {"h24": float(row.get("volume24h") or row.get("volume_24h") or row.get("dex_volume24h") or 0)},
            "baseToken": {"symbol": row.get("symbol") or ""},
            "url": row.get("dex_url") or "",
            "pairCreatedAt": None,
        }
        existing = result.get(address)
        if not existing or liquidity > float(((existing.get("liquidity") or {}).get("usd") or 0) or 0):
            result[address] = quote
    return result


def score_call(call: Call, pair: dict[str, Any] | None) -> dict[str, Any]:
    current_mcap = 0.0
    liquidity = 0.0
    volume_24h = 0.0
    chain = call.chain_hint
    symbol = call.symbol_hint
    pair_url = ""
    age_hours = None
    if pair:
        current_mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
        liquidity = float(((pair.get("liquidity") or {}).get("usd") or 0) or 0)
        volume_24h = float(((pair.get("volume") or {}).get("h24") or 0) or 0)
        chain = str(pair.get("chainId") or chain)
        symbol = str(((pair.get("baseToken") or {}).get("symbol") or symbol))
        pair_url = str(pair.get("url") or "")
        created = pair.get("pairCreatedAt")
        if created:
            age_hours = round((time.time() - float(created) / 1000) / 3600, 3)
    multiple = current_mcap / call.entry_market_cap if current_mcap > 0 and call.entry_market_cap > 0 else 0.0
    return {
        "channel": call.channel,
        "symbol": symbol,
        "chain": normalize_chain(chain),
        "address": call.address,
        "post_url": call.post_url,
        "observed_at": call.observed_at,
        "entry_market_cap": round(call.entry_market_cap, 4),
        "current_market_cap": round(current_mcap, 4),
        "current_multiple": round(multiple, 4),
        "liquidity": round(liquidity, 4),
        "volume_24h": round(volume_24h, 4),
        "pair_age_hours": age_hours,
        "dex_url": pair_url,
        "backtestable": call.entry_market_cap > 0 and current_mcap > 0,
        "hit_2x": multiple >= 2.0,
        "hit_5x": multiple >= 5.0,
        "dead_or_unpriced": current_mcap <= 0 or liquidity <= 0,
        "text_sample": call.text,
    }


def channel_summary(channel: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    scoped = [row for row in rows if row["channel"] == channel]
    tested = [row for row in scoped if row["backtestable"]]
    if not tested:
        return {
            "channel": channel,
            "calls": len(scoped),
            "tested": 0,
            "win_2x_pct": 0.0,
            "win_5x_pct": 0.0,
            "avg_multiple": 0.0,
            "median_multiple": 0.0,
            "dead_rate_pct": round(100 * sum(1 for row in scoped if row["dead_or_unpriced"]) / max(1, len(scoped)), 2),
            "grade": "no_data",
        }
    multiples = sorted(float(row["current_multiple"]) for row in tested)
    median = multiples[len(multiples) // 2] if len(multiples) % 2 else (multiples[len(multiples) // 2 - 1] + multiples[len(multiples) // 2]) / 2
    win_2x = sum(1 for row in tested if row["hit_2x"]) / len(tested)
    win_5x = sum(1 for row in tested if row["hit_5x"]) / len(tested)
    avg = sum(multiples) / len(multiples)
    dead = sum(1 for row in scoped if row["dead_or_unpriced"]) / max(1, len(scoped))
    grade = "watch"
    if len(tested) >= 3 and win_2x >= 0.35 and avg >= 1.4 and dead <= 0.55:
        grade = "candidate"
    if len(tested) >= 4 and win_2x >= 0.5 and avg >= 2.0 and dead <= 0.4:
        grade = "priority"
    if len(scoped) >= 3 and (dead > 0.75 or (len(tested) >= 3 and win_2x < 0.15)):
        grade = "reject"
    return {
        "channel": channel,
        "calls": len(scoped),
        "tested": len(tested),
        "win_2x_pct": round(win_2x * 100, 2),
        "win_5x_pct": round(win_5x * 100, 2),
        "avg_multiple": round(avg, 4),
        "median_multiple": round(median, 4),
        "dead_rate_pct": round(dead * 100, 2),
        "grade": grade,
    }


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Telegram Meme Call Channel Backtest",
        "",
        f"- Updated: {payload['updated_at']}",
        f"- Channels scanned: {len(payload['channels'])}",
        f"- Calls found: {len(payload['calls'])}",
        f"- Backtestable calls: {sum(row['tested'] for row in payload['summary'])}",
        "",
        "## Channel Ranking",
        "",
        "| Grade | Channel | Calls | Tested | 2x Win | 5x Win | Avg x | Median x | Dead |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    order = {"priority": 0, "candidate": 1, "watch": 2, "no_data": 3, "reject": 4}
    for row in sorted(payload["summary"], key=lambda r: (order.get(r["grade"], 9), -r["tested"], -r["avg_multiple"])):
        lines.append(
            f"| {row['grade']} | {row['channel']} | {row['calls']} | {row['tested']} | "
            f"{row['win_2x_pct']}% | {row['win_5x_pct']}% | {row['avg_multiple']} | "
            f"{row['median_multiple']} | {row['dead_rate_pct']}% |"
        )
    lines.extend(["", "## Best Tested Calls", ""])
    best = sorted([row for row in payload["calls"] if row["backtestable"]], key=lambda r: r["current_multiple"], reverse=True)[:20]
    for row in best:
        lines.append(
            f"- {row['channel']} {row['symbol']} {row['chain']} {row['current_multiple']}x "
            f"entry ${row['entry_market_cap']:,.0f} -> current ${row['current_market_cap']:,.0f} "
            f"{row['post_url']}"
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    channels = dict(CHANNELS)
    for spec in args.channel:
        if "=" in spec:
            name, url = spec.split("=", 1)
            channels[name.strip()] = url.strip()
        else:
            name = spec.strip().rstrip("/").split("/")[-1]
            channels[name] = spec.strip()
    calls: list[Call] = []
    errors: list[str] = []
    for channel, url in channels.items():
        try:
            calls.extend(parse_telegram_page(channel, fetch_text(url, args.timeout_seconds)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{channel}: {exc}")
    rows: list[dict[str, Any]] = []
    pair_cache: dict[str, list[dict[str, Any]]] = {}
    local_quotes = load_local_quote_map(args.report)
    for call in calls[: args.max_calls]:
        key = call.address.lower()
        if key in local_quotes:
            rows.append(score_call(call, local_quotes[key]))
            continue
        if key not in pair_cache:
            try:
                pair_cache[key] = fetch_dex_pairs(call.address, args.timeout_seconds)
                time.sleep(args.sleep_seconds)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"{call.channel} {call.address}: {exc}")
                pair_cache[key] = []
        rows.append(score_call(call, best_pair_for_call(call, pair_cache[key])))
    summary = [channel_summary(channel, rows) for channel in channels]
    payload = {
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "method": "public t.me/s pages; entry market cap parsed from call text; current market cap prefers local Alpha Radar report, then DexScreener latest token endpoint",
        "local_quote_count": len(local_quotes),
        "channels": channels,
        "summary": summary,
        "calls": rows,
        "errors": errors,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(markdown_report(payload), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest public Telegram meme-call channels.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--max-calls", type=int, default=160)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    parser.add_argument("--channel", action="append", default=[], help="Extra channel URL or name=url")
    args = parser.parse_args()
    payload = run(args)
    print(json.dumps({"ok": True, "channels": len(payload["channels"]), "calls": len(payload["calls"]), "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
