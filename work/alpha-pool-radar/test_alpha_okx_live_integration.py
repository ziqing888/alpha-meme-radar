"""End-to-end worker/transport boundary with deterministic offline RPC and signer."""
import json
from decimal import Decimal

from alpha_gmgn_live_worker import GmgnLiveWorker
from test_alpha_gmgn_live_worker import Clock, payload
from test_alpha_okx_live_transport import system, TOKEN, POOL, SELL


def test_real_adapter_buy_approve_stop_sell_and_restart(system, tmp_path):
    transport, rpc, http, signer, _ = system
    clock = Clock()
    source = tmp_path / "signals.json"
    candidate = payload((TOKEN,))
    candidate["signals"][0]["pool_address"] = POOL
    candidate["quotes"][0]["pool_address"] = POOL
    source.write_text(json.dumps(candidate), encoding="utf-8")
    worker = GmgnLiveWorker(source, transport, state_root=tmp_path / "worker", enabled=True,
                            clock=clock, provider="okx")
    initial = worker.run_once()
    assert initial["pending_orders"] == 1 and initial["open_positions"] == 0
    buy = initial["orders"][0]["order_id"]
    assert signer.signed[0]["value"] == 5 * 10**15
    rpc.mine(buy)
    source.unlink()  # Holding management is independent of the scanner.
    bought = worker.run_once()
    assert bought["positions"][0]["remaining_atomic"] == str(SELL)
    assert len(bought["orders"]) == 1
    assert len(rpc.sent) == 2  # Buy, then exact approval for stop-loss exit.
    approval = rpc.sent[-1]
    assert signer.signed[-1]["data"].startswith("0x095ea7b3")
    rpc.mine(approval)
    resumed = GmgnLiveWorker(source, transport, state_root=tmp_path / "worker", enabled=True,
                             clock=clock, provider="okx")
    selling = resumed.run_once()
    assert len(selling["orders"]) == 2
    sell = selling["orders"][-1]
    assert sell["side"] == "sell" and sell["amount_atomic"] == str(SELL)
    rpc.mine(sell["order_id"])
    closed = resumed.run_once()
    assert closed["open_positions"] == closed["pending_orders"] == 0
    assert len(rpc.sent) == 3
    assert Decimal(closed["realized_pnl_usd_estimate"]) < 0
    assert resumed.run_once()["cash_usd_estimate"] == closed["cash_usd_estimate"]
    assert len(rpc.sent) == 3
