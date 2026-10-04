import importlib.util
import sys
from pathlib import Path

import pytest


BASE = Path(__file__).parent
NOW = "2026-09-10T12:00:00+00:00"
OLD = "2026-09-10T10:00:00+00:00"
TOKEN = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
OTHER = "0x1111111111111111111111111111111111111111"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_module("alpha_monitor_v3", BASE / "alpha_monitor_v3.py")
evidence = load_module("alpha_token_evidence", BASE / "alpha_token_evidence.py")


def provider_row(feed: str, event_id: str, **extra):
    return {
        "chain": "bsc",
        "contract_address": TOKEN,
        "provider_feed": feed,
        "provider_event_id": event_id,
        "event_at": NOW,
        "observed_at": NOW,
        **extra,
    }


def test_evidence_id_is_stable_and_field_scoped():
    args = (f"bsc:{TOKEN}", "gmgn", "gmgn_trending", "rank-7", "market.market_cap_usd", NOW, "abc123")

    first = evidence.evidence_id(*args)
    duplicate = evidence.evidence_id(*args)
    other_field = evidence.evidence_id(*args[:4], "market.liquidity_usd", *args[5:])

    assert first == duplicate
    assert first != other_field
    assert len(first) == 64


def test_build_evidence_record_covers_all_task_provider_feeds_as_one_identity():
    rows = [
        provider_row("gmgn_new_launch", "gmgn-launch"),
        provider_row("gmgn_trending", "gmgn-trending"),
        provider_row("gmgn_hot_search", "gmgn-hot"),
        provider_row("gmgn_kol", "gmgn-kol"),
        provider_row("gmgn_smart_money", "gmgn-wallet", wallet_address="0xwallet", direction="buy"),
        provider_row("gmgn_audit", "gmgn-audit", is_honeypot=False),
        provider_row("okx_new", "okx-new", evidence_role="discovery", signal_lane="new_launch"),
        provider_row("okx_signal", "okx-signal"),
        provider_row("okx_audit", "okx-audit", is_honeypot=False),
        provider_row("okx_developer", "okx-dev", developer_history={"launches": 4, "rugs": 1}),
        provider_row("985_monitor", "985-monitor", direction="sell", wallet_address="wallet-985", amount_usd=80),
        provider_row("985_fomo_wallets", "985-fomo"),
        provider_row("985_smartmoney", "985-smart", smart_wallet_online_count=12, wallet_addresses=["a", "b"]),
        provider_row("debot_rank", "debot-rank", evidence_role="ranking", signal_lane="trending"),
        provider_row("debot_safe_info", "debot-safe", evidence_role="audit", signal_lane="security_audit", buy_tax=None),
        provider_row("wind_monitor", "wind-post", post_text="BSC CA update", extracted_contracts=[TOKEN]),
        {**provider_row("gmgn_trending", "wrong-contract"), "contract_address": OTHER},
    ]

    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN, "symbol": "MEME", "name": "Meme"},
        rows,
        NOW,
    )

    assert record["identity"] == {
        "key": f"bsc:{TOKEN}",
        "chain": "bsc",
        "contract_address": TOKEN,
        "symbol": "MEME",
        "name": "Meme",
    }
    feeds = {item["provider_feed"] for item in record["source_events"]}
    assert feeds == {row["provider_feed"] for row in rows[:-1]}
    assert {item["provider_family"] for item in record["source_events"] if item["provider_feed"].startswith("985_")} == {"985"}
    assert all(item["identity_key"] == f"bsc:{TOKEN}" for item in record["source_events"])


def test_arcscan_optional_supply_holder_and_activity_evidence_keeps_provenance():
    record = evidence.build_evidence_record(
        {"chain": "arc-mainnet", "contract_address": TOKEN},
        [{
            "chain": "5042",
            "contract_address": TOKEN.upper().replace("0X", "0x"),
            "provider_feed": "arc_arcscan",
            "provider_event_id": "arcscan-token-1",
            "event_at": OLD,
            "observed_at": NOW,
            "holder_count": 37,
            "total_supply": 1_000_000,
            "first_transfer_block": 120,
            "last_transfer_block": 245,
            "transfer_count": 19,
            "activity_count": None,
        }],
        "2026-09-10T13:00:00+00:00",
    )

    facts = {item["field"]: item for item in record["market_snapshots"]}
    assert record["identity"]["key"] == f"arc:{TOKEN}"
    assert {name: facts[name]["value"] for name in (
        "holder_count", "total_supply", "first_transfer_block", "last_transfer_block", "transfer_count"
    )} == {
        "holder_count": 37,
        "total_supply": 1_000_000,
        "first_transfer_block": 120,
        "last_transfer_block": 245,
        "transfer_count": 19,
    }
    assert facts["activity_count"]["value"] is None
    assert facts["activity_count"]["status"] == "unknown"
    assert all(item["observed_at"] == NOW for item in facts.values())
    assert all(item["provider_feed"] == "arc_arcscan" for item in facts.values())


