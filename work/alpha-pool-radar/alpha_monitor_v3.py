#!/usr/bin/env python3
"""Source semantics and normalized evidence contracts for MEME Monitor V3."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from alpha_monitor_chains import monitor_chain_names, normalize_monitor_chain


SCHEMA_VERSION = 1
MONITOR_SCHEMA_VERSION = 3
RESONANCE_FRESHNESS_SECONDS = {"bsc": 15 * 60, "robinhood": 30 * 60, "arc": 15 * 60}
WALLET_FRESHNESS_SECONDS = {"bsc": 15 * 60, "robinhood": 30 * 60, "arc": 15 * 60}
OBSERVATION_FRESHNESS_SECONDS = {"bsc": 2 * 60, "robinhood": 5 * 60, "arc": 2 * 60}
MARKET_FRESHNESS_SECONDS = {"bsc": 2 * 60, "robinhood": 5 * 60, "arc": 2 * 60}
CONFIRMATION_MIN_MARKET_CAP_USD = {"bsc": 10_000.0, "robinhood": 10_000.0, "arc": 10_000.0}
CONFIRMATION_MAX_INITIAL_MARKET_CAP_USD = {
    "bsc": 500_000.0,
    "robinhood": 1_000_000.0,
    "arc": 500_000.0,
}
CONFIRMATION_MIN_LIQUIDITY_USD = {"bsc": 8_000.0, "robinhood": 8_000.0, "arc": 8_000.0}
CONFIRMATION_MIN_HOLDERS = {"bsc": 20, "robinhood": None, "arc": 20}
CONFIRMATION_MIN_MCAP_MULTIPLE = 1.3
CONFIRMATION_HOLD_SECONDS = {"bsc": 60, "robinhood": 90, "arc": 60}
ALERT_COOLDOWN_SECONDS = 5 * 60
LOCK_STALE_SECONDS = 60
SOURCE_HEALTH_STALE_SECONDS = 2 * 60
SUPPORTED_CHAINS = monitor_chain_names()
EVIDENCE_ROLES = {"discovery", "ranking", "wallet", "market", "audit", "enrichment"}
SIGNAL_LANES = {
    "new_launch",
    "trending",
    "hot_search",
    "smart_money",
    "kol_social",
    "market_flow",
    "security_audit",
}
RESONANCE_ROLES = {"discovery", "ranking", "wallet", "market"}
DISPLAY_ONLY_SOURCES = {"ds", "alpha_ai"}
DEXSCREENER_FEEDS = {
    "profile_latest",
    "boost_top",
    "boost_latest",
    "ads_latest",
    "community_takeover",
    "pair_detail",
    "token_detail",
    "market_detail",
}
BSC_ONCHAIN_PREFIXES = ("bsc_", "fourmeme_", "flap_", "pancake_", "pancakeswap_")
ROBINHOOD_FACTORY_FEEDS = {
    "robinhood_factory_pair_created",
    "robinhood_factory_pair-created",
}
ARC_ONCHAIN_FEEDS = {"arc_onchain", "arc_rpc", "arc_arcscan"}
SOURCE_FEED_ALIASES = {
    "985-monitor-fast": "985_monitor",
    "985-monitor": "985_monitor",
    "985-fomo-wallets": "985_fomo_wallets",
    "985-smartmoney": "985_smartmoney",
    "wind-monitor": "wind_monitor",
    "proficy-trending": "proficy_trending",
    "noxa-launchpad": "noxa_launchpad",
}
EVM_ADDRESS = re.compile(r"^0[xX][0-9a-fA-F]{40}$")


class RejectionReason(str, Enum):
    INVALID_ADDRESS = "invalid_address"
    MISSING_TIMESTAMP = "missing_timestamp"
    INVALID_TIMESTAMP = "invalid_timestamp"
    UNSUPPORTED_SOURCE = "unsupported_source"
    DISPLAY_ONLY_SOURCE = "display_only_source"
    WRONG_CHAIN = "wrong_chain"


class _DisplayOnlySource(ValueError):
    pass


@dataclass(frozen=True)
class SourceDescriptor:
    provider_family: str
    provider_feed: str
    evidence_role: str
    signal_lane: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _onchain_source_chain(source: str) -> str | None:
    if source.startswith(BSC_ONCHAIN_PREFIXES):
        return "bsc"
    if source in ROBINHOOD_FACTORY_FEEDS:
        return "robinhood"
    if source in ARC_ONCHAIN_FEEDS:
        return "arc"
    return None


def _normalize_upstream_provider(value: Any) -> str:
    text = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    aliases = {
        "goplus": "goplus",
        "goplusio": "goplus",
        "gopluslabs": "goplus",
        "goplussecurity": "goplus",
        "dexscreener": "dexscreener",
        "gmgn": "gmgn",
        "gmgnai": "gmgn",
        "okx": "okx",
        "okxdex": "okx",
        "okxdexsdk": "okx",
        "directrpc": "direct_rpc",
    }
    if compact in aliases:
        return aliases[compact]
    normalized = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return normalized or text


def _role_and_lane(source: str) -> tuple[str, str]:
    if "audit" in source or "security" in source or "risk" in source:
        return "audit", "security_audit"
    if "hot_search" in source or "hotsearch" in source:
        return "ranking", "hot_search"
    if "kol" in source or "social" in source:
        return "ranking", "kol_social"
    if "smart" in source or "wallet" in source:
        return "wallet", "smart_money"
    if "trend" in source:
        return "ranking", "trending"
    if any(word in source for word in ("trench", "launch", "new_pair", "new_token")):
        return "discovery", "new_launch"
    return "market", "market_flow"


def _explicit_semantics(raw: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    role = str(raw.get("evidence_role") or "").strip().lower() or None
    lane = str(raw.get("signal_lane") or "").strip().lower() or None
    if role is not None and role not in EVIDENCE_ROLES:
        raise ValueError(f"unsupported evidence role: {role}")
    if lane is not None and lane not in SIGNAL_LANES:
        raise ValueError(f"unsupported signal lane: {lane}")
    return role, lane


def _lane_for_role(role: str) -> str:
    return {
        "discovery": "new_launch",
        "ranking": "trending",
        "wallet": "smart_money",
        "market": "market_flow",
        "audit": "security_audit",
        "enrichment": "market_flow",
    }[role]


def source_descriptor(source: str, raw: Mapping[str, Any] | None = None) -> SourceDescriptor:
    """Return normalized provider semantics for a configured source feed."""
    feed = SOURCE_FEED_ALIASES.get(str(source or "").strip().lower(), str(source or "").strip().lower())
    if feed in DISPLAY_ONLY_SOURCES:
        raise _DisplayOnlySource(feed)
    if not feed:
        raise ValueError("source is required")

    lookup_by_ca = raw is not None and raw.get("lookup_by_ca") is True
    explicit_audit_lookup = bool(
        lookup_by_ca
        and str(raw.get("evidence_role") or "").strip().lower() == "audit"
        and str(raw.get("signal_lane") or "").strip().lower() == "security_audit"
    )
    explicit_role, explicit_lane = (None, None) if lookup_by_ca else _explicit_semantics(raw)
    if feed.startswith("gmgn_"):
        family = "gmgn"
        role, lane = _role_and_lane(feed)
    elif feed.startswith("okx_"):
        family = "okx"
        role, lane = _role_and_lane(feed)
    elif feed == "985_monitor":
        family = "985"
        kind = str((raw or {}).get("monitor985_kind") or (raw or {}).get("kind") or "").strip().lower()
        if kind == "dex_paid":
            role, lane = "ranking", "trending"
        elif kind == "pump_callout":
            role, lane = "ranking", "kol_social"
        else:
            role, lane = "wallet", "smart_money"
    elif feed in {"985_fomo_wallets", "985_smartmoney"}:
        family, role, lane = "985", "wallet", "smart_money"
    elif feed.startswith("debot_"):
        family = "debot"
        role, lane = _role_and_lane(feed)
    elif feed == "wind_monitor":
        family, role, lane = "wind", "ranking", "kol_social"
    elif feed == "proficy_trending":
        family, role, lane = "proficy", "ranking", "trending"
    elif feed == "noxa_launchpad":
        family, role, lane = "noxa", "discovery", "new_launch"
    elif _onchain_source_chain(feed) is not None:
        family, role, lane = "onchain", "discovery", "new_launch"
    elif feed in DEXSCREENER_FEEDS or feed.startswith(("dexscreener_", "dex_")):
        family, role, lane = "dexscreener", "enrichment", "market_flow"
        if raw is not None and raw.get("lookup_by_ca") is False:
            if any(word in feed for word in ("profile", "boost", "ads", "takeover")):
                role, lane = "ranking", "trending"
            else:
                role, lane = "market", "market_flow"
    else:
        raise ValueError(f"unsupported source: {feed}")

    feed_is_audit = role == "audit" and lane == "security_audit"
    role = explicit_role or role
    lane = explicit_lane or (_lane_for_role(role) if explicit_role else lane)
    if lookup_by_ca:
        role, lane = (
            ("audit", "security_audit")
            if feed_is_audit or explicit_audit_lookup
            else ("enrichment", "market_flow")
        )
    return SourceDescriptor(family, feed, role, lane)


def _normalize_chain(value: Any) -> str:
    return normalize_monitor_chain(value)


def _raw_chain(raw: Mapping[str, Any]) -> Any:
    for key in ("chain", "chain_name", "chainName", "network", "chain_id", "chainId"):
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _timestamp(raw: Mapping[str, Any]) -> tuple[Any, str] | tuple[None, None]:
    for key in (
        "event_at",
        "timestamp",
        "created_at",
        "createdAt",
        "pair_created_at",
        "pairCreatedAt",
        "block_timestamp",
        "blockTime",
        "time",
        "observed_at",
    ):
        if key in raw and raw[key] not in (None, ""):
            return raw[key], key
    return None, None


def _normalize_timestamp(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) >= 1_000_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        raise ValueError("empty timestamp")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return _normalize_timestamp(float(text))
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _rejection(
    reason: RejectionReason,
    *,
    source: str,
    chain: str,
    contract_address: str,
    observed_at: str,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "reason": reason.value,
        "source": str(source or ""),
        "chain": str(chain or ""),
        "contract_address": str(contract_address or ""),
        "observed_at": observed_at,
        "raw_fingerprint": _sha256(raw),
    }


def _first_value(raw: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def _normalize_provider_event_id(value: Any) -> str:
    text = str(value).strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        return text.lower()
    return text


def _fallback_provider_event_id(
    raw: Mapping[str, Any],
    *,
    descriptor: SourceDescriptor,
    event_type: str,
    event_at: str,
    event_time_basis: str,
) -> str:
    stable_raw_fields = {}
    for key in (
        "wallet_address",
        "wallet",
        "maker_address",
        "maker",
        "trader_address",
        "creator",
        "pool_address",
        "tx_index",
        "log_index",
        "event_index",
        "direction",
        "side",
    ):
        if key in raw and raw[key] not in (None, ""):
            stable_raw_fields[key] = raw[key]
    material = {
        "provider_feed": descriptor.provider_feed,
        "event_type": event_type,
        "event_at": None if event_time_basis.endswith("observed_at") else event_at,
        "stable_fields": stable_raw_fields,
    }
    return f"fallback:{_sha256(material)}"


def normalize_provider_event(
    raw: Mapping[str, Any],
    *,
    source: str,
    chain: str,
    contract_address: str,
    observed_at: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate one provider row and return a versioned, provenance-rich event."""
    normalized_chain = _normalize_chain(chain)
    if normalized_chain not in SUPPORTED_CHAINS:
        return None, _rejection(
            RejectionReason.WRONG_CHAIN,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )

    row_chain = _raw_chain(raw)
    if row_chain is not None and _normalize_chain(row_chain) != normalized_chain:
        return None, _rejection(
            RejectionReason.WRONG_CHAIN,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )

    address = str(contract_address or "").strip()
    if not EVM_ADDRESS.fullmatch(address):
        return None, _rejection(
            RejectionReason.INVALID_ADDRESS,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )

    try:
        descriptor = source_descriptor(source, raw)
    except _DisplayOnlySource:
        return None, _rejection(
            RejectionReason.DISPLAY_ONLY_SOURCE,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )
    except (TypeError, ValueError):
        return None, _rejection(
            RejectionReason.UNSUPPORTED_SOURCE,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )

    source_chain = _onchain_source_chain(descriptor.provider_feed)
    if source_chain is not None and source_chain != normalized_chain:
        return None, _rejection(
            RejectionReason.WRONG_CHAIN,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )

    event_time, time_key = _timestamp(raw)
    if event_time is None:
        return None, _rejection(
            RejectionReason.MISSING_TIMESTAMP,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )
    try:
        event_at = _normalize_timestamp(event_time)
        normalized_observed_at = _normalize_timestamp(observed_at)
    except (OverflowError, TypeError, ValueError):
        return None, _rejection(
            RejectionReason.INVALID_TIMESTAMP,
            source=source,
            chain=chain,
            contract_address=contract_address,
            observed_at=observed_at,
            raw=raw,
        )

    raw_fingerprint = _sha256(raw)
    event_type = str(raw.get("event_type") or raw.get("type") or descriptor.signal_lane)
    event_time_basis = str(raw.get("event_time_basis") or time_key)
    upstream_event_id = _first_value(
        raw,
        (
            "provider_event_id",
            "event_id",
            "id",
            "tx_hash",
            "transaction_hash",
            "txHash",
            "signature",
            "pair_address",
            "pairAddress",
            "telegram_post_id",
        ),
    )
    provider_event_id = (
        _normalize_provider_event_id(upstream_event_id)
        if upstream_event_id is not None
        else _fallback_provider_event_id(
            raw,
            descriptor=descriptor,
            event_type=event_type,
            event_at=event_at,
            event_time_basis=event_time_basis,
        )
    )
    source_url = _first_value(raw, ("source_url", "url", "event_url"))
    upstream = _normalize_upstream_provider(raw.get("upstream_provider") or descriptor.provider_family)
    lookup_by_ca = raw.get("lookup_by_ca") is True
    counts_for_resonance = descriptor.evidence_role in RESONANCE_ROLES and not lookup_by_ca
    event = {
        "schema_version": SCHEMA_VERSION,
        "chain": normalized_chain,
        "contract_address": address.lower(),
        "event_type": event_type,
        "event_at": event_at,
        "observed_at": normalized_observed_at,
        "provider_family": descriptor.provider_family,
        "provider_feed": descriptor.provider_feed,
        "evidence_role": descriptor.evidence_role,
        "signal_lane": descriptor.signal_lane,
        "provider_event_id": provider_event_id,
        "source_url": str(source_url) if source_url not in (None, "") else None,
        "raw_fingerprint": raw_fingerprint,
        "upstream_provider": upstream,
        "event_time_basis": event_time_basis,
        "counts_for_resonance": counts_for_resonance,
    }
    created_at = _first_value(
        raw,
        (
            "real_created_at",
            "contract_created_at",
            "creation_timestamp",
            "open_timestamp",
            "pool_created_at",
        ),
    )
    if created_at not in (None, ""):
        try:
            event["real_created_at"] = _normalize_timestamp(created_at)
        except (OverflowError, TypeError, ValueError):
            pass
    if descriptor.evidence_role == "wallet":
        wallet_address = _first_value(
            raw,
            (
                "wallet_address",
                "wallet",
                "walletAddress",
                "monitor985_wallet",
                "maker_address",
                "makerAddress",
                "maker",
                "trader_address",
                "traderAddress",
                "trader",
                "buyer_address",
                "buyerAddress",
                "buyer",
            ),
        )
        direction = _first_value(raw, ("direction", "side"))
        verified = _first_value(raw, ("wallet_verified", "address_verified", "verified"))
        amount_usd = _first_value(raw, ("amount_usd", "value_usd", "buy_amount_usd"))
        if wallet_address not in (None, ""):
            event["wallet_address"] = str(wallet_address).strip().lower()
        if direction not in (None, ""):
            event["direction"] = str(direction).strip().lower()
        if verified is not None:
            event["wallet_verified"] = verified is True
        if amount_usd not in (None, ""):
            event["amount_usd"] = amount_usd
        for canonical, aliases in (
            ("provider_sequence", ("provider_sequence", "sequence", "seq")),
            ("block_number", ("block_number", "blockNumber")),
            ("transaction_index", ("transaction_index", "transactionIndex", "tx_index")),
            ("log_index", ("log_index", "logIndex")),
            ("event_index", ("event_index",)),
        ):
            order_value = _first_value(raw, aliases)
            if order_value not in (None, ""):
                event[canonical] = order_value
    identity_fields = ("chain", "contract_address", "provider_family", "provider_event_id")
    event["event_id"] = _sha256([event[field] for field in identity_fields])
    return event, None


