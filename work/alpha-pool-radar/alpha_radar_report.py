#!/usr/bin/env python3
"""
Screenshot-style Alpha/Meme/Dealer radar report.

This is still read-only. It adds two layers on top of alpha_pool_radar.py:
- Meme candidates from DexScreener latest profiles and token boosts.
- Dealer radar proxy from volume/mcap, futures OI, funding, pool age, liquidity, and optional holder concentration.

Holder concentration uses free explorer adapters where available; unsupported chains remain marked as missing.
"""

from __future__ import annotations

import argparse
import copy
import heapq
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_dealer import dealer_profile, dealer_score, dealer_dimensions, holder_coverage_summary
from alpha_alpha_buyflow import buyflow_coverage_summary
from alpha_alpha_buyflow import enrich_alpha_buyflow
from alpha_gold_watch import load_watch_state
from alpha_gold_watch import public_pick
from alpha_gold_watch import row_identity
from alpha_gold_watch import update_watch_state
from alpha_gold_watch import watch_summary
from alpha_gold_watch import write_watch_state
from alpha_gold_backtest import build_gold_backtest
from alpha_meme_potential import build_meme_heat_rows
from alpha_meme_potential import build_meme_conviction_rows
from alpha_meme_potential import build_meme_potential_rows
from alpha_meme_potential import annotate_meme_heat
from alpha_meme_potential import annotate_meme_early_conviction
from alpha_narrative import apply_meme_seed_terms
from alpha_narrative import apply_tweet_narrative_seeds
from alpha_narrative import extract_tweet_narrative_seeds
from alpha_narrative import normalize_meme_seed_terms
from alpha_recommendations import build_recommendation_rows, classify_row
from alpha_replay import row_key as replay_row_key
from alpha_replay import replay_action_calibration
from alpha_replay import replay_leaderboards
from alpha_replay import update_replay_history
from alpha_meme import (
    DEX_ADS_LATEST_URL,
    DEX_BOOSTS_LATEST_URL,
    DEX_BOOSTS_TOP_URL,
    DEX_CTO_LATEST_URL,
    DEX_PROFILES_LATEST_URL,
    HEAT_SOURCE_WEIGHTS,
    MEME_KEYWORDS,
    address_from_gmgn,
    apply_solana_holder_metrics_to_meme_rows,
    cap_score,
    chain_from_gmgn,
    fetch_gmgn_sources,
    fetch_pairs_for_source,
    fetch_profile_sources,
    apply_holder_metrics_to_meme_rows,
    heat_metrics,
    is_meme_like,
    load_meme_candidates,
    load_meme_observation_batch,
    nested_rows,
    optional_meme_source_status,
    pair_text,
    profile_text,
    source_rows,
    token_key,
)
from alpha_monitor_v3 import MONITOR_SCHEMA_VERSION
from alpha_monitor_v3 import _atomic_json as _atomic_monitor_json
from alpha_monitor_v3 import _bounded_lock as _monitor_v3_lock
from alpha_monitor_v3 import _load_state as _load_monitor_state
from alpha_monitor_v3 import compact_monitor_snapshot
from alpha_monitor_v3 import publish_monitor_v3
from alpha_monitor_v3 import source_descriptor
import alpha_pool_radar as alpha
import alpha_radar_dashboard
from alpha_stage_model import attach_live_stage_profiles
from alpha_stage_model import stage_profile
from alpha_stage_model import stage_sort_key
from alpha_token_intelligence import build_token_intelligence_records, identity_from_row, normalize_identity


TOKEN_INTELLIGENCE_FIELDS = (
    "status",
    "one_line_judgement",
    "project_narrative",
    "attention_evidence",
    "smart_wallets",
    "identity",
    "risks",
    "missing_evidence",
    "sources",
    "ai_narrative",
    "official_ca_status",
    "ca_conflict",
    "social_source_count",
    "wash_score",
    "flow_acceleration",
    "ai_risk_flags",
    "evidence_urls",
    "ai_analyzed_at",
    "generated_at",
)
TOKEN_INTELLIGENCE_REQUIRED_FIELDS = tuple(
    field
    for field in TOKEN_INTELLIGENCE_FIELDS
    if field not in {
        "ai_narrative", "official_ca_status", "ca_conflict", "social_source_count",
        "wash_score", "flow_acceleration", "ai_risk_flags", "evidence_urls", "ai_analyzed_at",
    }
)
TOKEN_INTELLIGENCE_AUX_FILES = (
    ("alpha-radar-replay-history.json", "rows", None),
    ("alpha-fast-watch-state.json", "candidates", None),
    ("alpha-gold-watch-state.json", "candidates", None),
    ("alpha-fast-track.json", "quotes", None),
    ("alpha-fast-track.json", "evidence", "smart_money_evidence"),
)
TOKEN_INTELLIGENCE_MAX_AUX_BYTES = 32 * 1024 * 1024
TOKEN_INTELLIGENCE_MAX_AUX_ROWS = 500
TOKEN_INTELLIGENCE_RESEARCH_PRIORITY_FIELDS = (
    "gold_watch_confirmed_pick",
    "gold_watch_early_pick",
    "gold_watch_pick",
    "gold_dog_pick",
    "meme_potential_rows",
    "meme_conviction_rows",
    "meme_heat_rows",
    "meme_rows",
    "meme_shadow_rows",
    "gold_watch_alerts",
    "meme_watch_universe",
)
TOKEN_INTELLIGENCE_V3_RESEARCH_STATES = (
    "smart_cluster",
    "resonating",
    "revival",
    "building",
)
TOKEN_INTELLIGENCE_SECRET_QUERY_FIELDS = {
    "api_key", "apikey", "key", "token", "access_token", "authorization", "auth", "secret", "signature", "sig",
    "password", "passwd", "credential", "private_key",
}
_TOKEN_INTELLIGENCE_RESEARCH_LOCK = threading.Lock()
_DEFAULT_MEME_CANDIDATE_LOADER = load_meme_candidates
_DEFAULT_MEME_OBSERVATION_LOADER = load_meme_observation_batch


def collect_meme_observation_batch(limit: int, concurrency: int) -> dict[str, Any]:
    """Use V3 ingestion while retaining dependency-injection compatibility."""
    if load_meme_observation_batch is not _DEFAULT_MEME_OBSERVATION_LOADER:
        return load_meme_observation_batch(limit, concurrency)
    if load_meme_candidates is not _DEFAULT_MEME_CANDIDATE_LOADER:
        candidates, errors = load_meme_candidates(limit, concurrency)
        return {
            "candidates": candidates, "events": [], "rejections": [],
            "errors": errors, "feed_counts": {}, "_legacy_candidates_only": True,
        }
    return load_meme_observation_batch(limit, concurrency)


def row_dicts(rows: list[alpha.RadarRow]) -> list[dict[str, Any]]:
    return [row.as_dict() for row in rows]


def attach_stage_profile(rows: list[dict[str, Any]], *, enable_live_stage: bool = True) -> None:
    for row in rows:
        if row.get("action") and row.get("stage"):
            continue
        market_cap = alpha.to_float(row.get("market_cap") or row.get("mcap") or row.get("dex_market_cap") or row.get("fdv"))
        volume = max(
            alpha.to_float(row.get("dealer_volume_usd")),
            alpha.to_float(row.get("alpha_volume24h")),
            alpha.to_float(row.get("dex_volume24h") or row.get("volume24h")),
        )
        row.update(stage_profile(row, market_cap=market_cap, volume=volume))
    if enable_live_stage:
        attach_live_stage_profiles(rows)


