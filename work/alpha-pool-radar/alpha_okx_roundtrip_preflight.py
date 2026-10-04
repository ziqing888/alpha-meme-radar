"""Stateful read-only buy/approve/sell simulation. Never signs or broadcasts.

eth_simulateV1 carries state between calls inside an ephemeral simulated block.
Validation is eth_call-style, not transaction admission or real fee validation.
No state overrides, imported keys, impersonation RPCs, or execution flags.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

from alpha_okx_dag_preflight import (
    BSC_APPROVER, BSC_ROUTER, OKX_NATIVE, USDT, OkxLiveTransport, PreflightError,
    ReadOnlyRpc, address, attest_adapters, inspect_dag, pin_block, require, uint, verify_v3_pool,
)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def simulate_roundtrip(rpc, *, buy, sell, token, block, buy_minimum, sell_amount,
                       sell_minimum, quote_deadline, calldata_deadlines):
    wallet = address(buy["from"])
    token = address(token)
    require(address(sell["from"]) == wallet, "sender_mismatch")
    require(address(buy["to"]) == address(sell["to"]) == BSC_ROUTER, "router_mismatch")
    require(uint(buy.get("value")) > 0 and uint(sell.get("value", 0)) == 0, "native_value_mismatch")
    buy_minimum, sell_amount, sell_minimum = map(uint, (buy_minimum, sell_amount, sell_minimum))
    require(0 < sell_amount <= buy_minimum and sell_minimum > 0, "roundtrip_amount_invalid")
    require(time.monotonic() <= quote_deadline, "quote_expired_before_simulation")
    header = rpc.call("eth_getBlockByNumber", [block["number"], False])
    require(header.get("hash") == block["hash"], "roundtrip_parent_mismatch")

    def tx(to, data, value=0):
        return {"from": wallet, "to": to, "data": data, "value": hex(uint(value)), "gas": "0x2dc6c0"}

    balance = tx(token, "0x70a08231" + wallet[2:].zfill(64))
    approve = "0x095ea7b3" + BSC_APPROVER[2:].zfill(64)
    calls = [balance, tx(BSC_ROUTER, buy["data"], buy["value"]), balance,
             tx(token, approve + "0" * 64), tx(token, approve + f"{sell_amount:064x}"),
             tx(BSC_ROUTER, sell["data"]), balance]
    blocks = rpc.call("eth_simulateV1", [{"blockStateCalls": [{"calls": calls}],
                                         "validation": False, "traceTransfers": True}, block["number"]])
    require(time.monotonic() <= quote_deadline, "quote_expired_after_simulation")
    require(isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict),
            "roundtrip_response_invalid")
    require(blocks[0].get("parentHash") == block["hash"], "roundtrip_parent_mismatch")
    require(uint(blocks[0].get("number")) == uint(block["number"]) + 1, "roundtrip_child_number_invalid")
    timestamp = uint(blocks[0].get("timestamp"))
    require(block["timestamp"] < timestamp <= block["timestamp"] + 60, "roundtrip_child_timestamp_invalid")
    require(len(calldata_deadlines) == 2 and all(timestamp <= uint(d) for d in calldata_deadlines),
            "roundtrip_calldata_expired")
    header = rpc.call("eth_getBlockByNumber", [block["number"], False])
    require(header.get("hash") == block["hash"], "roundtrip_parent_mismatch")
    require(time.monotonic() <= quote_deadline, "quote_expired_after_simulation")
    results = blocks[0].get("calls")
    require(isinstance(results, list) and len(results) == 7, "roundtrip_response_invalid")
    values, gas = [], []
    for i, row in enumerate(results):
        require(isinstance(row, dict) and row.get("status") == "0x1", "roundtrip_call_failed_" + str(i))
        value = row.get("returnData")
        require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", value), "roundtrip_return_invalid")
        values.append(uint(value))
        gas.append(uint(row.get("gasUsed")))
    before, buy_output, after_buy, reset_ok, approve_ok, sell_output, after_sell = values
    bought = after_buy - before
    require(bought >= buy_minimum and bought >= sell_amount and buy_output >= buy_minimum,
            "roundtrip_buy_delta_insufficient")
    require(reset_ok == approve_ok == 1, "roundtrip_approval_failed")
    require(after_buy - after_sell == sell_amount, "roundtrip_sell_delta_mismatch")
    require(sell_output >= sell_minimum, "roundtrip_sell_minimum_failed")
    received = 0
    wallet_topic = "0x" + wallet[2:].zfill(64)
    for log in results[5].get("logs", []):
        if not isinstance(log, dict) or str(log.get("address", "")).lower() != OKX_NATIVE:
            continue
        topics, data = log.get("topics"), log.get("data")
        require(isinstance(topics, list) and len(topics) == 3 and topics[0] == TRANSFER_TOPIC
                and all(isinstance(t, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", t) for t in topics)
                and isinstance(data, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", data),
                "roundtrip_native_log_invalid")
        received += uint(data) * ((topics[2].lower() == wallet_topic) - (topics[1].lower() == wallet_topic))
    require(received >= sell_minimum, "roundtrip_native_receipt_insufficient")
    return {"status": "simulated_round_trip", "broadcast": False, "execution_ready": False,
            "transaction_admission_verified": False, "state_overrides": False,
            "bought_atomic": str(bought), "sold_atomic": str(sell_amount),
            "remaining_new_token_atomic": str(after_sell - before),
            "native_received_atomic": str(received), "router_reported_native_atomic": str(sell_output),
            "gas_used_by_call": gas,
            "parent_block": block["number"], "parent_hash": block["hash"]}


def run(config, *, notional_usd='5', token=USDT):
    wallet = address(config["wallet_address"])
    token = address(token)
    require(token != OKX_NATIVE, 'invalid_probe_token')
    env = {key: os.environ.get(key, "") for key in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")}
    env.update(BSC_WALLET_ADDRESS=wallet, BSC_RPC_URL=config["bsc_rpc_url"],
               OKX_LIVE_ENABLED="0", OKX_ALLOW_AUTOMATED_TRADES="0")
    transport, rpc = OkxLiveTransport(env=env), ReadOnlyRpc(config["bsc_rpc_url"])
    require(uint(rpc.call("eth_chainId", [])) == 56, "chain_mismatch")
    require(notional_usd in {'1', '5'}, 'invalid_probe_notional')
    amount = int(Decimal(transport.market()["max_buy_amount_atomic"]) * Decimal(notional_usd) / 5)
    started = time.monotonic()

    def build(source, target, amount):
        response = transport._client()._request("/api/v6/dex/aggregator/swap", {
            "chainIndex": "56", "fromTokenAddress": source, "toTokenAddress": target,
            "amount": str(amount), "userWalletAddress": wallet, "slippagePercent": "1", "dexIds": "200"})
        require(str(response.get("code")) == "0", "okx_build_failed")
        row = response["data"][0]
        require(not row.get("additionalSignatureData"), "additional_signature_unsupported")
        route, tx = row["routerResult"], row["tx"]
        transport._route_identity(route, source, target, amount)
        minimum = uint(route["toTokenAmount"]) * 99 // 100
        inspection = inspect_dag(tx, wallet=wallet, source=source, target=target, amount=amount,
                                  minimum=minimum, simulation_only_long_deadline=True)
        return tx, inspection

    buy, buy_check = build(OKX_NATIVE, token, amount)
    sell_amount = int(buy_check["min_output_atomic"])
    sell, sell_check = build(token, OKX_NATIVE, sell_amount)
    block = pin_block(rpc)
    result = simulate_roundtrip(rpc, buy=buy, sell=sell, token=token, block=block,
                                buy_minimum=sell_amount, sell_amount=sell_amount,
                                calldata_deadlines=[buy_check["deadline"], sell_check["deadline"]],
                                sell_minimum=int(sell_check["min_output_atomic"]), quote_deadline=started + 10)
    result.update(token=token, native_input_atomic=str(amount), memecoin_verified=False,
                  inspections=[buy_check, sell_check], pool_checks=[], adapter_checks=[])
    seen = set()
    for inspection in result["inspections"]:
        for hop in inspection["hops"]:
            identity = (hop["pool"], hop["input_token"], hop["output_token"])
            if identity not in seen:
                result["pool_checks"].append(verify_v3_pool(rpc, hop, block["reference"]))
                seen.add(identity)
        inspection["blockers"].remove("pool_factory_unverified")
        inspection["pool_membership_verified"] = True
        result["adapter_checks"].extend(attest_adapters(rpc, inspection, block["reference"]))
    result["quote_fresh_at_report"] = time.monotonic() <= started + 10
    if not result["quote_fresh_at_report"]:
        result["status"] = "historical_simulated_round_trip"
    return result


def main():
    root = Path.home() / ".config/alpha-radar"
    report = {"mode": "read_only", "signed": False, "broadcast": False, "execution_ready": False,
              "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        config = json.loads((root / "okx-live-config.json").read_text(encoding="utf-8-sig"))
        report.update(run(config))
    except Exception as exc:
        code = str(exc)
        report["error"] = code if isinstance(exc, PreflightError) and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", code) else "roundtrip_probe_failed"
        if isinstance(exc, PreflightError) and exc.details:
            report["diagnostics"] = exc.details
    (root / "okx-roundtrip-preflight.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 1 if "error" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