AUDIT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("honeypot", ("honeypot", "is_honeypot")),
    ("open_source", ("open_source", "is_open_source", "source_verified")),
    ("ownership", ("ownership", "owner_renounced", "ownership_renounced", "owner_address")),
    (
        "lp_burn_lock",
        ("lp_burn_lock", "burn_status", "lp_burned", "lp_locked", "is_lp_burned", "is_lp_locked"),
    ),
    ("buy_tax", ("buy_tax", "buyTax")),
    ("sell_tax", ("sell_tax", "sellTax")),
    ("top10", ("top10", "top10_holder_pct", "top10_holder_rate", "top_10_holder_rate")),
    (
        "developer",
        ("developer", "developer_holdings", "dev_holder_pct", "dev_team_hold_rate", "creator_balance_rate"),
    ),
    ("insider", ("insider", "insider_holdings", "insider_pct", "suspected_insider_hold_rate")),
    (
        "sniper",
        ("sniper", "sniper_holdings", "sniper_pct", "top70_sniper_hold_rate", "rat_trader_amount_rate"),
    ),
    (
        "bundler",
        ("bundler", "bundler_holdings", "bundler_pct", "bundler_trader_amount_rate"),
    ),
    ("fresh_wallet", ("fresh_wallet", "fresh_wallet_share", "fresh_wallet_pct", "fresh_wallet_rate")),
    ("mint", ("mint", "is_mintable", "mintable", "mint_authority")),
    ("freeze", ("freeze", "freeze_authority", "freezable", "is_freezable")),
    ("blacklist", ("blacklist", "is_blacklisted", "blacklistable")),
    ("pause", ("pause", "transfer_pausable", "trading_paused", "pausable")),
    ("proxy", ("proxy", "is_proxy", "proxy_contract", "upgradeable")),
    ("wash", ("wash", "is_wash_trading", "wash_score", "wash_trading")),
    (
        "deployer_history",
        ("deployer_history", "developer_history", "developer_rug_history", "deployer_rug_history"),
    ),
)
AUDIT_STATUSES = {"known", "unknown", "stale", "conflicting"}
AUDIT_CONFIDENCE = {"provider_reported", "cross_checked", "locally_verified"}


def _present_alias(raw: Mapping[str, Any], aliases: tuple[str, ...]) -> tuple[str, Any] | None:
    for alias in aliases:
        if alias in raw:
            return alias, raw[alias]
    return None


def normalize_audit_facts(
    raw: Mapping[str, Any],
    *,
    provider_family: str,
    observed_at: str,
    identity_key: str,
) -> list[dict[str, Any]]:
    """Normalize provider audit fields without turning unknown values into passes."""
    family = str(provider_family or "").strip().lower()
    normalized_observed_at = _normalize_timestamp(observed_at)
    upstream = _normalize_upstream_provider(raw.get("upstream_provider") or family) or family
    facts: list[dict[str, Any]] = []

    for audit_field, aliases in AUDIT_ALIASES:
        found = _present_alias(raw, aliases)
        if found is None:
            continue
        source_field, value = found
        supplied_status = str(
            raw.get(f"{source_field}_status") or raw.get(f"{audit_field}_status") or ""
        ).strip().lower()
        status = supplied_status if supplied_status in AUDIT_STATUSES else ("unknown" if value is None else "known")
        supplied_confidence = str(
            raw.get(f"{source_field}_confidence")
            or raw.get(f"{audit_field}_confidence")
            or raw.get("audit_confidence")
            or raw.get("confidence")
            or "provider_reported"
        ).strip().lower()
        confidence = (
            supplied_confidence if supplied_confidence in AUDIT_CONFIDENCE else "provider_reported"
        )
        evidence_material = {
            "identity_key": identity_key,
            "audit_field": audit_field,
            "value": value,
            "status": status,
            "provider_family": family,
            "upstream_provider": upstream,
            "observed_at": normalized_observed_at,
        }
        facts.append(
            {
                "audit_field": audit_field,
                "value": value,
                "status": status,
                "provider_family": family,
                "upstream_provider": upstream,
                "observed_at": normalized_observed_at,
                "confidence": confidence,
                "evidence_id": _sha256(evidence_material),
            }
        )
    return facts


MARKET_FIELDS: tuple[str, ...] = (
    "price_usd",
    "market_cap_usd",
    "liquidity_usd",
    "volume_1m_usd",
    "volume_5m_usd",
    "volume_24h_usd",
    "buys_1m",
    "sells_1m",
    "buys_5m",
    "sells_5m",
    "unique_buyers_1m",
    "unique_buyers_5m",
    "net_buy_flow_1m_usd",
    "net_buy_flow_5m_usd",
    "trades_1m",
    "trades_5m",
    "holders",
    "top10_holder_pct",
    "dev_holder_pct",
    "bundler_pct",
    "sniper_pct",
)
MARKET_ALIASES: dict[str, tuple[str, ...]] = {
    "price_usd": ("price_usd", "price"),
    "market_cap_usd": ("market_cap_usd", "market_cap", "mcap"),
    "liquidity_usd": ("liquidity_usd", "liquidity"),
    "volume_1m_usd": ("volume_1m_usd", "volume1m", "volume_m1"),
    "volume_5m_usd": ("volume_5m_usd", "volume5m", "volume_m5"),
    "volume_24h_usd": ("volume_24h_usd", "volume24h", "volume_24h", "volume"),
    "buys_1m": ("buys_1m", "buys1m"),
    "sells_1m": ("sells_1m", "sells1m"),
    "buys_5m": ("buys_5m", "buys5m"),
    "sells_5m": ("sells_5m", "sells5m"),
    "unique_buyers_1m": ("unique_buyers_1m", "uniqueBuyers1m"),
    "unique_buyers_5m": ("unique_buyers_5m", "uniqueBuyers5m"),
    "net_buy_flow_1m_usd": ("net_buy_flow_1m_usd", "net_flow_1m_usd", "net_buy_flow_1m"),
    "net_buy_flow_5m_usd": ("net_buy_flow_5m_usd", "net_flow_5m_usd", "net_buy_flow_5m"),
    "trades_1m": ("trades_1m", "txns_1m", "transactions_1m"),
    "trades_5m": ("trades_5m", "txns_5m", "transactions_5m"),
    "holders": ("holders", "holder_count"),
    "top10_holder_pct": ("top10_holder_pct", "top10_holder_rate", "top_10_holder_rate"),
    "dev_holder_pct": ("dev_holder_pct", "dev_team_hold_rate"),
    "bundler_pct": ("bundler_pct", "bundler_trader_amount_rate"),
    "sniper_pct": ("sniper_pct", "top70_sniper_hold_rate"),
}
STATE_ORDER = (
    "new",
    "building",
    "resonating",
    "smart_cluster",
    "cooling",
    "trend_watch",
    "revival",
    "blocked_risk",
    "stale",
)
HARD_AUDIT_FAILURES = {
    "honeypot": True,
    "blacklist": True,
    "pause": True,
}
FLOW_METRICS = {
    "unique_buyers": ("unique_buyers_1m", "unique_buyers_5m"),
    "net_buy_flow": ("net_buy_flow_1m_usd", "net_buy_flow_5m_usd"),
    "trade_frequency": ("trades_1m", "trades_5m"),
    "volume": ("volume_1m_usd", "volume_5m_usd", "volume_24h_usd"),
}


