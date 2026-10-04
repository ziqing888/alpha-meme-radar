"""Read-only DAG diagnostics, intentionally NOT an execution allowlist.

Canonical ABI follows okxlabs/Web3-DEX-Router-EVM-V1 IDexRouter.sol.
The deployed adapter bytecode/inner ABI still require independent verification.
This module never loads a wallet key, signs, approves, or broadcasts transactions.
An eth_call success proves neither a fill nor an executable round trip.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from eth_abi import decode, encode
from eth_utils import keccak

from alpha_okx_live_transport import (
    BSC_APPROVER, BSC_ROUTER, OKX_NATIVE, USDT, WBNB, OkxLiveTransport,
)

SELECTOR = "0x0c307f76"
ABI = ["uint256", "address", "(uint256,address,uint256,uint256,uint256)",
       "(address[],address[],uint256[],bytes[],uint256)[]"]
TRIM_FLAG = 0x777777771111
MASK = (1 << 160) - 1
# Official BSC deployment, checked 2026-09-08. Never accept a factory supplied
# solely by the quote or by the candidate pool itself.
PANCAKE_V3_FACTORY = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"
PANCAKE_V3_SOURCE = "https://developer.pancakeswap.finance/contracts/v3/addresses"
UNIVERSAL_V3_ADAPTER = "0x7a7ad9aa93cd0a2d0255326e5fb145cec14997ff"
# Sourcify chain 56 exact_match, UniversalUniswapV3Adaptor, checked 2026-09-08.
# Source identity is NOT evidence of OKX authorization or recipient ownership.
UNIVERSAL_V3_RUNTIME_HASH = "0x9a8f25e0af9de624cf9aeb9667e7723643da0bf3d43f9ac5e2f67a40f43eaa4d"
UNIVERSAL_V3_SOURCE = "https://sourcify.dev/server/v2/contract/56/" + UNIVERSAL_V3_ADAPTER + "?fields=all"


class PreflightError(ValueError):
    def __init__(self, code, details=None):
        super().__init__(code)
        self.details = details or {}


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise PreflightError("address_invalid")
    return value.lower()


def uint(value):
    if isinstance(value, bool) or not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", str(value)):
        raise PreflightError("integer_invalid")
    result = int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)
    if not 0 <= result < 1 << 256:
        raise PreflightError("integer_invalid")
    return result


def require(ok, code):
    if not ok:
        raise PreflightError(code)


def inspect_dag(tx, *, wallet, source, target, amount, minimum, now=None,
                simulation_only_long_deadline=False):
    """Check envelope and linear path structure; inner adapters remain untrusted."""
    now = time.time() if now is None else now
    wallet, source, target = map(address, (wallet, source, target))
    amount, minimum = uint(amount), uint(minimum)
    require(amount > 0 and minimum > 0, "amount_invalid")
    require(address(tx.get("from")) == wallet, "sender_mismatch")
    require(address(tx.get("to")) == BSC_ROUTER, "router_mismatch")
    require(uint(tx.get("chainId", 56)) == 56, "chain_mismatch")
    require(uint(tx.get("value", 0)) == (amount if source == OKX_NATIVE else 0), "native_value_mismatch")
    require(not tx.get("additionalSignatureData"), "additional_signature_unsupported")
    data = tx.get("data", "")
    require(isinstance(data, str) and data[:10].lower() == SELECTOR, "selector_unsupported")
    require(len(data) <= 131082 and re.fullmatch(r"0x[0-9a-fA-F]+", data), "abi_invalid")
    try:
        body = bytes.fromhex(data[10:])
        values = decode(ABI, body, strict=True)
        canonical = encode(ABI, values)
    except Exception:
        raise PreflightError("abi_invalid") from None
    require(body.startswith(canonical), "abi_noncanonical")
    trailer = body[len(canonical):]
    _, receiver, base, paths = values
    require(receiver == wallet, "recipient_mismatch")
    require(base[0] == int(source, 16) and base[1] == target, "token_mismatch")
    require(base[2] == amount, "amount_mismatch")
    require(base[3] >= minimum, "minimum_output_insufficient")
    deadline_limit = 3600 if simulation_only_long_deadline else 1200
    if not now < base[4] <= now + deadline_limit:
        raise PreflightError("deadline_invalid", {"deadline": base[4], "now": int(now),
                                                  "remaining_seconds": int(base[4] - now)})
    require(1 <= len(paths) <= 3, "path_count_unsupported")
    blockers = ["router_runtime_unverified", "adapter_runtime_unverified",
                "adapter_payload_unverified", "pool_factory_unverified"]
    if base[4] > now + 1200:
        blockers.append("deadline_exceeds_live_limit")
    hops = []
    for index, (adapters, assets, raw, extras, from_token) in enumerate(paths):
        require(len(adapters) == len(assets) == len(raw) == len(extras) == 1, "branched_path_unsupported")
        require(0 < from_token <= MASK, "transfer_mode_unsupported")
        if index == 0:
            require(from_token == int(WBNB if source == OKX_NATIVE else source, 16), "path_source_mismatch")
        packed = raw[0]
        allowed = MASK | (0xffff << 160) | (0xffff << 176) | (1 << 255)
        require(packed & ~allowed == 0, "path_flags_unsupported")
        require((packed >> 160) & 0xffff == 10000, "path_weight_invalid")
        require((packed >> 184) & 0xff == index and (packed >> 176) & 0xff == index + 1,
                "path_index_invalid")
        require(int(adapters[0], 16) != 0 and assets[0] == adapters[0], "asset_destination_unsupported")
        pool = "0x" + f"{packed & MASK:040x}"
        require(int(pool, 16) != 0, "pool_invalid")
        # Universal V3 uses a two-address payload, unlike the dedicated Pancake
        # adapter's three-field payload. Recognition does not authenticate code.
        try:
            limit, inner = decode(["uint160", "bytes"], extras[0], strict=True)
            require(encode(["uint160", "bytes"], [limit, inner]) == extras[0], "adapter_abi_noncanonical")
            require(limit == 0 and len(inner) == 64, "adapter_schema_unsupported")
            token_in, token_out = decode(["address", "address"], inner, strict=True)
            require(encode(["address", "address"], [token_in, token_out]) == inner, "adapter_abi_noncanonical")
        except PreflightError:
            raise
        except Exception:
            raise PreflightError("adapter_abi_invalid") from None
        terminal = WBNB if target == OKX_NATIVE else target
        expected_out = int(terminal, 16) if index == len(paths) - 1 else paths[index + 1][4]
        require(int(token_in, 16) == from_token and int(token_out, 16) == expected_out,
                "adapter_token_path_mismatch")
        require(token_in != token_out, "adapter_token_path_mismatch")
        hops.append({"pool": pool, "adapter": adapters[0], "asset_to": assets[0],
                     "input_token": "0x" + f"{from_token:040x}",
                     "output_token": token_out, "adapter_schema": "universal_v3_two_address",
                     "input_index": index, "output_index": index + 1,
                     "extra_data_bytes": len(extras[0]),
                     "extra_data_hash": "0x" + keccak(extras[0]).hex()})
    trim = None
    if trailer:
        require(len(trailer) == 64, "trailer_unsupported")
        expected, fee = (int.from_bytes(trailer[i:i+32], "big") for i in (0, 32))
        require(expected >> 208 == fee >> 208 == TRIM_FLAG, "trim_flag_invalid")
        require((expected >> 160) & ((1 << 47) - 1) == 0, "trim_reserved_bits")
        rate = (fee >> 160) & ((1 << 48) - 1)
        require(0 <= rate <= 100, "trim_rate_invalid")
        require((expected & MASK) >= base[3], "trim_expected_output_invalid")
        require(fee & MASK != 0, "trim_recipient_invalid")
        trim = {"rate_per_1000": rate, "recipient": "0x" + f"{fee & MASK:040x}",
                "expected_output_atomic": str(expected & MASK), "to_b": bool(expected & (1 << 207))}
        blockers.append("trim_recipient_unverified")
    return {"envelope_valid": True, "execution_ready": False, "selector": SELECTOR,
            "min_output_atomic": str(base[3]), "deadline": base[4], "hops": hops,
            "trim": trim, "blockers": blockers}


class ReadOnlyRpc:
    METHODS = frozenset({"eth_chainId", "eth_blockNumber", "eth_getCode", "eth_getBalance",
                         "eth_getBlockByNumber", "eth_call", "eth_estimateGas", "eth_simulateV1"})

    def __init__(self, url):
        self.url = url

    def call(self, method, params):
        require(method in self.METHODS, "rpc_method_forbidden")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        request = Request(self.url, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "Accept": "application/json",
                                   "User-Agent": "alpha-radar/1.0"}, method="POST")
        try:
            with urlopen(request, timeout=12) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise PreflightError("rpc_http_" + str(exc.code)) from None
        except Exception:
            raise PreflightError("rpc_transport_failed") from None
        if result.get("error") is not None:
            code = result["error"].get("code") if isinstance(result["error"], dict) else None
            suffix = str(code) if type(code) is int else "unknown"
            raise PreflightError("rpc_error_" + suffix, {"rpc_method": method})
        require("result" in result, "rpc_response_invalid")
        return result["result"]


def pin_block(rpc, *, now=None):
    number = rpc.call("eth_blockNumber", [])
    require(isinstance(number, str) and re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})", number),
            "block_number_invalid")
    header = rpc.call("eth_getBlockByNumber", [number, False])
    require(isinstance(header, dict) and header.get("number") == number, "block_header_invalid")
    block_hash = header.get("hash")
    require(isinstance(block_hash, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash), "block_hash_invalid")
    timestamp = uint(header.get("timestamp"))
    age = (time.time() if now is None else now) - timestamp
    require(-10 <= age <= 60, "block_not_fresh")
    return {"number": number, "hash": block_hash, "timestamp": timestamp,
            "reference": {"blockHash": block_hash, "requireCanonical": True}}


def require_canonical_block(block):
    require(isinstance(block, dict) and block.get("requireCanonical") is True
            and isinstance(block.get("blockHash"), str)
            and re.fullmatch(r"0x[0-9a-fA-F]{64}", block["blockHash"]), "canonical_block_required")


def verify_v3_adapter(rpc, adapter, block):
    require_canonical_block(block)
    require(address(adapter) == UNIVERSAL_V3_ADAPTER, "adapter_unknown")
    require(uint(rpc.call("eth_chainId", [])) == 56, "chain_mismatch")
    code = rpc.call("eth_getCode", [adapter, block])
    require(isinstance(code, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", code), "contract_code_missing")
    observed = "0x" + keccak(bytes.fromhex(code[2:])).hex()
    require(observed == UNIVERSAL_V3_RUNTIME_HASH, "adapter_runtime_mismatch")
    weth = rpc.call("eth_call", [{"to": adapter, "data": "0xad5c4648"}, block])
    require(weth.lower() == "0x" + WBNB[2:].zfill(64) if isinstance(weth, str) else False,
            "adapter_wrapped_native_mismatch")
    return {"adapter": adapter, "runtime_source_verified": True, "runtime_hash": observed,
            "source": UNIVERSAL_V3_SOURCE, "block_hash": block["blockHash"],
            "official_adapter_authorization_verified": False}


def attest_adapters(rpc, inspection, block):
    records = [verify_v3_adapter(rpc, adapter, block) for adapter in
               sorted({hop["adapter"] for hop in inspection["hops"]})]
    inspection["blockers"].remove("adapter_runtime_unverified")
    inspection["blockers"].remove("adapter_payload_unverified")
    inspection["blockers"].append("official_adapter_authorization_unverified")
    return records


def verify_v3_pool(rpc, hop, block):
    """Authenticate pool membership on BSC, not the router or its adapter."""
    require_canonical_block(block)
    require(uint(rpc.call("eth_chainId", [])) == 56, "chain_mismatch")
    pool, token_in, token_out = map(address, (hop["pool"], hop["input_token"], hop["output_token"]))
    hashes = {}
    for contract in (PANCAKE_V3_FACTORY, pool):
        code = rpc.call("eth_getCode", [contract, block])
        require(isinstance(code, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+", code),
                "contract_code_missing")
        hashes[contract] = "0x" + keccak(bytes.fromhex(code[2:])).hex()

    def read(contract, data, abi_type):
        value = rpc.call("eth_call", [{"to": contract, "data": data}, block])
        require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", value), "pool_abi_invalid")
        try:
            return decode([abi_type], bytes.fromhex(value[2:]), strict=True)[0]
        except Exception:
            raise PreflightError("pool_abi_invalid") from None

    factory = read(pool, "0xc45a0155", "address")
    require(factory == PANCAKE_V3_FACTORY, "pool_factory_mismatch")
    token0 = read(pool, "0x0dfe1681", "address")
    token1 = read(pool, "0xd21220a7", "address")
    require(token0 != token1 and {token0, token1} == {token_in, token_out}, "pool_tokens_mismatch")
    fee = read(pool, "0xddca3f43", "uint24")
    require(0 < fee < 1_000_000, "pool_fee_invalid")
    lookup = "0x" + keccak(text="getPool(address,address,uint24)")[:4].hex()
    lookup += encode(["address", "address", "uint24"], [token_in, token_out, fee]).hex()
    require(read(PANCAKE_V3_FACTORY, lookup, "address") == pool, "pool_not_registered")
    return {"verified": True, "pool": pool, "factory": factory, "token0": token0, "token1": token1,
            "fee": fee, "block_hash": block["blockHash"], "code_hashes": hashes,
            "factory_source": PANCAKE_V3_SOURCE}


def simulate(rpc, tx, block, minimum, *, quote_deadline=None):
    # Each call runs against unchanged chain state. An approval call does NOT
    # grant allowance for the subsequent swap call.
    call = {"from": tx["from"], "to": tx["to"], "data": tx["data"],
            "value": hex(uint(tx.get("value", 0)))}
    try:
        if quote_deadline is not None and time.monotonic() > quote_deadline:
            return {"status": "expired_quote", "broadcast": False}
        result = rpc.call("eth_call", [call, block])
        if quote_deadline is not None and time.monotonic() > quote_deadline:
            return {"status": "expired_quote", "broadcast": False}
        if not isinstance(result, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", result):
            return {"status": "invalid_return", "broadcast": False}
        output = int(result, 16)
        return {"status": "simulated" if output >= minimum else "below_minimum",
                "output_atomic": str(output), "broadcast": False}
    except PreflightError as exc:
        return {"status": "failed", "error": str(exc), "broadcast": False}


def run_probe(config, *, transport=None, rpc=None):
    """Public config + three API credentials only; never access DPAPI key files."""
    wallet = address(config["wallet_address"])
    env = {name: os.environ.get(name, "") for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")}
    env.update(BSC_WALLET_ADDRESS=wallet, BSC_RPC_URL=config["bsc_rpc_url"],
               OKX_LIVE_ENABLED="0", OKX_ALLOW_AUTOMATED_TRADES="0")
    transport = transport or OkxLiveTransport(env=env)
    rpc = rpc or ReadOnlyRpc(config["bsc_rpc_url"])
    require(uint(rpc.call("eth_chainId", [])) == 56, "chain_mismatch")
    market = transport.market()
    native_amount = int(market["max_buy_amount_atomic"])
    report = {"schema_version": 1, "mode": "read_only", "execution_ready": False,
              "signed": False, "broadcast": False, "round_trip_verified": False,
              "created_at": datetime.now(timezone.utc).isoformat(), "legs": [],
              "note": "Independent calls against existing state, not a sequential buy/sell fill test."}
    for label, source, target, amount in (
        ("BNB_to_USDT", OKX_NATIVE, USDT, native_amount),
        ("USDT_to_BNB", USDT, OKX_NATIVE, 5 * 10**18),
    ):
        leg = {"pair": label, "input_atomic": str(amount), "dex_id": "200"}
        report["legs"].append(leg)
        try:
            started = time.monotonic()
            params = {"chainIndex": "56", "fromTokenAddress": source, "toTokenAddress": target,
                      "amount": str(amount), "slippagePercent": "1", "dexIds": "200",
                      "userWalletAddress": wallet}
            response = transport._client()._request("/api/v6/dex/aggregator/swap", params)
            require(str(response.get("code")) == "0", "okx_build_failed")
            row = response["data"][0]
            route, tx = row["routerResult"], row["tx"]
            require(not row.get("additionalSignatureData"), "additional_signature_unsupported")
            transport._route_identity(route, source, target, amount)
            output = uint(route["toTokenAmount"])
            require(output > 0, "quote_output_invalid")
            minimum = output * 99 // 100
            leg["quote_output_atomic"] = str(output)
            leg["inspection"] = inspect_dag(tx, wallet=wallet, source=source, target=target,
                                               amount=amount, minimum=minimum,
                                               simulation_only_long_deadline=True)
            pinned = pin_block(rpc)
            block = pinned["reference"]
            leg["block"] = {k: pinned[k] for k in ("number", "hash", "timestamp")}
            if source != OKX_NATIVE:
                allowance_data = "0xdd62ed3e" + wallet[2:].zfill(64) + BSC_APPROVER[2:].zfill(64)
                allowance = uint(rpc.call("eth_call", [{"to": source, "data": allowance_data}, block]))
                leg["allowance_sufficient"] = allowance >= amount
                approval = {"from": wallet, "to": source, "value": "0",
                            "data": "0x095ea7b3" + BSC_APPROVER[2:].zfill(64) + f"{amount:064x}"}
                leg["approval_call"] = simulate(rpc, approval, block, 1)
            require(time.monotonic() - started <= 10, "quote_expired_before_simulation")
            leg["swap_call"] = simulate(rpc, tx, block, int(leg["inspection"]["min_output_atomic"]),
                                         quote_deadline=started + 10)
            leg["code_hashes"] = {}
            contracts = {BSC_ROUTER} | {h["adapter"] for h in leg["inspection"]["hops"]}
            for contract in sorted(contracts):
                code = rpc.call("eth_getCode", [contract, block])
                leg["code_hashes"][contract] = "0x" + keccak(bytes.fromhex(code[2:])).hex() if code != "0x" else None
            leg["pool_checks"] = []
            for hop in leg["inspection"]["hops"]:
                leg["pool_checks"].append(verify_v3_pool(rpc, hop, block))
            leg["inspection"]["blockers"].remove("pool_factory_unverified")
            leg["inspection"]["pool_membership_verified"] = True
            leg["adapter_checks"] = attest_adapters(rpc, leg["inspection"], block)
        except Exception as exc:
            # Do not serialize provider text, credentials, raw calldata or key paths.
            text = str(exc)
            leg["error"] = text if re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", text) else "probe_failed"
            if isinstance(exc, PreflightError) and exc.details:
                leg["diagnostics"] = exc.details
    return report


def main():
    root = Path.home() / ".config/alpha-radar"
    config = json.loads((root / "okx-live-config.json").read_text(encoding="utf-8-sig"))
    try:
        report = run_probe(config)
    except Exception as exc:
        code = str(exc)
        report = {"mode": "read_only", "execution_ready": False, "signed": False, "broadcast": False,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "error": code if re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", code) else "probe_failed"}
    destination = root / "okx-dag-preflight.json"
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    failed = "error" in report or any("error" in leg or leg.get("swap_call", {}).get("status") != "simulated"
                                      for leg in report.get("legs", []))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
