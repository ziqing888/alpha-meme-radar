import importlib.util
import json
import sys
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


flap_export = load_module("alpha_flap_live_export", BASE / "alpha_flap_live_export.py")


def arg(name: str, value_key: str, value):
    return {"Name": name, "Value": {value_key: value}}


def test_normalize_token_created_event_extracts_flap_launch():
    token = "0x1234567890abcdef1234567890abcdef12348888"
    row = flap_export.normalize_token_created_event(
        {
            "Log": {"Signature": {"Name": "TokenCreated"}},
            "Arguments": [
                arg("token", "address", token),
                arg("symbol", "string", "FLAP"),
                arg("name", "string", "Flap Gold"),
                arg("creator", "address", "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"),
            ],
            "Transaction": {"Hash": "0xhash", "From": "0xfrom"},
            "Block": {"Time": "2026-08-22T10:00:00Z", "Number": 123},
        }
    )

    assert row is not None
    assert row["chain"] == "bsc"
    assert row["tokenAddress"] == token.lower()
    assert row["source_family"] == "flap_launchpad"
    assert row["launchpad_platform"] == "Flap.sh"
    assert row["flap_token_type"] == "standard"
    assert row["market_data_pending"] is True


def test_flap_curve_event_marks_near_graduation_lifecycle():
    token = "0x1234567890abcdef1234567890abcdef12348888"
    row = flap_export.normalize_token_created_event(
        {
            "Log": {"Signature": {"Name": "TokenCurveSetV2"}},
            "Arguments": [
                arg("token", "address", token),
                arg("symbol", "string", "FLAP"),
                arg("progressBps", "integer", 9750),
            ],
            "Transaction": {"Hash": "0xcurve", "From": "0xfrom"},
            "Block": {"Time": "2026-08-22T10:05:00Z", "Number": 125},
        }
    )

    assert row is not None
    assert row["bonding_curve_progress_pct"] == 97.5
    assert row["launchpad_lifecycle_stage"] == "near_graduation"
    assert row["launchpad_stage_label"] == "曲线快毕业"


def test_missing_bitquery_token_writes_quiet_flap_status_and_empty_inbox(tmp_path):
    out_path = tmp_path / "flap-launches.json"
    status_path = tmp_path / "status.json"

    status = flap_export.stream_once(
        token="",
        out_path=out_path,
        status_path=status_path,
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is False
    assert status["reason"] == "missing_bitquery_token"
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["source"] == "flap_launchpad"
    assert payload["data"] == []


def test_recv_timeout_is_quiet_empty_window(monkeypatch, tmp_path):
    class TimeoutError(Exception):
        pass

    class FakeWebSocket:
        WebSocketTimeoutException = TimeoutError

        class Conn:
            def settimeout(self, _timeout):
                pass

            def send(self, _payload):
                pass

            def recv(self):
                raise TimeoutError("empty window")

            def close(self):
                pass

        @staticmethod
        def create_connection(*_args, **_kwargs):
            return FakeWebSocket.Conn()

    monkeypatch.setattr(flap_export, "websocket", FakeWebSocket)
    status = flap_export.stream_once(
        token="token",
        out_path=tmp_path / "flap-launches.json",
        status_path=tmp_path / "status.json",
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is True
    assert status["rows"] == 0
