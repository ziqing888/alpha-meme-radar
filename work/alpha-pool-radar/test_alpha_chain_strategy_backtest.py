from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from alpha_chain_strategy_backtest import (
    build_backtest,
    chronological_split,
    freeze_input_watermark,
    replay_candidate,
    run_from_paths,
    summarize_results,
)


TOKEN = "0xabcdefabcdefabcdefabcdefabcdefabcdef1234"


def _costs(**overrides):
    costs = {
        "gas_usd": 0.01,
        "buy_tax_pct": 1.0,
        "sell_tax_pct": 1.0,
        "route_loss_pct": 1.0,
        "buy_impact_pct": 1.0,
        "sell_impact_pct": 1.0,
    }
    costs.update(overrides)
    return costs


def _chain_name(chain: str) -> str:
    return "bsc" if chain.lower() in {"56", "bnb", "bsc-mainnet"} else chain.lower()


def _observation(
    at: str,
    multiple: float,
    *,
    executable: bool = True,
    chain: str = "robinhood",
    token: str = TOKEN,
    pool: str = "pool-a",
    sell_fraction: float = 1.0,
    **overrides,
):
    notional = 2.0 if _chain_name(chain) == "robinhood" else 1.0
    row = {
        "seen_at": at,
        "price_usd": multiple,
        "quote_source": "okx" if executable else "dexscreener",
        "quote_time_basis": "executable_quote" if executable else "http_observation_not_trade_timestamp",
    }
    if executable:
        row["executable_quote"] = {
            "quote_observed_at": at,
            "chain": _chain_name(chain),
            "contract_address": token,
            "pool_address": pool,
            "notional_usd": notional * sell_fraction,
            "sell_token_amount": notional * sell_fraction,
            "sell_price_usd": multiple,
            **_costs(),
        }
    row.update(overrides)
    return row


def _candidate(
    *,
    chain: str = "robinhood",
    stage: str = "aggregate_early_bird",
    first_seen_at: str = "2026-09-01T10:00:00+00:00",
    observations=None,
    provenance: str | None = "observed",
):
    normalized_chain = _chain_name(chain)
    notional = 2.0 if normalized_chain == "robinhood" else 1.0
    mcap = 100_000 if normalized_chain == "robinhood" else 50_000
    snapshot = {
        "chain": normalized_chain,
        "contract_address": TOKEN,
        "signal_stage": stage,
        "signal_stage_at": first_seen_at,
        "quote_source": "okx",
        "quote_time_basis": "executable_quote",
        "price_usd": 1.0,
        "mcap": mcap,
        "pool_address": "pool-a",
        "liquidity_usd": 30_000,
        "rank_score": 95,
        "recommendation_bucket": "ambush",
        "pair_age_hours": 0.1,
        "source_labels": ["okx", "dexscreener"],
        "buy_route_fresh": True,
        "sell_route_fresh": True,
        "round_trip_loss_pct": 5.0,
        "buy_price_impact_pct": 2.0,
        "sell_price_impact_pct": 2.0,
        "hard_risk_pass": True,
        "sellable_cycles": 2,
        "sell_count": 1,
        "entry_costs": _costs(),
        "order_notional_usd": notional,
        "entry_quote": {
            "quote_source": "okx",
            "quote_time_basis": "executable_quote",
            "quote_observed_at": first_seen_at,
            "chain": normalized_chain,
            "contract_address": TOKEN,
            "pool_address": "pool-a",
            "notional_usd": notional,
            "buy_price_usd": 1.0,
            "bought_token_amount": notional,
            "spent_usd": notional,
            **_costs(),
        },
    }
    if provenance is not None:
        snapshot["snapshot_provenance"] = provenance
    return {
        "chain": chain,
        "contract_address": TOKEN,
        "symbol": "TEST",
        "first_seen_at": first_seen_at,
        "first_price_usd": 1.0,
        "first_snapshot": snapshot,
        "observations": observations
        if observations is not None
        else [
            _observation("2026-09-01T10:01:00+00:00", 1.0, chain=chain),
            _observation("2026-09-01T10:02:00+00:00", 0.77, chain=chain),
        ],
    }


