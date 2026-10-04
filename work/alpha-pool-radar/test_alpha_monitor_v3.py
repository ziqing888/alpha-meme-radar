import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


BASE = Path(__file__).parent
NOW = "2026-09-10T12:00:00+00:00"
TOKEN = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


monitor = load_module("alpha_monitor_v3", BASE / "alpha_monitor_v3.py")


@pytest.mark.parametrize(
    ("source", "family", "role", "lane"),
    [
        ("gmgn_trenches", "gmgn", "discovery", "new_launch"),
        ("gmgn_skills_trending", "gmgn", "ranking", "trending"),
        ("gmgn_skills_hot_searches", "gmgn", "ranking", "hot_search"),
        ("gmgn_cli_smartmoney", "gmgn", "wallet", "smart_money"),
        ("gmgn_skills_kol", "gmgn", "ranking", "kol_social"),
        ("okx_trenches", "okx", "discovery", "new_launch"),
        ("okx_signal", "okx", "market", "market_flow"),
        ("985_monitor", "985", "wallet", "smart_money"),
        ("985_fomo_wallets", "985", "wallet", "smart_money"),
        ("985_smartmoney", "985", "wallet", "smart_money"),
        ("debot_trenches", "debot", "discovery", "new_launch"),
        ("debot_signal", "debot", "market", "market_flow"),
        ("wind_monitor", "wind", "ranking", "kol_social"),
        ("proficy_trending", "proficy", "ranking", "trending"),
        ("noxa_launchpad", "noxa", "discovery", "new_launch"),
        ("bsc_onchain", "onchain", "discovery", "new_launch"),
        ("fourmeme_launchpad", "onchain", "discovery", "new_launch"),
        ("flap_launchpad", "onchain", "discovery", "new_launch"),
        ("pancakeswap_v2_pair_created", "onchain", "discovery", "new_launch"),
        ("robinhood_factory_pair-created", "onchain", "discovery", "new_launch"),
    ],
)
def test_source_descriptor_maps_provider_roles_and_lanes(source, family, role, lane):
    descriptor = monitor.source_descriptor(source)

    assert descriptor.provider_family == family
    assert descriptor.provider_feed == source
    assert descriptor.evidence_role == role
    assert descriptor.signal_lane == lane


@pytest.mark.parametrize(
    ("kind", "role", "lane"),
    [
        ("dex_paid", "ranking", "trending"),
        ("pump_callout", "ranking", "kol_social"),
        ("pump_trade_buy", "wallet", "smart_money"),
        ("pump_trade_sell", "wallet", "smart_money"),
    ],
)
def test_985_monitor_event_kind_controls_evidence_semantics(kind, role, lane):
    descriptor = monitor.source_descriptor("985_monitor", {"monitor985_kind": kind})

    assert descriptor.provider_family == "985"
    assert descriptor.evidence_role == role
    assert descriptor.signal_lane == lane


@pytest.mark.parametrize(
    ("source", "raw", "family", "role", "lane"),
    [
        ("985-monitor-fast", {"kind": "dex_paid"}, "985", "ranking", "trending"),
        ("985-fomo-wallets", {}, "985", "wallet", "smart_money"),
        ("985-smartmoney", {}, "985", "wallet", "smart_money"),
        ("proficy-trending", {}, "proficy", "ranking", "trending"),
        ("wind-monitor", {}, "wind", "ranking", "kol_social"),
        ("noxa-launchpad", {}, "noxa", "discovery", "new_launch"),
        ("gmgn_trending", {}, "gmgn", "ranking", "trending"),
        ("okx_trenches", {}, "okx", "discovery", "new_launch"),
        ("arc_rpc", {}, "onchain", "discovery", "new_launch"),
        ("arc_arcscan", {}, "onchain", "discovery", "new_launch"),
    ],
)
def test_arc_provider_rows_keep_configured_source_semantics(source, raw, family, role, lane):
    descriptor = monitor.source_descriptor(source, raw)

    assert descriptor.provider_family == family
    assert descriptor.evidence_role == role
    assert descriptor.signal_lane == lane


@pytest.mark.parametrize(
    ("source", "raw"),
    [
        ("985-monitor-fast", {"kind": "dex_paid"}),
        ("985-fomo-wallets", {"buyer_address": "0x1111111111111111111111111111111111111111", "direction": "buy", "verified": True}),
        ("985-fomo-wallets", {"fomo_count": 12}),
        ("985-smartmoney", {"buyer_address": "0x2222222222222222222222222222222222222222", "direction": "buy", "verified": True}),
        ("proficy-trending", {}),
        ("wind-monitor", {}),
        ("noxa-launchpad", {}),
        ("gmgn_trending", {}),
        ("okx_trenches", {}),
        ("arc_rpc", {}),
        ("arc_arcscan", {}),
    ],
)
def test_arc_provider_rows_accept_valid_arc_evm_addresses(source, raw):
    event, rejection = monitor.normalize_provider_event(
        {"event_at": NOW, **raw},
        source=source,
        chain="0x13b2",
        contract_address=TOKEN.upper(),
        observed_at=NOW,
    )

    assert rejection is None
    assert event["chain"] == "arc"
    assert event["contract_address"] == TOKEN


