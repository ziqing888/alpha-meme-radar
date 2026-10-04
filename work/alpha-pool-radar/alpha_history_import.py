#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpha_replay import replay_summary
from alpha_replay import row_key


SNAPSHOT_FIELD_MAP = {
    "mcap": ("mcap", "market_cap", "marketCap", "fdv"),
    "market_cap": ("market_cap", "marketCap"),
    "liquidity": ("liquidity", "liquidity_usd", "liquidityUsd"),
    "volume24h": ("volume24h", "volume_24h", "volume24", "dex_volume24h"),
    "pair_age_hours": ("pair_age_hours", "age_hours", "pool_age_hours"),
    "change_m5": ("change_m5", "price_change_m5", "m5"),
    "change_h1": ("change_h1", "price_change_h1", "h1"),
    "change_h24": ("change_h24", "price_change_h24", "h24"),
    "txns24h": ("txns24h", "transactions24h", "txns_24h"),
    "smart_money": ("smart_money", "smartMoney", "smart_money_count"),
    "kol": ("kol", "kol_count"),
    "holders": ("holders", "holder_count", "holderCount"),
    "top10_holder_pct": ("top10_holder_pct", "top10", "top10_pct"),
    "top20_holder_pct": ("top20_holder_pct", "top20", "top20_pct"),
    "max_holder_pct": ("max_holder_pct", "max_holder"),
    "gold_dog_conviction_score": ("gold_dog_conviction_score", "conviction", "score"),
}


