"""Monitor public portfolio trades of wallets with qualifying profit history."""
from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from alpha_fast_track import age_seconds, read_json, utc_now, write_json
from alpha_gmgn_smart_money_top50 import GMGN_SKILLS_DIR, ROOT, gmgn_env, normalize_gmgn_trade, normalize_wallet_address, resolve_gmgn_runner


SOURCE = "gmgn_profitable_wallet_trades"
BATCH_SIZE = 4
PAGE_SIZE = 20
CACHE_SECONDS = 15 * 60
POLL_SECONDS = 60
COOLDOWN_SECONDS = 5 * 60


def qualified_wallets(payload: dict, now_iso: str, limit: int = 50) -> list[dict]:
    # Keep the legacy normalizer importable while the shared quality module is installed.
    from alpha_wallet_quality import qualified_wallets as qualify
    return qualify(payload, now_iso, limit=limit)


def _number(value):
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _epoch(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 1e11 else number
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _chain(value) -> str:
    raw = str(value or "").strip().lower()
    return {"sol": "solana", "501": "solana", "56": "bsc", "bnb": "bsc", "eth": "ethereum", "1": "ethereum", "8453": "base"}.get(raw, raw)


def _wallet_key(row: dict) -> tuple[str, str]:
    return _chain(row.get("chain")), normalize_wallet_address(row.get("wallet") or row.get("wallet_address") or row.get("address"))


def normalize_portfolio_event(row: dict, chain: str, wallet: str) -> dict | None:
    token = row.get("token") if isinstance(row.get("token"), dict) else {}
    address = normalize_wallet_address(token.get("address"))
    side = str(row.get("event_type") or "").strip().lower()
    stamp = row.get("timestamp")
    tx = str(row.get("tx_hash") or row.get("transaction_hash") or "").strip()
    if side not in {"buy", "sell"} or not address or not tx or _epoch(stamp) is None:
        return None
    # For a sale, cost_usd is proceeds; buy_cost_usd is its historical cost basis.
    amount = _number(row.get("cost_usd"))
    return {"source": SOURCE, "source_family": SOURCE, "chain": _chain(chain),
            "wallet": wallet, "wallet_address": wallet, "qualified_wallet": True,
            "qualification_source": "gmgn-smart-money-top50.json",
            "activity_source": "gmgn_public_portfolio_activity",
            "contract_address": address, "tokenAddress": address,
            "symbol": token.get("symbol") or token.get("name") or "UNKNOWN",
            "side": side, "event_type": side, "monitor985_trade_side": side,
            "amount_usd": amount, "monitor985_trade_amount_usd": amount,
            "cost_usd": amount, "buy_cost_usd": _number(row.get("buy_cost_usd")),
            "timestamp": stamp, "observed_at": stamp, "tx_hash": tx,
            "independent_profit_verified": False}


def _activity_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)][:PAGE_SIZE]
    if isinstance(payload, dict):
        for name in ("activities", "data", "rows", "items", "result"):
            if isinstance(payload.get(name), (dict, list)):
                return _activity_rows(payload[name])
    return []


def _cached_rows(rows: list[dict], qualified_keys: set[tuple[str, str]], now_epoch: float) -> list[dict]:
    rows_by_trade = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("source") != SOURCE or row.get("qualified_wallet") is not True:
            continue
        wallet_key = _wallet_key(row)
        event_epoch = _epoch(row.get("observed_at"))
        tx = str(row.get("tx_hash") or "")
        token = normalize_wallet_address(row.get("contract_address"))
        side = row.get("side")
        if wallet_key not in qualified_keys or event_epoch is None or not 0 <= now_epoch - event_epoch <= CACHE_SECONDS:
            continue
        if not tx or not token or side not in {"buy", "sell"}:
            continue
        trade_key = (*wallet_key, tx.lower() if tx.startswith("0x") else tx, token, side)
        rows_by_trade.setdefault(trade_key, row)
    return list(rows_by_trade.values())


def _cooldown(shared: dict, previous: dict) -> float:
    return max(_epoch(shared.get("retry_after_epoch")) or 0,
               _epoch(shared.get("retry_after")) or 0,
               _epoch(previous.get("retry_after")) or 0)


