import io
import json
import urllib.error
import pytest
from pathlib import Path

from alpha_fast_track import (annotate_new_execution_candidates, apply_quote, attach_fast_discovery_ranks, build_bsc_execution_input, build_live_replay_view, build_live_replay_with_sightings, build_robinhood_execution_input, build_shadow_entry_report, compact_jsonl, enrich_live_candidate_security, execution_impact_fields, fetch_pairs, identity, key_of, live_priority_targets, merge_market_quotes, prune_quote_cache, quote_from_report_row, read_priority_state, read_replay_history_cached, replay_requires_fast_persist, update_live_first_sightings,
                               overlay_report, priority_targets, quote_from_pairs, report_rows, research_shadow_assessment, retry_delay, run_cycle)
from alpha_gmgn_quote import GmgnRateLimitError, fetch_gmgn_token_info, normalize_gmgn_token_info


NOW = "2026-09-07T05:00:00+00:00"
TOKEN = "0x" + "a" * 40
ROW = {"chain": "bsc", "contract_address": TOKEN, "mcap": 1000, "price_usd": 1}


def v2_row(*, chain="robinhood", stage="aggregate_early_bird", address=TOKEN, **overrides):
    row = {
        "chain": chain,
        "contract_address": address,
        "symbol": "V2",
        "signal_stage": stage,
        "price_usd": 1.1,
        "mcap": 55_000 if chain == "bsc" else 220_000,
        "liquidity_usd": 25_000,
        "rank_score": 95,
        "pair_age_hours": 1,
        "recommendation_bucket": "ambush",
        "source_labels": ["GMGN", "DS"],
        "transaction_count5m": 50,
        "transaction_acceleration_ratio": 2,
        "narrative_score": 100,
        "smart_money_score": 100,
        "holder_quality_score": 100,
        "rank_components": {"timing": 31.5, "sources": 22, "unsafe": "drop-me"},
        "buy_route_ready": True,
        "sell_route_ready": True,
        "round_trip_loss_pct": 6,
        "buy_price_impact_pct": 4,
        "sell_price_impact_pct": 4,
        "hard_risk_pass": True,
        "sellable_cycles": 2,
        "sell_count": 1,
    }
    row.update(overrides)
    return row


def test_report_rows_preserves_explicit_promoted_stage_for_robinhood():
    row = v2_row(chain="robinhood", stage="aggregate_confirmation")

    selected = report_rows({"meme_potential_rows": [row]})

    assert selected[0]["signal_stage"] == "aggregate_confirmation"


def v2_history(row, *, first_price=1.0, first_mcap=None, first_seen="2026-09-07T04:55:00+00:00"):
    return {
        "first_seen_at": first_seen,
        "first_snapshot": {
            **row,
            "price_usd": first_price,
            "mcap": first_mcap or (50_000 if identity(row)[0] == "bsc" else 200_000),
        },
    }


def write_v2_history(tmp_path, *rows):
    history = {"rows": {key_of(row): v2_history(row) for row in rows}}
    (tmp_path / "alpha-radar-replay-history.json").write_text(json.dumps(history), encoding="utf-8")


def v2_quote(row, now=NOW):
    market_cap = row["mcap"]
    return quote_from_pairs(
        row,
        [{**pair(price=row["price_usd"], pool="pool-v2", liquidity=row["liquidity_usd"],
                 chain=row["chain"], address=row["contract_address"]), "marketCap": market_cap}],
        {},
        now,
    )


def classified_v2_row(*, chain="robinhood", stage=None, address=TOKEN, **overrides):
    from alpha_chain_strategy import classify_signal

    stage = stage or "aggregate_early_bird"
    row = v2_row(chain=chain, stage=stage, address=address, **overrides)
    return {
        **row,
        **classify_signal(row, v2_history(row), NOW),
        "signal_at": "2026-09-07T04:55:00+00:00",
        "execution_candidate": True,
        "execution_arm": "first_discovery",
    }


def test_v2_execution_input_separates_live_and_shadow_candidates(tmp_path):
    discovery = v2_row(chain="bsc", stage="aggregate_discovery", address="0x" + "b" * 40)
    early_bird = v2_row(chain="bsc", stage="aggregate_early_bird", address="0x" + "c" * 40)
    write_v2_history(tmp_path, discovery, early_bird)

    rows = annotate_new_execution_candidates([discovery, early_bird], tmp_path, NOW)
    quotes = {key_of(row): v2_quote(row) for row in (discovery, early_bird)}
    payload = build_bsc_execution_input(rows, quotes, NOW)

    assert payload["strategy_version"] == "chain_v2"
    assert [row["signal_stage"] for row in payload["signals"]] == ["aggregate_early_bird"]
    assert [row["signal_stage"] for row in payload["shadow_signals"]] == ["aggregate_discovery"]
    assert payload["signals"][0]["execution_mode"] == "live_candidate"
    assert payload["shadow_signals"][0]["execution_mode"] == "shadow"
    assert payload["signals"][0]["rank_components"] == {"timing": 31.5, "sources": 22.0}


def test_monitor_sections_publish_stable_signal_stages_and_keep_promotion():
    address = "0x" + "b" * 40
    discovery = v2_row(chain="bsc", stage="aggregate_discovery", address=address)
    early_bird = {**discovery, "symbol": "PROMOTED"}

    rows = report_rows({
        "meme_potential_rows": [early_bird],
        "meme_shadow_rows": [discovery],
    })

    assert len(rows) == 1
    assert rows[0]["signal_stage"] == "aggregate_early_bird"
    assert rows[0]["symbol"] == "PROMOTED"


def test_robinhood_potential_row_promotes_discovery_to_early_bird_execution_stage():
    address = "0x" + "d" * 40
    discovery = v2_row(chain="robinhood", stage="aggregate_discovery", address=address)
    potential = {**discovery, "symbol": "PROMOTED", "recommendation_bucket": "ambush"}

    rows = report_rows({
        "meme_rows": [discovery],
        "meme_potential_rows": [potential],
    })

    assert len(rows) == 1
    assert rows[0]["signal_stage"] == "aggregate_early_bird"
    assert rows[0]["recommendation_bucket"] == "ambush"


def test_fast_rank_uses_lightweight_sighting_time_for_a_new_token():
    now = "2026-09-07T05:00:30+00:00"
    row = v2_row(
        chain="robinhood",
        stage="aggregate_discovery",
        recommendation_bucket="shadow",
        rank_score=None,
    )
    row.update({
        "quote_status": "fresh",
        "quote_observed_at": now,
        "valuation_type": "market_cap",
    })
    sightings = {
        "rows": {
            key_of(row): {
                "key": key_of(row),
                "chain": "robinhood",
                "contract_address": TOKEN,
                "symbol": "V2",
                "first_seen_at": "2026-09-07T05:00:00+00:00",
            }
        }
    }

    replay = build_live_replay_with_sightings({}, sightings, [row], now)
    ranked = attach_fast_discovery_ranks([row], replay, now)

    assert ranked[0]["rank_components"]["timing"] == 19.0


def test_monitor_top_rows_publish_aggregate_discovery_before_promotion():
    robinhood = v2_row(chain="robinhood", address="0x" + "d" * 40)
    bsc = v2_row(chain="bsc", address="0x" + "e" * 40)
    robinhood.pop("signal_stage")
    bsc.pop("signal_stage")

    rows = report_rows({
        "meme_rows": [robinhood, bsc],
        "meme_potential_rows": [{**bsc, "symbol": "BSC-PROMOTED"}],
    })
    by_chain = {row["chain"]: row for row in rows}

    assert by_chain["robinhood"]["signal_stage"] == "aggregate_discovery"
    assert by_chain["bsc"]["signal_stage"] == "aggregate_early_bird"


def test_security_enrichment_only_blocks_live_entry_stages(tmp_path):
    bsc_discovery = v2_row(chain="bsc", stage="aggregate_discovery", address="0x" + "b" * 40, first_seen_at=NOW)
    bsc_early = v2_row(chain="bsc", stage="aggregate_early_bird", address="0x" + "c" * 40, first_seen_at=NOW)
    robinhood_discovery = v2_row(chain="robinhood", stage="aggregate_discovery", address="0x" + "d" * 40, first_seen_at=NOW)
    expired = v2_row(
        chain="robinhood",
        stage="aggregate_discovery",
        address="0x" + "e" * 40,
        first_seen_at="2026-09-07T04:00:00+00:00",
    )
    history = {
        "rows": {key_of(row): v2_history(row, first_seen=row["first_seen_at"]) for row in (
            bsc_discovery, bsc_early, robinhood_discovery, expired
        )}
    }
    seen = []

    def fake_enricher(rows, cache_path, now):
        seen.extend(key_of(row) for row in rows)
        return [{**row, "hard_risk_pass": True, "token_security": {"assessment_status": "ready"}} for row in rows]

    enriched = enrich_live_candidate_security(
        [bsc_discovery, bsc_early, robinhood_discovery, expired],
        history,
        tmp_path / "security.json",
        NOW,
        fake_enricher,
    )

    assert seen == [key_of(bsc_early)]
    by_key = {key_of(row): row for row in enriched}
    assert "token_security" not in by_key[key_of(bsc_discovery)]
    assert "token_security" not in by_key[key_of(expired)]
    assert by_key[key_of(bsc_early)]["token_security"]["assessment_status"] == "ready"
    assert "token_security" not in by_key[key_of(robinhood_discovery)]


