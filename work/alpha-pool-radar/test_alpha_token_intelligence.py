import copy

import pytest

import alpha_token_intelligence as intelligence
import alpha_radar_report as report_pipeline
from alpha_token_intelligence import build_token_intelligence, build_token_intelligence_records, normalize_identity


NOW = "2026-09-10T12:00:00+00:00"
TOKEN_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WALLET = "0x9999999999999999999999999999999999999999"
EVIDENCE_ARRAY_FIELDS = (
    "source_events",
    "market_snapshots",
    "wallet_events",
    "audit_facts",
    "developer_history",
    "social_evidence",
)


def token_view(contract=TOKEN_A, *, chain="bsc", symbol="SAME"):
    return {
        "identity": {
            "chain": chain,
            "contract_address": contract,
            "symbol": symbol,
            "name": f"{symbol} token",
        }
    }


def provider_row(contract=TOKEN_A, *, chain="bsc", event_id="event-a", **extra):
    return {
        "chain": chain,
        "contract_address": contract,
        "provider_feed": "gmgn_trending",
        "provider_event_id": event_id,
        "event_at": NOW,
        "observed_at": NOW,
        **extra,
    }


def evidence_items(record):
    return [item for field in EVIDENCE_ARRAY_FIELDS for item in record[field]]


def test_normalize_identity_uses_chain_and_contract_without_destroying_solana_case():
    assert normalize_identity("56", " 0xABCdef ") == ("bsc", "0xabcdef")
    assert normalize_identity("SOL", "AbCdMint") == ("solana", "AbCdMint")

    with pytest.raises(ValueError, match="chain and contract"):
        normalize_identity("bsc", "")


def test_arc_intelligence_keeps_optional_arcscan_facts_unknown_instead_of_zero():
    record = build_token_intelligence(
        token_view(chain="0x13b2"),
        [provider_row(
            chain="arc_mainnet",
            event_id="arcscan-token",
            provider_feed="arc_arcscan",
            holder_count=None,
            total_supply=500_000,
            first_transfer_block=100,
            last_transfer_block=180,
            activity_count=None,
        )],
        observed_at=NOW,
    )

    assert record["identity"]["key"] == f"arc:{TOKEN_A}"
    assert record["deterministic"]["market"]["holder_count"] is None
    assert record["deterministic"]["market_status"]["holder_count"] == "unknown"
    assert record["deterministic"]["market"]["total_supply"] == 500_000
    assert record["deterministic"]["market"]["first_transfer_block"] == 100
    assert record["deterministic"]["market"]["last_transfer_block"] == 180
    assert record["deterministic"]["market"]["activity_count"] is None
    assert record["deterministic"]["market_status"]["activity_count"] == "unknown"
    assert {
        item["observed_at"] for item in record["market_snapshots"]
    } == {NOW}


def test_new_record_is_schema_v2_with_explicit_unknowns_and_pending_ai():
    record = build_token_intelligence(
        token_view(),
        [provider_row(marketCap=125_000, liquidity=None)],
        observed_at=NOW,
    )

    assert record["schema_version"] == 2
    assert record["identity"] == {
        "key": f"bsc:{TOKEN_A}",
        "chain": "bsc",
        "contract_address": TOKEN_A,
        "symbol": "SAME",
        "name": "SAME token",
    }
    assert record["record_version"] == 1
    assert len(record["evidence_set_hash"]) == 64
    assert record["fast_generated_at"] == NOW
    assert record["deterministic"]["market"]["market_cap_usd"] == 125_000
    assert record["deterministic"]["market"]["liquidity_usd"] is None
    assert record["deterministic"]["market"]["price_usd"] is None
    assert record["deterministic"]["audit"]["honeypot"] is None
    assert record["deterministic"]["developer"]["rug_count"] is None
    assert record["ai"] == {
        "status": "pending",
        "requested_at": None,
        "analyzed_at": None,
    }


