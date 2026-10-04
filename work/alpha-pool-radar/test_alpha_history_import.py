import csv
import importlib.util
import json
import pickle
import sys
from pathlib import Path


BASE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_module("alpha_replay", BASE / "alpha_replay.py")
history_import = load_module("alpha_history_import", BASE / "alpha_history_import.py")


def test_import_csv_history_converts_rows_to_replay_history(tmp_path):
    csv_path = tmp_path / "history.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "symbol",
                "chain",
                "token_address",
                "detected_at",
                "entry_price",
                "peak_price",
                "market_cap",
                "liquidity",
                "volume24h",
                "pair_age_hours",
                "smart_money",
                "kol",
                "top10_holder_pct",
                "gold_dog_conviction_score",
                "source_labels",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "symbol": "MOON",
                "chain": "solana",
                "token_address": "MintMoon",
                "detected_at": "2025-08-15T00:00:00+08:00",
                "entry_price": "0.001",
                "peak_price": "0.004",
                "market_cap": "240000",
                "liquidity": "56000",
                "volume24h": "420000",
                "pair_age_hours": "5",
                "smart_money": "32",
                "kol": "7",
                "top10_holder_pct": "19",
                "gold_dog_conviction_score": "84",
                "source_labels": "GMGN;DS;Alpha_AI",
            }
        )

    history = history_import.load_history_file(csv_path)
    row = history["rows"]["solana:mintmoon"]

    assert row["symbol"] == "MOON"
    assert row["recommendation_bucket"] == "shadow"
    assert row["first_seen_at"] == "2025-08-15T00:00:00+08:00"
    assert row["first_price_usd"] == 0.001
    assert row["peak_price_usd"] == 0.004
    assert row["peak_return_pct"] == 300.0
    assert row["return_since_first_pct"] == 300.0
    assert row["peak_mcap"] == 960000.0
    assert row["first_snapshot"]["mcap"] == 240000.0
    assert row["first_snapshot"]["source_labels"] == ["GMGN", "DS", "Alpha_AI"]
    assert history["summary"]["tracked_count"] == 1


def test_import_json_history_accepts_existing_rows_shape(tmp_path):
    json_path = tmp_path / "history.json"
    json_path.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "symbol": "DOG",
                        "chain": "bsc",
                        "contract_address": "0xdog",
                        "first_seen_at": "2025-09-01T00:00:00+08:00",
                        "first_price_usd": 0.01,
                        "max_return_pct": 180,
                        "mcap": 180000,
                        "pair_age_hours": 3,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    history = history_import.load_history_file(json_path)
    row = history["rows"]["bsc:0xdog"]

    assert row["return_since_first_pct"] == 180.0
    assert row["peak_return_pct"] == 180.0
    assert row["first_snapshot"]["pair_age_hours"] == 3.0


def test_merge_histories_preserves_existing_and_adds_imported_rows():
    base = {"rows": {"solana:base": {"key": "solana:base", "symbol": "BASE"}}}
    imported = {"rows": {"solana:imported": {"key": "solana:imported", "symbol": "IMPORTED"}}}

    merged = history_import.merge_histories(base, imported)

    assert set(merged["rows"]) == {"solana:base", "solana:imported"}
    assert merged["summary"]["tracked_count"] == 2


def test_pickle_history_is_rejected_before_deserialization(tmp_path):
    pkl_path = tmp_path / "feature.pkl"
    with pkl_path.open("wb") as handle:
        pickle.dump({"rows": []}, handle)

    try:
        history_import.load_history_file(pkl_path)
    except ValueError as exc:
        assert str(exc) == "unsupported_history_format:.pkl"
    else:
        raise AssertionError("pickle history must be rejected")


def test_load_melt_dataset_dir_merges_labels_with_launch_times(tmp_path):
    data_dir = tmp_path / "MELT" / "data"
    (data_dir / "label").mkdir(parents=True)
    (data_dir / "memecoin").mkdir(parents=True)
    (data_dir / "label" / "label.csv").write_text(
        "mint_address,min_ratio,manipulated,return_ratio,label\n"
        "MintA,0.1,no,3.5,low\n",
        encoding="utf-8",
    )
    (data_dir / "memecoin" / "memecoin_list.jsonl").write_text(
        '{"token_address":"MintA","timestamp":"1740787160","time":"2025-02-28T23:59:20Z","creator":"CreatorA"}\n',
        encoding="utf-8",
    )
    (data_dir / "memecoin" / "metadata.jsonl").write_text(
        '{"address":"MintA","symbol":"MELTA","name":"Melt Alpha"}\n',
        encoding="utf-8",
    )

    history = history_import.load_melt_dataset_dir(data_dir)
    row = history["rows"]["solana:minta"]

    assert row["first_seen_at"] == "2025-02-28T23:59:20+00:00"
    assert row["symbol"] == "MELTA"
    assert row["peak_return_pct"] == 350.0
    assert row["first_snapshot"]["source_labels"] == ["MELT"]
