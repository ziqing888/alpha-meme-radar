import importlib.util
import json
import sys
import threading
from pathlib import Path


BASE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("alpha_985_monitor_export", BASE / "alpha_985_monitor_export.py")
monitor985 = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = monitor985
SPEC.loader.exec_module(monitor985)


def test_collect_rows_fetches_core_endpoints_concurrently(monkeypatch):
    barrier = threading.Barrier(3, timeout=1)

    def fetch_json(url, timeout_seconds):
        barrier.wait()
        return {"events": []}

    monkeypatch.setattr(monitor985, "fetch_json", fetch_json)

    rows, counts = monitor985.collect_rows("https://985monitor.example", 20, 3)

    assert rows == []
    assert counts == {"dex_events": 0, "pump_trades": 0, "pump_callouts": 0}


def test_normalize_dex_paid_event_reads_market_fields():
    event = {
        "source": "DEX_PAID",
        "key": "dex:paid:bsc:0xabc",
        "createdAt": "2026-09-05T04:20:00.000Z",
        "content": {
            "dexMeta": {
                "chain": "BNB Chain",
                "ca": "0xAbC",
                "symbol": "DOG",
                "name": "Dog Token",
                "mc": 91_000,
                "liq": 34_000,
                "vol24": 188_000,
                "txnsH24": 520,
                "priceChg24": 62.5,
                "priceUsd": "0.00042",
                "isFirstPaid": True,
                "payCount": 2,
                "ageDays": 0.25,
                "url": "https://dexscreener.com/bsc/0xabc",
            }
        },
    }

    row = monitor985.normalize_event(event)

    assert row["source_family"] == "985_monitor"
    assert row["chain"] == "bsc"
    assert row["address"] == "0xAbC"
    assert row["symbol"] == "DOG"
    assert row["monitor985_kind"] == "dex_paid"
    assert row["marketCap"] == 91_000
    assert row["liquidity"] == 34_000
    assert row["volume24h"] == 188_000
    assert row["txns24h"] == 520
    assert row["pair_age_hours"] == 6
    assert row["monitor985_is_first_paid"] is True
    assert row["dex_url"] == "https://dexscreener.com/bsc/0xabc"


def test_normalize_dex_paid_event_keeps_absent_and_malformed_numbers_unknown():
    row = monitor985.normalize_dex_event(
        {
            "key": "dex:paid:bsc:unknown",
            "createdAt": "2026-09-10T11:00:00Z",
            "content": {
                "dexMeta": {
                    "chain": "bsc",
                    "ca": "0xUnknown",
                    "symbol": "UNK",
                    "mc": "not-a-number",
                }
            },
        }
    )

    assert row["marketCap"] is None
    assert row["market_cap"] is None
    assert row["liquidity"] is None
    assert row["volume24h"] is None
    assert row["price"] is None
    assert row["txns24h"] is None
    assert row["monitor985_pay_count"] is None


def test_normalize_pump_trade_keeps_buy_and_sell_provenance():
    buy = {
        "eventType": "PUMP_TRADE",
        "key": "pump:buy:sol:mint",
        "createdAt": "2026-09-05T04:21:00.000Z",
        "content": {
            "pumpTrade": {
                "side": "buy",
                "chainName": "Solana",
                "mint": "MintPump",
                "symbol": "PUMPY",
                "name": "Pumpy",
                "amountUsd": 420,
                "netAmountUsd": 390,
                "priceUsd": "0.000003",
                "marketCapUsd": 44_000,
                "program": "pump.fun",
                "isBondingCurve": True,
                "wallet": "WalletA",
                "walletName": "Smart Buyer",
                "followers": 12_000,
                "txHash": "0xTrade",
                "txUrl": "https://985monitor.xyz/tx/0xTrade",
            }
        },
    }
    sell = {
        **buy,
        "key": "pump:sell:sol:mint",
        "content": {"pumpTrade": {**buy["content"]["pumpTrade"], "side": "sell", "txHash": "0xSell"}},
    }

    buy_row = monitor985.normalize_event(buy)
    sell_row = monitor985.normalize_event(sell)

    assert buy_row["source_family"] == "985_monitor"
    assert buy_row["chain"] == "solana"
    assert buy_row["address"] == "MintPump"
    assert buy_row["monitor985_kind"] == "pump_trade_buy"
    assert buy_row["marketCap"] == 44_000
    assert buy_row["monitor985_trade_amount_usd"] == 390
    assert buy_row["monitor985_wallet"] == "WalletA"
    assert buy_row["monitor985_tx_hash"] == "0xTrade"
    assert buy_row["monitor985_event_key"] == "pump:buy:sol:mint"
    assert buy_row["smart_money"] == 1
    assert buy_row["launchpad_stage_label"] == "Pump 买入"
    assert sell_row["monitor985_kind"] == "pump_trade_sell"
    assert sell_row["monitor985_trade_side"] == "sell"
    assert sell_row["monitor985_wallet"] == "WalletA"
    assert sell_row["monitor985_tx_hash"] == "0xSell"
    assert sell_row["monitor985_event_key"] == "pump:sell:sol:mint"


