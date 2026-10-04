from alpha_execution_diagnosis import CN, diagnose, metrics, stamp
from alpha_execution_exit_comparison import simulate


def position():
    return {"entry_at": "2026-09-08T00:00:00+00:00", "entry_price_usd": 1,
            "quantity": 35, "debit_usd": 35.8, "pool_address": "0xabcdef"}


def quote(minute, price, pool="0xaBcDeF"):
    return {"quote_at": f"2026-09-08T00:{minute:02d}:00+00:00", "price_usd": price,
            "liquidity_usd": 100_000, "pool_address": pool}


def test_metrics_reconciles_and_reports_winner_concentration():
    rows = [{"net_usd": 9, "gross_usd": 10, "costs_usd": 1},
            {"net_usd": -5, "gross_usd": -4, "costs_usd": 1}]
    result = metrics(rows)
    assert result["net_usd"] == 4
    assert result["profit_factor"] == 1.8
    assert result["net_without_best"] == -5
    assert metrics([])["win_pct"] is None


def test_day_bucketing_uses_china_timezone():
    assert stamp("2026-09-07T18:00:00+00:00").astimezone(CN).date().isoformat() == "2026-09-08"


def test_diagnosis_joins_exact_trade_events_and_reconciles_costs():
    key = "bsc:0xabc"
    entry = "2026-09-07T18:00:20+00:00"
    end = "2026-09-07T18:02:30+00:00"
    buy_intent = "2026-09-07T18:00:00+00:00"
    sell_intent = "2026-09-07T18:02:00+00:00"
    common = {"key": key, "arm": "pullback"}
    events = [{**common, "type": "fill", "side": "buy", "time": entry,
               "created_at": buy_intent, "signal_snapshot": {"price_usd": 1},
               "quote": {"liquidity_usd": 20_000}},
              {**common, "type": "intent", "side": "sell", "time": sell_intent, "reason": "stop_loss"},
              {**common, "type": "fill", "side": "sell", "time": end, "created_at": sell_intent}]
    p = {**common, "symbol": "TEST", "entry_at": entry, "exit_at": end,
         "entry_price_usd": 1, "quantity": 35, "pnl_usd": -8,
         "exit_quote": {"price_usd": 0.8}, "buy_costs": {"total_usd": 0.5},
         "sell_costs": {"total_usd": 0.5}}
    result, rows = diagnose({"events": events, "closed": [p], "positions": {}, "last_run_at": end}, {})
    assert result["accounting_mismatches"] == 0
    assert rows[0]["day"] == "2026-09-08"
    assert rows[0]["exit_reason"] == "stop_loss"
    assert rows[0]["buy_delay_seconds"] == 20
    assert rows[0]["sell_delay_seconds"] == 30


def test_exit_needs_later_quote_and_normalizes_evm_pool_case():
    pending = simulate(position(), [quote(1, 2)], "full_tp")
    assert not pending["closed"]
    filled = simulate(position(), [quote(1, 2), quote(2, 1.9)], "full_tp")
    assert filled["closed"]
    assert filled["fills"][0]["price"] == 1.9
    assert filled["fills"][0]["delay_seconds"] == 60


def test_runner_retains_original_quantity_and_waits_for_trailing_fill():
    result = simulate(position(), [quote(1, 2), quote(2, 2.1), quote(3, 3),
                                   quote(4, 1.8), quote(5, 1.7)], "runner")
    assert result["closed"]
    assert [round(f["fraction"], 4) for f in result["fills"]] == [0.8, 0.2]
    assert [f["price"] for f in result["fills"]] == [2.1, 1.7]


def test_duplicate_timestamp_other_pool_and_depth_cannot_fill():
    assert not simulate(position(), [quote(1, 2), quote(1, 3)], "full_tp")["closed"]
    assert not simulate(position(), [quote(1, 2), quote(2, 3, "0xother")], "full_tp")["closed"]
    assert not simulate(position(), [quote(1, 2), quote(2, 100)], "full_tp")["closed"]
