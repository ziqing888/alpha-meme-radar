"""Bridge the website report to the local TG Alpha stage monitor.

The website may score every Binance Alpha row as a candidate or risk, but
trade-like actions are allowed only when the local 15m stage model confirms a
Binance USDT perpetual with OI and funding context.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from alpha_http import http_json as cached_http_json


_stage_monitor_path = os.environ.get("ALPHA_STAGE_MONITOR_PATH", "").strip()
TG_STAGE_MONITOR_PATH = Path(_stage_monitor_path).expanduser() if _stage_monitor_path else None

STAGE_NONE = "暂无"
STAGE_SILENT = "静默蓄势"
STAGE_EARLY_LONG = "早期试多"
STAGE_WASH_HOLD = "洗盘抗住"
STAGE_MARKUP_HOLD = "主升持有"
STAGE_TOP_WARN = "跑路前兆"
STAGE_EXIT_WARN = "跑路预警"
STAGE_SHORT = "砸盘确认/试空"
STAGE_NO_CHASE = "不追"
STAGE_UNKNOWN = "未知"
STAGE_WATCH = "观察"
STAGE_DEALER_RISK = "高控盘风险"
STAGE_CONTRACT_MISSING = "合约数据不足"

OPERATIONAL_STAGES = {
    STAGE_EARLY_LONG,
    STAGE_WASH_HOLD,
    STAGE_MARKUP_HOLD,
    STAGE_TOP_WARN,
    STAGE_EXIT_WARN,
    STAGE_SHORT,
}

STAGE_PRIORITY = {
    STAGE_SHORT: 100,
    STAGE_EXIT_WARN: 90,
    STAGE_TOP_WARN: 85,
    STAGE_WASH_HOLD: 80,
    STAGE_MARKUP_HOLD: 75,
    STAGE_EARLY_LONG: 70,
    STAGE_SILENT: 60,
    STAGE_NO_CHASE: 30,
    STAGE_DEALER_RISK: 20,
    STAGE_CONTRACT_MISSING: 10,
    STAGE_WATCH: 0,
    STAGE_NONE: 0,
}


def _value(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(row, dict):
            value = row.get(name)
        else:
            value = getattr(row, name, None)
        if value is not None and value != "":
            return value
    return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        return default


def best_change(row: Any) -> float:
    alpha_change = to_float(_value(row, "alpha_change24h_pct", "alpha_change24h"))
    futures_change = to_float(_value(row, "futures_price_change24h_pct", "futures_price_change24h"))
    dex_change = to_float(_value(row, "change_h24", "price_change_24h_pct"))
    candidates = [alpha_change, futures_change, dex_change]
    return max(candidates, key=lambda item: abs(item))


def has_full_futures_context(row: Any) -> bool:
    return bool(_value(row, "futures_symbol")) and _value(row, "oi_change_1h_pct") is not None and _value(row, "funding_rate_pct") is not None


def has_high_control(row: Any) -> bool:
    top10 = to_float(_value(row, "top10_holder_pct"), -1)
    max_holder = to_float(_value(row, "max_holder_pct"), -1)
    return top10 >= 55 or max_holder >= 18


def load_tg_stage_monitor() -> Any:
    if TG_STAGE_MONITOR_PATH is None or not TG_STAGE_MONITOR_PATH.exists():
        raise FileNotFoundError(f"stage monitor not found: {TG_STAGE_MONITOR_PATH}")
    module_name = "_alpha_tg_stage_monitor"
    if module_name in sys.modules:
        module = sys.modules[module_name]
        module.http_json = _tg_http_json
        return module
    spec = importlib.util.spec_from_file_location(module_name, TG_STAGE_MONITOR_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load stage monitor: {TG_STAGE_MONITOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module.http_json = _tg_http_json
    return module


def _tg_http_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        url = f"{url}?{urlencode(params)}"
    timeout = int(os.environ.get("ALPHA_STAGE_HTTP_TIMEOUT_SECONDS", "6"))
    return cached_http_json(url, timeout=timeout)


def _numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", text)]


def _stage_code(stage: str) -> str:
    return {
        STAGE_SILENT: "silent_build",
        STAGE_EARLY_LONG: "early_long",
        STAGE_WASH_HOLD: "wash_hold",
        STAGE_MARKUP_HOLD: "markup_hold",
        STAGE_TOP_WARN: "top_warn",
        STAGE_EXIT_WARN: "exit_warning",
        STAGE_SHORT: "short_confirm",
        STAGE_NO_CHASE: "overheated",
        STAGE_DEALER_RISK: "dealer_risk",
        STAGE_CONTRACT_MISSING: "contract_missing",
    }.get(stage, "watch")


def _blank_operational_fields() -> dict[str, Any]:
    return {
        "direction": "观察",
        "action": "",
        "key_level": None,
        "invalid_level": None,
        "holding_text": "",
        "protection_text": "",
    }


def observation_profile(row: Any, market_cap: float | None = None, volume: float | None = None) -> dict[str, Any]:
    change = best_change(row)
    mcap = to_float(market_cap if market_cap is not None else _value(row, "market_cap", "mcap", "dex_market_cap", "fdv"))
    vol = to_float(
        volume
        if volume is not None
        else max(
            to_float(_value(row, "dealer_volume_usd")),
            to_float(_value(row, "alpha_volume24h")),
            to_float(_value(row, "dex_volume24h", "volume24h")),
        )
    )
    vol_to_mcap = vol / mcap if mcap > 0 else 0.0
    futures_symbol = str(_value(row, "futures_symbol", default="") or "")
    has_futures = bool(futures_symbol)
    has_oi = _value(row, "oi_change_1h_pct") is not None
    has_funding = _value(row, "funding_rate_pct") is not None

    if has_high_control(row):
        stage = STAGE_DEALER_RISK
        label = "高控盘风险"
        reason = "筹码集中或单钱包占比高，只能作为庄家风险观察，不触发交易动作。"
        alpha_type = "dealer_risk"
    elif not (has_futures and has_oi and has_funding):
        stage = STAGE_CONTRACT_MISSING
        label = "观察"
        missing = []
        if not has_futures:
            missing.append("未上 Binance USDT 永续")
        if not has_oi:
            missing.append("缺 OI")
        if not has_funding:
            missing.append("缺资金费率")
        reason = " / ".join(missing) + "，不允许生成做多/做空/持仓动作。"
        alpha_type = "contract_missing"
    else:
        stage = STAGE_WATCH
        label = "观察"
        reason = "等待本地 TG 15m 阶段模型确认；静默/暂无阶段不算操作动作。"
        alpha_type = "watch"

    return {
        "stage": stage,
        **_blank_operational_fields(),
        "reason": reason,
        "alpha_stage": _stage_code(stage),
        "alpha_stage_label": stage,
        "alpha_type": alpha_type,
        "alpha_label": label,
        "alpha_explain": reason,
        "stage_change_pct": round(change, 4),
        "stage_volume_to_mcap_pct": round(vol_to_mcap * 100, 4) if mcap > 0 else None,
    }


def profile_from_tg_stage(row: Any, tg_row: dict[str, Any], tg_module: Any | None = None) -> dict[str, Any]:
    tg_module = tg_module or load_tg_stage_monitor()
    stage = str(tg_row.get("stage") or STAGE_UNKNOWN)
    symbol = str(_value(row, "symbol", default=tg_row.get("symbol") or "--") or "--")
    reason = str(tg_row.get("reason") or "")
    key = str(tg_row.get("key") or "")
    nums = tg_module.key_numbers(key) if hasattr(tg_module, "key_numbers") else _numbers(key)
    key_level = nums[0] if nums else None
    invalid_level = nums[1] if len(nums) >= 2 else None
    change = to_float(tg_row.get("p24"), best_change(row))

    profile = observation_profile(row)
    profile.update(
        {
            "stage": stage,
            "reason": reason,
            "alpha_stage": _stage_code(stage),
            "alpha_stage_label": stage,
            "alpha_type": _stage_code(stage),
            "alpha_label": stage if stage not in {STAGE_NONE, STAGE_UNKNOWN} else "观察",
            "alpha_explain": reason,
            "key_level": key_level,
            "invalid_level": invalid_level,
            "stage_change_pct": round(change, 4),
        }
    )

    if stage in OPERATIONAL_STAGES:
        enriched = {**tg_row, "symbol": symbol}
        profile.update(
            {
                "direction": tg_module.direction_text(enriched),
                "action": tg_module.trade_call_text(enriched),
                "holding_text": tg_module.holding_text(enriched),
                "protection_text": tg_module.protection_text(enriched),
            }
        )
    else:
        profile.update(_blank_operational_fields())
        if stage == STAGE_SILENT:
            profile["reason"] = reason or "静默蓄势只进观察池，不提醒，不生成操作动作。"
            profile["alpha_explain"] = profile["reason"]
        elif stage in {STAGE_NONE, STAGE_UNKNOWN}:
            profile["stage"] = STAGE_WATCH
            profile["alpha_stage"] = "watch"
            profile["alpha_stage_label"] = STAGE_WATCH
            profile["alpha_label"] = "观察"
    return profile


def stage_profile(row: Any, market_cap: float | None = None, volume: float | None = None) -> dict[str, Any]:
    return observation_profile(row, market_cap=market_cap, volume=volume)


def attach_live_stage_profiles(rows: list[dict[str, Any]]) -> None:
    candidates = [row for row in rows if has_full_futures_context(row)]
    if not candidates:
        return
    try:
        tg_module = load_tg_stage_monitor()
        ticks = tg_module.ticker_map()
    except Exception as exc:  # noqa: BLE001
        for row in candidates:
            if row.get("action"):
                continue
            row.update(observation_profile(row))
            row["stage_error"] = str(exc)
        return

    def scan_symbol(symbol: str) -> tuple[str, dict[str, Any]]:
        if symbol not in ticks:
            return symbol, {"stage": STAGE_UNKNOWN, "reason": "未上永续或无数据"}
        return symbol, tg_module.stage_symbol(symbol, tg_module.klines(symbol), ticks[symbol])

    symbols = sorted({str(row.get("futures_symbol") or "").upper() for row in candidates if row.get("futures_symbol")})
    cache: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as executor:
        future_by_symbol = {executor.submit(scan_symbol, symbol): symbol for symbol in symbols}
        for future in as_completed(future_by_symbol):
            symbol = future_by_symbol[future]
            try:
                _, payload = future.result()
                cache[symbol] = payload
            except Exception as exc:  # noqa: BLE001
                cache[symbol] = {"stage": STAGE_UNKNOWN, "reason": str(exc)}

    for row in candidates:
        symbol = str(row.get("futures_symbol") or "").upper()
        if not symbol:
            continue
        try:
            row.update(profile_from_tg_stage(row, cache[symbol], tg_module=tg_module))
        except Exception as exc:  # noqa: BLE001
            row.update(observation_profile(row))
            row["stage_error"] = str(exc)


def stage_priority(row: Any) -> int:
    return STAGE_PRIORITY.get(str(_value(row, "stage", default="")), 0)


def stage_sort_key(row: dict[str, Any]) -> tuple[float, float, float]:
    score = max(to_float(row.get("dealer_score")), to_float(row.get("score")))
    return (stage_priority(row), score, abs(best_change(row)))
