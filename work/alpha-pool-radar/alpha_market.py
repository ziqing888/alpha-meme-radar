"""Public market-data adapters for Alpha radar."""

from __future__ import annotations

import math
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from alpha_http import http_json
from alpha_radar_sources import (
    BINANCE_ALPHA_LIST_URL,
    CHAIN_ID_TO_DEX,
    DEX_TOKEN_PAIRS_URL,
    FAPI_24H_TICKER_URL,
    FAPI_EXCHANGE_INFO_URL,
    FAPI_FUNDING_URL,
    FAPI_OI_HIST_URL,
)


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, str) and value.strip() == "":
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def norm_symbol(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def strip_multiplier(value: str) -> str:
    value = norm_symbol(value)
    for prefix in ("1000000", "100000", "10000", "1000"):
        if value.startswith(prefix) and len(value) > len(prefix):
            return value[len(prefix) :]
    return value


def fetch_alpha_tokens(limit: int | None = None) -> list[dict[str, Any]]:
    payload = http_json(BINANCE_ALPHA_LIST_URL)
    if not payload.get("success") and payload.get("code") != "000000":
        raise RuntimeError(f"Alpha list error: {payload.get('code')} {payload.get('message')}")
    tokens = payload.get("data") or []
    tokens = [token for token in tokens if not token.get("offline") and not token.get("fullyDelisted")]
    tokens.sort(
        key=lambda token: (
            to_float(token.get("volume24h")),
            to_float(token.get("marketCap")),
        ),
        reverse=True,
    )
    if limit:
        return tokens[:limit]
    return tokens


def dex_chain_id(token: dict[str, Any]) -> str:
    chain_id = str(token.get("chainId") or "").strip()
    chain_name = str(token.get("chainName") or "").strip().lower()
    if chain_id in CHAIN_ID_TO_DEX:
        return CHAIN_ID_TO_DEX[chain_id]
    if chain_name:
        return re.sub(r"[^a-z0-9]+", "", chain_name)
    return chain_id


def fetch_dex_pairs(token: dict[str, Any]) -> list[dict[str, Any]]:
    address = token.get("contractAddress")
    chain = dex_chain_id(token)
    if not address or not chain:
        return []
    url = DEX_TOKEN_PAIRS_URL.format(
        chain=urllib.parse.quote(str(chain), safe=""),
        address=urllib.parse.quote(str(address), safe=""),
    )
    payload = http_json(url)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("pairs"), list):
        return payload["pairs"]
    return []


