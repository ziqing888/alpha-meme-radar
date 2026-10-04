#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from alpha_replay import row_key
from alpha_smart_money_evidence import FRESH_SECONDS, TIME_FIELDS, assess_smart_money, parse_timestamp


STRONG_CONVICTION = 82.0
CANDIDATE_CONVICTION = 68.0
OBSERVATION_LIMIT = 24
SEED_MIN_MCAP = 5_000.0
SEED_MAX_MCAP = 30_000.0
SEED_MAX_AGE_HOURS = 2.0
EARLY_MAX_MCAP = 100_000.0
EARLY_MAX_AGE_HOURS = 6.0
EARLY_CONFIRMATIONS = 2
DISCOVERY_ALERT_CONVICTION = 78.0
MIN_EVIDENCE_INTERVAL_SECONDS = 60
CONFIRMED_ALERT_MAX_MCAP = 500_000.0
CONFIRMED_ALERT_MAX_AGE_HOURS = 6.0
MAX_PICK_DRAWDOWN_PCT = 45.0
MAX_RECENT_DROP_H1_PCT = -25.0
MARKET_RECOVERY_RATIO = 0.72
STALE_CANDIDATE_HOURS = 24.0
MAX_STORED_CANDIDATES = 5_000
GOLD_ACHIEVED_MIN_PEAK_MCAP = 1_000_000.0
GOLD_ACHIEVED_MIN_MULTIPLE = 10.0
MATURE_GOLD_MCAP = 10_000_000.0
MAX_TOP10_HOLDER_PCT = 65.0
MAX_SINGLE_HOLDER_PCT = 25.0
MIN_CONFIRMATION_SOURCE_COUNT = 2
CORE_CONFIRMATION_SOURCES = {"GMGN", "DEBOT", "OKX信号"}
ONCHAIN_FIRST_LAYER_TOKENS = (
    "bsc_onchain",
    "bsc-onchain",
    "bnbchain",
    "bnb chain",
    "pancake",
    "pancakeswap",
    "fourmeme",
    "four.meme",
    "four meme",
    "flap",
    "butterfly",
    "pump",
    "pump.fun",
    "pumpfun",
    "raydium",
    "orca",
    "helius",
    "quicknode",
    "triton",
    "jupiter",
)
SCREENING_SOURCE_ALIASES = (
    ("OKX", ("okx",)),
    ("GMGN", ("gmgn",)),
    ("DEBOT", ("debot",)),
    ("Noxa", ("noxa",)),
    ("985", ("985",)),
    ("听风", ("wind", "tingfeng", "听风")),
    ("Proficy", ("proficy",)),
    ("DS", ("ds", "dexscreener", "profile", "boost")),
    ("Birdeye", ("birdeye",)),
    ("Mobula", ("mobula",)),
)
BSC_CHAIN_ALIASES = {
    "56",
    "bsc",
    "bnb",
    "bnbchain",
    "bnb chain",
    "binance smart chain",
    "binance-smart-chain",
}


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalized_chain_value(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def row_chain_values(row: dict[str, Any]) -> list[str]:
    keys = ("chain", "chain_id", "chainId", "network", "blockchain", "chainIndex")
    return [normalized_chain_value(row.get(key)) for key in keys if str(row.get(key) or "").strip()]


def is_bsc_row(row: dict[str, Any]) -> bool:
    values = row_chain_values(row)
    if any(value in BSC_CHAIN_ALIASES for value in values):
        return True
    url_text = " ".join(str(row.get(key) or "") for key in ("url", "dex_url", "pair_url")).lower()
    return any(marker in url_text for marker in ("/bsc/", "chain=bsc", "bnbchain", "pancakeswap"))


def scoped_rows(rows: list[dict[str, Any]], chain_scope: str | None) -> list[dict[str, Any]]:
    scope = str(chain_scope or "").strip().lower()
    if scope in {"bsc", "bnb", "56", "bnbchain"}:
        return [row for row in rows if is_bsc_row(row)]
    return rows


def parse_dt(value: Any) -> datetime | None:
    return parse_timestamp(value)


def evidence_progress(row: dict[str, Any], previous: dict[str, Any], now_iso: str) -> dict[str, Any]:
    evidence = row.get("smart_money_evidence") or {}
    has_quote = any(field in row for field in ("quote_status", "quote_observed_at", "quote_fingerprint"))
    quote_status = str(row.get("quote_status") or "").strip().lower()
    quote_blocks_monitor_confirmation = has_quote and quote_status not in {"unavailable", "not_available"}
    stamps = [parse_dt(row.get("quote_observed_at"))] if quote_blocks_monitor_confirmation else [
        parse_dt(row.get(field)) for field in TIME_FIELDS
    ]
    if not quote_blocks_monitor_confirmation:
        stamps.append(parse_dt(evidence.get("latest_evidence_at")))
    now = parse_dt(now_iso)
    stamp = max((value for value in stamps if value and now and value <= now), default=None)
    fresh = bool(stamp and now and 0 <= (now - stamp).total_seconds() <= FRESH_SECONDS)
    if quote_blocks_monitor_confirmation:
        fresh = fresh and row.get("quote_status") == "fresh" and bool(row.get("quote_fingerprint"))
    # Report generation time, age, and derived scores cannot manufacture evidence.
    facts = {field: row.get(field) for field in (
        "mcap", "market_cap", "price_usd", "liquidity", "smart_money", "kol",
        "top10_holder_pct", "max_holder_pct", "change_h1", "risk_flags", "gmgn_risk_flags",
    )}
    facts["evidence"] = evidence.get("fingerprint")
    facts["quote_fingerprint"] = row.get("quote_fingerprint")
    facts["pair_address"] = row.get("pair_address")
    facts["observed_at"] = stamp.isoformat() if stamp else None
    fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()
    last_stamp = parse_dt(previous.get("last_counted_evidence_at"))
    last_count = parse_dt(previous.get("last_evidence_counted_at"))
    advanced = bool(fresh and (not last_stamp or (
        (stamp - last_stamp).total_seconds() >= MIN_EVIDENCE_INTERVAL_SECONDS
        and last_count and (now - last_count).total_seconds() >= MIN_EVIDENCE_INTERVAL_SECONDS
        and fingerprint != previous.get("last_counted_evidence_fingerprint")
        and (
            not quote_blocks_monitor_confirmation
            or row.get("quote_fingerprint") != previous.get("last_counted_quote_fingerprint")
        )
    )))
    return {"fresh": fresh, "advanced": advanced, "timestamp": stamp.isoformat() if stamp else None,
            "fingerprint": fingerprint}


def hours_between(start_iso: Any, end_iso: Any) -> float:
    start = parse_dt(start_iso)
    end = parse_dt(end_iso)
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _open_watch_state(path: Path):
    if os.name != "nt":
        return path.open(encoding="utf-8")
    import _winapi
    import msvcrt

    # Share read/write/delete so another process can atomically replace this file.
    handle = _winapi.CreateFile(str(path), _winapi.GENERIC_READ, 0x1 | 0x2 | 0x4,
                               0, _winapi.OPEN_EXISTING, 0x80, 0)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, encoding="utf-8")


