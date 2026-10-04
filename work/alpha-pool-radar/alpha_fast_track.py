"""Priority market quotes, isolated signal state and strict paper observation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping

from alpha_chain_strategy import STRATEGY_VERSION, StrategyPolicy, classify_signal
from alpha_discovery_rank import compute_discovery_rank
from alpha_gmgn_quote import GmgnRateLimitError
from alpha_monitor_chains import MONITOR_CHAINS, normalize_monitor_chain
from alpha_replay import first_snapshot

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
TTL_SECONDS = 30
DEX_QUOTE_REFRESH_SECONDS = 5
GMGN_QUOTE_TTL_SECONDS = 60
GMGN_REFRESH_LIMIT = 1
MAX_QUOTE_JOURNAL_BYTES = 128 * 1024 * 1024
RETAIN_QUOTE_JOURNAL_BYTES = 64 * 1024 * 1024
FAST_QUOTE_CACHE_RETENTION_SECONDS = 6 * 60 * 60
MAX_FAST_QUOTE_CACHE_ROWS = 2000
MAX_PRIORITY_STATE_BYTES = 64 * 1024 * 1024
PAPER_ORDER_NOTIONAL_USD = 5.0
ROW_FIELDS = ("meme_rows", "meme_potential_rows", "meme_shadow_rows", "meme_watch_universe")
CHAIN_ALIASES = {"56": "bsc", "bnb": "bsc", "bsc-mainnet": "bsc", "sol": "solana", "eth": "ethereum", "4663": "robinhood", "base-mainnet": "base"}
EXECUTION_CHAINS = frozenset({"bsc", "robinhood"})
SECTION_SIGNAL_STAGES = {
    "meme_rows": "aggregate_discovery",
    "meme_potential_rows": "aggregate_early_bird",
    "meme_shadow_rows": "aggregate_discovery",
}
SIGNAL_STAGE_PRIORITY = {
    "aggregate_discovery": 1,
    "aggregate_early_bird": 2,
    "aggregate_confirmation": 3,
}
LIVE_SECURITY_STAGES = {
    "bsc": {"aggregate_early_bird", "aggregate_confirmation"},
    "robinhood": {"aggregate_early_bird", "aggregate_confirmation"},
}
SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "secret_key",
    "private_key",
    "passphrase",
    "mnemonic",
    "seed_phrase",
)
GMGN_HOLDER_CACHE_FIELDS = (
    "holders",
    "holder_count",
    "top10_holder_pct",
    "holder_source",
    "holder_observed_at",
    "gmgn_observed_at",
    "gmgn_attempted_at",
)
_REPLAY_CACHE: dict[Path, tuple[int, int, dict]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def age_seconds(stamp: Any, now: str) -> float:
    try:
        start = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        end = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if start.tzinfo is None or end.tzinfo is None:
            return float("inf")
        delta = (end - start).total_seconds()
        return delta if delta >= -5 else float("inf")
    except (TypeError, ValueError):
        return float("inf")


def _epoch(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        result = float(value)
        return result / 1000 if result > 1e11 else result
    except (TypeError, ValueError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return stamp.timestamp() if stamp.tzinfo else 0.0
        except (TypeError, ValueError, OverflowError, OSError):
            return 0.0


def gmgn_cooldown_epoch(out_dir: Path, previous: dict | None = None) -> float:
    states = [previous or {}]
    states.extend(read_json(out_dir / name) for name in (
        "gmgn-wallet-profit-query-status.json",
        "gmgn-skills-status.json",
    ))
    return max((_epoch(state.get("gmgn_retry_after") or state.get("retry_after_epoch") or state.get("retry_after")) for state in states), default=0.0)


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def read_replay_history_cached(path: Path) -> dict:
    """Parse the large replay only when its atomic file version changes."""
    try:
        stat = path.stat()
    except OSError:
        return {}
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _REPLAY_CACHE.get(path.resolve())
    if cached and cached[:2] == signature:
        return cached[2]
    payload = read_json(path)
    _REPLAY_CACHE[path.resolve()] = (*signature, payload)
    return payload


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    for attempt in range(6):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.025 * (2 ** attempt))


def compact_jsonl(path: Path, *, max_bytes: int = MAX_QUOTE_JOURNAL_BYTES,
                  retain_bytes: int = RETAIN_QUOTE_JOURNAL_BYTES) -> bool:
    """Keep the newest complete JSONL records without loading the journal into memory."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False
    keep = max(1, min(retain_bytes, max_bytes))
    with path.open("rb") as source:
        source.seek(max(0, size - keep))
        tail = source.read()
    if size > keep:
        newline = tail.find(b"\n")
        tail = tail[newline + 1:] if newline >= 0 else b""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.compact.tmp")
    temporary.write_bytes(tail)
    os.replace(temporary, path)
    return True


def prune_quote_cache(quotes: dict, now: str, *, keep_keys=(),
                      max_rows: int = MAX_FAST_QUOTE_CACHE_ROWS) -> dict:
    """Keep active identities plus the newest bounded set of rebuildable quotes."""
    keep = set(keep_keys)
    pinned = [(key, value) for key, value in quotes.items() if key in keep]
    recent = [
        (key, value) for key, value in quotes.items()
        if key not in keep and age_seconds(
            value.get("last_attempt_at") or value.get("quote_observed_at"), now
        ) < FAST_QUOTE_CACHE_RETENTION_SECONDS
    ]
    recent.sort(
        key=lambda item: _epoch(item[1].get("last_attempt_at") or
                                item[1].get("quote_observed_at")),
        reverse=True,
    )
    remaining = max(0, max_rows - len(pinned))
    return dict([*pinned, *recent[:remaining]])


def read_priority_state(path: Path, *, max_bytes: int = MAX_PRIORITY_STATE_BYTES) -> dict:
    """Use the lightweight report when a paper ledger is too large for target rotation."""
    try:
        if path.stat().st_size <= max_bytes:
            return read_json(path)
    except OSError:
        return {}
    report_path = (
        path.with_name("alpha-execution-report.json")
        if path.name == "alpha-execution-state.json"
        else path.with_name("report.json")
    )
    report = read_json(report_path)
    positions = report.get("positions") or report.get("open_positions") or []
    orders = report.get("pending_orders") or []
    return {
        "open_positions": positions if isinstance(positions, list) else list(positions.values()),
        "orders": {str(index): row for index, row in enumerate(orders)},
    }


def identity(row: dict) -> tuple[str, str]:
    chain = str(row.get("chain") or row.get("chain_id") or row.get("chainId") or "").lower()
    chain = normalize_monitor_chain(CHAIN_ALIASES.get(chain, chain))
    address = str(row.get("contract_address") or row.get("token_address") or row.get("tokenAddress") or row.get("address") or "").strip()
    if address.lower().startswith("0x"):
        address = address.lower()
    return chain, address


def key_of(row: dict) -> str:
    return ":".join(identity(row))


def monitor_safe_cached_quote(row: dict, quote: dict) -> dict:
    """Prevent unsupported ARC GMGN market or holder state from being reused."""
    if identity(row)[0] != "arc" or not isinstance(quote, dict):
        return quote
    if str(quote.get("quote_source") or "").lower().startswith("gmgn"):
        return {}
    safe = dict(quote)
    if str(safe.get("holder_source") or "").lower().startswith("gmgn"):
        for field in GMGN_HOLDER_CACHE_FIELDS:
            safe.pop(field, None)
    safe["gmgn_holder_status"] = "provider_unavailable"
    return safe


def execution_impact_fields(liquidity_usd: float, notional_usd: float = PAPER_ORDER_NOTIONAL_USD) -> dict:
    """Add an explicit paper-fill impact estimate to a market snapshot.

    DexScreener exposes pool state, not a swap-route simulation.  Keep the
    unavailable route price impact explicit at zero and model only the fixed
    depth proxy used by the paper worker.  The provenance fields prevent this
    estimate from being mistaken for an OKX/router quote.
    """
    liquidity = number(liquidity_usd)
    notional = max(0.0, number(notional_usd))
    depth = (notional / (liquidity / 2.0) * 100.0) if liquidity > 0 else 100.0
    return {
        "price_impact": 0.0,
        "depth_impact": min(100.0, max(0.0, depth)),
        "impact_notional_usd": notional,
        "impact_source": "dexscreener_depth_proxy",
        "price_impact_source": "unavailable_from_dexscreener",
    }


def valid_identity(row: dict) -> bool:
    chain, address = identity(row)
    return bool(chain and re.fullmatch(r"[a-z0-9_-]+", chain) and (
        re.fullmatch(r"0x[0-9a-f]{40}", address) or
        (chain == "solana" and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address))
    ))