def test_same_symbol_contracts_and_cross_chain_rows_never_merge_evidence():
    views = [
        token_view(TOKEN_A, chain="bsc"),
        token_view(TOKEN_B, chain="bsc"),
        token_view(TOKEN_A, chain="robinhood"),
    ]
    rows = [
        provider_row(TOKEN_A, chain="bsc", event_id="bsc-a", marketCap=101),
        provider_row(TOKEN_B, chain="bsc", event_id="bsc-b", marketCap=202),
        provider_row(TOKEN_A, chain="robinhood", event_id="rh-a", marketCap=303),
        {"symbol": "SAME", "provider_feed": "gmgn_trending", "marketCap": 999},
    ]

    records = build_token_intelligence_records(views, provider_rows=rows, observed_at=NOW)
    by_identity = {record["identity"]["key"]: record for record in records}

    assert set(by_identity) == {
        f"bsc:{TOKEN_A}",
        f"bsc:{TOKEN_B}",
        f"robinhood:{TOKEN_A}",
    }
    assert by_identity[f"bsc:{TOKEN_A}"]["deterministic"]["market"]["market_cap_usd"] == 101
    assert by_identity[f"bsc:{TOKEN_B}"]["deterministic"]["market"]["market_cap_usd"] == 202
    assert by_identity[f"robinhood:{TOKEN_A}"]["deterministic"]["market"]["market_cap_usd"] == 303

    id_sets = [{item["evidence_id"] for item in evidence_items(record)} for record in records]
    assert all(left.isdisjoint(right) for index, left in enumerate(id_sets) for right in id_sets[index + 1 :])
    assert all(
        item["identity_key"] == record["identity"]["key"]
        for record in records
        for item in evidence_items(record)
    )


def test_every_evidence_id_is_unique_and_resolves_inside_its_record():
    row = provider_row(
        event_id="complete-event",
        marketCap=125_000,
        wallet_address=WALLET,
        direction="buy",
        amount_usd=450,
        is_honeypot=False,
        developer_rug_count=0,
        post_text="SAME is getting attention",
        extracted_contracts=[TOKEN_A],
    )
    record = build_token_intelligence(token_view(), [row], observed_at=NOW)
    items = evidence_items(record)
    ids = [item["evidence_id"] for item in items]

    assert ids
    assert len(ids) == len(set(ids))
    assert record["deterministic"]["evidence_ids"] == sorted(ids)
    registry = {item["evidence_id"]: item for item in items}
    assert all(registry[evidence_id]["identity_key"] == record["identity"]["key"] for evidence_id in ids)


def test_evidence_hash_is_order_independent_and_record_version_increments():
    rows = [
        provider_row(event_id="first", marketCap=100),
        provider_row(event_id="second", liquidity=50),
    ]
    first = build_token_intelligence(token_view(), rows, observed_at=NOW)
    reordered = build_token_intelligence(token_view(), list(reversed(rows)), observed_at=NOW)
    next_record = build_token_intelligence(token_view(), rows, observed_at=NOW, previous=first)
    changed = build_token_intelligence(
        token_view(),
        [rows[0], {**rows[1], "liquidity": 51}],
        observed_at=NOW,
        previous=next_record,
    )

    assert reordered["evidence_set_hash"] == first["evidence_set_hash"]
    assert next_record["record_version"] == 2
    assert next_record["evidence_set_hash"] == first["evidence_set_hash"]
    assert changed["record_version"] == 3
    assert changed["evidence_set_hash"] != first["evidence_set_hash"]


def test_evidence_hash_changes_when_fact_changes_despite_reused_raw_fingerprint():
    first_row = provider_row(event_id="same-event", marketCap=100, raw_fingerprint="trusted-but-stale")
    changed_row = {**first_row, "marketCap": 200}

    first = build_token_intelligence(token_view(), [first_row], observed_at=NOW)
    changed = build_token_intelligence(token_view(), [changed_row], observed_at=NOW)

    assert first["market_snapshots"][0]["evidence_id"] == changed["market_snapshots"][0]["evidence_id"]
    assert first["evidence_set_hash"] != changed["evidence_set_hash"]


