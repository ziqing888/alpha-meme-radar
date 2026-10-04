import argparse
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path


BASE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("alpha_wind_monitor_export", BASE / "alpha_wind_monitor_export.py")
wind = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = wind
SPEC.loader.exec_module(wind)


def test_normalize_event_extracts_evm_ca_from_wind_payload():
    event = {
        "id": "evt-1",
        "seq": 88,
        "platform": "fomo",
        "action": "fomo_buy",
        "handle": "unipcs",
        "payload": {
            "content_text": "buy $BEN on BSC 0x7777777777777777777777777777777777777777",
            "created_at": "2026-09-10T11:00:00Z",
            "author": {"id": "author-1", "name": "Uni", "followers": 12000, "verified": True},
            "reference": {"tweet_url": "https://social.example/reference/2"},
            "extra": {"fomo": {"amount_usd": 2400, "market_cap": 120000}},
            "tweet_url": "https://wind.jokkimon.club/e/evt-1",
        },
    }

    rows = wind.normalize_event(event)

    assert len(rows) == 1
    row = rows[0]
    assert row["source_family"] == "wind_monitor"
    assert row["chain"] == "bsc"
    assert row["address"] == "0x7777777777777777777777777777777777777777"
    assert row["symbol"] == "BEN"
    assert row["smart_money"] == 1
    assert row["marketCap"] == 120000
    assert row["volume"] == 2400
    assert row["provider_family"] == "wind"
    assert row["provider_feed"] == "wind_monitor"
    assert row["provider_event_id"] == "evt-1"
    assert row["event_at"] == "2026-09-10T11:00:00Z"
    assert row["source_url"] == "https://wind.jokkimon.club/e/evt-1"
    assert row["wind_post_text"] == "buy $BEN on BSC 0x7777777777777777777777777777777777777777"
    assert row["wind_author"] == {
        "id": "author-1",
        "handle": "unipcs",
        "name": "Uni",
        "followers": 12000,
        "verified": True,
        "platform": "fomo",
    }
    assert row["wind_original_url"] == "https://wind.jokkimon.club/e/evt-1"
    assert row["wind_referenced_url"] == "https://social.example/reference/2"
    assert row["wind_extracted_contracts"] == ["0x7777777777777777777777777777777777777777"]
    assert row["wind_attention_only"] is False


def test_normalize_event_keeps_ca_only_event_as_attention_only():
    event = {
        "id": "evt-ca-only",
        "platform": "fomo",
        "action": "tweet",
        "handle": "scanner",
        "ca_info": {"chain": "bsc", "address": "0x7777777777777777777777777777777777777777"},
        "payload": {"tweet_url": "https://wind.jokkimon.club/e/evt-ca-only"},
    }

    rows = wind.normalize_event(event)

    assert len(rows) == 1
    assert rows[0]["wind_post_text"] is None
    assert rows[0]["wind_attention_only"] is True
    assert rows[0]["wind_extracted_contracts"] == ["0x7777777777777777777777777777777777777777"]
    assert rows[0]["marketCap"] is None
    assert rows[0]["volume"] is None


def test_structured_ca_does_not_receive_text_that_mentions_another_ca():
    structured = "0x1111111111111111111111111111111111111111"
    body_ca = "0x2222222222222222222222222222222222222222"
    event = {
        "id": "evt-conflict",
        "platform": "fomo",
        "action": "quote",
        "ca_info": {"chain": "bsc", "address": structured},
        "payload": {
            "content_text": f"BSC contract is {body_ca}",
            "reference_text": f"BSC archived contract is {structured}",
            "tweet_url": "https://social.example/original",
            "reference": {"tweet_url": "https://social.example/referenced"},
        },
    }

    rows = {row["address"].lower(): row for row in wind.normalize_event(event)}

    assert set(rows) == {structured, body_ca}
    assert rows[structured]["wind_original_post_text"] is None
    assert rows[structured]["wind_referenced_post_text"] == f"BSC archived contract is {structured}"
    assert rows[structured]["source_url"] == "https://social.example/referenced"
    assert rows[body_ca]["wind_original_post_text"] == f"BSC contract is {body_ca}"
    assert rows[body_ca]["wind_referenced_post_text"] is None
    assert rows[body_ca]["source_url"] == "https://social.example/original"


def test_chainless_evm_ca_remains_unbound():
    address = "0x3333333333333333333333333333333333333333"

    rows = wind.normalize_event(
        {
            "id": "evt-unbound",
            "platform": "fomo",
            "action": "tweet",
            "payload": {"content_text": f"new CA {address}"},
        }
    )

    assert len(rows) == 1
    assert rows[0]["address"] == address
    assert rows[0]["chain"] is None
    assert rows[0]["identity_status"] == "unbound"


def test_same_address_on_two_chains_keeps_each_post_origin_separate():
    address = "0x5555555555555555555555555555555555555555"
    event = {
        "id": "evt-cross-chain",
        "platform": "fomo",
        "action": "quote",
        "payload": {
            "content_text": f"BSC CA {address}",
            "reference_text": f"Base CA {address}",
            "tweet_url": "https://social.example/bsc-post",
            "reference": {"tweet_url": "https://social.example/base-post"},
        },
    }

    rows = {(row["chain"], row["address"].lower()): row for row in wind.normalize_event(event)}

    bsc = rows[("bsc", address)]
    base = rows[("base", address)]
    assert bsc["wind_original_post_text"] == f"BSC CA {address}"
    assert bsc["wind_referenced_post_text"] is None
    assert bsc["source_url"] == "https://social.example/bsc-post"
    assert base["wind_original_post_text"] is None
    assert base["wind_referenced_post_text"] == f"Base CA {address}"
    assert base["source_url"] == "https://social.example/base-post"