def test_unscoped_and_partially_scoped_provider_rows_remain_unbound():
    rows = [
        {"provider_feed": "gmgn_trending", "symbol": "MEME", "marketCap": 1},
        {"provider_feed": "gmgn_trending", "chain": "bsc", "symbol": "MEME", "marketCap": 2},
        {"provider_feed": "gmgn_trending", "contract_address": TOKEN, "symbol": "MEME", "marketCap": 3},
        {"provider_feed": "gmgn_trending", "chain": "bsc", "contract_address": OTHER, "marketCap": 4},
    ]

    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN, "symbol": "MEME"},
        rows,
        NOW,
    )

    for field in (
        "source_events",
        "market_snapshots",
        "wallet_events",
        "audit_facts",
        "developer_history",
        "social_evidence",
    ):
        assert record[field] == []


def test_market_audit_and_developer_facts_keep_field_level_provenance_and_unknowns():
    record = evidence.build_evidence_record(
        {"identity": {"chain": "bsc", "contract_address": TOKEN}},
        [
            provider_row(
                "gmgn_audit",
                "gmgn-audit",
                market_cap_usd=125_000,
                liquidity_usd=31_000,
                is_honeypot=None,
                upstream_provider="GoPlus Labs",
                source_url="https://gmgn.example/token",
            ),
            provider_row(
                "okx_developer",
                "okx-dev",
                developer_launch_count=7,
                developer_rug_count=None,
                creator_address="0xcreator",
            ),
        ],
        NOW,
    )

    market = {item["field"]: item for item in record["market_snapshots"]}
    audits = {item["audit_field"]: item for item in record["audit_facts"]}
    developer = {item["field"]: item for item in record["developer_history"]}

    assert market["market_cap_usd"]["value"] == 125_000
    assert market["market_cap_usd"]["source_url"] == "https://gmgn.example/token"
    assert market["liquidity_usd"]["fact_path"] == "market.liquidity_usd"
    assert audits["honeypot"]["value"] is None
    assert audits["honeypot"]["status"] == "unknown"
    assert audits["honeypot"]["upstream_provider"] == "goplus"
    assert developer["launch_count"]["value"] == 7
    assert developer["rug_count"]["value"] is None
    assert developer["rug_count"]["status"] == "unknown"
    assert developer["creator_address"]["value"] == "0xcreator"


def test_explicit_missing_market_value_is_unknown_not_known_zero():
    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        [provider_row("wind_monitor", "wind-empty-market", marketCap=None, volume=None)],
        NOW,
    )

    market = {item["field"]: item for item in record["market_snapshots"]}
    assert market["market_cap_usd"]["value"] is None
    assert market["market_cap_usd"]["status"] == "unknown"
    assert market["volume_24h_usd"]["value"] is None
    assert market["volume_24h_usd"]["status"] == "unknown"


def test_blank_direct_facts_are_normalized_to_unknown_none_across_families():
    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        [
            provider_row(
                "gmgn_audit",
                "blank-facts",
                marketCap="",
                is_honeypot="   ",
                developer_rug_count="",
            )
        ],
        NOW,
    )

    market = {item["field"]: item for item in record["market_snapshots"]}
    audits = {item["audit_field"]: item for item in record["audit_facts"]}
    developer = {item["field"]: item for item in record["developer_history"]}
    assert market["market_cap_usd"]["value"] is None
    assert market["market_cap_usd"]["status"] == "unknown"
    assert audits["honeypot"]["value"] is None
    assert audits["honeypot"]["status"] == "unknown"
    assert developer["rug_count"]["value"] is None
    assert developer["rug_count"]["status"] == "unknown"