def test_base_numeric_alias_is_canonicalized_before_evidence_builder():
    record = build_token_intelligence(
        token_view(chain="base"),
        [provider_row(chain="8453", marketCap=8453)],
        observed_at=NOW,
    )

    assert record["identity"]["key"] == f"base:{TOKEN_A}"
    assert record["deterministic"]["market"]["market_cap_usd"] == 8453
    assert record["market_snapshots"][0]["identity_key"] == f"base:{TOKEN_A}"


def test_embedded_evidence_requires_its_own_matching_normalized_identity():
    view = token_view()
    view["audit_facts"] = [
        {
            "identity_key": f"bsc:{TOKEN_B}",
            "chain": "bsc",
            "contract_address": TOKEN_B,
            "audit_field": "honeypot",
            "value": True,
            "provider_family": "gmgn",
            "provider_feed": "gmgn_audit",
            "provider_event_id": "foreign-audit",
            "observed_at": NOW,
        }
    ]
    view["events"] = [
        provider_row(TOKEN_B, event_id="foreign-event", marketCap=999),
        {
            **provider_row(TOKEN_A, event_id="conflicting-profile", marketCap=888),
            "profile": {"chain": "base", "contract_address": TOKEN_B},
        },
    ]

    record = build_token_intelligence(view, [], observed_at=NOW)

    assert record["audit_facts"] == []
    assert record["source_events"] == []
    assert record["market_snapshots"] == []


def test_matching_embedded_audit_is_retained_without_identity_reassignment():
    view = token_view()
    view["audit_facts"] = [
        {
            "identity_key": f"BSC:{TOKEN_A.upper()}",
            "chain": "56",
            "contract_address": TOKEN_A.upper(),
            "audit_field": "honeypot",
            "value": False,
            "provider_family": "gmgn",
            "provider_feed": "gmgn_audit",
            "provider_event_id": "local-audit",
            "observed_at": NOW,
        }
    ]

    record = build_token_intelligence(view, [], observed_at=NOW)

    assert len(record["audit_facts"]) == 1
    fact = record["audit_facts"][0]
    assert fact["identity_key"] == f"bsc:{TOKEN_A}"
    assert fact["chain"] == "bsc"
    assert fact["contract_address"] == TOKEN_A


def test_partial_nested_and_malformed_identities_fail_closed_but_prose_is_ignored():
    rows = [
        {
            **provider_row(event_id="partial-profile", marketCap=111, is_honeypot=True),
            "profile": {"contract_address": TOKEN_B},
        },
        {
            **provider_row(event_id="partial-contract", marketCap=222, is_honeypot=True),
            "contracts": [{"chain": "bsc"}],
        },
        {
            **provider_row(event_id="malformed-key", marketCap=333, is_honeypot=True),
            "identity_key": "not-an-identity",
        },
        {
            **provider_row(event_id="deep-partial", marketCap=334, is_honeypot=True),
            "payload": {"metadata": {"profile": {"contract_address": TOKEN_B}}},
        },
        {
            **provider_row(event_id="deep-malformed-key", marketCap=335, is_honeypot=True),
            "payload": {
                "metadata": {
                    "identity": {
                        "chain": "bsc",
                        "contract_address": TOKEN_A,
                        "key": "not-an-identity",
                    }
                }
            },
        },
        {
            **provider_row(event_id="wrapped-foreign", marketCap=336, is_honeypot=True),
            "payload": {"chain": "bsc", "contract_address": TOKEN_B},
        },
        {
            **provider_row(event_id="wrapped-malformed-key", marketCap=337, is_honeypot=True),
            "payload": {"identity_key": "not-an-identity"},
        },
        {
            **provider_row(event_id="valid-prose", marketCap=444),
            "description": f"comparison text mentions bsc:{TOKEN_B}",
            "source_url": f"https://example.test/research?contract={TOKEN_B}",
        },
    ]

    record = build_token_intelligence(token_view(), rows, observed_at=NOW)

    assert [item["provider_event_id"] for item in record["source_events"]] == ["valid-prose"]
    assert [item["value"] for item in record["market_snapshots"]] == [444]
    assert record["audit_facts"] == []