def test_freeze_input_watermark_records_exact_source_and_excludes_later_rows(tmp_path):
    path = tmp_path / "history.json"
    payload = {
        "rows": {
            "one": _candidate(
                observations=[
                    _observation("2026-09-01T10:01:00+00:00", 1.0),
                    _observation("2026-09-01T10:06:00+00:00", 9.0),
                ]
            ),
            "later": _candidate(first_seen_at="2026-09-01T10:06:00+00:00"),
        },
        "updated_at": "2026-09-01T10:05:00+00:00",
    }
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)

    frozen, watermark = freeze_input_watermark(path, as_of="2026-09-01T10:05:00+00:00")

    assert list(frozen["rows"]) == ["one"]
    assert len(frozen["rows"]["one"]["observations"]) == 1
    assert watermark["sha256"]
    assert watermark["byte_length"] == len(raw)
    assert watermark["source_row_count"] == 2
    assert watermark["frozen_row_count"] == 1
    assert watermark["updated_at"] == payload["updated_at"]
    assert watermark["as_of"] == "2026-09-01T10:05:00+00:00"


def test_watermark_ignores_mutable_top_level_stage_timestamp(tmp_path):
    row = _candidate(first_seen_at="2026-09-01T10:06:00+00:00")
    row["first_snapshot"].pop("signal_stage_at")
    row["signal_stage_at"] = "2026-09-01T10:00:00+00:00"
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps({"rows": {"late": row}, "updated_at": "2026-09-01T10:05:00+00:00"}),
        encoding="utf-8",
    )

    frozen, _ = freeze_input_watermark(path, as_of="2026-09-01T10:05:00+00:00")

    assert frozen["rows"] == {}


def test_backtest_never_enters_before_immutable_signal_time():
    row = _candidate(
        first_seen_at="2026-09-01T09:59:00+00:00",
        observations=[
            _observation("2026-09-01T09:59:30+00:00", 9.0),
            _observation("2026-09-01T10:00:00+00:00", 1.0),
            _observation("2026-09-01T10:01:00+00:00", 0.77),
        ],
    )
    row["first_snapshot"]["signal_stage_at"] = "2026-09-01T10:00:00+00:00"
    row["first_snapshot"]["entry_quote"]["quote_observed_at"] = "2026-09-01T10:00:00+00:00"

    result = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")

    assert result["entry_at"] == "2026-09-01T10:00:00+00:00"
    assert result["peak_multiple"] == 1.0


def test_missing_signal_stage_or_timestamp_is_not_evaluable():
    missing_stage = _candidate()
    missing_stage["first_snapshot"].pop("signal_stage")
    missing_time = _candidate()
    missing_time["first_snapshot"].pop("signal_stage_at")

    assert replay_candidate(missing_stage)["evidence_class"] == "not_evaluable"
    assert replay_candidate(missing_time)["not_evaluable_reason"] == "missing_signal_stage_timestamp"


def test_mutable_top_level_stage_cannot_replace_immutable_snapshot_stage():
    row = _candidate()
    row["first_snapshot"].pop("signal_stage")
    row["signal_stage"] = "aggregate_discovery"
    row["signal_stage_at"] = row["first_seen_at"]

    assert replay_candidate(row)["evidence_class"] == "not_evaluable"


def test_missing_snapshot_provenance_is_reference_only_not_assumed_observed():
    result = replay_candidate(_candidate(provenance=None))

    assert result["evidence_class"] == "reference_only"
    assert result["quote_provenance"]["snapshot_provenance"] == "unknown"
    assert "missing_provenance" in result["reference_reasons"]


@pytest.mark.parametrize(
    "provenance,source",
    [("imported", "okx"), ("synthetic", "okx"), ("observed", "dexscreener")],
)
def test_imported_synthetic_or_dexscreener_only_history_is_reference_only(provenance, source):
    row = _candidate(provenance=provenance)
    row["first_snapshot"]["quote_source"] = source
    row["first_snapshot"]["entry_quote"]["quote_source"] = source
    if source == "dexscreener":
        row["first_snapshot"]["quote_time_basis"] = "http_observation_not_trade_timestamp"
        row["observations"] = [_observation("2026-09-01T10:01:00+00:00", 2.2, executable=False)]

    result = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")

    assert result["evidence_class"] == "reference_only"
    assert result["net_return"] is None
    assert result["quote_provenance"]["entry_source"] == source


