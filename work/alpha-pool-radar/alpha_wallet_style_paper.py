from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPORT_PATH = OUT_DIR / "alpha-radar-report-latest.json"
DEFAULT_STATE_PATH = OUT_DIR / "alpha-wallet-style-paper-state.json"
DEFAULT_MARKDOWN_PATH = OUT_DIR / "alpha-wallet-style-paper-latest.md"
DEFAULT_EVENTS_PATH = OUT_DIR / "alpha-wallet-style-paper-events.jsonl"

CN_TZ = timezone(timedelta(hours=8))


SETTINGS = {
    "starting_cash_usd": 1000.0,
    "max_open_positions": 5,
    "max_new_entries_per_run": 2,
    "max_new_entries_per_day": 999,
    "min_position_usd": 45.0,
    "base_position_usd": 60.0,
    "max_position_usd": 100.0,
    "tp1_return_pct": 45.0,
    "tp1_sell_fraction": 0.50,
    "tp2_return_pct": 120.0,
    "tp2_sell_fraction": 0.30,
    "runner_trail_drawdown_pct": 35.0,
    "reentry_cooldown_minutes": 180,
}


def now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "").replace("%", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    if value is None:
        return "unknown"
    number = to_float(value)
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"
    if abs(number) >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:.2f}"


def pct(value: Any) -> str:
    if value is None:
        return "unknown"
    return f"{to_float(value):.1f}%"


def norm_text(value: Any) -> str:
    return str(value or "").strip()


def token_key(row: dict[str, Any]) -> str:
    chain = norm_text(row.get("chain") or row.get("network") or "bsc").lower()
    contract = norm_text(row.get("contract_address") or row.get("token_address") or row.get("address")).lower()
    return f"{chain}:{contract}" if contract else ""


def risk_value(flags: list[str], prefix: str) -> float:
    for flag in flags:
        text = str(flag)
        if not text.startswith(prefix):
            continue
        digits = "".join(ch if (ch.isdigit() or ch in ".-") else " " for ch in text).split()
        if digits:
            return to_float(digits[0])
    return 0.0


def extract_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in ("meme_potential_rows", "meme_shadow_rows"):
        for row in report.get(section) or []:
            if isinstance(row, dict):
                rows.append({**row, "_paper_source_section": section})
    return rows