def pick_best_pair(token: dict[str, Any], pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pairs:
        return None
    address = str(token.get("contractAddress") or "").lower()

    def score(pair: dict[str, Any]) -> tuple[int, float, float]:
        base_address = str((pair.get("baseToken") or {}).get("address") or "").lower()
        quote_address = str((pair.get("quoteToken") or {}).get("address") or "").lower()
        exact = 1 if address in (base_address, quote_address) else 0
        liquidity = to_float((pair.get("liquidity") or {}).get("usd"))
        volume = to_float((pair.get("volume") or {}).get("h24"))
        return exact, liquidity, volume

    return max(pairs, key=score)


def fetch_futures_index() -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    exchange = http_json(FAPI_EXCHANGE_INFO_URL)
    ticker_rows = http_json(FAPI_24H_TICKER_URL)
    tickers = {row.get("symbol"): row for row in ticker_rows if isinstance(row, dict)}

    symbol_by_clean_base: dict[str, str] = {}
    symbols: dict[str, dict[str, Any]] = {}
    for row in exchange.get("symbols") or []:
        if row.get("status") != "TRADING" or row.get("quoteAsset") != "USDT":
            continue
        symbol = row.get("symbol")
        if not symbol:
            continue
        symbols[symbol] = row
        base = strip_multiplier(str(row.get("baseAsset") or ""))
        if base:
            symbol_by_clean_base.setdefault(base, symbol)
        clean_from_symbol = strip_multiplier(symbol[:-4] if symbol.endswith("USDT") else symbol)
        if clean_from_symbol:
            symbol_by_clean_base.setdefault(clean_from_symbol, symbol)
    return symbol_by_clean_base, symbols, tickers


def find_futures_symbol(alpha_symbol: str, symbol_by_clean_base: dict[str, str]) -> str | None:
    clean = strip_multiplier(alpha_symbol)
    if not clean:
        return None
    direct_candidates = [
        f"{clean}USDT",
        f"1000{clean}USDT",
        f"10000{clean}USDT",
        f"100000{clean}USDT",
        f"1000000{clean}USDT",
    ]
    for candidate in direct_candidates:
        if candidate in symbol_by_clean_base.values():
            return candidate
    return symbol_by_clean_base.get(clean)


def fetch_funding(symbol: str) -> float | None:
    query = urllib.parse.urlencode({"symbol": symbol, "limit": 1})
    payload = http_json(f"{FAPI_FUNDING_URL}?{query}")
    if isinstance(payload, list) and payload:
        return to_float(payload[-1].get("fundingRate")) * 100
    return None


def fetch_oi_metrics(symbol: str, period: str = "5m", limit: int = 13) -> dict[str, Any]:
    query = urllib.parse.urlencode({"symbol": symbol, "period": period, "limit": limit})
    payload = http_json(f"{FAPI_OI_HIST_URL}?{query}")
    if not isinstance(payload, list) or len(payload) < 2:
        return {}
    result = {"oi_observed_at_ms": payload[-1].get("timestamp"), "oi_basis": "quantity"}
    for field, output in (("sumOpenInterest", "oi_change_1h_pct"), ("sumOpenInterestValue", "oi_value_change_1h_pct")):
        first = to_float(payload[0].get(field))
        last = to_float(payload[-1].get(field))
        result[output] = (last - first) / first * 100 if first > 0 and payload[-1].get(field) is not None else None
    result["oi_value"] = to_float(payload[-1]["sumOpenInterestValue"]) if payload[-1].get("sumOpenInterestValue") is not None else None
    return result


def fetch_oi_change(symbol: str, period: str = "5m", limit: int = 13) -> tuple[float | None, float | None]:
    # Retain the legacy value-based helper for external callers.
    metrics = fetch_oi_metrics(symbol, period, limit)
    return metrics.get("oi_value_change_1h_pct"), metrics.get("oi_value")


def fetch_funding_metrics(symbol: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"symbol": symbol, "limit": 3})
    payload = http_json(f"{FAPI_FUNDING_URL}?{query}")
    if not isinstance(payload, list) or not payload:
        return {}
    latest = payload[-1]
    rate = to_float(latest["fundingRate"]) * 100 if latest.get("fundingRate") is not None else None
    interval = None
    if len(payload) >= 2:
        delta = to_float(latest.get("fundingTime")) - to_float(payload[-2].get("fundingTime"))
        interval = round(delta / 3_600_000, 3) if 0 < delta <= 86_400_000 else None
    return {
        "funding_rate_pct": rate,
        "funding_settled_at_ms": latest.get("fundingTime"),
        "funding_interval_hours": interval,
        "funding_rate_8h_pct": rate * 8 / interval if rate is not None and interval else None,
        "funding_history": [{"time": item.get("fundingTime"), "rate_pct": to_float(item["fundingRate"]) * 100} for item in payload if item.get("fundingRate") is not None],
    }


def fetch_dex_for_tokens(tokens: list[dict[str, Any]], concurrency: int) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    results: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {}
        for token in tokens:
            address = str(token.get("contractAddress") or "")
            if not address:
                continue
            key = f"{token.get('chainId')}:{address.lower()}"
            futures[pool.submit(fetch_dex_pairs, token)] = (key, token)
        for future in as_completed(futures):
            key, token = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"DexScreener {token.get('symbol')} {key}: {exc}")
                results[key] = []
    return results, errors


def fetch_futures_details(symbols: list[str], concurrency: int) -> tuple[dict[str, dict[str, Any]], list[str]]:
    details: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def fetch_one(symbol: str) -> tuple[str, dict[str, Any]]:
        return symbol, {
            **fetch_funding_metrics(symbol),
            **fetch_oi_metrics(symbol),
        }

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(fetch_one, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                key, value = future.result()
                details[key] = value
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Futures detail {symbol}: {exc}")
    return details, errors