def test_direct_builder_unifies_base_numeric_chain_aliases():
    record = evidence.build_evidence_record(
        {"chain": "base", "contract_address": TOKEN},
        [provider_row("gmgn_trending", "base-alias", chain="8453", marketCap=8453)],
        NOW,
    )

    assert record["identity"]["key"] == f"base:{TOKEN}"
    assert record["market_snapshots"][0]["value"] == 8453


@pytest.mark.parametrize("alias", ("arc", "5042", "0x13b2", "arc-mainnet", "arc_mainnet"))
def test_mapping_identity_collections_accept_every_arc_alias_with_unknown_facts(alias):
    record = evidence.build_evidence_record(
        {"chain": "arc", "contract_address": TOKEN},
        [provider_row(
            "arc_arcscan",
            f"arc-map-{alias}",
            chain=alias,
            contracts={alias: TOKEN.upper()},
            holder_count=None,
            activity_count=None,
        )],
        NOW,
    )

    facts = {item["field"]: item for item in record["market_snapshots"]}
    assert record["identity"]["key"] == f"arc:{TOKEN}"
    assert [item["provider_event_id"] for item in record["source_events"]] == [f"arc-map-{alias}"]
    assert facts["holder_count"]["value"] is None
    assert facts["holder_count"]["status"] == "unknown"
    assert facts["activity_count"]["value"] is None
    assert facts["activity_count"]["status"] == "unknown"
    assert all(item["identity_key"] == f"arc:{TOKEN}" for item in facts.values())


@pytest.mark.parametrize("chain,alias", (("bsc", "56"), ("robinhood", "4663")))
def test_mapping_identity_collection_keeps_existing_execution_chain_aliases(chain, alias):
    record = evidence.build_evidence_record(
        {"chain": chain, "contract_address": TOKEN},
        [provider_row(
            "gmgn_trending",
            f"{chain}-map",
            chain=alias,
            contracts={alias: TOKEN},
            marketCap=12_345,
        )],
        NOW,
    )

    assert record["identity"]["key"] == f"{chain}:{TOKEN}"
    assert record["market_snapshots"][0]["value"] == 12_345


def test_direct_rows_reject_nested_partial_and_malformed_identity_scopes_only():
    rows = [
        {
            **provider_row("gmgn_audit", "partial-profile", marketCap=111, is_honeypot=True),
            "profile": {"contract_address": OTHER},
        },
        {
            **provider_row("gmgn_audit", "foreign-extracted", marketCap=222, is_honeypot=True),
            "extracted_identity": {"chain": "bsc", "contract_address": OTHER},
        },
        {
            **provider_row("gmgn_audit", "foreign-contract-list", marketCap=333, is_honeypot=True),
            "contracts": [{"chain": "bsc", "contract_address": OTHER}],
        },
        {
            **provider_row("gmgn_audit", "malformed-key", marketCap=444, is_honeypot=True),
            "identity_key": "not-an-identity",
        },
        {
            **provider_row("gmgn_audit", "deep-partial", marketCap=445, is_honeypot=True),
            "payload": {"metadata": {"profile": {"contract_address": OTHER}}},
        },
        {
            **provider_row("gmgn_audit", "deep-malformed-key", marketCap=446, is_honeypot=True),
            "payload": {
                "metadata": {
                    "identity": {
                        "chain": "bsc",
                        "contract_address": TOKEN,
                        "key": "not-an-identity",
                    }
                }
            },
        },
        {
            **provider_row("gmgn_audit", "wrapped-foreign", marketCap=447, is_honeypot=True),
            "payload": {"chain": "bsc", "contract_address": OTHER},
        },
        {
            **provider_row("gmgn_audit", "wrapped-malformed-key", marketCap=448, is_honeypot=True),
            "payload": {"identity_key": "not-an-identity"},
        },
        {
            **provider_row("gmgn_trending", "valid-prose", marketCap=555),
            "description": f"comparison mentions bsc:{OTHER}",
            "source_url": f"https://example.test/post?contract={OTHER}",
        },
    ]

    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        rows,
        NOW,
    )

    assert [item["provider_event_id"] for item in record["source_events"]] == ["valid-prose"]
    assert [item["value"] for item in record["market_snapshots"]] == [555]
    assert record["audit_facts"] == []


