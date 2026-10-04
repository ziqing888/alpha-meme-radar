"""Offline provider/RPC fixtures; no host credentials, config, or network."""

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from io import BytesIO
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
from urllib.error import HTTPError

import pytest
from eth_account import Account
from eth_utils import keccak

import alpha_okx_live_transport as mod
from alpha_bsc_live_adapter import LocalEvmSigner


@pytest.mark.parametrize("code", ["50105", "50114", "50011", "51000"])
def test_route_exposes_only_numeric_api_error_codes(code):
    with pytest.raises(mod.OkxTransportError, match="^okx_api_" + code + "$"):
        mod.OkxLiveTransport._route({"ok": False, "error": "okx_api_" + code})


def test_route_does_not_expose_arbitrary_provider_error_text():
    with pytest.raises(mod.OkxTransportError, match="^okx_quote_or_build_failed$"):
        mod.OkxLiveTransport._route({"ok": False, "error": "okx_api_secret-value"})


@pytest.mark.parametrize("body,expected", [
    (b'{"code":"50105","msg":"private response text"}', "50105"),
    (b'<html>blocked request with private headers</html>', "http_401"),
    (b'{"code":"0"}', "http_401"),
])
def test_http_errors_keep_only_safe_status_codes(monkeypatch, body, expected):
    class Opener:
        def open(self, *args, **kwargs):
            raise HTTPError("https://web3.okx.com/fixture", 401, "private message", {}, BytesIO(body))

    monkeypatch.setattr(mod, "build_opener", lambda *args: Opener())
    result = mod._Http(lambda: 1).request("GET", "/api/v6/dex/aggregator/quote", params={}, headers={})
    assert result == {"code": expected}
    with pytest.raises(mod.OkxTransportError, match="^okx_api_" + expected + "$"):
        mod.OkxLiveTransport._route({"ok": False, "error": "okx_api_" + expected})


def test_http_identifies_json_api_client(monkeypatch):
    class Opener:
        def open(self, request, **kwargs):
            assert request.get_header("User-agent") == "alpha-radar/1.0"
            assert request.get_header("Accept") == "application/json"
            assert request.get_header("Ok-access-key") == "fixture-key"
            return BytesIO(b'{"code":"0","data":[]}')

    monkeypatch.setattr(mod, "build_opener", lambda *args: Opener())
    assert mod._Http(lambda: 1).request("GET", "/api/v6/dex/aggregator/quote", params={},
                                      headers={"OK-ACCESS-KEY": "fixture-key"})["code"] == "0"

TOKEN = "0x" + "12" * 20
POOL = "0x" + "34" * 20
OTHER = "0x" + "56" * 20
KEY = "0x" + "01" * 32  # Published deterministic test vector, never a host credential.
WALLET = Account.from_key(KEY).address.lower()
BLOCK = "0x" + "ab" * 32
BUY = 10**15
SELL = 10_000


def word(n):
    return f"{int(n, 16) if isinstance(n, str) and n.startswith('0x') else int(n):064x}"


def risk(token):
    return {"token": token, "chain": "bsc", "observed_at": datetime.now(timezone.utc).isoformat(),
            "is_honeypot": False, "cannot_sell_all": False, "buy_tax": "0.05", "sell_tax": "0.05"}


def calldata(side, amount, minimum, *, selector="0xb8815477", pool=POOL, wallet=WALLET):
    source, target = (mod.OKX_NATIVE, TOKEN) if side == "buy" else (TOKEN, mod.OKX_NATIVE)
    v3 = selector in mod._V3
    packed = int(pool, 16) | (0 if v3 else 997_500_000 << 160)
    if side == "sell":
        packed |= (1 << 255) | (1 << (253 if v3 else 254))
    if selector in mod._BASE:
        words = [42, wallet, source, target, amount, minimum, int(time.time()) + 300, 256, 1, packed]
    elif selector == "0x08298b5a":
        words = [source, amount, minimum, wallet, 160, 1, packed]
    elif selector == "0x9871efa4":
        words = [source, amount, minimum, 128, 1, packed]
    else:
        words = [wallet, amount, minimum, 128, 1, packed]
    return selector + "".join(word(w) for w in words)


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.selector = "0xb8815477"
        self.mutate_tx = lambda tx: tx
        self.mutate_approval = lambda row: row
        self.price = "1000"

    def request(self, method, path, *, params, headers, body=None):
        self.calls.append((method, path, params, headers))
        assert path.startswith("/api/v6/")
        assert "slippage" not in params
        if path.endswith("approve-transaction"):
            amount = int(params["approveAmount"])
            row = {"data": "0x095ea7b3" + word(mod.BSC_APPROVER) + word(amount),
                   "dexContractAddress": mod.BSC_APPROVER, "gasLimit": "50000", "gasPrice": "1000000000"}
            return {"code": "0", "data": [self.mutate_approval(row)]}
        amount = int(params["amount"])
        side = "buy" if params["fromTokenAddress"] == mod.OKX_NATIVE else "sell"
        output = SELL if side == "buy" else BUY
        minimum = int(Decimal(output) * (100 - Decimal(params["slippagePercent"])) / 100)
        row = {"chainIndex": "56", "swapMode": "exactIn", "fromTokenAmount": str(amount),
               "toTokenAmount": str(output),
               "fromToken": {"tokenContractAddress": params["fromTokenAddress"], "tokenUnitPrice": self.price},
               "toToken": {"tokenContractAddress": params["toTokenAddress"]}}
        if path.endswith("/swap"):
            tx = {"from": WALLET, "to": mod.BSC_ROUTER, "value": str(amount if side == "buy" else 0),
                  "data": calldata(side, amount, minimum, selector=self.selector), "gas": "1", "nonce": "999"}
            row = {"routerResult": row, "tx": self.mutate_tx(tx)}
        return {"code": "0", "data": [row]}


class RecordingSigner:
    def __init__(self):
        self.signer = LocalEvmSigner(WALLET, KEY)
        self.raw = {}
        self.signed = []

    def sign(self, tx):
        raw = self.signer.sign(tx)
        self.raw[raw] = dict(tx)
        self.signed.append(dict(tx))
        return raw

    def verify_signed_transaction(self, raw, tx):
        return self.signer.verify_signed_transaction(raw, tx)


