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


poller = load_module("alpha_bsc_public_pair_poll", BASE / "alpha_bsc_public_pair_poll.py")


def topic_address(address: str) -> str:
    return "0x" + ("0" * 24) + address.lower().replace("0x", "")


def data_address(address: str) -> str:
    return "0x" + ("0" * 24) + address.lower().replace("0x", "") + ("0" * 64)


def test_run_once_writes_public_rpc_pair_inbox(monkeypatch, tmp_path):
    token = "0x1234567890abcdef1234567890abcdef12345678"
    pair = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    log = {
        "topics": [
            poller.bsc_onchain.PAIR_CREATED_TOPIC,
            topic_address(poller.bsc_onchain.WBNB),
            topic_address(token),
        ],
        "data": data_address(pair),
        "transactionHash": "0xhash",
        "blockNumber": "0x10",
    }
    monkeypatch.setattr(poller, "block_number", lambda urls, timeout_seconds: (10_000, "https://rpc.example"))
    monkeypatch.setattr(
        poller,
        "pair_logs",
        lambda urls, factory, from_block, to_block, quote_assets_, timeout_seconds: ([log], "https://rpc.example"),
    )
    args = SimpleNamespace(
        rpc_urls=["https://rpc.example"],
        factory=poller.bsc_onchain.PANCAKE_V2_FACTORY,
        out=tmp_path / "meme-source-inbox" / "bsc-pancake-pairs.json",
        status=tmp_path / "bsc-public-pair-poll-status.json",
        max_rows=20,
        lookback_blocks=100,
        timeout_seconds=5,
        quote_assets=[],
        broad=False,
    )

    status = poller.run_once(args)
    payload = json.loads(args.out.read_text(encoding="utf-8"))

    assert status["ok"] is True
    assert status["rows"] == 1
    assert payload["mode"] == "public_rpc_poll"
    assert payload["data"][0]["tokenAddress"] == token.lower()
    assert payload["data"][0]["pair_address"] == pair.lower()
    assert payload["data"][0]["market_data_pending"] is True


def test_run_once_preserves_previous_inbox_on_rpc_error(monkeypatch, tmp_path):
    out = tmp_path / "meme-source-inbox" / "bsc-pancake-pairs.json"
    out.parent.mkdir()
    previous = {"ok": True, "data": [{"tokenAddress": "0xold", "symbol": "OLD"}]}
    out.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(poller, "block_number", lambda urls, timeout_seconds: (_ for _ in ()).throw(RuntimeError("rpc down")))
    args = SimpleNamespace(
        rpc_urls=["https://rpc.example"],
        factory=poller.bsc_onchain.PANCAKE_V2_FACTORY,
        out=out,
        status=tmp_path / "bsc-public-pair-poll-status.json",
        max_rows=20,
        lookback_blocks=100,
        timeout_seconds=5,
        quote_assets=[],
        broad=False,
    )

    status = poller.run_once(args)
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert status["ok"] is False
    assert status["preserved_previous_rows"] == 1
    assert payload == previous
