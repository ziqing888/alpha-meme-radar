from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
DEFAULT_REPORT_PATH = OUT_DIR / "alpha-radar-report-latest.json"
DEFAULT_REPLAY_HISTORY_PATH = OUT_DIR / "alpha-radar-replay-history.json"
DEFAULT_STATE_PATH = OUT_DIR / "alpha-first-discovery-paper-state.json"
DEFAULT_MARKDOWN_PATH = OUT_DIR / "alpha-first-discovery-paper-latest.md"
DEFAULT_EVENTS_PATH = OUT_DIR / "alpha-first-discovery-paper-events.jsonl"
SHADOW_STATE_PATH = OUT_DIR / "alpha-first-discovery-shadow-paper-state.json"
SHADOW_MARKDOWN_PATH = OUT_DIR / "alpha-first-discovery-shadow-paper-latest.md"
SHADOW_EVENTS_PATH = OUT_DIR / "alpha-first-discovery-shadow-paper-events.jsonl"

CN_TZ = timezone(timedelta(hours=8))

SETTINGS = {
    "starting_cash_usd": 1000.0,
    "max_open_positions": 12,
    "max_new_entries_per_run": 3,
    "max_new_entries_per_day": 999,
    "min_position_usd": 20.0,
    "base_position_usd": 35.0,
    "max_position_usd": 55.0,
    "sweet_first_mcap_min": 30_000.0,
    "sweet_first_mcap_max": 100_000.0,
    "low_lottery_first_mcap_min": 10_000.0,
    "low_lottery_first_mcap_max": 30_000.0,
    "low_lottery_max_entry_delay_minutes": 45.0,
    "late_first_mcap_max": 300_000.0,
    "narrative_breakout_first_mcap_max": 1_000_000.0,
    "narrative_breakout_min_liquidity_usd": 30_000.0,
    "narrative_breakout_max_h1_change_pct": 100.0,
    "narrative_breakout_max_entry_delay_minutes": 45.0,
    "max_entry_delay_minutes": 45.0,
    "min_optimized_signal_score": 0.0,
    "min_liquidity_usd": 8_000.0,
    "min_smart_money": 0.0,
    "max_top10_holder_pct": 999.0,
    "min_h1_change_pct": -20.0,
    "max_h1_change_pct": 80.0,
    "min_m5_change_pct": -25.0,
    "max_m5_change_pct": 45.0,
    "max_first_pair_age_hours": 6.0,
    "tp1_return_pct": 100.0,
    "tp1_sell_fraction": 0.80,
    "tp2_return_pct": 999.0,
    "tp2_sell_fraction": 0.0,
    "time_stop_minutes": 90.0,
    "runner_trail_drawdown_pct": 35.0,
    "estimated_roundtrip_cost_pct": 5.0,
    "estimated_gas_usd_per_swap": 0.12,
    "reentry_cooldown_minutes": 360.0,
    "profile": "strict_45m",
    "allowed_chains": ["bsc", "bnb", "bsc-mainnet", "robinhood", "solana", "sol", "base", "base-mainnet"],
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


def parse_iso(value: Any) -> datetime | None:
    text = norm_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def day_key(value: Any) -> str:
    parsed = parse_iso(value)
    return parsed.date().isoformat() if parsed else ""


def allowed_chains() -> set[str]:
    return {norm_text(chain).lower() for chain in SETTINGS.get("allowed_chains", []) if norm_text(chain)}


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


def row_mcap(row: dict[str, Any]) -> float:
    return to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))


def optimized_signal_score(snapshot: dict[str, Any]) -> float:
    score = (
        to_float(snapshot.get("score")) * 0.24
        + to_float(snapshot.get("entry_score")) * 0.22
        + to_float(snapshot.get("gold_dog_score")) * 0.16
        + to_float(snapshot.get("gold_dog_conviction_score")) * 0.16
        + to_float(snapshot.get("backtest_rule_score")) * 0.12
        + to_float(snapshot.get("security_filter_score")) * 0.05
        + to_float(snapshot.get("holder_quality_score")) * 0.05
    )
    sources = snapshot.get("source_labels") or snapshot.get("sources") or []
    if isinstance(sources, list) and len(sources) >= 2:
        score += 4
    if to_float(snapshot.get("smart_money")) >= 30:
        score += 3
    if to_float(snapshot.get("kol")) >= 8:
        score += 2
    return max(0.0, min(100.0, score))


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