class FakeRpc:
    def __init__(self, signer):
        self.signer = signer
        self.calls, self.sent = [], []
        self.transactions, self.receipts, self.traces = {}, {}, {}
        self.latest, self.pending, self.allowance = 0, 0, 0
        self.gas_price, self.gas_estimate = 10**9, 100_000
        self.fail_send = False
        self.chain = "0x38"
        self.trace_mode = "call"
        self.token0, self.token1 = mod.WBNB, TOKEN
        self.decimals = 6
        self.balance = 10**19
        self.finalized_height = 10
        self.finalized_supported = True
        self.finalized_hash_override = None
        self.before_send = lambda: None

    def _call(self, method, params, timeout_seconds=None):
        self.calls.append((method, params, timeout_seconds))
        assert 0 < timeout_seconds <= 10
        if method == "eth_chainId":
            value = self.chain
        elif method == "eth_getTransactionCount":
            value = hex(self.latest if params[1] == "latest" else self.pending)
        elif method == "eth_gasPrice":
            value = hex(self.gas_price)
        elif method == "eth_estimateGas":
            value = hex(50_000 if params[0]["data"].startswith("0x095ea7b3") else self.gas_estimate)
        elif method == "eth_getBalance":
            value = hex(self.balance)
        elif method == "eth_call":
            data = params[0]["data"]
            if data.startswith("0xdd62ed3e"):
                value = "0x" + word(self.allowance)
            elif data == "0x313ce567":
                value = "0x" + word(self.decimals)
            elif data == "0x0dfe1681":
                value = "0x" + word(self.token0)
            elif data == "0xd21220a7":
                value = "0x" + word(self.token1)
            elif data.startswith("0x70a08231"):
                value = "0x" + word(SELL)
            else:
                raise AssertionError(data)
        elif method == "eth_sendRawTransaction":
            self.before_send()
            raw = params[0]
            value = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
            self.sent.append(value)
            tx = self.signer.raw[raw]
            self.transactions[value] = {**tx, **{k: hex(tx[k]) for k in ("value", "nonce", "gas", "gasPrice", "chainId")},
                                        "hash": value, "input": tx["data"]}
            self.pending += 1
            if self.fail_send:
                return None
        elif method == "eth_getTransactionReceipt":
            value = self.receipts.get(params[0])
        elif method == "eth_getTransactionByHash":
            value = self.transactions.get(params[0])
        elif method == "eth_getBlockByNumber":
            if params[0] == "finalized" and not self.finalized_supported:
                return None
            height = (self.finalized_height if params[0] == "finalized" else
                      10 if params[0] == "latest" else int(params[0], 16))
            block_hash = BLOCK if height == 10 else "0x" + word(height + 1)
            if params[0] == "finalized" and self.finalized_hash_override is not None:
                block_hash = self.finalized_hash_override
            value = {"hash": block_hash, "number": hex(height), "timestamp": hex(int(time.time())),
                     "transactions": list(self.receipts) if height == 10 else []}
        elif method == "debug_traceTransaction":
            if params[1]["tracer"] == "callTracer" and self.trace_mode == "call":
                value = self.traces[params[0]]
            elif params[1]["tracer"] == "prestateTracer" and self.trace_mode == "prestate":
                tx = self.transactions[params[0]]
                delta = -BUY if int(tx["value"], 16) else BUY
                gas = int(self.receipts[params[0]]["gasUsed"], 16) * self.gas_price
                value = {"pre": {WALLET: {"balance": hex(10**19)}},
                         "post": {WALLET: {"balance": hex(10**19 + delta - gas)}}}
            else:
                return None
        else:
            raise AssertionError(method)
        return {"jsonrpc": "2.0", "id": 1, "result": value}

    def mine(self, txhash, *, success=True, native_output=BUY, token_amount=SELL, refund=0):
        tx = self.transactions[txhash]
        tx.update(blockNumber="0xa", blockHash=BLOCK)
        self.latest = self.pending
        approval = tx["input"].startswith("0x095ea7b3")
        if approval and success:
            self.allowance = int(tx["input"][-64:], 16)
        buy = int(tx["value"], 16) > 0
        self.receipts[txhash] = {
            "transactionHash": txhash, "from": WALLET, "to": tx["to"], "blockNumber": "0xa", "blockHash": BLOCK,
            "status": "0x1" if success else "0x0", "gasUsed": hex(50_000 if approval else self.gas_estimate),
            "effectiveGasPrice": hex(self.gas_price), "logs": [] if approval else [
                {"address": TOKEN, "topics": [mod.TRANSFER_TOPIC, "0x" + word(POOL if buy else WALLET),
                                               "0x" + word(WALLET if buy else POOL)],
                 "data": "0x" + word(token_amount), "removed": False, "transactionHash": txhash,
                 "blockHash": BLOCK, "logIndex": "0x0"}]}
        children = []
        if not approval and (not buy or refund):
            children = [{"type": "CALL", "from": mod.BSC_ROUTER, "to": WALLET,
                         "value": hex(refund if buy else native_output), "input": "0x"}]
        self.traces[txhash] = {"type": "CALL", "from": WALLET, "to": tx["to"], "value": tx["value"],
                               "input": tx["input"], "calls": children}


@pytest.fixture
def system(tmp_path, monkeypatch):
    # A missed injection must fail the test, never reach the network or host keys.
    monkeypatch.setattr(mod, "build_opener", lambda *a: pytest.fail("network forbidden"))
    signer = RecordingSigner()
    rpc, http = FakeRpc(signer), FakeHttp()
    env = {"BSC_WALLET_ADDRESS": WALLET, "OKX_LIVE_ENABLED": "1", "OKX_ALLOW_AUTOMATED_TRADES": "1",
           "OKX_API_KEY": "fixture-key", "OKX_SECRET_KEY": "fixture-secret", "OKX_PASSPHRASE": "fixture-pass",
           "BSC_PRIVATE_KEY": KEY, "BSC_RPC_URL": "https://fixture.invalid"}
    transport = mod.OkxLiveTransport(env, state_dir=tmp_path, rpc=rpc, http=http, signer=signer, security_reader=risk)
    transport.set_context(TOKEN, POOL)
    return transport, rpc, http, signer, env


