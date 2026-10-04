"""Deterministic, contract-scoped evidence for token intelligence records.

This module has no network, filesystem, or runtime-worker dependencies. Facts
are admitted only when their input has a source, an observation time, and an
exact token identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from alpha_monitor_chains import normalize_monitor_chain
from alpha_token_evidence import (
    DEVELOPER_FIELDS as EVIDENCE_DEVELOPER_FIELDS,
    MARKET_FIELDS as EVIDENCE_MARKET_FIELDS,
    build_evidence_record,
    resolve_token_identity,
    token_identity_compatible,
)


CHAIN_ALIASES = {
    "1": "ethereum",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "56": "bsc",
    "bnb": "bsc",
    "bnb chain": "bsc",
    "bnb-chain": "bsc",
    "bnbchain": "bsc",
    "bsc": "bsc",
    "501": "solana",
    "ct_501": "solana",
    "ct 501": "solana",
    "sol": "solana",
    "solana": "solana",
    "8453": "base",
    "base": "base",
    "42161": "arbitrum",
    "arbitrum": "arbitrum",
    "4663": "robinhood",
    "robinhood-chain": "robinhood",
    "robinhood chain": "robinhood",
    "robinhood": "robinhood",
}

OBSERVED_FIELDS = (
    "quote_observed_at",
    "observed_at",
    "fetched_at",
    "updated_at",
    "generated_at",
    "holder_observed_at",
)
MARKET_FIELDS = {
    "price_usd": ("price_usd", "price", "priceUsd"),
    "market_cap_usd": ("market_cap", "mcap", "marketCap", "dex_market_cap"),
    "liquidity_usd": ("liquidity", "liquidity_usd", "dex_liquidity", "alpha_liquidity"),
    "volume_24h_usd": (
        "volume24h",
        "volume_24h",
        "volume_24h_usd",
        "volume",
        "dex_volume24h",
        "alpha_volume24h",
    ),
    "price_change_1h_pct": ("change_h1", "price_change_percent1h", "priceChange1h"),
    "pair_age_hours": ("pair_age_hours",),
    "transactions_24h": ("txns24h", "swaps"),
    "holder_count": ("holders", "holder_count", "holderCount"),
    "total_supply": ("total_supply", "totalSupply"),
    "first_transfer_block": ("first_transfer_block", "firstTransferBlock"),
    "last_transfer_block": ("last_transfer_block", "lastTransferBlock"),
    "transfer_count": ("transfer_count", "transferCount"),
    "transfer_count_24h": ("transfer_count_24h", "transferCount24h"),
    "activity_count": ("activity_count", "activityCount"),
    "top10_holder_pct": ("top10_holder_pct", "top_10_holder_rate"),
    "risk_score": ("risk_score",),
    "risk_level": ("risk_level",),
    "wash_score": ("wash_score", "bot_score"),
    "is_wash_trading": ("is_wash_trading",),
    "flow_acceleration": (
        "transaction_acceleration_ratio",
        "tx_acceleration_ratio",
        "transaction_acceleration",
        "tx_acceleration",
    ),
}
SMART_FIELDS = (
    "unique_wallet_count",
    "qualified_wallet_count",
    "qualified_wallets",
    "candidate_buy_wallet_count",
    "candidate_buy_wallets",
    "candidate_verified_buy_wallet_count",
    "candidate_buy_transaction_count",
    "buy_usd",
    "sell_usd",
    "net_flow_usd",
    "confirmation_status",
    "reason",
    "freshness",
)
MARKET_COUNT_FIELDS = {
    "transactions_24h",
    "holder_count",
    "total_supply",
    "first_transfer_block",
    "last_transfer_block",
    "transfer_count",
    "transfer_count_24h",
    "activity_count",
}
MARKET_NONNEGATIVE_FIELDS = {
    "price_usd",
    "market_cap_usd",
    "liquidity_usd",
    "volume_24h_usd",
    "pair_age_hours",
    *MARKET_COUNT_FIELDS,
}
MARKET_PERCENT_FIELDS = {"top10_holder_pct", "risk_score"}
SMART_COUNT_FIELDS = {
    "unique_wallet_count",
    "qualified_wallet_count",
    "candidate_buy_wallet_count",
    "candidate_verified_buy_wallet_count",
    "candidate_buy_transaction_count",
}
SMART_MONEY_FIELDS = {"buy_usd", "sell_usd", "net_flow_usd"}
SMART_LIST_FIELDS = {"qualified_wallets", "candidate_buy_wallets"}
SMART_TEXT_FIELDS = {"confirmation_status", "reason", "freshness"}


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _first(mapping: Mapping[str, Any], fields: Iterable[str]) -> Any:
    for field in fields:
        value = mapping.get(field)
        if value not in (None, ""):
            return value
    return None


def _normalize_chain(chain: Any) -> str:
    raw = _text(chain).lower()
    monitor_chain = normalize_monitor_chain(raw)
    if monitor_chain != raw:
        return monitor_chain
    spaced = raw.replace("_", " ").replace("-", " ")
    return normalize_monitor_chain(CHAIN_ALIASES.get(raw, CHAIN_ALIASES.get(spaced, raw)))


def normalize_identity(chain: Any, contract_address: Any) -> tuple[str, str]:
    """Return canonical ``(chain, contract)``; never fall back to symbol."""

    normalized_chain = _normalize_chain(chain)
    address = _text(contract_address)
    if not normalized_chain or not address:
        raise ValueError("chain and contract address are required")
    if address.lower().startswith("0x"):
        address = address.lower()
    return normalized_chain, address


def _identity_from_row(row: Mapping[str, Any]) -> tuple[str, str] | None:
    return resolve_token_identity(row)


def identity_from_row(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return one unambiguous scoped identity, rejecting cross-scope conflicts."""

    return _identity_from_row(row)


