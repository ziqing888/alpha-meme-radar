from alpha_entry_selection_backtest import Selector, _split_days, evaluate_selector


def _row(day: str, stress_return: float, **overrides):
    row = {
        "entry_at": f"{day}T12:00:00+08:00",
        "chain": "robinhood",
        "first_mcap_usd": 150_000,
        "score": 65,
        "liquidity_usd": 30_000,
        "source_count": 2,
        "change_h1_pct": 20,
        "change_m5_pct": 10,
        "stress_return": stress_return,
        "gross_return": stress_return + 0.1,
        "conservative_return": stress_return,
        "conservative_stress_return": stress_return,
        "peak_multiple": 2.2 if stress_return > 0 else 1.0,
        "resolved": True,
        "observation_count": 3,
    }
    row.update(overrides)
    return row


def _selector():
    return Selector(
        chain="robinhood",
        mcap_min=100_000,
        mcap_max=300_001,
        score_min=60,
        score_max=70,
        liquidity_min=20_000,
        source_count_min=2,
        h1_min=-10,
        h1_max=50,
        m5_min=0,
        m5_max=30,
    )


def test_split_days_keeps_the_last_days_for_holdout():
    rows = [_row(f"2026-09-0{day}", 0.2) for day in range(1, 7)]
    train, test = _split_days(rows)
    assert train == {"2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"}
    assert test == {"2026-09-05", "2026-09-06"}


def test_selector_requires_positive_train_and_test_with_minimum_samples():
    train_days = {"2026-09-01"}
    test_days = {"2026-09-02"}
    rows = [_row("2026-09-01", 0.2) for _ in range(12)]
    rows += [_row("2026-09-02", 0.1) for _ in range(8)]
    result = evaluate_selector(rows, _selector(), train_days, test_days)
    assert result["robustness_pass"] is True
    assert result["all"]["count"] == 20
    assert result["all"]["observation_coverage"] == 1.0
    assert result["all"]["resolved_rate"] == 1.0


def test_selector_rejects_a_profitable_train_segment_that_loses_in_holdout():
    train_days = {"2026-09-01"}
    test_days = {"2026-09-02"}
    rows = [_row("2026-09-01", 0.2) for _ in range(12)]
    rows += [_row("2026-09-02", -0.2) for _ in range(8)]
    result = evaluate_selector(rows, _selector(), train_days, test_days)
    assert result["robustness_pass"] is False
    assert result["test"]["stress_average_return"] < 0


def test_selector_filters_on_all_requested_first_snapshot_features():
    train_days = {"2026-09-01"}
    test_days = {"2026-09-02"}
    rows = [_row("2026-09-01", 0.2) for _ in range(12)]
    rows += [_row("2026-09-02", 0.1) for _ in range(8)]
    rows.append(_row("2026-09-02", 9.0, score=75))
    result = evaluate_selector(rows, _selector(), train_days, test_days)
    assert result["all"]["count"] == 20
