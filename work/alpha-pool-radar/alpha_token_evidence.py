#!/usr/bin/env python3
"""Field-level provider evidence normalization for token intelligence."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from alpha_monitor_chains import monitor_chain_names, normalize_monitor_chain
from alpha_monitor_v3 import normalize_audit_facts, source_descriptor


MARKET_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("price_usd", ("price_usd", "priceUsd", "price")),
    ("market_cap_usd", ("market_cap_usd", "marketCap", "market_cap", "mcap")),
    ("liquidity_usd", ("liquidity_usd", "liquidityUsd", "liquidity")),
    ("volume_1m_usd", ("volume_1m_usd", "volume1mUsd", "volume_1m")),
    ("volume_5m_usd", ("volume_5m_usd", "volume5mUsd", "volume_5m")),
    ("volume_24h_usd", ("volume_24h_usd", "volume24hUsd", "volume24h", "volume")),
    ("buys_1m", ("buys_1m", "buys1m")),
    ("sells_1m", ("sells_1m", "sells1m")),
    ("buys_5m", ("buys_5m", "buys5m")),
    ("sells_5m", ("sells_5m", "sells5m")),
    ("unique_buyers_1m", ("unique_buyers_1m", "uniqueBuyers1m")),
    ("unique_buyers_5m", ("unique_buyers_5m", "uniqueBuyers5m")),
    ("holder_count", ("holder_count", "holderCount", "holders")),
    ("total_supply", ("total_supply", "totalSupply")),
    ("first_transfer_block", ("first_transfer_block", "firstTransferBlock")),
    ("last_transfer_block", ("last_transfer_block", "lastTransferBlock")),
    ("transfer_count", ("transfer_count", "transferCount")),
    ("transfer_count_24h", ("transfer_count_24h", "transferCount24h")),
    ("activity_count", ("activity_count", "activityCount")),
)
DEVELOPER_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("history", ("developer_history", "deployer_history")),
    ("launch_count", ("developer_launch_count", "deployer_launch_count", "launch_count")),
    ("rug_count", ("developer_rug_count", "deployer_rug_count", "rug_count")),
    ("migration_count", ("developer_migration_count", "deployer_migration_count", "migration_count")),
    ("best_prior_market_cap_usd", ("best_prior_market_cap_usd", "best_prior_market_cap")),
    ("creator_address", ("creator_address", "creator", "deployer_address")),
)
CHAIN_ALIASES = {
    "1": "ethereum",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "56": "bsc",
    "bnb": "bsc",
    "bnb chain": "bsc",
    "bnbchain": "bsc",
    "bsc": "bsc",
    "501": "solana",
    "ct 501": "solana",
    "8453": "base",
    "base": "base",
    "42161": "arbitrum",
    "arbitrum": "arbitrum",
    "4663": "robinhood",
    "robinhood": "robinhood",
    "robinhood chain": "robinhood",
    "sol": "solana",
    "solana": "solana",
}
TOKEN_CHAIN_FIELDS = ("chain", "chain_id", "chainId", "chainIndex", "blockchain")
TOKEN_NETWORK_FIELDS = ("network", "networkId")
TOKEN_CONTRACT_FIELDS = (
    "contract_address",
    "token_address",
    "tokenAddress",
    "contractAddress",
    "base_address",
    "baseAddress",
    "mint",
    "contract",
    "ca",
)
TOKEN_IDENTITY_WRAPPERS = {
    "identity",
    "profile",
    "scope",
    "extracted_identity",
    "wind_extracted_identity",
    "token_identity",
    "tokenIdentity",
    "token",
    "base_token",
    "baseToken",
    "contract_details",
    "contractDetails",
    "ca_info",
}
TOKEN_IDENTITY_COLLECTIONS = {
    "extracted_identities",
    "contracts",
    "extracted_contracts",
    "wind_extracted_contracts",
    "wind_extracted_contract_details",
}
NON_TOKEN_IDENTITY_CONTEXTS = {
    "author",
    "user",
    "account",
    "wallet",
    "maker",
    "trader",
    "transaction",
    "transaction_metadata",
    "tx",
    "tx_metadata",
    "transfer",
    "receipt",
    "block",
    "log",
    "logs",
    "official_identity",
    "official_contract",
    "same_symbol_contracts",
    "source_references",
    "legacy_unattributed",
    "audit",
    "audit_metadata",
    "auditMetadata",
    "audit_info",
    "auditInfo",
    "security_audit",
    "securityAudit",
    "audit_facts",
    "source_events",
    "events",
    "market_snapshots",
    "wallet_events",
    "developer_history",
    "social_evidence",
}
EVM_ADDRESS = re.compile(r"^0[xX][0-9a-fA-F]{40}$")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def evidence_id(
    identity_key: str,
    family: str,
    feed: str,
    event_id: str,
    fact_path: str,
    observed_at: str,
    fingerprint: str,
) -> str:
    """Return a stable ID for one provider fact and its exact provenance."""
    material = [
        str(identity_key or ""),
        str(family or "").strip().lower(),
        str(feed or "").strip().lower(),
        str(event_id or ""),
        str(fact_path or ""),
        str(observed_at or ""),
        str(fingerprint or ""),
    ]
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _normalize_chain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    monitor_chain = normalize_monitor_chain(raw)
    if monitor_chain != raw:
        return monitor_chain
    spaced = raw.replace("_", " ").replace("-", " ")
    return normalize_monitor_chain(CHAIN_ALIASES.get(spaced, spaced.replace(" ", "-")))


def _normalize_address(value: Any) -> str:
    address = str(value or "").strip()
    return address.lower() if EVM_ADDRESS.fullmatch(address) else address


def _present(row: Mapping[str, Any], aliases: tuple[str, ...]) -> tuple[bool, Any]:
    for alias in aliases:
        if alias in row:
            return True, row[alias]
    return False, None


def _first(row: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        value = row.get(alias)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _fact_value(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


def _identity(token_view: Mapping[str, Any]) -> dict[str, Any]:
    resolved = resolve_token_identity(token_view)
    if resolved is None:
        raise ValueError("token_view requires chain and contract_address")
    chain, contract = resolved
    nested = token_view.get("identity") if isinstance(token_view.get("identity"), Mapping) else {}
    return {
        "key": f"{chain}:{contract}",
        "chain": chain,
        "contract_address": contract,
        "symbol": _first(nested, ("symbol",)) or token_view.get("symbol"),
        "name": _first(nested, ("name",)) or token_view.get("name"),
    }


def _iter_rows(provider_rows: Any) -> Iterable[dict[str, Any]]:
    if isinstance(provider_rows, Mapping):
        for feed, payload in provider_rows.items():
            value = payload.get("data") if isinstance(payload, Mapping) and isinstance(payload.get("data"), list) else payload
            rows = value if isinstance(value, list) else [value]
            for row in rows:
                if isinstance(row, Mapping):
                    yield {"provider_feed": feed, **dict(row)}
        return
    if isinstance(provider_rows, Iterable) and not isinstance(provider_rows, (str, bytes)):
        for row in provider_rows:
            if isinstance(row, Mapping):
                yield dict(row)


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    chain = _normalize_chain(_first(row, ("chain", "chainId", "chain_id", "network")))
    contract = _normalize_address(
        _first(
            row,
            ("contract_address", "contractAddress", "token_address", "tokenAddress", "address", "mint"),
        )
    )
    return chain, contract


def _feed(row: Mapping[str, Any]) -> str:
    return str(_first(row, ("provider_feed", "source_family", "source")) or "").strip().lower()


def _provider_event_id(row: Mapping[str, Any], fingerprint: str) -> str:
    value = _first(
        row,
        (
            "provider_event_id",
            "wind_event_id",
            "monitor985_event_key",
            "event_id",
            "id",
            "key",
            "tx_hash",
            "monitor985_tx_hash",
        ),
    )
    return str(value) if value not in (None, "") else f"fallback:{fingerprint}"


def _status(row: Mapping[str, Any], value: Any = ...) -> str:
    supplied = str(
        _first(row, ("provenance_status", "evidence_status", "status")) or ""
    ).strip().lower()
    if row.get("stale") is True or row.get("is_stale") is True or supplied == "stale":
        return "stale"
    if supplied == "conflicting":
        return "conflicting"
    if value is None or (isinstance(value, str) and not value.strip()):
        return "unknown"
    if supplied in {"known", "unknown"}:
        return supplied
    return "known"


def _base(
    row: Mapping[str, Any],
    *,
    identity_key: str,
    family: str,
    feed: str,
    provider_event_id: str,
    fact_path: str,
    observed_at: str,
    fingerprint: str,
    value: Any = ...,
) -> dict[str, Any]:
    row_observed_at = _first(row, ("observed_at", "fetched_at")) or observed_at
    event_at = _first(
        row,
        (
            "event_at",
            "monitor985_created_at",
            "created_at",
            "createdAt",
            "updated_at",
            "timestamp",
            "time",
        ),
    )
    source_url = _first(
        row,
        ("source_url", "original_url", "wind_original_url", "wind_url", "url", "tx_url", "source_origin"),
    )
    return {
        "evidence_id": evidence_id(
            identity_key,
            family,
            feed,
            provider_event_id,
            fact_path,
            str(row_observed_at or ""),
            fingerprint,
        ),
        "identity_key": identity_key,
        "provider_family": family,
        "provider_feed": feed,
        "provider_event_id": provider_event_id,
        "fact_path": fact_path,
        "event_at": event_at,
        "observed_at": row_observed_at,
        "source_url": str(source_url) if source_url not in (None, "") else None,
        "raw_fingerprint": fingerprint,
        "status": _status(row, value),
    }


def _append_unique(target: list[dict[str, Any]], item: dict[str, Any]) -> None:
    item_id = item["evidence_id"]
    if not any(existing["evidence_id"] == item_id for existing in target):
        target.append(item)


def _normalize_extracted_contracts(row: Mapping[str, Any]) -> list[str]:
    value = _first(row, ("extracted_contracts", "wind_extracted_contracts", "extracted_cas"))
    values = value if isinstance(value, list) else [value] if value not in (None, "") else []
    contracts: list[str] = []
    for item in values:
        address = _first(item, ("address", "contract_address", "ca")) if isinstance(item, Mapping) else item
        normalized = _normalize_address(address)
        if normalized and normalized not in contracts:
            contracts.append(normalized)
    return contracts


def _normalize_extracted_identities(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = _first(
        row,
        ("extracted_identities", "wind_extracted_contract_details"),
    )
    values = value if isinstance(value, list) else [value] if isinstance(value, Mapping) else []
    identities: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        chain = _normalize_chain(_first(item, ("chain", "chain_id", "chainId"))) or None
        contract = _normalize_address(_first(item, ("contract_address", "address", "ca")))
        identity = {"chain": chain, "contract_address": contract}
        if contract and identity not in identities:
            identities.append(identity)
    return identities


def _social_author(row: Mapping[str, Any]) -> dict[str, Any] | None:
    existing = _first(row, ("author", "wind_author"))
    if isinstance(existing, Mapping):
        return dict(existing)
    author = {
        "id": _first(row, ("author_id", "user_id")),
        "handle": _first(row, ("author_handle", "wind_handle", "handle", "username")),
        "name": _first(row, ("author_name", "display_name")),
        "followers": _first(row, ("author_followers", "followers")),
        "verified": _first(row, ("author_verified", "verified")),
        "platform": _first(row, ("platform", "wind_platform")),
    }
    return author if any(value is not None for value in author.values()) else None


def _existing_token_rows(token_view: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("source_events", "events"):
        value = token_view.get(key)
        if isinstance(value, list):
            rows.extend(dict(item) for item in value if isinstance(item, Mapping))
    return rows


def _identity_from_key(value: Any) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if ":" not in raw:
        return None
    chain, contract = raw.split(":", 1)
    normalized = (_normalize_chain(chain), _normalize_address(contract))
    return normalized if all(normalized) else None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _identity_mapping_claims(
    scope: Mapping[str, Any],
    *,
    named_scope: bool,
    allow_short_key: bool,
) -> tuple[bool, list[tuple[str | None, str | None]]]:
    claims: list[tuple[str | None, str | None]] = []
    strong_chains = [scope[field] for field in TOKEN_CHAIN_FIELDS if field in scope]
    network_chains = [scope[field] for field in TOKEN_NETWORK_FIELDS if field in scope]
    strong_contracts = [
        scope[field]
        for field in TOKEN_CONTRACT_FIELDS
        if field in scope
        and not (field == "contract" and isinstance(scope[field], Mapping))
    ]
    address_contracts = [scope["address"]] if "address" in scope else []

    chain_values = (
        strong_chains
        if strong_chains
        else network_chains if named_scope or strong_contracts else []
    )
    contract_values = (
        strong_contracts
        if strong_contracts
        else address_contracts if named_scope or strong_chains else []
    )
    if any(_blank(value) for value in [*chain_values, *contract_values]):
        return False, claims
    if named_scope and chain_values and not contract_values:
        return False, claims

    normalized_chains = {_normalize_chain(value) for value in chain_values}
    normalized_contracts = {_normalize_address(value) for value in contract_values}
    if "" in normalized_chains or "" in normalized_contracts:
        return False, claims
    if len(normalized_chains) > 1 or len(normalized_contracts) > 1:
        return False, claims
    chain = next(iter(normalized_chains), None)
    contract = next(iter(normalized_contracts), None)
    if chain is not None or contract is not None:
        claims.append((chain, contract))

    key_fields = ["identity_key"]
    if allow_short_key:
        key_fields.append("key")
    for field in key_fields:
        if field not in scope:
            continue
        keyed = _identity_from_key(scope[field])
        if keyed is None:
            return False, claims
        claims.append(keyed)
    return True, claims


def _known_chain(value: Any) -> str | None:
    normalized = _normalize_chain(value)
    known_chains = set(CHAIN_ALIASES.values()) | set(monitor_chain_names())
    return normalized if normalized in known_chains else None


def _wrapped_identity_claims(
    field: str,
    value: Any,
) -> tuple[bool, list[tuple[str | None, str | None]]]:
    if _blank(value) or value == [] or value == {}:
        return True, []
    if field == "contract" or field == "extracted_identity":
        if isinstance(value, Mapping):
            return _identity_mapping_claims(
                value,
                named_scope=True,
                allow_short_key=field == "extracted_identity",
            )
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            return False, []
        keyed = _identity_from_key(value)
        if keyed is not None:
            return True, [keyed]
        contract = _normalize_address(value)
        return (True, [(None, contract)]) if contract else (False, [])

    if isinstance(value, Mapping):
        has_identity_fields = any(
            key in value
            for key in (
                *TOKEN_CHAIN_FIELDS,
                *TOKEN_NETWORK_FIELDS,
                *TOKEN_CONTRACT_FIELDS,
                "address",
                "identity_key",
                "key",
            )
        )
        if has_identity_fields:
            return _identity_mapping_claims(
                value,
                named_scope=True,
                allow_short_key=field == "identity",
            )
        if field not in TOKEN_IDENTITY_COLLECTIONS:
            return True, []
        claims: list[tuple[str | None, str | None]] = []
        for chain_value, contract_value in value.items():
            chain = _known_chain(chain_value)
            contract = _normalize_address(contract_value)
            if chain is None or not contract:
                return False, claims
            claims.append((chain, contract))
        return True, claims

    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        claims: list[tuple[str | None, str | None]] = []
        for item in value:
            if isinstance(item, Mapping):
                valid, nested = _identity_mapping_claims(
                    item,
                    named_scope=True,
                    allow_short_key=field == "extracted_identities",
                )
            else:
                valid, nested = _wrapped_identity_claims("contract", item)
            if not valid:
                return False, claims
            claims.extend(nested)
        return True, claims

    return _wrapped_identity_claims("contract", value)


def _nested_token_identity_claims(
    value: Any,
    *,
    inspect_current: bool = True,
) -> tuple[bool, list[tuple[str | None, str | None]]]:
    claims: list[tuple[str | None, str | None]] = []
    if isinstance(value, Mapping):
        if inspect_current and any(field in value for field in TOKEN_CONTRACT_FIELDS):
            valid, direct = _identity_mapping_claims(
                value,
                named_scope=True,
                allow_short_key=False,
            )
            if not valid:
                return False, claims
            claims.extend(direct)
        for field, child in value.items():
            if field in NON_TOKEN_IDENTITY_CONTEXTS:
                continue
            if field == "identity_key":
                keyed = _identity_from_key(child)
                if keyed is None:
                    return False, claims
                claims.append(keyed)
            elif field == "contract" or field in TOKEN_IDENTITY_WRAPPERS or field in TOKEN_IDENTITY_COLLECTIONS:
                valid, nested = _wrapped_identity_claims(field, child)
                if not valid:
                    return False, claims
                claims.extend(nested)
            if isinstance(child, (Mapping, list, tuple)):
                valid, nested = _nested_token_identity_claims(child)
                if not valid:
                    return False, claims
                claims.extend(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            valid, nested = _nested_token_identity_claims(item)
            if not valid:
                return False, claims
            claims.extend(nested)
    return True, claims


def _token_identity_claims(
    row: Mapping[str, Any],
) -> tuple[bool, list[tuple[str | None, str | None]]]:
    valid, claims = _identity_mapping_claims(
        row,
        named_scope=False,
        allow_short_key=False,
    )
    if not valid:
        return False, claims
    nested_valid, nested = _nested_token_identity_claims(row, inspect_current=False)
    return nested_valid, [*claims, *nested]


def resolve_token_identity(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """Resolve one unambiguous token identity from explicit token schemas."""

    valid, claims = _token_identity_claims(row)
    if not valid:
        return None
    chains = {chain for chain, _ in claims if chain}
    contracts = {contract for _, contract in claims if contract}
    if len(chains) != 1 or len(contracts) != 1:
        return None
    return next(iter(chains)), next(iter(contracts))


def token_identity_compatible(
    row: Mapping[str, Any],
    expected: tuple[str, str],
) -> bool:
    """Return whether every explicit token claim is compatible with expected."""

    valid, claims = _token_identity_claims(row)
    return valid and all(
        (chain is None or chain == expected[0])
        and (contract is None or contract == expected[1])
        for chain, contract in claims
    )


def _validated_explicit_identities(
    row: Mapping[str, Any],
) -> tuple[bool, set[tuple[str, str]]]:
    valid, claims = _token_identity_claims(row)
    if not valid:
        return False, set()
    chains = {chain for chain, _ in claims if chain}
    contracts = {contract for _, contract in claims if contract}
    if len(chains) > 1 or len(contracts) > 1:
        return False, set()
    if len(chains) == 1 and len(contracts) == 1:
        return True, {(next(iter(chains)), next(iter(contracts)))}
    return True, set()


def _scoped_existing_audit(
    raw: Mapping[str, Any],
    expected: tuple[str, str],
) -> dict[str, Any] | None:
    valid, identities = _validated_explicit_identities(raw)
    if not valid or identities != {expected}:
        return None
    scoped = dict(raw)
    scoped["identity_key"] = f"{expected[0]}:{expected[1]}"
    scoped["chain"] = expected[0]
    scoped["contract_address"] = expected[1]
    return scoped


def _append_existing_audit_facts(
    record: dict[str, Any],
    token_view: Mapping[str, Any],
    observed_at: str,
) -> None:
    facts = token_view.get("audit_facts")
    if not isinstance(facts, list):
        return
    identity_key = record["identity"]["key"]
    expected_identity = (
        record["identity"]["chain"],
        record["identity"]["contract_address"],
    )
    for raw in facts:
        if not isinstance(raw, Mapping) or not raw.get("audit_field"):
            continue
        scoped = _scoped_existing_audit(raw, expected_identity)
        if scoped is None:
            continue
        family = str(scoped.get("provider_family") or "").strip().lower()
        feed = str(scoped.get("provider_feed") or f"{family}_audit").strip().lower()
        if not family or not feed:
            continue
        fingerprint = str(
            scoped.get("raw_fingerprint")
            or scoped.get("evidence_id")
            or _fingerprint(scoped)
        )
        provider_event_id = _provider_event_id(scoped, fingerprint)
        value = _fact_value(scoped.get("value"))
        item = {
            **scoped,
            **_base(
                scoped,
                identity_key=identity_key,
                family=family,
                feed=feed,
                provider_event_id=provider_event_id,
                fact_path=f"audit.{scoped['audit_field']}",
                observed_at=observed_at,
                fingerprint=fingerprint,
                value=value,
            ),
            "audit_field": scoped["audit_field"],
            "value": value,
            "upstream_provider": scoped.get("upstream_provider") or family,
            "confidence": scoped.get("confidence") or "provider_reported",
        }
        _append_unique(record["audit_facts"], item)


def build_evidence_record(
    token_view: Mapping[str, Any],
    provider_rows: Any,
    observed_at: str,
) -> dict[str, Any]:
    """Build identity-scoped evidence arrays from provider and token-view rows."""
    identity = _identity(token_view)
    identity_key = identity["key"]
    record: dict[str, Any] = {
        "identity": identity,
        "source_events": [],
        "market_snapshots": [],
        "wallet_events": [],
        "audit_facts": [],
        "developer_history": [],
        "social_evidence": [],
    }

    rows = [*_existing_token_rows(token_view), *_iter_rows(provider_rows)]
    for row in rows:
        valid_identity, identities = _validated_explicit_identities(row)
        if not valid_identity or identities != {(identity["chain"], identity["contract_address"])}:
            continue
        feed = _feed(row)
        if not feed:
            continue
        try:
            descriptor = source_descriptor(feed, row)
        except (TypeError, ValueError):
            continue
        family = descriptor.provider_family
        fingerprint = str(row.get("raw_fingerprint") or _fingerprint(row))
        provider_event_id = _provider_event_id(row, fingerprint)
        base_args = {
            "identity_key": identity_key,
            "family": family,
            "feed": descriptor.provider_feed,
            "provider_event_id": provider_event_id,
            "observed_at": observed_at,
            "fingerprint": fingerprint,
        }

        source_event = {
            **_base(row, fact_path="source_event", **base_args),
            "event_id": row.get("event_id"),
            "event_type": row.get("event_type") or row.get("type") or descriptor.signal_lane,
            "evidence_role": descriptor.evidence_role,
            "signal_lane": descriptor.signal_lane,
            "counts_for_resonance": bool(
                row.get("counts_for_resonance")
                if "counts_for_resonance" in row
                else descriptor.evidence_role in {"discovery", "ranking", "wallet", "market"}
                and row.get("lookup_by_ca") is not True
            ),
            "upstream_provider": row.get("upstream_provider") or family,
        }
        _append_unique(record["source_events"], source_event)

        for field, aliases in MARKET_FIELDS:
            present, value = _present(row, aliases)
            if not present:
                continue
            value = _fact_value(value)
            item = {
                **_base(row, fact_path=f"market.{field}", value=value, **base_args),
                "field": field,
                "value": value,
            }
            _append_unique(record["market_snapshots"], item)

        wallet_address = _first(
            row,
            ("wallet_address", "monitor985_wallet", "wallet", "maker_address", "trader_address"),
        )
        direction_raw = _first(row, ("direction", "side", "monitor985_trade_side"))
        direction_text = str(direction_raw or "").strip().lower()
        direction = "buy" if "buy" in direction_text else "sell" if "sell" in direction_text else None
        event_at = _base(row, fact_path="wallet.event", **base_args)["event_at"]
        if wallet_address not in (None, "") and direction and event_at:
            amount = _first(row, ("amount", "token_amount", "quantity"))
            amount_usd = _first(
                row,
                ("amount_usd", "monitor985_trade_amount_usd", "netAmountUsd", "usd"),
            )
            tx_hash = _first(
                row,
                ("tx_hash", "monitor985_tx_hash", "transaction_hash", "txHash", "signature"),
            )
            wallet = {
                **_base(row, fact_path="wallet.event", **base_args),
                "wallet_address": str(wallet_address),
                "wallet_label": _first(
                    row,
                    ("wallet_label", "monitor985_wallet_name", "wallet_name", "label"),
                ),
                "direction": direction,
                "amount": amount,
                "amount_usd": amount_usd,
                "tx_hash": str(tx_hash) if tx_hash not in (None, "") else None,
                "verified_address_event": True,
            }
            _append_unique(record["wallet_events"], wallet)

        audit_rows = normalize_audit_facts(
            row,
            provider_family=family,
            observed_at=str(_first(row, ("observed_at", "fetched_at")) or observed_at),
            identity_key=identity_key,
        )
        for audit in audit_rows:
            fact_path = f"audit.{audit['audit_field']}"
            value = _fact_value(audit.get("value"))
            item = {
                **audit,
                **_base(row, fact_path=fact_path, value=value, **base_args),
                "value": value,
                "upstream_provider": audit["upstream_provider"],
                "confidence": audit["confidence"],
            }
            if audit["status"] in {"unknown", "stale", "conflicting"}:
                item["status"] = audit["status"]
            _append_unique(record["audit_facts"], item)

        for field, aliases in DEVELOPER_FIELDS:
            present, value = _present(row, aliases)
            if not present:
                continue
            value = _fact_value(value)
            item = {
                **_base(row, fact_path=f"developer.{field}", value=value, **base_args),
                "field": field,
                "value": value,
            }
            _append_unique(record["developer_history"], item)

        text_present, text_value = _present(
            row,
            ("post_text", "wind_post_text", "content_text", "text", "thesis", "monitor985_thesis"),
        )
        text_value = str(text_value).strip() if text_value not in (None, "") else None
        original_present, original_text = _present(
            row,
            ("original_post_text", "wind_original_post_text"),
        )
        referenced_present, referenced_text = _present(
            row,
            ("referenced_post_text", "wind_referenced_post_text"),
        )
        original_text = str(original_text).strip() if original_text not in (None, "") else None
        referenced_text = str(referenced_text).strip() if referenced_text not in (None, "") else None
        if not original_present and not referenced_present:
            original_text = text_value
        extracted_contracts = _normalize_extracted_contracts(row)
        extracted_identities = _normalize_extracted_identities(row)
        if not extracted_identities and identity["contract_address"] in extracted_contracts:
            extracted_identities = [
                {
                    "chain": identity["chain"],
                    "contract_address": identity["contract_address"],
                }
            ]
        is_social = descriptor.signal_lane == "kol_social" or family == "wind"
        if is_social and (text_present or extracted_contracts or family == "wind"):
            exact_extracted_identity = {
                "chain": identity["chain"],
                "contract_address": identity["contract_address"],
            }
            if extracted_identities and exact_extracted_identity not in extracted_identities:
                continue
            original_url = _first(
                row,
                ("original_url", "wind_original_url", "post_url", "tweet_url", "wind_url", "url"),
            )
            referenced_url = _first(
                row,
                ("referenced_url", "wind_referenced_url", "reference_url", "quoted_url"),
            )
            selected_source_url = (
                original_url if original_text and original_url
                else referenced_url if referenced_text and referenced_url
                else _first(row, ("source_url",))
            )
            social = {
                **_base(row, fact_path="social.post", value=text_value, **base_args),
                "text": text_value,
                "original_text": original_text,
                "referenced_text": referenced_text,
                "author": _social_author(row),
                "original_url": str(original_url) if original_url not in (None, "") else None,
                "referenced_url": str(referenced_url) if referenced_url not in (None, "") else None,
                "extracted_contracts": extracted_contracts,
                "extracted_identities": extracted_identities,
                "extracted_identity": exact_extracted_identity if exact_extracted_identity in extracted_identities else None,
                "extracted_ca": identity["contract_address"] if identity["contract_address"] in extracted_contracts else None,
                "extracted_chain": identity["chain"] if exact_extracted_identity in extracted_identities else None,
                "attention_only": text_value is None,
            }
            if selected_source_url not in (None, ""):
                social["source_url"] = str(selected_source_url)
            _append_unique(record["social_evidence"], social)

    _append_existing_audit_facts(record, token_view, observed_at)
    return record
