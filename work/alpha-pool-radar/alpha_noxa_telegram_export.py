"""Read-only Noxa/Robinhood launch signal exporter from Telegram public preview."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "https://t.me/s/memmememjk"
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "noxa-launches.json"
DEFAULT_STATUS = ROOT / "outputs" / "noxa-telegram-export-status.json"
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")


def strip_tags(fragment: str) -> str:
    normalized = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    normalized = re.sub(r"</(?:div|p|span|a|code|b|strong)>", "\n", normalized, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", normalized)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def parse_money(text: str) -> float:
    cleaned = text.replace("$", "").replace(",", "").strip()
    multiplier = 1.0
    if cleaned.lower().endswith("k"):
        multiplier = 1_000.0
        cleaned = cleaned[:-1]
    elif cleaned.lower().endswith("m"):
        multiplier = 1_000_000.0
        cleaned = cleaned[:-1]
    try:
        return round(float(cleaned) * multiplier, 6)
    except ValueError:
        return 0.0


def parse_float(text: str) -> float:
    try:
        return float(text.replace(",", "").replace("%", "").strip())
    except ValueError:
        return 0.0


def parse_int(text: str) -> int:
    try:
        return int(float(text.replace(",", "").strip()))
    except ValueError:
        return 0


def parse_message(fragment: str) -> dict[str, Any] | None:
    if "fun.noxa.fi/robinhood/token" not in fragment and "dexscreener.com/robinhood" not in fragment:
        return None
    address_match = ADDRESS_RE.search(fragment)
    if not address_match:
        return None
    text = strip_tags(fragment)
    symbol = ""
    name = ""
    identity = re.search(r"(?:📌\s*)?([^\n()]{1,80}?)\s+\(\$([^)]+)\)", text)
    if identity:
        name = identity.group(1).strip(" -:\n")
        symbol = identity.group(2).strip()
    action = ""
    action_match = re.search(r"AI信号\s*\(RBH\)\s*([^\n]+)", text)
    if action_match:
        action = action_match.group(1).strip().split()[0]
    signal_time = ""
    age_text = ""
    time_match = re.search(r"信号时间:\s*([^|\n]+)(?:\|\s*币龄:\s*([^\n]+))?", text)
    if time_match:
        signal_time = time_match.group(1).strip()
        age_text = (time_match.group(2) or "").strip()
    market_cap = 0.0
    liquidity = 0.0
    money_match = re.search(r"MC:\s*\$?([0-9,.kKmM]+)\s*\|\s*流动性:\s*\$?([0-9,.kKmM]+)", text)
    if money_match:
        market_cap = parse_money(money_match.group(1))
        liquidity = parse_money(money_match.group(2))
    holders = 0
    change_1h = 0.0
    volume_1h = 0.0
    activity_match = re.search(r"Holders:\s*([0-9,]+)\s*\|\s*1h涨幅:\s*([+-]?[0-9,.]+)%\s*\|\s*1h成交:\s*\$?([0-9,.kKmM]+)", text)
    if activity_match:
        holders = parse_int(activity_match.group(1))
        change_1h = parse_float(activity_match.group(2))
        volume_1h = parse_money(activity_match.group(3))
    smart_money = 0
    smart_match = re.search(r"聪明钱包:\s*([0-9,]+)个买入", text)
    if smart_match:
        smart_money = parse_int(smart_match.group(1))
    post = ""
    post_match = re.search(r'data-post="([^"]+)"', fragment)
    if post_match:
        post = post_match.group(1)
    observed_at = ""
    time_tag = re.search(r'<time[^>]+datetime="([^"]+)"', fragment)
    if time_tag:
        observed_at = time_tag.group(1)
    address = address_match.group(0).lower()
    return {
        "chain": "robinhood",
        "chainId": "robinhood",
        "address": address,
        "tokenAddress": address,
        "contract_address": address,
        "symbol": symbol,
        "name": name,
        "marketCap": market_cap,
        "market_cap": market_cap,
        "liquidity": liquidity,
        "liquidity_usd": liquidity,
        "holders": holders,
        "holder_count": holders,
        "priceChange1h": change_1h,
        "price_change_percent1h": change_1h,
        "volume_1h": volume_1h,
        "volume": volume_1h,
        "smart_money": smart_money,
        "source_family": "noxa_launchpad",
        "source_origin": "telegram_public_preview",
        "launchpad_platform": "Noxa",
        "launchpad_lifecycle_stage": "confirmed_market" if market_cap and liquidity else "launchpad_new",
        "launchpad_stage_label": "行情确认" if market_cap and liquidity else "发射台首发",
        "noxa_url": f"https://fun.noxa.fi/robinhood/token/{address}",
        "dex_url": f"https://dexscreener.com/robinhood/{address}",
        "telegram_post": f"https://t.me/{post}" if post else "",
        "telegram_post_id": post,
        "signal_action": action,
        "signal_time_text": signal_time,
        "age_text": age_text,
        "observed_at": observed_at,
    }


def parse_public_preview(html_text: str) -> list[dict[str, Any]]:
    fragments = re.split(
        r'(?=<div class="tgme_widget_message(?:\s|"))',
        html_text,
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fragment in fragments:
        row = parse_message(fragment)
        if not row:
            continue
        key = f"{row['chain']}:{row['address']}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def fetch_text(url: str, timeout_seconds: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AlphaRadar/1.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_payload(rows: list[dict[str, Any]], out_path: Path) -> dict[str, Any]:
    payload = {
        "source": "noxa_launchpad",
        "channel": "memmememjk",
        "chain": "robinhood",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": rows,
    }
    write_json(out_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Export public Noxa/Robinhood launch signals into meme-source-inbox.")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--timeout-seconds", type=int, default=20)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    started = time.time()
    status: dict[str, Any] = {
        "source": "noxa_launchpad",
        "channel": "memmememjk",
        "ok": False,
        "out": str(args.out),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        html_text = fetch_text(args.url, args.timeout_seconds)
        rows = parse_public_preview(html_text)[: max(1, args.limit)]
        write_payload(rows, args.out)
        status.update({"ok": True, "row_count": len(rows), "elapsed_seconds": round(time.time() - started, 3)})
    except Exception as exc:  # noqa: BLE001
        status.update({"error": str(exc), "elapsed_seconds": round(time.time() - started, 3)})
    write_json(args.status, status)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
