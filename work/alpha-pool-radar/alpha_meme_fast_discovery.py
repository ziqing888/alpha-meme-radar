"""Low-latency multi-source MEME monitor snapshot publisher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import alpha_985_monitor_export
import alpha_okx_market
from alpha_arc_public_chain_poll import (
    DEFAULT_ARCSCAN_BASE_URL,
    DEFAULT_RPC_URLS as DEFAULT_ARC_RPC_URLS,
    run_once as run_arc_public_chain_once,
)
from alpha_gmgn_skills import refresh_fast_hot_search as _refresh_gmgn_hot_search
from alpha_discovery_rank import compute_discovery_rank
from alpha_fast_track import (
    EXECUTION_CHAINS,
    apply_quote,
    annotate_new_execution_candidates,
    build_bsc_execution_input,
    build_live_replay_with_sightings,
    build_robinhood_execution_input,
    fetch_pairs,
    key_of,
    merge_market_quotes,
    monitor_safe_cached_quote,
    quote_from_report_row,
    quote_from_pairs,
    report_rows as fast_report_rows,
    update_live_first_sightings,
)
from alpha_gmgn_quote import GmgnRateLimitError, fetch_gmgn_token_info
from alpha_chain_strategy import StrategyPolicy
from alpha_gold_watch import load_watch_state, update_watch_state
from alpha_meme import (
    DEFAULT_MEME_CHAINS,
    load_local_inbox_sources,
    load_meme_candidates,
    load_meme_observation_batch,
)
from alpha_meme_live_refresh import (
    atomic_write_json,
    now_iso,
    read_json,
    refresh_bsc_public_pairs,
    refresh_noxa_launchpad,
    refresh_proficy_trending,
    refresh_wind_monitor,
)
from alpha_meme_potential import (
    MAX_NEW_POOL_CANDIDATE_AGE_HOURS,
    age_missing,
    annotate_meme_early_conviction,
    annotate_meme_heat,
    build_meme_potential_rows,
    classify_meme_row,
    to_float,
)
from alpha_monitor_v3 import (
    build_monitor_baselines,
    build_monitor_snapshot_baselines,
    compact_monitor_snapshot,
    update_monitor_state,
)
from alpha_monitor_outcomes import update_monitor_outcomes
from alpha_monitor_chains import normalize_monitor_chain
from alpha_radar_report import (
    attach_gold_watch_fields,
    build_shadow_meme_rows,
    load_replay_history,
    monitor_source_health,
    schedule_token_intelligence_research,
)
from alpha_replay import replay_action_calibration
from alpha_smart_money_evidence import enrich_smart_money


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
REPORT_PATH = OUT_DIR / "alpha-radar-report-latest.json"
FAST_SNAPSHOT_NAME = "alpha-meme-fast-latest.json"
MONITOR_QUOTE_CACHE_NAME = "alpha-monitor-quote-cache.json"
STATUS_PATH = OUT_DIR / "alpha-meme-fast-discovery-status.json"
GMGN_HOLDER_MIN_INTERVAL_SECONDS = 15
GMGN_HOLDER_REQUESTS_PER_CHAIN_CYCLE = 1
LIVE_CANDIDATE_LIMIT_PER_CHAIN = 8
LIVE_WATCH_STATUSES = {"candidate", "early_candidate", "seed_pool", "strong_candidate"}
TERMINAL_WATCH_STATUSES = {"expired", "invalidated", "rejected"}
BLOCKED_EXECUTION_WATCH_STATUSES = {
    *TERMINAL_WATCH_STATUSES,
    "pullback",
    "deteriorating",
    "achieved_gold",
}
FAST_SOURCE_TOTAL_LIMIT = 30
FAST_SOURCE_PER_CHAIN_LIMIT = 18
FAST_RECENT_RESERVE_PER_CHAIN = 6
OKX_MARKET_FAST_MIN_INTERVAL_SECONDS = 20
OKX_MARKET_FAST_LIMIT = 20
FAST_SOURCE_RATE_LIMIT_BACKOFF_SECONDS = 120
FAST_SOURCE_MIN_INTERVAL_SECONDS = {
    "arc_onchain": 2,
    "onchain": 5,
    "noxa": 20,
    "proficy": 15,
    "985": 5,
    "wind": 30,
    "gmgn": 0,
    "okx": 0,
}
FAST_PROVIDER_RESERVE_ORDER = (
    "onchain",
    "gmgn",
    "okx",
    "985",
    "wind",
    "noxa",
    "proficy",
    "debot",
)
_SOURCE_REFRESH_LOCK = threading.Lock()
_SOURCE_REFRESH_STATE: dict[str, dict[str, Any]] = {}
_FAST_SOURCE_CADENCE_LOCK = threading.Lock()
_FAST_SOURCE_CADENCE_STATE: dict[str, dict[str, Any]] = {}
_MONITOR_QUOTE_LOCK = threading.Lock()
_MONITOR_QUOTE_STATE: dict[str, dict[str, Any]] = {}
_MONITOR_INTELLIGENCE_LOCK = threading.Lock()
_MONITOR_INTELLIGENCE_STATE: dict[str, float] = {}


def configure_fast_source_environment() -> None:
    """Use provider market snapshots; downstream quote only selected candidates."""
    os.environ["MEME_SKIP_DEX_ENRICH"] = "1"
    os.environ["MEME_BATCH_PAIRS"] = "1"
    os.environ["MEME_PAIR_TOTAL_TIMEOUT_SECONDS"] = "6"


def refresh_gmgn_hot_search(out_dir: Path) -> dict[str, Any]:
    chains = [item.strip().lower() for item in DEFAULT_MEME_CHAINS.split(",") if item.strip()]
    return _refresh_gmgn_hot_search(out_dir, chains=chains)


def refresh_arc_public_chain(out_dir: Path) -> dict[str, object]:
    status = run_arc_public_chain_once(
        out_dir=out_dir,
        rpc_urls=list(DEFAULT_ARC_RPC_URLS),
        arcscan_base_url=DEFAULT_ARCSCAN_BASE_URL,
    )
    return {"source": "arc_onchain", **status}


def _fast_source_priority(item: dict[str, Any]) -> tuple[Any, ...]:
    sources = {
        str(source).strip().lower()
        for source in (item.get("sources") or [])
        if str(source).strip()
    }
    profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
    discovery_source = any(
        marker in source
        for source in sources
        for marker in ("trenches", "launch", "onchain", "pair_created", "memepump")
    )
    age = to_float(profile.get("pair_age_hours"))
    rank = max(
        to_float(profile.get("rank_score")),
        to_float(profile.get("score")),
        to_float(profile.get("gmgn_score")),
        to_float(profile.get("smart_money")) * 2,
        to_float(profile.get("kol")),
    )
    return (
        -len(sources),
        -int(discovery_source),
        age if age > 0 else float("inf"),
        -rank,
        str(item.get("chainId") or ""),
        str(item.get("tokenAddress") or ""),
    )


def _fast_source_observed_epoch(item: dict[str, Any]) -> float:
    profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
    values: list[Any] = [
        profile.get("observed_at"),
        profile.get("quote_observed_at"),
        item.get("observed_at"),
    ]
    for event in item.get("source_events") or []:
        if isinstance(event, dict):
            values.extend((event.get("observed_at"), event.get("event_at")))
    epochs: list[float] = []
    for value in values:
        if value in (None, ""):
            continue
        try:
            if isinstance(value, (int, float)) or str(value).strip().isdigit():
                epoch = float(value)
                if epoch > 1e11:
                    epoch /= 1000
            else:
                epoch = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if epoch >= 0:
            epochs.append(epoch)
    return max(epochs, default=0.0)


def _timestamp_epoch(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            epoch = float(value)
            return epoch / 1000 if epoch > 1e11 else epoch
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _fast_provider_buckets(item: dict[str, Any]) -> set[str]:
    buckets: set[str] = set()
    for raw_source in item.get("sources") or []:
        source = str(raw_source).strip().lower()
        if source.startswith("gmgn_"):
            buckets.add("gmgn")
        elif source.startswith("okx_"):
            buckets.add("okx")
        elif source.startswith("985_"):
            buckets.add("985")
        elif source.startswith("wind_") or source == "tingfeng_monitor":
            buckets.add("wind")
        elif source.startswith("noxa_"):
            buckets.add("noxa")
        elif source.startswith("proficy_"):
            buckets.add("proficy")
        elif source.startswith("debot_"):
            buckets.add("debot")
        elif "onchain" in source or "pair" in source:
            buckets.add("onchain")
    return buckets


def load_fast_local_sources(
    limit: int,
    *,
    total_limit: int = FAST_SOURCE_TOTAL_LIMIT,
    per_chain_limit: int = FAST_SOURCE_PER_CHAIN_LIMIT,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Select a bounded, chain-balanced quote universe from all current inboxes."""
    sources, errors = load_local_inbox_sources(limit)
    ordered = sorted(sources.items(), key=lambda item: _fast_source_priority(item[1]))
    selected: dict[str, dict[str, Any]] = {}
    chain_counts: dict[str, int] = {}
    recent_counts: dict[str, int] = {}
    deferred: list[tuple[str, dict[str, Any]]] = []
    chains = sorted({str(item.get("chainId") or "").strip().lower() for item in sources.values()} - {""})
    provider_ranked = sorted(
        sources.items(),
        key=lambda item: (-_fast_source_observed_epoch(item[1]), _fast_source_priority(item[1])),
    )
    for chain in chains:
        for provider in FAST_PROVIDER_RESERVE_ORDER:
            match = next(
                (
                    (key, item)
                    for key, item in provider_ranked
                    if key not in selected
                    and str(item.get("chainId") or "").strip().lower() == chain
                    and provider in _fast_provider_buckets(item)
                ),
                None,
            )
            if match is None or chain_counts.get(chain, 0) >= per_chain_limit:
                continue
            key, item = match
            selected[key] = item
            chain_counts[chain] = chain_counts.get(chain, 0) + 1
            if len(selected) >= total_limit:
                return selected, errors
    recent = sorted(
        sources.items(),
        key=lambda item: (-_fast_source_observed_epoch(item[1]), _fast_source_priority(item[1])),
    )
    for key, item in recent:
        if key in selected:
            continue
        if _fast_source_observed_epoch(item) <= 0:
            continue
        chain = str(item.get("chainId") or "").strip().lower()
        if recent_counts.get(chain, 0) >= min(FAST_RECENT_RESERVE_PER_CHAIN, per_chain_limit):
            continue
        selected[key] = item
        recent_counts[chain] = recent_counts.get(chain, 0) + 1
        chain_counts[chain] = chain_counts.get(chain, 0) + 1
        if len(selected) >= total_limit:
            return selected, errors
    for key, item in ordered:
        if key in selected:
            continue
        chain = str(item.get("chainId") or "").strip().lower()
        if chain_counts.get(chain, 0) >= per_chain_limit:
            deferred.append((key, item))
            continue
        selected[key] = item
        chain_counts[chain] = chain_counts.get(chain, 0) + 1
        if len(selected) >= total_limit:
            break
    if len(selected) < total_limit:
        for key, item in deferred:
            if key in selected:
                continue
            selected[key] = item
            if len(selected) >= total_limit:
                break
    return selected, errors


