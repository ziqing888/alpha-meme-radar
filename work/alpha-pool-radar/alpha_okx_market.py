"""Read-only OKX market discovery. Never handles wallets or payment signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOST = "https://web3.okx.com"
TRENCHES = "/api/v6/dex/market/memepump/tokenList"
SIGNALS = "/api/v6/dex/market/signal/list"
CHAINS = {"bsc": "56", "robinhood": "4663", "solana": "501"}
WALLET_TYPE_LABELS = {
    "1": "SMART_MONEY",
    "2": "INFLUENCER",
    "3": "WHALE",
    "SMART_MONEY": "SMART_MONEY",
    "KOL": "INFLUENCER",
    "INFLUENCER": "INFLUENCER",
    "WHALE": "WHALE",
}
_status: dict = {}
CREDENTIAL_NAMES = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
CREDENTIAL_PATH = Path(__file__).resolve().parents[2] / ".okx-market.local.json"
REST_SIGNAL_FILE = "okx-signal-rest.json"
REST_TRENCHES_FILE = "okx-memepump-rest.json"


def credentials() -> tuple[str, str, str]:
    # Reload per refresh so local setup does not require another process restart.
    values = {}
    try:
        payload = json.loads(CREDENTIAL_PATH.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict):
            values = payload
    except (OSError, ValueError):
        pass
    result = []
    for name in CREDENTIAL_NAMES:
        value = os.environ.get(name) or values.get(name, "")
        result.append(value.strip() if isinstance(value, str) else "")
    return tuple(result)


def status() -> dict:
    current = credentials()
    configured = all(current)
    return {"configured": configured, "ok": False,
            "missing_fields": [name for name, value in zip(CREDENTIAL_NAMES, current) if not value],
            "reason": "awaiting_refresh" if configured else "missing_market_api_credentials",
            **(_status if configured else {})}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(method: str, path: str, params: dict) -> list[dict]:
    if (method, path) not in {("GET", TRENCHES), ("POST", SIGNALS)}:
        raise ValueError("unsupported_market_endpoint")
    key, secret, passphrase = credentials()
    body = json.dumps([params], separators=(",", ":")) if method == "POST" else ""
    target = path + ("?" + urllib.parse.urlencode(params) if method == "GET" else "")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    signature = base64.b64encode(hmac.new(secret.encode(),
        (timestamp + method + target + body).encode(), hashlib.sha256).digest()).decode()
    req = urllib.request.Request(HOST + target, data=body.encode() if body else None,
        method=method, headers={"Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "AlphaRadar/1.0", "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": signature, "OK-ACCESS-PASSPHRASE": passphrase,
        "OK-ACCESS-TIMESTAMP": timestamp})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=6) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("payment_required" if exc.code == 402 else f"http_{exc.code}") from None
    if str(payload.get("code")) != "0":
        raise RuntimeError("okx_api_" + str(payload.get("code", "invalid_response")))
    data = payload.get("data")
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid_market_rows")
    return rows


def normalize_kind(kind: str) -> str:
    raw = str(kind or "").strip().upper()
    if raw in {"SIGNAL", "OKX_SIGNAL"}:
        return "SIGNAL"
    return raw or "TRENCHES"


def normalize_wallet_type(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return WALLET_TYPE_LABELS.get(raw, raw)


def csv_values(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = str(value or "").split(",")
    return [str(item).strip() for item in values if str(item).strip()]


def normalize(row: dict, chain: str, kind: str) -> dict:
    normalized_kind = normalize_kind(kind)
    token = row.get("token") if isinstance(row.get("token"), dict) else row
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
    social = row.get("social") if isinstance(row.get("social"), dict) else {}
    address = token.get("tokenContractAddress") or token.get("tokenAddress") or row.get("tokenContractAddress") or row.get("tokenAddress") or ""
    wallet_type = normalize_wallet_type(row.get("walletType"))
    trigger_count = row.get("triggerWalletCount")
    result = {
        "chain": chain,
        "chainId": chain,
        "address": address.lower() if chain in {"bsc", "robinhood"} else address,
        "tokenAddress": address.lower() if chain in {"bsc", "robinhood"} else address,
        "contractAddress": address.lower() if chain in {"bsc", "robinhood"} else address,
        "symbol": token.get("symbol"),
        "name": token.get("name"),
        "logo": token.get("logo") or token.get("logoUrl") or row.get("logo") or row.get("logoUrl"),
        "price": row.get("price") or token.get("price") or market.get("price"),
        "marketCap": market.get("marketCapUsd") or token.get("marketCapUsd"),
        "holder_count": token.get("holders") or tags.get("totalHolders"),
        "holders": token.get("holders") or tags.get("totalHolders"),
        "top_10_holder_rate": (
            token.get("top10HolderPercent")
            or token.get("top10HolderPct")
            or tags.get("top10HoldingsPercent")
        ),
        "liquidity": market.get("liquidityUsd") or token.get("liquidityUsd"),
        "okx_kind": normalized_kind,
        "okx_source_panel": "signal" if normalized_kind == "SIGNAL" else "trenches",
        "okx_signal_panel": normalized_kind == "SIGNAL",
        "okx_signal_timestamp": row.get("timestamp"),
        "okx_signal_type": wallet_type,
        "okx_signal_type_label": {"SMART_MONEY": "Smart Money", "INFLUENCER": "KOL/Influencer", "WHALE": "Whale"}.get(wallet_type, wallet_type),
        "okx_wallet_type": row.get("walletType"),
        "okx_trigger_wallet_count": trigger_count,
        "okx_signal_amount_usd": row.get("amountUsd"),
        "okx_signal_sold_ratio_pct": row.get("soldRatioPercent"),
        "okx_signal_price_usd": row.get("price"),
        "okx_signal_wallet_addresses": csv_values(row.get("triggerWalletAddress"))[:20],
        "okx_volume_1h": market.get("volumeUsd1h"),
        "okx_tx_count_1h": market.get("txCount1h"),
        "okx_buy_tx_count_1h": market.get("buyTxCount1h"),
        "okx_sell_tx_count_1h": market.get("sellTxCount1h"),
        "okx_bonding_percent": row.get("bondingPercent"),
        "okx_created_timestamp": row.get("createdTimestamp"),
        "okx_protocol_id": row.get("protocolId"),
        "okx_tags": tags,
        "okx_social": social,
        "okx_dev_holdings_pct": tags.get("devHoldingsPercent"),
        "okx_insiders_pct": tags.get("insidersPercent"),
        "okx_bundlers_pct": tags.get("bundlersPercent"),
        "okx_snipers_pct": tags.get("snipersPercent"),
        "okx_fresh_wallets_pct": tags.get("freshWalletsPercent"),
        "okx_suspected_phishing_pct": tags.get("suspectedPhishingWalletPercent"),
    }
    if normalized_kind != "SIGNAL":
        result["market_data_pending"] = not bool(result.get("liquidity"))
        result["launchpad_platform"] = "OKX Memepump"
        result["platform"] = "OKX Memepump"
        result["creation_timestamp"] = row.get("createdTimestamp")
        result["bundler_rate"] = tags.get("bundlersPercent")
    if normalized_kind == "SIGNAL":
        if wallet_type == "SMART_MONEY":
            result["smart_money"] = trigger_count
        elif wallet_type == "INFLUENCER":
            result["kol"] = trigger_count
    return result


def persist_snapshot(out_dir: Path, rows: list[dict], errors: list[str]) -> dict[str, Any]:
    """Persist successful REST results so the low-latency reader keeps OKX evidence."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    buckets = {
        "okx_signal": [row for row in rows if row.get("okx_kind") == "SIGNAL"],
        "okx_trenches": [row for row in rows if row.get("okx_kind") != "SIGNAL"],
    }
    filenames = {
        "okx_signal": REST_SIGNAL_FILE,
        "okx_trenches": REST_TRENCHES_FILE,
    }
    written = []
    for source, bucket in buckets.items():
        path = out_dir / filenames[source]
        if errors and not bucket:
            continue
        payload = {
            "ok": not errors,
            "partial": bool(errors and bucket),
            "source": source,
            "source_family": source,
            "updated_at": now,
            "errors": list(errors),
            "count": len(bucket),
            "data": bucket,
        }
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
        written.append(source)
    return {
        "ok": not errors,
        "partial": bool(errors and written),
        "signal_rows": len(buckets["okx_signal"]),
        "trenches_rows": len(buckets["okx_trenches"]),
        "written": written,
        "updated_at": now,
    }