def first_value(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    multiplier = 1.0
    if text.lower().endswith("k"):
        multiplier = 1_000.0
        text = text[:-1]
    elif text.lower().endswith("m"):
        multiplier = 1_000_000.0
        text = text[:-1]
    elif text.lower().endswith("b"):
        multiplier = 1_000_000_000.0
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def normalize_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    text = str(value).strip()
    if text.isdigit():
        return normalize_time(float(text))
    return text.replace("Z", "+00:00")


def split_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    text = str(value)
    for sep in (";", "|"):
        text = text.replace(sep, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def pct_field(value: Any) -> float:
    number = to_float(value)
    if 0 < number <= 1:
        return round(number * 100, 4)
    return number


def melt_risk_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    sniper_0s = to_float(row.get("group2_sniper_0s_num"))
    if sniper_0s > 0:
        flags.append(f"sniper_0s_{int(sniper_0s)}")
    bundled = pct_field(row.get("group2_bundled_pct"))
    if bundled > 0:
        flags.append(f"bundled_{bundled:g}%")
    return flags


def snapshot_from_record(row: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for target, names in SNAPSHOT_FIELD_MAP.items():
        value = first_value(row, names)
        if value not in (None, ""):
            snapshot[target] = to_float(value)
    if "group2_top10_pct" in row:
        snapshot["top10_holder_pct"] = pct_field(row.get("group2_top10_pct"))
    if "group2_top20_pct" in row:
        snapshot["top20_holder_pct"] = pct_field(row.get("group2_top20_pct"))
    if "group3_holder_num" in row:
        snapshot["holders"] = to_float(row.get("group3_holder_num"))
    if "group3_swap_tx_cnt" in row:
        snapshot["txns24h"] = to_float(row.get("group3_swap_tx_cnt"))
    source_labels = split_list(first_value(row, ("source_labels", "sourceLabels", "sources")))
    if source_labels:
        snapshot["source_labels"] = source_labels
    risks = split_list(first_value(row, ("gmgn_risk_flags", "risk_flags", "risks")))
    risks.extend(melt_risk_flags(row))
    if risks:
        snapshot["gmgn_risk_flags"] = risks
    narratives = split_list(first_value(row, ("narrative_tags", "narratives", "tags")))
    if narratives:
        snapshot["narrative_tags"] = narratives
    return snapshot


def normalize_record(row: dict[str, Any], fallback_index: int = 0) -> dict[str, Any] | None:
    symbol = str(first_value(row, ("symbol", "ticker", "base_symbol", "baseSymbol")) or "").strip()
    chain = str(first_value(row, ("chain", "chain_id", "chainId", "network")) or "").strip().lower()
    address = str(
        first_value(row, ("contract_address", "token_address", "address", "mint", "mint_address", "ca", "base_token_address")) or ""
    ).strip()
    if not chain and "mint_address" in row:
        chain = "solana"
    if not (symbol or address):
        return None

    first_seen_at = normalize_time(first_value(row, ("first_seen_at", "detected_at", "timestamp", "created_at", "launch_time", "mint_ts")))
    latest_seen_at = normalize_time(first_value(row, ("latest_seen_at", "outcome_at", "evaluated_at"))) or first_seen_at
    first_price = to_float(first_value(row, ("first_price_usd", "entry_price", "price_usd", "price", "open_price")))
    latest_price = to_float(first_value(row, ("latest_price_usd", "current_price", "exit_price", "close_price")))
    peak_price = to_float(first_value(row, ("peak_price_usd", "peak_price", "max_price_usd", "high_price_usd", "ath_price")))
    peak_return = to_float(first_value(row, ("peak_return_pct", "max_return_pct", "return_since_first_pct", "return_pct")))
    return_ratio = to_float(first_value(row, ("return_ratio", "max_return_ratio", "peak_return_ratio")))
    if peak_return == 0.0 and return_ratio > 0:
        peak_return = round(return_ratio * 100, 2)
    if peak_return == 0.0 and first_price > 0 and peak_price > 0:
        peak_return = round((peak_price / first_price - 1) * 100, 2)
    if peak_return == 0.0 and first_price > 0 and latest_price > 0:
        peak_return = round((latest_price / first_price - 1) * 100, 2)
    if latest_price <= 0 and first_price > 0 and peak_return:
        latest_price = round(first_price * (1 + peak_return / 100), 12)
    if peak_price <= 0 and first_price > 0 and peak_return:
        peak_price = round(first_price * (1 + peak_return / 100), 12)
    snapshot = snapshot_from_record(row)
    first_mcap = to_float(snapshot.get("mcap") or snapshot.get("market_cap"))
    peak_mcap = to_float(first_value(row, ("peak_mcap", "max_mcap", "ath_mcap", "peak_market_cap", "max_market_cap")))
    if peak_mcap <= 0 and first_mcap > 0 and peak_return > -100:
        peak_mcap = round(first_mcap * (1 + peak_return / 100), 2)

    replay_row = {
        "key": "",
        "symbol": symbol,
        "chain": chain,
        "contract_address": address,
        "first_seen_at": first_seen_at or datetime.now(timezone.utc).isoformat(),
        "latest_seen_at": latest_seen_at or first_seen_at or datetime.now(timezone.utc).isoformat(),
        "first_price_usd": first_price,
        "latest_price_usd": latest_price or peak_price or first_price,
        "peak_price_usd": peak_price,
        "peak_return_pct": peak_return,
        "return_since_first_pct": peak_return,
        "return_1h_pct": to_float(first_value(row, ("return_1h_pct", "return1h_pct", "h1_return_pct"))) or None,
        "return_6h_pct": to_float(first_value(row, ("return_6h_pct", "return6h_pct", "h6_return_pct"))) or None,
        "return_24h_pct": to_float(first_value(row, ("return_24h_pct", "return24h_pct", "h24_return_pct"))) or None,
        "recommendation_bucket": str(first_value(row, ("recommendation_bucket", "bucket")) or "shadow"),
        "recommendation_action": str(first_value(row, ("recommendation_action", "action")) or "历史导入"),
        "hit_status": str(first_value(row, ("hit_status", "status")) or "tracking"),
        "peak_mcap": peak_mcap,
        "first_snapshot": snapshot,
        "observations": [],
    }
    replay_row = {key: value for key, value in replay_row.items() if value not in (None, "")}
    key = row_key(replay_row)
    if not key.strip(":"):
        key = f"imported:{fallback_index}"
    replay_row["key"] = key
    return replay_row


def rows_from_json(payload: Any) -> list[dict[str, Any]]:
    if hasattr(payload, "to_dict"):
        records = payload.to_dict(orient="records")
        return [row for row in records if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("rows"), list):
        return [row for row in payload["rows"] if isinstance(row, dict)]
    if isinstance(payload.get("rows"), dict):
        return [row for row in payload["rows"].values() if isinstance(row, dict)]
    for key in ("data", "items", "records"):
        if isinstance(payload.get(key), list):
            return [row for row in payload[key] if isinstance(row, dict)]
    return []


def history_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        normalized = normalize_record(record, fallback_index=index)
        if not normalized:
            continue
        rows[normalized["key"]] = normalized
    history = {"rows": rows}
    history["summary"] = replay_summary(history)
    return history


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def load_melt_dataset_dir(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir)
    label_path = root / "label" / "label.csv"
    launch_path = root / "memecoin" / "memecoin_list.jsonl"
    metadata_path = root / "memecoin" / "metadata.jsonl"
    if not label_path.exists():
        raise FileNotFoundError(f"missing MELT label file: {label_path}")
    with label_path.open("r", newline="", encoding="utf-8-sig") as handle:
        labels = list(csv.DictReader(handle))
    launches = {
        str(row.get("token_address") or "").strip(): row
        for row in read_jsonl(launch_path)
        if str(row.get("token_address") or "").strip()
    }
    metadata = {
        str(row.get("address") or "").strip(): row
        for row in read_jsonl(metadata_path)
        if str(row.get("address") or "").strip()
    }
    merged: list[dict[str, Any]] = []
    for label in labels:
        mint = str(label.get("mint_address") or "").strip()
        launch = launches.get(mint) or {}
        meta = metadata.get(mint) or {}
        merged.append(
            {
                **label,
                "chain": "solana",
                "mint_address": mint,
                "symbol": meta.get("symbol"),
                "name": meta.get("name"),
                "first_seen_at": launch.get("time") or launch.get("timestamp"),
                "creator": launch.get("creator"),
                "source_labels": "MELT",
                "recommendation_bucket": "shadow",
                "recommendation_action": "历史导入",
            }
        )
    return history_from_records(merged)


def load_history_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        with source.open("r", newline="", encoding="utf-8-sig") as handle:
            return history_from_records(list(csv.DictReader(handle)))
    if suffix in {".pkl", ".pickle"}:
        raise ValueError(f"unsupported_history_format:{suffix}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    return history_from_records(rows_from_json(payload))


def merge_histories(base: dict[str, Any], imported: dict[str, Any]) -> dict[str, Any]:
    rows = dict(base.get("rows") or {})
    for key, row in (imported.get("rows") or {}).items():
        rows[key] = row
    merged = {**base, "rows": rows}
    merged["summary"] = replay_summary(merged)
    return merged