def test_live_only_replay_persistence_detects_new_identity():
    previous = {"rows": {"bsc:old": {}}, "pending_first_snapshots": {}}
    unchanged = {"rows": {"bsc:old": {"observations": [1]}}, "pending_first_snapshots": {}}
    added = {"rows": {"bsc:old": {}, "bsc:new": {}}, "pending_first_snapshots": {}}

    assert replay_requires_fast_persist(previous, unchanged) is False
    assert replay_requires_fast_persist(previous, added) is True


def test_live_first_sighting_can_classify_the_first_fresh_quote_without_large_replay_write():
    row = {
        **v2_row(chain="bsc", stage="aggregate_early_bird", address="0x" + "f" * 40),
        "quote_status": "fresh", "quote_observed_at": NOW,
        "valuation_type": "market_cap", "mcap": 50_000, "market_cap": 50_000,
        "price_usd": 1, "pair_address": "pool-a",
    }
    sightings = update_live_first_sightings({}, [row], {"rows": {}}, NOW)
    replay = build_live_replay_with_sightings({"rows": {}}, sightings, [row], NOW)

    assert replay["rows"][key_of(row)]["first_seen_at"] == NOW
    assert replay["rows"][key_of(row)]["first_price_usd"] == 1
    assert replay["rows"][key_of(row)]["first_snapshot"]["mcap"] == 50_000


def test_live_first_sighting_freezes_discovery_metrics_before_a_fresh_quote_exists():
    address = "0x" + "9" * 40
    discovered = {
        **v2_row(chain="robinhood", stage="aggregate_discovery", address=address),
        "quote_status": "unavailable",
        "price_usd": 0.00005,
        "mcap": 50_000,
        "liquidity_usd": 25_000,
        "valuation_type": "market_cap",
        "source_groups": ["gmgn", "985"],
    }

    sightings = update_live_first_sightings({}, [discovered], {"rows": {}}, NOW)
    stored = sightings["rows"][key_of(discovered)]
    assert stored["first_price_usd"] == 0.00005
    assert stored["first_mcap_usd"] == 50_000
    assert stored["first_liquidity_usd"] == 25_000
    assert stored["discovery_snapshot"]["mcap"] == 50_000
    assert stored["discovery_snapshot"]["source_groups"] == ["gmgn", "985"]

    repriced = {
        **discovered,
        "quote_status": "fresh",
        "quote_observed_at": NOW,
        "price_usd": 0.0001,
        "mcap": 100_000,
        "market_cap": 100_000,
        "pair_address": "pool-later",
    }
    unchanged = update_live_first_sightings(sightings, [repriced], {"rows": {}}, NOW)
    replay = build_live_replay_with_sightings({"rows": {}}, unchanged, [repriced], NOW)
    history_row = replay["rows"][key_of(repriced)]

    assert history_row["first_price_usd"] == 0.00005
    assert history_row["first_mcap_usd"] == 50_000
    assert history_row["discovery_snapshot"]["mcap"] == 50_000
    assert history_row["first_snapshot"]["mcap"] == 100_000


def test_live_first_sighting_builds_static_replay_before_external_quote_is_fresh():
    address = "0x" + "8" * 40
    row = {
        **v2_row(chain="robinhood", stage="aggregate_discovery", address=address),
        "first_seen_at": NOW,
        "quote_status": "unavailable",
        "price_usd": 0.00005,
        "mcap": 50_000,
        "liquidity_usd": 25_000,
        "valuation_type": "market_cap",
        "pair_address": "0x" + "7" * 64,
        "source_groups": ["gmgn", "okx"],
    }

    sightings = update_live_first_sightings({}, [row], {"rows": {}}, NOW)
    replay = build_live_replay_with_sightings({"rows": {}}, sightings, [row], NOW)
    stored = replay["rows"][key_of(row)]

    assert stored["first_seen_at"] == NOW
    assert stored["first_mcap_usd"] == 50_000
    assert stored["first_tradeable_quote_at"] is None
    assert stored["first_snapshot"]["pair_address"] == row["pair_address"]


def test_execution_input_routes_recent_early_bird_reference_to_preflight_channel():
    from alpha_chain_strategy import classify_signal

    address = "0x" + "6" * 40
    pair_address = "0x" + "5" * 64
    source = v2_row(
        chain="robinhood",
        stage="aggregate_early_bird",
        address=address,
        pair_address=pair_address,
    )
    row = {
        **source,
        **classify_signal(source, v2_history(source, first_seen=NOW), NOW),
        "quote_status": "unavailable",
        "quote_observed_at": None,
    }

    payload = build_robinhood_execution_input([row], {}, NOW)

    assert payload["signals"] == []
    assert payload["quotes"] == []
    assert len(payload["preflight_signals"]) == 1
    candidate = payload["preflight_signals"][0]
    assert candidate["contract_address"] == address
    assert candidate["pool_address"] == pair_address
    assert candidate["snapshot_status"] == "discovery_reference"
    assert candidate["preflight_only"] is True
    assert candidate["order_authorized"] is False
    assert candidate["entry_authorized"] is True
    assert candidate["discovery_snapshot"] == {
        "chain": "robinhood",
        "contract_address": address,
        "pool_address": pair_address,
        "pair_address": pair_address,
        "first_seen_at": NOW,
        "price_usd": 1.0,
        "mcap": 200_000,
        "market_cap": 200_000,
        "liquidity": 25_000,
        "valuation_type": "market_cap",
    }


def test_execution_input_keeps_authorized_candidate_in_preflight_for_entry_window():
    from alpha_chain_strategy import classify_signal

    address = "0x" + "7" * 40
    pair_address = "0x" + "8" * 64
    first_seen = "2026-09-07T04:55:00+00:00"
    source = v2_row(
        chain="robinhood",
        stage="aggregate_early_bird",
        address=address,
        pair_address=pair_address,
    )
    row = {
        **source,
        **classify_signal(source, v2_history(source, first_seen=first_seen), NOW),
        "quote_status": "unavailable",
        "quote_observed_at": None,
    }

    payload = build_robinhood_execution_input([row], {}, NOW)

    assert payload["signals"] == []
    assert len(payload["preflight_signals"]) == 1
    assert payload["preflight_signals"][0]["first_seen_at"] == first_seen


def test_quote_journal_compaction_keeps_only_complete_recent_lines(tmp_path):
    path = tmp_path / "quotes.jsonl"
    path.write_bytes(b'{"id":1}\n{"id":2,"padding":"xxxx"}\n{"id":3}\n')

    assert compact_jsonl(path, max_bytes=30, retain_bytes=22) is True

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"id":3}']
    assert compact_jsonl(path, max_bytes=30, retain_bytes=22) is False


def test_fast_quote_cache_caps_rows_but_keeps_stale_active_tokens():
    keep = "bsc:0x" + "f" * 40
    quotes = {
        f"bsc:0x{index:040x}": {"last_attempt_at": NOW, "price_usd": index + 1}
        for index in range(2005)
    }
    quotes[keep] = {"last_attempt_at": "2026-09-01T00:00:00+00:00", "price_usd": 1}

    compacted = prune_quote_cache(quotes, NOW, keep_keys={keep})

    assert len(compacted) == 2000
    assert keep in compacted


def test_oversized_priority_state_uses_lightweight_report(tmp_path):
    held = {**ROW, "symbol": "HELD"}
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    (tmp_path / "report.json").write_text(
        json.dumps({"positions": [held], "pending_orders": []}),
        encoding="utf-8",
    )

    state = read_priority_state(state_path, max_bytes=1)

    assert state["open_positions"] == [held]


def test_live_replay_view_never_fabricates_history_for_a_new_candidate():
    existing = v2_row(address="0x" + "b" * 40, first_seen_at=NOW)
    new = v2_row(address="0x" + "c" * 40, first_seen_at=NOW)
    stored = v2_history(existing, first_seen=NOW)
    source = {"rows": {key_of(existing): stored}}

    view = build_live_replay_view(source, [existing, new], NOW)

    assert view["rows"][key_of(existing)] is stored
    assert key_of(new) not in view["rows"]
    assert key_of(new) not in source["rows"]


