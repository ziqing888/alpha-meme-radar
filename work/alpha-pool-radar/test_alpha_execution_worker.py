from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpha_execution_worker import ExecutionWorker


CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 7, 22, 0, tzinfo=CN)


class FakeClient:
    def __init__(self):
        self.calls: list[str] = []

    def sign(self, *_args, **_kwargs):
        self.calls.append("sign")
        raise AssertionError("worker must not sign")

    def send(self, *_args, **_kwargs):
        self.calls.append("send")
        raise AssertionError("worker must not broadcast")


def read_state(path: Path) -> dict:
    return json.loads((path / "state.json").read_text(encoding="utf-8"))


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def input_payload(*, quote_age: int = 0, price: float = 1.0, mcap: float = 50_000) -> dict:
    signal_at = NOW - timedelta(seconds=1)
    quote_at = NOW - timedelta(seconds=quote_age)
    return {
        "updated_at": NOW.isoformat(),
        "mode": "read_only_signal_input",
        "signals": [{
            "chain": "bsc",
            "contract_address": "0xABC",
            "pool_address": "0xPOOL",
            "execution_arm": "first_discovery",
            "signal_at": signal_at.isoformat(),
            "first_seen_at": (NOW - timedelta(minutes=10)).isoformat(),
            "quote_at": quote_at.isoformat(),
            "mcap": mcap,
            "entry_score": 75,
            "change_m5": 3,
            "change_h1": 15,
            "watch_status": "active",
            "gmgn_risk_flags": [],
        }],
        "quotes": [{
            "chain": "bsc",
            "contract_address": "0xABC",
            "pool_address": "0xPOOL",
            "pair_address": "0xPOOL",
            "quote_at": quote_at.isoformat(),
            "quote_observed_at": quote_at.isoformat(),
            "quote_status": "fresh",
            "price_usd": price,
            "price_impact": 0.0,
            "depth_impact": 0.0,
            "liquidity_usd": 20_000,
            "mcap": 999_999_999,
        }],
    }


def worker_for(tmp_path: Path, *, mode: str = "paper", quote_age: int = 0, price: float = 1.0) -> ExecutionWorker:
    input_path = tmp_path / "bsc-execution-input.json"
    input_path.write_text(json.dumps(input_payload(quote_age=quote_age, price=price)), encoding="utf-8")
    return ExecutionWorker(input_path, tmp_path, FakeClient(), mode=mode)


def replace_quote(worker: ExecutionWorker, *, price: float, at: datetime, **changes) -> None:
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    for quote in payload["quotes"]:
        quote["price_usd"] = price
        quote["quote_at"] = at.isoformat()
        quote["quote_observed_at"] = at.isoformat()
        quote.update(changes)
    payload["updated_at"] = at.isoformat()
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")


def test_paper_worker_records_one_buy_once_and_never_calls_signer(tmp_path):
    worker = worker_for(tmp_path, mode="paper")

    first = worker.run_once(NOW)
    second = worker.run_once(NOW + timedelta(seconds=1))
    replace_quote(worker, price=1.0, at=NOW + timedelta(seconds=2))
    third = worker.run_once(NOW + timedelta(seconds=2))

    assert [event["type"] for event in first["events"]] == ["intent"]
    assert second["events"] == []
    assert [event["type"] for event in third["events"]] == ["fill"]
    assert len(read_state(tmp_path)["positions"]) == 1
    assert worker.client.calls == []
    assert all(event["idempotency_key"].endswith(":buy") for event in read_events(tmp_path))


def test_worker_rejects_stale_quote_and_keeps_reason(tmp_path):
    result = worker_for(tmp_path, quote_age=31).run_once(NOW)

    assert result["rejections"][0]["reason"] == "stale_quote"
    assert result["summary"]["open_count"] == 0
    assert read_events(tmp_path) == []


def test_worker_rejects_same_token_on_a_different_pool(tmp_path):
    worker = worker_for(tmp_path)
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    payload["quotes"][0]["pool_address"] = "0xOTHER"
    payload["quotes"][0]["pair_address"] = "0xOTHER"
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")

    result = worker.run_once(NOW)

    assert result["rejections"][0]["reason"] == "pool_identity_mismatch"
    assert read_state(tmp_path)["positions"] == {}


def test_worker_uses_live_quote_price_instead_of_report_mcap(tmp_path):
    worker = worker_for(tmp_path, price=0.25)
    worker.run_once(NOW)
    replace_quote(worker, price=0.25, at=NOW + timedelta(seconds=1))
    result = worker.run_once(NOW + timedelta(seconds=1))

    position = next(iter(read_state(tmp_path)["positions"].values()))
    assert position["quote_price_usd"] == 0.25
    assert position["entry_price_usd"] == 0.25
    assert position["entry_fill_price_usd"] == pytest.approx(0.25 * 1.005)
    assert result["events"][0]["quote"]["price_usd"] == 0.25
    assert position["entry_price_usd"] != 999_999_999


