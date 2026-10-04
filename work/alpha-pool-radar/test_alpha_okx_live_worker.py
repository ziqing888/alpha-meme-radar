import json
from decimal import Decimal
import sqlite3
from datetime import timedelta

import pytest

from alpha_gmgn_live_worker import GmgnLiveWorker, ExecutionError
from test_alpha_gmgn_live_worker import FakeTransport, Clock, payload, TOKEN, POOL

HASH = "0x" + "a" * 64


class OkxTransport(FakeTransport):
    durable_prepare = True

    def __init__(self):
        super().__init__()
        self.contexts = []
        self.allowance = True
        self.approvals = []
        self.after_prepare = None

    def set_context(self, token, pool):
        self.contexts.append((token, pool))

    def ensure_allowance(self, token, amount_atomic):
        self.approvals.append((token, amount_atomic))
        return {"ready": self.allowance}

    def submit(self, token, side, amount_atomic, slippage_percent, *, deadline=None, on_prepared=None):
        result = super().submit(token, side, amount_atomic, slippage_percent, deadline=deadline)
        tx = "0x" + str(len(self.submissions)).zfill(64)
        self.receipts[tx] = {**self.receipts.pop(result["order_id"]), "order_id": tx}
        on_prepared(tx)
        if self.after_prepare:
            raise self.after_prepare
        return {"order_id": tx}


def make(tmp_path, provider="okx"):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload()), encoding="utf-8")
    transport, clock = OkxTransport(), Clock()
    worker = GmgnLiveWorker(source, transport, state_root=tmp_path / "ledger", enabled=True,
                            clock=clock, provider=provider)
    return worker, transport, clock