def test_direct_token_shorthand_rejects_foreign_contracts_but_preserves_author_and_tx_metadata():
    foreign_scopes = [
        {"contract": OTHER},
        {"contracts": [OTHER]},
        {"contracts": {"bsc": OTHER}},
        {"profile": {"ca": OTHER}},
        {"extracted_identity": OTHER},
        {"payload": {"token": {"contract": OTHER}}},
    ]
    rows = [
        {
            **provider_row("gmgn_audit", f"foreign-short-{index}", marketCap=100 + index),
            **scope,
        }
        for index, scope in enumerate(foreign_scopes)
    ]
    rows.append(
        {
            **provider_row(
                "wind_monitor",
                "valid-context",
                marketCap=777,
                post_text="still local",
            ),
            "contract": TOKEN,
            "profile": {"ca": TOKEN},
            "extracted_identity": TOKEN,
            "author": {
                "network": "base",
                "address": OTHER,
                "profile": {"ca": OTHER},
                "handle": "caller",
            },
            "transaction": {
                "network": "base",
                "address": OTHER,
                "contract": OTHER,
                "tx_hash": "0xtx",
            },
        }
    )

    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        rows,
        NOW,
    )

    assert [item["provider_event_id"] for item in record["source_events"]] == ["valid-context"]
    assert record["market_snapshots"][0]["value"] == 777
    assert record["social_evidence"][0]["author"]["handle"] == "caller"


@pytest.mark.parametrize(
    "foreign_scope",
    [
        {"contract": OTHER},
        {"contracts": [OTHER]},
        {"contracts": {"bsc": OTHER}},
        {"profile": {"ca": OTHER}},
        {"extracted_identity": OTHER},
        {"payload": {"token": {"contract": OTHER}}},
    ],
)
def test_direct_target_rejects_foreign_token_shorthand(foreign_scope):
    with pytest.raises(ValueError, match="chain and contract"):
        evidence.build_evidence_record(
            {"chain": "bsc", "contract_address": TOKEN, **foreign_scope},
            [],
            NOW,
        )


def test_direct_target_keeps_author_and_transaction_address_metadata():
    target = {
        "chain": "bsc",
        "contract_address": TOKEN,
        "contract": TOKEN,
        "profile": {"symbol": "MEME", "description": "descriptive metadata"},
        "author": {
            "network": "base",
            "address": OTHER,
            "profile": {"ca": OTHER},
            "handle": "caller",
        },
        "transaction": {
            "network": "base",
            "address": OTHER,
            "contract": OTHER,
            "tx_hash": "0xtx",
        },
    }

    record = evidence.build_evidence_record(target, [], NOW)

    assert record["identity"]["key"] == f"bsc:{TOKEN}"


def test_direct_matching_contract_object_is_parsed_only_as_a_token_wrapper():
    contract = {"chain": "bsc", "contract_address": TOKEN}
    row = {
        **provider_row("gmgn_trending", "matching-contract-object", marketCap=919),
        "contract": contract,
    }

    provider_record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        [row],
        NOW,
    )
    target_record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN, "contract": contract},
        [],
        NOW,
    )

    assert [item["provider_event_id"] for item in provider_record["source_events"]] == [
        "matching-contract-object"
    ]
    assert provider_record["market_snapshots"][0]["value"] == 919
    assert target_record["identity"]["key"] == f"bsc:{TOKEN}"


def test_direct_audit_metadata_addresses_are_not_token_identity_claims():
    audit_context = {
        "audit": {"network": "base", "address": OTHER, "contract": OTHER},
        "audit_metadata": {"network": "base", "address": OTHER, "contract": OTHER},
    }
    row = {
        **provider_row("gmgn_audit", "audit-context", marketCap=929, is_honeypot=False),
        **audit_context,
    }

    provider_record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        [row],
        NOW,
    )
    target_record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN, **audit_context},
        [],
        NOW,
    )

    assert [item["provider_event_id"] for item in provider_record["source_events"]] == [
        "audit-context"
    ]
    assert provider_record["market_snapshots"][0]["value"] == 929
    assert provider_record["audit_facts"][0]["audit_field"] == "honeypot"
    assert target_record["identity"]["key"] == f"bsc:{TOKEN}"


