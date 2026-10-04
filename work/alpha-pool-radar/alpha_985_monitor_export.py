"""Read-only 985monitor public event exporter for Meme source aggregation."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "https://985monitor.xyz"
DEFAULT_OUT = ROOT / "outputs" / "meme-source-inbox" / "985-monitor.json"
DEFAULT_FOMO_OUT = ROOT / "outputs" / "meme-source-inbox" / "985-fomo-wallets.json"
DEFAULT_SMARTMONEY_OUT = ROOT / "outputs" / "meme-source-inbox" / "985-smartmoney.json"
DEFAULT_SMART_WALLETS_OUT = ROOT / "outputs" / "wallet-candidates" / "985-smart-wallet-candidates.json"
DEFAULT_CROSS_CHAIN_WATCH_OUT = ROOT / "outputs" / "wallet-candidates" / "evm-cross-chain-watch.json"
DEFAULT_STATUS = ROOT / "outputs" / "985-monitor-export-status.json"
DEFAULT_ENDPOINTS = {
    "dex_events": "/api/dex-events?limit={limit}",
    "pump_trades": "/api/pump-trade-events?limit={limit}",
    "pump_callouts": "/api/pump-callout-events?limit={limit}",
}
DEFAULT_FOMO_LEADERBOARDS_PATH = "/fomo-leaderboards.json"
DEFAULT_FOMO_PROFILE_PATH = "/api/fomo-watch/profile?handle={handle}&limit={limit}"
DEFAULT_SMARTMONEY_DATA_PATH = "/smartmoney/data.json"
CHAIN_ALIASES = {
    "56": "bsc",
    "bnb": "bsc",
    "bsc": "bsc",
    "bnb chain": "bsc",
    "binance smart chain": "bsc",
    "1399811149": "solana",
    "sol": "solana",
    "solana": "solana",
    "4663": "robinhood",
    "robinhood": "robinhood",
    "robinhood chain": "robinhood",
    "8453": "base",
    "base": "base",
    "1": "ethereum",
    "eth": "ethereum",
    "ethereum": "ethereum",
}
EVM_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
SOLANA_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
SMART_WALLET_POLICY = {
    "provider_reported_pnl_gt": 500,
    "provider_reported_win_rate_min": 50,
    "provider_reported_samples_min": 10,
}


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def optional_int(value: Any) -> int | None:
    number = optional_float(value)
    return int(number) if number is not None else None


def normalize_chain(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", " ").replace("-", " ")
    return CHAIN_ALIASES.get(raw, raw.replace(" ", "-"))


def first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def fetch_json(url: str, timeout_seconds: int) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AlphaRadar/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8", errors="replace"))


def fetch_text(url: str, timeout_seconds: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AlphaRadar/1.0",
            "Accept": "application/json,text/csv,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        return response.read().decode("utf-8-sig", errors="replace")


def endpoint_url(base_url: str, path_template: str, limit: int) -> str:
    base = base_url.rstrip("/")
    return f"{base}{path_template.format(limit=max(1, limit))}"


def public_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def event_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return [event for event in payload["events"] if isinstance(event, dict)]
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    return []


def iso_from_ms(value: Any) -> str:
    number = to_float(value)
    if number <= 0:
        return ""
    if number < 10_000_000_000:
        number *= 1000
    return datetime.fromtimestamp(number / 1000).astimezone().isoformat(timespec="seconds")


def pair_age_hours_from_ms(value: Any) -> float | None:
    number = to_float(value)
    if number <= 0:
        return None
    if number < 10_000_000_000:
        number *= 1000
    return round(max(0.0, (time.time() * 1000 - number) / 3_600_000), 4)


def row_base(
    *,
    chain: str,
    address: str,
    symbol: str,
    name: str,
    kind: str,
    event: dict[str, Any],
    source_family: str = "985_monitor",
    source_origin: str = "985monitor_public_api",
) -> dict[str, Any]:
    created_at = event.get("createdAt") or None
    event_key = event.get("key") or ""
    return {
        "chain": chain,
        "chainId": chain,
        "address": address,
        "tokenAddress": address,
        "contract_address": address,
        "symbol": symbol,
        "name": name or symbol,
        "source_family": source_family,
        "source_origin": source_origin,
        "provider_family": "985",
        "provider_feed": source_family,
        "provider_event_id": event_key,
        "monitor985_kind": kind,
        "monitor985_event_key": event_key,
        "monitor985_created_at": created_at,
        "event_at": created_at or iso_from_ms(event.get("savedAt")),
        "observed_at": created_at or iso_from_ms(event.get("savedAt")),
        "provenance_status": "known",
    }


def normalize_dex_event(event: dict[str, Any]) -> dict[str, Any] | None:
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    meta = content.get("dexMeta") if isinstance(content.get("dexMeta"), dict) else {}
    chain = normalize_chain(first_present(meta, "chain", "chainId", "networkId"))
    address = str(first_present(meta, "ca", "address", "tokenAddress") or "").strip()
    if not chain or not address:
        return None
    age_days = first_present(meta, "ageDays")
    pair_age_hours = to_float(age_days) * 24 if age_days not in (None, "") else pair_age_hours_from_ms(meta.get("pairCreatedAt"))
    market_cap = optional_float(first_present(meta, "mc", "marketCap", "fdv"))
    liquidity = optional_float(first_present(meta, "liq", "liquidity"))
    volume_24h = optional_float(first_present(meta, "vol24", "volume24h"))
    price = optional_float(first_present(meta, "priceUsd", "price_usd"))
    price_change_24h = optional_float(first_present(meta, "priceChg24", "priceChange24h"))
    txns_24h = optional_int(first_present(meta, "txnsH24", "txns24h"))
    pay_count = optional_int(meta.get("payCount"))
    row = row_base(
        chain=chain,
        address=address,
        symbol=str(first_present(meta, "symbol") or "").strip(),
        name=str(first_present(meta, "name") or "").strip(),
        kind="dex_paid",
        event=event,
    )
    row.update(
        {
            "marketCap": market_cap,
            "market_cap": market_cap,
            "liquidity": liquidity,
            "liquidity_usd": liquidity,
            "volume": volume_24h,
            "volume24h": volume_24h,
            "price": price,
            "priceUsd": str(first_present(meta, "priceUsd")) if first_present(meta, "priceUsd") is not None else None,
            "priceChange24h": price_change_24h,
            "price_change_percent24h": price_change_24h,
            "txns24h": txns_24h,
            "pair_age_hours": round(pair_age_hours, 4) if pair_age_hours is not None else None,
            "rank_score": 45 + min(25, (pay_count or 0) * 4) + (10 if meta.get("isFirstPaid") else 0),
            "monitor985_order_type": meta.get("orderType") or "",
            "monitor985_pay_count": pay_count,
            "monitor985_is_first_paid": bool(meta.get("isFirstPaid")),
            "url": meta.get("url") or "",
            "dex_url": meta.get("url") or "",
        }
    )
    return row


def normalize_pump_trade_event(event: dict[str, Any]) -> dict[str, Any] | None:
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    trade = content.get("pumpTrade") if isinstance(content.get("pumpTrade"), dict) else {}
    side = str(trade.get("side") or "").strip().lower()
    chain = normalize_chain(first_present(trade, "chainName", "networkId", "chainId"))
    address = str(first_present(trade, "mint", "tokenAddress", "address") or "").strip()
    if not chain or not address:
        return None
    row = row_base(
        chain=chain,
        address=address,
        symbol=str(first_present(trade, "symbol") or "").strip(),
        name=str(first_present(trade, "name") or "").strip(),
        kind=f"pump_trade_{side}" if side in {"buy", "sell"} else "pump_trade",
        event=event,
    )
    amount_usd = optional_float(first_present(trade, "netAmountUsd", "amountUsd"))
    market_cap = optional_float(first_present(trade, "marketCapUsd", "marketCap"))
    price = optional_float(first_present(trade, "priceUsd", "price_usd"))
    row.update(
        {
            "marketCap": market_cap,
            "market_cap": market_cap,
            "price": price,
            "priceUsd": str(first_present(trade, "priceUsd")) if first_present(trade, "priceUsd") is not None else None,
            "volume": amount_usd,
            "volume24h": amount_usd,
            "smart_money": 1,
            "rank_score": 38 + min(22, (amount_usd or 0) / 10) + min(12, to_float(trade.get("followers")) / 1000),
            "monitor985_trade_side": trade.get("side") or "",
            "monitor985_trade_amount_usd": amount_usd,
            "monitor985_wallet": trade.get("wallet") or "",
            "monitor985_wallet_name": trade.get("walletName") or trade.get("watchName") or "",
            "monitor985_username": trade.get("username") or trade.get("xUsername") or "",
            "monitor985_followers": optional_int(trade.get("followers")),
            "monitor985_tx_hash": first_present(trade, "txHash", "transactionHash", "signature") or "",
            "tx_hash": first_present(trade, "txHash", "transactionHash", "signature") or "",
            "tx_url": trade.get("txUrl") or "",
            "direction": side or None,
            "amount_usd": amount_usd,
            "launchpad_platform": "Pump.fun" if str(trade.get("program") or "").lower().startswith("pump") else "",
            "launchpad_lifecycle_stage": "curve_accelerating" if trade.get("isBondingCurve") else "confirmed_market",
            "launchpad_stage_label": (
                "Pump 买入" if side == "buy" and trade.get("isBondingCurve")
                else "链上买入" if side == "buy"
                else "Pump 卖出" if trade.get("isBondingCurve")
                else "链上卖出"
            ),
        }
    )
    return row


def normalize_pump_callout_event(event: dict[str, Any]) -> dict[str, Any] | None:
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    meta = content.get("pumpMeta") if isinstance(content.get("pumpMeta"), dict) else {}
    chain = normalize_chain(first_present(event, "chainName", "networkId") or first_present(meta, "chainName", "networkId"))
    address = str(first_present(event, "tokenAddress") or first_present(meta, "coinMint", "tokenAddress") or "").strip()
    if not chain or not address:
        return None
    username = str(first_present(meta, "username") or event.get("handle") or event.get("userName") or "").strip()
    row = row_base(
        chain=chain,
        address=address,
        symbol=str(event.get("symbol") or "").strip(),
        name=str(event.get("symbol") or "").strip(),
        kind="pump_callout",
        event=event,
    )
    market_cap = optional_float(first_present(meta, "marketCap") or event.get("marketCap"))
    row.update(
        {
            "marketCap": market_cap,
            "market_cap": market_cap,
            "smart_money": 1 if username else 0,
            "kol": 1 if username else 0,
            "rank_score": 40 + min(20, max(0, to_float(meta.get("maxMultiplier")) - 1) * 4) + min(10, to_int(event.get("followers")) / 1000),
            "monitor985_username": username,
            "monitor985_thesis": first_present(meta, "thesis") or event.get("comment") or "",
            "monitor985_callout_multiple": optional_float(meta.get("multiple")),
            "monitor985_callout_max_multiplier": optional_float(meta.get("maxMultiplier")),
            "monitor985_likes": optional_int(meta.get("likes")),
            "monitor985_reply_count": optional_int(meta.get("replyCount")),
            "monitor985_holding_usd": optional_float(event.get("holdingUsd")),
            "monitor985_followers": optional_int(event.get("followers")),
        }
    )
    return row


def normalize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    source = str(event.get("source") or event.get("eventType") or "").lower()
    if "dex" in source:
        return normalize_dex_event(event)
    if "pump-trade" in source or str(event.get("eventType") or "").upper() == "PUMP_TRADE":
        return normalize_pump_trade_event(event)
    if "pump-callout" in source or str(event.get("eventType") or "").upper() == "PUMP_CALLOUT":
        return normalize_pump_callout_event(event)
    return None


def leaderboard_handles(payload: Any, handle_limit: int) -> list[dict[str, Any]]:
    boards = payload.get("boards") if isinstance(payload, dict) else {}
    common = payload.get("commonFollowing") if isinstance(payload, dict) else []
    seen: set[str] = set()
    leaders: list[dict[str, Any]] = []
    for board_name in ("24h", "7d", "30d", "all"):
        rows = boards.get(board_name) if isinstance(boards, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows[: max(2, handle_limit)]:
            if not isinstance(row, dict):
                continue
            handle = str(row.get("handle") or "").strip().lower()
            if not handle or handle in seen:
                continue
            seen.add(handle)
            leaders.append({**row, "fomo_board": board_name})
            if len(leaders) >= handle_limit:
                return leaders
    if isinstance(common, list):
        for row in common[: max(2, handle_limit)]:
            if not isinstance(row, dict):
                continue
            handle = str(row.get("handle") or "").strip().lower()
            if not handle or handle in seen:
                continue
            seen.add(handle)
            leaders.append({**row, "fomo_board": "common"})
            if len(leaders) >= handle_limit:
                break
    return leaders


def fomo_quality_score(profile: dict[str, Any], leader: dict[str, Any]) -> float:
    rank = max(1, to_int(leader.get("rank")))
    followers = to_float(profile.get("followers") or leader.get("followers"))
    pnl = max(0.0, to_float(leader.get("pnl")))
    trades = to_float(leader.get("numTrades"))
    points = 22.0
    points += max(0.0, 22.0 - min(rank, 120) / 6)
    points += min(16.0, followers / 30_000)
    points += min(18.0, pnl / 150_000)
    points += min(8.0, trades / 500)
    return round(max(0.0, min(100.0, points)), 2)


def normalize_fomo_trade(trade: dict[str, Any], profile: dict[str, Any], leader: dict[str, Any]) -> dict[str, Any] | None:
    side = str(trade.get("side") or trade.get("eventType") or "").strip().upper()
    direction = "buy" if "BUY" in side else "sell" if "SELL" in side else ""
    if not direction:
        return None
    chain = normalize_chain(first_present(trade, "chainName", "networkId", "chainId"))
    address = str(first_present(trade, "tokenAddress", "address", "mint") or "").strip()
    symbol = str(first_present(trade, "symbol") or "").strip()
    if not chain or not address or not symbol:
        return None
    event = {
        "key": trade.get("key") or trade.get("txHash") or "",
        "createdAt": trade.get("createdAt") or iso_from_ms(trade.get("ts")),
    }
    quality = fomo_quality_score(profile, leader)
    followers = to_int(profile.get("followers") or leader.get("followers"))
    row = row_base(
        chain=chain,
        address=address,
        symbol=symbol,
        name=symbol,
        kind=f"fomo_wallet_{direction}",
        event=event,
        source_family="985_fomo_wallets",
        source_origin="985monitor_fomo_profile",
    )
    amount_usd = optional_float(trade.get("usd"))
    market_cap = optional_float(trade.get("marketCap"))
    price = optional_float(trade.get("priceUsd"))
    row.update(
        {
            "marketCap": market_cap,
            "market_cap": market_cap,
            "price": price,
            "priceUsd": str(trade.get("priceUsd")) if trade.get("priceUsd") not in (None, "") else None,
            "volume": amount_usd,
            "volume24h": amount_usd,
            "smart_money": 12 if quality >= 70 else 8 if quality >= 55 else 4,
            "kol": min(8, max(0, int(followers / 75_000))),
            "rank_score": quality,
            "market_data_pending": True,
            "monitor985_fomo_handle": profile.get("handle") or leader.get("handle") or "",
            "monitor985_fomo_name": profile.get("name") or leader.get("name") or "",
            "monitor985_fomo_board": leader.get("fomo_board") or "",
            "monitor985_fomo_rank": optional_int(leader.get("rank")),
            "monitor985_fomo_pnl": optional_float(leader.get("pnl")),
            "monitor985_fomo_followers": followers,
            "monitor985_trade_side": side,
            "monitor985_trade_amount_usd": amount_usd,
            "monitor985_wallet": first_present(trade, "wallet", "walletAddress", "trader") or "",
            "monitor985_tx_hash": trade.get("txHash") or "",
            "tx_hash": trade.get("txHash") or "",
            "tx_url": trade.get("txUrl") or "",
            "direction": direction,
            "amount_usd": amount_usd,
        }
    )
    return row


def collect_fomo_wallet_rows(base_url: str, handle_limit: int, trade_limit: int, timeout_seconds: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    payload = fetch_json(public_url(base_url, DEFAULT_FOMO_LEADERBOARDS_PATH), timeout_seconds)
    leaders = leaderboard_handles(payload, max(1, handle_limit))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for leader in leaders:
        handle = str(leader.get("handle") or "").strip()
        if not handle:
            continue
        path = DEFAULT_FOMO_PROFILE_PATH.format(handle=urllib.parse.quote(handle, safe=""), limit=max(1, trade_limit))
        profile_payload = fetch_json(public_url(base_url, path), timeout_seconds)
        profile = profile_payload.get("profile") if isinstance(profile_payload, dict) and isinstance(profile_payload.get("profile"), dict) else {}
        trades = profile_payload.get("trades") if isinstance(profile_payload, dict) and isinstance(profile_payload.get("trades"), list) else []
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            row = normalize_fomo_trade(trade, profile, leader)
            if not row:
                continue
            dedupe_key = f"{row['chain']}:{row['address']}:{row.get('monitor985_tx_hash') or row.get('monitor985_event_key')}:{row.get('monitor985_fomo_handle')}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rows.append(row)
    return rows, {"leader_handles": len(leaders), "buy_trades": len(rows)}


def wallet_quality(wallet: dict[str, Any]) -> float:
    label = str(wallet.get("v2_label") or wallet.get("group") or "")
    role = str(wallet.get("role") or "")
    pnl = max(0.0, to_float(wallet.get("v2_total_pnl")))
    win_rate = max(0.0, to_float(wallet.get("v2_win_rate")))
    samples = max(0, to_int(wallet.get("v2_items_n")))
    best_pnl = max(0.0, to_float(wallet.get("best_realized_pnl")))
    points = 0.0
    if any(term in label for term in ("底部高倍数", "暴富新人", "赌徒命中", "中等盈利")):
        points += 22
    if "current-holder" in role:
        points += 12
    if "early-buyer" in role:
        points += 16
    if "peak-seller" in role:
        points += 8
    points += min(20.0, pnl / 25_000)
    points += min(14.0, best_pnl / 50_000)
    points += min(14.0, max(0.0, win_rate - 45) / 2.5)
    points += min(8.0, samples / 12)
    return max(0.0, min(100.0, points))


def normalize_smart_wallet_address(value: Any, chain: str) -> str:
    address = str(value or "").strip()
    if chain == "solana":
        return address if 32 <= len(address) <= 44 and set(address).issubset(SOLANA_ALPHABET) else ""
    if chain in {"bsc", "robinhood", "base", "ethereum"} and EVM_ADDRESS_RE.fullmatch(address):
        return address.lower() if int(address[2:], 16) else ""
    return ""


def smart_wallet_candidates_from_payload(
    payload: Any,
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tokens = payload.get("tokens") if isinstance(payload, dict) and isinstance(payload.get("tokens"), list) else []
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    linked_wallet_rows = 0
    for token in tokens:
        if not isinstance(token, dict) or not token.get("watching"):
            continue
        token_chain = normalize_chain(token.get("chain"))
        token_address = str(token.get("addr") or token.get("token_addr") or "").strip()
        token_symbol = str(token.get("symbol") or token.get("token_symbol") or "").strip()
        token_seen_at = iso_from_ms(token.get("last_seen_ts"))
        for group in ("early_buyers", "current_holders", "pnl_leaders", "peak_sellers"):
            wallets = token.get(group)
            if not isinstance(wallets, list):
                continue
            for wallet in wallets:
                if not isinstance(wallet, dict):
                    continue
                linked_wallet_rows += 1
                chain = normalize_chain(wallet.get("chain") or token_chain)
                address = normalize_smart_wallet_address(
                    wallet.get("main_wallet") or wallet.get("addr"),
                    chain,
                )
                if not address:
                    continue
                key = (chain, address)
                candidate = candidates.setdefault(
                    key,
                    {
                        "chain": chain,
                        "address": address,
                        "source": "985_smartmoney",
                        "source_origin": "public_985monitor_smartmoney",
                        "verification_status": "candidate",
                        "provider_reported_label": "",
                        "provider_reported_pnl": 0.0,
                        "provider_reported_win_rate": 0.0,
                        "provider_reported_samples": 0,
                        "best_realized_pnl": 0.0,
                        "quality_score": 0.0,
                        "_profile_rank": (-1.0, -1, -1.0),
                        "roles": set(),
                        "tokens": {},
                        "last_seen_at": None,
                    },
                )
                profile_score = round(wallet_quality(wallet), 2)
                profile_pnl = to_float(wallet.get("v2_total_pnl"))
                profile_samples = to_int(wallet.get("v2_items_n"))
                profile_rank = (profile_score, profile_samples, profile_pnl)
                if profile_rank > candidate["_profile_rank"]:
                    candidate["_profile_rank"] = profile_rank
                    candidate["provider_reported_label"] = str(wallet.get("v2_label") or "")
                    candidate["provider_reported_pnl"] = profile_pnl
                    candidate["provider_reported_win_rate"] = to_float(wallet.get("v2_win_rate"))
                    candidate["provider_reported_samples"] = profile_samples
                    candidate["best_realized_pnl"] = to_float(wallet.get("best_realized_pnl"))
                    candidate["quality_score"] = profile_score
                roles = [item.strip() for item in str(wallet.get("role") or "").split(",") if item.strip()]
                candidate["roles"].update(roles)
                if token_address:
                    candidate["tokens"][f"{token_chain}:{token_address}"] = {
                        "chain": token_chain,
                        "address": token_address,
                        "symbol": token_symbol,
                        "last_seen_at": token_seen_at or None,
                    }
                if token_seen_at and (candidate["last_seen_at"] is None or token_seen_at > candidate["last_seen_at"]):
                    candidate["last_seen_at"] = token_seen_at

    qualified = []
    for candidate in candidates.values():
        if not (
            candidate["provider_reported_pnl"] > SMART_WALLET_POLICY["provider_reported_pnl_gt"]
            and candidate["provider_reported_win_rate"] >= SMART_WALLET_POLICY["provider_reported_win_rate_min"]
            and candidate["provider_reported_samples"] >= SMART_WALLET_POLICY["provider_reported_samples_min"]
        ):
            continue
        candidate["roles"] = sorted(candidate["roles"])
        candidate.pop("_profile_rank", None)
        candidate["tokens"] = sorted(
            candidate["tokens"].values(),
            key=lambda item: str(item.get("last_seen_at") or ""),
            reverse=True,
        )[:20]
        candidate["token_count"] = len(candidate["tokens"])
        candidate["screen_policy"] = dict(SMART_WALLET_POLICY)
        qualified.append(candidate)
    qualified.sort(
        key=lambda row: (
            -to_float(row.get("quality_score")),
            -to_float(row.get("provider_reported_pnl")),
            str(row.get("address") or ""),
        )
    )
    published = qualified[: max(1, limit)]
    return published, {
        "linked_wallet_rows": linked_wallet_rows,
        "unique_wallets": len(candidates),
        "qualified_candidates": len(qualified),
        "published_candidates": len(published),
    }


def cross_chain_observation_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for candidate in candidates:
        if candidate.get("chain") != "bsc":
            continue
        address = normalize_smart_wallet_address(candidate.get("address"), "bsc")
        if not address:
            continue
        rows.append(
            {
                "chain": "robinhood",
                "address": address,
                "source_chain": "bsc",
                "source": "985_smart_wallet_candidates",
                "status": "observation_only",
                "verification_status": "unverified_on_target_chain",
                "counts_for_resonance": False,
                "quality_score": to_float(candidate.get("quality_score")),
            }
        )
    return rows


def normalize_smartmoney_token(token: dict[str, Any]) -> dict[str, Any] | None:
    if not token.get("watching"):
        return None
    chain = normalize_chain(token.get("chain"))
    address = str(token.get("addr") or token.get("token_addr") or "").strip()
    symbol = str(token.get("symbol") or token.get("token_symbol") or "").strip()
    if not chain or not address or not symbol:
        return None
    wallet_groups: list[dict[str, Any]] = []
    for key in ("early_buyers", "current_holders", "pnl_leaders"):
        value = token.get(key)
        if isinstance(value, list):
            wallet_groups.extend(wallet for wallet in value if isinstance(wallet, dict))
    qualities = sorted((wallet_quality(wallet) for wallet in wallet_groups), reverse=True)
    top_quality = qualities[:5]
    current_holders_value = token.get("current_holders") if isinstance(token.get("current_holders"), list) else None
    early_buyers_value = token.get("early_buyers") if isinstance(token.get("early_buyers"), list) else None
    peak_sellers_value = token.get("peak_sellers") if isinstance(token.get("peak_sellers"), list) else None
    current_holders = current_holders_value or []
    early_buyers = early_buyers_value or []
    peak_sellers = peak_sellers_value or []
    current_mc = optional_float(token.get("last_mc"))
    peak_mc = optional_float(token.get("peak_mc"))
    event = {"key": f"smartmoney:{chain}:{address}", "createdAt": iso_from_ms(token.get("last_seen_ts"))}
    quality_score = round(sum(top_quality) / len(top_quality), 2) if top_quality else 0.0
    row = row_base(
        chain=chain,
        address=address,
        symbol=symbol,
        name=str(token.get("name") or symbol),
        kind="smartmoney_token",
        event=event,
        source_family="985_smartmoney",
        source_origin="985monitor_smartmoney_data",
    )
    row.update(
        {
            "marketCap": current_mc,
            "market_cap": current_mc,
            "market_data_pending": True,
            "rank_score": min(100.0, quality_score + min(18.0, len(current_holders) * 2.5) + min(12.0, len(early_buyers) * 2.0)),
            "smart_money": min(60, int(round(sum(1 for score in qualities if score >= 35) * 6 + quality_score / 4))),
            "kol": 0,
            "monitor985_smart_peak_mc": peak_mc,
            "monitor985_smart_current_mc": current_mc,
            "monitor985_smart_drawdown_pct": round((1 - current_mc / peak_mc) * 100, 2) if peak_mc and current_mc else None,
            "monitor985_smart_early_buyers": len(early_buyers) if early_buyers_value is not None else None,
            "monitor985_smart_current_holders": len(current_holders) if current_holders_value is not None else None,
            "monitor985_smart_peak_sellers": len(peak_sellers) if peak_sellers_value is not None else None,
            "monitor985_smart_wallet_quality": quality_score,
            "monitor985_smart_snapshots_count": optional_int(token.get("snapshots_count")),
            "launchpad_platform": token.get("protocol") or "",
        }
    )
    return row


def smartmoney_rows_from_payload(payload: Any, limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    tokens = payload.get("tokens") if isinstance(payload, dict) and isinstance(payload.get("tokens"), list) else []
    rows = [row for token in tokens if isinstance(token, dict) for row in [normalize_smartmoney_token(token)] if row]
    rows.sort(
        key=lambda row: (
            -to_float(row.get("rank_score")),
            to_float(row.get("marketCap")) if to_float(row.get("marketCap")) > 0 else 10**18,
        )
    )
    meta = payload.get("meta") if isinstance(payload, dict) and isinstance(payload.get("meta"), dict) else {}
    return rows[: max(1, limit)], {
        "tokens_total": to_int(meta.get("tokens_total") or len(tokens)),
        "tokens_watching": to_int(meta.get("tokens_watching")),
        "rows": min(len(rows), max(1, limit)),
    }


def collect_smartmoney_rows(base_url: str, limit: int, timeout_seconds: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    payload = fetch_json(public_url(base_url, DEFAULT_SMARTMONEY_DATA_PATH), timeout_seconds)
    return smartmoney_rows_from_payload(payload, limit)


def parse_smart_wallet_csv(text: str, limit: int) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows: list[dict[str, Any]] = []
    for row in reader:
        token = {
            "watching": str(row.get("token_watching") or "") == "1",
            "chain": row.get("chain"),
            "addr": row.get("token_addr"),
            "symbol": row.get("token_symbol"),
            "name": row.get("token_symbol"),
            "last_mc": row.get("token_last_mc"),
            "peak_mc": row.get("token_peak_mc"),
            "early_buyers": [row],
            "current_holders": [row] if row.get("token_status") == "追踪中" else [],
            "peak_sellers": [row] if "peak-seller" in str(row.get("role") or "") else [],
        }
        normalized = normalize_smartmoney_token(token)
        if normalized:
            rows.append(normalized)
        if len(rows) >= limit:
            break
    return rows


def collect_rows(base_url: str, limit: int, timeout_seconds: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=len(DEFAULT_ENDPOINTS)) as pool:
        jobs = {
            name: pool.submit(
                fetch_json,
                endpoint_url(base_url, path, limit),
                timeout_seconds,
            )
            for name, path in DEFAULT_ENDPOINTS.items()
        }
        payloads = {name: job.result() for name, job in jobs.items()}
    for name in DEFAULT_ENDPOINTS:
        payload = payloads[name]
        events = event_list(payload)
        counts[name] = len(events)
        for event in events:
            row = normalize_event(event)
            if row:
                rows.append(row)
    return rows, counts


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_payload(
    rows: list[dict[str, Any]],
    out_path: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    source: str = "985_monitor",
    origin: str = "public_985monitor_api",
) -> dict[str, Any]:
    payload = {
        "source": source,
        "origin": origin,
        "base_url": base_url,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": rows,
    }
    write_json(out_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Export public 985monitor token events into meme-source-inbox.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--fomo-out", type=Path, default=DEFAULT_FOMO_OUT)
    parser.add_argument("--smartmoney-out", type=Path, default=DEFAULT_SMARTMONEY_OUT)
    parser.add_argument("--smart-wallets-out", type=Path, default=DEFAULT_SMART_WALLETS_OUT)
    parser.add_argument("--cross-chain-watch-out", type=Path, default=DEFAULT_CROSS_CHAIN_WATCH_OUT)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--timeout-seconds", type=int, default=12)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--fomo-handles", type=int, default=5)
    parser.add_argument("--fomo-trades", type=int, default=20)
    parser.add_argument("--smartmoney-limit", type=int, default=40)
    parser.add_argument("--smart-wallets-limit", type=int, default=500)
    parser.add_argument("--disable-fomo", action="store_true")
    parser.add_argument("--disable-smartmoney", action="store_true")
    args = parser.parse_args()
    started = time.time()
    status: dict[str, Any] = {
        "source": "985_monitor",
        "ok": False,
        "base_url": args.base_url,
        "out": str(args.out),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        rows, counts = collect_rows(args.base_url, max(1, args.limit), max(3, args.timeout_seconds))
        write_payload(rows, args.out, base_url=args.base_url)
        fomo_rows: list[dict[str, Any]] = []
        fomo_counts: dict[str, int] = {}
        if not args.disable_fomo:
            fomo_rows, fomo_counts = collect_fomo_wallet_rows(
                args.base_url,
                max(1, args.fomo_handles),
                max(1, args.fomo_trades),
                max(3, args.timeout_seconds),
            )
            write_payload(
                fomo_rows,
                args.fomo_out,
                base_url=args.base_url,
                source="985_fomo_wallets",
                origin="public_985monitor_fomo",
            )
        smartmoney_rows: list[dict[str, Any]] = []
        smartmoney_counts: dict[str, int] = {}
        smart_wallet_rows: list[dict[str, Any]] = []
        smart_wallet_counts: dict[str, int] = {}
        cross_chain_rows: list[dict[str, Any]] = []
        if not args.disable_smartmoney:
            smartmoney_payload = fetch_json(
                public_url(args.base_url, DEFAULT_SMARTMONEY_DATA_PATH),
                max(3, args.timeout_seconds),
            )
            smartmoney_rows, smartmoney_counts = smartmoney_rows_from_payload(
                smartmoney_payload,
                max(1, args.smartmoney_limit),
            )
            smart_wallet_rows, smart_wallet_counts = smart_wallet_candidates_from_payload(
                smartmoney_payload,
                limit=max(1, args.smart_wallets_limit),
            )
            write_payload(
                smartmoney_rows,
                args.smartmoney_out,
                base_url=args.base_url,
                source="985_smartmoney",
                origin="public_985monitor_smartmoney",
            )
            write_payload(
                smart_wallet_rows,
                args.smart_wallets_out,
                base_url=args.base_url,
                source="985_smart_wallet_candidates",
                origin="public_985monitor_smartmoney",
            )
            cross_chain_rows = cross_chain_observation_rows(smart_wallet_rows)
            write_payload(
                cross_chain_rows,
                args.cross_chain_watch_out,
                base_url=args.base_url,
                source="evm_cross_chain_watch",
                origin="985_bsc_wallet_candidates",
            )
        status.update(
            {
                "ok": True,
                "row_count": len(rows),
                "event_counts": counts,
                "fomo_wallet_count": len(fomo_rows),
                "fomo_counts": fomo_counts,
                "smartmoney_count": len(smartmoney_rows),
                "smartmoney_counts": smartmoney_counts,
                "smart_wallet_candidate_count": len(smart_wallet_rows),
                "smart_wallet_candidate_counts": smart_wallet_counts,
                "cross_chain_observation_count": len(cross_chain_rows),
                "elapsed_seconds": round(time.time() - started, 3),
            }
        )
    except Exception as exc:  # noqa: BLE001
        status.update({"error": str(exc), "elapsed_seconds": round(time.time() - started, 3)})
    write_json(args.status, status)
    print(json.dumps(status, ensure_ascii=False))
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