def test_missing_any_cost_component_blocks_executable_net_profit():
    row = _candidate()
    row["observations"][1]["executable_quote"].pop("sell_tax_pct")

    result = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")

    assert result["evidence_class"] == "reference_only"
    assert result["net_return"] is None
    assert "sell_tax_pct" in result["missing_cost_components"]


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("chain", "bsc"),
        ("contract_address", "0x1111111111111111111111111111111111111111"),
        ("pool_address", "pool-b"),
        ("notional_usd", 4.99),
        ("quote_observed_at", "2026-09-01T10:01:01+00:00"),
    ],
)
def test_executable_exit_quote_must_match_identity_pool_notional_and_timestamp(field, bad_value):
    row = _candidate()
    row["observations"][0]["executable_quote"][field] = bad_value

    result = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")

    assert result["evidence_class"] == "reference_only"
    assert "quote_binding_mismatch" in result["reference_reasons"]


def test_entry_quote_binding_mismatch_is_reference_only():
    row = _candidate()
    row["first_snapshot"]["entry_quote"]["notional_usd"] = 1.0

    result = replay_candidate(row)

    assert result["evidence_class"] == "reference_only"
    assert "entry_quote_binding_mismatch" in result["reference_reasons"]


def test_entry_costs_reduce_net_return_numerically():
    free = _candidate()
    costly = _candidate()
    free["first_snapshot"]["entry_costs"] = _costs(
        gas_usd=0, buy_tax_pct=0, route_loss_pct=0, buy_impact_pct=0
    )
    free["first_snapshot"]["entry_quote"].update(free["first_snapshot"]["entry_costs"])
    costly["first_snapshot"]["entry_costs"] = _costs(
        gas_usd=0.10, buy_tax_pct=2, route_loss_pct=1, buy_impact_pct=1
    )
    costly["first_snapshot"]["entry_quote"].update(costly["first_snapshot"]["entry_costs"])
    costly["first_snapshot"]["entry_quote"].update({
        "buy_price_usd": 2.0 / 1.92,
        "bought_token_amount": 1.92,
        "spent_usd": 2.0,
    })
    for observation in costly["observations"]:
        observation["executable_quote"]["sell_token_amount"] = 1.92

    free_result = replay_candidate(free)
    costly_result = replay_candidate(costly)

    assert free_result["net_return"] is not None
    assert costly_result["net_return"] is not None
    assert costly_result["net_return"] < free_result["net_return"]
    assert costly_result["entry_cost_units"] == pytest.approx(0.05)


def test_entry_uses_actual_executable_buy_fill_not_first_display_price():
    row = _candidate(
        observations=[
            _observation(
                "2026-09-01T11:31:00+00:00",
                3.0,
                sell_fraction=1.0,
                executable_quote={
                    "quote_observed_at": "2026-09-01T11:31:00+00:00",
                    "chain": "robinhood",
                    "contract_address": TOKEN,
                    "pool_address": "pool-a",
                    "notional_usd": 2.0,
                    "sell_token_amount": 1.0,
                    "sell_price_usd": 3.0,
                    **_costs(gas_usd=0, sell_tax_pct=0, route_loss_pct=0, sell_impact_pct=0),
                },
            )
        ]
    )
    row["first_snapshot"]["price_usd"] = 1.0
    row["first_price_usd"] = 1.0
    row["first_snapshot"]["entry_quote"].update({
        "buy_price_usd": 2.0,
        "bought_token_amount": 1.0,
        "spent_usd": 2.0,
        **_costs(gas_usd=0, buy_tax_pct=0, route_loss_pct=0, buy_impact_pct=0),
    })

    result = replay_candidate(
        row,
        as_of="2026-09-02T12:00:00+00:00",
        max_gap_seconds=6_000,
    )

    assert result["entry_buy_price_usd"] == 2.0
    assert result["entry_bought_token_amount"] == 1.0
    assert result["entry_spent_usd"] == 2.0
    assert result["peak_multiple"] == pytest.approx(1.5)
    assert [fill["reason"] for fill in result["fills"]] == ["time_stop"]
    assert result["net_return"] == pytest.approx(0.5)


def test_partial_exit_quote_must_bind_the_actual_slice_not_original_notional():
    row = _candidate(
        observations=[_observation("2026-09-01T10:01:00+00:00", 2.2, sell_fraction=1.0)]
    )

    result = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")

    assert result["fills"] == []
    assert result["unsellable_after_entry"] is True
    assert "partial_exit_quote_binding_mismatch" in result["reference_reasons"]