def _rate_limited(proc, payload) -> bool:
    error = (proc.stderr or "")
    if proc.returncode or payload is None:
        error += " " + (proc.stdout or "")
    elif isinstance(payload, dict):
        error += " " + json.dumps({key: payload.get(key) for key in ("code", "status", "status_code", "error", "message")})
    return bool(re.search(r"\b429\b|rate[_ -]?limit|too many requests", error, re.I))


def _retry_epoch(message: str, now_epoch: float) -> float:
    deadline = now_epoch + COOLDOWN_SECONDS
    reset = re.search(r'(?:reset_at|resetAt|retry_after_epoch)[^0-9]{0,12}(\d{10,13})', message)
    delay = re.search(r'retry[-_ ]after[^0-9]{0,12}(\d{1,6})(?!\d)', message, re.I)
    if reset:
        deadline = max(deadline, _epoch(reset.group(1)) or 0)
    if delay:
        deadline = max(deadline, now_epoch + int(delay.group(1)))
    return deadline


def normalize_trade(row: dict, chain: str) -> dict:
    result = normalize_gmgn_trade(row, chain)
    # Source documentation identifies base_address as the traded token.
    result["tokenAddress"] = row.get("base_address") or result.get("tokenAddress")
    result["contract_address"] = result.get("tokenAddress")
    result["side"] = str(row.get("side") or "").lower()
    result["monitor985_trade_side"] = result["side"]
    result["observed_at"] = row.get("timestamp") or row.get("time") or ""
    result["tx_hash"] = row.get("tx_hash") or row.get("tx") or row.get("transaction_hash") or ""
    return result