def positive_price(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        price = to_float(value)
    except OverflowError:
        return 0.0
    return price if math.isfinite(price) and price > 0 else 0.0


def row_price(row: dict[str, Any]) -> float:
    return positive_price(row.get("price_usd") or row.get("price") or row.get("token_price"))


def is_ambush_bucket(row: dict[str, Any]) -> bool:
    bucket = norm_text(row.get("recommendation_bucket")).lower()
    shadow_bucket = norm_text(row.get("shadow_original_bucket")).lower()
    return bucket == "ambush" or shadow_bucket == "ambush"


def classify_candidate(row: dict[str, Any]) -> dict[str, Any]:
    key = token_key(row)
    chain = key.split(":", 1)[0] if ":" in key else norm_text(row.get("chain")).lower()
    price = row_price(row)
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = to_float(row.get("liquidity") or row.get("liquidity_usd"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_liq = volume / liquidity if liquidity > 0 else 0.0
    age = to_float(row.get("pair_age_hours"))
    change_m5 = to_float(row.get("change_m5"))
    change_h1 = to_float(row.get("change_h1"))
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    source_count = to_float(row.get("source_count"))
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    status = norm_text(row.get("watch_status")).lower()
    bucket = norm_text(row.get("recommendation_bucket")).lower()
    shadow_bucket = norm_text(row.get("shadow_original_bucket")).lower()
    ambush_like = is_ambush_bucket(row)
    second_entry_like = (
        status == "achieved_gold"
        and shadow_bucket == "pullback"
        and 0 < age <= 120
        and 0 < mcap <= 800_000
        and smart_money >= 50
        and kol >= 15
        and 0 < top10 <= 25
        and -12 <= change_m5 <= 8
        and -15 <= change_h1 <= 20
        and 0.5 <= volume_to_liq <= 10
    )
    style_mode = "second_entry_pullback" if second_entry_like else "early_ambush"

    reject_reason = ""
    if not key:
        reject_reason = "缺少合约地址"
    elif chain not in {"bsc", "bnb", "bsc-mainnet"}:
        reject_reason = f"不是 BSC 链：{chain}"
    elif price <= 0:
        reject_reason = "没有可用价格"
    elif not ambush_like and not second_entry_like:
        reject_reason = "不是小仓试探或二次回踩画像"
    elif status == "invalidated" or row.get("watch_invalid_reason"):
        reject_reason = norm_text(row.get("watch_invalid_reason")) or "监控票已失效"
    elif bucket in {"danger", "reject"}:
        reject_reason = f"当前分类为 {bucket}"
    elif risk_value(flags, "rug_") >= 25 or risk_value(flags, "bundler_") >= 25:
        reject_reason = "GMGN 硬风险偏高"
    elif risk_value(flags, "sniper_") >= 45:
        reject_reason = "狙击盘比例过高"
    elif top10 >= 40 or max_holder >= 18:
        reject_reason = f"筹码集中，Top10 {pct(top10)}，最大钱包 {pct(max_holder)}"
    elif liquidity < 8_000 or volume < 50_000:
        reject_reason = f"池子或成交太弱，流动性 {money(liquidity)}，成交 {money(volume)}"
    elif age > 36 and not second_entry_like:
        reject_reason = f"池龄 {age:.1f}h，过了钱包复刻的早期窗口"
    elif mcap > 1_200_000:
        reject_reason = f"当前市值 {money(mcap)}，已经偏晚"
    elif status == "achieved_gold" and mcap > 800_000:
        reject_reason = "已经金狗确认，当前不追确认后的高位"
    elif change_m5 > 22 or change_h1 > 120:
        reject_reason = f"短线过热，5m {pct(change_m5)}，1h {pct(change_h1)}"
    elif change_h1 < -35:
        reject_reason = f"1h 跌幅 {pct(change_h1)}，不接自由落体"
    elif volume_to_liq > 14 and change_h1 < 0:
        reject_reason = f"换手过猛但承接转弱，量池比 {volume_to_liq:.1f}"

    score = (
        to_float(row.get("entry_score")) * 0.30
        + to_float(row.get("gold_dog_conviction_score")) * 0.25
        + to_float(row.get("backtest_rule_score")) * 0.18
        + to_float(row.get("pro_signal_score")) * 0.17
        + to_float(row.get("gold_dog_score")) * 0.10
    )
    if source_count >= 2:
        score += 4
    if smart_money >= 20:
        score += 6
    if kol >= 5:
        score += 4
    if 0 < top10 <= 25:
        score += 4
    if 0 < age <= 12:
        score += 5
    elif age <= 24:
        score += 2
    elif second_entry_like:
        score += 5
    if 0 < mcap <= 300_000:
        score += 8
    elif mcap <= 800_000:
        score += 4
    if 15_000 <= liquidity <= 150_000:
        score += 4
    if -8 <= change_m5 <= 12:
        score += 4
    if 2 <= change_h1 <= 55:
        score += 4
    elif change_h1 < 0:
        score -= 2
    if second_entry_like:
        score += 6
    if reject_reason:
        score = min(score, 69.0)

    reasons = [
        f"模式 {style_mode}",
        f"聪明钱 {smart_money:.0f}/KOL {kol:.0f}",
        f"市值 {money(mcap)}、池子 {money(liquidity)}、量池比 {volume_to_liq:.1f}",
        f"池龄 {age:.1f}h，5m {pct(change_m5)}，1h {pct(change_h1)}",
        f"Top10 {pct(top10)}，风险 {', '.join(flags) or '无硬标记'}",
    ]
    eligible = not reject_reason and score >= 75
    return {
        "eligible": eligible,
        "score": round(max(0.0, min(100.0, score)), 2),
        "reject_reason": reject_reason,
        "reasons": reasons,
        "key": key,
        "style_mode": style_mode,
    }


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


def load_state(path: Path) -> dict[str, Any]:
    state = load_json(path, {})
    if not isinstance(state, dict) or not state:
        state = {
            "cash_usd": SETTINGS["starting_cash_usd"],
            "realized_pnl_usd": 0.0,
            "open_positions": [],
            "closed_positions": [],
            "events": [],
            "settings": SETTINGS,
        }
    state.setdefault("cash_usd", SETTINGS["starting_cash_usd"])
    state.setdefault("realized_pnl_usd", 0.0)
    state.setdefault("open_positions", [])
    state.setdefault("closed_positions", [])
    state.setdefault("events", [])
    state["settings"] = SETTINGS
    return state


def position_return(position: dict[str, Any], current_price: float) -> float:
    entry = to_float(position.get("entry_price_usd"))
    if entry <= 0:
        return 0.0
    return (current_price / entry - 1.0) * 100.0


def stop_pct_for(row: dict[str, Any], score: float) -> float:
    top10 = to_float(row.get("top10_holder_pct"))
    sniper = risk_value([str(flag) for flag in (row.get("gmgn_risk_flags") or [])], "sniper_")
    mcap = to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    if score >= 90 and top10 <= 25 and sniper <= 15:
        return 22.0
    if top10 > 30 or sniper > 25 or mcap < 25_000:
        return 14.0
    return 18.0


def size_for(row: dict[str, Any], score: float, cash_usd: float) -> float:
    base = SETTINGS["base_position_usd"] * (score / 82.0)
    if to_float(row.get("smart_money")) >= 50 and to_float(row.get("kol")) >= 10:
        base += 15
    if to_float(row.get("top10_holder_pct")) > 30:
        base -= 15
    size = max(SETTINGS["min_position_usd"], min(SETTINGS["max_position_usd"], base))
    return round(min(size, max(0.0, cash_usd)), 2)


def sell_fraction(
    state: dict[str, Any],
    position: dict[str, Any],
    fraction: float,
    current_price: float,
    event_type: str,
    now: str,
    reason: str,
) -> dict[str, Any]:
    remaining = to_float(position.get("remaining_fraction"), 1.0)
    sold_fraction = round(min(remaining, fraction), 6)
    ret_pct = position_return(position, current_price)
    size_usd = to_float(position.get("size_usd"))
    proceeds = size_usd * sold_fraction * (1.0 + ret_pct / 100.0)
    cost = size_usd * sold_fraction
    pnl = proceeds - cost
    state["cash_usd"] = round(to_float(state.get("cash_usd")) + proceeds, 4)
    state["realized_pnl_usd"] = round(to_float(state.get("realized_pnl_usd")) + pnl, 4)
    position["remaining_fraction"] = round(max(0.0, remaining - sold_fraction), 6)
    position["realized_pnl_usd"] = round(to_float(position.get("realized_pnl_usd")) + pnl, 4)
    event = {
        "type": event_type,
        "time": now,
        "key": position.get("key"),
        "symbol": position.get("symbol"),
        "price_usd": current_price,
        "sold_fraction": sold_fraction,
        "return_pct": round(ret_pct, 2),
        "pnl_usd": round(pnl, 4),
        "reason": reason,
    }
    return event


def update_positions(state: dict[str, Any], row_by_key: dict[str, dict[str, Any]], now: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = []
    for position in state.get("open_positions") or []:
        key = norm_text(position.get("key"))
        row = row_by_key.get(key)
        if not row:
            position["last_update_note"] = "当前报告没有这个币，保留观察"
            still_open.append(position)
            continue
        price = row_price(row)
        if price <= 0:
            position["last_update_note"] = "当前价格不可用，保留观察"
            still_open.append(position)
            continue

        high = max(to_float(position.get("high_price_usd")), price)
        position["high_price_usd"] = high
        position["last_price_usd"] = price
        position["last_return_pct"] = round(position_return(position, price), 2)
        position["last_seen_at"] = now

        ret = position["last_return_pct"]
        stop_pct = to_float(position.get("stop_pct"))
        drawdown_from_high = (price / high - 1.0) * 100.0 if high > 0 else 0.0

        if ret <= -stop_pct:
            events.append(sell_fraction(state, position, 1.0, price, "stop_loss", now, f"跌破 -{stop_pct:.0f}% 止损"))
        elif not position.get("tp1_hit") and ret >= SETTINGS["tp1_return_pct"]:
            events.append(
                sell_fraction(
                    state,
                    position,
                    SETTINGS["tp1_sell_fraction"],
                    price,
                    "take_profit_1",
                    now,
                    f"达到 TP1 +{SETTINGS['tp1_return_pct']:.0f}%，先收本金和利润",
                )
            )
            position["tp1_hit"] = True
        elif not position.get("tp2_hit") and ret >= SETTINGS["tp2_return_pct"]:
            events.append(
                sell_fraction(
                    state,
                    position,
                    SETTINGS["tp2_sell_fraction"],
                    price,
                    "take_profit_2",
                    now,
                    f"达到 TP2 +{SETTINGS['tp2_return_pct']:.0f}%，继续减仓",
                )
            )
            position["tp2_hit"] = True
        elif ret >= 80 and drawdown_from_high <= -SETTINGS["runner_trail_drawdown_pct"]:
            events.append(
                sell_fraction(
                    state,
                    position,
                    1.0,
                    price,
                    "runner_trailing_exit",
                    now,
                    f"尾仓从高点回撤 {abs(drawdown_from_high):.1f}%",
                )
            )

        if to_float(position.get("remaining_fraction"), 0.0) > 0:
            still_open.append(position)
        else:
            position["closed_at"] = now
            position["status"] = "closed"
            state.setdefault("closed_positions", []).append(position)
    state["open_positions"] = still_open
    return events


def open_positions(
    state: dict[str, Any],
    candidates: list[dict[str, Any]],
    row_by_key: dict[str, dict[str, Any]],
    now: str,
    blocked_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    open_keys = {norm_text(pos.get("key")) for pos in state.get("open_positions") or []}
    blocked_keys = blocked_keys or set()
    slots = max(0, int(SETTINGS["max_open_positions"]) - len(open_keys))
    remaining_daily = max(0, int(SETTINGS["max_new_entries_per_day"]) - daily_open_count(state, now))
    new_limit = min(slots, int(SETTINGS["max_new_entries_per_run"]), remaining_daily)
    for item in candidates:
        if len(events) >= new_limit:
            break
        if not item["classification"]["eligible"]:
            continue
        key = item["classification"]["key"]
        if key in open_keys:
            continue
        if key in blocked_keys:
            continue
        row = row_by_key[key]
        price = row_price(row)
        score = to_float(item["classification"]["score"])
        size = size_for(row, score, to_float(state.get("cash_usd")))
        if size < SETTINGS["min_position_usd"]:
            break
        stop_pct = stop_pct_for(row, score)
        state["cash_usd"] = round(to_float(state.get("cash_usd")) - size, 4)
        position = {
            "status": "open",
            "key": key,
            "symbol": norm_text(row.get("symbol") or row.get("name") or key),
            "chain": "bsc",
            "contract_address": norm_text(row.get("contract_address") or row.get("token_address") or row.get("address")),
            "entry_time": now,
            "entry_price_usd": price,
            "entry_mcap_usd": to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv")),
            "size_usd": size,
            "margin_usd": size,
            "leverage": "1x spot paper",
            "remaining_fraction": 1.0,
            "realized_pnl_usd": 0.0,
            "high_price_usd": price,
            "last_price_usd": price,
            "last_return_pct": 0.0,
            "stop_pct": stop_pct,
            "stop_price_usd": round(price * (1.0 - stop_pct / 100.0), 12),
            "tp1_price_usd": round(price * (1.0 + SETTINGS["tp1_return_pct"] / 100.0), 12),
            "tp2_price_usd": round(price * (1.0 + SETTINGS["tp2_return_pct"] / 100.0), 12),
            "tp1_hit": False,
            "tp2_hit": False,
            "score": score,
            "style_mode": item["classification"]["style_mode"],
            "rationale": item["classification"]["reasons"],
            "url": row.get("url") or row.get("dex_url") or "",
        }
        state.setdefault("open_positions", []).append(position)
        open_keys.add(key)
        events.append(
            {
                "type": "open",
                "time": now,
                "key": key,
                "symbol": position["symbol"],
                "price_usd": price,
                "size_usd": size,
                "margin_usd": size,
                "stop_price_usd": position["stop_price_usd"],
                "tp1_price_usd": position["tp1_price_usd"],
                "tp2_price_usd": position["tp2_price_usd"],
                "score": score,
                "style_mode": item["classification"]["style_mode"],
                "reason": "钱包复刻：BSC 小仓 + 聪明钱/KOL + 可承接盘口",
            }
        )
    return events


def account_summary(state: dict[str, Any], row_by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    unrealized = 0.0
    exposure = 0.0
    stale_positions = unpriced_positions = 0
    for position in state.get("open_positions") or []:
        row = row_by_key.get(norm_text(position.get("key")))
        remaining = to_float(position.get("remaining_fraction"), 1.0)
        size = to_float(position.get("size_usd"))
        exposure += size * remaining
        if remaining <= 0:
            continue
        price = row_price(row) if row else 0.0
        if price <= 0:
            price = positive_price(position.get("last_price_usd"))
            if price > 0:
                stale_positions += 1
        if price <= 0 or positive_price(position.get("entry_price_usd")) <= 0:
            unpriced_positions += 1
            continue
        ret = position_return(position, price)
        unrealized += size * remaining * ret / 100.0
    equity = to_float(state.get("cash_usd")) + exposure + unrealized
    return {
        "cash_usd": round(to_float(state.get("cash_usd")), 4),
        "open_exposure_usd": round(exposure, 4),
        "realized_pnl_usd": round(to_float(state.get("realized_pnl_usd")), 4),
        "unrealized_pnl_usd": round(unrealized, 4) if not unpriced_positions else None,
        "equity_usd": round(equity, 4) if not unpriced_positions else None,
        "valuation_status": "unavailable" if unpriced_positions else "stale" if stale_positions else "fresh",
        "stale_positions": stale_positions,
        "unpriced_positions": unpriced_positions,
    }


def build_candidates(report: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    row_by_key: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for row in extract_rows(report):
        key = token_key(row)
        if not key:
            continue
        existing = row_by_key.get(key)
        if existing and to_float(existing.get("entry_score")) >= to_float(row.get("entry_score")):
            continue
        row_by_key[key] = row
    for row in row_by_key.values():
        classification = classify_candidate(row)
        candidates.append({"row": row, "classification": classification})
    candidates.sort(
        key=lambda item: (
            not item["classification"]["eligible"],
            -to_float(item["classification"]["score"]),
            norm_text(item["row"].get("symbol")),
        )
    )
    return candidates, row_by_key


def parse_iso(value: Any) -> datetime | None:
    text = norm_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def day_key(value: Any) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return ""
    return parsed.astimezone(CN_TZ).date().isoformat()


def daily_open_count(state: dict[str, Any], now: str) -> int:
    today = day_key(now)
    if not today:
        return 0
    count = 0
    for event in state.get("events") or []:
        if event.get("type") == "open" and day_key(event.get("time")) == today:
            count += 1
    return count


def reentry_blocked_keys(state: dict[str, Any], now: str) -> set[str]:
    current = parse_iso(now)
    blocked: set[str] = set()
    for position in state.get("closed_positions") or []:
        key = norm_text(position.get("key"))
        if not key:
            continue
        closed_at = parse_iso(position.get("closed_at"))
        if not current or not closed_at:
            blocked.add(key)
            continue
        age_minutes = (current - closed_at).total_seconds() / 60
        if age_minutes <= SETTINGS["reentry_cooldown_minutes"]:
            blocked.add(key)
    return blocked


def run_paper_once(report: dict[str, Any], state: dict[str, Any], now: str) -> dict[str, Any]:
    candidates, row_by_key = build_candidates(report)
    events = update_positions(state, row_by_key, now)
    # Retire KOL-based entries without rewriting the legacy ledger or its exits.
    state["entry_policy"] = "legacy_exit_only"
    for item in candidates:
        item["classification"]["eligible"] = False
        item["classification"]["reject_reason"] = "旧钱包风格已停止开仓，盈利钱包验证独立记账"
    state["last_updated_at"] = now
    state.setdefault("events", []).extend(events)
    state["last_summary"] = account_summary(state, row_by_key)
    return {"state": state, "events": events, "candidates": candidates, "row_by_key": row_by_key}


def append_events(path: Path, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def render_markdown(result: dict[str, Any], now: str) -> str:
    state = result["state"]
    summary = state.get("last_summary") or {}
    events = result["events"]
    candidates = result["candidates"]

    lines = [
        "# Alpha Wallet Style Paper",
        "",
        f"- generated_at: {now}",
        f"- entry_policy: {state.get('entry_policy', 'legacy_unverified')}",
        "- 旧策略仅管理存量模拟仓位，不能作为盈利钱包跟随收益。",
        f"- equity: {money(summary.get('equity_usd'))}",
        f"- cash: {money(summary.get('cash_usd'))}",
        f"- open_exposure: {money(summary.get('open_exposure_usd'))}",
        f"- realized_pnl: {money(summary.get('realized_pnl_usd'))}",
        f"- unrealized_pnl: {money(summary.get('unrealized_pnl_usd'))}",
        f"- valuation_status: {summary.get('valuation_status') or 'unknown'}",
        f"- stale_positions: {summary.get('stale_positions', 'unknown')}",
        f"- unpriced_positions: {summary.get('unpriced_positions', 'unknown')}",
        "",
        "## Events",
    ]
    if events:
        for event in events:
            if event["type"] == "open":
                lines.append(
                    f"- OPEN {event['symbol']} size={money(event['size_usd'])} "
                    f"entry={event['price_usd']:.12g} SL={event['stop_price_usd']:.12g} "
                    f"TP1={event['tp1_price_usd']:.12g} TP2={event['tp2_price_usd']:.12g}"
                )
            else:
                lines.append(
                    f"- {event['type'].upper()} {event['symbol']} sold={event['sold_fraction']:.2f} "
                    f"return={event['return_pct']:.1f}% pnl={money(event['pnl_usd'])} reason={event['reason']}"
                )
    else:
        lines.append("- NO_ACTION 没有新开仓或出场动作。")

    lines.extend(["", "## Open Positions"])
    if state.get("open_positions"):
        for pos in state["open_positions"]:
            lines.append(
                f"- {pos['symbol']} {pos['contract_address']} size={money(pos['size_usd'])} "
                f"remain={to_float(pos.get('remaining_fraction')):.2f} "
                f"ret={pct(pos.get('last_return_pct'))} SL={pos['stop_price_usd']:.12g} "
                f"TP1={pos['tp1_price_usd']:.12g} TP2={pos['tp2_price_usd']:.12g}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Top Scan"])
    for item in candidates[:10]:
        row = item["row"]
        c = item["classification"]
        label = "ELIGIBLE" if c["eligible"] else "SKIP"
        reason = c["reject_reason"] or "; ".join(c["reasons"][:2])
        lines.append(
            f"- {label} {norm_text(row.get('symbol') or row.get('name'))} "
            f"mode={c['style_mode']} score={c['score']:.1f} "
            f"mcap={money(row.get('mcap') or row.get('market_cap') or row.get('fdv'))} "
            f"age={to_float(row.get('pair_age_hours')):.1f}h reason={reason}"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only BSC MEME wallet-style paper simulation.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN_PATH)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--now", default="")
    parser.add_argument("--interval-seconds", type=int, default=0, help="Run continuously when greater than 0.")
    args = parser.parse_args(argv)

    while True:
        report = load_json(args.report, {})
        if not report:
            raise SystemExit(f"report not found or invalid: {args.report}")
        state = load_state(args.state)
        current_now = args.now or now_iso()
        result = run_paper_once(report, state, current_now)
        save_json(args.state, result["state"])
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(result, current_now), encoding="utf-8")
        append_events(args.events, result["events"])

        print(json.dumps({"ok": True, "events": result["events"], "summary": result["state"]["last_summary"]}, ensure_ascii=False))
        if args.interval_seconds <= 0 or args.now:
            return 0
        time.sleep(max(15, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
