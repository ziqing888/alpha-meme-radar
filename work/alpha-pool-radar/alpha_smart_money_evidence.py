"""Read-only, attributable evidence from local inbox records and wallet watchlists."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from alpha_wallet_quality import qualified_wallets

FRESH_SECONDS = 900
QUOTE_FRESH_SECONDS = 30
MIN_CONFIRMATION_LIQUIDITY_USD = 10_000.0
MIN_SELL_DOMINANCE_USD = 250.0
SELL_DOMINANCE_LIQUIDITY_FRACTION = 0.01
SELL_BUY_DOMINANCE_RATIO = 2.0
WALLET_FIELDS = (
    "monitor985_wallet", "wallet", "walletAddress", "wallet_address",
    "smart_wallet", "smartWallet", "smartWalletAddress", "smart_wallet_address",
    "trader", "traderAddress", "maker", "makerAddress", "buyer", "buyerAddress",
)
WALLET_LIST_FIELDS = (
    "okx_signal_wallet_addresses", "triggerWalletAddress", "trigger_wallet_addresses",
    "wallets", "smart_wallets",
)
TIME_FIELDS = (
    "monitor985_created_at", "okx_signal_timestamp", "trade_timestamp",
    "block_timestamp", "observed_at", "timestamp", "time",
)
AGGREGATE_FIELDS = (
    "smart_money", "smart_money_count", "smart_wallet_count", "okx_trigger_wallet_count",
    "monitor985_smart_early_buyers", "monitor985_smart_current_holders",
    "monitor985_smart_peak_sellers", "uniq_wallet_swaps",
    "smart_wallet_online_count", "smart_wallet_total_count",
    "gmgn_smart_degen_count", "gmgn_renowned_count", "gmgn_bot_degen_count",
    "gmgn_market_rank",
)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        raw = str(value).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            number = float(raw)
            return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _first(row: dict, fields: tuple[str, ...]) -> Any:
    return next((row[k] for k in fields if row.get(k) not in (None, "")), None)


def _chain(row: dict) -> str:
    raw = str(_first(row, ("chain", "chainId", "chain_id", "network", "chainIndex")) or "").lower()
    return {"sol": "solana", "501": "solana", "56": "bsc", "bnb": "bsc",
            "bnbchain": "bsc", "1": "ethereum", "eth": "ethereum", "8453": "base"}.get(raw, raw)


def _address(value: Any, chain: str) -> str:
    raw = str(value or "").strip()
    if chain == "solana":
        return raw if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", raw) else ""
    return raw.lower() if chain and re.fullmatch(r"0x[a-fA-F0-9]{40}", raw) else ""


def _token(row: dict) -> tuple[str, str]:
    chain = _chain(row)
    address = _first(row, ("contract_address", "token_address", "tokenAddress", "contractAddress", "base_address", "baseAddress", "mint", "address"))
    return chain, _address(address, chain)


def _wallets(row: dict, chain: str) -> list[str]:
    values = [row.get(k) for k in WALLET_FIELDS]
    for field in WALLET_LIST_FIELDS:
        value = row.get(field)
        values.extend(value if isinstance(value, list) else str(value or "").replace("\n", ",").split(","))
    addresses = set()
    for value in values:
        if isinstance(value, dict):
            value = _first(value, ("address", "wallet_address", "walletAddress", "wallet"))
        address = _address(value, chain)
        if address:
            addresses.add(address)
    return sorted(addresses)


def _records(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "rows", "items", "tokens", "pairs", "result", "records"):
            if isinstance(payload.get(key), (dict, list)):
                return _records(payload[key])
    return []


def _read(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        errors.append(str(path))
        return {}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def assess_smart_money(row: dict[str, Any], now_iso: str) -> dict[str, Any]:
    """Evaluate attributed trades, not platform totals; market progression is the watch's gate.

    No known cluster overlap does not establish separate beneficial ownership.
    Recheck event times on every call so a cached enrichment cannot stay fresh forever.
    """
    now = parse_timestamp(now_iso)
    if now is None:
        raise ValueError("now_iso must be a timezone-aware timestamp")
    chain, token = _token(row)
    evidence = row.get("smart_money_evidence") or {}
    profiles = qualified_wallets({"wallets": evidence.get("qualified_wallet_profiles") or []}, now_iso)
    approved = {(_chain(p), _address(p.get("address"), _chain(p))) for p in profiles}
    events = evidence.get("events") or []
    transactions: dict[tuple, dict] = {}
    conflicts: set[tuple] = set()
    clusters: dict[str, set[str]] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("chain") != chain:
            continue
        if not token or event.get("token_address") != token:
            continue
        wallets = event.get("wallets") or []
        if not isinstance(wallets, list) or len(wallets) != 1:
            continue
        wallet = _address(wallets[0], chain)
        if not wallet:
            continue
        labels = event.get("linked_cluster_ids") or []
        labels = labels if isinstance(labels, list) else [labels]
        labels = [*labels, event.get("linked_cluster_id")]
        clusters.setdefault(wallet, set()).update(str(label) for label in labels if label)
        tx = str(event.get("tx_hash") or "")
        valid_tx = re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,88}", tx) if chain == "solana" else re.fullmatch(r"0x[a-fA-F0-9]{64}", tx)
        if not valid_tx:
            continue
        tx = tx if chain == "solana" else tx.lower()
        key = (chain, tx, wallet)
        if event.get("conflicting_reports"):
            conflicts.add(key)
        stamp = parse_timestamp(event.get("observed_at"))
        amount = _number(event.get("amount_usd"))
        side = event.get("direction")
        if not (event.get("flow_eligible") and event.get("provenance") and stamp
                and 0 <= (now - stamp).total_seconds() <= FRESH_SECONDS
                and amount is not None and amount > 0 and side in {"buy", "sell"}):
            continue
        trade = {"wallet": wallet, "tx_hash": tx, "direction": side,
                 "amount_usd": amount, "observed_at": stamp.isoformat()}
        if key in transactions and transactions[key] != trade:
            conflicts.add(key)
        transactions[key] = trade
    trades = [trade for key, trade in transactions.items() if key not in conflicts]
    qualified_trades = [trade for trade in trades if (chain, trade["wallet"]) in approved]
    buys = [trade for trade in qualified_trades if trade["direction"] == "buy"]
    buy_wallets = sorted({trade["wallet"] for trade in buys})
    buy_tx_count = len({trade["tx_hash"] for trade in buys})
    # Connected groups also catch overlap through a third wallet's cluster labels.
    groups: list[tuple[set[str], set[str]]] = []
    for wallet, labels in clusters.items():
        members = {wallet}
        remaining = []
        for group_members, group_labels in groups:
            if labels & group_labels:
                members |= group_members
                labels = labels | group_labels
            else:
                remaining.append((group_members, group_labels))
        groups = [*remaining, (members, labels)]
    independent_groups = [sorted(members & set(buy_wallets)) for members, _ in groups if members & set(buy_wallets)]
    buy = sum(trade["amount_usd"] for trade in buys)
    sell = sum(trade["amount_usd"] for trade in qualified_trades if trade["direction"] == "sell")
    attributed_buy = sum(trade["amount_usd"] for trade in trades if trade["direction"] == "buy")
    attributed_sell = sum(trade["amount_usd"] for trade in trades if trade["direction"] == "sell")
    liquidity = _number(_first(row, ("liquidity", "liquidity_usd"))) or 0.0
    quote_time = parse_timestamp(row.get("quote_observed_at"))
    market_fresh = bool(row.get("quote_status") == "fresh" and row.get("quote_fingerprint")
                        and quote_time and 0 <= (now - quote_time).total_seconds() <= QUOTE_FRESH_SECONDS)
    market_liquid = liquidity >= MIN_CONFIRMATION_LIQUIDITY_USD
    sell_threshold = max(MIN_SELL_DOMINANCE_USD, liquidity * SELL_DOMINANCE_LIQUIDITY_FRACTION)
    sell_dominance = bool(market_fresh and liquidity > 0 and attributed_sell >= attributed_buy * SELL_BUY_DOMINANCE_RATIO
                          and attributed_sell - attributed_buy >= sell_threshold)
    wallet_support = len(independent_groups) >= 2 and buy_tx_count >= 2 and buy > sell
    support = bool(wallet_support and market_fresh and market_liquid and not sell_dominance)
    if sell_dominance:
        status = "blocked_sell_dominance"
        reason = (f"近15分钟有明确钱包归属的卖出明显占优：卖出 {attributed_sell:,.2f} 美元，买入 {attributed_buy:,.2f} 美元，"
                  f"净流出 {attributed_sell - attributed_buy:,.2f} 美元，已达到 {sell_threshold:,.2f} 美元的拦截门槛。未核验盈利的钱包卖压同样计入风险检查。")
    elif not wallet_support:
        status = "insufficient_wallet_evidence"
        reason = (f"盈利钱包买盘证据还不够：近15分钟有 {len(buy_wallets)} 个已核验盈利的买入地址，合并已知关联后为 {len(independent_groups)} 组，"
                  f"涉及 {buy_tx_count} 笔买入交易，净买入 {buy - sell:,.2f} 美元。"
                  "需要至少2个历史盈利核验通过且未发现关联的买入地址、2笔不同的买入交易，且买入金额高于卖出。KOL标签和平台公布的人数不能替代盈利核验。")
    elif not market_fresh or not market_liquid:
        status = "waiting_liquid_fresh_market"
        reason = f"已找到钱包买入记录，但还需要最新行情，且池子流动性至少为 {MIN_CONFIRMATION_LIQUIDITY_USD:,.0f} 美元。"
    else:
        status = "wallet_evidence_supported"
        reason = (f"近15分钟有 {len(buy_wallets)} 个已核验盈利的地址买入，按已知关联归为 {len(independent_groups)} 组，"
                  f"涉及 {buy_tx_count} 笔买入交易，净流入 {buy - sell:,.2f} 美元。"
                  "暂未发现组间关联，但是否由不同人控制仍未核实。还需行情持续更新，并通过风险检查。")
    return {"confirmation_status": status, "reason": reason, "buy_support": support,
            "sell_dominance": sell_dominance, "buy_wallets": buy_wallets,
            "buy_wallet_count": len(buy_wallets), "buy_transaction_count": buy_tx_count,
            "non_overlapping_buy_groups": independent_groups,
            "ownership_verified": False, "ownership_independence": "unknown",
            "buy_usd": buy, "sell_usd": sell, "net_flow_usd": buy - sell,
            "attributed_buy_usd": attributed_buy, "attributed_sell_usd": attributed_sell,
            "market_fresh": market_fresh, "market_liquid": market_liquid,
            "thresholds": {"fresh_seconds": FRESH_SECONDS, "quote_fresh_seconds": QUOTE_FRESH_SECONDS, "min_buy_wallets": 2, "min_buy_transactions": 2,
                           "min_liquidity_usd": MIN_CONFIRMATION_LIQUIDITY_USD,
                           "sell_buy_ratio": SELL_BUY_DOMINANCE_RATIO,
                           "min_net_sell_usd": MIN_SELL_DOMINANCE_USD,
                           "net_sell_liquidity_fraction": SELL_DOMINANCE_LIQUIDITY_FRACTION,
                           "material_net_sell_usd": sell_threshold}}


def enrich_smart_money(rows: list[dict[str, Any]], out_dir: str | Path, now_iso: str) -> list[dict[str, Any]]:
    """Return copied rows with smart_money_evidence; never write files or fetch data.

    out_dir contains meme-source-inbox/*.json and optional gmgn-smart-money-top50.json.
    Unique wallets mean explicit syntactically valid addresses attributed by a source,
    not independently verified ownership or profitability. Flows require a transaction,
    exactly one attributed wallet, USD amount, explicit direction and event time.
    """
    now = parse_timestamp(now_iso)
    if now is None:
        raise ValueError("now_iso must be a timezone-aware timestamp")
    out_dir = Path(out_dir)
    errors: list[str] = []
    indexed: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for path in sorted((out_dir / "meme-source-inbox").glob("*.json")):
        payload = _read(path, errors)
        meta = payload if isinstance(payload, dict) else {}
        for index, row in enumerate(_records(payload)):
            key = _token(row)
            if not key[1]:
                continue
            provenance = {"source": row.get("source_family") or meta.get("source") or path.stem,
                          "file": str(path), "record_index": index,
                          "fetched_at": meta.get("fetched_at")}
            indexed.setdefault(key, []).append((row, provenance))
    watch_path = out_dir / "gmgn-smart-money-top50.json"
    watch = _read(watch_path, errors) if watch_path.exists() else {}
    watch = watch if isinstance(watch, dict) else {}
    wallet_entries = qualified_wallets(watch, now_iso)
    watch_wallets = {(_chain(row), _address(row.get("address"), _chain(row)))
                     for row in wallet_entries if isinstance(row, dict)} if isinstance(wallet_entries, list) else set()
    output = []
    for row in rows:
        chain, token = _token(row)
        records = list(indexed.get((chain, token), [])) if token else []
        records.append((row, {"source": row.get("source_family") or row.get("source") or "caller_row",
                              "file": None, "record_index": None, "fetched_at": row.get("fetched_at")}))
        events: dict[tuple, dict] = {}
        aggregates = []
        for record, provenance in records:
            wallets = _wallets(record, chain)
            stamp = parse_timestamp(_first(record, TIME_FIELDS))
            timestamp = stamp.isoformat() if stamp else None
            age = (now - stamp).total_seconds() if stamp else None
            freshness = "unknown" if age is None else "future" if age < 0 else "fresh" if age <= FRESH_SECONDS else "stale"
            side = str(_first(record, ("monitor985_trade_side", "side", "direction", "trade_side")) or "").lower()
            amount = _number(_first(record, ("monitor985_trade_amount_usd", "amountUsd", "amount_usd")))
            tx = str(_first(record, ("monitor985_tx_hash", "tx_hash", "transaction_hash", "txHash", "signature")) or "")
            valid_tx = bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,88}", tx)) if chain == "solana" else bool(re.fullmatch(r"0x[a-fA-F0-9]{64}", tx))
            tx = tx if valid_tx else ""
            if chain != "solana":
                tx = tx.lower()
            counts = {field: record[field] for field in AGGREGATE_FIELDS if _number(record.get(field)) is not None}
            market_info = record.get("market_info") or {}
            if isinstance(market_info, dict):
                for field in ("uniq_wallet_swaps", "uniq_wallet_swaps_1h"):
                    if _number(market_info.get(field)) is not None:
                        counts["market_info." + field] = market_info[field]
            if counts:
                aggregate = {"reported_counts": counts, "observed_at": timestamp,
                             "freshness": freshness, "provenance": provenance,
                             "unique_wallets_verified": False}
                if aggregate not in aggregates:
                    aggregates.append(aggregate)
            if not wallets and not tx and not counts:
                continue
            cluster = _first(record, ("linked_cluster_id", "wallet_cluster_id", "cluster_id"))
            cluster_ids = record.get("linked_cluster_ids") or []
            cluster_ids = cluster_ids if isinstance(cluster_ids, list) else [cluster_ids]
            cluster_ids = sorted({str(value) for value in [*cluster_ids, cluster] if value})
            event = {"chain": chain, "token_address": token, "tx_hash": tx or None, "wallets": wallets,
                     "linked_cluster_id": str(cluster) if cluster else None,
                     "linked_cluster_ids": cluster_ids,
                     "direction": side if side in {"buy", "sell"} else None,
                     "amount_usd": amount, "observed_at": timestamp, "freshness": freshness,
                     "reported_counts": counts,
                     "gmgn_wallet_screen_score": _number(record.get("gmgn_wallet_screen_score")),
                     "provenance": [provenance]}
            # Do not multiply a group signal's amount by the number of wallets.
            event["flow_eligible"] = bool(tx and len(wallets) == 1 and amount is not None and amount > 0
                                          and side in {"buy", "sell"} and stamp and freshness != "future")
            key = (chain, tx, tuple(wallets)) if tx else (chain, "unverified", _fingerprint({k: v for k, v in event.items() if k not in {"provenance", "freshness"}}))
            if key in events:
                existing = events[key]
                existing["provenance"].append(provenance)
                existing["linked_cluster_ids"] = sorted(set(existing["linked_cluster_ids"] + event["linked_cluster_ids"]))
                if any(existing.get(k) != event.get(k) for k in ("amount_usd", "direction", "observed_at")):
                    existing["conflicting_reports"] = True
                    existing["flow_eligible"] = False
            else:
                events[key] = event
        evidence_rows = list(events.values())
        # Keep GMGN Skills Market evidence separate from the profit-history pool:
        # platform smart-money activity is useful for discovery, but does not
        # qualify a wallet for the formal watchlist.
        skill_buy_events = []
        skill_sell_events = []
        skill_kol_buy_events = []
        skill_kol_sell_events = []
        skill_trending = []
        skill_signals = []
        skill_trenches = []
        skill_hot_searches = []
        for event in evidence_rows:
            event_sources = {
                str(item.get("source"))
                for item in (event.get("provenance") or [])
                if isinstance(item, dict) and item.get("source")
            }
            if "gmgn_skills_smartmoney" in event_sources and event.get("flow_eligible") and event.get("freshness") == "fresh":
                if event.get("direction") == "buy":
                    skill_buy_events.append(event)
                elif event.get("direction") == "sell":
                    skill_sell_events.append(event)
            if "gmgn_skills_kol" in event_sources and event.get("flow_eligible") and event.get("freshness") == "fresh":
                if event.get("direction") == "buy":
                    skill_kol_buy_events.append(event)
                elif event.get("direction") == "sell":
                    skill_kol_sell_events.append(event)
        for record, provenance in records:
            source = provenance.get("source")
            if source == "gmgn_skills_trending" and isinstance(record, dict):
                skill_trending.append(record)
            elif source == "gmgn_skills_signal" and isinstance(record, dict):
                skill_signals.append(record)
            elif source == "gmgn_skills_trenches" and isinstance(record, dict):
                skill_trenches.append(record)
            elif source == "gmgn_skills_hot_searches" and isinstance(record, dict):
                skill_hot_searches.append(record)
        skill_buy_wallets = sorted({event["wallets"][0] for event in skill_buy_events if len(event.get("wallets") or []) == 1})
        skill_sell_wallets = sorted({event["wallets"][0] for event in skill_sell_events if len(event.get("wallets") or []) == 1})
        skill_kol_buy_wallets = sorted({event["wallets"][0] for event in skill_kol_buy_events if len(event.get("wallets") or []) == 1})
        skill_kol_sell_wallets = sorted({event["wallets"][0] for event in skill_kol_sell_events if len(event.get("wallets") or []) == 1})
        skill_buy_usd = sum(float(event.get("amount_usd") or 0) for event in skill_buy_events)
        skill_sell_usd = sum(float(event.get("amount_usd") or 0) for event in skill_sell_events)
        screen_scores = [event.get("gmgn_wallet_screen_score") for event in [*skill_buy_events, *skill_sell_events]
                         if _number(event.get("gmgn_wallet_screen_score")) is not None]
        skill_degen_counts = [_number(item.get("gmgn_smart_degen_count")) for item in skill_trending]
        skill_rank_values = [_number(item.get("gmgn_market_rank")) for item in skill_trending]
        signal_types = sorted({int(value) for item in skill_signals if (value := _number(item.get("gmgn_signal_type"))) is not None})
        trenches_stages = sorted({str(item.get("gmgn_trenches_stage")) for item in skill_trenches if item.get("gmgn_trenches_stage")})
        hot_ranks = [_number(item.get("gmgn_hot_search_rank")) for item in skill_hot_searches]
        visiting_counts = [_number(item.get("gmgn_visiting_count")) for item in skill_hot_searches]
        gmgn_skill_evidence = {
            "schema_version": 1,
            "source": "gmgn_skills_market",
            "available": bool(skill_buy_events or skill_sell_events or skill_kol_buy_events or skill_kol_sell_events or skill_trending or skill_signals or skill_trenches or skill_hot_searches),
            "smartmoney_buy_count": len(skill_buy_events),
            "smartmoney_buy_wallet_count": len(skill_buy_wallets),
            "smartmoney_buy_wallets": skill_buy_wallets,
            "smartmoney_buy_usd": round(skill_buy_usd, 4),
            "smartmoney_sell_count": len(skill_sell_events),
            "smartmoney_sell_wallet_count": len(skill_sell_wallets),
            "smartmoney_sell_wallets": skill_sell_wallets,
            "smartmoney_sell_usd": round(skill_sell_usd, 4),
            "kol_buy_wallet_count": len(skill_kol_buy_wallets),
            "kol_buy_wallets": skill_kol_buy_wallets,
            "kol_sell_wallet_count": len(skill_kol_sell_wallets),
            "kol_sell_wallets": skill_kol_sell_wallets,
            "wallet_screen_count": len(screen_scores),
            "wallet_screen_mean": round(sum(screen_scores) / len(screen_scores), 2) if screen_scores else None,
            "wallet_screen_max": round(max(screen_scores), 2) if screen_scores else None,
            "cluster_buy": len(skill_buy_wallets) >= 2,
            "cluster_exit": len(skill_sell_wallets) >= 2 and skill_sell_usd > skill_buy_usd,
            "signal_types": signal_types,
            "signal_names": sorted({str(item.get("gmgn_signal_name")) for item in skill_signals if item.get("gmgn_signal_name")}),
            "positive_signal": bool(set(signal_types) & {12, 14, 15, 16, 20}),
            "exit_signal": 10 in signal_types,
            "trenches_stages": trenches_stages,
            "hot_search_rank": min((value for value in hot_ranks if value and value > 0), default=None),
            "visiting_count": max((value or 0 for value in visiting_counts), default=0),
            "smart_degen_count": max((value or 0 for value in skill_degen_counts), default=0),
            "market_rank": min((value for value in skill_rank_values if value and value > 0), default=None),
            "observed_at": max((event.get("observed_at") for event in [*skill_buy_events, *skill_sell_events] if event.get("observed_at")), default=None),
        }
        addresses = sorted({address for event in evidence_rows for address in event["wallets"]})
        matched_profiles = [p for p in wallet_entries if _chain(p) == chain and _address(p.get("address"), chain) in addresses]
        matched_addresses = sorted({_address(p.get("address"), chain) for p in matched_profiles})
        qualified_events = [e for e in evidence_rows if e["flow_eligible"] and len(e["wallets"]) == 1 and e["wallets"][0] in matched_addresses]
        qualified_stamps = [e["observed_at"] for e in qualified_events if e["observed_at"] and e["freshness"] != "future"]
        fresh_addresses = sorted({address for event in evidence_rows if event["freshness"] == "fresh" for address in event["wallets"]})
        flows = [event for event in evidence_rows if event["flow_eligible"] and event["freshness"] == "fresh"]
        buy = sum(event["amount_usd"] for event in flows if event["direction"] == "buy")
        sell = sum(event["amount_usd"] for event in flows if event["direction"] == "sell")
        candidate_buy_events = [event for event in flows if event["direction"] == "buy"]
        candidate_sell_events = [event for event in flows if event["direction"] == "sell"]
        candidate_buy_wallets = sorted({event["wallets"][0] for event in candidate_buy_events if len(event.get("wallets") or []) == 1})
        candidate_sell_wallets = sorted({event["wallets"][0] for event in candidate_sell_events if len(event.get("wallets") or []) == 1})
        candidate_sources = sorted({
            str(item.get("source"))
            for event in [*candidate_buy_events, *candidate_sell_events]
            for item in (event.get("provenance") or [])
            if isinstance(item, dict) and item.get("source")
        })
        candidate_verified_buy_wallets = sorted(set(candidate_buy_wallets) & set(matched_addresses))
        stamps = [event["observed_at"] for event in evidence_rows if event["observed_at"] and event["freshness"] != "future"]
        latest = max(stamps, default=None)
        output.append({**row, "gmgn_skill_evidence": gmgn_skill_evidence, "smart_money_evidence": {
            "schema_version": 1, "as_of": now.isoformat(), "fresh_window_seconds": FRESH_SECONDS,
            "unique_wallet_count": len(addresses), "unique_wallets": addresses,
            "qualified_wallet_count": len(matched_addresses), "qualified_wallets": matched_addresses,
            "qualified_wallet_profiles": matched_profiles,
            "qualified_latest_evidence_at": max(qualified_stamps, default=None),
            "qualified_freshness": "fresh" if any(e["freshness"] == "fresh" for e in qualified_events) else "stale" if qualified_stamps else "unknown",
            "unqualified_wallet_count": len(set(addresses) - set(matched_addresses)),
            "fresh_unique_wallet_count": len(fresh_addresses), "fresh_unique_wallets": fresh_addresses,
            "wallet_verification": "profit_history_gated",
            "linked_clusters": sorted({cluster for event in evidence_rows for cluster in event["linked_cluster_ids"]}),
            "buy_usd": buy, "sell_usd": sell, "net_flow_usd": buy - sell,
            "flow_event_count": len(flows), "events": evidence_rows,
            "candidate_buy_wallet_count": len(candidate_buy_wallets),
            "candidate_buy_wallets": candidate_buy_wallets,
            "candidate_sell_wallet_count": len(candidate_sell_wallets),
            "candidate_sell_wallets": candidate_sell_wallets,
            "candidate_buy_transaction_count": len({event["tx_hash"] for event in candidate_buy_events if event.get("tx_hash")}),
            "candidate_buy_usd": sum(event["amount_usd"] for event in candidate_buy_events),
            "candidate_sell_usd": sum(event["amount_usd"] for event in candidate_sell_events),
            "candidate_net_flow_usd": buy - sell,
            "candidate_sources": candidate_sources,
            "candidate_verified_buy_wallet_count": len(candidate_verified_buy_wallets),
            "candidate_layer": "verified" if len(candidate_verified_buy_wallets) >= 2 else "cluster" if len(candidate_buy_wallets) >= 2 else "early" if candidate_buy_wallets else "none",
            "aggregate_reports": aggregates, "aggregate_counts_are_unique_wallets": False,
            "latest_evidence_at": latest,
            "freshness": "fresh" if any(e["freshness"] == "fresh" for e in evidence_rows) else "stale" if latest else "unknown",
            "fingerprint": _fingerprint(sorted(_fingerprint({k: v for k, v in e.items() if k not in {"provenance", "freshness"}}) for e in evidence_rows)),
            "watchlist": {"matched_wallets": [a for a in addresses if (chain, a) in watch_wallets],
                          "source_file": str(watch_path), "updated_at": watch.get("updated_at"),
                          "status": "profit_history_gated", "proven_profitability": bool(matched_profiles),
                          "independent_profit_verified": False},
            "read_errors": list(errors),
        }})
        assessment = assess_smart_money(output[-1], now_iso)
        output[-1]["smart_money_evidence"].update({
            "confirmation_status": assessment["confirmation_status"], "reason": assessment["reason"],
            "confirmation": assessment,
        })
    return output