def test_token_shorthand_scopes_reject_foreign_contracts_but_keep_author_and_transaction_metadata():
    foreign_scopes = [
        {"contract": TOKEN_B},
        {"contracts": [TOKEN_B]},
        {"contracts": {"bsc": TOKEN_B}},
        {"profile": {"ca": TOKEN_B}},
        {"extracted_identity": TOKEN_B},
        {"payload": {"token": {"contract": TOKEN_B}}},
    ]
    rows = [
        {**provider_row(event_id=f"foreign-{index}", marketCap=100 + index), **scope}
        for index, scope in enumerate(foreign_scopes)
    ]
    rows.append(
        {
            **provider_row(
                event_id="valid-context",
                provider_feed="wind_monitor",
                marketCap=777,
                post_text="still local",
            ),
            "contract": TOKEN_A,
            "profile": {"ca": TOKEN_A},
            "extracted_identity": TOKEN_A,
            "author": {
                "network": "base",
                "address": TOKEN_B,
                "profile": {"ca": TOKEN_B},
                "handle": "caller",
            },
            "transaction": {
                "network": "base",
                "address": TOKEN_B,
                "contract": TOKEN_B,
                "tx_hash": "0xtx",
            },
        }
    )

    record = build_token_intelligence(token_view(), rows, observed_at=NOW)

    assert [item["provider_event_id"] for item in record["source_events"]] == ["valid-context"]
    assert record["market_snapshots"][0]["value"] == 777
    assert record["social_evidence"][0]["author"]["handle"] == "caller"


@pytest.mark.parametrize(
    "foreign_scope",
    [
        {"contract": TOKEN_B},
        {"contracts": [TOKEN_B]},
        {"contracts": {"bsc": TOKEN_B}},
        {"profile": {"ca": TOKEN_B}},
        {"extracted_identity": TOKEN_B},
        {"payload": {"token": {"contract": TOKEN_B}}},
    ],
)
def test_target_and_v1_paths_reject_foreign_token_shorthand(foreign_scope):
    with pytest.raises(ValueError, match="chain and contract|conflicts"):
        build_token_intelligence({**token_view(), **foreign_scope}, [], observed_at=NOW)

    with pytest.raises(ValueError, match="chain and contract|conflicts"):
        intelligence.adapt_v1_record(
            {
                "schema_version": 1,
                "chain": "bsc",
                "contract_address": TOKEN_A,
                **foreign_scope,
            },
            f"bsc:{TOKEN_A}",
        )


def test_target_and_v1_paths_preserve_non_token_author_and_transaction_addresses():
    context = {
        "contract": TOKEN_A,
        "profile": {"symbol": "SAME", "description": "descriptive metadata"},
        "author": {
            "network": "base",
            "address": TOKEN_B,
            "profile": {"ca": TOKEN_B},
            "handle": "caller",
        },
        "transaction": {
            "network": "base",
            "address": TOKEN_B,
            "contract": TOKEN_B,
            "tx_hash": "0xtx",
        },
    }

    record = build_token_intelligence({**token_view(), **context}, [], observed_at=NOW)
    adapted = intelligence.adapt_v1_record(
        {"schema_version": 1, "chain": "bsc", "contract_address": TOKEN_A, **context}
    )

    assert record["identity"]["key"] == f"bsc:{TOKEN_A}"
    assert adapted["author"] == context["author"]
    assert adapted["transaction"] == context["transaction"]


def test_matching_contract_object_is_retained_in_provider_target_and_v1_paths():
    contract = {"chain": "bsc", "contract_address": TOKEN_A}
    provider = {
        **provider_row(event_id="matching-contract-object", marketCap=939),
        "contract": contract,
    }

    provider_record = build_token_intelligence(token_view(), [provider], observed_at=NOW)
    target_record = build_token_intelligence(
        {**token_view(), "contract": contract},
        [],
        observed_at=NOW,
    )
    adapted = intelligence.adapt_v1_record(
        {
            "schema_version": 1,
            "chain": "bsc",
            "contract_address": TOKEN_A,
            "contract": contract,
            "status": "partial",
        }
    )

    assert [item["provider_event_id"] for item in provider_record["source_events"]] == [
        "matching-contract-object"
    ]
    assert provider_record["deterministic"]["market"]["market_cap_usd"] == 939
    assert target_record["identity"]["key"] == f"bsc:{TOKEN_A}"
    assert adapted["identity"]["key"] == f"bsc:{TOKEN_A}"
    assert adapted["contract"] == contract


