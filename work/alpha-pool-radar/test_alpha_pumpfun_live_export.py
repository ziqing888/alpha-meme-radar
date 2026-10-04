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


pumpfun_export = load_module("alpha_pumpfun_live_export", BASE / "alpha_pumpfun_live_export.py")


def test_normalize_event_row_accepts_common_pumpfun_launch_shape():
    row = pumpfun_export.normalize_event_row(
        "new_coin",
        {
            "data": {
                "mint": "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump",
                "symbol": "PUMPY",
                "name": "Pumpy",
                "marketCap": 23_456,
                "virtualSolReserves": 12_300,
            }
        },
        origin="https://example.invalid/events/tokens/new",
    )

    assert row is not None
    assert row["chain"] == "solana"
    assert row["tokenAddress"] == "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump"
    assert row["symbol"] == "PUMPY"
    assert row["marketCap"] == 23_456
    assert row["liquidityUsd"] == 12_300
    assert row["source_family"] == "pumpfun_live"
    assert row["pumpfun_event_type"] == "new_coin"


def test_missing_token_writes_quiet_status_and_empty_inbox(tmp_path):
    out_path = tmp_path / "pumpfun-live.json"
    status_path = tmp_path / "status.json"

    status = pumpfun_export.stream_once(
        server_url="https://example.invalid",
        endpoint="all",
        api_token="",
        out_path=out_path,
        status_path=status_path,
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is False
    assert status["reason"] == "missing_apify_token"
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["source"] == "pumpfun_live"
    assert payload["data"] == []
