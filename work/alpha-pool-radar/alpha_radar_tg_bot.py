#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_REPORT = Path("outputs/alpha-radar-report-latest.json")
DEFAULT_STATE = Path("outputs/alpha-radar-tg-state.json")
DEFAULT_FAST_TRACK = Path("outputs/alpha-fast-track.json")
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{to_float(value):.{digits}f}%"


def money(value: Any) -> str:
    amount = to_float(value)
    if amount <= 0:
        return "-"
    for suffix, denom in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if amount >= denom:
            return f"${amount / denom:.1f}{suffix}"
    return f"${amount:.0f}"


def row_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or "?").strip()


def row_url(row: dict[str, Any]) -> str:
    return str(row.get("dex_url") or row.get("url") or "").strip()


def compact_reasons(row: dict[str, Any], include_metrics: bool = True) -> str:
    flags = row.get("dealer_flags") or row.get("flags") or row.get("sources") or []
    if isinstance(flags, str):
        flags = [flags]
    risk_flags = row.get("risk_flags") or []
    if isinstance(risk_flags, str):
        risk_flags = [risk_flags]
    heat_flags = row.get("heat_flags") or []
    if isinstance(heat_flags, str):
        heat_flags = [heat_flags]
    flags = list(risk_flags) + list(heat_flags) + list(flags)
    reasons: list[str] = []
    seen: set[str] = set()
    if include_metrics and row.get("top10_holder_pct") is not None:
        reasons.append(f"Top10 {pct(row.get('top10_holder_pct'))}")
        seen.add("top10")
    if include_metrics and row.get("oi_change_1h_pct") is not None and abs(to_float(row.get("oi_change_1h_pct"))) >= 5:
        reasons.append(f"OI {pct(row.get('oi_change_1h_pct'))}")
        seen.add("oi")
    if include_metrics and row.get("funding_rate_pct") is not None and abs(to_float(row.get("funding_rate_pct"))) >= 0.05:
        reasons.append(f"费率 {pct(row.get('funding_rate_pct'), 3)}")
        seen.add("funding")
    for flag in flags:
        for part in str(flag).replace(",", ";").split(";"):
            reason = part.strip()
            if not reason:
                continue
            key = reason.lower()
            if key in seen or key.startswith("top10 ") or key.startswith("oi ") or key.startswith("费率 "):
                continue
            seen.add(key)
            reasons.append(reason)
    return " / ".join(reasons[:4]) or "-"


def format_dealer_row(row: dict[str, Any], rank: int) -> str:
    score = to_float(row.get("dealer_score") or row.get("score"))
    return (
        f"{rank}. {row_symbol(row)} {score:.0f}分 | "
        f"Top10 {pct(row.get('top10_holder_pct'))} | Max {pct(row.get('max_holder_pct'))} | "
        f"{compact_reasons(row, include_metrics=False)}"
    )


def format_alpha_row(row: dict[str, Any], rank: int) -> str:
    score = to_float(row.get("score"))
    return (
        f"{rank}. {row_symbol(row)} {score:.0f}分 | "
        f"OI {pct(row.get('oi_change_1h_pct'))} | 费率 {pct(row.get('funding_rate_pct'), 3)} | "
        f"{compact_reasons(row, include_metrics=False)}"
    )


def format_meme_row(row: dict[str, Any], rank: int) -> str:
    score = to_float(row.get("score"))
    cap = money(row.get("mcap") or row.get("market_cap"))
    heat = to_float(row.get("heat_score"))
    heat_text = f" | Heat {heat:.0f}" if heat > 0 else ""
    return (
        f"{rank}. {row_symbol(row)} {score:.0f}分 | {cap}{heat_text} | "
        f"5m {pct(row.get('change_m5'))} | 1h {pct(row.get('change_h1'))}"
    )


def format_section(title: str, rows: list[dict[str, Any]], formatter, limit: int = 5) -> str:
    if not rows:
        return f"{title}\n暂无数据"
    lines = [title]
    for idx, row in enumerate(rows[:limit], start=1):
        lines.append(formatter(row, idx))
    return "\n".join(lines)


def format_top(report: dict[str, Any], limit: int = 3) -> str:
    generated = (report.get("meta") or {}).get("report_generated_at") or (report.get("meta") or {}).get("generated_at") or ""
    parts = [f"Alpha/Meme/庄家雷达\n更新: {generated}".strip()]
    parts.append(format_section("庄家雷达", report.get("dealer_rows") or [], format_dealer_row, limit))
    parts.append(format_section("Alpha 妖币", report.get("alpha_rows") or [], format_alpha_row, limit))
    parts.append(format_section("Meme 推荐", report.get("meme_rows") or [], format_meme_row, limit))
    return "\n\n".join(parts)


