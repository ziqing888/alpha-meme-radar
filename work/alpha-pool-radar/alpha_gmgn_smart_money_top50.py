#!/usr/bin/env python3
"""Publish qualified GMGN profit-history wallets from a local evidence report.

Legacy discovery/CLI helpers remain available to the scanner. run_once never
calls them: only dated, independently re-gated report rows can be published.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_wallet_quality import POLICY, POLICY_ID, qualified_wallets


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "outputs"
INBOX_DIR = OUT_DIR / "meme-source-inbox"
GMGN_SKILLS_DIR = ROOT / "work" / "external" / "open-source-radar" / "gmgn-skills"
GMGN_DIST_CLI = GMGN_SKILLS_DIR / "dist" / "index.js"
DEFAULT_JSON_OUT = OUT_DIR / "gmgn-smart-money-top50.json"
DEFAULT_MD_OUT = OUT_DIR / "gmgn-smart-money-top50.md"
DEFAULT_MAX_AGE_DAYS = 3.0
DEFAULT_LIMIT = 50
DEFAULT_CHAINS = ("bsc", "robinhood", "base", "sol")
DEFAULT_CLI_TIMEOUT_SECONDS = 12
DEFAULT_READ_ONLY_GMGN_API_KEY = "gmgn_solbscbaseethmonadtron"
EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

WALLET_FIELDS = (
    "monitor985_wallet",
    "wallet",
    "walletAddress",
    "wallet_address",
    "smart_wallet",
    "smartWallet",
    "smartWalletAddress",
    "smart_wallet_address",
    "trader",
    "traderAddress",
    "maker",
    "makerAddress",
    "buyer",
    "buyerAddress",
)
WALLET_LIST_FIELDS = (
    "okx_signal_wallet_addresses",
    "triggerWalletAddress",
    "trigger_wallet_addresses",
    "wallets",
    "smart_wallets",
)
SOURCE_WEIGHTS = {
    "gmgn_cli_smartmoney": 26.0,
    "gmgn_live_trending": 18.0,
    "gmgn_trenches": 18.0,
    "985_monitor": 14.0,
    "985_fomo_wallets": 22.0,
    "985_smartmoney": 12.0,
    "okx_signal": 18.0,
    "okx_trenches": 10.0,
    "proficy_trending": 10.0,
    "wind_monitor": 12.0,
    "binance_wallet_hot": 12.0,
    "binance_wallet_signal": 14.0,
    "debot_signal": 14.0,
    "debot_trenches": 10.0,
}

GMGN_ROW_FIELDS = (
    "list",
    "rows",
    "items",
    "data",
    "result",
    "trades",
    "records",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            values[key] = value
    return values


def nested_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "rows", "value", "items", "tokens", "pairs"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("items", "tokens", "rows", "pairs"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def source_label_from_payload(path: Path, payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("source") or payload.get("source_family")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return path.stem


def split_wallet_values(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = str(value or "").replace("\n", ",").split(",")
    return [str(item).strip() for item in raw_values if str(item).strip()]


def normalize_wallet_address(value: Any) -> str:
    raw = str(value or "").strip()
    if EVM_ADDRESS_RE.match(raw):
        return raw.lower()
    if SOLANA_ADDRESS_RE.match(raw):
        return raw
    return ""


def token_id(row: dict[str, Any]) -> str:
    chain = str(row.get("chain") or row.get("chainId") or row.get("network") or "unknown").strip().lower()
    address = str(
        row.get("tokenAddress")
        or row.get("token_address")
        or row.get("contract_address")
        or row.get("contractAddress")
        or row.get("address")
        or row.get("mint")
        or row.get("symbol")
        or "unknown"
    ).strip()
    return f"{chain}:{address}".lower()


def token_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("name") or "UNKNOWN").strip() or "UNKNOWN"


def observed_at(row: dict[str, Any]) -> str:
    for key in ("observed_at", "monitor985_created_at", "createdAt", "created_at", "timestamp", "fetched_at", "time"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


@dataclass
class TokenEvidence:
    symbol: str
    token: str
    chain: str
    count: int = 0
    max_market_cap: float = 0.0


@dataclass
class WalletScore:
    address: str
    chain: str
    aliases: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    tokens: dict[str, TokenEvidence] = field(default_factory=dict)
    events: int = 0
    buy_events: int = 0
    total_buy_usd: float = 0.0
    smart_money_sum: float = 0.0
    quality_score: float = 0.0
    source_score: float = 0.0
    first_seen: str = ""
    last_seen: str = ""

    def add(self, row: dict[str, Any], source: str) -> None:
        self.events += 1
        self.sources.add(source)
        for key in (
            "monitor985_wallet_name",
            "monitor985_fomo_handle",
            "monitor985_fomo_name",
            "walletName",
            "watchName",
            "alias",
            "label",
        ):
            value = row.get(key)
            if value:
                self.aliases.add(str(value).strip())
        side = str(row.get("monitor985_trade_side") or row.get("side") or "").upper()
        if not side or "BUY" in side:
            self.buy_events += 1
        self.total_buy_usd += to_float(row.get("monitor985_trade_amount_usd") or row.get("amountUsd") or row.get("amount_usd"))
        self.smart_money_sum += to_float(row.get("smart_money"))
        self.quality_score = max(self.quality_score, to_float(row.get("monitor985_smart_wallet_quality")))
        self.source_score += SOURCE_WEIGHTS.get(source, 8.0)
        stamp = observed_at(row)
        if stamp:
            if not self.first_seen or stamp < self.first_seen:
                self.first_seen = stamp
            if not self.last_seen or stamp > self.last_seen:
                self.last_seen = stamp
        key = token_id(row)
        item = self.tokens.get(key)
        if not item:
            item = TokenEvidence(
                symbol=token_symbol(row),
                token=key,
                chain=str(row.get("chain") or row.get("chainId") or self.chain),
            )
            self.tokens[key] = item
        item.count += 1
        item.max_market_cap = max(item.max_market_cap, to_float(row.get("market_cap") or row.get("marketCap") or row.get("mcap")))

    def score(self) -> float:
        unique_tokens = len(self.tokens)
        return round(
            self.source_score
            + self.events * 4.0
            + unique_tokens * 8.0
            + min(30.0, self.total_buy_usd / 500.0)
            + min(35.0, self.smart_money_sum / 4.0)
            + min(20.0, self.quality_score / 4.0),
            2,
        )

    def to_row(self, rank: int) -> dict[str, Any]:
        tokens = sorted(self.tokens.values(), key=lambda item: (-item.count, -item.max_market_cap, item.symbol))[:5]
        return {
            "rank": rank,
            "score": self.score(),
            "address": self.address,
            "chain": self.chain,
            "aliases": sorted(self.aliases),
            "sources": sorted(self.sources),
            "events": self.events,
            "buy_events": self.buy_events,
            "unique_tokens": len(self.tokens),
            "total_buy_usd": round(self.total_buy_usd, 2),
            "smart_money_sum": round(self.smart_money_sum, 2),
            "quality_score": round(self.quality_score, 2),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "representative_tokens": [
                {
                    "symbol": item.symbol,
                    "token": item.token,
                    "chain": item.chain,
                    "events": item.count,
                    "max_market_cap": round(item.max_market_cap, 2),
                }
                for item in tokens
            ],
            "status": "candidate_watch_only",
        }


def wallet_addresses_from_row(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field_name in WALLET_FIELDS:
        values.extend(split_wallet_values(row.get(field_name)))
    for field_name in WALLET_LIST_FIELDS:
        values.extend(split_wallet_values(row.get(field_name)))
    addresses: list[str] = []
    for value in values:
        normalized = normalize_wallet_address(value)
        if normalized and normalized not in addresses:
            addresses.append(normalized)
    return addresses


def resolve_gmgn_runner() -> tuple[list[str], dict[str, Any]]:
    configured = os.environ.get("GMGN_SMART_MONEY_CLI_PATH", "").strip()
    if configured:
        path = Path(configured)
        if path.exists():
            if path.suffix.lower() == ".js":
                return ["node", str(path)], {"mode": "configured_node", "path": str(path)}
            return [str(path)], {"mode": "configured_binary", "path": str(path)}
        found = shutil.which(configured)
        if found:
            return [found], {"mode": "configured_path", "path": found}
        return [], {"available": False, "reason": "configured_cli_missing", "path": configured}

    found = shutil.which("gmgn-cli")
    if found:
        return [found], {"mode": "global", "path": found}
    if GMGN_DIST_CLI.exists():
        return ["node", str(GMGN_DIST_CLI)], {"mode": "local_dist", "path": str(GMGN_DIST_CLI)}
    return [], {"available": False, "reason": "gmgn_cli_not_found", "expected_dist": str(GMGN_DIST_CLI)}


def gmgn_env() -> tuple[dict[str, str], dict[str, Any]]:
    env = dict(os.environ)
    loaded_from: list[str] = []
    for path in (ROOT / ".env.local", ROOT / ".env", GMGN_SKILLS_DIR / ".env"):
        values = read_env_file(path)
        if values:
            loaded_from.append(str(path))
        for key, value in values.items():
            env.setdefault(key, value)
    if not env.get("GMGN_API_KEY"):
        env["GMGN_API_KEY"] = DEFAULT_READ_ONLY_GMGN_API_KEY
        env.pop("GMGN_PRIVATE_KEY", None)
        return env, {
            "configured": True,
            "loaded_from": loaded_from,
            "default_read_only_key_used": True,
            "private_key_forwarded": False,
        }
    env.pop("GMGN_PRIVATE_KEY", None)
    return env, {"configured": True, "loaded_from": loaded_from, "default_read_only_key_used": False, "private_key_forwarded": False}


def chain_list(raw: str | None = None) -> list[str]:
    value = raw or os.environ.get("GMGN_SMART_MONEY_CHAINS") or os.environ.get("ALPHA_MEME_CHAINS") or ",".join(DEFAULT_CHAINS)
    chains = [item.strip().lower() for item in re.split(r"[,;\s]+", value) if item.strip()]
    return list(dict.fromkeys(chains)) or list(DEFAULT_CHAINS)


def extract_json_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in GMGN_ROW_FIELDS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_json_rows(value)
            if nested:
                return nested
    return [payload]


def normalize_gmgn_trade(row: dict[str, Any], chain: str) -> dict[str, Any]:
    base_token = row.get("base_token") if isinstance(row.get("base_token"), dict) else {}
    maker_info = row.get("maker_info") if isinstance(row.get("maker_info"), dict) else {}
    maker = (
        row.get("wallet_address")
        or row.get("walletAddress")
        or row.get("wallet")
        or row.get("maker")
        or row.get("maker_address")
        or row.get("makerAddress")
        or row.get("trader")
        or row.get("trader_address")
        or row.get("traderAddress")
    )
    token = (
        row.get("token_address")
        or row.get("tokenAddress")
        or row.get("address")
        or row.get("base_address")
        or row.get("baseAddress")
        or base_token.get("address")
        or base_token.get("token_address")
        or row.get("mint")
    )
    symbol = (
        row.get("symbol")
        or row.get("token_symbol")
        or row.get("tokenSymbol")
        or row.get("base_symbol")
        or row.get("baseSymbol")
        or row.get("token_name")
        or row.get("tokenName")
        or row.get("name")
        or base_token.get("symbol")
        or base_token.get("name")
    )
    amount_usd = (
        row.get("amount_usd")
        or row.get("amountUsd")
        or row.get("usd_amount")
        or row.get("usdAmount")
        or row.get("value_usd")
        or row.get("valueUsd")
        or row.get("volume_usd")
        or row.get("volumeUsd")
    )
    side = row.get("side") or row.get("event") or row.get("type") or "buy"
    name = (
        row.get("maker_name")
        or row.get("makerName")
        or row.get("wallet_name")
        or row.get("walletName")
        or row.get("label")
        or maker_info.get("twitter_name")
        or maker_info.get("twitter_username")
        or maker_info.get("name")
    )
    return {
        **row,
        "source": "gmgn_cli_smartmoney",
        "chain": chain,
        "wallet": maker,
        "tokenAddress": token,
        "symbol": symbol or "UNKNOWN",
        "monitor985_trade_side": side,
        "monitor985_trade_amount_usd": amount_usd,
        "walletName": name,
        "observed_at": row.get("timestamp") or row.get("time") or now_iso(),
    }


def collect_gmgn_cli_rows(*, limit: int, chains: list[str] | None = None, timeout_seconds: int = DEFAULT_CLI_TIMEOUT_SECONDS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if os.environ.get("GMGN_SMART_MONEY_CLI_DISABLE", "").strip():
        return [], {"ok": True, "enabled": False, "reason": "disabled"}
    runner, runner_status = resolve_gmgn_runner()
    env, env_status = gmgn_env()
    status: dict[str, Any] = {
        "ok": False,
        "enabled": True,
        "runner": runner_status,
        "env": env_status,
        "chains": chains or chain_list(),
        "rows": 0,
        "errors": [],
    }
    if not runner:
        status.update({"ok": False, "reason": runner_status.get("reason") or "runner_missing"})
        return [], status
    if not env_status.get("configured"):
        status.update({"ok": False, "reason": env_status.get("reason") or "GMGN_API_KEY_missing"})
        return [], status

    rows: list[dict[str, Any]] = []
    for chain in chains or chain_list():
        command = [
            *runner,
            "track",
            "smartmoney",
            "--chain",
            chain,
            "--limit",
            str(min(200, max(1, limit))),
            "--side",
            "buy",
            "--raw",
        ]
        try:
            proc = subprocess.run(
                command,
                cwd=GMGN_SKILLS_DIR if GMGN_SKILLS_DIR.exists() else ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=max(3, timeout_seconds),
            )
        except Exception as exc:  # noqa: BLE001
            status["errors"].append({"chain": chain, "error": str(exc)})
            continue
        if proc.returncode != 0:
            status["errors"].append({"chain": chain, "returncode": proc.returncode, "stderr": (proc.stderr or "").strip()[-800:]})
            continue
        try:
            payload = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            status["errors"].append({"chain": chain, "error": f"json_parse_failed: {exc}", "stdout": proc.stdout.strip()[:400]})
            continue
        for row in extract_json_rows(payload):
            normalized = normalize_gmgn_trade(row, chain)
            if wallet_addresses_from_row(normalized):
                rows.append(normalized)
    status["rows"] = len(rows)
    status["ok"] = bool(rows)
    if not rows and not status["errors"]:
        status["reason"] = "empty"
    return rows, status


def build_wallet_rows(
    inbox_dir: Path,
    limit: int = DEFAULT_LIMIT,
    *,
    gmgn_rows: list[dict[str, Any]] | None = None,
    gmgn_cli_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    wallets: dict[str, WalletScore] = {}
    source_files: list[str] = []
    token_level_rows = 0
    wallet_event_rows = 0
    gmgn_wallet_event_rows = 0
    for path in sorted(inbox_dir.glob("*.json")) if inbox_dir.exists() else []:
        payload = read_json(path)
        rows = nested_rows(payload)
        if not rows:
            continue
        source = source_label_from_payload(path, payload)
        source_files.append(str(path))
        for row in rows:
            addresses = wallet_addresses_from_row(row)
            if not addresses:
                if to_float(row.get("smart_money")) > 0:
                    token_level_rows += 1
                continue
            wallet_event_rows += 1
            chain = str(row.get("chain") or row.get("chainId") or "unknown").strip().lower() or "unknown"
            for address in addresses:
                key = f"{chain}:{address}"
                wallet = wallets.get(key)
                if not wallet:
                    wallet = WalletScore(address=address, chain=chain)
                    wallets[key] = wallet
                wallet.add(row, source)

    for row in gmgn_rows or []:
        addresses = wallet_addresses_from_row(row)
        if not addresses:
            continue
        gmgn_wallet_event_rows += 1
        wallet_event_rows += 1
        source = "gmgn_cli_smartmoney"
        chain = str(row.get("chain") or row.get("chainId") or "unknown").strip().lower() or "unknown"
        for address in addresses:
            key = f"{chain}:{address}"
            wallet = wallets.get(key)
            if not wallet:
                wallet = WalletScore(address=address, chain=chain)
                wallets[key] = wallet
            wallet.add(row, source)

    ranked = sorted(wallets.values(), key=lambda wallet: (-wallet.score(), -wallet.events, wallet.address))
    rows = [wallet.to_row(index + 1) for index, wallet in enumerate(ranked[:limit])]
    complete = len(rows) >= limit
    return {
        "ok": True,
        "complete": complete,
        "insufficient_wallet_addresses": not complete,
        "generated_at": now_iso(),
        "updated_at": now_iso(),
        "source": "gmgn_cli_smartmoney_plus_local_wallet_attribution" if gmgn_rows else "local_meme_source_inbox_wallet_attribution",
        "requested_count": limit,
        "count": len(rows),
        "source_files": source_files,
        "wallet_event_rows": wallet_event_rows,
        "gmgn_wallet_event_rows": gmgn_wallet_event_rows,
        "gmgn_cli_status": gmgn_cli_status or {"ok": False, "reason": "not_run"},
        "token_level_smart_money_rows": token_level_rows,
        "limitation": ""
        if complete
        else "当前可归因真实钱包地址不足 50；若 gmgn_cli_status 显示 GMGN_API_KEY_missing，需要配置 GMGN_API_KEY 后才能直接拉 GMGN 官方 smartmoney。",
        "wallets": rows,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def render_markdown(payload: dict[str, Any]) -> str:
    rows = payload.get("wallets") if isinstance(payload.get("wallets"), list) else []
    lines = [
        "# GMGN Qualified Profit-History Wallets",
        "",
        f"- Updated: {payload.get('updated_at') or payload.get('generated_at') or ''}",
        f"- Oldest published wallet evidence: {payload.get('evidence_checked_at') or 'none'}",
        "- Updated is publication time; generated_at/evidence_checked_at retain the oldest wallet evidence time.",
        f"- Count: {payload.get('count', 0)}/{payload.get('requested_count', DEFAULT_LIMIT)}",
        f"- Policy: {POLICY_ID}; schema 2",
        "- Source: sibling gmgn-profitable-wallets.json; local evidence only.",
        "- Requires supported history, 30d realized profit > $500 / 20 tokens / 20 sells,",
        "  and 7d realized profit > $0 / 5 tokens / 5 sells.",
        "- Activity: >=5 tokens, >=3 tokens with both sides, >=10 costed sells,",
        "  >=80% sell basis coverage, >=3 profitable tokens, positive sampled margin;",
        "  largest token trade share <=75% and positive-margin share <=80%.",
        "- Evidence timestamps must be within 3 days and not in the future.",
        "- KOL/leader labels confer no qualification and do not exclude qualifying wallets.",
        "- Ranking balances chains, then reported profit within each chain; no padding to 50.",
        "- Reported realized profit and partial activity remain upstream claims;",
        "  full-ledger fee-inclusive profit and copy-trading returns are not independently verified.",
        "",
    ]
    if payload.get("limitation"):
        lines.extend([f"> {payload['limitation']}", ""])
    lines.extend(
        [
            "| Rank | Chain | Address | 30d realized USD | 7d realized USD | Sell basis coverage | Evidence checked |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['rank']} | {row['chain']} | {row['address']} | "
            f"{float(row['stats_30d']['realized_profit_usd']):.2f} | "
            f"{float(row['stats_7d']['realized_profit_usd']):.2f} | "
            f"{float(row['activity']['sell_cost_basis_coverage']):.1%} | {row['evidence_checked_at']} |"
        )
    return "\n".join(lines) + "\n"


def should_refresh(path: Path, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> bool:
    if not path.exists():
        return True
    payload = read_json(path)
    if not isinstance(payload, dict):
        return True
    curated = payload.get("policy_id") == POLICY_ID
    stamp = payload.get("evidence_checked_at") if curated else payload.get("updated_at") or payload.get("generated_at")
    if isinstance(stamp, str) and stamp.strip():
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if curated and parsed.tzinfo is None:
                return True
            age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
            return age < 0 or age >= max_age_days * 86400
        except ValueError:
            pass
    if curated:
        return True
    return time.time() - path.stat().st_mtime >= max_age_days * 86400


def run_once(
    *,
    inbox_dir: Path = INBOX_DIR,
    json_out: Path = DEFAULT_JSON_OUT,
    markdown_out: Path = DEFAULT_MD_OUT,
    limit: int = DEFAULT_LIMIT,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
    chains: list[str] | None = None,
    cli_timeout_seconds: int = DEFAULT_CLI_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    # Legacy options stay callable, but cannot bypass the publication policy.
    # Revalidate each invocation so fresh output cannot preserve expired evidence.
    source_path = json_out.parent / "gmgn-profitable-wallets.json"
    if json_out.resolve() == source_path.resolve() or markdown_out.resolve() in {source_path.resolve(), json_out.resolve()}:
        raise ValueError("Publisher output paths must be distinct from the source and each other")
    source = read_json(source_path)
    updated = now_iso()
    wallets = qualified_wallets(source, updated, limit=limit)
    evidence_checked_at = min(
        (row["evidence_checked_at"] for row in wallets),
        key=lambda stamp: datetime.fromisoformat(stamp.replace("Z", "+00:00")),
        default=None,
    )
    payload = {
        "schema_version": 2, "policy_id": POLICY_ID, "selection_policy": POLICY,
        "updated_at": updated, "qualification_checked_at": updated,
        "generated_at": evidence_checked_at, "evidence_checked_at": evidence_checked_at,
        "source_report": str(source_path),
        "source_updated_at": source.get("updated_at") if isinstance(source, dict) else None,
        "source_status": "available" if isinstance(source, dict) else "missing_or_invalid",
        "count": len(wallets), "requested_count": limit, "complete": len(wallets) >= limit,
        "insufficient_wallet_addresses": len(wallets) < limit,
        "limitation": "Only qualifying dated profit-history evidence is published; the list may contain fewer than 50 wallets.",
        "gmgn_cli_status": {"ok": True, "skipped": True, "called": False,
                            "reason": "not_required_local_evidence"},
        "wallets": wallets,
    }
    previous = read_json(json_out)
    archives = []
    if not isinstance(previous, dict) or previous.get("policy_id") != POLICY_ID or previous.get("schema_version") != 2:
        for path in (json_out, markdown_out):
            archive = path.with_name(path.stem + ".legacy-watchlist" + path.suffix)
            if path.exists():
                if not archive.exists():
                    # Exclusive creation preserves the first legacy bytes on reruns.
                    with archive.open("xb") as handle:
                        handle.write(path.read_bytes())
                archives.append(str(archive))
    write_json(json_out, payload)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.write_text(render_markdown(payload), encoding="utf-8")
    return {
        "ok": True,
        "skipped": False,
        "complete": payload.get("complete"),
        "insufficient_wallet_addresses": payload.get("insufficient_wallet_addresses"),
        "count": payload.get("count"),
        "requested_count": payload.get("requested_count"),
        "out": str(json_out),
        "markdown": str(markdown_out),
        "limitation": payload.get("limitation") or "",
        "gmgn_cli_status": payload["gmgn_cli_status"],
        "schema_version": 2, "policy_id": POLICY_ID,
        "updated_at": updated, "generated_at": evidence_checked_at,
        "evidence_checked_at": evidence_checked_at,
        "source_status": payload["source_status"], "archives": archives,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish qualified wallets from sibling gmgn-profitable-wallets.json; no API calls.")
    parser.add_argument("--inbox-dir", type=Path, default=INBOX_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD_OUT)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--chains", default=None, help="Comma/space separated chains, default from GMGN_SMART_MONEY_CHAINS or ALPHA_MEME_CHAINS.")
    parser.add_argument("--cli-timeout-seconds", type=int, default=DEFAULT_CLI_TIMEOUT_SECONDS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    status = run_once(
        inbox_dir=args.inbox_dir,
        json_out=args.out,
        markdown_out=args.markdown,
        limit=max(1, args.limit),
        max_age_days=max(0.0, args.max_age_days),
        force=args.force,
        chains=chain_list(args.chains),
        cli_timeout_seconds=max(3, args.cli_timeout_seconds),
    )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
