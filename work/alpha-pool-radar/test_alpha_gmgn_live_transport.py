"""Offline contract tests: every subprocess is fake, including enabled swaps."""

import copy
import json
import subprocess
import traceback
from decimal import Decimal, localcontext

import pytest

from alpha_gmgn_live_transport import GmgnLiveTransport, GmgnTransportError, NATIVE


TOKEN = "0x" + "ab" * 20
WALLET = "0x" + "12" * 20
TX = "0x" + "de" * 32
# Test-only structural PEM. Real execution also validates the key with Node crypto.
PEM_LABEL = "PRIVATE KEY"
PEM = f"-----BEGIN {PEM_LABEL}-----\nMAMCAQA=\n-----END {PEM_LABEL}-----"
ENV = {"GMGN_API_KEY": "fake-api-secret", "GMGN_PRIVATE_KEY": PEM,
       "GMGN_WALLET_ADDRESS": WALLET, "GMGN_NODE_PATH": "node",
       "GMGN_TIMEOUT_SECONDS": "12"}


class FakeRun:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        assert isinstance(command, list)
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        if "const payload=JSON.parse" not in command[3]:
            assert command[-1] == "--raw"
        assert self.responses, "Unexpected subprocess call"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    def args(self, index=0):
        return self.calls[index][0][5:]


def transport(fake, **env):
    return GmgnLiveTransport({**ENV, "GMGN_LIVE_ENABLED": "1", **env}, run=fake)


def gas(price="600"):
    return {"chain": "bsc", "native_token_usd_price": price,
            "average": "50000000", "auto_mev": "60000000", "last_block": 999,
            "high_estimate_time": 1}


def test_slow_presubmit_market_cannot_dispatch_expired_order(monkeypatch):
    import time
    fake = FakeRun()
    live = transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1")
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def slow_market():
        clock[0] = 120.0
        return {"max_buy_amount_atomic": "5000000000000000"}
    monkeypatch.setattr(live, "market", slow_market)
    with pytest.raises(GmgnTransportError, match="deadline_expired") as err:
        live.submit(TOKEN, "buy", "5000000000000000", 5, deadline=110.0)
    assert not err.value.ambiguous
    assert fake.calls == []