def test_wallet_events_require_address_direction_and_event_time_not_aggregate_lists():
    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        [
            provider_row(
                "985_monitor",
                "trade-1",
                wallet_address="wallet-a",
                direction="SELL",
                amount=42,
                amount_usd=84,
                tx_hash="0xtx",
                source_url="https://985.example/tx/0xtx",
            ),
            provider_row(
                "985_smartmoney",
                "aggregate-1",
                smart_wallet_online_count=2,
                wallet_addresses=["wallet-b", "wallet-c"],
            ),
            provider_row("gmgn_smart_money", "missing-side", wallet_address="wallet-d"),
        ],
        NOW,
    )

    assert len(record["wallet_events"]) == 1
    wallet = record["wallet_events"][0]
    assert wallet["provider_family"] == "985"
    assert wallet["wallet_address"] == "wallet-a"
    assert wallet["direction"] == "sell"
    assert wallet["amount"] == 42
    assert wallet["amount_usd"] == 84
    assert wallet["tx_hash"] == "0xtx"
    assert wallet["verified_address_event"] is True


def test_debot_staleness_and_wind_social_attention_are_preserved():
    record = evidence.build_evidence_record(
        {"chain": "bsc", "contract_address": TOKEN},
        [
            provider_row(
                "debot_rank",
                "rank-old",
                event_at=OLD,
                observed_at=OLD,
                provenance_status="stale",
                source_url="https://debot.ai/rank",
            ),
            provider_row(
                "wind_monitor",
                "wind-text",
                event_at=OLD,
                post_text="The contract is live",
                author={"handle": "caller", "followers": 9000},
                original_url="https://social.example/post/1",
                referenced_url="https://project.example/launch",
                extracted_contracts=[TOKEN],
            ),
            provider_row(
                "wind_monitor",
                "wind-ca-only",
                event_at=OLD,
                post_text=None,
                author={"handle": "scanner"},
                original_url="https://social.example/post/2",
                extracted_contracts=[TOKEN],
            ),
        ],
        NOW,
    )

    debot_event = next(item for item in record["source_events"] if item["provider_event_id"] == "rank-old")
    assert debot_event["status"] == "stale"
    assert debot_event["event_at"] == OLD
    assert debot_event["observed_at"] == OLD

    social = {item["provider_event_id"]: item for item in record["social_evidence"]}
    assert social["wind-text"]["text"] == "The contract is live"
    assert social["wind-text"]["author"]["handle"] == "caller"
    assert social["wind-text"]["source_url"] == "https://social.example/post/1"
    assert social["wind-text"]["referenced_url"] == "https://project.example/launch"
    assert social["wind-text"]["original_text"] == "The contract is live"
    assert social["wind-text"]["referenced_text"] is None
    assert social["wind-text"]["extracted_contracts"] == [TOKEN]
    assert social["wind-text"]["attention_only"] is False
    assert social["wind-ca-only"]["text"] is None
    assert social["wind-ca-only"]["attention_only"] is True


def test_reference_only_wind_evidence_keeps_reference_origin_and_exact_identity():
    address = "0x4444444444444444444444444444444444444444"
    reference_url = "https://social.example/reference-only"
    record = evidence.build_evidence_record(
        {"chain": "base", "contract_address": address},
        [
            {
                "chain": "base",
                "contract_address": address,
                "provider_feed": "wind_monitor",
                "provider_event_id": "wind-reference-only",
                "event_at": OLD,
                "observed_at": NOW,
                "source_url": reference_url,
                "wind_original_url": "https://social.example/unrelated-original",
                "wind_referenced_url": reference_url,
                "wind_post_text": f"Base CA {address}",
                "wind_original_post_text": None,
                "wind_referenced_post_text": f"Base CA {address}",
                "wind_extracted_contracts": [address],
                "wind_extracted_contract_details": [
                    {"chain": "base", "address": address}
                ],
            }
        ],
        NOW,
    )

    social = record["social_evidence"][0]
    assert social["source_url"] == reference_url
    assert social["original_text"] is None
    assert social["referenced_text"] == f"Base CA {address}"
    assert social["extracted_identities"] == [
        {"chain": "base", "contract_address": address}
    ]
    assert social["extracted_identity"] == {
        "chain": "base",
        "contract_address": address,
    }