def test_live_priority_targets_keep_fresh_entries_and_real_positions(tmp_path):
    fresh = v2_row(chain="robinhood", address="0x" + "b" * 40, first_seen_at=NOW)
    old = v2_row(
        chain="robinhood",
        address="0x" + "c" * 40,
        first_seen_at="2026-09-07T04:00:00+00:00",
    )
    held = v2_row(
        chain="bsc",
        stage="aggregate_early_bird",
        address="0x" + "d" * 40,
        first_seen_at="2026-09-07T04:00:00+00:00",
    )
    (tmp_path / "okx-dex-sdk-live-state.json").write_text(
        json.dumps({"positions": {key_of(held): held}}), encoding="utf-8"
    )
    history = {
        "rows": {
            key_of(fresh): v2_history(fresh, first_seen=NOW),
            key_of(old): v2_history(old, first_seen=old["first_seen_at"]),
            key_of(held): v2_history(held, first_seen=held["first_seen_at"]),
        }
    }

    targets = live_priority_targets(
        {"meme_rows": [fresh, old], "meme_potential_rows": [held]},
        tmp_path,
        history,
        now=NOW,
    )

    assert [key_of(row) for row in targets] == [key_of(held), key_of(fresh)]


def test_v2_dogshit_style_robinhood_candidate_is_live_when_markup_is_within_limit(tmp_path):
    row = v2_row(
        chain="robinhood",
        stage="aggregate_early_bird",
        mcap=227_505.3,
        price_usd=1.1,
        change_m5=283,
        rank_score=100,
    )
    history = {"rows": {key_of(row): v2_history(row, first_mcap=206_823)}}
    (tmp_path / "alpha-radar-replay-history.json").write_text(json.dumps(history), encoding="utf-8")

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    payload = build_robinhood_execution_input(annotated, {key_of(row): v2_quote(row)}, NOW)

    assert len(payload["signals"]) == 1
    signal = payload["signals"][0]
    assert signal["first_mcap_usd"] == 206_823
    assert signal["markup_from_first"] == 1.1
    assert signal["rank_score"] == 100
    assert signal["change_m5"] == 283


def test_robinhood_early_bird_without_okx_quote_evidence_reaches_node_evaluation(tmp_path):
    row = v2_row(chain="robinhood", stage="aggregate_early_bird")
    for key in (
        "buy_route_ready",
        "sell_route_ready",
        "round_trip_loss_pct",
        "buy_price_impact_pct",
        "sell_price_impact_pct",
    ):
        row.pop(key)
    write_v2_history(tmp_path, row)

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    payload = build_robinhood_execution_input(annotated, {key_of(row): v2_quote(row)}, NOW)

    assert len(payload["signals"]) == 1
    assert payload["mode"] == "read_only_signal_input"
    assert payload["order_authorized"] is False
    signal = payload["signals"][0]
    assert signal["execution_mode"] == "live_candidate"
    assert signal["tradeability"]["status"] == "pending"
    assert signal["order_authorized"] is False
    assert signal["requires_executor_tradeability_check"] is True


def test_fast_track_fresh_route_without_executable_evidence_stays_pending(tmp_path):
    row = v2_row(chain="robinhood", stage="aggregate_early_bird")
    row.pop("buy_route_ready")
    row.pop("sell_route_ready")
    row["buy_route_fresh"] = True
    row["sell_route_fresh"] = True
    write_v2_history(tmp_path, row)

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    payload = build_robinhood_execution_input(annotated, {key_of(row): v2_quote(row)}, NOW)

    assert len(payload["signals"]) == 1
    assert payload["signals"][0]["tradeability"]["status"] == "pending"
    assert payload["signals"][0]["order_authorized"] is False


def test_v2_missing_rank_is_rejected_without_fabricating_zero(tmp_path):
    row = v2_row(rank_score=None)
    write_v2_history(tmp_path, row)

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    payload = build_robinhood_execution_input(annotated, {key_of(row): v2_quote(row)}, NOW)

    assert payload["signals"] == []
    rejection = payload["rejections"][0]
    assert rejection["rank_score"] is None
    assert rejection["rank_components"] == {"timing": 31.5, "sources": 22.0}
    assert rejection["reject_reason"] == "robinhood_rank_unavailable"


def test_v2_output_does_not_copy_secret_fields(tmp_path):
    row = v2_row(
        OKX_SECRET_KEY="secret",
        OKX_PASSPHRASE="passphrase",
        BSC_PRIVATE_KEY="private",
    )
    write_v2_history(tmp_path, row)

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    signal = build_robinhood_execution_input(annotated, {key_of(row): v2_quote(row)}, NOW)["signals"][0]

    assert "OKX_SECRET_KEY" not in signal
    assert "OKX_PASSPHRASE" not in signal
    assert "BSC_PRIVATE_KEY" not in signal


def pair(price=1, pool="pool-a", liquidity=20000, chain="bsc", address=TOKEN):
    return {"chainId": chain, "baseToken": {"address": address}, "priceUsd": price, "marketCap": 40000, "pairAddress": pool, "liquidity": {"usd": liquidity}, "txns": {"m5": {"buys": 12, "sells": 5}}, "volume": {"m5": 1500}}


def test_arc_dexscreener_lookup_uses_slug_and_preserves_market_fields(monkeypatch):
    observed_url = []
    created_at = 1_788_436_800_000
    payload = [{
        "chainId": "arc",
        "baseToken": {"address": TOKEN.upper().replace("0X", "0x")},
        "pairAddress": "0x" + "b" * 40,
        "priceUsd": "0.0042",
        "liquidity": {"usd": 18_500},
        "volume": {"m5": 320, "h24": 44_000},
        "txns": {"m5": {"buys": 17, "sells": 9}},
        "pairCreatedAt": created_at,
        "marketCap": 210_000,
        "url": f"https://dexscreener.com/arc/{'b' * 40}",
    }]

    def fake_urlopen(request, timeout):
        observed_url.append((request.full_url, timeout))
        return io.StringIO(json.dumps(payload))

    monkeypatch.setattr("alpha_fast_track.urllib.request.urlopen", fake_urlopen)

    pairs = fetch_pairs("5042", [TOKEN])
    quote = quote_from_pairs(
        {"chain": "arc-mainnet", "contract_address": TOKEN},
        pairs,
        {},
        "2026-09-07T12:00:00+00:00",
    )

    assert observed_url == [(f"https://api.dexscreener.com/tokens/v1/arc/{TOKEN}", 6)]
    assert quote["chain"] == "arc"
    assert quote["pair_address"] == "0x" + "b" * 40
    assert quote["price_usd"] == 0.0042
    assert quote["liquidity"] == 18_500
    assert quote["volume5m"] == 320
    assert quote["volume24h"] == 44_000
    assert (quote["buy_count5m"], quote["sell_count5m"]) == (17, 9)
    assert quote["pair_age_hours"] == 96.0
    assert quote["market_cap"] == 210_000


@pytest.mark.parametrize("value", [None, 0, -1, "NaN", "Infinity", True])
def test_r7_unknown_market_cap_is_null_and_not_executable(value):
    row = classified_v2_row(chain="bsc")
    quote = quote_from_pairs(row, [{**pair(), "marketCap": value, "fdv": 50_000}], {}, NOW)
    assert quote["mcap"] is None and quote["market_cap"] is None
    assert quote["fdv"] == 50_000
    assert quote["valuation_type"] == "fdv"
    assert quote["market_cap_source"] is None
    assert quote["fdv_source"] == "dexscreener.fdv"
    result = build_bsc_execution_input([row], {key_of(row): quote}, NOW)
    assert result["signals"] == []
    assert result["rejections"][0]["reject_reason"] == "market_cap_unavailable"
    assert result["rejections"][0]["fdv"] == 50_000


def test_r7_known_market_cap_preserves_separate_valuation_sources():
    quote = quote_from_pairs(ROW, [{**pair(), "fdv": 90_000}], {}, NOW)
    assert quote["mcap"] == quote["market_cap"] == 40_000
    assert quote["fdv"] == 90_000
    assert quote["valuation_type"] == "market_cap"
    assert quote["market_cap_source"] == "dexscreener.marketCap"


def test_r3_future_quote_cannot_overlay_price():
    quote = quote_from_pairs(ROW, [pair(price=2)], {}, "2026-09-07T05:00:01+00:00")
    result = apply_quote(ROW, quote, NOW)
    assert result["quote_status"] != "fresh"
    assert result["price_usd"] == ROW["price_usd"]


@pytest.mark.parametrize("valuation_type", ["fdv", "unavailable", "unknown", None, ""])
def test_r7_explicit_fdv_quote_cannot_emit_signal(valuation_type):
    row = classified_v2_row(chain="bsc")
    quote = {**quote_from_pairs(row, [pair()], {}, NOW), "valuation_type": valuation_type}
    result = build_bsc_execution_input([row], {key_of(row): quote}, NOW)
    assert result["signals"] == []
    assert result["rejections"][0]["reject_reason"] == "market_cap_unavailable"