def format_symbol(report: dict[str, Any], symbol: str) -> str:
    target = symbol.strip().upper()
    matches: list[tuple[str, dict[str, Any]]] = []
    for section, rows in (("庄家", report.get("dealer_rows") or []), ("Alpha", report.get("alpha_rows") or []), ("Meme", report.get("meme_rows") or [])):
        for row in rows:
            if row_symbol(row).upper() == target:
                matches.append((section, row))
    if not matches:
        return f"没找到 {symbol}，可以先用 /top 看当前前排。"
    lines = [f"{target} 详情"]
    for section, row in matches:
        score = to_float(row.get("dealer_score") or row.get("score"))
        lines.append(
            f"{section}: {score:.0f}分 | Top10 {pct(row.get('top10_holder_pct'))} | "
            f"OI {pct(row.get('oi_change_1h_pct'))} | 费率 {pct(row.get('funding_rate_pct'), 3)}"
        )
        if row.get("risk_level"):
            lines.append(f"Risk: {row.get('risk_level')} {pct(row.get('risk_score'), 0)} | {row.get('risk_source') or '-'}")
        reasons = compact_reasons(row, include_metrics=False)
        if reasons != "-":
            lines.append(f"原因: {reasons}")
        if row_url(row):
            lines.append(row_url(row))
    return "\n".join(lines)


def normalize_watchlist(state: dict[str, Any]) -> list[str]:
    watchlist = state.get("watchlist") or []
    return sorted({str(symbol).strip().upper() for symbol in watchlist if str(symbol).strip()})


