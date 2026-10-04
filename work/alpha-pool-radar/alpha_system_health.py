#!/usr/bin/env python3
"""Single, read-only health view for the local MEME live-trading stack."""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHAIN_CONFIG = {
    "bsc": {
        "status": "okx-dex-sdk-live-status.json",
        "state": "okx-dex-sdk-live-state.json",
        "input": "bsc-execution-input.json",
        "amount_usd": "1",
        "signal_stage": "aggregate_early_bird",
        "entry_route": "bsc_aggregate_early_bird",
    },
    "robinhood": {
        "status": "okx-dex-sdk-robinhood-live-status.json",
        "state": "okx-dex-sdk-robinhood-live-state.json",
        "input": "robinhood-execution-input.json",
        "amount_usd": "2",
        "signal_stage": "aggregate_early_bird",
        "entry_route": "robinhood_aggregate_early_bird",
    },
}
HEALTHY_ACTIVITIES = {
    "checking", "waiting_for_strategy_candidate", "holding_existing_position",
    "position_opened", "position_reduced", "position_closed", "exit_only",
}
STORAGE_LIMITS = {
    "alpha-fast-quotes.jsonl": 256 * 1024 * 1024,
    "alpha-fast-watch-state.json": 64 * 1024 * 1024,
    "alpha-execution-state.json": 64 * 1024 * 1024,
}


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError, OSError):
        return None


def _age(value: Any, now: datetime) -> float:
    parsed = _stamp(value)
    if parsed is None:
        return math.inf
    age = (now - parsed).total_seconds()
    return age if age >= -5 else math.inf


def _file_age(path: Path, now: datetime) -> float:
    try:
        return max(0.0, now.timestamp() - path.stat().st_mtime)
    except OSError:
        return math.inf


def _issue(component: str, code: str, message: str, *, severity: str = "error",
           entries: bool = False, exits: bool = False, remote_only: bool = False) -> dict[str, Any]:
    return {
        "component": component,
        "code": code,
        "severity": severity,
        "message": message,
        "affects_live_entries": entries,
        "affects_live_exits": exits,
        "remote_display_only": remote_only,
    }