def test_r7_signal_uses_current_quote_valuation_not_old_candidate():
    row = classified_v2_row(chain="bsc", mcap=55_000, fdv=200_000)
    quote = quote_from_pairs(row, [{**pair(), "fdv": 90_000}], {}, NOW)
    payload = build_bsc_execution_input([row], {key_of(row): quote}, NOW)
    signal = payload["signals"][0]
    for field in ("mcap", "market_cap", "fdv", "valuation_type", "market_cap_source", "fdv_source"):
        assert signal[field] == payload["quotes"][0][field] == quote[field]


def test_r7_legacy_market_cap_quote_publishes_explicit_type():
    row = classified_v2_row(chain="bsc")
    quote = quote_from_pairs(row, [pair()], {}, NOW)
    quote.pop("valuation_type")
    quote.pop("market_cap")
    result = build_bsc_execution_input([row], {key_of(row): quote}, NOW)
    for item in (result["signals"][0], result["quotes"][0]):
        assert item["valuation_type"] == "market_cap"
        assert item["mcap"] == item["market_cap"] == 40_000


def test_legacy_narrative_arm_cannot_authorize_a_v2_trade(tmp_path):
    row = {**ROW, "execution_arm": "narrative_breakout", "entry_score": 95}
    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    quote = quote_from_pairs(row, [pair(liquidity=303_000)], {}, NOW)
    result = build_bsc_execution_input(annotated, {key_of(row): quote}, NOW, paper_positions=[])

    assert result["signals"] == []
    assert result["rejections"][0]["reject_reason"] == "missing_first_snapshot"


def test_robinhood_candidate_reaches_its_own_execution_input(tmp_path):
    token = "0x" + "d" * 40
    pool = "0x" + "e" * 64
    row = v2_row(chain="robinhood", address=token, pair_address=pool)
    write_v2_history(tmp_path, row)
    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    quote = quote_from_pairs(
        row,
        [{**pair(pool=pool, chain="robinhood", address=token), "marketCap": row["mcap"]}],
        {},
        NOW,
    )
    result = build_robinhood_execution_input(annotated, {key_of(row): quote}, NOW)
    assert len(result["signals"]) == 1
    assert result["signals"][0]["chain"] == "robinhood"
    assert result["signals"][0]["pair_address"] == pool


def test_research_shadow_marks_only_the_chronological_robinhood_segment():
    row = {
        **ROW,
        "chain": "robinhood",
        "mcap": 120_000,
        "liquidity": 25_000,
        "change_h1": 20,
        "change_m5": 8,
        "source_count": 2,
        "source_groups": ["gmgn", "okx"],
    }
    result = research_shadow_assessment(
        row,
        {"first_snapshot": row},
        {"eligible": True, "score": 65, "first_mcap_usd": 120_000},
    )
    assert result["eligible"] is True
    assert result["mode"] == "shadow_only"
    assert result["metrics"]["source_count"] == 2
    assert result["metrics"]["source_groups"] == ["gmgn", "okx"]


def test_research_shadow_never_promotes_bsc_or_a_production_rejection():
    bsc = research_shadow_assessment(
        {**ROW, "mcap": 50_000, "liquidity": 25_000, "change_h1": 20, "change_m5": 8},
        None,
        {"eligible": True, "score": 65, "first_mcap_usd": 50_000},
    )
    rejected = research_shadow_assessment(
        {**ROW, "chain": "robinhood", "mcap": 50_000, "liquidity": 25_000, "change_h1": 20, "change_m5": 8},
        None,
        {"eligible": False, "score": 65, "first_mcap_usd": 50_000},
    )
    assert bsc["eligible"] is False
    assert "chain_not_validated" in bsc["reasons"]
    assert rejected["eligible"] is False
    assert "production_classifier_rejected" in rejected["reasons"]


def test_shadow_report_is_separate_from_execution_signals():
    shadow = {
        "policy": "chronological_holdout_robinhood_r1",
        "mode": "shadow_only",
        "eligible": True,
        "reasons": [],
        "metrics": {"score": 65},
    }
    report = build_shadow_entry_report([
        {**ROW, "chain": "robinhood", "entry_shadow": shadow, "execution_candidate": True},
    ], NOW)
    assert report["mode"] == "shadow_only_no_execution_effect"
    assert report["selected_count"] == 1


def test_new_candidate_does_not_reset_stale_first_seen_time(tmp_path):
    row = v2_row(chain="bsc", stage="aggregate_early_bird")
    history = {"rows": {key_of(row): v2_history(row, first_seen="2026-09-07T03:00:00+00:00")}}
    (tmp_path / "alpha-radar-replay-history.json").write_text(json.dumps(history), encoding="utf-8")
    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    quote = v2_quote(row)
    assert build_bsc_execution_input(annotated, {key_of(row): quote}, NOW)["signals"] == []
    assert annotated[0]["reject_reason"] == "bsc_entry_window"


def test_rejected_candidate_reason_is_published_without_becoming_a_signal(tmp_path):
    row = v2_row(chain="bsc", stage="aggregate_early_bird", symbol="BURRITO", rank_score=None)
    write_v2_history(tmp_path, row)

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    payload = build_bsc_execution_input(annotated, {}, NOW, paper_positions=[])

    assert payload["signals"] == []
    assert payload["rejections"][0]["symbol"] == "BURRITO"
    assert payload["rejections"][0]["rank_score"] is None
    assert payload["rejections"][0]["reject_reason"] == "bsc_rank_unavailable"


def test_reclassification_rejection_cannot_reuse_an_old_arm_or_paper_position(tmp_path):
    row = v2_row(chain="bsc", stage="aggregate_early_bird", hard_risk_pass=False)
    write_v2_history(tmp_path, row)
    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)
    quote = v2_quote(row)
    position = {
        "status": "open", "strategy": "first_discovery_probe", "chain": "bsc",
        "contract_address": TOKEN, "entry_time": NOW, "first_seen_at": NOW,
    }

    payload = build_bsc_execution_input(
        annotated, {key_of(row): quote}, NOW, paper_positions=[position],
    )

    assert payload["signals"] == []
    assert payload["rejections"][0]["reject_reason"] == "bsc_hard_risk"


def test_eligible_classification_carries_validated_first_discovery_evidence(tmp_path):
    row = v2_row(chain="robinhood", mcap=60_000, price_usd=1.2, pair_address="pool-a")
    history = {"rows": {key_of(row): v2_history(row, first_mcap=50_000)}}
    (tmp_path / "alpha-radar-replay-history.json").write_text(json.dumps(history), encoding="utf-8")
    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)

    assert annotated[0]["first_mcap_usd"] == 50_000
    assert annotated[0]["current_mcap_usd"] == 60_000
    assert annotated[0]["markup_from_first"] == 1.2


def test_watch_first_discovery_without_replay_history_cannot_enter_live(tmp_path):
    row = {
        **v2_row(chain="robinhood", mcap=50_000, price_usd=1.25),
        "symbol": "LAPTOP-fixture",
        "volume24h": 30_000,
        "change_m5": 5,
        "change_h1": 15,
        "pair_age_hours": 0.5,
        "watch_first_seen_at": "2026-09-07T04:58:00+00:00",
        "watch_first_seen_mcap": 40_000,
        "gmgn_risk_flags": [],
    }

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)

    assert annotated[0]["execution_candidate"] is False
    assert annotated[0]["execution_mode"] == "rejected"
    assert annotated[0]["reject_reason"] == "missing_first_snapshot"


def test_old_score_does_not_fill_missing_v2_rank(tmp_path):
    row = v2_row(rank_score=None, score=100, entry_score=100)
    write_v2_history(tmp_path, row)

    annotated = annotate_new_execution_candidates([row], tmp_path, NOW)

    assert annotated[0]["rank_score"] is None
    assert annotated[0]["legacy_score"] == 100
    assert annotated[0]["execution_candidate"] is False


def test_bnc4_like_large_bsc_discovery_is_not_promoted_by_legacy_narrative_arm(tmp_path):
    row = v2_row(chain="bsc", stage="aggregate_early_bird", symbol="BNC4-fixture",
                 mcap=668_900, liquidity_usd=303_000, entry_score=20)
    first = "2026-09-07T04:50:00+00:00"
    history = {"rows": {key_of(row): {"first_seen_at": first, "first_snapshot": row}}}
    (tmp_path / "alpha-radar-replay-history.json").write_text(json.dumps(history), encoding="utf-8")
    rows = annotate_new_execution_candidates([row], tmp_path, NOW)
    assert rows[0]["execution_mode"] == "rejected"
    quote = quote_from_pairs(row, [{**pair(liquidity=303_000), "marketCap": 668_900}], {}, NOW)
    payload = build_bsc_execution_input(rows, {key_of(row): quote}, NOW, paper_positions=[])
    assert payload["signals"] == []
    assert payload["rejections"][0]["reject_reason"] == "bsc_first_mcap_range"