def handle_command(text: str, report: dict[str, Any], state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    command = (text or "").strip()
    next_state = dict(state)
    if command in {"/start", "/help"}:
        return (
            "命令: /top /dealer /alpha /meme /find SYMBOL /watch SYMBOL /unwatch SYMBOL /watchlist",
            next_state,
        )
    if command == "/top":
        return format_top(report, limit=3), next_state
    if command == "/dealer":
        return format_section("庄家雷达", report.get("dealer_rows") or [], format_dealer_row, 10), next_state
    if command == "/alpha":
        return format_section("Alpha 妖币", report.get("alpha_rows") or [], format_alpha_row, 10), next_state
    if command == "/meme":
        return format_section("Meme 推荐", report.get("meme_rows") or [], format_meme_row, 10), next_state
    if command.startswith("/find "):
        return format_symbol(report, command.split(maxsplit=1)[1]), next_state
    if command.startswith("/watch "):
        symbol = command.split(maxsplit=1)[1].strip().upper()
        watchlist = normalize_watchlist(next_state)
        if symbol and symbol not in watchlist:
            watchlist.append(symbol)
        next_state["watchlist"] = sorted(watchlist)
        return f"已加入观察: {symbol}\n当前观察: {', '.join(next_state['watchlist'])}", next_state
    if command.startswith("/unwatch "):
        symbol = command.split(maxsplit=1)[1].strip().upper()
        next_state["watchlist"] = [item for item in normalize_watchlist(next_state) if item != symbol]
        return f"已移除观察: {symbol}\n当前观察: {', '.join(next_state['watchlist']) or '-'}", next_state
    if command == "/watchlist":
        return f"当前观察: {', '.join(normalize_watchlist(next_state)) or '-'}", next_state
    return "未知命令。发 /help 看命令。", next_state


def alert_key(section: str, row: dict[str, Any]) -> str:
    score = round(to_float(row.get("dealer_score") or row.get("score")), 1)
    top10 = round(to_float(row.get("top10_holder_pct")), 1) if row.get("top10_holder_pct") is not None else "-"
    oi = round(to_float(row.get("oi_change_1h_pct")), 1)
    funding = round(to_float(row.get("funding_rate_pct")), 3)
    return f"{section}:{row_symbol(row).upper()}:{score}:{top10}:{oi}:{funding}"


def is_alert_candidate(section: str, row: dict[str, Any], watchlist: set[str]) -> bool:
    symbol = row_symbol(row).upper()
    score = to_float(row.get("dealer_score") or row.get("score"))
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    oi = abs(to_float(row.get("oi_change_1h_pct")))
    funding = abs(to_float(row.get("funding_rate_pct")))
    if symbol in watchlist:
        return True
    if section == "dealer":
        return score >= 70 or top10 >= 55 or max_holder >= 18
    if section == "alpha":
        return score >= 58 or oi >= 5 or funding >= 0.05
    return score >= 80 or abs(to_float(row.get("change_h1"))) >= 35 or abs(to_float(row.get("change_m5"))) >= 20


def format_alert(section: str, row: dict[str, Any]) -> str:
    label = {"dealer": "庄家雷达", "alpha": "Alpha", "meme": "Meme"}.get(section, section)
    score = to_float(row.get("dealer_score") or row.get("score"))
    lines = [f"{label}提醒: {row_symbol(row)} {score:.0f}分"]
    lines.append(f"原因: {compact_reasons(row, include_metrics=False)}")
    if row.get("top10_holder_pct") is not None:
        lines.append(f"Top10: {pct(row.get('top10_holder_pct'))} | Max: {pct(row.get('max_holder_pct'))}")
    if row.get("risk_level"):
        lines.append(f"Risk: {row.get('risk_level')} {pct(row.get('risk_score'), 0)} | {row.get('risk_source') or '-'}")
    if row.get("oi_change_1h_pct") is not None or row.get("funding_rate_pct") is not None:
        lines.append(f"OI: {pct(row.get('oi_change_1h_pct'))} | 费率: {pct(row.get('funding_rate_pct'), 3)}")
    if row_url(row):
        lines.append(row_url(row))
    return "\n".join(lines)


def build_alert_messages(report: dict[str, Any], state: dict[str, Any], max_alerts: int = 5) -> list[dict[str, str]]:
    sent = set(state.get("sent_alert_keys") or [])
    watchlist = set(normalize_watchlist(state))
    alerts: list[dict[str, str]] = []
    for section, rows in (
        ("dealer", report.get("dealer_rows") or []),
        ("alpha", report.get("alpha_rows") or []),
        ("meme", report.get("meme_rows") or []),
    ):
        for row in rows:
            if not is_alert_candidate(section, row, watchlist):
                continue
            key = alert_key(section, row)
            if key in sent:
                continue
            alerts.append({"key": key, "text": format_alert(section, row)})
            if len(alerts) >= max_alerts:
                return alerts
    return alerts


def mark_alerts_sent(state: dict[str, Any], alerts: list[dict[str, str]]) -> None:
    sent = list(state.get("sent_alert_keys") or [])
    for alert in alerts:
        key = alert.get("key")
        if key and key not in sent:
            sent.append(key)
    state["sent_alert_keys"] = sent[-500:]
    state["last_alert_at"] = time.time()


FAST_TRACK_ALERT_TYPES = {"confirmed", "new", "first_discovery", "recovered", "pullback", "high_risk"}
FAST_TRACK_LABELS = {
    "confirmed": "金狗确认",
    "new": "首次发现",
    "first_discovery": "首次发现",
    "recovered": "恢复观察",
    "pullback": "回撤观察",
    "high_risk": "高风险重点",
}


def fast_track_alert_key(alert: dict[str, Any]) -> str:
    return ":".join(str(alert.get(key) or "") for key in ("id", "alert_type", "chain", "symbol", "created_at", "reason"))


def format_fast_track_alert(alert: dict[str, Any]) -> str:
    alert_type = str(alert.get("alert_type") or "confirmed")
    label = FAST_TRACK_LABELS.get(alert_type, "雷达事件")
    symbol = str(alert.get("symbol") or "?").strip()
    chain = str(alert.get("chain") or "-").strip()
    score = to_float(alert.get("gold_dog_conviction_score") or alert.get("rating"))
    score_text = f" | {score:.0f}分" if score > 0 else ""
    lines = [f"{label}｜{symbol}{score_text}", f"链: {chain} | 市值: {money(alert.get('mcap') or alert.get('market_cap'))}"]
    reason = str(alert.get("reason") or "").strip()
    if reason:
        lines.append(f"判断: {reason[:180]}")
    change_h1 = alert.get("change_h1")
    if change_h1 is not None:
        lines.append(f"1h: {pct(change_h1)} | 流动性: {money(alert.get('liquidity_usd'))}")
    url = str(alert.get("url") or "").strip()
    if url:
        lines.append(url)
    return "\n".join(lines)


def build_fast_track_alert_messages(
    fast_track: dict[str, Any], state: dict[str, Any], max_alerts: int = 5,
) -> list[dict[str, str]]:
    sent = set(state.get("fast_track_alert_keys") or [])
    alerts = [
        alert for alert in (fast_track.get("alerts") or [])
        if isinstance(alert, dict) and str(alert.get("alert_type") or "confirmed") in FAST_TRACK_ALERT_TYPES
    ]
    if not state.get("fast_track_initialized"):
        state["fast_track_initialized"] = True
        state["fast_track_alert_keys"] = [fast_track_alert_key(alert) for alert in alerts][-500:]
        return []
    result: list[dict[str, str]] = []
    for alert in alerts:
        key = fast_track_alert_key(alert)
        if not key or key in sent:
            continue
        result.append({"key": key, "text": format_fast_track_alert(alert)})
        if len(result) >= max_alerts:
            break
    return result


def mark_fast_track_alerts_sent(state: dict[str, Any], alerts: list[dict[str, str]]) -> None:
    sent = list(state.get("fast_track_alert_keys") or [])
    for alert in alerts:
        key = alert.get("key")
        if key and key not in sent:
            sent.append(key)
    state["fast_track_alert_keys"] = sent[-500:]
    state["fast_track_initialized"] = True


def telegram_request(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = TELEGRAM_API.format(token=token, method=method)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(token: str, chat_id: str, text: str, dry_run: bool = False) -> None:
    if dry_run:
        print(text)
        print()
        return
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})