def _symbol(row: Mapping[str, Any]) -> str:
    profile = row.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    return _text(row.get("symbol") or row.get("ticker") or profile.get("symbol"))


def _name(row: Mapping[str, Any]) -> str:
    profile = row.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    return _text(row.get("name") or profile.get("name"))


def _identity_dict(identity: tuple[str, str]) -> dict[str, str]:
    chain, contract = identity
    return {"key": f"{chain}:{contract}", "chain": chain, "contract_address": contract}


def build_same_symbol_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Index symbol collisions without treating a symbol as token identity."""

    indexed: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = _symbol(row)
        identity = _identity_from_row(row)
        if not symbol or identity is None:
            continue
        context = {
            "symbol": symbol,
            "name": _name(row),
            "chain": identity[0],
            "contract_address": identity[1],
            "identity_key": f"{identity[0]}:{identity[1]}",
        }
        indexed.setdefault(symbol.upper(), {}).setdefault(identity, context)
    return {
        symbol: sorted(contracts.values(), key=lambda item: (item["chain"], item["contract_address"]))
        for symbol, contracts in sorted(indexed.items())
    }


def _strings(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    output = []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("source") or item.get("label") or item.get("name")
        text = _text(item)
        if text:
            output.append(text)
    return output


def _row_sources(row: Mapping[str, Any]) -> list[str]:
    sources: list[str] = []
    for field in ("source_labels", "sources", "quote_source", "source", "source_family", "holder_source", "risk_source"):
        sources.extend(_strings(row.get(field)))
    return sorted(set(sources), key=lambda value: (value.casefold(), value))


def _observed_at(row: Mapping[str, Any], fallback: str | None) -> str:
    return _text(_first(row, OBSERVED_FIELDS) or fallback)


def _field(row: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    return _first(row, aliases)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or isinstance(value, Mapping):
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.endswith("%"):
            value = value[:-1].strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _market_value(name: str, value: Any) -> Any:
    if name == "risk_level":
        return value.strip() if isinstance(value, str) and value.strip() else None
    if name == "is_wash_trading":
        return value if isinstance(value, bool) else None
    number = _number(value)
    if number is None:
        return None
    if name in MARKET_NONNEGATIVE_FIELDS and number < 0:
        return None
    if name in MARKET_PERCENT_FIELDS and not 0 <= number <= 100:
        return None
    if name in MARKET_COUNT_FIELDS:
        return int(number) if number.is_integer() else None
    return number


def _smart_value(name: str, value: Any) -> Any:
    if name in SMART_COUNT_FIELDS:
        number = _number(value)
        return int(number) if number is not None and number >= 0 and number.is_integer() else None
    if name in SMART_MONEY_FIELDS:
        number = _number(value)
        if number is None or (name != "net_flow_usd" and number < 0):
            return None
        return number
    if name in SMART_LIST_FIELDS:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return None
        items = [_text(item) for item in value if isinstance(item, str) and _text(item)]
        return items or None
    if name in SMART_TEXT_FIELDS:
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def _risk_flags(row: Mapping[str, Any]) -> list[str]:
    raw = row.get("risk_flags") or row.get("gmgn_risk_flags") or []
    if isinstance(raw, str):
        raw = raw.split(";")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return sorted({item.strip() for item in raw if isinstance(item, str) and item.strip()})


def _market_evidence(
    rows: Iterable[Mapping[str, Any]],
    identity: tuple[str, str],
    observed_at: str | None,
) -> dict[str, Any] | None:
    candidates = []
    for position, row in enumerate(rows):
        if _identity_from_row(row) != identity:
            continue
        sources = _row_sources(row)
        stamp = _observed_at(row, observed_at)
        values = {
            name: sanitized
            for name, aliases in MARKET_FIELDS.items()
            if (sanitized := _market_value(name, _field(row, aliases))) is not None
        }
        flags = _risk_flags(row)
        if flags:
            values["risk_flags"] = flags
        if not sources or not stamp or not values:
            continue
        evidence = {
            **values,
            "sources": sources,
            "observed_at": stamp,
            "scope": {"chain": identity[0], "contract_address": identity[1]},
        }
        candidates.append((len(values), -position, evidence))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _provenance_sources(evidence: Mapping[str, Any]) -> list[str]:
    sources = []
    for field in ("candidate_sources", "sources", "source"):
        sources.extend(_strings(evidence.get(field)))
    for event in evidence.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        sources.extend(_strings(event.get("provenance")))
    return sorted(set(sources), key=lambda value: (value.casefold(), value))


def _smart_wallet_evidence(
    rows: Iterable[Mapping[str, Any]],
    identity: tuple[str, str],
    observed_at: str | None,
) -> dict[str, Any] | None:
    candidates = []
    for position, row in enumerate(rows):
        if _identity_from_row(row) != identity:
            continue
        evidence = row.get("smart_money_evidence")
        if not isinstance(evidence, Mapping) or not evidence:
            continue
        sources = _provenance_sources(evidence)
        stamp = _text(
            evidence.get("latest_evidence_at")
            or evidence.get("as_of")
            or evidence.get("observed_at")
            or observed_at
        )
        values = {
            field: sanitized
            for field in SMART_FIELDS
            if (sanitized := _smart_value(field, evidence.get(field))) is not None
        }
        if not sources or not stamp or not values:
            continue
        condensed = {
            "status": _text(evidence.get("confirmation_status")) or "available",
            **values,
            "sources": sources,
            "observed_at": stamp,
            "scope": {"chain": identity[0], "contract_address": identity[1]},
        }
        candidates.append((len(values), -position, condensed))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def _official_identity(
    records: Iterable[Mapping[str, Any]],
    identity: tuple[str, str],
    symbol: str,
    observed_at: str | None,
) -> dict[str, Any]:
    contracts: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, Mapping):
            continue
        candidate_symbol = _symbol(row)
        if symbol and candidate_symbol and candidate_symbol.casefold() != symbol.casefold():
            continue
        candidate = _identity_from_row(row)
        sources = _row_sources(row)
        stamp = _observed_at(row, observed_at)
        if candidate is None or not sources or not stamp:
            continue
        item = {
            "chain": candidate[0],
            "contract_address": candidate[1],
            "identity_key": f"{candidate[0]}:{candidate[1]}",
            "sources": sources,
            "observed_at": stamp,
        }
        url = _text(row.get("url") or row.get("website") or row.get("source_url"))
        if url.startswith(("https://", "http://")):
            item["url"] = url
        contracts.setdefault(candidate, item)
    ordered = [contracts[key] for key in sorted(contracts)]
    status = "match" if identity in contracts else "mismatch" if ordered else "unknown"
    return {
        "status": status,
        "contract_scoped": True,
        "contracts": ordered,
        "matched_contract": contracts.get(identity),
    }


def _source_references(
    market: Mapping[str, Any] | None,
    smart: Mapping[str, Any] | None,
    official_identity: Mapping[str, Any],
    identity: tuple[str, str],
    row: Mapping[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    references = []
    market_url = _text(row.get("dex_url") or row.get("url"))
    for evidence in (market, smart):
        if not evidence:
            continue
        for source in evidence.get("sources") or []:
            ref = {
                "source": source,
                "observed_at": evidence["observed_at"],
                "scope": {"chain": identity[0], "contract_address": identity[1]},
            }
            if evidence is market and market_url.startswith(("https://", "http://")):
                ref["url"] = market_url
            references.append(ref)
    for contract in official_identity.get("contracts") or []:
        for source in contract.get("sources") or []:
            ref = {
                "source": source,
                "observed_at": contract["observed_at"],
                "scope": {"chain": contract["chain"], "contract_address": contract["contract_address"]},
            }
            if contract.get("url"):
                ref["url"] = contract["url"]
            references.append(ref)
    deduped = []
    seen = set()
    for ref in references:
        key = (
            ref["source"],
            ref["observed_at"],
            ref["scope"]["chain"],
            ref["scope"]["contract_address"],
            ref.get("url", ""),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(ref)
    return deduped[: max(0, int(limit))]


def _money(value: Any) -> str:
    amount = _number(value)
    if amount is None:
        return "未知"
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.2f}亿"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.2f}万"
    return f"{amount:.2f}"


def _fallback_summary(
    symbol: str,
    market: Mapping[str, Any] | None,
    smart: Mapping[str, Any] | None,
    official_identity: Mapping[str, Any],
) -> str:
    label = symbol or "该代币"
    if market is None and smart is None and official_identity["status"] == "unknown":
        return f"{label}：本地可归因证据暂不可用。"
    parts = [f"{label}："]
    if official_identity["status"] == "match":
        parts.append("官方合约一致。")
    elif official_identity["status"] == "mismatch":
        parts.append("官方来源合约与当前合约不一致，仅作同名上下文。")
    else:
        parts.append("官方合约身份待核实。")
    if market:
        metrics = []
        if market.get("market_cap_usd") is not None:
            metrics.append(f"市值约 {_money(market['market_cap_usd'])} 美元")
        if market.get("liquidity_usd") is not None:
            metrics.append(f"流动性约 {_money(market['liquidity_usd'])} 美元")
        if market.get("volume_24h_usd") is not None:
            metrics.append(f"24 小时成交约 {_money(market['volume_24h_usd'])} 美元")
        if metrics:
            parts.append("本地市场证据显示" + "，".join(metrics) + "。")
        flags = market.get("risk_flags") or []
        if flags:
            parts.append("已记录风险：" + "、".join(flags) + "。")
    if smart:
        qualified = _smart_value("qualified_wallet_count", smart.get("qualified_wallet_count")) or 0
        candidates = _smart_value("candidate_buy_wallet_count", smart.get("candidate_buy_wallet_count")) or 0
        if qualified:
            parts.append(f"有 {qualified} 个已核验盈利钱包进入证据集。")
        elif candidates:
            parts.append(f"观察到 {candidates} 个候选买入钱包，但尚未通过盈利历史核验。")
        else:
            parts.append("钱包证据已接入，但没有可归因的核验钱包。")
    return "".join(parts)


EVIDENCE_ARRAY_FIELDS = (
    "source_events",
    "market_snapshots",
    "wallet_events",
    "audit_facts",
    "developer_history",
    "social_evidence",
)
DETERMINISTIC_MARKET_FIELDS = tuple(field for field, _aliases in EVIDENCE_MARKET_FIELDS)
DETERMINISTIC_AUDIT_FIELDS = (
    "honeypot",
    "open_source",
    "ownership",
    "lp_burn_lock",
    "buy_tax",
    "sell_tax",
    "top10",
    "developer",
    "insider",
    "sniper",
    "bundler",
    "fresh_wallet",
    "mint",
    "freeze",
    "blacklist",
    "pause",
    "proxy",
    "wash",
    "deployer_history",
)
DETERMINISTIC_DEVELOPER_FIELDS = tuple(field for field, _aliases in EVIDENCE_DEVELOPER_FIELDS)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _materialize_provider_rows(provider_rows: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(provider_rows, Mapping):
        for feed, payload in provider_rows.items():
            value = (
                payload.get("data")
                if isinstance(payload, Mapping) and isinstance(payload.get("data"), list)
                else payload
            )
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, Mapping):
                    rows.append({"provider_feed": feed, **dict(item)})
        return rows
    if isinstance(provider_rows, Iterable) and not isinstance(provider_rows, (str, bytes)):
        rows.extend(dict(item) for item in provider_rows if isinstance(item, Mapping))
    return rows


def _identity_payload(row: Mapping[str, Any], identity: tuple[str, str]) -> dict[str, Any]:
    nested = row.get("identity") if isinstance(row.get("identity"), Mapping) else {}
    return {
        "key": f"{identity[0]}:{identity[1]}",
        "chain": identity[0],
        "contract_address": identity[1],
        "symbol": _first(nested, ("symbol",)) or _symbol(row) or None,
        "name": _first(nested, ("name",)) or _name(row) or None,
    }


def _validated_explicit_identities(
    row: Mapping[str, Any],
) -> tuple[bool, set[tuple[str, str]]]:
    identity = resolve_token_identity(row)
    return (identity is not None, {identity} if identity is not None else set())


def _canonical_scoped_row(
    row: Mapping[str, Any],
    identity: tuple[str, str],
) -> dict[str, Any] | None:
    valid, identities = _validated_explicit_identities(row)
    if not valid or identities != {identity}:
        return None
    canonical = copy.deepcopy(dict(row))
    canonical["chain"] = identity[0]
    canonical["contract_address"] = identity[1]
    if "identity_key" in canonical:
        canonical["identity_key"] = f"{identity[0]}:{identity[1]}"
    for field in ("identity", "scope", "extracted_identity"):
        scope = canonical.get(field)
        if isinstance(scope, Mapping) and _validated_explicit_identities(scope)[1]:
            canonical[field] = {
                **dict(scope),
                "chain": identity[0],
                "contract_address": identity[1],
            }
            if field == "identity" or "key" in scope:
                canonical[field]["key"] = f"{identity[0]}:{identity[1]}"
    return canonical


def _scoped_evidence_input(
    token_view: Mapping[str, Any],
    provider_rows: Any,
    identity: tuple[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    clean_view = dict(token_view)
    candidates: list[dict[str, Any]] = []
    for field in ("source_events", "events"):
        embedded = clean_view.pop(field, None)
        if isinstance(embedded, list):
            candidates.extend(dict(item) for item in embedded if isinstance(item, Mapping))
    embedded_audits = clean_view.pop("audit_facts", None)
    canonical_audits = []
    if isinstance(embedded_audits, list):
        canonical_audits = [
            canonical
            for item in embedded_audits
            if isinstance(item, Mapping)
            and (canonical := _canonical_scoped_row(item, identity)) is not None
        ]
    if canonical_audits:
        clean_view["audit_facts"] = canonical_audits
    if _first(token_view, ("provider_feed", "source_family", "source")):
        candidates.append(dict(token_view))
    candidates.extend(_materialize_provider_rows(provider_rows))
    scoped = [
        canonical
        for item in candidates
        if (canonical := _canonical_scoped_row(item, identity)) is not None
    ]
    return clean_view, scoped


def _normalize_empty_strings(value: Any) -> Any:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, Mapping):
        return {key: _normalize_empty_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_empty_strings(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_empty_strings(item) for item in value)
    return value


def _canonicalize_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    canonical = {"identity": _normalize_empty_strings(copy.deepcopy(evidence["identity"]))}
    seen: set[str] = set()
    identity_key = canonical["identity"]["key"]
    identity = normalize_identity(
        canonical["identity"]["chain"],
        canonical["identity"]["contract_address"],
    )
    for field in EVIDENCE_ARRAY_FIELDS:
        items = [
            _normalize_empty_strings(copy.deepcopy(item))
            for item in evidence.get(field, [])
            if isinstance(item, Mapping)
        ]
        items.sort(key=lambda item: str(item.get("evidence_id") or ""))
        for item in items:
            evidence_id = _text(item.get("evidence_id"))
            valid_identity, explicit = _validated_explicit_identities(item)
            if (
                not evidence_id
                or evidence_id in seen
                or item.get("identity_key") != identity_key
                or not valid_identity
                or explicit != {identity}
            ):
                raise ValueError("evidence IDs must resolve once inside the record identity")
            item["identity_key"] = identity_key
            if "chain" in item:
                item["chain"] = identity[0]
            if "contract_address" in item:
                item["contract_address"] = identity[1]
            if item.get("value") is None and item.get("status") == "known":
                item["status"] = "unknown"
            seen.add(evidence_id)
        canonical[field] = items
    return canonical


def _evidence_ids(evidence: Mapping[str, Any]) -> list[str]:
    return sorted(
        item["evidence_id"]
        for field in EVIDENCE_ARRAY_FIELDS
        for item in evidence.get(field, [])
        if isinstance(item, Mapping) and item.get("evidence_id")
    )


def hash_evidence(evidence: Mapping[str, Any]) -> str:
    """Hash an identity-scoped evidence set independently of input order."""

    material = {
        "identity": copy.deepcopy(evidence.get("identity")),
        **{
            field: sorted(
                (copy.deepcopy(item) for item in evidence.get(field, []) if isinstance(item, Mapping)),
                key=lambda item: _text(item.get("evidence_id")),
            )
            for field in EVIDENCE_ARRAY_FIELDS
        },
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _latest_field_values(
    items: Iterable[Mapping[str, Any]],
    field_name: str,
    fields: Iterable[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    values = {field: None for field in fields}
    statuses = {field: "unknown" for field in fields}
    latest: dict[str, Mapping[str, Any]] = {}
    for item in items:
        field = item.get(field_name)
        if field not in values:
            continue
        current = latest.get(field)
        item_key = (_text(item.get("observed_at")), _text(item.get("evidence_id")))
        current_key = (
            (_text(current.get("observed_at")), _text(current.get("evidence_id")))
            if current is not None
            else ("", "")
        )
        if current is None or item_key >= current_key:
            latest[field] = item
    for field, item in latest.items():
        status = _text(item.get("status")).lower() or "unknown"
        value = item.get("value")
        if isinstance(value, str) and not value.strip():
            value = None
            status = "unknown"
        statuses[field] = status
        values[field] = value if status == "known" else None
    return values, statuses


def deterministic_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Project evidence into deterministic facts while preserving unknowns as ``None``."""

    market, market_status = _latest_field_values(
        evidence.get("market_snapshots", []),
        "field",
        DETERMINISTIC_MARKET_FIELDS,
    )
    audit, audit_status = _latest_field_values(
        evidence.get("audit_facts", []),
        "audit_field",
        DETERMINISTIC_AUDIT_FIELDS,
    )
    developer, developer_status = _latest_field_values(
        evidence.get("developer_history", []),
        "field",
        DETERMINISTIC_DEVELOPER_FIELDS,
    )
    wallets = list(evidence.get("wallet_events", []))
    buy_usd = sum(
        float(item["amount_usd"])
        for item in wallets
        if item.get("direction") == "buy" and isinstance(item.get("amount_usd"), (int, float))
    )
    sell_usd = sum(
        float(item["amount_usd"])
        for item in wallets
        if item.get("direction") == "sell" and isinstance(item.get("amount_usd"), (int, float))
    )
    has_wallet_usd = any(isinstance(item.get("amount_usd"), (int, float)) for item in wallets)
    source_events = list(evidence.get("source_events", []))
    social = list(evidence.get("social_evidence", []))
    return {
        "market": market,
        "market_status": market_status,
        "audit": audit,
        "audit_status": audit_status,
        "developer": developer,
        "developer_status": developer_status,
        "wallet": {
            "event_count": len(wallets),
            "unique_wallet_count": len(
                {_text(item.get("wallet_address")) for item in wallets if _text(item.get("wallet_address"))}
            ),
            "buy_event_count": sum(item.get("direction") == "buy" for item in wallets),
            "sell_event_count": sum(item.get("direction") == "sell" for item in wallets),
            "buy_usd": buy_usd if has_wallet_usd else None,
            "sell_usd": sell_usd if has_wallet_usd else None,
            "net_flow_usd": buy_usd - sell_usd if has_wallet_usd else None,
        },
        "sources": {
            "event_count": len(source_events),
            "provider_families": sorted(
                {_text(item.get("provider_family")) for item in source_events if _text(item.get("provider_family"))}
            ),
            "signal_lanes": sorted(
                {_text(item.get("signal_lane")) for item in source_events if _text(item.get("signal_lane"))}
            ),
        },
        "social": {
            "evidence_count": len(social),
            "provider_families": sorted(
                {_text(item.get("provider_family")) for item in social if _text(item.get("provider_family"))}
            ),
        },
        "evidence_ids": _evidence_ids(evidence),
    }


