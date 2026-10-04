"""Read-only DeBot signal exporter for the Meme second-screen inbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("ALPHA_MEME_CHAINS", "*")

import alpha_meme

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URLS = (
    "https://debot.ai/",
    "https://debot.ai/deFiBot/strategy",
    "https://debot.ai/discover",
)
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "debot-signals.json"
DEFAULT_STATUS = ROOT / "outputs" / "debot-signal-export-status.json"
SIGNAL_URL_HINTS = ("api", "signal", "ai", "token", "meme", "trend", "discover", "strategy")
TOKEN_HREF_RE = re.compile(r"/token/([^/?#]+)/([^/?#]+)")
COMPACT_NUMBER_RE = re.compile(r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*([KMB])?", re.IGNORECASE)
PERCENT_RE = re.compile(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*%")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def csv_env(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def emit_json(payload: Any) -> None:
    if sys.stdout is None:
        return
    print(json.dumps(payload, ensure_ascii=False))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def debot_feed(origin: str) -> tuple[str, str, str]:
    lowered = str(origin or "").lower().replace("-", "_")
    if "safe_info" in lowered or "security" in lowered or "audit" in lowered:
        return "debot_safe_info", "audit", "security_audit"
    if "rank" in lowered or "trending" in lowered:
        return "debot_rank", "ranking", "trending"
    return "debot_signal", "market", "market_flow"


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def optional_int(value: Any) -> int | None:
    number = optional_float(value)
    return int(number) if number is not None else None


def fallback_provider_event_id(row: dict[str, Any], feed: str) -> str:
    transient = {
        "observed_at",
        "fetched_at",
        "stale_at",
        "provenance_status",
        "event_time_status",
        "stale",
    }
    material = {
        "feed": feed,
        "row": {key: value for key, value in row.items() if key not in transient},
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return f"fallback:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def add_provider_metadata(
    row: dict[str, Any],
    origin: str,
    observed_at: str,
) -> dict[str, Any]:
    feed, role, lane = debot_feed(origin)
    event_at = next(
        (
            row[key]
            for key in ("event_at", "updated_at", "created_at", "createdAt", "timestamp", "time")
            if row.get(key) not in (None, "")
        ),
        None,
    )
    event_id = next(
        (
            row[key]
            for key in ("provider_event_id", "event_id", "id", "key", "tx_hash", "txHash")
            if row.get(key) not in (None, "")
        ),
        None,
    )
    if event_id is None:
        event_id = fallback_provider_event_id(row, feed)
    supplied_status = str(row.get("provenance_status") or row.get("status") or "").lower()
    if row.get("stale") is True or supplied_status == "stale":
        status = "stale"
    elif event_at is None:
        status = "unknown"
    else:
        status = "known"
    if status == "stale":
        evidence_observed_at = row.get("observed_at") or row.get("fetched_at") or event_at
    else:
        evidence_observed_at = observed_at
    row.update(
        {
            "source_family": feed,
            "source_origin": origin[:180],
            "provider_family": "debot",
            "provider_feed": feed,
            "provider_event_id": str(event_id),
            "event_at": event_at,
            "event_time_status": "known" if event_at is not None else "unknown",
            "observed_at": evidence_observed_at,
            "source_url": origin[:180],
            "evidence_role": role,
            "signal_lane": lane,
            "provenance_status": status,
        }
    )
    return row


def normalize_debot_payload(
    payload: Any,
    origin: str,
    max_rows: int,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    observed = observed_at or utc_now_iso()
    for row in alpha_meme.nested_rows(payload):
        flattened = add_provider_metadata(flatten_debot_row(row), origin, observed)
        if not alpha_meme.normalize_external_meme_row(flattened, "debot_signal"):
            continue
        rows.append(flattened)
        if len(rows) >= max_rows:
            break
    return rows


def percent_value(value: Any) -> float | None:
    numeric = optional_float(value)
    if numeric is None:
        return None
    if -3 <= numeric <= 3:
        return numeric * 100
    return numeric


def flatten_debot_row(row: dict[str, Any]) -> dict[str, Any]:
    flattened = dict(row)
    market_info = row.get("market_info") if isinstance(row.get("market_info"), dict) else {}
    pair_info = row.get("pair_summary_info") if isinstance(row.get("pair_summary_info"), dict) else {}
    if market_info:
        flattened.setdefault("marketCap", optional_float(market_info.get("mkt_cap") or market_info.get("fdv")))
        flattened.setdefault("fdv", optional_float(market_info.get("fdv")))
        flattened.setdefault("liquidityUsd", optional_float(pair_info.get("liquidity")))
        flattened.setdefault("volume24hUsd", optional_float(market_info.get("volume")))
        flattened.setdefault("priceUsd", optional_float(market_info.get("price")))
        flattened.setdefault("holders", optional_int(market_info.get("holders")))
        flattened.setdefault("swaps", optional_int(market_info.get("swaps")))
        flattened.setdefault("price_change_5m_percent", percent_value(market_info.get("percent_5m")))
        flattened.setdefault("price_change_1h_percent", percent_value(market_info.get("percent_1h")))
        flattened.setdefault("price_change_24h_percent", percent_value(market_info.get("percent_24h")))
    if "activity_score" in row:
        activity_score = optional_float(row.get("activity_score"))
        flattened.setdefault("score", activity_score * 100 if activity_score is not None else None)
    if "smart_wallet_online_count" in row:
        flattened.setdefault("smart_money", optional_int(row.get("smart_wallet_online_count")))
    return flattened


def compact_number(raw: str) -> float | None:
    match = COMPACT_NUMBER_RE.search(str(raw or ""))
    if not match:
        return None
    value = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    elif suffix == "B":
        value *= 1_000_000_000
    return value


def chain_from_href(raw: str) -> str:
    return alpha_meme.normalize_gmgn_chain(raw)


def rows_from_anchor_items(items: list[dict[str, str]], max_rows: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        href = str(item.get("href") or "")
        match = TOKEN_HREF_RE.search(href)
        if not match:
            continue
        chain = chain_from_href(match.group(1))
        address = match.group(2).strip()
        text = str(item.get("text") or "").strip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        symbol = lines[0] if lines else ""
        row: dict[str, Any] = {
            "chain": chain,
            "address": address,
            "symbol": symbol,
        }
        for index, line in enumerate(lines):
            if line.upper() == "MC" and index + 1 < len(lines):
                market_cap = compact_number(lines[index + 1])
                if market_cap is not None:
                    row["marketCap"] = market_cap
            if "%" in line:
                pct = PERCENT_RE.search(line)
                if pct:
                    row["price_change_1h_percent"] = float(pct.group(1))
        if not alpha_meme.normalize_external_meme_row(row, "debot_signal"):
            continue
        add_provider_metadata(row, href, utc_now_iso())
        if symbol:
            row.setdefault("name", symbol)
        rows.append(row)
        if len(rows) >= max_rows:
            break
    return rows


def dedupe_rows(rows: list[dict[str, Any]], max_rows: int) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        chain = str(row.get("chainId") or row.get("chain") or "")
        address = str(row.get("tokenAddress") or row.get("token_address") or row.get("address") or row.get("mint") or "")
        key = alpha_meme.token_key(chain, address)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
        if len(deduped) >= max_rows:
            break
    return deduped


def page_json_rows(page: Any, max_rows: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for script in page.locator("script").all():
        try:
            text = script.text_content(timeout=500) or ""
        except Exception:  # noqa: BLE001
            continue
        if not text or "token" not in text.lower():
            continue
        for marker in ("self.__next_f.push(", "__NEXT_DATA__"):
            if marker not in text:
                continue
            rows.extend(normalize_debot_payload({"text": text}, f"inline:{marker}", max_rows))
    return rows


def capture_with_playwright(
    *,
    urls: list[str],
    out_path: Path,
    status_path: Path,
    timeout_seconds: int,
    max_rows: int,
    headed: bool,
    user_data_dir: str,
    channel: str,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "reason": "playwright_unavailable",
            "error": str(exc),
            "rows": 0,
            "finished_at": utc_now_iso(),
        }

    captured_rows: list[dict[str, Any]] = []
    seen_responses: list[str] = []
    errors: list[str] = []
    page_observations: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "headless": not headed,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            launch_kwargs["channel"] = channel
        if user_data_dir:
            context = playwright.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
            browser = None
        else:
            browser = playwright.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
                )
            )

        def handle_response(response: Any) -> None:
            url = response.url
            lowered = url.lower()
            content_type = (response.headers.get("content-type") or "").lower()
            if "json" not in content_type and not any(hint in lowered for hint in SIGNAL_URL_HINTS):
                return
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001
                return
            rows = normalize_debot_payload(payload, url, max_rows)
            if rows:
                captured_rows.extend(rows)
                seen_responses.append(url)

        page = context.new_page()
        page.on("response", handle_response)
        deadline_ms = max(5, timeout_seconds) * 1000
        try:
            for url in urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=deadline_ms)
                    page.wait_for_timeout(min(deadline_ms, 8000))
                    title = ""
                    body = ""
                    try:
                        title = page.title()[:120]
                    except Exception:  # noqa: BLE001
                        title = ""
                    try:
                        body = (page.locator("body").inner_text(timeout=1000) or "")[:240]
                    except Exception:  # noqa: BLE001
                        body = ""
                    page_observations.append({"url": url, "title": title, "body": body})
                    try:
                        anchors = page.evaluate(
                            """() => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                                text: (a.innerText || a.textContent || '').slice(0, 240),
                                href: a.href
                            }))"""
                        )
                        captured_rows.extend(rows_from_anchor_items(anchors, max_rows))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{url} anchors: {exc}")
                    captured_rows.extend(page_json_rows(page, max_rows))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{url}: {exc}")
                if len(captured_rows) >= max_rows:
                    break
        finally:
            context.close()
            if browser:
                browser.close()

    rows = dedupe_rows(captured_rows, max_rows)
    reason = "captured" if rows else "no_debot_signal_rows_captured"
    observed_text = "\n".join(
        f"{item.get('title','')}\n{item.get('body','')}" for item in page_observations
    ).lower()
    if not rows and ("just a moment" in observed_text or "cloudflare" in observed_text):
        reason = "cloudflare_challenge"
    elif not rows and ("login" in observed_text or "sign in" in observed_text or "登录" in observed_text):
        reason = "login_required_or_no_public_signal"
    if rows:
        write_json(out_path, {"data": rows, "source": "debot_browser_export", "updated_at": utc_now_iso()})
    status = {
        "ok": bool(rows),
        "reason": reason,
        "rows": len(rows),
        "output": str(out_path),
        "urls": urls,
        "response_count": len(seen_responses),
        "page_observations": page_observations[-3:],
        "errors": errors[:5],
        "finished_at": utc_now_iso(),
    }
    write_json(status_path, status)
    return status


def output_cached_or_empty(out_path: Path, status_path: Path, cache_max_age_seconds: int) -> bool:
    if not out_path.exists() or not status_path.exists():
        return False
    try:
        age = time.time() - max(out_path.stat().st_mtime, status_path.stat().st_mtime)
        if age > cache_max_age_seconds:
            return False
        payload = read_json(out_path)
        emit_json(payload)
        return True
    except Exception:  # noqa: BLE001
        return False


def mark_payload_stale(payload: Any, stale_at: str) -> dict[str, Any]:
    current = dict(payload) if isinstance(payload, dict) else {"data": []}
    rows = current.get("data") if isinstance(current.get("data"), list) else []
    current["data"] = [
        {
            **row,
            "provenance_status": "stale",
            "stale": True,
            "stale_at": stale_at,
        }
        for row in rows
        if isinstance(row, dict)
    ]
    current["provenance_status"] = "stale"
    current["stale_at"] = stale_at
    return current


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    out_path = Path(args.out)
    status_path = Path(args.status)
    if args.stdout_cache and output_cached_or_empty(out_path, status_path, args.cache_max_age_seconds):
        return {"ok": True, "reason": "cached", "finished_at": utc_now_iso()}
    try:
        status = capture_with_playwright(
            urls=args.urls,
            out_path=out_path,
            status_path=status_path,
            timeout_seconds=args.timeout_seconds,
            max_rows=args.max_rows,
            headed=args.headed,
            user_data_dir=args.user_data_dir,
            channel=args.channel,
        )
    except Exception as exc:  # noqa: BLE001
        status = {
            "ok": False,
            "reason": "capture_exception",
            "error": str(exc),
            "finished_at": utc_now_iso(),
        }
        write_json(status_path, status)
    payload = read_json(out_path) if out_path.exists() else {"data": []}
    if not status.get("ok") and payload.get("data"):
        payload = mark_payload_stale(payload, str(status.get("finished_at") or utc_now_iso()))
        write_json(out_path, payload)
    emit_json(payload)
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export DeBot read-only AI signals to the Meme source inbox.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--status", default=str(DEFAULT_STATUS))
    parser.add_argument("--url", dest="urls", action="append")
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("DEBOT_CAPTURE_TIMEOUT_SECONDS", "25")))
    parser.add_argument("--max-rows", type=int, default=int(os.environ.get("DEBOT_SOURCE_LIMIT", "20")))
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--headed", action="store_true", default=os.environ.get("DEBOT_BROWSER_HEADED") == "1")
    parser.add_argument("--user-data-dir", default=os.environ.get("DEBOT_BROWSER_USER_DATA_DIR", ""))
    parser.add_argument("--channel", default=os.environ.get("DEBOT_BROWSER_CHANNEL", ""))
    parser.add_argument("--stdout-cache", action="store_true", default=True)
    parser.add_argument("--cache-max-age-seconds", type=int, default=int(os.environ.get("DEBOT_CACHE_MAX_AGE_SECONDS", "55")))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.urls = args.urls or csv_env("DEBOT_CAPTURE_URLS", DEFAULT_URLS)
    if not args.watch:
        run_once(args)
        return 0
    while True:
        try:
            run_once(args)
        except Exception as exc:  # noqa: BLE001
            write_json(
                Path(args.status),
                {"ok": False, "reason": "watch_exception", "error": str(exc), "finished_at": utc_now_iso()},
            )
        time.sleep(max(10, args.interval_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
