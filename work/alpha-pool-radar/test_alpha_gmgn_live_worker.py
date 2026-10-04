import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alpha_gmgn_live_worker import GmgnLiveWorker, ExecutionError, NATIVE, account_lock, atoms, decimal

NOW = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
TOKEN = "0x" + "1" * 40
POOL = "0x" + "2" * 40
WALLET = "0x" + "3" * 40


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def payload(tokens=(TOKEN,)):
    return {"updated_at": NOW.isoformat(), "signals": [
        {"chain": "bsc", "contract_address": token, "pool_address": POOL,
         "execution_arm": "first_discovery", "signal_at": (NOW - timedelta(seconds=1)).isoformat(),
         "first_seen_at": (NOW - timedelta(minutes=10)).isoformat(), "mcap": 50000,
         "entry_score": 75, "change_m5": 3, "change_h1": 15, "symbol": "TEST"} for token in tokens],
        "quotes": [{"chain": "bsc", "contract_address": token, "pool_address": POOL,
                    "quote_at": NOW.isoformat(), "quote_status": "fresh", "price_usd": 1,
                    "liquidity_usd": 20000} for token in tokens]}


class FakeTransport:
    wallet_address = WALLET

    def __init__(self):
        self.submissions = []
        self.price = Decimal(1)
        self.native_price = Decimal(1000)
        self.receipts = {}
        self.crash = None
        self.pending = False
        self.minimum_factor = Decimal("0.95")
        self.reads = []
        self.safe = True

    def market(self):
        self.reads.append("market")
        return {"native_price_usd": str(self.native_price)}

    def balance(self, token=NATIVE):
        self.reads.append("balance")
        return {"token": token, "amount_atomic": str(10**40), "decimals": 18}

    def security(self, token):
        self.reads.append("security")
        return {"safe": self.safe}

    def quote(self, token, side, amount_atomic, slippage_percent):
        self.reads.append("quote")
        amount = int(amount_atomic)
        output = int(Decimal(amount) * self.native_price / self.price if side == "buy"
                     else Decimal(amount) * self.price / self.native_price)
        return {"input_token": NATIVE if side == "buy" else token,
                "output_token": token if side == "buy" else NATIVE,
                "input_amount": str(amount), "output_amount": str(output),
                "min_output_amount": str(int(Decimal(output) * self.minimum_factor))}

    def submit(self, token, side, amount_atomic, slippage_percent, *, deadline=None):
        self.submissions.append((token, side, int(amount_atomic)))
        if self.crash:
            raise self.crash
        order_id = str(len(self.submissions))
        q = self.quote(token, side, amount_atomic, slippage_percent)
        self.receipts[order_id] = {**q, "order_id": order_id, "status": "confirmed", "wallet_address": WALLET,
                                   "chain": "bsc", "tx_hash": "0x" + order_id.zfill(64), "gas_usd": "0.01",
                                   "input_decimals": 18, "output_decimals": 18, "created_at": NOW.isoformat()}
        return {"order_id": order_id, "status": "pending"}

    def order(self, order_id):
        self.reads.append("order")
        if self.pending:
            return {"order_id": order_id, "status": "pending"}
        return self.receipts[order_id]


def make(tmp_path, *, enabled=True, tokens=(TOKEN,)):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload(tokens)), encoding="utf-8")
    transport, clock = FakeTransport(), Clock()
    worker = GmgnLiveWorker(source, transport, state_root=tmp_path / "ledger", enabled=enabled, clock=clock)
    return worker, transport, clock


def restart(worker, transport, clock):
    return GmgnLiveWorker(worker.input_path, transport, state_root=worker.directory.parent, enabled=True, clock=clock)


def test_default_disabled_never_calls_provider(tmp_path):
    worker, client, _ = make(tmp_path, enabled=False)
    assert worker.run_once()["status"] == "disabled"
    assert client.reads == client.submissions == []


def test_real_receipt_required_and_no_duplicate_after_restart(tmp_path):
    worker, client, clock = make(tmp_path)
    first = worker.run_once()
    assert first["open_positions"] == 0 and first["pending_orders"] == 1
    assert client.submissions[0][2] == 5 * 10**15
    second = restart(worker, client, clock).run_once()
    assert second["open_positions"] == 1 and second["pending_orders"] == 0
    assert second["positions"][0]["remaining_atomic"] == str(5 * 10**18)
    assert len(client.submissions) == 1