def test_partial_exit_quote_accepts_the_actual_token_amount():
    row = _candidate(
        observations=[_observation("2026-09-01T10:01:00+00:00", 2.2, sell_fraction=0.5)]
    )

    result = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")

    assert [fill["fraction"] for fill in result["fills"]] == [0.5]
    assert result["fills"][0]["token_amount"] == pytest.approx(1.0)


def test_sparse_jump_cannot_synthesize_multiple_ideal_target_fills():
    row = _candidate(
        observations=[
            _observation("2026-09-01T10:01:00+00:00", 1.0),
            _observation("2026-09-01T10:02:00+00:00", 6.0, sell_fraction=0.5),
        ]
    )

    result = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")

    assert [fill["reason"] for fill in result["fills"]] == ["take_profit_2x"]
    assert result["remaining_fraction"] == 0.5
    assert result["ambiguous_sparse_jump"] is True
    assert result["evidence_class"] == "reference_only"


def test_robinhood_and_bsc_use_different_exit_ladders():
    observations = [
        _observation("2026-09-01T10:01:00+00:00", 2.2, sell_fraction=0.5),
        _observation("2026-09-01T10:02:00+00:00", 3.3, sell_fraction=0.1),
        _observation("2026-09-01T10:03:00+00:00", 5.4, sell_fraction=0.1),
        _observation("2026-09-01T10:04:00+00:00", 2.9, sell_fraction=0.3),
    ]
    robinhood = replay_candidate(_candidate(observations=observations))
    bsc = replay_candidate(
        _candidate(
            chain="bsc",
            stage="aggregate_early_bird",
            observations=[
                _observation("2026-09-01T10:01:00+00:00", 2.2, chain="bsc", sell_fraction=0.5),
                _observation("2026-09-01T10:02:00+00:00", 3.3, chain="bsc", sell_fraction=0.5),
                _observation("2026-09-01T10:03:00+00:00", 5.4, chain="bsc", sell_fraction=0.5),
                _observation("2026-09-01T10:04:00+00:00", 2.9, chain="bsc", sell_fraction=0.5),
            ],
        )
    )

    assert [fill["fraction"] for fill in robinhood["fills"]] == [0.5, 0.1, 0.1, 0.3]
    assert [fill["fraction"] for fill in bsc["fills"]] == [0.5, 0.5]
    assert [fill["reason"] for fill in bsc["fills"]] == ["take_profit_2x", "trailing_stop"]
    assert bsc["shadow_hits"] == {"3x": True, "5x": True}


def test_chain_56_alias_uses_bsc_exit_policy_and_notional():
    result = replay_candidate(
        _candidate(
            chain="56",
            stage="aggregate_early_bird",
            observations=[
                _observation("2026-09-01T10:01:00+00:00", 2.2, chain="56"),
                _observation("2026-09-01T10:02:00+00:00", 3.3, chain="56", sell_fraction=0.5),
                _observation("2026-09-01T10:03:00+00:00", 1.8, chain="56", sell_fraction=0.5),
            ],
        )
    )

    assert result["chain"] == "bsc"
    assert [fill["fraction"] for fill in result["fills"]] == [0.5, 0.5]


def test_chronological_split_keeps_identity_in_one_partition_and_deduplicates_stage():
    duplicate = _candidate(first_seen_at="2026-09-01T11:00:00+00:00")
    rows = [
        _candidate(first_seen_at="2026-09-01T10:00:00+00:00"),
        duplicate,
        _candidate(first_seen_at="2026-09-02T10:00:00+00:00"),
        _candidate(first_seen_at="2026-09-03T10:00:00+00:00"),
    ]
    rows[2]["contract_address"] = "0x2222222222222222222222222222222222222222"
    rows[3]["contract_address"] = "0x3333333333333333333333333333333333333333"

    split = chronological_split(rows, holdout_fraction=1 / 3)

    assert len(split["train"]) == 2
    assert len(split["test"]) == 1
    assert not ({row["identity"] for row in split["train"]} & {row["identity"] for row in split["test"]})
    assert split["duplicate_count"] == 1


