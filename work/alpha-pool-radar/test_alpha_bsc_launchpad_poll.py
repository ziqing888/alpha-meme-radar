import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


poller = load_module("alpha_bsc_launchpad_poll", BASE / "alpha_bsc_launchpad_poll.py")


def arg(name: str, value_key: str, value):
    return {"Name": name, "Value": {value_key: value}}


def test_attach_creation_timestamp_from_iso_time():
    row = poller.attach_creation_timestamp({"first_seen_at": "2026-08-22T13:40:00Z"})

    assert row["creation_timestamp"] > 1_700_000_000
    assert row["open_timestamp"] == row["creation_timestamp"]


def test_run_once_writes_launchpad_inboxes(monkeypatch, tmp_path):
    four_out = tmp_path / "fourmeme-launches.json"
    four_status = tmp_path / "fourmeme-status.json"
    flap_out = tmp_path / "flap-launches.json"
    flap_status = tmp_path / "flap-status.json"
    monkeypatch.setattr(poller.fourmeme, "DEFAULT_OUT", four_out)
    monkeypatch.setattr(poller.fourmeme, "DEFAULT_STATUS", four_status)
    monkeypatch.setattr(poller.flap, "DEFAULT_OUT", flap_out)
    monkeypatch.setattr(poller.flap, "DEFAULT_STATUS", flap_status)

    def fake_post(query, *, token, url, timeout_seconds):
        if poller.fourmeme.FOURMEME_PROXY in query:
            token_address = "0x1234567890abcdef1234567890abcdef12345678"
            return {
                "data": {
                    "EVM": {
                        "Events": [
                            {
                                "Arguments": [arg("token", "address", token_address), arg("symbol", "string", "FOUR")],
                                "Transaction": {"Hash": "0xfour", "From": "0xfrom"},
                                "Block": {"Time": "2026-08-22T13:40:00Z", "Number": 1},
                            }
                        ]
                    }
                }
            }
        token_address = "0x1234567890abcdef1234567890abcdef12348888"
        return {
            "data": {
                "EVM": {
                    "Events": [
                        {
                            "Log": {"Signature": {"Name": "TokenCreated"}},
                            "Arguments": [arg("token", "address", token_address), arg("symbol", "string", "FLAP")],
                            "Transaction": {"Hash": "0xflap", "From": "0xfrom"},
                            "Block": {"Time": "2026-08-22T13:41:00Z", "Number": 2},
                        }
                    ]
                }
            }
        }

    monkeypatch.setattr(poller, "bitquery_post", fake_post)
    args = SimpleNamespace(token="token", url="https://example.invalid/graphql", status=tmp_path / "poll-status.json", limit=10, timeout_seconds=5)

    status = poller.run_once(args)
    four_payload = json.loads(four_out.read_text(encoding="utf-8"))
    flap_payload = json.loads(flap_out.read_text(encoding="utf-8"))

    assert status["ok"] is True
    assert four_payload["data"][0]["symbol"] == "FOUR"
    assert four_payload["data"][0]["creation_timestamp"] > 0
    assert flap_payload["data"][0]["symbol"] == "FLAP"
    assert flap_payload["data"][0]["creation_timestamp"] > 0


def test_run_once_missing_token_is_quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(poller, "resolve_token", lambda: "")
    args = SimpleNamespace(token="", status=tmp_path / "poll-status.json")

    status = poller.run_once(args)

    assert status["ok"] is False
    assert status["reason"] == "missing_bitquery_token"