def test_worker_executes_tp1_then_trailing_exit(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    replace_quote(worker, price=1.0, at=NOW + timedelta(seconds=1))
    worker.run_once(NOW + timedelta(seconds=1))

    replace_quote(worker, price=2.0, at=NOW + timedelta(seconds=10))
    tp1 = worker.run_once(NOW + timedelta(seconds=10))
    assert tp1["events"][0]["type"] == "exit_intent"
    assert tp1["events"][0]["sell_fraction"] == 0.8
    assert len(read_state(tmp_path)["positions"]) == 1

    replace_quote(worker, price=2.0, at=NOW + timedelta(seconds=11))
    worker.run_once(NOW + timedelta(seconds=11))

    replace_quote(worker, price=1.2, at=NOW + timedelta(seconds=20))
    trailing = worker.run_once(NOW + timedelta(seconds=20))
    assert trailing["events"][0]["type"] == "exit_intent"
    assert trailing["events"][0]["sell_fraction"] == pytest.approx(0.2)


def test_shadow_records_quote_evidence_without_changing_cash(tmp_path):
    worker = worker_for(tmp_path, mode="shadow")

    result = worker.run_once(NOW)
    state = read_state(tmp_path)

    assert [event["type"] for event in result["events"]] == ["intent", "shadow_fill"]
    assert state["cash_usd"] == 100.0
    assert state["positions"] == {}
    assert result["events"][1]["quote"]["price_usd"] == 1.0


def test_paper_order_applies_fee_gas_and_five_usd_notional(tmp_path):
    worker = worker_for(tmp_path)

    worker.run_once(NOW)
    replace_quote(worker, price=1.0, at=NOW + timedelta(seconds=1))
    worker.run_once(NOW + timedelta(seconds=1))
    state = read_state(tmp_path)

    assert state["cash_usd"] == pytest.approx(100.0 - 5.0 - 5.0 * 0.003 - 0.12)
    assert next(iter(state["positions"].values()))["entry_notional_usd"] == 5.0


def test_worker_enforces_three_positions_and_fifteen_usd_exposure(tmp_path):
    worker = worker_for(tmp_path)
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    signals = []
    quotes = []
    for index in range(4):
        token = f"0x{index + 1:040x}"
        pool = f"0x{index + 11:040x}"
        signal = dict(payload["signals"][0], contract_address=token, pool_address=pool)
        quote = dict(payload["quotes"][0], contract_address=token, pool_address=pool, pair_address=pool)
        signals.append(signal)
        quotes.append(quote)
    payload["signals"] = signals
    payload["quotes"] = quotes
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")

    worker.run_once(NOW)
    replace_quote(worker, price=1.0, at=NOW + timedelta(seconds=1))
    result = worker.run_once(NOW + timedelta(seconds=1))

    assert result["summary"]["open_count"] == 3
    assert result["summary"]["open_exposure_usd"] == 15.0
    assert any(item["reason"] == "exposure_limit" for item in result["rejections"])


def test_live_mode_without_an_adapter_fails_preflight_without_calls(tmp_path, monkeypatch):
    worker = worker_for(tmp_path, mode="live")
    worker.client.signer = object()
    worker.client.broadcaster = object()
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    result = worker.run_once(NOW)

    assert result["status"] == "live_preflight_failed"
    assert "adapter_interface" in result["preflight"]["missing"]
    assert worker.client.calls == []
    assert read_state(tmp_path)["positions"] == {}


def test_missing_state_with_existing_events_is_ledger_error(tmp_path):
    worker = worker_for(tmp_path)
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"type": "intent", "id": 1, "time": NOW.isoformat(), "idempotency_key": "bsc:x:p:s:buy"}) + "\n",
        encoding="utf-8",
    )

    result = worker.run_once(NOW)

    assert result["status"] == "ledger_error"
    assert not (tmp_path / "state.json").exists()


