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


bsc_export = load_module("alpha_bsc_onchain_export", BASE / "alpha_bsc_onchain_export.py")


def topic_for_address(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().replace("0x", "")


def data_for_pair(pair: str) -> str:
    return "0x" + "0" * 24 + pair.lower().replace("0x", "") + "0" * 64


def test_normalize_pair_log_extracts_new_bsc_token_against_wbnb():
    meme_token = "0x1234567890abcdef1234567890abcdef12345678"
    pair = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    row = bsc_export.normalize_pair_log(
        {
            "topics": [
                bsc_export.PAIR_CREATED_TOPIC,
                topic_for_address(bsc_export.WBNB),
                topic_for_address(meme_token),
            ],
            "data": data_for_pair(pair),
            "transactionHash": "0xhash",
            "blockNumber": "0x10",
        },
        factory=bsc_export.PANCAKE_V2_FACTORY,
    )

    assert row is not None
    assert row["chain"] == "bsc"
    assert row["tokenAddress"] == meme_token.lower()
    assert row["pair_address"] == pair.lower()
    assert row["source_family"] == "bsc_onchain"
    assert row["market_data_pending"] is True
    assert row["bsc_event_type"] == "PairCreated"


def test_missing_bsc_wss_writes_quiet_status_and_empty_inbox(tmp_path):
    out_path = tmp_path / "bsc-pancake-pairs.json"
    status_path = tmp_path / "status.json"

    status = bsc_export.stream_once(
        wss_url="",
        factory=bsc_export.PANCAKE_V2_FACTORY,
        out_path=out_path,
        status_path=status_path,
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is False
    assert status["reason"] == "missing_bsc_wss"
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["source"] == "bsc_onchain"
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

    monkeypatch.setattr(bsc_export, "websocket", FakeWebSocket)
    status = bsc_export.stream_once(
        wss_url="wss://example.invalid",
        factory=bsc_export.PANCAKE_V2_FACTORY,
        out_path=tmp_path / "bsc-pancake-pairs.json",
        status_path=tmp_path / "status.json",
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is True
    assert status["rows"] == 0