def test_tp80_then_trailing_sells_only_remaining20(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    worker.run_once()
    client.price = Decimal(2)
    result = worker.run_once()
    assert client.submissions[-1][1:] == ("sell", 4 * 10**18)
    result = worker.run_once()
    assert result["positions"][0]["remaining_atomic"] == str(10**18)
    assert result["positions"][0]["tp1_hit"] is True
    client.price = Decimal("1.25")
    worker.run_once()
    assert client.submissions[-1][1:] == ("sell", 10**18)
    result = worker.run_once()
    assert result["open_positions"] == 0
    assert Decimal(result["realized_pnl_usd_estimate"]) == Decimal("4.22")
    worker.run_once()
    assert len(client.submissions) == 3


def test_stop_and_time_exit_work_when_scanner_input_disappears(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    worker.run_once()
    worker.input_path.unlink()
    client.price = Decimal("0.77")
    worker.run_once()
    assert client.submissions[-1][1:] == ("sell", 5 * 10**18)
    assert worker.run_once()["open_positions"] == 0


def test_time_stop_without_scanner(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    worker.run_once()
    clock.now += timedelta(minutes=91)
    worker.run_once()
    assert client.submissions[-1][1] == "sell"


@pytest.mark.parametrize("crash", [TimeoutError("secret should not leak"), KeyboardInterrupt()])
def test_ambiguous_submit_and_crash_never_resubmit(tmp_path, crash):
    worker, client, clock = make(tmp_path)
    client.crash = crash
    if isinstance(crash, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            worker.run_once()
    else:
        worker.run_once()
    client.crash = None
    result = restart(worker, client, clock).run_once()
    assert len(client.submissions) == 1
    assert result["pending_orders"] == 1
    assert result["open_positions"] == 0
    assert "secret" not in json.dumps(result)


def test_pending_reserves_slots_and_not_one_trade_per_day(tmp_path):
    tokens = tuple("0x" + str(n) * 40 for n in range(1, 6))
    worker, client, _ = make(tmp_path, tokens=tokens)
    client.pending = True
    result = worker.run_once()
    assert len(client.submissions) == 3
    assert result["pending_orders"] == 3
    worker.run_once()
    assert len(client.submissions) == 3


@pytest.mark.parametrize("field,value", [("output_token", "0x" + "9" * 40), ("wallet_address", "0x" + "9" * 40),
                                        ("input_amount", str(6 * 10**15)), ("output_decimals", 300)])
def test_invalid_receipt_does_not_create_position(tmp_path, field, value):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    client.receipts["1"][field] = value
    result = worker.run_once()
    assert result["open_positions"] == 0
    assert result["orders"][0]["status"] == "reconcile_required"


def test_stale_signal_or_unknown_security_never_buys(tmp_path):
    worker, client, clock = make(tmp_path)
    client.safe = False
    worker.run_once()
    assert not client.submissions
    client.safe = True
    clock.now += timedelta(seconds=31)
    worker.run_once()
    assert not client.submissions


def test_excess_slippage_quote_rejected(tmp_path):
    worker, client, _ = make(tmp_path)
    client.minimum_factor = Decimal("0.80")
    worker.run_once()
    assert not client.submissions


def test_daily_loss_gate_blocks_entries(tmp_path):
    worker, client, _ = make(tmp_path, enabled=False)
    worker.run_once()
    with sqlite3.connect(worker.directory / "ledger.sqlite3") as db:
        data = json.loads(db.execute("SELECT data FROM ledger").fetchone()[0])
        data["daily_losses"] = {"2026-09-08": "8"}
        db.execute("UPDATE ledger SET data=?", (json.dumps(data),))
    worker.enabled = True
    worker.run_once()
    assert not client.submissions


def test_definitively_failed_sell_retries_after_cooldown(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    worker.run_once()
    client.price = Decimal("0.77")
    worker.run_once()
    client.receipts["2"]["status"] = "failed"
    worker.run_once()
    assert len(client.submissions) == 2
    clock.now += timedelta(seconds=61)
    worker = restart(worker, client, clock)
    worker.run_once()
    assert client.submissions[-1][1:] == ("sell", 5 * 10**18)
    assert len(client.submissions) == 3


def test_candidate_cannot_override_position_limit(tmp_path):
    tokens = tuple("0x" + str(n) * 40 for n in range(1, 5))
    worker, client, clock = make(tmp_path, tokens=tokens[:3])
    worker.run_once()
    worker.run_once()
    client.price = Decimal(2)
    worker.run_once()
    worker.run_once()
    data = payload(tokens[3:])
    data["signals"][0]["open_positions"] = 0
    worker.input_path.write_text(json.dumps(data), encoding="utf-8")
    worker.run_once()
    assert len([s for s in client.submissions if s[1] == "buy"]) == 3


def test_failure_actual_gas_is_counted(tmp_path):
    worker, client, _ = make(tmp_path)
    worker.run_once()
    client.receipts["1"].update(status="failed", gas_usd="9")
    result = worker.run_once()
    assert result["cash_usd_estimate"] == "91"
    assert worker.state["daily_losses"]["2026-09-08"] == "9"


def test_delayed_reconcile_does_not_restart_time_stop(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    clock.now += timedelta(hours=2)
    restart(worker, client, clock).run_once()
    assert client.submissions[-1][1] == "sell"


def test_unknown_order_can_be_attached_without_resubmit(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    with sqlite3.connect(worker.directory / "ledger.sqlite3") as db:
        data = json.loads(db.execute("SELECT data FROM ledger").fetchone()[0])
        key = next(iter(data["orders"]))
        data["orders"][key].pop("order_id")
        data["orders"][key]["status"] = "unknown"
        db.execute("UPDATE ledger SET data=?", (json.dumps(data),))
    worker = restart(worker, client, clock)
    result = worker.attach_order(key, "1")
    assert result["open_positions"] == 1
    assert len(client.submissions) == 1
    with pytest.raises(ExecutionError):
        worker.attach_order(key, "1")


def test_recovery_rejects_historical_same_amount_fill(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    with sqlite3.connect(worker.directory / "ledger.sqlite3") as db:
        data = json.loads(db.execute("SELECT data FROM ledger").fetchone()[0])
        key = next(iter(data["orders"]))
        data["orders"][key].pop("order_id")
        data["orders"][key]["status"] = "unknown"
        db.execute("UPDATE ledger SET data=?", (json.dumps(data),))
    client.receipts["1"]["created_at"] = (NOW - timedelta(days=1)).isoformat()
    with pytest.raises(ExecutionError, match="execution_time_mismatch"):
        worker.attach_order(key, "1")
    assert restart(worker, client, clock).run_once()["pending_orders"] == 1


def test_worker_through_real_transport_parsers_with_fake_cli(tmp_path, monkeypatch):
    import subprocess
    from alpha_gmgn_live_transport import GmgnLiveTransport
    from test_alpha_gmgn_live_transport import ENV

    sim = FakeTransport()
    def run(command, **kwargs):
        assert kwargs["shell"] is False
        args = command[5:]
        def flag(name):
            return args[args.index(name) + 1]
        if args[0] == "gas-price":
            result = {"native_token_usd_price": "1000", "chain": "bsc"}
        elif args[:2] == ["token", "security"]:
            result = {"is_honeypot": "no", "rug_ratio": "0.01", "buy_tax": "0.01", "sell_tax": "0.01",
                      "top_10_holder_rate": "0.2", "open_source": "yes", "owner_renounced": "yes", "is_wash_trading": False}
        elif args[:2] == ["order", "get"]:
            receipt = sim.order(flag("--order-id"))
            result = {"order_id": receipt["order_id"], "status": "successful", "state": 30,
                      "hash": receipt["tx_hash"], "report": {**receipt,
                      "input_token_decimals": receipt["input_decimals"], "output_token_decimals": receipt["output_decimals"]}}
        else:
            side = "buy" if flag("--input-token") == NATIVE else "sell"
            token = flag("--output-token") if side == "buy" else flag("--input-token")
            amount = flag("--amount")
            if args[0] == "swap":
                result = sim.submit(token, side, amount, "5")
            else:
                assert args[:2] == ["order", "quote"]
                result = {**sim.quote(token, side, amount, "5"), "slippage": 5}
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
    def forbidden(*args, **kwargs):
        pytest.fail("real subprocess forbidden")
    monkeypatch.setattr(subprocess, "run", forbidden)
    client = GmgnLiveTransport({**ENV, "GMGN_WALLET_ADDRESS": WALLET,
                               "GMGN_LIVE_ENABLED": "1", "GMGN_ALLOW_AUTOMATED_TRADES": "1"}, run=run)
    monkeypatch.setattr(client, "balance", sim.balance)
    worker, _, clock = make(tmp_path)
    worker.transport = client
    worker.run_once()
    result = worker.run_once()
    assert result["open_positions"] == 1, result
    sim.price = Decimal(2)
    worker.run_once()
    result = worker.run_once()
    assert result["positions"][0]["remaining_atomic"] == str(10**18), result


def test_account_lock_blocks_second_worker(tmp_path):
    worker, client, clock = make(tmp_path)
    with account_lock(worker.directory / "worker.lock"):
        with pytest.raises(ExecutionError, match="already_running"):
            restart(worker, client, clock).run_once()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", True, "1e18"])
def test_invalid_atoms(value):
    with pytest.raises(ExecutionError):
        atoms(value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
def test_invalid_decimal(value):
    with pytest.raises(ExecutionError):
        decimal(value)
