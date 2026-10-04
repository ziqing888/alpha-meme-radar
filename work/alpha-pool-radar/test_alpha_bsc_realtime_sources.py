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


supervisor = load_module("alpha_bsc_realtime_sources", BASE / "alpha_bsc_realtime_sources.py")


def test_resolve_secret_prefers_process_env(monkeypatch):
    monkeypatch.setenv("BITQUERY_API_KEY", "process-token")

    assert supervisor.resolve_secret(("BITQUERY_API_KEY", "BITQUERY_TOKEN"), {"BITQUERY_API_KEY": "file-token"}) == "process-token"


def test_run_cycle_writes_missing_source_statuses(monkeypatch, tmp_path):
    monkeypatch.delenv("BSC_WSS_URL", raising=False)
    monkeypatch.delenv("BNBCHAIN_WSS_URL", raising=False)
    monkeypatch.delenv("BSC_PANCAKE_WSS_URL", raising=False)
    monkeypatch.delenv("QUICKNODE_BSC_WSS_URL", raising=False)
    monkeypatch.delenv("BITQUERY_API_KEY", raising=False)
    monkeypatch.delenv("BITQUERY_TOKEN", raising=False)
    monkeypatch.setattr(supervisor, "ENV_FILES", (tmp_path / ".env",))
    monkeypatch.setattr(supervisor, "windows_environment_value", lambda _name: "")

    args = SimpleNamespace(
        status=tmp_path / "bsc-realtime-sources-status.json",
        duration_seconds=1,
        timeout_seconds=1,
        bsc_max_rows=10,
        launchpad_max_rows=10,
    )

    status = supervisor.run_cycle(args)
    written = json.loads(args.status.read_text(encoding="utf-8"))

    assert status["ok"] is True
    assert written["sources"]["bsc_onchain"]["reason"] == "missing_bsc_wss"
    assert written["sources"]["fourmeme_launchpad"]["reason"] == "missing_bitquery_token"
    assert written["sources"]["flap_launchpad"]["reason"] == "missing_bitquery_token"