def test_existing_v3_audit_facts_are_retained_with_canonical_identity():
    record = evidence.build_evidence_record(
        {
            "identity": {
                "key": f"BSC:{TOKEN.upper()}",
                "chain": "BSC",
                "contract_address": TOKEN.upper(),
            },
            "audit_facts": [
                {
                    "identity_key": f"BSC:{TOKEN.upper()}",
                    "chain": "56",
                    "contract_address": TOKEN.upper(),
                    "audit_field": "honeypot",
                    "value": None,
                    "status": "unknown",
                    "provider_family": "gmgn",
                    "provider_feed": "gmgn_audit",
                    "provider_event_id": "audit-existing",
                    "observed_at": OLD,
                    "source_url": f"https://gmgn.example/audit?comparison={OTHER}",
                    "note": f"comparison prose mentions bsc:{OTHER}",
                    "raw_fingerprint": "audit-fingerprint",
                    "upstream_provider": "goplus",
                    "confidence": "provider_reported",
                }
            ],
        },
        [],
        NOW,
    )

    assert record["identity"]["key"] == f"bsc:{TOKEN}"
    assert len(record["audit_facts"]) == 1
    fact = record["audit_facts"][0]
    assert fact["audit_field"] == "honeypot"
    assert fact["value"] is None
    assert fact["status"] == "unknown"
    assert fact["observed_at"] == OLD
    assert fact["source_url"] == f"https://gmgn.example/audit?comparison={OTHER}"
    assert fact["raw_fingerprint"] == "audit-fingerprint"
    assert fact["upstream_provider"] == "goplus"
    assert fact["confidence"] == "provider_reported"
    assert fact["identity_key"] == f"bsc:{TOKEN}"


def test_existing_blank_audit_value_cannot_remain_known():
    record = evidence.build_evidence_record(
        {
            "identity": {"chain": "bsc", "contract_address": TOKEN},
            "audit_facts": [
                {
                    "identity_key": f"bsc:{TOKEN}",
                    "chain": "bsc",
                    "contract_address": TOKEN,
                    "audit_field": "honeypot",
                    "value": "   ",
                    "status": "known",
                    "provider_family": "gmgn",
                    "provider_feed": "gmgn_audit",
                    "provider_event_id": "blank-existing-audit",
                    "observed_at": OLD,
                }
            ],
        },
        [],
        NOW,
    )

    assert record["audit_facts"][0]["value"] is None
    assert record["audit_facts"][0]["status"] == "unknown"


def test_existing_audit_facts_reject_foreign_conflicting_and_unscoped_identities():
    common = {
        "audit_field": "honeypot",
        "value": None,
        "status": "unknown",
        "provider_family": "gmgn",
        "provider_feed": "gmgn_audit",
        "observed_at": OLD,
        "upstream_provider": "goplus",
        "confidence": "provider_reported",
    }
    record = evidence.build_evidence_record(
        {
            "identity": {"chain": "bsc", "contract_address": TOKEN},
            "audit_facts": [
                {
                    **common,
                    "provider_event_id": "foreign-contract",
                    "chain": "bsc",
                    "contract_address": OTHER,
                    "identity_key": f"bsc:{OTHER}",
                },
                {
                    **common,
                    "provider_event_id": "foreign-chain",
                    "chain": "robinhood",
                    "contract_address": TOKEN,
                    "identity_key": f"robinhood:{TOKEN}",
                },
                {
                    **common,
                    "provider_event_id": "conflicting-key",
                    "chain": "bsc",
                    "contract_address": TOKEN,
                    "identity_key": f"bsc:{OTHER}",
                },
                {
                    **common,
                    "provider_event_id": "partial-profile",
                    "chain": "bsc",
                    "contract_address": TOKEN,
                    "profile": {"contract_address": OTHER},
                },
                {
                    **common,
                    "provider_event_id": "foreign-extracted",
                    "chain": "bsc",
                    "contract_address": TOKEN,
                    "extracted_identity": {"chain": "bsc", "contract_address": OTHER},
                },
                {
                    **common,
                    "provider_event_id": "foreign-contract-list",
                    "chain": "bsc",
                    "contract_address": TOKEN,
                    "contracts": [{"chain": "bsc", "contract_address": OTHER}],
                },
                {
                    **common,
                    "provider_event_id": "malformed-key",
                    "chain": "bsc",
                    "contract_address": TOKEN,
                    "identity_key": "not-an-identity",
                },
                {**common, "provider_event_id": "unscoped"},
            ],
        },
        [],
        NOW,
    )

    assert record["audit_facts"] == []
