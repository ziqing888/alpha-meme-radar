import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


alpha = load_module("alpha_pool_radar", BASE / "alpha_pool_radar.py")
alpha_meme = load_module("alpha_meme", BASE / "alpha_meme.py")
debot_export = load_module("debot_signal_export", BASE / "debot_signal_export.py")


def test_rows_from_anchor_items_extracts_debot_token_cards():
    rows = debot_export.rows_from_anchor_items(
        [
            {
                "text": "PUMP\n+12.5%\nMC\n$3.66B\nTXs\n1.23K/1.09K\n#1",
                "href": "https://debot.ai/token/solana/pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",
            }
        ],
        max_rows=5,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["chain"] == "solana"
    assert row["address"] == "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
    assert row["symbol"] == "PUMP"
    assert row["marketCap"] == 3_660_000_000
    assert row["price_change_1h_percent"] == 12.5


def test_normalize_debot_payload_flattens_official_rank_response():
    rows = debot_export.normalize_debot_payload(
        {
            "code": 0,
            "data": [
                {
                    "address": "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump",
                    "symbol": "TripleM",
                    "name": "Mom Mum Mim",
                    "chain": "solana",
                    "market_info": {
                        "mkt_cap": 38_407.56,
                        "volume": 49_818.10,
                        "percent_5m": 0.2214,
                        "percent_1h": 0.0414,
                        "percent_24h": 0.0414,
                        "holders": 596,
                        "swaps": 1270,
                    },
                    "pair_summary_info": {"liquidity": 16_058.98},
                    "activity_score": 0.6687,
                    "smart_wallet_online_count": 3,
                    "id": "rank-17",
                    "updated_at": "2026-09-10T10:00:00Z",
                }
            ],
        },
        "https://debot.ai/api/community/signal/channel/activity/rank",
        max_rows=5,
        observed_at="2026-09-10T10:00:05Z",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["marketCap"] == 38_407.56
    assert row["liquidityUsd"] == 16_058.98
    assert row["volume24hUsd"] == 49_818.10
    assert row["price_change_5m_percent"] == 22.14
    assert row["score"] == pytest.approx(66.87)
    assert row["provider_family"] == "debot"
    assert row["provider_feed"] == "debot_rank"
    assert row["provider_event_id"] == "rank-17"
    assert row["event_at"] == "2026-09-10T10:00:00Z"
    assert row["observed_at"] == "2026-09-10T10:00:05Z"
    assert row["source_url"] == "https://debot.ai/api/community/signal/channel/activity/rank"
    assert row["provenance_status"] == "known"


def test_normalize_debot_safe_info_keeps_stale_provider_metadata():
    rows = debot_export.normalize_debot_payload(
        {
            "data": [
                {
                    "address": "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump",
                    "symbol": "TripleM",
                    "chain": "solana",
                    "buy_tax": None,
                    "id": "safe-4",
                    "updated_at": "2026-09-10T09:00:00Z",
                    "stale": True,
                }
            ]
        },
        "https://debot.ai/api/token/safe-info",
        max_rows=5,
        observed_at="2026-09-10T10:00:05Z",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["provider_family"] == "debot"
    assert row["provider_feed"] == "debot_safe_info"
    assert row["evidence_role"] == "audit"
    assert row["signal_lane"] == "security_audit"
    assert row["event_at"] == "2026-09-10T09:00:00Z"
    assert row["observed_at"] == "2026-09-10T09:00:00Z"
    assert row["source_url"] == "https://debot.ai/api/token/safe-info"
    assert row["provenance_status"] == "stale"


def test_missing_provider_time_stays_unknown_and_fallback_id_ignores_observation_clock():
    payload = {
        "data": [
            {
                "address": "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump",
                "symbol": "TripleM",
                "chain": "solana",
            }
        ]
    }

    first = debot_export.normalize_debot_payload(
        payload,
        "https://debot.ai/api/community/signal/channel/activity/rank",
        max_rows=5,
        observed_at="2026-09-10T10:00:05Z",
    )[0]
    second = debot_export.normalize_debot_payload(
        payload,
        "https://debot.ai/api/community/signal/channel/activity/rank",
        max_rows=5,
        observed_at="2026-09-10T10:01:05Z",
    )[0]

    assert first["event_at"] is None
    assert first["event_time_status"] == "unknown"
    assert first["provenance_status"] == "unknown"
    assert first["provider_event_id"] == second["provider_event_id"]


def test_capture_failure_marks_retained_rows_stale_without_rewriting_event_time(tmp_path, monkeypatch):
    out = tmp_path / "debot-signals.json"
    status_path = tmp_path / "debot-status.json"
    original_event_at = "2026-09-10T09:00:00Z"
    out.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "solana",
                        "address": "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump",
                        "provider_feed": "debot_rank",
                        "provider_event_id": "rank-old",
                        "event_at": original_event_at,
                        "observed_at": "2026-09-10T09:00:05Z",
                        "provenance_status": "known",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        debot_export,
        "capture_with_playwright",
        lambda **_kwargs: {
            "ok": False,
            "reason": "capture_failed",
            "finished_at": "2026-09-10T10:00:00Z",
        },
    )
    emitted = []
    monkeypatch.setattr(debot_export, "emit_json", emitted.append)
    args = argparse.Namespace(
        out=str(out),
        status=str(status_path),
        stdout_cache=False,
        cache_max_age_seconds=55,
        urls=["https://debot.invalid"],
        timeout_seconds=3,
        max_rows=5,
        headed=False,
        user_data_dir="",
        channel="",
    )

    status = debot_export.run_once(args)

    retained = json.loads(out.read_text(encoding="utf-8"))["data"][0]
    assert status["ok"] is False
    assert retained["provenance_status"] == "stale"
    assert retained["event_at"] == original_event_at
    assert retained["observed_at"] == "2026-09-10T09:00:05Z"
    assert retained["stale_at"] == "2026-09-10T10:00:00Z"
    assert emitted[0]["data"][0] == retained


def test_capture_exception_marks_retained_rows_stale_and_returns_failure(tmp_path, monkeypatch):
    out = tmp_path / "debot-signals.json"
    status_path = tmp_path / "debot-status.json"
    out.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "chain": "solana",
                        "address": "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump",
                        "provider_feed": "debot_rank",
                        "provider_event_id": "rank-retained",
                        "event_at": "2026-09-10T08:00:00Z",
                        "observed_at": "2026-09-10T08:00:05Z",
                        "provenance_status": "known",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def raise_capture(**_kwargs):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(debot_export, "capture_with_playwright", raise_capture)
    monkeypatch.setattr(debot_export, "utc_now_iso", lambda: "2026-09-10T11:00:00Z")
    emitted = []
    monkeypatch.setattr(debot_export, "emit_json", emitted.append)
    args = argparse.Namespace(
        out=str(out),
        status=str(status_path),
        stdout_cache=False,
        cache_max_age_seconds=55,
        urls=["https://debot.invalid"],
        timeout_seconds=3,
        max_rows=5,
        headed=False,
        user_data_dir="",
        channel="",
    )

    status = debot_export.run_once(args)

    retained = json.loads(out.read_text(encoding="utf-8"))["data"][0]
    assert status["ok"] is False
    assert status["reason"] == "capture_exception"
    assert retained["provenance_status"] == "stale"
    assert retained["event_at"] == "2026-09-10T08:00:00Z"
    assert retained["observed_at"] == "2026-09-10T08:00:05Z"
    assert retained["stale_at"] == "2026-09-10T11:00:00Z"
    assert emitted[0]["data"][0] == retained