def report_rows(report: dict) -> list[dict]:
    rows: dict[str, dict] = {}
    for name in ROW_FIELDS:
        for row in report.get(name) or []:
            if isinstance(row, dict) and valid_identity(row):
                key = key_of(row)
                candidate = dict(row)
                if name in SECTION_SIGNAL_STAGES:
                    section_stage = SECTION_SIGNAL_STAGES[name]
                    if identity(candidate)[0] == "robinhood" and not candidate.get("signal_stage"):
                        section_stage = "aggregate_discovery"
                    candidate_stage = str(candidate.get("signal_stage") or "")
                    if SIGNAL_STAGE_PRIORITY.get(section_stage, 0) > SIGNAL_STAGE_PRIORITY.get(candidate_stage, 0):
                        candidate["signal_stage"] = section_stage
                previous = rows.get(key, {})
                merged = {**candidate, **previous}
                previous_stage = str(previous.get("signal_stage") or "")
                candidate_stage = str(candidate.get("signal_stage") or "")
                if SIGNAL_STAGE_PRIORITY.get(candidate_stage, 0) > SIGNAL_STAGE_PRIORITY.get(previous_stage, 0):
                    merged["signal_stage"] = candidate_stage
                rows[key] = merged
    return list(rows.values())


def enrich_live_candidate_security(
    rows: list[dict],
    replay_history: dict,
    cache_path: Path,
    now: str,
    security_enricher,
) -> list[dict]:
    """Run blocking security lookups only for stages that can reach a live executor."""
    targets = []
    for row in rows:
        chain = identity(row)[0]
        if str(row.get("signal_stage") or "") not in LIVE_SECURITY_STAGES.get(chain, set()):
            continue
        history = _history_row_for(row, replay_history)
        first_snapshot = history.get("first_snapshot") if isinstance(history.get("first_snapshot"), dict) else {}
        first_seen_at = (
            history.get("first_seen_at")
            or first_snapshot.get("first_seen_at")
            or first_snapshot.get("seen_at")
            or row.get("first_seen_at")
            or row.get("watch_first_seen_at")
        )
        if 0 <= age_seconds(first_seen_at, now) <= StrategyPolicy.for_chain(chain).max_entry_delay_seconds:
            targets.append(row)
    if not targets:
        return rows
    secured = {key_of(row): row for row in security_enricher(targets, cache_path, now)}
    return [secured.get(key_of(row), row) for row in rows]


def replay_requires_fast_persist(previous: dict, updated: dict) -> bool:
    """Persist immediately only when a new immutable identity snapshot appeared."""
    for field in ("rows", "pending_first_snapshots"):
        before = previous.get(field) if isinstance(previous.get(field), dict) else {}
        after = updated.get(field) if isinstance(updated.get(field), dict) else {}
        if set(before) != set(after):
            return True
    return False


def _public_payload(value: Any) -> Any:
    """Recursively remove credential-like fields from published JSON."""
    if isinstance(value, Mapping):
        return {
            str(key): _public_payload(item)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in SENSITIVE_FIELD_MARKERS)
        }
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_public_payload(item) for item in value]
    return value


def _history_row_for(row: dict, history: dict) -> dict:
    history_rows = history.get("rows") if isinstance(history.get("rows"), dict) else {}
    result = history_rows.get(key_of(row)) if isinstance(history_rows, dict) else None
    return result if isinstance(result, dict) else {}


def build_live_replay_view(history: dict, rows: list[dict], now: str) -> dict:
    """Build a small execution-only view from persisted immutable history."""
    history_rows = history.get("rows") if isinstance(history.get("rows"), dict) else {}
    selected: dict[str, dict] = {}
    for row in rows:
        key = key_of(row)
        existing = history_rows.get(key) if isinstance(history_rows, dict) else None
        if isinstance(existing, dict) and existing:
            selected[key] = existing
    return {"rows": selected}


def update_live_first_sightings(sightings: dict, rows: list[dict], history: dict,
                                now: str, *, limit: int = 2000) -> dict:
    """Persist the first discovery baseline without rewriting the large replay file."""
    history_rows = history.get("rows") if isinstance(history.get("rows"), dict) else {}
    current = sightings.get("rows") if isinstance(sightings.get("rows"), dict) else {}
    result = {key: value for key, value in current.items() if isinstance(value, dict)}
    now_epoch = _epoch(now)
    for row in rows:
        key = key_of(row)
        if not key.strip(":") or key in history_rows or key in result:
            continue
        replay = row.get("replay") if isinstance(row.get("replay"), dict) else {}
        candidates = [
            row.get("first_seen_at"), row.get("watch_first_seen_at"),
            replay.get("first_seen_at"), row.get("observed_at"), now,
        ]
        valid = [(stamp, _epoch(stamp)) for stamp in candidates if _epoch(stamp) > 0 and _epoch(stamp) <= now_epoch + 5]
        first_seen_at = min(valid, key=lambda item: item[1])[0] if valid else now
        raw_mcap = row.get("mcap", row.get("market_cap"))
        market_cap = number(raw_mcap)
        price = number(row.get("price_usd") or row.get("price"))
        liquidity = number(row.get("liquidity_usd") or row.get("liquidity"))
        market_cap_is_valid = (
            market_cap > 0
            and row.get("valuation_type") in (None, "", "market_cap")
        )
        discovery_snapshot = first_snapshot(row)
        discovery_snapshot.update({
            "chain": identity(row)[0],
            "contract_address": identity(row)[1],
            "first_seen_at": first_seen_at,
        })
        pool_address = row.get("pool_address") or row.get("pair_address") or row.get("pool")
        if pool_address:
            discovery_snapshot["pair_address"] = pool_address
            discovery_snapshot["pool_address"] = pool_address
        if price > 0:
            discovery_snapshot["price_usd"] = price
        if market_cap_is_valid:
            discovery_snapshot["mcap"] = market_cap
            discovery_snapshot["market_cap"] = market_cap
            discovery_snapshot["valuation_type"] = "market_cap"
        if liquidity > 0:
            discovery_snapshot["liquidity"] = liquidity
        result[key] = {
            "key": key,
            "chain": identity(row)[0],
            "contract_address": identity(row)[1],
            "symbol": str(row.get("symbol") or "")[:40],
            "first_seen_at": first_seen_at,
            "first_price_usd": price if price > 0 else None,
            "first_mcap_usd": market_cap if market_cap_is_valid else None,
            "first_liquidity_usd": liquidity if liquidity > 0 else None,
            "discovery_snapshot": discovery_snapshot,
        }
    ordered = sorted(result.items(), key=lambda item: _epoch(item[1].get("first_seen_at")), reverse=True)
    return {"updated_at": now, "rows": dict(ordered[:limit])}


def build_live_replay_with_sightings(history: dict, sightings: dict, rows: list[dict], now: str) -> dict:
    """Build static history immediately, then attach the first tradeable quote when available."""
    view = build_live_replay_view(history, rows, now)
    sighting_rows = sightings.get("rows") if isinstance(sightings.get("rows"), dict) else {}
    for row in rows:
        key = key_of(row)
        if key in view["rows"]:
            continue
        sighting = sighting_rows.get(key) if isinstance(sighting_rows, dict) else None
        if not isinstance(sighting, dict):
            continue
        discovery_snapshot = (
            dict(sighting.get("discovery_snapshot"))
            if isinstance(sighting.get("discovery_snapshot"), dict)
            else {}
        )
        quote_fresh = (
            row.get("quote_status") == "fresh"
            and age_seconds(row.get("quote_observed_at"), now) <= TTL_SECONDS
            and number(row.get("price_usd")) > 0
            and number(row.get("mcap", row.get("market_cap"))) > 0
            and row.get("valuation_type") in (None, "market_cap")
        )
        first_price = number(sighting.get("first_price_usd") or discovery_snapshot.get("price_usd"))
        if first_price <= 0 and quote_fresh:
            first_price = number(row.get("price_usd"))
        first_mcap = number(
            sighting.get("first_mcap_usd")
            or discovery_snapshot.get("mcap")
            or discovery_snapshot.get("market_cap")
        )
        if first_mcap <= 0 and quote_fresh:
            first_mcap = number(row.get("mcap", row.get("market_cap")))
        first_liquidity = number(
            sighting.get("first_liquidity_usd")
            or discovery_snapshot.get("liquidity_usd")
            or discovery_snapshot.get("liquidity")
        )
        if first_liquidity <= 0 and quote_fresh:
            first_liquidity = number(row.get("liquidity_usd") or row.get("liquidity"))
        pool_address = (
            row.get("pool_address") or row.get("pair_address")
            or discovery_snapshot.get("pool_address") or discovery_snapshot.get("pair_address")
        )
        if first_price <= 0 or first_mcap <= 0 or first_liquidity <= 0:
            continue
        discovery_snapshot.update({
            "chain": identity(row)[0],
            "contract_address": identity(row)[1],
            "first_seen_at": sighting.get("first_seen_at"),
            "price_usd": first_price,
            "mcap": first_mcap,
            "market_cap": first_mcap,
            "liquidity": first_liquidity,
            "valuation_type": "market_cap",
            "pair_address": pool_address,
            "pool_address": pool_address,
        })
        price = number(row.get("price_usd")) if quote_fresh else first_price
        market_cap = number(row.get("mcap", row.get("market_cap"))) if quote_fresh else first_mcap
        if quote_fresh:
            baseline_snapshot = first_snapshot(row)
            baseline_snapshot.update({
                "chain": identity(row)[0],
                "contract_address": identity(row)[1],
                "first_seen_at": sighting.get("first_seen_at"),
                "price_usd": price,
                "mcap": market_cap,
                "market_cap": market_cap,
                "valuation_type": "market_cap",
                "pair_address": pool_address,
                "pool_address": pool_address,
            })
        else:
            baseline_snapshot = dict(discovery_snapshot)
        view["rows"][key] = {
            **sighting,
            "first_price_usd": first_price,
            "first_mcap_usd": first_mcap,
            "first_liquidity_usd": first_liquidity,
            "first_tradeable_quote_at": (row.get("quote_observed_at") or now) if quote_fresh else None,
            "first_snapshot": baseline_snapshot,
            "discovery_snapshot": discovery_snapshot,
            "latest_seen_at": now,
            "latest_price_usd": price,
            "observations": [],
        }
    return view


