"""Offline-configurable OKX v6 / BSC transport for GmgnLiveWorker.

Construction never contacts providers, loads dotenv, or initializes a signer.
preflight checks supplied credentials/key identity offline, without signing.
Runtime credentials are read lazily from the supplied env mapping
(os.environ when omitted). No GMGN client or credentials are used.

Integration: set_context(token, pool) before quote/submit. on_prepared(hash)
is called for SWAPS only, after a FULL-synchronous SQLite commit and before
broadcast. Call ensure_allowance(token, amount) BEFORE creating a sell intent.
Approval hashes live only in this journal; ready=False means defer that sell.
Repeated ensure_allowance calls reconcile but never rebroadcast an approval.
submit never creates approvals or returns their hashes as swap order IDs.
Uncertain broadcasts, crashes after preparation, and unproven fills block new
submissions. There is no transaction replacement or broadcast retry.
Handled failures before send invocation are durably cancelled; order() returns
rejected with zero gas, and the nonce can be used by a new intent. A durable
send_attempted marker prevents releasing any possibly broadcast transaction.

security() defaults to a fresh public GoPlus BSC token_security request.
It requires known is_honeypot=false, cannot_sell_all=false and known buy/sell
tax ratios below 0.1, matching alpha_onchain's high-tax boundary. Known
cannot_buy/cannot_sell=true also reject. No rug-ratio metric is required.
security_reader(token) can override the reader with token, chain='bsc',
observed_at (UTC ISO), those flags (bool or '0'/'1'), and tax ratios.
Evidence expires in 60s. Missing required fields fail closed.

Legacy selectors support canonical, single-pool V2/V3 calldata. The separately
opted-in DAG gate supports bounded universal V3 paths with pinned code/factory
and endpoint pool binding. Trim fees require a separate explicit policy.
SmartSwap, PMM, permit and unknown selectors fail closed.
The allowlists/ABI were checked against these official sources on 2026-09-08:
https://web3.okx.com/onchainos/dev-docs/trade/dex-smart-contract
https://web3.okx.com/onchainos/dev-docs/trade/dex-swap
https://web3.okx.com/onchainos/dev-docs/trade/dex-approve-transaction
https://github.com/okxlabs/DEX-Router-EVM-V1/tree/main/contracts/8

state_dir is the wallet directory, not its parent. Default journal:
~/.config/alpha-radar/gmgn-live/<wallet>/okx-transactions.sqlite3.
order() performs reads only on-chain; gas_usd is an estimate at preparation
price. Terminal receipts and approval fees require the RPC finalized block
tag, checked against canonical block numbers/hashes. Inclusion alone remains
pending; unsupported finality fails readiness, with no head-depth fallback.
Native settlement requires callTracer or prestateTracer diff evidence;
quote output is NEVER used as a fill. No live verification is implied.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from alpha_bsc_live_adapter import (
    AdapterError, JsonRpcBroadcaster, LocalEvmSigner,
    _validate_okx_dynamic_calldata, _validate_transaction_semantics,
)
from alpha_okx_swap import HOST, OkxSwapClient, _min_out

NATIVE = "0x" + "0" * 40
OKX_NATIVE = "0x" + "e" * 40
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
BSC_ROUTER = "0x5994814f2c4040b863a0125a45de152a8c2a4dec"
BSC_APPROVER = "0x2c34a2fb1d0b4f55de51e1d0bdefaddce6b7cdd6"
NATIVE_DECIMALS = 18
MAX_BUY_USD = Decimal("5")
MAX_GAS_USD = Decimal("0.5")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_MASK = (1 << 160) - 1
_V3 = {"0x44014e98", "0x0d5f0e3b"}
_BASE = {"0x44014e98", "0xb8815477"}
_SELECTORS = _BASE | {"0x0d5f0e3b", "0x08298b5a", "0x9871efa4"}


class OkxTransportError(RuntimeError):
    def __init__(self, code, *, ambiguous=False, order_id=None, approval_tx_hash=None):
        super().__init__(code)
        self.code = code
        self.ambiguous = ambiguous
        self.order_id = order_id
        self.approval_tx_hash = approval_tx_hash


def _address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise OkxTransportError("invalid_address")
    return value.lower()


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise OkxTransportError("invalid_transaction_hash")
    return value.lower()


def _uint(value, *, positive=False):
    if type(value) not in (str, int) or not re.fullmatch(r"[0-9]{1,78}", str(value)):
        raise OkxTransportError("invalid_amount")
    result = int(value)
    if not int(positive) <= result < 2**256:
        raise OkxTransportError("invalid_amount")
    return result


def _hex(value, *, abi=False):
    pattern = r"0x[0-9a-fA-F]{64}" if abi else r"0x[0-9a-fA-F]{1,64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise OkxTransportError("invalid_rpc_quantity")
    return int(value, 16)


def _number(value, *, positive=False, maximum=None):
    if isinstance(value, bool) or len(str(value)) > 100:
        raise OkxTransportError("invalid_number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise OkxTransportError("invalid_number") from None
    if (not number.is_finite() or number < 0 or (positive and number == 0)
            or abs(number.adjusted()) > 100 or (maximum is not None and number > maximum)):
        raise OkxTransportError("invalid_number")
    return number


def _slippage(value):
    number = _number(value, maximum=Decimal(10))
    if number < 1:
        raise OkxTransportError("invalid_slippage")
    return format(number, "f")


def _api_token(token):
    return OKX_NATIVE if token == NATIVE else token


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Http:
    def __init__(self, timeout):
        self.timeout = timeout

    def request(self, method, path, *, params, headers, body=None):
        if not path.startswith("/api/v6/dex/aggregator/") or method != "GET":
            raise OkxTransportError("api_path_not_allowed")
        request = Request(HOST + path + "?" + urlencode(params),
                          headers={"User-Agent": "alpha-radar/1.0", "Accept": "application/json", **headers}, method=method)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=self.timeout()) as response:
                return json.loads(response.read(4_000_001))
        except HTTPError as error:
            # Error bodies can echo request details. Retain only numeric status codes.
            code = "http_" + str(error.code)
            try:
                payload = json.loads(error.read(16_384))
                api_code = str(payload.get("code", "")) if isinstance(payload, Mapping) else ""
                if re.fullmatch(r"[1-9][0-9]{0,9}", api_code):
                    code = api_code
            except Exception:
                pass
            finally:
                error.close()
            return {"code": code}
        except Exception:
            raise OkxTransportError("okx_request_failed") from None


class OkxV6SwapClient(OkxSwapClient):
    """Translate BEFORE inherited HMAC signing; never sign the v5 target."""

    def _request(self, path, params):
        path = path.replace("/api/v5/", "/api/v6/", 1)
        params = dict(params)
        if "slippage" in params:
            params["slippagePercent"] = params.pop("slippage")
        if path.endswith(("/quote", "/swap")):
            params.update(swapMode="exactIn", singleRouteOnly="true",
                          singlePoolPerHop="true", disableRFQ="true")
        if path.endswith("/swap"):
            params.update(autoSlippage="false", swapReceiverAddress=params["userWalletAddress"])
        return super()._request(path, params)


class OkxLiveTransport:
    durable_prepare = True

    def __init__(self, env: Mapping[str, str] | None = None, *, state_dir=None,
                 wallet_address=None, http=None, rpc=None, signer=None,
                 security_reader=None, monotonic=time.monotonic):
        self._env = os.environ if env is None else env
        self.wallet_address = _address(wallet_address or self._env.get("BSC_WALLET_ADDRESS"))
        if self.wallet_address in (NATIVE, OKX_NATIVE):
            raise OkxTransportError("invalid_wallet")
        self.state_dir = (Path(state_dir) if state_dir is not None else
                          Path.home() / ".config/alpha-radar/gmgn-live" / self.wallet_address)
        self.journal_path = self.state_dir / "okx-transactions.sqlite3"
        self._http, self._rpc_client, self._signer = http, rpc, signer
        self._security_reader = security_reader
        self._clock, self._deadline = monotonic, None
        self._contexts, self._quotes = {}, {}
        self._lock = threading.RLock()

    def preflight(self):
        """Validate local configuration, without network or transaction signing."""
        self._client()
        self._rpc_url()
        try:
            LocalEvmSigner(self.wallet_address, env=self._env)
        except AdapterError as error:
            raise OkxTransportError(error.code) from None
        return {"ok": True, "chain": "bsc", "wallet_address": self.wallet_address,
                "offline": True, "live_verified": False, "credentials_checked": True,
                "enabled": self._enabled(), "journal_path": str(self.journal_path),
                "max_buy_usd": "5", "max_gas_usd": "0.5", "slippage_range": [1, 10],
                "blockers": [], "security_provider": "goplus" if self._security_reader is None else "injected",
                "dag_enabled": self._env.get("OKX_DAG_ENABLED") == "1",
                "supported_selectors": sorted(_SELECTORS | ({"0x0c307f76"} if self._env.get("OKX_DAG_ENABLED") == "1" else set())),
                "required_env": ["OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE",
                                 "BSC_PRIVATE_KEY", "BSC_WALLET_ADDRESS", "BSC_RPC_URL",
                                 "OKX_LIVE_ENABLED", "OKX_ALLOW_AUTOMATED_TRADES"]}

    def _enabled(self):
        return (self._env.get("OKX_LIVE_ENABLED") == "1"
                and self._env.get("OKX_ALLOW_AUTOMATED_TRADES") == "1")

    def verify_connection(self):
        """Read-only chain/balance/API probe, also usable with both flags off."""
        self._chain()
        balance = self.balance(NATIVE)
        market = self.market()
        finality = self._finalized_block()
        proof = self._verify_trace_capability()
        return {"ok": True, "ready": True, "chain": "bsc", "chain_id": 56, "trace": proof,
                "finality": finality,
                "wallet_address": self.wallet_address, "balance": balance, "market": market,
                "wallet_source": "configured_address", "signer_checked": False,
                "live_verified": False, "read_only": True}

    def _finalized_block(self):
        """Require the node's finalized tag and independently validate its header."""
        try:
            block = self._rpc("eth_getBlockByNumber", ["finalized", False])
            if not isinstance(block, Mapping):
                raise ValueError()
            height, block_hash = _hex(block.get("number")), _hash(block.get("hash"))
            if int(block_hash, 16) == 0:
                raise ValueError()
            canonical = self._rpc("eth_getBlockByNumber", [hex(height), False])
            if (not isinstance(canonical, Mapping) or _hex(canonical.get("number")) != height
                    or _hash(canonical.get("hash")) != block_hash):
                raise ValueError()
            return {"source": "rpc_finalized_tag", "block_number": height, "block_hash": block_hash}
        except Exception:
            raise OkxTransportError("finalized_rpc_required") from None

    def _receipt_finality(self, receipt):
        finality = self._finalized_block()
        height = _hex(receipt["blockNumber"])
        if height > finality["block_number"]:
            return False, finality
        # Re-read after the finalized checkpoint: a pre-checkpoint receipt/header
        # lookup could have observed a branch displaced while the RPC calls ran.
        canonical = self._rpc("eth_getBlockByNumber", [hex(height), False])
        if (not isinstance(canonical, Mapping) or _hex(canonical.get("number")) != height
                or _hash(canonical.get("hash")) != _hash(receipt["blockHash"])):
            raise OkxTransportError("receipt_finalized_block_mismatch")
        return True, finality

    def _verify_trace_capability(self):
        latest = self._rpc("eth_getBlockByNumber", ["latest", True])
        if not isinstance(latest, Mapping):
            raise OkxTransportError("trace_rpc_required")
        height = _hex(latest.get("number"))
        for offset in range(min(3, height + 1)):
            block = latest if offset == 0 else self._rpc("eth_getBlockByNumber", [hex(height - offset), True])
            if not isinstance(block, Mapping):
                continue
            for candidate in block.get("transactions", [])[:5]:
                tx = candidate if isinstance(candidate, Mapping) else self._rpc("eth_getTransactionByHash", [_hash(candidate)])
                if not isinstance(tx, Mapping) or tx.get("to") is None:
                    continue
                tx_hash = _hash(tx.get("hash"))
                receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
                if not isinstance(receipt, Mapping) or _hex(receipt.get("status")) != 1:
                    continue
                if (_hash(receipt.get("transactionHash")) != tx_hash
                        or _hash(tx.get("blockHash")) != _hash(block.get("hash"))
                        or _hash(receipt.get("blockHash")) != _hash(block.get("hash"))):
                    continue
                gas = _hex(receipt.get("gasUsed")) * _hex(receipt.get("effectiveGasPrice"))
                delta, source = self._native_delta(tx_hash, tx, gas, wallet=_address(tx["from"]))
                if delta is not None:
                    return {"supported": True, "source": source, "probe_tx_hash": tx_hash}
        raise OkxTransportError("trace_rpc_required")

    def ready_for_entries(self):
        """Boolean admission probe; never sends a transaction or consumes a nonce."""
        with self._lock:
            try:
                self._chain()
                self._finalized_block()
                with self._journal() as db:
                    rows = list(db.execute("SELECT tx_hash FROM transactions WHERE status NOT IN ('confirmed','failed','cancelled')"))
                for row in rows:
                    self.order(row["tx_hash"])
                with self._journal() as db:
                    if db.execute("SELECT 1 FROM transactions WHERE status NOT IN ('confirmed','failed','cancelled') LIMIT 1").fetchone():
                        return False
                self._nonce()
                return True
            except Exception:
                return False

    def fees(self):
        """Settled approval gas only. Stable IDs let the worker expense once.

        Reconciles receipts without signing/broadcast. Both successful and
        reverted approvals consume gas. USD is valued at preparation price.
        """
        with self._lock:
            with self._journal() as db:
                rows = list(db.execute("SELECT tx_hash FROM transactions WHERE kind='approval'"))
            events = []
            for row in rows:
                result = self.order(row["tx_hash"])
                if result.get("approval_confirmed") or result["status"] == "failed":
                    if result["gas_usd"] is not None:
                        events.append({"id": "okx-approval:" + row["tx_hash"], "gas_usd": result["gas_usd"]})
            return events

    def _timeout(self):
        remaining = 10.0 if self._deadline is None else self._deadline - self._clock()
        if remaining <= 0:
            raise OkxTransportError("submission_deadline_expired")
        return min(10.0, remaining)

    def _client(self):
        credentials = {key: self._env.get(key, "") for key in
                       ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")}
        if not all(isinstance(v, str) and v.strip() for v in credentials.values()):
            raise OkxTransportError("missing_okx_credentials")
        if any(v != v.strip() or any(ord(c) < 33 or ord(c) > 126 for c in v) for v in credentials.values()):
            raise OkxTransportError("invalid_okx_credentials")
        return OkxV6SwapClient(self._http or _Http(self._timeout), credentials)

    def _rpc_url(self):
        url = self._env.get("BSC_RPC_URL", "")
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment
                    or any(c.isspace() for c in url)):
                raise ValueError()
            parsed.port
        except (ValueError, TypeError):
            raise OkxTransportError("invalid_rpc_url") from None
        return url

    def _rpc(self, method, params):
        timeout = self._timeout()
        if self._rpc_client is None:
            self._rpc_client = JsonRpcBroadcaster(self._rpc_url())
        result = self._rpc_client._call(method, params, timeout_seconds=timeout)
        if not isinstance(result, Mapping) or result.get("error") is not None or "result" not in result:
            raise OkxTransportError("rpc_request_failed")
        return result["result"]

    def _chain(self):
        if _hex(self._rpc("eth_chainId", [])) != 56:
            raise OkxTransportError("rpc_wrong_chain")

    def _call(self, token, data, block="latest"):
        return self._rpc("eth_call", [{"to": token, "data": data}, block])

    def _decimals(self, token, block="latest"):
        value = 18 if token == NATIVE else _hex(self._call(token, "0x313ce567", block), abi=True)
        if value > 36:
            raise OkxTransportError("unsupported_token_decimals")
        return value

    def balance(self, token=NATIVE):
        token = _address(token)
        self._chain()
        if token == NATIVE:
            amount = _hex(self._rpc("eth_getBalance", [self.wallet_address, "latest"]))
        else:
            amount = _hex(self._call(token, "0x70a08231" + self.wallet_address[2:].zfill(64)), abi=True)
        return {"token": token, "wallet_address": self.wallet_address, "chain": "bsc",
                "amount_atomic": str(amount), "decimals": self._decimals(token)}

    @staticmethod
    def _route(result):
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            code = result.get("error") if isinstance(result, Mapping) else None
            if isinstance(code, str) and re.fullmatch(r"okx_api_(?:[0-9]{1,10}|http_[1-5][0-9]{2})", code):
                raise OkxTransportError(code)
            raise OkxTransportError("okx_quote_or_build_failed")
        row = result.get("route")
        if not isinstance(row, Mapping):
            raise OkxTransportError("invalid_okx_route")
        route = row.get("routerResult", row)
        if not isinstance(route, Mapping):
            raise OkxTransportError("invalid_okx_route")
        return route

    @staticmethod
    def _route_identity(route, source, target, amount):
        if (str(route.get("chainIndex")) != "56" or route.get("swapMode", "exactIn") != "exactIn"
                or _uint(route.get("fromTokenAmount"), positive=True) != amount):
            raise OkxTransportError("quote_identity_mismatch")
        for field, token in (("fromToken", source), ("toToken", target)):
            info = route.get(field)
            if not isinstance(info, Mapping) or _address(info.get("tokenContractAddress")) != _api_token(token):
                raise OkxTransportError("quote_identity_mismatch")

    def market(self):
        result = self._client().quote_bsc(USDT, str(10**18), OKX_NATIVE, "1")
        route = self._route(result)
        self._route_identity(route, NATIVE, USDT, 10**18)
        price = _number(route["fromToken"].get("tokenUnitPrice"), positive=True)
        with localcontext() as ctx:
            ctx.prec = 120
            maximum = int(MAX_BUY_USD * 10**18 / price)
        return {"chain": "bsc", "native_token": NATIVE, "native_price_usd": str(price),
                "max_buy_amount_atomic": str(maximum), "max_buy_usd": "5"}

    def security(self, token):
        token = _address(token)
        result = {"chain": "bsc", "token": token, "safe": False, "is_honeypot": None,
                  "cannot_sell_all": None, "buy_tax": None, "sell_tax": None,
                  "assessment_source": "goplus_execution_gate"}
        try:
            data = (self._security_reader or self._goplus_security)(token)
            if _address(data.get("token")) != token or data.get("chain") != "bsc":
                raise ValueError()
            observed = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
            if observed.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 60:
                raise ValueError()
            for field in ("is_honeypot", "cannot_sell_all", "cannot_buy", "cannot_sell"):
                value = data.get(field)
                result[field] = (value if type(value) is bool else
                                 False if value == "0" else True if value == "1" else None)
            for field in ("buy_tax", "sell_tax"):
                if data.get(field) not in (None, ""):
                    result[field] = str(_number(data[field], maximum=Decimal(1)))
            result["safe"] = (result["is_honeypot"] is False and result["cannot_sell_all"] is False
                              and result["cannot_buy"] is not True and result["cannot_sell"] is not True
                              and all(result[k] is not None and Decimal(result[k]) < Decimal("0.1")
                                      for k in ("buy_tax", "sell_tax")))
        except Exception:
            result.update(safe=False, error="security_evidence_unavailable")
        return result

    def _goplus_security(self, token):
        # The scanner helper's cache fallback cannot establish live freshness.
        # Use the same documented endpoint with a deadline and no cache/retry.
        url = "https://api.gopluslabs.io/api/v1/token_security/56?" + urlencode({"contract_addresses": token})
        request = Request(url, headers={"Accept": "application/json"})
        started = self._clock()
        with build_opener(_NoRedirect()).open(request, timeout=self._timeout()) as response:
            payload = json.loads(response.read(4_000_001))
        if str(payload.get("code")) != "1" or self._clock() - started > 10:
            raise OkxTransportError("security_evidence_unavailable")
        matches = [row for key, row in payload.get("result", {}).items() if _address(key) == token]
        if len(matches) != 1 or not isinstance(matches[0], Mapping):
            raise OkxTransportError("security_identity_mismatch")
        return {**matches[0], "token": token, "chain": "bsc", "observed_at": datetime.now(timezone.utc).isoformat()}

    def set_context(self, token, pool):
        token, pool = _address(token), _address(pool)
        if token in (NATIVE, OKX_NATIVE, WBNB) or pool in (NATIVE, OKX_NATIVE, token):
            raise OkxTransportError("invalid_pool_context")
        with self._lock:
            if self._contexts.get(token) != pool:
                self._quotes = {key: value for key, value in self._quotes.items() if key[0] != token}
            self._contexts[token] = pool

    def _pair(self, token, side, amount, slippage):
        token, amount, slippage = _address(token), _uint(amount, positive=True), _slippage(slippage)
        if side not in ("buy", "sell") or token not in self._contexts:
            raise OkxTransportError("missing_pool_context_or_invalid_side")
        source, target = (NATIVE, token) if side == "buy" else (token, NATIVE)
        return token, source, target, amount, slippage

    def quote(self, token, side, amount_atomic, slippage_percent):
        with self._lock:
            token, source, target, amount, slippage = self._pair(token, side, amount_atomic, slippage_percent)
            started = self._clock()
            route = self._route(self._client().quote_bsc(_api_token(target), str(amount), _api_token(source), slippage))
            self._route_identity(route, source, target, amount)
            output = _uint(route.get("toTokenAmount"), positive=True)
            with localcontext() as ctx:
                ctx.prec = 120
                minimum = _min_out(str(output), slippage)
            quote = {"chain": "bsc", "side": side, "input_token": source, "output_token": target,
                     "input_amount": str(amount), "output_amount": str(output), "min_output_amount": minimum,
                     "input_decimals": 18 if side == "buy" else None,
                     "output_decimals": 18 if side == "sell" else None, "slippage_percent": slippage}
            if self._clock() - started > 10:
                raise OkxTransportError("quote_expired")
            self._quotes[(token, side, amount, slippage)] = (started + 10, dict(quote))
            return quote

    def _validate_calldata(self, tx, token, side, amount, minimum, source, target):
        data = tx["data"].lower()
        selector = data[:10]
        if selector == "0x0c307f76" and self._env.get("OKX_DAG_ENABLED") == "1":
            # Lazy import keeps the diagnostic module out of legacy startup.
            from alpha_okx_dag_execution import prepare_dag
            from alpha_okx_dag_preflight import PreflightError, ReadOnlyRpc
            transport = self

            class CheckedRpc:
                def call(self, method, params):
                    if method not in ReadOnlyRpc.METHODS:
                        raise PreflightError("rpc_method_forbidden")
                    return transport._rpc(method, params)

            try:
                prepared = prepare_dag(tx, rpc=CheckedRpc(), wallet=self.wallet_address,
                                       source=_api_token(source), target=_api_token(target), amount=amount,
                                       minimum=minimum, expected_pool=self._contexts[token],
                                       trim_recipient=self._env.get("OKX_DAG_TRIM_RECIPIENT"),
                                       max_trim_per_mille=self._env.get("OKX_DAG_MAX_TRIM_PER_MILLE", "0"))
            except PreflightError as error:
                raise OkxTransportError(str(error)) from None
            self._timeout()
            tx["data"] = prepared["data"]
            return
        if selector not in _SELECTORS or (len(data) - 10) % 64:
            raise OkxTransportError("calldata_unsupported")
        words = [int(data[i:i+64], 16) for i in range(10, len(data), 64)]
        head = 8 if selector in _BASE else 5 if selector == "0x08298b5a" else 4
        # Exact canonical ABI layout disallows hidden pools, offset aliasing,
        # trailing fee/commission metadata, and a pool planted in unused bytes.
        if len(words) != head + 2 or words[head-1] != head * 32 or words[head] != 1:
            raise OkxTransportError("pool_unverifiable")
        packed = words[-1]
        pool = "0x" + f"{packed & _MASK:040x}"
        if pool != self._contexts[token]:
            raise OkxTransportError("pool_binding_mismatch")
        v3 = selector in _V3
        flags = (1 << 255) | ((1 << 253) if v3 else (7 << 252) | (1 << 254))
        allowed = _MASK | flags | (0 if v3 else ((1 << 32) - 1) << 160)
        if packed & ~allowed:
            raise OkxTransportError("unsupported_pool_flags")
        if not v3 and not 0 < (packed >> 160) & ((1 << 32) - 1) <= 1_000_000_000:
            raise OkxTransportError("invalid_pool_fee")
        unwrap = bool(packed & (1 << (253 if v3 else 254)))
        if unwrap != (side == "sell"):
            raise OkxTransportError("native_unwrap_mismatch")
        token0 = self._pool_token(pool, "0x0dfe1681")
        token1 = self._pool_token(pool, "0xd21220a7")
        actual_source, actual_target = (token1, token0) if packed & (1 << 255) else (token0, token1)
        if (actual_source, actual_target) != ((WBNB, token) if side == "buy" else (token, WBNB)):
            raise OkxTransportError("pool_token_direction_mismatch")
        if selector in _BASE:
            if words[2] > _MASK or words[3] > _MASK or words[1] > _MASK:
                raise OkxTransportError("unsupported_transfer_mode")
            expiry = words[6]
            if not time.time() < expiry <= time.time() + 1200:
                raise OkxTransportError("invalid_calldata_deadline")
        actual_minimum = words[5] if selector in _BASE else words[2]
        if actual_minimum < minimum:
            raise OkxTransportError("calldata_slippage_exceeded")
        # The existing validator supplies amount, recipient and token semantics.
        # Normalize the documented temporary zero-native input alias for V2.
        validation_data = data
        source_index = 2 if selector in _BASE else 0
        if selector not in _V3 and source == NATIVE and words[source_index] & _MASK == 0:
            start = 10 + source_index * 64
            replacement = (words[source_index] & ~_MASK) | int(OKX_NATIVE, 16)
            validation_data = data[:start] + f"{replacement:064x}" + data[start+64:]
        try:
            _validate_okx_dynamic_calldata(validation_data, side=side, token=token, pool=pool,
                                          wallet=self.wallet_address, from_token=_api_token(source),
                                          target_token=_api_token(target), amount=str(amount),
                                          context={"min_out": str(actual_minimum)})
        except AdapterError as error:
            raise OkxTransportError(error.code) from None

    def _pool_token(self, pool, selector):
        value = _hex(self._call(pool, selector), abi=True)
        if value > _MASK:
            raise OkxTransportError("invalid_pool_token")
        return "0x" + f"{value:040x}"

    @contextmanager
    def _journal(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.journal_path, timeout=0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS transactions (tx_hash TEXT PRIMARY KEY, "
                       "wallet TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, "
                       "nonce INTEGER NOT NULL, intent TEXT NOT NULL, created_at TEXT NOT NULL, "
                       "send_attempted INTEGER NOT NULL DEFAULT 0 CHECK(send_attempted IN (0,1)))")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(transactions)")}
            if "send_attempted" not in columns:
                # Legacy rows cannot prove whether send was invoked. Preserve
                # every row and conservatively mark them as possibly sent.
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE transactions_v2 (tx_hash TEXT PRIMARY KEY, "
                           "wallet TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, "
                           "nonce INTEGER NOT NULL, intent TEXT NOT NULL, created_at TEXT NOT NULL, "
                           "send_attempted INTEGER NOT NULL DEFAULT 0 CHECK(send_attempted IN (0,1)))")
                db.execute("INSERT INTO transactions_v2 SELECT tx_hash,wallet,kind,status,nonce,intent,created_at,1 FROM transactions")
                db.execute("DROP TABLE transactions")
                db.execute("ALTER TABLE transactions_v2 RENAME TO transactions")
                db.execute("CREATE UNIQUE INDEX transactions_reserved_nonce ON transactions(wallet,nonce) WHERE status != 'cancelled'")
                db.commit()
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS transactions_reserved_nonce "
                       "ON transactions(wallet,nonce) WHERE status != 'cancelled'")
            if db.execute("SELECT 1 FROM transactions WHERE wallet != ? LIMIT 1", (self.wallet_address,)).fetchone():
                raise OkxTransportError("journal_wallet_mismatch")
            yield db
        finally:
            db.close()

    def _nonce(self):
        latest = _hex(self._rpc("eth_getTransactionCount", [self.wallet_address, "latest"]))
        pending = _hex(self._rpc("eth_getTransactionCount", [self.wallet_address, "pending"]))
        if latest != pending:
            raise OkxTransportError("wallet_nonce_pending")
        return latest

    def _gas_transaction(self, tx, price, spent_gas=Decimal(0)):
        nonce = self._nonce()
        gas_price = _hex(self._rpc("eth_gasPrice", []))
        clean = {"from": self.wallet_address, "to": _address(tx["to"]), "data": tx["data"],
                 "value": _hex(tx["value"]) if str(tx["value"]).startswith("0x") else _uint(tx["value"]),
                 "chainId": 56, "nonce": nonce, "gasPrice": gas_price}
        rpc_tx = {**clean, "value": hex(clean["value"]), "chainId": "0x38",
                  "nonce": hex(nonce), "gasPrice": hex(gas_price)}
        estimate = _hex(self._rpc("eth_estimateGas", [rpc_tx]))
        gas = (estimate * 120 + 99) // 100
        with localcontext() as ctx:
            ctx.prec = 120
            cost = Decimal(gas * gas_price) * price / 10**18
        if gas <= 0 or gas_price <= 0 or cost + spent_gas > MAX_GAS_USD:
            raise OkxTransportError("gas_exceeds_0_5_usd")
        if _hex(self._rpc("eth_getBalance", [self.wallet_address, "pending"])) < clean["value"] + gas * gas_price:
            raise OkxTransportError("insufficient_native_balance")
        clean["gas"] = gas
        return clean

    def _broadcast(self, db, tx, intent, kind, on_prepared=None):
        self._timeout()
        if not self._enabled():
            raise OkxTransportError("submission_disabled")
        self._finalized_block()
        cancelled = list(db.execute("SELECT intent FROM transactions WHERE nonce=? AND status='cancelled'", (tx["nonce"],)))
        if cancelled:
            # Keep a rejected order ID permanently rejected, even if the API
            # returns identical calldata/expiry on an immediate retry.
            previous_gas = max(json.loads(row["intent"])["transaction"]["gas"] for row in cancelled)
            tx = {**tx, "gas": max(tx["gas"], previous_gas + 1)}
            price = _number(intent["native_price_usd"], positive=True)
            spent = self._approval_cost(db, intent["token"], price) if kind == "approval" or intent.get("side") == "sell" else Decimal(0)
            with localcontext() as ctx:
                ctx.prec = 200
                if Decimal(tx["gas"] * tx["gasPrice"]) * price / 10**18 + spent > MAX_GAS_USD:
                    raise OkxTransportError("gas_exceeds_0_5_usd")
            if _hex(self._rpc("eth_getBalance", [self.wallet_address, "pending"])) < tx["value"] + tx["gas"] * tx["gasPrice"]:
                raise OkxTransportError("insufficient_native_balance")
        if self._signer is None:
            try:
                self._signer = LocalEvmSigner(self.wallet_address, env=self._env)
            except AdapterError as error:
                raise OkxTransportError(error.code) from None
        try:
            from eth_utils import keccak, to_checksum_address

            tx = {**tx, "from": to_checksum_address(tx["from"]), "to": to_checksum_address(tx["to"])}
            raw = self._signer.sign(tx)
            if not self._signer.verify_signed_transaction(raw, tx):
                raise ValueError()
            tx_hash = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
        except Exception:
            raise OkxTransportError("signing_failed") from None
        intent = {**intent, "transaction": tx}
        created = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO transactions (tx_hash,wallet,kind,status,nonce,intent,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (tx_hash, self.wallet_address, kind, "prepared", tx["nonce"],
                    json.dumps(intent, sort_keys=True), created))
        db.commit()  # Hash and intent are durable before either external side effect.
        send_attempted = False
        try:
            if on_prepared is not None:
                on_prepared(tx_hash)
            self._timeout()
            if not self._enabled():
                raise OkxTransportError("submission_disabled")
            self._finalized_block()
            # Callback latency must not allow another pending nonce to slip in.
            if self._nonce() != tx["nonce"]:
                raise OkxTransportError("nonce_changed_before_broadcast")
            self._timeout()
            db.execute("UPDATE transactions SET send_attempted=1 WHERE tx_hash=?", (tx_hash,))
            db.commit()
            send_attempted = True
            sent = _hash(self._rpc("eth_sendRawTransaction", [raw]))
            if sent != tx_hash:
                raise OkxTransportError("broadcast_hash_mismatch")
            db.execute("UPDATE transactions SET status='pending' WHERE tx_hash=?", (tx_hash,))
        except Exception:
            status = "unknown" if send_attempted else "cancelled"
            db.execute("UPDATE transactions SET status=? WHERE tx_hash=?", (status, tx_hash))
            db.commit()
            code = "broadcast_unknown_do_not_retry" if send_attempted else "cancelled_before_send"
            raise OkxTransportError(code, ambiguous=send_attempted,
                                    order_id=tx_hash if kind == "swap" else None,
                                    approval_tx_hash=tx_hash if kind == "approval" else None) from None
        return tx_hash

    def _allowance(self, token):
        data = "0xdd62ed3e" + self.wallet_address[2:].zfill(64) + BSC_APPROVER[2:].zfill(64)
        return _hex(self._call(token, data), abi=True)

    def ensure_allowance(self, token, amount_atomic, *, allow_submit=True):
        """Prepare exact allowance (zero-reset first when needed), without a swap.

        ready=False statuses: pending, unknown, blocked. This method must be
        called again after confirmation. It never invokes the worker callback.
        """
        token, amount = _address(token), _uint(amount_atomic, positive=True)
        if type(allow_submit) is not bool:
            raise OkxTransportError("invalid_approval_mode")
        if token in (NATIVE, OKX_NATIVE, WBNB):
            raise OkxTransportError("invalid_approval_token")
        with self._lock:
            if not self._enabled():
                raise OkxTransportError("submission_disabled")
            self._deadline = self._clock() + 10
            try:
                self._chain()
                with self._journal() as db:
                    outstanding = list(db.execute("SELECT tx_hash FROM transactions WHERE status NOT IN ('confirmed','failed','cancelled')"))
                for row in outstanding:
                    self.order(row["tx_hash"])
                with self._journal() as db:
                    try:
                        db.execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError:
                        raise OkxTransportError("transport_busy") from None
                    row = db.execute("SELECT * FROM transactions WHERE status NOT IN ('confirmed','failed','cancelled') LIMIT 1").fetchone()
                    if row:
                        return {"ready": False, "status": row["status"] if row["kind"] == "approval" else "blocked",
                                "approval_tx_hash": row["tx_hash"] if row["kind"] == "approval" else None,
                                "error": "journal_unresolved_do_not_retry"}
                    self._nonce()
                    allowance = self._allowance(token)
                    if allowance >= amount:
                        return {"ready": True, "status": "confirmed", "allowance_atomic": str(allowance)}
                    if not allow_submit:
                        return {"ready": False, "status": "blocked", "error": "approval_limit_reached"}
                    # Tokens requiring zero-before-nonzero work in two durable phases.
                    approve_amount = 0 if allowance else amount
                    response = self._client().approve_bsc(token, str(approve_amount), token)
                    approval_row = self._route(response)
                    expected = "0x095ea7b3" + BSC_APPROVER[2:].zfill(64) + f"{approve_amount:064x}"
                    if (_address(approval_row.get("dexContractAddress")) != BSC_APPROVER
                            or str(approval_row.get("data", "")).lower() != expected):
                        raise OkxTransportError("approval_calldata_mismatch")
                    price = _number(self.market()["native_price_usd"], positive=True)
                    tx = self._gas_transaction({"to": token, "data": expected, "value": 0}, price,
                                               self._approval_cost(db, token, price))
                    intent = {"token": token, "amount": str(approve_amount), "native_price_usd": str(price)}
                    try:
                        tx_hash = self._broadcast(db, tx, intent, "approval")
                    except OkxTransportError as error:
                        if not error.ambiguous and error.approval_tx_hash:
                            return {"ready": False, "status": "rejected", "approval_tx_hash": error.approval_tx_hash,
                                    "error": "cancelled_before_send"}
                        if not error.ambiguous:
                            raise
                        return {"ready": False, "status": "unknown", "approval_tx_hash": error.approval_tx_hash,
                                "error": "broadcast_unknown_do_not_retry"}
                    return {"ready": False, "status": "pending", "approval_tx_hash": tx_hash,
                            "approve_amount_atomic": str(approve_amount)}
            finally:
                self._deadline = None

    @staticmethod
    def _approval_cost(db, token, price):
        cost = Decimal(0)
        for row in db.execute("SELECT * FROM transactions WHERE status IN ('confirmed','failed') ORDER BY nonce DESC"):
            intent = json.loads(row["intent"])
            if intent["token"] != token:
                continue
            if row["kind"] == "swap":
                break
            cost += Decimal(intent.get("approval_gas_native", "0")) * price
        return cost

    def submit(self, token, side, amount_atomic, slippage_percent, *, deadline=None, on_prepared=None):
        with self._lock:
            if not self._enabled():
                raise OkxTransportError("submission_disabled")
            if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
                raise OkxTransportError("invalid_deadline")
            self._deadline = min(self._clock() + 10, deadline) if deadline is not None else self._clock() + 10
            try:
                return self._submit(token, side, amount_atomic, slippage_percent, on_prepared)
            finally:
                self._deadline = None

    def _submit(self, token, side, amount_atomic, slippage_percent, on_prepared):
        token, source, target, amount, slippage = self._pair(token, side, amount_atomic, slippage_percent)
        if on_prepared is not None and not callable(on_prepared):
            raise OkxTransportError("invalid_prepared_callback")
        self._timeout()
        self._chain()
        # Reconciliation is read-only on chain, including for approvals.
        with self._journal() as db:
            outstanding = list(db.execute("SELECT tx_hash FROM transactions WHERE status NOT IN ('confirmed','failed','cancelled')"))
        for row in outstanding:
            self.order(row["tx_hash"])
        with self._journal() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                raise OkxTransportError("transport_busy") from None
            outstanding = db.execute("SELECT * FROM transactions WHERE status NOT IN ('confirmed','failed','cancelled') LIMIT 1").fetchone()
            if outstanding:
                if outstanding["kind"] == "approval" and outstanding["status"] == "pending":
                    raise OkxTransportError("approval_pending", approval_tx_hash=outstanding["tx_hash"])
                raise OkxTransportError("journal_unresolved_do_not_retry", ambiguous=True)
            self._nonce()
            if side == "buy" and self.security(token)["safe"] is not True:
                raise OkxTransportError("security_not_passed")
            price = _number(self.market()["native_price_usd"], positive=True)
            with localcontext() as ctx:
                ctx.prec = 200
                if side == "buy" and Decimal(amount) * price > MAX_BUY_USD * 10**18:
                    raise OkxTransportError("buy_exceeds_5_usd")
            cached = self._quotes.get((token, side, amount, slippage))
            if cached is None:
                self.quote(token, side, amount, slippage)
                cached = self._quotes[(token, side, amount, slippage)]
            expiry, quote = cached
            self._deadline = min(self._deadline, expiry)
            self._timeout()
            result = self._client().build_swap_bsc(_api_token(target), str(amount), _api_token(source), slippage, self.wallet_address)
            route = self._route(result)
            self._route_identity(route, source, target, amount)
            if any(result.get(part, {}).get(field) for part in ("tx", "route")
                   for field in ("signatureData", "additionalSignatureData")):
                raise OkxTransportError("additional_signature_unsupported")
            tx = dict(result.get("tx") or {})
            tx.setdefault("chainId", 56)
            try:
                tx = _validate_transaction_semantics(tx, self.wallet_address, {BSC_ROUTER},
                                                      from_token=_api_token(source), amount=str(amount))
            except AdapterError as error:
                raise OkxTransportError(error.code) from None
            minimum = _uint(quote["min_output_amount"], positive=True)
            self._validate_calldata(tx, token, side, amount, minimum, source, target)
            intent = {"token": token, "side": side, "input_token": source, "output_token": target,
                      "amount": str(amount), "minimum": str(minimum), "pool": self._contexts[token],
                      "native_price_usd": str(price)}
            # Include the exact approval's gas in the eventual swap's $0.50 cap.
            spent = self._approval_cost(db, token, price)
            if side == "sell" and self._allowance(token) < amount:
                raise OkxTransportError("allowance_not_ready")
            tx = self._gas_transaction(tx, price, spent if side == "sell" else Decimal(0))
            if tx["data"][:10].lower() == "0x0c307f76":
                from alpha_okx_dag_execution import require_fresh_deadline
                from alpha_okx_dag_preflight import PreflightError
                try:
                    require_fresh_deadline(tx)
                except PreflightError as error:
                    raise OkxTransportError(str(error)) from None
            tx_hash = self._broadcast(db, tx, intent, "swap", on_prepared)
            return {"chain": "bsc", "wallet_address": self.wallet_address, "order_id": tx_hash,
                    "tx_hash": tx_hash, "status": "pending", "fill_complete": False,
                    "created_at": datetime.now(timezone.utc).isoformat()}

    def _receipt_identity(self, row, receipt):
        tx_hash = row["tx_hash"]
        intent = json.loads(row["intent"])
        expected = intent["transaction"]
        if _hash(receipt.get("transactionHash")) != tx_hash:
            raise OkxTransportError("receipt_identity_mismatch")
        tx = self._rpc("eth_getTransactionByHash", [tx_hash])
        if not isinstance(tx, Mapping) or _hash(tx.get("hash")) != tx_hash:
            raise OkxTransportError("receipt_transaction_unavailable")
        for field in ("from", "to"):
            if _address(tx.get(field)) != _address(expected[field]) or _address(receipt.get(field)) != _address(expected[field]):
                raise OkxTransportError("receipt_identity_mismatch")
        for field in ("value", "nonce", "gas"):
            if _hex(tx.get(field)) != expected[field]:
                raise OkxTransportError("receipt_transaction_mismatch")
        if str(tx.get("input", "")).lower() != expected["data"].lower():
            raise OkxTransportError("receipt_calldata_mismatch")
        if "chainId" in tx and _hex(tx["chainId"]) != 56:
            raise OkxTransportError("rpc_wrong_chain")
        height = _hex(receipt.get("blockNumber"))
        block = self._rpc("eth_getBlockByNumber", [hex(height), False])
        block_hash = _hash(receipt.get("blockHash"))
        if (not isinstance(block, Mapping) or _hex(block.get("number")) != height
                or _hash(block.get("hash")) != block_hash or _hash(tx.get("blockHash")) != block_hash
                or _hex(tx.get("blockNumber")) != height
                or tx_hash not in [_hash(h) for h in block.get("transactions", [])]):
            raise OkxTransportError("receipt_block_mismatch")
        return intent, tx, block

    def _native_delta(self, tx_hash, tx, gas_wei, *, wallet=None):
        """Net wallet BNB transfer, excluding gas; None means no proof."""
        wallet = wallet or self.wallet_address
        try:
            trace = self._rpc("debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
            if (not isinstance(trace, Mapping) or trace.get("error")
                    or trace.get("type") != "CALL"
                    or _address(trace.get("from")) != wallet
                    or _address(trace.get("to")) != _address(tx["to"])
                    or _hex(trace.get("value")) != _hex(tx["value"])
                    or str(trace.get("input", "")).lower() != tx["input"].lower()):
                raise ValueError()

            def delta(frame):
                if not isinstance(frame, Mapping) or any(frame.get(k) for k in ("truncated", "timeout", "failed")):
                    raise ValueError()
                if frame.get("error"):
                    return 0  # A reverted ancestor rolls back every descendant transfer.
                kind = frame.get("type")
                if kind not in ("CALL", "STATICCALL", "DELEGATECALL", "CALLCODE", "CREATE", "CREATE2"):
                    raise ValueError()
                total = 0
                if kind in ("CALL", "CREATE", "CREATE2"):
                    value = _hex(frame.get("value", "0x0"))
                    sender, recipient = _address(frame["from"]), _address(frame["to"])
                    total = value * (int(recipient == wallet) - int(sender == wallet))
                calls = frame.get("calls", [])
                if not isinstance(calls, list):
                    raise ValueError()
                return total + sum(delta(child) for child in calls)

            return delta(trace), "callTracer"
        except Exception:
            pass
        try:
            trace = self._rpc("debug_traceTransaction", [tx_hash, {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}}])
            if not isinstance(trace, Mapping) or any(trace.get(k) for k in ("error", "truncated", "timeout", "failed")):
                raise ValueError()
            pre = {_address(k): v for k, v in trace["pre"].items()}
            post = {_address(k): v for k, v in trace["post"].items()}
            before, after = pre[wallet], post[wallet]
            if "balance" not in before or "balance" not in after:
                raise ValueError()
            return _hex(after["balance"]) - _hex(before["balance"]) + gas_wei, "prestateTracer"
        except Exception:
            return None, None

    def _token_delta(self, receipt, token):
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            raise OkxTransportError("receipt_logs_unavailable")
        total, seen = 0, set()
        for log in logs:
            if _address(log.get("address")) != token:
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or not topics or topics[0].lower() != TRANSFER_TOPIC:
                continue
            if (len(topics) != 3 or log.get("removed") is not False
                    or _hash(log.get("transactionHash")) != _hash(receipt["transactionHash"])
                    or _hash(log.get("blockHash")) != _hash(receipt["blockHash"])):
                raise OkxTransportError("receipt_log_identity_mismatch")
            index = _hex(log.get("logIndex"))
            if index in seen:
                raise OkxTransportError("duplicate_transfer_log")
            seen.add(index)
            sender, recipient = _hex(topics[1], abi=True), _hex(topics[2], abi=True)
            if max(sender, recipient) > _MASK:
                raise OkxTransportError("invalid_transfer_log")
            wallet = int(self.wallet_address, 16)
            total += _hex(log.get("data"), abi=True) * (int(recipient == wallet) - int(sender == wallet))
        return total

    def order(self, txhash):
        tx_hash = _hash(txhash)
        with self._lock, self._journal() as db:
            row = db.execute("SELECT * FROM transactions WHERE tx_hash=? AND wallet=?", (tx_hash, self.wallet_address)).fetchone()
            if row is None:
                raise OkxTransportError("transaction_not_in_journal", order_id=tx_hash)
            result = {"order_id": tx_hash, "tx_hash": tx_hash, "chain": "bsc", "wallet_address": self.wallet_address,
                      "created_at": row["created_at"], "status": "pending", "fill_complete": False,
                      "transaction_kind": row["kind"], "provider_status": row["status"],
                      "input_amount": None, "output_amount": None, "input_decimals": None, "output_decimals": None,
                      "input_token": None, "output_token": None, "gas_usd": None, "gas_native": None,
                      "native_amount_proven": False, "finalized": False,
                      "broadcast_attempted": bool(row["send_attempted"])}
            if row["status"] == "cancelled" and row["send_attempted"] == 0:
                return {**result, "status": "rejected", "provider_status": "cancelled",
                        "gas_usd": "0", "gas_native": "0", "error": "cancelled_before_send"}
            try:
                self._chain()
                receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
                if receipt is None:
                    if row["status"] in ("confirmed", "failed"):
                        db.execute("UPDATE transactions SET status='unknown' WHERE tx_hash=?", (tx_hash,))
                    return result
                intent, tx, block = self._receipt_identity(row, receipt)
                status = _hex(receipt.get("status"))
                if status not in (0, 1):
                    raise OkxTransportError("invalid_receipt_status")
                finalized, finality = self._receipt_finality(receipt)
                result.update(finality=finality, finalized=finalized)
                if not finalized:
                    db.execute("UPDATE transactions SET status='pending' WHERE tx_hash=?", (tx_hash,))
                    return {**result, "provider_status": "mined", "error": "awaiting_finality"}
                gas_used = _hex(receipt.get("gasUsed"))
                gas_price = _hex(receipt.get("effectiveGasPrice"))
                if gas_used > intent["transaction"]["gas"] or gas_price != intent["transaction"]["gasPrice"]:
                    raise OkxTransportError("receipt_gas_mismatch")
                gas_wei = gas_used * gas_price
                gas_native = Decimal(gas_wei) / 10**18
                result.update(gas_native=str(gas_native), gas_usd=str(gas_native * Decimal(intent["native_price_usd"])),
                              gas_usd_source="receipt_gas_at_preparation_price",
                              created_at=datetime.fromtimestamp(_hex(block["timestamp"]), timezone.utc).isoformat(),
                              created_at_source="execution_block_timestamp", provider_status="confirmed" if status else "failed")
                if status == 0:
                    if row["kind"] == "approval":
                        intent["approval_gas_native"] = str(gas_native)
                    db.execute("UPDATE transactions SET status='failed', intent=? WHERE tx_hash=?", (json.dumps(intent), tx_hash))
                    return {**result, "status": "failed"}
                if row["kind"] == "approval":
                    intent["approval_gas_native"] = str(gas_native)
                    db.execute("UPDATE transactions SET status='confirmed', intent=? WHERE tx_hash=?", (json.dumps(intent), tx_hash))
                    return {**result, "approval_confirmed": True}  # Never a confirmed SWAP.
                db.execute("UPDATE transactions SET status='pending' WHERE tx_hash=?", (tx_hash,))
                native_delta, proof = self._native_delta(tx_hash, tx, gas_wei)
                token_delta = self._token_delta(receipt, intent["token"])
                if native_delta is None:
                    return {**result, "error": "native_amount_unproven"}
                used, received = (-native_delta, token_delta) if intent["side"] == "buy" else (-token_delta, native_delta)
                if used <= 0 or received <= 0:
                    return {**result, "error": "fill_amount_mismatch"}
                anomalies = []
                if used > int(intent["amount"]):
                    anomalies.append("input_exceeds_intent")
                if received < int(intent["minimum"]):
                    anomalies.append("output_below_minimum")
                decimals = self._decimals(intent["token"], receipt["blockNumber"])
                result.update(status="confirmed", fill_complete=True, native_amount_proven=True,
                              fill_anomalies=anomalies,
                              native_amount_source=proof, input_amount=str(used), output_amount=str(received),
                              input_token=intent["input_token"], output_token=intent["output_token"],
                              input_decimals=18 if intent["side"] == "buy" else decimals,
                              output_decimals=decimals if intent["side"] == "buy" else 18)
                db.execute("UPDATE transactions SET status='confirmed' WHERE tx_hash=?", (tx_hash,))
                return result
            except Exception:
                if row["status"] in ("confirmed", "failed"):
                    db.execute("UPDATE transactions SET status='unknown' WHERE tx_hash=?", (tx_hash,))
                return {**result, "error": "receipt_evidence_unavailable"}


__all__ = ["OkxLiveTransport", "OkxTransportError", "OkxV6SwapClient", "NATIVE"]
