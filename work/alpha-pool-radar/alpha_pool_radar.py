#!/usr/bin/env python3
"""
Read-only Binance Alpha pool radar.

It combines:
- Binance Alpha token list and Alpha market fields.
- DexScreener token pairs for DEX liquidity, volume, pool age, and socials.
- Binance USD-M futures market data for contract listing, OI change, and funding.

No private keys, no account endpoints, no order endpoints.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_http import (
    HTTP_EVENTS,
    cache_path_for_url,
    configure_cache,
    data_quality_summary,
    http_json,
    read_http_cache,
    reset_data_quality,
    source_name,
)
from alpha_market import (
    dex_chain_id,
    fetch_alpha_tokens,
    fetch_dex_for_tokens,
    fetch_dex_pairs,
    fetch_funding,
    fetch_futures_details,
    fetch_futures_index,
    fetch_oi_change,
    find_futures_symbol,
    pick_best_pair,
    strip_multiplier,
)
from alpha_onchain import (
    apply_holder_metrics,
    apply_risk_metrics,
    apply_risk_metrics_to_row,
    evm_scan_api_key,
    fetch_bscscan_top_holders,
    fetch_goplus_token_security,
    fetch_rugcheck_report,
    fetch_rugcheck_summary,
    fetch_solana_holder_metrics,
    fetch_top_holders,
    goplus_risk_metrics,
    holder_api_key,
    holder_metrics_from_rows,
    holder_quantity,
    inferred_total_supply,
    is_evm_chain_id,
    normalize_holder_provider,
    normalize_holder_quantities,
    parse_bscscan_holder_export,
    resolve_holder_provider,
    risk_level_from_score,
    rugcheck_holder_metrics,
    rugcheck_risk_metrics,
    solana_amount_value,
    solana_holder_metrics_from_rpc,
    solana_rpc_request,
)
from alpha_radar_sources import (
    BINANCE_ALPHA_LIST_URL,
    CHAIN_ID_TO_DEX,
    DEX_TOKEN_PAIRS_URL,
    FAPI_24H_TICKER_URL,
    FAPI_EXCHANGE_INFO_URL,
    FAPI_FUNDING_URL,
    FAPI_OI_HIST_URL,
    GOPLUS_TOKEN_SECURITY_URL,
    REQUEST_HEADERS,
    RUGCHECK_SUMMARY_URL,
)
from alpha_scoring import score_row as apply_score_row


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, str) and value.strip() == "":
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def short_money(value: Any) -> str:
    amount = to_float(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    for suffix, denom in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if amount >= denom:
            return f"{sign}{amount / denom:.2f}{suffix}"
    return f"{sign}{amount:.0f}"


def pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{to_float(value):.{digits}f}%"


@dataclass
class RadarRow:
    symbol: str
    name: str
    chain: str
    contract_address: str
    alpha_id: str
    market_cap: float
    fdv: float
    holders: int
    alpha_volume24h: float
    alpha_change24h: float
    alpha_liquidity: float
    listing_cex: bool
    hot_tag: bool
    chain_id: str = ""
    price_usd: float = 0.0
    dex_url: str = ""
    image_url: str = ""
    dex_liquidity: float = 0.0
    dex_volume24h: float = 0.0
    dex_market_cap: float = 0.0
    pair_age_hours: float | None = None
    txns24h: int = 0
    buy_sell_ratio24h: float | None = None
    social_count: int = 0
    futures_symbol: str = ""
    futures_quote_volume24h: float = 0.0
    futures_price_change24h: float = 0.0
    funding_rate_pct: float | None = None
    oi_change_1h_pct: float | None = None
    oi_value: float | None = None
    futures_metrics: dict[str, Any] = field(default_factory=dict)
    top10_holder_pct: float | None = None
    top20_holder_pct: float | None = None
    max_holder_pct: float | None = None
    holder_contract_count: int | None = None
    holder_source: str = ""
    holder_observed_at: str = ""
    risk_score: float | None = None
    risk_level: str = ""
    risk_flags: list[str] = field(default_factory=list)
    risk_source: str = ""
    stage: str = ""
    direction: str = ""
    action: str = ""
    reason: str = ""
    key_level: float | None = None
    invalid_level: float | None = None
    holding_text: str = ""
    protection_text: str = ""
    stage_change_pct: float | None = None
    stage_volume_to_mcap_pct: float | None = None
    alpha_stage: str = ""
    alpha_stage_label: str = ""
    alpha_type: str = ""
    alpha_label: str = ""
    alpha_explain: str = ""
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "symbol": self.symbol,
            "name": self.name,
            "chain": self.chain,
            "chain_id": self.chain_id,
            "alpha_id": self.alpha_id,
            "contract_address": self.contract_address,
            "price_usd": self.price_usd,
            "market_cap": self.market_cap,
            "fdv": self.fdv,
            "holders": self.holders,
            "alpha_volume24h": self.alpha_volume24h,
            "alpha_change24h_pct": self.alpha_change24h,
            "alpha_liquidity": self.alpha_liquidity,
            "dex_url": self.dex_url,
            "image_url": self.image_url,
            "dex_liquidity": self.dex_liquidity,
            "dex_volume24h": self.dex_volume24h,
            "dex_market_cap": self.dex_market_cap,
            "pair_age_hours": self.pair_age_hours,
            "txns24h": self.txns24h,
            "buy_sell_ratio24h": self.buy_sell_ratio24h,
            "social_count": self.social_count,
            "futures_symbol": self.futures_symbol,
            "futures_quote_volume24h": self.futures_quote_volume24h,
            "futures_price_change24h_pct": self.futures_price_change24h,
            "funding_rate_pct": self.funding_rate_pct,
            "oi_change_1h_pct": self.oi_change_1h_pct,
            "oi_value": self.oi_value,
            **self.futures_metrics,
            "top10_holder_pct": self.top10_holder_pct,
            "top20_holder_pct": self.top20_holder_pct,
            "max_holder_pct": self.max_holder_pct,
            "holder_contract_count": self.holder_contract_count,
            "holder_source": self.holder_source,
            "holder_observed_at": self.holder_observed_at,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "risk_flags": ";".join(self.risk_flags),
            "risk_source": self.risk_source,
            "stage": self.stage,
            "direction": self.direction,
            "action": self.action,
            "reason": self.reason,
            "key_level": self.key_level,
            "invalid_level": self.invalid_level,
            "holding_text": self.holding_text,
            "protection_text": self.protection_text,
            "stage_change_pct": self.stage_change_pct,
            "stage_volume_to_mcap_pct": self.stage_volume_to_mcap_pct,
            "alpha_stage": self.alpha_stage,
            "alpha_stage_label": self.alpha_stage_label,
            "alpha_type": self.alpha_type,
            "alpha_label": self.alpha_label,
            "alpha_explain": self.alpha_explain,
            "listing_cex": self.listing_cex,
            "hot_tag": self.hot_tag,
            "flags": ";".join(self.flags),
            "notes": "; ".join(self.notes),
        }


def pair_metrics(pair: dict[str, Any] | None, now_ms: int) -> dict[str, Any]:
    if not pair:
        return {}
    txns = pair.get("txns") or {}
    h24_txns = txns.get("h24") or {}
    buys = to_int(h24_txns.get("buys"))
    sells = to_int(h24_txns.get("sells"))
    created_at = to_int(pair.get("pairCreatedAt"), 0)
    info = pair.get("info") or {}
    socials = info.get("socials") or []
    websites = info.get("websites") or []
    return {
        "url": pair.get("url") or "",
        "image_url": str(info.get("imageUrl") or info.get("image") or ""),
        "price_usd": to_float(pair.get("priceUsd")),
        "liquidity": to_float((pair.get("liquidity") or {}).get("usd")),
        "volume24h": to_float((pair.get("volume") or {}).get("h24")),
        "market_cap": to_float(pair.get("marketCap") or pair.get("fdv")),
        "pair_age_hours": ((now_ms - created_at) / 3_600_000) if created_at else None,
        "txns24h": buys + sells,
        "buy_sell_ratio24h": (buys / sells) if sells else (float(buys) if buys else None),
        "social_count": len(socials) + len(websites),
    }


def score_row(row: RadarRow, max_market_cap: float) -> None:
    apply_score_row(row, max_market_cap=max_market_cap)


def build_rows(
    tokens: list[dict[str, Any]],
    dex_pairs: dict[str, list[dict[str, Any]]],
    futures_index: tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    futures_details: dict[str, dict[str, Any]],
    max_market_cap: float,
) -> list[RadarRow]:
    now = utc_now_ms()
    symbol_by_clean_base, _symbols, tickers = futures_index
    rows: list[RadarRow] = []
    for token in tokens:
        symbol = str(token.get("symbol") or "").strip()
        contract_address = str(token.get("contractAddress") or "")
        token_key = f"{token.get('chainId')}:{contract_address.lower()}"
        best_pair = pick_best_pair(token, dex_pairs.get(token_key, []))
        dex = pair_metrics(best_pair, now)

        futures_symbol = find_futures_symbol(symbol, symbol_by_clean_base) or ""
        ticker = tickers.get(futures_symbol) or {}
        details = futures_details.get(futures_symbol) or {}

        row = RadarRow(
            symbol=symbol,
            name=str(token.get("name") or ""),
            chain=str(token.get("chainName") or token.get("chainId") or ""),
            chain_id=str(token.get("chainId") or ""),
            contract_address=contract_address,
            alpha_id=str(token.get("alphaId") or ""),
            price_usd=to_float(dex.get("price_usd")),
            market_cap=to_float(token.get("marketCap")),
            fdv=to_float(token.get("fdv")),
            holders=to_int(token.get("holders")),
            alpha_volume24h=to_float(token.get("volume24h")),
            alpha_change24h=to_float(token.get("percentChange24h")),
            alpha_liquidity=to_float(token.get("liquidity")),
            listing_cex=bool(token.get("listingCex")),
            hot_tag=bool(token.get("hotTag")),
            dex_url=dex.get("url", ""),
            image_url=dex.get("image_url", ""),
            dex_liquidity=to_float(dex.get("liquidity")),
            dex_volume24h=to_float(dex.get("volume24h")),
            dex_market_cap=to_float(dex.get("market_cap")),
            pair_age_hours=dex.get("pair_age_hours"),
            txns24h=to_int(dex.get("txns24h")),
            buy_sell_ratio24h=dex.get("buy_sell_ratio24h"),
            social_count=to_int(dex.get("social_count")),
            futures_symbol=futures_symbol,
            futures_quote_volume24h=to_float(ticker.get("quoteVolume")),
            futures_price_change24h=to_float(ticker.get("priceChangePercent")),
            funding_rate_pct=details.get("funding_rate_pct"),
            oi_change_1h_pct=details.get("oi_change_1h_pct"),
            oi_value=details.get("oi_value"),
            futures_metrics={key: details.get(key) for key in ("oi_basis", "oi_value_change_1h_pct", "oi_observed_at_ms", "funding_settled_at_ms", "funding_interval_hours", "funding_rate_8h_pct", "funding_history")},
        )
        score_row(row, max_market_cap=max_market_cap)
        rows.append(row)
    rows.sort(key=lambda item: item.score, reverse=True)
    return rows


def filter_tokens(
    tokens: list[dict[str, Any]],
    max_market_cap: float,
    min_volume: float,
    chains: set[str] | None,
) -> list[dict[str, Any]]:
    filtered = []
    for token in tokens:
        market_cap = to_float(token.get("marketCap") or token.get("fdv"))
        volume = to_float(token.get("volume24h"))
        chain = str(token.get("chainName") or token.get("chainId") or "").lower()
        if market_cap <= 0 or market_cap > max_market_cap:
            continue
        if volume < min_volume:
            continue
        if chains and chain not in chains and dex_chain_id(token).lower() not in chains:
            continue
        filtered.append(token)
    return filtered


def write_json(path: Path, rows: list[RadarRow], meta: dict[str, Any]) -> None:
    payload = {"meta": meta, "rows": [row.as_dict() for row in rows]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[RadarRow]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].as_dict().keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())


def write_markdown(path: Path, rows: list[RadarRow], meta: dict[str, Any]) -> None:
    quality = meta.get("data_quality") or {}
    lines = [
        "# Alpha Pool Radar (read-only)",
        "",
        f"- Generated: {meta['generated_at']}",
        f"- Alpha candidates: {meta['alpha_count']}; filtered: {meta['filtered_count']}; output: {meta['output_count']}",
        f"- Filter: market cap <= {short_money(meta['max_market_cap'])}; Alpha 24h volume >= {short_money(meta['min_volume'])}",
        f"- Data quality: {quality.get('overall', 'unknown')} | cached requests: {quality.get('cached_count', 0)} | failed requests: {quality.get('failed_count', 0)}",
        "- Top10 holder concentration is reserved for a later explorer adapter.",
        "",
    ]
    if meta.get("errors"):
        lines.extend(["## Fetch Errors", ""])
        for error in meta["errors"][:20]:
            lines.append(f"- {error}")
        if len(meta["errors"]) > 20:
            lines.append(f"- ...and {len(meta['errors']) - 20} more")
        lines.append("")

    lines.extend(
        [
            "## Ranking",
            "",
            "| # | Score | Token | Chain | MCap | Alpha 24h Vol | Dex 24h Vol | Top10 | Pool Age | Futures | OI 1h | Funding | Flags |",
            "|---:|---:|---|---|---:|---:|---:|---:|---:|---|---:|---:|---|",
        ]
    )
    for idx, row in enumerate(rows, start=1):
        age = "" if row.pair_age_hours is None else f"{row.pair_age_hours:.1f}h"
        token_cell = f"[{row.symbol}]({row.dex_url})" if row.dex_url else row.symbol
        futures = row.futures_symbol or ""
        oi = "" if row.oi_change_1h_pct is None else pct(row.oi_change_1h_pct)
        funding = "" if row.funding_rate_pct is None else pct(row.funding_rate_pct, 4)
        top10 = "" if row.top10_holder_pct is None else pct(row.top10_holder_pct)
        flags = ", ".join(row.flags[:5])
        lines.append(
            "| "
            f"{idx} | {row.score:.1f} | {token_cell} | {row.chain} | "
            f"{short_money(row.market_cap or row.dex_market_cap or row.fdv)} | "
            f"{short_money(row.alpha_volume24h)} | {short_money(row.dex_volume24h)} | "
            f"{top10} | {age} | {futures} | {oi} | {funding} | {flags} |"
        )

    lines.extend(["", "## Notes", ""])
    lines.extend(
        [
            "- Score combines small market cap, volume-to-market-cap, Dex volume, pool age, futures listing, OI, funding, and Alpha heat.",
            "- OI 1h uses Binance USDS-M 5m openInterestHist over roughly the latest hour.",
            "- Funding uses the latest Binance USDS-M fundingRate value, shown as a percentage.",
            "- Top10 holder concentration needs a later BscScan/Etherscan/Solscan/Birdeye adapter.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_scan(args: argparse.Namespace) -> tuple[list[RadarRow], dict[str, Any]]:
    started = time.time()
    if getattr(args, "out_dir", None):
        configure_cache(Path(args.out_dir))
    reset_data_quality()
    chains = {item.strip().lower() for item in args.chains.split(",") if item.strip()} or None
    errors: list[str] = []

    print("Fetching Binance Alpha token list...", file=sys.stderr)
    tokens = fetch_alpha_tokens(limit=args.alpha_limit)
    filtered = filter_tokens(
        tokens,
        max_market_cap=args.max_market_cap,
        min_volume=args.min_volume,
        chains=chains,
    )

    print(f"Filtered {len(filtered)} of {len(tokens)} Alpha tokens.", file=sys.stderr)
    if args.skip_dex:
        dex_pairs = {}
    else:
        print("Fetching DexScreener token pairs...", file=sys.stderr)
        dex_pairs, dex_errors = fetch_dex_for_tokens(filtered, concurrency=args.concurrency)
        errors.extend(dex_errors)

    print("Fetching Binance futures index...", file=sys.stderr)
    futures_index = fetch_futures_index()
    symbol_by_clean_base, _symbols, _tickers = futures_index
    futures_symbols = sorted(
        {
            found
            for token in filtered
            for found in [find_futures_symbol(str(token.get("symbol") or ""), symbol_by_clean_base)]
            if found
        }
    )

    if args.skip_futures_details:
        futures_details = {}
    else:
        print(f"Fetching OI/funding for {len(futures_symbols)} futures symbols...", file=sys.stderr)
        futures_details, futures_errors = fetch_futures_details(
            futures_symbols,
            concurrency=min(args.concurrency, 6),
        )
        errors.extend(futures_errors)

    rows = build_rows(
        filtered,
        dex_pairs=dex_pairs,
        futures_index=futures_index,
        futures_details=futures_details,
        max_market_cap=args.max_market_cap,
    )

    holder_errors = apply_holder_metrics(rows, args)
    errors.extend(holder_errors)
    risk_errors = apply_risk_metrics(rows, args)
    errors.extend(risk_errors)
    rows.sort(key=lambda item: item.score, reverse=True)
    rows = rows[: args.top]

    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    meta = {
        "generated_at": generated_at,
        "runtime_seconds": round(time.time() - started, 2),
        "alpha_count": len(tokens),
        "filtered_count": len(filtered),
        "output_count": len(rows),
        "max_market_cap": args.max_market_cap,
        "min_volume": args.min_volume,
        "chains": sorted(chains) if chains else [],
        "errors": errors,
        "data_quality": data_quality_summary(),
        "holder_adapter": {
            "enabled": bool(getattr(args, "holders_enable", False)),
            "provider": normalize_holder_provider(getattr(args, "holder_provider", "routescan")),
            "api_key_present": bool(holder_api_key(getattr(args, "holder_provider", "routescan"))),
            "top_tokens": getattr(args, "holder_top_tokens", 12),
            "offset": getattr(args, "holder_offset", 20),
            "source": f"{normalize_holder_provider(getattr(args, 'holder_provider', 'routescan'))}_holder_adapter",
            "errors": holder_errors,
        },
        "risk_adapter": {
            "enabled": bool(getattr(args, "risk_enable", False)),
            "top_tokens": getattr(args, "risk_top_tokens", 12),
            "goplus_key_present": bool(os.environ.get("GOPLUS_API_KEY", "").strip()),
            "sources": ["goplus", "rugcheck"],
            "errors": risk_errors,
        },
        "sources": {
            "binance_alpha": BINANCE_ALPHA_LIST_URL,
            "dexscreener": DEX_TOKEN_PAIRS_URL,
            "binance_futures_exchange_info": FAPI_EXCHANGE_INFO_URL,
            "binance_futures_ticker": FAPI_24H_TICKER_URL,
            "binance_futures_funding": FAPI_FUNDING_URL,
            "binance_futures_open_interest_hist": FAPI_OI_HIST_URL,
            "goplus_token_security": GOPLUS_TOKEN_SECURITY_URL,
            "rugcheck_summary": RUGCHECK_SUMMARY_URL,
        },
    }
    return rows, meta


def write_scan_outputs(out_dir: Path, rows: list[RadarRow], meta: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"alpha-pool-radar-{stamp}"
    write_json(base.with_suffix(".json"), rows, meta)
    write_csv(base.with_suffix(".csv"), rows)
    write_markdown(base.with_suffix(".md"), rows, meta)

    latest_md = out_dir / "alpha-pool-radar-latest.md"
    latest_json = out_dir / "alpha-pool-radar-latest.json"
    latest_csv = out_dir / "alpha-pool-radar-latest.csv"
    write_markdown(latest_md, rows, meta)
    write_json(latest_json, rows, meta)
    write_csv(latest_csv, rows)

    print(f"Wrote {latest_md}", file=sys.stderr)
    print(f"Wrote {latest_json}", file=sys.stderr)
    print(f"Wrote {latest_csv}", file=sys.stderr)


def row_key(row: dict[str, Any] | RadarRow) -> str:
    if isinstance(row, RadarRow):
        return f"{row.chain}:{row.contract_address}".lower()
    return f"{row.get('chain')}:{row.get('contract_address')}".lower()


def load_monitor_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"rows": {}, "last_alert_at": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"rows": {}, "last_alert_at": {}}


def save_monitor_state(path: Path, rows: list[RadarRow], previous: dict[str, Any]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "rows": {row_key(row): row.as_dict() for row in rows},
        "last_alert_at": previous.get("last_alert_at", {}),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cooldown_ok(state: dict[str, Any], key: str, cooldown_seconds: int) -> bool:
    last_alert_at = state.setdefault("last_alert_at", {})
    previous = to_float(last_alert_at.get(key))
    return previous <= 0 or (time.time() - previous) >= cooldown_seconds


def mark_alerted(state: dict[str, Any], key: str) -> None:
    state.setdefault("last_alert_at", {})[key] = time.time()


def detect_alerts(rows: list[RadarRow], state: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    previous_rows = state.get("rows") or {}
    alerts: list[dict[str, Any]] = []

    for rank, row in enumerate(rows, start=1):
        key = row_key(row)
        previous = previous_rows.get(key) or {}
        previous_score = to_float(previous.get("score"))
        previous_oi = previous.get("oi_change_1h_pct")
        reasons: list[str] = []

        if row.score >= args.alert_score and not previous:
            reasons.append(f"new high-score candidate score={row.score:.1f}")
        if previous and (row.score - previous_score) >= args.alert_score_jump:
            reasons.append(f"score jump {previous_score:.1f}->{row.score:.1f}")
        if row.oi_change_1h_pct is not None and row.oi_change_1h_pct >= args.alert_oi:
            if previous_oi is None or to_float(previous_oi) < args.alert_oi:
                reasons.append(f"OI 1h {row.oi_change_1h_pct:.2f}%")
        if row.funding_rate_pct is not None and abs(row.funding_rate_pct) >= args.alert_funding_abs:
            previous_funding = previous.get("funding_rate_pct")
            if previous_funding is None or abs(to_float(previous_funding)) < args.alert_funding_abs:
                reasons.append(f"funding {row.funding_rate_pct:.4f}%")
        if row.pair_age_hours is not None and row.pair_age_hours <= args.alert_fresh_pool_hours:
            if not previous:
                reasons.append(f"fresh pool {row.pair_age_hours:.1f}h")

        if reasons and cooldown_ok(state, key, args.alert_cooldown_seconds):
            mark_alerted(state, key)
            alerts.append(
                {
                    "alerted_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "rank": rank,
                    "symbol": row.symbol,
                    "name": row.name,
                    "chain": row.chain,
                    "score": round(row.score, 2),
                    "market_cap": row.market_cap or row.dex_market_cap or row.fdv,
                    "alpha_volume24h": row.alpha_volume24h,
                    "dex_volume24h": row.dex_volume24h,
                    "futures_symbol": row.futures_symbol,
                    "oi_change_1h_pct": row.oi_change_1h_pct,
                    "funding_rate_pct": row.funding_rate_pct,
                    "dex_url": row.dex_url,
                    "flags": row.flags,
                    "reasons": reasons,
                }
            )

    return alerts


def write_alerts(out_dir: Path, alerts: list[dict[str, Any]]) -> None:
    latest = out_dir / "alpha-pool-radar-alerts-latest.md"
    lines = [
        "# Alpha Pool Radar Alerts",
        "",
        f"- Updated: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        "",
    ]
    if not alerts:
        lines.append("- No new alerts this cycle.")
        latest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    alerts_jsonl = out_dir / "alpha-pool-radar-alerts.jsonl"
    with alerts_jsonl.open("a", encoding="utf-8") as handle:
        for alert in alerts:
            handle.write(json.dumps(alert, ensure_ascii=False) + "\n")

    for alert in alerts:
        link = f" [{alert['symbol']}]({alert['dex_url']})" if alert.get("dex_url") else f" {alert['symbol']}"
        reasons = "; ".join(alert["reasons"])
        lines.append(
            f"- #{alert['rank']}{link} score={alert['score']:.1f} "
            f"mcap={short_money(alert['market_cap'])} "
            f"alphaVol={short_money(alert['alpha_volume24h'])} "
            f"dexVol={short_money(alert['dex_volume24h'])} "
            f"futures={alert.get('futures_symbol') or '-'} "
            f"OI={pct(alert.get('oi_change_1h_pct'))} "
            f"funding={pct(alert.get('funding_rate_pct'), 4)} | {reasons}"
        )
    latest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def maybe_beep(enabled: bool) -> None:
    if not enabled:
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        print("\a", end="", flush=True)


def monitor_loop(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    configure_cache(out_dir)
    state_path = out_dir / "alpha-pool-radar-monitor-state.json"
    heartbeat_path = out_dir / "alpha-pool-radar-monitor-heartbeat.json"
    print(f"Monitor started. Interval={args.interval_seconds}s. Outputs={out_dir}", file=sys.stderr)

    while True:
        cycle_started = time.time()
        try:
            rows, meta = run_scan(args)
            write_scan_outputs(out_dir, rows, meta)
            state = load_monitor_state(state_path)
            alerts = detect_alerts(rows, state, args)
            write_alerts(out_dir, alerts)
            save_monitor_state(state_path, rows, state)
            heartbeat = {
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "ok": True,
                "alert_count": len(alerts),
                "top": [row.as_dict() for row in rows[:5]],
                "latest_alerts": alerts,
                "next_run_after_seconds": args.interval_seconds,
            }
            heartbeat_path.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2), encoding="utf-8")
            if alerts:
                maybe_beep(args.beep)
                print(f"ALERT {len(alerts)} new signal(s). See {out_dir / 'alpha-pool-radar-alerts-latest.md'}", file=sys.stderr)
            else:
                print("No new alerts.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            heartbeat = {
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "ok": False,
                "error": str(exc),
                "next_run_after_seconds": args.interval_seconds,
            }
            heartbeat_path.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Monitor cycle failed: {exc}", file=sys.stderr)

        if args.once:
            return 0
        elapsed = time.time() - cycle_started
        time.sleep(max(1, args.interval_seconds - elapsed))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Binance Alpha meme pool radar.")
    parser.add_argument("--monitor", action="store_true", help="Run continuously and write alert files.")
    parser.add_argument("--once", action="store_true", help="Run one monitor cycle and exit.")
    parser.add_argument("--interval-seconds", type=int, default=120, help="Monitor refresh interval.")
    parser.add_argument("--alpha-limit", type=int, default=120, help="Alpha tokens to inspect before filters.")
    parser.add_argument("--top", type=int, default=30, help="Rows to output.")
    parser.add_argument("--max-market-cap", type=float, default=200_000_000, help="Max market cap filter.")
    parser.add_argument("--min-volume", type=float, default=1_000_000, help="Min Alpha 24h volume filter.")
    parser.add_argument("--chains", default="", help="Comma-separated chainName or DexScreener chainId filter.")
    parser.add_argument("--concurrency", type=int, default=8, help="Read-only request concurrency.")
    parser.add_argument("--out-dir", default="outputs", help="Output directory.")
    parser.add_argument("--skip-dex", action="store_true", help="Skip DexScreener calls.")
    parser.add_argument("--skip-futures-details", action="store_true", help="Only detect futures listing, skip OI/funding.")
    parser.add_argument("--holders-enable", action="store_true", help="Enable optional EVM TopHolders adapter.")
    parser.add_argument("--holder-provider", default="routescan", choices=["routescan", "etherscan", "blockscout", "bscscan", "solana_rpc", "auto", "goplus"], help="Holder data provider.")
    parser.add_argument("--holder-top-tokens", type=int, default=12, help="Top ranked EVM rows to inspect for holders.")
    parser.add_argument("--holder-offset", type=int, default=20, help="Top holders to request per token.")
    parser.add_argument("--risk-enable", action="store_true", help="Enable optional GoPlus/RugCheck risk adapter.")
    parser.add_argument("--risk-top-tokens", type=int, default=12, help="Top ranked rows to inspect for contract risk.")
    parser.add_argument("--alert-score", type=float, default=58.0, help="Alert when a new candidate reaches this score.")
    parser.add_argument("--alert-score-jump", type=float, default=8.0, help="Alert when score jumps by this much.")
    parser.add_argument("--alert-oi", type=float, default=5.0, help="Alert when 1h OI change crosses this percent.")
    parser.add_argument("--alert-funding-abs", type=float, default=0.05, help="Alert when absolute funding percent crosses this.")
    parser.add_argument("--alert-fresh-pool-hours", type=float, default=24.0, help="Alert on new fresh pools below this age.")
    parser.add_argument("--alert-cooldown-seconds", type=int, default=1800, help="Per-token alert cooldown.")
    parser.add_argument("--beep", action="store_true", help="Play a Windows notification beep when alerts fire.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    configure_cache(out_dir)
    if args.monitor or args.once:
        return monitor_loop(args)
    rows, meta = run_scan(args)
    write_scan_outputs(out_dir, rows, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

