from __future__ import annotations

import json

from alpha_token_security import assess_security, enrich_token_security


TOKEN = "0x" + "a" * 40
NOW = "2026-09-10T02:00:00+00:00"


def safe_payload() -> dict:
    return {
        "is_honeypot": "0",
        "cannot_buy": "0",
        "cannot_sell": "0",
        "cannot_sell_all": "0",
        "is_blacklisted": "0",
        "is_mintable": "0",
        "transfer_pausable": "0",
        "slippage_modifiable": "0",
        "personal_slippage_modifiable": "0",
        "owner_change_balance": "0",
        "hidden_owner": "0",
        "is_proxy": "0",
        "buy_tax": "0.01",
        "sell_tax": "0.02",
    }


def test_assessment_requires_explicit_bsc_contract_and_tax_evidence():
    passed = assess_security("bsc", TOKEN, safe_payload(), NOW)
    missing = assess_security("bsc", TOKEN, {**safe_payload(), "sell_tax": ""}, NOW)
    risky = assess_security("bsc", TOKEN, {**safe_payload(), "is_mintable": "1"}, NOW)

    assert passed["hard_risk_pass"] is True
    assert missing["hard_risk_pass"] is None
    assert risky["hard_risk_pass"] is False
    assert "is_mintable" in risky["hard_risk_flags"]


def test_bsc_does_not_require_unavailable_top_level_cannot_sell_field():
    payload = safe_payload()
    payload.pop("cannot_sell")

    result = assess_security("bsc", TOKEN, payload, NOW)

    assert result["hard_risk_pass"] is True
    assert result["assessment_status"] == "passed"
    assert result["checks"]["cannot_sell"] is None


def test_nested_b20_cannot_sell_still_fails_security():
    payload = safe_payload()
    payload.pop("cannot_sell")
    payload["b20_token"] = {
        "b20_info": {"cannot_sell": {"status": "1", "admin": []}}
    }

    result = assess_security("bsc", TOKEN, payload, NOW)

    assert result["hard_risk_pass"] is False
    assert "cannot_sell" in result["hard_risk_flags"]


def test_robinhood_incomplete_goplus_result_is_deferred_to_exact_executor():
    result = assess_security(
        "robinhood",
        TOKEN,
        {"cannot_buy": "0", "cannot_sell": "0", "buy_tax": "", "sell_tax": ""},
        NOW,
    )

    assert result["hard_risk_pass"] is None
    assert result["assessment_status"] == "executor_confirmation_required"


def test_enrichment_binds_chain_contract_and_cache_time(tmp_path):
    calls = []

    def fetcher(chain, addresses):
        calls.append((chain, addresses))
        return {TOKEN: safe_payload()}

    rows = [{"chain": "bsc", "contract_address": TOKEN, "signal_stage": "aggregate_early_bird"}]
    cache = tmp_path / "security.json"
    first = enrich_token_security(rows, cache, NOW, fetcher=fetcher)
    second = enrich_token_security(rows, cache, "2026-09-10T02:01:00+00:00", fetcher=fetcher)

    assert first[0]["hard_risk_pass"] is True
    assert second[0]["hard_risk_pass"] is True
    assert second[0]["token_security"]["chain"] == "bsc"
    assert second[0]["token_security"]["contract_address"] == TOKEN
    assert len(calls) == 1
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["rows"][f"bsc:{TOKEN}"]["observed_at"] == NOW


def test_fetch_failure_never_invents_a_pass(tmp_path):
    def broken_fetcher(chain, addresses):
        raise TimeoutError("upstream timeout")

    rows = [{"chain": "bsc", "contract_address": TOKEN, "signal_stage": "aggregate_early_bird"}]
    result = enrich_token_security(rows, tmp_path / "security.json", NOW, fetcher=broken_fetcher)

    assert "hard_risk_pass" not in result[0]
    assert result[0]["token_security"]["assessment_status"] == "unavailable"