def submit(system, side="buy", amount=None, **kwargs):
    transport = system[0]
    amount = amount if amount is not None else BUY if side == "buy" else SELL
    transport.quote(TOKEN, side, amount, "1")
    return transport.submit(TOKEN, side, amount, "1", **kwargs)


def journal(transport):
    with sqlite3.connect(transport.journal_path) as db:
        return db.execute("SELECT tx_hash, kind, status, intent FROM transactions ORDER BY nonce").fetchall()


def test_offline_constructor_never_reads_secrets_or_creates_journal(tmp_path):
    class Guard(dict):
        def get(self, key, default=None):
            assert key not in {"BSC_PRIVATE_KEY", "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "BSC_RPC_URL"}
            return super().get(key, default)

    transport = mod.OkxLiveTransport(Guard(BSC_WALLET_ADDRESS=WALLET), state_dir=tmp_path)
    assert not transport.journal_path.exists()
    assert transport.durable_prepare is True


def test_offline_preflight_validates_without_signing_network_or_journal(system):
    transport, rpc, http, signer, _ = system
    result = transport.preflight()
    assert result["offline"] and result["credentials_checked"] and result["blockers"] == []
    assert not rpc.calls and not http.calls and not signer.signed
    assert not transport.journal_path.exists()


def test_readonly_allowance_probe_never_builds_or_signs(system):
    transport, rpc, http, signer, _ = system
    rpc.allowance = 0
    result = transport.ensure_allowance(TOKEN, SELL, allow_submit=False)
    assert result == {'ready': False, 'status': 'blocked', 'error': 'approval_limit_reached'}
    assert not http.calls and not signer.signed and not rpc.sent


@pytest.mark.parametrize("key,value,code", [
    ("BSC_PRIVATE_KEY", "", "missing_private_key"),
    ("BSC_PRIVATE_KEY", "invalid", "invalid_private_key"),
    ("BSC_PRIVATE_KEY", "0x" + "02" * 32, "address_mismatch"),
    ("OKX_API_KEY", "", "missing_okx_credentials"),
    ("OKX_SECRET_KEY", "line\nbreak", "invalid_okx_credentials"),
    ("OKX_PASSPHRASE", "", "missing_okx_credentials"),
    ("BSC_RPC_URL", "file:///tmp/rpc", "invalid_rpc_url"),
    ("BSC_RPC_URL", "https://x:bad", "invalid_rpc_url"),
])
def test_preflight_raises_for_invalid_configuration(system, key, value, code):
    system[4][key] = value
    with pytest.raises(mod.OkxTransportError, match=code):
        system[0].preflight()
    assert not system[1].calls and not system[3].signed


def test_default_path():
    transport = mod.OkxLiveTransport({"BSC_WALLET_ADDRESS": WALLET})
    assert transport.journal_path == mod.Path.home() / ".config/alpha-radar/gmgn-live" / WALLET / "okx-transactions.sqlite3"


def test_v6_signature_covers_translated_path_and_params(system):
    transport, _, http, _, _ = system
    transport.quote(TOKEN, "buy", BUY, "1.5")
    transport._client().build_swap_bsc(TOKEN, str(BUY), mod.OKX_NATIVE, "1.5", WALLET)
    for method, path, params, headers in http.calls:
        target = path + "?" + urlencode(params)
        message = headers["OK-ACCESS-TIMESTAMP"] + method + target
        expected = base64.b64encode(hmac.new(b"fixture-secret", message.encode(), hashlib.sha256).digest()).decode()
        assert headers["OK-ACCESS-SIGN"] == expected
        assert params["slippagePercent"] == "1.5"
        assert params["swapMode"] == "exactIn" and params["disableRFQ"] == "true"
        assert "/v5/" not in target and "slippage=" not in target


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "0.99", "10.001", "50", -1])
def test_slippage_rejected(system, value):
    with pytest.raises(mod.OkxTransportError):
        system[0].quote(TOKEN, "buy", BUY, value)
    assert not system[1].sent


@pytest.mark.parametrize("flags", [("0", "0"), ("1", "0"), ("0", "1"), ("true", "1")])
def test_disabled_never_signs_or_sends(system, flags):
    transport, rpc, _, signer, env = system
    env.update(OKX_LIVE_ENABLED=flags[0], OKX_ALLOW_AUTOMATED_TRADES=flags[1])
    for action in (lambda: transport.submit(TOKEN, "buy", BUY, 1), lambda: transport.ensure_allowance(TOKEN, SELL)):
        with pytest.raises(mod.OkxTransportError, match="submission_disabled"):
            action()
    assert not signer.signed and not rpc.calls


def test_verify_connection_with_flags_off_and_no_private_key(system):
    transport, rpc, _, signer, env = system
    result = submit(system)
    rpc.mine(result["order_id"])
    signed_before = len(signer.signed)
    env.update(OKX_LIVE_ENABLED="0", OKX_ALLOW_AUTOMATED_TRADES="0")
    env.pop("BSC_PRIVATE_KEY")
    result = transport.verify_connection()
    assert result["ok"] and result["ready"] and result["read_only"] and not result["signer_checked"]
    assert result["trace"]["source"] == "callTracer"
    assert result["balance"]["amount_atomic"] == str(rpc.balance)
    assert len(signer.signed) == signed_before and len(rpc.sent) == 1
    assert transport.balance(TOKEN)["decimals"] == 6


def test_verify_connection_requires_trace_rpc(system):
    transport, rpc, _, _, env = system
    result = submit(system)
    rpc.mine(result["order_id"])
    env.update(OKX_LIVE_ENABLED="0", OKX_ALLOW_AUTOMATED_TRADES="0")
    rpc.trace_mode = "unavailable"
    with pytest.raises(mod.OkxTransportError, match="trace_rpc_required"):
        transport.verify_connection()
    rpc.trace_mode = "prestate"
    assert transport.verify_connection()["trace"]["source"] == "prestateTracer"
    assert len(rpc.sent) == 1


def test_security_original_gate_and_no_gmgn(system):
    transport = system[0]
    assert transport.security(TOKEN)["safe"] is True
    for update in ({"cannot_sell_all": None}, {"cannot_sell_all": True}, {"is_honeypot": True},
                   {"sell_tax": "0.1"}, {"buy_tax": ""}, {"token": OTHER}, {"chain": "eth"},
                   {"observed_at": "2020-01-01T00:00:00+00:00"}, {"is_honeypot": "false"}):
        transport._security_reader = lambda token: {**risk(token), **update}
        assert transport.security(TOKEN)["safe"] is False