def test_headline_metrics_exclude_reference_censored_and_rejected_rows():
    executable = {
        "identity": "robinhood:a",
        "entry_at": "2026-09-01T10:00:00+00:00",
        "evidence_class": "executable",
        "resolved": True,
        "net_return": 1.0,
        "peak_multiple": 5.2,
    }
    rows = [
        executable,
        {**executable, "identity": "robinhood:b", "evidence_class": "reference_only", "resolved": False, "net_return": 99.0},
        {**executable, "identity": "robinhood:c", "resolved": False, "net_return": -1.0},
        {**executable, "identity": "robinhood:d", "evidence_class": "not_evaluable", "net_return": -1.0},
    ]

    summary = summarize_results(rows)

    assert summary["total_candidates"] == 4
    assert summary["executable_resolved_count"] == 1
    assert summary["reference_only_count"] == 1
    assert summary["unresolved_count"] == 2
    assert summary["not_evaluable_count"] == 1
    assert summary["net_return_units"] == 1.0
    assert summary["profit_factor"] is None


def test_unsellable_after_entry_is_reported_and_counted_as_full_loss_in_stress_metrics():
    row = _candidate(
        observations=[
            _observation(
                "2026-09-01T10:01:00+00:00",
                0.9,
                executable=False,
            )
        ]
    )

    replayed = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")
    summary = summarize_results([replayed])

    assert replayed["entry_executable"] is True
    assert replayed["unsellable_after_entry"] is True
    assert summary["unsellable_after_entry_count"] == 1
    assert summary["unsellable_after_entry_rate"] == 1.0
    assert summary["stress_evaluated_count"] == 1
    assert summary["stress_net_return_units"] == -1.0
    assert summary["stress_loss_rate"] == 1.0
    assert summary["stress_catastrophic_loss_rate"] == 1.0


def test_rejected_candidate_is_not_counted_as_an_executable_entry():
    row = _candidate()
    row["first_snapshot"]["hard_risk_pass"] = False

    replayed = replay_candidate(row, as_of="2026-09-02T12:00:00+00:00")
    summary = summarize_results([replayed])

    assert replayed["eligible"] is False
    assert replayed["entry_executable"] is False
    assert summary["entry_executable_count"] == 0
    assert summary["unsellable_after_entry_rate"] is None


def test_build_backtest_reports_groups_and_never_uses_holdout_to_select_policy():
    rows = [
        _candidate(first_seen_at="2026-09-01T10:00:00+00:00"),
        _candidate(first_seen_at="2026-09-02T10:00:00+00:00"),
        _candidate(first_seen_at="2026-09-03T10:00:00+00:00"),
    ]
    for index, row in enumerate(rows):
        contract = f"0x{index + 1:040x}"
        row["contract_address"] = contract
        row["first_snapshot"]["contract_address"] = contract
        row["first_snapshot"]["entry_quote"]["contract_address"] = contract
        for observation in row["observations"]:
            observation["executable_quote"]["contract_address"] = contract
    result = build_backtest({"rows": rows, "updated_at": "2026-09-04T00:00:00+00:00"})

    assert set(result["comparison"]) == {"current", "aggregate_discovery", "chain_v2"}
    assert result["comparison"]["current"] == {
        "status": "not_evaluable",
        "reason": "policy_not_implemented",
    }
    assert result["comparison"]["aggregate_discovery"]["status"] == "not_evaluable"
    assert result["comparison"]["chain_v2"]["status"] == "implemented"
    assert result["comparison"]["chain_v2"]["summary"]["total_candidates"] == 3
    assert "robinhood" in result["groups"]["by_chain"]
    assert "aggregate_early_bird" in result["groups"]["by_signal_stage"]
    assert result["holdout"]["policy_selection_source"] == "train_only"


def test_current_comparison_keeps_legacy_rows_with_missing_stage_visible():
    row = _candidate()
    row["first_snapshot"].pop("signal_stage")
    result = build_backtest({"rows": [row], "updated_at": "2026-09-02T00:00:00+00:00"})

    assert result["universe"]["total_candidates"] == 1
    assert result["universe"]["not_evaluable_count"] == 1
    assert result["comparison"]["current"]["status"] == "not_evaluable"
    assert result["comparison"]["chain_v2"]["summary"]["total_candidates"] == 0


def test_run_from_paths_writes_json_and_markdown_to_explicit_paths(tmp_path):
    history = tmp_path / "history.json"
    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"
    history.write_text(
        json.dumps({"rows": {"one": _candidate()}, "updated_at": "2026-09-02T00:00:00+00:00"}),
        encoding="utf-8",
    )

    result = run_from_paths(history, json_out, md_out, as_of="2026-09-02T00:00:00+00:00")

    assert json.loads(json_out.read_text(encoding="utf-8"))["watermark"] == result["watermark"]
    assert "Evidence classes" in md_out.read_text(encoding="utf-8")