def test_bridge_uses_rank_for_priority_without_rejecting_a_hard_gate_pass():
    low = classified_v2_row(chain="bsc", rank_score=15)
    high = classified_v2_row(chain="bsc", address="0x" + "b" * 40, rank_score=95)
    quotes = {key_of(r): v2_quote(r)
              for r in (low, high)}
    signals = build_bsc_execution_input([low, high], quotes, NOW)["signals"]
    assert [r["rank_score"] for r in signals] == [95, 15]


def test_pinned_pool_does_not_switch_to_high_price_or_other_chain():
    prior = quote_from_pairs(ROW, [pair()], {}, NOW)
    q = quote_from_pairs(ROW, [pair(1.1), pair(20, "pool-b", 90000), pair(90, "pool-a", chain="base")], prior, NOW)
    assert q["price_usd"] == 1.1
    assert q["pair_address"] == "pool-a"
    assert quote_from_pairs(ROW, [pair(20, "pool-b")], prior, NOW)["quote_status"] == "unavailable"


def test_dead_dust_pool_repins_to_a_liquid_replacement():
    prior = quote_from_pairs(ROW, [pair(price=0.0000002, pool="dust", liquidity=0.02)], {}, NOW)
    replacement = pair(price=0.000068, pool="main", liquidity=24_000)

    quote = quote_from_pairs(
        ROW,
        [replacement],
        {**prior, "quote_status": "unavailable"},
        "2026-09-07T05:00:10+00:00",
    )

    assert quote["quote_status"] == "fresh"
    assert quote["pair_address"] == "main"
    assert quote["price_usd"] == 0.000068
    assert quote["pool_replaced_from"] == "dust"


def test_gmgn_token_info_normalizes_primary_market_quote():
    payload = {
        "address": TOKEN,
        "symbol": "TEST",
        "biggest_pool_address": "gmgn-main",
        "circulating_supply": "1000000",
        "liquidity": "25000",
        "price": {
            "price": "0.08",
            "price_5m": "0.04",
            "buys_5m": 17,
            "sells_5m": 8,
            "volume_5m": "2000",
            "volume_24h": "100000",
        },
        "pool": {"pool_address": "gmgn-main", "creation_timestamp": 1788753600},
    }

    quote = normalize_gmgn_token_info(payload, "bsc", TOKEN, NOW)

    assert quote["quote_status"] == "fresh"
    assert quote["quote_source"] == "gmgn_skill_token_info"
    assert quote["price_usd"] == 0.08
    assert quote["market_cap"] == 80_000
    assert quote["liquidity"] == 25_000
    assert quote["pair_address"] == "gmgn-main"
    assert quote["change_m5"] == 100
    assert quote["buy_count5m"] == 17


def test_gmgn_token_info_uses_direct_readonly_api_without_cli_startup(monkeypatch):
    seen = {}
    payload = {
        "code": 0,
        "data": {
            "address": TOKEN,
            "symbol": "FAST",
            "biggest_pool_address": "gmgn-main",
            "circulating_supply": "1000000",
            "liquidity": "25000",
            "holder_count": 456,
            "dev": {"top_10_holder_rate": 0.123},
            "price": {"price": "0.08"},
        },
    }

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def opener(request, timeout):
        seen.update(url=request.full_url, api_key=request.get_header("X-apikey"), timeout=timeout)
        return Response()

    monkeypatch.setattr("alpha_gmgn_quote.gmgn_env", lambda: ({"GMGN_API_KEY": "secret"}, {}))
    quote = fetch_gmgn_token_info("bsc", TOKEN, timeout_seconds=3, opener=opener)

    assert seen["url"].startswith("https://openapi.gmgn.ai/v1/token/info?")
    assert "client_id=" in seen["url"] and "timestamp=" in seen["url"]
    assert seen["api_key"] == "secret"
    assert seen["timeout"] == 3
    assert quote["holder_count"] == 456
    assert quote["top10_holder_pct"] == 12.3


def test_gmgn_primary_quote_replaces_dead_dex_pool():
    prior = quote_from_pairs(ROW, [pair(price=0.0000002, pool="dust", liquidity=0.02)], {}, NOW)
    dex = {**prior, "quote_status": "unavailable", "quote_error": "pinned_pool_unavailable"}
    gmgn = {
        "chain": "bsc", "contract_address": TOKEN, "price_usd": 0.000068,
        "market_cap": 68_000, "mcap": 68_000, "liquidity": 24_000,
        "pair_address": "main", "quote_observed_at": "2026-09-07T05:00:10+00:00",
        "quote_source": "gmgn_skill_token_info", "quote_status": "fresh",
    }

    quote = merge_market_quotes(gmgn, dex, prior, "2026-09-07T05:00:10+00:00")

    assert quote["quote_status"] == "fresh"
    assert quote["quote_source"] == "gmgn_skill_token_info"
    assert quote["pair_address"] == "main"
    assert quote["price_usd"] == 0.000068
    assert quote["pool_replaced_from"] == "dust"


def test_same_pool_gmgn_dex_price_conflict_is_quarantined():
    gmgn = {
        "chain": "bsc", "contract_address": TOKEN, "price_usd": 1.0,
        "liquidity": 25_000, "pair_address": "main", "quote_observed_at": NOW,
        "quote_source": "gmgn_skill_token_info", "quote_status": "fresh",
    }
    dex = quote_from_pairs(ROW, [pair(price=0.1, pool="main", liquidity=25_000)], {}, NOW)

    quote = merge_market_quotes(gmgn, dex, {}, NOW)

    assert quote["quote_status"] == "quarantined"
    assert quote["quote_error"] == "gmgn_dex_price_conflict"


def test_run_cycle_uses_gmgn_first_and_dex_as_fallback(tmp_path):
    report_path = tmp_path / "report.json"
    gmgn_row = v2_row(chain="robinhood", address=TOKEN, mcap=80_000, price_usd=0.08)
    fallback_address = "0x" + "b" * 40
    fallback_row = v2_row(chain="robinhood", address=fallback_address, mcap=70_000, price_usd=0.07)
    report_path.write_text(json.dumps({"meme_rows": [gmgn_row, fallback_row]}), encoding="utf-8")

    def gmgn_fetch(chain, address):
        if address.lower() != TOKEN.lower():
            raise RuntimeError("gmgn unavailable")
        return {
            "chain": chain, "contract_address": address.lower(), "price_usd": 0.08,
            "market_cap": 80_000, "mcap": 80_000, "liquidity": 25_000,
            "pair_address": "gmgn-main", "quote_observed_at": NOW,
            "quote_source": "gmgn_skill_token_info", "quote_status": "fresh",
        }

    run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda chain, addresses: [
            pair(chain=chain, address=address, price=0.07, pool="dex-main", liquidity=20_000)
            for address in addresses
        ],
        gmgn_fetcher=gmgn_fetch,
        now=NOW,
    )

    state = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))
    assert state["quotes"][key_of(gmgn_row)]["quote_source"] == "gmgn_skill_token_info"
    assert state["quotes"][key_of(fallback_row)]["quote_source"] == "dexscreener"


def test_run_cycle_never_sends_arc_to_gmgn_and_keeps_dex_market_quote(tmp_path):
    report_path = tmp_path / "report.json"
    arc_address = "0x" + "c" * 40
    arc_row = {
        "chain": "5042",
        "contract_address": arc_address,
        "symbol": "ARC",
        "score": 100,
        "market_data_pending": True,
    }
    bsc_row = {**ROW, "symbol": "BSC", "score": 1}
    report_path.write_text(json.dumps({"meme_rows": [arc_row, bsc_row]}), encoding="utf-8")
    gmgn_calls = []

    def gmgn_fetch(chain, address):
        gmgn_calls.append((chain, address))
        return {
            "chain": chain,
            "contract_address": address,
            "price_usd": 1,
            "market_cap": 40_000,
            "mcap": 40_000,
            "liquidity": 20_000,
            "pair_address": "gmgn-main",
            "quote_observed_at": NOW,
            "quote_source": "gmgn_skill_token_info",
            "quote_status": "fresh",
        }

    run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda chain, addresses: [
            pair(chain=chain, address=address, pool=f"{chain}-dex")
            for address in addresses
        ],
        gmgn_fetcher=gmgn_fetch,
        now=NOW,
    )

    state = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))
    assert gmgn_calls == [("bsc", TOKEN)]
    assert state["quotes"][f"arc:{arc_address}"]["quote_source"] == "dexscreener"
    assert state["quotes"][f"arc:{arc_address}"]["gmgn_holder_status"] == "provider_unavailable"