def test_normalize_pump_callout_event_reads_social_fields():
    event = {
        "eventType": "PUMP_CALLOUT",
        "key": "pump:callout:rbh:0xdef",
        "createdAt": "2026-09-05T04:22:00.000Z",
        "tokenAddress": "0xDef",
        "chainName": "Robinhood",
        "symbol": "ROBI",
        "followers": 18_000,
        "holdingUsd": 1300,
        "comment": "early call",
        "content": {
            "pumpMeta": {
                "username": "caller",
                "marketCap": 66_000,
                "multiple": 1.4,
                "maxMultiplier": 2.6,
                "thesis": "first wave",
                "likes": 14,
                "replyCount": 5,
            }
        },
    }

    row = monitor985.normalize_event(event)

    assert row["source_family"] == "985_monitor"
    assert row["chain"] == "robinhood"
    assert row["address"] == "0xDef"
    assert row["monitor985_kind"] == "pump_callout"
    assert row["marketCap"] == 66_000
    assert row["kol"] == 1
    assert row["monitor985_username"] == "caller"
    assert row["monitor985_thesis"] == "first wave"
    assert row["monitor985_callout_max_multiplier"] == 2.6


def test_write_payload_writes_985_source_contract(tmp_path):
    rows = [{"chain": "bsc", "address": "0xabc", "symbol": "DOG"}]
    out = tmp_path / "985-monitor.json"

    payload = monitor985.write_payload(rows, out)

    assert payload["source"] == "985_monitor"
    assert payload["origin"] == "public_985monitor_api"
    assert payload["data"] == rows
    assert json.loads(out.read_text(encoding="utf-8"))["data"][0]["symbol"] == "DOG"


def test_leaderboard_handles_dedupes_top_boards():
    payload = {
        "boards": {
            "24h": [{"handle": "Alpha", "rank": 1}, {"handle": "Beta", "rank": 2}],
            "7d": [{"handle": "alpha", "rank": 3}, {"handle": "Gamma", "rank": 4}],
        },
        "commonFollowing": [{"handle": "Delta", "rank": 5}],
    }

    rows = monitor985.leaderboard_handles(payload, 3)

    assert [row["handle"] for row in rows] == ["Alpha", "Beta", "Gamma"]
    assert [row["fomo_board"] for row in rows] == ["24h", "24h", "7d"]


def test_normalize_fomo_trade_keeps_buy_and_sell_wallet_events():
    leader = {"handle": "good", "rank": 3, "pnl": 1_000_000, "followers": 200_000, "numTrades": 500, "fomo_board": "24h"}
    profile = {"handle": "good", "name": "Good Caller", "followers": 200_000}
    buy = {
        "side": "BUY",
        "chainName": "BSC",
        "tokenAddress": "0xBuy",
        "symbol": "BUY",
        "usd": 250,
        "marketCap": 90_000,
        "priceUsd": "0.0001",
        "createdAt": "2026-09-05T04:22:00.000Z",
        "txHash": "0xTx",
        "wallet": "WalletFomo",
    }
    sell = {**buy, "side": "SELL", "txHash": "0xSell"}

    buy_row = monitor985.normalize_fomo_trade(buy, profile, leader)
    sell_row = monitor985.normalize_fomo_trade(sell, profile, leader)

    assert buy_row["source_family"] == "985_fomo_wallets"
    assert buy_row["monitor985_kind"] == "fomo_wallet_buy"
    assert buy_row["chain"] == "bsc"
    assert buy_row["address"] == "0xBuy"
    assert buy_row["market_data_pending"] is True
    assert buy_row["smart_money"] > 0
    assert buy_row["monitor985_fomo_handle"] == "good"
    assert buy_row["monitor985_wallet"] == "WalletFomo"
    assert sell_row["monitor985_kind"] == "fomo_wallet_sell"
    assert sell_row["monitor985_trade_side"] == "SELL"
    assert sell_row["monitor985_wallet"] == "WalletFomo"
    assert sell_row["monitor985_tx_hash"] == "0xSell"


