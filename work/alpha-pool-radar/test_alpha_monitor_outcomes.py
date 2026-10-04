import alpha_monitor_outcomes as outcomes


TOKEN = "0x" + "a" * 40


def monitor_token(*, state="building", market_cap=20_000, flags=()):
    return {
        "id": f"bsc:{TOKEN}",
        "identity": {
            "chain": "bsc",
            "contract_address": TOKEN,
            "symbol": "DOG",
        },
        "primary_state": state,
        "active_states": [state],
        "state_updated_at": "2026-09-10T12:00:00+00:00",
        "market": {"market_cap_usd": market_cap, "price_usd": 0.001},
        "ranking_axes": {
            "market_behavior": {
                "disposition": "observe" if flags else "pass",
                "flags": list(flags),
            }
        },
        "resonance": {"provider_families": ["gmgn", "okx"]},
    }


def test_outcome_tracker_records_selected_and_fills_due_checkpoints():
    first = outcomes.update_monitor_outcomes(
        None,
        {"tokens": [monitor_token()]},
        observed_at="2026-09-10T12:00:00+00:00",
    )
    later = outcomes.update_monitor_outcomes(
        first,
        {"tokens": [monitor_token(state="resonating", market_cap=30_000)]},
        observed_at="2026-09-10T12:05:10+00:00",
    )

    record = later["records"][f"bsc:{TOKEN}"]
    assert record["cohort"] == "selected"
    assert record["entry_stage"] == "aggregate_early_bird"
    assert record["best_stage"] == "aggregate_confirmation"
    assert record["checkpoints"]["5m"]["return_pct"] == 50.0
    assert later["summary"]["cohorts"]["selected"]["count"] == 1


def test_outcome_tracker_keeps_behavior_downgrade_in_rejected_cohort():
    result = outcomes.update_monitor_outcomes(
        None,
        {"tokens": [monitor_token(state="trend_watch", flags=("thin_holder_base",))]},
        observed_at="2026-09-10T12:00:00+00:00",
    )

    record = result["records"][f"bsc:{TOKEN}"]
    assert record["cohort"] == "downgraded"
    assert record["entry_stage"] == "trend_watch"
    assert record["reasons"] == ["thin_holder_base"]
    assert result["summary"]["reasons"]["thin_holder_base"] == 1


def test_arc_outcome_keeps_canonical_identity_across_monitor_cohorts():
    address = "0x" + "B" * 40
    state = None
    expected = (
        ("new", "discovered"),
        ("building", "selected"),
        ("resonating", "confirmed"),
        ("cooling", "cooling"),
        ("revival", "revival"),
    )
    for minute, (primary_state, cohort) in enumerate(expected):
        token = monitor_token(state=primary_state, market_cap=20_000 + minute * 1_000)
        token["id"] = f"5042:{address}"
        token["identity"] = {
            "chain": "arc-mainnet",
            "contract_address": address,
            "symbol": "ARC",
        }
        state = outcomes.update_monitor_outcomes(
            state,
            {"tokens": [token]},
            observed_at=f"2026-09-10T12:0{minute}:00+00:00",
        )
        record = state["records"][f"arc:{address.lower()}"]
        assert record["cohort"] == cohort
        assert record["chain"] == "arc"
        assert record["contract_address"] == address.lower()
        assert set(state["records"]) == {f"arc:{address.lower()}"}

    assert [item["cohort"] for item in record["cohort_history"]] == [
        "discovered", "selected", "confirmed", "cooling", "revival"
    ]
