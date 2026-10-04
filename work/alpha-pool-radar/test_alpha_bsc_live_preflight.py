from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alpha_bsc_live_preflight as preflight
from alpha_okx_swap import OkxSwapClient


WALLET = "0x" + "1" * 40
ROUTER = "0x" + "2" * 40
SELECTOR = "0x12345678"
SECRET = "do-not-echo-private-key"


class FakeOkxClient:
    def build_swap_bsc_with_context(self, **kwargs: Any) -> dict[str, Any]:
        return {}


class FakeTransport:
    def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("preflight must not call OKX transport")


def complete_env() -> dict[str, str]:
    return {
        "BSC_PRIVATE_KEY": SECRET,
        "BSC_RPC_URL": "http://example.invalid/rpc",
        "BSC_WALLET_ADDRESS": WALLET,
        "BSC_ALLOWED_ROUTER_ADDRESSES": ROUTER,
        "BSC_OKX_SWAP_SELECTOR": SELECTOR,
        "LIVE_TRADING_ENABLED": "true",
        "ARMED": "true",
        "BSC_LIVE_BUY_AMOUNT_ATOMIC": "5000000000000000",
        "BSC_LIVE_SLIPPAGE_PERCENT": "3",
    }


def test_current_empty_environment_is_not_ready_and_has_machine_reasons():
    result = preflight.run_preflight(
        env={}, eth_account_available=False, okx_client=object()
    )

    assert result["ready"] is False
    assert "BSC_PRIVATE_KEY" in result["missing"]
    assert "BSC_RPC_URL" in result["missing"]
    assert result["checks"]["eth_account"]["ok"] is False
    assert result["checks"]["OKX_SWAP_BUILDER"]["ok"] is False
    json.dumps(result)


def test_secret_presence_is_checked_without_echoing_secret_value():
    env = complete_env()
    result = preflight.run_preflight(
        env=env, eth_account_available=False, okx_client=FakeOkxClient()
    )

    rendered = json.dumps(result, sort_keys=True)
    assert SECRET not in rendered
    assert result["checks"]["BSC_PRIVATE_KEY"]["ok"] is True


def test_empty_private_key_is_not_ready_without_echoing_value():
    env = complete_env()
    env["BSC_PRIVATE_KEY"] = ""
    result = preflight.run_preflight(
        env=env, eth_account_available=True, okx_client=FakeOkxClient()
    )

    assert result["ready"] is False
    assert result["checks"]["BSC_PRIVATE_KEY"] == {"ok": False, "reason": "missing"}
    assert SECRET not in json.dumps(result)


def test_complete_configuration_is_ready():
    result = preflight.run_preflight(
        env=complete_env(), eth_account_available=True, okx_client=FakeOkxClient()
    )

    assert result["ready"] is True
    assert result["missing"] == []
    assert result["reasons"] == []
    assert all(item["ok"] for item in result["checks"].values())


@pytest.mark.parametrize(
    ("field", "value", "check", "reason"),
    [
        ("LIVE_TRADING_ENABLED", "false", "LIVE_TRADING_ENABLED", "not_true"),
        ("ARMED", "0", "ARMED", "not_true"),
        ("BSC_WALLET_ADDRESS", "bsc-wallet", "BSC_WALLET_ADDRESS", "invalid_address"),
        ("BSC_RPC_URL", "", "BSC_RPC_URL", "missing"),
        ("BSC_RPC_URL", "ftp://example.invalid/rpc", "BSC_RPC_URL", "invalid_url"),
        ("BSC_RPC_URL", "https:///rpc", "BSC_RPC_URL", "invalid_url"),
        ("BSC_RPC_URL", "http://[", "BSC_RPC_URL", "invalid_url"),
        ("BSC_ALLOWED_ROUTER_ADDRESSES", "not-an-address", "BSC_ALLOWED_ROUTER_ADDRESSES", "invalid_address"),
        ("BSC_OKX_SWAP_SELECTOR", "0x1234", "BSC_OKX_SWAP_SELECTOR", "invalid_selector"),
        ("BSC_LIVE_BUY_AMOUNT_ATOMIC", "0", "BSC_LIVE_BUY_AMOUNT_ATOMIC", "invalid_amount"),
        ("BSC_LIVE_SLIPPAGE_PERCENT", "51", "BSC_LIVE_SLIPPAGE_PERCENT", "invalid_slippage"),
    ],
)
def test_configuration_failures_keep_ready_false(
    field: str, value: str, check: str, reason: str
):
    env = complete_env()
    env[field] = value

    result = preflight.run_preflight(
        env=env, eth_account_available=True, okx_client=FakeOkxClient()
    )

    assert result["ready"] is False
    assert result["checks"][check] == {"ok": False, "reason": reason}


def test_missing_okx_builder_and_eth_dependency_are_independent_failures():
    result = preflight.run_preflight(
        env=complete_env(), eth_account_available=False, okx_client=object()
    )

    assert result["ready"] is False
    assert result["checks"]["eth_account"] == {"ok": False, "reason": "missing"}
    assert result["checks"]["OKX_SWAP_BUILDER"] == {"ok": False, "reason": "missing"}


def test_actual_okx_swap_client_exposes_dynamic_calldata_support():
    client = OkxSwapClient(FakeTransport(), {})

    result = preflight.run_preflight(
        env=complete_env(), eth_account_available=True, okx_client=client
    )

    assert result["ready"] is True
    assert result["checks"]["OKX_SWAP_BUILDER"] == {"ok": True}


def test_eth_account_check_requires_imported_module_with_account(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(preflight, "_load_eth_account", lambda: type("Module", (), {"Account": object})())
    assert preflight._eth_account_available() is True

    monkeypatch.setattr(preflight, "_load_eth_account", lambda: type("Module", (), {})())
    assert preflight._eth_account_available() is False


def test_json_cli_emits_json_without_secret(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(preflight, "_eth_account_available", lambda: True)
    monkeypatch.setattr(preflight, "_load_okx_client_type", lambda: FakeOkxClient)

    exit_code = preflight.main(["--json"], env=complete_env())
    output = capsys.readouterr().out
    result = json.loads(output)

    assert exit_code == 0
    assert result["ready"] is True
    assert SECRET not in output


def test_default_cli_failure_exit_is_still_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(preflight, "_eth_account_available", lambda: False)
    monkeypatch.setattr(preflight, "_load_okx_client_type", lambda: None)

    exit_code = preflight.main([], env={})
    output = capsys.readouterr().out

    assert exit_code == 1
    assert json.loads(output)["ready"] is False
