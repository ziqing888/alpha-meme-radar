#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import alpha_pool_radar as alpha
import alpha_bsc_public_pair_poll
import alpha_985_monitor_export
import alpha_first_discovery_paper
import alpha_gmgn_smart_money_top50
import alpha_noxa_telegram_export
import alpha_proficy_trending_export
import alpha_tweet_sources
import alpha_wallet_style_paper
import alpha_wind_monitor_export
import alpha_prelaunch_watch
import alpha_gmgn_wallet_flow
import alpha_gmgn_skills
from alpha_discovery_rank import compute_discovery_rank
from alpha_smart_money_evidence import enrich_smart_money
from alpha_fast_track import age_seconds
from alpha_fast_track import fetch_pairs as fetch_fast_pairs
from alpha_fast_track import identity as fast_identity
from alpha_fast_track import key_of as fast_key_of
from alpha_fast_track import quote_from_pairs
from alpha_fast_track import valid_identity as valid_fast_identity
from alpha_gold_watch import load_watch_state
from alpha_gold_watch import row_identity
from alpha_gold_watch import update_watch_state
from alpha_gold_watch import watch_summary
from alpha_gold_watch import write_watch_state
from alpha_local_voice import play_voice_alert as play_local_voice_alert
from alpha_gold_backtest import build_gold_backtest
from alpha_meme import load_meme_candidates
from alpha_meme import load_meme_observation_batch
from alpha_meme import DEFAULT_MEME_CHAINS
from alpha_meme import meme_chain_allowed
from alpha_meme import optional_meme_source_status
from alpha_meme_potential import annotate_meme_heat
from alpha_meme_potential import annotate_meme_early_conviction
from alpha_meme_potential import build_meme_conviction_rows
from alpha_meme_potential import build_meme_heat_rows
from alpha_meme_potential import build_meme_potential_rows
from alpha_narrative import apply_meme_seed_terms
from alpha_narrative import apply_tweet_narrative_seeds
from alpha_radar_report import attach_gold_watch_fields
from alpha_radar_report import attach_replay_rows
from alpha_radar_report import batch_error_messages
from alpha_radar_report import build_shadow_meme_rows
from alpha_radar_report import load_replay_history
from alpha_radar_report import load_meme_seed_report
from alpha_radar_report import publish_token_intelligence_cache
from alpha_radar_report import load_tweet_narrative_report
from alpha_radar_report import publish_report_monitor_v3
from alpha_radar_report import publish_latest_report
from alpha_radar_report import newest_monitor_v3
from alpha_radar_report import refresh_token_intelligence
from alpha_radar_report import schedule_token_intelligence_research
from alpha_radar_report import wait_for_token_intelligence_research
from alpha_replay import replay_action_calibration
from alpha_replay import replay_leaderboards
from alpha_replay import update_replay_history
from alpha_rating_v2_dataset import build_dataset as build_rating_v2_dataset
from alpha_rating_v2_train import refresh_models as refresh_rating_v2_models
from alpha_rating_v2_score import attach_shadow_scores as attach_rating_v2_scores


_DEFAULT_MEME_CANDIDATE_LOADER = load_meme_candidates
_DEFAULT_MEME_OBSERVATION_LOADER = load_meme_observation_batch


def collect_meme_observation_batch(limit: int, concurrency: int) -> dict[str, Any]:
    if load_meme_observation_batch is not _DEFAULT_MEME_OBSERVATION_LOADER:
        return load_meme_observation_batch(limit, concurrency)
    if load_meme_candidates is not _DEFAULT_MEME_CANDIDATE_LOADER:
        candidates, errors = load_meme_candidates(limit, concurrency)
        return {
            "candidates": candidates, "events": [], "rejections": [],
            "errors": errors, "feed_counts": {}, "_legacy_candidates_only": True,
        }
    return load_meme_observation_batch(limit, concurrency)


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
REPORT_PATH = OUT_DIR / "alpha-radar-report-latest.json"
STATUS_PATH = OUT_DIR / "alpha-meme-live-refresh-status.json"
PAPER_STATUS_PATH = OUT_DIR / "alpha-paper-trading-status.json"
GOLD_STATE_PATH = OUT_DIR / "alpha-gold-watch-state.json"
REPLAY_HISTORY_PATH = OUT_DIR / "alpha-radar-replay-history.json"
REPLAY_FOLLOWUP_STATUS_PATH = OUT_DIR / "alpha-replay-followup-status.json"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_VOICE_ALERT_TEXT = "金狗来了，注意观察"
DEFAULT_VOICE_ALERT_REPEAT = 3
VOICE_ALERT_MESSAGES = {
    "discovered": "金狗来了，发现 {symbol}。",
    "early": "金狗来了，发现 {symbol}。",
    "confirmed": "{symbol}，信号确认。",
    "high_risk": "发现 {symbol}，高风险，注意观察。",
    "deterioration": "{symbol}，信号转弱。",
    "runner": "{symbol}，金狗正在启动。",
    "recovered": "{symbol}，二次启动，注意观察。",
    "pullback": "{symbol}，注意回撤。",
    "invalidated": "{symbol}，信号失效。",
}
DEFAULT_BSC_PAIR_POLL_LOOKBACK_BLOCKS = 20
DEFAULT_BSC_PAIR_POLL_TIMEOUT_SECONDS = 8


def attach_discovery_ranks(rows: list[dict[str, Any]], replay_history: dict[str, Any], now: str) -> None:
    """Attach an ordering-only V2 rank using the immutable first snapshot."""
    evaluated_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
    history_rows = replay_history.get("rows") if isinstance(replay_history.get("rows"), dict) else {}
    for row in rows:
        history = history_rows.get(row_identity(row)) if isinstance(history_rows, dict) else None
        snapshot = history.get("first_snapshot") if isinstance(history, dict) else None
        row.update(compute_discovery_rank(row, first_snapshot=snapshot, now=evaluated_at))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            tmp_path.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.025 * (2 ** attempt))


def replay_followup_interval_seconds(age_seconds_from_first: float) -> int:
    if age_seconds_from_first <= 15 * 60:
        return 60
    if age_seconds_from_first <= 60 * 60:
        return 180
    if age_seconds_from_first <= 6 * 60 * 60:
        return 900
    return 3600