def refresh(out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    path = out_dir / "meme-source-inbox" / "gmgn-wallet-flow.json"
    cooldown_path = out_dir / "gmgn-wallet-profit-query-status.json"
    previous = read_json(path)
    now = utc_now()
    now_epoch = _epoch(now)
    if now_epoch is None:
        raise ValueError("utc_now must return a timezone-aware timestamp")
    shared = read_json(cooldown_path)
    retry_epoch = _cooldown(shared, previous)
    if retry_epoch > now_epoch and retry_epoch > _cooldown(shared, {}):
        write_json(cooldown_path, {**shared, "ok": False, "rate_limited": True,
                                  "retry_after_epoch": retry_epoch,
                                  "retry_after": datetime.fromtimestamp(retry_epoch, timezone.utc).isoformat(),
                                  "source": SOURCE})
    try:
        selected = qualified_wallets(read_json(out_dir / "gmgn-smart-money-top50.json"), now, limit=50)
    except ImportError:
        selected, qualification_error = [], "wallet_quality_unavailable"
    except (TypeError, ValueError):
        selected, qualification_error = [], "wallet_quality_invalid"
    else:
        qualification_error = ""
    if not isinstance(selected, list):
        selected, qualification_error = [], "wallet_quality_invalid"
    targets = {}
    for wallet in selected[:50]:
        if not isinstance(wallet, dict):
            continue
        key = _wallet_key(wallet)
        if key[0] in {"solana", "bsc", "base", "ethereum", "robinhood", "arc", "stable"} and key[1]:
            targets.setdefault(key, wallet)
    keys = sorted(targets)
    rows = _cached_rows(previous.get("rows") or [], set(keys), now_epoch)
    try:
        cursor = max(0, int(previous.get("next_wallet_index") or 0)) % max(1, len(keys))
    except (ValueError, TypeError):
        cursor = 0
    errors, attempted = [], []
    config_attempted = False

    def finish(reason="", skipped=False):
        finished = utc_now()
        retained = _cached_rows(rows, set(keys), _epoch(finished) or now_epoch)
        status = {"ok": not errors and not reason, "partial": bool((errors or reason) and retained),
                  "checked_at": finished, "row_count": len(retained), "qualified_wallet_count": len(keys),
                  "queried_wallet_count": len(attempted), "queried_wallets": attempted,
                  "buy_count": sum(r.get("side") == "buy" for r in retained),
                  "sell_count": sum(r.get("side") == "sell" for r in retained), "errors": errors}
        if reason:
            status["reason"] = reason
        if skipped:
            status["skipped"] = True
        payload = {"source": SOURCE, "updated_at": finished, "rows": retained, "status": status,
                   "next_wallet_index": cursor,
                   "last_attempt_at": now if attempted or config_attempted else previous.get("last_attempt_at")}
        if retry_epoch > now_epoch:
            payload["retry_after"] = datetime.fromtimestamp(retry_epoch, timezone.utc).isoformat()
            status["retry_after"] = payload["retry_after"]
            status["retry_after_epoch"] = retry_epoch
        write_json(path, payload)
        return status

    def rate_limit(proc, call_epoch, context):
        nonlocal retry_epoch
        shared = read_json(cooldown_path)
        retry_epoch = max(_cooldown(shared, previous),
                          _retry_epoch((proc.stderr or "") + " " + (proc.stdout or ""), _epoch(utc_now()) or call_epoch))
        write_json(cooldown_path, {**shared, "ok": False, "rate_limited": True,
                                  "retry_after_epoch": retry_epoch,
                                  "retry_after": datetime.fromtimestamp(retry_epoch, timezone.utc).isoformat(),
                                  "source": SOURCE})
        errors.append({**context, "reason": "rate_limited"})
        return finish("rate_limited")

    if retry_epoch > now_epoch:
        return finish("rate_limit_backoff", skipped=True)
    if not keys:
        return finish(qualification_error or "no_qualified_wallets", skipped=True)
    if age_seconds(previous.get("last_attempt_at"), now) < POLL_SECONDS:
        return finish(skipped=True)
    runner, _ = resolve_gmgn_runner()
    if not runner:
        return finish("gmgn_cli_missing")
    env, _ = gmgn_env()
    env = dict(env)
    env.pop("GMGN_PRIVATE_KEY", None)
    kwargs = {"env": env, "cwd": GMGN_SKILLS_DIR if GMGN_SKILLS_DIR.exists() else ROOT,
              "capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "timeout": 10,
              "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    retry_epoch = max(retry_epoch, _cooldown(read_json(cooldown_path), previous))
    if retry_epoch > (_epoch(utc_now()) or now_epoch):
        return finish("rate_limit_backoff", skipped=True)
    config_attempted = True
    try:
        checked = subprocess.run([*runner, "config", "--check"], **kwargs)
    except (subprocess.TimeoutExpired, OSError) as exc:
        errors.append({"stage": "config_check", "reason": type(exc).__name__})
        return finish("gmgn_config_check_failed")
    if _rate_limited(checked, None):
        return rate_limit(checked, now_epoch, {"stage": "config_check"})
    if checked.returncode:
        return finish("gmgn_config_check_failed")
    for _ in range(min(BATCH_SIZE, len(keys))):
        # Re-read before each call: the history screener shares this cooldown.
        call_epoch = _epoch(utc_now()) or now_epoch
        retry_epoch = max(retry_epoch, _cooldown(read_json(cooldown_path), previous))
        if retry_epoch > call_epoch:
            return finish("rate_limit_backoff", skipped=True)
        chain, wallet = keys[cursor]
        cli_chain = {"solana": "sol", "ethereum": "eth"}.get(chain, chain)
        cursor = (cursor + 1) % len(keys)
        attempted.append({"chain": chain, "wallet": wallet})
        try:
            proc = subprocess.run([*runner, "portfolio", "activity", "--chain", cli_chain,
                                   "--wallet", wallet, "--limit", str(PAGE_SIZE), "--raw"], **kwargs)
        except (subprocess.TimeoutExpired, OSError) as exc:
            errors.append({"chain": chain, "wallet": wallet, "reason": type(exc).__name__})
            continue
        try:
            payload = json.loads(proc.stdout)
        except (TypeError, ValueError):
            payload = None
        if _rate_limited(proc, payload):
            return rate_limit(proc, call_epoch, {"chain": chain, "wallet": wallet})
        if proc.returncode or (isinstance(payload, dict) and payload.get("code") not in {None, 0, "0", 200, "200"}):
            errors.append({"chain": chain, "wallet": wallet, "reason": "upstream_failed"})
            continue
        if payload is None:
            errors.append({"chain": chain, "wallet": wallet, "reason": "invalid_json"})
            continue
        fresh_rows = [normalized for event in _activity_rows(payload)
                      if (normalized := normalize_portfolio_event(event, chain, wallet)) is not None]
        for row in fresh_rows:
            row["wallet_quality_evidence_checked_at"] = targets[(chain, wallet)].get("evidence_checked_at")
            row["wallet_quality_policy"] = targets[(chain, wallet)].get("quality_policy")
        rows = _cached_rows([*rows, *fresh_rows], set(keys), call_epoch)
    return finish()
