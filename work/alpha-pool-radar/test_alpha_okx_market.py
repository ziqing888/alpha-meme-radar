import pytest
import json

import alpha_okx_market as okx
import alpha_meme as meme


@pytest.fixture(autouse=True)
def no_live_pacing(monkeypatch):
    monkeypatch.setattr(okx.time, "sleep", lambda seconds: None)


def test_local_credentials_reload_and_do_not_leak(monkeypatch, tmp_path):
    path = tmp_path / "credentials.json"
    monkeypatch.setattr(okx, "CREDENTIAL_PATH", path)
    for name in okx.CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)
    path.write_text(json.dumps({"OKX_API_KEY": "test-key", "OKX_SECRET_KEY": "test-secret"}))
    assert okx.credentials() == ("test-key", "test-secret", "")
    assert okx.status()["missing_fields"] == ["OKX_PASSPHRASE"]
    assert "test-secret" not in json.dumps(okx.status())
    path.write_text(json.dumps(dict(zip(okx.CREDENTIAL_NAMES, ("test-key", "test-secret", "test-pass")))))
    assert okx.credentials()[-1] == "test-pass"
    monkeypatch.setenv("OKX_PASSPHRASE", "env-pass")
    assert okx.credentials()[-1] == "env-pass"
    path.write_text("{")
    assert okx.credentials() == ("", "", "env-pass")


def test_no_credentials_never_requests(monkeypatch):
    monkeypatch.setattr(okx, "credentials", lambda: ("", "", ""))
    monkeypatch.setattr(okx, "request", lambda *args: pytest.fail("unexpected request"))
    assert okx.fetch({"bsc"}, 20) == ([], [])
    assert okx.status()["reason"] == "missing_market_api_credentials"


def test_stages_signals_dedup_and_partial_error(monkeypatch):
    monkeypatch.setattr(okx, "credentials", lambda: ("test", "test", "test"))
    def request(method, path, params):
        if params.get("stage") == "MIGRATING":
            raise RuntimeError("payment_required")
        row = {"tokenContractAddress": "0xABC", "symbol": "TEST", "market": {"marketCapUsd": "50000"}}
        if method == "POST":
            row = {"timestamp": "123", "walletType": "1", "triggerWalletCount": "3", "token": {"tokenAddress": "0xABC", "symbol": "TEST"}}
        return [row, row]
    monkeypatch.setattr(okx, "request", request)
    rows, errors = okx.fetch({"bsc"}, 20)
    assert len(rows) == 3
    assert all(row["address"] == "0xabc" for row in rows)
    assert errors == ["bsc/MIGRATING: payment_required"]
    assert okx.status()["signal_row_count"] == 1
    assert okx.status()["ok"] is False


def test_official_payload_reaches_radar(monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "bsc")
    row = meme.normalize_external_meme_row({"chainIndex": "56", "tokenContractAddress": "0xABC",
        "market": {"marketCapUsd": "50000", "volumeUsd1h": "1000"}}, "okx_trenches")
    assert row["tokenAddress"] == "0xabc"
    assert row["profile"]["market_cap"] == 50000
    assert row["profile"]["volume"] == 0  # 1h volume must not masquerade as 24h.
    signal = meme.normalize_external_meme_row({"chainIndex": "56", "timestamp": "123", "walletType": "1",
        "triggerWalletCount": "3", "amountUsd": "1912.7", "soldRatioPercent": "64.29",
        "token": {"tokenAddress": "0xABC", "marketCapUsd": "60000", "holders": "445", "top10HolderPercent": "17.9547"}}, "okx_signal")
    assert signal["profile"]["market_cap"] == 60000
    assert signal["profile"]["holder_count"] == "445"
    assert signal["profile"]["top_10_holder_rate"] == "17.9547"
    assert signal["profile"]["okx_signal_panel"] is True
    assert signal["profile"]["smart_money"] == "3"
    assert signal["profile"]["okx_signal_amount_usd"] == "1912.7"
    assert meme.external_source_label("okx_signal") == "OKX信号"


def test_memepump_payload_preserves_launch_and_risk_fields(monkeypatch):
    monkeypatch.setenv("ALPHA_MEME_CHAINS", "solana")
    row = okx.normalize(
        {
            "chainIndex": "501",
            "tokenAddress": "MintPump111111111111111111111111111111111",
            "symbol": "PUMP",
            "createdTimestamp": "1773111278502",
            "market": {"marketCapUsd": "32000", "volumeUsd1h": "800", "txCount1h": "12"},
            "bondingPercent": "41",
            "tags": {"top10HoldingsPercent": "19", "bundlersPercent": "5", "totalHolders": "94"},
            "social": {"dexScreenerPaid": True},
        },
        "solana",
        "TRENCHES",
    )
    normalized = meme.normalize_external_meme_row(row, "okx_trenches")
    assert normalized["profile"]["market_cap"] == 32000
    assert normalized["profile"]["volume"] == 0
    assert normalized["profile"]["okx_volume_1h"] == "800"
    assert normalized["profile"]["okx_bonding_percent"] == "41"
    assert normalized["profile"]["top_10_holder_rate"] == "19"
    assert normalized["profile"]["bundler_rate"] == "5"
    assert normalized["profile"]["holder_count"] == "94"
    assert normalized["profile"]["market_data_pending"] is True


def test_endpoint_allowlist():
    with pytest.raises(ValueError, match="unsupported_market_endpoint"):
        okx.request("POST", "/api/v6/dex/aggregator/swap", {})


def test_robinhood_mainnet_sources(monkeypatch):
    monkeypatch.setattr(okx, "credentials", lambda: ("test", "test", "test"))
    calls = []
    def request(method, path, params):
        calls.append(params)
        return [{"tokenContractAddress": "0xABC", "chainIndex": "4663"}]
    monkeypatch.setattr(okx, "request", request)
    rows, errors = okx.fetch({"robinhood"}, 20)
    assert not errors
    assert len(calls) == 4
    assert all(call["chainIndex"] == "4663" for call in calls)
    assert all(row["chain"] == "robinhood" and row["address"] == "0xabc" for row in rows)


def test_rest_snapshot_persists_okx_sources_for_fast_discovery(monkeypatch, tmp_path):
    inbox = tmp_path / "outputs" / "meme-source-inbox"
    signal = okx.normalize(
        {"timestamp": "123", "walletType": "1", "triggerWalletCount": "3", "token": {"tokenAddress": "0xABC", "symbol": "SIG"}},
        "bsc",
        "SIGNAL",
    )
    trenches = okx.normalize(
        {"tokenContractAddress": "0xDEF", "symbol": "NEW", "market": {"marketCapUsd": "12000"}},
        "bsc",
        "NEW",
    )

    status = okx.persist_snapshot(inbox, [signal, trenches], [])
    monkeypatch.setattr(meme, "ROOT", tmp_path)
    monkeypatch.delenv("ALPHA_MEME_SOURCE_INBOX_DISABLE", raising=False)
    sources, errors = meme.load_local_inbox_sources(20)

    assert status["signal_rows"] == 1
    assert status["trenches_rows"] == 1
    assert not errors
    assert sources["bsc:0xabc"]["sources"] == ["okx_signal"]
    assert sources["bsc:0xdef"]["sources"] == ["okx_trenches"]
