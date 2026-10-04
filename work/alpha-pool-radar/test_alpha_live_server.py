import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_live_server.py")
SPEC = importlib.util.spec_from_file_location("alpha_live_server", MODULE_PATH)
live_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = live_server
SPEC.loader.exec_module(live_server)


def test_load_report_payload_returns_latest_report_json(tmp_path):
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text(json.dumps({"meta": {"ok": True}, "meme_rows": [{"symbol": "DOG"}]}), encoding="utf-8")

    status, payload = live_server.load_report_payload(report_path)

    assert status == 200
    assert payload["meta"]["ok"] is True
    assert payload["meme_rows"][0]["symbol"] == "DOG"


def test_load_report_payload_overlays_newer_fast_meme_snapshot(tmp_path):
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text(json.dumps({
        "meta": {"meme_live_generated_at": "2026-09-11T09:00:00+08:00", "slow": True},
        "alpha_rows": [{"symbol": "ALPHA"}],
        "meme_rows": [{"symbol": "SLOW"}],
    }), encoding="utf-8")
    (tmp_path / "alpha-meme-fast-latest.json").write_text(json.dumps({
        "meta": {"meme_live_generated_at": "2026-09-11T09:00:08+08:00", "live_refresh_mode": "meme_fast_discovery"},
        "meme_rows": [{"symbol": "FAST"}],
        "meme_pending_rows": [{"symbol": "WAITING"}],
        "meme_potential_rows": [{"symbol": "CONFIRMED"}],
    }), encoding="utf-8")

    status, payload = live_server.load_report_payload(report_path)

    assert status == 200
    assert payload["alpha_rows"] == [{"symbol": "ALPHA"}]
    assert payload["meme_rows"] == [{"symbol": "FAST"}]
    assert payload["meme_pending_rows"] == [{"symbol": "WAITING"}]
    assert payload["meme_potential_rows"] == [{"symbol": "CONFIRMED"}]
    assert payload["meta"]["slow"] is True
    assert payload["meta"]["live_refresh_mode"] == "meme_fast_discovery"


def test_load_report_payload_does_not_overlay_stale_fast_snapshot(tmp_path):
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text(json.dumps({
        "meta": {"meme_live_generated_at": "2026-09-11T09:01:00+08:00"},
        "meme_rows": [{"symbol": "NEWER-SLOW"}],
    }), encoding="utf-8")
    (tmp_path / "alpha-meme-fast-latest.json").write_text(json.dumps({
        "meta": {"meme_live_generated_at": "2026-09-11T09:00:00+08:00"},
        "meme_rows": [{"symbol": "STALE-FAST"}],
    }), encoding="utf-8")

    status, payload = live_server.load_report_payload(report_path)

    assert status == 200
    assert payload["meme_rows"] == [{"symbol": "NEWER-SLOW"}]


def test_load_report_payload_fails_closed_when_report_missing(tmp_path):
    status, payload = live_server.load_report_payload(tmp_path / "missing.json")

    assert status == 503
    assert payload["ok"] is False
    assert payload["step"] == "read_report"


def test_load_monitor_payload_prefers_compact_snapshot(tmp_path):
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text(json.dumps({"monitor_v3": {"observed_at": "old"}}), encoding="utf-8")
    (tmp_path / "alpha-meme-monitor-v3-latest.json").write_text(
        json.dumps({"schema_version": 3, "observed_at": "new", "tokens": []}),
        encoding="utf-8",
    )

    status, payload = live_server.load_monitor_payload(report_path)

    assert status == 200
    assert payload["monitor_v3"]["observed_at"] == "new"


def test_load_monitor_payload_includes_only_visible_token_intelligence(tmp_path):
    address = "0x" + "a" * 40
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text("{}", encoding="utf-8")
    (tmp_path / "alpha-meme-monitor-v3-latest.json").write_text(
        json.dumps({"schema_version": 3, "tokens": [{"identity": {"chain": "bsc", "contract_address": address}}]}),
        encoding="utf-8",
    )
    (tmp_path / "token-intelligence.json").write_text(
        json.dumps({"records": {f"bsc:{address}": {"status": "partial"}, "bsc:0xother": {"status": "ready"}}}),
        encoding="utf-8",
    )

    status, payload = live_server.load_monitor_payload(report_path)

    assert status == 200
    assert payload["monitor_intelligence_rows"] == [
        {"chain": "bsc", "contract_address": address, "token_intelligence": {"status": "partial"}}
    ]