def test_multiple_chain_contract_statements_are_scoped_independently():
    bsc_ca = "0x6666666666666666666666666666666666666666"
    base_ca = "0x7777777777777777777777777777777777777777"
    text = f"BSC {bsc_ca}; Base {base_ca}"

    rows = {row["address"].lower(): row for row in wind.normalize_event(
        {
            "id": "evt-multi-chain",
            "platform": "fomo",
            "action": "tweet",
            "payload": {"content_text": text, "tweet_url": "https://social.example/multi"},
        }
    )}

    assert rows[bsc_ca]["chain"] == "bsc"
    assert rows[base_ca]["chain"] == "base"
    assert rows[bsc_ca]["wind_original_post_text"] == f"BSC {bsc_ca}"
    assert rows[base_ca]["wind_original_post_text"] == f"Base {base_ca}"


def test_suffix_labeled_mixed_chain_contracts_bind_and_scope_each_ca():
    bsc_ca = "0x9999999999999999999999999999999999999999"
    base_ca = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    text = f"CA {bsc_ca} on BSC, CA {base_ca} on Base"

    rows = {
        (row["chain"], row["address"].lower()): row
        for row in wind.normalize_event(
            {
                "id": "evt-suffix-multi-chain",
                "platform": "fomo",
                "action": "tweet",
                "payload": {"content_text": text, "tweet_url": "https://social.example/suffix-multi"},
            }
        )
    }

    bsc_row = rows[("bsc", bsc_ca)]
    base_row = rows[("base", base_ca)]
    assert bsc_row["wind_original_post_text"] == f"CA {bsc_ca} on BSC"
    assert base_row["wind_original_post_text"] == f"CA {base_ca} on Base"
    assert base_ca not in bsc_row["wind_original_post_text"]
    assert bsc_ca not in base_row["wind_original_post_text"]


def test_reference_only_post_is_not_attributed_as_original_text():
    address = "0x8888888888888888888888888888888888888888"
    row = wind.normalize_event(
        {
            "id": "evt-reference-only",
            "platform": "fomo",
            "action": "quote",
            "payload": {
                "reference_text": f"Base CA {address}",
                "tweet_url": "https://social.example/empty-original",
                "reference": {"tweet_url": "https://social.example/reference-only"},
            },
        }
    )[0]

    assert row["chain"] == "base"
    assert row["wind_original_post_text"] is None
    assert row["wind_referenced_post_text"] == f"Base CA {address}"
    assert row["source_url"] == "https://social.example/reference-only"


def test_run_once_records_feed_429_without_overwriting_existing_file(tmp_path, monkeypatch):
    out = tmp_path / "wind-monitor.json"
    out.write_text(json.dumps({"data": [{"symbol": "OLD"}]}), encoding="utf-8")
    status_path = tmp_path / "wind-status.json"

    def fake_fetch(url, timeout):
        if "/api/activities" in url:
            return {"events": []}
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)

    monkeypatch.setattr(wind, "fetch_json", fake_fetch)
    args = argparse.Namespace(
        base_url="https://wind.example",
        out=out,
        status=status_path,
        timeout_seconds=3,
        limit=10,
        max_handles=2,
        feed_actions="fomo_buy",
        use_catalog=False,
        overwrite_empty=False,
    )

    result = wind.run_once(args)

    assert result["ok"] is True
    assert result["row_count"] == 0
    assert result["feed_error"] == "http_429"
    assert json.loads(out.read_text(encoding="utf-8"))["data"][0]["symbol"] == "OLD"
    assert json.loads(status_path.read_text(encoding="utf-8"))["feed_error"] == "http_429"


def test_run_once_writes_rows_from_feed(tmp_path, monkeypatch):
    out = tmp_path / "wind-monitor.json"
    status_path = tmp_path / "wind-status.json"

    def fake_fetch(url, timeout):
        if "/api/activities" in url:
            return {"events": []}
        return {
            "events": [
                {
                    "id": "evt-2",
                    "platform": "pumpfun",
                    "action": "pump_callout",
                    "handle": "0xsun",
                    "payload": {"content_text": "new pump CA 9xQeWvG816bUx9EPjHmaT23yvVM2ZW8g2vNuuKqupump"},
                }
            ]
        }

    monkeypatch.setattr(wind, "fetch_json", fake_fetch)
    args = argparse.Namespace(
        base_url="https://wind.example",
        out=out,
        status=status_path,
        timeout_seconds=3,
        limit=10,
        max_handles=2,
        feed_actions="pump_callout",
        use_catalog=False,
        overwrite_empty=False,
    )

    result = wind.run_once(args)

    assert result["ok"] is True
    assert result["row_count"] == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["source"] == "wind_monitor"
    assert payload["data"][0]["chain"] == "solana"