def attach_fast_discovery_ranks(rows: list[dict], history: dict, now: str) -> list[dict]:
    """Compute the ordering rank in the fast path after its first snapshot exists."""
    evaluated_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
    ranked: list[dict] = []
    for row in rows:
        history_row = _history_row_for(row, history)
        stored_snapshot = history_row.get("first_snapshot") if isinstance(history_row, dict) else None
        first_snapshot = dict(stored_snapshot) if isinstance(stored_snapshot, dict) else {}
        first_seen_at = history_row.get("first_seen_at") if isinstance(history_row, dict) else None
        if first_seen_at and not first_snapshot.get("first_seen_at"):
            first_snapshot["first_seen_at"] = first_seen_at
        ranked.append({
            **row,
            **compute_discovery_rank(row, first_snapshot=first_snapshot, now=evaluated_at),
        })
    return ranked


def _strategy_rejection(row: dict) -> dict:
    return {
        "symbol": str(row.get("symbol") or "MEME")[:40],
        "chain": identity(row)[0],
        "contract_address": identity(row)[1],
        "strategy_version": row.get("strategy_version"),
        "signal_stage": row.get("signal_stage"),
        "entry_route": row.get("entry_route"),
        "execution_mode": row.get("execution_mode"),
        "rank_score": row.get("rank_score"),
        "rank_components": row.get("rank_components") or {},
        "legacy_score": row.get("legacy_score"),
        "score": row.get("legacy_score"),
        "reject_reason": str(row.get("reject_reason") or "strategy_contract_rejected")[:240],
        "first_seen_at": row.get("first_seen_at"),
        "first_price_usd": row.get("first_price_usd"),
        "first_mcap_usd": row.get("first_mcap_usd"),
        "current_price_usd": row.get("current_price_usd"),
        "current_mcap_usd": row.get("current_mcap_usd"),
        "entry_delay_seconds": row.get("entry_delay_seconds"),
        "entry_delay_minutes": (
            round(number(row.get("entry_delay_seconds")) / 60, 2)
            if row.get("entry_delay_seconds") is not None
            else None
        ),
        "markup_from_first": row.get("markup_from_first"),
        "policy_checks": row.get("policy_checks") or {},
        "tradeability": row.get("tradeability") or {"status": "pending"},
        "order_authorized": False,
        "requires_executor_tradeability_check": True,
    }


def priority_targets(report: dict, out_dir: Path, previous: dict, limit: int = 80, now: str | None = None) -> list[dict]:
    rows = {key_of(row): row for row in report_rows(report)}
    from alpha_wallet_quality import qualified_wallets, wallet_identity
    from alpha_smart_money_evidence import parse_timestamp
    now = now or utc_now()
    approved = {wallet_identity(w["chain"], w["address"]) for w in qualified_wallets(
        read_json(out_dir / "gmgn-smart-money-top50.json"), now)}
    interested = set()
    for row in read_json(out_dir / "meme-source-inbox" / "gmgn-wallet-flow.json").get("rows") or []:
        stamp = parse_timestamp(row.get("observed_at"))
        wallet = wallet_identity(row.get("chain"), row.get("wallet") or row.get("wallet_address"))
        if (wallet not in approved or not stamp or not valid_identity(row)
                or not 0 <= age_seconds(stamp.isoformat(), now) <= 900):
            continue
        interested.add(key_of(row))
        rows.setdefault(key_of(row), row)
    held = set()
    for path in [*out_dir.glob("*paper*state.json"), out_dir / "alpha-execution-state.json",
                 out_dir / "profitable-wallet-paper" / "state.json",
                 out_dir / "execution-challenger" / "state.json"]:
        state = read_priority_state(path)
        state = state.get("execution", state)
        positions = list(state.get("open_positions") or [])
        positions.extend((state.get("positions") or {}).values())
        positions.extend((state.get("orders") or {}).values())
        for arm in (state.get("strategies") or {}).values():
            if isinstance(arm, dict):
                positions.extend(arm.get("open_positions") or [])
        for pos in positions:
            if not valid_identity(pos) and ":" in str(pos.get("key") or ""):
                chain, address = pos["key"].split(":", 1)
                pos = {**(state.get("quotes") or {}).get(pos["key"], {}), **pos,
                       "chain": chain, "contract_address": address}
            if valid_identity(pos):
                key = key_of(pos)
                held.add(key)
                rows.setdefault(key, pos)
    quotes = previous.get("quotes") or {}
    interested = set(sorted(interested - held, key=lambda k: quotes.get(k, {}).get("last_attempt_at", ""))[:20])
    hot = sorted([r for r in rows.values() if key_of(r) not in held and number(r.get("gold_dog_conviction_score")) >= 82], key=lambda r: -number(r.get("gold_dog_conviction_score")))[:20]
    hot_keys = {key_of(r) for r in hot}
    ranked = sorted(rows.values(), key=lambda row: (
        key_of(row) not in held,
        key_of(row) not in interested,
        key_of(row) not in hot_keys,
        quotes.get(key_of(row), {}).get("last_attempt_at", quotes.get(key_of(row), {}).get("quote_observed_at", "")),
        -number(row.get("gold_dog_conviction_score") or row.get("score")),
    ))
    return ranked[:limit]


def live_priority_targets(
    report: dict,
    out_dir: Path,
    replay_history: dict,
    *,
    now: str | None = None,
    limit: int = 40,
) -> list[dict]:
    """Select only entry-window candidates and actual live positions for quoting."""
    now = now or utc_now()
    rows = {key_of(row): row for row in report_rows(report)}
    held: set[str] = set()
    for filename in ("okx-dex-sdk-live-state.json", "okx-dex-sdk-robinhood-live-state.json"):
        state = read_json(out_dir / filename)
        positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
        for raw_key, position in positions.items():
            row = position if isinstance(position, dict) else {}
            if not valid_identity(row) and ":" in str(raw_key):
                chain, address = str(raw_key).split(":", 1)
                row = {**row, "chain": chain, "contract_address": address}
            if valid_identity(row):
                key = key_of(row)
                held.add(key)
                rows.setdefault(key, row)

    selected = []
    for key, row in rows.items():
        if key in held:
            selected.append(row)
            continue
        chain = identity(row)[0]
        if str(row.get("signal_stage") or "") not in LIVE_SECURITY_STAGES.get(chain, set()):
            continue
        history = _history_row_for(row, replay_history)
        snapshot = history.get("first_snapshot") if isinstance(history.get("first_snapshot"), dict) else {}
        first_seen_at = (
            history.get("first_seen_at")
            or snapshot.get("first_seen_at")
            or snapshot.get("seen_at")
            or row.get("first_seen_at")
            or row.get("watch_first_seen_at")
        )
        age = age_seconds(first_seen_at, now)
        if first_seen_at and not 0 <= age <= StrategyPolicy.for_chain(chain).max_entry_delay_seconds:
            continue
        selected.append(row)
    selected.sort(
        key=lambda row: (
            key_of(row) not in held,
            age_seconds(
                (_history_row_for(row, replay_history).get("first_seen_at") or row.get("first_seen_at")),
                now,
            ),
            -number(row.get("rank_score") or row.get("score")),
        )
    )
    return selected[:limit]


def fetch_pairs(chain: str, addresses: list[str]) -> list[dict]:
    normalized_chain = normalize_monitor_chain(chain)
    config = MONITOR_CHAINS.get(normalized_chain)
    slug = config.dexscreener_slug if config is not None else normalized_chain
    url = f"https://api.dexscreener.com/tokens/v1/{slug}/{','.join(addresses)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "AlphaRadar/2.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=6) as response:
        payload = json.load(response)
    return payload if isinstance(payload, list) else []


