from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_execution_worker import ExecutionWorker
from test_alpha_execution_worker import NOW, input_payload


SECRET = "0x" + "a" * 64
RAW_TRANSACTION = "0x" + "b" * 160
TX_HASH = "0x" + "c" * 64


class FakeLiveExecutionAdapter:
    def __init__(self, *, sell_result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sell_result = sell_result or self._result("sell")

    @staticmethod
    def _result(side: str) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "confirmed",
            "side": side,
            "chain_id": 56,
            "tx_hash": TX_HASH,
            "private_key": SECRET,
            "raw_transaction": RAW_TRANSACTION,
            "transaction": {"data": SECRET},
        }

    def buy(self, token_address: str, pool_address: str, amount_atomic: Any, slippage_percent: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({
            "side": "buy", "token_address": token_address, "pool_address": pool_address,
            "amount_atomic": amount_atomic, "slippage_percent": slippage_percent, **kwargs,
        })
        return self._result("buy")

    def sell(self, token_address: str, pool_address: str, amount_atomic: Any, slippage_percent: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({
            "side": "sell", "token_address": token_address, "pool_address": pool_address,
            "amount_atomic": amount_atomic, "slippage_percent": slippage_percent, **kwargs,
        })
        return self.sell_result


def live_input() -> dict[str, Any]:
    payload = input_payload(price=1.0)
    payload["signals"][0].update({
        "amount_atomic": "1000",
        "sell_amount_atomic": "1000",
        "slippage_percent": "1",
    })
    return payload


def make_worker(tmp_path: Path, adapter: Any, payload: dict[str, Any] | None = None) -> ExecutionWorker:
    input_path = tmp_path / "bsc-execution-input.json"
    input_path.write_text(json.dumps(payload or live_input()), encoding="utf-8")
    return ExecutionWorker(
        input_path,
        tmp_path,
        adapter,
        mode="live",
        live_preflight={"ready": True, "checks": {"adapter": {"ok": True}}, "missing": [], "reasons": []},
    )


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_live_preflight_failure_is_explicit_and_does_not_call_adapter(tmp_path: Path):
    adapter = FakeLiveExecutionAdapter()
    payload = live_input()
    input_path = tmp_path / "bsc-execution-input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    worker = ExecutionWorker(
        input_path,
        tmp_path,
        None,
        mode="live",
        live_preflight={
            "ready": False,
            "checks": {"BSC_RPC_URL": {"ok": False, "reason": "missing"}},
            "missing": ["BSC_RPC_URL"],
            "reasons": [],
        },
    )

    result = worker.run_once(NOW)

    assert result["status"] == "live_preflight_failed"
    assert "BSC_RPC_URL" in result["preflight"]["missing"]
    assert result["events"] == []
    assert not adapter.calls
    assert not (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip()


def test_injected_live_adapter_receives_policy_accepted_entry_and_only_public_fields_are_recorded(tmp_path: Path):
    adapter = FakeLiveExecutionAdapter()
    result = make_worker(tmp_path, adapter).run_once(NOW)

    assert [call["side"] for call in adapter.calls] == ["buy"]
    assert adapter.calls[0]["amount_atomic"] == "1000"
    assert adapter.calls[0]["slippage_percent"] == "1"
    events = read_events(tmp_path)
    assert [event["type"] for event in events] == ["intent", "fill"]
    assert events[1]["execution"] == {
        "ok": True, "status": "confirmed", "side": "buy", "chain_id": 56, "tx_hash": TX_HASH,
    }
    rendered = json.dumps(result, sort_keys=True) + json.dumps(events, sort_keys=True)
    assert SECRET not in rendered
    assert RAW_TRANSACTION not in rendered


def test_injected_live_adapter_receives_policy_triggered_exit(tmp_path: Path):
    adapter = FakeLiveExecutionAdapter()
    worker = make_worker(tmp_path, adapter)
    worker.run_once(NOW)
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    payload["quotes"][0]["price_usd"] = 2.0
    payload["quotes"][0]["quote_at"] = (NOW + timedelta(seconds=1)).isoformat()
    payload["quotes"][0]["quote_observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert [call["side"] for call in adapter.calls] == ["buy", "sell"]
    assert adapter.calls[1]["amount_atomic"] == "800"
    assert result["events"][0]["type"] == "exit_intent"
    assert result["events"][1]["type"] == "exit_fill"
    assert result["events"][1]["execution"]["side"] == "sell"
