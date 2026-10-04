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


fourmeme_export = load_module("alpha_fourmeme_live_export", BASE / "alpha_fourmeme_live_export.py")


def arg(name: str, value_key: str, value):
    return {"Name": name, "Value": {value_key: value}}


def test_normalize_token_create_event_extracts_fourmeme_launch():
    token = "0x1234567890abcdef1234567890abcdef12345678"
    row = fourmeme_export.normalize_token_create_event(
        {
            "Arguments": [
                arg("token", "address", token),
                arg("symbol", "string", "FOUR"),
                arg("name", "string", "Four Gold"),
                arg("creator", "address", "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"),
            ],
            "Transaction": {"Hash": "0xhash", "From": "0xfrom"},
            "Block": {"Time": "2026-08-22T10:00:00Z", "Number": 123},
        }
    )

    assert row is not None
    assert row["chain"] == "bsc"
    assert row["tokenAddress"] == token.lower()
    assert row["source_family"] == "fourmeme_launchpad"
    assert row["launchpad_platform"] == "Four.meme"
    assert row["market_data_pending"] is True


def test_fourmeme_purchase_event_updates_lifecycle_snapshot():
    token = "0x1234567890abcdef1234567890abcdef12345678"
    snapshot = fourmeme_export.FourMemeSnapshot(10)
    snapshot.add(
        fourmeme_export.normalize_token_create_event(
            {
                "Log": {"Signature": {"Name": "TokenCreate"}},
                "Arguments": [arg("token", "address", token), arg("symbol", "string", "FOUR")],
                "Transaction": {"Hash": "0xcreate", "From": "0xfrom"},
                "Block": {"Time": "2026-08-22T10:00:00Z", "Number": 123},
            }
        )
    )
    snapshot.add(
        fourmeme_export.normalize_token_create_event(
            {
                "Log": {"Signature": {"Name": "TokenPurchase"}},
                "Arguments": [
                    arg("token", "address", token),
                    arg("progress", "integer", 96),
                    arg("bnbAmount", "bigInteger", "2000000000000000000"),
                ],
                "Transaction": {"Hash": "0xbuy", "From": "0xbuyer"},
                "Block": {"Time": "2026-08-22T10:03:00Z", "Number": 124},
            }
        )
    )

    row = snapshot.payload()["data"][0]

    assert row["purchase_count"] == 1
    assert row["purchase_bnb"] == 2
    assert row["bonding_curve_progress_pct"] == 96
    assert row["launchpad_lifecycle_stage"] == "near_graduation"
    assert row["launchpad_stage_label"] == "曲线快毕业"


def test_missing_bitquery_token_writes_quiet_fourmeme_status_and_empty_inbox(tmp_path):
    out_path = tmp_path / "fourmeme-launches.json"
    status_path = tmp_path / "status.json"

    status = fourmeme_export.stream_once(
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
    assert payload["source"] == "fourmeme_launchpad"
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

    monkeypatch.setattr(fourmeme_export, "websocket", FakeWebSocket)
    status = fourmeme_export.stream_once(
        token="token",
        out_path=tmp_path / "fourmeme-launches.json",
        status_path=tmp_path / "status.json",
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is True
    assert status["rows"] == 0