def apply_profile(profile: str) -> None:
    if profile == "shadow-180m":
        SETTINGS["profile"] = "shadow_180m"
        SETTINGS["max_entry_delay_minutes"] = 180.0
        return
    SETTINGS["profile"] = "strict_45m"
    SETTINGS["max_entry_delay_minutes"] = 45.0


def replay_row_for(row: dict[str, Any], replay_history: dict[str, Any]) -> dict[str, Any]:
    rows = replay_history.get("rows") if isinstance(replay_history.get("rows"), dict) else {}
    return rows.get(token_key(row), {}) if isinstance(rows, dict) else {}


def first_snapshot_for(history_row: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    snapshot = history_row.get("first_snapshot") if isinstance(history_row.get("first_snapshot"), dict) else {}
    if snapshot:
        return snapshot
    return row


def discovery_age_minutes(history_row: dict[str, Any], now: str) -> float:
    first_seen = parse_iso(history_row.get("first_seen_at"))
    current = parse_iso(now)
    if not first_seen or not current:
        return 999999.0
    return max(0.0, (current - first_seen).total_seconds() / 60.0)


def minutes_between(start: Any, end: Any) -> float:
    start_time = parse_iso(start)
    end_time = parse_iso(end)
    if not start_time or not end_time:
        return 0.0
    return max(0.0, (end_time - start_time).total_seconds() / 60.0)


def discovery_bucket(first_mcap: float) -> str:
    if SETTINGS["sweet_first_mcap_min"] <= first_mcap <= SETTINGS["sweet_first_mcap_max"]:
        return "sweet_30k_100k"
    if SETTINGS["low_lottery_first_mcap_min"] <= first_mcap < SETTINGS["low_lottery_first_mcap_max"]:
        return "small_lottery_10k_30k"
    if SETTINGS["sweet_first_mcap_max"] < first_mcap <= SETTINGS["late_first_mcap_max"]:
        return "late_100k_300k"
    if SETTINGS["late_first_mcap_max"] < first_mcap <= SETTINGS["narrative_breakout_first_mcap_max"]:
        return "narrative_300k_1m"
    return "out_of_range"


def max_entry_delay_for_bucket(bucket: str) -> float:
    if bucket == "small_lottery_10k_30k":
        return SETTINGS["low_lottery_max_entry_delay_minutes"]
    return SETTINGS["max_entry_delay_minutes"]


def source_groups(row: dict[str, Any], snapshot: dict[str, Any]) -> set[str]:
    """Collapse provider labels into independent discovery groups."""
    values: list[str] = []
    for source_row in (row, snapshot):
        for field in ("source_labels", "sources", "source_groups", "source_names"):
            value = source_row.get(field)
            if isinstance(value, list):
                values.extend(str(item).strip().lower() for item in value if str(item).strip())
            elif value:
                values.append(str(value).strip().lower())
        counts = source_row.get("source_hit_counts")
        if isinstance(counts, dict):
            values.extend(str(item).strip().lower() for item in counts if str(item).strip())
    groups: set[str] = set()
    for value in values:
        if "okx" in value:
            groups.add("okx")
        if "gmgn" in value:
            groups.add("gmgn")
        if "dexscreener" in value or value == "ds" or "dex screen" in value:
            groups.add("ds")
    return groups


def narrative_breakout_confirmed(row: dict[str, Any], snapshot: dict[str, Any], first_mcap: float, liquidity: float, change_h1: float) -> bool:
    groups = source_groups(row, snapshot)
    return (
        SETTINGS["late_first_mcap_max"] < first_mcap <= SETTINGS["narrative_breakout_first_mcap_max"]
        and liquidity >= SETTINGS["narrative_breakout_min_liquidity_usd"]
        and change_h1 <= SETTINGS["narrative_breakout_max_h1_change_pct"]
        and "okx" in groups
        and bool(groups & {"gmgn", "ds"})
    )


def classify_candidate(row: dict[str, Any], history_row: dict[str, Any], now: str) -> dict[str, Any]:
    key = token_key(row)
    chain = key.split(":", 1)[0] if ":" in key else norm_text(row.get("chain")).lower()
    snapshot = first_snapshot_for(history_row, row)
    price = row_price(row)
    current_mcap = row_mcap(row)
    first_mcap = row_mcap(snapshot)
    first_price = row_price(snapshot)
    age_minutes = discovery_age_minutes(history_row, now)
    markup = current_mcap / first_mcap if first_mcap > 0 and current_mcap > 0 else 999999.0
    bucket = discovery_bucket(first_mcap)
    bucket_max_delay = max_entry_delay_for_bucket(bucket)

    liquidity = to_float(row.get("liquidity") or row.get("liquidity_usd"))
    volume = to_float(row.get("volume24h") or row.get("dex_volume24h"))
    volume_to_liq = volume / liquidity if liquidity > 0 else 0.0
    change_m5 = to_float(row.get("change_m5"))
    change_h1 = to_float(row.get("change_h1"))
    smart_money = to_float(row.get("smart_money"))
    kol = to_float(row.get("kol"))
    source_count = to_float(row.get("source_count"))
    top10 = to_float(row.get("top10_holder_pct"))
    max_holder = to_float(row.get("max_holder_pct"))
    flags = [str(flag) for flag in (row.get("gmgn_risk_flags") or [])]
    status = norm_text(row.get("watch_status")).lower()
    first_pair_age = to_float(snapshot.get("pair_age_hours"))
    optimized_score = optimized_signal_score(snapshot)
    narrative_confirmed = narrative_breakout_confirmed(row, snapshot, first_mcap, liquidity, change_h1)
    execution_arm = "narrative_breakout" if narrative_confirmed else "first_discovery"
    if bucket == "narrative_300k_1m":
        bucket_max_delay = SETTINGS["narrative_breakout_max_entry_delay_minutes"]
    reject_reason = ""
    if not key:
        reject_reason = "缺少合约地址"
    elif chain not in allowed_chains():
        reject_reason = f"不在当前多链模拟范围：{chain}"
    elif not history_row:
        reject_reason = "没有首次发现历史，不做补票"
    elif price <= 0 or current_mcap <= 0 or first_mcap <= 0:
        reject_reason = "价格或市值不可用"
    elif age_minutes > bucket_max_delay:
        if bucket == "small_lottery_10k_30k":
            reject_reason = f"10K-30K 小彩票池只允许 {bucket_max_delay:.0f} 分钟内埋伏，当前已过去 {age_minutes:.0f} 分钟"
        else:
            reject_reason = f"首次发现已过去 {age_minutes:.0f} 分钟，超出新发现窗口"
    elif bucket == "out_of_range":
        reject_reason = f"首次发现市值 {money(first_mcap)} 不在试探区"
    elif bucket == "narrative_300k_1m" and not narrative_confirmed:
        groups = ", ".join(sorted(source_groups(row, snapshot))) or "无"
        reject_reason = f"大叙事突破缺少独立确认（当前来源组：{groups}）"
    elif optimized_score < SETTINGS["min_optimized_signal_score"]:
        reject_reason = f"优化分数 {optimized_score:.1f} 低于 {SETTINGS['min_optimized_signal_score']:.0f}"
    elif first_pair_age > SETTINGS["max_first_pair_age_hours"]:
        reject_reason = f"首次发现时池龄 {first_pair_age:.1f}h，超过优化窗口"
    elif smart_money < SETTINGS["min_smart_money"]:
        reject_reason = f"聪明钱 {smart_money:.0f} 低于优化门槛"
    elif bucket not in {"late_100k_300k", "narrative_300k_1m"} and markup > 2.2:
        reject_reason = f"已从首次发现涨到 {markup:.2f}x，不再算底部埋伏"
    elif status == "invalidated" or row.get("watch_invalid_reason"):
        reject_reason = norm_text(row.get("watch_invalid_reason")) or "监控票已失效"
    elif risk_value(flags, "rug_") >= 20 or risk_value(flags, "bundler_") >= 20:
        reject_reason = "GMGN 硬风险偏高"
    elif risk_value(flags, "sniper_") >= 35:
        reject_reason = "狙击盘比例过高"
    elif top10 > SETTINGS["max_top10_holder_pct"] or max_holder >= 18:
        reject_reason = f"筹码集中，Top10 {pct(top10)}，最大钱包 {pct(max_holder)}"
    elif liquidity < SETTINGS["min_liquidity_usd"]:
        reject_reason = f"池子太薄，流动性 {money(liquidity)}"
    elif not (SETTINGS["min_h1_change_pct"] <= change_h1 <= (SETTINGS["narrative_breakout_max_h1_change_pct"] if narrative_confirmed else SETTINGS["max_h1_change_pct"])):
        reject_reason = f"1h {pct(change_h1)} 不在优化区间"
    elif not (SETTINGS["min_m5_change_pct"] <= change_m5 <= SETTINGS["max_m5_change_pct"]):
        reject_reason = f"5m {pct(change_m5)} 不在优化区间"
    elif volume_to_liq > 18 and change_m5 < 0:
        reject_reason = f"换手过猛但短线转弱，量池比 {volume_to_liq:.1f}"

    score = optimized_score
    eligible = not reject_reason
    if reject_reason:
        score = min(score, 69.0)

    return {
        "eligible": eligible,
        "score": round(max(0.0, min(100.0, score)), 2),
        "reject_reason": reject_reason,
        "key": key,
        "chain": chain,
        "bucket": bucket,
        "execution_arm": execution_arm,
        "route_label": "大叙事突破" if execution_arm == "narrative_breakout" else "首次发现小仓",
        "source_groups": sorted(source_groups(row, snapshot)),
        "first_seen_at": history_row.get("first_seen_at"),
        "first_mcap_usd": round(first_mcap, 4),
        "current_mcap_usd": round(current_mcap, 4),
        "first_price_usd": first_price,
        "current_price_usd": price,
        "entry_delay_minutes": round(age_minutes, 2),
        "markup_from_first": round(markup, 4),
        "reasons": [
            f"首次发现市值 {money(first_mcap)}，当前 {money(current_mcap)}，涨幅 {markup:.2f}x",
            f"发现延迟 {age_minutes:.0f} 分钟，桶 {bucket}，优化分 {optimized_score:.1f}",
            f"聪明钱 {smart_money:.0f}/KOL {kol:.0f}/来源 {source_count:.0f}",
            f"池子 {money(liquidity)}，24h {money(volume)}，量池比 {volume_to_liq:.1f}",
            f"5m {pct(change_m5)}，1h {pct(change_h1)}，Top10 {pct(top10)}",
        ],
    }


def position_return(position: dict[str, Any], current_price: float) -> float:
    entry = to_float(position.get("entry_price_usd"))
    if entry <= 0:
        return 0.0
    return (current_price / entry - 1.0) * 100.0


def stop_pct_for(classification: dict[str, Any]) -> float:
    return 22.0


def size_for(classification: dict[str, Any], cash_usd: float) -> float:
    score = to_float(classification.get("score"))
    bucket = classification.get("bucket")
    size = SETTINGS["base_position_usd"] * max(0.55, score / 90.0)
    if bucket == "sweet_30k_100k":
        size += 8
    elif bucket == "small_lottery_10k_30k":
        size -= 10
    elif classification.get("execution_arm") == "narrative_breakout":
        size -= 10
    else:
        size -= 5
    size = max(SETTINGS["min_position_usd"], min(SETTINGS["max_position_usd"], size))
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
    gross_proceeds = size_usd * sold_fraction * (1.0 + ret_pct / 100.0)
    roundtrip_cost = size_usd * sold_fraction * SETTINGS["estimated_roundtrip_cost_pct"] / 100.0
    gas_cost = SETTINGS["estimated_gas_usd_per_swap"] * (1.0 + sold_fraction)
    trading_cost = roundtrip_cost + gas_cost
    proceeds = gross_proceeds - trading_cost
    cost = size_usd * sold_fraction
    pnl = proceeds - cost
    state["cash_usd"] = round(to_float(state.get("cash_usd")) + proceeds, 4)
    state["realized_pnl_usd"] = round(to_float(state.get("realized_pnl_usd")) + pnl, 4)
    position["remaining_fraction"] = round(max(0.0, remaining - sold_fraction), 6)
    position["realized_pnl_usd"] = round(to_float(position.get("realized_pnl_usd")) + pnl, 4)
    return {
        "type": event_type,
        "time": now,
        "key": position.get("key"),
        "symbol": position.get("symbol"),
        "price_usd": current_price,
        "sold_fraction": sold_fraction,
        "return_pct": round(ret_pct, 2),
        "trading_cost_usd": round(trading_cost, 4),
        "pnl_usd": round(pnl, 4),
        "reason": reason,
    }


def update_positions(state: dict[str, Any], row_by_key: dict[str, dict[str, Any]], now: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = []
    for position in state.get("open_positions") or []:
        held_minutes = minutes_between(position.get("entry_time"), now)
        row = row_by_key.get(norm_text(position.get("key")))
        if not row:
            if held_minutes >= SETTINGS["time_stop_minutes"]:
                price = to_float(position.get("last_price_usd") or position.get("entry_price_usd"))
                events.append(
                    sell_fraction(
                        state,
                        position,
                        1.0,
                        price,
                        "time_stop_exit",
                        now,
                        f"持仓 {held_minutes:.0f} 分钟当前报告缺失，按最后可用价时间退出",
                    )
                )
            else:
                position["last_update_note"] = "当前报告缺失，保留观察"
                still_open.append(position)
                continue
            if to_float(position.get("remaining_fraction")) <= 0:
                position["closed_at"] = now
                position["status"] = "closed"
                state.setdefault("closed_positions", []).append(position)
            else:
                still_open.append(position)
            continue
        price = row_price(row)
        if price <= 0:
            if held_minutes >= SETTINGS["time_stop_minutes"]:
                price = to_float(position.get("last_price_usd") or position.get("entry_price_usd"))
                events.append(
                    sell_fraction(
                        state,
                        position,
                        1.0,
                        price,
                        "time_stop_exit",
                        now,
                        f"持仓 {held_minutes:.0f} 分钟当前价格不可用，按最后可用价时间退出",
                    )
                )
            else:
                position["last_update_note"] = "当前价格不可用，保留观察"
                still_open.append(position)
                continue
            if to_float(position.get("remaining_fraction")) <= 0:
                position["closed_at"] = now
                position["status"] = "closed"
                state.setdefault("closed_positions", []).append(position)
            else:
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
            events.append(sell_fraction(state, position, 1.0, price, "stop_loss", now, f"首次发现试探跌破 -{stop_pct:.0f}%"))
        elif not position.get("tp1_hit") and ret >= SETTINGS["tp1_return_pct"]:
            events.append(sell_fraction(state, position, SETTINGS["tp1_sell_fraction"], price, "take_profit_1", now, "首次发现试探达到 TP1，先收大部分本金"))
            position["tp1_hit"] = True
        elif not position.get("tp2_hit") and ret >= SETTINGS["tp2_return_pct"]:
            events.append(sell_fraction(state, position, SETTINGS["tp2_sell_fraction"], price, "take_profit_2", now, "首次发现试探达到 TP2，继续减仓"))
            position["tp2_hit"] = True
        elif position.get("tp1_hit") and drawdown_from_high <= -SETTINGS["runner_trail_drawdown_pct"]:
            events.append(sell_fraction(state, position, 1.0, price, "runner_trailing_exit", now, f"尾仓从高点回撤 {abs(drawdown_from_high):.1f}%"))
        elif held_minutes >= SETTINGS["time_stop_minutes"]:
            events.append(sell_fraction(state, position, 1.0, price, "time_stop_exit", now, f"持仓 {held_minutes:.0f} 分钟未继续走强，时间退出"))

        if to_float(position.get("remaining_fraction")) > 0:
            still_open.append(position)
        else:
            position["closed_at"] = now
            position["status"] = "closed"
            state.setdefault("closed_positions", []).append(position)
    state["open_positions"] = still_open
    return events


def daily_open_count(state: dict[str, Any], now: str) -> int:
    today = day_key(now)
    return sum(
        1
        for event in state.get("events") or []
        if event.get("type") == "open" and day_key(event.get("time")) == today
    )


def reentry_blocked_keys(state: dict[str, Any], now: str) -> set[str]:
    current = parse_iso(now)
    blocked: set[str] = set()
    for position in state.get("closed_positions") or []:
        key = norm_text(position.get("key"))
        closed_at = parse_iso(position.get("closed_at"))
        if not key or not current or not closed_at:
            continue
        if (current - closed_at).total_seconds() / 60.0 <= SETTINGS["reentry_cooldown_minutes"]:
            blocked.add(key)
    return blocked


def build_candidates(
    report: dict[str, Any],
    replay_history: dict[str, Any],
    now: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    row_by_key: dict[str, dict[str, Any]] = {}
    for row in extract_rows(report):
        key = token_key(row)
        if not key:
            continue
        existing = row_by_key.get(key)
        if existing and row_mcap(existing) >= row_mcap(row):
            continue
        row_by_key[key] = row

    candidates: list[dict[str, Any]] = []
    for row in row_by_key.values():
        history_row = replay_row_for(row, replay_history)
        classification = classify_candidate(row, history_row, now)
        candidates.append({"row": row, "history": history_row, "classification": classification})
    candidates.sort(
        key=lambda item: (
            not item["classification"]["eligible"],
            -to_float(item["classification"]["score"]),
            to_float(item["classification"]["markup_from_first"]),
            norm_text(item["row"].get("symbol")),
        )
    )
    return candidates, row_by_key


def open_positions(
    state: dict[str, Any],
    candidates: list[dict[str, Any]],
    row_by_key: dict[str, dict[str, Any]],
    now: str,
    blocked_keys: set[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    open_keys = {norm_text(pos.get("key")) for pos in state.get("open_positions") or []}
    slots = max(0, int(SETTINGS["max_open_positions"]) - len(open_keys))
    remaining_daily = max(0, int(SETTINGS["max_new_entries_per_day"]) - daily_open_count(state, now))
    new_limit = min(slots, int(SETTINGS["max_new_entries_per_run"]), remaining_daily)
    for item in candidates:
        if len(events) >= new_limit:
            break
        classification = item["classification"]
        if not classification["eligible"]:
            continue
        key = classification["key"]
        if key in open_keys or key in blocked_keys:
            continue
        row = row_by_key[key]
        price = row_price(row)
        size = size_for(classification, to_float(state.get("cash_usd")))
        if size < SETTINGS["min_position_usd"]:
            break
        stop_pct = stop_pct_for(classification)
        state["cash_usd"] = round(to_float(state.get("cash_usd")) - size, 4)
        position = {
            "status": "open",
            "strategy": f"{classification.get('execution_arm') or 'first_discovery'}_probe",
            "key": key,
            "symbol": norm_text(row.get("symbol") or row.get("name") or key),
            "chain": classification.get("chain") or norm_text(row.get("chain") or row.get("network") or "bsc").lower(),
            "contract_address": norm_text(row.get("contract_address") or row.get("token_address") or row.get("address")),
            "entry_time": now,
            "first_seen_at": classification.get("first_seen_at"),
            "entry_delay_minutes": classification.get("entry_delay_minutes"),
            "first_mcap_usd": classification.get("first_mcap_usd"),
            "entry_mcap_usd": row_mcap(row),
            "entry_price_usd": price,
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
            "score": classification["score"],
            "bucket": classification["bucket"],
            "execution_arm": classification.get("execution_arm") or "first_discovery",
            "route_label": classification.get("route_label") or "首次发现小仓",
            "markup_from_first": classification["markup_from_first"],
            "rationale": classification["reasons"],
            "url": row.get("url") or row.get("dex_url") or "",
        }
        state.setdefault("open_positions", []).append(position)
        open_keys.add(key)
        events.append(
            {
                "type": "open",
                "time": now,
                "strategy": f"{classification.get('execution_arm') or 'first_discovery'}_probe",
                "key": key,
                "symbol": position["symbol"],
                "price_usd": price,
                "size_usd": size,
                "margin_usd": size,
                "first_mcap_usd": position["first_mcap_usd"],
                "entry_mcap_usd": position["entry_mcap_usd"],
                "entry_delay_minutes": position["entry_delay_minutes"],
                "stop_price_usd": position["stop_price_usd"],
                "tp1_price_usd": position["tp1_price_usd"],
                "tp2_price_usd": position["tp2_price_usd"],
                "score": position["score"],
                "bucket": position["bucket"],
                "execution_arm": position.get("execution_arm") or classification.get("execution_arm") or "first_discovery",
                "route_label": position.get("route_label") or classification.get("route_label") or "首次发现小仓",
                "reason": "首次发现纸面试探：新发现窗口 + 优化回测参数 + 低市值赔率",
                "profile": SETTINGS["profile"],
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


def run_paper_once(
    report: dict[str, Any],
    replay_history: dict[str, Any],
    state: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    candidates, row_by_key = build_candidates(report, replay_history, now)
    events = update_positions(state, row_by_key, now)
    exit_keys = {norm_text(event.get("key")) for event in events if event.get("type") != "open"}
    blocked = reentry_blocked_keys(state, now) | exit_keys
    events.extend(open_positions(state, candidates, row_by_key, now, blocked))
    state["last_updated_at"] = now
    state.setdefault("events", []).extend(events)
    state["last_summary"] = account_summary(state, row_by_key)
    state["last_candidate_audit"] = [
        {
            "symbol": item["row"].get("symbol"),
            "key": item["classification"].get("key"),
            "eligible": item["classification"].get("eligible"),
            "score": item["classification"].get("score"),
            "bucket": item["classification"].get("bucket"),
            "execution_arm": item["classification"].get("execution_arm"),
            "route_label": item["classification"].get("route_label"),
            "first_mcap_usd": item["classification"].get("first_mcap_usd"),
            "current_mcap_usd": item["classification"].get("current_mcap_usd"),
            "entry_delay_minutes": item["classification"].get("entry_delay_minutes"),
            "markup_from_first": item["classification"].get("markup_from_first"),
            "reject_reason": item["classification"].get("reject_reason"),
        }
        for item in candidates[:25]
    ]
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
        "# Alpha First Discovery Paper",
        "",
        f"- generated_at: {now}",
        f"- equity: {money(summary.get('equity_usd'))}",
        f"- cash: {money(summary.get('cash_usd'))}",
        f"- open_exposure: {money(summary.get('open_exposure_usd'))}",
        f"- realized_pnl: {money(summary.get('realized_pnl_usd'))}",
        f"- unrealized_pnl: {money(summary.get('unrealized_pnl_usd'))}",
        f"- valuation_status: {summary.get('valuation_status') or 'unknown'}",
        f"- stale_positions: {summary.get('stale_positions', 'unknown')}",
        f"- unpriced_positions: {summary.get('unpriced_positions', 'unknown')}",
        f"- profile: {SETTINGS['profile']}",
        f"- max_entry_delay: {SETTINGS['max_entry_delay_minutes']:.0f}m",
        "",
        "## Events",
    ]
    if events:
        for event in events:
            if event["type"] == "open":
                lines.append(
                    f"- OPEN {event['symbol']} size={money(event['size_usd'])} "
                    f"first={money(event['first_mcap_usd'])} entry_mcap={money(event['entry_mcap_usd'])} "
                    f"delay={event['entry_delay_minutes']:.0f}m SL={event['stop_price_usd']:.12g} "
                    f"TP1={event['tp1_price_usd']:.12g}"
                )
            else:
                lines.append(
                    f"- {event['type'].upper()} {event['symbol']} return={pct(event['return_pct'])} "
                    f"cost={money(event.get('trading_cost_usd'))} pnl={money(event['pnl_usd'])} reason={event['reason']}"
                )
    else:
        lines.append("- no new event")

    lines.extend(["", "## Open Positions"])
    if state.get("open_positions"):
        for pos in state["open_positions"]:
            lines.append(
                f"- {pos['symbol']} bucket={pos['bucket']} size={money(pos['size_usd'])} "
                f"remaining={pct(to_float(pos['remaining_fraction']) * 100)} "
                f"entry_mcap={money(pos['entry_mcap_usd'])} ret={pct(pos.get('last_return_pct'))} "
                f"SL={pos['stop_price_usd']:.12g} TP1={pos['tp1_price_usd']:.12g} TP2={pos['tp2_price_usd']:.12g}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Top Candidate Audit"])
    for item in candidates[:10]:
        row = item["row"]
        c = item["classification"]
        label = "PASS" if c["eligible"] else "SKIP"
        reason = c["reject_reason"] or "eligible"
        lines.append(
            f"- {label} {row.get('symbol')} score={c['score']:.1f} bucket={c['bucket']} "
            f"first={money(c['first_mcap_usd'])} current={money(c['current_mcap_usd'])} "
            f"delay={c['entry_delay_minutes']:.0f}m markup={c['markup_from_first']:.2f}x reason={reason}"
        )
    lines.append("")
    return "\n".join(lines)


def run_from_paths(
    report_path: Path,
    replay_history_path: Path,
    state_path: Path,
    markdown_path: Path,
    events_path: Path,
) -> dict[str, Any]:
    now = now_iso()
    report = load_json(report_path, {})
    replay_history = load_json(replay_history_path, {})
    state = load_state(state_path)
    result = run_paper_once(report, replay_history, state, now)
    save_json(state_path, result["state"])
    markdown_path.write_text(render_markdown(result, now), encoding="utf-8")
    append_events(events_path, result["events"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run first-discovery paper simulation from local Alpha radar data.")
    parser.add_argument("--profile", choices=["strict-45m", "shadow-180m"], default="strict-45m")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--replay-history", type=Path, default=DEFAULT_REPLAY_HISTORY_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN_PATH)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    args = parser.parse_args()
    apply_profile(args.profile)
    if args.profile == "shadow-180m":
        if args.state == DEFAULT_STATE_PATH:
            args.state = SHADOW_STATE_PATH
        if args.markdown == DEFAULT_MARKDOWN_PATH:
            args.markdown = SHADOW_MARKDOWN_PATH
        if args.events == DEFAULT_EVENTS_PATH:
            args.events = SHADOW_EVENTS_PATH

    while True:
        result = run_from_paths(args.report, args.replay_history, args.state, args.markdown, args.events)
        summary = result["state"].get("last_summary") or {}
        print(
            f"{now_iso()} events={len(result['events'])} "
            f"open={len(result['state'].get('open_positions') or [])} "
            f"equity={summary.get('equity_usd')}"
        )
        if args.interval_seconds <= 0:
            break
        time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