def load_previous_report(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / "alpha-radar-report-latest.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _batch_error_message(value: Any) -> str:
    if isinstance(value, Mapping):
        for field in ("message", "error", "detail", "reason"):
            if value.get(field):
                return str(value[field])
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _batch_error_family(value: Any) -> str:
    direct_family = ""
    source = ""
    if isinstance(value, Mapping):
        direct_family = str(value.get("provider_family") or "").strip().lower()
        source = str(
            value.get("provider_feed") or value.get("source") or value.get("feed") or ""
        ).strip().lower()
    known_families = {
        "gmgn", "okx", "985", "debot", "wind", "proficy", "noxa", "onchain", "dexscreener",
    }
    if direct_family in known_families:
        return direct_family
    message = _batch_error_message(value).lower()
    candidates = [source, *re.findall(r"[a-z0-9_]+", message)]
    for candidate in candidates:
        if candidate in known_families:
            return candidate
        try:
            return source_descriptor(candidate).provider_family
        except (TypeError, ValueError):
            continue
    return direct_family or "meme_sources"


def batch_error_messages(batch: Mapping[str, Any]) -> list[str]:
    errors = batch.get("errors") if isinstance(batch.get("errors"), list) else []
    return [_batch_error_message(value) for value in errors]


def monitor_source_health(batch: Mapping[str, Any], observed_at: str) -> dict[str, dict[str, Any]]:
    health = copy.deepcopy(batch.get("source_health") or {})
    events = batch.get("events") if isinstance(batch.get("events"), list) else []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        family = str(event.get("provider_family") or "meme_sources")
        entry = health.setdefault(family, {})
        explicit_failure = str(entry.get("status") or "").strip().lower() in {
            "error", "failed", "degraded", "unavailable",
        }
        if not explicit_failure:
            entry.update({"status": "ok", "observed_at": observed_at, "last_success": observed_at})
        entry["row_count"] = int(entry.get("row_count") or 0) + 1
        event_at = event.get("event_at")
        if event_at and (not entry.get("last_event") or str(event_at) > str(entry["last_event"])):
            entry["last_event"] = event_at

    errors = batch.get("errors") if isinstance(batch.get("errors"), list) else []
    for value in errors:
        message = _batch_error_message(value)
        family = _batch_error_family(value)
        entry = health.setdefault(family, {})
        entry.update({"status": "error", "observed_at": observed_at, "error": message})
        if isinstance(value, Mapping):
            category = value.get("error_category") or value.get("category")
            if category:
                entry["error_category"] = str(category)

    if not errors:
        health["meme_sources"] = {
            "status": "ok",
            "observed_at": observed_at,
            "last_success": observed_at,
            "row_count": len(events),
        }

    if not health:
        health["meme_sources"] = {
            "status": "ok", "observed_at": observed_at,
            "last_success": observed_at, "row_count": 0,
        }
    return health


def publish_report_monitor_v3(
    *,
    out_dir: Path,
    batch: Mapping[str, Any],
    observed_at: str,
    previous_snapshot: Mapping[str, Any] | None,
    persist_latest: bool = True,
) -> dict[str, Any]:
    monitor_batch = batch
    if batch.get("_legacy_candidates_only") is True:
        monitor_batch = {**batch, "candidates": []}
    source_health = monitor_source_health(batch, observed_at)
    degraded = any(
        str(entry.get("status") or "unknown").lower() not in {"ok", "healthy", "success", "fresh"}
        for entry in source_health.values()
        if isinstance(entry, Mapping)
    )
    previous_tokens = previous_snapshot.get("tokens") if isinstance(previous_snapshot, Mapping) else []
    try:
        if degraded and isinstance(previous_tokens, list) and previous_tokens:
            state_path = Path(out_dir) / "alpha-meme-monitor-v3-state.json"
            lock_path = Path(out_dir) / "alpha-meme-monitor-v3.lock"
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            with _monitor_v3_lock(lock_path):
                current_state = _load_monitor_state(state_path)
                current_tokens = current_state.get("tokens") if isinstance(current_state, Mapping) else []
                if not current_tokens:
                    _atomic_monitor_json(state_path, dict(previous_snapshot))
        snapshot = publish_monitor_v3(
            out_dir=out_dir,
            batch=monitor_batch,
            source_health=source_health,
            observed_at=observed_at,
        )
        compact = compact_monitor_snapshot(snapshot)
        if persist_latest:
            _atomic_monitor_json(Path(out_dir) / "alpha-meme-monitor-v3-latest.json", compact)
        return compact
    except Exception as exc:  # noqa: BLE001
        snapshot = copy.deepcopy(dict(previous_snapshot or {}))
        snapshot.setdefault("schema_version", MONITOR_SCHEMA_VERSION)
        snapshot.setdefault("observed_at", observed_at)
        snapshot.setdefault("tokens", [])
        snapshot.setdefault("rejections", [])
        source_health = snapshot.get("source_health")
        source_health = copy.deepcopy(source_health) if isinstance(source_health, Mapping) else {}
        source_health["persistence"] = {
            "status": "error", "observed_at": observed_at, "error": str(exc),
        }
        snapshot["source_health"] = source_health
        snapshot["monitor_status"] = "degraded"
        return snapshot


def _monitor_observed_at(snapshot: Any) -> datetime:
    if not isinstance(snapshot, Mapping):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(snapshot.get("observed_at") or "").replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def newest_monitor_v3(*snapshots: Any) -> dict[str, Any] | None:
    candidates = [snapshot for snapshot in snapshots if isinstance(snapshot, Mapping)]
    if not candidates:
        return None

    def ordering(indexed: tuple[int, Mapping[str, Any]]) -> tuple[datetime, bool, int]:
        index, snapshot = indexed
        health = snapshot.get("source_health")
        persistence_error = isinstance(health, Mapping) and isinstance(health.get("persistence"), Mapping)
        return _monitor_observed_at(snapshot), persistence_error, index

    return copy.deepcopy(dict(max(enumerate(candidates), key=ordering)[1]))


def publish_latest_report(path: Path, payload: Mapping[str, Any], *, out_dir: Path) -> dict[str, Any]:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / "alpha-meme-monitor-v3.lock"
    state_path = output / "alpha-meme-monitor-v3-state.json"
    with _monitor_v3_lock(lock_path):
        current_report = _read_bounded_json(path, TOKEN_INTELLIGENCE_MAX_AUX_BYTES * 4)
        current_state = _load_monitor_state(state_path)
        report = copy.deepcopy(dict(payload))
        selected = newest_monitor_v3(
            report.get("monitor_v3"),
            current_report.get("monitor_v3"),
            current_state,
        )
        if selected is not None:
            report["monitor_v3"] = selected
        _atomic_write_token_intelligence(path, report)
        return report


def _identity_key(row: Mapping[str, Any]) -> str | None:
    normalized = identity_from_row(row)
    if normalized is None:
        return None
    return f"{normalized[0]}:{normalized[1]}"


def _parse_intelligence_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _intelligence_freshness(generated_at: Any, now: str, ttl_seconds: int = 21600) -> tuple[str, int | None]:
    generated = _parse_intelligence_time(generated_at)
    current = _parse_intelligence_time(now)
    if generated is None or current is None:
        return "unknown", None
    age = int((current - generated).total_seconds())
    if age < 0:
        return "unknown", None
    return ("fresh" if age <= ttl_seconds else "stale"), age


def _validated_ui_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or any(field not in value for field in TOKEN_INTELLIGENCE_REQUIRED_FIELDS):
        return None
    if value.get("status") not in {"ready", "partial", "unavailable", "pending"}:
        return None
    if not isinstance(value.get("identity"), Mapping):
        return None
    if not isinstance(value.get("one_line_judgement"), str) or not isinstance(value.get("project_narrative"), str):
        return None
    list_fields = ("attention_evidence", "smart_wallets", "risks", "missing_evidence", "sources")
    if any(not isinstance(value.get(field), list) for field in list_fields):
        return None
    record = {field: copy.deepcopy(value.get(field)) for field in TOKEN_INTELLIGENCE_FIELDS}
    model_generated_at = value.get("model_generated_at")
    record["model_generated_at"] = model_generated_at if isinstance(model_generated_at, str) else None
    for field in ("attention_evidence", "smart_wallets", "risks", "missing_evidence"):
        record[field] = [item for item in record[field] if isinstance(item, (str, Mapping))]
    record["sources"] = [
        {
            "source_id": str(item.get("source_id") or ""),
            "title": str(item.get("title") or "evidence"),
            "url": _sanitize_source_url(item.get("url")),
            "observed_at": item.get("observed_at"),
        }
        for item in record["sources"]
        if isinstance(item, Mapping)
    ]
    record["ai_narrative"] = str(record.get("ai_narrative") or "").strip() or None
    record["official_ca_status"] = str(record.get("official_ca_status") or "unknown")
    record["ca_conflict"] = record.get("ca_conflict") if isinstance(record.get("ca_conflict"), bool) else None
    try:
        record["social_source_count"] = (
            max(0, int(record["social_source_count"]))
            if record.get("social_source_count") is not None
            else None
        )
    except (TypeError, ValueError, OverflowError):
        record["social_source_count"] = None
    record["wash_score"] = (
        record.get("wash_score")
        if isinstance(record.get("wash_score"), (int, float)) and not isinstance(record.get("wash_score"), bool)
        else None
    )
    record["flow_acceleration"] = (
        record.get("flow_acceleration")
        if isinstance(record.get("flow_acceleration"), (int, float)) and not isinstance(record.get("flow_acceleration"), bool)
        else None
    )
    record["ai_risk_flags"] = [item for item in (record.get("ai_risk_flags") or []) if isinstance(item, (str, Mapping))]
    record["evidence_urls"] = [
        sanitized
        for url in (record.get("evidence_urls") or [])
        if (sanitized := _sanitize_source_url(url))
    ]
    record["ai_analyzed_at"] = record.get("ai_analyzed_at") if isinstance(record.get("ai_analyzed_at"), str) else None
    return record


def _sanitize_source_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        query = urllib.parse.urlencode([
            (key, item)
            for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not _is_secret_query_field(key)
        ])
        return urllib.parse.urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, query, ""))
    except (TypeError, ValueError):
        return None