def test_normalize_smartmoney_token_scores_wallet_quality():
    token = {
        "watching": 1,
        "chain": "bsc",
        "addr": "0xSmart",
        "symbol": "SMRT",
        "name": "Smart",
        "last_mc": 85_000,
        "peak_mc": 400_000,
        "protocol": "fourmeme",
        "snapshots_count": 4,
        "early_buyers": [
            {
                "v2_label": "底部高倍数",
                "role": "early-buyer,current-holder",
                "v2_total_pnl": 80_000,
                "v2_win_rate": 62,
                "v2_items_n": 30,
                "best_realized_pnl": 120_000,
            }
        ],
        "current_holders": [
            {
                "v2_label": "中等盈利",
                "role": "current-holder",
                "v2_total_pnl": 35_000,
                "v2_win_rate": 58,
                "v2_items_n": 18,
            },
            {
                "v2_label": "中等盈利",
                "role": "current-holder",
                "v2_total_pnl": 28_000,
                "v2_win_rate": 56,
                "v2_items_n": 16,
            },
            {
                "v2_label": "中等盈利",
                "role": "current-holder",
                "v2_total_pnl": 22_000,
                "v2_win_rate": 54,
                "v2_items_n": 14,
            },
        ],
        "peak_sellers": [],
    }

    row = monitor985.normalize_smartmoney_token(token)

    assert row["source_family"] == "985_smartmoney"
    assert row["monitor985_kind"] == "smartmoney_token"
    assert row["chain"] == "bsc"
    assert row["marketCap"] == 85_000
    assert row["market_data_pending"] is True
    assert row["rank_score"] > 50
    assert row["smart_money"] > 0
    assert row["monitor985_smart_drawdown_pct"] == 78.75
    assert row["launchpad_platform"] == "fourmeme"


def test_collect_smartmoney_rows_sorts_by_score(monkeypatch):
    payload = {
        "meta": {"tokens_total": 2, "tokens_watching": 2},
        "tokens": [
            {
                "watching": 1,
                "chain": "base",
                "addr": "0xWeak",
                "symbol": "WEAK",
                "last_mc": 50_000,
                "early_buyers": [],
                "current_holders": [],
            },
            {
                "watching": 1,
                "chain": "bsc",
                "addr": "0xStrong",
                "symbol": "STRONG",
                "last_mc": 80_000,
                "early_buyers": [
                    {
                        "v2_label": "底部高倍数",
                        "role": "early-buyer,current-holder",
                        "v2_total_pnl": 90_000,
                        "v2_win_rate": 65,
                        "v2_items_n": 20,
                    }
                ],
                "current_holders": [],
            },
        ],
    }

    monkeypatch.setattr(monitor985, "fetch_json", lambda _url, _timeout: payload)

    rows, counts = monitor985.collect_smartmoney_rows("https://985monitor.xyz", 2, 3)

    assert [row["symbol"] for row in rows] == ["STRONG", "WEAK"]
    assert counts["tokens_total"] == 2
    assert counts["tokens_watching"] == 2