def quote_payload(side="buy", amount="1000000000000000", output="999000"):
    return {"input_token": NATIVE if side == "buy" else TOKEN,
            "output_token": TOKEN if side == "buy" else NATIVE,
            "input_amount": amount, "output_amount": output,
            "min_output_amount": str(int(output) // 2), "slippage": 1}


def receipt(status="successful", state=30, side="buy"):
    return {"order_id": "order-123", "hash": TX, "status": status, "state": state,
            "report": {"input_token": NATIVE if side == "buy" else TOKEN,
                       "output_token": TOKEN if side == "buy" else NATIVE,
                       "input_amount": "1000000000000000", "output_amount": "987654321012345678901",
                       "input_token_decimals": 18 if side == "buy" else 6,
                       "output_token_decimals": 6 if side == "buy" else 18,
                       "quote_token": NATIVE, "quote_decimals": 18,
                       "quote_amount": "1000000000000000", "base_token": TOKEN,
                       "base_decimals": 6, "base_amount": "987654321012345678901",
                       "price": "0.000002", "price_usd": "0.0012", "gas_native": "0.00001",
                       "gas_usd": "0.006", "height": 999, "order_height": 998, "swap_mode": "ExactIn"}}


@pytest.fixture(autouse=True)
def forbid_real_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Real subprocess is forbidden in transport tests")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def test_construct_is_inert_and_submission_defaults_off():
    fake = FakeRun()
    live = transport(fake)
    assert live.wallet_address == WALLET
    with pytest.raises(GmgnTransportError, match="submission_disabled") as err:
        live.submit(TOKEN, "buy", "100", 1)
    assert not err.value.ambiguous
    assert not fake.calls


@pytest.mark.parametrize("opt_in", ["", "0", "true", "yes", " 1", "1 "])
def test_only_exact_opt_in_works(opt_in):
    fake = FakeRun()
    with pytest.raises(GmgnTransportError, match="submission_disabled"):
        transport(fake, GMGN_ALLOW_AUTOMATED_TRADES=opt_in).submit(TOKEN, "sell", "100", 1)
    assert not fake.calls


def test_market_official_native_price_and_floor_are_exact():
    fake = FakeRun(gas("601.123456789"))
    result = transport(fake).market()
    with localcontext() as context:
        context.prec = 100
        atoms = Decimal(result["max_buy_amount_atomic"])
        price = Decimal(result["native_price_usd"])
        assert atoms * price / 10**18 <= 5
        assert (atoms + 1) * price / 10**18 > 5
    assert result["gas"]["average"] == "50000000"
    assert result["gas_price_unit"] == "wei"
    assert result["other_provider_fees_usd"] is None
    assert fake.args() == ["gas-price", "--chain", "bsc", "--raw"]
    assert "GMGN_PRIVATE_KEY" not in fake.calls[0][1]["env"]


@pytest.mark.parametrize("price", [None, "0", "NaN", "Infinity", "-1", True, "1e999", {}, []])
def test_market_rejects_unusable_prices(price):
    with pytest.raises(GmgnTransportError):
        transport(FakeRun(gas(price))).market()


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_quote_official_schema_exact_amounts_and_optional_decimals(side):
    payload = quote_payload(side=side)
    fake = FakeRun(payload)
    result = transport(fake).quote(TOKEN, side, payload["input_amount"], 1)
    for field in ("input_token", "output_token", "input_amount", "output_amount", "min_output_amount"):
        assert result[field] == payload[field]
    assert result["input_decimals"] == (18 if side == "buy" else None)
    assert result["output_decimals"] == (None if side == "buy" else 18)
    assert result["native_price_usd"] is None and result["input_usd"] is None
    assert len(fake.calls) == 1
    assert fake.args() == ["order", "quote", "--chain", "bsc", "--from", WALLET,
                           "--input-token", payload["input_token"], "--output-token", payload["output_token"],
                           "--amount", payload["input_amount"], "--slippage", "1", "--raw"]


def test_quote_vendor_requires_local_pem_even_though_endpoint_is_unsigned():
    fake = FakeRun()
    with pytest.raises(GmgnTransportError, match="api_signing_pem_required"):
        transport(fake, GMGN_PRIVATE_KEY="").quote(TOKEN, "buy", "1", 1)
    assert not fake.calls


@pytest.mark.parametrize("amount", [0, -1, True, False, None, 1.1, 1.0, float("nan"),
                                   float("inf"), "NaN", "1e18", " 1", "1 ", "+1", "1.0", "١", "１", "1;foo", 2**256])
def test_reject_bad_atoms_before_subprocess(amount):
    fake = FakeRun()
    with pytest.raises(GmgnTransportError):
        transport(fake).quote(TOKEN, "buy", amount, 1)
    assert not fake.calls


@pytest.mark.parametrize("slippage", [True, "NaN", "Infinity", -1, 101, "1.5", "1;echo", "auto", " 1"])
def test_reject_bad_slippage_before_subprocess(slippage):
    fake = FakeRun()
    with pytest.raises(GmgnTransportError):
        transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1").submit(TOKEN, "sell", "1", slippage)
    assert not fake.calls


@pytest.mark.parametrize("value", [None, "", NATIVE, "0x123", "0x" + "ab" * 20 + ";echo", "So11111111111111111111111111111111111111112"])
def test_reject_bad_wallet(value):
    env = dict(ENV)
    if value is None:
        env.pop("GMGN_WALLET_ADDRESS")
    else:
        env["GMGN_WALLET_ADDRESS"] = value
    with pytest.raises(GmgnTransportError):
        GmgnLiveTransport(env, FakeRun())


@pytest.mark.parametrize(
    "key",
    ["", "0x" + "ab" * 32, "ab" * 32,
     f"-----BEGIN EC {PEM_LABEL}-----\nMAMCAQA=\n-----END EC {PEM_LABEL}-----"],
)
def test_wallet_keys_not_accepted_as_api_signing_pem(key):
    fake = FakeRun()
    with pytest.raises(GmgnTransportError, match="api_signing_pem_required"):
        transport(fake, GMGN_PRIVATE_KEY=key, GMGN_ALLOW_AUTOMATED_TRADES="1").submit(TOKEN, "sell", 1, 0)
    assert not fake.calls


def test_submit_buy_budget_is_dynamic_and_has_no_condition_orders():
    fake = FakeRun(gas("1000"), {"order_id": "order-123", "status": "pending"})
    live = transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1", GMGN_DEBUG="1", NODE_OPTIONS="secret-options")
    result = live.submit(TOKEN, "buy", "5000000000000000", 1)
    assert result["status"] == "pending" and result["input_amount"] is None
    args = fake.args(1)
    assert args == ["swap", "--chain", "bsc", "--from", WALLET, "--input-token", NATIVE,
                    "--output-token", TOKEN, "--amount", "5000000000000000", "--slippage", "1",
                    "--anti-mev", "--yes", "--raw"]
    for command, kwargs in fake.calls:
        assert ENV["GMGN_API_KEY"] not in " ".join(command)
        assert PEM not in " ".join(command)
        assert "GMGN_DEBUG" not in kwargs["env"]
        assert "NODE_OPTIONS" not in kwargs["env"]
        assert kwargs["timeout"] == 12
    assert fake.calls[1][1]["env"]["GMGN_ALLOW_AUTOMATED_TRADES"] == "1"
    assert "GMGN_ALLOW_AUTOMATED_TRADES" not in fake.calls[0][1]["env"]


def test_submit_rejects_one_atom_over_five_usd_before_swap():
    fake = FakeRun(gas("1000"))
    with pytest.raises(GmgnTransportError, match="buy_exceeds_5_usd") as err:
        transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1").submit(TOKEN, "buy", "5000000000000001", 1)
    assert not err.value.ambiguous and len(fake.calls) == 1


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_order_report_is_actual_atomic_fill_with_token_usd_and_gas(side):
    data = receipt(side=side)
    fake = FakeRun(data)
    result = transport(fake).order("order-123")
    assert result["status"] == "confirmed" and result["fill_complete"]
    assert result["input_amount"] == data["report"]["input_amount"]
    assert result["output_amount"] == "987654321012345678901"
    assert result["output_decimals"] == (6 if side == "buy" else 18)
    assert result["price_usd"] == "0.0012"
    assert result["price"] == "0.000002"
    assert result["gas_usd"] == "0.006" and result["gas_native"] == "0.00001"
    assert result["tx_hash"] == TX and result["wallet_address"] is None
    assert result["chain_source"] == "request"
    assert result["report"]["base_amount"] == "987654321012345678901"
    assert result["other_provider_fees_usd"] is None


@pytest.mark.parametrize("status,state,expected", [("pending", 10, "pending"), ("processed", 20, "pending"),
                                                  ("failed", 40, "failed"), ("expired", 40, "failed"),
                                                  ("future_status", 99, "unknown"), ("successful", 20, "unknown")])
def test_nonfinal_or_failed_reports_are_never_used_as_fills(status, state, expected):
    result = transport(FakeRun(receipt(status, state))).order("order-123")
    assert result["status"] == expected
    assert result["report"] is None and result["input_amount"] is None
    assert not result["fill_complete"]


def test_confirmed_without_report_retains_unknown_actuals():
    result = transport(FakeRun({"order_id": "order-123", "status": "confirmed"})).order("order-123")
    assert result["status"] == "confirmed"
    assert not result["fill_complete"] and result["output_amount"] is None


@pytest.mark.parametrize("field,value", [("input_token_decimals", True), ("output_token_decimals", 256),
                                       ("input_token_decimals", 17), ("input_amount", "1.5"),
                                       ("gas_usd", "NaN"), ("gas_native", -1), ("price_usd", "Infinity")])
def test_malformed_actual_fills_fail_closed(field, value):
    data = receipt()
    data["report"][field] = value
    with pytest.raises(GmgnTransportError):
        transport(FakeRun(data)).order("order-123")


@pytest.mark.parametrize("operation", ["order", "submit"])
def test_receipt_chain_mismatch(operation):
    data = receipt(side="sell")
    data["chain"] = "eth"
    live = transport(FakeRun(data), GMGN_ALLOW_AUTOMATED_TRADES="1")
    with pytest.raises(GmgnTransportError) as err:
        live.order("order-123") if operation == "order" else live.submit(TOKEN, "sell", 1, 1)
    assert err.value.ambiguous == (operation == "submit")


@pytest.mark.parametrize("failure,code", [
    (subprocess.TimeoutExpired(["secret-command"], 12, output=PEM, stderr="fake-api-secret"), "cli_timeout"),
    (subprocess.CalledProcessError(1, "secret-command", output=PEM), "cli_process_error"),
    (subprocess.CompletedProcess([], 1, PEM, "429 fake-api-secret"), "cli_failed"),
    (subprocess.CompletedProcess([], 0, "not-json " + PEM, ""), "invalid_cli_json"),
    (subprocess.CompletedProcess([], 0, "{\"code\":429,\"error\":\"fake-api-secret\"}", ""), "invalid_cli_response"),
])
def test_submit_failure_is_ambiguous_sanitized_and_never_retried(failure, code):
    fake = FakeRun(failure)
    with pytest.raises(GmgnTransportError) as err:
        transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1").submit(TOKEN, "sell", 1, 1)
    assert err.value.code == code and err.value.ambiguous
    assert len(fake.calls) == 1
    rendered = "".join(traceback.format_exception(err.type, err.value, err.tb))
    assert PEM not in rendered and "fake-api-secret" not in rendered
    assert err.value.__context__ is None


def test_invalid_submission_receipt_preserves_reconciliation_order_id():
    data = receipt(side="sell")
    data["report"]["output_amount"] = "NaN"
    with pytest.raises(GmgnTransportError) as err:
        transport(FakeRun(data), GMGN_ALLOW_AUTOMATED_TRADES="1").submit(TOKEN, "sell", 1, 1)
    assert err.value.ambiguous and err.value.order_id == "order-123"


def test_order_id_validation_and_response_identity():
    fake = FakeRun({"order_id": "different", "status": "pending"})
    with pytest.raises(GmgnTransportError, match="invalid_order_id"):
        transport(fake).order("--yes")
    assert not fake.calls
    with pytest.raises(GmgnTransportError, match="order_identity_mismatch"):
        transport(fake).order("order-123")


def test_security_uses_documented_yes_no_and_ratio_units():
    safe = {"is_honeypot": "no", "open_source": "yes", "owner_renounced": "yes",
            "buy_tax": "0.03", "sell_tax": 0, "rug_ratio": 0.01,
            "top_10_holder_rate": 0.2, "is_wash_trading": False}
    data = transport(FakeRun(safe)).security(TOKEN)
    assert data["safe"] and data["buy_tax"] == "0.03"
    assert data["is_honeypot"] is False
    for field, value in [("is_honeypot", "yes"), ("is_honeypot", "unknown"), ("sell_tax", "0.2")]:
        assert not transport(FakeRun({**safe, field: value})).security(TOKEN)["safe"]
    assert transport(FakeRun({})).security(TOKEN)["safe"] is False


@pytest.mark.parametrize("rug_ratio", ["0", "0.1", "0.100001", "0.25", "0.3"])
def test_security_minimal_critical_fields_are_sufficient(rug_ratio):
    result = transport(FakeRun({"is_honeypot": "no", "rug_ratio": rug_ratio})).security(TOKEN)
    assert result["safe"] is True
    assert result["buy_tax"] is None and result["sell_tax"] is None
    assert result["owner_renounced"] is None and result["top_10_holder_rate"] is None
    assert result["assessment_source"] == "local_execution_gate_v2"
    assert "known rug_ratio<=0.3" in result["gate_description"]
    assert "Missing taxes are allowed" in result["gate_description"]


@pytest.mark.parametrize("fields", [
    {"is_honeypot": "unknown", "rug_ratio": "0"},
    {"is_honeypot": "yes", "rug_ratio": "0"},
    {"rug_ratio": "0"}, {"is_honeypot": "no"},
    {"is_honeypot": "no", "rug_ratio": None},
    {"is_honeypot": "no", "rug_ratio": "0.300000000000000001"},
])
def test_security_requires_known_critical_fields_with_rug_ceiling(fields):
    assert transport(FakeRun(fields)).security(TOKEN)["safe"] is False


@pytest.mark.parametrize("field", ["buy_tax", "sell_tax"])
@pytest.mark.parametrize("value,expected", [(None, True), ("0", True), ("0.05", True),
                                           ("0.050000000000000001", False)])
def test_security_checks_tax_only_when_known(field, value, expected):
    fields = {"is_honeypot": "no", "rug_ratio": "0.3", field: value}
    assert transport(FakeRun(fields)).security(TOKEN)["safe"] is expected


def test_security_informational_risks_remain_exposed_without_extra_entry_gates():
    fields = {"is_honeypot": "no", "rug_ratio": "0.3", "owner_renounced": "no",
              "open_source": "no", "is_wash_trading": True, "top_10_holder_rate": "0.99"}
    result = transport(FakeRun(fields)).security(TOKEN)
    assert result["safe"] is True
    assert result["owner_renounced"] is False and result["open_source"] is False
    assert result["is_wash_trading"] is True and result["top_10_holder_rate"] == "0.99"


def test_balance_native_human_to_atomic_and_missing_is_not_zero():
    fake = FakeRun({"holdings": [{"token": {"address": NATIVE}, "balance": "0.123456789012345678"}]})
    result = transport(fake).balance()
    assert result["amount_atomic"] == "123456789012345678" and result["decimals"] == 18
    with pytest.raises(GmgnTransportError, match="balance_unavailable"):
        transport(FakeRun({"holdings": []})).balance()


def test_balance_token_pagination_and_exact_decimals():
    fake = FakeRun({"holdings": [], "next": "page2"},
                   {"holdings": [{"token": {"address": TOKEN}, "balance": "1.234567"}]},
                   {"address": TOKEN, "decimals": 6})
    result = transport(fake).balance(TOKEN)
    assert result["amount_atomic"] == "1234567" and result["decimals"] == 6
    assert "page2" in fake.args(1)
    assert fake.args(2) == ["token", "info", "--chain", "bsc", "--address", TOKEN, "--raw"]


def test_balance_never_rounds_exit_quantity_up_or_loops_cursors():
    fake = FakeRun({"holdings": [{"token": {"address": TOKEN}, "balance": "0.0000001"}]},
                   {"address": TOKEN, "decimals": 6})
    with pytest.raises(GmgnTransportError, match="inexact_balance"):
        transport(fake).balance(TOKEN)
    fake = FakeRun({"holdings": [], "next": "same"}, {"holdings": [], "next": "same"})
    with pytest.raises(GmgnTransportError, match="invalid_balance_cursor"):
        transport(fake).balance()


def test_cli_json_is_strict_not_a_guessed_http_envelope():
    for raw in ['{"code":0,"data":{}}', '{"holdings":[],"holdings":[]}', '[]', 'null',
                '{"holdings":[],"number":NaN}', '{"holdings":[],"number":Infinity}']:
        with pytest.raises(GmgnTransportError):
            transport(FakeRun(subprocess.CompletedProcess([], 0, raw, ""))).balance()


def test_mutable_environment_is_snapshotted_and_repr_contains_no_keys():
    env = copy.deepcopy(ENV)
    fake = FakeRun()
    live = GmgnLiveTransport(env, fake)
    env["GMGN_ALLOW_AUTOMATED_TRADES"] = "1"
    with pytest.raises(GmgnTransportError, match="submission_disabled"):
        live.submit(TOKEN, "sell", 1, 1)
    assert ENV["GMGN_API_KEY"] not in repr(live)


@pytest.mark.parametrize("timeout", ["NaN", "inf", "0", "-1", "301"])
def test_timeout_must_be_finite_positive_and_bounded(timeout):
    with pytest.raises(GmgnTransportError):
        transport(FakeRun(), GMGN_TIMEOUT_SECONDS=timeout)


@pytest.mark.parametrize("enabled,allowed", [("", "1"), ("1", ""), ("true", "1"), ("1", "true")])
def test_both_live_gates_required(enabled, allowed):
    fake = FakeRun()
    with pytest.raises(GmgnTransportError, match="submission_disabled"):
        transport(fake, GMGN_LIVE_ENABLED=enabled, GMGN_ALLOW_AUTOMATED_TRADES=allowed).submit(TOKEN, "sell", 1, 1)
    assert not fake.calls


def test_preflight_is_offline_and_does_not_claim_binding_verification():
    fake = FakeRun()
    result = transport(fake).preflight()
    assert result["configured"] and result["offline"] and not result["live_enabled"]
    assert not result["credentials_verified"] and not result["wallet_binding_verified"]
    assert not fake.calls
    with pytest.raises(GmgnTransportError, match="api_signing_pem_required"):
        transport(fake, GMGN_PRIVATE_KEY="").preflight()
    with pytest.raises(GmgnTransportError, match="cli_unavailable"):
        GmgnLiveTransport(
            {**ENV, "GMGN_NODE_PATH": "nonexistent-gmgn-node-executable"}
        ).preflight()


def rpc(result):
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def test_rpc_native_balance_is_exact_and_checks_chain_before_read():
    fake = FakeRun(rpc("0x38"), rpc(hex(12345678901234567890)))
    result = transport(fake, BSC_RPC_URL="https://rpc.example/fake-secret").balance()
    assert result["amount_atomic"] == "12345678901234567890" and result["decimals"] == 18
    methods = [json.loads(command[4])["method"] for command, _ in fake.calls]
    assert methods == ["eth_chainId", "eth_getBalance"]
    for command, kwargs in fake.calls:
        assert "fake-secret" not in " ".join(command)
        assert "GMGN_API_KEY" not in kwargs["env"] and "GMGN_PRIVATE_KEY" not in kwargs["env"]


def test_rpc_token_balance_abi_and_decimals_are_exact():
    fake = FakeRun(rpc("0x38"), rpc("0x" + format(1234567, "064x")), rpc("0x" + format(6, "064x")))
    result = transport(fake, BSC_RPC_URL="https://rpc.example").balance(TOKEN)
    assert result["amount_atomic"] == "1234567" and result["decimals"] == 6
    payload = json.loads(fake.calls[1][0][4])
    assert payload["method"] == "eth_call"
    assert payload["params"] == [{"to": TOKEN, "data": "0x70a08231" + WALLET[2:].rjust(64, "0")}, "latest"]


def test_rpc_wrong_chain_and_empty_abi_fail_closed():
    fake = FakeRun(rpc("0x1"))
    with pytest.raises(GmgnTransportError, match="rpc_wrong_chain"):
        transport(fake, BSC_RPC_URL="https://rpc.example").balance()
    assert len(fake.calls) == 1
    with pytest.raises(GmgnTransportError, match="invalid_rpc_quantity"):
        transport(FakeRun(rpc("0x38"), rpc("0x")), BSC_RPC_URL="https://rpc.example").balance(TOKEN)


def test_failed_report_preserves_gas_without_using_failed_amounts():
    result = transport(FakeRun(receipt("failed", 40))).order("order-123")
    assert result["status"] == "failed" and result["input_amount"] is None
    assert result["gas_usd"] == "0.006" and result["gas_native"] == "0.00001"


def test_order_rpc_verifies_sender_and_uses_original_block_time():
    block_hash = "0x" + "34" * 32
    tx = {"hash": TX, "from": WALLET, "chainId": "0x38", "blockNumber": "0x7", "blockHash": block_hash}
    block = {"number": "0x7", "hash": block_hash, "timestamp": "0x6553f100"}
    fake = FakeRun(receipt(), rpc("0x38"), rpc(tx), rpc(block))
    result = transport(fake, BSC_RPC_URL="https://rpc.example").order("order-123")
    assert result["wallet_address"] == WALLET
    assert result["created_at"] == "2023-11-14T22:13:20+00:00"
    assert result["created_at_source"] == "execution_block_timestamp"


def test_rpc_failure_does_not_discard_known_submitted_order():
    fake = FakeRun(receipt(side="sell"), rpc("0x1"))
    result = transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1", BSC_RPC_URL="https://rpc.example").submit(TOKEN, "sell", 1, 1)
    assert result["order_id"] == "order-123" and result["status"] == "confirmed"
    assert result["rpc_verification_error"] == "receipt_rpc_unavailable"


def test_manual_recovery_sender_read_only():
    fake = FakeRun(rpc("0x38"), rpc({"hash": TX, "from": WALLET}))
    assert transport(fake, BSC_RPC_URL="https://rpc.example").transaction_sender(TX) == WALLET


def test_prelaunch_failure_is_definitely_not_submitted():
    fake = FakeRun(FileNotFoundError("private path"))
    with pytest.raises(GmgnTransportError) as err:
        transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1").submit(TOKEN, "sell", 1, 1)
    assert not err.value.ambiguous


def test_transaction_hash_is_canonical_lowercase():
    data = receipt()
    data["hash"] = "0x" + "DE" * 32
    assert transport(FakeRun(data)).order("order-123")["tx_hash"] == TX


@pytest.mark.parametrize("timeout", [True, False])
def test_ambiguous_subprocess_error_preserves_known_order_id(timeout):
    output = json.dumps({"order_id": "known-order", "status": "pending"})
    response = (subprocess.TimeoutExpired([], 12, output=output) if timeout else
                subprocess.CompletedProcess([], 1, output, "secret-error"))
    fake = FakeRun(response)
    with pytest.raises(GmgnTransportError) as err:
        transport(fake, GMGN_ALLOW_AUTOMATED_TRADES="1").submit(TOKEN, "sell", 1, 1)
    assert err.value.ambiguous and err.value.order_id == "known-order"
    assert len(fake.calls) == 1