def test_run_cycle_discards_persisted_arc_gmgn_cache_but_keeps_bsc_cache(tmp_path):
    report_path = tmp_path / "report.json"
    arc_address = "0x" + "f" * 40
    arc_row = {"chain": "arc_mainnet", "contract_address": arc_address, "symbol": "ARC"}
    bsc_row = {**ROW, "symbol": "BSC"}
    report_path.write_text(json.dumps({"meme_rows": [arc_row, bsc_row]}), encoding="utf-8")
    cached_at = "2026-09-07T04:59:50+00:00"
    (tmp_path / "alpha-fast-track.json").write_text(json.dumps({
        "quotes": {
            f"arc:{arc_address}": {
                "chain": "0x13b2",
                "contract_address": arc_address,
                "price_usd": 9,
                "market_cap": 900_000,
                "mcap": 900_000,
                "liquidity": 90_000,
                "holder_count": 999,
                "holders": 999,
                "pair_address": "legacy-arc-gmgn",
                "quote_observed_at": cached_at,
                "gmgn_observed_at": cached_at,
                "quote_source": "gmgn_skill_token_info",
                "quote_status": "fresh",
            },
            f"bsc:{TOKEN}": {
                "chain": "bsc",
                "contract_address": TOKEN,
                "price_usd": 2,
                "market_cap": 80_000,
                "mcap": 80_000,
                "liquidity": 30_000,
                "holder_count": 88,
                "pair_address": "bsc-gmgn",
                "quote_observed_at": cached_at,
                "gmgn_observed_at": cached_at,
                "quote_source": "gmgn_skill_token_info",
                "quote_status": "fresh",
            },
        },
        "status": {},
    }), encoding="utf-8")
    gmgn_calls = []

    run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda chain, addresses: [
            pair(chain=chain, address=address, pool=f"{chain}-dex")
            for address in addresses
        ],
        gmgn_fetcher=lambda *args: gmgn_calls.append(args) or {},
        now=NOW,
    )

    state = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))
    arc_quote = state["quotes"][f"arc:{arc_address}"]
    bsc_quote = state["quotes"][f"bsc:{TOKEN}"]
    assert gmgn_calls == []
    assert arc_quote["quote_source"] == "dexscreener"
    assert arc_quote["price_usd"] == 1
    assert arc_quote["pair_address"] == "arc-dex"
    assert "holder_count" not in arc_quote
    assert "holders" not in arc_quote
    assert arc_quote["gmgn_holder_status"] == "provider_unavailable"
    assert bsc_quote["quote_source"] == "gmgn_skill_token_info"
    assert bsc_quote["price_usd"] == 2
    assert bsc_quote["pair_address"] == "bsc-gmgn"
    assert bsc_quote["holder_count"] == 88


def test_run_cycle_discards_persisted_arc_gmgn_cache_when_dex_is_pending(tmp_path):
    report_path = tmp_path / "report.json"
    arc_address = "0x" + "9" * 40
    report_path.write_text(json.dumps({
        "meme_rows": [{
            "chain": "arc-mainnet",
            "contract_address": arc_address,
            "symbol": "ARC-PENDING",
            "market_data_pending": True,
        }]
    }), encoding="utf-8")
    cached_at = "2026-09-07T04:59:50+00:00"
    (tmp_path / "alpha-fast-track.json").write_text(json.dumps({
        "quotes": {f"arc:{arc_address}": {
            "chain": "5042",
            "contract_address": arc_address,
            "price_usd": 7,
            "holder_count": 777,
            "pair_address": "legacy-arc-gmgn",
            "quote_observed_at": cached_at,
            "gmgn_observed_at": cached_at,
            "quote_source": "gmgn_skill_token_info",
            "quote_status": "fresh",
        }},
        "status": {},
    }), encoding="utf-8")

    run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda chain, addresses: [],
        gmgn_fetcher=lambda *args: pytest.fail(f"ARC reached GMGN: {args}"),
        now=NOW,
    )

    quote = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))[
        "quotes"
    ][f"arc:{arc_address}"]
    assert quote["quote_status"] == "unavailable"
    assert quote["quote_error"] == "no_liquid_matching_pool"
    assert quote["gmgn_holder_status"] == "provider_unavailable"
    assert "price_usd" not in quote
    assert "holder_count" not in quote
    assert "pair_address" not in quote


def test_run_cycle_filters_arc_before_annotation_without_dropping_monitor_quote(monkeypatch, tmp_path):
    report_path = tmp_path / "report.json"
    arc_address = "0x" + "d" * 40
    robinhood_address = "0x" + "e" * 40
    rows = [
        {"chain": "arc-mainnet", "contract_address": arc_address, "symbol": "ARC"},
        {**ROW, "symbol": "BSC"},
        {"chain": "robinhood", "contract_address": robinhood_address, "symbol": "RH"},
    ]
    report_path.write_text(json.dumps({"meme_rows": rows}), encoding="utf-8")
    annotated_chains = []

    def capture(rows, out_dir, now, *, replay_history=None):
        annotated_chains.extend(identity(row)[0] for row in rows)
        return rows

    monkeypatch.setattr("alpha_fast_track.annotate_new_execution_candidates", capture)

    run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda chain, addresses: [
            pair(chain=chain, address=address, pool=f"{chain}-dex")
            for address in addresses
        ],
        gmgn_fetcher=None,
        now=NOW,
    )

    state = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))
    assert annotated_chains == ["bsc", "robinhood"]
    assert f"arc:{arc_address}" in state["quotes"]
    assert f"arc:{arc_address}" in state["evidence"]


def test_live_cycle_reuses_fresh_monitor_quote_without_duplicate_dex_request(tmp_path):
    row = v2_row(
        chain="robinhood",
        address=TOKEN,
        quote_status="fresh",
        quote_observed_at=NOW,
        quote_source="dexscreener",
        pair_address="pool-live",
        valuation_type="market_cap",
        volume5m=12_000,
        buy_count5m=42,
        sell_count5m=18,
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"meme_rows": [row]}), encoding="utf-8")
    history = {"rows": {key_of(row): v2_history(row, first_seen="2026-09-07T04:59:50+00:00")}}
    (tmp_path / "alpha-radar-replay-history.json").write_text(json.dumps(history), encoding="utf-8")

    def forbidden_fetch(*_args):
        raise AssertionError("fresh monitor quote should be reused")

    status = run_cycle(
        tmp_path,
        report_path,
        fetcher=forbidden_fetch,
        gmgn_fetcher=None,
        now=NOW,
        live_only=True,
    )

    assert status["ok"] is True
    assert status["fresh_count"] >= 1
    quote = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))["quotes"][key_of(row)]
    assert quote["pair_address"] == "pool-live"
    assert quote["quote_source"] == "dexscreener"


def test_live_cycle_reuses_recent_cached_quote_without_duplicate_dex_request(tmp_path):
    row = v2_row(chain="robinhood", address=TOKEN)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"meme_rows": [row]}), encoding="utf-8")
    write_v2_history(tmp_path, row)
    cached = v2_quote(row, NOW)
    (tmp_path / "alpha-fast-track.json").write_text(json.dumps({
        "quotes": {key_of(row): cached},
        "status": {},
    }), encoding="utf-8")
    calls = []

    def unexpected_fetch(*args):
        calls.append(args)
        return []

    status = run_cycle(
        tmp_path,
        report_path,
        fetcher=unexpected_fetch,
        gmgn_fetcher=None,
        now="2026-09-07T05:00:02+00:00",
        live_only=True,
    )

    assert calls == []
    assert status["fresh_count"] == 1
    assert status["errors"] == []


def test_replay_cache_reuses_unchanged_parse_and_reloads_changed_file(tmp_path):
    path = tmp_path / "alpha-radar-replay-history.json"
    path.write_text(json.dumps({"rows": {"first": {"value": 1}}}), encoding="utf-8")

    first = read_replay_history_cached(path)
    second = read_replay_history_cached(path)
    assert second is first

    path.write_text(json.dumps({"rows": {"second": {"value": 2}}}), encoding="utf-8")
    reloaded = read_replay_history_cached(path)
    assert reloaded is not first
    assert "second" in reloaded["rows"]


def test_quote_from_report_row_rejects_stale_or_route_less_values():
    fresh = quote_from_report_row({
        **ROW,
        "quote_status": "fresh",
        "quote_observed_at": NOW,
        "pair_address": "pool-a",
        "liquidity": 20_000,
        "valuation_type": "market_cap",
    }, NOW)
    assert fresh["pair_address"] == "pool-a"
    assert quote_from_report_row({**fresh, "pair_address": ""}, NOW) == {}
    assert quote_from_report_row({**fresh, "quote_observed_at": "2026-09-07T04:00:00+00:00"}, NOW) == {}