def test_default_goplus_provider_uses_fresh_token_bound_response(system, monkeypatch):
    transport = system[0]
    transport._security_reader = None
    urls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"code": 1, "result": {TOKEN: {"is_honeypot": "0", "cannot_sell_all": "0",
                                                              "buy_tax": "0.01", "sell_tax": "0.02"}}}).encode()

    class Opener:
        def open(self, request, timeout):
            urls.append(request.full_url)
            assert not request.has_header("Authorization") and 0 < timeout <= 10
            return Response()

    monkeypatch.setattr(mod, "build_opener", lambda *args: Opener())
    assert transport.security(TOKEN)["safe"]
    assert urls == ["https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=" + TOKEN]


@pytest.mark.parametrize("selector", sorted(mod._SELECTORS))
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_supported_actual_abi_forms_and_normalized_receipt(system, selector, side):
    transport, rpc, http, signer, _ = system
    http.selector = selector
    rpc.allowance = SELL
    result = submit(system, side)
    txhash = result["order_id"]
    assert result["status"] == "pending" and txhash == rpc.sent[0]
    rpc.mine(txhash)
    receipt = transport.order(txhash)
    assert receipt["status"] == "confirmed" and receipt["fill_complete"] and receipt["native_amount_proven"]
    assert receipt["wallet_address"] == WALLET
    assert receipt["input_token"] == (mod.NATIVE if side == "buy" else TOKEN)
    assert receipt["output_token"] == (TOKEN if side == "buy" else mod.NATIVE)
    assert receipt["input_amount"] == str(BUY if side == "buy" else SELL)
    assert receipt["output_amount"] == str(SELL if side == "buy" else BUY)
    assert receipt["gas_usd"] == "0.1000"
    assert signer.signed[0]["nonce"] == 0  # Never trust upstream nonce/gas.
    assert signer.signed[0]["gas"] == 120_000
    assert transport.ready_for_entries()


def test_commit_and_callback_precede_broadcast(system):
    transport, rpc, _, _, _ = system
    callbacks = []

    def prepared(txhash):
        rows = journal(transport)
        assert rows[0][0] == txhash and rows[0][2] == "prepared"
        assert not rpc.sent
        callbacks.append(txhash)

    rpc.before_send = lambda: bool(callbacks) or pytest.fail("callback missing")
    result = submit(system, on_prepared=prepared)
    assert callbacks == [result["order_id"]]
    assert journal(transport)[0][2] == "pending"
    assert not transport.ready_for_entries()


def test_unknown_broadcast_is_durable_and_never_retried(system):
    transport, rpc, http, signer, env = system
    rpc.fail_send = True
    with pytest.raises(mod.OkxTransportError) as error:
        submit(system)
    assert error.value.ambiguous and error.value.order_id == rpc.sent[0]
    assert journal(transport)[0][2] == "unknown"
    restarted = mod.OkxLiveTransport(env, state_dir=transport.state_dir, rpc=rpc, http=http, signer=signer, security_reader=risk)
    restarted.set_context(TOKEN, POOL)
    assert not restarted.ready_for_entries()
    with pytest.raises(mod.OkxTransportError, match="journal_unresolved"):
        restarted.submit(TOKEN, "buy", BUY, 1)
    assert len(rpc.sent) == 1
    rpc.mine(error.value.order_id)
    assert restarted.order(error.value.order_id)["status"] == "confirmed"


def test_callback_crash_leaves_prepared_hash_before_any_send(system):
    transport, rpc, _, _, _ = system

    def crash(txhash):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        submit(system, on_prepared=crash)
    assert journal(transport)[0][2] == "prepared" and not rpc.sent
    assert not transport.ready_for_entries()
    with pytest.raises(mod.OkxTransportError, match="journal_unresolved"):
        submit(system)


def test_callback_exception_is_cancelled_without_secret_leak(system):
    def crash(txhash):
        raise RuntimeError("sensitive fixture string")

    with pytest.raises(mod.OkxTransportError) as error:
        submit(system, on_prepared=crash)
    assert not error.value.ambiguous and error.value.order_id
    assert "sensitive" not in str(error.value)
    assert not system[1].sent
    receipt = system[0].order(error.value.order_id)
    assert receipt["status"] == "rejected" and receipt["broadcast_attempted"] is False


def test_pending_external_nonce_blocks_before_signing(system):
    transport, rpc, _, signer, _ = system
    rpc.pending = 1
    assert not transport.ready_for_entries()
    with pytest.raises(mod.OkxTransportError, match="wallet_nonce_pending"):
        submit(system)
    assert not signer.signed


def test_deadline_expiry_before_and_after_callback(system):
    transport, rpc, _, signer, _ = system
    with pytest.raises(mod.OkxTransportError, match="deadline_expired"):
        submit(system, deadline=time.monotonic() - 1)
    assert not signer.signed
    with pytest.raises(mod.OkxTransportError) as error:
        submit(system, on_prepared=lambda _: setattr(transport, "_deadline", 0))
    assert not error.value.ambiguous and not rpc.sent
    assert transport.order(error.value.order_id)["status"] == "rejected"


@pytest.mark.parametrize("mutation", [
    lambda tx: {**tx, "to": OTHER},
    lambda tx: {**tx, "from": OTHER},
    lambda tx: {**tx, "chainId": 1},
    lambda tx: {**tx, "value": "0"},
    lambda tx: {**tx, "data": "0xdeadbeef" + tx["data"][10:]},
    lambda tx: {**tx, "data": calldata("buy", BUY, 9900, pool=OTHER)},
    lambda tx: {**tx, "data": calldata("buy", BUY, 9900, wallet=OTHER)},
    lambda tx: {**tx, "data": calldata("buy", BUY + 1, 9900)},
    lambda tx: {**tx, "data": calldata("buy", BUY, 9899)},
    lambda tx: {**tx, "data": tx["data"] + word(POOL)},
    lambda tx: {**tx, "signatureData": ["permit"]},
])
def test_untrusted_transaction_rejected_before_signing(system, mutation):
    _, rpc, http, signer, _ = system
    http.mutate_tx = mutation
    with pytest.raises(mod.OkxTransportError):
        submit(system)
    assert not signer.signed and not rpc.sent