def test_smart_wallet_candidates_filter_and_dedupe_provider_wallets():
    wallet = {
        "chain": "bsc",
        "main_wallet": "0x1111111111111111111111111111111111111111",
        "v2_label": "底部高倍数",
        "v2_total_pnl": 25_000,
        "v2_win_rate": 62,
        "v2_items_n": 24,
        "best_realized_pnl": 80_000,
    }
    payload = {
        "tokens": [
            {
                "watching": 1,
                "chain": "bsc",
                "addr": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "symbol": "FIRST",
                "last_seen_ts": 1_789_232_400_000,
                "early_buyers": [{**wallet, "role": "early-buyer"}],
                "current_holders": [{**wallet, "role": "current-holder"}],
            },
            {
                "watching": 1,
                "chain": "bsc",
                "addr": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "symbol": "SECOND",
                "last_seen_ts": 1_789_232_460_000,
                "early_buyers": [{**wallet, "role": "early-buyer"}],
                "current_holders": [
                    {
                        "chain": "bsc",
                        "main_wallet": "0x2222222222222222222222222222222222222222",
                        "role": "current-holder",
                        "v2_total_pnl": 100,
                        "v2_win_rate": 40,
                        "v2_items_n": 3,
                    }
                ],
            },
        ]
    }

    rows, counts = monitor985.smart_wallet_candidates_from_payload(payload, limit=100)

    assert len(rows) == 1
    assert rows[0]["address"] == wallet["main_wallet"]
    assert rows[0]["chain"] == "bsc"
    assert rows[0]["roles"] == ["current-holder", "early-buyer"]
    assert rows[0]["token_count"] == 2
    assert [token["symbol"] for token in rows[0]["tokens"]] == ["SECOND", "FIRST"]
    assert rows[0]["provider_reported_pnl"] == 25_000
    assert rows[0]["provider_reported_win_rate"] == 62
    assert rows[0]["provider_reported_samples"] == 24
    assert rows[0]["verification_status"] == "candidate"
    assert counts == {
        "linked_wallet_rows": 4,
        "unique_wallets": 2,
        "qualified_candidates": 1,
        "published_candidates": 1,
    }


def test_smart_wallet_candidates_reject_invalid_addresses():
    payload = {
        "tokens": [
            {
                "watching": 1,
                "chain": "bsc",
                "addr": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "symbol": "BAD",
                "early_buyers": [
                    {
                        "chain": "bsc",
                        "main_wallet": "not-an-address",
                        "role": "early-buyer",
                        "v2_total_pnl": 50_000,
                        "v2_win_rate": 80,
                        "v2_items_n": 50,
                    }
                ],
            }
        ]
    }

    rows, counts = monitor985.smart_wallet_candidates_from_payload(payload, limit=100)

    assert rows == []
    assert counts["unique_wallets"] == 0


def test_smart_wallet_candidates_do_not_combine_best_fields_from_different_profiles():
    address = "0x3333333333333333333333333333333333333333"
    payload = {
        "tokens": [
            {
                "watching": 1,
                "chain": "bsc",
                "addr": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "symbol": "MIXED",
                "early_buyers": [
                    {
                        "chain": "bsc",
                        "main_wallet": address,
                        "role": "early-buyer",
                        "v2_total_pnl": 25_000,
                        "v2_win_rate": 40,
                        "v2_items_n": 20,
                    },
                    {
                        "chain": "bsc",
                        "main_wallet": address,
                        "role": "early-buyer",
                        "v2_total_pnl": 100,
                        "v2_win_rate": 80,
                        "v2_items_n": 30,
                    },
                ],
            }
        ]
    }

    rows, _ = monitor985.smart_wallet_candidates_from_payload(payload, limit=100)

    assert rows == []


def test_cross_chain_observation_pool_copies_bsc_identity_without_promoting_evidence():
    candidates = [
        {
            "chain": "bsc",
            "address": "0x1111111111111111111111111111111111111111",
            "quality_score": 76,
            "verification_status": "candidate",
        },
        {
            "chain": "solana",
            "address": "11111111111111111111111111111111",
            "quality_score": 90,
            "verification_status": "candidate",
        },
    ]

    rows = monitor985.cross_chain_observation_rows(candidates)

    assert rows == [
        {
            "chain": "robinhood",
            "address": "0x1111111111111111111111111111111111111111",
            "source_chain": "bsc",
            "source": "985_smart_wallet_candidates",
            "status": "observation_only",
            "verification_status": "unverified_on_target_chain",
            "counts_for_resonance": False,
            "quality_score": 76,
        }
    ]