def _is_secret_query_field(value: Any) -> bool:
    raw = str(value or "").strip()
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
    if normalized in TOKEN_INTELLIGENCE_SECRET_QUERY_FIELDS:
        return True
    if normalized.endswith((
        "_api_key",
        "_key",
        "_secret",
        "_token",
        "_password",
        "_passwd",
        "_credential",
        "_authorization",
        "_signature",
    )):
        return True
    compact = normalized.replace("_", "")
    return compact.endswith((
        "apikey",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "bearertoken",
        "password",
        "privatekey",
        "credential",
        "authorization",
        "signature",
    ))


def load_token_intelligence_cache(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), dict):
        return {}
    records = {}
    for raw_key, value in payload["records"].items():
        key = str(raw_key)
        if ":" not in key:
            continue
        chain, contract = key.split(":", 1)
        try:
            key = ":".join(normalize_identity(chain, contract))
        except ValueError:
            continue
        record = _validated_ui_record(value)
        if record is not None:
            records[key] = record
    if payload.get("records") and not records:
        return {}
    return {**payload, "records": records}


def attach_token_intelligence(report: dict[str, Any], cache: Mapping[str, Any], *, now: str) -> int:
    records = cache.get("records") if isinstance(cache.get("records"), Mapping) else {}
    attached = 0
    for value in report.values():
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                continue
            record = records.get(_identity_key(row))
            if record is None:
                row.pop("token_intelligence", None)
                continue
            row["token_intelligence"] = copy.deepcopy(record)
            attached += 1
    freshness, age_seconds = _intelligence_freshness(cache.get("generated_at"), now)
    status = str(cache.get("status") or "unavailable") if records else "unavailable"
    if freshness == "stale" and records:
        status = "stale"
    meta = report.setdefault("meta", {})
    if not isinstance(meta, dict):
        meta = {}
        report["meta"] = meta
    meta["token_intelligence"] = {
        "status": status,
        "freshness": freshness,
        "generated_at": cache.get("generated_at"),
        "age_seconds": age_seconds,
        "record_count": len(records),
        "attached_count": attached,
    }
    return attached


def publish_token_intelligence_cache(report_path: Path, out_dir: Path, *, now: str) -> int:
    report = _read_bounded_json(report_path, TOKEN_INTELLIGENCE_MAX_AUX_BYTES * 4)
    if not report:
        return 0
    cache = load_token_intelligence_cache(out_dir / "token-intelligence.json")
    attached = attach_token_intelligence(report, cache, now=now)
    _atomic_write_token_intelligence(report_path, report)
    return attached


def _read_bounded_json(path: Path, max_bytes: int) -> dict[str, Any]:
    try:
        if path.stat().st_size > max_bytes:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _rows_from_mapping(
    value: Any,
    *,
    observed_at: Any = None,
    wrapper: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    rows = []
    recent_items = heapq.nlargest(
        TOKEN_INTELLIGENCE_MAX_AUX_ROWS,
        value.items(),
        key=lambda item: str(
            item[1].get("latest_seen_at")
            or item[1].get("last_seen_at")
            or item[1].get("quote_observed_at")
            or item[1].get("latest_evidence_at")
            or item[1].get("observed_at")
            or observed_at
            or ""
        )
        if isinstance(item[1], Mapping)
        else "",
    )
    for key, raw in recent_items:
        if not isinstance(raw, Mapping):
            continue
        row = {wrapper: dict(raw)} if wrapper else {name: item for name, item in raw.items() if name != "observations"}
        if _identity_key(row) is None and ":" in str(key):
            chain, contract = str(key).split(":", 1)
            row.update({"chain": chain, "contract_address": contract})
        if observed_at and not any(row.get(field) for field in ("observed_at", "quote_observed_at", "latest_seen_at", "updated_at")):
            row["observed_at"] = observed_at
        if _identity_key(row) is not None:
            rows.append(row)
    rows.sort(
        key=lambda row: str(
            row.get("latest_seen_at")
            or row.get("last_seen_at")
            or row.get("quote_observed_at")
            or row.get("observed_at")
            or ""
        ),
        reverse=True,
    )
    return rows[:TOKEN_INTELLIGENCE_MAX_AUX_ROWS]


def collect_token_intelligence_rows(
    report: Mapping[str, Any],
    out_dir: Path,
    auxiliary_rows: Any = (),
) -> tuple[list[dict[str, Any]], int]:
    report_rows = []
    report_keys = set()
    for value in report.values():
        if isinstance(value, list):
            candidates = value
        elif isinstance(value, Mapping) and _identity_key(value) is not None:
            candidates = [value]
        else:
            continue
        for row in candidates:
            if isinstance(row, Mapping) and _identity_key(row) is not None:
                report_rows.append(dict(row))
                report_keys.add(_identity_key(row))

    try:
        configured = int(os.environ.get("TOKEN_INTELLIGENCE_AUX_MAX_BYTES", TOKEN_INTELLIGENCE_MAX_AUX_BYTES))
        max_bytes = max(1024, configured)
    except ValueError:
        max_bytes = TOKEN_INTELLIGENCE_MAX_AUX_BYTES
    has_current_watch_rows = bool(auxiliary_rows)
    auxiliary = _rows_from_mapping(auxiliary_rows) if isinstance(auxiliary_rows, Mapping) else [
        {name: item for name, item in row.items() if name != "observations"}
        for row in list(auxiliary_rows)[:TOKEN_INTELLIGENCE_MAX_AUX_ROWS]
        if isinstance(row, Mapping)
    ]
    payloads = {}
    auxiliary_files = () if report.get("token_intelligence_selected_only") is True else TOKEN_INTELLIGENCE_AUX_FILES
    for filename, field, wrapper in auxiliary_files:
        if filename == "alpha-gold-watch-state.json" and has_current_watch_rows:
            continue
        if filename not in payloads:
            payloads[filename] = _read_bounded_json(out_dir / filename, max_bytes)
        payload = payloads[filename]
        auxiliary.extend(
            _rows_from_mapping(
                payload.get(field),
                observed_at=payload.get("updated_at"),
                wrapper=wrapper,
            )
        )
    auxiliary_keys = {
        key
        for row in auxiliary
        if (key := _identity_key(row)) is not None and key not in report_keys
    }
    return [*report_rows, *auxiliary], len(auxiliary_keys)


def _token_intelligence_research_targets(
    report: Mapping[str, Any],
    core_records: list[dict[str, Any]],
    limit: int,
    skip_keys: set[str] | None = None,
) -> list[str]:
    if limit <= 0:
        return []
    available = {
        f"{record['chain']}:{record['contract_address']}"
        for record in core_records
    }
    ordered = []
    seen = set()
    skipped = skip_keys or set()

    monitor_v3 = report.get("monitor_v3")
    if isinstance(monitor_v3, Mapping) and monitor_v3.get("schema_version") == 3:
        tokens = monitor_v3.get("tokens")
        candidates = tokens if isinstance(tokens, list) else []
        state_rank = {
            state: len(TOKEN_INTELLIGENCE_V3_RESEARCH_STATES) - index
            for index, state in enumerate(TOKEN_INTELLIGENCE_V3_RESEARCH_STATES)
        }

        def candidate_rank(row: Mapping[str, Any]) -> int:
            active = set(row.get("active_states") or [])
            return max((state_rank.get(str(state), 0) for state in active), default=0)

        selected = []
        for row in candidates:
            if not isinstance(row, Mapping):
                continue
            active = {str(state) for state in row.get("active_states") or []}
            freshness = row.get("freshness") if isinstance(row.get("freshness"), Mapping) else {}
            if (
                freshness.get("status") != "fresh"
                or "blocked_risk" in active
                or "trend_watch" in active
                or not active.intersection(TOKEN_INTELLIGENCE_V3_RESEARCH_STATES)
            ):
                continue
            selected.append(row)
        selected.sort(key=candidate_rank, reverse=True)
        for row in selected:
            key = _identity_key(row)
            if key in available and key not in seen and key not in skipped:
                seen.add(key)
                ordered.append(key)
                if len(ordered) >= limit:
                    break
        return ordered

    for field in TOKEN_INTELLIGENCE_RESEARCH_PRIORITY_FIELDS:
        value = report.get(field)
        candidates = value if isinstance(value, list) else [value]
        for row in candidates:
            if not isinstance(row, Mapping):
                continue
            key = _identity_key(row)
            if key in available and key not in seen and key not in skipped:
                seen.add(key)
                ordered.append(key)
                if len(ordered) >= limit:
                    return ordered
    return ordered


def _evidence_line(label: str, value: Any, suffix: str = "") -> str | None:
    if value in (None, ""):
        return None
    return f"{label} {value}{suffix}"


def _model_statement_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def _is_social_research_source(source: Mapping[str, Any]) -> bool:
    try:
        host = (urllib.parse.urlsplit(str(source.get("url") or "")).hostname or "").lower()
    except ValueError:
        host = ""
    return host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "t.me", "telegram.me", "reddit.com", "www.reddit.com"}


