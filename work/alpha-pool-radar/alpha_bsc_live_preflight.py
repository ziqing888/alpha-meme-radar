"""Offline, read-only preflight checks for the BSC live adapter."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit


CHAIN_ID_BSC = 56
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SELECTOR_RE = re.compile(r"^0x[0-9a-fA-F]{8}$")

PRIVATE_KEY_ENV = "BSC_PRIVATE_KEY"
RPC_URL_ENV = "BSC_RPC_URL"
WALLET_ENV = "BSC_WALLET_ADDRESS"
ROUTERS_ENV = "BSC_ALLOWED_ROUTER_ADDRESSES"
SELECTOR_ENV = "BSC_OKX_SWAP_SELECTOR"
LIVE_ENV = "LIVE_TRADING_ENABLED"
ARMED_ENV = "ARMED"
BUY_AMOUNT_ENV = "BSC_LIVE_BUY_AMOUNT_ATOMIC"
SLIPPAGE_ENV = "BSC_LIVE_SLIPPAGE_PERCENT"


def _load_okx_client_type() -> Any | None:
    try:
        from alpha_okx_swap import OkxSwapClient
    except Exception:
        return None
    return OkxSwapClient


def _load_eth_account() -> Any | None:
    try:
        import eth_account  # type: ignore
    except Exception:
        return None
    return eth_account


def _eth_account_available() -> bool:
    module = _load_eth_account()
    return module is not None and hasattr(module, "Account")


def _text(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    return value.strip() if isinstance(value, str) else ""


def _check(ok: bool, reason: str | None = None) -> dict[str, Any]:
    return {"ok": bool(ok), "reason": reason} if not ok else {"ok": True}


def _flag_check(values: Mapping[str, Any], name: str, false_reason: str) -> tuple[dict[str, Any], bool]:
    if name not in values:
        return _check(False, "missing"), True
    if _text(values, name).lower() != "true":
        return _check(False, false_reason), False
    return _check(True), False


def _router_check(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    raw = _text(values, ROUTERS_ENV)
    if not raw:
        return _check(False, "missing"), True
    addresses = [item.strip() for item in raw.split(",") if item.strip()]
    if not addresses or not all(ADDRESS_RE.fullmatch(item) for item in addresses):
        return _check(False, "invalid_address"), False
    return _check(True), False


def _selector_check(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if SELECTOR_ENV not in values or not _text(values, SELECTOR_ENV):
        return _check(False, "missing"), True
    return _check(bool(SELECTOR_RE.fullmatch(_text(values, SELECTOR_ENV))), "invalid_selector"), False


def _positive_integer_check(values: Mapping[str, Any], name: str) -> tuple[dict[str, Any], bool]:
    if name not in values or not _text(values, name):
        return _check(False, "missing"), True
    value = _text(values, name)
    return _check(value.isdigit() and int(value) > 0, "invalid_amount"), False


def _slippage_check(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if SLIPPAGE_ENV not in values or not _text(values, SLIPPAGE_ENV):
        return _check(False, "missing"), True
    value = _text(values, SLIPPAGE_ENV)
    try:
        valid = bool(re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", value)) and 0 <= float(value) <= 50
    except (TypeError, ValueError):
        valid = False
    return _check(valid, "invalid_slippage"), False


def _address_check(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if WALLET_ENV not in values or not _text(values, WALLET_ENV):
        return _check(False, "missing"), True
    return _check(bool(ADDRESS_RE.fullmatch(_text(values, WALLET_ENV))), "invalid_address"), False


def _rpc_check(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if RPC_URL_ENV not in values or not _text(values, RPC_URL_ENV):
        return _check(False, "missing"), True
    try:
        parsed = urlsplit(_text(values, RPC_URL_ENV))
    except Exception:
        return _check(False, "invalid_url"), False
    valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    return _check(valid, "invalid_url"), False


def _private_key_check(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if PRIVATE_KEY_ENV not in values:
        return _check(False, "missing"), True
    value = values.get(PRIVATE_KEY_ENV)
    if not isinstance(value, str) or not value.strip():
        return _check(False, "missing"), True
    return _check(True), False


def _okx_builder_check(okx_client: Any | None) -> dict[str, Any]:
    candidate = okx_client if okx_client is not None else _load_okx_client_type()
    if not callable(getattr(candidate, "build_swap_bsc_with_context", None)):
        return _check(False, "missing")
    if getattr(candidate, "supports_dynamic_calldata", None) is False:
        return _check(False, "dynamic_calldata_unsupported")
    return _check(True)


def run_preflight(
    *,
    env: Mapping[str, Any] | None = None,
    eth_account_available: bool | None = None,
    okx_client: Any | None = None,
) -> dict[str, Any]:
    """Return JSON-safe checks without reading secret values or making requests."""
    values = os.environ if env is None else env
    checks: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    reasons: list[str] = []

    def add(name: str, result: dict[str, Any], is_missing: bool = False) -> None:
        checks[name] = result
        if is_missing:
            missing.append(name)
        if not result["ok"] and result.get("reason") not in {None, "missing"}:
            reasons.append(f"{name}:{result['reason']}")

    private_key_result, private_key_missing = _private_key_check(values)
    add(PRIVATE_KEY_ENV, private_key_result, private_key_missing)
    live_result, live_missing = _flag_check(values, LIVE_ENV, "not_true")
    add(LIVE_ENV, live_result, live_missing)
    armed_result, armed_missing = _flag_check(values, ARMED_ENV, "not_true")
    add(ARMED_ENV, armed_result, armed_missing)
    wallet_result, wallet_missing = _address_check(values)
    add(WALLET_ENV, wallet_result, wallet_missing)
    rpc_result, rpc_missing = _rpc_check(values)
    add(RPC_URL_ENV, rpc_result, rpc_missing)

    eth_ok = _eth_account_available() if eth_account_available is None else eth_account_available is True
    add("eth_account", _check(eth_ok, "missing"), not eth_ok)
    add("OKX_SWAP_BUILDER", _okx_builder_check(okx_client))
    routers_result, routers_missing = _router_check(values)
    add(ROUTERS_ENV, routers_result, routers_missing)
    selector_result, selector_missing = _selector_check(values)
    add(SELECTOR_ENV, selector_result, selector_missing)
    amount_result, amount_missing = _positive_integer_check(values, BUY_AMOUNT_ENV)
    add(BUY_AMOUNT_ENV, amount_result, amount_missing)
    slippage_result, slippage_missing = _slippage_check(values)
    add(SLIPPAGE_ENV, slippage_result, slippage_missing)

    return {
        "ready": all(item["ok"] for item in checks.values()),
        "checks": checks,
        "missing": missing,
        "reasons": reasons,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, Any] | None = None,
    eth_account_available: bool | None = None,
    okx_client: Any | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Offline BSC live adapter preflight")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    result = run_preflight(
        env=env,
        eth_account_available=eth_account_available,
        okx_client=okx_client,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