def test_pool_substring_in_order_id_is_not_binding(system):
    def malicious(tx):
        data = calldata("buy", BUY, 9900, pool=OTHER)
        return {**tx, "data": data[:10] + word(POOL) + data[74:]}

    system[2].mutate_tx = malicious
    with pytest.raises(mod.OkxTransportError, match="pool_binding"):
        submit(system)
    assert not system[3].signed


def test_pool_direction_and_native_unwrap_checked(system):
    system[1].token0, system[1].token1 = TOKEN, mod.WBNB
    with pytest.raises(mod.OkxTransportError, match="pool_token_direction"):
        submit(system)


@pytest.mark.parametrize("amount", [5 * BUY + 1, 6 * BUY])
def test_buy_budget(system, amount):
    with pytest.raises(mod.OkxTransportError, match="buy_exceeds_5_usd"):
        submit(system, amount=amount)
    assert not system[3].signed


def test_gas_cap_uses_rpc_estimate_not_api_gas(system):
    system[1].gas_estimate = 500_000
    with pytest.raises(mod.OkxTransportError, match="gas_exceeds"):
        submit(system)
    assert not system[3].signed


def test_approval_preparation_is_separate_and_durable(system):
    transport, rpc, _, signer, _ = system
    with pytest.raises(mod.OkxTransportError, match="allowance_not_ready"):
        submit(system, "sell")
    assert not signer.signed
    first = transport.ensure_allowance(TOKEN, SELL)
    txhash = first["approval_tx_hash"]
    assert first["ready"] is False and "order_id" not in first
    assert journal(transport)[0][1] == "approval"
    assert signer.signed[0]["data"] == "0x095ea7b3" + word(mod.BSC_APPROVER) + word(SELL)
    assert transport.ensure_allowance(TOKEN, SELL)["ready"] is False
    assert len(rpc.sent) == 1 and not transport.fees()
    rpc.mine(txhash)
    assert transport.ensure_allowance(TOKEN, SELL)["ready"] is True
    receipt = transport.order(txhash)
    assert receipt["status"] == "pending" and receipt["approval_confirmed"]
    assert not receipt["fill_complete"] and receipt["output_amount"] is None
    assert transport.fees() == [{"id": "okx-approval:" + txhash, "gas_usd": "0.05000"}]
    assert transport.fees() == transport.fees()
    result = submit(system, "sell")
    rpc.mine(result["order_id"])
    assert transport.order(result["order_id"])["status"] == "confirmed"
    assert len(signer.signed) == 2


def test_allowance_zero_reset_then_exact_amount(system):
    transport, rpc, _, signer, _ = system
    rpc.allowance = 1
    reset = transport.ensure_allowance(TOKEN, SELL)
    assert reset["approve_amount_atomic"] == "0"
    rpc.mine(reset["approval_tx_hash"])
    approval = transport.ensure_allowance(TOKEN, SELL)
    assert approval["approve_amount_atomic"] == str(SELL)
    rpc.mine(approval["approval_tx_hash"])
    assert transport.ensure_allowance(TOKEN, SELL)["ready"]
    assert len(transport.fees()) == 2 and len(signer.signed) == 2


def test_approval_unknown_never_resent_or_returned_as_swap(system):
    transport, rpc, _, _, _ = system
    rpc.fail_send = True
    result = transport.ensure_allowance(TOKEN, SELL)
    assert result["status"] == "unknown" and not result["ready"] and "order_id" not in result
    assert not transport.ensure_allowance(TOKEN, SELL)["ready"]
    assert len(rpc.sent) == 1


def test_approval_fee_in_gas_budget_without_double_count_in_receipt(system):
    transport, rpc, _, _, _ = system
    approval = transport.ensure_allowance(TOKEN, SELL)
    rpc.mine(approval["approval_tx_hash"])
    assert transport.ensure_allowance(TOKEN, SELL)["ready"]
    rpc.gas_estimate = 400_000  # .48 bounded swap gas + .05 approval > .50.
    with pytest.raises(mod.OkxTransportError, match="gas_exceeds"):
        submit(system, "sell")
    assert len(rpc.sent) == 1


@pytest.mark.parametrize("mutation", [
    lambda row: {**row, "dexContractAddress": OTHER},
    lambda row: {**row, "data": "0x095ea7b3" + word(OTHER) + word(SELL)},
    lambda row: {**row, "data": "0x095ea7b3" + word(mod.BSC_APPROVER) + word(2**256 - 1)},
])
def test_approval_spender_and_exact_amount_validation(system, mutation):
    system[2].mutate_approval = mutation
    with pytest.raises(mod.OkxTransportError, match="approval_calldata"):
        system[0].ensure_allowance(TOKEN, SELL)
    assert not system[3].signed


