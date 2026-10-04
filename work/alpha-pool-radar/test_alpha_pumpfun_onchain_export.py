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


onchain = load_module("alpha_pumpfun_onchain_export", BASE / "alpha_pumpfun_onchain_export.py")


def test_normalize_notification_extracts_pumpfun_create_mint_from_logs():
    row = onchain.normalize_notification(
        {
            "signature": "Sig1111111111111111111111111111111111111111111",
            "err": None,
            "logs": [
                "Program log: Instruction: Create",
                "Program data: 6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump",
            ],
        },
        rpc_url="",
        program_id=onchain.DEFAULT_PROGRAM_ID,
        timeout_seconds=1,
    )

    assert row is not None
    assert row["source_family"] == "pumpfun_onchain"
    assert row["tokenAddress"] == "6wcyE46tT6p4LJfrLXGe1uhbhKyfuRw4ciYCRFXopump"
    assert row["market_data_pending"] is True
    assert row["pumpfun_event_type"] == "create"


def test_missing_wss_writes_quiet_status_and_empty_inbox(tmp_path):
    out_path = tmp_path / "pumpfun-onchain.json"
    status_path = tmp_path / "status.json"

    status = onchain.stream_once(
        wss_url="",
        rpc_url="",
        program_id=onchain.DEFAULT_PROGRAM_ID,
        out_path=out_path,
        status_path=status_path,
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is False
    assert status["reason"] == "missing_solana_wss"
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["source"] == "pumpfun_onchain"
    assert payload["data"] == []