def _legacy_market_from_v2(
    evidence: Mapping[str, Any],
    deterministic: Mapping[str, Any],
    identity: tuple[str, str],
) -> dict[str, Any] | None:
    values = {
        field: value
        for field, value in deterministic.get("market", {}).items()
        if value is not None
    }
    if not values:
        return None
    snapshots = list(evidence.get("market_snapshots", []))
    sources = sorted(
        {
            _text(item.get("provider_feed") or item.get("provider_family"))
            for item in snapshots
            if _text(item.get("provider_feed") or item.get("provider_family"))
        }
    )
    observed_at = max((_text(item.get("observed_at")) for item in snapshots), default="")
    return {
        **values,
        "sources": sources,
        "observed_at": observed_at or None,
        "scope": {"chain": identity[0], "contract_address": identity[1]},
    }


def _legacy_smart_from_v2(
    evidence: Mapping[str, Any],
    deterministic: Mapping[str, Any],
    identity: tuple[str, str],
) -> dict[str, Any] | None:
    wallets = list(evidence.get("wallet_events", []))
    if not wallets:
        return None
    summary = deterministic["wallet"]
    return {
        "status": "available",
        "verified_wallet_event_count": summary["event_count"],
        "unique_wallet_count": summary["unique_wallet_count"],
        "buy_usd": summary["buy_usd"],
        "sell_usd": summary["sell_usd"],
        "net_flow_usd": summary["net_flow_usd"],
        "sources": sorted(
            {
                _text(item.get("provider_feed") or item.get("provider_family"))
                for item in wallets
                if _text(item.get("provider_feed") or item.get("provider_family"))
            }
        ),
        "observed_at": max((_text(item.get("observed_at")) for item in wallets), default="") or None,
        "scope": {"chain": identity[0], "contract_address": identity[1]},
    }


