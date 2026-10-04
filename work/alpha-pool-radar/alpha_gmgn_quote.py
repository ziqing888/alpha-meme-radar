"""Low-latency read-only GMGN token quotes for the execution fast path."""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from alpha_gmgn_smart_money_top50 import gmgn_env


class GmgnRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_epoch: float):
        super().__init__(message)
        self.retry_after_epoch = retry_after_epoch


def _rate_limit_retry_epoch(message: str, now_epoch: float | None = None) -> float:
    now_epoch = now_epoch or time.time()
    deadline = now_epoch + 5 * 60
    reset = re.search(r"(?:reset_at|resetAt|retry_after_epoch)[^0-9]{0,20}(\d{10,13})", message, re.I)
    iso = re.search(r"until\s+(\d{4}-\d{2}-\d{2}T[^\s,]+)", message, re.I)
    if reset:
        value = float(reset.group(1))
        deadline = max(deadline, value / 1000 if value > 1e11 else value)
    if iso:
        try:
            deadline = max(deadline, datetime.fromisoformat(iso.group(1).replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return deadline


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _change(current: float, previous: Any) -> float | None:
    reference = _number(previous)
    if current <= 0 or reference <= 0:
        return None
    return round((current / reference - 1) * 100, 6)


def normalize_gmgn_token_info(payload: Any, chain: str, address: str, now: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"chain": chain, "contract_address": address.lower(), "quote_status": "unavailable",
                "quote_error": "gmgn_invalid_payload", "gmgn_attempted_at": now}
    token_address = str(payload.get("address") or "").strip().lower()
    expected = str(address or "").strip().lower()
    if token_address != expected:
        return {"chain": chain, "contract_address": expected, "quote_status": "unavailable",
                "quote_error": "gmgn_identity_mismatch", "gmgn_attempted_at": now}
    price_data = payload.get("price") if isinstance(payload.get("price"), dict) else {}
    pool = payload.get("pool") if isinstance(payload.get("pool"), dict) else {}
    price = _number(price_data.get("price"))
    liquidity = _number(payload.get("liquidity") or pool.get("liquidity"))
    pair_address = str(payload.get("biggest_pool_address") or pool.get("pool_address") or "").strip()
    if price <= 0 or liquidity <= 0 or not pair_address:
        return {"chain": chain, "contract_address": expected, "quote_status": "unavailable",
                "quote_error": "gmgn_no_liquid_pool", "gmgn_attempted_at": now}
    circulating_supply = _number(payload.get("circulating_supply"))
    market_cap = _number(payload.get("market_cap") or payload.get("market_cap_usd"))
    if market_cap <= 0 and circulating_supply > 0:
        market_cap = price * circulating_supply
    created = _number(pool.get("creation_timestamp") or payload.get("open_timestamp"))
    quote: dict[str, Any] = {
        "chain": chain,
        "contract_address": expected,
        "symbol": payload.get("symbol"),
        "price_usd": price,
        "mcap": market_cap or None,
        "market_cap": market_cap or None,
        "market_cap_source": "gmgn.price_x_circulating_supply" if market_cap else None,
        "valuation_type": "market_cap" if market_cap else "unavailable",
        "liquidity": liquidity,
        "volume5m": _number(price_data.get("volume_5m")),
        "volume24h": _number(price_data.get("volume_24h")),
        "buy_count5m": _number(price_data.get("buys_5m")),
        "sell_count5m": _number(price_data.get("sells_5m")),
        "change_m5": _change(price, price_data.get("price_5m")),
        "change_h1": _change(price, price_data.get("price_1h")),
        "change_h24": _change(price, price_data.get("price_24h")),
        "pair_address": pair_address,
        "gmgn_url": ((payload.get("link") or {}).get("gmgn") if isinstance(payload.get("link"), dict) else None),
        "holder_count": _number(payload.get("holder_count")),
        "top10_holder_pct": _number((payload.get("dev") or {}).get("top_10_holder_rate")) * 100
        if isinstance(payload.get("dev"), dict) else None,
        "quote_observed_at": now,
        "gmgn_observed_at": now,
        "gmgn_attempted_at": now,
        "quote_source": "gmgn_skill_token_info",
        "quote_status": "fresh",
        "quote_time_basis": "gmgn_http_observation_not_trade_timestamp",
    }
    if created > 0:
        observed = datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp()
        quote["pair_age_hours"] = max(0.0, (observed - created) / 3600)
    return quote


def fetch_gmgn_token_info(
    chain: str,
    address: str,
    *,
    timeout_seconds: int = 4,
    opener=None,
) -> dict[str, Any]:
    """Read token info directly from GMGN OpenAPI without starting Node per token."""
    env, _ = gmgn_env()
    api_key = str(env.get("GMGN_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("gmgn_api_key_missing")
    query = urllib.parse.urlencode({
        "chain": chain,
        "address": address,
        "timestamp": int(time.time()),
        "client_id": str(uuid.uuid4()),
    })
    request = urllib.request.Request(
        f"https://openapi.gmgn.ai/v1/token/info?{query}",
        headers={"X-APIKEY": api_key, "Content-Type": "application/json", "User-Agent": "gmgn-cli/monitor"},
    )
    open_request = opener or urllib.request.urlopen
    try:
        with open_request(request, timeout=max(2, timeout_seconds)) as response:
            envelope = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        reset_at = exc.headers.get("x-ratelimit-reset") if exc.headers else None
        if exc.code == 429 or re.search(r"rate[_ -]?limit|too many requests", message, re.I):
            detail = f"{message} reset_at={reset_at}" if reset_at else message
            raise GmgnRateLimitError("gmgn_rate_limited", retry_after_epoch=_rate_limit_retry_epoch(detail)) from exc
        raise RuntimeError(f"gmgn_http_{exc.code}") from exc
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("gmgn_invalid_json") from exc
    if not isinstance(envelope, dict) or envelope.get("code") != 0:
        message = json.dumps(envelope, ensure_ascii=False) if isinstance(envelope, dict) else "gmgn_invalid_envelope"
        if re.search(r"rate[_ -]?limit|too many requests", message, re.I):
            raise GmgnRateLimitError("gmgn_rate_limited", retry_after_epoch=_rate_limit_retry_epoch(message))
        raise RuntimeError("gmgn_api_error")
    payload = envelope.get("data")
    from alpha_fast_track import utc_now

    return normalize_gmgn_token_info(payload, chain, address, utc_now())