def load_watch_state(path: Path) -> dict[str, Any]:
    path = Path(path)
    for attempt in range(5):
        try:
            with _open_watch_state(path) as handle:
                payload = json.load(handle)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))
    return payload if isinstance(payload, dict) else {}


def write_watch_state(path: Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # Readers and Windows file scanners can briefly hold replacement handles.
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(min(0.05 * (attempt + 1), 0.2))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def row_identity(row: dict[str, Any]) -> str:
    key = row_key(row)
    return key if key.strip(":") else str(row.get("symbol") or row.get("name") or "").strip().lower()


def candidate_status(row: dict[str, Any], strong_scan_count: int, min_confirmations: int,
                     confirmation_ok: bool | None = None) -> str:
    conviction = to_float(row.get("gold_dog_conviction_score"))
    stage = ticket_stage(row)
    source_ok = bool(confirmation_source_check(row).get("ok")) if confirmation_ok is None else confirmation_ok
    if stage == "micro":
        return "watch"
    if stage == "late":
        return "expired" if conviction >= CANDIDATE_CONVICTION else "watch"
    if stage == "seed" and conviction >= CANDIDATE_CONVICTION:
        return "seed_pool"
    if stage == "early" and conviction >= STRONG_CONVICTION and strong_scan_count >= EARLY_CONFIRMATIONS and source_ok:
        return "early_candidate"
    if conviction >= STRONG_CONVICTION and strong_scan_count >= min_confirmations and source_ok:
        return "strong_candidate"
    if conviction >= CANDIDATE_CONVICTION:
        return "candidate"
    return "watch"


def ticket_stage(row: dict[str, Any]) -> str:
    mcap = to_float(row.get("mcap") or row.get("market_cap"))
    age = to_float(row.get("pair_age_hours"))
    conviction = to_float(row.get("gold_dog_conviction_score"))
    if 0 < mcap < SEED_MIN_MCAP:
        return "micro"
    if SEED_MIN_MCAP <= mcap < SEED_MAX_MCAP and 0 < age <= SEED_MAX_AGE_HOURS:
        return "seed"
    if SEED_MAX_MCAP <= mcap < EARLY_MAX_MCAP and 0 < age <= EARLY_MAX_AGE_HOURS:
        return "early"
    if EARLY_MAX_MCAP <= mcap <= CONFIRMED_ALERT_MAX_MCAP and 0 < age <= CONFIRMED_ALERT_MAX_AGE_HOURS:
        return "confirmed"
    return "late"


def is_alert_eligible(row: dict[str, Any]) -> bool:
    return ticket_stage(row) == "confirmed"


def normalized_sources(row: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for field in ("source_labels", "sources"):
        value = row.get(field) or []
        values.extend(value if isinstance(value, list) else [value])
    values.extend([row.get("source"), row.get("source_family"), row.get("holder_source")])
    return {str(value).strip().lower() for value in values if str(value or "").strip()}


def confirmation_source_values(row: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for field in ("source_groups", "sources", "source_names"):
        value = row.get(field) or []
        values.extend(value if isinstance(value, list) else [value])
    values.extend([row.get("source"), row.get("source_family"), row.get("holder_source")])
    for value in row.get("source_labels") or []:
        label = str(value or "").strip()
        if label.lower() in {"ds", "alpha_ai", "alpha ai"}:
            continue
        values.append(label)
    return {str(value).strip().lower() for value in values if str(value or "").strip()}


def screening_sources(row: dict[str, Any]) -> list[str]:
    values = confirmation_source_values(row)
    found: list[str] = []
    for label, aliases in SCREENING_SOURCE_ALIASES:
        if any(alias in value for alias in aliases for value in values):
            found.append(label)
    return found


def confirmation_source_check(row: dict[str, Any]) -> dict[str, Any]:
    screeners = screening_sources(row)
    normalized = confirmation_source_values(row)
    okx_signal_present = any("okx_signal" in source for source in normalized)
    has_core = any(source in CORE_CONFIRMATION_SOURCES for source in screeners) or okx_signal_present
    sources = list(screeners)
    if discovery_layer(row) == "onchain_first_layer":
        sources.insert(0, "ONCHAIN")
    unique_sources = list(dict.fromkeys(sources))
    enough_sources = len(unique_sources) >= MIN_CONFIRMATION_SOURCE_COUNT
    ok = enough_sources and has_core
    if ok:
        reason = "双源确认已满足：" + "+".join(unique_sources[:4])
    elif not enough_sources and not has_core:
        reason = "还缺双源确认，且缺 GMGN/DeBot 核心源"
    elif not enough_sources:
        reason = "还缺第二个独立来源确认"
    else:
        reason = "还缺 GMGN/DeBot 核心源确认"
    return {
        "ok": ok,
        "sources": unique_sources,
        "core_sources": [source for source in screeners if source in CORE_CONFIRMATION_SOURCES] + (["OKX信号"] if okx_signal_present else []),
        "reason": reason,
    }


def quote_waiting_for_confirmation(row: dict[str, Any]) -> bool:
    has_quote = any(field in row for field in ("quote_status", "quote_observed_at", "quote_fingerprint"))
    quote_status = str(row.get("quote_status") or "").strip().lower()
    return has_quote and quote_status in {"unavailable", "not_available"}


def discovery_layer(row: dict[str, Any]) -> str:
    values = normalized_sources(row)
    if any(token in value for token in ONCHAIN_FIRST_LAYER_TOKENS for value in values):
        return "onchain_first_layer"
    return "aggregator_second_screen"


def pipeline_role(row: dict[str, Any]) -> str:
    return "first_layer" if discovery_layer(row) == "onchain_first_layer" else "second_screen"


def invalidation_reason(row: dict[str, Any], state_row: dict[str, Any]) -> str:
    top10_holder_pct = to_float(row.get("top10_holder_pct"))
    if top10_holder_pct >= MAX_TOP10_HOLDER_PCT:
        return f"Top10 持仓 {top10_holder_pct:.1f}% ，筹码过度集中"

    max_holder_pct = to_float(row.get("max_holder_pct"))
    if max_holder_pct >= MAX_SINGLE_HOLDER_PCT:
        return f"最大钱包持仓 {max_holder_pct:.1f}% ，单钱包控盘风险"

    risk_flags = row.get("gmgn_risk_flags") or row.get("risk_flags") or []
    if isinstance(risk_flags, list) and risk_flags:
        return "风险标签：" + " / ".join(str(flag) for flag in risk_flags[:3])

    change_h1 = to_float(row.get("change_h1") or row.get("price_change_h1") or row.get("price_change_1h_pct"))
    if change_h1 <= MAX_RECENT_DROP_H1_PCT:
        return f"1h 跌幅 {change_h1:.1f}% ，短线承接失败"

    return ""


def high_risk_focus_reason(row: dict[str, Any]) -> str:
    """Create a separate actionable alert for fast/risky tokens, even when not buyable."""
    flags = row.get("gmgn_risk_flags") or row.get("risk_flags") or []
    flags = [str(flag) for flag in flags] if isinstance(flags, list) else []
    reasons: list[str] = []
    for prefix, label, threshold in (("bundler_", "bundler", 20.0), ("sniper_", "sniper", 35.0), ("rug_", "rug", 20.0)):
        for flag in flags:
            match = re.search(rf"{re.escape(prefix)}([0-9]+(?:\.[0-9]+)?)", flag.lower())
            if match and to_float(match.group(1)) >= threshold:
                reasons.append(f"{label} {to_float(match.group(1)):.1f}%")
    if not reasons:
        return ""
    change_h1 = to_float(row.get("change_h1") or row.get("price_change_h1") or row.get("price_change_1h_pct"))
    if change_h1 >= 100.0:
        reasons.append(f"1h 涨幅 {change_h1:.1f}%")
    return "高风险重点提醒：" + " / ".join(reasons[:4]) if reasons else ""


def market_deterioration_reason(row: dict[str, Any], state_row: dict[str, Any]) -> str:
    """Return a reversible market warning, separate from structural invalidation."""
    current_mcap = to_float(row.get("mcap") or row.get("market_cap"))
    max_seen_mcap = to_float(state_row.get("max_seen_mcap"))
    if current_mcap > 0 and max_seen_mcap > 0:
        drawdown_pct = (max_seen_mcap - current_mcap) / max_seen_mcap * 100
        if drawdown_pct >= MAX_PICK_DRAWDOWN_PCT:
            return f"从监控最高市值回撤 {drawdown_pct:.1f}% ，进入回撤观察"
    return ""


def is_market_deterioration_reason(reason: Any) -> bool:
    text = str(reason or "")
    return text.startswith("从监控最高市值回撤") or text.startswith("1h 跌幅")


def market_recovered(previous: dict[str, Any], current_mcap: float) -> bool:
    if previous.get("status") not in {"invalidated", "pullback"}:
        return False
    previous_reason = previous.get("pullback_reason") or previous.get("invalid_reason")
    if not is_market_deterioration_reason(previous_reason):
        return False
    previous_peak = to_float(previous.get("max_seen_mcap"))
    first_confirmed_mcap = to_float(previous.get("first_confirmed_mcap") or previous.get("alert_mcap"))
    if previous_peak <= 0 or current_mcap <= 0:
        return False
    return current_mcap >= previous_peak * MARKET_RECOVERY_RATIO and (
        first_confirmed_mcap <= 0 or current_mcap >= first_confirmed_mcap * 0.9
    )


def watch_v2_tier(state_row: dict[str, Any]) -> str:
    if state_row.get("first_confirmation_active"):
        return "confirmed"
    if state_row.get("status") == "early_candidate":
        return "early"
    if (
        state_row.get("status") == "candidate"
        and state_row.get("ticket_stage") in {"early", "confirmed"}
        and state_row.get("confirmation_evidence_ok", state_row.get("source_confirmation_ok"))
    ):
        return "early"
    if state_row.get("status") == "seed_pool":
        return "discovered"
    if (
        state_row.get("status") == "candidate"
        and state_row.get("ticket_stage") in {"seed", "early", "confirmed"}
    ):
        return "discovered"
    return "review"


def has_first_confirmation(state_row: dict[str, Any]) -> bool:
    return bool(
        state_row.get("first_confirmed_at")
        or state_row.get("alerted_at")
        or to_float(state_row.get("first_confirmed_mcap"))
        or to_float(state_row.get("alert_mcap"))
    )


def gold_capture_type(state_row: dict[str, Any]) -> str:
    if state_row.get("status") != "achieved_gold":
        return ""
    return "confirmed_runner" if has_first_confirmation(state_row) else "review_only"


def achieved_gold_reason(row: dict[str, Any], state_row: dict[str, Any]) -> str:
    current_mcap = to_float(row.get("mcap") or row.get("market_cap"))
    first_seen_mcap = to_float(state_row.get("first_seen_mcap"))
    max_seen_mcap = max(to_float(state_row.get("max_seen_mcap")), current_mcap)
    multiple = max_seen_mcap / first_seen_mcap if first_seen_mcap > 0 and max_seen_mcap > 0 else 0.0
    if max_seen_mcap >= GOLD_ACHIEVED_MIN_PEAK_MCAP and multiple >= GOLD_ACHIEVED_MIN_MULTIPLE:
        return f"已成金狗：监控峰值市值 {max_seen_mcap:.0f}，从首次监控约 {multiple:.1f} 倍"
    if current_mcap >= MATURE_GOLD_MCAP:
        return f"已成金狗：当前市值 {current_mcap:.0f}，已经过早期上车窗口"
    return ""


def entry_reason(row: dict[str, Any], state_row: dict[str, Any]) -> str:
    if state_row.get("status") == "achieved_gold":
        return state_row.get("achieved_gold_reason") or "已成金狗，转复盘"
    if state_row.get("status") == "invalidated":
        return state_row.get("invalid_reason") or "已失效"
    if state_row.get("status") == "pullback":
        return state_row.get("pullback_reason") or "进入回撤观察，等待收复"
    reasons = row.get("gold_dog_rationale") or []
    if reasons:
        return " / ".join(str(reason) for reason in reasons[:3])
    sources = state_row.get("source_confirmation_sources") or row.get("watch_source_confirmation_sources") or []
    if sources:
        return "命中 " + " + ".join(str(source) for source in sources[:4])
    if state_row.get("reason") and state_row.get("status") in {"expired", "review"}:
        return str(state_row.get("reason"))
    return "进入金狗候选池观察"


def alert_gate_reason(row: dict[str, Any], state_row: dict[str, Any], min_confirmations: int = MIN_CONFIRMATION_SOURCE_COUNT) -> str:
    if state_row.get("confirmation_status") in {"blocked_risk", "blocked_sell_dominance"}:
        return state_row.get("confirmation_reason") or "当前不满足确认条件，暂不升级"
    if state_row.get("first_confirmation_active") or state_row.get("first_confirmed_at") or state_row.get("alerted_at"):
        return "已进确认层"
    if state_row.get("status") == "achieved_gold":
        return state_row.get("achieved_gold_reason") or "已成金狗，转复盘"
    if state_row.get("status") == "invalidated":
        return state_row.get("invalid_reason") or "已失效"
    if state_row.get("status") == "pullback":
        return state_row.get("pullback_reason") or "进入回撤观察，等待收复"
    if state_row.get("status") == "expired":
        return state_row.get("invalid_reason") or "窗口已过"
    if not state_row.get("alert_eligible"):
        stage = state_row.get("ticket_stage") or ticket_stage(row)
        if stage in {"micro", "seed"}:
            return "发现层，不强提醒"
        if stage == "early":
            return "早鸟层，等确认"
        if stage == "late":
            return "偏晚/复盘，不提醒"
        return "不满足提醒门槛"
    if not state_row.get("confirmation_evidence_ok", state_row.get("source_confirmation_ok")):
        return state_row.get("source_confirmation_reason") or "等二源确认"
    missing = max(0, min_confirmations - int(state_row.get("strong_scan_count") or 0))
    if missing:
        return f"还差 {missing} 次连续确认"
    return "等待首次确认"


def public_pick(row: dict[str, Any], state_row: dict[str, Any]) -> dict[str, Any]:
    first_confirmed_mcap = state_row.get("first_confirmed_mcap") or state_row.get("alert_mcap")
    first_confirmed_at = state_row.get("first_confirmed_at") or state_row.get("alerted_at")
    return {
        **row,
        "confirmation_status": state_row.get("confirmation_status"),
        "confirmation_reason": state_row.get("confirmation_reason"),
        "confirmation_basis": state_row.get("confirmation_basis"),
        "smart_money_confirmation": state_row.get("smart_money_confirmation"),
        "watch_status": state_row.get("status"),
        "watch_ticket_stage": state_row.get("ticket_stage"),
        "watch_alert_eligible": state_row.get("alert_eligible"),
        "watch_alerted_at": first_confirmed_at,
        "watch_alert_mcap": first_confirmed_mcap,
        "watch_first_confirmed_at": first_confirmed_at,
        "watch_first_confirmed_mcap": first_confirmed_mcap,
        "watch_first_confirmation_active": bool(state_row.get("first_confirmation_active")),
        "watch_v2_tier": state_row.get("watch_v2_tier") or watch_v2_tier(state_row),
        "watch_pipeline_role": state_row.get("pipeline_role") or pipeline_role(row),
        "watch_discovery_layer": state_row.get("discovery_layer") or discovery_layer(row),
        "watch_screening_sources": state_row.get("screening_sources") or screening_sources(row),
        "watch_source_confirmation_sources": state_row.get("source_confirmation_sources") or [],
        "watch_source_confirmation_ok": state_row.get("source_confirmation_ok"),
        "watch_source_confirmation_reason": state_row.get("source_confirmation_reason") or "",
        "watch_alert_conviction_score": state_row.get("alert_conviction_score"),
        "watch_scan_count": state_row.get("scan_count"),
        "watch_strong_scan_count": state_row.get("strong_scan_count"),
        "watch_first_seen_at": state_row.get("first_seen_at"),
        "watch_last_seen_at": state_row.get("last_seen_at"),
        "watch_first_seen_mcap": state_row.get("first_seen_mcap"),
        "watch_last_seen_mcap": state_row.get("last_seen_mcap"),
        "watch_max_seen_mcap": state_row.get("max_seen_mcap"),
        "watch_mcap_multiple_from_first": state_row.get("mcap_multiple_from_first"),
        "watch_best_conviction_score": state_row.get("best_conviction_score"),
        "watch_gold_capture_type": state_row.get("gold_capture_type") or gold_capture_type(state_row),
        "watch_achieved_gold_reason": state_row.get("achieved_gold_reason") or "",
        "watch_invalid_reason": state_row.get("invalid_reason") or "",
        "watch_pullback_reason": state_row.get("pullback_reason") or "",
        "watch_recovery_reason": state_row.get("recovery_reason") or "",
        "watch_reason": state_row.get("reason") or "",
        "watch_entry_reason": entry_reason(row, state_row),
        "watch_alert_gate_reason": alert_gate_reason(
            row,
            state_row,
            int(state_row.get("min_confirmations") or MIN_CONFIRMATION_SOURCE_COUNT),
        ),
    }


def rank_key(row: dict[str, Any], state_row: dict[str, Any]) -> tuple[float, float, float, float, float]:
    return (
        to_float(row.get("gold_dog_conviction_score")),
        to_float(row.get("gold_dog_score")),
        to_float(row.get("smart_kol_score") or row.get("smart_money")),
        to_float(row.get("holder_quality_score")),
        -to_float(row.get("pair_age_hours")),
    )


def pending_reason(state_row: dict[str, Any], min_confirmations: int) -> str:
    missing = max(0, min_confirmations - int(state_row.get("strong_scan_count") or 0))
    symbol = state_row.get("symbol") or "--"
    if state_row.get("confirmation_status") == "blocked_sell_dominance":
        return str(state_row.get("confirmation_reason"))
    if to_float(state_row.get("last_conviction_score")) >= STRONG_CONVICTION and not state_row.get("confirmation_evidence_ok", state_row.get("source_confirmation_ok")):
        return f"{symbol} 信号够强，但{state_row.get('source_confirmation_reason') or '还缺双源确认'}"
    if missing:
        return f"{symbol} 信号够强，但还差 {missing} 次确认"
    return f"{symbol} 已满足连续确认"


def build_alert(row: dict[str, Any], state_row: dict[str, Any], now_iso: str, alert_type: str = "confirmed") -> dict[str, Any]:
    reasons = row.get("gold_dog_rationale") or []
    market_cap = to_float(row.get("mcap") or row.get("market_cap"))
    first_confirmed_mcap = to_float(state_row.get("first_confirmed_mcap")) or market_cap
    first_confirmed_at = state_row.get("first_confirmed_at") or now_iso
    return {
        "alert_type": alert_type,
        "confirmation_status": state_row.get("confirmation_status"),
        "confirmation_reason": state_row.get("confirmation_reason"),
        "confirmation_basis": state_row.get("confirmation_basis"),
        "created_at": now_iso,
        "symbol": row.get("symbol") or row.get("name") or "",
        "chain": row.get("chain") or row.get("chain_id") or "",
        "contract_address": row.get("contract_address") or row.get("token_address") or "",
        "market_cap": market_cap,
        "mcap": market_cap,
        "first_confirmed_mcap": first_confirmed_mcap if alert_type == "confirmed" else None,
        "first_confirmed_at": first_confirmed_at if alert_type == "confirmed" else None,
        "gold_dog_conviction_score": to_float(row.get("gold_dog_conviction_score")),
        "rating": row.get("early_rating") or row.get("rating") or row.get("grade") or "",
        "change_h1": to_float(row.get("change_h1") or row.get("price_change_h1") or row.get("price_change_1h_pct")),
        "liquidity_usd": to_float(row.get("liquidity") or row.get("liquidity_usd")),
        "risk_flags": row.get("gmgn_risk_flags") or row.get("risk_flags") or [],
        "ticket_stage": state_row.get("ticket_stage"),
        "watch_scan_count": state_row.get("scan_count"),
        "watch_strong_scan_count": state_row.get("strong_scan_count"),
        "reason": " / ".join(reasons) if reasons else state_row.get("reason") or "连续强信号确认",
        "url": row.get("dex_url") or row.get("url") or "",
    }


def alert_key_from_alert(alert: dict[str, Any]) -> str:
    key = row_identity(alert)
    if key:
        return key
    return str(alert.get("symbol") or "").strip().lower()


def alert_metadata(alert: dict[str, Any]) -> dict[str, Any]:
    first_confirmed_mcap = to_float(alert.get("first_confirmed_mcap") or alert.get("mcap") or alert.get("market_cap"))
    first_confirmed_at = alert.get("first_confirmed_at") or alert.get("created_at")
    return {
        "alerted_at": first_confirmed_at,
        "alert_mcap": first_confirmed_mcap,
        "first_confirmed_at": first_confirmed_at,
        "first_confirmed_mcap": first_confirmed_mcap,
        "alert_conviction_score": to_float(alert.get("gold_dog_conviction_score")),
        "alert_strong_scan_count": alert.get("watch_strong_scan_count"),
    }


def first_confirmation_metadata(row: dict[str, Any], state_row: dict[str, Any], now_iso: str) -> dict[str, Any]:
    first_confirmed_mcap = to_float(state_row.get("first_confirmed_mcap")) or to_float(
        row.get("mcap") or row.get("market_cap")
    )
    first_confirmed_at = state_row.get("first_confirmed_at") or now_iso
    return {
        "first_confirmed_at": first_confirmed_at,
        "first_confirmed_mcap": first_confirmed_mcap,
        "alert_conviction_score": state_row.get("alert_conviction_score")
        or to_float(row.get("gold_dog_conviction_score")),
        "alert_strong_scan_count": state_row.get("alert_strong_scan_count")
        or state_row.get("strong_scan_count"),
    }


def update_watch_state(
    state: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    now_iso: str,
    *,
    min_confirmations: int = 3,
    chain_scope: str | None = None,
) -> dict[str, Any]:
    payload = dict(state or {})
    candidates = dict(payload.get("candidates") or {})
    alerts = list(payload.get("alerts") or [])
    if str(chain_scope or "").strip().lower() in {"bsc", "bnb", "56", "bnbchain"}:
        candidates = {key: item for key, item in candidates.items() if isinstance(item, dict) and is_bsc_row(item)}
        alerts = [alert for alert in alerts if isinstance(alert, dict) and is_bsc_row(alert)]
    rows = scoped_rows(rows, chain_scope)
    new_alerts: list[dict[str, Any]] = []
    confirmed_alerts = [alert for alert in alerts if isinstance(alert, dict) and alert.get("alert_type", "confirmed") == "confirmed"]
    alerted_keys = set(payload.get("alerted_keys") or [])
    if not alerted_keys:
        alerted_keys = {key for key in (alert_key_from_alert(alert) for alert in confirmed_alerts) if key}
    elif str(chain_scope or "").strip().lower() in {"bsc", "bnb", "56", "bnbchain"}:
        alerted_keys = {key for key in alerted_keys if key in {alert_key_from_alert(alert) for alert in alerts if isinstance(alert, dict)}}
    alerted_at_by_key = dict(payload.get("alerted_at_by_key") or {})
    alerted_at_by_key = {key: value for key, value in alerted_at_by_key.items() if key in alerted_keys}
    alert_meta_by_key = {
        key: alert_metadata(alert)
        for alert in confirmed_alerts
        if isinstance(alert, dict) and (key := alert_key_from_alert(alert))
    }
    latest_by_key: dict[str, dict[str, Any]] = {}

    for row in rows:
        key = row_identity(row)
        if not key or key in latest_by_key:
            continue
        restored_alert_meta = alert_meta_by_key.get(key) or {}
        latest_by_key[key] = row
        previous = dict(candidates.get(key) or {})
        progress = evidence_progress(row, previous, now_iso)
        quote_waiting = quote_waiting_for_confirmation(row)
        conviction = to_float(row.get("gold_dog_conviction_score"))
        current_mcap = to_float(row.get("mcap") or row.get("market_cap"))
        first_seen_mcap = to_float(previous.get("first_seen_mcap")) or current_mcap
        max_seen_mcap = max(to_float(previous.get("max_seen_mcap")), current_mcap)
        mcap_multiple = current_mcap / first_seen_mcap if first_seen_mcap > 0 and current_mcap > 0 else 0.0
        is_strong = conviction >= STRONG_CONVICTION
        source_check = confirmation_source_check(row)
        smart_check = assess_smart_money(row, now_iso)
        observations = list(previous.get("observations") or [])
        observations.append(
            {
                "seen_at": now_iso,
                "gold_dog_conviction_score": conviction,
                "gold_dog_score": to_float(row.get("gold_dog_score")),
                "mcap": current_mcap,
                "pair_age_hours": to_float(row.get("pair_age_hours")),
                "smart_money": to_float(row.get("smart_money")),
                "kol": to_float(row.get("kol")),
                "entry_level": row.get("entry_level") or "",
            }
        )
        observations = observations[-OBSERVATION_LIMIT:]
        scan_count = int(previous.get("scan_count") or 0) + 1
        previous_consecutive = int(previous.get("consecutive_strong_scan_count") or 0) if previous.get("last_counted_evidence_at") else 0
        if hours_between(previous.get("last_counted_evidence_at"), now_iso) * 3600 > FRESH_SECONDS:
            previous_consecutive = 0
        strong_scan_count = previous_consecutive + int(progress["advanced"]) if is_strong and progress["fresh"] and not quote_waiting else 0
        if smart_check["sell_dominance"] or quote_waiting:
            strong_scan_count = 0
        wallet_ready = bool(smart_check["buy_support"] and strong_scan_count >= 2
                            and previous.get("last_counted_quote_fingerprint"))
        confirmation_ok = bool((source_check.get("ok") or wallet_ready) and not smart_check["sell_dominance"] and not quote_waiting)
        basis = "wallet_evidence" if smart_check["buy_support"] else "platform_labels_unverified" if source_check.get("ok") else "none"
        lifetime_strong_scan_count = int(previous.get("lifetime_strong_scan_count") or previous.get("strong_scan_count") or 0) + (
            1 if is_strong and progress["advanced"] and not quote_waiting else 0
        )
        merged = {
            **previous,
            "key": key,
            "symbol": row.get("symbol") or previous.get("symbol") or "",
            "chain": row.get("chain") or row.get("chain_id") or previous.get("chain") or "",
            "contract_address": row.get("contract_address") or row.get("token_address") or previous.get("contract_address") or "",
            "first_seen_at": previous.get("first_seen_at") or now_iso,
            "last_seen_at": now_iso,
            "scan_count": scan_count,
            "strong_scan_count": strong_scan_count,
            "consecutive_strong_scan_count": strong_scan_count,
            "lifetime_strong_scan_count": lifetime_strong_scan_count,
            "last_conviction_score": conviction,
            "best_conviction_score": max(to_float(previous.get("best_conviction_score")), conviction),
            "first_seen_mcap": first_seen_mcap,
            "last_seen_mcap": current_mcap,
            "max_seen_mcap": max_seen_mcap,
            "mcap_multiple_from_first": round(mcap_multiple, 2) if mcap_multiple else 0.0,
            "last_entry_level": row.get("entry_level") or previous.get("last_entry_level") or "",
            "ticket_stage": ticket_stage(row),
            "alert_eligible": is_alert_eligible(row),
            "pipeline_role": pipeline_role(row),
            "discovery_layer": discovery_layer(row),
            "screening_sources": screening_sources(row),
            "source_confirmation_sources": source_check.get("sources") or [],
            "source_confirmation_ok": bool(source_check.get("ok")),
            "source_confirmation_reason": source_check.get("reason") or "",
            "confirmation_evidence_ok": confirmation_ok,
            "confirmation_basis": basis,
            "smart_money_confirmation": smart_check,
            "min_confirmations": min_confirmations,
            "alerted_at": previous.get("alerted_at") or restored_alert_meta.get("alerted_at"),
            "alert_mcap": previous.get("alert_mcap") or restored_alert_meta.get("alert_mcap"),
            "first_confirmed_at": previous.get("first_confirmed_at")
            or previous.get("alerted_at")
            or restored_alert_meta.get("first_confirmed_at")
            or restored_alert_meta.get("alerted_at"),
            "first_confirmed_mcap": previous.get("first_confirmed_mcap")
            or previous.get("alert_mcap")
            or restored_alert_meta.get("first_confirmed_mcap")
            or restored_alert_meta.get("alert_mcap"),
            "alert_conviction_score": previous.get("alert_conviction_score")
            or restored_alert_meta.get("alert_conviction_score"),
            "alert_strong_scan_count": previous.get("alert_strong_scan_count")
            or restored_alert_meta.get("alert_strong_scan_count"),
            "reason": " / ".join(row.get("gold_dog_rationale") or []) or previous.get("reason") or "",
            "observations": observations,
            "first_confirmation_active": False,
            "evidence_fresh": progress["fresh"],
            "evidence_advanced": progress["advanced"],
            "quote_waiting_for_confirmation": quote_waiting,
        }
        if progress["advanced"] and not quote_waiting:
            merged.update({"last_counted_evidence_at": progress["timestamp"],
                           "last_counted_evidence_fingerprint": progress["fingerprint"],
                           "last_counted_quote_fingerprint": row.get("quote_fingerprint"),
                           "last_evidence_counted_at": now_iso})
        achieved_reason = achieved_gold_reason(row, merged)
        invalid_reason = invalidation_reason(row, merged)
        market_reason = market_deterioration_reason(row, merged)
        recovered_from_pullback = market_recovered(previous, current_mcap)
        if achieved_reason:
            merged["status"] = "achieved_gold"
            merged["achieved_gold_reason"] = achieved_reason
            merged["invalid_reason"] = ""
            merged["pullback_reason"] = ""
            merged["reason"] = achieved_reason
        elif invalid_reason:
            merged["status"] = "invalidated"
            merged["strong_scan_count"] = 0
            merged["consecutive_strong_scan_count"] = 0
            merged["achieved_gold_reason"] = ""
            merged["pullback_reason"] = ""
            merged["invalid_reason"] = invalid_reason
            merged["reason"] = invalid_reason
        elif market_reason and not recovered_from_pullback:
            merged["status"] = "pullback"
            merged["strong_scan_count"] = 0
            merged["consecutive_strong_scan_count"] = 0
            merged["achieved_gold_reason"] = ""
            merged["invalid_reason"] = ""
            merged["pullback_reason"] = market_reason
            merged["reason"] = market_reason
        elif smart_check["sell_dominance"]:
            merged["status"] = "deteriorating"
            merged["achieved_gold_reason"] = ""
            merged["invalid_reason"] = ""
            merged["pullback_reason"] = ""
            merged["reason"] = smart_check["reason"]
        else:
            merged["achieved_gold_reason"] = ""
            merged["invalid_reason"] = ""
            merged["pullback_reason"] = ""
            merged["status"] = candidate_status(row, strong_scan_count, min_confirmations, confirmation_ok)
            if smart_check["buy_support"]:
                merged["reason"] = smart_check["reason"]
            elif is_strong and not source_check.get("ok"):
                merged["reason"] = str(source_check.get("reason") or "还缺双源确认")
            if recovered_from_pullback:
                merged["recovered_at"] = now_iso
                merged["recovery_reason"] = (
                    f"回撤修复：当前市值 {current_mcap:.0f}，已收复前高的 {current_mcap / max(to_float(previous.get('max_seen_mcap')), 1) * 100:.1f}%"
                )
                merged["reason"] = merged["recovery_reason"]
        if invalid_reason:
            merged["confirmation_status"] = "blocked_risk"
            merged["confirmation_reason"] = invalid_reason
            merged["confirmation_evidence_ok"] = False
        elif market_reason and not recovered_from_pullback:
            merged["confirmation_status"] = "blocked_pullback"
            merged["confirmation_reason"] = market_reason + "；等待重新收复，不把回撤直接判死。"
            merged["confirmation_evidence_ok"] = False
        elif smart_check["sell_dominance"]:
            merged["confirmation_status"] = "blocked_sell_dominance"
            merged["confirmation_reason"] = smart_check["reason"]
        elif quote_waiting or not progress["fresh"]:
            merged["confirmation_status"] = "waiting_fresh_market"
            merged["confirmation_reason"] = "等待最新行情；报价不可用或重复读取旧数据不计入确认次数。"
            merged["confirmation_evidence_ok"] = False
        elif merged["status"] in {"expired", "achieved_gold", "seed_pool", "watch"}:
            merged["confirmation_status"] = "outside_confirmation_gate"
            merged["confirmation_reason"] = merged["reason"] or "当前评分或入场窗口尚不满足确认条件。"
        elif basis != "none":
            confirmed_now = merged["status"] == "strong_candidate"
            merged["confirmation_status"] = ("confirmed_" if confirmed_now else "pending_") + basis
            detail = smart_check["reason"] if basis == "wallet_evidence" else "目前按平台来源交叉确认；尚无足够的逐笔买入证据，也未核实这些地址是否由不同人控制。"
            merged["confirmation_reason"] = f"{detail}有效行情确认已累计 {strong_scan_count} 次，门槛为 {min_confirmations} 次。"
        else:
            merged["confirmation_status"] = "insufficient_evidence"
            merged["confirmation_reason"] = smart_check["reason"] + " " + str(source_check.get("reason") or "")
        if (
            merged.get("status") == "strong_candidate"
            and merged.get("alert_eligible")
            and progress["advanced"]
            and not has_first_confirmation(merged)
        ):
            merged.update(first_confirmation_metadata(row, merged, now_iso))
        single_source_observation = len(source_check.get("sources") or []) == 1 or (
            not source_check.get("sources")
            and len({source.split("_")[0] for source in normalized_sources(row)}) == 1
        )
        multi_source_early_signal = (
            ticket_stage(row) in {"early", "confirmed"}
            and strong_scan_count >= EARLY_CONFIRMATIONS
            and source_check.get("ok")
        )
        if (
            progress["advanced"] and not quote_waiting and not invalid_reason and not achieved_reason and not smart_check["sell_dominance"]
            and conviction >= CANDIDATE_CONVICTION
            and ticket_stage(row) in {"seed", "early", "confirmed"}
            and (single_source_observation or multi_source_early_signal)
            and not previous.get("early_alerted_at") and not has_first_confirmation(merged)
        ):
            alert = build_alert(row, merged, now_iso, "early")
            alerts.append(alert)
            new_alerts.append(alert)
            merged["early_alerted_at"] = now_iso
        discovery_alert = (
            progress["advanced"] and not quote_waiting and not invalid_reason and not achieved_reason and not smart_check["sell_dominance"]
            and conviction >= DISCOVERY_ALERT_CONVICTION
            and ticket_stage(row) in {"seed", "early", "confirmed"}
            and (ticket_stage(row) == "seed" or not source_check.get("ok"))
            and not merged.get("early_alerted_at")
            and not previous.get("discovered_alerted_at")
            and not has_first_confirmation(merged)
        )
        if discovery_alert:
            alert = build_alert(row, merged, now_iso, "discovered")
            alert["reason"] = str(merged.get("reason") or source_check.get("reason") or "首次发现，等待第二核心源")
            alerts.append(alert)
            new_alerts.append(alert)
            merged["discovered_alerted_at"] = now_iso
        high_risk_reason = high_risk_focus_reason(row)
        if (
            high_risk_reason and progress["fresh"] and not achieved_reason
            and not previous.get("high_risk_alert_active")
        ):
            alert = build_alert(row, merged, now_iso, "high_risk")
            alert["reason"] = high_risk_reason
            alert["priority"] = "high"
            alerts.append(alert)
            new_alerts.append(alert)
            merged["high_risk_alert_active"] = True
        elif not high_risk_reason and progress["fresh"]:
            merged["high_risk_alert_active"] = False
        if (
            invalid_reason and merged.get("status") == "invalidated" and progress["fresh"]
            and not previous.get("invalidation_alert_active")
            and (previous.get("early_alerted_at") or has_first_confirmation(merged))
        ):
            alert = build_alert(row, merged, now_iso, "invalidated")
            alert["reason"] = invalid_reason
            alerts.append(alert)
            new_alerts.append(alert)
            merged["invalidation_alert_active"] = True
        elif not invalid_reason and progress["fresh"]:
            merged["invalidation_alert_active"] = False
        if (
            market_reason and not invalid_reason and not recovered_from_pullback and progress["fresh"]
            and not previous.get("pullback_alert_active")
            and (previous.get("early_alerted_at") or has_first_confirmation(merged))
        ):
            alert = build_alert(row, merged, now_iso, "pullback")
            alert["reason"] = market_reason
            alerts.append(alert)
            new_alerts.append(alert)
            merged["pullback_alert_active"] = True
        elif not market_reason and progress["fresh"]:
            merged["pullback_alert_active"] = False
        if (
            recovered_from_pullback and progress["fresh"]
            and not invalid_reason and not smart_check["sell_dominance"]
            and not previous.get("recovery_alert_active")
        ):
            alert = build_alert(row, merged, now_iso, "recovered")
            alert["reason"] = merged.get("recovery_reason") or "回撤修复，重新观察"
            alerts.append(alert)
            new_alerts.append(alert)
            merged["recovery_alert_active"] = True
        elif not recovered_from_pullback:
            merged["recovery_alert_active"] = False
        if (
            smart_check["sell_dominance"] and not invalid_reason
            and not previous.get("deterioration_alert_active")
            and (previous.get("early_alerted_at") or has_first_confirmation(merged))
        ):
            alert = build_alert(row, merged, now_iso, "deterioration")
            alert["reason"] = smart_check["reason"]
            alerts.append(alert)
            new_alerts.append(alert)
            merged["deterioration_alert_active"] = True
        elif smart_check["market_fresh"] and not smart_check["sell_dominance"] and smart_check["buy_usd"] + smart_check["sell_usd"] > 0:
            merged["deterioration_alert_active"] = False
        merged["gold_capture_type"] = gold_capture_type(merged)
        merged["watch_v2_tier"] = watch_v2_tier(merged)
        candidates[key] = merged

    candidates = {
        key: item
        for key, item in candidates.items()
        if key in latest_by_key or (
            parse_dt((item or {}).get("last_seen_at")) is not None
            and hours_between((item or {}).get("last_seen_at"), now_iso) <= STALE_CANDIDATE_HOURS
        )
    }
    if len(candidates) > MAX_STORED_CANDIDATES:
        ordered_candidates = sorted(
            candidates.items(),
            key=lambda pair: (
                parse_dt((pair[1] or {}).get("last_seen_at")).timestamp()
                if parse_dt((pair[1] or {}).get("last_seen_at")) else 0.0
            ),
            reverse=True,
        )
        candidates = dict(ordered_candidates[:MAX_STORED_CANDIDATES])

    confirmed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    early: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    pending: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for key, state_row in candidates.items():
        row = latest_by_key.get(key)
        if not row:
            continue
        if state_row.get("status") == "strong_candidate":
            confirmed.append((key, row, state_row))
        elif state_row.get("status") == "early_candidate":
            early.append((key, row, state_row))
        elif state_row.get("status") == "candidate":
            pending.append((key, row, state_row))

    confirmed.sort(key=lambda item: rank_key(item[1], item[2]), reverse=True)
    early.sort(key=lambda item: rank_key(item[1], item[2]), reverse=True)
    pending.sort(key=lambda item: rank_key(item[1], item[2]), reverse=True)

    last_alert_key = payload.get("last_alert_key")
    new_confirmed_source: tuple[str, dict[str, Any], dict[str, Any]] | None = None
    for key, row, state_row in confirmed:
        if key in alerted_keys or not state_row.get("alert_eligible") or not state_row.get("evidence_advanced"):
            continue
        if new_confirmed_source is None:
            new_confirmed_source = (key, row, state_row)
        alert = build_alert(row, state_row, now_iso)
        alerts.append(alert)
        new_alerts.append(alert)
        last_alert_key = key
        alerted_keys.add(key)
        alerted_at_by_key[key] = now_iso
        state_row.update(alert_metadata(alert))
        state_row["first_confirmation_active"] = True
        state_row["watch_v2_tier"] = watch_v2_tier(state_row)
        candidates[key] = state_row
    if confirmed and not new_confirmed_source:
        last_alert_key = confirmed[0][0]

    pick_source = new_confirmed_source or (early[0] if early else None)
    pick = public_pick(pick_source[1], pick_source[2]) if pick_source else None
    confirmed_pick = (
        public_pick(new_confirmed_source[1], new_confirmed_source[2]) if new_confirmed_source else None
    )
    early_pick = public_pick(early[0][1], early[0][2]) if early else None

    payload.update(
        {
            "updated_at": now_iso,
            "min_confirmations": min_confirmations,
            "candidates": candidates,
            "pick": pick,
            "confirmed_pick": confirmed_pick,
            "early_pick": early_pick,
            "last_alert_key": last_alert_key,
            "alerted_keys": sorted(alerted_keys),
            "alerted_at_by_key": alerted_at_by_key,
            "alerts": alerts[-50:],
            "new_alerts": new_alerts,
            "top_pending_key": pending[0][0] if pending else None,
            "top_pending_reason": pending_reason(pending[0][2], min_confirmations) if pending else "",
        }
    )
    payload["summary"] = watch_summary(payload)
    return payload


def watch_summary(state: dict[str, Any]) -> dict[str, Any]:
    candidates = state.get("candidates") or {}
    updated_at = state.get("updated_at")
    stored_items = [item for item in candidates.values() if isinstance(item, dict)]
    active_items = [
        item
        for item in candidates.values()
        if isinstance(item, dict) and (not updated_at or item.get("last_seen_at") == updated_at)
    ]
    statuses = [item.get("status") for item in active_items]
    v2_tiers = [item.get("watch_v2_tier") or watch_v2_tier(item) for item in active_items]
    historical_first_confirmed = [item for item in stored_items if has_first_confirmation(item)]
    confirmed_runners = [
        item for item in stored_items if item.get("status") == "achieved_gold" and has_first_confirmation(item)
    ]
    review_only_achieved = [
        item for item in stored_items if item.get("status") == "achieved_gold" and not has_first_confirmation(item)
    ]
    current_confirmed_runners = [
        item for item in active_items if item.get("status") == "achieved_gold" and has_first_confirmation(item)
    ]
    current_review_only_achieved = [
        item for item in active_items if item.get("status") == "achieved_gold" and not has_first_confirmation(item)
    ]
    latest_first_confirmed = max(
        historical_first_confirmed,
        key=lambda item: str(item.get("first_confirmed_at") or item.get("alerted_at") or ""),
        default=None,
    )
    latest_confirmed_runner = max(
        confirmed_runners,
        key=lambda item: str(item.get("first_confirmed_at") or item.get("alerted_at") or ""),
        default=None,
    )
    success_rate = (
        len(confirmed_runners) / len(historical_first_confirmed) * 100
        if historical_first_confirmed
        else 0.0
    )
    return {
        "tracked_count": len(active_items),
        "stored_tracked_count": len(candidates),
        "discovered_count": v2_tiers.count("discovered"),
        "first_confirmed_count": v2_tiers.count("confirmed"),
        "review_count": v2_tiers.count("review"),
        "seed_pool_count": statuses.count("seed_pool"),
        "early_candidate_count": statuses.count("early_candidate"),
        "candidate_count": statuses.count("candidate"),
        "strong_candidate_count": statuses.count("strong_candidate"),
        "late_confirmed_count": sum(
            1
            for item in active_items
            if isinstance(item, dict)
            and item.get("status") == "strong_candidate"
            and not item.get("alert_eligible")
        ),
        "expired_count": statuses.count("expired"),
        "invalidated_count": statuses.count("invalidated"),
        "pullback_count": statuses.count("pullback"),
        "deteriorating_count": statuses.count("deteriorating"),
        "achieved_gold_count": statuses.count("achieved_gold"),
        "historical_first_confirmed_count": len(historical_first_confirmed),
        "confirmed_runner_count": len(confirmed_runners),
        "review_only_achieved_gold_count": len(review_only_achieved),
        "current_confirmed_runner_count": len(current_confirmed_runners),
        "current_review_only_achieved_gold_count": len(current_review_only_achieved),
        "first_confirmation_success_rate_pct": round(success_rate, 2),
        "latest_first_confirmed_symbol": latest_first_confirmed.get("symbol") if latest_first_confirmed else None,
        "latest_first_confirmed_at": (
            latest_first_confirmed.get("first_confirmed_at") or latest_first_confirmed.get("alerted_at")
            if latest_first_confirmed
            else None
        ),
        "latest_confirmed_runner_symbol": latest_confirmed_runner.get("symbol") if latest_confirmed_runner else None,
        "latest_confirmed_runner_at": (
            latest_confirmed_runner.get("first_confirmed_at") or latest_confirmed_runner.get("alerted_at")
            if latest_confirmed_runner
            else None
        ),
        "alerted_count": len(state.get("alerted_keys") or []),
        "pick_symbol": (state.get("pick") or {}).get("symbol") if isinstance(state.get("pick"), dict) else None,
        "confirmed_pick_symbol": (state.get("confirmed_pick") or {}).get("symbol") if isinstance(state.get("confirmed_pick"), dict) else None,
        "early_pick_symbol": (state.get("early_pick") or {}).get("symbol") if isinstance(state.get("early_pick"), dict) else None,
        "top_pending_reason": state.get("top_pending_reason") or "",
        "min_confirmations": state.get("min_confirmations"),
        "updated_at": state.get("updated_at"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update gold dog watch state from the latest radar report.")
    parser.add_argument("--report", default="outputs/alpha-radar-report-latest.json")
    parser.add_argument("--out-dir", default="outputs")
    parser.add_argument("--min-confirmations", type=int, default=3)
    parser.add_argument("--chain-scope", default="bsc", choices=["bsc", "*", "all"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_path = Path(args.report)
    out_dir = Path(args.out_dir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = list(report.get("meme_potential_rows") or []) + list(report.get("meme_shadow_rows") or [])
    now_iso = ((report.get("meta") or {}).get("report_generated_at")) or datetime.now().astimezone().isoformat(timespec="seconds")
    state_path = out_dir / "alpha-gold-watch-state.json"
    state = update_watch_state(
        load_watch_state(state_path),
        rows,
        now_iso,
        min_confirmations=args.min_confirmations,
        chain_scope=args.chain_scope,
    )
    write_watch_state(state_path, state)
    print(json.dumps(watch_summary(state), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
