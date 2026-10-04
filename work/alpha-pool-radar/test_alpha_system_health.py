import json
import os
from datetime import datetime, timezone

from alpha_system_health import build_system_health


NOW = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)


def write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    timestamp = NOW.timestamp()
    os.utime(path, (timestamp, timestamp))


def healthy_stack(tmp_path):
    write(tmp_path / "alpha-radar-report-latest.json", {"meta": {}})
    write(tmp_path / "alpha-fast-track-status.json", {"ok": True, "updated_at": NOW.isoformat()})
    for chain, stem, amount, stage, route in (
        ("bsc", "okx-dex-sdk", "1", "aggregate_early_bird", "bsc_aggregate_early_bird"),
        ("robinhood", "okx-dex-sdk-robinhood", "2", "aggregate_early_bird", "robinhood_aggregate_early_bird"),
    ):
        write(tmp_path / f"{stem}-live-status.json", {
            "status": "waiting_for_strategy_candidate",
            "terminal_control": {
                "supported": True, "pid": 100, "updated_at": NOW.isoformat(),
                "strategy_version": "chain_v2", "signal_stage": stage,
                "entry_route": route, "amount_usd": amount,
            },
        })
        write(tmp_path / f"{stem}-live-state.json", {"positions": {}, "terminal_pending": 0})
        write(tmp_path / ("bsc-execution-input.json" if chain == "bsc" else "robinhood-execution-input.json"),
              {"signals": []})


def test_cloud_display_failure_does_not_mark_live_execution_unhealthy(tmp_path):
    healthy_stack(tmp_path)
    write(tmp_path / "alpha-cloud-sync-status.json", {"ok": True, "persisted": False, "mode": "memory"})

    result = build_system_health(tmp_path, NOW)

    assert result["ok"] is True
    assert result["live_execution"]["entry_path_healthy"] is True
    assert result["cloud_display"]["persisted"] is False
    assert any(issue["remote_display_only"] for issue in result["issues"])


def test_stale_worker_blocks_entries_and_only_blocks_exits_with_a_position(tmp_path):
    healthy_stack(tmp_path)
    status = tmp_path / "okx-dex-sdk-live-status.json"
    payload = json.loads(status.read_text())
    payload["terminal_control"]["updated_at"] = "2026-09-10T03:00:00+00:00"
    write(status, payload)
    write(tmp_path / "okx-dex-sdk-live-state.json", {"positions": {"bsc:token": {}}, "terminal_pending": 0})

    result = build_system_health(tmp_path, NOW)

    assert result["ok"] is False
    assert result["live_execution"]["entry_path_healthy"] is False
    assert result["live_execution"]["exit_path_healthy"] is False


def test_oversized_history_is_visible_but_does_not_falsely_stop_execution(tmp_path, monkeypatch):
    healthy_stack(tmp_path)
    write(tmp_path / "alpha-cloud-sync-status.json", {"ok": True, "persisted": True})
    path = tmp_path / "alpha-fast-quotes.jsonl"
    path.write_bytes(b"x" * 20)
    monkeypatch.setattr("alpha_system_health.STORAGE_LIMITS", {"alpha-fast-quotes.jsonl": 10})

    result = build_system_health(tmp_path, NOW)

    assert result["ok"] is True
    assert result["storage"]["alpha-fast-quotes.jsonl"]["over_limit"] is True


def test_fresh_empty_fast_cycle_is_not_reported_as_stale(tmp_path):
    healthy_stack(tmp_path)
    write(tmp_path / "alpha-fast-track-status.json", {
        "ok": False,
        "updated_at": NOW.isoformat(),
        "tracked_count": 0,
        "fresh_count": 0,
        "universe_count": 0,
        "errors": [],
    })

    result = build_system_health(tmp_path, NOW)

    assert result["live_execution"]["fast_track_fresh"] is True
    assert result["live_execution"]["entry_path_healthy"] is True
    assert not any(issue["code"] == "fast_track_stale" for issue in result["issues"])


def test_stale_full_report_does_not_block_entries_when_fast_track_is_fresh(tmp_path):
    healthy_stack(tmp_path)
    old_timestamp = datetime(2026, 9, 10, 3, 50, tzinfo=timezone.utc).timestamp()
    os.utime(tmp_path / "alpha-radar-report-latest.json", (old_timestamp, old_timestamp))

    result = build_system_health(tmp_path, NOW)

    assert result["live_execution"]["report_fresh"] is False
    assert result["live_execution"]["fast_track_fresh"] is True
    assert result["live_execution"]["entry_path_healthy"] is True
    issue = next(issue for issue in result["issues"] if issue["code"] == "report_stale")
    assert issue["severity"] == "warning"
    assert issue["affects_live_entries"] is False


def test_fresh_fast_cycle_with_an_error_blocks_entries_without_claiming_staleness(tmp_path):
    healthy_stack(tmp_path)
    write(tmp_path / "alpha-fast-track-status.json", {
        "ok": False,
        "updated_at": NOW.isoformat(),
        "tracked_count": 1,
        "fresh_count": 0,
        "universe_count": 1,
        "errors": ["quote source unavailable"],
    })

    result = build_system_health(tmp_path, NOW)

    assert result["live_execution"]["fast_track_fresh"] is True
    assert result["live_execution"]["entry_path_healthy"] is False
    assert any(issue["code"] == "fast_track_error" for issue in result["issues"])
    assert not any(issue["code"] == "fast_track_stale" for issue in result["issues"])


def test_gmgn_error_with_fresh_fallback_quote_is_degraded_but_does_not_block_entries(tmp_path):
    healthy_stack(tmp_path)
    write(tmp_path / "alpha-fast-track-status.json", {
        "ok": True,
        "updated_at": NOW.isoformat(),
        "tracked_count": 1,
        "fresh_count": 1,
        "universe_count": 1,
        "gmgn_rate_limited": True,
        "errors": ["gmgn token: gmgn_rate_limited"],
    })

    result = build_system_health(tmp_path, NOW)

    assert result["live_execution"]["entry_path_healthy"] is True
    assert any(issue["code"] == "fast_track_degraded" for issue in result["issues"])
    assert not any(issue["affects_live_entries"] for issue in result["issues"])
