#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any, Callable

from alpha_http import configure_cache
from alpha_http import data_quality_summary
from alpha_http import http_json
from alpha_http import reset_data_quality
from alpha_radar_sources import DEX_TOKEN_PAIRS_URL


GOLD_MIN_PEAK_MCAP = 1_000_000.0


def to_float(value: Any) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_candidate_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [row for row in payload["rows"] if isinstance(row, dict)]
    return []


def pair_liquidity_usd(pair: dict[str, Any]) -> float:
    liquidity = pair.get("liquidity") or {}
    return to_float(liquidity.get("usd") if isinstance(liquidity, dict) else None)


def pair_mcap(pair: dict[str, Any]) -> float:
    return to_float(pair.get("marketCap") or pair.get("fdv"))


def select_best_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [pair for pair in pairs if isinstance(pair, dict)]
    if not valid:
        return None
    return max(valid, key=lambda pair: (pair_liquidity_usd(pair), pair_mcap(pair)))


def dex_pairs_for_token(
    address: str,
    chain: str = "solana",
    http_get: Callable[..., Any] = http_json,
) -> list[dict[str, Any]]:
    if not address:
        return []
    url = DEX_TOKEN_PAIRS_URL.format(chain=chain, address=address)
    payload = http_get(url, timeout=15)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("pairs"), list):
        return [row for row in payload["pairs"] if isinstance(row, dict)]
    return []


def mcap_confirmation(current_mcap: float) -> str:
    if current_mcap >= GOLD_MIN_PEAK_MCAP:
        return "current_over_1m"
    if current_mcap > 0:
        return "current_below_1m_peak_unknown"
    return "no_dex_mcap"


def enrich_candidate(
    row: dict[str, Any],
    http_get: Callable[..., Any] = http_json,
    chain: str = "solana",
) -> dict[str, Any]:
    address = str(row.get("mint_address") or row.get("token_address") or row.get("contract_address") or row.get("address") or "").strip()
    pairs = dex_pairs_for_token(address, chain=chain, http_get=http_get)
    best = select_best_pair(pairs)
    enriched = dict(row)
    enriched["chain"] = chain
    enriched["mint_address"] = address or enriched.get("mint_address") or ""
    enriched["dex_pair_count"] = len(pairs)
    enriched["historical_peak_mcap_confirmed"] = False
    if not best:
        enriched.update(
            {
                "dex_found": False,
                "mcap_confirmation": "no_dex_pair",
                "current_mcap": 0.0,
                "current_liquidity": 0.0,
                "current_volume24h": 0.0,
                "dex_url": "",
            }
        )
        return enriched

    current_mcap = pair_mcap(best)
    volume = best.get("volume") or {}
    price_change = best.get("priceChange") or {}
    enriched.update(
        {
            "dex_found": True,
            "dex_chain": best.get("chainId") or chain,
            "dex_pair_address": best.get("pairAddress") or "",
            "dex_url": best.get("url") or "",
            "current_price_usd": to_float(best.get("priceUsd")),
            "current_mcap": current_mcap,
            "current_fdv": to_float(best.get("fdv")),
            "current_liquidity": pair_liquidity_usd(best),
            "current_volume24h": to_float(volume.get("h24") if isinstance(volume, dict) else None),
            "current_change24h_pct": to_float(price_change.get("h24") if isinstance(price_change, dict) else None),
            "pair_created_at": best.get("pairCreatedAt") or "",
            "mcap_confirmation": mcap_confirmation(current_mcap),
        }
    )
    return enriched


def enrichment_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "input_count": len(rows),
        "dex_found_count": len([row for row in rows if row.get("dex_found")]),
        "current_over_1m_count": len([row for row in rows if row.get("mcap_confirmation") == "current_over_1m"]),
        "current_below_1m_peak_unknown_count": len(
            [row for row in rows if row.get("mcap_confirmation") == "current_below_1m_peak_unknown"]
        ),
        "unresolved_count": len([row for row in rows if row.get("mcap_confirmation") in {"no_dex_pair", "no_dex_mcap"}]),
    }


def enrich_candidates(
    rows: list[dict[str, Any]],
    http_get: Callable[..., Any] = http_json,
    chain: str = "solana",
    sleep_seconds: float = 0.0,
) -> dict[str, Any]:
    enriched: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        enriched.append(enrich_candidate(row, http_get=http_get, chain=chain))
        if sleep_seconds > 0 and index < len(rows) - 1:
            time.sleep(sleep_seconds)
    return {"summary": enrichment_summary(enriched), "rows": enriched, "data_quality": data_quality_summary()}


def write_outputs(payload: dict[str, Any], out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "gold-dog-10x-candidates-enriched.json"
    csv_path = out_dir / "gold-dog-10x-candidates-enriched.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = payload.get("rows") or []
    if rows:
        fieldnames = sorted({key for row in rows for key in row.keys()})
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return {"json": str(json_path), "csv": str(csv_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich 10x gold-dog candidates with public DexScreener evidence.")
    parser.add_argument("--input", default="outputs/gold-dog-10x-candidates-melt.json")
    parser.add_argument("--out-dir", default="outputs")
    parser.add_argument("--chain", default="solana")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    parser.add_argument("--cache-seconds", type=int, default=24 * 60 * 60)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    configure_cache(out_dir, max_age_seconds=args.cache_seconds)
    reset_data_quality()
    rows = load_candidate_rows(Path(args.input))
    if args.limit > 0:
        rows = rows[: args.limit]
    payload = enrich_candidates(rows, chain=args.chain, sleep_seconds=args.sleep_seconds)
    outputs = write_outputs(payload, out_dir)
    print(json.dumps({"ok": True, "summary": payload["summary"], **outputs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
