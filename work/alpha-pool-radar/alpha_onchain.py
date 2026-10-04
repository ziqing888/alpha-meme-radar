"""On-chain holder and token-risk adapters for the Alpha radar."""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from alpha_http import http_json, http_post_json, record_http_event, write_http_cache
from alpha_radar_sources import (
    BLOCKSCOUT_API_BASES,
    BSC_SCAN_TOKEN_HOLDERS_URL,
    ETHERSCAN_V2_API_URL,
    GOPLUS_TOKEN_SECURITY_URL,
    REQUEST_HEADERS,
    ROUTESCAN_ETHERSCAN_API_URL,
    RUGCHECK_REPORT_URL,
    RUGCHECK_SUMMARY_URL,
    SOLANA_RPC_URL,
)
from alpha_scoring import score_row


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, str) and value.strip() == "":
            return default
        result = float(value)
        if result != result or result in {float("inf"), float("-inf")}:
            return default
        return result
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def evm_scan_api_key() -> str:
    return (
        os.environ.get("ETHERSCAN_API_KEY")
        or os.environ.get("EVM_SCAN_API_KEY")
        or ""
    ).strip()


def normalize_holder_provider(provider: str) -> str:
    provider = (provider or "routescan").strip().lower()
    if provider not in {"routescan", "etherscan", "blockscout", "bscscan", "solana_rpc", "auto", "goplus"}:
        raise ValueError(f"Unknown holder provider: {provider}")
    return provider


def resolve_holder_provider(provider: str, chain_id: str) -> str:
    provider = normalize_holder_provider(provider)
    if provider != "auto":
        return provider
    if str(chain_id) in BLOCKSCOUT_API_BASES:
        return "blockscout"
    if str(chain_id) == "56":
        return "bscscan"
    if str(chain_id) == "CT_501" or str(chain_id).lower() == "solana":
        return "solana_rpc"
    return ""


def holder_api_key(provider: str) -> str:
    provider = normalize_holder_provider(provider)
    if provider in {"auto", "goplus"}:
        return ""
    if provider == "routescan":
        return os.environ.get("ROUTESCAN_API_KEY", "").strip()
    if provider == "bscscan":
        return os.environ.get("BSCSCAN_API_KEY", "").strip()
    if provider == "solana_rpc":
        return ""
    if provider == "etherscan":
        return evm_scan_api_key()
    return os.environ.get("BLOCKSCOUT_API_KEY", "").strip()


def is_evm_chain_id(chain_id: str) -> bool:
    return str(chain_id).isdigit()


def inferred_total_supply(row: Any) -> float:
    price = row.price_usd
    if price <= 0:
        return 0.0
    supply_value = row.fdv or row.dex_market_cap or row.market_cap
    if supply_value <= 0:
        return 0.0
    return supply_value / price


def blockscout_api_base(chain_id: str) -> str:
    configured = os.environ.get("BLOCKSCOUT_API_BASE", "").strip().rstrip("/")
    if configured:
        return configured
    if str(chain_id) in BLOCKSCOUT_API_BASES:
        return BLOCKSCOUT_API_BASES[str(chain_id)]
    raise RuntimeError("Set BLOCKSCOUT_API_BASE for this chain before using blockscout holder provider.")


def parse_bscscan_holder_export(html: str) -> list[dict[str, Any]]:
    match = re.search(r"quickExportTokenHolerData\s*=\s*'(?P<data>\[.*?\])';", html, flags=re.DOTALL)
    if not match:
        return []
    try:
        rows = json.loads(match.group("data"))
    except json.JSONDecodeError:
        return []

    parsed: list[dict[str, Any]] = []
    exchange_names = ("binance", "mexc", "okx", "gate", "kucoin")
    for row in rows:
        if not isinstance(row, list) or len(row) < 4:
            continue
        label = str(row[2] or "")
        parsed.append(
            {
                "TokenHolderAddress": str(row[1] or ""),
                "TokenHolderQuantity": str(row[3] or "").replace(",", ""),
                "TokenHolderAddressType": "E" if any(name in label.lower() for name in exchange_names) else "",
            }
        )
    return parsed