def select_replay_followup_targets(
    replay_history: dict[str, Any],
    current_rows: list[dict[str, Any]],
    now: str,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Select recent candidates that disappeared from the current source pages."""
    current_keys = {
        fast_key_of(row)
        for row in current_rows
        if isinstance(row, dict) and valid_fast_identity(row)
    }
    raw_rows = replay_history.get("rows") or {}
    history_rows = raw_rows.values() if isinstance(raw_rows, dict) else raw_rows
    due: list[tuple[tuple[int, float], dict[str, Any]]] = []
    for source in history_rows:
        if not isinstance(source, dict) or not valid_fast_identity(source):
            continue
        key = fast_key_of(source)
        if key in current_keys:
            continue
        first_age = age_seconds(source.get("first_seen_at"), now)
        if not 0 <= first_age <= 24 * 60 * 60:
            continue
        snapshot = source.get("first_snapshot") if isinstance(source.get("first_snapshot"), dict) else {}
        try:
            first_mcap = float(snapshot.get("mcap") or snapshot.get("market_cap") or 0)
            first_price = float(source.get("first_price_usd") or snapshot.get("price_usd") or 0)
        except (TypeError, ValueError):
            continue
        if not 5_000 <= first_mcap <= 500_000 or first_price <= 0:
            continue
        latest_age = age_seconds(source.get("latest_seen_at") or source.get("first_seen_at"), now)
        if latest_age < replay_followup_interval_seconds(first_age):
            continue
        trajectory = source.get("trajectory") if isinstance(source.get("trajectory"), dict) else {}
        complete_rank = 0 if trajectory.get("coverage_from_first") is True else 1
        due.append(((complete_rank, -latest_age), source))
    due.sort(key=lambda item: item[0])
    return [{**row, "key": fast_key_of(row)} for _, row in due[: max(0, int(limit))]]


def refresh_replay_followups(
    replay_history: dict[str, Any],
    current_rows: list[dict[str, Any]],
    now: str,
    *,
    fetcher: Any = fetch_fast_pairs,
    limit: int = 60,
) -> dict[str, Any]:
    selected = select_replay_followup_targets(replay_history, current_rows, now, limit=limit)
    batches: list[tuple[str, list[dict[str, Any]]]] = []
    by_chain: dict[str, list[dict[str, Any]]] = {}
    for row in selected:
        chain, _ = fast_identity(row)
        by_chain.setdefault(chain, []).append(row)
    for chain, rows in by_chain.items():
        for offset in range(0, len(rows), 30):
            batches.append((chain, rows[offset : offset + 30]))

    refreshed: list[dict[str, Any]] = []
    errors: list[str] = []

    def fetch_batch(chain: str, rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        addresses = [fast_identity(row)[1] for row in rows]
        return chain, rows, fetcher(chain, addresses)

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(batches)))) as pool:
        futures = {pool.submit(fetch_batch, chain, rows): (chain, rows) for chain, rows in batches}
        for future in as_completed(futures):
            chain, rows = futures[future]
            try:
                _, rows, pairs = future.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{chain}: {exc}")
                continue
            for row in rows:
                snapshot = row.get("first_snapshot") if isinstance(row.get("first_snapshot"), dict) else {}
                previous_quote = {
                    **snapshot,
                    "chain": row.get("chain"),
                    "contract_address": row.get("contract_address"),
                    "price_usd": row.get("latest_price_usd"),
                }
                quote = quote_from_pairs(row, pairs, previous_quote, now)
                if quote.get("quote_status") != "fresh":
                    continue
                refreshed.append({
                    "symbol": row.get("symbol") or "",
                    "recommendation_bucket": row.get("recommendation_bucket") or "shadow",
                    **quote,
                })
    return {
        "rows": refreshed,
        "status": {
            "checked_at": now,
            "selected": len(selected),
            "fresh": len(refreshed),
            "errors": errors,
            "mode": "post_report_followup",
        },
    }


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def play_voice_alert(message: str = DEFAULT_VOICE_ALERT_TEXT, repeat_count: int = DEFAULT_VOICE_ALERT_REPEAT) -> dict[str, Any]:
    return play_local_voice_alert(message, repeat_count, allow_generate=True)


def build_voice_alert_status(
    previous_status: dict[str, Any],
    *,
    enabled: bool,
    message: str,
    repeat_count: int,
) -> dict[str, Any]:
    previous_voice = previous_status.get("voice_alert") if isinstance(previous_status.get("voice_alert"), dict) else {}
    return {
        "enabled": bool(enabled),
        "triggered": False,
        "reason": "waiting_for_first_confirmation" if enabled else "disabled",
        "message": message,
        "repeat_count": max(1, min(10, int(repeat_count))),
        "last_triggered_at": previous_voice.get("last_triggered_at"),
        "last_trigger_symbols": previous_voice.get("last_trigger_symbols") or [],
        "last_tested_at": previous_voice.get("last_tested_at"),
        "last_alert_type": previous_voice.get("last_alert_type"),
    }


def refresh_rating_v2_shadow(
    dataset: dict[str, Any],
    *,
    out_dir: Path,
    now: str,
) -> dict[str, Any]:
    """Refresh optional shadow models without blocking the live discovery report."""
    status_path = out_dir / "alpha-rating-v2-model-status.json"
    previous = read_json(status_path)
    try:
        status = refresh_rating_v2_models(
            dataset,
            model_dir=out_dir / "alpha-rating-v2-models",
            previous_status=previous,
            now=now,
        )
        status["refresh_status"] = "ok"
        status.pop("refresh_error", None)
        status.pop("refresh_error_type", None)
    except Exception as exc:  # noqa: BLE001
        status = dict(previous)
        status.setdefault("mode", "shadow_only_no_execution_effect")
        status.setdefault("targets", {})
        status.setdefault("ready_target_count", 0)
        status["checked_at"] = now
        status["dataset_training_rows"] = len(dataset.get("rows") or [])
        status["refresh_status"] = "degraded"
        status["refresh_error_type"] = type(exc).__name__
        status["refresh_error"] = str(exc)
    atomic_write_json(status_path, status)
    return status


def voice_symbol(alert: dict[str, Any]) -> str:
    symbol = " ".join(str(alert.get("symbol") or alert.get("name") or "新币").strip().split())
    if symbol.isascii() and symbol:
        return " ".join(symbol[:24])
    return symbol[:24]


def voice_alert_message(alerts: list[dict[str, Any]], fallback: str) -> tuple[str, str]:
    """Choose one short phrase from the event type; details stay in the popup."""
    valid_alerts = [alert for alert in alerts if isinstance(alert, dict)]
    alert_priority = (
        "discovered", "early", "confirmed", "runner", "recovered",
        "high_risk", "pullback", "deterioration", "invalidated",
    )
    for wanted_type in alert_priority:
        for alert in valid_alerts:
            alert_type = str(alert.get("alert_type") or alert.get("type") or "").strip().lower()
            if alert_type == wanted_type:
                return VOICE_ALERT_MESSAGES[alert_type].format(symbol=voice_symbol(alert)), alert_type
    return fallback, "test" if not alerts else "unknown"


def apply_voice_alert(
    voice_status: dict[str, Any],
    *,
    alerts: list[dict[str, Any]],
    force_test: bool = False,
) -> dict[str, Any]:
    if not voice_status.get("enabled"):
        return voice_status
    should_trigger = force_test or bool(alerts)
    if not should_trigger:
        return voice_status
    configured_message = str(voice_status.get("message") or DEFAULT_VOICE_ALERT_TEXT)
    if force_test and not alerts:
        spoken_message, alert_type = "金狗来了，注意观察。", "test"
    else:
        spoken_message, alert_type = voice_alert_message(alerts, configured_message)
    voice_result = play_voice_alert(spoken_message, int(voice_status.get("repeat_count") or 1))
    now = now_iso()
    symbols = [str(alert.get("symbol") or alert.get("name") or "").strip() for alert in alerts if isinstance(alert, dict)]
    symbols = [symbol for symbol in symbols if symbol]
    next_status = {
        **voice_status,
        **voice_result,
        "message": spoken_message,
        "last_alert_type": alert_type,
        "triggered": bool(voice_result.get("ok")),
        "reason": "voice_test" if force_test and voice_result.get("ok") else ("" if voice_result.get("ok") else voice_result.get("reason") or "voice_alert_failed"),
    }
    if force_test:
        next_status["last_tested_at"] = now
    if voice_result.get("ok") and alerts:
        next_status["last_triggered_at"] = now
        next_status["last_trigger_symbols"] = symbols[:5]
    return next_status


def section_quality(previous_report: dict[str, Any], *, used_previous_meme: bool, meme_errors: list[str], meme_rows: list[dict[str, Any]]) -> dict[str, Any]:
    previous_meta = previous_report.get("meta") if isinstance(previous_report.get("meta"), dict) else {}
    previous_quality = previous_meta.get("section_quality") if isinstance(previous_meta.get("section_quality"), dict) else {}
    return {
        **previous_quality,
        "meme": "cached" if used_previous_meme else ("failed" if meme_errors and not meme_rows else ("partial" if meme_errors else "live")),
    }


def refresh_bsc_public_pairs(out_dir: Path) -> dict[str, Any]:
    if os.environ.get("ALPHA_BSC_PUBLIC_PAIR_POLL_DISABLE", "").strip():
        return {"ok": False, "reason": "disabled"}
    args = argparse.Namespace(
        rpc_urls=[],
        factory=os.environ.get("PANCAKE_V2_FACTORY", alpha_bsc_public_pair_poll.bsc_onchain.PANCAKE_V2_FACTORY),
        out=out_dir / "meme-source-inbox" / "bsc-pancake-pairs.json",
        status=out_dir / "bsc-public-pair-poll-status.json",
        max_rows=max(1, alpha.to_int(os.environ.get("BSC_PUBLIC_PAIR_MAX_ROWS"), default=160)),
        lookback_blocks=max(1, alpha.to_int(os.environ.get("BSC_PUBLIC_PAIR_LOOKBACK_BLOCKS"), default=DEFAULT_BSC_PAIR_POLL_LOOKBACK_BLOCKS)),
        timeout_seconds=max(3, alpha.to_int(os.environ.get("BSC_PUBLIC_PAIR_TIMEOUT_SECONDS"), default=DEFAULT_BSC_PAIR_POLL_TIMEOUT_SECONDS)),
        quote_assets=[],
        broad=bool(os.environ.get("BSC_PUBLIC_PAIR_BROAD", "").strip()),
    )
    return alpha_bsc_public_pair_poll.run_once(args)


def refresh_noxa_launchpad(out_dir: Path) -> dict[str, Any]:
    if os.environ.get("NOXA_LAUNCHPAD_DISABLE", "").strip():
        return {"ok": False, "reason": "disabled"}
    out_path = out_dir / "meme-source-inbox" / "noxa-launches.json"
    status_path = out_dir / "noxa-telegram-export-status.json"
    started = time.time()
    status: dict[str, Any] = {
        "source": "noxa_launchpad",
        "channel": "memmememjk",
        "ok": False,
        "out": str(out_path),
        "checked_at": now_iso(),
    }
    try:
        html_text = alpha_noxa_telegram_export.fetch_text(
            os.environ.get("NOXA_LAUNCHPAD_URL", alpha_noxa_telegram_export.DEFAULT_URL),
            max(3, alpha.to_int(os.environ.get("NOXA_LAUNCHPAD_TIMEOUT_SECONDS"), default=8)),
        )
        limit = max(1, alpha.to_int(os.environ.get("NOXA_LAUNCHPAD_LIMIT"), default=30))
        rows = alpha_noxa_telegram_export.parse_public_preview(html_text)[:limit]
        alpha_noxa_telegram_export.write_payload(rows, out_path)
        status.update({"ok": True, "row_count": len(rows)})
    except Exception as exc:  # noqa: BLE001
        status.update({"error": str(exc)})
    status["elapsed_seconds"] = round(time.time() - started, 3)
    alpha_noxa_telegram_export.write_json(status_path, status)
    return status


def refresh_proficy_trending(out_dir: Path) -> dict[str, Any]:
    if os.environ.get("PROFICY_TRENDING_DISABLE", "").strip():
        return {"ok": False, "reason": "disabled"}
    out_path = out_dir / "meme-source-inbox" / "proficy-trending.json"
    status_path = out_dir / "proficy-trending-export-status.json"
    started = time.time()
    status: dict[str, Any] = {
        "source": "proficy_trending",
        "ok": False,
        "out": str(out_path),
        "checked_at": now_iso(),
    }
    try:
        html_text = alpha_proficy_trending_export.fetch_text(
            os.environ.get("PROFICY_TRENDING_URL", alpha_proficy_trending_export.DEFAULT_URL),
            max(3, alpha.to_int(os.environ.get("PROFICY_TRENDING_TIMEOUT_SECONDS"), default=8)),
        )
        limit = max(1, alpha.to_int(os.environ.get("PROFICY_TRENDING_LIMIT"), default=25))
        rows = alpha_proficy_trending_export.parse_trending_page(html_text)[:limit]
        alpha_proficy_trending_export.write_payload(rows, out_path)
        status.update({"ok": True, "row_count": len(rows)})
    except Exception as exc:  # noqa: BLE001
        status.update({"error": str(exc)})
    status["elapsed_seconds"] = round(time.time() - started, 3)
    alpha_proficy_trending_export.write_json(status_path, status)
    return status


def refresh_985_monitor(out_dir: Path) -> dict[str, Any]:
    if os.environ.get("MONITOR985_DISABLE", "").strip():
        return {"ok": False, "reason": "disabled"}
    out_path = out_dir / "meme-source-inbox" / "985-monitor.json"
    fomo_out_path = out_dir / "meme-source-inbox" / "985-fomo-wallets.json"
    smartmoney_out_path = out_dir / "meme-source-inbox" / "985-smartmoney.json"
    status_path = out_dir / "985-monitor-export-status.json"
    started = time.time()
    status: dict[str, Any] = {
        "source": "985_monitor",
        "ok": False,
        "out": str(out_path),
        "fomo_out": str(fomo_out_path),
        "smartmoney_out": str(smartmoney_out_path),
        "checked_at": now_iso(),
    }
    try:
        base_url = os.environ.get("MONITOR985_BASE_URL", alpha_985_monitor_export.DEFAULT_BASE_URL)
        timeout = max(3, alpha.to_int(os.environ.get("MONITOR985_TIMEOUT_SECONDS"), default=8))
        limit = max(1, alpha.to_int(os.environ.get("MONITOR985_LIMIT"), default=20))
        rows, counts = alpha_985_monitor_export.collect_rows(
            base_url,
            limit,
            timeout,
        )
        alpha_985_monitor_export.write_payload(rows, out_path, base_url=base_url)

        fomo_rows: list[dict[str, Any]] = []
        fomo_counts: dict[str, int] = {}
        if not os.environ.get("MONITOR985_FOMO_DISABLE", "").strip():
            fomo_rows, fomo_counts = alpha_985_monitor_export.collect_fomo_wallet_rows(
                base_url,
                max(1, alpha.to_int(os.environ.get("MONITOR985_FOMO_HANDLE_LIMIT"), default=5)),
                max(1, alpha.to_int(os.environ.get("MONITOR985_FOMO_TRADE_LIMIT"), default=20)),
                timeout,
            )
            alpha_985_monitor_export.write_payload(
                fomo_rows,
                fomo_out_path,
                base_url=base_url,
                source="985_fomo_wallets",
                origin="public_985monitor_fomo",
            )

        smartmoney_rows: list[dict[str, Any]] = []
        smartmoney_counts: dict[str, int] = {}
        if not os.environ.get("MONITOR985_SMARTMONEY_DISABLE", "").strip():
            smartmoney_rows, smartmoney_counts = alpha_985_monitor_export.collect_smartmoney_rows(
                base_url,
                max(1, alpha.to_int(os.environ.get("MONITOR985_SMARTMONEY_LIMIT"), default=40)),
                timeout,
            )
            alpha_985_monitor_export.write_payload(
                smartmoney_rows,
                smartmoney_out_path,
                base_url=base_url,
                source="985_smartmoney",
                origin="public_985monitor_smartmoney",
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
            }
        )
    except Exception as exc:  # noqa: BLE001
        status.update({"error": str(exc)})
    status["elapsed_seconds"] = round(time.time() - started, 3)
    alpha_985_monitor_export.write_json(status_path, status)
    return status


def refresh_wind_monitor(out_dir: Path) -> dict[str, Any]:
    if os.environ.get("WIND_MONITOR_DISABLE", "").strip():
        return {"ok": False, "reason": "disabled"}
    args = argparse.Namespace(
        base_url=os.environ.get("WIND_MONITOR_BASE_URL", alpha_wind_monitor_export.DEFAULT_BASE_URL),
        out=out_dir / "meme-source-inbox" / "wind-monitor.json",
        status=out_dir / "wind-monitor-export-status.json",
        timeout_seconds=max(3, alpha.to_int(os.environ.get("WIND_MONITOR_TIMEOUT_SECONDS"), default=10)),
        limit=max(1, alpha.to_int(os.environ.get("WIND_MONITOR_LIMIT"), default=40)),
        max_handles=max(1, alpha.to_int(os.environ.get("WIND_MONITOR_MAX_HANDLES"), default=12)),
        feed_actions=os.environ.get("WIND_MONITOR_FEED_ACTIONS", alpha_wind_monitor_export.DEFAULT_FEED_ACTIONS),
        use_catalog=bool(os.environ.get("WIND_MONITOR_USE_CATALOG_DEFAULT", "").strip()),
        overwrite_empty=bool(os.environ.get("WIND_MONITOR_OVERWRITE_EMPTY", "").strip()),
    )
    return alpha_wind_monitor_export.run_once(args)


def refresh_gmgn_smart_money_top50(out_dir: Path) -> dict[str, Any]:
    if os.environ.get("GMGN_SMART_MONEY_TOP50_DISABLE", "").strip():
        return {"ok": True, "skipped": True, "reason": "disabled"}
    return alpha_gmgn_smart_money_top50.run_once(
        inbox_dir=out_dir / "meme-source-inbox",
        json_out=out_dir / "gmgn-smart-money-top50.json",
        markdown_out=out_dir / "gmgn-smart-money-top50.md",
        limit=max(1, alpha.to_int(os.environ.get("GMGN_SMART_MONEY_TOP50_LIMIT"), default=50)),
        max_age_days=max(0.0, alpha.to_float(os.environ.get("GMGN_SMART_MONEY_TOP50_MAX_AGE_DAYS") or 3)),
        force=bool(os.environ.get("GMGN_SMART_MONEY_TOP50_FORCE", "").strip()),
        chains=alpha_gmgn_smart_money_top50.chain_list(),
        cli_timeout_seconds=max(3, alpha.to_int(os.environ.get("GMGN_SMART_MONEY_CLI_TIMEOUT_SECONDS"), default=12)),
    )


def refresh_gmgn_skills(out_dir: Path) -> dict[str, Any]:
    if os.environ.get("GMGN_SKILLS_DISABLE", "").strip():
        return {"ok": False, "skipped": True, "reason": "disabled"}
    return alpha_gmgn_skills.refresh(
        out_dir,
        limit=max(1, alpha.to_int(os.environ.get("GMGN_SKILLS_LIMIT"), default=50)),
        timeout_seconds=max(3, alpha.to_int(os.environ.get("GMGN_SKILLS_TIMEOUT_SECONDS"), default=12)),
    )


def refresh_gmgn_sources(out_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use the native Skills track feed without the legacy per-wallet N+1 poller."""
    skills_status = refresh_gmgn_skills(out_dir)
    wallet_status = {
        "ok": True,
        "skipped": True,
        "reason": "superseded_by_gmgn_skills_track",
    }
    return skills_status, wallet_status


def build_meme_live_report(
    previous_report: dict[str, Any],
    *,
    out_dir: Path,
    meme_source_limit: int,
    meme_top: int,
    meme_potential_top: int,
    meme_shadow_top: int,
    concurrency: int,
    gold_watch_confirmations: int,
    tweet_source_status: dict[str, Any] | None = None,
    bsc_pair_status: dict[str, Any] | None = None,
    noxa_launchpad_status: dict[str, Any] | None = None,
    proficy_trending_status: dict[str, Any] | None = None,
    monitor985_status: dict[str, Any] | None = None,
    wind_monitor_status: dict[str, Any] | None = None,
    gmgn_smart_money_top50_status: dict[str, Any] | None = None,
    gmgn_skills_status: dict[str, Any] | None = None,
    fast_publish: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    alpha.configure_cache(out_dir)
    monitor_observed_at = now_iso()
    try:
        observation_batch = collect_meme_observation_batch(meme_source_limit, concurrency)
    except Exception as exc:  # noqa: BLE001
        observation_batch = {
            "candidates": [], "events": [], "rejections": [],
            "errors": [f"meme_fast_source_error: {exc}"], "feed_counts": {},
        }
    meme_rows = observation_batch["candidates"]
    meme_errors = batch_error_messages(observation_batch)
    monitor_v3 = publish_report_monitor_v3(
        out_dir=out_dir,
        batch=observation_batch,
        observed_at=monitor_observed_at,
        previous_snapshot=previous_report.get("monitor_v3"),
        persist_latest=False,
    )
    used_previous_meme = False
    if not meme_rows and meme_errors:
        previous_meme = previous_report.get("meme_rows") or []
        if isinstance(previous_meme, list) and previous_meme:
            meme_rows = previous_meme
            used_previous_meme = True
            meme_errors.append("meme_rows fell back to previous report cache")
    if used_previous_meme:
        meme_rows = [
            row
            for row in meme_rows
            if not (row.get("chain") or row.get("chain_id")) or meme_chain_allowed(row.get("chain") or row.get("chain_id"))
        ]
    else:
        meme_rows = [row for row in meme_rows if meme_chain_allowed(row.get("chain") or row.get("chain_id"))]
    chain_scope = os.environ.get("ALPHA_MEME_CHAINS", DEFAULT_MEME_CHAINS)
    meme_rows = enrich_smart_money(meme_rows, out_dir, now_iso())
    tweet_narrative = load_tweet_narrative_report(out_dir)
    tweet_seeds = tweet_narrative.get("seeds") or []
    if isinstance(tweet_seeds, list) and tweet_seeds:
        meme_rows = apply_tweet_narrative_seeds(meme_rows, tweet_seeds)
    meme_seed_terms = load_meme_seed_report(out_dir)
    meme_seeds = meme_seed_terms.get("seeds") or []
    if isinstance(meme_seeds, list) and meme_seeds:
        meme_rows = apply_meme_seed_terms(meme_rows, meme_seeds)
    meme_rows = [annotate_meme_heat(annotate_meme_early_conviction(row)) for row in meme_rows]

    previous_replay_history = load_replay_history(out_dir)
    generated_at = now_iso()
    attach_discovery_ranks(meme_rows, previous_replay_history, generated_at)
    replay_calibration = replay_action_calibration(previous_replay_history.get("summary") or {})
    meme_potential_rows = build_meme_potential_rows(
        meme_rows,
        limit=meme_potential_top,
        replay_calibration=replay_calibration,
        chain_scope=chain_scope,
    )
    meme_shadow_rows = build_shadow_meme_rows(
        meme_rows,
        selected_rows=meme_potential_rows,
        limit=meme_shadow_top,
        replay_calibration=replay_calibration,
        chain_scope=chain_scope,
    )
    attach_discovery_ranks(meme_potential_rows, previous_replay_history, generated_at)
    attach_discovery_ranks(meme_shadow_rows, previous_replay_history, generated_at)

    all_watch_rows = {row_identity(row): row for row in meme_rows}
    for row in [*meme_shadow_rows, *meme_potential_rows]:
        all_watch_rows[row_identity(row)] = row
    previous_gold_watch_state = load_watch_state(out_dir / "alpha-gold-watch-state.json")
    previous_alerts = {json.dumps(item, sort_keys=True) for item in previous_gold_watch_state.get("alerts") or []}
    gold_watch_state = update_watch_state(
        previous_gold_watch_state,
        list(all_watch_rows.values()),
        generated_at,
        min_confirmations=gold_watch_confirmations,
        chain_scope=chain_scope,
    )
    new_gold_watch_alerts = [item for item in gold_watch_state.get("alerts") or [] if json.dumps(item, sort_keys=True) not in previous_alerts]
    write_watch_state(out_dir / "alpha-gold-watch-state.json", gold_watch_state)
    attach_gold_watch_fields(meme_potential_rows, gold_watch_state)
    attach_gold_watch_fields(meme_shadow_rows, gold_watch_state)
    attach_gold_watch_fields(meme_rows, gold_watch_state)
    if fast_publish is not None:
        fast_report = dict(previous_report)
        fast_meta = dict(fast_report.get("meta") if isinstance(fast_report.get("meta"), dict) else {})
        fast_meta.update(
            {
                "section_quality": section_quality(
                    previous_report,
                    used_previous_meme=used_previous_meme,
                    meme_errors=meme_errors,
                    meme_rows=meme_rows,
                ),
                "used_previous_meme": used_previous_meme,
                "meme_errors": meme_errors,
                "meme_count": len(meme_rows),
                "meme_chain_scope": chain_scope,
                "meme_potential_count": len(meme_potential_rows),
                "meme_shadow_count": len(meme_shadow_rows),
                "report_generated_at": generated_at,
                "meme_live_generated_at": generated_at,
                "live_refresh_mode": "meme_fast_snapshot",
            }
        )
        fast_report.update(
            {
                "meta": fast_meta,
                "meme_rows": meme_rows[:meme_top],
                "meme_watch_universe": list(all_watch_rows.values()),
                "meme_potential_rows": meme_potential_rows,
                "meme_shadow_rows": meme_shadow_rows,
                "monitor_v3": monitor_v3,
            }
        )
        fast_publish(fast_report)
    replay_history = update_replay_history(previous_replay_history, list(all_watch_rows.values()), generated_at)
    atomic_write_json(out_dir / "alpha-radar-replay-history.json", replay_history)
    rating_v2 = build_rating_v2_dataset(replay_history)
    atomic_write_json(out_dir / "alpha-rating-v2-dataset.json", rating_v2)
    rating_v2_model = refresh_rating_v2_shadow(
        rating_v2,
        out_dir=out_dir,
        now=generated_at,
    )
    attach_rating_v2_scores(
        list(all_watch_rows.values()),
        replay=replay_history,
        model_status=rating_v2_model,
        model_dir=out_dir / "alpha-rating-v2-models",
    )
    attach_replay_rows(meme_potential_rows, replay_history)
    attach_replay_rows(meme_shadow_rows, replay_history)

    meme_heat_rows = build_meme_heat_rows(
        meme_rows,
        limit=max(12, meme_shadow_top),
        chain_scope=chain_scope,
    )
    meme_conviction_rows = build_meme_conviction_rows(
        meme_rows,
        limit=max(12, meme_shadow_top),
        chain_scope=chain_scope,
    )
    gold_watch_pick = gold_watch_state.get("pick") if isinstance(gold_watch_state.get("pick"), dict) else None
    gold_watch_confirmed_pick = gold_watch_state.get("confirmed_pick") if isinstance(gold_watch_state.get("confirmed_pick"), dict) else None
    gold_watch_early_pick = gold_watch_state.get("early_pick") if isinstance(gold_watch_state.get("early_pick"), dict) else None
    gold_backtest = build_gold_backtest(replay_history, gold_watch_state, now_iso=generated_at)

    report = dict(previous_report)
    meta = dict(report.get("meta") if isinstance(report.get("meta"), dict) else {})
    meta.update(
        {
            "section_quality": section_quality(
                previous_report,
                used_previous_meme=used_previous_meme,
                meme_errors=meme_errors,
                meme_rows=meme_rows,
            ),
            "used_previous_meme": used_previous_meme,
            "meme_errors": meme_errors,
            "meme_source_status": optional_meme_source_status(),
            "bsc_public_pair_status": bsc_pair_status or {"ok": False, "reason": "not_run"},
            "noxa_launchpad_status": noxa_launchpad_status or {"ok": False, "reason": "not_run"},
            "proficy_trending_status": proficy_trending_status or {"ok": False, "reason": "not_run"},
            "monitor985_status": monitor985_status or {"ok": False, "reason": "not_run"},
            "wind_monitor_status": wind_monitor_status or {"ok": False, "reason": "not_run"},
            "gmgn_smart_money_top50_status": gmgn_smart_money_top50_status or {"ok": False, "reason": "not_run"},
            "gmgn_skills_status": gmgn_skills_status or {"ok": False, "reason": "not_run"},
            "tweet_source_status": tweet_source_status or {"ok": False, "tweet_count": 0, "errors": []},
            "tweet_narrative": tweet_narrative,
            "meme_seed_terms": meme_seed_terms,
            "meme_count": len(meme_rows),
            "meme_chain_scope": chain_scope,
            "meme_potential_count": len(meme_potential_rows),
            "meme_shadow_count": len(meme_shadow_rows),
            "meme_heat_count": len(meme_heat_rows),
            "meme_conviction_count": len(meme_conviction_rows),
            "replay": replay_history.get("summary") or {},
            "replay_action_calibration": replay_calibration,
            "replay_boards": replay_leaderboards(replay_history),
            "rating_v2": {
                "mode": rating_v2["mode"],
                "readiness": rating_v2["readiness"],
                "model": rating_v2_model,
            },
            "gold_watch": watch_summary(gold_watch_state),
            "gold_backtest": gold_backtest,
            "report_generated_at": generated_at,
            "meme_live_generated_at": generated_at,
            "live_refresh_mode": "meme_fast",
        }
    )
    report.update(
        {
            "meta": meta,
            "meme_rows": meme_rows[:meme_top],
            "meme_watch_universe": list(all_watch_rows.values()),
            "gold_dog_pick": gold_watch_pick,
            "gold_watch_pick": gold_watch_pick,
            "gold_watch_confirmed_pick": gold_watch_confirmed_pick,
            "gold_watch_early_pick": gold_watch_early_pick,
            "gold_watch_alerts": (gold_watch_state.get("alerts") or [])[-100:],
            "meme_potential_rows": meme_potential_rows,
            "meme_shadow_rows": meme_shadow_rows,
            "meme_heat_rows": meme_heat_rows,
            "meme_conviction_rows": meme_conviction_rows,
            "monitor_v3": monitor_v3,
        }
    )
    token_intelligence = refresh_token_intelligence(
        report,
        out_dir=out_dir,
        now=generated_at,
        research_enabled=False,
        auxiliary_rows=gold_watch_state.get("candidates") or {},
    )
    status_details = {
        "used_previous_meme": used_previous_meme,
        "bsc_public_pair_status": bsc_pair_status or {"ok": False, "reason": "not_run"},
        "noxa_launchpad_status": noxa_launchpad_status or {"ok": False, "reason": "not_run"},
        "proficy_trending_status": proficy_trending_status or {"ok": False, "reason": "not_run"},
        "monitor985_status": monitor985_status or {"ok": False, "reason": "not_run"},
        "wind_monitor_status": wind_monitor_status or {"ok": False, "reason": "not_run"},
        "gmgn_smart_money_top50_status": gmgn_smart_money_top50_status or {"ok": False, "reason": "not_run"},
        "gmgn_skills_status": gmgn_skills_status or {"ok": False, "reason": "not_run"},
        "meme_count": len(meme_rows),
        "meme_potential_count": len(meme_potential_rows),
        "meme_shadow_count": len(meme_shadow_rows),
        "gold_watch": watch_summary(gold_watch_state),
        "meme_chain_scope": chain_scope,
        "new_gold_watch_alert_count": len(new_gold_watch_alerts),
        "new_gold_watch_alerts": new_gold_watch_alerts,
        "meme_seed_terms": {
            "enabled": bool(meme_seed_terms.get("enabled")),
            "seed_count": meme_seed_terms.get("seed_count") or 0,
            "top_seed": meme_seed_terms.get("top_seed"),
            "source_file": meme_seed_terms.get("source_file"),
            "error": meme_seed_terms.get("error"),
        },
        "token_intelligence": token_intelligence,
    }
    return report, status_details


def paper_summary(result: dict[str, Any], *, state_path: Path, markdown_path: Path, events_path: Path) -> dict[str, Any]:
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    events = result.get("events") if isinstance(result.get("events"), list) else []
    summary = state.get("last_summary") if isinstance(state.get("last_summary"), dict) else {}
    open_positions = state.get("open_positions") if isinstance(state.get("open_positions"), list) else []
    closed_positions = state.get("closed_positions") if isinstance(state.get("closed_positions"), list) else []
    return {
        "ok": True,
        "summary": summary,
        "entry_policy": state.get("entry_policy"),
        "events_count": len(events),
        "last_events": events[-5:],
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "state_path": str(state_path),
        "markdown_path": str(markdown_path),
        "events_path": str(events_path),
        "last_candidate_audit": state.get("last_candidate_audit") or [],
    }


def run_first_discovery_paper_cycle(
    *,
    report: dict[str, Any],
    replay_history: dict[str, Any],
    out_dir: Path,
    now: str,
    profile: str,
) -> dict[str, Any]:
    shadow = profile == "shadow-180m"
    state_path = out_dir / ("alpha-first-discovery-shadow-paper-state.json" if shadow else "alpha-first-discovery-paper-state.json")
    markdown_path = out_dir / ("alpha-first-discovery-shadow-paper-latest.md" if shadow else "alpha-first-discovery-paper-latest.md")
    events_path = out_dir / ("alpha-first-discovery-shadow-paper-events.jsonl" if shadow else "alpha-first-discovery-paper-events.jsonl")
    alpha_first_discovery_paper.apply_profile(profile)
    state = alpha_first_discovery_paper.load_state(state_path)
    result = alpha_first_discovery_paper.run_paper_once(report, replay_history, state, now)
    alpha_first_discovery_paper.save_json(state_path, result["state"])
    markdown_path.write_text(alpha_first_discovery_paper.render_markdown(result, now), encoding="utf-8")
    alpha_first_discovery_paper.append_events(events_path, result["events"])
    summary = paper_summary(result, state_path=state_path, markdown_path=markdown_path, events_path=events_path)
    summary["profile"] = profile
    return summary


def run_wallet_style_paper_cycle(*, report: dict[str, Any], out_dir: Path, now: str) -> dict[str, Any]:
    state_path = out_dir / "alpha-wallet-style-paper-state.json"
    markdown_path = out_dir / "alpha-wallet-style-paper-latest.md"
    events_path = out_dir / "alpha-wallet-style-paper-events.jsonl"
    state = alpha_wallet_style_paper.load_state(state_path)
    result = alpha_wallet_style_paper.run_paper_once(report, state, now)
    alpha_wallet_style_paper.save_json(state_path, result["state"])
    markdown_path.write_text(alpha_wallet_style_paper.render_markdown(result, now), encoding="utf-8")
    alpha_wallet_style_paper.append_events(events_path, result["events"])
    return paper_summary(result, state_path=state_path, markdown_path=markdown_path, events_path=events_path)


def run_paper_trading_cycle(report: dict[str, Any], *, out_dir: Path, now: str) -> dict[str, Any]:
    replay_history = read_json(out_dir / "alpha-radar-replay-history.json")
    payload: dict[str, Any] = {"ok": True, "updated_at": now, "mode": "read_only_paper"}
    jobs = (
        ("first_discovery", lambda: run_first_discovery_paper_cycle(
            report=report,
            replay_history=replay_history,
            out_dir=out_dir,
            now=now,
            profile="strict-45m",
        )),
        ("first_discovery_shadow", lambda: run_first_discovery_paper_cycle(
            report=report,
            replay_history=replay_history,
            out_dir=out_dir,
            now=now,
            profile="shadow-180m",
        )),
        ("wallet_style", lambda: run_wallet_style_paper_cycle(report=report, out_dir=out_dir, now=now)),
    )
    for name, job in jobs:
        try:
            payload[name] = job()
        except Exception as exc:  # noqa: BLE001
            payload["ok"] = False
            payload[name] = {"ok": False, "error": str(exc)}
        finally:
            alpha_first_discovery_paper.apply_profile("strict-45m")
    atomic_write_json(out_dir / "alpha-paper-trading-status.json", payload)
    return payload


def merge_refresh_changes(previous: dict[str, Any], updated: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    """Preserve Alpha updates completed while the Meme network scan was running."""
    merged = dict(latest)
    for key, value in updated.items():
        if key == "meta":
            meta = dict(latest.get("meta") or {})
            previous_meta = previous.get("meta") or {}
            for name, item in value.items():
                if name not in previous_meta or item != previous_meta[name]:
                    if name == "section_quality" and isinstance(item, dict):
                        meta[name] = {**(meta.get(name) or {}), **{k: v for k, v in item.items() if v != (previous_meta.get(name) or {}).get(k)}}
                    else:
                        meta[name] = item
            merged[key] = meta
        elif key == "monitor_v3":
            selected = newest_monitor_v3(value, latest.get(key))
            if selected is not None:
                merged[key] = selected
        elif key not in previous or value != previous[key]:
            merged[key] = value
    return merged


def run_meme_refresh_once(
    *,
    out_dir: Path = OUT_DIR,
    report_path: Path = REPORT_PATH,
    status_path: Path = STATUS_PATH,
    meme_source_limit: int = 80,
    meme_top: int = 40,
    meme_potential_top: int = 1,
    meme_shadow_top: int = 40,
    concurrency: int = 8,
    gold_watch_confirmations: int = 3,
    tweet_collector: Any = None,
    voice_alert: bool = False,
    voice_alert_text: str = DEFAULT_VOICE_ALERT_TEXT,
    voice_alert_repeat: int = DEFAULT_VOICE_ALERT_REPEAT,
    voice_alert_test: bool = False,
) -> dict[str, Any]:
    started_at = now_iso()
    started = time.monotonic()
    live_report_publish_seconds: float | None = None
    try:
        previous_status = read_json(status_path)
        previous_report = read_json(report_path)
        # These adapters write separate inbox files; one slow source must not serialize all discovery.
        with ThreadPoolExecutor(max_workers=5) as pool:
            jobs = [pool.submit(fn, out_dir) for fn in (refresh_bsc_public_pairs, refresh_noxa_launchpad, refresh_proficy_trending, refresh_985_monitor, refresh_wind_monitor)]
            source_results = []
            for job in jobs:
                try:
                    source_results.append(job.result())
                except Exception as exc:
                    source_results.append({"ok": False, "error": str(exc)})
        bsc_pair_status, noxa_launchpad_status, proficy_trending_status, monitor985_status, wind_monitor_status = source_results
        gmgn_smart_money_top50_status = refresh_gmgn_smart_money_top50(out_dir)
        gmgn_skills_status, wallet_flow_status = refresh_gmgn_sources(out_dir)
        collector = tweet_collector or collect_tweet_sources_for_out_dir
        tweet_source_status = collector(out_dir)

        def publish_fast_report(fast_report: dict[str, Any]) -> None:
            nonlocal live_report_publish_seconds
            merged = merge_refresh_changes(previous_report, fast_report, read_json(report_path))
            publish_latest_report(report_path, merged, out_dir=out_dir)
            if live_report_publish_seconds is None:
                live_report_publish_seconds = round(time.monotonic() - started, 3)

        report, details = build_meme_live_report(
            previous_report,
            out_dir=out_dir,
            meme_source_limit=meme_source_limit,
            meme_top=meme_top,
            meme_potential_top=meme_potential_top,
            meme_shadow_top=meme_shadow_top,
            concurrency=concurrency,
            gold_watch_confirmations=gold_watch_confirmations,
            tweet_source_status=tweet_source_status,
            bsc_pair_status=bsc_pair_status,
            noxa_launchpad_status=noxa_launchpad_status,
            proficy_trending_status=proficy_trending_status,
            monitor985_status=monitor985_status,
            wind_monitor_status=wind_monitor_status,
            gmgn_smart_money_top50_status=gmgn_smart_money_top50_status,
            gmgn_skills_status=gmgn_skills_status,
            fast_publish=publish_fast_report,
        )
        status = {
            "ok": True,
            "started_at": started_at,
            "finished_at": now_iso(),
            "report": str(report_path),
            **details,
        }
        # Publish fresh discovery data before paper ledgers, prelaunch research,
        # and replay followups so the live quote path is not serialized behind them.
        report = merge_refresh_changes(previous_report, report, read_json(report_path))
        report = publish_latest_report(report_path, report, out_dir=out_dir)
        status["live_report_publish_seconds"] = (
            live_report_publish_seconds
            if live_report_publish_seconds is not None
            else round(time.monotonic() - started, 3)
        )
        if voice_alert or voice_alert_test:
            status["voice_alert"] = apply_voice_alert(
                build_voice_alert_status(
                    previous_status,
                    enabled=True,
                    message=voice_alert_text,
                    repeat_count=voice_alert_repeat,
                ),
                alerts=details.get("new_gold_watch_alerts") or [],
                force_test=voice_alert_test,
            )
        else:
            status["voice_alert"] = build_voice_alert_status(
                previous_status,
                enabled=False,
                message=voice_alert_text,
                repeat_count=voice_alert_repeat,
            )
        paper_now = str((report.get("meta") or {}).get("meme_live_generated_at") or now_iso())
        paper_trading = run_paper_trading_cycle(report, out_dir=out_dir, now=paper_now)
        status["paper_trading"] = paper_trading
        meta = dict(report.get("meta") if isinstance(report.get("meta"), dict) else {})
        meta["voice_alert"] = status["voice_alert"]
        meta["paper_trading"] = paper_trading
        meta["gmgn_wallet_flow"] = wallet_flow_status
        status["gmgn_wallet_flow"] = wallet_flow_status
        prelaunch = refresh_prelaunch_watch(out_dir)
        meta["prelaunch_watch"] = {"checked_at": prelaunch.get("updated_at"), "candidate_count": len(prelaunch.get("candidates") or []), "source_count": len(prelaunch.get("sources") or {}), "errors": prelaunch.get("errors") or [], "official_verified": False}
        report["prelaunch_projects"] = [{**row, "official_verified": False} for row in prelaunch.get("candidates") or []]
        report["meta"] = meta
        report = merge_refresh_changes(previous_report, report, read_json(report_path))
        report = publish_latest_report(report_path, report, out_dir=out_dir)
        status["token_intelligence_research_scheduled"] = schedule_token_intelligence_research(
            report,
            out_dir=out_dir,
            now=paper_now,
        )
        try:
            followup_limit = max(0, int(os.environ.get("ALPHA_REPLAY_FOLLOWUP_LIMIT", "60")))
        except ValueError:
            followup_limit = 60
        followup = refresh_replay_followups(
            read_json(out_dir / "alpha-radar-replay-history.json"),
            report.get("meme_watch_universe") or [],
            now_iso(),
            limit=followup_limit,
        )
        if followup["rows"]:
            followup_history = read_json(out_dir / "alpha-radar-replay-history.json")
            followup_history = update_replay_history(
                followup_history,
                followup["rows"],
                followup["status"]["checked_at"],
            )
            atomic_write_json(out_dir / "alpha-radar-replay-history.json", followup_history)
        atomic_write_json(out_dir / "alpha-replay-followup-status.json", followup["status"])
        status["replay_followup"] = followup["status"]
        status["finished_at"] = now_iso()
    except Exception as exc:  # noqa: BLE001
        status = {
            "ok": False,
            "started_at": started_at,
            "finished_at": now_iso(),
            "report": str(report_path),
            "error": str(exc),
        }
    atomic_write_json(status_path, status)
    return status


def refresh_prelaunch_watch(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "prelaunch-project-watch.json"
    previous = read_json(path)
    if previous and age_seconds(previous.get("updated_at"), now_iso()) < 900:
        return previous
    sources = os.environ.get("ALPHA_PRELAUNCH_SOURCES", "mobai19999=https://t.me/s/mobai19999").split(",")
    try:
        return alpha_prelaunch_watch.run(argparse.Namespace(source=sources, out=path, markdown=out_dir / "prelaunch-project-watch.md", timeout_seconds=8, sleep_seconds=0, limit=80))
    except Exception as exc:
        return {**previous, "errors": [str(exc)]}


def collect_tweet_sources_for_out_dir(out_dir: Path) -> dict[str, Any]:
    try:
        local_files = [out_dir / "alpha-narrative-tweets-manual.json"]
        local_files.extend(Path(path) for path in alpha_tweet_sources.env_list("ALPHA_NARRATIVE_TWEET_FILES"))
        result = alpha_tweet_sources.collect_tweets(
            local_files=local_files,
            source_urls=alpha_tweet_sources.env_list("ALPHA_NARRATIVE_TWEET_URLS"),
            browser_commands=alpha_tweet_sources.env_command_list("ALPHA_NARRATIVE_BROWSER_COMMANDS"),
        )
        alpha_tweet_sources.write_tweets_file(out_dir / "alpha-narrative-tweets.json", result)
        alpha_tweet_sources.write_json(
            out_dir / "alpha-tweet-sources-status.json",
            {key: value for key, value in result.items() if key != "tweets"},
        )
        return {key: value for key, value in result.items() if key != "tweets"}
    except Exception as exc:  # noqa: BLE001
        status = {"ok": False, "tweet_count": 0, "errors": [str(exc)], "updated_at": now_iso()}
        alpha_tweet_sources.write_json(out_dir / "alpha-tweet-sources-status.json", status)
        return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="High-frequency Meme-only refresh for the local Alpha Radar web feed.")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--meme-source-limit", type=int, default=80)
    parser.add_argument("--meme-top", type=int, default=40)
    parser.add_argument("--meme-potential-top", type=int, default=1)
    parser.add_argument("--meme-shadow-top", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--gold-watch-confirmations", type=int, default=3)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--status", type=Path, default=STATUS_PATH)
    parser.add_argument("--voice-alert", action="store_true", default=os.environ.get("ALPHA_GOLD_VOICE_ALERT") == "1")
    parser.add_argument("--voice-alert-text", default=os.environ.get("ALPHA_GOLD_VOICE_ALERT_TEXT", DEFAULT_VOICE_ALERT_TEXT))
    parser.add_argument("--voice-alert-repeat", type=int, default=int(os.environ.get("ALPHA_GOLD_VOICE_ALERT_REPEAT", DEFAULT_VOICE_ALERT_REPEAT)))
    parser.add_argument("--voice-alert-test", action="store_true", help="Play one voice-alert test during this refresh.")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.environ.setdefault("MEME_BATCH_PAIRS", "1")
    os.environ.setdefault("ALPHA_MEME_CHAINS", DEFAULT_MEME_CHAINS)
    os.environ.setdefault("MEME_OPTIONAL_SOURCE_TIMEOUT_SECONDS", "8")
    os.environ.setdefault("MEME_PAIR_TOTAL_TIMEOUT_SECONDS", "10")
    os.environ.setdefault("GMGN_CLI_TIMEOUT_SECONDS", "10")
    while True:
        cycle_started = time.monotonic()
        status = run_meme_refresh_once(
            out_dir=args.out_dir,
            report_path=args.report,
            status_path=args.status,
            meme_source_limit=args.meme_source_limit,
            meme_top=args.meme_top,
            meme_potential_top=args.meme_potential_top,
            meme_shadow_top=args.meme_shadow_top,
            concurrency=args.concurrency,
            gold_watch_confirmations=args.gold_watch_confirmations,
            voice_alert=args.voice_alert,
            voice_alert_text=args.voice_alert_text,
            voice_alert_repeat=args.voice_alert_repeat,
            voice_alert_test=args.voice_alert_test,
        )
        if args.once:
            if wait_for_token_intelligence_research():
                publish_token_intelligence_cache(args.report, args.out_dir, now=now_iso())
            return 0 if status.get("ok") else 1
        time.sleep(max(1, max(5, args.interval_seconds) - (time.monotonic() - cycle_started)))


if __name__ == "__main__":
    raise SystemExit(main())