def test_state_and_events_transaction_mismatch_is_ledger_error(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    state = read_state(tmp_path)
    state["orders"] = {}
    state["pending_intents"] = {}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert result["status"] == "ledger_error"
    assert result["events"] == []


def test_duplicate_event_submission_is_ledger_error(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    event_lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    (tmp_path / "events.jsonl").write_text("\n".join(event_lines + [event_lines[0]]) + "\n", encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert result["status"] == "ledger_error"
    assert result["events"] == []


def test_duplicate_event_id_is_ledger_error_without_new_action(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    event_lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    duplicate = json.loads(event_lines[0])
    duplicate["tx_id"] = "different-transaction"
    duplicate["tx_seq"] = 1
    duplicate["tx_size"] = 1
    duplicate["tx_complete"] = True
    (tmp_path / "events.jsonl").write_text(
        "\n".join(event_lines + [json.dumps(duplicate)]) + "\n", encoding="utf-8"
    )

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert result["status"] == "ledger_error"
    assert result["error"] == "event_id_duplicate"
    assert len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize("bad_id", [0, -1, "1", 1.5, True])
def test_invalid_event_id_format_is_ledger_error(tmp_path, bad_id):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    event_lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    event = json.loads(event_lines[0])
    event["id"] = bad_id
    (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert result["status"] == "ledger_error"
    assert result["events"] == []


def test_state_sequence_below_event_id_is_ledger_error(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    state = read_state(tmp_path)
    state["sequence"] = 0
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert result["status"] == "ledger_error"
    assert result["error"] == "state_sequence_behind_events"
    assert len(read_events(tmp_path)) == 1


def test_corrupt_position_schema_is_ledger_error(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    state = read_state(tmp_path)
    state["positions"][next(iter(state["positions"]), "missing")] = {}
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert result["status"] == "ledger_error"
    assert result["events"] == []


def test_duplicate_quotes_reject_the_entire_round(tmp_path):
    worker = worker_for(tmp_path)
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    payload["quotes"].append(dict(payload["quotes"][0], price_usd=2.0))
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")

    result = worker.run_once(NOW)

    assert result["status"] == "input_error"
    assert any(item["reason"] == "duplicate_quote" for item in result["rejections"])
    assert read_state(tmp_path)["positions"] == {}
    assert read_events(tmp_path) == []


def test_price_and_depth_impact_change_paper_buy_fill(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    replace_quote(worker, price=1.0, at=NOW + timedelta(seconds=1), price_impact=2.0, depth_impact=3.0)
    worker.run_once(NOW + timedelta(seconds=1))

    fill = read_events(tmp_path)[1]
    assert fill["fill_price_usd"] == pytest.approx(1.0 * (1 + 0.005 + 0.02 + 0.03))
    assert fill["price_impact_rate"] == pytest.approx(0.02)
    assert fill["depth_impact_rate"] == pytest.approx(0.03)


def test_missing_impact_rejects_entry_without_action(tmp_path):
    worker = worker_for(tmp_path)
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    payload["quotes"][0].pop("depth_impact")
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")

    result = worker.run_once(NOW)

    assert result["status"] == "input_error"
    assert any(item["reason"] == "invalid_quote_impact" for item in result["rejections"])
    assert read_events(tmp_path) == []


def test_stale_exit_quote_is_rejected_without_changing_position(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    replace_quote(worker, price=1.0, at=NOW + timedelta(seconds=1))
    worker.run_once(NOW + timedelta(seconds=1))
    replace_quote(worker, price=0.78, at=NOW + timedelta(seconds=2))
    worker.run_once(NOW + timedelta(seconds=2))
    before = read_state(tmp_path)
    replace_quote(worker, price=0.5, at=NOW + timedelta(seconds=40))
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    payload["quotes"][0]["quote_observed_at"] = (NOW + timedelta(seconds=2)).isoformat()
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=40))

    assert any(item["reason"] == "stale_exit_quote" for item in result["rejections"])
    assert read_state(tmp_path)["positions"] == before["positions"]


def test_daily_loss_breaker_blocks_new_entry(tmp_path):
    worker = worker_for(tmp_path)
    worker.run_once(NOW)
    state = read_state(tmp_path)
    state["daily_loss_usd"] = 8.0
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    payload = json.loads(worker.input_path.read_text(encoding="utf-8"))
    payload["signals"][0]["contract_address"] = "0xDEF"
    payload["signals"][0]["pool_address"] = "0xPOOL2"
    payload["quotes"][0]["contract_address"] = "0xDEF"
    payload["quotes"][0]["pool_address"] = "0xPOOL2"
    payload["quotes"][0]["pair_address"] = "0xPOOL2"
    worker.input_path.write_text(json.dumps(payload), encoding="utf-8")

    result = worker.run_once(NOW + timedelta(seconds=1))

    assert any(item["reason"] == "daily_loss_circuit_breaker" for item in result["rejections"])
    assert len(read_state(tmp_path)["positions"]) == 0


def test_live_mode_is_disabled_without_explicit_activation(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    result = worker_for(tmp_path, mode="live").run_once(NOW)

    assert result["status"] == "live_preflight_failed"
    assert result["preflight"]["checks"]["adapter_interface"]["reason"] == "missing"
    assert read_state(tmp_path)["positions"] == {}
    assert read_events(tmp_path) == []


def test_corrupt_state_returns_ledger_error_and_does_nothing(tmp_path):
    worker = worker_for(tmp_path)
    (tmp_path / "state.json").write_text("{not-json", encoding="utf-8")

    result = worker.run_once(NOW)

    assert result["status"] == "ledger_error"
    assert result["events"] == []
    assert worker.client.calls == []
    assert not (tmp_path / "events.jsonl").exists()


def test_state_and_events_use_required_ledger_files(tmp_path):
    worker_for(tmp_path).run_once(NOW)

    assert (tmp_path / "state.json").exists()
    assert (tmp_path / "events.jsonl").exists()
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "health.json").exists()