@pytest.mark.parametrize("chain", ("bsc", "robinhood", "ethereum"))
def test_arc_provider_rows_reject_non_arc_chain_identities(chain):
    event, rejection = monitor.normalize_provider_event(
        {"event_at": NOW, "fomo_count": 12},
        source="arc_rpc",
        chain=chain,
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert event is None
    assert rejection["reason"] == "wrong_chain"


def test_arc_provider_rows_reject_malformed_evm_addresses():
    event, rejection = monitor.normalize_provider_event(
        {"event_at": NOW},
        source="arc_arcscan",
        chain="arc-mainnet",
        contract_address="0x1234",
        observed_at=NOW,
    )

    assert event is None
    assert rejection["reason"] == "invalid_address"


def test_arc_dex_paid_and_addressless_fomo_never_become_verified_wallet_evidence():
    paid, paid_rejection = monitor.normalize_provider_event(
        {"event_at": NOW, "kind": "dex_paid"},
        source="985-monitor-fast",
        chain="arc",
        contract_address=TOKEN,
        observed_at=NOW,
    )
    fomo, fomo_rejection = monitor.normalize_provider_event(
        {"event_at": NOW, "fomo_count": 12},
        source="985-fomo-wallets",
        chain="arc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert paid_rejection is None
    assert fomo_rejection is None
    token = token_from(
        project(events=[{**paid, "chain": "bsc"}, {**fomo, "chain": "bsc"}])
    )
    assert token["wallet_evidence"]["verified_buyers"] == 0
    assert "smart_cluster" not in token["active_states"]


def test_existing_985_event_semantics_are_corrected_without_duplicating_event():
    old = projection_event(
        "985-paid",
        chain="robinhood",
        family="985",
        feed="985_monitor",
        role="wallet",
        lane="smart_money",
    )
    initial = project(events=[old])
    corrected = project(
        initial,
        events=[{
            **old,
            "event_type": "trending",
            "evidence_role": "ranking",
            "signal_lane": "trending",
        }],
        observed_at="2026-09-10T12:00:10+00:00",
    )
    token = token_from(corrected, chain="robinhood")

    assert len(token["events"]) == 1
    assert token["events"][0]["evidence_role"] == "ranking"
    assert token["events"][0]["signal_lane"] == "trending"


def test_source_descriptor_is_immutable():
    descriptor = monitor.source_descriptor("gmgn_trending")

    with pytest.raises(Exception):
        descriptor.provider_family = "other"


def test_dexscreener_lookup_is_enrichment_and_never_resonance():
    event, rejection = monitor.normalize_provider_event(
        {"lookup_by_ca": True, "observed_at": NOW},
        source="profile_latest",
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["provider_family"] == "dexscreener"
    assert event["evidence_role"] == "enrichment"
    assert event["counts_for_resonance"] is False


def test_gmgn_creation_timestamp_is_preserved_for_age_gating():
    event, rejection = monitor.normalize_provider_event(
        {
            "observed_at": NOW,
            "creation_timestamp": 1788998400,
        },
        source="gmgn_skills_hot_searches",
        chain="robinhood",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["real_created_at"] == "2026-09-10T00:00:00+00:00"


@pytest.mark.parametrize("source", ["gmgn_skill_token_info", "okx_market_detail"])
def test_provider_detail_lookup_by_ca_is_enrichment(source):
    event, rejection = monitor.normalize_provider_event(
        {"lookup_by_ca": True, "event_at": NOW},
        source=source,
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["evidence_role"] == "enrichment"
    assert event["counts_for_resonance"] is False


@pytest.mark.parametrize(
    ("explicit_role", "explicit_lane"),
    [("ranking", "hot_search"), ("market", "market_flow"), ("audit", "trending")],
)
def test_lookup_by_ca_overrides_contradictory_explicit_semantics(explicit_role, explicit_lane):
    event, rejection = monitor.normalize_provider_event(
        {
            "lookup_by_ca": True,
            "evidence_role": explicit_role,
            "signal_lane": explicit_lane,
            "event_at": NOW,
        },
        source="boost_top",
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["evidence_role"] == "enrichment"
    assert event["signal_lane"] == "market_flow"
    assert event["counts_for_resonance"] is False


@pytest.mark.parametrize("source", ["gmgn_audit", "okx_security"])
def test_genuine_audit_feed_lookup_by_ca_keeps_audit_semantics(source):
    event, rejection = monitor.normalize_provider_event(
        {"lookup_by_ca": True, "event_at": NOW},
        source=source,
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["evidence_role"] == "audit"
    assert event["signal_lane"] == "security_audit"
    assert event["counts_for_resonance"] is False


def test_explicit_audit_lookup_by_ca_keeps_audit_semantics():
    event, rejection = monitor.normalize_provider_event(
        {
            "lookup_by_ca": True,
            "evidence_role": "audit",
            "signal_lane": "security_audit",
            "event_at": NOW,
        },
        source="profile_latest",
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["evidence_role"] == "audit"
    assert event["signal_lane"] == "security_audit"
    assert event["counts_for_resonance"] is False


@pytest.mark.parametrize(
    ("raw", "expected_role", "expected_lane", "counts"),
    [
        ({"lookup_by_ca": False}, "ranking", "trending", True),
        ({"evidence_role": "market", "signal_lane": "market_flow"}, "market", "market_flow", True),
        ({"evidence_role": "audit", "signal_lane": "security_audit"}, "audit", "security_audit", False),
    ],
)
def test_dexscreener_can_prove_unsolicited_or_audit_evidence(raw, expected_role, expected_lane, counts):
    event, rejection = monitor.normalize_provider_event(
        {**raw, "event_at": NOW},
        source="boost_top",
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["evidence_role"] == expected_role
    assert event["signal_lane"] == expected_lane
    assert event["counts_for_resonance"] is counts


def test_event_contract_normalizes_address_and_contains_required_fields():
    event, rejection = monitor.normalize_provider_event(
        {
            "event_at": NOW,
            "provider_event_id": "provider-42",
            "source_url": "https://example.test/events/42",
            "upstream_provider": "direct_rpc",
            "event_type": "pair_created",
        },
        source="bsc_onchain",
        chain="BSC",
        contract_address=TOKEN.upper(),
        observed_at=NOW,
    )

    assert rejection is None
    assert event["contract_address"] == TOKEN
    assert event == {
        "schema_version": 1,
        "event_id": event["event_id"],
        "chain": "bsc",
        "contract_address": TOKEN,
        "event_type": "pair_created",
        "event_at": NOW,
        "observed_at": NOW,
        "provider_family": "onchain",
        "provider_feed": "bsc_onchain",
        "evidence_role": "discovery",
        "signal_lane": "new_launch",
        "provider_event_id": "provider-42",
        "source_url": "https://example.test/events/42",
        "raw_fingerprint": event["raw_fingerprint"],
        "upstream_provider": "direct_rpc",
        "event_time_basis": "event_at",
        "counts_for_resonance": True,
    }
    assert len(event["event_id"]) == 64
    assert len(event["raw_fingerprint"]) == 64


def test_duplicate_identity_material_produces_the_same_stable_event_id():
    raw = {"timestamp": 1789041600, "id": "same-event", "amount": 12}

    first, _ = monitor.normalize_provider_event(
        raw, source="gmgn_trending", chain="bsc", contract_address=TOKEN, observed_at=NOW
    )
    second, _ = monitor.normalize_provider_event(
        dict(reversed(list(raw.items()))),
        source="gmgn_trending",
        chain="bsc",
        contract_address=TOKEN.upper(),
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert first["event_id"] == second["event_id"]
    assert first["raw_fingerprint"] == second["raw_fingerprint"]


def test_provider_event_identity_is_stable_across_feed_payload_and_time_changes():
    first, _ = monitor.normalize_provider_event(
        {"event_at": NOW, "provider_event_id": "event-7", "rank": 1},
        source="gmgn_trending",
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )
    second, _ = monitor.normalize_provider_event(
        {
            "event_at": "2026-09-10T12:01:00+00:00",
            "provider_event_id": "event-7",
            "rank": 19,
            "volume": 25000,
        },
        source="gmgn_skills_trending",
        chain="bsc",
        contract_address=TOKEN,
        observed_at="2026-09-10T12:02:00+00:00",
    )

    assert first["provider_family"] == second["provider_family"] == "gmgn"
    assert first["provider_feed"] != second["provider_feed"]
    assert first["raw_fingerprint"] != second["raw_fingerprint"]
    assert first["event_id"] == second["event_id"]


def test_fallback_event_identity_ignores_poll_rank_and_payload_changes():
    first, _ = monitor.normalize_provider_event(
        {"event_at": NOW, "rank": 2, "volume": 100},
        source="gmgn_trending",
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )
    second, _ = monitor.normalize_provider_event(
        {"event_at": NOW, "rank": 8, "volume": 900},
        source="gmgn_trending",
        chain="bsc",
        contract_address=TOKEN,
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert first["raw_fingerprint"] != second["raw_fingerprint"]
    assert first["provider_event_id"] == second["provider_event_id"]
    assert first["event_id"] == second["event_id"]


def test_snapshot_observation_fallback_identity_is_stable_across_polls():
    first, _ = monitor.normalize_provider_event(
        {
            "observed_at": NOW,
            "event_time_basis": "snapshot_observed_at",
            "rank": 2,
        },
        source="noxa_launchpad",
        chain="robinhood",
        contract_address=TOKEN,
        observed_at=NOW,
    )
    second_at = "2026-09-10T12:01:00+00:00"
    second, _ = monitor.normalize_provider_event(
        {
            "observed_at": second_at,
            "event_time_basis": "snapshot_observed_at",
            "rank": 8,
        },
        source="noxa_launchpad",
        chain="robinhood",
        contract_address=TOKEN,
        observed_at=second_at,
    )

    assert first["provider_event_id"] == second["provider_event_id"]
    assert first["event_id"] == second["event_id"]


def test_telegram_post_id_is_used_as_provider_event_identity():
    event, rejection = monitor.normalize_provider_event(
        {
            "observed_at": NOW,
            "telegram_post_id": "memmememjk/52468",
        },
        source="noxa_launchpad",
        chain="robinhood",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["provider_event_id"] == "memmememjk/52468"


@pytest.mark.parametrize(
    ("source", "chain", "address", "raw", "reason"),
    [
        ("gmgn_trending", "bsc", "0x1234", {"event_at": NOW}, "invalid_address"),
        ("gmgn_trending", "bsc", "0x" + "g" * 40, {"event_at": NOW}, "invalid_address"),
        ("gmgn_trending", "solana", TOKEN, {"event_at": NOW}, "wrong_chain"),
        ("gmgn_trending", "bsc", TOKEN, {"chain": "robinhood", "event_at": NOW}, "wrong_chain"),
        ("gmgn_trending", "bsc", TOKEN, {}, "missing_timestamp"),
        ("DS", "bsc", TOKEN, {"event_at": NOW}, "display_only_source"),
        ("Alpha_AI", "bsc", TOKEN, {"event_at": NOW}, "display_only_source"),
        ("mystery_feed", "bsc", TOKEN, {"event_at": NOW}, "unsupported_source"),
        ("social_pair_created_alert", "bsc", TOKEN, {"event_at": NOW}, "unsupported_source"),
        ("robinhood_factory_pair_created", "bsc", TOKEN, {"event_at": NOW}, "wrong_chain"),
        ("bsc_onchain", "robinhood", TOKEN, {"event_at": NOW}, "wrong_chain"),
        ("fourmeme_launchpad", "robinhood", TOKEN, {"event_at": NOW}, "wrong_chain"),
    ],
)
def test_invalid_provider_events_return_enum_rejections(source, chain, address, raw, reason):
    event, rejection = monitor.normalize_provider_event(
        raw,
        source=source,
        chain=chain,
        contract_address=address,
        observed_at=NOW,
    )

    assert event is None
    assert rejection["reason"] == reason
    assert monitor.RejectionReason(reason).value == reason


def test_robinhood_requires_the_same_evm_address_shape_as_bsc():
    event, rejection = monitor.normalize_provider_event(
        {"event_at": NOW},
        source="robinhood_factory_pair_created",
        chain="robinhood",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["chain"] == "robinhood"
    assert event["contract_address"] == TOKEN


def test_event_upstream_provider_is_canonicalized():
    event, rejection = monitor.normalize_provider_event(
        {"event_at": NOW, "provider_event_id": "audit-1", "upstream_provider": " GoPlus Labs "},
        source="gmgn_audit",
        chain="bsc",
        contract_address=TOKEN,
        observed_at=NOW,
    )

    assert rejection is None
    assert event["upstream_provider"] == "goplus"


def test_audit_facts_preserve_unknowns_aliases_confidence_and_upstream():
    facts = monitor.normalize_audit_facts(
        {
            "is_honeypot": None,
            "open_source": True,
            "owner_renounced": False,
            "burn_status": "locked",
            "buy_tax": 0.01,
            "sell_tax": 0.02,
            "top_10_holder_rate": 0.18,
            "dev_team_hold_rate": 0.04,
            "suspected_insider_hold_rate": 0.03,
            "top70_sniper_hold_rate": 0.02,
            "bundler_trader_amount_rate": 0.01,
            "fresh_wallet_rate": 0.25,
            "is_mintable": False,
            "freeze_authority": None,
            "is_blacklisted": False,
            "transfer_pausable": False,
            "is_proxy": True,
            "is_wash_trading": False,
            "developer_rug_history": {"launches": 3, "rugs": 1},
            "upstream_provider": "goplus",
        },
        provider_family="okx",
        observed_at=NOW,
        identity_key=f"bsc:{TOKEN}",
    )
    by_field = {fact["audit_field"]: fact for fact in facts}

    assert set(by_field) == {
        "honeypot",
        "open_source",
        "ownership",
        "lp_burn_lock",
        "buy_tax",
        "sell_tax",
        "top10",
        "developer",
        "insider",
        "sniper",
        "bundler",
        "fresh_wallet",
        "mint",
        "freeze",
        "blacklist",
        "pause",
        "proxy",
        "wash",
        "deployer_history",
    }
    assert by_field["honeypot"]["value"] is None
    assert by_field["honeypot"]["status"] == "unknown"
    assert by_field["freeze"]["value"] is None
    assert by_field["freeze"]["status"] == "unknown"
    assert by_field["open_source"]["status"] == "known"
    assert all(fact["confidence"] == "provider_reported" for fact in facts)
    assert all(fact["upstream_provider"] == "goplus" for fact in facts)
    assert all(len(fact["evidence_id"]) == 64 for fact in facts)


def test_audit_evidence_ids_are_stable_but_distinguish_upstream_provenance():
    raw = {"is_honeypot": False, "upstream_provider": "goplus"}
    kwargs = {
        "provider_family": "gmgn",
        "observed_at": NOW,
        "identity_key": f"bsc:{TOKEN}",
    }

    first = monitor.normalize_audit_facts(raw, **kwargs)
    duplicate = monitor.normalize_audit_facts(
        {"is_honeypot": False, "upstream_provider": " GoPlus "}, **kwargs
    )
    alias = monitor.normalize_audit_facts(
        {"is_honeypot": False, "upstream_provider": "goplus-labs"}, **kwargs
    )
    other_upstream = monitor.normalize_audit_facts(
        {**raw, "upstream_provider": "provider_native"}, **kwargs
    )

    assert first[0]["evidence_id"] == duplicate[0]["evidence_id"]
    assert first[0]["evidence_id"] == alias[0]["evidence_id"]
    assert duplicate[0]["upstream_provider"] == alias[0]["upstream_provider"] == "goplus"
    assert first[0]["evidence_id"] != other_upstream[0]["evidence_id"]


def projection_event(
    event_id="event-1",
    *,
    token=TOKEN,
    chain="bsc",
    family="gmgn",
    feed="gmgn_trenches",
    role="discovery",
    lane="new_launch",
    event_at=NOW,
    observed_at=NOW,
    **extra,
):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "chain": chain,
        "contract_address": token,
        "event_type": lane,
        "event_at": event_at,
        "observed_at": observed_at,
        "provider_family": family,
        "provider_feed": feed,
        "evidence_role": role,
        "signal_lane": lane,
        "provider_event_id": event_id,
        "source_url": None,
        "raw_fingerprint": f"raw-{event_id}",
        "upstream_provider": family,
        "event_time_basis": "event_at",
        "counts_for_resonance": role in {"discovery", "ranking", "wallet", "market"},
        **extra,
    }


def market_snapshot(
    observed_at,
    *,
    token=TOKEN,
    chain="bsc",
    buyers=None,
    net_flow=None,
    trades=None,
    volume=None,
    **extra,
):
    return {
        "chain": chain,
        "contract_address": token,
        "observed_at": observed_at,
        "unique_buyers_1m": buyers,
        "net_buy_flow_1m_usd": net_flow,
        "trades_1m": trades,
        "volume_1m_usd": volume,
        **extra,
    }


def token_from(snapshot, token=TOKEN, chain="bsc"):
    return next(item for item in snapshot["tokens"] if item["id"] == f"{chain}:{token}")


def project(previous=None, *, events=(), candidates=(), health=None, observed_at=NOW, rejections=()):
    return monitor.update_monitor_state(
        previous,
        events=list(events),
        candidates=list(candidates),
        rejections=list(rejections),
        source_health=health or {"gmgn": {"status": "ok"}},
        observed_at=observed_at,
    )


def arc_event(event_id, *, family="onchain", feed="arc_rpc", role="discovery", lane="new_launch", at=NOW):
    return projection_event(
        event_id,
        chain="arc",
        family=family,
        feed=feed,
        role=role,
        lane=lane,
        event_at=at,
        observed_at=at,
    )


def arc_market(observed_at=NOW, **overrides):
    values = {
        "market_cap_usd": 40_000,
        "liquidity_usd": 12_000,
        "holders": 40,
        **overrides,
    }
    return market_snapshot(
        observed_at,
        chain="arc",
        **values,
    )


def test_arc_uses_explicit_bsc_speed_timing_and_confirmation_thresholds():
    assert monitor.OBSERVATION_FRESHNESS_SECONDS["arc"] == 120
    assert monitor.MARKET_FRESHNESS_SECONDS["arc"] == 120
    assert monitor.RESONANCE_FRESHNESS_SECONDS["arc"] == 900
    assert monitor.WALLET_FRESHNESS_SECONDS["arc"] == 900
    assert monitor.CONFIRMATION_HOLD_SECONDS["arc"] == 60
    assert monitor.CONFIRMATION_MIN_MARKET_CAP_USD["arc"] == 10_000
    assert monitor.CONFIRMATION_MAX_INITIAL_MARKET_CAP_USD["arc"] == 500_000
    assert monitor.CONFIRMATION_MIN_LIQUIDITY_USD["arc"] == 8_000
    assert monitor.CONFIRMATION_MIN_HOLDERS["arc"] == 20


def test_arc_fresh_technical_discovery_stays_discovery_and_does_not_alert():
    snapshot = project(events=[arc_event("arc-rpc-discovery")])
    token = token_from(snapshot, chain="arc")

    assert token["primary_state"] == "new"
    assert token["active_states"] == ["new"]
    assert token["freshness"]["status"] == "fresh"
    assert token["resonance"]["family_count"] == 1
    assert snapshot["alerts"] == []


def test_arc_onchain_plus_independent_ranking_and_current_market_reaches_early_bird():
    snapshot = project(
        events=[
            arc_event("arc-rpc-early"),
            arc_event(
                "arc-985-early",
                family="985",
                feed="985_monitor",
                role="ranking",
                lane="trending",
            ),
        ],
        candidates=[arc_market()],
    )
    token = token_from(snapshot, chain="arc")

    assert token["resonance"]["provider_families"] == ["985", "onchain"]
    assert token["resonance"]["family_count"] == 2
    assert "building" in token["active_states"]
    assert "resonating" not in token["active_states"]
    assert snapshot["alerts"][0]["policy"] == {"chain": "arc", "stage": "early_focus"}


def test_one_family_accelerating_arc_token_stays_in_discovery():
    initial = project(
        events=[arc_event("arc-one-family-flow")],
        candidates=[arc_market(buyers=2, net_flow=20, trades=4, volume=100)],
    )
    observed_at = "2026-09-10T12:00:30+00:00"
    accelerated = project(
        initial,
        candidates=[arc_market(observed_at, buyers=9, net_flow=300, trades=18, volume=800)],
        observed_at=observed_at,
    )
    token = token_from(accelerated, chain="arc")

    assert token["ranking_axes"]["flow"]["direction"] == "accelerating"
    assert token["resonance"]["family_count"] == 1
    assert token["primary_state"] == "new"
    assert "building" not in token["active_states"]
    assert accelerated["alerts"] == []


@pytest.mark.parametrize("market_case", ("insufficient_thresholds", "unknown_holders"))
def test_accelerating_arc_flow_cannot_bypass_market_quality_gate(market_case):
    initial_market = arc_market(buyers=2, net_flow=20, trades=4, volume=100)
    next_at = "2026-09-10T12:00:30+00:00"
    next_market = arc_market(next_at, buyers=9, net_flow=300, trades=18, volume=800)
    if market_case == "insufficient_thresholds":
        initial_market.update({"market_cap_usd": 5_000, "liquidity_usd": 4_000})
        next_market.update({"market_cap_usd": 6_000, "liquidity_usd": 5_000})
        expected_failures = {"market_cap_too_low", "liquidity_too_low"}
    else:
        initial_market.pop("holders")
        next_market.pop("holders")
        expected_failures = {"holders_unknown"}

    initial = project(
        events=[
            arc_event("arc-gated-flow-rpc"),
            arc_event(
                "arc-gated-flow-wind",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
        ],
        candidates=[initial_market],
    )
    accelerated = project(
        initial,
        candidates=[next_market],
        observed_at=next_at,
    )
    token = token_from(accelerated, chain="arc")

    assert token["ranking_axes"]["flow"]["direction"] == "accelerating"
    assert expected_failures.issubset(set(token["resonance"]["confirmation_gate"]["failures"]))
    assert token["primary_state"] == "new"
    assert "building" not in token["active_states"]
    assert accelerated["alerts"] == []


def test_arc_three_independent_families_and_quality_market_reach_confirmation():
    snapshot = project(
        events=[
            arc_event("arc-rpc-confirm"),
            arc_event(
                "arc-wind-confirm",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
            arc_event(
                "arc-proficy-confirm",
                family="proficy",
                feed="proficy_trending",
                role="ranking",
                lane="trending",
            ),
        ],
        candidates=[arc_market(market_cap_usd=500_000, liquidity_usd=8_000, holders=20)],
    )
    token = token_from(snapshot, chain="arc")

    assert token["resonance"]["family_count"] == 3
    assert token["resonance"]["confirmation_gate"]["eligible"] is True
    assert "resonating" in token["active_states"]
    assert snapshot["alerts"][0]["policy"] == {"chain": "arc", "stage": "high_priority"}


def test_arc_alert_policy_excludes_discovery_but_accepts_selected_aggregate_stages():
    discovery = project(events=[arc_event("arc-policy-discovery")])
    early = project(
        events=[
            arc_event("arc-policy-early-rpc"),
            arc_event(
                "arc-policy-early-wind",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
        ],
        candidates=[arc_market()],
    )
    confirmation = project(
        events=[
            arc_event("arc-policy-confirm-rpc"),
            arc_event(
                "arc-policy-confirm-wind",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
            arc_event(
                "arc-policy-confirm-proficy",
                family="proficy",
                feed="proficy_trending",
                role="ranking",
                lane="trending",
            ),
        ],
        candidates=[arc_market()],
    )

    assert monitor._alert_policy(token_from(discovery, chain="arc")) is None
    assert monitor._alert_policy(token_from(early, chain="arc"))["stage"] == "early_focus"
    assert monitor._alert_policy(token_from(confirmation, chain="arc"))["stage"] == "high_priority"


def test_arc_non_aggregate_wallet_and_failed_gate_states_emit_no_selected_alert():
    verified_wallet_events = [
        arc_event(
            f"arc-verified-wallet-{index}",
            family="985",
            feed="985_smartmoney",
            role="wallet",
            lane="smart_money",
        )
        | {
            "wallet_address": f"0x{index:040x}",
            "direction": "buy",
            "wallet_verified": True,
        }
        for index in (1, 2)
    ]
    smart_cluster = project(
        events=[arc_event("arc-smart-cluster-rpc"), *verified_wallet_events],
        candidates=[arc_market()],
    )

    candidate_wallet_events = [
        arc_event(
            f"arc-candidate-wallet-{index}",
            family="985",
            feed="985_smartmoney",
            role="wallet",
            lane="smart_money",
        )
        | {
            "wallet_address": f"0x{index + 10:040x}",
            "direction": "buy",
            "wallet_verified": False,
        }
        for index in (1, 2, 3)
    ]
    wallet_only = project(
        events=[arc_event("arc-wallet-only-rpc"), *candidate_wallet_events],
        candidates=[arc_market()],
    )

    qualified = project(
        events=[
            arc_event("arc-failed-gate-rpc"),
            arc_event(
                "arc-failed-gate-wind",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
        ],
        candidates=[arc_market()],
    )
    retained_failed_gate = json.loads(json.dumps(token_from(qualified, chain="arc")))
    retained_failed_gate["primary_state"] = "building"
    retained_failed_gate["active_states"] = ["new", "building"]
    retained_failed_gate["resonance"]["confirmation_gate"] = {
        **retained_failed_gate["resonance"]["confirmation_gate"],
        "eligible": False,
        "failures": ["holders_unknown"],
    }

    smart_token = token_from(smart_cluster, chain="arc")
    wallet_token = token_from(wallet_only, chain="arc")
    assert "smart_cluster" in smart_token["active_states"]
    assert "building" not in smart_token["active_states"]
    assert smart_cluster["alerts"] == []
    assert wallet_token["wallet_evidence"]["candidate_buyers"] == 3
    assert wallet_only["alerts"] == []
    assert monitor._alert_policy(retained_failed_gate) is None


@pytest.mark.parametrize("chain", ("bsc", "robinhood"))
def test_non_arc_chains_keep_flow_acceleration_building_behavior(chain):
    initial = project(
        events=[projection_event(f"{chain}-flow", chain=chain)],
        candidates=[
            market_snapshot(
                NOW,
                chain=chain,
                buyers=2,
                net_flow=20,
                trades=4,
                volume=100,
            )
        ],
    )
    observed_at = "2026-09-10T12:01:00+00:00"
    accelerated = project(
        initial,
        candidates=[
            market_snapshot(
                observed_at,
                chain=chain,
                buyers=9,
                net_flow=300,
                trades=18,
                volume=800,
            )
        ],
        observed_at=observed_at,
    )

    assert token_from(accelerated, chain=chain)["primary_state"] == "building"


def test_arc_unknown_holders_stay_unknown_and_fail_the_known_holder_gate():
    market = arc_market()
    market.pop("holders")
    snapshot = project(
        events=[
            arc_event("arc-rpc-no-holders"),
            arc_event(
                "arc-wind-no-holders",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
            arc_event(
                "arc-proficy-no-holders",
                family="proficy",
                feed="proficy_trending",
                role="ranking",
                lane="trending",
            ),
        ],
        candidates=[market],
    )
    token = token_from(snapshot, chain="arc")

    assert token["market"]["holders"] is None
    assert token["ranking_axes"]["quality"]["holders"] is None
    assert token["resonance"]["confirmation_gate"]["eligible"] is False
    assert "holders_unknown" in token["resonance"]["confirmation_gate"]["failures"]
    assert "building" not in token["active_states"]
    assert "resonating" not in token["active_states"]


@pytest.mark.parametrize(
    ("events_at", "market_at", "observed_at", "expected_failure"),
    [
        (NOW, "2026-09-10T11:57:59+00:00", NOW, "stale_market"),
        ("2026-09-10T11:44:59+00:00", NOW, NOW, "discovery_window_expired"),
    ],
)
def test_arc_stale_market_or_resonance_cannot_promote(events_at, market_at, observed_at, expected_failure):
    snapshot = project(
        events=[
            arc_event("arc-rpc-stale", at=events_at),
            arc_event(
                "arc-wind-stale",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
                at=events_at,
            ),
        ],
        candidates=[arc_market(market_at)],
        observed_at=observed_at,
    )
    token = token_from(snapshot, chain="arc")

    assert expected_failure in token["resonance"]["confirmation_gate"]["failures"]
    assert "building" not in token["active_states"]
    assert "resonating" not in token["active_states"]


def test_arc_confirmation_can_cool_and_a_later_independent_event_can_revive_it():
    confirmed = project(
        events=[
            arc_event("arc-rpc-cycle"),
            arc_event(
                "arc-wind-cycle",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
            arc_event(
                "arc-proficy-cycle",
                family="proficy",
                feed="proficy_trending",
                role="ranking",
                lane="trending",
            ),
        ],
        candidates=[arc_market(buyers=10, net_flow=500, trades=20, volume=1_000)],
    )
    cooled_at = "2026-09-10T12:00:30+00:00"
    cooled = project(
        confirmed,
        candidates=[arc_market(cooled_at, buyers=1, net_flow=-50, trades=2, volume=100)],
        observed_at=cooled_at,
    )
    revived_at = "2026-09-10T12:01:00+00:00"
    revived = project(
        cooled,
        events=[
            arc_event(
                "arc-gmgn-revival",
                family="gmgn",
                feed="gmgn_skills_hot_searches",
                role="ranking",
                lane="hot_search",
                at=revived_at,
            )
        ],
        candidates=[arc_market(revived_at, buyers=16, net_flow=900, trades=32, volume=2_000)],
        observed_at=revived_at,
    )

    assert token_from(confirmed, chain="arc")["primary_state"] == "resonating"
    assert token_from(cooled, chain="arc")["primary_state"] == "cooling"
    assert token_from(revived, chain="arc")["primary_state"] == "revival"
    assert revived["alerts"][-1]["policy"] == {"chain": "arc", "stage": "revival"}


def test_arc_rpc_and_arcscan_remain_one_onchain_family_without_resonance():
    snapshot = project(
        events=[
            arc_event("arc-rpc-one"),
            arc_event("arc-scan-one", feed="arc_arcscan"),
        ]
    )
    token = token_from(snapshot, chain="arc")

    assert token["resonance"]["provider_families"] == ["onchain"]
    assert token["resonance"]["family_count"] == 1
    assert token["resonance"]["subtype"] is None
    assert token["active_states"] == ["new"]


def test_arc_985_wind_and_proficy_count_as_three_independent_families():
    snapshot = project(
        events=[
            arc_event("arc-985-family", family="985", feed="985_monitor", role="ranking", lane="trending"),
            arc_event("arc-wind-family", family="wind", feed="wind_monitor", role="ranking", lane="kol_social"),
            arc_event("arc-proficy-family", family="proficy", feed="proficy_trending", role="ranking", lane="trending"),
        ]
    )
    resonance = token_from(snapshot, chain="arc")["resonance"]

    assert resonance["provider_families"] == ["985", "proficy", "wind"]
    assert resonance["family_count"] == 3


def test_arc_duplicate_events_from_one_family_do_not_inflate_family_count():
    snapshot = project(
        events=[
            arc_event("arc-985-duplicate-1", family="985", feed="985_monitor", role="ranking", lane="trending"),
            arc_event("arc-985-duplicate-2", family="985", feed="985_monitor", role="ranking", lane="trending"),
            arc_event("arc-985-duplicate-3", family="985", feed="985_fomo_wallets", role="ranking", lane="trending"),
        ]
    )
    resonance = token_from(snapshot, chain="arc")["resonance"]

    assert len(resonance["evidence_ids"]) == 3
    assert resonance["provider_families"] == ["985"]
    assert resonance["family_count"] == 1


def test_market_behavior_demotes_thin_holder_resonance_to_trend_watch():
    first = project(
        events=[
            projection_event(chain="robinhood"),
            projection_event(
                "okx-thin",
                chain="robinhood",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=250_000,
            holders=3,
        )],
    )
    observed_at = "2026-09-10T12:01:00+00:00"
    result = project(
        first,
        candidates=[market_snapshot(
            observed_at,
            chain="robinhood",
            market_cap_usd=70_000,
            liquidity_usd=18_000,
            volume_24h_usd=500_000,
            holders=3,
        )],
        observed_at=observed_at,
    )

    token = token_from(result, chain="robinhood")
    assert token["ranking_axes"]["market_behavior"]["disposition"] == "observe"
    assert "thin_holder_base" in token["ranking_axes"]["market_behavior"]["flags"]
    assert token["primary_state"] == "trend_watch"
    assert "resonating" not in token["active_states"]
    assert result["alerts"][-1]["label"] == "趋势观察"
    assert result["alerts"][-1]["policy"]["stage"] == "market_behavior"
    assert result["kill_counts"]["by_reason"]["thin_holder_base"] == 1


def test_market_behavior_demotes_volume_without_buyer_participation():
    first = project(
        events=[
            projection_event(),
            projection_event(
                "okx-wash",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_5m_usd=40_000,
            unique_buyers_5m=2,
            holders=80,
        )],
    )
    observed_at = "2026-09-10T12:01:00+00:00"
    result = project(
        first,
        candidates=[market_snapshot(
            observed_at,
            market_cap_usd=70_000,
            liquidity_usd=18_000,
            volume_5m_usd=90_000,
            unique_buyers_5m=2,
            holders=81,
        )],
        observed_at=observed_at,
    )

    token = token_from(result)
    behavior = token["ranking_axes"]["market_behavior"]
    assert "volume_without_buyer_growth" in behavior["flags"]
    assert token["primary_state"] == "trend_watch"
    assert "resonating" not in token["active_states"]


def test_clean_market_behavior_keeps_cross_provider_confirmation():
    first = project(
        events=[
            projection_event(),
            projection_event(
                "okx-clean",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            buyers=20,
            net_flow=2_000,
            trades=40,
            volume=20_000,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_5m_usd=50_000,
            unique_buyers_5m=20,
            buys_5m=30,
            sells_5m=10,
            holders=80,
        )],
    )
    observed_at = "2026-09-10T12:01:00+00:00"
    result = project(
        first,
        candidates=[market_snapshot(
            observed_at,
            buyers=35,
            net_flow=5_000,
            trades=70,
            volume=50_000,
            market_cap_usd=70_000,
            liquidity_usd=18_000,
            volume_5m_usd=90_000,
            unique_buyers_5m=35,
            buys_5m=60,
            sells_5m=20,
            holders=120,
        )],
        observed_at=observed_at,
    )

    token = token_from(result)
    assert token["ranking_axes"]["market_behavior"]["disposition"] == "pass"
    assert token["ranking_axes"]["market_behavior"]["flags"] == []
    assert "resonating" in token["active_states"]


def test_robinhood_confirmation_survives_a_short_source_gap_but_not_a_long_one():
    discovered = project(
        events=[
            projection_event(chain="robinhood", family="noxa", feed="noxa_launchpad"),
            projection_event(
                "gmgn-hot",
                chain="robinhood",
                family="gmgn",
                feed="gmgn_hot_search",
                role="ranking",
                lane="hot_search",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            market_cap_usd=40_000,
            liquidity_usd=18_000,
            volume_24h_usd=80_000,
            holders=120,
        )],
    )
    confirmed_at = "2026-09-10T12:00:10+00:00"
    confirmed = project(
        discovered,
        candidates=[market_snapshot(
            confirmed_at,
            chain="robinhood",
            market_cap_usd=56_000,
            liquidity_usd=20_000,
            volume_24h_usd=95_000,
            holders=135,
        )],
        observed_at=confirmed_at,
    )
    confirmed_token = token_from(confirmed, chain="robinhood")
    assert confirmed_token["primary_state"] == "resonating"

    short_at = "2026-09-10T12:00:30+00:00"
    short_source = project(
        events=[projection_event(
            "noxa-refresh",
            chain="robinhood",
            family="noxa",
            feed="noxa_launchpad",
            event_at=short_at,
            observed_at=short_at,
        )],
        candidates=[market_snapshot(
            short_at,
            chain="robinhood",
            market_cap_usd=42_000,
            liquidity_usd=18_500,
            volume_24h_usd=82_000,
            holders=125,
        )],
        observed_at=short_at,
    )
    held = json.loads(json.dumps(confirmed_token))
    held["events"] = token_from(short_source, chain="robinhood")["events"]
    held["market"] = token_from(short_source, chain="robinhood")["market"]
    monitor._derive_token(held, short_at)

    assert held["primary_state"] == "resonating"
    assert held["confirmation_last_seen_at"] == confirmed_at

    expired_at = "2026-09-10T12:01:41+00:00"
    expired_source = project(
        events=[projection_event(
            "noxa-late",
            chain="robinhood",
            family="noxa",
            feed="noxa_launchpad",
            event_at=expired_at,
            observed_at=expired_at,
        )],
        candidates=[market_snapshot(
            expired_at,
            chain="robinhood",
            market_cap_usd=43_000,
            liquidity_usd=18_500,
            volume_24h_usd=83_000,
            holders=126,
        )],
        observed_at=expired_at,
    )
    held["events"] = token_from(expired_source, chain="robinhood")["events"]
    held["market"] = token_from(expired_source, chain="robinhood")["market"]
    monitor._derive_token(held, expired_at)

    assert held["primary_state"] != "resonating"


def test_confirmation_hold_never_overrides_a_new_hard_risk():
    discovered = project(
        events=[
            projection_event(chain="robinhood", family="noxa", feed="noxa_launchpad"),
            projection_event(
                "gmgn-hot-risk",
                chain="robinhood",
                family="gmgn",
                feed="gmgn_hot_search",
                role="ranking",
                lane="hot_search",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            market_cap_usd=40_000,
            liquidity_usd=18_000,
            volume_24h_usd=80_000,
            holders=120,
        )],
    )
    confirmed_at = "2026-09-10T12:00:10+00:00"
    confirmed = project(
        discovered,
        candidates=[market_snapshot(
            confirmed_at,
            chain="robinhood",
            market_cap_usd=56_000,
            liquidity_usd=20_000,
            volume_24h_usd=95_000,
            holders=135,
        )],
        observed_at=confirmed_at,
    )
    assert "resonating" in token_from(confirmed, chain="robinhood")["active_states"]
    blocked_at = "2026-09-10T12:00:30+00:00"
    blocked = project(
        confirmed,
        candidates=[market_snapshot(
            blocked_at,
            chain="robinhood",
            market_cap_usd=42_000,
            liquidity_usd=18_500,
            volume_24h_usd=82_000,
            holders=125,
            hard_risk=True,
            hard_risk_flags=["confirmed_honeypot"],
        )],
        observed_at=blocked_at,
    )

    token = token_from(blocked, chain="robinhood")
    assert token["risk"]["hard_blocked"] is True
    assert "resonating" not in token["active_states"]


def test_projection_keeps_one_source_discovery_visible_with_unknown_market_values():
    snapshot = project(events=[projection_event()])
    token = token_from(snapshot)

    assert snapshot["schema_version"] == 3
    assert token["identity"]["contract_address"] == TOKEN
    assert token["identity"]["symbol"] is None
    assert token["primary_state"] == "new"
    assert token["market"]["liquidity_usd"] is None
    assert token["market"]["field_status"]["liquidity_usd"] == "unknown"
    assert token["ranking_axes"]["quality"]["status"] == "unknown"
    assert "liquidity_usd" in token["missing_evidence"]


def test_placeholder_zero_price_market_cap_and_holders_remain_unknown():
    snapshot = project(
        events=[projection_event()],
        candidates=[market_snapshot(
            NOW,
            price_usd=0,
            market_cap_usd=0,
            liquidity_usd=0,
            volume_24h_usd=0,
            holders=0,
        )],
    )
    market = token_from(snapshot)["market"]

    assert market["price_usd"] is None
    assert market["market_cap_usd"] is None
    assert market["holders"] is None
    assert market["field_status"]["price_usd"] == "unknown"
    assert market["field_status"]["market_cap_usd"] == "unknown"
    assert market["field_status"]["holders"] == "unknown"
    assert market["liquidity_usd"] == 0
    assert market["volume_24h_usd"] == 0


def test_event_observation_and_report_refresh_times_remain_separate():
    event = projection_event(
        event_at="2026-09-10T11:50:00+00:00",
        observed_at="2026-09-10T11:59:00+00:00",
    )
    snapshot = project(events=[event], observed_at=NOW)
    token = token_from(snapshot)

    assert token["events"][0]["event_at"] == "2026-09-10T11:50:00+00:00"
    assert token["events"][0]["observed_at"] == "2026-09-10T11:59:00+00:00"
    assert token["identity"]["first_seen_at"] == "2026-09-10T11:59:00+00:00"
    assert token["freshness"]["last_observed_at"] == "2026-09-10T11:59:00+00:00"
    assert token["freshness"]["report_refreshed_at"] == NOW


def test_empty_degraded_poll_retains_token_and_marks_stale_without_demotion():
    first = project(events=[projection_event()])
    retained = project(
        first,
        health={"gmgn": {"status": "error", "error_category": "timeout"}},
        observed_at="2026-09-10T12:10:00+00:00",
    )
    token = token_from(retained)

    assert len(token["events"]) == 1
    assert token["primary_state"] == "new"
    assert "stale" in token["active_states"]
    assert token["freshness"]["status"] == "stale"
    assert retained["monitor_status"] == "degraded"


def test_duplicate_event_is_suppressed_without_state_transition():
    event = projection_event()
    first = project(events=[event])
    duplicate = project(first, events=[dict(event)], observed_at="2026-09-10T12:01:00+00:00")

    token = token_from(duplicate)
    assert [item["event_id"] for item in token["events"]] == ["event-1"]
    assert token["state_version"] == token_from(first)["state_version"]
    assert token["state_updated_at"] == token_from(first)["state_updated_at"]


def test_source_failure_isolated_from_token_with_fresh_independent_snapshot():
    other = "0x1111111111111111111111111111111111111111"
    first = project(
        events=[projection_event(), projection_event("other-1", token=other, family="okx", feed="okx_trenches")]
    )
    next_snapshot = project(
        first,
        candidates=[market_snapshot("2026-09-10T12:05:00+00:00", buyers=3)],
        health={"gmgn": {"status": "error"}, "okx": {"status": "ok"}},
        observed_at="2026-09-10T12:05:00+00:00",
    )

    assert token_from(next_snapshot)["freshness"]["status"] == "fresh"
    assert token_from(next_snapshot, other)["freshness"]["status"] == "stale"
    assert next_snapshot["monitor_status"] == "degraded"


def test_hard_risk_token_remains_visible_and_rejections_stay_separate():
    rejection = {"reason": "invalid_address", "raw_fingerprint": "rejected-1"}
    snapshot = project(
        events=[projection_event()],
        candidates=[market_snapshot(NOW, hard_risk=True, hard_risk_flags=["confirmed_honeypot"])],
        rejections=[rejection],
    )
    token = token_from(snapshot)

    assert token["primary_state"] == "blocked_risk"
    assert "blocked_risk" in token["active_states"]
    assert token["risk"]["hard_blocked"] is True
    assert token["risk"]["hard_failures"] == ["confirmed_honeypot"]
    assert snapshot["rejections"] == [rejection]
    assert len(snapshot["tokens"]) == 1


def test_ordered_market_snapshots_create_building_then_cooling_with_state_history():
    first = project(
        events=[projection_event()],
        candidates=[market_snapshot(NOW, buyers=2, net_flow=50, trades=4, volume=100)],
    )
    building = project(
        first,
        candidates=[market_snapshot("2026-09-10T12:01:00+00:00", buyers=7, net_flow=180, trades=12, volume=400)],
        observed_at="2026-09-10T12:01:00+00:00",
    )
    cooling = project(
        building,
        candidates=[market_snapshot("2026-09-10T12:02:00+00:00", buyers=1, net_flow=-20, trades=2, volume=40)],
        observed_at="2026-09-10T12:02:00+00:00",
    )

    assert token_from(building)["primary_state"] == "building"
    assert token_from(building)["ranking_axes"]["flow"]["direction"] == "accelerating"
    assert token_from(cooling)["primary_state"] == "cooling"
    assert "building" not in token_from(cooling)["active_states"]
    assert "building" in {item["state"] for item in token_from(cooling)["state_history"]}
    assert token_from(cooling)["ranking_axes"]["flow"]["direction"] == "decaying"


def test_same_provider_different_lanes_is_platform_stack_not_cross_provider():
    snapshot = project(
        events=[
            projection_event(),
            projection_event("trend-1", feed="gmgn_trending", role="ranking", lane="trending"),
        ]
    )
    token = token_from(snapshot)

    assert token["resonance"]["subtype"] == "platform_stack"
    assert token["resonance"]["provider_families"] == ["gmgn"]
    assert token["primary_state"] == "new"
    assert token["resonance"]["confirmation_gate"]["eligible"] is False


def test_gmgn_kol_and_hot_search_create_platform_stack_without_wallet_claims():
    events = []
    for event_id, source in (("kol-1", "gmgn_skills_kol"), ("hot-1", "gmgn_skills_hot_searches")):
        event, rejection = monitor.normalize_provider_event(
            {"event_at": NOW, "provider_event_id": event_id},
            source=source,
            chain="bsc",
            contract_address=TOKEN,
            observed_at=NOW,
        )
        assert rejection is None
        events.append(event)

    token = token_from(project(events=events))

    assert token["resonance"]["subtype"] == "platform_stack"
    assert token["resonance"]["signal_lanes"] == ["hot_search", "kol_social"]
    assert token["wallet_evidence"]["verified_buyers"] == 0
    assert token["primary_state"] == "new"


def test_low_participation_bsc_resonance_stays_unconfirmed():
    snapshot = project(
        events=[
            projection_event(),
            projection_event(
                "okx-1",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.000004,
            market_cap_usd=4_400,
            liquidity_usd=0,
            volume_24h_usd=0,
            holders=2,
        )],
    )
    token = token_from(snapshot)

    assert token["resonance"]["subtype"] == "cross_provider"
    assert "resonating" not in token["active_states"]
    assert token["resonance"]["confirmation_gate"] == {
        "eligible": False,
        "failures": ["market_cap_too_low", "liquidity_too_low", "holders_too_low"],
        "minimum_market_cap_usd": 10_000.0,
        "minimum_liquidity_usd": 8_000.0,
        "minimum_holders": 20,
    }
    assert snapshot["alerts"] == []


def test_quality_eligible_bsc_resonance_starts_as_early_bird():
    snapshot = project(
        events=[
            projection_event(),
            projection_event(
                "okx-1",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.00004,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    token = token_from(snapshot)

    assert token["resonance"]["subtype"] == "cross_provider"
    assert token["resonance"]["confirmation_gate"]["eligible"] is True
    assert "building" in token["active_states"]
    assert "resonating" not in token["active_states"]


def test_gmgn_noxa_attention_pair_stays_in_discovery_without_quality_signal():
    snapshot = project(
        events=[
            projection_event(
                "rh-gmgn-hot",
                chain="robinhood",
                feed="gmgn_skills_hot_searches",
                role="ranking",
                lane="hot_search",
            ),
            projection_event(
                "rh-noxa-launch",
                chain="robinhood",
                family="noxa",
                feed="noxa_launchpad",
                role="discovery",
                lane="new_launch",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    token = token_from(snapshot, chain="robinhood")

    assert token["resonance"]["subtype"] == "cross_provider"
    assert token["resonance"]["confirmation_gate"]["eligible"] is True
    assert token["primary_state"] == "new"
    assert "building" not in token["active_states"]
    assert "resonating" not in token["active_states"]
    assert snapshot["alerts"] == []


def test_gmgn_noxa_pair_promotes_when_wind_quality_signal_arrives():
    snapshot = project(
        events=[
            projection_event(
                "rh-gmgn-hot",
                chain="robinhood",
                feed="gmgn_skills_hot_searches",
                role="ranking",
                lane="hot_search",
            ),
            projection_event(
                "rh-noxa-launch",
                chain="robinhood",
                family="noxa",
                feed="noxa_launchpad",
                role="discovery",
                lane="new_launch",
            ),
            projection_event(
                "rh-wind-kol",
                chain="robinhood",
                family="wind",
                feed="wind_monitor",
                role="ranking",
                lane="kol_social",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    token = token_from(snapshot, chain="robinhood")

    assert token["resonance"]["provider_families"] == ["gmgn", "noxa", "wind"]
    assert "building" in token["active_states"]


def test_deep_discovery_drawdown_demotes_early_bird_to_trend_watch():
    first = project(
        events=[
            projection_event(),
            projection_event(
                "okx-flow",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            market_cap_usd=100_000,
            liquidity_usd=20_000,
            volume_24h_usd=80_000,
            holders=100,
        )],
    )
    observed_at = "2026-09-10T12:01:00+00:00"
    result = project(
        first,
        candidates=[market_snapshot(
            observed_at,
            market_cap_usd=45_000,
            liquidity_usd=12_000,
            volume_24h_usd=90_000,
            holders=105,
        )],
        observed_at=observed_at,
    )
    token = token_from(result)

    behavior = token["ranking_axes"]["market_behavior"]
    assert behavior["disposition"] == "observe"
    assert "deep_discovery_drawdown" in behavior["flags"]
    assert behavior["evidence"]["discovery_drawdown_pct"] == -55.0
    assert token["primary_state"] == "trend_watch"
    assert "building" not in token["active_states"]
    assert "resonating" not in token["active_states"]


def test_cross_provider_with_market_follow_through_becomes_confirmation():
    first = project(
        events=[
            projection_event(),
            projection_event(
                "okx-1",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.00004,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    confirmed = project(
        first,
        candidates=[market_snapshot(
            "2026-09-10T12:01:00+00:00",
            price_usd=0.000072,
            market_cap_usd=72_000,
            liquidity_usd=18_000,
            volume_24h_usd=80_000,
            holders=110,
        )],
        observed_at="2026-09-10T12:01:00+00:00",
    )
    token = token_from(confirmed)

    assert token["resonance"]["confirmation_gate"]["eligible"] is True
    assert "building" in token["active_states"]
    assert "resonating" in token["active_states"]


def test_pair_age_blocks_old_token_when_creation_timestamp_is_missing():
    snapshot = project(
        events=[
            projection_event(
                "gmgn-hot-old",
                chain="robinhood",
                family="gmgn",
                feed="gmgn_skills_hot_searches",
                role="ranking",
                lane="hot_search",
            ),
            projection_event(
                "proficy-old",
                chain="robinhood",
                family="proficy",
                feed="proficy_trending",
                role="ranking",
                lane="trending",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            pair_age_hours=4,
            market_cap_usd=350_000,
            liquidity_usd=250_000,
        )],
    )
    token = token_from(snapshot, chain="robinhood")

    assert token["identity"]["pair_created_at"] == "2026-09-10T08:00:00+00:00"
    assert token["ranking_axes"]["timing"]["pair_age_seconds"] == 14_400.0
    assert token["resonance"]["confirmation_gate"]["eligible"] is False
    assert "discovery_window_expired" in token["resonance"]["confirmation_gate"]["failures"]
    assert snapshot["alerts"] == []


def test_reobserved_old_token_cannot_reenter_aggregate_confirmation():
    first = project(
        events=[projection_event("first-seen")],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.00004,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    later = "2026-09-10T14:00:00+00:00"
    refreshed = project(
        first,
        events=[
            projection_event(
                "gmgn-relisted",
                event_at=later,
                observed_at=later,
            ),
            projection_event(
                "okx-relisted",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
                event_at=later,
                observed_at=later,
            ),
        ],
        candidates=[market_snapshot(
            later,
            price_usd=0.00004,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
        observed_at=later,
    )
    token = token_from(refreshed)

    assert token["resonance"]["subtype"] == "cross_provider"
    assert token["resonance"]["confirmation_gate"]["eligible"] is False
    assert "discovery_window_expired" in token["resonance"]["confirmation_gate"]["failures"]
    assert "resonating" not in token["active_states"]


def test_expired_discovery_with_wallet_or_resonance_alerts_as_trend_watch_not_confirmation():
    first = project(
        events=[projection_event("old-first-seen")],
        candidates=[
            market_snapshot(
                NOW,
                price_usd=0.00004,
                market_cap_usd=40_000,
                liquidity_usd=12_000,
                holders=80,
            )
        ],
    )
    later = "2026-09-10T14:00:00+00:00"
    refreshed = project(
        first,
        events=[
            projection_event(
                "old-gmgn-live",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
                event_at=later,
                observed_at=later,
            ),
            projection_event(
                "old-okx-flow",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
                event_at=later,
                observed_at=later,
            ),
            projection_event(
                "old-wallet-buy",
                family="985",
                feed="985_smartmoney",
                role="wallet",
                lane="smart_money",
                event_at=later,
                observed_at=later,
                wallet_address="0x4444444444444444444444444444444444444444",
                direction="buy",
                wallet_verified=True,
            ),
        ],
        candidates=[
            market_snapshot(
                later,
                price_usd=0.00008,
                market_cap_usd=80_000,
                liquidity_usd=18_000,
                holders=130,
                buyers=9,
                net_flow=800,
                trades=16,
                volume=1_400,
            )
        ],
        observed_at=later,
    )
    token = token_from(refreshed)
    alert = refreshed["alerts"][-1]

    assert "discovery_window_expired" in token["resonance"]["confirmation_gate"]["failures"]
    assert "resonating" not in token["active_states"]
    assert alert["label"] == "趋势观察"
    assert alert["policy"] == {"chain": "bsc", "stage": "late_resonance"}
    assert alert["severity"] == "medium"
    assert "high_priority" not in json.dumps(alert, ensure_ascii=False)


def test_previously_confirmed_token_moves_to_trend_watch_when_discovery_window_expires():
    discovered = project(
        events=[
            projection_event(chain="robinhood", family="noxa", feed="noxa_launchpad"),
            projection_event(
                "gmgn-near-expiry",
                chain="robinhood",
                family="gmgn",
                feed="gmgn_hot_search",
                role="ranking",
                lane="hot_search",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            pair_age_hours=0.49,
            market_cap_usd=40_000,
            liquidity_usd=18_000,
            volume_24h_usd=80_000,
            holders=120,
        )],
    )
    confirmed_at = "2026-09-10T12:00:10+00:00"
    confirmed = project(
        discovered,
        candidates=[market_snapshot(
            confirmed_at,
            chain="robinhood",
            market_cap_usd=56_000,
            liquidity_usd=20_000,
            volume_24h_usd=95_000,
            holders=135,
        )],
        observed_at=confirmed_at,
    )
    assert token_from(confirmed, chain="robinhood")["primary_state"] == "resonating"

    expired_at = "2026-09-10T12:00:50+00:00"
    expired = project(
        confirmed,
        events=[projection_event(
            "okx-after-expiry",
            chain="robinhood",
            family="okx",
            feed="okx_signal",
            role="market",
            lane="market_flow",
            event_at=expired_at,
            observed_at=expired_at,
        )],
        candidates=[market_snapshot(
            expired_at,
            chain="robinhood",
            market_cap_usd=58_000,
            liquidity_usd=20_500,
            volume_24h_usd=98_000,
            holders=140,
        )],
        observed_at=expired_at,
    )
    token = token_from(expired, chain="robinhood")

    assert "discovery_window_expired" in token["resonance"]["confirmation_gate"]["failures"]
    assert token["primary_state"] == "trend_watch"
    assert expired["alerts"][-1]["label"] == "趋势观察"
    assert expired["alerts"][-1]["transition"] == "cross_provider->trend_watch"


def test_expired_confirmation_history_recovers_trend_state_after_projection_restart():
    discovered = project(
        events=[
            projection_event(chain="robinhood", family="noxa", feed="noxa_launchpad"),
            projection_event(
                "gmgn-recovery",
                chain="robinhood",
                family="gmgn",
                feed="gmgn_hot_search",
                role="ranking",
                lane="hot_search",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            chain="robinhood",
            pair_age_hours=0.49,
            market_cap_usd=40_000,
            liquidity_usd=18_000,
            volume_24h_usd=80_000,
            holders=120,
        )],
    )
    confirmed = project(
        discovered,
        candidates=[market_snapshot(
            "2026-09-10T12:00:10+00:00",
            chain="robinhood",
            market_cap_usd=56_000,
            liquidity_usd=20_000,
            volume_24h_usd=95_000,
            holders=135,
        )],
        observed_at="2026-09-10T12:00:10+00:00",
    )
    restarted = json.loads(json.dumps(confirmed))
    restarted_token = token_from(restarted, chain="robinhood")
    restarted_token["primary_state"] = "new"
    restarted_token["active_states"] = ["new"]

    recovered = project(
        restarted,
        events=[projection_event(
            "okx-recovery",
            chain="robinhood",
            family="okx",
            feed="okx_signal",
            role="market",
            lane="market_flow",
            event_at="2026-09-10T12:00:50+00:00",
            observed_at="2026-09-10T12:00:50+00:00",
        )],
        candidates=[market_snapshot(
            "2026-09-10T12:00:50+00:00",
            chain="robinhood",
            market_cap_usd=58_000,
            liquidity_usd=20_500,
            volume_24h_usd=98_000,
            holders=140,
        )],
        observed_at="2026-09-10T12:00:50+00:00",
    )

    assert token_from(recovered, chain="robinhood")["primary_state"] == "trend_watch"


def test_high_initial_market_cap_stays_out_of_early_confirmation():
    snapshot = project(
        events=[
            projection_event(),
            projection_event(
                "okx-high-cap",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.02,
            market_cap_usd=15_000_000,
            liquidity_usd=600_000,
            volume_24h_usd=30_000_000,
            holders=2_000,
        )],
    )
    token = token_from(snapshot)

    assert token["resonance"]["subtype"] == "cross_provider"
    assert token["resonance"]["confirmation_gate"]["eligible"] is False
    assert "initial_market_cap_too_high" in token["resonance"]["confirmation_gate"]["failures"]
    assert "resonating" not in token["active_states"]


def test_independent_provider_families_create_cross_provider_resonance():
    snapshot = project(
        events=[
            projection_event(),
            projection_event("okx-1", family="okx", feed="okx_signal", role="market", lane="market_flow"),
        ]
    )

    assert token_from(snapshot)["resonance"]["subtype"] == "cross_provider"
    assert token_from(snapshot)["resonance"]["provider_families"] == ["gmgn", "okx"]


def test_address_level_verified_buys_create_smart_cluster_but_aggregate_count_does_not():
    aggregate = project(
        events=[projection_event()],
        candidates=[market_snapshot(NOW, smart_money=41)],
    )
    wallet_events = [
        projection_event(
            "wallet-1",
            family="985",
            feed="985_smartmoney",
            role="wallet",
            lane="smart_money",
            wallet_address="0x2222222222222222222222222222222222222222",
            direction="buy",
            wallet_verified=True,
        ),
        projection_event(
            "wallet-2",
            family="985",
            feed="985_smartmoney",
            role="wallet",
            lane="smart_money",
            wallet_address="0x3333333333333333333333333333333333333333",
            direction="buy",
            wallet_verified=True,
        ),
    ]
    clustered = project(events=[projection_event(), *wallet_events])

    assert "smart_cluster" not in token_from(aggregate)["active_states"]
    assert token_from(clustered)["wallet_evidence"]["verified_buyers"] == 2
    assert "smart_cluster" in token_from(clustered)["active_states"]


def test_platform_wallet_candidates_are_counted_separately_from_verified_buyers():
    candidate_wallet = "0x4444444444444444444444444444444444444444"
    verified_wallet = "0x5555555555555555555555555555555555555555"
    token = token_from(
        project(
            events=[
                projection_event(),
                projection_event(
                    "platform-wallet",
                    family="985",
                    feed="985_smartmoney",
                    role="wallet",
                    lane="smart_money",
                    event_at=NOW,
                    observed_at=NOW,
                    wallet_address=candidate_wallet,
                    direction="buy",
                ),
                projection_event(
                    "verified-wallet",
                    family="985",
                    feed="985_smartmoney",
                    role="wallet",
                    lane="smart_money",
                    event_at=NOW,
                    observed_at=NOW,
                    wallet_address=verified_wallet,
                    direction="buy",
                    wallet_verified=True,
                ),
            ]
        )
    )

    assert token["wallet_evidence"]["candidate_buyers"] == 2
    assert token["wallet_evidence"]["candidate_wallet_addresses"] == [candidate_wallet, verified_wallet]
    assert token["wallet_evidence"]["verified_buyers"] == 1
    assert token["wallet_evidence"]["wallet_addresses"] == [verified_wallet]
    assert "smart_cluster" not in token["active_states"]


def test_alerts_expose_candidate_wallet_counts_without_promoting_to_verified():
    wallet_a = "0x6666666666666666666666666666666666666666"
    wallet_b = "0x7777777777777777777777777777777777777777"
    snapshot = project(
        events=[
            projection_event(),
            projection_event(
                "platform-wallet-a",
                family="985",
                feed="985_smartmoney",
                role="wallet",
                lane="smart_money",
                wallet_address=wallet_a,
                direction="buy",
            ),
            projection_event(
                "platform-wallet-b",
                family="gmgn",
                feed="gmgn_skills_smartmoney",
                role="wallet",
                lane="smart_money",
                wallet_address=wallet_b,
                direction="buy",
            ),
        ],
        candidates=[market_snapshot(NOW, buyers=5, net_flow=120, trades=12, volume=400)],
    )

    alert = snapshot["alerts"][-1]

    assert alert["verified_wallet_count"] == 0
    assert alert["candidate_wallet_count"] == 2
    assert alert["candidate_wallets"] == [
        {"address": wallet_a, "evidence_id": "platform-wallet-a", "provider_family": "985", "provider_feed": "985_smartmoney", "event_at": NOW, "observed_at": NOW, "net_flow_usd": None},
        {"address": wallet_b, "evidence_id": "platform-wallet-b", "provider_family": "gmgn", "provider_feed": "gmgn_skills_smartmoney", "event_at": NOW, "observed_at": NOW, "net_flow_usd": None},
    ]


def test_normalized_wallet_events_keep_address_level_buy_evidence_for_projection():
    events = []
    for index, wallet in enumerate(
        (
            "0x6666666666666666666666666666666666666666",
            "0x7777777777777777777777777777777777777777",
        ),
        start=1,
    ):
        event, rejection = monitor.normalize_provider_event(
            {
                "event_at": NOW,
                "provider_event_id": f"wallet-normalized-{index}",
                "wallet_address": wallet,
                "direction": "buy",
                "wallet_verified": True,
                "amount_usd": 125 * index,
            },
            source="985_smartmoney",
            chain="bsc",
            contract_address=TOKEN,
            observed_at=NOW,
        )
        assert rejection is None
        events.append(event)

    snapshot = project(events=[projection_event(), *events])

    assert token_from(snapshot)["wallet_evidence"]["verified_buyers"] == 2
    assert token_from(snapshot)["wallet_evidence"]["wallet_addresses"] == [
        "0x6666666666666666666666666666666666666666",
        "0x7777777777777777777777777777777777777777",
    ]


def test_old_age_alone_never_creates_revival_but_new_baseline_acceleration_does():
    old_event = projection_event(
        family="onchain",
        feed="bsc_onchain",
        event_at="2026-09-07T12:00:00+00:00",
    )
    first = project(
        events=[old_event],
        candidates=[market_snapshot(NOW, buyers=4, net_flow=80, trades=8, volume=200)],
    )
    assert "revival" not in token_from(first)["active_states"]

    cooling = project(
        first,
        candidates=[market_snapshot("2026-09-10T12:01:00+00:00", buyers=1, net_flow=-10, trades=2, volume=40)],
        observed_at="2026-09-10T12:01:00+00:00",
    )
    revival = project(
        cooling,
        candidates=[market_snapshot("2026-09-10T12:02:00+00:00", buyers=10, net_flow=300, trades=20, volume=800)],
        observed_at="2026-09-10T12:02:00+00:00",
    )

    assert token_from(cooling)["primary_state"] == "cooling"
    assert token_from(revival)["primary_state"] == "revival"
    assert token_from(revival)["ranking_axes"]["timing"]["real_age_seconds"] == 259320.0


def test_publish_recovers_truncated_jsonl_tail_and_atomically_replaces_state(tmp_path):
    old_token = "0x4444444444444444444444444444444444444444"
    old_event = projection_event("old-1", token=old_token)
    ledger = tmp_path / "alpha-meme-events-v3.jsonl"
    ledger.write_bytes((json.dumps(old_event) + "\n" + '{"event_id":"truncated').encode("utf-8"))

    snapshot = monitor.publish_monitor_v3(
        out_dir=tmp_path,
        batch={"events": [projection_event()], "candidates": [], "rejections": []},
        source_health={"gmgn": {"status": "ok"}},
        observed_at=NOW,
    )

    persisted = json.loads((tmp_path / "alpha-meme-monitor-v3-state.json").read_text(encoding="utf-8"))
    complete_lines = ledger.read_text(encoding="utf-8").splitlines()
    assert persisted == snapshot
    assert {item["id"] for item in snapshot["tokens"]} == {f"bsc:{TOKEN}", f"bsc:{old_token}"}
    assert [json.loads(line)["event_id"] for line in complete_lines] == ["old-1", "event-1"]
    assert not (tmp_path / "alpha-meme-monitor-v3.lock").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_publishers_do_not_lose_events_or_corrupt_state(tmp_path):
    other = "0x5555555555555555555555555555555555555555"

    def publish(event):
        return monitor.publish_monitor_v3(
            out_dir=tmp_path,
            batch={"events": [event], "candidates": [], "rejections": []},
            source_health={event["provider_family"]: {"status": "ok"}},
            observed_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(publish, [projection_event(), projection_event("other-1", token=other, family="okx", feed="okx_trenches")]))

    persisted = json.loads((tmp_path / "alpha-meme-monitor-v3-state.json").read_text(encoding="utf-8"))
    ledger = [json.loads(line) for line in (tmp_path / "alpha-meme-events-v3.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {item["id"] for item in persisted["tokens"]} == {f"bsc:{TOKEN}", f"bsc:{other}"}
    assert {event["event_id"] for event in ledger} == {"event-1", "other-1"}


def test_publish_recovers_stale_orphaned_monitor_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "alpha-meme-monitor-v3.lock"
    lock_path.write_text("999999\n", encoding="ascii")
    monkeypatch.setattr(monitor, "LOCK_STALE_SECONDS", 0)

    snapshot = monitor.publish_monitor_v3(
        out_dir=tmp_path,
        batch={"events": [projection_event()], "candidates": [], "rejections": []},
        source_health={"gmgn": {"status": "ok"}},
        observed_at=NOW,
    )

    assert snapshot["monitor_status"] == "healthy"
    assert not lock_path.exists()


def test_old_duplicate_is_retained_but_does_not_refresh_or_promote_current_evidence():
    old = "2026-09-07T12:00:00+00:00"
    discovery = projection_event(event_at=old, observed_at=old)
    first = project(events=[discovery], observed_at=old)
    replayed = project(first, events=[dict(discovery)], observed_at=NOW)
    token = token_from(replayed)

    assert len(token["events"]) == 1
    assert token["freshness"]["status"] == "stale"
    assert token["freshness"]["last_observed_at"] == old
    assert token["state_version"] == token_from(first)["state_version"]


def test_current_resonance_and_wallet_clusters_require_fresh_event_and_observation_times():
    old = "2026-09-07T12:00:00+00:00"
    events = [
        projection_event(event_at=old, observed_at=NOW),
        projection_event(
            "okx-old-observation",
            family="okx",
            feed="okx_signal",
            role="market",
            lane="market_flow",
            event_at=NOW,
            observed_at=old,
        ),
        projection_event(
            "wallet-old-1",
            family="985",
            feed="985_smartmoney",
            role="wallet",
            lane="smart_money",
            event_at=old,
            observed_at=old,
            wallet_address="0x8888888888888888888888888888888888888888",
            direction="buy",
            wallet_verified=True,
        ),
        projection_event(
            "wallet-old-2",
            family="985",
            feed="985_smartmoney",
            role="wallet",
            lane="smart_money",
            event_at=old,
            observed_at=old,
            wallet_address="0x9999999999999999999999999999999999999999",
            direction="buy",
            wallet_verified=True,
        ),
    ]

    token = token_from(project(events=events))

    assert len(token["events"]) == 4
    assert token["resonance"]["subtype"] is None
    assert token["wallet_evidence"]["verified_buyers"] == 0
    assert token["wallet_evidence"]["historical_verified_buyers"] == 2
    assert "resonating" not in token["active_states"]
    assert "smart_cluster" not in token["active_states"]


def test_resonance_freshness_window_is_chain_specific():
    event_at = "2026-09-10T11:40:00+00:00"
    bsc = project(
        events=[
            projection_event(event_at=event_at, observed_at=event_at),
            projection_event(
                "bsc-okx",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
                event_at=event_at,
                observed_at=event_at,
            ),
        ]
    )
    robinhood = project(
        events=[
            projection_event(
                "rh-gmgn",
                chain="robinhood",
                event_at=event_at,
                observed_at=event_at,
            ),
            projection_event(
                "rh-okx",
                chain="robinhood",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
                event_at=event_at,
                observed_at=event_at,
            ),
        ]
    )

    assert token_from(bsc)["resonance"]["subtype"] is None
    assert token_from(bsc)["resonance"]["freshness_window_seconds"] == 900
    assert token_from(robinhood, chain="robinhood")["resonance"]["subtype"] == "cross_provider"
    assert token_from(robinhood, chain="robinhood")["resonance"]["freshness_window_seconds"] == 1800


def test_equal_time_market_rows_coalesce_deterministically_without_flow_delta():
    first = project(events=[projection_event()])
    low = market_snapshot(NOW, buyers=2, net_flow=20, trades=4, volume=100, field_source="a")
    high = market_snapshot(NOW, buyers=9, net_flow=200, trades=15, volume=500, field_source="b")

    forward = project(first, candidates=[low, high])
    reverse = project(first, candidates=[high, low])
    forward_token = token_from(forward)
    reverse_token = token_from(reverse)

    assert len(forward_token["market"]["snapshots"]) == 1
    assert forward_token["market"] == reverse_token["market"]
    assert forward_token["ranking_axes"]["flow"]["direction"] == "unknown"
    assert forward_token["primary_state"] == "new"


def test_market_snapshot_tracks_holder_source_and_observation_separately():
    candidate = market_snapshot(
        "2026-09-10T12:05:00+00:00",
        price_usd=0.001,
        holders=240,
        top10_holder_pct=13.4,
        holder_source="gmgn",
        holder_observed_at="2026-09-10T12:04:55+00:00",
        quote_source="dexscreener",
        quote_observed_at="2026-09-10T12:05:00+00:00",
    )

    token = token_from(project(
        events=[projection_event(event_at="2026-09-10T12:04:50+00:00", observed_at="2026-09-10T12:04:50+00:00")],
        candidates=[candidate],
        observed_at="2026-09-10T12:05:01+00:00",
    ))

    assert token["market"]["field_sources"]["holders"] == "gmgn"
    assert token["market"]["field_sources"]["top10_holder_pct"] == "gmgn"
    assert token["market"]["field_observed_at"]["holders"] == "2026-09-10T12:04:55+00:00"
    assert token["market"]["field_observed_at"]["top10_holder_pct"] == "2026-09-10T12:04:55+00:00"
    assert token["market"]["field_sources"]["price_usd"] == "dexscreener"


def test_first_seen_uses_earliest_valid_observation_and_deterministic_source():
    events = [
        projection_event(
            "late",
            family="okx",
            feed="okx_signal",
            role="market",
            lane="market_flow",
            event_at="2026-09-10T12:05:00+00:00",
            observed_at="2026-09-10T12:05:00+00:00",
        ),
        projection_event(
            "early-z",
            family="okx",
            feed="okx_trenches",
            event_at="2026-09-10T12:01:00+00:00",
            observed_at="2026-09-10T12:01:00+00:00",
        ),
        projection_event(
            "early-a",
            feed="gmgn_trenches",
            event_at="2026-09-10T12:01:00+00:00",
            observed_at="2026-09-10T12:01:00+00:00",
        ),
    ]

    forward = token_from(project(events=events, observed_at="2026-09-10T12:06:00+00:00"))
    reverse = token_from(project(events=list(reversed(events)), observed_at="2026-09-10T12:06:00+00:00"))

    assert forward["identity"]["first_seen_at"] == "2026-09-10T12:01:00+00:00"
    assert forward["identity"]["first_seen_source"] == "gmgn_trenches"
    assert reverse["identity"]["first_seen_at"] == forward["identity"]["first_seen_at"]
    assert reverse["identity"]["first_seen_source"] == forward["identity"]["first_seen_source"]


def test_compaction_and_later_events_never_move_first_seen_forward():
    events = [
        projection_event(
            f"event-{minute:02d}",
            event_at=f"2026-09-10T12:{minute:02d}:00+00:00",
            observed_at=f"2026-09-10T12:{minute:02d}:00+00:00",
        )
        for minute in range(60)
    ]
    initial = project(events=events, observed_at="2026-09-10T13:00:00+00:00")
    compact = monitor.compact_monitor_snapshot(initial)
    compact_token = token_from(compact)

    assert compact_token["identity"]["first_seen_at"] == "2026-09-10T12:00:00+00:00"
    assert any(event["event_id"] == "event-00" for event in compact_token["events"])

    updated = project(
        compact,
        events=[projection_event(
            "event-next",
            event_at="2026-09-10T13:01:00+00:00",
            observed_at="2026-09-10T13:01:00+00:00",
        )],
        observed_at="2026-09-10T13:01:00+00:00",
    )

    assert token_from(updated)["identity"]["first_seen_at"] == "2026-09-10T12:00:00+00:00"


def test_compaction_recomputes_counts_after_stale_tokens_are_hidden():
    initial = project(
        events=[projection_event("compact-count")],
        candidates=[market_snapshot(NOW)],
    )
    stale = project(initial, observed_at="2026-09-10T12:20:00+00:00")

    compact = monitor.compact_monitor_snapshot(stale)

    assert compact["tokens"] == []
    assert compact["counts"]["tokens"] == 0
    assert compact["counts"]["primary_state"] == {}


def test_out_of_order_publish_preserves_monotonic_metadata_and_unique_events(tmp_path):
    newer = "2026-09-10T12:02:00+00:00"
    older = "2026-09-10T12:01:00+00:00"
    first = monitor.publish_monitor_v3(
        out_dir=tmp_path,
        batch={
            "events": [projection_event("newer", event_at=newer, observed_at=newer)],
            "candidates": [market_snapshot(newer, buyers=8, market_cap_usd=20_000)],
            "rejections": [],
        },
        source_health={"gmgn": {"status": "ok", "observed_at": newer, "last_success": newer}},
        observed_at=newer,
    )
    second = monitor.publish_monitor_v3(
        out_dir=tmp_path,
        batch={
            "events": [projection_event("older", event_at=older, observed_at=older)],
            "candidates": [market_snapshot(older, buyers=1, market_cap_usd=5_000)],
            "rejections": [],
        },
        source_health={"gmgn": {"status": "error", "observed_at": older, "last_success": older}},
        observed_at=older,
    )
    token = token_from(second)
    ledger = [
        json.loads(line)
        for line in (tmp_path / "alpha-meme-events-v3.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert first["observed_at"] == second["observed_at"] == newer
    assert second["source_health"]["gmgn"] == first["source_health"]["gmgn"]
    assert token["freshness"]["last_observed_at"] == newer
    assert token["freshness"]["report_refreshed_at"] == newer
    assert token["market"]["observed_at"] == newer
    assert token["market"]["market_cap_usd"] == 20_000
    assert {event["event_id"] for event in ledger} == {"newer", "older"}


def test_undated_candidate_uses_incoming_stamp_and_cannot_replace_newer_market(tmp_path):
    newer = "2026-09-10T12:02:00+00:00"
    older = "2026-09-10T12:01:00+00:00"
    monitor.publish_monitor_v3(
        out_dir=tmp_path,
        batch={
            "events": [projection_event("newer-undated", event_at=newer, observed_at=newer)],
            "candidates": [market_snapshot(newer, buyers=8, market_cap_usd=20_000)],
            "rejections": [],
        },
        source_health={"gmgn": {"status": "ok", "observed_at": newer}},
        observed_at=newer,
    )
    snapshot = monitor.publish_monitor_v3(
        out_dir=tmp_path,
        batch={
            "events": [projection_event("older-undated", event_at=older, observed_at=older)],
            "candidates": [
                {
                    "chain": "bsc",
                    "contract_address": TOKEN,
                    "unique_buyers_1m": 1,
                    "market_cap_usd": 1_000,
                }
            ],
            "rejections": [],
        },
        source_health={"gmgn": {"status": "ok", "observed_at": older}},
        observed_at=older,
    )
    token = token_from(snapshot)

    assert token["market"]["observed_at"] == newer
    assert token["market"]["unique_buyers_1m"] == 8
    assert token["market"]["market_cap_usd"] == 20_000
    assert len(token["market"]["snapshots"]) == 1
    assert {event["event_id"] for event in token["events"]} == {
        "newer-undated",
        "older-undated",
    }


def test_current_source_health_updates_dynamic_metrics_but_not_historical_timestamps():
    first = project(
        events=[projection_event()],
        health={
            "gmgn": {
                "status": "ok",
                "row_count": 10,
                "latency_ms": 200,
                "last_success": NOW,
            }
        },
    )
    current = project(
        first,
        health={
            "gmgn": {
                "status": "ok",
                "row_count": 3,
                "latency_ms": 100,
                "last_success": "2026-09-10T11:59:00+00:00",
            }
        },
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert current["source_health"]["gmgn"]["row_count"] == 3
    assert current["source_health"]["gmgn"]["latency_ms"] == 100
    assert current["source_health"]["gmgn"]["last_success"] == NOW


def test_unreported_source_health_expires_instead_of_remaining_online_forever():
    first = project(
        events=[projection_event()],
        health={
            "gmgn": {"status": "ok", "observed_at": NOW, "row_count": 10},
            "okx": {"status": "ok", "observed_at": NOW, "row_count": 8},
        },
    )
    current = project(
        first,
        health={
            "gmgn": {
                "status": "ok",
                "observed_at": "2026-09-10T12:03:00+00:00",
                "row_count": 12,
            }
        },
        observed_at="2026-09-10T12:03:00+00:00",
    )

    assert current["source_health"]["gmgn"]["status"] == "ok"
    assert current["source_health"]["okx"]["status"] == "stale"
    assert current["source_health"]["okx"]["stale_age_seconds"] == 180
    assert current["monitor_status"] == "degraded"


def test_temporarily_unreported_source_stays_online_during_async_refresh_grace():
    first = project(
        events=[projection_event()],
        health={"okx": {"status": "ok", "observed_at": NOW, "row_count": 8}},
    )
    current = project(
        first,
        health={"gmgn": {"status": "ok", "observed_at": "2026-09-10T12:00:30+00:00"}},
        observed_at="2026-09-10T12:00:30+00:00",
    )

    assert current["source_health"]["okx"]["status"] == "ok"


def test_recovered_source_health_clears_previous_error_text():
    first = project(
        events=[projection_event()],
        health={
            "gmgn": {
                "status": "error",
                "observed_at": NOW,
                "error": "rate limited",
            }
        },
    )
    current = project(
        first,
        health={
            "gmgn": {
                "status": "ok",
                "observed_at": "2026-09-10T12:01:00+00:00",
                "row_count": 20,
            }
        },
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert current["source_health"]["gmgn"]["status"] == "ok"
    assert "error" not in current["source_health"]["gmgn"]


def test_snapshot_baseline_uses_earliest_market_observation_not_late_watch_value():
    token = {
        "id": "robinhood:0x" + "9" * 40,
        "identity": {
            "chain": "robinhood",
            "contract_address": "0x" + "9" * 40,
            "first_seen_at": "2026-09-12T12:07:09+00:00",
        },
        "market": {
            "snapshots": [
                {
                    "observed_at": "2026-09-12T12:07:05+00:00",
                    "market_cap_usd": 46_297,
                    "price_usd": None,
                },
                {
                    "observed_at": "2026-09-12T12:18:15+00:00",
                    "market_cap_usd": 158_982,
                    "price_usd": 0.0001589,
                },
            ]
        },
    }

    baselines = monitor.build_monitor_snapshot_baselines({"tokens": [token]})

    assert baselines[token["id"]]["first_market_cap_usd"] == 46_297
    assert baselines[token["id"]]["peak_market_cap_usd"] == 158_982
    assert baselines[token["id"]]["first_seen_at"] == "2026-09-12T12:07:09+00:00"


def test_stale_source_health_entry_cannot_replace_newer_source_status():
    first = project(
        events=[projection_event()],
        health={"gmgn": {"status": "ok", "observed_at": NOW, "row_count": 10}},
    )
    current = project(
        first,
        health={
            "gmgn": {
                "status": "error",
                "observed_at": "2026-09-10T11:59:00+00:00",
                "row_count": 0,
            }
        },
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert current["source_health"]["gmgn"] == first["source_health"]["gmgn"]


@pytest.mark.parametrize("unknown_timestamp", [None, "not-a-timestamp"])
def test_unknown_source_health_timestamp_preserves_prior_known_time_while_status_updates(
    unknown_timestamp,
):
    first = project(
        events=[projection_event()],
        health={"gmgn": {"status": "ok", "row_count": 10, "last_success": NOW}},
    )
    current = project(
        first,
        health={
            "gmgn": {
                "status": "error",
                "row_count": 0,
                "last_success": unknown_timestamp,
            }
        },
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert current["source_health"]["gmgn"]["status"] == "error"
    assert current["source_health"]["gmgn"]["row_count"] == 0
    assert current["source_health"]["gmgn"]["last_success"] == NOW


def test_equal_time_wallet_buy_sell_conflicts_are_neutral_without_provider_order():
    wallet_a = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    wallet_b = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    events = [projection_event()]
    for index, wallet in enumerate((wallet_a, wallet_b), start=1):
        events.extend(
            [
                projection_event(
                    f"a-sell-{index}",
                    family="985",
                    feed="985_smartmoney",
                    role="wallet",
                    lane="smart_money",
                    wallet_address=wallet,
                    direction="sell",
                    wallet_verified=True,
                ),
                projection_event(
                    f"z-buy-{index}",
                    family="985",
                    feed="985_smartmoney",
                    role="wallet",
                    lane="smart_money",
                    wallet_address=wallet,
                    direction="buy",
                    wallet_verified=True,
                ),
            ]
        )

    token = token_from(project(events=events))

    assert token["wallet_evidence"]["verified_buyers"] == 0
    assert token["wallet_evidence"]["conflicting_wallets"] == [wallet_a, wallet_b]
    assert "smart_cluster" not in token["active_states"]
    assert "resonating" not in token["active_states"]


def test_equal_time_wallet_conflict_can_use_real_provider_log_order():
    wallet = "0xcccccccccccccccccccccccccccccccccccccccc"
    token = token_from(
        project(
            events=[
                projection_event(),
                projection_event(
                    "z-sell-first",
                    family="985",
                    feed="985_smartmoney",
                    role="wallet",
                    lane="smart_money",
                    wallet_address=wallet,
                    direction="sell",
                    wallet_verified=True,
                    log_index=4,
                ),
                projection_event(
                    "a-buy-second",
                    family="985",
                    feed="985_smartmoney",
                    role="wallet",
                    lane="smart_money",
                    wallet_address=wallet,
                    direction="buy",
                    wallet_verified=True,
                    log_index=5,
                ),
            ]
        )
    )

    assert token["wallet_evidence"]["verified_buyers"] == 1
    assert token["wallet_evidence"]["wallet_addresses"] == [wallet]
    assert token["wallet_evidence"]["conflicting_wallets"] == []


def test_timing_axis_exposes_independent_observation_market_event_pair_and_migration_ages():
    events = [
        projection_event(
            "created",
            family="onchain",
            feed="bsc_onchain",
            event_at="2026-09-10T09:00:00+00:00",
            observed_at="2026-09-10T09:01:00+00:00",
        ),
        projection_event(
            "previous",
            event_at="2026-09-10T11:55:00+00:00",
            observed_at="2026-09-10T11:55:30+00:00",
        ),
        projection_event(
            "latest",
            family="okx",
            feed="okx_signal",
            role="market",
            lane="market_flow",
            event_at="2026-09-10T11:58:00+00:00",
            observed_at="2026-09-10T11:58:30+00:00",
        ),
    ]
    snapshot = project(
        events=events,
        candidates=[
            market_snapshot(
                "2026-09-10T11:59:00+00:00",
                buyers=4,
                pair_created_at="2026-09-10T11:00:00+00:00",
                migration_at="2026-09-10T11:30:00+00:00",
            )
        ],
    )
    timing = token_from(snapshot)["ranking_axes"]["timing"]

    assert timing["real_age_seconds"] == 10_800.0
    assert timing["current_observation_age_seconds"] == 60.0
    assert timing["last_observation_age_seconds"] == 60.0
    assert timing["market_snapshot_age_seconds"] == 60.0
    assert timing["latest_event_age_seconds"] == 120.0
    assert timing["previous_meaningful_event_age_seconds"] == 300.0
    assert timing["pair_age_seconds"] == 3_600.0
    assert timing["migration_age_seconds"] == 1_800.0


def test_dynamic_states_expire_when_their_market_evidence_is_no_longer_current():
    first = project(
        events=[
            projection_event(
                family="onchain",
                feed="bsc_onchain",
                event_at="2026-09-07T12:00:00+00:00",
            )
        ],
        candidates=[market_snapshot(NOW, buyers=2, net_flow=50, trades=4, volume=100)],
    )
    building = project(
        first,
        candidates=[market_snapshot("2026-09-10T12:01:00+00:00", buyers=8, net_flow=200, trades=16, volume=500)],
        observed_at="2026-09-10T12:01:00+00:00",
    )
    cooling = project(
        building,
        candidates=[market_snapshot("2026-09-10T12:02:00+00:00", buyers=1, net_flow=-20, trades=2, volume=30)],
        observed_at="2026-09-10T12:02:00+00:00",
    )
    revival = project(
        cooling,
        candidates=[market_snapshot("2026-09-10T12:03:00+00:00", buyers=12, net_flow=400, trades=24, volume=900)],
        observed_at="2026-09-10T12:03:00+00:00",
    )
    expired = project(revival, observed_at="2026-09-10T12:20:00+00:00")

    assert token_from(building)["primary_state"] == "trend_watch"
    assert token_from(cooling)["primary_state"] == "cooling"
    assert token_from(revival)["primary_state"] == "revival"
    assert "stale" in token_from(expired)["active_states"]
    assert not {"building", "trend_watch", "cooling", "revival"}.intersection(token_from(expired)["active_states"])


def test_transition_alert_is_emitted_once_with_complete_evidence():
    created = projection_event(
        "created-alert",
        family="onchain",
        feed="bsc_onchain",
        event_at="2026-09-10T11:58:00+00:00",
        observed_at="2026-09-10T11:58:10+00:00",
        symbol="ALERT",
    )
    first = project(
        events=[created],
        candidates=[
            market_snapshot(
                "2026-09-10T11:58:10+00:00",
                market_cap_usd=10_000,
                liquidity_usd=4_000,
                buys_1m=3,
                sells_1m=1,
                buys_5m=8,
                sells_5m=3,
                unique_buyers_5m=6,
                net_buy_flow_5m_usd=700,
            )
        ],
        observed_at="2026-09-10T11:58:10+00:00",
    )
    assert first["alerts"] == []

    promoted = project(
        first,
        events=[
            projection_event(
                "gmgn-trending-alert",
                family="gmgn",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
                event_at="2026-09-10T11:59:00+00:00",
                observed_at="2026-09-10T11:59:02+00:00",
            )
        ],
        candidates=[
            market_snapshot(
                "2026-09-10T11:59:02+00:00",
                market_cap_usd=20_000,
                liquidity_usd=8_000,
                holders=80,
                buys_1m=9,
                sells_1m=2,
                buys_5m=20,
                sells_5m=5,
                unique_buyers_5m=15,
                net_buy_flow_5m_usd=2_100,
            )
        ],
        observed_at="2026-09-10T11:59:02+00:00",
    )
    alert = promoted["alerts"][0]

    assert alert["key"] == f"bsc:{TOKEN}:new->full:2"
    assert alert["label"] == "多源共振"
    assert alert["policy"] == {"chain": "bsc", "stage": "high_priority"}
    assert alert["chain"] == "bsc"
    assert alert["contract_address"] == TOKEN
    assert alert["event_at"] == "2026-09-10T11:59:00+00:00"
    assert alert["observed_at"] == "2026-09-10T11:59:02+00:00"
    assert alert["real_age_seconds"] == 62.0
    assert alert["event_age_seconds"] == 2.0
    assert alert["market"]["first_market_cap_usd"] == 10_000
    assert alert["market"]["current_market_cap_usd"] == 20_000
    assert alert["market"]["market_cap_multiple"] == 2.0
    assert alert["market"]["liquidity_usd"] == 8_000
    assert alert["market"]["buys_1m"] == 9
    assert alert["market"]["sells_5m"] == 5
    assert alert["market"]["unique_buyers_5m"] == 15
    assert alert["market"]["net_buy_flow_5m_usd"] == 2_100
    assert {item["provider_family"] for item in alert["sources"]} == {"onchain", "gmgn"}
    assert all(item["event_at"] and item["observed_at"] and item["evidence_id"] for item in alert["sources"])
    assert alert["verified_wallets"] == []
    assert alert["risks"]["hard_blocked"] is False
    assert set(alert["evidence_ids"]) == {"created-alert", "gmgn-trending-alert"}
    assert "推荐买入" not in json.dumps(alert, ensure_ascii=False)

    polled = project(
        promoted,
        observed_at="2026-09-10T11:59:30+00:00",
        health={"gmgn": {"status": "ok"}, "onchain": {"status": "ok"}},
    )
    assert polled["alerts"] == promoted["alerts"]
    assert token_from(polled)["state_version"] == 2


def test_material_source_transition_creates_a_new_alert_version():
    first = project(
        events=[
            projection_event(
                "bsc-live-trending",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
            )
        ],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.00004,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    assert first["alerts"][0]["key"] == f"bsc:{TOKEN}:new->early_focus:1"

    promoted = project(
        first,
        events=[
            projection_event(
                "okx-flow-confirmation",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
                event_at="2026-09-10T12:01:00+00:00",
                observed_at="2026-09-10T12:01:00+00:00",
            )
        ],
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert [item["key"] for item in promoted["alerts"]] == [
        f"bsc:{TOKEN}:new->early_focus:1",
        f"bsc:{TOKEN}:early_focus->cross_provider:2",
    ]
    assert promoted["alerts"][-1]["label"] == "多源共振"


def test_new_independent_source_can_alert_while_resonance_state_stays_the_same():
    first = project(
        events=[
            projection_event(
                "same-state-gmgn",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
            ),
            projection_event(
                "same-state-okx",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
            ),
        ],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.00004,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    assert token_from(first)["resonance"]["subtype"] == "cross_provider"

    expanded = project(
        first,
        events=[
            projection_event(
                "same-state-debot",
                family="debot",
                feed="debot_signal",
                role="market",
                lane="market_flow",
                event_at="2026-09-10T12:01:00+00:00",
                observed_at="2026-09-10T12:01:00+00:00",
            )
        ],
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert token_from(expanded)["resonance"]["subtype"] == "cross_provider"
    assert token_from(expanded)["state_version"] == token_from(first)["state_version"] + 1
    assert expanded["alerts"][-1]["transition"] == "cross_provider->cross_provider"
    assert expanded["alerts"][-1]["key"].endswith(":cross_provider->cross_provider:3")


def test_ai_absence_or_failure_never_suppresses_deterministic_alert():
    first = project(events=[projection_event()])
    token_from(first)["ai"] = {
        "status": "unavailable",
        "analyzed_at": None,
        "evidence_ids": [],
        "error": "timeout",
    }
    building = project(
        first,
        candidates=[market_snapshot(NOW, buyers=2, net_flow=20, trades=4, volume=100)],
    )
    building = project(
        building,
        candidates=[
            market_snapshot(
                "2026-09-10T12:01:00+00:00",
                buyers=8,
                net_flow=250,
                trades=14,
                volume=600,
            )
        ],
        observed_at="2026-09-10T12:01:00+00:00",
    )

    assert building["alerts"][-1]["transition"] == "new->building"
    assert building["alerts"][-1]["label"] == "加速"


def test_bsc_and_robinhood_apply_different_early_focus_policies():
    bsc = project(
        events=[
            projection_event(
                "bsc-trend-only",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
            )
        ]
    )
    robinhood_trend = project(
        events=[
            projection_event(
                "rh-trend-only",
                chain="robinhood",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
            )
        ]
    )
    robinhood_stack = project(
        events=[
            projection_event(
                "rh-launch",
                chain="robinhood",
                feed="gmgn_trenches",
                role="discovery",
                lane="new_launch",
            ),
            projection_event(
                "rh-wallet",
                chain="robinhood",
                feed="gmgn_cli_smartmoney",
                role="wallet",
                lane="smart_money",
                wallet_address="0x1111111111111111111111111111111111111111",
                direction="buy",
                wallet_verified=True,
            ),
        ]
    )

    assert bsc["alerts"][0]["policy"] == {"chain": "bsc", "stage": "early_focus"}
    assert robinhood_trend["alerts"] == []
    assert robinhood_stack["alerts"][0]["policy"] == {
        "chain": "robinhood",
        "stage": "early_focus",
    }
    assert robinhood_stack["alerts"][0]["transition"] == "new->platform_stack"


def test_bsc_cross_provider_without_live_trending_or_launch_trending_does_not_alert():
    snapshot = project(
        events=[
            projection_event(
                "bsc-okx-hot-search",
                family="okx",
                feed="okx_hot_search",
                role="ranking",
                lane="hot_search",
            ),
            projection_event(
                "bsc-debot-flow",
                family="debot",
                feed="debot_signal",
                role="market",
                lane="market_flow",
            ),
        ]
    )

    assert token_from(snapshot)["resonance"]["subtype"] == "cross_provider"
    assert snapshot["alerts"] == []


def test_robinhood_two_providers_with_only_one_lane_does_not_alert():
    snapshot = project(
        events=[
            projection_event(
                "rh-gmgn-trend",
                chain="robinhood",
                family="gmgn",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
            ),
            projection_event(
                "rh-okx-trend",
                chain="robinhood",
                family="okx",
                feed="okx_trending",
                role="ranking",
                lane="trending",
            ),
        ]
    )

    token = token_from(snapshot, chain="robinhood")
    assert token["resonance"]["subtype"] == "cross_provider"
    assert token["resonance"]["signal_lanes"] == ["trending"]
    assert snapshot["alerts"] == []


def test_out_of_order_publisher_retains_old_event_without_advancing_current_state():
    current = project(
        events=[
            projection_event(
                "current-gmgn-trend",
                feed="gmgn_live_trending",
                role="ranking",
                lane="trending",
            )
        ],
        candidates=[market_snapshot(
            NOW,
            price_usd=0.00004,
            market_cap_usd=40_000,
            liquidity_usd=12_000,
            volume_24h_usd=30_000,
            holders=80,
        )],
    )
    current_token = token_from(current)

    out_of_order = project(
        current,
        events=[
            projection_event(
                "older-okx-flow",
                family="okx",
                feed="okx_signal",
                role="market",
                lane="market_flow",
                event_at="2026-09-10T11:58:00+00:00",
                observed_at="2026-09-10T11:59:00+00:00",
            )
        ],
        observed_at="2026-09-10T11:59:00+00:00",
    )
    retained = token_from(out_of_order)

    assert {event["event_id"] for event in retained["events"]} == {
        "current-gmgn-trend",
        "older-okx-flow",
    }
    assert out_of_order["observed_at"] == current["observed_at"]
    assert retained["state_version"] == current_token["state_version"]
    assert retained["primary_state"] == current_token["primary_state"]
    assert retained["resonance"] == current_token["resonance"]
    assert retained["state_history"] == current_token["state_history"]
    assert out_of_order["alerts"] == current["alerts"]

    chronological = project(
        out_of_order,
        observed_at="2026-09-10T12:01:00+00:00",
    )
    assert token_from(chronological)["state_version"] == current_token["state_version"] + 1
    assert chronological["alerts"][-1]["transition"] == "early_focus->cross_provider"


def test_alert_cooldown_suppresses_flow_oscillation_but_expires():
    first = project(
        events=[
            projection_event(
                "cooldown-launch",
                family="onchain",
                feed="bsc_onchain",
                event_at="2026-09-10T11:58:00+00:00",
                observed_at="2026-09-10T11:58:00+00:00",
            )
        ],
        candidates=[market_snapshot("2026-09-10T11:58:00+00:00", buyers=2, net_flow=20, trades=4, volume=100)],
        observed_at="2026-09-10T11:58:00+00:00",
    )
    building = project(
        first,
        candidates=[market_snapshot("2026-09-10T11:59:00+00:00", buyers=8, net_flow=200, trades=16, volume=500)],
        observed_at="2026-09-10T11:59:00+00:00",
    )
    cooling = project(
        building,
        candidates=[market_snapshot("2026-09-10T12:00:00+00:00", buyers=1, net_flow=-20, trades=2, volume=30)],
        observed_at="2026-09-10T12:00:00+00:00",
    )
    rebound_inside_cooldown = project(
        cooling,
        candidates=[market_snapshot("2026-09-10T12:00:30+00:00", buyers=9, net_flow=250, trades=18, volume=600)],
        observed_at="2026-09-10T12:00:30+00:00",
    )

    assert building["alerts"][-1]["transition"] == "new->building"
    assert token_from(rebound_inside_cooldown)["primary_state"] == "building"
    assert token_from(rebound_inside_cooldown)["state_version"] > token_from(building)["state_version"]
    assert rebound_inside_cooldown["alerts"] == building["alerts"]

    cooled_again = project(
        rebound_inside_cooldown,
        candidates=[market_snapshot("2026-09-10T12:05:00+00:00", buyers=1, net_flow=-30, trades=2, volume=25)],
        observed_at="2026-09-10T12:05:00+00:00",
    )
    rebound_after_cooldown = project(
        cooled_again,
        candidates=[market_snapshot("2026-09-10T12:05:30+00:00", buyers=10, net_flow=300, trades=20, volume=700)],
        observed_at="2026-09-10T12:05:30+00:00",
    )

    assert len(rebound_after_cooldown["alerts"]) == len(building["alerts"]) + 1
    assert rebound_after_cooldown["alerts"][-1]["transition"] == "cooling->building"


def test_critical_risk_escalation_bypasses_alert_cooldown():
    first = project(
        events=[
            projection_event(
                "risk-cooldown-launch",
                family="onchain",
                feed="bsc_onchain",
                event_at="2026-09-10T11:58:00+00:00",
                observed_at="2026-09-10T11:58:00+00:00",
            )
        ],
        candidates=[market_snapshot("2026-09-10T11:58:00+00:00", buyers=2, net_flow=20, trades=4, volume=100)],
        observed_at="2026-09-10T11:58:00+00:00",
    )
    building = project(
        first,
        candidates=[market_snapshot("2026-09-10T11:59:00+00:00", buyers=8, net_flow=200, trades=16, volume=500)],
        observed_at="2026-09-10T11:59:00+00:00",
    )
    blocked = project(
        building,
        candidates=[
            market_snapshot(
                "2026-09-10T11:59:30+00:00",
                buyers=8,
                net_flow=200,
                trades=16,
                volume=500,
                hard_risk=True,
                hard_risk_flags=["confirmed_honeypot"],
                risk_source="okx",
            )
        ],
        observed_at="2026-09-10T11:59:30+00:00",
    )

    assert len(blocked["alerts"]) == len(building["alerts"]) + 1
    assert blocked["alerts"][-1]["transition"] == "building->blocked_risk"
    assert blocked["alerts"][-1]["label"] == "高风险"


def test_aggregate_wallet_count_cannot_create_robinhood_wallet_alert():
    snapshot = project(
        events=[
            projection_event(
                "rh-launch-count",
                chain="robinhood",
                feed="gmgn_trenches",
            ),
            projection_event(
                "rh-aggregate-wallet-count",
                chain="robinhood",
                feed="gmgn_cli_smartmoney",
                role="wallet",
                lane="smart_money",
                smart_money_count=41,
            ),
        ]
    )
    token = token_from(snapshot, chain="robinhood")

    assert token["wallet_evidence"]["verified_buyers"] == 0
    assert "smart_cluster" not in token["active_states"]
    assert snapshot["alerts"] == []


def test_verified_wallet_alert_keeps_unknown_net_flow_unknown():
    snapshot = project(
        events=[
            projection_event("rh-launch-unknown-flow", chain="robinhood"),
            projection_event(
                "rh-wallet-unknown-flow",
                chain="robinhood",
                feed="gmgn_cli_smartmoney",
                role="wallet",
                lane="smart_money",
                wallet_address="0x2222222222222222222222222222222222222222",
                direction="buy",
                wallet_verified=True,
            ),
        ]
    )
    alert = snapshot["alerts"][0]

    assert alert["verified_wallet_count"] == 1
    assert alert["verified_wallet_net_flow_usd"] is None
    assert alert["verified_wallets"][0]["net_flow_usd"] is None


def test_risk_transition_stays_visible_and_populates_counts_and_kill_counts():
    first = project(events=[projection_event("risk-launch", family="onchain", feed="bsc_onchain")])
    blocked = project(
        first,
        candidates=[
            market_snapshot(
                "2026-09-10T12:01:00+00:00",
                hard_risk=True,
                hard_risk_flags=["confirmed_honeypot", "official_ca_conflict"],
                risk_source="okx",
            )
        ],
        rejections=[
            {"reason": "invalid_address", "raw_fingerprint": "bad-1"},
            {"reason": "invalid_address", "raw_fingerprint": "bad-2"},
        ],
        observed_at="2026-09-10T12:01:00+00:00",
    )
    token = token_from(blocked)
    alert = blocked["alerts"][-1]

    assert token["primary_state"] == "blocked_risk"
    assert alert["transition"] == "new->blocked_risk"
    assert alert["label"] == "高风险"
    assert alert["risks"]["hard_failures"] == ["confirmed_honeypot", "official_ca_conflict"]
    assert {item["reason"] for item in alert["risks"]["evidence"]} == {
        "confirmed_honeypot",
        "official_ca_conflict",
    }
    assert all(item["source"] == "okx" for item in alert["risks"]["evidence"])
    assert all(item["observed_at"] == "2026-09-10T12:01:00+00:00" for item in alert["risks"]["evidence"])
    assert all(len(item["evidence_id"]) == 64 for item in alert["risks"]["evidence"])
    assert blocked["counts"]["tokens"] == 1
    assert blocked["counts"]["primary_state"]["blocked_risk"] == 1
    assert blocked["counts"]["active_state"]["blocked_risk"] == 1
    assert blocked["counts"]["freshness"]["fresh"] == 1
    assert blocked["kill_counts"] == {
        "total": 4,
        "rejections": 2,
        "hard_risks": 2,
        "by_reason": {
            "confirmed_honeypot": 1,
            "invalid_address": 2,
            "official_ca_conflict": 1,
        },
    }


@pytest.mark.parametrize(
    ("rejections", "health", "reason"),
    [
        ([], {"gmgn": {"status": "ok"}}, "no_events"),
        ([{"reason": "invalid_address", "raw_fingerprint": "bad"}], {"gmgn": {"status": "ok"}}, "all_rejected"),
        ([], {"gmgn": {"status": "stale"}}, "stale_sources"),
        ([], {"gmgn": {"status": "error", "error_category": "timeout"}}, "source_failure"),
    ],
)
def test_empty_state_truthfully_explains_why_no_tokens_are_visible(rejections, health, reason):
    snapshot = project(rejections=rejections, health=health)

    assert snapshot["tokens"] == []
    assert snapshot["empty_state"]["is_empty"] is True
    assert snapshot["empty_state"]["reason"] == reason
    assert snapshot["empty_state"]["event_count"] == 0
    assert snapshot["empty_state"]["rejection_count"] == len(rejections)
    assert snapshot["empty_state"]["failed_sources"] == (
        ["gmgn"] if reason == "source_failure" else []
    )
    assert snapshot["empty_state"]["stale_sources"] == (
        ["gmgn"] if reason == "stale_sources" else []
    )