def test_audit_metadata_addresses_are_retained_in_provider_target_and_v1_paths():
    audit_context = {
        "audit": {"network": "base", "address": TOKEN_B, "contract": TOKEN_B},
        "audit_metadata": {
            "network": "base",
            "address": TOKEN_B,
            "contract": TOKEN_B,
        },
    }
    provider = {
        **provider_row(event_id="audit-context", marketCap=949, is_honeypot=False),
        **audit_context,
    }

    provider_record = build_token_intelligence(token_view(), [provider], observed_at=NOW)
    target_record = build_token_intelligence(
        {**token_view(), **audit_context},
        [],
        observed_at=NOW,
    )
    adapted = intelligence.adapt_v1_record(
        {
            "schema_version": 1,
            "chain": "bsc",
            "contract_address": TOKEN_A,
            **audit_context,
        }
    )

    assert [item["provider_event_id"] for item in provider_record["source_events"]] == [
        "audit-context"
    ]
    assert provider_record["deterministic"]["market"]["market_cap_usd"] == 949
    assert provider_record["deterministic"]["audit"]["honeypot"] is False
    assert target_record["identity"]["key"] == f"bsc:{TOKEN_A}"
    assert adapted["audit"] == audit_context["audit"]
    assert adapted["audit_metadata"] == audit_context["audit_metadata"]


def test_empty_string_facts_become_unknown_none_in_evidence_and_summary():
    record = build_token_intelligence(
        token_view(),
        [provider_row(marketCap="")],
        observed_at=NOW,
    )

    snapshot = record["market_snapshots"][0]
    assert snapshot["value"] is None
    assert snapshot["status"] == "unknown"
    assert record["deterministic"]["market"]["market_cap_usd"] is None
    assert record["deterministic"]["market_status"]["market_cap_usd"] == "unknown"


def test_v1_read_adapter_is_non_mutating_and_returns_v2_read_shape():
    v1 = {
        "schema_version": 1,
        "status": "partial",
        "identity": {
            "chain": "56",
            "contract_address": TOKEN_A.upper().replace("0X", "0x"),
        },
        "symbol": "OLD",
        "name": "Old token",
        "observed_at": NOW,
        "market_evidence": {"market_cap_usd": 88_000, "liquidity_usd": None},
        "smart_wallet_evidence": None,
    }
    original = copy.deepcopy(v1)

    adapted = intelligence.adapt_v1_record(v1)

    assert v1 == original
    assert adapted["schema_version"] == 2
    assert adapted["identity"]["key"] == f"bsc:{TOKEN_A}"
    assert adapted["record_version"] == 1
    assert adapted["fast_generated_at"] == NOW
    assert adapted["deterministic"]["market"]["market_cap_usd"] == 88_000
    assert adapted["deterministic"]["market"]["liquidity_usd"] is None
    assert adapted["source_events"] == []
    assert adapted["ai"]["status"] == "pending"
    assert intelligence.read_token_intelligence_record(adapted) == adapted
    assert intelligence.read_token_intelligence_record(adapted) is not adapted


def test_v1_adapter_preserves_legacy_contract_without_fabricating_evidence():
    v1 = {
        "schema_version": 1,
        "status": "partial",
        "identity": {"chain": "bsc", "contract_address": TOKEN_A},
        "market_evidence": {"market_cap_usd": 91_000},
        "smart_wallet_evidence": {"qualified_wallet_count": 2, "net_flow_usd": 500},
        "official_identity": {"status": "mismatch", "contracts": [{"contract_address": TOKEN_B}]},
        "same_symbol_contracts": [{"chain": "bsc", "contract_address": TOKEN_B}],
        "missing_evidence": ["official_identity"],
        "source_references": [{"source": "legacy", "url": "https://example.test/source"}],
        "fallback_summary": "legacy fallback",
        "risks": ["legacy risk"],
        "one_line_judgement": "legacy judgement",
    }

    adapted = intelligence.adapt_v1_record(v1)

    assert adapted["legacy_unattributed"]["identity"] == v1["identity"]
    for field in (
        "status",
        "market_evidence",
        "smart_wallet_evidence",
        "official_identity",
        "same_symbol_contracts",
        "missing_evidence",
        "source_references",
        "fallback_summary",
        "risks",
        "one_line_judgement",
    ):
        assert adapted[field] == v1[field]
        assert adapted["legacy_unattributed"][field] == v1[field]
    assert evidence_items(adapted) == []
    assert adapted["deterministic"]["evidence_ids"] == []