def quote_from_report_row(row: dict, now: str) -> dict:
    """Reuse a route-bearing quote already published by the monitor collector."""
    observed_at = row.get("quote_observed_at") or row.get("observed_at")
    pair_address = row.get("pair_address") or row.get("pool_address") or row.get("pool")
    price = number(row.get("price_usd") or row.get("price"))
    liquidity = number(row.get("liquidity") or row.get("liquidity_usd"))
    if (
        row.get("quote_status") != "fresh"
        or age_seconds(observed_at, now) > TTL_SECONDS
        or not pair_address
        or price <= 0
        or liquidity <= 0
    ):
        return {}
    chain, address = identity(row)
    raw_mcap = row.get("mcap", row.get("market_cap"))
    market_cap = number(raw_mcap) if row.get("valuation_type") in (None, "market_cap") else 0.0
    fdv = number(row.get("fdv"))
    quote = {
        "chain": chain,
        "contract_address": address,
        "price_usd": price,
        "mcap": market_cap or None,
        "market_cap": market_cap or None,
        "fdv": fdv or None,
        "market_cap_source": row.get("market_cap_source"),
        "fdv_source": row.get("fdv_source"),
        "valuation_type": "market_cap" if market_cap else "fdv" if fdv else "unavailable",
        "liquidity": liquidity,
        "volume5m": number(row.get("volume5m") or row.get("volume_5m_usd")),
        "volume24h": number(row.get("volume24h") or row.get("volume_24h_usd") or row.get("volume")),
        "buy_count5m": number(row.get("buy_count5m") or row.get("buys_5m")),
        "sell_count5m": number(row.get("sell_count5m") or row.get("sells_5m")),
        "change_m5": number(row.get("change_m5")),
        "change_h1": number(row.get("change_h1")),
        "change_h24": number(row.get("change_h24")),
        "pair_address": pair_address,
        "dex_url": row.get("dex_url") or row.get("url"),
        "quote_observed_at": observed_at,
        "quote_source": row.get("quote_source") or "monitor_reuse",
        "quote_status": "fresh",
        "quote_time_basis": row.get("quote_time_basis") or "upstream_observation",
        "pair_age_hours": row.get("pair_age_hours"),
    }
    quote.update(execution_impact_fields(liquidity))
    fingerprint = {
        key: quote.get(key)
        for key in ("price_usd", "liquidity", "volume5m", "buy_count5m", "sell_count5m", "pair_address")
    }
    quote["quote_fingerprint"] = row.get("quote_fingerprint") or hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True).encode()
    ).hexdigest()[:20]
    return quote


def retry_delay(error: Exception, failures: int, now: str) -> int:
    delay = min(900, 60 * 2 ** min(max(failures - 1, 0), 4))
    if isinstance(error, urllib.error.HTTPError) and error.headers:
        value = error.headers.get("Retry-After", "")
        try:
            delay = max(delay, int(value))
        except (ValueError, TypeError):
            try:
                seconds = (parsedate_to_datetime(value) - datetime.fromisoformat(now.replace("Z", "+00:00"))).total_seconds()
                delay = max(delay, math.ceil(seconds))
            except (ValueError, TypeError, OverflowError):
                pass
    return delay


def quote_from_pairs(row: dict, pairs: list[dict], previous: dict, now: str) -> dict:
    chain, address = identity(row)
    matching = [p for p in pairs if identity({"chain": p.get("chainId"), "address": (p.get("baseToken") or {}).get("address")}) == (chain, address)
                and number(p.get("priceUsd")) > 0 and number((p.get("liquidity") or {}).get("usd")) > 0]
    pinned = previous.get("pair_address")
    replaced_pool = None
    if pinned:
        pinned_matches = [p for p in matching if str(p.get("pairAddress") or "").lower() == str(pinned).lower()]
        if pinned_matches:
            matching = pinned_matches
        elif matching:
            replacement = max(matching, key=lambda p: number((p.get("liquidity") or {}).get("usd")))
            previous_liquidity = number(previous.get("liquidity"))
            replacement_liquidity = number((replacement.get("liquidity") or {}).get("usd"))
            dead_dust_pool = (
                previous.get("quote_status") == "unavailable"
                and previous_liquidity < 100
                and replacement_liquidity >= 1_000
                and replacement_liquidity >= max(previous_liquidity * 100, 1_000)
            )
            if dead_dust_pool:
                matching = [replacement]
                replaced_pool = pinned
            else:
                matching = []
    if not matching:
        return {**previous, "quote_status": "unavailable", "quote_error": "pinned_pool_unavailable" if pinned else "no_liquid_matching_pool"}
    pair = max(matching, key=lambda p: number((p.get("liquidity") or {}).get("usd")))
    tx = (pair.get("txns") or {}).get("m5") or {}
    changes = pair.get("priceChange") or {}
    mcap = number(pair.get("marketCap")) if not isinstance(pair.get("marketCap"), bool) else 0
    fdv = number(pair.get("fdv")) if not isinstance(pair.get("fdv"), bool) else 0
    mcap, fdv = (mcap if mcap > 0 else None), (fdv if fdv > 0 else None)
    quote = {
        "chain": chain, "contract_address": address,
        "price_usd": number(pair.get("priceUsd")),
        "mcap": mcap, "market_cap": mcap, "fdv": fdv,
        "market_cap_source": "dexscreener.marketCap" if mcap else None,
        "fdv_source": "dexscreener.fdv" if fdv else None,
        "valuation_type": "market_cap" if mcap else "fdv" if fdv else "unavailable",
        "liquidity": number((pair.get("liquidity") or {}).get("usd")),
        "volume5m": number((pair.get("volume") or {}).get("m5")),
        "volume24h": number((pair.get("volume") or {}).get("h24")),
        "buy_count5m": number(tx.get("buys")), "sell_count5m": number(tx.get("sells")),
        "change_m5": number(changes.get("m5")), "change_h1": number(changes.get("h1")), "change_h24": number(changes.get("h24")),
        "pair_address": pair.get("pairAddress"), "dex_url": pair.get("url"),
        "quote_observed_at": now, "quote_source": "dexscreener", "quote_status": "fresh",
        "quote_time_basis": "http_observation_not_trade_timestamp",
    }
    if replaced_pool:
        quote["pool_replaced_from"] = replaced_pool
        quote["pool_replaced_at"] = now
    quote.update(execution_impact_fields(quote["liquidity"]))
    created = number(pair.get("pairCreatedAt"))
    if created:
        quote["pair_age_hours"] = max(0.0, (datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() - created / 1000) / 3600)
    fingerprint = {k: quote[k] for k in ("price_usd", "liquidity", "volume5m", "buy_count5m", "sell_count5m", "pair_address")}
    quote["quote_fingerprint"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:20]
    old_price = 0 if replaced_pool else number(previous.get("price_usd"))
    if old_price and (quote["price_usd"] / old_price > 2.5 or quote["price_usd"] / old_price < 0.4):
        # Never book an isolated price spike; require consecutive observations.
        pending = number(previous.get("pending_price_usd"))
        count = int(previous.get("pending_count") or 0) + 1 if pending and abs(quote["price_usd"] / pending - 1) < 0.15 else 1
        if count < 3:
            return {**previous, "quote_status": "quarantined", "quote_error": "price_jump_pending",
                    "pending_price_usd": quote["price_usd"], "pending_count": count, "pending_observed_at": now}
    return quote


def merge_market_quotes(gmgn: dict, dex: dict, previous: dict, now: str) -> dict:
    """Prefer GMGN market data while retaining DexScreener as a cross-check/fallback."""
    gmgn_fresh = gmgn.get("quote_status") == "fresh" and age_seconds(gmgn.get("quote_observed_at"), now) <= TTL_SECONDS
    dex_fresh = dex.get("quote_status") == "fresh" and age_seconds(dex.get("quote_observed_at"), now) <= TTL_SECONDS
    if not gmgn_fresh:
        return dex if dex_fresh else (gmgn or dex or previous)

    quote = {**(dex if dex_fresh else {}), **gmgn}
    quote["quote_source"] = "gmgn_skill_token_info"
    quote["quote_status"] = "fresh"
    prior_pair = str(previous.get("pair_address") or "")
    current_pair = str(quote.get("pair_address") or "")
    if prior_pair and current_pair and prior_pair.lower() != current_pair.lower():
        prior_liquidity = number(previous.get("liquidity"))
        if (previous.get("quote_status") == "unavailable" or dex.get("quote_status") == "unavailable") and prior_liquidity < 100 and number(quote.get("liquidity")) >= 1_000:
            quote["pool_replaced_from"] = prior_pair
            quote["pool_replaced_at"] = now

    if dex_fresh:
        gmgn_price = number(gmgn.get("price_usd"))
        dex_price = number(dex.get("price_usd"))
        quote["cross_check_source"] = "dexscreener"
        quote["cross_check_price_usd"] = dex_price
        quote["cross_check_deviation_pct"] = round(abs(gmgn_price / dex_price - 1) * 100, 4) if dex_price else None
        same_pool = str(gmgn.get("pair_address") or "").lower() == str(dex.get("pair_address") or "").lower()
        if same_pool and dex_price and abs(gmgn_price / dex_price - 1) > 0.6:
            return {**previous, "quote_status": "quarantined", "quote_error": "gmgn_dex_price_conflict",
                    "gmgn_attempted_at": gmgn.get("gmgn_attempted_at") or now,
                    "pending_price_usd": gmgn_price, "pending_observed_at": now}

    quote.update(execution_impact_fields(number(quote.get("liquidity"))))
    fingerprint = {k: quote.get(k) for k in ("price_usd", "liquidity", "volume5m", "buy_count5m", "sell_count5m", "pair_address", "quote_source")}
    quote["quote_fingerprint"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:20]
    return quote