def _ui_record(
    core: Mapping[str, Any],
    generated_at: str,
    research: Mapping[str, Any] | None = None,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    market = core.get("market_evidence") if isinstance(core.get("market_evidence"), Mapping) else {}
    smart = core.get("smart_wallet_evidence") if isinstance(core.get("smart_wallet_evidence"), Mapping) else {}
    official = core.get("official_identity") if isinstance(core.get("official_identity"), Mapping) else {}
    attention = [
        line
        for line in (
            _evidence_line("市值", market.get("market_cap_usd"), " 美元"),
            _evidence_line("流动性", market.get("liquidity_usd"), " 美元"),
            _evidence_line("24h 成交", market.get("volume_24h_usd"), " 美元"),
        )
        if line
    ]
    smart_wallets = []
    if smart:
        smart_wallets.append(
            {
                "summary": (
                    f"核验盈利钱包 {smart.get('qualified_wallet_count') or 0} 个，"
                    f"候选买入钱包 {smart.get('candidate_buy_wallet_count') or 0} 个"
                ),
                "source": " / ".join(str(item) for item in smart.get("sources") or []),
                "observed_at": smart.get("observed_at"),
            }
        )
    risks = [str(item) for item in market.get("risk_flags") or [] if str(item).strip()]
    missing = [str(item) for item in core.get("missing_evidence") or []]
    official_status = str(official.get("status") or "unknown")
    matched = official.get("matched_contract") if isinstance(official.get("matched_contract"), Mapping) else {}
    official_contract = (
        str(matched.get("contract_address") or core.get("contract_address") or "")
        if official_status == "match"
        else ""
    )

    research_sources = []
    research_official_contracts = []
    research_narrative = ""
    if isinstance(research, Mapping):
        research_official = research.get("official_contract") if isinstance(research.get("official_contract"), Mapping) else {}
        research_official_contracts = [
            str(item).strip().lower()
            for item in research_official.get("contracts") or []
            if str(item).strip()
        ]
        research_official_status = {
            "exact_match": "match",
            "match": "match",
            "mismatch": "mismatch",
        }.get(str(research_official.get("status") or ""))
        if research_official_status:
            official_status = research_official_status
            official_contract = str(core.get("contract_address") or "") if official_status == "match" else ""
            missing = [item for item in missing if item != "official_identity"]
        for source in research.get("sources") or []:
            if not isinstance(source, Mapping):
                continue
            research_sources.append(
                {
                    "source_id": str(source.get("source_id") or ""),
                    "source": str(source.get("source") or ""),
                    "title": str(source.get("title") or source.get("source") or "public research"),
                    "url": _sanitize_source_url(source.get("url")),
                    "observed_at": source.get("observed_at") or research.get("observed_at"),
                }
            )
            snippet = str(source.get("snippet") or "").strip()
            if snippet:
                if source.get("official_candidate") and not research_narrative:
                    research_narrative = snippet[:600]
                attention.append(
                    {
                        "summary": snippet[:240],
                        "source": source.get("title") or source.get("source"),
                        "observed_at": source.get("observed_at"),
                    }
                )
    if official_status == "mismatch":
        risks.append("官方来源合约与当前合约不一致")

    source_items = []
    seen_sources = set()
    for source in [*(core.get("source_references") or []), *research_sources]:
        if not isinstance(source, Mapping):
            continue
        item = {
            "source_id": str(source.get("source_id") or ""),
            "title": str(source.get("title") or source.get("source") or "evidence"),
            "url": _sanitize_source_url(source.get("url")),
            "observed_at": source.get("observed_at"),
        }
        key = (item["source_id"], item["title"], item["url"], item["observed_at"])
        if key not in seen_sources:
            seen_sources.add(key)
            source_items.append(item)

    model_summary = summary.get("summary") if isinstance(summary, Mapping) and summary.get("status") == "ready" else {}
    model_summary = model_summary if isinstance(model_summary, Mapping) else {}
    fallback = str(core.get("fallback_summary") or "本地可归因证据暂不可用。")
    alternates = [
        {"address": item.get("contract_address"), "label": item.get("chain")}
        for item in core.get("same_symbol_contracts") or []
        if isinstance(item, Mapping) and not item.get("is_current")
    ]
    model_judgement = _model_statement_text(model_summary.get("one_line_judgement"))
    model_narrative = _model_statement_text(model_summary.get("project_narrative"))
    model_risks = list(model_summary.get("risks") or [])
    model_evidence_ids = set()
    for field in ("one_line_judgement", "project_narrative", "attention_evidence", "smart_wallet_evidence", "risks"):
        raw_statements = model_summary.get(field)
        statements = raw_statements if isinstance(raw_statements, list) else [raw_statements]
        for statement in statements:
            if isinstance(statement, Mapping):
                model_evidence_ids.update(
                    str(item)
                    for item in statement.get("evidence_ids") or []
                    if str(item).strip()
                )
    symbol = str(core.get("symbol") or core.get("name") or "该代币")
    research_judgement = (
        f"{symbol}：Dex 行情页与项目官网均命中当前完整合约；其余资金和风险证据按缺失项单列。"
        if official_status == "match"
        else ""
    )
    research_project_narrative = (
        f"项目官网公开内容：{research_narrative}" if research_narrative else research_judgement
    )
    core_status = str(core.get("status") or "unavailable")
    research_status = str(research.get("status") or "") if isinstance(research, Mapping) else ""
    ui_status = "partial" if core_status == "unavailable" and research_status in {"ready", "partial"} else core_status
    if ui_status != "unavailable" and not missing:
        ui_status = "ready"
    wash_score = market.get("wash_score")
    if wash_score is None and isinstance(market.get("is_wash_trading"), bool):
        wash_score = 100.0 if market["is_wash_trading"] else 0.0
    ai_risk_flags = [
        text
        for item in model_risks
        if (text := _model_statement_text(item))
    ]
    evidence_urls = list(dict.fromkeys(
        str(item.get("url"))
        for item in source_items
        if item.get("url") and item.get("source_id") in model_evidence_ids
    ))
    return {
        "status": ui_status,
        "one_line_judgement": model_judgement or research_judgement or fallback,
        "project_narrative": model_narrative or research_project_narrative or fallback,
        "attention_evidence": list(model_summary.get("attention_evidence") or attention)[:6],
        "smart_wallets": list(model_summary.get("smart_wallet_evidence") or smart_wallets)[:6],
        "identity": {
            "official_status": official_status,
            "official_contract": official_contract,
            "alternate_contracts": alternates[:8],
        },
        "risks": list(model_risks or risks)[:8],
        "missing_evidence": missing,
        "sources": source_items[:8],
        "ai_narrative": model_narrative or None,
        "official_ca_status": official_status,
        "ca_conflict": (
            official_status == "mismatch" or len(set(research_official_contracts)) > 1
            if official_status != "unknown" or research_official_contracts
            else None
        ),
        "social_source_count": (
            sum(_is_social_research_source(item) for item in research_sources)
            if isinstance(research, Mapping)
            else None
        ),
        "wash_score": wash_score,
        "flow_acceleration": market.get("flow_acceleration"),
        "ai_risk_flags": ai_risk_flags[:8],
        "evidence_urls": evidence_urls[:8],
        "ai_analyzed_at": generated_at if model_summary else None,
        "model_generated_at": generated_at if model_summary else None,
        "generated_at": generated_at,
    }


def _merge_previous_intelligence(
    current: dict[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    keep_previous_text: bool,
) -> dict[str, Any]:
    validated = _validated_ui_record(previous)
    if validated is None:
        return current
    merged = copy.deepcopy(current)
    previous_text_at = validated.get("model_generated_at") or validated.get("generated_at")
    current_generated_at = merged.get("generated_at")
    previous_text_is_fresh = _intelligence_freshness(previous_text_at, current_generated_at)[0] == "fresh"
    if keep_previous_text and previous_text_is_fresh:
        for field in ("one_line_judgement", "project_narrative"):
            if validated.get(field):
                merged[field] = validated[field]
        merged["model_generated_at"] = previous_text_at
        for field in ("ai_narrative", "ai_risk_flags", "evidence_urls", "ai_analyzed_at"):
            if validated.get(field):
                merged[field] = copy.deepcopy(validated[field])
    for field in ("attention_evidence", "smart_wallets", "risks", "sources"):
        combined = []
        seen = set()
        for item in [*(merged.get(field) or []), *(validated.get(field) or [])]:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, Mapping) else str(item)
            if marker not in seen:
                seen.add(marker)
                combined.append(item)
        merged[field] = combined[:8]
    previous_identity = validated.get("identity") if isinstance(validated.get("identity"), Mapping) else {}
    current_identity = merged.get("identity") if isinstance(merged.get("identity"), Mapping) else {}
    if current_identity.get("official_status") in {None, "", "unknown", "unavailable"} and previous_identity:
        current_identity = {**previous_identity, **current_identity}
        current_identity["official_status"] = previous_identity.get("official_status")
        current_identity["official_contract"] = previous_identity.get("official_contract")
        merged["identity"] = current_identity
    if current_identity.get("official_status") in {"match", "matched", "verified"}:
        merged["missing_evidence"] = [
            item for item in merged.get("missing_evidence") or [] if str(item) != "official_identity"
        ]
    if merged.get("official_ca_status") in {None, "", "unknown", "unavailable"}:
        merged["official_ca_status"] = validated.get("official_ca_status") or "unknown"
    if merged.get("ca_conflict") is None:
        merged["ca_conflict"] = validated.get("ca_conflict")
    for field in ("social_source_count", "wash_score", "flow_acceleration"):
        if merged.get(field) is None and validated.get(field) is not None:
            merged[field] = validated[field]
    if validated.get("status") == "ready" or (merged.get("status") == "unavailable" and validated.get("status") == "partial"):
        merged["status"] = validated.get("status")
    return merged


def _atomic_write_token_intelligence(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_name = stream.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def refresh_token_intelligence(
    report: dict[str, Any],
    *,
    out_dir: Path,
    now: str,
    research_enabled: bool | None = None,
    auxiliary_rows: Any = (),
) -> dict[str, Any]:
    cache_path = out_dir / "token-intelligence.json"
    previous = load_token_intelligence_cache(cache_path)
    if research_enabled is None:
        research_enabled = os.environ.get("TOKEN_INTELLIGENCE_RESEARCH_ENABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    try:
        rows, auxiliary_count = collect_token_intelligence_rows(report, out_dir, auxiliary_rows)
        core_records = build_token_intelligence_records(rows, observed_at=now, max_source_references=8)
        try:
            max_external = max(0, min(8, int(os.environ.get("TOKEN_INTELLIGENCE_RESEARCH_LIMIT", "2"))))
            timeout_seconds = max(
                0.5,
                min(15.0, float(os.environ.get("TOKEN_INTELLIGENCE_RESEARCH_TIMEOUT_SECONDS", "12"))),
            )
            total_budget = max(
                1.0,
                min(30.0, float(os.environ.get("TOKEN_INTELLIGENCE_RESEARCH_TOTAL_SECONDS", "20"))),
            )
        except ValueError:
            max_external, timeout_seconds, total_budget = 1, 12.0, 20.0

        ui_records = {}
        previous_records = previous.get("records") if isinstance(previous.get("records"), Mapping) else {}
        provider_failed = False
        research_count = 0
        research_success_count = 0
        force_research = os.environ.get("TOKEN_INTELLIGENCE_RESEARCH_FORCE", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        fresh_model_keys = set() if force_research else {
            key
            for key, record in previous_records.items()
            if isinstance(record, Mapping)
            and record.get("ai_analyzed_at")
            and record.get("model_generated_at")
            and _intelligence_freshness(record.get("model_generated_at"), now)[0] == "fresh"
        }
        research_targets = _token_intelligence_research_targets(
            report,
            core_records,
            max_external,
            fresh_model_keys,
        )
        research_target_set = set(research_targets)
        core_by_key = {
            f"{record['chain']}:{record['contract_address']}": record
            for record in core_records
        }
        ordered_core_records = [core_by_key[key] for key in research_targets]
        ordered_core_records.extend(
            record
            for record in core_records
            if f"{record['chain']}:{record['contract_address']}" not in research_target_set
        )
        started = time.monotonic()
        for core in ordered_core_records:
            research = None
            summary = None
            key = f"{core['chain']}:{core['contract_address']}"
            if research_enabled and key in research_target_set and time.monotonic() - started < total_budget:
                try:
                    from alpha_token_research import research_token_from_env, summarize_grounded, summarize_monitor_signal

                    research = research_token_from_env(
                        str(core.get("chain") or ""),
                        str(core.get("contract_address") or ""),
                        symbol=str(core.get("symbol") or ""),
                        name=str(core.get("name") or ""),
                        now=now,
                        timeout_seconds=timeout_seconds,
                    )
                    provider_failed = provider_failed or research.get("status") in {
                        "provider_timeout",
                        "provider_error",
                    }
                    if research.get("status") in {"ready", "partial"}:
                        research_success_count += 1
                    evidence = {
                        "identity": {
                            "chain": core.get("chain"),
                            "contract": core.get("contract_address"),
                        },
                        "deterministic": core,
                        "research": research,
                    }
                    summarizer = summarize_monitor_signal if report.get("token_intelligence_selected_only") is True else summarize_grounded
                    summary = summarizer(
                        evidence,
                        core.get("fallback_summary"),
                        timeout_seconds=timeout_seconds,
                    )
                    research_count += 1
                except Exception:  # noqa: BLE001 - research must not fail the report
                    provider_failed = True
            current_record = _ui_record(
                core,
                now,
                research,
                summary,
            )
            summary_ready = isinstance(summary, Mapping) and summary.get("status") == "ready"
            research_failed = isinstance(research, Mapping) and research.get("status") in {
                "provider_timeout",
                "provider_error",
            }
            ui_records[key] = _merge_previous_intelligence(
                current_record,
                previous_records.get(key),
                keep_previous_text=not summary_ready,
            )

        statuses = {record["status"] for record in ui_records.values()}
        cache_status = (
            "unavailable"
            if not ui_records or statuses == {"unavailable"}
            else "ready"
            if statuses == {"ready"}
            else "partial"
        )
        payload = {
            "schema_version": 1,
            "status": cache_status,
            "generated_at": now,
            "records": ui_records,
            "research": {
                "enabled": bool(research_enabled),
                "attempted": research_count,
                "provider_failed": provider_failed,
                "bounded": True,
            },
        }
        cache_written = not provider_failed or research_success_count > 0
        if cache_written:
            _atomic_write_token_intelligence(cache_path, payload)
        attach_token_intelligence(report, payload, now=now)
        return {
            "cache_written": cache_written,
            "cache_path": str(cache_path),
            "record_count": len(ui_records),
            "auxiliary_identity_count": auxiliary_count,
            "research_attempted": research_count,
            "research_succeeded": research_success_count,
            "provider_failed": provider_failed,
        }
    except Exception as exc:  # noqa: BLE001 - retain and attach the last valid cache
        attach_token_intelligence(report, previous, now=now)
        meta = report.setdefault("meta", {}).setdefault("token_intelligence", {})
        meta.update(
            {
                "status": "stale" if previous.get("records") else "unavailable",
                "error": type(exc).__name__,
            }
        )
        return {
            "cache_written": False,
            "cache_path": str(cache_path),
            "record_count": len(previous.get("records") or {}),
            "auxiliary_identity_count": 0,
            "error": type(exc).__name__,
        }


def schedule_token_intelligence_research(
    report: Mapping[str, Any],
    *,
    out_dir: Path,
    now: str,
    auxiliary_rows: Any = (),
    report_path: Path | None = None,
) -> bool:
    enabled = os.environ.get("TOKEN_INTELLIGENCE_RESEARCH_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"} or not _TOKEN_INTELLIGENCE_RESEARCH_LOCK.acquire(blocking=False):
        return False
    report_snapshot = copy.deepcopy(dict(report))
    auxiliary_snapshot = copy.deepcopy(auxiliary_rows)

    def run() -> None:
        try:
            refresh_token_intelligence(
                report_snapshot,
                out_dir=out_dir,
                now=now,
                research_enabled=True,
                auxiliary_rows=auxiliary_snapshot,
            )
            if report_path is not None:
                publish_token_intelligence_cache(report_path, out_dir, now=now)
        finally:
            _TOKEN_INTELLIGENCE_RESEARCH_LOCK.release()

    threading.Thread(target=run, name="token-intelligence-research", daemon=True).start()
    return True


def wait_for_token_intelligence_research(timeout_seconds: float = 35.0) -> bool:
    acquired = _TOKEN_INTELLIGENCE_RESEARCH_LOCK.acquire(timeout=max(0.0, timeout_seconds))
    if not acquired:
        return False
    _TOKEN_INTELLIGENCE_RESEARCH_LOCK.release()
    return True


def attach_gold_watch_fields(rows: list[dict[str, Any]], state: dict[str, Any]) -> None:
    candidates = state.get("candidates") or {}
    for row in rows:
        state_row = candidates.get(row_identity(row))
        if isinstance(state_row, dict):
            row.update(public_pick(row, state_row))


LAUNCHPAD_OBSERVE_SOURCES = {
    "noxa_launchpad": "Noxa",
    "fourmeme_launchpad": "Four.meme",
    "flap_launchpad": "Flap",
    "bsc_onchain": "BSC 链上新池",
}


def launchpad_observe_label(row: dict[str, Any]) -> str:
    sources = {str(source or "").strip().lower() for source in row.get("sources") or [] if str(source or "").strip()}
    source_family = str(row.get("source_family") or "").strip().lower()
    if source_family:
        sources.add(source_family)
    for source, label in LAUNCHPAD_OBSERVE_SOURCES.items():
        if source in sources:
            return label
    return ""


def has_launchpad_market_snapshot(row: dict[str, Any]) -> bool:
    market_cap = alpha.to_float(row.get("mcap") or row.get("market_cap") or row.get("fdv"))
    liquidity = alpha.to_float(row.get("liquidity") or row.get("liquidity_usd"))
    return market_cap > 0 and liquidity > 0


def shadow_observation_row(row: dict[str, Any]) -> dict[str, Any]:
    original_bucket = row.get("recommendation_bucket") or ""
    original_action = row.get("recommendation_action") or ""
    original_reason = row.get("recommendation_reason") or ""
    launchpad_label = launchpad_observe_label(row)
    if launchpad_label and has_launchpad_market_snapshot(row):
        action = f"{launchpad_label} 观察"
        reason = f"{launchpad_label} 发射源保底观察；原判断：{original_action}；{original_reason}"
        next_step = "进入发现层静默跟踪，等二源确认、流动性加厚或健康回踩后再升级。"
    else:
        action = "影子观察"
        reason = f"未进唯一主推；原判断：{original_action}；{original_reason}"
        next_step = "后台静默跟踪，若后续涨成金狗，复盘漏掉原因。"
    return {
        **row,
        "shadow_original_bucket": original_bucket,
        "shadow_original_action": original_action,
        "recommendation_bucket": "shadow",
        "recommendation_label": "影子观察",
        "recommendation_action": action,
        "recommendation_reason": reason,
        "recommendation_next_step": next_step,
    }


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


_REPLAY_HISTORY_CACHE: dict[Path, tuple[int, int, dict[str, Any]]] = {}


def load_replay_history(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "alpha-radar-replay-history.json"
    if not path.exists():
        return {}
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = path.resolve()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _REPLAY_HISTORY_CACHE.get(key)
    if cached and cached[:2] == signature:
        return cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    result = payload if isinstance(payload, dict) else {}
    _REPLAY_HISTORY_CACHE[key] = (*signature, result)
    return result


def load_tweet_narrative_report(out_dir: Path) -> dict[str, Any]:
    env_path = os.environ.get("ALPHA_NARRATIVE_TWEETS_FILE", "").strip()
    paths = [Path(env_path)] if env_path else []
    paths.extend([out_dir / "alpha-narrative-tweets.json", out_dir / "narrative-tweets.json"])
    for path in paths:
        if not path or not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"enabled": True, "source_file": str(path), "error": str(exc), "source_count": 0, "seed_count": 0, "seeds": []}
        tweets = payload.get("tweets") if isinstance(payload, dict) else payload
        if not isinstance(tweets, list):
            return {"enabled": True, "source_file": str(path), "error": "tweets must be a list", "source_count": 0, "seed_count": 0, "seeds": []}
        report = extract_tweet_narrative_seeds([tweet for tweet in tweets if isinstance(tweet, dict)])
        report["enabled"] = True
        report["source_file"] = str(path)
        return report
    return {"enabled": False, "source_count": 0, "seed_count": 0, "seeds": []}


def load_meme_seed_report(out_dir: Path) -> dict[str, Any]:
    env_path = os.environ.get("ALPHA_MEME_SEEDS_FILE", "").strip()
    paths = [Path(env_path)] if env_path else []
    paths.extend([out_dir / "meme-seed-terms.json", out_dir / "alpha-meme-seeds.json", out_dir / "offchain-meme-seeds.json"])
    for path in paths:
        if not path or not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "enabled": True,
                "source_file": str(path),
                "error": str(exc),
                "source_count": 0,
                "seed_count": 0,
                "top_seed": None,
                "seeds": [],
            }
        report = normalize_meme_seed_terms(payload)
        report["enabled"] = True
        report["source_file"] = str(path)
        return report
    return {"enabled": False, "source_count": 0, "seed_count": 0, "top_seed": None, "seeds": []}


def attach_replay_rows(recommendation_rows: list[dict[str, Any]], history: dict[str, Any]) -> None:
    rows_by_key = history.get("rows") or {}
    for row in recommendation_rows:
        replay_row = rows_by_key.get(replay_row_key(row))
        if not isinstance(replay_row, dict):
            continue
        first_snapshot = replay_row.get("first_snapshot") if isinstance(replay_row.get("first_snapshot"), dict) else {}
        row["replay"] = {
            "first_seen_at": replay_row.get("first_seen_at"),
            "latest_seen_at": replay_row.get("latest_seen_at"),
            "first_price_usd": replay_row.get("first_price_usd"),
            "first_mcap_usd": first_snapshot.get("market_cap") or first_snapshot.get("mcap"),
            "latest_price_usd": replay_row.get("latest_price_usd"),
            "return_since_first_pct": replay_row.get("return_since_first_pct"),
            "return_1h_pct": replay_row.get("return_1h_pct"),
            "return_6h_pct": replay_row.get("return_6h_pct"),
            "return_24h_pct": replay_row.get("return_24h_pct"),
            "hit_status": replay_row.get("hit_status"),
        }


def build_shadow_meme_rows(
    meme_rows: list[dict[str, Any]],
    selected_rows: list[dict[str, Any]],
    limit: int,
    replay_calibration: dict[str, dict[str, Any]] | None,
    chain_scope: str | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    selected_keys = {replay_row_key(row) for row in selected_rows}
    shadow_rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    candidates = build_meme_potential_rows(
        meme_rows,
        limit=None,
        include_rejects=True,
        replay_calibration=replay_calibration,
        chain_scope=chain_scope,
    )
    for row in candidates:
        if row.get("market_data_pending"):
            continue
        key = replay_row_key(row)
        if key in selected_keys or key in seen_keys:
            continue
        seen_keys.add(key)
        shadow_rows.append(shadow_observation_row(row))
        if len(shadow_rows) >= limit:
            break
    visible_keys = {replay_row_key(row) for row in shadow_rows}
    launchpad_rows = [
        row
        for row in candidates
        if launchpad_observe_label(row)
        and has_launchpad_market_snapshot(row)
        and not row.get("market_data_pending")
        and replay_row_key(row) not in selected_keys
        and replay_row_key(row) not in visible_keys
    ]
    for row in launchpad_rows[: min(5, limit)]:
        if len(shadow_rows) >= limit:
            replaced = False
            for index in range(len(shadow_rows) - 1, -1, -1):
                if not launchpad_observe_label(shadow_rows[index]):
                    visible_keys.discard(replay_row_key(shadow_rows[index]))
                    shadow_rows[index] = shadow_observation_row(row)
                    visible_keys.add(replay_row_key(row))
                    replaced = True
                    break
            if not replaced:
                break
        else:
            shadow_rows.append(shadow_observation_row(row))
            visible_keys.add(replay_row_key(row))
    return shadow_rows


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    previous_report = load_previous_report(out_dir)
    alpha.configure_cache(out_dir)
    scan_args = argparse.Namespace(
        alpha_limit=args.alpha_limit,
        top=args.top,
        max_market_cap=args.max_market_cap,
        min_volume=args.min_volume,
        chains=args.chains,
        concurrency=args.concurrency,
        skip_dex=False,
        skip_futures_details=False,
        out_dir=args.out_dir,
        holders_enable=args.holders_enable,
        holder_provider=args.holder_provider,
        holder_top_tokens=args.holder_top_tokens,
        holder_offset=args.holder_offset,
        risk_enable=args.risk_enable,
        risk_top_tokens=args.risk_top_tokens,
    )
    alpha_rows, meta = alpha.run_scan(scan_args)
    used_previous_meme = False
    meme_cache_only = bool(getattr(args, "meme_cache_only", False)) or env_flag("ALPHA_MEME_CACHE_ONLY")
    meme_rows: list[dict[str, Any]] = []
    meme_errors: list[str] = []
    monitor_observed_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    observation_batch: dict[str, Any] = {
        "candidates": [], "events": [], "rejections": [], "errors": [], "feed_counts": {}
    }
    if meme_cache_only:
        previous_meme = (previous_report or {}).get("meme_rows") or []
        if previous_meme:
            meme_rows = [dict(row) for row in previous_meme if isinstance(row, dict)]
            used_previous_meme = True
            meme_errors.append("meme_rows reused from previous report cache")
        else:
            meme_errors.append("meme cache-only mode enabled but previous meme_rows are unavailable")
        observation_batch["errors"] = ["meme_source_error: cache-only mode"]
    else:
        try:
            observation_batch = collect_meme_observation_batch(args.meme_source_limit, args.concurrency)
        except Exception as exc:  # noqa: BLE001
            observation_batch = {
                "candidates": [], "events": [], "rejections": [],
                "errors": [f"meme_source_error: {exc}"], "feed_counts": {},
            }
        meme_rows = observation_batch["candidates"]
        meme_errors = batch_error_messages(observation_batch)
    monitor_v3 = publish_report_monitor_v3(
        out_dir=out_dir,
        batch=observation_batch,
        observed_at=monitor_observed_at,
        previous_snapshot=(previous_report or {}).get("monitor_v3"),
    )
    if not meme_rows and meme_errors and previous_report and not meme_cache_only:
        previous_meme = previous_report.get("meme_rows") or []
        if previous_meme:
            meme_rows = previous_meme
            used_previous_meme = True
            meme_errors.append("meme_rows fell back to previous report cache")
    if getattr(args, "holders_enable", False) and not meme_cache_only:
        meme_errors.extend(
            apply_holder_metrics_to_meme_rows(
                meme_rows,
                getattr(args, "holder_top_tokens", 12),
                "auto" if getattr(args, "holder_provider", "auto") == "goplus" else getattr(args, "holder_provider", "auto"),
                getattr(args, "holder_offset", 20),
            )
        )
    tweet_narrative = load_tweet_narrative_report(out_dir)
    tweet_seeds = tweet_narrative.get("seeds") or []
    if isinstance(tweet_seeds, list) and tweet_seeds:
        meme_rows = apply_tweet_narrative_seeds(meme_rows, tweet_seeds)
    meme_seed_terms = load_meme_seed_report(out_dir)
    meme_seeds = meme_seed_terms.get("seeds") or []
    if isinstance(meme_seeds, list) and meme_seeds:
        meme_rows = apply_meme_seed_terms(meme_rows, meme_seeds)
    meme_rows = [annotate_meme_heat(annotate_meme_early_conviction(row)) for row in meme_rows]
    alpha_dicts = row_dicts(alpha_rows)
    enrich_alpha_buyflow(alpha_dicts)
    attach_stage_profile(alpha_dicts, enable_live_stage=getattr(args, "live_stage_enable", True))
    alpha_dicts.sort(key=stage_sort_key, reverse=True)

    dealer_rows = []
    for item in alpha_dicts:
        item.update(dealer_dimensions(item))
        item.update(classify_row(item))
        score, flags = dealer_score(item)
        if score <= 0:
            continue
        profile = dealer_profile(item)
        dealer_rows.append({**item, "dealer_score": score, "dealer_flags": flags, **profile})
    dealer_rows.sort(key=lambda row: (row["dealer_score"], *stage_sort_key(row)), reverse=True)
    holder_coverage = holder_coverage_summary(alpha_dicts)
    alpha_buyflow_coverage = buyflow_coverage_summary(alpha_dicts)
    meme_holder_coverage = holder_coverage_summary(meme_rows)
    recommendation_rows = build_recommendation_rows(alpha_dicts, dealer_rows, limit=args.dealer_top)
    # Preserve all candidates before display limits for later chronological replay.
    with (out_dir / "alpha-dealer-observations-v1.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"observed_at": datetime.now().astimezone().isoformat(),
                                 "model": "risk-separated-v1", "rows": dealer_rows}, ensure_ascii=False) + "\n")
    previous_replay_history = load_replay_history(out_dir)
    replay_calibration = replay_action_calibration(previous_replay_history.get("summary") or {})
    meme_chain_scope = getattr(args, "meme_chain_scope", "*")
    meme_potential_rows = build_meme_potential_rows(
        meme_rows,
        limit=getattr(args, "meme_potential_top", 1),
        replay_calibration=replay_calibration,
        chain_scope=meme_chain_scope,
    )
    meme_shadow_rows = build_shadow_meme_rows(
        meme_rows,
        selected_rows=meme_potential_rows,
        limit=getattr(args, "meme_shadow_top", 40),
        replay_calibration=replay_calibration,
        chain_scope=meme_chain_scope,
    )
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    cache_only_meme = os.environ.get("ALPHA_MEME_CACHE_ONLY", "").strip() == "1"
    all_meme_rows = {row_identity(row): row for row in meme_rows}
    for row in [*meme_shadow_rows, *meme_potential_rows]:
        all_meme_rows[row_identity(row)] = row
    replay_rows = recommendation_rows if cache_only_meme else [*recommendation_rows, *all_meme_rows.values()]
    replay_history = update_replay_history(previous_replay_history, replay_rows, generated_at)
    (out_dir / "alpha-radar-replay-history.json").write_text(json.dumps(replay_history, ensure_ascii=False, indent=2), encoding="utf-8")
    attach_replay_rows(recommendation_rows, replay_history)
    attach_replay_rows(meme_potential_rows, replay_history)
    attach_replay_rows(meme_shadow_rows, replay_history)
    gold_watch_state_path = out_dir / "alpha-gold-watch-state.json"
    gold_watch_state = load_watch_state(gold_watch_state_path)
    if not cache_only_meme:
        gold_watch_state = update_watch_state(
            gold_watch_state,
            [*meme_potential_rows, *meme_shadow_rows],
            generated_at,
            min_confirmations=getattr(args, "gold_watch_confirmations", 3),
            chain_scope=meme_chain_scope,
        )
        write_watch_state(gold_watch_state_path, gold_watch_state)
    attach_gold_watch_fields(meme_potential_rows, gold_watch_state)
    attach_gold_watch_fields(meme_shadow_rows, gold_watch_state)
    attach_gold_watch_fields(meme_rows, gold_watch_state)
    meme_heat_rows = build_meme_heat_rows(
        meme_rows,
        limit=max(12, getattr(args, "meme_shadow_top", 40)),
        chain_scope=meme_chain_scope,
    )
    meme_conviction_rows = build_meme_conviction_rows(
        meme_rows,
        limit=max(12, getattr(args, "meme_shadow_top", 40)),
        chain_scope=meme_chain_scope,
    )
    gold_watch_pick = gold_watch_state.get("pick") if isinstance(gold_watch_state.get("pick"), dict) else None
    gold_watch_confirmed_pick = gold_watch_state.get("confirmed_pick") if isinstance(gold_watch_state.get("confirmed_pick"), dict) else None
    gold_watch_early_pick = gold_watch_state.get("early_pick") if isinstance(gold_watch_state.get("early_pick"), dict) else None
    gold_dog_pick = gold_watch_pick
    gold_backtest = build_gold_backtest(replay_history, gold_watch_state, now_iso=generated_at)

    data_quality = alpha.data_quality_summary()
    dex_quality = (data_quality.get("by_source") or {}).get("dexscreener") or {}
    section_quality = {
        "alpha": "partial" if meta.get("errors") else data_quality.get("overall", "unknown"),
        "meme": "cached" if used_previous_meme else ("failed" if meme_errors and not meme_rows else ("partial" if meme_errors else "live")),
        "dealer": "partial" if dex_quality.get("failed") else data_quality.get("overall", "unknown"),
    }

    report = {
        "meta": {
            **meta,
            "data_quality": data_quality,
            "section_quality": section_quality,
            "used_previous_meme": used_previous_meme,
            "meme_errors": meme_errors,
            "meme_source_status": optional_meme_source_status(),
            "tweet_narrative": tweet_narrative,
            "meme_seed_terms": meme_seed_terms,
            "meme_count": len(meme_rows),
            "meme_chain_scope": meme_chain_scope,
            "meme_potential_count": len(meme_potential_rows),
            "meme_shadow_count": len(meme_shadow_rows),
            "meme_heat_count": len(meme_heat_rows),
            "meme_conviction_count": len(meme_conviction_rows),
            "dealer_count": len(dealer_rows),
            "recommendation_count": len(recommendation_rows),
            "replay": replay_history.get("summary") or {},
            "replay_action_calibration": replay_calibration,
            "replay_boards": replay_leaderboards(replay_history),
            "gold_watch": watch_summary(gold_watch_state),
            "gold_backtest": gold_backtest,
            "holder_coverage": holder_coverage,
            "alpha_buyflow_coverage": alpha_buyflow_coverage,
            "meme_holder_coverage": meme_holder_coverage,
            "report_generated_at": generated_at,
        },
        "alpha_rows": alpha_dicts[: args.top],
        "meme_rows": meme_rows[: args.meme_top],
        "meme_watch_universe": meme_rows,
        "gold_dog_pick": gold_dog_pick,
        "gold_watch_pick": gold_watch_pick,
        "gold_watch_confirmed_pick": gold_watch_confirmed_pick,
        "gold_watch_early_pick": gold_watch_early_pick,
        "gold_watch_alerts": (gold_watch_state.get("alerts") or [])[-100:],
        "meme_potential_rows": meme_potential_rows,
        "meme_shadow_rows": meme_shadow_rows,
        "meme_heat_rows": meme_heat_rows,
        "meme_conviction_rows": meme_conviction_rows,
        "dealer_rows": dealer_rows[: args.dealer_top],
        "recommendation_rows": recommendation_rows,
        "funding_rows": sorted(
            [row for row in alpha_dicts if row.get("funding_rate_pct") is not None],
            key=lambda row: alpha.to_float(row.get("funding_rate_pct")),
        )[: args.dealer_top],
        "hot_rows": sorted(
            alpha_dicts,
            key=lambda row: (
                alpha.to_float(row.get("alpha_volume24h")),
                alpha.to_float(row.get("alpha_change24h_pct")),
            ),
            reverse=True,
        )[: args.dealer_top],
        "monitor_v3": monitor_v3,
    }
    refresh_token_intelligence(
        report,
        out_dir=out_dir,
        now=generated_at,
        auxiliary_rows=gold_watch_state.get("candidates") or {},
    )
    return report


def md_money(value: Any) -> str:
    return f"${alpha.short_money(value)}"


def age_days(hours: Any) -> str:
    if hours is None:
        return ""
    return f"{alpha.to_float(hours) / 24:.0f}d"


def write_report(path: Path, report: dict[str, Any]) -> None:
    meta = report["meta"]
    quality = meta.get("data_quality") or {}
    section_quality = meta.get("section_quality") or {}
    lines = [
        "# Alpha / Meme / Dealer Radar",
        "",
        f"- Updated: {meta['report_generated_at']}",
        f"- Alpha candidates: {meta['filtered_count']} | Meme heat candidates: {meta['meme_count']} | Meme potential: {meta['meme_potential_count']} | Dealer candidates: {meta['dealer_count']}",
        f"- Data quality: {quality.get('overall', 'unknown')} | cached={quality.get('cached_count', 0)} | failed={quality.get('failed_count', 0)}",
        f"- Section quality: Meme={section_quality.get('meme', 'unknown')} | Alpha={section_quality.get('alpha', 'unknown')} | Dealer={section_quality.get('dealer', 'unknown')}",
        "",
        "## Dealer Radar",
        "",
        "### Heat List",
    ]
    if meta.get("used_previous_meme"):
        lines.append("- Meme fallback: using previous valid Meme rows because current fetch failed.")

    for row in report["hot_rows"][:8]:
        lines.append(
            f"- {row['symbol']} ~{md_money(row.get('market_cap'))} "
            f"change {alpha.pct(row.get('alpha_change24h_pct'), 0)} | "
            f"volume {md_money(row.get('alpha_volume24h'))} | age {age_days(row.get('pair_age_hours'))}"
        )

    lines.extend(["", "### Funding Short Bias"])
    for row in report["funding_rows"][:8]:
        if alpha.to_float(row.get("funding_rate_pct")) >= 0:
            continue
        lines.append(
            f"- {row['symbol']} funding {alpha.pct(row.get('funding_rate_pct'), 3)} -> "
            f"change {alpha.pct(row.get('futures_price_change24h_pct'), 0)} | {md_money(row.get('market_cap'))}"
        )

    lines.extend(["", "### Composite"])
    for row in report["dealer_rows"][:10]:
        mcap = row.get("market_cap") if "market_cap" in row else row.get("mcap")
        dex_url = row.get("dex_url") or row.get("url") or ""
        symbol = row.get("symbol") or row.get("name") or "?"
        link = f"[{symbol}]({dex_url})" if dex_url else symbol
        lines.append(
            f"- {link} {row['dealer_score']:.0f} pts | {md_money(mcap)} | "
            f"OI {alpha.pct(row.get('oi_change_1h_pct'))} | "
            f"funding {alpha.pct(row.get('funding_rate_pct'), 3)} | "
            f"{' / '.join(row.get('dealer_flags') or [])}"
        )

    lines.extend(["", "## Meme Potential"])
    for idx, row in enumerate(report.get("meme_potential_rows", [])[:15], start=1):
        symbol = row.get("symbol") or row.get("name") or "?"
        link = f"[{symbol}]({row.get('url')})" if row.get("url") else symbol
        lines.append(
            f"- #{idx} {link} "
            f"{row['recommendation_action']} | {row['recommendation_reason']} | "
            f"risk {row['recommendation_risk']} | {row.get('chain') or '--'} | {md_money(row.get('mcap'))}"
        )

    lines.extend(["", "## Meme Heat List"])
    for idx, row in enumerate(report["meme_rows"][:15], start=1):
        lines.append(
            f"- #{idx} [{row['symbol'] or row['name']}]({row['url']}) "
            f"{row['chain']} | {md_money(row['mcap'])} | "
            f"Heat {alpha.to_float(row.get('heat_score')):.1f} | "
            f"5m {alpha.pct(row['change_m5'], 1)} | 1h {alpha.pct(row['change_h1'], 1)} | "
            f"24h volume {md_money(row['volume24h'])} | age {age_days(row['pair_age_hours'])} | "
            f"score {row['score']:.1f} | sources {', '.join(row.get('sources') or [])}"
        )

    lines.extend(["", "## Alpha List"])
    for idx, row in enumerate(report["alpha_rows"][:15], start=1):
        lines.append(
            f"- #{idx} [{row['symbol']}]({row.get('dex_url') or ''}) "
            f"{row['chain']} | score {row['score']:.1f} | {md_money(row['market_cap'])} | "
            f"Alpha volume {md_money(row['alpha_volume24h'])} | OI {alpha.pct(row.get('oi_change_1h_pct'))} | "
            f"funding {alpha.pct(row.get('funding_rate_pct'), 3)}"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "- Dealer radar combines proxy signals and connected Top10/Top20 holder data where available.",
            "- Unsupported chains stay marked as missing holder coverage until more adapters are added.",
            "- This is read-only market data output. No trading or account endpoints are used.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alpha + Meme + dealer proxy radar report.")
    parser.add_argument("--alpha-limit", type=int, default=120)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--meme-top", type=int, default=20)
    parser.add_argument("--meme-potential-top", type=int, default=1)
    parser.add_argument("--meme-shadow-top", type=int, default=40)
    parser.add_argument("--meme-chain-scope", default="bsc", choices=["bsc", "*", "all"])
    parser.add_argument("--gold-watch-confirmations", type=int, default=3)
    parser.add_argument("--dealer-top", type=int, default=20)
    parser.add_argument("--meme-source-limit", type=int, default=60)
    parser.add_argument("--max-market-cap", type=float, default=200_000_000)
    parser.add_argument("--min-volume", type=float, default=1_000_000)
    parser.add_argument("--chains", default="")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--holders-enable", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--holder-provider", default="goplus", choices=["routescan", "etherscan", "blockscout", "bscscan", "solana_rpc", "auto", "goplus"])
    parser.add_argument("--holder-top-tokens", type=int, default=120)
    parser.add_argument("--holder-offset", type=int, default=20)
    parser.add_argument("--risk-enable", action="store_true")
    parser.add_argument("--risk-top-tokens", type=int, default=12)
    parser.add_argument("--live-stage-disable", dest="live_stage_enable", action="store_false")
    parser.add_argument("--out-dir", default="outputs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args)
    latest_md = out_dir / "alpha-radar-report-latest.md"
    latest_json = out_dir / "alpha-radar-report-latest.json"
    report = publish_latest_report(latest_json, report, out_dir=out_dir)
    write_report(latest_md, report)
    latest_dashboard = out_dir / "alpha-radar-dashboard.html"
    latest_dashboard.write_text(alpha_radar_dashboard.render(report), encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"alpha-radar-report-{stamp}.md").write_text(latest_md.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Wrote {latest_md}", file=sys.stderr)
    print(f"Wrote {latest_json}", file=sys.stderr)
    print(f"Wrote {latest_dashboard}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