def _as_utc(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(_normalize_timestamp(value))
    except (OverflowError, TypeError, ValueError):
        return None


def _age_seconds(observed_at: str, earlier: Any) -> float | None:
    current = _as_utc(observed_at)
    previous = _as_utc(earlier)
    if current is None or previous is None:
        return None
    age = (current - previous).total_seconds()
    return age if age >= 0 else None


def _within_window(observed_at: str, value: Any, window_seconds: int) -> bool:
    age = _age_seconds(observed_at, value)
    return age is not None and age <= window_seconds


def _event_is_current(event: Mapping[str, Any], observed_at: str, window_seconds: int) -> bool:
    return _within_window(observed_at, event.get("event_at"), window_seconds) and _within_window(
        observed_at, event.get("observed_at"), window_seconds
    )


def _known_number(value: Any) -> int | float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number.is_integer() else number


def _mapping_value(row: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def build_monitor_baselines(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build the small discovery baseline projection used by the live UI."""
    baselines: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        chain = _normalize_chain(row.get("chain") or row.get("chain_id") or row.get("chainId"))
        address = str(
            row.get("contract_address")
            or row.get("token_address")
            or row.get("tokenAddress")
            or ""
        ).strip().lower()
        if chain not in SUPPORTED_CHAINS or not EVM_ADDRESS.fullmatch(address):
            continue
        replay = row.get("replay") if isinstance(row.get("replay"), Mapping) else {}
        first_snapshot = replay.get("first_snapshot") if isinstance(replay.get("first_snapshot"), Mapping) else {}

        def positive(*values: Any) -> int | float | None:
            for value in values:
                number = _known_number(value)
                if number is not None and number > 0:
                    return number
            return None

        incoming = {
            "first_seen_at": row.get("watch_first_seen_at") or replay.get("first_seen_at") or None,
            "first_market_cap_usd": positive(
                row.get("watch_first_seen_mcap"),
                replay.get("first_mcap_usd"),
                first_snapshot.get("mcap"),
                first_snapshot.get("market_cap"),
            ),
            "first_price_usd": positive(
                replay.get("first_price_usd"), first_snapshot.get("price_usd")
            ),
            "peak_market_cap_usd": positive(
                row.get("watch_max_seen_mcap"),
                replay.get("max_mcap_usd"),
                row.get("watch_first_seen_mcap"),
            ),
        }
        key = f"{chain}:{address}"
        existing = baselines.get(key)
        if existing is None:
            baselines[key] = incoming
            continue
        existing_time = str(existing.get("first_seen_at") or "")
        incoming_time = str(incoming.get("first_seen_at") or "")
        earlier = incoming if incoming_time and (not existing_time or incoming_time < existing_time) else existing
        later = existing if earlier is incoming else incoming
        merged = {**later, **{field: value for field, value in earlier.items() if value is not None}}
        peaks = [
            value
            for value in (
                positive(existing.get("peak_market_cap_usd")),
                positive(incoming.get("peak_market_cap_usd")),
            )
            if value is not None
        ]
        merged["peak_market_cap_usd"] = max(peaks) if peaks else None
        baselines[key] = merged
    return baselines


def build_monitor_snapshot_baselines(
    snapshot: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Project immutable discovery values from the canonical monitor history."""
    baselines: dict[str, dict[str, Any]] = {}
    for token in snapshot.get("tokens") or []:
        if not isinstance(token, Mapping):
            continue
        identity = token.get("identity") if isinstance(token.get("identity"), Mapping) else {}
        chain = _normalize_chain(identity.get("chain"))
        address = str(identity.get("contract_address") or "").strip().lower()
        if chain not in SUPPORTED_CHAINS or not EVM_ADDRESS.fullmatch(address):
            continue
        market = token.get("market") if isinstance(token.get("market"), Mapping) else {}
        snapshots = sorted(
            (item for item in market.get("snapshots") or [] if isinstance(item, Mapping)),
            key=lambda item: str(item.get("observed_at") or ""),
        )
        market_caps = [
            value
            for item in snapshots
            if (value := _known_number(item.get("market_cap_usd"))) is not None and value > 0
        ]
        prices = [
            value
            for item in snapshots
            if (value := _known_number(item.get("price_usd"))) is not None and value > 0
        ]
        baselines[f"{chain}:{address}"] = {
            "first_seen_at": identity.get("first_seen_at"),
            "first_market_cap_usd": market_caps[0] if market_caps else None,
            "first_price_usd": prices[0] if prices else None,
            "peak_market_cap_usd": max(market_caps) if market_caps else None,
        }
    return baselines


def _market_number(field: str, value: Any) -> int | float | None:
    number = _known_number(value)
    if number is None:
        return None
    # A tradable token cannot have a zero price, market cap, or holder count.
    # Upstream launch feeds use zero as a placeholder while enrichment is pending.
    if field in {"price_usd", "market_cap_usd", "holders"} and number <= 0:
        return None
    return number


def _token_key(row: Mapping[str, Any]) -> str | None:
    chain = _normalize_chain(row.get("chain") or row.get("chain_id") or row.get("chainId"))
    address = str(
        row.get("contract_address")
        or row.get("token_address")
        or row.get("tokenAddress")
        or ""
    ).strip().lower()
    if chain not in SUPPORTED_CHAINS or not EVM_ADDRESS.fullmatch(address):
        return None
    return f"{chain}:{address}"


def _empty_market() -> dict[str, Any]:
    return {
        "status": "unknown",
        "observed_at": None,
        **{field: None for field in MARKET_FIELDS},
        "field_status": {field: "unknown" for field in MARKET_FIELDS},
        "field_sources": {field: None for field in MARKET_FIELDS},
        "field_observed_at": {field: None for field in MARKET_FIELDS},
        "snapshots": [],
    }


def _new_token(event: Mapping[str, Any]) -> dict[str, Any]:
    identity_key = _token_key(event)
    assert identity_key is not None
    event_at = _normalize_timestamp(event["event_at"])
    first_seen_at = _normalize_timestamp(event.get("observed_at") or event_at)
    is_creation_event = (
        event.get("provider_family") == "onchain"
        and event.get("evidence_role") == "discovery"
    )
    real_created_at = event.get("real_created_at") or (event_at if is_creation_event else None)
    return {
        "id": identity_key,
        "identity": {
            "chain": identity_key.split(":", 1)[0],
            "contract_address": identity_key.split(":", 1)[1],
            "symbol": event.get("symbol") or None,
            "name": event.get("name") or None,
            "creator": event.get("creator") or None,
            "launchpad": event.get("launchpad") or None,
            "real_created_at": _normalize_timestamp(real_created_at) if real_created_at else None,
            "real_created_source": event.get("provider_feed") if real_created_at else None,
            "first_seen_at": first_seen_at,
            "first_seen_source": event.get("provider_feed") or None,
            "pair_created_at": None,
            "migration_at": None,
        },
        "primary_state": "new",
        "active_states": ["new"],
        "state_version": 1,
        "state_updated_at": first_seen_at,
        "market": _empty_market(),
        "ranking_axes": {},
        "resonance": {},
        "wallet_evidence": {},
        "audit_facts": [],
        "risk": {
            "hard_blocked": False,
            "hard_failures": [],
            "soft_flags": [],
            "evidence": [],
        },
        "events": [],
        "missing_evidence": [],
        "ai": {"status": "unavailable", "analyzed_at": None, "evidence_ids": []},
        "freshness": {
            "status": "fresh",
            "last_observed_at": first_seen_at,
            "report_refreshed_at": first_seen_at,
        },
    }


def _dedupe_records(records: Sequence[Mapping[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = "|".join(str(record.get(field) or "") for field in key_fields)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(deepcopy(dict(record)))
    return deduped


def _append_audit_facts(token: dict[str, Any], facts: Sequence[Mapping[str, Any]]) -> None:
    existing = {str(fact.get("evidence_id") or "") for fact in token["audit_facts"]}
    for fact in facts:
        evidence_id = str(fact.get("evidence_id") or "")
        if evidence_id and evidence_id not in existing:
            token["audit_facts"].append(deepcopy(dict(fact)))
            existing.add(evidence_id)


def _apply_events(
    tokens: dict[str, dict[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    touched: dict[str, str] = {}
    existing_by_id = {
        str(event.get("event_id")): (token, event)
        for token in tokens.values()
        for event in token.get("events") or []
        if event.get("event_id")
    }
    all_seen = set(existing_by_id)
    semantic_fields = (
        "event_type",
        "provider_family",
        "provider_feed",
        "evidence_role",
        "signal_lane",
        "source_url",
        "upstream_provider",
        "event_time_basis",
        "counts_for_resonance",
    )
    for event in _dedupe_records(events, ("event_id",)):
        identity_key = _token_key(event)
        event_id = str(event.get("event_id") or "")
        if (
            identity_key is None
            or not event_id
            or _as_utc(event.get("event_at")) is None
            or _as_utc(event.get("observed_at")) is None
        ):
            continue
        if event_id in all_seen:
            existing_token, existing_event = existing_by_id[event_id]
            if str(existing_token.get("id") or "") != identity_key:
                continue
            changed = False
            for field in semantic_fields:
                if existing_event.get(field) != event.get(field):
                    existing_event[field] = deepcopy(event.get(field))
                    changed = True
            if changed:
                evidence_observed_at = _normalize_timestamp(event["observed_at"])
                touched[identity_key] = max(
                    touched.get(identity_key, evidence_observed_at), evidence_observed_at
                )
            continue
        token = tokens.setdefault(identity_key, _new_token(event))
        evidence_observed_at = _normalize_timestamp(event["observed_at"])
        touched[identity_key] = max(touched.get(identity_key, evidence_observed_at), evidence_observed_at)
        token["events"].append(event)
        token["events"].sort(
            key=lambda item: (_normalize_timestamp(item["event_at"]), str(item["event_id"]))
        )
        all_seen.add(event_id)
        existing_by_id[event_id] = (token, event)
        identity = token["identity"]
        for field in ("symbol", "name", "creator", "launchpad"):
            if identity.get(field) is None and event.get(field) not in (None, ""):
                identity[field] = event[field]
        explicit_created = event.get("real_created_at")
        if explicit_created and identity.get("real_created_at") is None:
            identity["real_created_at"] = _normalize_timestamp(explicit_created)
            identity["real_created_source"] = event.get("provider_feed")
        _append_audit_facts(token, event.get("audit_facts") or [])
    for token in tokens.values():
        identity = token["identity"]
        observed_events = [
            (
                _normalize_timestamp(event["observed_at"]),
                str(event.get("provider_feed") or ""),
                str(event.get("provider_family") or ""),
                str(event.get("event_id") or ""),
            )
            for event in token.get("events") or []
            if _as_utc(event.get("observed_at")) is not None
        ]
        existing_first_seen = identity.get("first_seen_at")
        if _as_utc(existing_first_seen) is not None:
            observed_events.append(
                (
                    _normalize_timestamp(existing_first_seen),
                    str(identity.get("first_seen_source") or ""),
                    "",
                    "",
                )
            )
        if observed_events:
            first_seen_at, first_seen_source, _family, _event_id = min(observed_events)
            identity["first_seen_at"] = first_seen_at
            identity["first_seen_source"] = first_seen_source or None
    return touched


def _market_snapshot(candidate: Mapping[str, Any], observed_at: str) -> dict[str, Any]:
    stamp_value = candidate.get("observed_at") or candidate.get("quote_observed_at") or observed_at
    stamp = _normalize_timestamp(stamp_value)
    values = {
        field: _market_number(field, _mapping_value(candidate, MARKET_ALIASES[field]))
        for field in MARKET_FIELDS
    }
    if values["trades_1m"] is None and values["buys_1m"] is not None and values["sells_1m"] is not None:
        values["trades_1m"] = values["buys_1m"] + values["sells_1m"]
    if values["trades_5m"] is None and values["buys_5m"] is not None and values["sells_5m"] is not None:
        values["trades_5m"] = values["buys_5m"] + values["sells_5m"]
    default_source = candidate.get("field_source") or candidate.get("quote_source") or None
    default_observed_at = candidate.get("quote_observed_at") or candidate.get("observed_at") or observed_at
    field_sources = {field: default_source for field in MARKET_FIELDS}
    field_observed_at = {field: _normalize_timestamp(default_observed_at) for field in MARKET_FIELDS}
    holder_source = candidate.get("holder_source")
    holder_observed_at = candidate.get("holder_observed_at") or candidate.get("gmgn_observed_at")
    if holder_source:
        field_sources["holders"] = holder_source
        field_sources["top10_holder_pct"] = holder_source
    if holder_observed_at:
        holder_stamp = _normalize_timestamp(holder_observed_at)
        field_observed_at["holders"] = holder_stamp
        field_observed_at["top10_holder_pct"] = holder_stamp
    return {
        "observed_at": stamp,
        "field_source": default_source,
        "field_sources": field_sources,
        "field_observed_at": field_observed_at,
        **values,
        "field_status": {
            field: "known" if values[field] is not None else "unknown" for field in MARKET_FIELDS
        },
    }


def _coalesce_market_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        (deepcopy(dict(row)) for row in rows),
        key=lambda row: _canonical_json({
            key: value
            for key, value in row.items()
            if key not in {"field_sources", "field_observed_at"}
        }),
    )
    observed_at = ordered[0]["observed_at"]
    chosen: dict[str, tuple[int | float | None, Mapping[str, Any] | None]] = {}
    for field in MARKET_FIELDS:
        chosen[field] = next(
            (
                (normalized, row)
                for row in ordered
                if (normalized := _market_number(field, row.get(field))) is not None
            ),
            (None, None),
        )
    values = {field: chosen[field][0] for field in MARKET_FIELDS}
    field_sources: dict[str, Any] = {}
    field_observed_at: dict[str, Any] = {}
    for field in MARKET_FIELDS:
        row = chosen[field][1]
        source_map = row.get("field_sources") if isinstance(row, Mapping) and isinstance(row.get("field_sources"), Mapping) else {}
        time_map = row.get("field_observed_at") if isinstance(row, Mapping) and isinstance(row.get("field_observed_at"), Mapping) else {}
        field_sources[field] = source_map.get(field) if source_map else (row.get("field_source") if isinstance(row, Mapping) else None)
        field_observed_at[field] = time_map.get(field) if time_map else (row.get("observed_at") if isinstance(row, Mapping) else None)
    sources = sorted({str(row.get("field_source")) for row in ordered if row.get("field_source")})
    return {
        "observed_at": observed_at,
        "field_source": "|".join(sources) if sources else None,
        "field_sources": field_sources,
        "field_observed_at": field_observed_at,
        **values,
        "field_status": {
            field: "known" if values[field] is not None else "unknown" for field in MARKET_FIELDS
        },
    }


def _coalesce_market_history(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if _as_utc(row.get("observed_at")) is None:
            continue
        stamp = _normalize_timestamp(row["observed_at"])
        grouped.setdefault(stamp, []).append({**row, "observed_at": stamp})
    return [_coalesce_market_rows(grouped[stamp]) for stamp in sorted(grouped)]


def _refresh_market_current(token: dict[str, Any]) -> None:
    market = token["market"]
    snapshots = _coalesce_market_history(market.get("snapshots") or [])
    market["snapshots"] = snapshots
    if not snapshots:
        return
    latest = snapshots[-1]
    market.update({field: latest[field] for field in MARKET_FIELDS})
    market["field_status"] = deepcopy(latest["field_status"])
    market["field_sources"] = deepcopy(latest.get("field_sources") or {})
    market["field_observed_at"] = deepcopy(latest.get("field_observed_at") or {})
    market["observed_at"] = latest["observed_at"]
    market["status"] = "known" if any(latest[field] is not None for field in MARKET_FIELDS) else "unknown"


def _candidate_risks(candidate: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    nested = candidate.get("risk") if isinstance(candidate.get("risk"), Mapping) else {}
    hard: list[str] = []
    soft: list[str] = []
    for value in (
        candidate.get("hard_risk_flags"),
        candidate.get("hard_failures"),
        nested.get("hard_failures"),
    ):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            hard.extend(str(item) for item in value if item not in (None, ""))
    if candidate.get("hard_risk") is True or nested.get("hard_blocked") is True:
        if not hard:
            hard.append("hard_risk")
    deterministic = {
        "can_sell": False,
        "sellable": False,
        "liquidity_removed": True,
        "official_ca_conflict": True,
    }
    for field, failing_value in deterministic.items():
        if candidate.get(field) is failing_value:
            hard.append(field)
    for value in (candidate.get("soft_risk_flags"), nested.get("soft_flags")):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            soft.extend(str(item) for item in value if item not in (None, ""))
    return sorted(set(hard)), sorted(set(soft))


def _apply_candidates(
    tokens: dict[str, dict[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    observed_at: str,
) -> dict[str, str]:
    touched: dict[str, str] = {}
    for token in tokens.values():
        _refresh_market_current(token)
    for candidate in candidates:
        identity_key = _token_key(candidate)
        if identity_key is None or identity_key not in tokens:
            continue
        token = tokens[identity_key]
        snapshot = _market_snapshot(candidate, observed_at)
        snapshots = token["market"].setdefault("snapshots", [])
        if snapshots and snapshot["observed_at"] < snapshots[-1]["observed_at"]:
            continue
        identity = token["identity"]
        for field in ("symbol", "name", "creator", "launchpad"):
            if candidate.get(field) not in (None, ""):
                identity[field] = candidate[field]
        for target, aliases in (
            ("real_created_at", ("real_created_at", "contract_created_at")),
            ("pair_created_at", ("pair_created_at", "pairCreatedAt")),
            ("migration_at", ("migration_at", "graduation_at")),
        ):
            value = _mapping_value(candidate, aliases)
            if value is not None:
                identity[target] = _normalize_timestamp(value)
                if target == "real_created_at":
                    identity["real_created_source"] = candidate.get("real_created_source") or candidate.get("quote_source")
        if identity.get("pair_created_at") is None:
            pair_age_hours = _known_number(
                _mapping_value(candidate, ("pair_age_hours", "pairAgeHours"))
            )
            snapshot_time = _as_utc(snapshot["observed_at"])
            if pair_age_hours is not None and pair_age_hours >= 0 and snapshot_time is not None:
                identity["pair_created_at"] = _normalize_timestamp(
                    snapshot_time - timedelta(hours=pair_age_hours)
                )
        touched[identity_key] = max(
            touched.get(identity_key, snapshot["observed_at"]), snapshot["observed_at"]
        )
        snapshots.append(snapshot)
        _refresh_market_current(token)
        _append_audit_facts(token, candidate.get("audit_facts") or [])
        hard, soft = _candidate_risks(candidate)
        token["risk"]["hard_failures"] = sorted(
            set(token["risk"].get("hard_failures") or []).union(hard)
        )
        token["risk"]["soft_flags"] = sorted(
            set(token["risk"].get("soft_flags") or []).union(soft)
        )
        existing_risk_evidence = {
            str(item.get("evidence_id") or "")
            for item in token["risk"].get("evidence") or []
            if isinstance(item, Mapping)
        }
        risk_source = (
            candidate.get("risk_source")
            or candidate.get("field_source")
            or candidate.get("quote_source")
            or None
        )
        for reason in hard:
            material = {
                "identity_key": identity_key,
                "reason": reason,
                "source": risk_source,
                "observed_at": snapshot["observed_at"],
            }
            evidence_id = _sha256(material)
            if evidence_id in existing_risk_evidence:
                continue
            token["risk"].setdefault("evidence", []).append(
                {**material, "evidence_id": evidence_id}
            )
            existing_risk_evidence.add(evidence_id)
    for token in tokens.values():
        _refresh_market_current(token)
    return touched


def _snapshot_metric(snapshot: Mapping[str, Any], aliases: tuple[str, ...]) -> int | float | None:
    for alias in aliases:
        value = snapshot.get(alias)
        if value is not None:
            return value
    return None


def _derive_flow(token: Mapping[str, Any]) -> dict[str, Any]:
    snapshots = token["market"].get("snapshots") or []
    deltas = {name: None for name in FLOW_METRICS}
    current = {name: None for name in FLOW_METRICS}
    baseline_deltas = {name: None for name in FLOW_METRICS}
    if snapshots:
        for name, aliases in FLOW_METRICS.items():
            current[name] = _snapshot_metric(snapshots[-1], aliases)
    if len(snapshots) >= 2:
        previous = snapshots[-2]
        latest = snapshots[-1]
        for name, aliases in FLOW_METRICS.items():
            before = _snapshot_metric(previous, aliases)
            after = _snapshot_metric(latest, aliases)
            if before is not None and after is not None:
                deltas[name] = after - before
            history = [
                value
                for item in snapshots[:-1]
                if (value := _snapshot_metric(item, aliases)) is not None
            ]
            if history and after is not None:
                baseline_deltas[name] = after - (sum(history) / len(history))
    comparable = [value for value in deltas.values() if value is not None]
    participation = [deltas[name] for name in ("unique_buyers", "net_buy_flow", "trade_frequency") if deltas[name] is not None]
    previous_positive = False
    if len(snapshots) >= 2:
        previous_positive = any(
            (value := _snapshot_metric(snapshots[-2], FLOW_METRICS[name])) is not None
            and value > 0
            for name in ("unique_buyers", "net_buy_flow", "trade_frequency")
        )
    score = sum(1 if value > 0 else -1 if value < 0 else 0 for value in comparable)
    if participation and any(value > 0 for value in participation) and score > 0:
        direction = "accelerating"
    elif participation and any(value < 0 for value in participation) and score < 0:
        direction = "decaying"
    elif comparable:
        direction = "flat"
    else:
        direction = "unknown"
    return {
        "status": "known" if comparable else "unknown",
        "direction": direction,
        "sample_count": len(snapshots),
        "previous_positive": previous_positive,
        "current": current,
        "deltas": deltas,
        "baseline_deltas": baseline_deltas,
    }


def _wallet_address(event: Mapping[str, Any]) -> str:
    return str(
        event.get("wallet_address")
        or event.get("maker_address")
        or event.get("trader_address")
        or ""
    ).strip().lower()


def _wallet_direction(event: Mapping[str, Any]) -> str:
    direction = str(event.get("direction") or event.get("side") or "").strip().lower()
    if direction in {"buy", "bought", "in"}:
        return "buy"
    if direction in {"sell", "sold", "out"}:
        return "sell"
    return "unknown"


def _wallet_verified(event: Mapping[str, Any]) -> bool:
    return bool(
        event.get("wallet_verified") is True
        or event.get("address_verified") is True
        or event.get("verified") is True
        or str(event.get("verification_status") or "").lower() == "verified"
    )


def _provider_order_winner(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if len({(event.get("provider_family"), event.get("provider_feed")) for event in events}) != 1:
        return None
    for field in (
        "provider_sequence",
        "block_number",
        "transaction_index",
        "log_index",
        "event_index",
    ):
        values = [_known_number(event.get(field)) for event in events]
        if any(value is None for value in values) or len(set(values)) == 1:
            continue
        highest = max(values)
        winners = [event for event, value in zip(events, values) if value == highest]
        if len(winners) == 1:
            return winners[0]
    return None


def _wallet_evidence(token: Mapping[str, Any], observed_at: str) -> dict[str, Any]:
    chain = token["identity"]["chain"]
    window_seconds = WALLET_FRESHNESS_SECONDS[chain]
    historical_candidate_buys: dict[str, dict[str, Any]] = {}
    historical_buys: dict[str, dict[str, Any]] = {}
    current_events: dict[str, list[dict[str, Any]]] = {}
    for event in token.get("events") or []:
        if event.get("evidence_role") != "wallet":
            continue
        address = _wallet_address(event)
        if not EVM_ADDRESS.fullmatch(address):
            continue
        if _wallet_direction(event) == "buy":
            historical_candidate_buys[address] = event
            if _wallet_verified(event):
                historical_buys[address] = event
        if _event_is_current(event, observed_at, window_seconds):
            current_events.setdefault(address, []).append(event)

    resolved: dict[str, Mapping[str, Any]] = {}
    conflicts: dict[str, list[Mapping[str, Any]]] = {}
    for address, events in current_events.items():
        latest_time = max(
            (_normalize_timestamp(event["event_at"]), _normalize_timestamp(event["observed_at"]))
            for event in events
        )
        tied = [
            event
            for event in events
            if (
                _normalize_timestamp(event["event_at"]),
                _normalize_timestamp(event["observed_at"]),
            ) == latest_time
        ]
        directions = {_wallet_direction(event) for event in tied}
        if len(directions) > 1:
            winner = _provider_order_winner(tied)
            if winner is None:
                conflicts[address] = tied
                continue
            resolved[address] = winner
        else:
            resolved[address] = min(tied, key=_canonical_json)

    candidate_buys = {
        address: event for address, event in resolved.items() if _wallet_direction(event) == "buy"
    }
    verified_buys = {
        address: event for address, event in candidate_buys.items() if _wallet_verified(event)
    }
    conflicting_ids = sorted(
        str(event["event_id"])
        for events in conflicts.values()
        for event in events
    )
    status = "verified" if verified_buys else "candidate" if candidate_buys else "conflicting" if conflicts else "unknown"
    return {
        "verified_buyers": len(verified_buys),
        "wallet_addresses": sorted(verified_buys),
        "evidence_ids": sorted(str(event["event_id"]) for event in verified_buys.values()),
        "candidate_buyers": len(candidate_buys),
        "candidate_wallet_addresses": sorted(candidate_buys),
        "candidate_evidence_ids": sorted(str(event["event_id"]) for event in candidate_buys.values()),
        "status": status,
        "freshness_window_seconds": window_seconds,
        "conflicting_wallets": sorted(conflicts),
        "conflicting_evidence_ids": conflicting_ids,
        "historical_candidate_buyers": len(historical_candidate_buys),
        "historical_candidate_wallet_addresses": sorted(historical_candidate_buys),
        "historical_verified_buyers": len(historical_buys),
        "historical_wallet_addresses": sorted(historical_buys),
    }


def _derive_resonance(
    token: Mapping[str, Any],
    flow: Mapping[str, Any],
    wallet: Mapping[str, Any],
    observed_at: str,
    market_current: bool,
) -> dict[str, Any]:
    chain = token["identity"]["chain"]
    window_seconds = RESONANCE_FRESHNESS_SECONDS[chain]
    historical_events = [
        event
        for event in token.get("events") or []
        if event.get("counts_for_resonance") is True
        and event.get("evidence_role") in RESONANCE_ROLES
    ]
    accepted_wallet_ids = set(wallet.get("evidence_ids") or [])
    events = []
    for event in historical_events:
        if not _event_is_current(event, observed_at, window_seconds):
            continue
        if event.get("evidence_role") == "wallet" and str(
            event.get("event_id") or ""
        ) not in accepted_wallet_ids:
            continue
        events.append(event)
    families = sorted({str(event.get("provider_family")) for event in events if event.get("provider_family")})
    lanes = sorted({str(event.get("signal_lane")) for event in events if event.get("signal_lane")})
    lanes_by_family: dict[str, set[str]] = {}
    for event in events:
        family = str(event.get("provider_family") or "")
        lane = str(event.get("signal_lane") or "")
        if family and lane:
            lanes_by_family.setdefault(family, set()).add(lane)
    platform_stack = any(len(values) >= 2 for values in lanes_by_family.values())
    cross_provider = len(families) >= 2
    time_confirmation = (
        market_current
        and flow.get("sample_count", 0) >= 2
        and flow.get("direction") == "accelerating"
    )
    if cross_provider and (time_confirmation or wallet.get("verified_buyers", 0) >= 2):
        subtype = "full"
    elif cross_provider:
        subtype = "cross_provider"
    elif platform_stack:
        subtype = "platform_stack"
    elif time_confirmation and len(events) >= 2:
        subtype = "persistent"
    else:
        subtype = None
    return {
        "subtype": subtype,
        "signal_resonance": subtype is not None,
        "provider_resonance": cross_provider,
        "time_confirmation": time_confirmation,
        "provider_families": families,
        "family_count": len(families),
        "signal_lanes": lanes,
        "lanes_by_family": {family: sorted(values) for family, values in sorted(lanes_by_family.items())},
        "evidence_ids": sorted(str(event["event_id"]) for event in events),
        "freshness_window_seconds": window_seconds,
        "historical_provider_families": sorted(
            {
                str(event.get("provider_family"))
                for event in historical_events
                if event.get("provider_family")
            }
        ),
        "historical_evidence_ids": sorted(
            str(event["event_id"]) for event in historical_events
        ),
    }


def _apply_audit_risk(token: dict[str, Any]) -> None:
    hard = set(token["risk"].get("hard_failures") or [])
    evidence = list(token["risk"].get("evidence") or [])
    evidence_ids = {
        str(item.get("evidence_id") or "")
        for item in evidence
        if isinstance(item, Mapping)
    }
    for fact in token.get("audit_facts") or []:
        field = fact.get("audit_field")
        if (
            field in HARD_AUDIT_FAILURES
            and fact.get("status") == "known"
            and fact.get("value") is HARD_AUDIT_FAILURES[field]
        ):
            hard.add(str(field))
            evidence_id = str(fact.get("evidence_id") or "")
            if evidence_id and evidence_id not in evidence_ids:
                evidence.append(
                    {
                        "identity_key": token.get("id"),
                        "reason": str(field),
                        "source": fact.get("provider_family"),
                        "observed_at": fact.get("observed_at"),
                        "evidence_id": evidence_id,
                    }
                )
                evidence_ids.add(evidence_id)
    token["risk"]["hard_failures"] = sorted(hard)
    token["risk"]["evidence"] = sorted(
        evidence,
        key=lambda item: (
            str(item.get("reason") or ""),
            str(item.get("observed_at") or ""),
            str(item.get("evidence_id") or ""),
        ),
    )
    token["risk"]["hard_blocked"] = bool(hard)
    token["risk"]["status"] = "blocked" if hard else ("known" if token.get("audit_facts") else "unknown")


def _quality_axis(token: Mapping[str, Any]) -> dict[str, Any]:
    market = token["market"]
    fields = {
        "liquidity_usd": market.get("liquidity_usd"),
        "holders": market.get("holders"),
        "top10_holder_pct": market.get("top10_holder_pct"),
        "dev_holder_pct": market.get("dev_holder_pct"),
        "bundler_pct": market.get("bundler_pct"),
        "sniper_pct": market.get("sniper_pct"),
        "audit_evidence_count": len(token.get("audit_facts") or []) or None,
    }
    return {"status": "known" if any(value is not None for value in fields.values()) else "unknown", **fields}


def _market_behavior(
    token: Mapping[str, Any],
    flow: Mapping[str, Any],
    resonance: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Flag observed market behavior that should not be promoted as confirmation."""
    market = token.get("market") if isinstance(token.get("market"), Mapping) else {}
    flags: list[str] = []
    evidence: dict[str, Any] = {}
    lanes = {str(item) for item in resonance.get("signal_lanes") or []}
    attention_lanes = lanes.intersection({"new_launch", "trending", "hot_search", "kol_social", "smart_money", "market_flow"})
    if attention_lanes and attention_lanes.issubset({"hot_search", "kol_social"}):
        flags.append("kol_only_attention")
        evidence["signal_lanes"] = sorted(attention_lanes)

    holders = _known_number(market.get("holders"))
    volume_5m = _known_number(market.get("volume_5m_usd"))
    volume_24h = _known_number(market.get("volume_24h_usd"))
    unique_buyers_5m = _known_number(market.get("unique_buyers_5m"))
    first_market_cap = _first_market_value(token, "market_cap_usd")
    current_market_cap = _known_number(market.get("market_cap_usd"))
    if (
        first_market_cap is not None
        and first_market_cap > 0
        and current_market_cap is not None
        and current_market_cap <= float(first_market_cap) * 0.5
    ):
        flags.append("deep_discovery_drawdown")
        evidence["discovery_drawdown_pct"] = round(
            (float(current_market_cap) / float(first_market_cap) - 1) * 100,
            2,
        )
    if holders is not None and holders <= 5 and (
        (volume_5m is not None and volume_5m >= 20_000)
        or (volume_24h is not None and volume_24h >= 50_000)
    ):
        flags.append("thin_holder_base")
        evidence["holders"] = holders
    if volume_5m is not None and volume_5m >= 20_000 and unique_buyers_5m is not None and unique_buyers_5m <= 3:
        flags.append("volume_without_buyer_growth")
        evidence["volume_5m_usd"] = volume_5m
        evidence["unique_buyers_5m"] = unique_buyers_5m

    buys_5m = _known_number(market.get("buys_5m"))
    sells_5m = _known_number(market.get("sells_5m"))
    if buys_5m is not None and sells_5m is not None and sells_5m >= 10 and sells_5m >= max(1, buys_5m) * 1.5:
        flags.append("sell_pressure")
        evidence["buys_5m"] = buys_5m
        evidence["sells_5m"] = sells_5m

    real_age = _age_seconds(observed_at, (token.get("identity") or {}).get("real_created_at"))
    had_cooling = any(
        item.get("state") == "cooling"
        for item in token.get("state_history") or []
        if isinstance(item, Mapping)
    )
    if real_age is not None and real_age >= 24 * 60 * 60 and flow.get("direction") == "accelerating" and not had_cooling:
        flags.append("old_token_sudden_pump")
        evidence["real_age_seconds"] = real_age

    flags = sorted(set(flags))
    has_participation = bool(
        (holders is not None and holders > 5)
        or (unique_buyers_5m is not None and unique_buyers_5m > 3)
        or (buys_5m is not None and sells_5m is not None)
    )
    return {
        "status": "known" if flags or has_participation else "unknown",
        "disposition": "observe" if flags else "pass" if has_participation else "unknown",
        "flags": flags,
        "evidence": evidence,
    }


def _latest_observation_at(token: Mapping[str, Any]) -> str | None:
    observations = [
        _normalize_timestamp(event["observed_at"])
        for event in token.get("events") or []
        if _as_utc(event.get("observed_at")) is not None
    ]
    market_at = token.get("market", {}).get("observed_at")
    if _as_utc(market_at) is not None:
        observations.append(_normalize_timestamp(market_at))
    return max(observations, default=None)


def _timing_axis(token: Mapping[str, Any], observed_at: str) -> dict[str, Any]:
    events = token.get("events") or []
    event_times = sorted(
        _normalize_timestamp(event["event_at"])
        for event in events
        if _as_utc(event.get("event_at")) is not None
    )
    meaningful_times = sorted(
        _normalize_timestamp(event["event_at"])
        for event in events
        if event.get("counts_for_resonance") is True
        and event.get("evidence_role") in RESONANCE_ROLES
        and _as_utc(event.get("event_at")) is not None
    )
    latest_event_at = event_times[-1] if event_times else None
    previous_meaningful_at = meaningful_times[-2] if len(meaningful_times) >= 2 else None
    identity = token["identity"]
    last_observation_at = _latest_observation_at(token)
    market_observed_at = token.get("market", {}).get("observed_at")
    return {
        "status": "known" if events else "unknown",
        "real_age_seconds": _age_seconds(observed_at, identity.get("real_created_at")),
        "first_seen_age_seconds": _age_seconds(observed_at, identity.get("first_seen_at")),
        "latest_event_age_seconds": _age_seconds(observed_at, latest_event_at),
        "current_observation_age_seconds": _age_seconds(observed_at, last_observation_at),
        "last_observation_age_seconds": _age_seconds(observed_at, last_observation_at),
        "market_snapshot_age_seconds": _age_seconds(observed_at, market_observed_at),
        "previous_meaningful_event_age_seconds": _age_seconds(
            observed_at, previous_meaningful_at
        ),
        "pair_created_at": identity.get("pair_created_at"),
        "pair_age_seconds": _age_seconds(observed_at, identity.get("pair_created_at")),
        "migration_at": identity.get("migration_at"),
        "migration_age_seconds": _age_seconds(observed_at, identity.get("migration_at")),
        "last_observation_at": last_observation_at,
        "market_observed_at": market_observed_at,
        "previous_meaningful_event_at": previous_meaningful_at,
        "report_refreshed_at": observed_at,
    }


def _is_revival(
    token: Mapping[str, Any], flow: Mapping[str, Any], had_cooling: bool, observed_at: str
) -> bool:
    baseline = [value for value in flow.get("baseline_deltas", {}).values() if value is not None]
    participation = [
        flow.get("baseline_deltas", {}).get(name)
        for name in ("unique_buyers", "net_buy_flow", "trade_frequency")
        if flow.get("baseline_deltas", {}).get(name) is not None
    ]
    accelerating_after_cooling = bool(
        had_cooling
        and flow.get("direction") == "accelerating"
        and participation
        and any(value > 0 for value in participation)
        and sum(1 if value > 0 else -1 if value < 0 else 0 for value in baseline) > 0
    )
    if not accelerating_after_cooling:
        return False

    chain = str(token["identity"]["chain"])
    if chain == "arc":
        cooling_times = [
            _as_utc(item.get("observed_at"))
            for item in token.get("state_history") or []
            if isinstance(item, Mapping) and item.get("state") == "cooling"
        ]
        last_cooling_at = max((value for value in cooling_times if value is not None), default=None)
        if last_cooling_at is None:
            return False
        for event in token.get("events") or []:
            event_time = _as_utc(event.get("event_at")) or _as_utc(event.get("observed_at"))
            if (
                event.get("counts_for_resonance") is True
                and event.get("evidence_role") in RESONANCE_ROLES
                and str(event.get("provider_family") or "") not in {"", "onchain"}
                and _event_is_current(event, observed_at, RESONANCE_FRESHNESS_SECONDS[chain])
                and event_time is not None
                and event_time > last_cooling_at
            ):
                return True
        return False

    real_age = token["ranking_axes"]["timing"].get("real_age_seconds")
    return real_age is not None and real_age >= 24 * 60 * 60


def _ordered_states(states: set[str]) -> list[str]:
    return [state for state in STATE_ORDER if state in states]


def _market_is_current(token: Mapping[str, Any], observed_at: str) -> bool:
    chain = token["identity"]["chain"]
    return _within_window(
        observed_at,
        token.get("market", {}).get("observed_at"),
        MARKET_FRESHNESS_SECONDS[chain],
    )


def _confirmation_market_value(
    token: Mapping[str, Any], field: str, observed_at: str
) -> int | float | None:
    market = token.get("market") if isinstance(token.get("market"), Mapping) else {}
    value = _known_number(market.get(field))
    if value is not None:
        return value
    chain = str(token["identity"]["chain"])
    snapshots = market.get("snapshots") if isinstance(market.get("snapshots"), list) else []
    for snapshot in reversed(snapshots):
        if not isinstance(snapshot, Mapping):
            continue
        if not _within_window(
            observed_at, snapshot.get("observed_at"), MARKET_FRESHNESS_SECONDS[chain]
        ):
            continue
        value = _known_number(snapshot.get(field))
        if value is not None:
            return value
    return None


def _first_market_value(token: Mapping[str, Any], field: str) -> int | float | None:
    market = token.get("market") if isinstance(token.get("market"), Mapping) else {}
    snapshots = market.get("snapshots") if isinstance(market.get("snapshots"), list) else []
    ordered = sorted(
        (snapshot for snapshot in snapshots if isinstance(snapshot, Mapping)),
        key=lambda snapshot: str(snapshot.get("observed_at") or ""),
    )
    for snapshot in ordered:
        value = _known_number(snapshot.get(field))
        if value is not None:
            return value
    return _known_number(market.get(field))


def _confirmation_gate(
    token: Mapping[str, Any],
    market_current: bool,
    observed_at: str,
    market_behavior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require usable market depth before source resonance becomes confirmation."""
    chain = str(token["identity"]["chain"])
    failures: list[str] = []
    if not market_current:
        failures.append("stale_market")

    identity = token.get("identity") if isinstance(token.get("identity"), Mapping) else {}
    discovery_age = None
    for field in ("real_created_at", "pair_created_at", "first_seen_at"):
        discovery_age = _age_seconds(observed_at, identity.get(field))
        if discovery_age is not None:
            break
    if (
        discovery_age is not None
        and discovery_age > RESONANCE_FRESHNESS_SECONDS[chain]
    ):
        failures.append("discovery_window_expired")

    market_cap = _confirmation_market_value(token, "market_cap_usd", observed_at)
    if market_cap is None:
        failures.append("market_cap_unknown")
    elif market_cap < CONFIRMATION_MIN_MARKET_CAP_USD[chain]:
        failures.append("market_cap_too_low")

    initial_market_cap = _first_market_value(token, "market_cap_usd")
    if (
        initial_market_cap is not None
        and initial_market_cap > CONFIRMATION_MAX_INITIAL_MARKET_CAP_USD[chain]
    ):
        failures.append("initial_market_cap_too_high")

    liquidity = _confirmation_market_value(token, "liquidity_usd", observed_at)
    if liquidity is None:
        failures.append("liquidity_unknown")
    elif liquidity < CONFIRMATION_MIN_LIQUIDITY_USD[chain]:
        failures.append("liquidity_too_low")

    minimum_holders = CONFIRMATION_MIN_HOLDERS[chain]
    if minimum_holders is not None:
        holders = _confirmation_market_value(token, "holders", observed_at)
        if holders is None:
            failures.append("holders_unknown")
        elif holders < minimum_holders:
            failures.append("holders_too_low")

    if isinstance(market_behavior, Mapping) and market_behavior.get("disposition") == "observe":
        failures.extend(f"market_behavior:{flag}" for flag in market_behavior.get("flags") or [])

    return {
        "eligible": not failures,
        "failures": failures,
        "minimum_market_cap_usd": CONFIRMATION_MIN_MARKET_CAP_USD[chain],
        "minimum_liquidity_usd": CONFIRMATION_MIN_LIQUIDITY_USD[chain],
        "minimum_holders": minimum_holders,
    }


def _market_follow_through(token: Mapping[str, Any]) -> bool:
    current_market_cap = _known_number(
        (token.get("market") or {}).get("market_cap_usd")
        if isinstance(token.get("market"), Mapping)
        else None
    )
    first_market_cap = _first_market_value(token, "market_cap_usd")
    if current_market_cap is None or first_market_cap is None or first_market_cap <= 0:
        return False
    return float(current_market_cap) >= float(first_market_cap) * CONFIRMATION_MIN_MCAP_MULTIPLE


def _ready_for_aggregate_early(
    token: Mapping[str, Any],
    resonance: Mapping[str, Any],
    flow: Mapping[str, Any],
    wallet: Mapping[str, Any],
) -> bool:
    families = set(resonance.get("provider_families") or [])
    lanes = set(resonance.get("signal_lanes") or [])
    if token["identity"]["chain"] == "arc":
        current_event_ids = {str(item) for item in resonance.get("evidence_ids") or []}
        has_independent_ranking = any(
            str(event.get("event_id") or "") in current_event_ids
            and event.get("evidence_role") == "ranking"
            and str(event.get("provider_family") or "") not in {"", "onchain"}
            for event in token.get("events") or []
        )
        return "onchain" in families and has_independent_ranking
    return bool(
        resonance.get("subtype") == "platform_stack"
        or len(families) >= 3
        or lanes.intersection({"smart_money", "market_flow", "kol_social"})
        or flow.get("direction") == "accelerating"
        or int(wallet.get("verified_buyers") or 0) >= 1
        or _market_follow_through(token)
    )


def _ready_for_aggregate_confirmation(
    token: Mapping[str, Any],
    resonance: Mapping[str, Any],
    wallet: Mapping[str, Any],
) -> bool:
    if token["identity"]["chain"] == "arc":
        return int(resonance.get("family_count") or 0) >= 3
    return bool(
        resonance.get("time_confirmation")
        or resonance.get("subtype") == "full"
        or int(wallet.get("verified_buyers") or 0) >= 2
        or _market_follow_through(token)
    )


def _confirmation_last_seen_at(token: Mapping[str, Any]) -> str | None:
    explicit = _as_utc(token.get("confirmation_last_seen_at"))
    if explicit is not None:
        return explicit.isoformat()
    observed = [
        _as_utc(item.get("observed_at"))
        for item in token.get("state_history") or []
        if isinstance(item, Mapping) and item.get("state") == "resonating"
    ]
    known = [value for value in observed if value is not None]
    return max(known).isoformat() if known else None


def _derive_token(token: dict[str, Any], observed_at: str) -> None:
    previous_primary = token.get("primary_state") or "new"
    previous_states = set(token.get("active_states") or ["new"])
    previous_signature = (previous_primary, tuple(sorted(previous_states - {"stale"})))
    state_history = token.setdefault("state_history", [])
    if not state_history:
        state_history.append(
            {
                "state": previous_primary,
                "state_version": int(token.get("state_version") or 1),
                "observed_at": token.get("state_updated_at") or token["identity"].get("first_seen_at"),
            }
        )
    market_current = _market_is_current(token, observed_at)
    flow = _derive_flow(token)
    wallet = _wallet_evidence(token, observed_at)
    resonance = _derive_resonance(token, flow, wallet, observed_at, market_current)
    market_behavior = _market_behavior(token, flow, resonance, observed_at)
    confirmation_gate = _confirmation_gate(
        token, market_current, observed_at, market_behavior
    )
    resonance["confirmation_gate"] = confirmation_gate
    token["wallet_evidence"] = wallet
    token["resonance"] = resonance
    _apply_audit_risk(token)
    token["ranking_axes"] = {
        "flow": flow,
        "quality": _quality_axis(token),
        "market_behavior": market_behavior,
        "risk": deepcopy(token["risk"]),
        "narrative": {
            "status": "known" if "kol_social" in resonance["signal_lanes"] else "unknown",
            "evidence_ids": [
                event["event_id"]
                for event in token["events"]
                if event.get("signal_lane") == "kol_social"
                and event.get("event_id") in resonance["evidence_ids"]
            ],
        },
        "timing": {},
    }
    token["ranking_axes"]["timing"] = _timing_axis(token, observed_at)

    states = {"new"}
    early_ready = _ready_for_aggregate_early(token, resonance, flow, wallet)
    if token["identity"]["chain"] == "arc":
        building_now = bool(
            market_current
            and resonance["subtype"] is not None
            and confirmation_gate["eligible"]
            and early_ready
        )
    else:
        building_now = bool(
            market_current
            and (
                flow["direction"] == "accelerating"
                or (
                    resonance["subtype"] is not None
                    and confirmation_gate["eligible"]
                    and early_ready
                )
            )
        )
    if building_now:
        states.add("building")
    confirmed_now = bool(
        resonance["subtype"] is not None
        and confirmation_gate["eligible"]
        and early_ready
        and _ready_for_aggregate_confirmation(token, resonance, wallet)
    )
    if confirmed_now:
        states.add("resonating")
        token["confirmation_last_seen_at"] = observed_at
    elif "resonating" in previous_states:
        last_confirmed_at = _confirmation_last_seen_at(token)
        hold_seconds = CONFIRMATION_HOLD_SECONDS[token["identity"]["chain"]]
        hold_active = (
            market_current
            and confirmation_gate["eligible"]
            and market_behavior["disposition"] != "observe"
            and not token["risk"]["hard_blocked"]
            and _within_window(observed_at, last_confirmed_at, hold_seconds)
        )
        if hold_active:
            states.add("resonating")
    had_selected_state = bool(
        previous_states.intersection({"building", "resonating", "smart_cluster"})
        or any(
            item.get("state") in {"building", "resonating", "smart_cluster"}
            for item in state_history
            if isinstance(item, Mapping)
        )
    )
    if (
        "discovery_window_expired" in confirmation_gate["failures"]
        and had_selected_state
        and resonance["subtype"] is not None
        and market_current
    ):
        states.discard("resonating")
        states.discard("building")
        states.add("trend_watch")
    if wallet["verified_buyers"] >= 2:
        states.add("smart_cluster")
    if market_current and flow["direction"] == "decaying" and (
        flow.get("previous_positive")
        or previous_states.intersection({"building", "resonating", "smart_cluster"})
    ):
        states.add("cooling")
    had_cooling = "cooling" in previous_states or any(
        item.get("state") == "cooling" for item in state_history
    )
    if market_current and _is_revival(token, flow, had_cooling, observed_at):
        states.add("revival")
    if market_current and market_behavior["disposition"] == "observe" and (
        resonance["subtype"] is not None or "building" in states
    ):
        states.discard("resonating")
        states.discard("building")
        states.add("trend_watch")
    if token["risk"]["hard_blocked"]:
        states.discard("resonating")
        states.discard("building")
        states.add("blocked_risk")

    freshness = token.setdefault("freshness", {})
    latest_observation_at = _latest_observation_at(token)
    observation_current = _within_window(
        observed_at,
        latest_observation_at,
        OBSERVATION_FRESHNESS_SECONDS[token["identity"]["chain"]],
    )
    if observation_current:
        freshness["status"] = "fresh"
        freshness["last_observed_at"] = latest_observation_at
    else:
        freshness["status"] = "stale"
        if latest_observation_at is not None:
            freshness["last_observed_at"] = latest_observation_at
        states.add("stale")
    freshness["report_refreshed_at"] = observed_at

    primary = "new"
    for state in ("building", "resonating", "cooling", "smart_cluster", "trend_watch", "revival", "blocked_risk"):
        if state in states:
            primary = state
    current_signature = (primary, tuple(sorted(states - {"stale"})))
    if current_signature != previous_signature:
        token["state_version"] = int(token.get("state_version") or 0) + 1
        token["state_updated_at"] = observed_at
        state_history.append(
            {
                "state": primary,
                "state_version": token["state_version"],
                "observed_at": observed_at,
            }
        )
    token["primary_state"] = primary
    token["active_states"] = _ordered_states(states)
    token["missing_evidence"] = sorted(
        field for field in MARKET_FIELDS if token["market"].get(field) is None
    )
    ai = token.get("ai") if isinstance(token.get("ai"), Mapping) else {}
    if ai.get("status") == "pending" and not ai.get("analyzed_at") and not ai.get("evidence_ids"):
        token["ai"] = {"status": "unavailable", "analyzed_at": None, "evidence_ids": []}


def compact_monitor_snapshot(
    snapshot: Mapping[str, Any],
    *,
    include_stale: bool = False,
    token_limit: int = 120,
) -> dict[str, Any]:
    """Return a bounded projection, optionally retaining stale continuity state."""
    compact = deepcopy(dict(snapshot))
    tokens = []
    for raw in snapshot.get("tokens") or []:
        if not isinstance(raw, Mapping):
            continue
        freshness = raw.get("freshness") if isinstance(raw.get("freshness"), Mapping) else {}
        if not include_stale and freshness.get("status") not in (None, "fresh"):
            continue
        token = deepcopy(dict(raw))

        events = [item for item in token.get("events") or [] if isinstance(item, Mapping)]
        recent_events = sorted(
            events,
            key=lambda item: str(item.get("observed_at") or item.get("event_at") or ""),
            reverse=True,
        )[:48]
        earliest_event = min(
            events,
            key=lambda value: str(value.get("observed_at") or value.get("event_at") or ""),
            default=None,
        )
        latest_discovery = next(
            (
                item for item in sorted(
                    events,
                    key=lambda value: str(value.get("observed_at") or value.get("event_at") or ""),
                    reverse=True,
                )
                if item.get("evidence_role") == "discovery"
            ),
            None,
        )
        if latest_discovery is not None and latest_discovery not in recent_events:
            recent_events.append(latest_discovery)
        if earliest_event is not None and earliest_event not in recent_events:
            recent_events.append(earliest_event)
        token["events"] = sorted(
            recent_events,
            key=lambda item: str(item.get("observed_at") or item.get("event_at") or ""),
        )

        market = token.get("market") if isinstance(token.get("market"), dict) else {}
        snapshots = [item for item in market.get("snapshots") or [] if isinstance(item, Mapping)]
        keep_snapshots = snapshots[-12:]
        if snapshots:
            keep_snapshots.extend([snapshots[0]])
            peak = max(snapshots, key=lambda item: float(item.get("market_cap_usd") or 0))
            keep_snapshots.append(peak)
        deduped_snapshots = {
            str(item.get("observed_at") or index): deepcopy(dict(item))
            for index, item in enumerate(keep_snapshots)
        }
        market["snapshots"] = sorted(
            deduped_snapshots.values(), key=lambda item: str(item.get("observed_at") or "")
        )
        token["market"] = market
        token["state_history"] = list(token.get("state_history") or [])[-12:]
        token["audit_facts"] = list(token.get("audit_facts") or [])[-16:]
        risk = token.get("risk") if isinstance(token.get("risk"), dict) else {}
        risk["evidence"] = list(risk.get("evidence") or [])[-16:]
        token["risk"] = risk
        resonance = token.get("resonance") if isinstance(token.get("resonance"), dict) else {}
        resonance["historical_evidence_ids"] = list(resonance.get("historical_evidence_ids") or [])[-48:]
        token["resonance"] = resonance
        wallet = token.get("wallet_evidence") if isinstance(token.get("wallet_evidence"), dict) else {}
        wallet["historical_candidate_wallet_addresses"] = list(
            wallet.get("historical_candidate_wallet_addresses") or []
        )[-48:]
        wallet["historical_wallet_addresses"] = list(wallet.get("historical_wallet_addresses") or [])[-48:]
        token["wallet_evidence"] = wallet
        tokens.append(token)

    compact["tokens"] = sorted(
        tokens,
        key=lambda item: str(item.get("state_updated_at") or ""),
        reverse=True,
    )[: max(0, token_limit)]
    compact["rejections"] = list(snapshot.get("rejections") or [])[-120:]
    if isinstance(compact.get("alerts"), list):
        compact["alerts"] = compact["alerts"][-80:]
    compact["counts"] = _snapshot_counts(compact["tokens"], compact.get("alerts") or [])
    compact["kill_counts"] = _kill_counts(compact["tokens"], compact["rejections"])
    compact["empty_state"] = _empty_state(
        compact["tokens"],
        compact["rejections"],
        compact.get("source_health") or {},
    )
    return compact


def _monitor_status(source_health: Mapping[str, Any]) -> str:
    if not source_health:
        return "unknown"
    statuses = []
    for health in source_health.values():
        if isinstance(health, Mapping):
            statuses.append(str(health.get("status") or "unknown").lower())
        elif isinstance(health, bool):
            statuses.append("ok" if health else "error")
        else:
            statuses.append(str(health or "unknown").lower())
    good = {"ok", "healthy", "success", "fresh"}
    return "healthy" if statuses and all(status in good for status in statuses) else "degraded"


def _merge_health_entry(previous: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(previous))
    for field, value in incoming.items():
        normalized_field = str(field).strip().lower()
        is_timestamp = normalized_field in {"last_success", "last_event"} or normalized_field.endswith(
            ("_at", "_time", "_timestamp")
        )
        if is_timestamp:
            previous_time = _as_utc(previous.get(field))
            incoming_time = _as_utc(value)
            if previous_time is not None and (
                incoming_time is None or incoming_time < previous_time
            ):
                continue
        merged[field] = deepcopy(value)
    if str(incoming.get("status") or "").strip().lower() in {
        "ok", "healthy", "success", "fresh",
    } and not incoming.get("error"):
        merged.pop("error", None)
        merged.pop("error_category", None)
        merged.pop("stale_age_seconds", None)
    return merged


def _health_observation_time(health: Mapping[str, Any]) -> datetime | None:
    values = [
        parsed
        for field in ("observed_at", "updated_at", "checked_at", "collected_at", "report_at")
        if (parsed := _as_utc(health.get(field))) is not None
    ]
    return max(values, default=None)


def _merge_source_health(
    previous: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
    *,
    previous_observed_at: str | None,
    incoming_observed_at: str,
) -> dict[str, Any]:
    previous_health = (previous or {}).get("source_health") or {}
    merged = deepcopy(dict(previous_health))
    stale_publisher = (
        previous_observed_at is not None
        and _as_utc(incoming_observed_at) < _as_utc(previous_observed_at)
    )
    if stale_publisher:
        return merged
    # The generic source is a per-cycle fallback, not a durable provider.
    # Drop its previous success once the current cycle reports explicit sources.
    if incoming and "meme_sources" not in incoming:
        merged.pop("meme_sources", None)
    for source, health in incoming.items():
        prior = merged.get(source)
        if isinstance(prior, Mapping) and isinstance(health, Mapping):
            prior_observed = _health_observation_time(prior)
            incoming_observed = _health_observation_time(health)
            if (
                prior_observed is not None
                and incoming_observed is not None
                and incoming_observed < prior_observed
            ):
                continue
            merged[source] = _merge_health_entry(prior, health)
        else:
            merged[source] = deepcopy(health)
    current_time = _as_utc(incoming_observed_at)
    if current_time is None:
        return merged
    healthy_statuses = {"ok", "healthy", "success", "fresh"}
    for health in merged.values():
        if not isinstance(health, dict):
            continue
        status = str(health.get("status") or "unknown").strip().lower()
        if status not in healthy_statuses:
            continue
        source_time = _health_observation_time(health)
        if source_time is None:
            continue
        age_seconds = max(0.0, (current_time - source_time).total_seconds())
        if age_seconds > SOURCE_HEALTH_STALE_SECONDS:
            health["status"] = "stale"
            health["stale_age_seconds"] = round(age_seconds, 3)
        else:
            health.pop("stale_age_seconds", None)
    return merged


def _material_state_signature(token: Mapping[str, Any]) -> tuple[Any, ...]:
    resonance = token.get("resonance") if isinstance(token.get("resonance"), Mapping) else {}
    wallet = token.get("wallet_evidence") if isinstance(token.get("wallet_evidence"), Mapping) else {}
    risk = token.get("risk") if isinstance(token.get("risk"), Mapping) else {}
    ranking = token.get("ranking_axes") if isinstance(token.get("ranking_axes"), Mapping) else {}
    flow = ranking.get("flow") if isinstance(ranking.get("flow"), Mapping) else {}
    behavior = ranking.get("market_behavior") if isinstance(ranking.get("market_behavior"), Mapping) else {}
    return (
        str(token.get("primary_state") or "new"),
        tuple(sorted(set(token.get("active_states") or []) - {"stale"})),
        resonance.get("subtype"),
        tuple(sorted(resonance.get("provider_families") or [])),
        tuple(sorted(resonance.get("signal_lanes") or [])),
        str(flow.get("direction") or "unknown"),
        str(behavior.get("disposition") or "unknown"),
        tuple(sorted(behavior.get("flags") or [])),
        tuple(sorted(wallet.get("wallet_addresses") or [])),
        tuple(sorted(wallet.get("candidate_wallet_addresses") or [])),
        tuple(sorted(risk.get("hard_failures") or [])),
        tuple(sorted(risk.get("soft_flags") or [])),
        tuple(
            sorted(
                (
                    str(item.get("reason") or ""),
                    str(item.get("source") or ""),
                )
                for item in risk.get("evidence") or []
                if isinstance(item, Mapping)
            )
        ),
    )


def _alert_policy(token: Mapping[str, Any]) -> dict[str, str] | None:
    identity = token.get("identity") if isinstance(token.get("identity"), Mapping) else {}
    chain = str(identity.get("chain") or "")
    states = set(token.get("active_states") or [])
    resonance = token.get("resonance") if isinstance(token.get("resonance"), Mapping) else {}
    wallet = token.get("wallet_evidence") if isinstance(token.get("wallet_evidence"), Mapping) else {}
    ranking = token.get("ranking_axes") if isinstance(token.get("ranking_axes"), Mapping) else {}
    flow = ranking.get("flow") if isinstance(ranking.get("flow"), Mapping) else {}
    behavior = ranking.get("market_behavior") if isinstance(ranking.get("market_behavior"), Mapping) else {}
    subtype = str(resonance.get("subtype") or "")
    families = set(resonance.get("provider_families") or [])
    lanes = set(resonance.get("signal_lanes") or [])
    verified_buyers = int(wallet.get("verified_buyers") or 0)
    candidate_buyers = int(wallet.get("candidate_buyers") or 0)
    candidate_ids = {str(item) for item in wallet.get("candidate_evidence_ids") or []}
    candidate_families = {
        str(event.get("provider_family") or "")
        for event in token.get("events") or []
        if str(event.get("event_id") or "") in candidate_ids
        and event.get("provider_family")
    }
    has_candidate_wallet_signal = candidate_buyers >= 2 and (
        len(candidate_families) >= 2 or candidate_buyers >= 3
    )
    confirmation_gate = resonance.get("confirmation_gate")
    gate_failures = set()
    if isinstance(confirmation_gate, Mapping):
        gate_failures = {str(item) for item in confirmation_gate.get("failures") or []}

    if "blocked_risk" in states:
        return {"state": "blocked_risk", "stage": "critical_risk", "severity": "critical", "label": "高风险"}
    if "revival" in states:
        return {"state": "revival", "stage": "revival", "severity": "high", "label": "加速"}
    if "trend_watch" in states:
        return {
            "state": "trend_watch",
            "stage": "market_behavior",
            "severity": "medium",
            "label": "趋势观察",
        }

    if "discovery_window_expired" in gate_failures:
        has_late_attention = bool(
            verified_buyers >= 1
            or "building" in states
            or "smart_money" in lanes
        )
        if has_late_attention:
            return {
                "state": "late_resonance",
                "stage": "late_resonance",
                "severity": "medium",
                "label": "趋势观察",
            }
        return None

    if chain == "arc":
        gate_eligible = bool(
            isinstance(confirmation_gate, Mapping)
            and confirmation_gate.get("eligible") is True
        )
        early_ready = _ready_for_aggregate_early(token, resonance, flow, wallet)
        if "resonating" in states and gate_eligible:
            return {
                "state": subtype or "resonating",
                "stage": "high_priority",
                "severity": "high",
                "label": "多源共振",
            }
        if "building" in states and gate_eligible and early_ready:
            return {
                "state": subtype or "building",
                "stage": "early_focus",
                "severity": "medium",
                "label": "重点",
            }
        return None

    if "smart_cluster" in states:
        return {"state": "smart_cluster", "stage": "high_priority", "severity": "high", "label": "重点"}

    if has_candidate_wallet_signal:
        return {"state": "wallet_candidate", "stage": "smart_money", "severity": "medium", "label": "重点"}

    if (
        subtype
        and isinstance(confirmation_gate, Mapping)
        and confirmation_gate.get("eligible") is not True
        and verified_buyers < 1
    ):
        if "building" in states:
            return {"state": "building", "stage": "transition", "severity": "medium", "label": "加速"}
        return None

    current_event_ids = set(resonance.get("evidence_ids") or [])
    current_events = [
        event
        for event in token.get("events") or []
        if str(event.get("event_id") or "") in current_event_ids
    ]
    has_live_gmgn_trend = any(
        event.get("provider_family") == "gmgn"
        and event.get("signal_lane") == "trending"
        and "live" in str(event.get("provider_feed") or "").lower()
        for event in current_events
    )
    has_launch_and_trend = {"new_launch", "trending"}.issubset(lanes)

    if chain == "bsc":
        early_focus = has_live_gmgn_trend or has_launch_and_trend
        high_priority = early_focus and (
            len(families) >= 2
            or flow.get("direction") == "accelerating"
            or verified_buyers >= 1
        )
        if high_priority:
            return {
                "state": subtype or "early_focus",
                "stage": "high_priority",
                "severity": "high",
                "label": "多源共振" if len(families) >= 2 else "加速" if flow.get("direction") == "accelerating" else "重点",
            }
        if early_focus:
            return {"state": subtype or "early_focus", "stage": "early_focus", "severity": "medium", "label": "重点"}
        if "building" in states:
            return {"state": "building", "stage": "transition", "severity": "medium", "label": "加速"}
        return None

    if chain == "robinhood":
        stage_ready = bool(states.intersection({"building", "resonating"}))
        has_launch = "new_launch" in lanes
        has_verified_wallet = verified_buyers >= 1
        two_lane_stack = subtype == "platform_stack" and len(lanes) >= 2
        early_focus = (has_launch and has_verified_wallet) or two_lane_stack
        high_priority = stage_ready and (
            (len(families) >= 2 and len(lanes) >= 2)
            or (flow.get("direction") == "accelerating" and has_verified_wallet)
        )
        if high_priority:
            return {
                "state": subtype or "building",
                "stage": "high_priority",
                "severity": "high",
                "label": "多源共振" if len(families) >= 2 else "加速",
            }
        if early_focus:
            return {"state": subtype or "early_focus", "stage": "early_focus", "severity": "medium", "label": "重点"}
        if "building" in states:
            return {"state": "building", "stage": "transition", "severity": "medium", "label": "加速"}
        return None

    if subtype in {"cross_provider", "full"}:
        return {"state": subtype, "stage": "transition", "severity": "high", "label": "多源共振"}
    if subtype in {"platform_stack", "persistent"}:
        return {"state": subtype, "stage": "transition", "severity": "medium", "label": "重点"}
    if "building" in states:
        return {"state": "building", "stage": "transition", "severity": "medium", "label": "加速"}
    return None


def _source_alert_evidence(token: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = (
        "provider_family",
        "provider_feed",
        "evidence_role",
        "signal_lane",
        "event_at",
        "observed_at",
        "source_url",
    )
    records = []
    for event in token.get("events") or []:
        evidence_id = str(event.get("event_id") or "")
        if not evidence_id:
            continue
        records.append({**{field: event.get(field) for field in fields}, "evidence_id": evidence_id})
    return sorted(
        records,
        key=lambda item: (
            str(item.get("event_at") or ""),
            str(item.get("provider_family") or ""),
            str(item.get("provider_feed") or ""),
            str(item.get("evidence_id") or ""),
        ),
    )


def _verified_wallet_alert_evidence(token: Mapping[str, Any]) -> list[dict[str, Any]]:
    wallet = token.get("wallet_evidence") if isinstance(token.get("wallet_evidence"), Mapping) else {}
    accepted_ids = set(wallet.get("evidence_ids") or [])
    return _wallet_alert_evidence(token, accepted_ids)


def _candidate_wallet_alert_evidence(token: Mapping[str, Any]) -> list[dict[str, Any]]:
    wallet = token.get("wallet_evidence") if isinstance(token.get("wallet_evidence"), Mapping) else {}
    accepted_ids = set(wallet.get("candidate_evidence_ids") or [])
    return _wallet_alert_evidence(token, accepted_ids)


def _wallet_alert_evidence(
    token: Mapping[str, Any],
    accepted_ids: set[Any],
) -> list[dict[str, Any]]:
    records = []
    for event in token.get("events") or []:
        evidence_id = str(event.get("event_id") or "")
        if evidence_id not in accepted_ids:
            continue
        records.append(
            {
                "address": _wallet_address(event),
                "evidence_id": evidence_id,
                "provider_family": event.get("provider_family"),
                "provider_feed": event.get("provider_feed"),
                "event_at": event.get("event_at"),
                "observed_at": event.get("observed_at"),
                "net_flow_usd": _known_number(event.get("amount_usd")),
            }
        )
    return sorted(records, key=lambda item: (item["address"], item["evidence_id"]))


def _alert_market(token: Mapping[str, Any]) -> dict[str, Any]:
    market = token.get("market") if isinstance(token.get("market"), Mapping) else {}
    snapshots = market.get("snapshots") or []
    first_market_cap = next(
        (snapshot.get("market_cap_usd") for snapshot in snapshots if snapshot.get("market_cap_usd") is not None),
        None,
    )
    current_market_cap = market.get("market_cap_usd")
    multiple = None
    if isinstance(first_market_cap, (int, float)) and first_market_cap > 0 and isinstance(current_market_cap, (int, float)):
        multiple = current_market_cap / first_market_cap
    fields = (
        "liquidity_usd",
        "buys_1m",
        "sells_1m",
        "buys_5m",
        "sells_5m",
        "unique_buyers_1m",
        "unique_buyers_5m",
        "net_buy_flow_1m_usd",
        "net_buy_flow_5m_usd",
    )
    return {
        "first_market_cap_usd": first_market_cap,
        "current_market_cap_usd": current_market_cap,
        "market_cap_multiple": multiple,
        "first_observed_at": snapshots[0].get("observed_at") if snapshots else None,
        "current_observed_at": market.get("observed_at"),
        **{field: market.get(field) for field in fields},
    }


def _build_alert(
    token: Mapping[str, Any],
    *,
    previous_state: str,
    policy: Mapping[str, str],
    observed_at: str,
) -> dict[str, Any]:
    identity = token["identity"]
    state = str(policy["state"])
    transition = f"{previous_state}->{state}"
    state_version = int(token.get("state_version") or 1)
    sources = _source_alert_evidence(token)
    latest_event_at = max(
        (str(item["event_at"]) for item in sources if item.get("event_at")),
        default=None,
    )
    wallets = _verified_wallet_alert_evidence(token)
    candidate_wallets = _candidate_wallet_alert_evidence(token)
    wallet_flows = [
        item["net_flow_usd"]
        for item in wallets
        if isinstance(item.get("net_flow_usd"), (int, float))
    ]
    candidate_wallet_flows = [
        item["net_flow_usd"]
        for item in candidate_wallets
        if isinstance(item.get("net_flow_usd"), (int, float))
    ]
    risk = token.get("risk") if isinstance(token.get("risk"), Mapping) else {}
    audit_facts = deepcopy(token.get("audit_facts") or [])
    evidence_ids = sorted(
        {str(item["evidence_id"]) for item in sources}
        | {str(fact.get("evidence_id")) for fact in audit_facts if fact.get("evidence_id")}
    )
    return {
        "key": f"{identity['chain']}:{identity['contract_address']}:{transition}:{state_version}",
        "label": policy["label"],
        "severity": policy["severity"],
        "policy": {"chain": identity["chain"], "stage": policy["stage"]},
        "chain": identity["chain"],
        "contract_address": identity["contract_address"],
        "symbol": identity.get("symbol"),
        "transition": transition,
        "state_version": state_version,
        "event_at": latest_event_at,
        "observed_at": observed_at,
        "real_age_seconds": _age_seconds(observed_at, identity.get("real_created_at")),
        "event_age_seconds": _age_seconds(observed_at, latest_event_at),
        "market": _alert_market(token),
        "sources": sources,
        "verified_wallet_count": len(wallets),
        "verified_wallet_net_flow_usd": sum(wallet_flows) if wallet_flows else None,
        "verified_wallets": wallets,
        "candidate_wallet_count": len(candidate_wallets),
        "candidate_wallet_net_flow_usd": sum(candidate_wallet_flows) if candidate_wallet_flows else None,
        "candidate_wallets": candidate_wallets,
        "risks": {
            "hard_blocked": bool(risk.get("hard_blocked")),
            "hard_failures": sorted(risk.get("hard_failures") or []),
            "soft_flags": sorted(risk.get("soft_flags") or []),
            "evidence": deepcopy(risk.get("evidence") or []),
            "conflicts": [
                deepcopy(fact)
                for fact in audit_facts
                if fact.get("status") == "conflicting"
            ],
            "audit_facts": audit_facts,
        },
        "missing_evidence": sorted(token.get("missing_evidence") or []),
        "evidence_ids": evidence_ids,
    }


def _alert_structural_signature(alert: Mapping[str, Any]) -> tuple[Any, ...]:
    risks = alert.get("risks") if isinstance(alert.get("risks"), Mapping) else {}
    return (
        tuple(
            sorted(
                str(item.get("evidence_id") or "")
                for item in alert.get("sources") or []
                if isinstance(item, Mapping) and item.get("evidence_id")
            )
        ),
        tuple(
            sorted(
                (
                    str(item.get("address") or ""),
                    str(item.get("evidence_id") or ""),
                )
                for item in alert.get("verified_wallets") or []
                if isinstance(item, Mapping)
            )
        ),
        tuple(sorted(str(item) for item in risks.get("hard_failures") or [])),
        tuple(sorted(str(item) for item in risks.get("soft_flags") or [])),
        tuple(
            sorted(
                str(item.get("evidence_id") or "")
                for item in risks.get("evidence") or []
                if isinstance(item, Mapping) and item.get("evidence_id")
            )
        ),
    )


def _alert_allowed_by_cooldown(
    alert: Mapping[str, Any], prior_alerts: Sequence[Mapping[str, Any]]
) -> bool:
    matching = [
        item
        for item in prior_alerts
        if item.get("chain") == alert.get("chain")
        and item.get("contract_address") == alert.get("contract_address")
    ]
    if not matching:
        return True
    latest = max(
        matching,
        key=lambda item: (
            _as_utc(item.get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc),
            int(item.get("state_version") or 0),
            str(item.get("key") or ""),
        ),
    )
    age = _age_seconds(str(alert.get("observed_at") or ""), latest.get("observed_at"))
    if age is not None and age >= ALERT_COOLDOWN_SECONDS:
        return True

    severity_rank = {"medium": 1, "high": 2, "critical": 3}
    current_severity = str(alert.get("severity") or "")
    latest_severity = str(latest.get("severity") or "")
    if severity_rank.get(current_severity, 0) > severity_rank.get(latest_severity, 0):
        return True
    if current_severity == "critical" and _alert_structural_signature(alert) != _alert_structural_signature(latest):
        return True
    if _alert_structural_signature(alert) != _alert_structural_signature(latest):
        return True
    return False


def _snapshot_counts(tokens: Sequence[Mapping[str, Any]], alerts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    primary: dict[str, int] = {}
    active: dict[str, int] = {}
    freshness: dict[str, int] = {}
    for token in tokens:
        state = str(token.get("primary_state") or "unknown")
        primary[state] = primary.get(state, 0) + 1
        for facet in token.get("active_states") or []:
            key = str(facet)
            active[key] = active.get(key, 0) + 1
        fresh = token.get("freshness") if isinstance(token.get("freshness"), Mapping) else {}
        status = str(fresh.get("status") or "unknown")
        freshness[status] = freshness.get(status, 0) + 1
    return {
        "tokens": len(tokens),
        "alerts": len(alerts),
        "primary_state": dict(sorted(primary.items())),
        "active_state": dict(sorted(active.items())),
        "freshness": dict(sorted(freshness.items())),
    }


def _kill_counts(tokens: Sequence[Mapping[str, Any]], rejections: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_reason: dict[str, int] = {}
    rejection_count = 0
    hard_risk_count = 0
    behavior_downgrade_count = 0
    for rejection in rejections:
        reason = str(rejection.get("reason") or "unknown_rejection")
        by_reason[reason] = by_reason.get(reason, 0) + 1
        rejection_count += 1
    for token in tokens:
        risk = token.get("risk") if isinstance(token.get("risk"), Mapping) else {}
        for reason in risk.get("hard_failures") or []:
            key = str(reason)
            by_reason[key] = by_reason.get(key, 0) + 1
            hard_risk_count += 1
        ranking = token.get("ranking_axes") if isinstance(token.get("ranking_axes"), Mapping) else {}
        behavior = ranking.get("market_behavior") if isinstance(ranking.get("market_behavior"), Mapping) else {}
        if behavior.get("disposition") == "observe":
            for reason in behavior.get("flags") or []:
                key = str(reason)
                by_reason[key] = by_reason.get(key, 0) + 1
                behavior_downgrade_count += 1
    return {
        "total": rejection_count + hard_risk_count + behavior_downgrade_count,
        "rejections": rejection_count,
        "hard_risks": hard_risk_count,
        "by_reason": dict(sorted(by_reason.items())),
    }


def _source_status_groups(source_health: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    failed_statuses = {"error", "failed", "failure", "timeout", "unavailable"}
    stale_statuses = {"stale", "expired"}
    failed: list[str] = []
    stale: list[str] = []
    for source, raw in source_health.items():
        status = str(raw.get("status") if isinstance(raw, Mapping) else raw or "unknown").lower()
        if status in failed_statuses:
            failed.append(str(source))
        elif status in stale_statuses:
            stale.append(str(source))
    return sorted(failed), sorted(stale)


def _empty_state(
    tokens: Sequence[Mapping[str, Any]],
    rejections: Sequence[Mapping[str, Any]],
    source_health: Mapping[str, Any],
) -> dict[str, Any]:
    failed, stale = _source_status_groups(source_health)
    event_count = sum(len(token.get("events") or []) for token in tokens)
    if tokens:
        reason = None
    elif failed:
        reason = "source_failure"
    elif stale:
        reason = "stale_sources"
    elif rejections:
        reason = "all_rejected"
    else:
        reason = "no_events"
    return {
        "is_empty": not bool(tokens),
        "reason": reason,
        "event_count": event_count,
        "rejection_count": len(rejections),
        "failed_sources": failed,
        "stale_sources": stale,
    }


def update_monitor_state(
    previous: Mapping[str, Any] | None,
    *,
    events: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    rejections: Sequence[Mapping[str, Any]],
    source_health: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Project immutable provider evidence into a retained V3 token snapshot."""
    incoming_stamp = _normalize_timestamp(observed_at)
    previous_stamp = (
        _normalize_timestamp(previous["observed_at"])
        if previous and _as_utc(previous.get("observed_at")) is not None
        else None
    )
    out_of_order = previous_stamp is not None and incoming_stamp < previous_stamp
    stamp = max(incoming_stamp, previous_stamp) if previous_stamp else incoming_stamp
    merged_source_health = _merge_source_health(
        previous,
        source_health,
        previous_observed_at=previous_stamp,
        incoming_observed_at=incoming_stamp,
    )
    tokens = {
        str(token["id"]): deepcopy(dict(token))
        for token in (previous or {}).get("tokens", [])
        if isinstance(token, Mapping) and token.get("id")
    }
    previous_tokens = deepcopy(tokens)
    event_touches = _apply_events(tokens, events)
    candidate_touches = _apply_candidates(tokens, candidates, incoming_stamp)
    material_touches = set(event_touches).union(candidate_touches)

    for identity_key, token in tokens.items():
        prior = previous_tokens.get(identity_key)
        if out_of_order and prior is not None and identity_key in material_touches:
            continue
        _derive_token(token, stamp)
        if (
            prior is not None
            and identity_key in material_touches
            and _material_state_signature(prior) != _material_state_signature(token)
            and int(token.get("state_version") or 0) == int(prior.get("state_version") or 0)
        ):
            token["state_version"] = int(token.get("state_version") or 0) + 1
            token["state_updated_at"] = stamp
            token.setdefault("state_history", []).append(
                {
                    "state": token.get("primary_state") or "new",
                    "state_version": token["state_version"],
                    "observed_at": stamp,
                    "transition_basis": "material_evidence",
                }
            )

    prior_rejections = (previous or {}).get("rejections", [])
    combined_rejections = _dedupe_records(
        [*prior_rejections, *rejections],
        ("reason", "raw_fingerprint", "source", "chain", "contract_address"),
    )
    alerts = _dedupe_records(
        [
            alert
            for alert in (previous or {}).get("alerts", [])
            if isinstance(alert, Mapping) and alert.get("key")
        ],
        ("key",),
    )
    alert_keys = {str(alert["key"]) for alert in alerts}
    for identity_key in sorted(tokens):
        token = tokens[identity_key]
        if out_of_order and identity_key in material_touches:
            continue
        policy = _alert_policy(token)
        if policy is None:
            continue
        prior = previous_tokens.get(identity_key)
        prior_policy = _alert_policy(prior) if prior is not None else None
        prior_version = int(prior.get("state_version") or 0) if prior is not None else 0
        current_version = int(token.get("state_version") or 0)
        if prior is not None and current_version == prior_version:
            continue
        previous_state = (
            str(prior_policy["state"])
            if prior_policy is not None
            else str(prior.get("primary_state") or "new")
            if prior is not None
            else "new"
        )
        alert = _build_alert(
            token,
            previous_state=previous_state,
            policy=policy,
            observed_at=stamp,
        )
        if alert["key"] not in alert_keys and _alert_allowed_by_cooldown(alert, alerts):
            alerts.append(alert)
            alert_keys.add(alert["key"])
    ordered_tokens = sorted(tokens.values(), key=lambda token: token["id"])
    return {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "observed_at": stamp,
        "monitor_status": _monitor_status(merged_source_health),
        "source_health": merged_source_health,
        "tokens": ordered_tokens,
        "rejections": combined_rejections,
        "alerts": alerts,
        "counts": _snapshot_counts(ordered_tokens, alerts),
        "kill_counts": _kill_counts(ordered_tokens, combined_rejections),
        "empty_state": _empty_state(ordered_tokens, combined_rejections, merged_source_health),
    }


@contextmanager
def _bounded_lock(path: Path, timeout_seconds: float = 5.0) -> Iterator[None]:
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                lock_age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if lock_age >= LOCK_STALE_SECONDS:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for monitor lock: {path}")
            time.sleep(0.01)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _recover_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = path.read_bytes()
    events: list[dict[str, Any]] = []
    valid_end = 0
    for line in data.splitlines(keepends=True):
        content = line.strip()
        if not content:
            valid_end += len(line)
            continue
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            break
        if not isinstance(value, dict):
            break
        events.append(value)
        valid_end += len(line)
    if valid_end != len(data):
        with path.open("r+b") as handle:
            handle.truncate(valid_end)
            handle.flush()
            os.fsync(handle.fileno())
    return _dedupe_records(events, ("event_id",))


def _append_jsonl(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    if not events:
        return
    needs_newline = path.exists() and path.stat().st_size > 0
    if needs_newline:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) not in (b"\n", b"\r")
    with path.open("ab") as handle:
        if needs_newline:
            handle.write(b"\n")
        for event in events:
            handle.write((_canonical_json(event) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def publish_monitor_v3(
    *,
    out_dir: Path,
    batch: Mapping[str, Any],
    source_health: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Merge and persist one V3 observation batch under a bounded process lock."""
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    ledger_path = output / "alpha-meme-events-v3.jsonl"
    state_path = output / "alpha-meme-monitor-v3-state.json"
    lock_path = output / "alpha-meme-monitor-v3.lock"

    with _bounded_lock(lock_path):
        ledger_events = _recover_jsonl(ledger_path)
        ledger_ids = {str(event.get("event_id") or "") for event in ledger_events}
        incoming = _dedupe_records(batch.get("events") or [], ("event_id",))
        appendable = [event for event in incoming if str(event.get("event_id") or "") not in ledger_ids]
        _append_jsonl(ledger_path, appendable)
        ledger_events.extend(appendable)

        previous = _load_state(state_path)
        projected_ids = {
            str(event.get("event_id") or "")
            for token in (previous or {}).get("tokens", [])
            for event in token.get("events") or []
        }
        recovered = [
            event for event in ledger_events if str(event.get("event_id") or "") not in projected_ids
        ]
        snapshot = update_monitor_state(
            previous,
            events=[*incoming, *recovered],
            candidates=batch.get("candidates") or [],
            rejections=batch.get("rejections") or [],
            source_health=source_health,
            observed_at=observed_at,
        )
        _atomic_json(state_path, snapshot)
        return snapshot
