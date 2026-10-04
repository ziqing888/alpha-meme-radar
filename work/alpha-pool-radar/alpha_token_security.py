"""Fresh chain-bound token-security evidence for V2 entry classification."""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


CHAIN_IDS = {"bsc": "56", "robinhood": "4663"}
RISK_FALSE_FIELDS = (
    "is_honeypot", "cannot_buy", "cannot_sell", "cannot_sell_all",
    "is_blacklisted", "is_mintable", "transfer_pausable",
    "slippage_modifiable", "personal_slippage_modifiable",
    "owner_change_balance", "hidden_owner", "is_proxy",
)
COMPLETE_REQUIRED_FIELDS = tuple(field for field in RISK_FALSE_FIELDS if field != "cannot_sell")
TAX_FIELDS = ("buy_tax", "sell_tax")
MAX_TAX = 0.10
CACHE_TTL_SECONDS = 300


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def _tax(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 <= result <= 1 else None
    except (TypeError, ValueError):
        return None


def _security_flag(raw: dict[str, Any], field: str) -> bool | None:
    value = _flag(raw.get(field))
    if value is not None or field != "cannot_sell":
        return value
    b20 = raw.get("b20_token")
    b20_info = b20.get("b20_info") if isinstance(b20, dict) else None
    item = b20_info.get(field) if isinstance(b20_info, dict) else None
    return _flag(item.get("status")) if isinstance(item, dict) else None


def assess_security(chain: str, contract: str, raw: dict[str, Any], observed_at: str) -> dict[str, Any]:
    chain = str(chain).lower()
    contract = str(contract).lower()
    checks = {field: _security_flag(raw, field) for field in RISK_FALSE_FIELDS}
    flags = [field for field, value in checks.items() if value is True]
    taxes = {field: _tax(raw.get(field)) for field in TAX_FIELDS}
    flags.extend(field for field, value in taxes.items() if value is not None and value > MAX_TAX)
    complete = (
        all(checks[field] is False for field in COMPLETE_REQUIRED_FIELDS)
        and all(value is not None for value in taxes.values())
    )
    if flags:
        passed: bool | None = False
        status = "failed"
    elif complete:
        passed = True
        status = "passed"
    else:
        passed = None
        status = "executor_confirmation_required" if chain == "robinhood" else "incomplete"
    return {
        "chain": chain,
        "chain_id": CHAIN_IDS.get(chain),
        "contract_address": contract,
        "observed_at": observed_at,
        "assessment_source": "goplus_token_security",
        "assessment_status": status,
        "hard_risk_pass": passed,
        "hard_risk_flags": flags,
        "checks": checks,
        "buy_tax": taxes["buy_tax"],
        "sell_tax": taxes["sell_tax"],
    }


def fetch_security_batch(chain: str, addresses: list[str], timeout: float = 6.0) -> dict[str, dict[str, Any]]:
    params = urllib.parse.urlencode({"contract_addresses": ",".join(addresses)})
    request = urllib.request.Request(
        f"https://api.gopluslabs.io/api/v1/token_security/{CHAIN_IDS[chain]}?{params}",
        headers={"Accept": "application/json", "User-Agent": "alpha-pool-radar/2"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(4_000_001))
    if str(payload.get("code")) != "1" or not isinstance(payload.get("result"), dict):
        raise ValueError("token_security_unavailable")
    return {str(key).lower(): value for key, value in payload["result"].items() if isinstance(value, dict)}


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def enrich_token_security(
    rows: list[dict[str, Any]],
    cache_path: Path,
    now: str,
    *,
    fetcher: Callable[[str, list[str]], dict[str, dict[str, Any]]] = fetch_security_batch,
) -> list[dict[str, Any]]:
    cache = _read_cache(cache_path)
    cached_rows = cache.get("rows") if isinstance(cache.get("rows"), dict) else {}
    current_time = _time(now) or datetime.now(timezone.utc)
    targets: dict[str, list[str]] = {chain: [] for chain in CHAIN_IDS}
    for row in rows:
        chain = str(row.get("chain") or row.get("chain_id") or "").lower()
        contract = str(row.get("contract_address") or row.get("token_address") or "").lower()
        if chain not in CHAIN_IDS or not contract.startswith("0x") or len(contract) != 42:
            continue
        cached = cached_rows.get(f"{chain}:{contract}")
        observed = _time(cached.get("observed_at")) if isinstance(cached, dict) else None
        if not observed or not 0 <= (current_time - observed).total_seconds() <= CACHE_TTL_SECONDS:
            targets[chain].append(contract)

    errors: dict[str, str] = {}
    for chain, addresses in targets.items():
        unique = list(dict.fromkeys(addresses))
        for offset in range(0, len(unique), 20):
            batch = unique[offset : offset + 20]
            if not batch:
                continue
            try:
                received = fetcher(chain, batch)
            except Exception as exc:  # noqa: BLE001
                errors[chain] = f"{type(exc).__name__}: {exc}"
                continue
            for contract in batch:
                raw = received.get(contract)
                if isinstance(raw, dict):
                    cached_rows[f"{chain}:{contract}"] = assess_security(chain, contract, raw, now)

    enriched: list[dict[str, Any]] = []
    for row in rows:
        result = dict(row)
        chain = str(row.get("chain") or row.get("chain_id") or "").lower()
        contract = str(row.get("contract_address") or row.get("token_address") or "").lower()
        assessment = cached_rows.get(f"{chain}:{contract}")
        observed = _time(assessment.get("observed_at")) if isinstance(assessment, dict) else None
        if isinstance(assessment, dict) and observed and 0 <= (current_time - observed).total_seconds() <= CACHE_TTL_SECONDS:
            result["token_security"] = assessment
            if assessment.get("hard_risk_pass") is not None:
                result["hard_risk_pass"] = assessment["hard_risk_pass"]
            if assessment.get("hard_risk_flags"):
                result["risk_flags"] = list(assessment["hard_risk_flags"])
        else:
            result["token_security"] = {
                "chain": chain,
                "contract_address": contract,
                "assessment_status": "unavailable",
                "error": errors.get(chain, "token_security_unavailable"),
            }
            result.pop("hard_risk_pass", None)
        enriched.append(result)

    _write_cache(cache_path, {"updated_at": now, "rows": cached_rows, "errors": errors})
    return enriched
