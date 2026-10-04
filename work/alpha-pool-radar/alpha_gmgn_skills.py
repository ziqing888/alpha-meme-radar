"""Read-only GMGN Skills Market adapters for the meme radar.

This module keeps the platform's smart-money feed separate from the
profit-history-gated wallet pool.  A GMGN label is useful live evidence, but
it is not by itself proof that a wallet is independently profitable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_fast_track import write_json
from alpha_gmgn_smart_money_top50 import (
    GMGN_SKILLS_DIR,
    ROOT,
    chain_list,
    gmgn_env,
    normalize_gmgn_trade,
    resolve_gmgn_runner,
)


SMARTMONEY_SOURCE = "gmgn_skills_smartmoney"
TRENDING_SOURCE = "gmgn_skills_trending"
SIGNAL_SOURCE = "gmgn_skills_signal"
TRENCHES_SOURCE = "gmgn_skills_trenches"
KOL_SOURCE = "gmgn_skills_kol"
HOT_SEARCH_SOURCE = "gmgn_skills_hot_searches"
DEFAULT_LIMIT = 50
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_COOLDOWN_SECONDS = 5 * 60
DEFAULT_REQUEST_INTERVAL_SECONDS = 1.0
DEFAULT_FAST_HOT_SEARCH_INTERVAL_SECONDS = 30.0
SUPPORTED_CHAINS = {"sol", "bsc", "base", "eth", "robinhood", "arc", "stable"}
SIGNAL_CHAINS = {"sol", "bsc", "robinhood", "arc", "stable"}
REQUEST_WEIGHTS = {
    "smartmoney": 1,
    "kol": 1,
    "trending": 1,
    "trenches": 3,
    "signal": 3,
    "hot_search": 3,
    "wallet_assessment": 1,
}
SIGNAL_NAMES = {
    1: "price_spike",
    2: "dex_ad",
    3: "social_link_update",
    4: "dex_trending_bar",
    5: "dex_boost",
    6: "price_up",
    7: "price_ath",
    8: "market_cap_key_level",
    9: "live_stream",
    10: "bundler_sell",
    11: "community_takeover",
    12: "smart_money_buy",
    13: "platform_call",
    14: "large_buy",
    15: "multi_buy",
    16: "multi_large_buy",
    17: "bags_claim",
    18: "pump_claim",
    19: "platform_call_v2",
    20: "kol_buy",
    21: "banker_claim",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().astimezone().isoformat(timespec="seconds")


def _epoch(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        if number > 1e11:
            number /= 1000
        return number if number >= 0 else None
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and number not in {float("inf"), float("-inf")} else None
    except (TypeError, ValueError):
        return None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("list", "rows", "rank", "items", "tokens", "records", "trades", "data", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _rows(value)
            if nested:
                return nested
    return []


def _unwrap(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _wallet_screen(payload: Any, chain: str, wallet: str) -> dict[str, Any]:
    stats = _unwrap(payload)
    pnl = stats.get("pnl_stat") if isinstance(stats.get("pnl_stat"), dict) else {}
    common = stats.get("common") if isinstance(stats.get("common"), dict) else {}
    buy = _number(stats.get("buy") or stats.get("buy_count")) or 0
    sell = _number(stats.get("sell") or stats.get("sell_count")) or 0
    trades = buy + sell
    realized = _number(stats.get("realized_profit")) or 0
    bought = _number(stats.get("bought_cost") or stats.get("total_cost")) or 0
    roi = _number(stats.get("realized_profit_pnl") or stats.get("pnl")) or 0
    winrate = _number(pnl.get("winrate")) or 0
    if winrate > 1:
        winrate /= 100
    token_num = _number(pnl.get("token_num")) or 0
    score = (
        35 * _clamp(winrate)
        + 25 * _clamp(roi if abs(roi) <= 1 else roi / 100)
        + 20 * _clamp(trades / 40)
        + 20 * _clamp(token_num / 20)
    )
    return {
        "chain": chain,
        "wallet": wallet,
        "gmgn_wallet_screen_score": round(score, 2),
        "realized_profit": round(realized, 4),
        "realized_profit_pnl": round(roi, 6),
        "winrate": round(winrate, 6),
        "buy_count": int(buy),
        "sell_count": int(sell),
        "trade_count": int(trades),
        "token_count": int(token_num),
        "created_token_count": int(_number(common.get("created_token_count")) or 0),
        "observed_at": _iso(),
    }


def _assessment_age_seconds(payload: dict[str, Any]) -> float:
    stamp = _epoch(payload.get("updated_at") or payload.get("observed_at"))
    return max(0.0, time.time() - stamp) if stamp else float("inf")


def _assess_wallets(
    smart_rows: list[dict[str, Any]],
    assessment_path: Path,
    runner: list[str],
    env: dict[str, str],
    timeout_seconds: int,
    limit: int = 8,
    request_limit: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        cached = json.loads(assessment_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cached = {}
    cached_wallets = cached.get("wallets") if isinstance(cached, dict) and isinstance(cached.get("wallets"), list) else []
    fresh_cached = {
        (str(item.get("chain") or ""), str(item.get("wallet") or "")): item
        for item in cached_wallets
        if isinstance(item, dict) and _assessment_age_seconds(item) <= 3 * 86400
    }

    targets: list[tuple[str, str]] = []
    for row in smart_rows:
        chain = str(row.get("chain") or "").strip().lower()
        wallet = str(row.get("wallet") or row.get("maker") or "").strip()
        key = (chain, wallet)
        if chain and wallet and key not in targets:
            targets.append(key)
        if len(targets) >= max(1, limit):
            break
    assessments: list[dict[str, Any]] = list(fresh_cached.values())
    errors: list[dict[str, Any]] = []
    pending = [(chain, wallet) for chain, wallet in targets if (chain, wallet) not in fresh_cached]
    requested = 0
    for chain, wallet in pending[: max(0, request_limit)]:
        requested += 1
        try:
            payload, proc = _run_cli(
                runner,
                env,
                ["portfolio", "stats", "--chain", chain, "--wallet", wallet, "--period", "7d"],
                timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append({"chain": chain, "wallet": wallet, "reason": type(exc).__name__})
            continue
        if _rate_limited(proc, payload):
            errors.append({"chain": chain, "wallet": wallet, "reason": "rate_limited"})
            break
        if proc.returncode or payload is None:
            errors.append({"chain": chain, "wallet": wallet, "reason": "upstream_failed"})
            continue
        item = _wallet_screen(payload, chain, wallet)
        fresh_cached[(chain, wallet)] = item
        assessments = list(fresh_cached.values())
    mode = "live_incremental" if requested else "cached"
    status = {"ok": bool(assessments) or not targets, "mode": mode, "count": len(assessments), "request_count": requested, "pending_count": max(0, len(pending) - requested), "errors": errors, "updated_at": _iso()}
    if assessments and requested:
        write_json(assessment_path, {"source": "gmgn_wallet_score", "updated_at": _iso(), "wallets": assessments})
    return assessments, status


def _rate_limited(proc: subprocess.CompletedProcess[str], payload: Any) -> bool:
    text = f"{proc.stderr or ''} {proc.stdout or ''}"
    if isinstance(payload, dict):
        text += " " + json.dumps(payload, ensure_ascii=False)
    return bool(re.search(r"\b429\b|rate[_ -]?limit|too many requests|cooldown", text, re.I))


def _cooldown(path: Path) -> float:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return float(payload.get("retry_after_epoch") or 0)
    except (OSError, ValueError, TypeError):
        return 0.0


def _retry_epoch(proc: subprocess.CompletedProcess[str], payload: Any) -> float:
    deadline = time.time() + DEFAULT_COOLDOWN_SECONDS
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend((payload.get("reset_at"), payload.get("retry_after_epoch"), payload.get("retry_after")))
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend((data.get("reset_at"), data.get("retry_after_epoch"), data.get("retry_after")))
    text = f"{proc.stderr or ''} {proc.stdout or ''}"
    match = re.search(r"(?:reset_at|resetAt|retry_after_epoch)[^0-9]{0,20}(\d{10,13})", text, re.I)
    if match:
        candidates.append(match.group(1))
    for value in candidates:
        deadline = max(deadline, _epoch(value) or 0)
    return deadline


def _write_shared_cooldown(path: Path, retry_after_epoch: float, source: str) -> None:
    write_json(path, {
        "ok": False,
        "rate_limited": True,
        "retry_after_epoch": retry_after_epoch,
        "retry_after": datetime.fromtimestamp(retry_after_epoch, timezone.utc).isoformat(),
        "source": source,
    })


def _write_status(path: Path, status: dict[str, Any]) -> None:
    write_json(path, {**status, "checked_at": _iso()})


def _run_cli(runner: list[str], env: dict[str, str], args: list[str], timeout: int) -> tuple[Any, subprocess.CompletedProcess[str]]:
    proc = subprocess.run(
        [*runner, *args, "--raw"],
        cwd=GMGN_SKILLS_DIR if GMGN_SKILLS_DIR.exists() else ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(3, timeout),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.loads(proc.stdout or "")
    except (TypeError, ValueError):
        payload = None
    return payload, proc


def _normalize_smartmoney(row: dict[str, Any], chain: str) -> dict[str, Any] | None:
    return _normalize_tracked_trade(row, chain, SMARTMONEY_SOURCE, "smartmoney_trade")


def _normalize_kol(row: dict[str, Any], chain: str) -> dict[str, Any] | None:
    return _normalize_tracked_trade(row, chain, KOL_SOURCE, "kol_trade")


def _normalize_tracked_trade(
    row: dict[str, Any],
    chain: str,
    source: str,
    skill_kind: str,
) -> dict[str, Any] | None:
    normalized = normalize_gmgn_trade(row, chain)
    token = str(row.get("base_address") or normalized.get("tokenAddress") or "").strip()
    wallet = str(row.get("maker") or normalized.get("wallet") or "").strip()
    side = str(row.get("side") or "").strip().lower()
    tx = str(row.get("transaction_hash") or row.get("tx_hash") or "").strip()
    if not token or not wallet or side not in {"buy", "sell"} or not tx:
        return None
    return {
        **normalized,
        "source": source,
        "source_family": source,
        "chain": chain,
        "tokenAddress": token,
        "contract_address": token,
        "token_address": token,
        "wallet": wallet,
        "wallet_address": wallet,
        "maker": wallet,
        "side": side,
        "event_type": side,
        "direction": side,
        "monitor985_trade_side": side,
        "amount_usd": _number(row.get("amount_usd")),
        "monitor985_trade_amount_usd": _number(row.get("amount_usd")),
        "transaction_hash": tx,
        "tx_hash": tx,
        "observed_at": row.get("timestamp") or row.get("time") or _iso(),
        "gmgn_skill_kind": skill_kind,
        "gmgn_wallet_tags": (row.get("maker_info") or {}).get("tags") if isinstance(row.get("maker_info"), dict) else [],
        "is_open_or_close": row.get("is_open_or_close"),
        "price_usd": row.get("price_usd"),
        "price_now": row.get("price_now"),
    }


def _normalize_trending(row: dict[str, Any], chain: str, rank: int) -> dict[str, Any] | None:
    token = str(row.get("address") or row.get("token_address") or row.get("tokenAddress") or row.get("base_address") or "").strip()
    if not token:
        return None
    smart_count = _number(row.get("smart_degen_count"))
    return {
        **row,
        "source": TRENDING_SOURCE,
        "source_family": TRENDING_SOURCE,
        "chain": chain,
        "tokenAddress": token,
        "contract_address": token,
        "token_address": token,
        "symbol": row.get("symbol") or row.get("name") or "UNKNOWN",
        "smart_money": smart_count or 0,
        "gmgn_smart_degen_count": smart_count or 0,
        "gmgn_renowned_count": _number(row.get("renowned_count")) or 0,
        "gmgn_bot_degen_count": _number(row.get("bot_degen_count")) or 0,
        "gmgn_market_rank": rank,
        "gmgn_skill_kind": "market_trending",
        "observed_at": _iso(),
    }


def _normalize_trenches(row: dict[str, Any], chain: str, stage: str) -> dict[str, Any] | None:
    token = str(row.get("address") or row.get("token_address") or row.get("tokenAddress") or "").strip()
    if not token:
        return None
    return {
        **row,
        "source": TRENCHES_SOURCE,
        "source_family": TRENCHES_SOURCE,
        "chain": chain,
        "tokenAddress": token,
        "contract_address": token,
        "token_address": token,
        "gmgn_trenches_stage": stage,
        "gmgn_skill_kind": "market_trenches",
        "observed_at": row.get("created_timestamp") or row.get("creation_timestamp") or _iso(),
    }


def _trenches_rows(payload: Any, chain: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key, stage in (("new_creation", "new_creation"), ("pump", "near_completion"), ("near_completion", "near_completion"), ("completed", "completed")):
        values = data.get(key)
        if isinstance(values, list):
            rows.extend(item for row in values if isinstance(row, dict) and (item := _normalize_trenches(row, chain, stage)))
    return rows


def _normalize_signal(row: dict[str, Any], chain: str) -> dict[str, Any] | None:
    token = str(row.get("token_address") or row.get("tokenAddress") or row.get("address") or "").strip()
    if not token:
        return None
    signal_type = int(_number(row.get("signal_type")) or 0)
    current = row.get("cur_data") if isinstance(row.get("cur_data"), dict) else {}
    return {
        **row,
        "source": SIGNAL_SOURCE,
        "source_family": SIGNAL_SOURCE,
        "chain": chain,
        "tokenAddress": token,
        "contract_address": token,
        "token_address": token,
        "market_cap": _number(row.get("market_cap")),
        "first_market_cap": _number(row.get("first_trigger_mc")),
        "signal_market_cap": _number(row.get("trigger_mc")),
        "liquidity": _number(current.get("liquidity")),
        "holder_count": _number(current.get("holder_count")),
        "top_10_holder_rate": _number(current.get("top_10_holder_rate")),
        "gmgn_signal_type": signal_type,
        "gmgn_signal_name": SIGNAL_NAMES.get(signal_type, f"signal_{signal_type}"),
        "gmgn_exit_signal": signal_type == 10,
        "gmgn_skill_kind": "market_signal",
        "observed_at": row.get("trigger_at") or _iso(),
    }


def _hot_search_rows(payload: Any) -> list[dict[str, Any]]:
    blocks = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(blocks, list):
        return []
    rows: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        chain = str(block.get("chain") or "").strip().lower()
        tokens = block.get("tokens")
        if not chain or not isinstance(tokens, list):
            continue
        for index, row in enumerate(tokens, start=1):
            if not isinstance(row, dict):
                continue
            token = str(row.get("address") or row.get("token_address") or row.get("tokenAddress") or "").strip()
            if not token:
                continue
            rows.append({
                **row,
                "source": HOT_SEARCH_SOURCE,
                "source_family": HOT_SEARCH_SOURCE,
                "chain": chain,
                "tokenAddress": token,
                "contract_address": token,
                "token_address": token,
                "gmgn_hot_search_rank": int(_number(row.get("rank")) or index),
                "gmgn_visiting_count": _number(row.get("visiting_count")) or 0,
                "gmgn_skill_kind": "market_hot_search",
                "observed_at": _iso(),
            })
    return rows


def refresh_fast_hot_search(
    out_dir: Path,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout_seconds: int = 6,
    min_interval_seconds: float | None = None,
    chains: list[str] | None = None,
) -> dict[str, Any]:
    """Refresh only GMGN hot searches for the low-latency monitor path."""
    started = time.monotonic()
    out_dir = Path(out_dir)
    inbox = out_dir / "meme-source-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    feed_path = inbox / "gmgn-skills-hot-searches.json"
    status_path = out_dir / "gmgn-skills-fast-status.json"
    shared_cooldown_path = out_dir / "gmgn-wallet-profit-query-status.json"

    try:
        previous_status = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        previous_status = {}
    try:
        cached_rows = _cached_feed_rows(feed_path)
    except (OSError, ValueError, TypeError):
        cached_rows = []

    if min_interval_seconds is None:
        try:
            min_interval_seconds = float(
                os.environ.get(
                    "GMGN_FAST_HOT_SEARCH_INTERVAL_SECONDS",
                    DEFAULT_FAST_HOT_SEARCH_INTERVAL_SECONDS,
                )
            )
        except (TypeError, ValueError):
            min_interval_seconds = DEFAULT_FAST_HOT_SEARCH_INTERVAL_SECONDS
    min_interval_seconds = min(120.0, max(5.0, min_interval_seconds))
    last_request_epoch = _epoch(previous_status.get("last_request_at"))
    now_epoch = time.time()
    if last_request_epoch is not None and now_epoch - last_request_epoch < min_interval_seconds:
        status = {
            "ok": bool(previous_status.get("ok", True)),
            "source": "gmgn_skills_fast_hot_search",
            "skipped": True,
            "reason": "min_interval",
            "last_request_at": previous_status.get("last_request_at"),
            "next_request_at": datetime.fromtimestamp(
                last_request_epoch + min_interval_seconds, timezone.utc
            ).isoformat(),
            "row_count": len(cached_rows),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        _write_status(status_path, status)
        return status

    cooldown_until = max(_cooldown(shared_cooldown_path), _cooldown(status_path))
    if cooldown_until > now_epoch:
        status = {
            "ok": False,
            "source": "gmgn_skills_fast_hot_search",
            "skipped": True,
            "reason": "rate_limit_backoff",
            "retry_after_epoch": cooldown_until,
            "retry_after": datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat(),
            "last_request_at": previous_status.get("last_request_at"),
            "row_count": len(cached_rows),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        _write_status(status_path, status)
        return status

    requested_at = _iso()
    runner, runner_status = resolve_gmgn_runner()
    env, env_status = gmgn_env()
    env = dict(env)
    env.pop("GMGN_PRIVATE_KEY", None)
    status: dict[str, Any] = {
        "ok": False,
        "source": "gmgn_skills_fast_hot_search",
        "runner": runner_status,
        "env": env_status,
        "request_count": 0,
        "last_request_at": requested_at,
        "row_count": len(cached_rows),
    }
    if not runner:
        status["reason"] = "gmgn_cli_missing"
        status["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_status(status_path, status)
        return status

    chains = [chain for chain in (chains or chain_list()) if chain in SUPPORTED_CHAINS]
    if not chains:
        status["reason"] = "no_supported_chain"
        status["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_status(status_path, status)
        return status
    args = ["market", "hot-searches"]
    for chain in chains:
        args.extend(["--chain", chain])
    args.extend(["--interval", "5m", "--limit", str(min(50, max(1, limit)))])
    status["request_count"] = 1
    status["chains"] = chains
    try:
        payload, proc = _run_cli(runner, env, args, timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        status["reason"] = type(exc).__name__
        status["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_status(status_path, status)
        return status
    if _rate_limited(proc, payload):
        retry = _retry_epoch(proc, payload)
        _write_shared_cooldown(shared_cooldown_path, retry, "gmgn_skills_fast_hot_search")
        status.update({
            "reason": "rate_limited",
            "retry_after_epoch": retry,
            "retry_after": datetime.fromtimestamp(retry, timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        })
        _write_status(status_path, status)
        return status
    if proc.returncode or payload is None:
        status["reason"] = "upstream_failed"
        status["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_status(status_path, status)
        return status

    rows = _hot_search_rows(payload)
    if not rows:
        status["reason"] = "empty"
        status["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_status(status_path, status)
        return status
    write_json(feed_path, {
        "source": HOT_SEARCH_SOURCE,
        "updated_at": _iso(),
        "rows": rows,
        "status": {"row_count": len(rows), "lane": "fast_hot_search"},
    })
    status.update({
        "ok": True,
        "row_count": len(rows),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    })
    _write_status(status_path, status)
    return status


def _cached_feed_rows(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def refresh(
    out_dir: Path,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    request_interval_seconds: float | None = None,
) -> dict[str, Any]:
    """Collect GMGN discovery, flow and context feeds under one request budget."""
    out_dir = Path(out_dir)
    inbox = out_dir / "meme-source-inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    status_path = out_dir / "gmgn-skills-status.json"
    shared_cooldown_path = out_dir / "gmgn-wallet-profit-query-status.json"
    previous_status: dict[str, Any] = {}
    try:
        previous_status = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    cooldown_until = max(
        _cooldown(status_path),
        _cooldown(shared_cooldown_path),
        float(previous_status.get("retry_after_epoch") or 0),
    )
    now_epoch = time.time()
    if request_interval_seconds is None:
        try:
            request_interval_seconds = float(
                os.environ.get("GMGN_SKILLS_REQUEST_INTERVAL_SECONDS", DEFAULT_REQUEST_INTERVAL_SECONDS)
            )
        except (TypeError, ValueError):
            request_interval_seconds = DEFAULT_REQUEST_INTERVAL_SECONDS
    request_interval_seconds = min(5.0, max(0.0, request_interval_seconds))
    if cooldown_until > now_epoch:
        retry_lane = str(previous_status.get("next_lane") or previous_status.get("next_slow_lane") or "discovery")
        retry_lane = {"flow_context": "context", "hot_search": "context"}.get(retry_lane, retry_lane)
        if retry_lane not in {"discovery", "flow", "context"}:
            retry_lane = "discovery"
        status = {
            "ok": False,
            "source": "gmgn_skills_market",
            "skipped": True,
            "reason": "rate_limit_backoff",
            "retry_after_epoch": cooldown_until,
            "retry_after": datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat(),
            "lane": retry_lane,
            "next_lane": retry_lane,
            "next_slow_lane": retry_lane,
            "next_lane_cursor": previous_status.get("next_lane_cursor") or 0,
        }
        _write_status(status_path, status)
        return status

    runner, runner_status = resolve_gmgn_runner()
    env, env_status = gmgn_env()
    env = dict(env)
    env.pop("GMGN_PRIVATE_KEY", None)
    lane = str(previous_status.get("next_lane") or previous_status.get("next_slow_lane") or "discovery")
    lane = {"flow_context": "context", "hot_search": "context"}.get(lane, lane)
    if lane not in {"discovery", "flow", "context"}:
        lane = "discovery"
    next_lane = {"discovery": "flow", "flow": "context", "context": "discovery"}[lane]
    try:
        lane_cursor = max(0, int(previous_status.get("next_lane_cursor") or 0))
    except (TypeError, ValueError):
        lane_cursor = 0
    shared_key_mode = bool(env_status.get("default_read_only_key_used"))
    status: dict[str, Any] = {
        "ok": False,
        "source": "gmgn_skills_market",
        "runner": runner_status,
        "env": env_status,
        "chains": [],
        "lane": lane,
        "next_lane": next_lane,
        "next_lane_cursor": 0,
        # Kept for older dashboard/status readers.
        "slow_lane": lane,
        "next_slow_lane": next_lane,
        "request_count": 0,
        "request_weight": 0,
        "shared_key_mode": shared_key_mode,
        "request_interval_seconds": request_interval_seconds,
        "errors": [],
    }
    if not runner:
        status["reason"] = "gmgn_cli_missing"
        _write_status(status_path, status)
        return status

    try:
        checked = subprocess.run(
            [*runner, "config", "--check"],
            cwd=GMGN_SKILLS_DIR if GMGN_SKILLS_DIR.exists() else ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(3, timeout_seconds),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        status["reason"] = "gmgn_config_check_failed"
        status["errors"].append(type(exc).__name__)
        _write_status(status_path, status)
        return status
    if checked.returncode:
        status["reason"] = "gmgn_config_check_failed"
        _write_status(status_path, status)
        return status
    if _rate_limited(checked, None):
        retry = _retry_epoch(checked, None)
        _write_shared_cooldown(shared_cooldown_path, retry, "gmgn_skills_market")
        status.update({"reason": "rate_limited", "retry_after_epoch": retry, "retry_after": datetime.fromtimestamp(retry, timezone.utc).isoformat()})
        _write_status(status_path, status)
        return status

    chains = [chain for chain in chain_list() if chain in SUPPORTED_CHAINS]
    status["chains"] = chains
    feeds: dict[str, list[dict[str, Any]]] = {
        "signal": [],
        "trenches": [],
        "smartmoney": [],
        "trending": [],
        "kol": [],
        "hot_search": [],
    }
    plan: list[tuple[str, str, list[str]]] = []
    bounded = str(min(50, max(1, limit)))
    if lane == "discovery":
        for chain in chains:
            if chain in SIGNAL_CHAINS:
                plan.append(("signal", chain, ["market", "signal", "--chain", chain]))
        for chain in chains:
            plan.append(("trenches", chain, ["market", "trenches", "--chain", chain, "--type", "new_creation", "--type", "near_completion", "--type", "completed", "--filter-preset", "safe", "--limit", bounded]))
    elif lane == "flow":
        for chain in chains:
            plan.append(("smartmoney", chain, ["track", "smartmoney", "--chain", chain, "--limit", str(min(200, max(1, limit)))]))
        for chain in chains:
            plan.append(("trending", chain, ["market", "trending", "--chain", chain, "--interval", "5m", "--order-by", "smart_degen_count", "--limit", bounded]))
    elif lane == "context":
        for chain in chains:
            plan.append(("kol", chain, ["track", "kol", "--chain", chain, "--limit", str(min(200, max(1, limit)))]))
        if chains:
            args = ["market", "hot-searches"]
            for chain in chains:
                args.extend(["--chain", chain])
            args.extend(["--interval", "5m", "--limit", bounded])
            plan.append(("hot_search", "multi", args))
    if shared_key_mode and plan:
        plan_size = len(plan)
        lane_cursor %= plan_size
        plan = [plan[lane_cursor]]
        if lane_cursor + 1 < plan_size:
            status["next_lane"] = lane
            status["next_slow_lane"] = lane
            status["next_lane_cursor"] = lane_cursor + 1
        else:
            status["next_lane_cursor"] = 0

    successful_kinds: set[str] = set()
    for request_index, (kind, chain, args) in enumerate(plan):
        if request_index and request_interval_seconds:
            time.sleep(request_interval_seconds)
        status["request_count"] += 1
        status["request_weight"] += REQUEST_WEIGHTS[kind]
        try:
            payload, proc = _run_cli(runner, env, args, timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            status["errors"].append({"chain": chain, "kind": kind, "reason": type(exc).__name__})
            continue
        if _rate_limited(proc, payload):
            retry = _retry_epoch(proc, payload)
            _write_shared_cooldown(shared_cooldown_path, retry, "gmgn_skills_market")
            status.update({
                "reason": "rate_limited",
                "retry_after_epoch": retry,
                "retry_after": datetime.fromtimestamp(retry, timezone.utc).isoformat(),
                "next_lane": lane,
                "next_slow_lane": lane,
                "next_lane_cursor": lane_cursor if shared_key_mode else 0,
            })
            _write_status(status_path, status)
            return status
        if proc.returncode or payload is None:
            status["errors"].append({"chain": chain, "kind": kind, "reason": "upstream_failed"})
            continue
        successful_kinds.add(kind)
        if kind == "signal":
            feeds[kind].extend(item for row in _rows(payload) if (item := _normalize_signal(row, chain)))
        elif kind == "trenches":
            feeds[kind].extend(_trenches_rows(payload, chain))
        elif kind == "smartmoney":
            feeds[kind].extend(item for row in _rows(payload) if (item := _normalize_smartmoney(row, chain)))
        elif kind == "kol":
            feeds[kind].extend(item for row in _rows(payload) if (item := _normalize_kol(row, chain)))
        elif kind == "trending":
            feeds[kind].extend(item for index, row in enumerate(_rows(payload), start=1) if (item := _normalize_trending(row, chain, index)))
        elif kind == "hot_search":
            feeds[kind].extend(_hot_search_rows(payload))

    file_specs = {
        "signal": ("gmgn-skills-signal.json", SIGNAL_SOURCE),
        "trenches": ("gmgn-skills-trenches.json", TRENCHES_SOURCE),
        "smartmoney": ("gmgn-skills-smartmoney.json", SMARTMONEY_SOURCE),
        "trending": ("gmgn-skills-trending.json", TRENDING_SOURCE),
        "kol": ("gmgn-skills-kol.json", KOL_SOURCE),
        "hot_search": ("gmgn-skills-hot-searches.json", HOT_SEARCH_SOURCE),
    }
    assessment_path = out_dir / "gmgn-skills-wallet-assessment.json"
    assessment_rows = feeds["smartmoney"]
    if lane == "context":
        assessment_rows = _cached_feed_rows(inbox / file_specs["smartmoney"][0])
    assessments, assessment_status = _assess_wallets(
        assessment_rows,
        assessment_path,
        runner,
        env,
        timeout_seconds,
        request_limit=1 if lane == "context" and not shared_key_mode else 0,
    )
    status["request_count"] += int(assessment_status.get("request_count") or 0)
    status["request_weight"] += int(assessment_status.get("request_count") or 0) * REQUEST_WEIGHTS["wallet_assessment"]
    assessment_by_wallet = {(item.get("chain"), item.get("wallet")): item for item in assessments}
    for row in feeds["smartmoney"]:
        item = assessment_by_wallet.get((row.get("chain"), row.get("wallet")))
        if item:
            row["gmgn_wallet_screen_score"] = item.get("gmgn_wallet_screen_score")
            row["gmgn_wallet_screen"] = item
    for kind in successful_kinds:
        filename, source = file_specs[kind]
        feed_status: dict[str, Any] = {"row_count": len(feeds[kind]), "lane": lane}
        if kind == "smartmoney":
            feed_status["wallet_assessment"] = assessment_status
        write_json(inbox / filename, {"source": source, "updated_at": _iso(), "rows": feeds[kind], "status": feed_status})
    effective_feeds = {
        kind: feeds[kind] if kind in successful_kinds else _cached_feed_rows(inbox / filename)
        for kind, (filename, _source) in file_specs.items()
    }
    smart_rows = effective_feeds["smartmoney"]
    buy_rows = [row for row in smart_rows if row.get("side") == "buy"]
    sell_rows = [row for row in smart_rows if row.get("side") == "sell"]
    status.update({
        "ok": bool(successful_kinds),
        "refreshed_feeds": sorted(successful_kinds),
        "signal_rows": len(effective_feeds["signal"]),
        "trenches_rows": len(effective_feeds["trenches"]),
        "smartmoney_rows": len(smart_rows),
        "smartmoney_unique_wallet_count": len({str(row.get("wallet")) for row in smart_rows if row.get("wallet")}),
        "smartmoney_unique_token_count": len({str(row.get("tokenAddress")) for row in smart_rows if row.get("tokenAddress")}),
        "smartmoney_buy_wallet_count": len({str(row.get("wallet")) for row in buy_rows if row.get("wallet")}),
        "smartmoney_sell_wallet_count": len({str(row.get("wallet")) for row in sell_rows if row.get("wallet")}),
        "smartmoney_buy_rows": len(buy_rows),
        "smartmoney_sell_rows": len(sell_rows),
        "trending_rows": len(effective_feeds["trending"]),
        "kol_rows": len(effective_feeds["kol"]),
        "hot_search_rows": len(effective_feeds["hot_search"]),
        "wallet_assessment": assessment_status,
    })
    if not status["ok"] and not status["errors"]:
        status["reason"] = "empty"
    _write_status(status_path, status)
    return status