def apply_quote(row: dict, quote: dict, now: str) -> dict:
    if quote.get("quote_status") != "fresh" or not 0 <= age_seconds(quote.get("quote_observed_at"), now) <= TTL_SECONDS:
        return {**row, "quote_status": (quote.get("quote_status") or "unavailable") if quote.get("quote_status") != "fresh" else "stale",
                "quote_observed_at": quote.get("quote_observed_at"), "quote_error": quote.get("quote_error", "")}
    source = "GMGN" if str(quote.get("quote_source") or "").startswith("gmgn") else "Dex"
    return {**row, **quote, "price": quote["price_usd"], "live_price_source": source, "live_price_updated_at": int(datetime.fromisoformat(quote["quote_observed_at"].replace("Z", "+00:00")).timestamp() * 1000)}


def overlay_report(report: dict, overlay: dict, now: str | None = None) -> dict:
    now = now or utc_now()
    result = dict(report)
    meta = dict(result.get("meta") or {})
    status = dict(overlay.get("status") or {})
    if not overlay:
        return result
    stale = age_seconds(overlay.get("updated_at"), now) > TTL_SECONDS
    status.update({"stale": stale, "ok": bool(status.get("ok")) and not stale})
    if overlay.get("gmgn_execution"):
        status["gmgn_execution"] = overlay["gmgn_execution"]
    meta["fast_track"] = status
    if overlay.get("execution_audit"):
        meta["execution_audit"] = overlay["execution_audit"]
    if overlay.get("execution_challenger"):
        meta["execution_challenger"] = {**overlay["execution_challenger"], "stale": stale}
    result["meta"] = meta
    quotes = overlay.get("quotes") or {}
    evidence = overlay.get("evidence") or {}
    watch_fields = overlay.get("watch_fields") or {}
    for field in ROW_FIELDS:
        if field in result:
            result[field] = [apply_quote({**row, **(watch_fields.get(key_of(row), {}) if not stale else {}), **({"smart_money_evidence": evidence[key_of(row)]} if key_of(row) in evidence and not stale else {})}, quotes.get(key_of(row), {}), now) for row in result[field]]
    if not stale:
        seen = set()
        merged = []
        for alert in [*(report.get("gold_watch_alerts") or []), *(overlay.get("alerts") or [])]:
            key = alert.get("id") or (key_of(alert) + str(alert.get("created_at")) + str(alert.get("alert_type")))
            if key not in seen:
                seen.add(key)
                merged.append(alert)
        result["gold_watch_alerts"] = merged[-100:]
    return result


def _paper_first_discovery_positions(paper_state: dict) -> list[dict]:
    positions = paper_state.get("open_positions") if isinstance(paper_state, dict) else None
    if not isinstance(positions, list):
        return []
    return [
        position for position in positions
        if isinstance(position, dict)
        and position.get("status", "open") == "open"
        and position.get("strategy") in {"first_discovery_probe", "narrative_breakout_probe"}
    ]


def _live_order_defaults() -> dict[str, str]:
    """Read explicit live order fields without inventing a trade size."""
    result: dict[str, str] = {}
    amount = os.environ.get("BSC_LIVE_BUY_AMOUNT_ATOMIC", "").strip()
    slippage = os.environ.get("BSC_LIVE_SLIPPAGE_PERCENT", "").strip()
    if amount:
        result["amount_atomic"] = amount
    if slippage:
        result["slippage_percent"] = slippage
    return result


def build_execution_input(
    rows: list[dict],
    quotes: dict[str, dict],
    now: str,
    *,
    chain: str,
    paper_positions: list[dict] | None = None,
) -> dict:
    """Publish classified V2 signals without allowing shadow promotion."""
    signals: list[dict] = []
    preflight_signals: list[dict] = []
    shadow_signals: list[dict] = []
    fresh_quotes: list[dict] = []
    seen_live: set[str] = set()
    seen_preflight: set[str] = set()
    seen_shadow: set[str] = set()
    valuation_rejections: dict[str, dict] = {}
    defaults = _live_order_defaults()
    positions = _paper_first_discovery_positions(
        {"open_positions": paper_positions}
    ) if paper_positions is not None else []
    positions_by_key = {key_of(position): position for position in positions if valid_identity(position)}

    for row in rows:
        if identity(row)[0] != chain or not valid_identity(row):
            continue
        key = key_of(row)
        if row.get("strategy_version") != STRATEGY_VERSION:
            continue
        mode = str(row.get("execution_mode") or "")
        if mode == "shadow":
            if key not in seen_shadow:
                seen_shadow.add(key)
                shadow_signals.append(_public_payload(row))
            continue
        if (mode != "live_candidate" or row.get("eligible") is not True
                or row.get("entry_authorized") is not True):
            continue

        first_seen_at = row.get("first_seen_at")
        quote = quotes.get(key) or {}
        quote_fresh = (
            identity(quote) == identity(row)
            and quote.get("quote_status") == "fresh"
            and 0 <= age_seconds(quote.get("quote_observed_at"), now) <= TTL_SECONDS
            and bool(quote.get("pair_address"))
        )
        if not quote_fresh:
            pool_address = row.get("pool_address") or row.get("pair_address") or row.get("pool")
            reference_age = age_seconds(first_seen_at, now)
            current_mcap = number(row.get("current_mcap_usd") or row.get("mcap") or row.get("market_cap"))
            first_mcap = number(row.get("first_mcap_usd"))
            first_price = number(row.get("first_price_usd"))
            current_price = number(row.get("current_price_usd") or row.get("price_usd") or row.get("price"))
            liquidity = number(row.get("current_liquidity_usd") or row.get("liquidity_usd") or row.get("liquidity"))
            if (
                key not in seen_preflight
                and pool_address
                and 0 <= reference_age <= StrategyPolicy.for_chain(chain).max_entry_delay_seconds
                and first_mcap > 0
                and current_mcap > 0
                and first_price > 0
                and current_price > 0
                and liquidity > 0
                and row.get("valuation_type") in (None, "", "market_cap")
            ):
                seen_preflight.add(key)
                stored_discovery = row.get("discovery_snapshot")
                discovery_snapshot = (
                    dict(stored_discovery)
                    if isinstance(stored_discovery, dict)
                    else {}
                )
                discovery_snapshot.update({
                    "chain": chain,
                    "contract_address": identity(row)[1],
                    "pool_address": pool_address,
                    "pair_address": pool_address,
                    "first_seen_at": discovery_snapshot.get("first_seen_at") or first_seen_at,
                    "price_usd": number(discovery_snapshot.get("price_usd")) or first_price,
                    "mcap": number(discovery_snapshot.get("mcap") or discovery_snapshot.get("market_cap")) or first_mcap,
                    "market_cap": number(discovery_snapshot.get("mcap") or discovery_snapshot.get("market_cap")) or first_mcap,
                    "liquidity": number(discovery_snapshot.get("liquidity_usd") or discovery_snapshot.get("liquidity")) or liquidity,
                    "valuation_type": "market_cap",
                })
                candidate = {
                    **row,
                    **defaults,
                    "chain": chain,
                    "pool_address": pool_address,
                    "pair_address": pool_address,
                    "first_seen_at": first_seen_at,
                    "signal_at": row.get("signal_at") or first_seen_at,
                    "quote_key": key,
                    "quote_at": first_seen_at,
                    "quote_status": "discovery_reference",
                    "snapshot_status": "discovery_reference",
                    "preflight_only": True,
                    "order_authorized": False,
                    "requires_executor_tradeability_check": True,
                    "discovery_snapshot": discovery_snapshot,
                }
                preflight_signals.append(_public_payload(candidate))
            continue
        # Unknown circulating supply must not silently turn FDV into entry MC.
        market_cap = quote.get("mcap", quote.get("market_cap"))
        if (isinstance(market_cap, bool) or number(market_cap) <= 0
                 or ("valuation_type" in quote and quote["valuation_type"] != "market_cap")):
            valuation_rejections[key] = {
                "symbol": str(row.get("symbol") or "MEME")[:40],
                "chain": chain,
                "contract_address": identity(row)[1],
                "reject_reason": "market_cap_unavailable",
                "strategy_version": STRATEGY_VERSION,
                "signal_stage": row.get("signal_stage"),
                "entry_route": row.get("entry_route"),
                "execution_mode": "rejected",
                "rank_score": row.get("rank_score"),
                "rank_components": row.get("rank_components") or {},
                "legacy_score": row.get("legacy_score"),
                "score": row.get("legacy_score"),
                "tradeability": row.get("tradeability") or {"status": "pending"},
                "order_authorized": False,
                "requires_executor_tradeability_check": True,
                "entry_delay_minutes": round(age_seconds(first_seen_at, now) / 60, 2),
                "mcap": None, "market_cap": None, "market_cap_source": None,
                "fdv": quote.get("fdv"), "fdv_source": quote.get("fdv_source"),
                "valuation_type": quote.get("valuation_type", "unavailable"),
            }
            continue
        if key in seen_live:
            continue
        seen_live.add(key)
        normalized_quote = {**quote, "chain": chain, "quote_at": quote.get("quote_observed_at"),
                            "mcap": number(market_cap), "market_cap": number(market_cap),
                            "valuation_type": "market_cap"}
        position = positions_by_key.get(key, {})
        signal = {**row, **defaults}
        signal.update({field: normalized_quote.get(field) for field in (
            "mcap", "market_cap", "fdv", "market_cap_source", "fdv_source", "valuation_type")})
        signal["chain"] = chain
        signal["first_seen_at"] = first_seen_at
        signal["signal_at"] = row.get("signal_at") or first_seen_at
        signal["quote_key"] = key
        signal["quote_at"] = quote.get("quote_observed_at")
        if position:
            signal["paper_strategy"] = position.get("strategy")
            signal["paper_entry_size_usd"] = position.get("size_usd")
        signals.append(_public_payload(signal))
        fresh_quotes.append(_public_payload(normalized_quote))
    signals.sort(key=lambda row: -(row.get("rank_score") if isinstance(row.get("rank_score"), (int, float)) else -1))
    preflight_signals.sort(key=lambda row: -(row.get("rank_score") if isinstance(row.get("rank_score"), (int, float)) else -1))
    shadow_signals.sort(key=lambda row: -(row.get("rank_score") if isinstance(row.get("rank_score"), (int, float)) else -1))
    rejections = list(valuation_rejections.values())
    for row in rows:
        if identity(row)[0] != chain or not valid_identity(row):
            continue
        if row.get("strategy_version") != STRATEGY_VERSION:
            continue
        if row.get("execution_mode") in {"live_candidate", "shadow"}:
            continue
        rejections.append(_strategy_rejection(row))
    rejections = [_public_payload(row) for row in rejections]
    rejections.sort(key=lambda row: (
        -(row.get("rank_score") if isinstance(row.get("rank_score"), (int, float)) else -1),
        row.get("entry_delay_minutes") if isinstance(row.get("entry_delay_minutes"), (int, float)) else float("inf"),
    ))
    return {
        "updated_at": now,
        "mode": "read_only_signal_input",
        "strategy_version": STRATEGY_VERSION,
        "order_authorized": False,
        "requires_executor_tradeability_check": True,
        "signals": signals,
        "preflight_signals": preflight_signals,
        "shadow_signals": shadow_signals,
        "quotes": fresh_quotes,
        "rejections": rejections[:5],
    }