def fetch(chains: set[str], limit: int) -> tuple[list[dict], list[str]]:
    global _status
    if not all(credentials()):
        _status = {}
        return [], []
    jobs = []
    for chain, index in CHAINS.items():
        if chains and chain not in chains:
            continue
        jobs.extend((chain, stage, "GET", TRENCHES, {"chainIndex": index, "stage": stage})
                    for stage in ("NEW", "MIGRATING", "MIGRATED"))
        jobs.append((chain, "SIGNAL", "POST", SIGNALS,
                     {"chainIndex": index, "walletType": "1,2,3", "limit": str(min(limit, 100))}))

    def run(job):
        chain, kind, method, path, params = job
        try:
            return [normalize(row, chain, kind) for row in request(method, path, params)[:limit]], None
        except Exception as exc:
            reason = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
            return [], f"{chain}/{kind}: {reason}"

    rows, errors = [], []
    # Trial quotas are shared across stages; avoid a multi-chain request burst.
    for index, job in enumerate(jobs):
        started = time.monotonic()
        batch, error = run(job)
        rows.extend(batch)
        if error:
            errors.append(error)
        if index + 1 < len(jobs):
            time.sleep(max(0, 1.1 - (time.monotonic() - started)))
    # A repeated list entry is not another market event. Preserve distinct signals only.
    unique = {}
    for row in rows:
        if row["address"]:
            key = (row["chain"], row["address"], row["okx_kind"],
                   row["okx_signal_timestamp"], row["okx_wallet_type"])
            unique[key] = row
    kind_counts: dict[str, int] = {}
    for row in unique.values():
        kind_counts[row["okx_kind"]] = kind_counts.get(row["okx_kind"], 0) + 1
    _status = {"ok": bool(jobs) and not errors, "reason": "; ".join(errors),
               "row_count": len(unique), "kind_counts": kind_counts,
               "signal_row_count": kind_counts.get("SIGNAL", 0),
               "trenches_row_count": len(unique) - kind_counts.get("SIGNAL", 0),
               "request_count": len(jobs),
               "last_checked_at": datetime.now(timezone.utc).isoformat()}
    return list(unique.values()), errors