def run_once(args: argparse.Namespace) -> int:
    if args.fast_track:
        fast_track = load_json(Path(args.fast_track_file), {})
        state = load_json(Path(args.state), {})
        alerts = build_fast_track_alert_messages(fast_track, state, max_alerts=args.max_alerts)
        if not alerts:
            save_json(Path(args.state), state)
            if args.dry_run:
                print("No new fast-track TG alerts.")
            return 0
        for alert in alerts:
            send_message(args.token, args.chat_id, alert["text"], args.dry_run or not args.token or not args.chat_id)
        mark_fast_track_alerts_sent(state, alerts)
        save_json(Path(args.state), state)
        return 0
    report = load_json(Path(args.report), {})
    state = load_json(Path(args.state), {})
    if args.command:
        text, state = handle_command(args.command, report, state)
        save_json(Path(args.state), state)
        send_message(args.token, args.chat_id, text, args.dry_run or not args.token or not args.chat_id)
        return 0
    alerts = build_alert_messages(report, state, max_alerts=args.max_alerts)
    if not alerts:
        if args.dry_run:
            print("No new TG alerts.")
        return 0
    for alert in alerts:
        send_message(args.token, args.chat_id, alert["text"], args.dry_run or not args.token or not args.chat_id)
    mark_alerts_sent(state, alerts)
    save_json(Path(args.state), state)
    return 0


def run_poll(args: argparse.Namespace) -> int:
    if not args.token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN or pass --token before polling.")
    report_path = Path(args.report)
    state_path = Path(args.state)
    state = load_json(state_path, {})
    offset = int(state.get("telegram_update_offset") or 0)
    allowed_chat = str(args.chat_id or "").strip()
    while True:
        payload = {"timeout": 25, "offset": offset}
        response = telegram_request(args.token, "getUpdates", payload)
        for update in response.get("result") or []:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            text = str(message.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            if allowed_chat and chat_id != allowed_chat:
                continue
            report = load_json(report_path, {})
            state = load_json(state_path, {})
            reply, state = handle_command(text, report, state)
            state["telegram_update_offset"] = offset
            save_json(state_path, state)
            send_message(args.token, chat_id, reply, dry_run=False)
        state["telegram_update_offset"] = offset
        save_json(state_path, state)
        time.sleep(max(1, args.poll_seconds))


def run_fast_track_poll(args: argparse.Namespace) -> int:
    if not args.token and not args.dry_run:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN or pass --token before fast-track polling.")
    while True:
        run_once(args)
        if args.once:
            return 0
        time.sleep(max(3, args.poll_seconds))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram shell for Alpha/Meme/Dealer radar reports.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--fast-track-file", default=str(DEFAULT_FAST_TRACK))
    parser.add_argument("--token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--command", default="", help="Run one local command, for example /top.")
    parser.add_argument("--once", action="store_true", help="Send only new alerts once.")
    parser.add_argument("--poll", action="store_true", help="Poll Telegram commands.")
    parser.add_argument("--fast-track", action="store_true", help="Push new fast-track radar events.")
    parser.add_argument("--dry-run", action="store_true", help="Print messages instead of sending.")
    parser.add_argument("--max-alerts", type=int, default=5)
    parser.add_argument("--poll-seconds", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fast_track:
        return run_fast_track_poll(args)
    if args.poll:
        return run_poll(args)
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