def build_bsc_execution_input(
    rows: list[dict],
    quotes: dict[str, dict],
    now: str,
    *,
    paper_positions: list[dict] | None = None,
) -> dict:
    return build_execution_input(rows, quotes, now, chain="bsc", paper_positions=paper_positions)


def build_robinhood_execution_input(
    rows: list[dict],
    quotes: dict[str, dict],
    now: str,
    *,
    paper_positions: list[dict] | None = None,
) -> dict:
    return build_execution_input(rows, quotes, now, chain="robinhood", paper_positions=paper_positions)


def research_shadow_assessment(
    row: dict,
    history_row: dict | None,
    classification: dict,
) -> dict:
    """Evaluate the first chronological holdout winner without gating execution."""
    snapshot = (
        history_row.get("first_snapshot")
        if isinstance(history_row, dict) and isinstance(history_row.get("first_snapshot"), dict)
        else row
    )
    raw_groups = snapshot.get("source_groups") or []
    source_groups = [str(value) for value in raw_groups] if isinstance(raw_groups, list) else []
    chain = identity(row)[0]
    metrics = {
        "first_mcap_usd": number(classification.get("first_mcap_usd") or snapshot.get("mcap") or snapshot.get("market_cap")),
        "score": number(classification.get("score")),
        "liquidity_usd": number(snapshot.get("liquidity") or snapshot.get("liquidity_usd")),
        "change_h1_pct": number(snapshot.get("change_h1")),
        "change_m5_pct": number(snapshot.get("change_m5")),
        "source_count": number(snapshot.get("source_count")) or len(source_groups),
        "source_groups": source_groups,
    }
    reasons = []
    if not classification.get("eligible"):
        reasons.append("production_classifier_rejected")
    if chain != "robinhood":
        reasons.append("chain_not_validated")
    if not 10_000 <= metrics["first_mcap_usd"] < 300_001:
        reasons.append("first_mcap_outside_10k_300k")
    if not 60 <= metrics["score"] < 70:
        reasons.append("legacy_score_outside_60_69")
    if metrics["liquidity_usd"] < 20_000:
        reasons.append("first_liquidity_below_20k")
    if not -20 <= metrics["change_h1_pct"] <= 80:
        reasons.append("first_h1_outside_minus20_80")
    if not 0 <= metrics["change_m5_pct"] <= 30:
        reasons.append("first_m5_outside_0_30")
    return {
        "policy": "chronological_holdout_robinhood_r1",
        "mode": "shadow_only",
        "eligible": not reasons,
        "reasons": reasons,
        "metrics": metrics,
    }


def build_shadow_entry_report(rows: list[dict], now: str) -> dict:
    v2_shadow = [
        _public_payload(row)
        for row in rows
        if row.get("strategy_version") == STRATEGY_VERSION
        and row.get("execution_mode") == "shadow"
        and valid_identity(row)
    ]
    candidates = []
    for row in rows:
        shadow = row.get("entry_shadow")
        if not isinstance(shadow, dict):
            continue
        candidates.append({
            "chain": identity(row)[0],
            "contract_address": identity(row)[1],
            "symbol": str(row.get("symbol") or "MEME")[:40],
            "first_seen_at": row.get("first_seen_at") or row.get("watch_first_seen_at"),
            "execution_candidate": bool(row.get("execution_candidate")),
            **shadow,
        })
    candidates = [*v2_shadow, *candidates]
    selected = [
        row for row in candidates
        if row.get("execution_mode") == "shadow" or row.get("eligible")
    ]
    return {
        "updated_at": now,
        "mode": "shadow_only_no_execution_effect",
        "strategy_version": STRATEGY_VERSION,
        "order_authorized": False,
        "policy": "chronological_holdout_robinhood_r1",
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected": selected,
        "candidates": candidates,
        "shadow_signals": v2_shadow,
    }


def annotate_new_execution_candidates(
    rows: list[dict],
    out_dir: Path,
    now: str,
    *,
    replay_history: dict | None = None,
) -> list[dict]:
    """Attach the authoritative V2 route classification to monitor rows."""
    history = (
        replay_history
        if replay_history is not None
        else read_json(out_dir / "alpha-radar-replay-history.json")
    )
    annotated: list[dict] = []
    for row in rows:
        if identity(row)[0] not in {"bsc", "robinhood"}:
            annotated.append(row)
            continue
        history_row = _history_row_for(row, history)
        classification = classify_signal(row, history_row or None, now)
        first_seen_at = classification.get("first_seen_at") or row.get("first_seen_at") or row.get("watch_first_seen_at")
        failed_checks = [
            name
            for name, passed in (classification.get("policy_checks") or {}).items()
            if not passed and name not in {"rank_threshold", "decision_bucket"}
        ]
        result = {
            **row,
            **classification,
            # These fields remain during the V1 consumer transition, but V2
            # eligibility above is the only source of live authorization.
            "execution_arm": "first_discovery",
            "route_label": classification.get("entry_route"),
            "signal_at": row.get("signal_at") or first_seen_at,
            "execution_candidate": classification.get("eligible") is True,
            "execution_candidate_score": classification.get("legacy_score"),
            "execution_candidate_reason": failed_checks,
            **({"discovery_snapshot": history_row["discovery_snapshot"]}
               if isinstance(history_row.get("discovery_snapshot"), dict) else {}),
            "entry_delay_minutes": (
                round(number(classification.get("entry_delay_seconds")) / 60, 2)
                if classification.get("entry_delay_seconds") is not None
                else None
            ),
        }
        if classification.get("execution_mode") == "rejected":
            result["execution_rejection"] = _strategy_rejection(result)
            result.pop("execution_arm", None)
        annotated.append(_public_payload(result))
    return annotated