def _legacy_references_from_v2(
    evidence: Mapping[str, Any],
    identity: tuple[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    references = []
    seen = set()
    for field in EVIDENCE_ARRAY_FIELDS:
        for item in evidence.get(field, []):
            source = _text(item.get("provider_feed") or item.get("provider_family"))
            observed_at = _text(item.get("observed_at")) or None
            url = _text(item.get("source_url")) or None
            if not source or not observed_at:
                continue
            key = (source, observed_at, url)
            if key in seen:
                continue
            seen.add(key)
            reference = {
                "source": source,
                "observed_at": observed_at,
                "scope": {"chain": identity[0], "contract_address": identity[1]},
            }
            if url:
                reference["url"] = url
            references.append(reference)
    return references[: max(0, int(limit))]


def _legacy_compatibility(
    row: Mapping[str, Any],
    context_rows: Iterable[Mapping[str, Any]],
    official_records: Iterable[Mapping[str, Any]],
    evidence: Mapping[str, Any],
    deterministic: Mapping[str, Any],
    identity: tuple[str, str],
    observed_at: str | None,
    max_source_references: int,
) -> dict[str, Any]:
    all_context = [row, *list(context_rows)]
    exact_rows = [item for item in all_context if _identity_from_row(item) == identity]
    symbol = _symbol(row)
    market = _market_evidence(exact_rows, identity, observed_at)
    if market is None:
        market = _legacy_market_from_v2(evidence, deterministic, identity)
    smart = _smart_wallet_evidence(exact_rows, identity, observed_at)
    if smart is None:
        smart = _legacy_smart_from_v2(evidence, deterministic, identity)
    official_state = _official_identity(official_records, identity, symbol, observed_at)
    same_symbol = build_same_symbol_index(all_context).get(symbol.upper(), []) if symbol else []
    identity_key = f"{identity[0]}:{identity[1]}"
    same_symbol = [
        {**item, "is_current": item["identity_key"] == identity_key}
        for item in same_symbol
    ]
    missing = []
    if market is None:
        missing.extend(("market", "holder_concentration", "risk_assessment"))
    else:
        if market.get("top10_holder_pct") is None:
            missing.append("holder_concentration")
        if market.get("risk_score") is None and not market.get("risk_level") and not market.get("risk_flags"):
            missing.append("risk_assessment")
    if smart is None:
        missing.append("smart_wallets")
    if official_state["status"] == "unknown":
        missing.append("official_identity")
    references = _source_references(
        market,
        smart,
        official_state,
        identity,
        row,
        max_source_references,
    )
    if not references:
        references = _legacy_references_from_v2(evidence, identity, max_source_references)
    has_evidence = bool(
        _evidence_ids(evidence)
        or market
        or smart
        or official_state["status"] != "unknown"
    )
    status = "unavailable" if not has_evidence else "ready" if not missing else "partial"
    generated_at = _text(observed_at) or max(
        (
            _text(market.get("observed_at")) if market else "",
            _text(smart.get("observed_at")) if smart else "",
        )
    )
    return {
        "status": status,
        "chain": identity[0],
        "contract_address": identity[1],
        "symbol": symbol or evidence["identity"].get("symbol"),
        "name": _name(row) or evidence["identity"].get("name"),
        "observed_at": generated_at or None,
        "market_evidence": market,
        "smart_wallet_evidence": smart,
        "official_identity": official_state,
        "same_symbol_contracts": same_symbol,
        "missing_evidence": missing,
        "source_references": references,
        "fallback_summary": _fallback_summary(symbol, market, smart, official_state),
    }


def _empty_evidence(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {"identity": copy.deepcopy(identity), **{field: [] for field in EVIDENCE_ARRAY_FIELDS}}


def _identity_from_key(identity_key: str | None) -> tuple[str, str] | None:
    if not identity_key or ":" not in identity_key:
        return None
    chain, contract = identity_key.split(":", 1)
    try:
        return normalize_identity(chain, contract)
    except ValueError:
        return None


def adapt_v1_record(
    record: Mapping[str, Any],
    identity_key: str | None = None,
) -> dict[str, Any]:
    """Return a v2 read view of a v1 record without mutating or rewriting it."""

    if not isinstance(record, Mapping) or record.get("schema_version", 1) != 1:
        raise ValueError("v1 token intelligence record required")
    normalized_record = _normalize_empty_strings(copy.deepcopy(dict(record)))
    record_identity = _identity_from_row(normalized_record)
    keyed_identity = _identity_from_key(identity_key)
    if identity_key is not None and keyed_identity is None:
        raise ValueError("v1 cache key conflicts with record identity")
    if record_identity is not None and keyed_identity is not None and record_identity != keyed_identity:
        raise ValueError("v1 cache key conflicts with record identity")
    if keyed_identity is not None:
        if not token_identity_compatible(normalized_record, keyed_identity):
            raise ValueError("v1 cache key conflicts with record identity")
    identity = record_identity or keyed_identity
    if identity is None:
        raise ValueError("token intelligence requires chain and contract address")
    identity_payload = _identity_payload(normalized_record, identity)
    evidence = _empty_evidence(identity_payload)
    deterministic = deterministic_summary(evidence)
    legacy_market = normalized_record.get("market_evidence")
    if isinstance(legacy_market, Mapping):
        for field in DETERMINISTIC_MARKET_FIELDS:
            if field in legacy_market:
                value = copy.deepcopy(legacy_market[field])
                deterministic["market"][field] = value
                deterministic["market_status"][field] = "known" if value is not None else "unknown"
    version = normalized_record.get("record_version")
    version = version if isinstance(version, int) and not isinstance(version, bool) and version > 0 else 1
    generated_at = _text(normalized_record.get("fast_generated_at") or normalized_record.get("observed_at") or normalized_record.get("generated_at"))
    reserved = {
        "schema_version",
        "identity",
        "record_version",
        "evidence_set_hash",
        "fast_generated_at",
        "deterministic",
        "ai",
        *EVIDENCE_ARRAY_FIELDS,
    }
    preserved_top_level = {
        key: copy.deepcopy(value)
        for key, value in normalized_record.items()
        if key not in reserved
    }
    legacy_unattributed = {
        key: copy.deepcopy(value)
        for key, value in normalized_record.items()
        if key != "schema_version"
    }
    return {
        **preserved_top_level,
        "schema_version": 2,
        **evidence,
        "chain": identity[0],
        "contract_address": identity[1],
        "symbol": identity_payload.get("symbol"),
        "name": identity_payload.get("name"),
        "record_version": version,
        "evidence_set_hash": hash_evidence(evidence),
        "fast_generated_at": generated_at or None,
        "deterministic": deterministic,
        "ai": {"status": "pending", "requested_at": None, "analyzed_at": None},
        "legacy_unattributed": legacy_unattributed,
    }


def read_token_intelligence_record(
    record: Mapping[str, Any],
    identity_key: str | None = None,
) -> dict[str, Any]:
    """Read v1 or v2 records through the v2 shape without mutating the source."""

    if not isinstance(record, Mapping):
        raise ValueError("token intelligence record must be a mapping")
    if record.get("schema_version") == 2:
        copied = copy.deepcopy(dict(record))
        keyed_identity = _identity_from_key(identity_key)
        if keyed_identity is not None and copied.get("identity", {}).get("key") != identity_key:
            raise ValueError("cache key conflicts with record identity")
        return copied
    if record.get("schema_version", 1) == 1:
        return adapt_v1_record(record, identity_key)
    raise ValueError("unsupported token intelligence schema version")


def read_token_intelligence_cache(cache: Mapping[str, Any]) -> dict[str, Any]:
    """Read a v1 or v2 cache envelope without modifying the persisted payload."""

    if not isinstance(cache, Mapping) or not isinstance(cache.get("records"), Mapping):
        raise ValueError("token intelligence cache must contain a records mapping")
    schema_version = cache.get("schema_version", 1)
    if schema_version == 2:
        return copy.deepcopy(dict(cache))
    if schema_version != 1:
        raise ValueError("unsupported token intelligence cache schema version")
    records = {
        str(identity_key): adapt_v1_record(record, str(identity_key))
        for identity_key, record in cache["records"].items()
        if isinstance(record, Mapping)
    }
    adapted = copy.deepcopy(dict(cache))
    adapted["schema_version"] = 2
    adapted["records"] = records
    return adapted


adapt_v1_cache = read_token_intelligence_cache


def next_record_version(previous: Mapping[str, Any] | None, identity_key: str | None = None) -> int:
    if not isinstance(previous, Mapping):
        return 1
    try:
        adapted = read_token_intelligence_record(previous, identity_key)
    except ValueError:
        return 1
    if identity_key and adapted.get("identity", {}).get("key") != identity_key:
        return 1
    version = adapted.get("record_version")
    return version + 1 if isinstance(version, int) and not isinstance(version, bool) and version > 0 else 1


def build_token_intelligence(
    row: Mapping[str, Any],
    provider_rows: Any = (),
    *,
    previous: Mapping[str, Any] | None = None,
    context_rows: Iterable[Mapping[str, Any]] = (),
    smart_money_rows: Iterable[Mapping[str, Any]] = (),
    official_records: Iterable[Mapping[str, Any]] = (),
    observed_at: str | None = None,
    max_source_references: int = 8,
) -> dict[str, Any]:
    """Build one schema-v2 intelligence record for the row's exact identity."""

    identity = _identity_from_row(row)
    if identity is None:
        raise ValueError("token intelligence requires chain and contract address")
    materialized_providers = _materialize_provider_rows(provider_rows)
    materialized_context = _materialize_provider_rows(context_rows)
    materialized_smart = _materialize_provider_rows(smart_money_rows)
    materialized_official = _materialize_provider_rows(official_records)
    combined_rows = [
        *materialized_providers,
        *materialized_context,
        *materialized_smart,
        *materialized_official,
    ]
    clean_view, scoped_rows = _scoped_evidence_input(row, combined_rows, identity)
    clean_view["identity"] = _identity_payload(row, identity)
    evidence = _canonicalize_evidence(
        build_evidence_record(clean_view, scoped_rows, _text(observed_at))
    )
    identity_key = evidence["identity"]["key"]
    deterministic = deterministic_summary(evidence)
    compatibility = _legacy_compatibility(
        row,
        [*materialized_providers, *materialized_context, *materialized_smart],
        materialized_official,
        evidence,
        deterministic,
        identity,
        observed_at,
        max_source_references,
    )
    return {
        **compatibility,
        "schema_version": 2,
        **evidence,
        "record_version": next_record_version(previous, identity_key),
        "evidence_set_hash": hash_evidence(evidence),
        "fast_generated_at": _text(observed_at) or None,
        "deterministic": deterministic,
        "ai": {"status": "pending", "requested_at": None, "analyzed_at": None},
    }


def build_token_intelligence_records(
    rows: Iterable[Mapping[str, Any]],
    *,
    provider_rows: Any = None,
    previous_records: Mapping[str, Mapping[str, Any]] | None = None,
    smart_money_rows: Iterable[Mapping[str, Any]] = (),
    official_records: Iterable[Mapping[str, Any]] = (),
    observed_at: str | None = None,
    max_source_references: int = 8,
) -> list[dict[str, Any]]:
    """Build one v2 record per exact identity, preserving first-seen order."""

    materialized = [dict(row) for row in rows if isinstance(row, Mapping)]
    providers = materialized if provider_rows is None else _materialize_provider_rows(provider_rows)
    materialized_smart = _materialize_provider_rows(smart_money_rows)
    materialized_official = _materialize_provider_rows(official_records)
    unique_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in materialized:
        identity = _identity_from_row(row)
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        unique_rows.append(row)
    output = []
    for row in unique_rows:
        identity = _identity_from_row(row)
        if identity is None:
            continue
        identity_key = f"{identity[0]}:{identity[1]}"
        previous = previous_records.get(identity_key) if isinstance(previous_records, Mapping) else None
        output.append(
            build_token_intelligence(
                row,
                [*providers, *materialized_smart, *materialized_official],
                previous=previous,
                observed_at=observed_at,
                max_source_references=max_source_references,
            )
        )
    return output