def test_provider_binding_prevents_alternating_budgets(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    gmgn = GmgnLiveWorker(worker.input_path, client, state_root=worker.directory.parent, clock=clock)
    with pytest.raises(ExecutionError, match="ledger_provider_mismatch"):
        gmgn.run_once()
    assert len(client.submissions) == 1


def test_old_nonempty_ledger_is_gmgn_not_okx(tmp_path):
    worker, client, clock = make(tmp_path, "gmgn")
    worker.run_once()
    with sqlite3.connect(worker.directory / "ledger.sqlite3") as db:
        state = json.loads(db.execute("SELECT data FROM ledger").fetchone()[0])
        state.pop("provider", None)
        db.execute("UPDATE ledger SET data=?", (json.dumps(state),))
    switched = GmgnLiveWorker(worker.input_path, client, state_root=worker.directory.parent,
                              clock=clock, provider="okx")
    with pytest.raises(ExecutionError, match="ledger_provider_mismatch"):
        switched.run_once()


@pytest.mark.parametrize("failure", [TimeoutError("response lost"), KeyboardInterrupt()])
def test_prepared_hash_survives_timeout_and_crash(tmp_path, failure):
    worker, client, clock = make(tmp_path)
    client.after_prepare = failure
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            worker.run_once()
    else:
        worker.run_once()
    client.after_prepare = None
    resumed = GmgnLiveWorker(worker.input_path, client, state_root=worker.directory.parent,
                             enabled=True, clock=clock, provider="okx")
    report = resumed.run_once()
    assert report["open_positions"] == 1
    assert report["pending_orders"] == 0
    assert report["provider"] == "okx"
    assert len(client.submissions) == 1
    assert client.contexts[0] == (TOKEN, POOL)


def test_approval_pending_is_not_sell_or_fill(tmp_path):
    worker, client, clock = make(tmp_path)
    worker.run_once()
    worker.run_once()
    client.allowance = False
    client.price = Decimal(2)
    waiting = worker.run_once()
    assert waiting["open_positions"] == 1
    assert len(waiting["orders"]) == len(client.submissions) == 1
    assert client.approvals[-1] == (TOKEN, str(4 * 10**18))
    assert any(i["reason"] == "allowance_pending" for i in waiting["issues"])
    client.allowance = True
    worker.run_once()
    assert client.submissions[-1][1:] == ("sell", 4 * 10**18)
    assert worker.run_once()["positions"][0]["remaining_atomic"] == str(10**18)


def test_disabled_okx_never_calls_provider(tmp_path):
    worker, client, _ = make(tmp_path)
    worker.enabled = False
    assert worker.run_once()["status"] == "disabled"
    assert client.reads == client.submissions == client.contexts == []


@pytest.mark.parametrize("flag", [None, "OKX_LIVE_ENABLED", "OKX_ALLOW_AUTOMATED_TRADES"])
def test_cli_requires_both_optins_before_constructing_transport(monkeypatch, capsys, flag):
    import alpha_okx_live_worker as cli
    monkeypatch.delenv("OKX_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("OKX_ALLOW_AUTOMATED_TRADES", raising=False)
    if flag:
        monkeypatch.setenv(flag, "1")
    assert cli.main(["--live", "--once"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "explicit_live_environment_required"


def test_cli_check_does_not_create_worker_or_connect(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    import alpha_okx_live_worker as cli
    calls = []
    fake = SimpleNamespace(wallet_address="0x" + "3" * 40,
                           preflight=lambda: calls.append("offline"))
    monkeypatch.setitem(sys.modules, "alpha_okx_live_transport", SimpleNamespace(OkxLiveTransport=lambda: fake))
    assert cli.main(["--check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configured"] and not result["live_started"]
    assert result["provider_permissions_verified"] is False
    assert calls == ["offline"]


def test_cli_does_not_echo_secret_errors(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    import alpha_okx_live_worker as cli

    def broken():
        raise ValueError("secret API credential")

    monkeypatch.setitem(sys.modules, "alpha_okx_live_transport", SimpleNamespace(OkxLiveTransport=broken))
    assert cli.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "secret API credential" not in output
    assert json.loads(output)["live_started"] is False


def test_approval_fees_are_expensed_once_including_after_restart(tmp_path):
    worker, client, clock = make(tmp_path)
    client.fees = lambda: [{"id": "okx-approval:" + HASH, "gas_usd": "0.12"}]
    first = worker.run_once()
    assert first["cash_usd_estimate"] == "99.88"
    assert first["realized_pnl_usd_estimate"] == "-0.12"
    resumed = GmgnLiveWorker(worker.input_path, client, state_root=worker.directory.parent,
                             enabled=True, clock=clock, provider="okx")
    result = resumed.run_once()
    assert Decimal(result["cash_usd_estimate"]) == Decimal("94.87")
    assert result["realized_pnl_usd_estimate"] == "-0.12"


def test_busy_transport_does_not_consume_candidate_attempt(tmp_path):
    worker, client, _ = make(tmp_path)
    client.ready_for_entries = lambda: False
    assert worker.run_once()["orders"] == []
    assert client.submissions == []
    client.ready_for_entries = lambda: True
    assert worker.run_once()["pending_orders"] == 1


def test_unavailable_fee_reconciliation_blocks_buys(tmp_path):
    worker, client, _ = make(tmp_path)

    def failed_fees():
        raise RuntimeError("RPC unavailable")

    client.fees = failed_fees
    report = worker.run_once()
    assert report["orders"] == []
    assert any(i["reason"] == "provider_fee_reconciliation_failed" for i in report["issues"])


def test_live_verifies_receipt_capability_before_creating_worker(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    import alpha_okx_live_worker as cli
    monkeypatch.setattr(cli, "GmgnLiveWorker", lambda *args, **kwargs: pytest.fail("worker constructed before readiness"))
    monkeypatch.setenv("OKX_LIVE_ENABLED", "1")
    monkeypatch.setenv("OKX_ALLOW_AUTOMATED_TRADES", "1")
    fake = SimpleNamespace(wallet_address="0x" + "3" * 40, preflight=lambda: None,
                           verify_connection=lambda: {"ready": False})
    monkeypatch.setitem(sys.modules, "alpha_okx_live_transport", SimpleNamespace(OkxLiveTransport=lambda: fake))
    assert cli.main(["--live", "--once"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "provider_verification_incomplete"
    assert result["live_started"] is False


def test_prebroadcast_exit_rejections_recover_after_three_attempts(tmp_path):
    class Rejected(Exception):
        ambiguous = False

    worker, client, clock = make(tmp_path)
    worker.run_once()
    worker.run_once()
    client.price = Decimal("0.70")
    client.crash = Rejected("gas_exceeds_0_5_usd")
    for _ in range(3):
        worker.run_once()
        clock.now += timedelta(seconds=61)
    assert len(client.submissions) == 4
    client.crash = None
    result = worker.run_once()
    assert len(client.submissions) == 5
    assert result["pending_orders"] == 1
    assert worker.run_once()["open_positions"] == 0


def test_prepared_but_never_broadcast_cancellation_is_not_mined_failure(tmp_path):
    worker, client, _ = make(tmp_path)
    worker.run_once()
    tx = next(iter(client.receipts))
    client.receipts[tx] = {"order_id": tx, "status": "rejected", "wallet_address": client.wallet_address,
                           "chain": "bsc", "broadcast_attempted": False, "gas_usd": "0"}
    result = worker.run_once()
    assert result["orders"][0]["status"] == "rejected"
    assert result["cash_usd_estimate"] == "100"
    assert result["pending_orders"] == result["open_positions"] == 0


def test_rejection_without_definitive_no_broadcast_evidence_remains_pending(tmp_path):
    worker, client, _ = make(tmp_path)
    worker.run_once()
    tx = next(iter(client.receipts))
    client.receipts[tx] = {"order_id": tx, "status": "rejected"}
    report = worker.run_once()
    assert report["pending_orders"] == 1
    assert report["cash_usd_estimate"] == "100"