def fetch_bscscan_top_holders(contract_address: str, offset: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"a": contract_address, "p": 1, "ps": max(25, offset)})
    request = urllib.request.Request(f"{BSC_SCAN_TOKEN_HOLDERS_URL}?{query}", headers=REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        html = response.read().decode("utf-8", errors="replace")
    return parse_bscscan_holder_export(html)[:offset]


def fetch_top_holders(provider: str, chain_id: str, contract_address: str, offset: int, api_key: str) -> list[dict[str, Any]]:
    provider = normalize_holder_provider(provider)
    if provider == "auto":
        raise RuntimeError("Resolve holder provider before fetching top holders.")
    if provider == "routescan":
        params: dict[str, Any] = {
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract_address,
            "page": 1,
            "offset": offset,
        }
        if api_key:
            params["apikey"] = api_key
        url = f"{ROUTESCAN_ETHERSCAN_API_URL.format(chain_id=chain_id)}?{urllib.parse.urlencode(params)}"
    elif provider == "etherscan":
        if not api_key:
            raise RuntimeError("Set ETHERSCAN_API_KEY or EVM_SCAN_API_KEY for etherscan holder provider.")
        params = {
            "chainid": chain_id,
            "module": "token",
            "action": "topholders",
            "contractaddress": contract_address,
            "offset": offset,
            "apikey": api_key,
        }
        url = f"{ETHERSCAN_V2_API_URL}?{urllib.parse.urlencode(params)}"
    elif provider == "bscscan":
        return fetch_bscscan_top_holders(contract_address, offset)
    else:
        params = {
            "module": "token",
            "action": "getTokenHolders",
            "contractaddress": contract_address,
            "page": 1,
            "offset": offset,
        }
        if api_key:
            params["apikey"] = api_key
        url = f"{blockscout_api_base(chain_id)}?{urllib.parse.urlencode(params)}"

    payload = http_json(url, timeout=20)
    if str(payload.get("status")) != "1":
        raise RuntimeError(f"EVM holder API error: {payload.get('message')} {payload.get('result')}")
    result = payload.get("result") or []
    return [row for row in result if isinstance(row, dict)]


def holder_quantity(row: dict[str, Any]) -> float:
    return to_float(row.get("TokenHolderQuantity") or row.get("value") or row.get("Value"))


def normalize_holder_quantities(quantities: list[float], total_supply: float) -> list[float]:
    if total_supply <= 0 or not quantities:
        return quantities
    if sum(quantities[:20]) <= total_supply * 1.05:
        return quantities
    for decimals in range(1, 37):
        factor = 10**decimals
        normalized = [quantity / factor for quantity in quantities]
        if sum(normalized[:20]) <= total_supply * 1.05:
            return normalized
    return quantities


def holder_metrics_from_rows(holder_rows: list[dict[str, Any]], total_supply: float) -> dict[str, Any]:
    quantities = [holder_quantity(row) for row in holder_rows]
    quantities = [quantity for quantity in quantities if quantity > 0]
    quantities = normalize_holder_quantities(quantities, total_supply)
    if total_supply <= 0 or not quantities:
        return {}

    def holder_pct(count: int) -> float:
        return (sum(quantities[:count]) / total_supply) * 100

    contract_count = 0
    for row in holder_rows[:20]:
        if str(row.get("TokenHolderAddressType") or "").upper() == "C":
            contract_count += 1
    return {
        "top10_holder_pct": round(holder_pct(10), 4),
        "top20_holder_pct": round(holder_pct(20), 4),
        "max_holder_pct": round((quantities[0] / total_supply) * 100, 4),
        "holder_contract_count": contract_count,
        "holder_source": "holderlist",
    }


def solana_rpc_request(method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = http_post_json(SOLANA_RPC_URL, payload, timeout=20)
    if response.get("error"):
        raise RuntimeError(f"Solana RPC error: {response['error']}")
    return response.get("result")


def solana_amount_value(row: dict[str, Any]) -> float:
    value = row.get("uiAmount")
    if value is not None:
        return to_float(value)
    raw = to_float(row.get("amount"))
    decimals = to_int(row.get("decimals"))
    return raw / (10**decimals) if decimals >= 0 else raw


def solana_holder_metrics_from_rpc(largest_accounts: list[dict[str, Any]], supply: dict[str, Any]) -> dict[str, Any]:
    quantities = [solana_amount_value(row) for row in largest_accounts]
    quantities = [quantity for quantity in quantities if quantity > 0]
    total_supply = solana_amount_value(supply)
    if total_supply <= 0 or not quantities:
        return {}

    def holder_pct(count: int) -> float:
        return (sum(quantities[:count]) / total_supply) * 100

    return {
        "top10_holder_pct": round(holder_pct(10), 4),
        "top20_holder_pct": round(holder_pct(20), 4),
        "max_holder_pct": round((quantities[0] / total_supply) * 100, 4),
        "holder_contract_count": 0,
        "holder_source": "holderlist",
    }


def fetch_rugcheck_report(mint: str) -> dict[str, Any]:
    url = RUGCHECK_REPORT_URL.format(mint=urllib.parse.quote(mint, safe=""))
    payload = http_json(url, timeout=20)
    return payload if isinstance(payload, dict) else {}


def rugcheck_holder_metrics(report: dict[str, Any]) -> dict[str, Any]:
    holder_rows = report.get("topHolders") or []
    if not isinstance(holder_rows, list):
        return {}
    percentages = [to_float(row.get("pct")) for row in holder_rows if isinstance(row, dict)]
    percentages = [value for value in percentages if value > 0]
    if not percentages:
        return {}

    special_count = 0
    for row in holder_rows[:20]:
        if not isinstance(row, dict):
            continue
        if row.get("insider") is True or str(row.get("type") or "").lower() in {"contract", "program"}:
            special_count += 1

    return {
        "top10_holder_pct": round(sum(percentages[:10]), 4),
        "top20_holder_pct": round(sum(percentages[:20]), 4),
        "max_holder_pct": round(percentages[0], 4),
        "holder_contract_count": special_count,
        "holder_source": "rugcheck_holderlist",
    }


def fetch_solana_holder_metrics(mint: str) -> dict[str, Any]:
    try:
        metrics = rugcheck_holder_metrics(fetch_rugcheck_report(mint))
        if metrics:
            return metrics
    except Exception:
        pass

    largest_result = solana_rpc_request("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    supply_result = solana_rpc_request("getTokenSupply", [mint, {"commitment": "confirmed"}])
    largest_accounts = ((largest_result or {}).get("value") or []) if isinstance(largest_result, dict) else []
    supply = ((supply_result or {}).get("value") or {}) if isinstance(supply_result, dict) else {}
    return solana_holder_metrics_from_rpc(largest_accounts, supply)


def goplus_holder_metrics(token: dict[str, Any]) -> dict[str, Any]:
    holders = token.get("holders")
    if not isinstance(holders, list) or not holders:
        return {}
    count = to_int(token.get("holder_count"))
    if len(holders) < 10 and (count <= 0 or len(holders) < count):
        return {}
    percentages = []
    addresses = set()
    for holder in holders:
        if not isinstance(holder, dict):
            return {}
        address = str(holder.get("address") or "").lower()
        try:
            fraction = float(holder["percent"])
        except (KeyError, TypeError, ValueError):
            return {}
        if not address or address in addresses or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            return {}
        addresses.add(address)
        percentages.append(fraction * 100)
    if sum(percentages) > 100.0001:
        return {}
    percentages.sort(reverse=True)
    return {
        "top10_holder_pct": round(sum(percentages[:10]), 4),
        "top20_holder_pct": None,
        "max_holder_pct": round(percentages[0], 4),
        "holder_contract_count": sum(str(h.get("is_contract")) == "1" for h in holders),
        "holder_source": "top10_addresses_including_contracts",
    }


def fetch_live_goplus_holders(chain_id: str, address: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"contract_addresses": address})
    url = f"{GOPLUS_TOKEN_SECURITY_URL.format(chain_id=chain_id)}?{query}"
    # Holder checks must not silently fall back to a six-hour-old HTTP cache.
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    for attempt in range(2):
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if str(payload.get("code")) != "4029" or attempt == 1:
            break
        time.sleep(3)
    if str(payload.get("code")) != "1":
        raise RuntimeError(f"GoPlus holder response code {payload.get('code')}")
    token = (payload.get("result") or {}).get(address.lower()) or {}
    record_http_event(url, "live")
    return goplus_holder_metrics(token)


def apply_holder_metrics(rows: list[Any], args: Any) -> list[str]:
    if not getattr(args, "holders_enable", False):
        return []
    configured_provider = normalize_holder_provider(getattr(args, "holder_provider", "routescan"))

    errors: list[str] = []
    candidates = [
        row
        for row in rows
        if (is_evm_chain_id(row.chain_id) and row.contract_address.startswith("0x"))
        or row.chain_id == "CT_501"
        or row.chain.lower() == "solana"
    ][: max(0, getattr(args, "holder_top_tokens", 12))]
    for row in candidates:
        provider = resolve_holder_provider(configured_provider, row.chain_id)
        if provider == "goplus":
            time.sleep(2.1)
            if row.chain_id == "CT_501" or row.chain.lower() == "solana":
                provider = "solana_rpc"
        if not provider:
            row.notes.append(f"Holder adapter skipped: no free provider for chain {row.chain_id}")
            continue
        try:
            if provider == "goplus" and is_evm_chain_id(row.chain_id):
                metrics = fetch_live_goplus_holders(row.chain_id, row.contract_address)
            elif provider == "solana_rpc" or (provider == "goplus" and row.chain_id == "CT_501"):
                metrics = fetch_solana_holder_metrics(row.contract_address)
            else:
                api_key = holder_api_key(provider)
                total_supply = inferred_total_supply(row)
                if total_supply <= 0:
                    errors.append(f"Holder adapter skipped {row.symbol}: no inferred supply from price/fdv.")
                    continue
                holders = fetch_top_holders(
                    provider,
                    row.chain_id,
                    row.contract_address,
                    max(20, getattr(args, "holder_offset", 20)),
                    api_key,
                )
                metrics = holder_metrics_from_rows(holders, total_supply)
            if not metrics:
                errors.append(f"Holder adapter empty {row.symbol}: no holder quantities.")
                continue
            row.top10_holder_pct = metrics["top10_holder_pct"]
            row.top20_holder_pct = metrics["top20_holder_pct"]
            row.max_holder_pct = metrics["max_holder_pct"]
            row.holder_contract_count = metrics["holder_contract_count"]
            row.holder_source = f"{provider}_{metrics['holder_source']}"
            if provider == "goplus" and is_evm_chain_id(row.chain_id):
                row.holder_observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                row.notes.append("Top10/最大持仓为地址口径，包含合约、交易所和池子，不等于单一庄家。")
            score_row(row, max_market_cap=getattr(args, "max_market_cap", 200_000_000))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Holder adapter {row.symbol} {row.chain_id}:{row.contract_address}: {exc}")
        time.sleep(0.55)
    return errors


def risk_level_from_score(score: float) -> str:
    if score >= 70:
        return "high"
    if score >= 35:
        return "medium"
    if score > 0:
        return "low"
    return ""


def goplus_risk_metrics(token: dict[str, Any]) -> dict[str, Any]:
    flags: list[str] = []
    score = 0.0
    if str(token.get("is_honeypot")) == "1":
        flags.append("honeypot")
        score += 80
    if str(token.get("cannot_sell_all")) == "1":
        flags.append("cannot_sell_all")
        score += 45
    if str(token.get("is_blacklisted")) == "1":
        flags.append("blacklist")
        score += 35
    if str(token.get("is_mintable")) == "1":
        flags.append("mintable")
        score += 20
    if str(token.get("is_open_source")) == "0":
        flags.append("not_open_source")
        score += 12
    buy_tax = to_float(token.get("buy_tax"))
    sell_tax = to_float(token.get("sell_tax"))
    if buy_tax >= 0.1:
        flags.append("high_buy_tax")
        score += min(25, buy_tax * 100)
    if sell_tax >= 0.1:
        flags.append("high_sell_tax")
        score += min(35, sell_tax * 120)
    score = min(100.0, score)
    return {
        "risk_score": round(score, 2),
        "risk_level": risk_level_from_score(score),
        "risk_flags": flags,
        "risk_source": "goplus",
    }


def rugcheck_risk_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    score = to_float(summary.get("score_normalised") if summary.get("score_normalised") is not None else summary.get("score"))
    flags: list[str] = []
    for risk in summary.get("risks") or []:
        if not isinstance(risk, dict):
            continue
        name = str(risk.get("name") or risk.get("description") or risk.get("level") or "").strip()
        if name:
            flags.append(name)
    lp_locked = summary.get("lpLockedPct")
    if lp_locked is not None and to_float(lp_locked) < 50:
        flags.append("lp_unlocked")
        score = max(score, 45)
    score = min(100.0, score)
    return {
        "risk_score": round(score, 2),
        "risk_level": risk_level_from_score(score),
        "risk_flags": flags[:8],
        "risk_source": "rugcheck",
    }


def apply_risk_metrics_to_row(row: Any, metrics: dict[str, Any]) -> None:
    row.risk_score = metrics.get("risk_score")
    row.risk_level = str(metrics.get("risk_level") or "")
    row.risk_flags = list(metrics.get("risk_flags") or [])
    row.risk_source = str(metrics.get("risk_source") or "")


def fetch_goplus_token_security(chain_id: str, contract_address: str, api_key: str = "") -> dict[str, Any]:
    query = urllib.parse.urlencode({"contract_addresses": contract_address})
    url = f"{GOPLUS_TOKEN_SECURITY_URL.format(chain_id=chain_id)}?{query}"
    if api_key:
        headers = dict(REQUEST_HEADERS)
        headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
            write_http_cache(url, payload)
            record_http_event(url, "live")
    else:
        payload = http_json(url, timeout=20)
    result = payload.get("result") or {}
    token = result.get(contract_address.lower()) or result.get(contract_address) or {}
    return token if isinstance(token, dict) else {}


def fetch_rugcheck_summary(mint: str) -> dict[str, Any]:
    url = RUGCHECK_SUMMARY_URL.format(mint=urllib.parse.quote(mint, safe=""))
    payload = http_json(url, timeout=20)
    return payload if isinstance(payload, dict) else {}


def apply_risk_metrics(rows: list[Any], args: Any) -> list[str]:
    if not getattr(args, "risk_enable", False):
        return []
    errors: list[str] = []
    api_key = os.environ.get("GOPLUS_API_KEY", "").strip()
    candidates = rows[: max(0, getattr(args, "risk_top_tokens", 12))]
    for row in candidates:
        try:
            if is_evm_chain_id(row.chain_id) and row.contract_address.startswith("0x"):
                token = fetch_goplus_token_security(row.chain_id, row.contract_address, api_key=api_key)
                if not token:
                    errors.append(f"Risk adapter empty {row.symbol}: GoPlus returned no token data.")
                    continue
                apply_risk_metrics_to_row(row, goplus_risk_metrics(token))
            elif row.chain_id == "CT_501" or row.chain.lower() == "solana":
                summary = fetch_rugcheck_summary(row.contract_address)
                if not summary:
                    errors.append(f"Risk adapter empty {row.symbol}: RugCheck returned no summary.")
                    continue
                apply_risk_metrics_to_row(row, rugcheck_risk_metrics(summary))
            else:
                continue
            score_row(row, max_market_cap=getattr(args, "max_market_cap", 200_000_000))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Risk adapter {row.symbol} {row.chain_id}:{row.contract_address}: {exc}")
        time.sleep(0.45)
    return errors
