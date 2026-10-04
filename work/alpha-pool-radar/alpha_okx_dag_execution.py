"""Bounded DAG transaction preparation; no signer, broadcaster or live enablement.

Router pin is bound to the official OKX BSC deployment and observed runtime on
2026-09-08. Adapter source and Pancake factory membership are checked on chain.
Fee acceptance is a separate explicit operator policy, never inferred from API.
"""
import time
import alpha_okx_dag_preflight as dag

ROUTER_RUNTIME_HASH = "0x281df27171714da102e62d164aad2e9770be72bacfde1025d2de4b6822e38700"


def require_fresh_deadline(tx):
    values = dag.decode(dag.ABI, bytes.fromhex(tx["data"][10:]), strict=True)
    dag.require(values[2][4] >= time.time() + 30, "dag_deadline_too_close")


def validate_trim(check, trim_recipient=None, max_trim_per_mille=0):
    cap = dag.uint(max_trim_per_mille)
    dag.require(cap <= 100, "dag_trim_cap_invalid")
    trim = check["trim"]
    if trim and trim["rate_per_1000"] > 0:
        dag.require(bool(trim_recipient) and cap > 0, "dag_trim_consent_required")
        dag.require(trim["recipient"] == dag.address(trim_recipient), "dag_trim_recipient_mismatch")
        dag.require(trim["rate_per_1000"] <= cap, "dag_trim_cap_exceeded")


def prepare_dag(tx, *, rpc, wallet, source, target, amount, minimum, expected_pool,
                now=None, trim_recipient=None, max_trim_per_mille=0):
    now = time.time() if now is None else now
    check = dag.inspect_dag(tx, wallet=wallet, source=source, target=target,
                            amount=amount, minimum=minimum, now=now,
                            simulation_only_long_deadline=True)
    endpoint = check["hops"][-1] if source == dag.OKX_NATIVE else check["hops"][0]
    dag.require(endpoint["pool"] == dag.address(expected_pool), "dag_candidate_pool_mismatch")
    validate_trim(check, trim_recipient, max_trim_per_mille)
    block = dag.pin_block(rpc)["reference"]
    code = rpc.call("eth_getCode", [dag.BSC_ROUTER, block])
    try:
        code_hash = "0x" + dag.keccak(bytes.fromhex(code[2:])).hex()
    except Exception:
        raise dag.PreflightError("dag_router_code_invalid") from None
    dag.require(code_hash == ROUTER_RUNTIME_HASH, "dag_router_runtime_mismatch")
    for hop in check["hops"]:
        dag.verify_v3_pool(rpc, hop, block)
        dag.verify_v3_adapter(rpc, hop["adapter"], block)
    # Shorten only the deadline; keep every route/fee/amount byte unchanged.
    body = bytes.fromhex(tx["data"][10:])
    values = list(dag.decode(dag.ABI, body, strict=True))
    canonical = dag.encode(dag.ABI, values)
    base = list(values[2])
    base[4] = min(base[4], int(now) + 180)
    values[2] = tuple(base)
    prepared = dict(tx)
    prepared["data"] = dag.SELECTOR + (dag.encode(dag.ABI, values) + body[len(canonical):]).hex()
    dag.inspect_dag(prepared, wallet=wallet, source=source, target=target, amount=amount,
                    minimum=minimum, now=now)
    simulated = dag.simulate(rpc, prepared, block, int(check["min_output_atomic"]))
    dag.require(simulated["status"] == "simulated", "dag_simulation_failed")
    require_fresh_deadline(prepared)
    return prepared
