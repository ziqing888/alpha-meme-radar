import importlib.util
import json
import subprocess
import sys
from pathlib import Path


BASE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("alpha_gmgn_smart_money_top50", BASE / "alpha_gmgn_smart_money_top50.py")
top50 = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = top50
SPEC.loader.exec_module(top50)


def test_build_wallet_rows_uses_real_wallet_fields_only(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    token_address = "0x1111111111111111111111111111111111111111"
    wallet_address = "0x2222222222222222222222222222222222222222"
    payload = {
        "source": "okx_signal",
        "data": [
            {
                "chain": "bsc",
                "address": token_address,
                "symbol": "DOG",
                "smart_money": 9,
                "okx_signal_wallet_addresses": [wallet_address],
                "monitor985_trade_side": "BUY",
                "monitor985_trade_amount_usd": 1000,
            }
        ],
    }
    (inbox / "okx-signal-stream.json").write_text(json.dumps(payload), encoding="utf-8")

    result = top50.build_wallet_rows(inbox, limit=50)

    assert result["count"] == 1
    assert result["wallets"][0]["address"] == wallet_address
    assert result["wallets"][0]["representative_tokens"][0]["token"] == f"bsc:{token_address}"


def test_token_level_smartmoney_does_not_become_wallet(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    payload = {
        "source": "985_smartmoney",
        "data": [
            {
                "chain": "bsc",
                "address": "0x3333333333333333333333333333333333333333",
                "symbol": "CAT",
                "smart_money": 45,
                "monitor985_smart_wallet_quality": 60,
            }
        ],
    }
    (inbox / "985-smartmoney.json").write_text(json.dumps(payload), encoding="utf-8")

    result = top50.build_wallet_rows(inbox, limit=50)

    assert result["count"] == 0
    assert result["token_level_smart_money_rows"] == 1
    assert result["insufficient_wallet_addresses"] is True


def test_run_once_replaces_even_fresh_legacy_output_with_empty_qualified_list(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    out = tmp_path / "top50.json"
    markdown = tmp_path / "top50.md"
    out.write_text(json.dumps({"updated_at": top50.now_iso(), "count": 3}), encoding="utf-8")

    result = top50.run_once(inbox_dir=inbox, json_out=out, markdown_out=markdown, force=False)

    assert result["ok"] is True
    assert result["skipped"] is False
    assert result["count"] == 0
    assert json.loads(out.read_text(encoding="utf-8"))["wallets"] == []
    assert json.loads((tmp_path / "top50.legacy-watchlist.json").read_text(encoding="utf-8"))["count"] == 3


def test_collect_gmgn_cli_rows_parses_smartmoney_stdout(monkeypatch):
    wallet_address = "0x4444444444444444444444444444444444444444"
    token_address = "0x5555555555555555555555555555555555555555"

    monkeypatch.setattr(top50, "resolve_gmgn_runner", lambda: (["gmgn-cli"], {"mode": "test"}))
    monkeypatch.setattr(top50, "gmgn_env", lambda: ({"GMGN_API_KEY": "test"}, {"configured": True, "private_key_forwarded": False}))

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["gmgn-cli"],
            returncode=0,
            stdout=json.dumps(
                {
                    "list": [
                        {
                            "maker": wallet_address,
                            "token_address": token_address,
                            "token_symbol": "DOG",
                            "side": "buy",
                            "amount_usd": 1200,
                        }
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(top50.subprocess, "run", fake_run)

    rows, status = top50.collect_gmgn_cli_rows(limit=50, chains=["bsc"], timeout_seconds=3)

    assert status["ok"] is True
    assert status["rows"] == 1
    assert rows[0]["wallet"] == wallet_address
    assert rows[0]["tokenAddress"] == token_address
    assert rows[0]["source"] == "gmgn_cli_smartmoney"


def test_build_wallet_rows_merges_gmgn_cli_wallets(tmp_path):
    wallet_address = "0x6666666666666666666666666666666666666666"
    token_address = "0x7777777777777777777777777777777777777777"
    gmgn_rows = [
        {
            "source": "gmgn_cli_smartmoney",
            "chain": "bsc",
            "wallet": wallet_address,
            "tokenAddress": token_address,
            "symbol": "CAT",
            "monitor985_trade_side": "buy",
            "monitor985_trade_amount_usd": 500,
        }
    ]

    result = top50.build_wallet_rows(
        tmp_path / "missing-inbox",
        limit=1,
        gmgn_rows=gmgn_rows,
        gmgn_cli_status={"ok": True, "rows": 1},
    )

    assert result["complete"] is True
    assert result["gmgn_wallet_event_rows"] == 1
    assert result["wallets"][0]["sources"] == ["gmgn_cli_smartmoney"]
    assert result["wallets"][0]["address"] == wallet_address


def test_curated_publication_is_offline_and_archives_legacy_once(tmp_path, monkeypatch):
    from test_alpha_wallet_quality import good_wallet, NOW

    monkeypatch.setattr(top50, "now_iso", lambda: NOW)
    monkeypatch.setattr(top50, "collect_gmgn_cli_rows", lambda **kw: (_ for _ in ()).throw(AssertionError("No CLI")))
    monkeypatch.setattr(top50, "build_wallet_rows", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("No activity ranking")))
    monkeypatch.setattr(top50.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("No process")))
    out, markdown = tmp_path / "top50.json", tmp_path / "top50.md"
    legacy = b'{"count":1,"wallets":[{"tags":["KOL"],"score":100}]}'
    out.write_bytes(legacy)
    markdown.write_bytes(b"Original KOL watchlist\r\n")
    source = tmp_path / "gmgn-profitable-wallets.json"
    source.write_text(json.dumps({"wallets": [good_wallet()]}), encoding="utf-8")
    source_bytes = source.read_bytes()
    for force in (False, True):
        result = top50.run_once(json_out=out, markdown_out=markdown, force=force)
        published = json.loads(out.read_text(encoding="utf-8"))
        assert result["count"] == 1 and result["complete"] is False
        assert published["schema_version"] == 2
        assert published["policy_id"] == "gmgn_profit_history_v1"
        assert published["selection_policy"]["legacy_fallback"] is False
        assert published["wallets"][0]["activity"] == good_wallet()["activity"]
        assert (tmp_path / "top50.legacy-watchlist.json").read_bytes() == legacy
        assert (tmp_path / "top50.legacy-watchlist.md").read_bytes() == b"Original KOL watchlist\r\n"
    assert source.read_bytes() == source_bytes
    assert "fee-inclusive" in markdown.read_text(encoding="utf-8")
    assert "1/50" in markdown.read_text(encoding="utf-8")


def test_missing_bad_or_stale_profit_report_empties_publisher_even_force(tmp_path, monkeypatch):
    from test_alpha_wallet_quality import good_wallet, NOW

    monkeypatch.setattr(top50, "now_iso", lambda: NOW)
    monkeypatch.setattr(top50, "collect_gmgn_cli_rows", lambda **kw: (_ for _ in ()).throw(AssertionError("No CLI fallback")))
    out, markdown = tmp_path / "top50.json", tmp_path / "top50.md"
    source = tmp_path / "gmgn-profitable-wallets.json"
    assert top50.run_once(json_out=out, markdown_out=markdown, force=True)["count"] == 0
    for raw in ("broken", "[]", json.dumps({"wallets": [dict(good_wallet(), evidence_checked_at=None)]})):
        source.write_text(raw, encoding="utf-8")
        result = top50.run_once(json_out=out, markdown_out=markdown, force=True)
        assert result["count"] == 0
        assert json.loads(out.read_text(encoding="utf-8"))["wallets"] == []


def test_evidence_expiry_is_rechecked_despite_fresh_publication(tmp_path, monkeypatch):
    from test_alpha_wallet_quality import good_wallet, NOW

    source = tmp_path / "gmgn-profitable-wallets.json"
    source.write_text(json.dumps({"wallets": [good_wallet()]}), encoding="utf-8")
    out, markdown = tmp_path / "top50.json", tmp_path / "top50.md"
    monkeypatch.setattr(top50, "now_iso", lambda: NOW)
    assert top50.run_once(json_out=out, markdown_out=markdown)["count"] == 1
    monkeypatch.setattr(top50, "now_iso", lambda: "2026-09-09T12:00:01+00:00")
    assert top50.run_once(json_out=out, markdown_out=markdown, max_age_days=999)["count"] == 0


def test_republication_keeps_minimum_actual_evidence_timestamp(tmp_path, monkeypatch):
    from test_alpha_wallet_quality import good_wallet, NOW

    first, second = good_wallet(), good_wallet("0x" + "b" * 40)
    first["evidence_checked_at"] = "2026-09-06T10:00:00+00:00"
    second["evidence_checked_at"] = "2026-09-06T11:00:00+02:00"
    source = tmp_path / "gmgn-profitable-wallets.json"
    source.write_text(json.dumps({"updated_at": "2050-01-01T00:00:00Z", "wallets": [first, second]}), encoding="utf-8")
    before = source.read_bytes()
    out, markdown = tmp_path / "top50.json", tmp_path / "top50.md"
    for current in (NOW, "2026-09-07T12:01:00+00:00"):
        monkeypatch.setattr(top50, "now_iso", lambda: current)
        status = top50.run_once(json_out=out, markdown_out=markdown)
        published = json.loads(out.read_text(encoding="utf-8"))
        for payload in (status, published):
            assert payload["generated_at"] == second["evidence_checked_at"]
            assert payload["evidence_checked_at"] == second["evidence_checked_at"]
            assert payload["updated_at"] == current
            assert payload["source_status"] == "available"
            assert payload["gmgn_cli_status"] == {"ok": True, "skipped": True, "called": False,
                                                   "reason": "not_required_local_evidence"}
        assert published["wallets"][0]["evidence_checked_at"] == first["evidence_checked_at"]
    assert source.read_bytes() == before


def test_empty_publication_has_null_evidence_and_no_provider_failure(tmp_path, monkeypatch):
    from test_alpha_wallet_quality import good_wallet, NOW

    monkeypatch.setattr(top50, "now_iso", lambda: NOW)
    out, markdown = tmp_path / "top50.json", tmp_path / "top50.md"
    missing = top50.run_once(json_out=out, markdown_out=markdown)
    assert missing["source_status"] == "missing_or_invalid"
    assert missing["gmgn_cli_status"]["ok"] is True
    assert missing["gmgn_cli_status"]["called"] is False
    source = tmp_path / "gmgn-profitable-wallets.json"
    stale = dict(good_wallet(), evidence_checked_at="2026-09-01T00:00:00Z")
    source.write_text(json.dumps({"updated_at": NOW, "wallets": [stale]}), encoding="utf-8")
    empty = top50.run_once(json_out=out, markdown_out=markdown)
    assert empty["source_status"] == "available"
    for payload in (missing, empty, json.loads(out.read_text(encoding="utf-8"))):
        assert payload["count"] == 0
        assert payload["generated_at"] is None
        assert payload["evidence_checked_at"] is None


def test_curated_refresh_uses_evidence_never_publication_time_or_mtime(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    out = tmp_path / "top50.json"
    for stamp, expected in ((None, True), ("bad", True),
                            ((now - timedelta(days=4)).isoformat(), True),
                            ((now + timedelta(days=1)).isoformat(), True),
                            ((now - timedelta(days=1)).isoformat(), False)):
        out.write_text(json.dumps({"schema_version": 2, "policy_id": top50.POLICY_ID,
            "updated_at": now.isoformat(), "generated_at": now.isoformat(),
            "evidence_checked_at": stamp}), encoding="utf-8")
        assert top50.should_refresh(out) is expected
