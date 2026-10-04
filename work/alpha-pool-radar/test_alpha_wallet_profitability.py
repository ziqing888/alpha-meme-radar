from alpha_wallet_profitability import normalize_stats, stats_gate, activity_summary, extract_stats, activity_gate
from types import SimpleNamespace
import alpha_wallet_profitability as subject
import os
import time
from datetime import datetime, timezone
import pytest
import json


@pytest.fixture(autouse=True)
def prohibit_real_cli(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Real subprocess calls are forbidden in scanner tests")
    monkeypatch.setattr(subject.subprocess, "run", blocked)


def options(tmp_path, **overrides):
    return SimpleNamespace(**{**dict(out_dir=tmp_path, cache_only=True, resume=True,
        activity_only=False, max_wallets=100, activity_wallets=50, activity_pages=3,
        expand_candidates=False), **overrides})


def cached_stats(tmp_path, chain="bsc", address=None):
    address = address or stats()["wallet_address"]
    subject.write(tmp_path / "gmgn-wallet-profit-candidates.json", {"wallets": [{"chain": chain, "address": address}]})
    cache = tmp_path / "wallet-profitability-evidence"
    for period in ("7d", "30d"):
        subject.write(cache / f"stats-{chain}-{address}-{period}.json", {**stats(), "wallet_address": address})
    return cache, address


def mock_public_cli(monkeypatch, data_response):
    calls = []
    monkeypatch.setattr(subject.gmgn, "resolve_gmgn_runner", lambda: (["mock-gmgn"], {}))
    monkeypatch.setattr(subject.gmgn, "gmgn_env", lambda: ({}, {}))
    monkeypatch.setattr(subject.time, "sleep", lambda _: None)
    def response(command, **kwargs):
        calls.append(command)
        if "config" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        payload = data_response(command)
        if isinstance(payload, SimpleNamespace):
            return payload
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(subject.subprocess, "run", response)
    return calls


def stats(profit=1000, tokens=30):
    return {"wallet_address": "0x" + "a" * 40, "realized_profit": str(profit),
            "buy": 100, "sell": 60, "bought_fee": "10", "sold_fee": "10",
            "pnl_stat": {"token_num": tokens, "winrate": .55}}


def test_actual_response_fields_preserve_missing_and_zero():
    row = normalize_stats(stats(0), "30d")
    assert row["realized_profit_usd"] == 0
    assert row["buy_count"] == 100
    assert row["winrate"] == .55
    assert row["unrealized_profit_usd"] is None
    assert row["fee_adjusted_profit_usd"] is None


def test_one_coin_and_one_window_cannot_pass():
    assert stats_gate(normalize_stats(stats(tokens=1), "30d"), normalize_stats(stats(), "7d"))
    assert stats_gate(normalize_stats(stats(), "30d"), {})
    assert not stats_gate(normalize_stats(stats(), "30d"), normalize_stats(stats(), "7d"))


def test_unknown_winrate_not_zero_or_automatic_fail_for_profitable_asymmetry():
    value = stats()
    value["pnl_stat"].pop("winrate")
    result = normalize_stats(value, "30d")
    assert result["winrate"] is None
    assert not stats_gate(result, normalize_stats(stats(), "7d"))


def test_batch_mapping_never_uses_response_position():
    assert extract_stats({"data": [stats()]})[0]["wallet_address"] == stats()["wallet_address"]
    assert extract_stats({"realized_profit": 90}) == []


def test_trade_summary_deduplicates_and_does_not_claim_transfer_as_profit():
    buy = {"tx_hash": "0x1", "timestamp": 100, "event_type": "buy", "token": {"address": "T", "symbol": "TOK"}, "cost_usd": "10"}
    sell = {**buy, "tx_hash": "0x2", "timestamp": 200, "event_type": "sell", "cost_usd": "15"}
    transfer = {**buy, "tx_hash": "0x3", "event_type": "transferIn", "cost_usd": "99999"}
    result = activity_summary([buy, buy, sell, transfer], False)
    assert result["observed_trade_count"] == 2
    assert result["tokens_with_buy_and_sell"] == 1
    assert result["transfer_event_count"] == 1
    assert result["independent_net_profit_usd"] is None
    assert result["history_complete"] is False


def test_small_known_basis_subset_cannot_validate_wallet():
    evidence = {"observed_token_count": 20, "tokens_with_buy_and_sell": 10,
                "sell_cost_basis_coverage": .02, "sells_with_reported_cost_basis": 2,
                "profitable_tokens_in_observed_sell_sample": 2, "sample_reported_sale_margin_usd": 2,
                "largest_token_trade_share": .1, "largest_positive_token_margin_share": .5}
    assert "insufficient_sell_cost_basis_coverage" in activity_gate(evidence)


def test_offline_resume_preserves_activity_and_marks_unchecked(tmp_path):
    address = stats()["wallet_address"]
    subject.write(tmp_path / "gmgn-wallet-profit-candidates.json", {"wallets": [
        {"chain": "bsc", "address": address}, {"chain": "sol", "address": "unqueried"}]})
    cache = tmp_path / "wallet-profitability-evidence"
    for period in ("7d", "30d"):
        subject.write(cache / f"stats-bsc-{address}-{period}.json", stats())
    path = cache / f"activity-bsc-{address}-0.json"
    subject.write(path, {"activities": [{"tx_hash": "t", "token": {"address": "a"}, "event_type": "buy", "timestamp": 1}]})
    before = path.stat().st_mtime_ns
    result = subject.run(SimpleNamespace(out_dir=tmp_path, cache_only=True, resume=True,
                         activity_only=False, max_wallets=100, activity_wallets=50, activity_pages=3))
    assert result["activity_checked_count"] == 1
    assert result["wallets"][0]["activity"]["observed_trade_count"] == 1
    assert result["wallets"][1]["status"] == "pending_stats"
    assert path.stat().st_mtime_ns == before


def test_activity_selection_reserves_slots_across_chains():
    rows = [{"chain": chain, "address": f"{chain}{i}", "stats_30d": {"realized_profit_usd": profit}}
            for chain, i, profit in [("bsc", 1, 10000), ("bsc", 2, 9000), ("sol", 1, 100), ("base", 1, 50)]]
    assert {r["chain"] for r in subject.balanced_shortlist(rows, 3)} == {"bsc", "sol", "base"}


def test_refreshing_first_page_invalidates_later_cached_pages(tmp_path, monkeypatch):
    address = stats()["wallet_address"]
    subject.write(tmp_path / "gmgn-wallet-profit-candidates.json", {"wallets": [{"chain": "bsc", "address": address}]})
    cache = tmp_path / "wallet-profitability-evidence"
    for period in ("7d", "30d"):
        subject.write(cache / f"stats-bsc-{address}-{period}.json", stats())
    for page in range(2):
        subject.write(cache / f"activity-bsc-{address}-{page}.json", {"activities": [], "next": "old-cursor"})
    old = time.time() - 4000
    os.utime(cache / f"activity-bsc-{address}-0.json", (old, old))
    calls = []

    class FakeCLI:
        def __init__(self, *args):
            pass

        def call(self, args):
            calls.append(args)
            return {"activities": [], "next": None if "--cursor" in args else "new-cursor"}

    monkeypatch.setattr(subject, "PublicCLI", FakeCLI)
    subject.run(SimpleNamespace(out_dir=tmp_path, cache_only=False, resume=True,
                activity_only=True, max_wallets=100, activity_wallets=1, activity_pages=2))
    assert len(calls) == 2
    assert calls[1][-2:] == ["--cursor", "new-cursor"]


@pytest.mark.parametrize("chain,address,oldest_kind", [
    ("bsc", "0x" + "a" * 40, "stats"),
    ("sol", "So11111111111111111111111111111111111111112", "activity"),
])
def test_evidence_time_uses_oldest_actual_file_across_stats_and_all_activity_pages(tmp_path, chain, address, oldest_kind):
    cache, address = cached_stats(tmp_path, chain, address)
    now = time.time()
    paths = [cache / f"stats-{chain}-{address}-{p}.json" for p in ("30d", "7d")]
    for page in range(2):
        path = cache / f"activity-{chain}-{address}-{page}.json"
        subject.write(path, {"activities": [], "next": "next-page" if page == 0 else "more-unread"})
        paths.append(path)
    ages = [900, 800, 700, 600] if oldest_kind == "stats" else [600, 500, 900, 800]
    for path, age in zip(paths, ages):
        os.utime(path, (now - age, now - age))
    mtimes = {path: path.stat().st_mtime_ns for path in paths}
    result = subject.run(options(tmp_path, activity_pages=2))
    wallet = result["wallets"][0]
    expected = datetime.fromtimestamp(min(path.stat().st_mtime for path in paths), timezone.utc).isoformat()
    assert wallet["evidence_checked_at"] == expected
    assert wallet["evidence_checked_at"] != result["updated_at"]
    assert wallet["activity"]["history_complete"] is False
    assert mtimes == {path: path.stat().st_mtime_ns for path in paths}


@pytest.mark.parametrize("missing", ["stats", "activity", "future_stats"])
def test_missing_or_future_evidence_stays_pending_without_fabricated_checked_at(tmp_path, missing):
    cache, address = cached_stats(tmp_path)
    stats_path = cache / f"stats-bsc-{address}-7d.json"
    if missing == "stats":
        stats_path.unlink()
    elif missing == "future_stats":
        future = time.time() + 600
        os.utime(stats_path, (future, future))
    if missing != "activity":
        subject.write(cache / f"activity-bsc-{address}-0.json", {"activities": []})
    result = subject.run(options(tmp_path))
    wallet = result["wallets"][0]
    assert wallet["evidence_checked_at"] is None
    assert wallet["status"] == ("positive_stats_pending_activity" if missing == "activity" else "pending_stats")
    assert result["supported_wallets"] == []
    if missing == "future_stats":
        assert stats_path.stat().st_mtime > time.time()


def test_shared_flow_cooldown_blocks_before_config_or_sleep(tmp_path, monkeypatch):
    retry = datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat()
    subject.write(tmp_path / "meme-source-inbox" / "gmgn-wallet-flow.json", {"retry_after": retry})
    monkeypatch.setattr(subject.time, "sleep", lambda _: pytest.fail("Cooldown must be read before sleep"))
    cli = subject.PublicCLI(tmp_path)
    with pytest.raises(RuntimeError, match="cooldown remains active"):
        cli.call(["portfolio", "stats", "--chain", "bsc", "--wallet", stats()["wallet_address"]])


def test_shared_cooldown_is_rechecked_after_query_spacing(tmp_path, monkeypatch):
    def during_sleep(_):
        retry = datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat()
        subject.write(tmp_path / "meme-source-inbox" / "gmgn-wallet-flow.json", {"retry_after": retry})
    monkeypatch.setattr(subject.time, "sleep", during_sleep)
    with pytest.raises(RuntimeError, match="cooldown remains active"):
        subject.PublicCLI(tmp_path).call(["track", "smartmoney", "--chain", "bsc"])


def test_429_persists_existing_common_query_cooldown_and_stops_retry(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subject.gmgn, "resolve_gmgn_runner", lambda: (["mock-gmgn"], {}))
    monkeypatch.setattr(subject.gmgn, "gmgn_env", lambda: ({}, {}))
    monkeypatch.setattr(subject.time, "sleep", lambda _: None)
    def response(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0 if "config" in command else 1, stdout="", stderr="429 rate_limit")
    monkeypatch.setattr(subject.subprocess, "run", response)
    path = tmp_path / "gmgn-wallet-profit-query-status.json"
    subject.write(path, {"preserve": "metadata"})
    cli = subject.PublicCLI(tmp_path)
    with pytest.raises(RuntimeError, match="stopped without retry"):
        cli.call(["track", "smartmoney", "--chain", "bsc"])
    status = subject.read(path)
    assert status["preserve"] == "metadata"
    assert status["rate_limited"] is True
    assert subject.retry_epoch(status) > time.time() + 290
    with pytest.raises(RuntimeError, match="cooldown remains active"):
        cli.call(["track", "smartmoney", "--chain", "bsc"])
    assert len(calls) == 2


@pytest.mark.parametrize("chain,address", [("bsc", "So11111111111111111111111111111111111111112"),
    ("unknown", "0x" + "a" * 40), ("sol", "0x" + "a" * 40), ("bsc", "../bad")])
def test_invalid_identity_rejects_cli_arguments_without_queries(tmp_path, chain, address):
    with pytest.raises(ValueError, match="invalid_"):
        subject.PublicCLI(tmp_path).call(["portfolio", "activity", "--chain", chain, "--wallet", address])


def test_invalid_cached_candidate_skips_queries_not_classified_as_losing_wallet(tmp_path):
    subject.write(tmp_path / "gmgn-wallet-profit-candidates.json", {"wallets": [
        {"chain": "bsc", "address": "So11111111111111111111111111111111111111112"}]})
    result = subject.run(options(tmp_path, cache_only=False))
    assert result["wallets"][0]["status"] == "pending_stats"
    assert result["wallets"][0]["query_skipped_reason"] == "invalid_wallet_address"
    assert result["wallets"][0]["evidence_checked_at"] is None


def test_activity_only_reuses_one_hour_stats_and_reports_stats_mtime(tmp_path, monkeypatch):
    cache, address = cached_stats(tmp_path)
    paths = [cache / f"stats-bsc-{address}-{p}.json" for p in ("30d", "7d")]
    for path in paths:
        old = time.time() - 3500
        os.utime(path, (old, old))
    calls = []
    class FakeCLI:
        def __init__(self, *args):
            pass
        def call(self, args):
            calls.append(args)
            assert args[:2] == ["portfolio", "activity"]
            return {"activities": [], "next": None}
    monkeypatch.setattr(subject, "PublicCLI", FakeCLI)
    result = subject.run(options(tmp_path, cache_only=False, activity_only=True))
    assert len(calls) == 1
    assert result["wallets"][0]["evidence_checked_at"] == datetime.fromtimestamp(min(p.stat().st_mtime for p in paths), timezone.utc).isoformat()


def test_optional_local_expansion_preserves_pool_and_never_discovers_over_cli(tmp_path):
    original = [{"chain": "bsc", "address": "0x" + "a" * 40}]
    subject.write(tmp_path / "gmgn-wallet-profit-candidates.json", {"wallets": original})
    subject.write(tmp_path / "gmgn-smart-money-top50.json", {"wallets": [{"chain": "sol", "address": "So11111111111111111111111111111111111111112"}]})
    unchanged = subject.run(options(tmp_path))
    assert unchanged["candidate_count"] == 1
    expanded = subject.run(options(tmp_path, expand_candidates=True, max_wallets=3))
    assert expanded["candidate_count"] == 3
    assert expanded["wallets"][0]["address"] == original[0]["address"]
    retained = subject.run(options(tmp_path, max_wallets=1))
    assert retained["candidate_count"] == 3


def test_cached_activity_cursor_identity_mismatch_is_not_accepted(tmp_path):
    cache, address = cached_stats(tmp_path)
    subject.write(cache / f"activity-bsc-{address}-0.json", {"activities": [], "next": "right-cursor"})
    subject.write(cache / f"activity-bsc-{address}-1.json", {"activities": [{"tx_hash": "bad", "token": {"address": "wrong"}, "event_type": "buy"}],
        "_profitability_request": {"chain": "sol", "wallet": address, "cursor": "wrong-cursor", "page": 1}})
    result = subject.run(options(tmp_path))
    assert result["wallets"][0]["activity"]["observed_trade_count"] == 0
    assert result["wallets"][0]["activity"]["history_complete"] is False


def test_query_budget_counts_only_data_requests_and_does_not_write_cooldown(tmp_path, monkeypatch):
    calls = mock_public_cli(monkeypatch, lambda _: {})
    cli = subject.PublicCLI(tmp_path, max_queries=2)
    command = ["track", "smartmoney", "--chain", "bsc"]
    cli.call(command)
    cli.call(command)
    with pytest.raises(RuntimeError, match="^query budget exhausted$"):
        cli.call(command)
    assert cli.query_count == 2
    assert len(calls) == 3  # One local config check plus two data requests.
    assert not cli.status_path.exists()
    assert not cli.shared_status_path.exists()


def test_default_query_budget_is_unlimited(tmp_path, monkeypatch):
    calls = mock_public_cli(monkeypatch, lambda _: {})
    cli = subject.PublicCLI(tmp_path)
    for _ in range(3):
        cli.call(["track", "smartmoney", "--chain", "bsc"])
    assert cli.query_count == 3
    assert len(calls) == 4


def test_timeout_consumes_query_budget_without_becoming_429(tmp_path, monkeypatch):
    def timeout(command):
        raise subject.subprocess.TimeoutExpired(command, 30)
    calls = mock_public_cli(monkeypatch, timeout)
    cli = subject.PublicCLI(tmp_path, max_queries=1)
    command = ["track", "smartmoney", "--chain", "bsc"]
    with pytest.raises(subject.subprocess.TimeoutExpired):
        cli.call(command)
    with pytest.raises(RuntimeError, match="^query budget exhausted$"):
        cli.call(command)
    assert len(calls) == 2
    assert not cli.status_path.exists()


def test_budget_exhaustion_still_processes_later_cached_stats_and_activity(tmp_path, monkeypatch):
    cache, cached_address = cached_stats(tmp_path)
    uncached_address = "0x" + "b" * 40
    subject.write(tmp_path / "gmgn-wallet-profit-candidates.json", {"wallets": [
        {"chain": "bsc", "address": uncached_address}, {"chain": "bsc", "address": cached_address}]})
    subject.write(cache / f"activity-bsc-{cached_address}-0.json", {"activities": []})
    calls = mock_public_cli(monkeypatch, lambda cmd: {**stats(), "wallet_address": cmd[cmd.index("--wallet") + 1]})
    result = subject.run(options(tmp_path, cache_only=False, max_queries=1))
    assert result["query_count"] == 1
    assert result["query_budget_exhausted"] is True
    assert len(calls) == 2
    assert result["activity_checked_count"] == 1
    assert result["wallets"][1]["evidence_checked_at"] is not None
    assert result["wallets"][0]["status"] == "pending_stats"
    assert any(e["error"] == "query budget exhausted" for e in result["errors"])
    assert not (tmp_path / "gmgn-wallet-profit-query-status.json").exists()


def test_72_hour_stats_reuse_leaves_budget_for_activity_and_preserves_evidence_age(tmp_path, monkeypatch):
    cache, address = cached_stats(tmp_path)
    paths = [cache / f"stats-bsc-{address}-{p}.json" for p in ("30d", "7d")]
    old = time.time() - 48 * 3600
    for path in paths:
        os.utime(path, (old, old))
    mtimes = [p.stat().st_mtime_ns for p in paths]
    def activity_only(command):
        assert command[1:3] == ["portfolio", "activity"]
        return {"activities": []}
    calls = mock_public_cli(monkeypatch, activity_only)
    result = subject.run(options(tmp_path, cache_only=False, activity_only=True, stats_cache_hours=72, max_queries=12))
    assert result["query_count"] == 1
    assert result["query_budget_exhausted"] is False
    assert len(calls) == 2
    assert [p.stat().st_mtime_ns for p in paths] == mtimes
    assert result["wallets"][0]["evidence_checked_at"] == datetime.fromtimestamp(min(p.stat().st_mtime for p in paths), timezone.utc).isoformat()
    default = subject.run(options(tmp_path))
    assert default["wallets"][0]["status"] == "pending_stats"
    assert default["wallets"][0]["evidence_checked_at"] is None


def test_stats_cache_duration_does_not_extend_activity_cache(tmp_path):
    cache, address = cached_stats(tmp_path)
    path = cache / f"activity-bsc-{address}-0.json"
    subject.write(path, {"activities": []})
    old = time.time() - 2 * 3600
    os.utime(path, (old, old))
    result = subject.run(options(tmp_path, stats_cache_hours=72))
    assert result["activity_checked_count"] == 0
    assert result["wallets"][0]["evidence_checked_at"] is None


def test_discovery_budget_retains_already_discovered_candidates(tmp_path, monkeypatch):
    calls = mock_public_cli(monkeypatch, lambda _: [{"maker": stats()["wallet_address"]}])
    cli = subject.PublicCLI(tmp_path, max_queries=1)
    candidates = subject.discover(tmp_path, cli, 300)
    assert {subject.REFERENCE, stats()["wallet_address"]} <= {r["address"] for r in candidates}
    assert cli.query_count == 1
    assert len(calls) == 2
    assert not cli.status_path.exists()


def activity_pool(tmp_path):
    wallets = [
        {"chain": "bsc", "address": subject.REFERENCE},
        {"chain": "robinhood", "address": "0x" + "b" * 40},
        {"chain": "sol", "address": "So11111111111111111111111111111111111111112"},
        {"chain": "base", "address": "0x" + "b" * 40},
    ]
    for wallet in wallets:
        cached_stats(tmp_path, wallet["chain"], wallet["address"])
    subject.write(tmp_path / "gmgn-wallet-profit-candidates.json", {"wallets": wallets})
    return wallets, tmp_path / "wallet-profitability-evidence"


def test_429_cursor_persists_failed_wallet_then_rotates_full_pool_before_limit(tmp_path, monkeypatch):
    wallets, cache = activity_pool(tmp_path)
    base = wallets[3]
    subject.write(cache / f"activity-base-{base['address']}-0.json", {"activities": [], "next": "base-next"})
    def response(command):
        chain = command[command.index("--chain") + 1]
        if chain == "robinhood":
            return SimpleNamespace(returncode=1, stdout="", stderr="429 rate_limit")
        return {"activities": [], "next": "bsc-next" if chain == "bsc" and "--cursor" not in command else None}
    calls = mock_public_cli(monkeypatch, response)
    args = options(tmp_path, cache_only=False, activity_only=True, activity_wallets=2)
    first = subject.run(args)
    expected = f"robinhood:{wallets[1]['address']}"
    assert first["activity_resume_after"] == expected
    assert subject.read(tmp_path / "gmgn-profitable-wallets.json")["activity_resume_after"] == expected
    assert first["query_count"] == 3
    assert first["activity_checked_count"] == 2  # BSC plus Base cache, even after 429.
    # Only expire the mocked cooldown in this test's temporary directory.
    subject.write(tmp_path / "gmgn-wallet-profit-query-status.json", {"retry_after_epoch": 1})
    calls.clear()
    second = subject.run(args)
    queried_chains = [c[c.index("--chain") + 1] for c in calls if "--chain" in c]
    assert queried_chains == ["sol", "base"]
    assert second["activity_resume_after"] == f"base:{base['address']}"
    assert second["activity_checked_count"] == 3  # Cached BSC is still processed.


def test_budget_preflight_does_not_skip_unattempted_wallet_on_next_run(tmp_path, monkeypatch):
    wallets, cache = activity_pool(tmp_path)
    base = wallets[3]
    subject.write(cache / f"activity-base-{base['address']}-0.json", {"activities": []})
    calls = mock_public_cli(monkeypatch, lambda _: {"activities": []})
    args = options(tmp_path, cache_only=False, activity_only=True, activity_wallets=2, max_queries=1)
    first = subject.run(args)
    assert first["activity_resume_after"] == f"bsc:{wallets[0]['address']}"
    assert first["query_budget_exhausted"] is True
    assert first["activity_checked_count"] == 2
    assert any(e["wallet"] == wallets[1]["address"] and e["error"] == "query budget exhausted" for e in first["errors"])
    calls.clear()
    second = subject.run(args)
    data_calls = [c for c in calls if "--chain" in c]
    assert len(data_calls) == 1
    assert data_calls[0][data_calls[0].index("--chain") + 1] == "robinhood"
    assert second["activity_resume_after"] == f"robinhood:{wallets[1]['address']}"
    assert second["activity_checked_count"] == 3


@pytest.mark.parametrize("cache_only", [True, False])
def test_cache_only_or_cooldown_blocked_run_preserves_cursor_and_all_cached_profiles(tmp_path, cache_only):
    wallets, cache = activity_pool(tmp_path)
    expected = f"robinhood:{wallets[1]['address']}"
    subject.write(tmp_path / "gmgn-profitable-wallets.json", {"activity_resume_after": expected})
    for wallet in wallets:
        subject.write(cache / f"activity-{wallet['chain']}-{wallet['address']}-0.json", {"activities": [], "next": "uncached-page"})
    subject.write(tmp_path / "meme-source-inbox" / "gmgn-wallet-flow.json", {
        "retry_after": datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat()})
    result = subject.run(options(tmp_path, cache_only=cache_only, activity_only=True, activity_wallets=1))
    assert result["query_count"] == 0
    assert result["activity_resume_after"] == expected
    assert result["activity_checked_count"] == 4
    assert all(row["evidence_checked_at"] is not None for row in result["wallets"])
    assert subject.read(tmp_path / "gmgn-profitable-wallets.json")["activity_resume_after"] == expected


def test_stats_requests_and_activity_budget_preflight_cannot_advance_activity_cursor(tmp_path, monkeypatch):
    wallets, _ = activity_pool(tmp_path)
    expected = f"robinhood:{wallets[1]['address']}"
    subject.write(tmp_path / "gmgn-profitable-wallets.json", {"activity_resume_after": expected})
    path = tmp_path / "wallet-profitability-evidence" / f"stats-bsc-{wallets[0]['address']}-7d.json"
    path.unlink()
    calls = mock_public_cli(monkeypatch, lambda _: {**stats(), "wallet_address": wallets[0]["address"]})
    result = subject.run(options(tmp_path, cache_only=False, max_queries=1))
    assert result["query_count"] == 1
    assert result["activity_resume_after"] == expected
    assert len(calls) == 2
    assert calls[1][1:3] == ["portfolio", "stats"]