def publish_fast_monitor_snapshot(
    *,
    out_dir: Path,
    batch: dict[str, Any],
    observed_at: str,
    previous_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Publish the UI snapshot without waiting for the full-history monitor lock."""
    sidecar_path = out_dir / "alpha-meme-monitor-v3-latest.json"
    state_path = out_dir / "alpha-meme-monitor-v3-fast-state.json"
    previous = read_json(state_path) or read_json(sidecar_path) or previous_snapshot
    snapshot = update_monitor_state(
        previous,
        events=batch.get("events") or [],
        candidates=batch.get("candidates") or [],
        rejections=batch.get("rejections") or [],
        source_health=monitor_source_health(batch, observed_at),
        observed_at=observed_at,
    )
    continuity = compact_monitor_snapshot(snapshot, include_stale=True, token_limit=240)
    atomic_write_json(state_path, continuity)
    compact = compact_monitor_snapshot(snapshot)
    atomic_write_json(sidecar_path, compact)
    return compact


def live_potential_eligible(row: dict[str, Any], *, evaluated_at: str | None = None) -> bool:
    """Keep non-entry states and late discoveries out of scarce live slots."""
    status = str(row.get("watch_status") or "").strip().lower()
    if status and status not in LIVE_WATCH_STATUSES:
        return False
    first_seen_at = row.get("watch_first_seen_at") or row.get("first_seen_at")
    if not first_seen_at:
        return True
    chain = str(row.get("chain") or row.get("chain_id") or "").strip().lower()
    try:
        first_seen = datetime.fromisoformat(str(first_seen_at).replace("Z", "+00:00"))
        current = datetime.fromisoformat(str(evaluated_at).replace("Z", "+00:00")) if evaluated_at else datetime.now(timezone.utc)
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age_seconds = (current.astimezone(timezone.utc) - first_seen.astimezone(timezone.utc)).total_seconds()
        return 0 <= age_seconds <= StrategyPolicy.for_chain(chain).max_entry_delay_seconds
    except (TypeError, ValueError):
        return False


def per_chain_potential_rows(
    rows: list[dict[str, Any]],
    *,
    replay_calibration: dict[str, Any],
    limit_per_chain: int = LIVE_CANDIDATE_LIMIT_PER_CHAIN,
    evaluated_at: str | None = None,
) -> list[dict[str, Any]]:
    """Reserve enough ranked candidates per chain for the live gates to evaluate."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not live_potential_eligible(row, evaluated_at=evaluated_at):
            continue
        chain = normalize_monitor_chain(row.get("chain") or row.get("chain_id"))
        if chain:
            grouped.setdefault(chain, []).append(row)
    selected: list[dict[str, Any]] = []
    for chain_rows in grouped.values():
        selected.extend(build_meme_potential_rows(
            chain_rows,
            limit=limit_per_chain,
            replay_calibration=replay_calibration,
        ))
    return selected


def build_pending_quote_rows(
    rows: list[dict[str, Any]], *, limit: int = 16
) -> list[dict[str, Any]]:
    """Retain recent multi-source candidates while their market quote is pending."""
    pending: list[dict[str, Any]] = []
    for raw in rows:
        row = {**raw, **classify_meme_row(raw)}
        if raw.get("recommendation_bucket") == "reject" or row.get("recommendation_bucket") == "reject":
            continue
        if str(row.get("watch_status") or "").strip().lower() in TERMINAL_WATCH_STATUSES:
            continue
        if age_missing(row):
            continue
        if to_float(row.get("pair_age_hours")) > MAX_NEW_POOL_CANDIDATE_AGE_HOURS:
            continue
        quote_status = str(row.get("quote_status") or "").strip().lower()
        if quote_status == "fresh":
            continue
        sources = {
            str(source).strip().lower()
            for source in (row.get("sources") or [])
            if str(source).strip()
        }
        source_count = max(int(to_float(row.get("source_count"))), len(sources))
        if source_count < 2:
            continue
        pending.append(
            {
                **row,
                "selection_state": "pending_quote",
                "selection_reason": "多源共振，等待新鲜报价",
            }
        )
    pending.sort(
        key=lambda row: (
            -to_float(row.get("rank_score")),
            -to_float(row.get("source_count")),
            to_float(row.get("pair_age_hours")),
            str(row.get("symbol") or ""),
        )
    )
    return pending[: max(0, limit)]


def monitor_quote_enrichment_targets(
    rows: list[dict[str, Any]],
    monitor_snapshot: dict[str, Any],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Select only fresh multi-source tokens whose market gaps can be enriched."""
    rows_by_key = {key_of(row): row for row in rows if key_of(row)}
    targets: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for token in monitor_snapshot.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        row = rows_by_key.get(str(token.get("id") or ""))
        if row is None:
            continue
        resonance = token.get("resonance") if isinstance(token.get("resonance"), dict) else {}
        if not resonance.get("subtype") or len(resonance.get("provider_families") or []) < 2:
            continue
        gate = resonance.get("confirmation_gate") if isinstance(resonance.get("confirmation_gate"), dict) else {}
        failures = {str(value) for value in gate.get("failures") or []}
        if "discovery_window_expired" in failures:
            continue
        risk = token.get("risk") if isinstance(token.get("risk"), dict) else {}
        axes = token.get("ranking_axes") if isinstance(token.get("ranking_axes"), dict) else {}
        behavior = axes.get("market_behavior") if isinstance(axes.get("market_behavior"), dict) else {}
        timing = axes.get("timing") if isinstance(axes.get("timing"), dict) else {}
        if risk.get("hard_blocked") or behavior.get("disposition") == "observe":
            continue
        latest_event_age = to_float(timing.get("latest_event_age_seconds"))
        if latest_event_age > 30 * 60:
            continue
        states = {str(value) for value in token.get("active_states") or []}
        priority = (
            -int("resonating" in states),
            -int("building" in states or "smart_cluster" in states),
            latest_event_age,
            -len(resonance.get("provider_families") or []),
            str(row.get("symbol") or ""),
        )
        targets.append((priority, row))
    return [row for _, row in sorted(targets, key=lambda item: item[0])[: max(0, limit)]]


def apply_monitor_quote_cache(
    rows: list[dict[str, Any]], cache: dict[str, Any], observed_at: str
) -> list[dict[str, Any]]:
    quotes = cache.get("quotes") if isinstance(cache.get("quotes"), dict) else {}
    return [
        apply_quote(
            row,
            monitor_safe_cached_quote(row, quotes[key_of(row)]),
            observed_at,
        )
        if key_of(row) in quotes
        else row
        for row in rows
    ]


def refresh_monitor_quotes(
    out_dir: Path,
    targets: list[dict[str, Any]],
    observed_at: str,
    *,
    fetcher=fetch_pairs,
    gmgn_fetcher=fetch_gmgn_token_info,
) -> dict[str, Any]:
    """Fetch market and holder evidence only for monitor-selected resonance targets."""
    cache_path = Path(out_dir) / MONITOR_QUOTE_CACHE_NAME
    previous_payload = read_json(cache_path)
    previous_quotes = previous_payload.get("quotes") if isinstance(previous_payload.get("quotes"), dict) else {}
    gmgn_retry_after_epoch = to_float(previous_payload.get("gmgn_retry_after_epoch"))
    gmgn_cooldown = gmgn_retry_after_epoch > time.time()
    observed_epoch = _timestamp_epoch(observed_at) or time.time()
    quotes = {
        key: monitor_safe_cached_quote(
            {"chain": str(key).partition(":")[0]},
            quote,
        )
        for key, quote in previous_quotes.items()
        if isinstance(quote, dict)
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in targets:
        chain = normalize_monitor_chain(row.get("chain") or row.get("chain_id"))
        address = str(row.get("contract_address") or row.get("token_address") or "").strip().lower()
        if chain and address:
            grouped.setdefault(chain, []).append(row)
    errors: list[str] = []
    fresh_count = 0
    fresh_holder_count = 0
    for chain, chain_rows in grouped.items():
        try:
            pairs = fetcher(chain, [str(row.get("contract_address") or row.get("token_address")) for row in chain_rows])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{chain}: {type(exc).__name__}: {exc}")
            continue

        due_gmgn_rows = []
        if chain != "arc" and not gmgn_cooldown:
            for row in chain_rows:
                previous = monitor_safe_cached_quote(
                    row, previous_quotes.get(key_of(row)) or {}
                )
                holder_observed_epoch = _timestamp_epoch(previous.get("holder_observed_at"))
                holder_missing = previous.get("holder_count") in (None, 0)
                if holder_missing or observed_epoch - holder_observed_epoch >= GMGN_HOLDER_MIN_INTERVAL_SECONDS:
                    due_gmgn_rows.append((not holder_missing, holder_observed_epoch, row))
            due_gmgn_rows.sort(key=lambda item: (item[0], item[1], key_of(item[2])))

        gmgn_quotes: dict[str, dict[str, Any]] = {}
        for _, _, row in due_gmgn_rows[:GMGN_HOLDER_REQUESTS_PER_CHAIN_CYCLE]:
            key = key_of(row)
            try:
                gmgn_quotes[key] = gmgn_fetcher(
                    chain,
                    str(row.get("contract_address") or row.get("token_address")),
                    timeout_seconds=6,
                )
            except GmgnRateLimitError as exc:
                gmgn_retry_after_epoch = max(gmgn_retry_after_epoch, exc.retry_after_epoch)
                gmgn_cooldown = True
                errors.append(f"{chain}: gmgn_rate_limited_until_{int(exc.retry_after_epoch)}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{chain}: gmgn_{type(exc).__name__}: {exc}")

        for row in chain_rows:
            key = key_of(row)
            previous = monitor_safe_cached_quote(row, previous_quotes.get(key) or {})
            dex_quote = quote_from_pairs(row, pairs, previous, observed_at)
            gmgn_quote = gmgn_quotes.get(key) or {}
            quote = monitor_safe_cached_quote(
                row,
                merge_market_quotes(gmgn_quote, dex_quote, previous, observed_at),
            )
            if chain == "arc":
                quote["gmgn_holder_status"] = "provider_unavailable"
            if gmgn_quote.get("quote_status") == "fresh":
                holder_observed_at = gmgn_quote.get("gmgn_observed_at") or gmgn_quote.get("quote_observed_at")
                holder_count = gmgn_quote.get("holder_count")
                top10_holder_pct = gmgn_quote.get("top10_holder_pct")
                if holder_count not in (None, 0) or top10_holder_pct not in (None, 0):
                    if holder_count not in (None, 0):
                        quote["holder_count"] = holder_count
                        quote["holders"] = holder_count
                    if top10_holder_pct not in (None, 0):
                        quote["top10_holder_pct"] = top10_holder_pct
                    quote["holder_source"] = "gmgn"
                    quote["holder_observed_at"] = holder_observed_at
                    fresh_holder_count += 1
            elif chain != "arc":
                for field in ("holders", "holder_count", "top10_holder_pct", "holder_source", "holder_observed_at"):
                    if previous.get(field) is not None:
                        quote[field] = previous[field]
            quotes[key] = quote
            fresh_count += int(quote.get("quote_status") == "fresh")
    ordered = sorted(
        quotes.items(),
        key=lambda item: str(item[1].get("quote_observed_at") or ""),
        reverse=True,
    )[:120]
    payload = {"updated_at": observed_at, "quotes": dict(ordered), "errors": errors}
    if gmgn_retry_after_epoch > time.time():
        payload["gmgn_retry_after_epoch"] = gmgn_retry_after_epoch
    atomic_write_json(cache_path, payload)
    return {
        "ok": not errors,
        "updated_at": observed_at,
        "target_count": len(targets),
        "fresh_count": fresh_count,
        "fresh_holder_count": fresh_holder_count,
        "gmgn_cooldown": gmgn_retry_after_epoch > time.time(),
        "gmgn_retry_after_epoch": gmgn_retry_after_epoch or None,
        "errors": errors,
    }


def schedule_monitor_quote_enrichment(
    out_dir: Path,
    targets: list[dict[str, Any]],
    observed_at: str,
    *,
    refresher=refresh_monitor_quotes,
    min_interval_seconds: float = 3.0,
) -> dict[str, Any]:
    if not targets:
        return {"scheduled": False, "reason": "no_fresh_resonance", "target_count": 0}
    key = str(Path(out_dir).resolve())
    now = time.monotonic()
    with _MONITOR_QUOTE_LOCK:
        state = _MONITOR_QUOTE_STATE.setdefault(key, {"started_at": 0.0, "thread": None, "result": {}})
        thread = state.get("thread")
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return {**state["result"], "scheduled": False, "running": True, "target_count": len(targets)}
        if state["started_at"] and now - float(state["started_at"]) < max(0.0, min_interval_seconds):
            return {**state["result"], "scheduled": False, "reason": "min_interval", "target_count": len(targets)}

        def worker() -> None:
            try:
                result = refresher(Path(out_dir), targets, observed_at)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"]}
            with _MONITOR_QUOTE_LOCK:
                current = _MONITOR_QUOTE_STATE.get(key)
                if current is not None:
                    current["result"] = result

        thread = threading.Thread(target=worker, name="alpha-monitor-quote-enrichment", daemon=True)
        state["started_at"] = now
        state["thread"] = thread
        thread.start()
        return {"scheduled": True, "running": True, "target_count": len(targets)}


def monitor_intelligence_report(monitor_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Limit paid AI research to live early, confirmed, and smart-wallet selections."""
    selected_states = {"building", "resonating", "smart_cluster"}
    tokens = []
    intelligence_rows = []
    for token in monitor_snapshot.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        active = {str(value) for value in token.get("active_states") or []}
        freshness = token.get("freshness") if isinstance(token.get("freshness"), dict) else {}
        if (
            freshness.get("status") == "fresh"
            and active.intersection(selected_states)
            and not active.intersection({"blocked_risk", "trend_watch", "stale"})
        ):
            compact = dict(token)
            compact["events"] = list(token.get("events") or [])[-16:]
            compact["audit_facts"] = list(token.get("audit_facts") or [])[-24:]
            market = token.get("market") if isinstance(token.get("market"), dict) else {}
            compact["market"] = {**market, "snapshots": list(market.get("snapshots") or [])[-6:]}
            tokens.append(compact)
            identity = token.get("identity") if isinstance(token.get("identity"), dict) else {}
            resonance = token.get("resonance") if isinstance(token.get("resonance"), dict) else {}
            risk = token.get("risk") if isinstance(token.get("risk"), dict) else {}
            wallet = token.get("wallet_evidence") if isinstance(token.get("wallet_evidence"), dict) else {}
            providers = [str(value) for value in resonance.get("provider_families") or []]
            holder_sources = market.get("field_sources") if isinstance(market.get("field_sources"), dict) else {}
            quote_source = holder_sources.get("holders") or ("gmgn" if "gmgn" in providers else providers[0] if providers else None)
            intelligence_rows.append({
                "chain": identity.get("chain") or str(token.get("id") or "").partition(":")[0],
                "contract_address": identity.get("contract_address") or str(token.get("id") or "").partition(":")[2],
                "symbol": identity.get("symbol"),
                "name": identity.get("name"),
                "market_cap": market.get("market_cap_usd"),
                "liquidity": market.get("liquidity_usd"),
                "volume24h": market.get("volume_24h_usd"),
                "holders": market.get("holders"),
                "top10_holder_pct": market.get("top10_holder_pct"),
                "quote_observed_at": market.get("observed_at"),
                "quote_source": quote_source,
                "provider_feed": "gmgn_token_info" if quote_source == "gmgn" else "dexscreener_token_detail",
                "observed_at": market.get("observed_at"),
                "source_labels": providers,
                "source_count": len(providers),
                "risk_flags": [*risk.get("hard_failures", []), *risk.get("soft_flags", [])],
                "smart_money_evidence": {
                    "qualified_wallet_count": wallet.get("verified_buyers"),
                    "candidate_buy_wallet_count": wallet.get("candidate_buyers"),
                    "sources": providers,
                    "observed_at": market.get("observed_at"),
                },
            })
    return {
        "token_intelligence_selected_only": True,
        "monitor_selected_rows": intelligence_rows,
        "monitor_v3": {
            "schema_version": monitor_snapshot.get("schema_version"),
            "observed_at": monitor_snapshot.get("observed_at"),
            "tokens": tokens,
        }
    }


def schedule_monitor_intelligence(
    out_dir: Path,
    monitor_snapshot: dict[str, Any],
    observed_at: str,
    *,
    scheduler=schedule_token_intelligence_research,
    min_interval_seconds: float = 12.0,
) -> dict[str, Any]:
    report = monitor_intelligence_report(monitor_snapshot)
    target_count = len(report["monitor_v3"]["tokens"])
    if not target_count:
        return {"scheduled": False, "reason": "no_selected_tokens", "target_count": 0}
    key = str(Path(out_dir).resolve())
    now = time.monotonic()
    with _MONITOR_INTELLIGENCE_LOCK:
        started_at = _MONITOR_INTELLIGENCE_STATE.get(key, 0.0)
        if started_at and now - started_at < max(0.0, min_interval_seconds):
            return {"scheduled": False, "reason": "min_interval", "target_count": target_count}
        scheduled = scheduler(report, out_dir=Path(out_dir), now=observed_at)
        if scheduled:
            _MONITOR_INTELLIGENCE_STATE[key] = now
    return {"scheduled": bool(scheduled), "target_count": target_count}


def publish_execution_inputs_from_fast_snapshot(
    *,
    out_dir: Path,
    potential: list[dict[str, Any]],
    replay: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    """Feed the live executors from the selected fast candidates, not the slow UI report."""
    rows = fast_report_rows({"meme_potential_rows": potential})
    execution_rows = [
        row for row in rows
        if normalize_monitor_chain(row.get("chain") or row.get("chain_id")) in EXECUTION_CHAINS
    ]
    if not execution_rows:
        return {
            "updated_at": generated_at,
            "published": False,
            "candidate_count": 0,
            "quote_count": 0,
            "bsc_signal_count": 0,
            "bsc_preflight_count": 0,
            "robinhood_signal_count": 0,
            "robinhood_preflight_count": 0,
        }
    sighting_path = out_dir / "alpha-live-first-sightings.json"
    sightings = update_live_first_sightings(read_json(sighting_path), execution_rows, replay, generated_at)
    atomic_write_json(sighting_path, sightings)
    live_replay = build_live_replay_with_sightings(replay, sightings, execution_rows, generated_at)
    annotated = annotate_new_execution_candidates(
        execution_rows,
        out_dir,
        generated_at,
        replay_history=live_replay,
    )
    quotes: dict[str, dict[str, Any]] = {}
    for row in execution_rows:
        quote = quote_from_report_row(row, generated_at)
        if quote:
            quotes[key_of(row)] = quote
    bsc_input = build_bsc_execution_input(annotated, quotes, generated_at)
    robinhood_input = build_robinhood_execution_input(annotated, quotes, generated_at)
    atomic_write_json(out_dir / "bsc-execution-input.json", bsc_input)
    atomic_write_json(out_dir / "robinhood-execution-input.json", robinhood_input)
    return {
        "updated_at": generated_at,
        "published": True,
        "candidate_count": len(execution_rows),
        "quote_count": len(quotes),
        "bsc_signal_count": len(bsc_input.get("signals") or []),
        "bsc_preflight_count": len(bsc_input.get("preflight_signals") or []),
        "robinhood_signal_count": len(robinhood_input.get("signals") or []),
        "robinhood_preflight_count": len(robinhood_input.get("preflight_signals") or []),
    }


def fast_execution_candidates(
    *groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep first-look selected rows for executors while excluding blocked watch states."""
    selected: dict[str, dict[str, Any]] = {}
    for group in groups:
        for row in group:
            key = ""
            chain = str(row.get("chain") or row.get("chain_id") or "").strip().lower()
            address = str(row.get("contract_address") or row.get("token_address") or "").strip().lower()
            if chain and address:
                key = f"{chain}:{address}"
            if not key:
                continue
            status = str(row.get("watch_status") or "").strip().lower()
            if status in BLOCKED_EXECUTION_WATCH_STATUSES:
                continue
            selected[key] = row
    return list(selected.values())


def monitor_selected_execution_candidates(
    rows: list[dict[str, Any]], monitor_snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """Pass only monitor-promoted early-bird and confirmation rows to execution."""
    tokens = {
        str(token.get("id") or ""): token
        for token in monitor_snapshot.get("tokens") or []
        if isinstance(token, dict)
    }
    selected: list[dict[str, Any]] = []
    for row in rows:
        token = tokens.get(key_of(row))
        if not isinstance(token, dict):
            continue
        states = {str(item) for item in token.get("active_states") or []}
        axes = token.get("ranking_axes") if isinstance(token.get("ranking_axes"), dict) else {}
        behavior = axes.get("market_behavior") if isinstance(axes.get("market_behavior"), dict) else {}
        if behavior.get("disposition") == "observe" or states.intersection({"trend_watch", "blocked_risk"}):
            continue
        if "resonating" in states:
            stage = "aggregate_confirmation"
        elif states.intersection({"building", "smart_cluster"}):
            stage = "aggregate_early_bird"
        else:
            continue
        selected.append({**row, "signal_stage": stage})
    return selected


def _rank(rows: list[dict[str, Any]], replay: dict[str, Any], now: str) -> None:
    from datetime import datetime

    evaluated_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
    history_rows = replay.get("rows") if isinstance(replay.get("rows"), dict) else {}
    for row in rows:
        chain = str(row.get("chain") or row.get("chain_id") or "").lower()
        address = str(row.get("contract_address") or row.get("token_address") or "").lower()
        history = history_rows.get(f"{chain}:{address}") if isinstance(history_rows, dict) else None
        snapshot = history.get("first_snapshot") if isinstance(history, dict) else None
        row.update(compute_discovery_rank(row, first_snapshot=snapshot, now=evaluated_at))


def refresh_okx_market(out_dir: Path) -> dict[str, Any]:
    """Refresh the read-only OKX discovery feed on its own bounded cadence."""
    out_dir = Path(out_dir)
    status_path = out_dir / "okx-market-fast-status.json"
    previous = read_json(status_path)
    now_epoch = time.time()
    previous_checked = _timestamp_epoch(previous.get("checked_at"))
    if (
        previous.get("ok") is True
        and previous_checked > 0
        and now_epoch - previous_checked < OKX_MARKET_FAST_MIN_INTERVAL_SECONDS
    ):
        return {**previous, "skipped": True, "reason": "min_interval"}

    checked_at = now_iso()
    if not alpha_okx_market.status().get("configured"):
        status = {
            "source": "okx_market_fast",
            "ok": False,
            "checked_at": checked_at,
            "row_count": 0,
            "reason": "missing_market_api_credentials",
        }
        atomic_write_json(status_path, status)
        return status

    chains = {
        value.strip().lower()
        for value in os.environ.get("ALPHA_MEME_CHAINS", DEFAULT_MEME_CHAINS).split(",")
        if value.strip().lower() in {"bsc", "robinhood"}
    }
    try:
        rows, errors = alpha_okx_market.fetch(chains, OKX_MARKET_FAST_LIMIT)
        alpha_okx_market.persist_snapshot(out_dir / "meme-source-inbox", rows, errors)
    except Exception as exc:  # noqa: BLE001
        status = {
            "source": "okx_market_fast",
            "ok": False,
            "checked_at": checked_at,
            "row_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        atomic_write_json(status_path, status)
        return status
    status = {
        "source": "okx_market_fast",
        "ok": not errors,
        "partial": bool(rows and errors),
        "checked_at": checked_at,
        "row_count": len(rows),
        "errors": list(errors),
    }
    if errors:
        status["error"] = "; ".join(errors)
    atomic_write_json(status_path, status)
    return status


def _source_error_text(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [
            text
            for child in value.values()
            for text in _source_error_text(child)
        ]
    if isinstance(value, (list, tuple, set)):
        return [text for child in value for text in _source_error_text(child)]
    return [str(value)] if value not in (None, "") else []


def _source_result_is_rate_limited(result: dict[str, Any]) -> bool:
    details = " ".join(
        text
        for field in (
            "error",
            "errors",
            "reason",
            "feed_error",
            "rpc_errors",
            "arcscan_errors",
        )
        for text in _source_error_text(result.get(field))
    ).lower()
    return "429" in details or "rate_limit" in details or "rate limit" in details


def refresh_fast_source_on_cadence(
    out_dir: Path,
    *,
    source: str,
    refresher,
    min_interval_seconds: float,
    rate_limit_backoff_seconds: float = FAST_SOURCE_RATE_LIMIT_BACKOFF_SECONDS,
    now_monotonic: float | None = None,
) -> dict[str, Any]:
    """Refresh one provider independently and reuse its latest bounded result."""
    now = time.monotonic() if now_monotonic is None else now_monotonic
    key = f"{Path(out_dir).resolve()}::{source}"
    with _FAST_SOURCE_CADENCE_LOCK:
        state = _FAST_SOURCE_CADENCE_STATE.setdefault(key, {"started_at": 0.0, "result": None})
        previous = state.get("result")
        previous_started = float(state.get("started_at") or 0.0)
        if isinstance(previous, dict):
            rate_limited = _source_result_is_rate_limited(previous)
            interval = max(
                0.0,
                rate_limit_backoff_seconds if rate_limited else min_interval_seconds,
            )
            if now - previous_started < interval:
                return {
                    **previous,
                    "skipped": True,
                    "reason": "rate_limit_backoff" if rate_limited else "min_interval",
                }
        state["started_at"] = now

    result = refresher(Path(out_dir))
    if not isinstance(result, dict):
        result = {"source": source, "ok": False, "error": "refresh_result_not_dict"}
    with _FAST_SOURCE_CADENCE_LOCK:
        state = _FAST_SOURCE_CADENCE_STATE.setdefault(key, {})
        state["started_at"] = now
        state["result"] = dict(result)
    return result


def refresh_fast_sources(out_dir: Path) -> list[dict[str, Any]]:
    jobs = (
        ("arc_onchain", refresh_arc_public_chain),
        ("onchain", refresh_bsc_public_pairs),
        ("noxa", refresh_noxa_launchpad),
        ("proficy", refresh_proficy_trending),
        ("985", refresh_985_core),
        ("wind", refresh_wind_monitor),
        ("gmgn", refresh_gmgn_hot_search),
        ("okx", refresh_okx_market),
    )
    results = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [
            (
                source,
                pool.submit(
                    refresh_fast_source_on_cadence,
                    out_dir,
                    source=source,
                    refresher=job,
                    min_interval_seconds=FAST_SOURCE_MIN_INTERVAL_SECONDS[source],
                    rate_limit_backoff_seconds=(
                        0 if source in {"gmgn", "okx"} else FAST_SOURCE_RATE_LIMIT_BACKOFF_SECONDS
                    ),
                ),
            )
            for source, job in jobs
        ]
        for source, future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"source": source, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return results


def _source_health_family(status: dict[str, Any]) -> str:
    source = str(status.get("source") or "").strip().lower()
    if source == "arc_onchain":
        return "arc_onchain"
    if source.startswith("gmgn_"):
        return "gmgn"
    if source.startswith("985_"):
        return "985"
    if source.startswith("wind_") or source == "tingfeng_monitor":
        return "wind"
    if source.startswith("noxa_"):
        return "noxa"
    if source.startswith("proficy_"):
        return "proficy"
    if source.startswith("okx_"):
        return "okx"
    if source.startswith("debot_"):
        return "debot"
    if source or str(status.get("mode") or "").strip().lower() == "public_rpc_poll":
        return source or "onchain"
    return ""


def fast_source_health(statuses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Translate the current refresh attempt into explicit provider health."""
    health: dict[str, dict[str, Any]] = {}
    for status in statuses:
        if not isinstance(status, dict) or status.get("source") == "refresh_scheduler":
            continue
        family = _source_health_family(status)
        if not family:
            continue
        observed_at = (
            status.get("observed_at")
            or status.get("checked_at")
            or status.get("finished_at")
            or status.get("updated_at")
            or status.get("last_request_at")
            or now_iso()
        )
        row_count = status.get("row_count")
        if row_count is None:
            row_count = status.get("rows") if isinstance(status.get("rows"), (int, float)) else status.get("count")
        cached_available = (
            bool(status.get("skipped"))
            and status.get("reason") == "min_interval"
            and int(row_count or 0) > 0
        )
        rate_limited = bool(status.get("skipped")) and status.get("reason") == "rate_limit_backoff"
        ok = bool(status.get("ok")) or cached_available
        explicit_status = str(status.get("status") or "").strip().lower()
        projected_status = explicit_status or (
            "ok" if ok else "degraded" if rate_limited else "error"
        )
        entry: dict[str, Any] = {
            "status": projected_status,
            "observed_at": observed_at,
            "row_count": int(row_count or 0),
        }
        if ok:
            entry["last_success"] = observed_at
        nested_errors = {
            field: status[field]
            for field in ("rpc_errors", "arcscan_errors")
            if isinstance(status.get(field), list)
        }
        entry.update(nested_errors)
        if projected_status not in {"ok", "healthy", "success", "fresh"}:
            endpoint_details = [
                text
                for field in ("rpc_errors", "arcscan_errors")
                for text in _source_error_text(status.get(field))
            ]
            entry["error"] = str(
                status.get("error")
                or status.get("reason")
                or "; ".join(endpoint_details)
                or "refresh_failed"
            )
        if status.get("skipped"):
            entry["skipped"] = True
            entry["reason"] = status.get("reason")
        health[family] = entry
    return health


def schedule_fast_source_refresh(
    out_dir: Path,
    *,
    refresher=refresh_fast_sources,
    min_interval_seconds: float = 5.0,
) -> list[dict[str, Any]]:
    """Start source I/O in the background and immediately return the last result."""
    out_dir = Path(out_dir)
    key = str(out_dir.resolve())
    now = time.monotonic()
    with _SOURCE_REFRESH_LOCK:
        state = _SOURCE_REFRESH_STATE.setdefault(key, {"results": [], "started_at": 0.0, "thread": None})
        thread = state.get("thread")
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return [*state["results"], {"source": "refresh_scheduler", "running": True}]
        if state["started_at"] and now - float(state["started_at"]) < max(0.0, min_interval_seconds):
            return [
                *state["results"],
                {"source": "refresh_scheduler", "running": False, "skipped": True, "reason": "min_interval"},
            ]

        def worker() -> None:
            try:
                results = refresher(out_dir)
                if not isinstance(results, list):
                    results = [{"ok": False, "error": "refresh_result_not_list"}]
            except Exception as exc:  # noqa: BLE001
                results = [{"ok": False, "error": f"{type(exc).__name__}: {exc}"}]
            with _SOURCE_REFRESH_LOCK:
                current = _SOURCE_REFRESH_STATE.get(key)
                if current is not None:
                    current["results"] = results
                    current["finished_at"] = time.monotonic()

        thread = threading.Thread(target=worker, name="alpha-fast-source-refresh", daemon=True)
        state["started_at"] = now
        state["thread"] = thread
        thread.start()
        return [*state["results"], {"source": "refresh_scheduler", "running": True}]


def wait_for_fast_source_refresh(out_dir: Path, *, timeout_seconds: float = 10.0) -> bool:
    key = str(Path(out_dir).resolve())
    with _SOURCE_REFRESH_LOCK:
        state = _SOURCE_REFRESH_STATE.get(key) or {}
        thread = state.get("thread")
    if not isinstance(thread, threading.Thread):
        return True
    thread.join(max(0.0, timeout_seconds))
    return not thread.is_alive()


def refresh_985_core(out_dir: Path) -> dict[str, Any]:
    """Refresh only 985's event feed; wallet tables remain in the slow lane."""
    out_path = out_dir / "meme-source-inbox" / "985-monitor-fast.json"
    status_path = out_dir / "985-monitor-fast-status.json"
    started = time.monotonic()
    base_url = os.environ.get("MONITOR985_BASE_URL", alpha_985_monitor_export.DEFAULT_BASE_URL)
    try:
        timeout = max(2, int(os.environ.get("MONITOR985_FAST_TIMEOUT_SECONDS", "5")))
    except ValueError:
        timeout = 5
    try:
        rows, counts = alpha_985_monitor_export.collect_rows(base_url, 20, timeout)
        alpha_985_monitor_export.write_payload(rows, out_path, base_url=base_url)
        status = {
            "source": "985_monitor_fast",
            "ok": True,
            "out": str(out_path),
            "checked_at": now_iso(),
            "row_count": len(rows),
            "event_counts": counts,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:  # noqa: BLE001
        status = {
            "source": "985_monitor_fast",
            "ok": False,
            "out": str(out_path),
            "checked_at": now_iso(),
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    alpha_985_monitor_export.write_json(status_path, status)
    return status


def run_once(
    *,
    out_dir: Path = OUT_DIR,
    report_path: Path = REPORT_PATH,
    status_path: Path = STATUS_PATH,
    source_refresher=refresh_fast_sources,
    candidate_loader=load_meme_candidates,
    observation_loader=load_meme_observation_batch,
    execution_handoff_enabled: bool = True,
    quote_enrichment_enabled: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    started_at = now_iso()
    try:
        source_status = source_refresher(out_dir)
        source_seconds = round(time.monotonic() - started, 3)
        if candidate_loader is load_meme_candidates:
            observation_batch = observation_loader(
                80,
                8,
                source_loader=load_fast_local_sources,
                observed_at=started_at,
            )
            rows = observation_batch["candidates"]
            errors = observation_batch["errors"]
        else:
            rows, errors = candidate_loader(80, 8, source_loader=load_local_inbox_sources)
            observation_batch = {
                "candidates": rows,
                "events": [],
                "rejections": [],
                "errors": errors,
                "feed_counts": {},
                "_legacy_candidates_only": True,
            }
        observation_batch["source_health"] = fast_source_health(source_status)
        candidate_seconds = round(time.monotonic() - started - source_seconds, 3)
        if not rows:
            raise ValueError("local_candidate_snapshot_empty")
        if quote_enrichment_enabled:
            rows = apply_monitor_quote_cache(
                rows,
                read_json(Path(out_dir) / MONITOR_QUOTE_CACHE_NAME),
                started_at,
            )
            observation_batch["candidates"] = rows
        rows = enrich_smart_money(rows, out_dir, now_iso())
        rows = [annotate_meme_heat(annotate_meme_early_conviction(row)) for row in rows]
        replay = load_replay_history(out_dir)
        generated_at = now_iso()
        _rank(rows, replay, generated_at)
        calibration = replay_action_calibration(replay.get("summary") or {})
        chain_scope = os.environ.get("ALPHA_MEME_CHAINS", DEFAULT_MEME_CHAINS)
        potential = per_chain_potential_rows(
            rows,
            replay_calibration=calibration,
            limit_per_chain=LIVE_CANDIDATE_LIMIT_PER_CHAIN,
            evaluated_at=generated_at,
        )
        shadow = build_shadow_meme_rows(
            rows,
            selected_rows=potential,
            limit=40,
            replay_calibration=calibration,
            chain_scope=chain_scope,
        )
        _rank(potential, replay, generated_at)
        _rank(shadow, replay, generated_at)
        universe = {}
        for row in [*rows, *shadow, *potential]:
            chain = str(row.get("chain") or row.get("chain_id") or "").lower()
            address = str(row.get("contract_address") or row.get("token_address") or "").lower()
            universe[f"{chain}:{address}"] = row
        watch_state = update_watch_state(
            load_watch_state(out_dir / "alpha-gold-watch-state.json"),
            list(universe.values()),
            generated_at,
            min_confirmations=2,
            chain_scope=chain_scope,
        )
        attach_gold_watch_fields(potential, watch_state)
        attach_gold_watch_fields(shadow, watch_state)
        attach_gold_watch_fields(rows, watch_state)
        first_look_execution_candidates = fast_execution_candidates(potential)
        potential = per_chain_potential_rows(
            rows,
            replay_calibration=calibration,
            limit_per_chain=LIVE_CANDIDATE_LIMIT_PER_CHAIN,
            evaluated_at=generated_at,
        )
        shadow = build_shadow_meme_rows(
            rows,
            selected_rows=potential,
            limit=40,
            replay_calibration=calibration,
            chain_scope=chain_scope,
        )
        attach_gold_watch_fields(potential, watch_state)
        attach_gold_watch_fields(shadow, watch_state)
        _rank(potential, replay, generated_at)
        _rank(shadow, replay, generated_at)
        pending = build_pending_quote_rows(rows)
        monitor_v3 = publish_fast_monitor_snapshot(
            out_dir=out_dir,
            batch=observation_batch,
            observed_at=generated_at,
            previous_snapshot=None,
        )
        monitor_baselines = build_monitor_baselines(list(universe.values()))
        monitor_baselines.update(build_monitor_snapshot_baselines(monitor_v3))
        atomic_write_json(out_dir / "alpha-monitor-baselines-latest.json", monitor_baselines)
        quote_enrichment = {"scheduled": False, "reason": "disabled", "target_count": 0}
        if quote_enrichment_enabled:
            quote_targets = monitor_quote_enrichment_targets(rows, monitor_v3)
            quote_enrichment = schedule_monitor_quote_enrichment(
                out_dir,
                quote_targets,
                generated_at,
            )
        outcome_path = out_dir / "alpha-monitor-outcomes-latest.json"
        monitor_outcomes = update_monitor_outcomes(
            read_json(outcome_path),
            monitor_v3,
            observed_at=generated_at,
        )
        atomic_write_json(outcome_path, monitor_outcomes)

        execution_candidates = fast_execution_candidates(
            first_look_execution_candidates, potential
        )
        if not observation_batch.get("_legacy_candidates_only"):
            execution_candidates = monitor_selected_execution_candidates(
                execution_candidates, monitor_v3
            )
        if execution_handoff_enabled:
            execution_publish = publish_execution_inputs_from_fast_snapshot(
                out_dir=out_dir,
                potential=execution_candidates,
                replay=replay,
                generated_at=generated_at,
            )
        else:
            execution_publish = {"published": False, "reason": "monitor_only"}
        snapshot = {
            "meta": {
                "meme_live_generated_at": generated_at,
                "live_refresh_mode": "meme_fast_discovery",
                "meme_count": len(rows),
                "meme_potential_count": len(potential),
                "meme_pending_count": len(pending),
                "meme_shadow_count": len(shadow),
                "execution_input": execution_publish,
                "monitor_quote_enrichment": quote_enrichment,
                "monitor_outcomes": monitor_outcomes.get("summary") or {},
            },
            "meme_rows": rows[:40],
            "meme_watch_universe": list(universe.values()),
            "meme_potential_rows": potential,
            "meme_pending_rows": pending,
            "meme_shadow_rows": shadow,
            "monitor_v3": monitor_v3,
        }
        for value in snapshot.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict):
                    item.pop("token_intelligence", None)
        fast_snapshot_path = out_dir / FAST_SNAPSHOT_NAME
        atomic_write_json(fast_snapshot_path, snapshot)
        intelligence_enrichment = {"scheduled": False, "reason": "disabled", "target_count": 0}
        if quote_enrichment_enabled:
            intelligence_enrichment = schedule_monitor_intelligence(
                out_dir,
                monitor_v3,
                generated_at,
            )
        status = {
            "ok": True,
            "started_at": started_at,
            "updated_at": now_iso(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "source_seconds": source_seconds,
            "candidate_seconds": candidate_seconds,
            "candidate_count": len(rows),
            "potential_count": len(potential),
            "pending_quote_count": len(pending),
            "execution_input": execution_publish,
            "monitor_quote_enrichment": quote_enrichment,
            "monitor_intelligence": intelligence_enrichment,
            "snapshot_path": str(fast_snapshot_path),
            "errors": errors,
            "source_status": source_status,
        }
    except Exception as exc:  # noqa: BLE001
        status = {
            "ok": False,
            "started_at": started_at,
            "updated_at": now_iso(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }
    atomic_write_json(status_path, status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=float, default=2)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--execution-handoff", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("ALPHA_MEME_CHAINS", DEFAULT_MEME_CHAINS)
    configure_fast_source_environment()
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        name = "Local\\AlphaMemeFastDiscovery-" + hashlib.sha256(str(OUT_DIR.resolve()).encode()).hexdigest()[:16]
        mutex = kernel.CreateMutexW(None, False, name)
        if not mutex or ctypes.get_last_error() == 183:
            return 0
    while True:
        cycle_started = time.monotonic()
        source_refresher = refresh_fast_sources if args.once else schedule_fast_source_refresh
        status = run_once(
            source_refresher=source_refresher,
            execution_handoff_enabled=args.execution_handoff,
            quote_enrichment_enabled=not args.once,
        )
        if args.once:
            print(json.dumps(status, ensure_ascii=False))
            return 0 if status.get("ok") else 1
        time.sleep(max(1, args.interval_seconds - (time.monotonic() - cycle_started)))


if __name__ == "__main__":
    raise SystemExit(main())