def test_run_cycle_respects_shared_gmgn_cooldown_and_keeps_dex_fallback(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"meme_rows": [ROW]}), encoding="utf-8")
    (tmp_path / "gmgn-wallet-profit-query-status.json").write_text(json.dumps({
        "retry_after": "2026-09-07T05:05:00+00:00",
        "retry_after_epoch": 1788757500,
    }), encoding="utf-8")

    def forbidden_gmgn(*args):
        raise AssertionError("GMGN must not be called during shared cooldown")

    status = run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda *args: [pair()],
        gmgn_fetcher=forbidden_gmgn,
        now=NOW,
    )

    state = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))
    assert status["gmgn_rate_limited"] is True
    assert state["quotes"][key_of(ROW)]["quote_source"] == "dexscreener"


def test_run_cycle_starts_shared_backoff_on_gmgn_429(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"meme_rows": [ROW]}), encoding="utf-8")

    def limited(*args):
        raise GmgnRateLimitError("rate limited", retry_after_epoch=1788757500)

    status = run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda *args: [pair()],
        gmgn_fetcher=limited,
        now=NOW,
    )

    cooldown = json.loads((tmp_path / "gmgn-wallet-profit-query-status.json").read_text(encoding="utf-8"))
    assert cooldown["retry_after_epoch"] == 1788757500
    assert status["gmgn_rate_limited"] is True
    assert status["fresh_count"] == 1


def test_dex_snapshot_adds_explicit_paper_impact_estimate():
    fields = execution_impact_fields(20_000)

    assert fields["price_impact"] == 0.0
    assert fields["depth_impact"] == 0.05
    assert fields["impact_notional_usd"] == 5.0
    assert fields["price_impact_source"] == "unavailable_from_dexscreener"


def test_quote_from_pairs_is_acceptable_to_execution_worker():
    quote = quote_from_pairs(ROW, [pair()], {}, NOW)

    assert quote["price_impact"] == 0.0
    assert quote["depth_impact"] == 0.05
    assert quote["impact_source"] == "dexscreener_depth_proxy"


def test_isolated_spike_is_quarantined_without_marking_fresh_price():
    old = quote_from_pairs(ROW, [pair()], {}, NOW)
    spike = quote_from_pairs(ROW, [pair(18)], old, "2026-09-07T05:00:10+00:00")
    assert spike["quote_status"] == "quarantined"
    assert spike["price_usd"] == 1
    assert spike["quote_observed_at"] == NOW
    restored = quote_from_pairs(ROW, [pair()], spike, "2026-09-07T05:00:20+00:00")
    assert restored["quote_status"] == "fresh"
    assert "pending_count" not in restored


def test_stale_overlay_does_not_apply_price_or_reemit_alert():
    q = quote_from_pairs(ROW, [pair(20)], {}, NOW)
    report = {"meme_rows": [ROW], "gold_watch_alerts": []}
    payload = {"updated_at": NOW, "quotes": {key_of(ROW): q}, "alerts": [{"id": "stale"}], "status": {"ok": True}}
    result = overlay_report(report, payload, "2026-09-07T05:01:00+00:00")
    assert result["meme_rows"][0]["price_usd"] == 1
    assert result["meme_rows"][0]["quote_status"] == "stale"
    assert result["gold_watch_alerts"] == []
    assert result["meta"]["fast_track"]["ok"] is False


def test_solana_case_preserved_and_evm_normalized():
    assert identity({"chain": "sol", "address": "AbCdEf"}) == ("solana", "AbCdEf")
    assert identity({"chain": "56", "address": TOKEN.upper().replace("0X", "0x")}) == ("bsc", TOKEN)


def test_unavailable_quote_never_falls_back_as_fresh():
    assert apply_quote(ROW, {}, NOW)["quote_status"] == "unavailable"
    assert quote_from_pairs(ROW, [pair(address="0x" + "b" * 40)], {}, NOW)["quote_status"] == "unavailable"


def test_held_positions_first_and_unquoted_rotate_before_recent(tmp_path: Path):
    held = {**ROW, "contract_address": "0x" + "b" * 40}
    (tmp_path / "alpha-test-paper-state.json").write_text(json.dumps({"open_positions": [held]}))
    other = {**ROW, "contract_address": "0x" + "c" * 40}
    rows = priority_targets({"meme_rows": [ROW, other]}, tmp_path, {"quotes": {key_of(ROW): {"quote_observed_at": NOW}}}, limit=2)
    assert [key_of(r) for r in rows] == [key_of(held), key_of(other)]


def test_strict_position_key_restores_identity_and_prioritizes_delisted_holding(tmp_path):
    state = {"positions": {"first|" + key_of(ROW): {"key": key_of(ROW), "symbol": "HELD"}},
             "quotes": {key_of(ROW): {**ROW, "pair_address": "pool-a"}}}
    (tmp_path / "alpha-execution-state.json").write_text(json.dumps(state))
    rows = priority_targets({"meme_rows": []}, tmp_path, {}, limit=1)
    assert key_of(rows[0]) == key_of(ROW)
    assert rows[0]["symbol"] == "HELD"
    assert rows[0]["pair_address"] == "pool-a"


def test_rate_limit_respects_retry_after():
    error = urllib.error.HTTPError("https://example.test", 429, "rate limit", {"Retry-After": "180"}, None)
    assert retry_delay(error, 1, NOW) == 180
    assert retry_delay(error, 4, NOW) == 480


def test_cooldown_makes_no_market_requests(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"meme_rows": [ROW]}))
    (tmp_path / "alpha-fast-track.json").write_text(json.dumps({"status": {
        "retry_after": "2026-09-07T05:01:00+00:00", "rate_limit_failures": 1}}))
    def forbidden_fetch(*args):
        raise AssertionError("must not fetch while cooling down")
    status = run_cycle(tmp_path, report_path, fetcher=forbidden_fetch, now=NOW)
    assert status["tracked_count"] == 0
    assert status["rate_limited"] is True
    assert status["fresh_count"] == 0
    assert status["errors"] == ["上游限流退避中，暂停新请求"]


def test_forward_paper_holding_and_pending_buy_remain_quote_targets(tmp_path):
    folder = tmp_path / "profitable-wallet-paper"
    folder.mkdir()
    held = "bsc:" + "0x" + "b" * 40
    pending = "base:" + "0x" + "c" * 40
    (folder / "state.json").write_text(json.dumps({"execution": {
        "positions": {"hold": {"key": held, "symbol": "HELD"}},
        "orders": {"pending": {"key": pending, "symbol": "PENDING", "side": "buy"}}}}))
    rows = priority_targets({"meme_rows": []}, tmp_path, {}, limit=2)
    assert {key_of(r) for r in rows} == {held, pending}


def test_challenger_holdings_remain_quote_targets(tmp_path):
    folder = tmp_path / "execution-challenger"
    folder.mkdir()
    (folder / "state.json").write_text(json.dumps({"execution": {
        "positions": {"pullback|" + key_of(ROW): {"key": key_of(ROW), "symbol": "TRIAL"}},
        "quotes": {key_of(ROW): ROW}}}))
    rows = priority_targets({"meme_rows": []}, tmp_path, {}, limit=1)
    assert len(rows) == 1 and rows[0]["symbol"] == "TRIAL"


def test_challenger_failure_isolated_from_execution_input(tmp_path, monkeypatch):
    import alpha_execution_challenger
    def fail(*args):
        raise ValueError("fixed config mismatch")
    monkeypatch.setattr(alpha_execution_challenger, "run_once", fail)
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"meme_rows": [ROW]}))
    result = run_cycle(tmp_path, path, fetcher=lambda *args: [pair()], now=NOW)
    assert result["ok"]
    assert result["execution_challenger"]["ok"] is False
    assert (tmp_path / "bsc-execution-input.json").exists()


def test_challenger_summary_overlay_is_distinct_and_stale_aware():
    result = overlay_report({}, {"updated_at": NOW, "status": {"ok": True},
        "execution_challenger": {"ok": True, "realized_pnl_usd": -2, "updated_at": NOW}},
        "2026-09-07T05:01:00+00:00")
    assert result["meta"]["execution_challenger"]["stale"] is True
    assert result["meta"]["execution_challenger"]["realized_pnl_usd"] == -2
    assert "execution_audit" not in result["meta"]


def test_wallet_paper_failure_does_not_break_market_quotes(tmp_path, monkeypatch):
    import alpha_wallet_forward_paper
    def fail(*args, **kwargs):
        raise ValueError("corrupt paper state")
    monkeypatch.setattr(alpha_wallet_forward_paper, "run_once", fail)
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"meme_rows": [ROW]}))
    status = run_cycle(tmp_path, path, fetcher=lambda *args: [pair()], now=NOW)
    assert status["ok"]
    assert not status["wallet_forward_paper"]["ok"]
    assert "corrupt paper state" in status["wallet_forward_paper"]["error"]


