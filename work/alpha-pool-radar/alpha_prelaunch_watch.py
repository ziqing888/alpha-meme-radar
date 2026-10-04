"""Find pre-launch meme projects from public text feeds.

This is intentionally CA-optional: it looks for projects that are warming up
before a token is live, then keeps them out of trade/paper-entry flows until a
contract appears in normal market sources.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "outputs" / "prelaunch-project-watch.json"
DEFAULT_MD = ROOT / "outputs" / "prelaunch-project-watch.md"

POST_RE = re.compile(r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"(.*?)</div>\s*</div>', re.S)
TIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"', re.I)
EVM_RE = re.compile(r"(?<![a-zA-Z0-9])0x[a-fA-F0-9]{40}(?![a-zA-Z0-9])")
SOL_RE = re.compile(r"(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{38,44})(?![A-Za-z0-9])")

PRELAUNCH_PATTERNS = {
    "launch_soon": (
        "launch soon",
        "launching soon",
        "coming soon",
        "ca soon",
        "contract soon",
        "即将发币",
        "准备发币",
        "快发币",
        "准备上线",
        "即将上线",
        "即将开盘",
    ),
    "launchpad": (
        "four.meme",
        "4.meme",
        "flap",
        "noxa",
        "pump.fun",
        "pumpfun",
        "发射台",
        "发射平台",
        "fair launch",
        "fairlaunch",
    ),
    "allowlist": (
        "whitelist",
        "white list",
        "allowlist",
        "presale",
        "pre-sale",
        "wl",
        "白名单",
        "预售",
        "私募",
    ),
    "airdrop_tge": (
        "airdrop",
        "tge",
        "snapshot",
        "claim",
        "空投",
        "快照",
        "领取",
    ),
    "official_build": (
        "website",
        "docs",
        "testnet",
        "mainnet",
        "discord",
        "telegram",
        "官网",
        "文档",
        "测试网",
        "主网",
    ),
}

NEGATIVE_PATTERNS = (
    "already launched",
    "is live",
    "ca:",
    "contract:",
    "已发",
    "已开盘",
    "已上线",
)


@dataclass
class PrelaunchCandidate:
    source: str
    post_url: str
    observed_at: str
    project: str
    score: float
    stage: str
    matched_signals: list[str]
    has_contract: bool
    text: str


def strip_tags(fragment: str) -> str:
    normalized = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    normalized = re.sub(r"</(?:div|p|span|a|code|b|strong|i)>", "\n", normalized, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", normalized)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def fetch_text(url: str, timeout_seconds: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 AlphaRadar/1.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def extract_project_name(text: str) -> str:
    cashtag = re.search(r"\$([A-Za-z0-9_\u4e00-\u9fff]{2,24})", text)
    if cashtag:
        return cashtag.group(1)
    prefix = re.search(
        r"^([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff ._-]{1,38}?)\s+"
        r"(?:launch|launching|coming|即将|准备|快发币|准备发币)",
        text,
        flags=re.I | re.M,
    )
    if prefix:
        return prefix.group(1).strip(" -:：")
    quoted = re.search(r"['\"“”「」]([^'\"“”「」]{2,40})['\"“”「」]", text)
    if quoted:
        return quoted.group(1).strip()
    for line in text.splitlines():
        cleaned = line.strip(" -:：|#")
        if 2 <= len(cleaned) <= 40 and not re.search(r"https?://|t\.me|launch|soon|发币|上线", cleaned, re.I):
            return cleaned
    return ""


def has_contract(text: str) -> bool:
    if EVM_RE.search(text):
        return True
    if re.search(r"\b(sol|solana|pump\.fun|pumpfun)\b", text, flags=re.I) and SOL_RE.search(text):
        return True
    return False


def score_text(text: str) -> tuple[float, list[str], str]:
    lowered = text.lower()
    score = 0.0
    signals: list[str] = []
    for group, patterns in PRELAUNCH_PATTERNS.items():
        hits = [pattern for pattern in patterns if pattern.lower() in lowered]
        if not hits:
            continue
        signals.append(group)
        if group == "launch_soon":
            score += 36
        elif group == "launchpad":
            score += 26
        elif group == "allowlist":
            score += 18
        elif group == "airdrop_tge":
            score += 16
        elif group == "official_build":
            score += 10
    if any(pattern in lowered for pattern in NEGATIVE_PATTERNS):
        score -= 18
    if re.search(r"https?://|t\.me/|x\.com/", text, flags=re.I):
        score += 8
    contract = has_contract(text)
    if contract:
        score -= 25
    if "launch_soon" in signals and "launchpad" in signals:
        score += 12
    if "allowlist" in signals and "official_build" in signals:
        score += 8
    score = max(0.0, min(100.0, score))
    stage = "prelaunch_watch"
    if contract:
        stage = "has_contract_route_to_meme_radar"
    elif score >= 70:
        stage = "priority_prelaunch"
    elif score >= 45:
        stage = "candidate_prelaunch"
    return round(score, 2), signals, stage


def parse_telegram_preview(source: str, html_text: str) -> list[PrelaunchCandidate]:
    rows: list[PrelaunchCandidate] = []
    for post_id, fragment in POST_RE.findall(html_text):
        text = strip_tags(fragment)
        if not text:
            continue
        score, signals, stage = score_text(text)
        if score < 30 and not signals:
            continue
        time_match = TIME_RE.search(fragment)
        project = extract_project_name(text)
        rows.append(
            PrelaunchCandidate(
                source=source,
                post_url=f"https://t.me/{post_id}",
                observed_at=time_match.group(1) if time_match else "",
                project=project,
                score=score,
                stage=stage,
                matched_signals=signals,
                has_contract=has_contract(text),
                text=text[:900],
            )
        )
    return rows


def candidate_to_dict(row: PrelaunchCandidate) -> dict[str, Any]:
    return {
        "source": row.source,
        "post_url": row.post_url,
        "observed_at": row.observed_at,
        "project": row.project,
        "score": row.score,
        "stage": row.stage,
        "matched_signals": row.matched_signals,
        "has_contract": row.has_contract,
        "text": row.text,
    }


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Prelaunch Project Watch",
        "",
        f"- Updated: {payload['updated_at']}",
        f"- Sources scanned: {len(payload['sources'])}",
        f"- Candidates: {len(payload['candidates'])}",
        "",
        "| Score | Stage | Project | Source | Signals | Post |",
        "|---:|---|---|---|---|---|",
    ]
    for row in payload["candidates"]:
        lines.append(
            f"| {row['score']} | {row['stage']} | {row['project'] or '--'} | {row['source']} | "
            f"{', '.join(row['matched_signals']) or '--'} | {row['post_url']} |"
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> dict[str, Any]:
    sources: dict[str, str] = {}
    for spec in args.source:
        if "=" in spec:
            name, url = spec.split("=", 1)
            sources[name.strip()] = url.strip()
        else:
            url = spec.strip()
            sources[url.rstrip("/").split("/")[-1]] = url
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, url in sources.items():
        try:
            candidates.extend(candidate_to_dict(row) for row in parse_telegram_preview(name, fetch_text(url, args.timeout_seconds)))
            time.sleep(args.sleep_seconds)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    candidates.sort(key=lambda row: (row["has_contract"], -float(row["score"]), row["source"]))
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "method": "public text preview; no account/private API; prelaunch-only scoring; CA rows are routed back to normal meme radar",
        "sources": sources,
        "candidates": candidates[: args.limit],
        "errors": errors,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(markdown_report(payload), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Find pre-launch meme projects from public text feeds.")
    parser.add_argument("--source", action="append", default=[], help="name=url or public preview URL")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--timeout-seconds", type=int, default=15)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()
    if not args.source:
        args.source = ["mobai19999=https://t.me/s/mobai19999"]
    payload = run(args)
    print(json.dumps({"ok": True, "sources": len(payload["sources"]), "candidates": len(payload["candidates"]), "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