def test_native_fill_is_actual_trace_not_quote_and_refunds_are_net(system):
    transport, rpc, _, _, _ = system
    result = submit(system)
    rpc.mine(result["order_id"], token_amount=12_000, refund=BUY // 10)
    receipt = transport.order(result["order_id"])
    assert receipt["input_amount"] == str(BUY * 9 // 10)
    assert receipt["output_amount"] == "12000"
    assert receipt["native_amount_source"] == "callTracer"


def test_below_minimum_fill_still_reports_actual_position_with_anomaly(system):
    transport, rpc, _, _, _ = system
    result = submit(system)
    rpc.mine(result["order_id"], token_amount=9000)
    receipt = transport.order(result["order_id"])
    assert receipt["status"] == "confirmed" and receipt["fill_complete"]
    assert receipt["output_amount"] == "9000" and receipt["fill_anomalies"] == ["output_below_minimum"]


def test_native_sell_proof_required_with_prestate_fallback(system):
    transport, rpc, _, _, _ = system
    rpc.allowance = SELL
    result = submit(system, "sell")
    rpc.mine(result["order_id"], native_output=BUY * 2)
    assert transport.order(result["order_id"])["output_amount"] == str(BUY * 2)
    rpc.trace_mode = "unavailable"
    receipt = transport.order(result["order_id"])
    assert receipt["status"] == "pending" and not receipt["fill_complete"]
    assert receipt["output_amount"] is None and receipt["error"] == "native_amount_unproven"
    rpc.trace_mode = "prestate"
    receipt = transport.order(result["order_id"])
    assert receipt["output_amount"] == str(BUY) and receipt["native_amount_source"] == "prestateTracer"


def test_reverted_trace_descendants_and_delegatecall_value_do_not_count(system):
    transport, rpc, _, _, _ = system
    rpc.allowance = SELL
    result = submit(system, "sell")
    rpc.mine(result["order_id"])
    trace = rpc.traces[result["order_id"]]
    trace["calls"] += [
        {"type": "DELEGATECALL", "from": OTHER, "to": WALLET, "value": hex(BUY), "calls": []},
        {"type": "CALL", "from": OTHER, "to": WALLET, "value": hex(BUY), "error": "execution reverted",
         "calls": [{"type": "CALL", "from": OTHER, "to": WALLET, "value": hex(BUY)}]}]
    assert transport.order(result["order_id"])["output_amount"] == str(BUY)


@pytest.mark.parametrize("field,value", [("from", OTHER), ("blockHash", "0x" + "00" * 32),
                                         ("transactionHash", "0x" + "ff" * 32), ("status", "0x2")])
def test_receipt_identity_rejected(system, field, value):
    transport, rpc, _, _, _ = system
    result = submit(system)
    rpc.mine(result["order_id"])
    rpc.receipts[result["order_id"]][field] = value
    receipt = transport.order(result["order_id"])
    assert receipt["status"] == "pending" and not receipt["fill_complete"]


def test_reverted_swap_and_approval_fees(system):
    transport, rpc, _, _, _ = system
    approval = transport.ensure_allowance(TOKEN, SELL)
    rpc.mine(approval["approval_tx_hash"], success=False)
    assert transport.order(approval["approval_tx_hash"])["status"] == "failed"
    assert len(transport.fees()) == 1
    result = submit(system)
    rpc.mine(result["order_id"], success=False)
    receipt = transport.order(result["order_id"])
    assert receipt["status"] == "failed" and not receipt["fill_complete"]
    assert receipt["gas_usd"] is not None


def test_missing_pool_context_and_changed_context_expire_quote(system):
    transport = system[0]
    with pytest.raises(mod.OkxTransportError, match="missing_pool_context"):
        transport.quote(OTHER, "buy", BUY, 1)
    transport.quote(TOKEN, "buy", BUY, 1)
    assert transport._quotes
    transport.set_context(TOKEN, OTHER)
    assert not transport._quotes


def test_journal_contains_no_raw_signature_or_credentials(system):
    transport, _, _, signer, _ = system
    submit(system)
    data = transport.journal_path.read_bytes()
    assert b"fixture-secret" not in data and b"fixture-pass" not in data and KEY.encode() not in data
    for raw in signer.raw:
        assert raw.encode() not in data


def test_other_transport_sees_prepared_reservation(system):
    transport, rpc, http, signer, env = system
    peer = mod.OkxLiveTransport(env, state_dir=transport.state_dir, rpc=rpc, http=http, signer=signer, security_reader=risk)
    peer.set_context(TOKEN, POOL)

    def prepared(txhash):
        assert not peer.ready_for_entries()
        with pytest.raises(mod.OkxTransportError, match="journal_unresolved"):
            peer.submit(TOKEN, "buy", BUY, 1)

    submit(system, on_prepared=prepared)
    assert len(rpc.sent) == 1


@pytest.mark.parametrize("field", ["is_honeypot", "cannot_sell_all", "buy_tax", "sell_tax"])
def test_missing_required_security_evidence_blocks_entry(system, field):
    transport = system[0]
    transport._security_reader = lambda token: {k: v for k, v in risk(token).items() if k != field}
    assert transport.security(TOKEN)["safe"] is False
    with pytest.raises(mod.OkxTransportError, match="security_not_passed"):
        submit(system)
    assert not system[3].signed


@pytest.mark.parametrize("marker", ["truncated", "timeout", "failed"])
def test_incomplete_trace_never_used_as_native_proof(system, marker):
    transport, rpc, _, _, _ = system
    result = submit(system)
    rpc.mine(result["order_id"])
    rpc.traces[result["order_id"]][marker] = True
    receipt = transport.order(result["order_id"])
    assert receipt["status"] == "pending" and not receipt["native_amount_proven"]
    assert not transport.ready_for_entries()


def test_reorg_missing_receipt_revokes_journal_completion(system):
    transport, rpc, _, _, _ = system
    result = submit(system)
    txhash = result["order_id"]
    rpc.mine(txhash)
    assert transport.order(txhash)["status"] == "confirmed"
    assert journal(transport)[0][2] == "confirmed"
    rpc.receipts.pop(txhash)
    assert transport.order(txhash)["status"] == "pending"
    assert journal(transport)[0][2] == "unknown"
    assert not transport.ready_for_entries()


def test_nonce_changes_in_worker_callback_abort_before_send(system):
    transport, rpc, _, _, _ = system
    with pytest.raises(mod.OkxTransportError) as error:
        submit(system, on_prepared=lambda _: setattr(rpc, "pending", 1))
    assert not error.value.ambiguous and error.value.order_id
    assert not rpc.sent and journal(transport)[0][2] == "cancelled"


def test_quote_deadline_cannot_be_extended_by_submit(system):
    transport = system[0]
    clock = [1.0]
    transport._clock = lambda: clock[0]
    transport.quote(TOKEN, "buy", BUY, 1)
    clock[0] = 12
    with pytest.raises(mod.OkxTransportError, match="submission_deadline_expired"):
        transport.submit(TOKEN, "buy", BUY, 1, deadline=100)
    assert not system[3].signed


def test_pool_offset_and_unwrap_corruption_fail_before_signing(system):
    def corrupt(tx):
        data = tx["data"]
        start = 10 + 7 * 64
        return {**tx, "data": data[:start] + word(0) + data[start+64:]}

    system[2].mutate_tx = corrupt
    with pytest.raises(mod.OkxTransportError, match="pool_unverifiable"):
        submit(system)
    system[2].mutate_tx = lambda tx: {**tx, "data": tx["data"][:-64] + word(int(tx["data"][-64:], 16) | (1 << 254))}
    with pytest.raises(mod.OkxTransportError, match="native_unwrap_mismatch"):
        submit(system)
    assert not system[3].signed


def test_wrong_chain_blocks_reads_and_submission(system):
    system[1].chain = "0x1"
    assert not system[0].ready_for_entries()
    for action in (system[0].verify_connection, lambda: submit(system)):
        with pytest.raises(mod.OkxTransportError, match="rpc_wrong_chain"):
            action()
    assert not system[3].signed


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("success", [True, False])
def test_mined_swap_remains_pending_until_rpc_finality(system, side, success):
    transport, rpc, _, _, _ = system
    rpc.allowance = SELL
    result = submit(system, side)
    txhash = result["order_id"]
    rpc.mine(txhash, success=success)
    rpc.finalized_height = 9
    pending = transport.order(txhash)
    assert pending["status"] == "pending" and not pending["fill_complete"]
    assert pending["error"] == "awaiting_finality" and pending["finalized"] is False
    assert pending["input_amount"] is None and pending["gas_usd"] is None
    assert journal(transport)[0][2] == "pending"
    assert not transport.ready_for_entries()
    rpc.finalized_height = 10
    settled = transport.order(txhash)
    assert settled["status"] == ("confirmed" if success else "failed")
    assert settled["finalized"] is True
    assert settled["finality"]["block_hash"] == BLOCK
    assert settled["gas_usd"] is not None


@pytest.mark.parametrize("success", [True, False])
def test_approval_fees_and_allowance_wait_for_finality(system, success):
    transport, rpc, _, _, _ = system
    approval = transport.ensure_allowance(TOKEN, SELL)
    txhash = approval["approval_tx_hash"]
    rpc.mine(txhash, success=success)
    rpc.finalized_height = 9
    assert transport.fees() == []
    assert transport.ensure_allowance(TOKEN, SELL)["ready"] is False
    assert len(rpc.sent) == 1
    rpc.finalized_height = 10
    assert transport.fees() == [{"id": "okx-approval:" + txhash, "gas_usd": "0.05000"}]
    if success:
        assert transport.ensure_allowance(TOKEN, SELL)["ready"] is True
    assert len(rpc.sent) == 1


def test_readiness_requires_supported_consistent_finalized_tag(system):
    transport, rpc, _, signer, env = system
    result = submit(system)
    rpc.mine(result["order_id"])
    env.update(OKX_LIVE_ENABLED="0", OKX_ALLOW_AUTOMATED_TRADES="0")
    rpc.finalized_supported = False
    with pytest.raises(mod.OkxTransportError, match="finalized_rpc_required"):
        transport.verify_connection()
    assert not transport.ready_for_entries()
    assert transport.order(result["order_id"])["status"] == "pending"
    rpc.finalized_supported = True
    rpc.finalized_hash_override = "0x" + "11" * 32
    with pytest.raises(mod.OkxTransportError, match="finalized_rpc_required"):
        transport.verify_connection()
    rpc.finalized_hash_override = None
    ready = transport.verify_connection()
    assert ready["ready"] and ready["finality"]["source"] == "rpc_finalized_tag"
    assert len(signer.signed) == len(rpc.sent) == 1


@pytest.mark.parametrize("bad_block", [None, {}, {"number": "pending", "hash": BLOCK},
                                       {"number": "0xa", "hash": "0x0"},
                                       {"number": "0xa", "hash": "0x" + "00" * 32}])
def test_malformed_finalized_headers_never_complete_receipt(system, bad_block):
    transport, rpc, _, _, _ = system
    result = submit(system)
    rpc.mine(result["order_id"])
    original = rpc._call

    def call(method, params, timeout_seconds=None):
        if method == "eth_getBlockByNumber" and params[0] == "finalized":
            return {"result": bad_block}
        return original(method, params, timeout_seconds=timeout_seconds)

    rpc._call = call
    assert transport.order(result["order_id"])["status"] == "pending"
    with pytest.raises(mod.OkxTransportError, match="finalized_rpc_required"):
        transport.verify_connection()


def test_unfinalized_orphan_sell_never_credited(system):
    transport, rpc, _, _, _ = system
    rpc.allowance = SELL
    result = submit(system, "sell")
    rpc.mine(result["order_id"])
    rpc.finalized_height = 9
    assert transport.order(result["order_id"])["status"] == "pending"
    rpc.receipts.pop(result["order_id"])
    rpc.finalized_height = 10
    receipt = transport.order(result["order_id"])
    assert receipt["status"] == "pending" and not receipt["fill_complete"]
    assert receipt["output_amount"] is None and not transport.ready_for_entries()


def test_canonical_receipt_block_rechecked_after_finalized_checkpoint(system):
    transport, rpc, _, _, _ = system
    result = submit(system)
    rpc.mine(result["order_id"])
    rpc.finalized_height = 11
    original = rpc._call
    saw_finalized = []

    def call(method, params, timeout_seconds=None):
        response = original(method, params, timeout_seconds=timeout_seconds)
        if method == "eth_getBlockByNumber" and params[0] == "finalized":
            saw_finalized.append(True)
        if method == "eth_getBlockByNumber" and params[0] == "0xa" and saw_finalized:
            response["result"]["hash"] = "0x" + "ff" * 32
        return response

    rpc._call = call
    receipt = transport.order(result["order_id"])
    assert receipt["status"] == "pending" and not receipt["fill_complete"]
    assert journal(transport)[0][2] == "pending"


@pytest.mark.parametrize("kind", ["buy", "sell", "approval"])
def test_no_sign_or_broadcast_without_finalized_tag_even_without_startup_verify(system, kind):
    transport, rpc, _, signer, _ = system
    rpc.finalized_supported = False
    rpc.allowance = SELL if kind == "sell" else 0
    with pytest.raises(mod.OkxTransportError, match="finalized_rpc_required"):
        if kind == "approval":
            transport.ensure_allowance(TOKEN, SELL)
        else:
            submit(system, kind)
    assert not signer.signed and not rpc.sent and journal(transport) == []


def test_finality_support_lost_after_prepared_callback_blocks_broadcast(system):
    transport, rpc, _, _, _ = system
    with pytest.raises(mod.OkxTransportError) as error:
        submit(system, on_prepared=lambda _: setattr(rpc, "finalized_supported", False))
    assert not error.value.ambiguous and error.value.order_id
    assert journal(transport)[0][2] == "cancelled" and not rpc.sent
    receipt = transport.order(error.value.order_id)
    assert receipt["status"] == "rejected" and receipt["broadcast_attempted"] is False
    assert receipt["gas_usd"] == receipt["gas_native"] == "0"
    assert receipt["wallet_address"] == WALLET and receipt["chain"] == "bsc"


def test_presend_cancellation_releases_same_nonce_without_reusing_rejected_hash(system):
    transport, rpc, http, signer, env = system
    fixed = calldata("buy", BUY, 9900)
    http.mutate_tx = lambda tx: {**tx, "data": fixed}
    with pytest.raises(mod.OkxTransportError) as error:
        submit(system, on_prepared=lambda _: setattr(rpc, "finalized_supported", False))
    cancelled_hash = error.value.order_id
    rpc.finalized_supported = True
    restarted = mod.OkxLiveTransport(env, state_dir=transport.state_dir, rpc=rpc, http=http, signer=signer, security_reader=risk)
    restarted.set_context(TOKEN, POOL)
    assert restarted.ready_for_entries()
    result = restarted.submit(TOKEN, "buy", BUY, 1)
    assert result["order_id"] != cancelled_hash
    assert [tx["nonce"] for tx in signer.signed] == [0, 0]
    assert signer.signed[1]["gas"] == signer.signed[0]["gas"] + 1
    assert rpc.sent == [result["order_id"]]
    assert restarted.order(cancelled_hash)["status"] == "rejected"
    with sqlite3.connect(transport.journal_path) as db:
        rows = db.execute("SELECT status,send_attempted FROM transactions ORDER BY rowid").fetchall()
    assert rows == [("cancelled", 0), ("pending", 1)]
    rpc.mine(result["order_id"])
    assert restarted.order(result["order_id"])["status"] == "confirmed"


def test_send_marker_committed_before_rpc_invocation_and_unknown_stays_reserved(system):
    transport, rpc, _, _, _ = system

    def before_send():
        with sqlite3.connect(transport.journal_path) as db:
            assert db.execute("SELECT status,send_attempted FROM transactions").fetchone() == ("prepared", 1)

    rpc.before_send = before_send
    rpc.fail_send = True
    with pytest.raises(mod.OkxTransportError) as error:
        submit(system)
    assert error.value.ambiguous
    assert transport.order(error.value.order_id)["broadcast_attempted"] is True
    # Even a recovered node reporting an empty pending pool is not proof of no send.
    rpc.fail_send = False
    rpc.pending = rpc.latest = 0
    with pytest.raises(mod.OkxTransportError, match="journal_unresolved"):
        submit(system)
    assert len(rpc.sent) == 1 and journal(transport)[0][2] == "unknown"


def test_rpc_send_exception_never_releases_reservation(system):
    transport, rpc, _, _, _ = system

    def fail_at_rpc():
        raise RuntimeError("rpc connection failed")

    rpc.before_send = fail_at_rpc
    with pytest.raises(mod.OkxTransportError) as error:
        submit(system)
    assert error.value.ambiguous
    with sqlite3.connect(transport.journal_path) as db:
        assert db.execute("SELECT status,send_attempted FROM transactions").fetchone() == ("unknown", 1)
    assert not transport.ready_for_entries()


def test_cancelled_approval_has_no_fees_and_retries_with_same_nonce(system):
    transport, rpc, _, signer, _ = system
    original = transport._finalized_block
    calls = []

    def probe():
        calls.append(True)
        if len(calls) == 2:
            raise mod.OkxTransportError("finalized_rpc_required")
        return original()

    transport._finalized_block = probe
    result = transport.ensure_allowance(TOKEN, SELL)
    assert result["status"] == "rejected" and not result["ready"]
    assert not rpc.sent and transport.fees() == []
    assert transport.order(result["approval_tx_hash"])["broadcast_attempted"] is False
    transport._finalized_block = original
    retry = transport.ensure_allowance(TOKEN, SELL)
    assert retry["approval_tx_hash"] != result["approval_tx_hash"]
    assert [tx["nonce"] for tx in signer.signed] == [0, 0]
    rpc.mine(retry["approval_tx_hash"])
    assert transport.fees() == [{"id": "okx-approval:" + retry["approval_tx_hash"], "gas_usd": "0.05000"}]


def test_cancelled_hash_disambiguation_keeps_gas_cap(system):
    transport, rpc, _, signer, _ = system
    rpc.gas_estimate = 416666  # Rounded gas limit reaches the exact $0.50 cap.
    with pytest.raises(mod.OkxTransportError, match="cancelled_before_send"):
        submit(system, on_prepared=lambda _: setattr(rpc, "finalized_supported", False))
    rpc.finalized_supported = True
    with pytest.raises(mod.OkxTransportError, match="gas_exceeds"):
        submit(system)
    assert len(signer.signed) == 1 and not rpc.sent


def test_legacy_unique_nonce_schema_migrates_preserving_unknown_reservation(system):
    transport, _, _, _, _ = system
    txhash = "0x" + "fe" * 32
    with sqlite3.connect(transport.journal_path) as db:
        db.execute("CREATE TABLE transactions (tx_hash TEXT PRIMARY KEY, wallet TEXT NOT NULL, "
                   "kind TEXT NOT NULL, status TEXT NOT NULL, nonce INTEGER NOT NULL UNIQUE, "
                   "intent TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?)",
                   (txhash, WALLET, "swap", "unknown", 0, '{"token":"' + TOKEN + '"}', "2026-09-08T00:00:00+00:00"))
    assert not transport.ready_for_entries()
    with sqlite3.connect(transport.journal_path) as db:
        row = db.execute("SELECT tx_hash,status,nonce,send_attempted FROM transactions").fetchone()
        index_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='transactions_reserved_nonce'").fetchone()[0]
    assert row == (txhash, "unknown", 0, 1)
    assert "WHERE status != 'cancelled'" in index_sql
    with pytest.raises(mod.OkxTransportError, match="journal_unresolved"):
        submit(system)