def test_load_monitor_payload_includes_canonical_discovery_baseline(tmp_path):
    address = "0x" + "b" * 40
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text("not-json", encoding="utf-8")
    (tmp_path / "alpha-meme-monitor-v3-latest.json").write_text(
        json.dumps({
            "schema_version": 3,
            "tokens": [{"identity": {"chain": "robinhood", "contract_address": address}}],
            "rejections": [],
            "alerts": [],
        }),
        encoding="utf-8",
    )
    (tmp_path / "alpha-monitor-baselines-latest.json").write_text(
        json.dumps({f"robinhood:{address}": {
            "first_seen_at": "2026-09-11T03:14:33+08:00",
            "first_market_cap_usd": 84_390,
            "first_price_usd": 0.00008439,
            "peak_market_cap_usd": 980_468,
        }}),
        encoding="utf-8",
    )

    status, payload = live_server.load_monitor_payload(report_path)

    assert status == 200
    assert payload["monitor_baselines"][f"robinhood:{address}"] == {
        "first_seen_at": "2026-09-11T03:14:33+08:00",
        "first_market_cap_usd": 84_390,
        "first_price_usd": 0.00008439,
        "peak_market_cap_usd": 980_468,
    }


def test_arc_numeric_alias_matches_canonical_monitor_identity_for_baselines():
    address = "0x" + "a" * 40
    snapshot = {
        "tokens": [{"identity": {"chain": "arc", "contract_address": address}}]
    }
    report = {
        "meme_rows": [{
            "chain": "5042",
            "contract_address": address.upper().replace("0X", "0x"),
            "watch_first_seen_at": "2026-09-16T01:00:00+00:00",
            "watch_first_seen_mcap": 12_000,
        }]
    }

    assert live_server.monitor_discovery_baselines(report, snapshot) == {
        f"arc:{address}": {
            "first_seen_at": "2026-09-16T01:00:00+00:00",
            "first_market_cap_usd": 12_000,
            "first_price_usd": None,
            "peak_market_cap_usd": 12_000,
        }
    }


def test_load_monitor_payload_never_parses_slow_report(tmp_path):
    address = "0x" + "d" * 40
    report_path = tmp_path / "alpha-radar-report-latest.json"
    report_path.write_text("not-json", encoding="utf-8")
    (tmp_path / "alpha-meme-monitor-v3-latest.json").write_text(
        json.dumps({
            "schema_version": 3,
            "tokens": [{"identity": {"chain": "bsc", "contract_address": address}}],
            "rejections": [],
            "alerts": [],
        }),
        encoding="utf-8",
    )
    (tmp_path / "alpha-monitor-baselines-latest.json").write_text(
        json.dumps({f"bsc:{address}": {"first_market_cap_usd": 12_000}}),
        encoding="utf-8",
    )

    status, payload = live_server.load_monitor_payload(report_path)

    assert status == 200
    assert payload["monitor_baselines"][f"bsc:{address}"]["first_market_cap_usd"] == 12_000


def test_monitor_transport_keeps_earliest_evidence_but_bounds_history():
    address = "0x" + "c" * 40
    events = [
        {"event_id": f"event-{index}", "observed_at": f"2026-09-11T03:{index:02d}:00+08:00"}
        for index in range(40)
    ]
    snapshots = [
        {"observed_at": f"2026-09-11T03:{index:02d}:00+08:00", "market_cap_usd": index * 1_000}
        for index in range(20)
    ]
    compact = live_server.compact_monitor_transport({
        "schema_version": 3,
        "tokens": [{
            "identity": {"chain": "bsc", "contract_address": address},
            "events": events,
            "market": {"snapshots": snapshots},
            "state_history": list(range(30)),
            "audit_facts": list(range(30)),
            "risk": {"evidence": list(range(30))},
            "resonance": {"historical_evidence_ids": list(range(60))},
            "wallet_evidence": {"historical_wallet_addresses": list(range(60))},
        }],
        "rejections": list(range(100)),
        "alerts": [
            {
                "key": f"alert-{index}",
                "sources": list(range(100)),
                "evidence_ids": [f"evidence-{index}"],
            }
            for index in range(100)
        ],
    })
    token = compact["tokens"][0]

    assert events[0] in token["events"]
    assert events[-1] in token["events"]
    assert len(token["events"]) <= 13
    assert len(token["market"]["snapshots"]) <= 7
    assert len(compact["rejections"]) == 40
    assert len(compact["alerts"]) == 40
    assert compact["alerts"][0]["evidence_ids"] == ["evidence-60"]
