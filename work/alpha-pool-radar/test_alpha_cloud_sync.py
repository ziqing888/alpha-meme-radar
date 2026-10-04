import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_cloud_sync.py")
SPEC = importlib.util.spec_from_file_location("alpha_cloud_sync", MODULE_PATH)
sync = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

LIVE_SPEC = importlib.util.spec_from_file_location(
    "alpha_cloudflare_live_sync", Path(__file__).with_name("alpha_cloudflare_live_sync.py")
)
live_sync = importlib.util.module_from_spec(LIVE_SPEC)
assert LIVE_SPEC and LIVE_SPEC.loader
sys.modules[LIVE_SPEC.name] = live_sync
LIVE_SPEC.loader.exec_module(live_sync)


def test_build_cloud_report_merges_fast_track_overlay(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"meta": {}, "meme_rows": [{"symbol": "A"}]}), encoding="utf-8")
    (tmp_path / "alpha-fast-track.json").write_text(
        json.dumps({"status": {"fresh_count": 2}, "execution_audit": {"equity_usd": 100}}),
        encoding="utf-8",
    )

    output = sync.build_cloud_report(report_path, tmp_path)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["meta"]["fast_track"]["fresh_count"] == 2
    assert payload["meta"]["execution_audit"]["equity_usd"] == 100


def test_cloud_projection_removes_wallet_attribution_secrets_and_local_paths():
    report = {
        "meta": {"report": r"C:\Users\operator\outputs\report.json", "api_key": "do-not-publish"},
        "monitor_v3": {
            "tokens": [{
                "identity": {"contract_address": "0x" + "1" * 40},
                "events": [{"wallet_address": "0x" + "2" * 40, "evidence_id": "internal-1"}],
                "wallet_evidence": {
                    "verified_buyers": 1,
                    "wallet_addresses": ["0x" + "2" * 40],
                    "evidence_ids": ["internal-1"],
                    "candidate_buyers": 1,
                    "candidate_wallet_addresses": ["0x" + "3" * 40],
                    "candidate_evidence_ids": ["internal-2"],
                    "status": "verified",
                    "freshness_window_seconds": 300,
                    "conflicting_wallets": ["0x" + "4" * 40],
                    "conflicting_evidence_ids": ["internal-3"],
                    "historical_verified_buyers": 1,
                    "historical_wallet_addresses": ["0x" + "5" * 40],
                },
            }],
            "alerts": [],
            "rejections": [],
        },
    }

    projected = sync.slim_for_cloudflare(report)
    serialized = json.dumps(projected)
    wallet = projected["monitor_v3"]["tokens"][0]["wallet_evidence"]

    assert projected["monitor_v3"]["tokens"][0]["identity"]["contract_address"] == "0x" + "1" * 40
    assert wallet["verified_buyers"] == 1
    assert wallet["wallet_addresses"] == []
    assert wallet["candidate_wallet_addresses"] == []
    assert wallet["historical_wallet_addresses"] == []
    assert projected["monitor_v3"]["tokens"][0]["events"] == []
    assert "do-not-publish" not in serialized
    assert "C:\\\\Users\\\\operator" not in serialized
    assert "internal-1" not in serialized


def test_live_monitor_projection_uses_the_same_public_redaction():
    payload = {
        "monitor_v3": {
            "schema_version": 3,
            "tokens": [{
                "events": [{"wallet_address": "0x" + "2" * 40}],
                "wallet_evidence": {
                    "verified_buyers": 1,
                    "wallet_addresses": ["0x" + "2" * 40],
                    "evidence_ids": ["private-evidence"],
                },
            }],
        }
    }

    projected = live_sync.slim_monitor_envelope(payload)
    token = projected["monitor_v3"]["tokens"][0]

    assert token["events"] == []
    assert token["wallet_evidence"]["wallet_addresses"] == []
    assert token["wallet_evidence"]["evidence_ids"] == []
