"""BSC/native-BNB GMGN CLI transport. Importing/constructing performs no I/O.

Configuration: GMGN_API_KEY, GMGN_PRIVATE_KEY (API signing PEM, Ed25519/RSA,
NOT an EVM wallet private key), GMGN_WALLET_ADDRESS (bound to the API key).
Only GMGN_LIVE_ENABLED=1 AND GMGN_ALLOW_AUTOMATED_TRADES=1 enable submit(). Optional settings:
GMGN_CLI_PATH (dist/index.js), GMGN_NODE_PATH, GMGN_TIMEOUT_SECONDS (default 30),
GMGN_QUOTE_SLIPPAGE_PERCENT (default 1). An explicit env replaces os.environ.

All monetary values are exact decimal strings; atoms are unsigned integer
strings, decimals are integers. None means unavailable, never zero. market()
returns native_price_usd and max_buy_amount_atomic=floor(5/price*10**18).
quote(token, side, amount_atomic, slippage_percent=...) returns input_amount,
output_amount, min_output_amount, input_token/output_token, input_decimals/
output_decimals. Quote amounts are estimates; non-native quote decimals and
USD valuations are None (not in the official quote). Use market() for USD.
submit()/order() return the same shape,
with status confirmed/pending/failed/unknown, provider_status, order_id,
tx_hash and actual report fields (None until a report is available).
price_usd is the executed TOKEN price. gas_native is human BNB, not wei.
other_provider_fees_usd=None: the documented report does not disclose these.

With BSC_RPC_URL, balance(token=NATIVE) uses chain-56-checked read-only RPC.
Otherwise balance uses documented portfolio holdings (human units) plus
token info decimals, paginating if necessary. The vendor does not document
token-balance response units; we intentionally do not infer them. If holdings
omits native BNB or a requested token, balance_unavailable is raised, not zero.
safe requires known non-honeypot status and known rug_ratio <= 0.3;
known buy/sell tax ratios must be <= 0.05. Missing taxes are permitted.
Other normalized risk fields are informational; entry policy stays in the worker.

No retry, polling, condition orders, config writes or execution at import.
After a submission timeout/process failure/unreadable receipt, ambiguous=True
means reconcile before doing anything further; this module never resubmits.
The CLI itself can retry read-only requests once on rate limiting, never POST.
Schema: vendored gmgn-skills src and gmgn-swap/token/portfolio SKILL.md.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import time
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


NATIVE = "0x0000000000000000000000000000000000000000"
NATIVE_DECIMALS = 18
MAX_BUY_USD = Decimal("5")
GMGN_DIST = Path(__file__).resolve().parents[1] / "external/open-source-radar/gmgn-skills/dist/index.js"
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_UINT = re.compile(r"[0-9]{1,78}\Z")
_DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")

# Load the vendor config once, then restore the explicitly supplied environment
# before getConfig/confirmTrade can run. This neutralizes dotenv override:true.
# Secrets are read only from the child environment, never interpolated here.
_BOOTSTRAP = """import {pathToFileURL} from 'node:url';
import {createPrivateKey} from 'node:crypto';
const cli=pathToFileURL(process.argv[1]);
const env={...process.env};
try {
  await import(new URL('./config.js',cli));
  for (const k of Object.keys(process.env)) delete process.env[k];
  Object.assign(process.env,env);
  delete process.env.GMGN_DEBUG;
  if (process.env.GMGN_PRIVATE_KEY) {
    const k=createPrivateKey(process.env.GMGN_PRIVATE_KEY.replace(/\\\\n/g,'\\n'));
    if (!['ed25519','rsa'].includes(k.asymmetricKeyType)) throw Error();
  }
  process.argv=[process.execPath,cli.pathname,...process.argv.slice(2)];
  await import(cli);
} catch { console.error('gmgn_transport_cli_failed'); process.exitCode=1; }
"""

_RPC_REQUEST = """const payload=JSON.parse(process.argv[1]);
const allowed=['eth_chainId','eth_getBalance','eth_call','eth_getTransactionByHash','eth_getBlockByNumber'];
try {
  if (!allowed.includes(payload.method)) throw Error();
  const r=await fetch(process.env.BSC_RPC_URL,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if (!r.ok) throw Error();
  console.log(JSON.stringify(await r.json()));
} catch { console.error('gmgn_transport_rpc_failed'); process.exitCode=1; }
"""


class GmgnTransportError(RuntimeError):
    """Secret-free failure. ambiguous=True requires submission reconciliation."""

    def __init__(self, code: str, *, ambiguous: bool = False, order_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.ambiguous = ambiguous
        self.order_id = order_id


def _address(value: Any, code: str = "invalid_token") -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise GmgnTransportError(code)
    return value.lower()


def _uint(value: Any, *, positive: bool = False, maximum: int = 2**256 - 1) -> str:
    if type(value) not in (int, str) or not _UINT.fullmatch(str(value)):
        raise GmgnTransportError("invalid_amount")
    result = int(value)
    if result < int(positive) or result > maximum:
        raise GmgnTransportError("invalid_amount")
    return str(result)


def _decimal(value: Any, *, positive: bool = False, maximum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise GmgnTransportError("invalid_number")
    raw = str(value)
    if len(raw) > 200 or not _DECIMAL.fullmatch(raw):
        raise GmgnTransportError("invalid_number")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise GmgnTransportError("invalid_number") from None
    if (not number.is_finite() or number < 0 or (positive and number == 0)
            or abs(number.adjusted()) > 100 or (maximum is not None and number > maximum)):
        raise GmgnTransportError("invalid_number")
    return number


def _slippage(value: Any) -> str:
    number = _decimal(value, maximum=Decimal(100))
    if number != number.to_integral_value():
        raise GmgnTransportError("invalid_slippage")
    return str(int(number))


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GmgnTransportError("invalid_order_id")
    return value


def _optional_number(data: dict, field: str) -> str | None:
    return None if data.get(field) is None else format(_decimal(data[field]), "f")


def _yes_no(value: Any) -> bool | None:
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


class GmgnLiveTransport:
    def __init__(self, env: Mapping[str, str] | None = None, run=None):
        self._env = dict(os.environ if env is None else env)
        if any(not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v
               for k, v in self._env.items()):
            raise GmgnTransportError("invalid_environment")
        self._uses_default_runner = run is None
        self._run = subprocess.run if run is None else run
        self._wallet = _address(self._env.get("GMGN_WALLET_ADDRESS"), "invalid_wallet")
        if self._wallet == NATIVE:
            raise GmgnTransportError("invalid_wallet")
        self._timeout = float(_decimal(self._env.get("GMGN_TIMEOUT_SECONDS", "30"),
                                       positive=True, maximum=Decimal(300)))
        self._quote_slippage = _slippage(self._env.get("GMGN_QUOTE_SLIPPAGE_PERCENT", "1"))

    @property
    def wallet_address(self) -> str:
        return self._wallet

    def _credentials(self, signed: bool = False) -> dict[str, str]:
        env = dict(self._env)
        api_key = env.get("GMGN_API_KEY", "")
        if not api_key or any(c.isspace() for c in api_key):
            raise GmgnTransportError("missing_or_invalid_api_key")
        if signed:
            key = env.get("GMGN_PRIVATE_KEY", "").replace("\\n", "\n").strip()
            match = re.fullmatch(r"-----BEGIN (PRIVATE KEY|RSA PRIVATE KEY)-----\s+"
                                 r"([A-Za-z0-9+/=\s]+)\s+-----END \1-----", key)
            if not match:
                raise GmgnTransportError("api_signing_pem_required")
            try:
                der = base64.b64decode(re.sub(r"\s", "", match[2]), validate=True)
            except (ValueError, binascii.Error):
                raise GmgnTransportError("api_signing_pem_required") from None
            if not der or der[0] != 0x30:
                raise GmgnTransportError("api_signing_pem_required")
            env["GMGN_PRIVATE_KEY"] = key
        else:
            env.pop("GMGN_PRIVATE_KEY", None)
        for key in ("GMGN_DEBUG", "NODE_OPTIONS", "NODE_DEBUG"):
            env.pop(key, None)
        return env

    def _cli(self, args: list[str], *, signed: bool = False, submission: bool = False, deadline: float | None = None) -> dict:
        env = self._credentials(signed)
        if not submission:
            env.pop("GMGN_ALLOW_AUTOMATED_TRADES", None)
        node, cli = self._runner()
        command = [node, "--input-type=module", "-e", _BOOTSTRAP, str(cli), *args, "--raw"]
        if deadline is not None and time.monotonic() >= deadline:
            raise GmgnTransportError("submission_deadline_expired")
        payload = self._process(command, env, str(cli.parent), submission=submission)
        # printResult receives json.data directly, NOT the HTTP envelope.
        if "code" in payload or "error" in payload:
            raise GmgnTransportError("invalid_cli_response", ambiguous=submission)
        return payload

    def _runner(self, *, check: bool = False):
        cli = Path(self._env.get("GMGN_CLI_PATH", str(GMGN_DIST))).resolve()
        if cli.suffix.lower() not in {".js", ".mjs"}:
            raise GmgnTransportError("cli_javascript_entrypoint_required")
        node = self._env.get("GMGN_NODE_PATH") or shutil.which("node") or "node"
        if Path(node).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise GmgnTransportError("node_executable_required")
        if check and self._uses_default_runner:
            resolved = shutil.which(node)
            if not resolved or not cli.is_file() or not cli.with_name("config.js").is_file():
                raise GmgnTransportError("cli_unavailable")
            node = resolved
        return node, cli

    def preflight(self) -> dict:
        """Offline config/runner check; no network, no credential/binding claim.

        PEM structure is checked here; Node crypto validates the actual signing
        key when invoked. This does not verify API access or GMGN wallet binding.
        Disabled live gates are reported, not treated as missing configuration.
        """
        self._credentials(signed=True)
        self._runner(check=True)
        if self._env.get("BSC_RPC_URL"):
            self._rpc_url()
        return {"ok": True, "configured": True, "offline": True, "chain": "bsc",
                "wallet_address": self.wallet_address, "credentials_verified": False,
                "wallet_binding_verified": False, "live_enabled": self._enabled(),
                "rpc_configured": bool(self._env.get("BSC_RPC_URL"))}

    def _enabled(self) -> bool:
        return (self._env.get("GMGN_LIVE_ENABLED") == "1"
                and self._env.get("GMGN_ALLOW_AUTOMATED_TRADES") == "1")

    def _process(self, command, env, cwd, *, submission=False):
        failure = None
        try:
            proc = self._run(command, shell=False, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="strict",
                             timeout=self._timeout, env=env, cwd=cwd,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            failure = GmgnTransportError("cli_timeout", ambiguous=submission,
                                         order_id=self._receipt_id(exc.output) if submission else None)
        except (FileNotFoundError, PermissionError):
            failure = GmgnTransportError("cli_unavailable")
        except Exception:
            failure = GmgnTransportError("cli_process_error", ambiguous=submission)
        # Raise outside except so captured subprocess output is not chained.
        if failure:
            raise failure
        if proc.returncode != 0:
            raise GmgnTransportError("cli_failed", ambiguous=submission,
                                     order_id=self._receipt_id(proc.stdout) if submission else None)
        try:
            payload = json.loads(proc.stdout, parse_float=Decimal,
                                 parse_constant=self._invalid_constant, object_pairs_hook=self._unique_object)
        except (ValueError, TypeError, RecursionError):
            failure = GmgnTransportError("invalid_cli_json", ambiguous=submission)
        if failure:
            raise failure
        if not isinstance(payload, dict):
            raise GmgnTransportError("invalid_cli_response", ambiguous=submission)
        return payload

    def _receipt_id(self, output):
        try:
            data = json.loads(output)
            order_id = data.get("order_id") if isinstance(data, dict) else None
            if isinstance(order_id, str) and _ID.fullmatch(order_id):
                if not any(secret and secret in order_id for secret in
                           (self._env.get("GMGN_API_KEY"), self._env.get("GMGN_PRIVATE_KEY"))):
                    return order_id
        except (TypeError, ValueError, RecursionError):
            pass
        return None

    @staticmethod
    def _invalid_constant(_value):
        raise ValueError("nonfinite_json_number")

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_field")
            result[key] = value
        return result

    @staticmethod
    def _pair(token, side, amount_atomic):
        token = _address(token)
        if token == NATIVE:
            raise GmgnTransportError("native_to_native_swap")
        if side not in ("buy", "sell"):
            raise GmgnTransportError("invalid_side")
        amount = _uint(amount_atomic, positive=True)
        return (NATIVE, token, amount) if side == "buy" else (token, NATIVE, amount)

    def _token_decimals(self, token: str) -> int:
        if token == NATIVE:
            return NATIVE_DECIMALS
        info = self._cli(["token", "info", "--chain", "bsc", "--address", token])
        if _address(info.get("address")) != token:
            raise GmgnTransportError("token_identity_mismatch")
        return int(_uint(info.get("decimals"), maximum=255))

    def market(self) -> dict:
        data = self._cli(["gas-price", "--chain", "bsc"])
        if data.get("chain", "bsc") != "bsc":
            raise GmgnTransportError("chain_mismatch")
        price = _decimal(data.get("native_token_usd_price"), positive=True)
        with localcontext() as context:
            context.prec = 220
            maximum = _uint(int(MAX_BUY_USD * 10**18 / price))
        gas_fields = ("auto", "auto_mev", "high", "average", "low", "suggest_base_fee",
                      "high_prio_fee", "average_prio_fee", "low_prio_fee",
                      "high_prio_fee_mixed", "average_prio_fee_mixed", "low_prio_fee_mixed",
                      "high_orign", "average_orign", "low_orign")
        gas = {field: _optional_number(data, field) for field in gas_fields if field in data}
        for field in ("last_block", "high_estimate_time", "average_estimate_time", "low_estimate_time"):
            if field in data:
                gas[field] = int(_uint(data[field]))
        return {"chain": "bsc", "native_token": NATIVE, "native_decimals": 18,
                "native_price_usd": format(price, "f"), "native_token_usd_price": format(price, "f"),
                "max_buy_amount_atomic": maximum, "max_buy_usd": "5", "gas": gas,
                "gas_price_unit": "wei", "other_provider_fees_usd": None}

    def quote(self, token, side, amount_atomic, slippage_percent=None) -> dict:
        input_token, output_token, amount = self._pair(token, side, amount_atomic)
        slippage = _slippage(self._quote_slippage if slippage_percent is None else slippage_percent)
        data = self._cli(["order", "quote", "--chain", "bsc", "--from", self.wallet_address,
                          "--input-token", input_token, "--output-token", output_token,
                          "--amount", amount, "--slippage", slippage], signed=True)
        if (_address(data.get("input_token")) != input_token
                or _address(data.get("output_token")) != output_token
                or _uint(data.get("input_amount"), positive=True) != amount):
            raise GmgnTransportError("quote_identity_mismatch")
        output = _uint(data.get("output_amount"), positive=True)
        minimum = _uint(data.get("min_output_amount"))
        if int(minimum) > int(output) or _slippage(data.get("slippage")) != slippage:
            raise GmgnTransportError("invalid_quote")
        return {"chain": "bsc", "side": side, "input_token": input_token, "output_token": output_token,
                "input_amount": amount, "output_amount": output, "min_output_amount": minimum,
                "input_decimals": 18 if side == "buy" else None,
                "output_decimals": None if side == "buy" else 18,
                "slippage_percent": int(slippage), "native_price_usd": None,
                "input_usd": None, "output_usd": None,
                "other_provider_fees_usd": None}

    def submit(self, token, side, amount_atomic, slippage_percent, *, deadline: float | None = None) -> dict:
        input_token, output_token, amount = self._pair(token, side, amount_atomic)
        slippage = _slippage(slippage_percent)
        if not self._enabled():
            raise GmgnTransportError("submission_disabled")
        self._credentials(signed=True)
        self._runner(check=True)
        if side == "buy" and int(amount) > int(self.market()["max_buy_amount_atomic"]):
            raise GmgnTransportError("buy_exceeds_5_usd")
        data = self._cli(["swap", "--chain", "bsc", "--from", self.wallet_address,
                          "--input-token", input_token, "--output-token", output_token,
                          "--amount", amount, "--slippage", slippage, "--anti-mev", "--yes"],
                         signed=True, submission=True, deadline=deadline)
        failure = None
        try:
            result = self._order_result(data)
            if result["input_token"] is not None and (
                    result["input_token"] != input_token or result["output_token"] != output_token):
                raise GmgnTransportError("fill_identity_mismatch")
        except GmgnTransportError:
            failure = GmgnTransportError("invalid_submission_receipt", ambiguous=True,
                                         order_id=self._receipt_id(json.dumps(data, default=str)))
        if failure:
            raise failure
        return self._enrich_order(result)

    def order(self, order_id) -> dict:
        order_id = _identifier(order_id)
        data = self._cli(["order", "get", "--chain", "bsc", "--order-id", order_id], signed=True)
        result = self._order_result(data)
        if result["order_id"] != order_id:
            raise GmgnTransportError("order_identity_mismatch", order_id=order_id)
        return self._enrich_order(result)

    @staticmethod
    def _order_result(data: dict) -> dict:
        order_id = _identifier(data.get("order_id"))
        if "chain" in data and data["chain"] != "bsc":
            raise GmgnTransportError("chain_mismatch")
        provider = data.get("status")
        if provider not in ("pending", "processed", "confirmed", "successful", "failed", "expired"):
            provider = "unknown"
        state = None if data.get("state") is None else int(_uint(data["state"]))
        status = {"processed": "pending", "expired": "failed", "successful": "confirmed"}.get(provider, provider)
        if provider == "successful" and state != 30:
            status = "unknown"
        if status == "confirmed" and state is not None and state != 30:
            status = "unknown"
        tx_hash = data.get("hash") or None
        if tx_hash is not None and (not isinstance(tx_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash)):
            raise GmgnTransportError("invalid_transaction_hash")
        tx_hash = tx_hash.lower() if tx_hash else None
        result = {"chain": "bsc", "order_id": order_id, "status": status,
                  "chain_source": "response" if "chain" in data else "request",
                  "wallet_address": None,
                  "created_at": None, "created_at_source": None,
                  "provider_status": provider, "state": state, "tx_hash": tx_hash,
                  "fill_complete": False, "other_provider_fees_usd": None,
                  "report": None}
        for field in ("input_amount", "output_amount", "input_decimals", "output_decimals",
                      "input_token", "output_token", "price", "price_usd", "gas_usd", "gas_native"):
            result[field] = None
        report = data.get("report")
        # A failed execution can still incur gas; it is not a successful fill.
        if isinstance(report, dict):
            for field in ("gas_usd", "gas_native"):
                result[field] = _optional_number(report, field)
        if status != "confirmed" or report is None:
            return result
        if not isinstance(report, dict):
            raise GmgnTransportError("invalid_execution_report")
        clean = {}
        for leg in ("input", "output"):
            clean[leg + "_token"] = _address(report.get(leg + "_token"))
            clean[leg + "_amount"] = _uint(report.get(leg + "_amount"), positive=True)
            clean[leg + "_token_decimals"] = int(_uint(report.get(leg + "_token_decimals"), maximum=255))
            result[leg + "_token"] = clean[leg + "_token"]
            result[leg + "_amount"] = clean[leg + "_amount"]
            result[leg + "_decimals"] = clean[leg + "_token_decimals"]
        if (result["input_token"] == NATIVE) == (result["output_token"] == NATIVE):
            raise GmgnTransportError("non_native_fill")
        native_leg = "input" if result["input_token"] == NATIVE else "output"
        if result[native_leg + "_decimals"] != 18:
            raise GmgnTransportError("invalid_native_decimals")
        for field in ("price", "price_usd", "gas_usd", "gas_native"):
            clean[field] = _optional_number(report, field)
        for field in ("quote_token", "base_token"):
            if field in report:
                clean[field] = _address(report[field])
        for field in ("quote_amount", "base_amount", "height", "order_height"):
            if field in report:
                clean[field] = _uint(report[field])
        for field in ("quote_decimals", "base_decimals"):
            if field in report:
                clean[field] = int(_uint(report[field], maximum=255))
        if report.get("swap_mode") in ("ExactIn", "ExactOut"):
            clean["swap_mode"] = report["swap_mode"]
        result.update({field: clean[field] for field in ("price", "price_usd", "gas_usd", "gas_native")})
        result.update(fill_complete=True, report=clean)
        return result

    def security(self, token) -> dict:
        token = _address(token)
        data = self._cli(["token", "security", "--chain", "bsc", "--address", token])
        if "address" in data and _address(data["address"]) != token:
            raise GmgnTransportError("token_identity_mismatch")
        result = {"chain": "bsc", "token": token}
        for field in ("is_honeypot", "open_source", "owner_renounced"):
            result[field] = _yes_no(data.get(field))
        for field in ("buy_tax", "sell_tax", "rug_ratio", "top_10_holder_rate", "dev_team_hold_rate",
                      "creator_balance_rate", "suspected_insider_hold_rate", "rat_trader_amount_rate",
                      "bundler_trader_amount_rate"):
            value = data.get(field)
            result[field] = None if value is None else format(_decimal(value, maximum=Decimal(1)), "f")
        result["is_wash_trading"] = data.get("is_wash_trading") if type(data.get("is_wash_trading")) is bool else None
        result["sniper_count"] = None if data.get("sniper_count") is None else int(_uint(data["sniper_count"]))
        result["safe"] = (result["is_honeypot"] is False
                          and result["rug_ratio"] is not None
                          and Decimal(result["rug_ratio"]) <= Decimal("0.3")
                          and all(result[k] is None or Decimal(result[k]) <= Decimal("0.05")
                                  for k in ("buy_tax", "sell_tax")))
        result["assessment_source"] = "local_execution_gate_v2"
        result["gate_description"] = (
            "Requires known is_honeypot=false and known rug_ratio<=0.3; "
            "buy_tax/sell_tax must be <=0.05 when known. Missing taxes are allowed. "
            "Owner renunciation, source verification, wash trading and holder concentration "
            "are informational; worker entry risk flags remain separate."
        )
        return result

    def balance(self, token=NATIVE) -> dict:
        token = _address(token)
        if self._env.get("BSC_RPC_URL"):
            return self._rpc_balance(token)
        cursor = None
        seen = set()
        for _ in range(100):
            args = ["portfolio", "holdings", "--chain", "bsc", "--wallet", self.wallet_address,
                    "--limit", "50", "--hide-abnormal", "false", "--hide-airdrop", "false", "--hide-closed", "false"]
            if cursor is not None:
                args.extend(["--cursor", cursor])
            data = self._cli(args)
            rows = data.get("holdings")
            if not isinstance(rows, list):
                raise GmgnTransportError("balance_unavailable")
            matches = [row for row in rows if isinstance(row, dict) and isinstance(row.get("token"), dict)
                       and str(row["token"].get("address", "")).lower() == token]
            if len(matches) > 1:
                raise GmgnTransportError("ambiguous_balance")
            if matches:
                decimals = self._token_decimals(token)
                value = _decimal(matches[0].get("balance"))
                with localcontext() as context:
                    context.prec = 500
                    atoms = value * Decimal(10)**decimals
                if atoms != atoms.to_integral_value():
                    raise GmgnTransportError("inexact_balance")
                return {"chain": "bsc", "wallet_address": self.wallet_address, "token": token,
                        "amount_atomic": _uint(int(atoms)), "decimals": decimals,
                        "source": "portfolio.holdings.balance"}
            cursor = data.get("next")
            if cursor in (None, ""):
                break
            if not isinstance(cursor, str) or len(cursor) > 512 or not re.fullmatch(r"[A-Za-z0-9_+/=.:~-]+", cursor) or cursor.startswith("-") or cursor in seen:
                raise GmgnTransportError("invalid_balance_cursor")
            seen.add(cursor)
        raise GmgnTransportError("balance_unavailable")

    def _rpc_url(self) -> str:
        value = self._env.get("BSC_RPC_URL", "")
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.fragment
            parsed.port
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in value):
            raise GmgnTransportError("invalid_rpc_url")
        return value

    def _rpc(self, method, params):
        # Reuse the existing JSON-RPC error boundary, injecting a read-only
        # subprocess transport. No signer or broadcaster send method is used.
        from alpha_bsc_live_adapter import JsonRpcBroadcaster

        allowed = {"eth_chainId", "eth_getBalance", "eth_call", "eth_getTransactionByHash", "eth_getBlockByNumber"}
        if method not in allowed:
            raise GmgnTransportError("rpc_method_not_allowed")
        node, cli = self._runner()
        env = dict(self._env)
        for key in ("GMGN_API_KEY", "GMGN_PRIVATE_KEY", "GMGN_DEBUG", "GMGN_LIVE_ENABLED",
                    "GMGN_ALLOW_AUTOMATED_TRADES", "NODE_OPTIONS", "NODE_DEBUG"):
            env.pop(key, None)

        def send(_url, payload, _timeout):
            args = [node, "--input-type=module", "-e", _RPC_REQUEST, json.dumps(payload, separators=(",", ":"))]
            return self._process(args, env, str(cli.parent))

        rpc = JsonRpcBroadcaster(self._rpc_url(), timeout_seconds=self._timeout, transport=send)
        data = rpc._call(method, params)
        if (not isinstance(data, dict) or data.get("jsonrpc") != "2.0"
                or type(data.get("id")) is not int or data["id"] != 1 or "result" not in data):
            raise GmgnTransportError("rpc_read_failed")
        return data["result"]

    @staticmethod
    def _hex_uint(value, *, abi=False):
        pattern = r"0x[0-9a-fA-F]{64}" if abi else r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})"
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise GmgnTransportError("invalid_rpc_quantity")
        return int(value, 16)

    def _rpc_chain(self):
        if self._hex_uint(self._rpc("eth_chainId", [])) != 56:
            raise GmgnTransportError("rpc_wrong_chain")

    def _rpc_balance(self, token):
        self._rpc_chain()
        if token == NATIVE:
            atoms = self._hex_uint(self._rpc("eth_getBalance", [self.wallet_address, "latest"]))
            decimals = 18
        else:
            call = {"to": token, "data": "0x70a08231" + self.wallet_address[2:].rjust(64, "0")}
            atoms = self._hex_uint(self._rpc("eth_call", [call, "latest"]), abi=True)
            decimals = self._hex_uint(self._rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), abi=True)
            if decimals > 255:
                raise GmgnTransportError("invalid_rpc_decimals")
        return {"chain": "bsc", "wallet_address": self.wallet_address, "token": token,
                "amount_atomic": str(atoms), "decimals": decimals, "source": "bsc_rpc"}

    def _transaction(self, tx_hash):
        if not isinstance(tx_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash):
            raise GmgnTransportError("invalid_transaction_hash")
        self._rpc_chain()
        tx = self._rpc("eth_getTransactionByHash", [tx_hash])
        if (not isinstance(tx, dict) or not isinstance(tx.get("hash"), str)
                or tx["hash"].lower() != tx_hash.lower()):
            raise GmgnTransportError("transaction_unavailable")
        _address(tx.get("from"), "invalid_transaction_sender")
        if "chainId" in tx and self._hex_uint(tx["chainId"]) != 56:
            raise GmgnTransportError("rpc_wrong_chain")
        return tx

    def transaction_sender(self, tx_hash) -> str:
        """Read-only chain-56 transaction sender for manual receipt attachment."""
        return _address(self._transaction(tx_hash)["from"])

    def _enrich_order(self, result):
        if not self._env.get("BSC_RPC_URL") or not result["tx_hash"]:
            return result
        try:
            tx = self._transaction(result["tx_hash"])
            result["wallet_address"] = _address(tx["from"])
            result["wallet_address_source"] = "eth_getTransactionByHash"
            if tx.get("blockNumber") is not None:
                height = self._hex_uint(tx["blockNumber"])
                block = self._rpc("eth_getBlockByNumber", [hex(height), False])
                if not isinstance(block, dict) or self._hex_uint(block.get("number")) != height:
                    raise GmgnTransportError("invalid_rpc_block")
                if (not isinstance(tx.get("blockHash"), str) or not isinstance(block.get("hash"), str)
                        or block["hash"].lower() != tx["blockHash"].lower()):
                    raise GmgnTransportError("rpc_block_mismatch")
                timestamp = self._hex_uint(block.get("timestamp"))
                result["created_at"] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                result["created_at_source"] = "execution_block_timestamp"
        except (GmgnTransportError, ValueError, OverflowError, OSError):
            result["rpc_verification_error"] = "receipt_rpc_unavailable"
        return result


__all__ = ["GmgnLiveTransport", "GmgnTransportError", "NATIVE", "NATIVE_DECIMALS"]
