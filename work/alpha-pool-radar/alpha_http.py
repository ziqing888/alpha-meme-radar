#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from alpha_radar_sources import REQUEST_HEADERS


HTTP_CACHE_DIR: Path | None = None
HTTP_CACHE_MAX_AGE_SECONDS = 6 * 60 * 60
HTTP_RETRIES = 2
HTTP_EVENTS: list[dict[str, Any]] = []


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, str) and value.strip() == "":
            return default
        result = float(value)
        return result
    except (TypeError, ValueError):
        return default


def configure_cache(out_dir: Path, max_age_seconds: int = HTTP_CACHE_MAX_AGE_SECONDS) -> None:
    global HTTP_CACHE_DIR, HTTP_CACHE_MAX_AGE_SECONDS
    HTTP_CACHE_DIR = out_dir / "alpha-radar-cache"
    HTTP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    HTTP_CACHE_MAX_AGE_SECONDS = max_age_seconds


def reset_data_quality() -> None:
    HTTP_EVENTS.clear()


def source_name(url: str) -> str:
    if "api.dexscreener.com" in url:
        return "dexscreener"
    if "binance.com" in url and "alpha" in url:
        return "binance_alpha"
    if "fapi.binance.com" in url:
        return "binance_futures"
    if "api.routescan.io" in url:
        return "routescan"
    if "blockscout.com" in url:
        return "blockscout"
    if "etherscan.io" in url:
        return "etherscan"
    if "gopluslabs.io" in url:
        return "goplus"
    if "rugcheck.xyz" in url:
        return "rugcheck"
    if "solana.com" in url or "solana" in url:
        return "solana_rpc"
    return "other"


def cache_path_for_url(url: str) -> Path | None:
    if HTTP_CACHE_DIR is None:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return HTTP_CACHE_DIR / f"{digest}.json"


def write_http_cache(url: str, data: Any) -> None:
    path = cache_path_for_url(url)
    if path is None:
        return
    payload = {"url": url, "fetched_at": time.time(), "data": data}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_http_cache(url: str) -> tuple[Any, float] | None:
    path = cache_path_for_url(url)
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if payload.get("url") != url:
        return None
    age_seconds = max(0.0, time.time() - _to_float(payload.get("fetched_at")))
    if age_seconds > HTTP_CACHE_MAX_AGE_SECONDS:
        return None
    return payload.get("data"), age_seconds


def record_http_event(url: str, status: str, error: str = "", cache_age_seconds: float | None = None) -> None:
    HTTP_EVENTS.append(
        {
            "source": source_name(url),
            "status": status,
            "url": url,
            "error": error,
            "cache_age_seconds": None if cache_age_seconds is None else round(cache_age_seconds, 1),
        }
    )


def data_quality_summary() -> dict[str, Any]:
    by_source: dict[str, dict[str, int]] = {}
    for event in HTTP_EVENTS:
        source = event["source"]
        status = event["status"]
        by_source.setdefault(source, {"live": 0, "cached": 0, "failed": 0})
        by_source[source][status] = by_source[source].get(status, 0) + 1

    if not HTTP_EVENTS:
        overall = "unknown"
    elif any(event["status"] == "failed" for event in HTTP_EVENTS):
        overall = "partial"
    elif any(event["status"] == "cached" for event in HTTP_EVENTS):
        overall = "cached"
    else:
        overall = "live"

    cached = [event for event in HTTP_EVENTS if event["status"] == "cached"]
    failed = [event for event in HTTP_EVENTS if event["status"] == "failed"]
    return {
        "overall": overall,
        "by_source": by_source,
        "request_count": len(HTTP_EVENTS),
        "cached_count": len(cached),
        "failed_count": len(failed),
        "cached_examples": cached[:8],
        "failed_examples": failed[:8],
    }


def http_json(url: str, timeout: int = 15, headers: dict[str, str] | None = None) -> Any:
    request_headers = {**REQUEST_HEADERS, **(headers or {})}
    last_error: Exception | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            write_http_cache(url, payload)
            record_http_event(url, "live")
            return payload
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < HTTP_RETRIES:
                time.sleep(0.4 * (attempt + 1))

    cached = read_http_cache(url)
    if cached is not None:
        payload, age_seconds = cached
        record_http_event(url, "cached", error=str(last_error), cache_age_seconds=age_seconds)
        return payload
    record_http_event(url, "failed", error=str(last_error))
    if last_error is not None:
        raise last_error
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def http_post_json(url: str, payload: dict[str, Any], timeout: int = 20) -> Any:
    data = json.dumps(payload).encode("utf-8")
    headers = {**REQUEST_HEADERS, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        record_http_event(url, "live")
        return json.loads(raw.decode("utf-8"))
    except Exception:
        record_http_event(url, "failed")
        raise