def run_cycle(
    out_dir: Path,
    report_path: Path,
    fetcher=fetch_pairs,
    gmgn_fetcher=None,
    now: str | None = None,
    security_enricher=None,
    live_only: bool = False,
    target_interval_seconds: float = 5,
) -> dict:
    started = time.monotonic()
    phase_started = started
    phase_seconds: dict[str, float] = {}

    def finish_phase(name: str) -> None:
        nonlocal phase_started
        current = time.monotonic()
        phase_seconds[name] = round(current - phase_started, 3)
        phase_started = current

    fixed_now = now
    report = read_json(report_path)
    if not report:
        raise ValueError("report_unavailable")
    previous = read_json(out_dir / "alpha-fast-track.json")
    request_now = now or utc_now()
    request_epoch = _epoch(request_now)
    gmgn_retry_epoch = gmgn_cooldown_epoch(out_dir, previous.get("status") or {})
    gmgn_cooling = gmgn_retry_epoch > request_epoch
    previous_status = previous.get("status") or {}
    retry_at = previous_status.get("retry_after")
    try:
        cooling = bool(retry_at and datetime.fromisoformat(retry_at) > datetime.fromisoformat(request_now))
    except ValueError:
        cooling = False
    failures = int(previous_status.get("rate_limit_failures") or 0)
    replay_path = out_dir / "alpha-radar-replay-history.json"
    previous_replay = read_replay_history_cached(replay_path) if live_only else None
    targets = (
        live_priority_targets(report, out_dir, previous_replay or {}, now=request_now)
        if live_only
        else priority_targets(report, out_dir, previous, now=request_now)
    )
    previous_quotes = previous.get("quotes") if isinstance(previous.get("quotes"), dict) else {}
    safe_previous_quotes = {
        key: monitor_safe_cached_quote(row, previous_quotes.get(key) or {})
        for row in targets
        if (key := key_of(row))
    }
    report_quotes = {
        key_of(row): quote
        for row in targets
        if (quote := quote_from_report_row(row, request_now))
    }
    cached_quotes = {
        key_of(row): prior
        for row in targets
        if key_of(row) not in report_quotes
        and isinstance((prior := safe_previous_quotes.get(key_of(row))), dict)
        and identity(prior) == identity(row)
        and prior.get("quote_status") == "fresh"
        and bool(prior.get("pair_address"))
        and 0 <= age_seconds(prior.get("quote_observed_at"), request_now) <= DEX_QUOTE_REFRESH_SECONDS
    }
    fetch_targets = [
        row for row in targets
        if key_of(row) not in report_quotes and key_of(row) not in cached_quotes
    ]
    batches = []
    grouped: dict[str, list[str]] = {}
    for row in fetch_targets:
        chain, address = identity(row)
        grouped.setdefault(chain, []).append(address)
    for chain, addresses in grouped.items():
        batches.extend((chain, addresses[i:i + 30]) for i in range(0, len(addresses), 30))
    pairs = []
    gmgn_quotes: dict[str, dict] = {}
    errors = ["上游限流退避中，暂停新请求"] if cooling else []
    rate_errors = []
    gmgn_targets = []
    if gmgn_fetcher is not None and not gmgn_cooling:
        for row in fetch_targets:
            if identity(row)[0] == "arc":
                continue
            prior = safe_previous_quotes.get(key_of(row)) or {}
            last_gmgn = prior.get("gmgn_observed_at") if str(prior.get("quote_source") or "").startswith("gmgn") else None
            if age_seconds(last_gmgn, request_now) > GMGN_QUOTE_TTL_SECONDS:
                gmgn_targets.append(row)
            if len(gmgn_targets) >= GMGN_REFRESH_LIMIT:
                break
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(fetcher, chain, addresses): chain for chain, addresses in ([] if cooling else batches)}
        gmgn_jobs = {pool.submit(gmgn_fetcher, *identity(row)): row for row in gmgn_targets}
        for job in as_completed(jobs):
            try:
                pairs.extend(job.result())
            except Exception as exc:
                errors.append(f"{jobs[job]}: {type(exc).__name__}: {exc}")
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    rate_errors.append(exc)
        for job in as_completed(gmgn_jobs):
            row = gmgn_jobs[job]
            try:
                quote = job.result()
                if isinstance(quote, dict):
                    gmgn_quotes[key_of(row)] = quote
            except Exception as exc:
                errors.append(f"gmgn {key_of(row)}: {type(exc).__name__}: {exc}")
                if isinstance(exc, GmgnRateLimitError):
                    gmgn_retry_epoch = max(gmgn_retry_epoch, exc.retry_after_epoch)
                    gmgn_cooling = True
                    write_json(out_dir / "gmgn-wallet-profit-query-status.json", {
                        "ok": False,
                        "rate_limited": True,
                        "source": "alpha_fast_track",
                        "retry_after_epoch": gmgn_retry_epoch,
                        "retry_after": datetime.fromtimestamp(gmgn_retry_epoch, timezone.utc).isoformat(),
                    })
    finish_phase("load_and_fetch_quotes")
    now = now or utc_now()
    if rate_errors:
        failures += 1
        delay = max(retry_delay(exc, failures, now) for exc in rate_errors)
        retry_at = (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat()
    elif not cooling and not errors:
        failures, retry_at = 0, None
    quotes = dict(previous_quotes)
    for row in targets:
        key = key_of(row)
        prior = safe_previous_quotes.get(key) or {}
        dex_quote = report_quotes.get(key) or cached_quotes.get(key) or (prior if cooling else quote_from_pairs(row, pairs, prior, now))
        cached_gmgn = prior if identity(row)[0] != "arc" and str(prior.get("quote_source") or "").startswith("gmgn") and age_seconds(prior.get("gmgn_observed_at"), now) <= GMGN_QUOTE_TTL_SECONDS else {}
        quotes[key] = {**merge_market_quotes(gmgn_quotes.get(key) or cached_gmgn, dex_quote, prior, now), "last_attempt_at": now}
        if identity(row)[0] == "arc":
            quotes[key]["gmgn_holder_status"] = "provider_unavailable"
    quotes = prune_quote_cache(quotes, now, keep_keys={key_of(row) for row in targets})
    fresh_count = sum(q.get("quote_status") == "fresh" and age_seconds(q.get("quote_observed_at"), now) <= TTL_SECONDS for q in quotes.values())
    source_rows = targets if live_only else report_rows(report)
    all_rows = {key_of(row): row for row in source_rows}
    all_rows.update({key_of(row): row for row in targets if key_of(row) not in all_rows})
    rows = [apply_quote(row, quotes.get(key, {}), now) for key, row in all_rows.items()]
    finish_phase("apply_quotes")
    previous_replay = previous_replay if previous_replay is not None else read_replay_history_cached(replay_path)
    if live_only:
        sighting_path = out_dir / "alpha-live-first-sightings.json"
        sightings = update_live_first_sightings(read_json(sighting_path), source_rows, previous_replay, now)
        write_json(sighting_path, sightings)
        # The full monitor owns the large replay. The fast path writes only a
        # small first-sighting journal and can classify the first fresh quote.
        replay_history = build_live_replay_with_sightings(previous_replay, sightings, rows, now)
    else:
        from alpha_replay import update_replay_history

        replay_history = update_replay_history(previous_replay, rows, now)
        write_json(replay_path, replay_history)
    rows = attach_fast_discovery_ranks(rows, replay_history, now)
    finish_phase("replay_and_rank")
    if security_enricher is not None:
        rows = enrich_live_candidate_security(
            rows,
            replay_history,
            out_dir / "alpha-token-security-cache.json",
            now,
            security_enricher,
        )
    finish_phase("live_security")
    from alpha_smart_money_evidence import enrich_smart_money
    rows = enrich_smart_money(rows, out_dir, now)
    finish_phase("smart_money")
    if live_only:
        watch = {"alerts": [], "candidates": {}}
    else:
        from alpha_gold_watch import update_watch_state, public_pick, row_identity

        previous_watch = read_json(out_dir / "alpha-fast-watch-state.json") or read_json(out_dir / "alpha-gold-watch-state.json")
        watch = update_watch_state(previous_watch, rows, now, min_confirmations=3)
        rows = [public_pick(row, (watch.get("candidates") or {}).get(row_identity(row), {})) for row in rows]
    rows_for_execution = [
        row for row in rows
        if normalize_monitor_chain(row.get("chain") or row.get("chain_id")) in EXECUTION_CHAINS
    ]
    annotated_execution = annotate_new_execution_candidates(
        rows_for_execution,
        out_dir,
        now,
        replay_history=replay_history,
    )
    annotated_by_key = {key_of(row): row for row in annotated_execution}
    rows = [annotated_by_key.get(key_of(row), row) for row in rows]
    finish_phase("watch_and_classify")
    entry_shadow = build_shadow_entry_report(rows, now)
    paper_state = read_json(out_dir / "alpha-first-discovery-paper-state.json")
    execution_rows = [
        row for row in rows
        if normalize_monitor_chain(row.get("chain") or row.get("chain_id")) in EXECUTION_CHAINS
    ]
    execution_input = build_bsc_execution_input(
        execution_rows,
        quotes,
        now,
        paper_positions=_paper_first_discovery_positions(paper_state),
    )
    write_json(out_dir / "bsc-execution-input.json", execution_input)
    robinhood_execution_input = build_robinhood_execution_input(
        execution_rows,
        quotes,
        now,
        paper_positions=_paper_first_discovery_positions(paper_state),
    )
    write_json(out_dir / "robinhood-execution-input.json", robinhood_execution_input)
    input_publish_seconds = round(time.monotonic() - started, 3)
    finish_phase("publish_inputs")
    if live_only:
        status = {
            "ok": fresh_count > 0,
            "mode": "live_only",
            "updated_at": now,
            "target_interval_seconds": target_interval_seconds,
            "input_publish_seconds": input_publish_seconds,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "phase_seconds": phase_seconds,
            "tracked_count": len(report_quotes) if cooling else len(targets),
            "fresh_count": fresh_count,
            "universe_count": len(rows),
            "rate_limited": cooling or bool(rate_errors),
            "gmgn_rate_limited": gmgn_cooling,
            "gmgn_retry_after": datetime.fromtimestamp(gmgn_retry_epoch, timezone.utc).isoformat() if gmgn_cooling else None,
            "gmgn_refresh_count": len(gmgn_quotes),
            "retry_after": retry_at,
            "rate_limit_failures": failures,
            "errors": errors,
        }
        write_json(
            out_dir / "alpha-fast-track.json",
            {
                **previous,
                "updated_at": now,
                "quotes": quotes,
                "execution_input": {
                    "path": str(out_dir / "bsc-execution-input.json"),
                    "signal_count": len(execution_input["signals"]),
                    "quote_count": len(execution_input["quotes"]),
                },
                "robinhood_execution_input": {
                    "path": str(out_dir / "robinhood-execution-input.json"),
                    "signal_count": len(robinhood_execution_input["signals"]),
                    "quote_count": len(robinhood_execution_input["quotes"]),
                },
                "status": status,
            },
        )
        write_json(out_dir / "alpha-fast-track-status.json", status)
        return status
    write_json(out_dir / "alpha-fast-watch-state.json", watch)
    write_json(out_dir / "alpha-entry-shadow.json", entry_shadow)
    finish_phase("persist_analysis_state")
    from alpha_gmgn_execution_bridge import export_intents as export_gmgn_intents
    gmgn_intents = export_gmgn_intents(
        out_dir / "bsc-execution-input.json",
        out_dir / "gmgn-execution-intents.json",
    )
    from alpha_execution_audit import run_once
    try:
        execution = run_once({**report, "meme_rows": rows, "meme_watch_universe": rows, "meme_potential_rows": [], "meme_shadow_rows": [], "legacy_ledger": read_json(out_dir / "alpha-first-discovery-paper-state.json")}, out_dir, now)
    except Exception as exc:
        execution = {"ok": False, "updated_at": now, "error": type(exc).__name__,
                     "status": "unavailable"}
    from alpha_wallet_forward_paper import run_once as wallet_paper_once
    paper_now = fixed_now or utc_now()
    try:
        paper = wallet_paper_once(rows, out_dir, paper_now,
                                 wallet_snapshot=read_json(out_dir / "gmgn-smart-money-top50.json"))
        paper_status = {"ok": True, "updated_at": paper_now, **{k: paper[k] for k in (
            "policy", "started_at", "receipt_count", "baseline_count", "eligible_buy_receipts",
            "closed_count", "candidate_count", "realized_pnl_usd", "equity_usd", "pending_valuations",
            "wallet_gates", "profitability_conclusion")},
            "open_count": len(paper["positions"]), "pending_order_count": len(paper["pending_orders"]),
            "last_events": [{k: e.get(k) for k in ("id", "type", "time", "side", "symbol", "key",
                "debit_usd", "proceeds_usd", "pnl_usd", "costs", "reason")} for e in paper["events"]],
            "report_path": str(out_dir / "profitable-wallet-paper" / "report.json")}
    except Exception as exc:
        paper_status = {"ok": False, "updated_at": paper_now, "error": f"{type(exc).__name__}: {exc}"}
    from alpha_execution_challenger import run_once as challenger_once
    try:
        challenger = challenger_once(rows, out_dir, paper_now)
        challenger_status = {"ok": True, "updated_at": challenger["at"],
            "status": "预热中" if challenger["warmup"] else "前向模拟中",
            **{k: challenger.get(k) for k in ("policy", "started_at", "warmup", "cash_usd",
                "equity_usd", "realized_pnl_usd", "pending_valuations", "observation_count",
                "candidate_count", "exclusion_counts", "arms", "profitability_conclusion")},
            "open_count": len(challenger["positions"]),
            "pending_count": len(challenger["pending_orders"])}
    except Exception as exc:
        challenger_status = {"ok": False, "updated_at": paper_now,
                             "error": type(exc).__name__, "status": "独立试验暂停"}
    payload = {"updated_at": now, "quotes": quotes, "evidence": {key_of(r): r.get("smart_money_evidence", {}) for r in rows},
               "alerts": watch.get("alerts") or [], "execution_audit": execution,
               "execution_challenger": challenger_status,
               "gmgn_execution": {"status": gmgn_intents.get("status"), "count": gmgn_intents.get("count", 0),
                                  "submitted": False, "configuration": gmgn_intents.get("configuration")},
               "execution_input": {"path": str(out_dir / "bsc-execution-input.json"),
                                   "signal_count": len(execution_input["signals"]),
                                   "quote_count": len(execution_input["quotes"])},
               "robinhood_execution_input": {"path": str(out_dir / "robinhood-execution-input.json"),
                                              "signal_count": len(robinhood_execution_input["signals"]),
                                              "quote_count": len(robinhood_execution_input["quotes"])},
               "entry_shadow": {"path": str(out_dir / "alpha-entry-shadow.json"),
                                "mode": entry_shadow["mode"],
                                "candidate_count": entry_shadow["candidate_count"],
                                "selected_count": entry_shadow["selected_count"]},
               "watch_fields": {key_of(r): {k: v for k, v in r.items() if k.startswith("watch_") or k in {
                   "confirmation_status", "confirmation_reason", "confirmation_basis", "smart_money_confirmation"
               }} for r in rows},
               "status": {"ok": fresh_count > 0, "updated_at": now, "target_interval_seconds": 10,
                          "execution_audit": {k: execution.get(k) for k in ("ok", "updated_at", "error", "status")},
                          "wallet_forward_paper": paper_status,
                          "execution_challenger": challenger_status,
                          "input_publish_seconds": input_publish_seconds,
                          "phase_seconds": phase_seconds,
                          "tracked_count": 0 if cooling else len(targets), "fresh_count": fresh_count, "universe_count": len(rows),
                          "rate_limited": cooling or bool(rate_errors), "retry_after": retry_at, "rate_limit_failures": failures,
                          "gmgn_rate_limited": gmgn_cooling,
                          "gmgn_retry_after": datetime.fromtimestamp(gmgn_retry_epoch, timezone.utc).isoformat() if gmgn_cooling else None,
                          "gmgn_refresh_count": len(gmgn_quotes),
                          "errors": errors, "elapsed_seconds": round(time.monotonic() - started, 3)}}
    write_json(out_dir / "alpha-fast-track.json", payload)
    write_json(out_dir / "alpha-fast-track-status.json", payload["status"])
    quote_journal = out_dir / "alpha-fast-quotes.jsonl"
    compact_jsonl(quote_journal)
    with quote_journal.open("a", encoding="utf-8") as stream:
        for row in rows:
            key = key_of(row)
            q = quotes.get(key) or {}
            if q.get("quote_status") == "fresh" and q.get("quote_fingerprint") != (previous.get("quotes", {}).get(key) or {}).get("quote_fingerprint"):
                stream.write(json.dumps({**q, "symbol": row.get("symbol"), "smart_money_evidence": row.get("smart_money_evidence")}, ensure_ascii=False) + "\n")
    return payload["status"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--report", type=Path, default=OUT_DIR / "alpha-radar-report-latest.json")
    parser.add_argument("--interval-seconds", type=float, default=10)
    parser.add_argument("--live-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, "Local\\AlphaFastTrack-" + hashlib.sha256(str(args.out_dir.resolve()).encode()).hexdigest()[:16])
        if not mutex or ctypes.get_last_error() == 183:
            return 0
    while True:
        started = time.monotonic()
        try:
            from alpha_token_security import enrich_token_security

            status = run_cycle(
                args.out_dir,
                args.report,
                gmgn_fetcher=__import__("alpha_gmgn_quote").fetch_gmgn_token_info,
                security_enricher=enrich_token_security,
                live_only=args.live_only,
                target_interval_seconds=args.interval_seconds,
            )
        except Exception as exc:
            status = {"ok": False, "updated_at": utc_now(), "error": f"{type(exc).__name__}: {exc}"}
            write_json(args.out_dir / "alpha-fast-track-status.json", status)
        if args.once:
            print(json.dumps(status, ensure_ascii=False))
            return 0 if status.get("ok") else 1
        time.sleep(max(1, args.interval_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