def build_system_health(out_dir: Path, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issues: list[dict[str, Any]] = []

    report_path = out_dir / "alpha-radar-report-latest.json"
    report_age = _file_age(report_path, now)
    report_fresh = report_age <= 300

    fast = _read(out_dir / "alpha-fast-track-status.json")
    fast_age = _age(fast.get("updated_at"), now)
    fast_fresh = fast_age <= 15
    if not report_fresh:
        report_stale_blocks_entries = not fast_fresh
        issues.append(_issue(
            "monitor",
            "report_stale",
            (
                "主监控报告超过 5 分钟未更新；实盘入口使用快速候选输入继续运行。"
                if fast_fresh else "主监控报告超过 5 分钟未更新。"
            ),
            severity="warning" if fast_fresh else "error",
            entries=report_stale_blocks_entries,
        ))
    fast_errors = fast.get("errors")
    fast_cycle_error = bool(fast.get("error"))
    fast_has_source_errors = (
        isinstance(fast_errors, (list, tuple, dict, set)) and bool(fast_errors)
    ) or (isinstance(fast_errors, str) and bool(fast_errors.strip()))
    if not fast_fresh:
        issues.append(_issue("market_data", "fast_track_stale", "快速行情与候选输入超过 15 秒未更新。", entries=True))
    elif fast_cycle_error:
        issues.append(_issue("market_data", "fast_track_error", "快速行情本轮采集出错。", entries=True))
    elif fast_has_source_errors:
        fresh_count = fast.get("fresh_count")
        universe_count = fast.get("universe_count")
        has_fresh_fallback = isinstance(fresh_count, (int, float)) and fresh_count > 0
        empty_universe = isinstance(universe_count, (int, float)) and universe_count == 0
        if has_fresh_fallback or empty_universe:
            message = (
                "GMGN 限流，当前使用新鲜备用报价。"
                if fast.get("gmgn_rate_limited") is True
                else "部分行情源异常，当前使用新鲜备用报价。"
            )
            issues.append(_issue("market_data", "fast_track_degraded", message, severity="warning"))
        else:
            issues.append(_issue("market_data", "fast_track_error", "快速行情本轮采集出错。", entries=True))

    chains: dict[str, dict[str, Any]] = {}
    for chain, config in CHAIN_CONFIG.items():
        status = _read(out_dir / config["status"])
        state = _read(out_dir / config["state"])
        input_path = out_dir / config["input"]
        execution_input = _read(input_path)
        receipt = status.get("terminal_control") if isinstance(status.get("terminal_control"), dict) else {}
        heartbeat_age = _age(receipt.get("updated_at") or status.get("updated_at"), now)
        pid = receipt.get("pid")
        worker_running = (
            heartbeat_age <= 40
            and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and receipt.get("supported") is True
        )
        strategy_effective = (
            receipt.get("strategy_version") == "chain_v2"
            and receipt.get("signal_stage") == config["signal_stage"]
            and receipt.get("entry_route") == config["entry_route"]
            and str(receipt.get("amount_usd")) == config["amount_usd"]
        )
        positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
        pending = state.get("terminal_pending")
        pending_count = pending if isinstance(pending, int) and not isinstance(pending, bool) else 0
        input_age = _file_age(input_path, now)
        input_fresh = input_age <= 15
        activity = str(status.get("status") or "unknown")

        if not worker_running:
            issues.append(_issue(chain, "worker_heartbeat_stale", f"{chain} 执行器心跳超过 40 秒。",
                                 entries=True, exits=bool(positions)))
        elif not strategy_effective:
            issues.append(_issue(chain, "strategy_ack_mismatch", f"{chain} 执行器未确认 V2 策略或金额。",
                                 entries=True))
        if not input_fresh:
            issues.append(_issue(chain, "execution_input_stale", f"{chain} 候选输入超过 15 秒未更新。",
                                 entries=True))
        if activity not in HEALTHY_ACTIVITIES:
            issues.append(_issue(chain, "executor_attention", f"{chain} 当前状态为 {activity}。",
                                 severity="warning", entries=True, exits=activity == "sell_blocked"))

        chains[chain] = {
            "worker_running": worker_running,
            "heartbeat_age_seconds": None if math.isinf(heartbeat_age) else round(heartbeat_age, 1),
            "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            "activity": activity,
            "activity_reason": status.get("reason"),
            "strategy_effective": strategy_effective,
            "amount_usd": receipt.get("amount_usd"),
            "input_fresh": input_fresh,
            "input_age_seconds": None if math.isinf(input_age) else round(input_age, 1),
            "signal_count": len(execution_input.get("signals") or []),
            "position_count": len(positions),
            "pending_transaction_count": pending_count,
        }

    cloud = _read(out_dir / "alpha-cloud-sync-status.json")
    cloud_persisted = cloud.get("ok") is True and cloud.get("persisted") is True
    if not cloud_persisted:
        issues.append(_issue(
            "cloud_display", "cloud_report_not_persisted",
            "远程看板当前未持久化；不影响本地执行，但网页冷启动时可能显示旧数据。",
            severity="warning", remote_only=True,
        ))

    storage: dict[str, Any] = {}
    for name, limit in STORAGE_LIMITS.items():
        path = out_dir / name
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        over = size > limit
        storage[name] = {"bytes": size, "limit_bytes": limit, "over_limit": over}
        if over:
            issues.append(_issue(
                "storage", "state_file_oversized",
                f"{name} 已达 {size / 1024 / 1024:.1f} MB，超过运行上限。",
                severity="warning",
            ))

    blocking_entries = [item for item in issues if item["affects_live_entries"]]
    blocking_exits = [item for item in issues if item["affects_live_exits"]]
    return {
        "ok": not blocking_entries and not blocking_exits,
        "updated_at": now.isoformat(timespec="seconds"),
        "live_execution": {
            "entry_path_healthy": not blocking_entries,
            "exit_path_healthy": not blocking_exits,
            "report_fresh": report_fresh,
            "fast_track_fresh": fast_fresh,
            "chains": chains,
        },
        "cloud_display": {
            "ok": cloud.get("ok") is True,
            "persisted": cloud.get("persisted") is True,
            "mode": cloud.get("mode"),
            "separate_from_live_execution": True,
        },
        "storage": storage,
        "issue_count": len(issues),
        "blocking_entry_issue_count": len(blocking_entries),
        "blocking_exit_issue_count": len(blocking_exits),
        "issues": issues,
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    payload = build_system_health(args.out_dir)
    if args.write:
        _atomic_write(args.out_dir / "alpha-system-health.json", payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
