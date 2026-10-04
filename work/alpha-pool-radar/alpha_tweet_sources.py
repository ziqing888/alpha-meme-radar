#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_OUTPUT = OUT_DIR / "alpha-narrative-tweets.json"
DEFAULT_STATUS = OUT_DIR / "alpha-tweet-sources-status.json"
DEFAULT_ACCOUNTS = ("cz_binance", "heyibinance", "binance", "binancezh", "binancewallet", "bnbchain")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def env_command_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return []
    return [item.strip() for item in raw.replace("||", "\n").splitlines() if item.strip()]


def http_text(url: str, timeout: int = 8, headers: dict[str, str] | None = None) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AlphaRadar/1.0",
            "Accept": "application/rss+xml, application/atom+xml, application/json, text/plain, */*",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def child_text(node: ET.Element, name: str) -> str:
    found = node.find(name)
    return "" if found is None or found.text is None else found.text.strip()


def account_from_url(url: str) -> str:
    match = re.search(r"(?:x|twitter)\.com/([^/?#]+)/?", url, flags=re.IGNORECASE)
    return "" if not match else match.group(1)


def parse_rss_feed(raw: str, *, default_account: str = "") -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    rows: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = child_text(item, "title")
        link = child_text(item, "link")
        pub_date = child_text(item, "pubDate")
        account = default_account or account_from_url(link)
        if title:
            rows.append(
                {
                    "account": account,
                    "text": title,
                    "created_at": pub_date,
                    "url": link,
                    "source": "rss",
                }
            )
    for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = child_text(entry, "{http://www.w3.org/2005/Atom}title")
        updated = child_text(entry, "{http://www.w3.org/2005/Atom}updated")
        link_node = entry.find("{http://www.w3.org/2005/Atom}link")
        link = "" if link_node is None else str(link_node.attrib.get("href") or "")
        account = default_account or account_from_url(link)
        if title:
            rows.append(
                {
                    "account": account,
                    "text": title,
                    "created_at": updated,
                    "url": link,
                    "source": "atom",
                }
            )
    return rows


def account_from_payload(payload: Any, default_account: str = "") -> str:
    if isinstance(payload, dict):
        includes = payload.get("includes") or {}
        users = includes.get("users") if isinstance(includes, dict) else None
        if isinstance(users, list) and users and isinstance(users[0], dict):
            return str(users[0].get("username") or default_account)
        user = payload.get("user") or payload.get("author")
        if isinstance(user, dict):
            return str(user.get("username") or user.get("account") or default_account)
    return default_account


def normalize_json_tweet(row: dict[str, Any], *, default_account: str = "") -> dict[str, Any] | None:
    text = str(row.get("text") or row.get("full_text") or row.get("content") or "").strip()
    if not text:
        return None
    account = str(row.get("account") or row.get("username") or row.get("author") or default_account)
    tweet_id = str(row.get("id") or row.get("tweet_id") or "").strip()
    url = str(row.get("url") or "")
    if not url and account and tweet_id:
        url = f"https://x.com/{account}/status/{tweet_id}"
    return {
        "account": account,
        "text": text,
        "created_at": str(row.get("created_at") or row.get("time") or row.get("published_at") or ""),
        "url": url,
        "source": str(row.get("source") or "json"),
    }


def parse_json_payload(payload: Any, *, default_account: str = "") -> list[dict[str, Any]]:
    account = account_from_payload(payload, default_account)
    rows = payload.get("tweets") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and rows is None:
        rows = payload.get("data") or payload.get("items") or payload.get("results")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []
    normalized = [normalize_json_tweet(row, default_account=account) for row in rows if isinstance(row, dict)]
    return [row for row in normalized if row]


def source_account_from_url(url: str) -> str:
    lowered = url.lower()
    for account in DEFAULT_ACCOUNTS:
        if account.lower() in lowered:
            return account
    return ""


def parse_url_payload(url: str, raw: str) -> list[dict[str, Any]]:
    default_account = source_account_from_url(url)
    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return parse_json_payload(json.loads(raw), default_account=default_account)
    return parse_rss_feed(raw, default_account=default_account)


def parse_command_payload(command: str, raw: str) -> list[dict[str, Any]]:
    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return parse_json_payload(json.loads(raw), default_account=source_account_from_url(command))
    return parse_rss_feed(raw, default_account=source_account_from_url(command))


def browser_command_text(command: str, timeout: int = 20) -> str:
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-240:]
        raise RuntimeError(tail or f"command exited {result.returncode}")
    return result.stdout


def dedupe_tweets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("url") or "").strip() or f"{row.get('account')}:{row.get('text')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def collect_tweets(
    *,
    local_files: list[Path] | None = None,
    source_urls: list[str] | None = None,
    http_text: Callable[..., str] = http_text,
    browser_commands: list[str] | None = None,
    command_runner: Callable[..., str] = browser_command_text,
) -> dict[str, Any]:
    tweets: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in local_files or []:
        if not path.exists():
            continue
        try:
            payload = read_json(path)
            tweets.extend(parse_json_payload(payload))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path}: {exc}")
    for url in source_urls or []:
        try:
            tweets.extend(parse_url_payload(url, http_text(url, timeout=8)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    for command in browser_commands or []:
        try:
            tweets.extend(parse_command_payload(command, command_runner(command, timeout=20)))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"browser command: {exc}")
    tweets = dedupe_tweets(tweets)
    return {
        "ok": not errors or bool(tweets),
        "updated_at": now_iso(),
        "tweet_count": len(tweets),
        "tweets": tweets[-100:],
        "errors": errors,
    }


def write_tweets_file(output: Path, result: dict[str, Any]) -> None:
    write_json(output, result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect read-only narrative tweet sources for Alpha Radar.")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--source-url", action="append", default=[])
    parser.add_argument("--local-file", action="append", type=Path, default=[])
    parser.add_argument("--browser-command", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    urls = [*env_list("ALPHA_NARRATIVE_TWEET_URLS"), *args.source_url]
    browser_commands = [*env_command_list("ALPHA_NARRATIVE_BROWSER_COMMANDS"), *args.browser_command]
    local_files = [*(Path(path) for path in env_list("ALPHA_NARRATIVE_TWEET_FILES")), *args.local_file]
    if not local_files:
        local_files = [args.out_dir / "alpha-narrative-tweets-manual.json"]
    result = collect_tweets(local_files=local_files, source_urls=urls, browser_commands=browser_commands)
    write_tweets_file(args.output, result)
    write_json(args.status, {key: value for key, value in result.items() if key != "tweets"})
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