def test_strict_audit_lock_does_not_break_market_publication(tmp_path, monkeypatch):
    import alpha_execution_audit
    import alpha_wallet_forward_paper
    import alpha_execution_challenger
    import alpha_gmgn_execution_bridge
    def unavailable(*args, **kwargs):
        raise ValueError("isolated paper fixture")
    monkeypatch.setattr(alpha_wallet_forward_paper, "run_once", unavailable)
    monkeypatch.setattr(alpha_execution_challenger, "run_once", unavailable)
    monkeypatch.setattr(alpha_gmgn_execution_bridge, "export_intents", lambda *a: {})
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"meme_rows": [ROW]}))
    lock = tmp_path / "alpha-execution.lock"
    with alpha_execution_audit.exclusive_lock(lock):
        status = run_cycle(tmp_path, path, fetcher=lambda *args: [pair()], now=NOW)
    assert status["ok"]
    assert status["execution_audit"]["ok"] is False
    assert status["execution_audit"]["error"] == "PermissionError"
    payload = json.loads((tmp_path / "alpha-fast-track.json").read_text(encoding="utf-8"))
    assert payload["updated_at"] == NOW
    assert payload["quotes"][key_of(ROW)]["quote_status"] == "fresh"
    assert (tmp_path / "bsc-execution-input.json").exists()
    assert lock.exists()


def test_qualified_wallet_inbox_tokens_join_quote_rotation(tmp_path):
    from test_alpha_wallet_forward_paper import profile, at, WALLETS
    (tmp_path / "gmgn-smart-money-top50.json").write_text(json.dumps({"wallets": [profile(WALLETS[0])]}))
    inbox = tmp_path / "meme-source-inbox"
    inbox.mkdir()
    (inbox / "gmgn-wallet-flow.json").write_text(json.dumps({"rows": [
        {**ROW, "wallet": WALLETS[0], "observed_at": at(1)},
        {**ROW, "contract_address": "0x" + "f" * 40, "wallet": "0x" + "e" * 40, "observed_at": at(1)}]}))
    rows = priority_targets({"meme_rows": []}, tmp_path, {}, now=at(10))
    assert [key_of(r) for r in rows] == [key_of(ROW)]


def test_run_cycle_writes_only_fresh_bsc_first_discovery_inputs(tmp_path):
    report_path = tmp_path / "report.json"
    valid = {
        **v2_row(chain="bsc", stage="aggregate_early_bird", mcap=40_000, price_usd=1),
        "symbol": "FRESH",
        "volume24h": 60_000,
        "change_m5": 10,
        "change_h1": 20,
        "pair_age_hours": 1,
        "signal_at": NOW,
        "first_seen_at": NOW,
        "entry_score": 88,
        "risk_flags": [],
        "gmgn_risk_flags": [],
        "source_evidence": {"source": "unit-test", "tx_hash": "0xabc"},
    }
    stale_signal = {**valid, "contract_address": "0x" + "b" * 40,
                    "signal_at": "2026-09-07T04:00:00+00:00",
                    "first_seen_at": "2026-09-07T04:00:00+00:00"}
    missing_signal = {**valid, "contract_address": "0x" + "c" * 40}
    missing_signal.pop("signal_at")
    missing_signal.pop("first_seen_at")
    missing_signal["rank_score"] = None
    missing_signal.pop("entry_score")
    non_bsc = {**valid, "contract_address": "0x" + "d" * 40, "chain": "base"}
    report_path.write_text(json.dumps({"meme_rows": [valid, stale_signal, missing_signal, non_bsc]}))
    history = {"rows": {
        key_of(row): {"first_seen_at": row.get("first_seen_at"), "first_snapshot": row}
        for row in (valid, stale_signal, missing_signal, non_bsc)
    }}
    (tmp_path / "alpha-radar-replay-history.json").write_text(json.dumps(history))

    status = run_cycle(tmp_path, report_path,
                       fetcher=lambda chain, addresses: [pair(address=address) for address in addresses], now=NOW)

    payload = json.loads((tmp_path / "bsc-execution-input.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "read_only_signal_input"
    assert payload["strategy_version"] == "chain_v2"
    assert [row["symbol"] for row in payload["signals"]] == ["FRESH"]
    assert payload["signals"][0]["chain"] == "bsc"
    assert payload["signals"][0]["signal_at"] == NOW
    assert payload["signals"][0]["first_seen_at"] == NOW
    assert payload["signals"][0]["execution_arm"] == "first_discovery"
    assert payload["signals"][0]["entry_score"] == 88
    assert payload["signals"][0]["source_evidence"] == {"source": "unit-test", "tx_hash": "0xabc"}
    assert len(payload["quotes"]) == 1
    assert payload["quotes"][0]["quote_status"] == "fresh"
    assert payload["quotes"][0]["chain"] == "bsc"
    assert payload["quotes"][0]["pair_address"] == "pool-a"
    assert 0 <= status["input_publish_seconds"] <= status["elapsed_seconds"]


def test_run_cycle_keeps_unlabeled_robinhood_top_discovery_out_of_live_input(tmp_path):
    report_path = tmp_path / "report.json"
    row = {
        **v2_row(chain="robinhood", mcap=200_000, price_usd=1),
        "symbol": "FRESH-RBH",
        "first_seen_at": NOW,
        "hard_risk_pass": True,
    }
    row.pop("signal_stage")
    report_path.write_text(json.dumps({"meme_rows": [row]}), encoding="utf-8")
    (tmp_path / "alpha-radar-replay-history.json").write_text(
        json.dumps({"rows": {key_of(row): v2_history(row, first_mcap=200_000)}}),
        encoding="utf-8",
    )

    run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda chain, addresses: [
            pair(chain="robinhood", address=address, price=1, liquidity=25_000)
            for address in addresses
        ],
        now=NOW,
    )

    payload = json.loads((tmp_path / "robinhood-execution-input.json").read_text(encoding="utf-8"))
    assert payload["signals"] == []


def test_run_cycle_promotes_a_fresh_quote_into_immutable_replay_before_classification(tmp_path):
    report_path = tmp_path / "report.json"
    row = {
        **v2_row(chain="bsc", stage="aggregate_early_bird", mcap=50_000, price_usd=1),
        "symbol": "NEW",
        "valuation_type": "market_cap",
        "quote_status": "unavailable",
        "quote_observed_at": None,
    }
    row.pop("rank_score")
    row.pop("rank_components")
    report_path.write_text(json.dumps({"meme_potential_rows": [row]}), encoding="utf-8")
    (tmp_path / "alpha-radar-replay-history.json").write_text(
        json.dumps({
            "rows": {},
            "pending_first_snapshots": {
                key_of(row): {
                    "key": key_of(row),
                    "chain": "bsc",
                    "contract_address": row["contract_address"],
                    "first_seen_at": NOW,
                    "first_snapshot": {"symbol": "NEW"},
                }
            },
        }),
        encoding="utf-8",
    )

    run_cycle(
        tmp_path,
        report_path,
        fetcher=lambda chain, addresses: [
            pair(address=address, price=1, liquidity=25_000) for address in addresses
        ],
        now=NOW,
    )

    replay = json.loads((tmp_path / "alpha-radar-replay-history.json").read_text(encoding="utf-8"))
    payload = json.loads((tmp_path / "bsc-execution-input.json").read_text(encoding="utf-8"))
    assert key_of(row) in replay["rows"]
    assert key_of(row) not in replay["pending_first_snapshots"]
    assert [signal["symbol"] for signal in payload["signals"]] == ["NEW"]
    assert isinstance(payload["signals"][0]["rank_score"], (int, float))
    assert payload["signals"][0]["rank_components"]


def test_execution_input_rejects_quote_token_or_chain_identity_mismatch():
    row = classified_v2_row(chain="bsc")
    quote = {**v2_quote(row), "quote_at": NOW}
    token_mismatch = {**quote, "contract_address": "0x" + "b" * 40}
    chain_mismatch = {**quote, "chain": "base"}

    assert build_bsc_execution_input([row], {key_of(row): token_mismatch}, NOW)["signals"] == []
    assert build_bsc_execution_input([row], {key_of(row): chain_mismatch}, NOW)["signals"] == []


def test_paper_position_cannot_promote_an_unclassified_row(monkeypatch):
    row = {
        **ROW,
        "symbol": "PAPER",
        "pair_address": "pool-a",
        "signal_at": NOW,
        "first_seen_at": NOW,
    }
    quote = {**quote_from_pairs(row, [pair()], {}, NOW), "quote_at": NOW}
    position = {
        "status": "open",
        "strategy": "first_discovery_probe",
        "chain": "bsc",
        "contract_address": TOKEN,
        "entry_time": NOW,
        "first_seen_at": NOW,
        "size_usd": 20,
    }
    monkeypatch.setenv("BSC_LIVE_BUY_AMOUNT_ATOMIC", "5000000000000000")
    monkeypatch.setenv("BSC_LIVE_SLIPPAGE_PERCENT", "3")

    payload = build_bsc_execution_input(
        [row],
        {key_of(row): quote},
        NOW,
        paper_positions=[position],
    )

    assert payload["signals"] == []
    assert payload["shadow_signals"] == []