def test_v1_adapter_normalizes_blank_facts_to_none_and_unknown():
    v1 = {
        "schema_version": 1,
        "identity": {"chain": "bsc", "contract_address": TOKEN_A},
        "market_evidence": {"market_cap_usd": "", "liquidity_usd": "   "},
    }

    adapted = intelligence.adapt_v1_record(v1)

    assert adapted["market_evidence"]["market_cap_usd"] is None
    assert adapted["market_evidence"]["liquidity_usd"] is None
    assert adapted["legacy_unattributed"]["market_evidence"]["market_cap_usd"] is None
    assert adapted["deterministic"]["market"]["market_cap_usd"] is None
    assert adapted["deterministic"]["market_status"]["market_cap_usd"] == "unknown"


def test_v1_cache_envelope_reads_records_by_identity_key_without_rewriting_source():
    cache = {
        "schema_version": 1,
        "generated_at": NOW,
        "records": {
            f"bsc:{TOKEN_A}": {
                "symbol": "OLD",
                "contract_address": TOKEN_A,
                "market_evidence": {"market_cap_usd": 77_000},
            }
        },
    }
    original = copy.deepcopy(cache)

    adapted = intelligence.read_token_intelligence_cache(cache)

    assert cache == original
    assert adapted["schema_version"] == 2
    assert adapted["generated_at"] == NOW
    assert set(adapted["records"]) == {f"bsc:{TOKEN_A}"}
    record = adapted["records"][f"bsc:{TOKEN_A}"]
    assert record["identity"]["key"] == f"bsc:{TOKEN_A}"
    assert record["deterministic"]["market"]["market_cap_usd"] == 77_000


def test_key_assisted_v1_migration_advances_record_version():
    key = f"bsc:{TOKEN_A}"
    previous = {
        "schema_version": 1,
        "record_version": 4,
        "market_evidence": {"market_cap_usd": 50_000},
    }

    records = build_token_intelligence_records(
        [token_view()],
        provider_rows=[provider_row(marketCap=60_000)],
        previous_records={key: previous},
        observed_at=NOW,
    )

    assert records[0]["record_version"] == 5


def test_v2_record_keeps_report_boundary_compatibility_fields():
    row = {
        "chain": "bsc",
        "contract_address": TOKEN_A,
        "symbol": "LIVE",
        "market_cap": 125_000,
        "liquidity": 48_000,
        "volume24h": 91_000,
        "quote_source": "dexscreener",
        "quote_observed_at": NOW,
    }
    core = build_token_intelligence(row, observed_at=NOW)

    assert core["chain"] == "bsc"
    assert core["contract_address"] == TOKEN_A
    assert core["market_evidence"]["market_cap_usd"] == 125_000
    assert "smart_wallet_evidence" in core
    assert "official_identity" in core
    assert report_pipeline._token_intelligence_research_targets(
        {"meme_rows": [row]}, [core], 1
    ) == [f"bsc:{TOKEN_A}"]
    ui_record = report_pipeline._ui_record(core, NOW)
    assert ui_record["attention_evidence"][0] == "市值 125000.0 美元"


def test_conflicting_identity_scopes_fail_closed():
    conflicted = {
        **token_view(),
        "chain": "robinhood",
        "contract_address": TOKEN_B,
    }

    with pytest.raises(ValueError, match="chain and contract"):
        build_token_intelligence(conflicted, [], observed_at=NOW)

    assert build_token_intelligence_records([conflicted], provider_rows=[], observed_at=NOW) == []
