import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import alpha_gmgn_wallet_flow as flow
from alpha_gmgn_wallet_flow import normalize_trade


NOW = "2026-09-07T12:00:00+00:00"
EPOCH = int(datetime.fromisoformat(NOW).timestamp())
TOKEN = "0x" + "a" * 40
TX = "0x" + "b" * 64


def wallet(index=1, **overrides):
    return {"chain": "bsc", "address": "0x" + f"{index:040x}", "status": "profit_history_supported",
            "evidence_checked_at": NOW,
            "stats_30d": {"realized_profit_usd": 1000, "token_count": 30, "sell_count": 40},
            "stats_7d": {"realized_profit_usd": 100, "token_count": 10, "sell_count": 15},
            "activity": {"observed_token_count": 10, "tokens_with_buy_and_sell": 5,
                         "sell_cost_basis_coverage": .9, "sells_with_reported_cost_basis": 12,
                         "profitable_tokens_in_observed_sell_sample": 4, "largest_token_trade_share": .3,
                         "largest_positive_token_margin_share": .5, "sample_reported_sale_margin_usd": 500},
            **overrides}


def event(**overrides):
    return {"token": {"address": TOKEN, "symbol": "TEST"}, "event_type": "buy", "cost_usd": 250,
            "buy_cost_usd": 0, "timestamp": EPOCH, "tx_hash": TX, **overrides}


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def curated(tmp_path, rows):
    write(tmp_path / "gmgn-smart-money-top50.json", {"wallets": rows})


def feed(tmp_path):
    return json.loads((tmp_path / "meme-source-inbox/gmgn-wallet-flow.json").read_text(encoding="utf-8"))


def reply(payload=None, *, code=0, stderr="", stdout=None):
    return SimpleNamespace(returncode=code, stderr=stderr,
                           stdout=stdout if stdout is not None else json.dumps(payload if payload is not None else {"activities": []}))


def mock_activity(monkeypatch, run):
    def checked_run(command, **kwargs):
        if command[1:] == ["config", "--check"]:
            return reply(stdout="configuration ready")
        return run(command, **kwargs)
    monkeypatch.setattr(flow.subprocess, "run", checked_run)


@pytest.fixture(autouse=True)
def no_real_queries_or_environment(monkeypatch):
    monkeypatch.setattr(flow, "utc_now", lambda: NOW)
    monkeypatch.setattr(flow, "resolve_gmgn_runner", lambda: (["mock-gmgn"], {}))
    monkeypatch.setattr(flow, "gmgn_env", lambda: ({"GMGN_API_KEY": "fake-key", "GMGN_PRIVATE_KEY": "must-not-forward"}, {}))
    def unexpected_query(*args, **kwargs):
        raise AssertionError("CLI calls must be mocked in this test")
    monkeypatch.setattr(flow.subprocess, "run", unexpected_query)


def test_explicit_sell_and_base_token_are_preserved():
    row = normalize_trade({"base_address": "0xbase", "address": "0xwrong", "side": "sell", "timestamp": 1788750000,
                           "amount_usd": 50, "transaction_hash": "0xtx", "maker": "0xwallet"}, "bsc")
    assert row["tokenAddress"] == "0xbase"
    assert row["contract_address"] == "0xbase"
    assert row["monitor985_trade_side"] == "sell"
    assert row["tx_hash"] == "0xtx"
    assert row["observed_at"] == 1788750000


def test_unknown_side_and_time_never_become_fresh_buy():
    row = normalize_trade({"base_address": "0xbase", "amount_usd": 50}, "bsc")
    assert row["monitor985_trade_side"] == ""
    assert row["observed_at"] == ""


def test_portfolio_sell_uses_proceeds_and_retains_cost_basis():
    result = flow.normalize_portfolio_event(event(event_type="sell", cost_usd=600, buy_cost_usd=200), "bsc", wallet()["address"])
    assert result["contract_address"] == TOKEN
    assert result["side"] == result["monitor985_trade_side"] == "sell"
    assert result["amount_usd"] == 600
    assert result["buy_cost_usd"] == 200
    assert result["observed_at"] == EPOCH
    assert result["qualified_wallet"] is True
    assert result["independent_profit_verified"] is False
    missing = flow.normalize_portfolio_event(event(event_type="sell", cost_usd=None, buy_cost_usd=200), "bsc", wallet()["address"])
    assert missing["amount_usd"] is None


@pytest.mark.parametrize("changes", [{"event_type": "transfer"}, {"event_type": None},
                                     {"timestamp": None}, {"timestamp": "not-time"},
                                     {"timestamp": "2026-09-07T12:00:00"}, {"token": {}}, {"tx_hash": ""}])
def test_portfolio_does_not_manufacture_missing_evidence(changes):
    assert flow.normalize_portfolio_event(event(**changes), "bsc", wallet()["address"]) is None


@pytest.mark.parametrize("changes", [{"status": "candidate_watch_only"}, {"status": "positive_stats_watch_only"},
                                     {"evidence_checked_at": "2026-09-01T00:00:00Z"},
                                     {"stats_30d": {"realized_profit_usd": -100}}, {"activity": {}}])
def test_nonqualifying_curated_wallets_make_no_calls_and_remove_old_generic_rows(tmp_path, changes, monkeypatch):
    curated(tmp_path, [wallet(**changes)])
    write(tmp_path / "meme-source-inbox/gmgn-wallet-flow.json", {"updated_at": NOW,
        "rows": [{"source": "gmgn_cli_smartmoney", "observed_at": EPOCH, "wallet": wallet()["address"]}]})
    monkeypatch.setattr(flow, "gmgn_env", lambda: pytest.fail("environment must not be loaded"))
    monkeypatch.setattr(flow, "resolve_gmgn_runner", lambda: pytest.fail("runner must not be resolved"))
    status = flow.refresh(tmp_path)
    assert status["reason"] == "no_qualified_wallets"
    assert feed(tmp_path)["rows"] == []


def test_missing_quality_dependency_fails_closed(tmp_path, monkeypatch):
    curated(tmp_path, [wallet()])
    def unavailable(*args, **kwargs):
        raise ImportError("pending shared helper")
    monkeypatch.setattr(flow, "qualified_wallets", unavailable)
    status = flow.refresh(tmp_path)
    assert status["reason"] == "wallet_quality_unavailable"
    assert feed(tmp_path)["rows"] == []


def test_round_robin_queries_four_explicit_wallets_one_page_and_preserves_both_sides(tmp_path, monkeypatch):
    curated(tmp_path, [wallet(i) for i in range(1, 7)])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[1:3] == ["portfolio", "activity"]
        assert "--wallet" in command and "--cursor" not in command and "--type" not in command
        assert "GMGN_PRIVATE_KEY" not in kwargs["env"]
        assert kwargs["env"]["GMGN_API_KEY"] == "fake-key"
        return reply({"data": {"activities": [event(), event(event_type="sell", tx_hash="0x" + "c" * 64)], "next": "ignored"}})
    mock_activity(monkeypatch, run)
    first = flow.refresh(tmp_path)
    assert len(calls) == 4
    assert first["buy_count"] == first["sell_count"] == 4
    assert feed(tmp_path)["next_wallet_index"] == 4
    assert all(row["source"] == flow.SOURCE and row["qualified_wallet"] for row in feed(tmp_path)["rows"])
    assert all(row["wallet_quality_policy"] == "gmgn_profit_history_v1" for row in feed(tmp_path)["rows"])
    monkeypatch.setattr(flow, "utc_now", lambda: "2026-09-07T12:01:00Z")
    second = flow.refresh(tmp_path)
    assert len(calls) == 8
    assert [cmd[cmd.index("--wallet") + 1] for cmd in calls[4:]] == [wallet(i)["address"] for i in (5, 6, 1, 2)]
    assert second["buy_count"] == second["sell_count"] == 6
    assert all(row["observed_at"] == EPOCH for row in feed(tmp_path)["rows"])


def test_cache_expiry_removed_qualification_and_future_events(tmp_path, monkeypatch):
    curated(tmp_path, [wallet()])
    old = flow.normalize_portfolio_event(event(timestamp=EPOCH - 901), "bsc", wallet()["address"])
    removed = flow.normalize_portfolio_event(event(), "bsc", wallet(2)["address"])
    valid = flow.normalize_portfolio_event(event(timestamp=EPOCH - 900), "bsc", wallet()["address"])
    write(tmp_path / "meme-source-inbox/gmgn-wallet-flow.json", {"rows": [old, removed, valid, valid]})
    mock_activity(monkeypatch, lambda *a, **kw: reply({"activities": [event(timestamp=EPOCH + 1)]}))
    assert flow.refresh(tmp_path)["row_count"] == 1
    assert feed(tmp_path)["rows"][0]["observed_at"] == EPOCH - 900
    monkeypatch.setattr(flow, "utc_now", lambda: "2026-09-07T12:00:01Z")
    assert flow.refresh(tmp_path)["row_count"] == 0


@pytest.mark.parametrize("own", [False, True])
def test_active_shared_or_own_cooldown_makes_no_calls_and_never_extends(tmp_path, monkeypatch, own):
    curated(tmp_path, [wallet()])
    cooldown_path = tmp_path / "gmgn-wallet-profit-query-status.json"
    deadline = EPOCH + 600
    if own:
        write(tmp_path / "meme-source-inbox/gmgn-wallet-flow.json", {"retry_after": datetime.fromtimestamp(deadline, timezone.utc).isoformat()})
    else:
        write(cooldown_path, {"retry_after_epoch": deadline, "marker": "history-screener"})
    monkeypatch.setattr(flow, "gmgn_env", lambda: pytest.fail("no environment during cooldown"))
    for _ in range(2):
        assert flow.refresh(tmp_path)["reason"] == "rate_limit_backoff"
        assert json.loads(cooldown_path.read_text())["retry_after_epoch"] == deadline


@pytest.mark.parametrize("response", [reply(code=1, stderr='429 reset_at: 1788789600 secret=fake-key'),
                                      reply({"code": 429, "message": "Too many requests"}),
                                      reply(stdout="HTTP 429 Too Many Requests")])
def test_429_stops_immediately_and_sets_shared_and_own_backoff(tmp_path, monkeypatch, response):
    curated(tmp_path, [wallet(i) for i in range(1, 7)])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return reply({"activities": [event()]}) if len(calls) == 1 else response
    mock_activity(monkeypatch, run)
    status = flow.refresh(tmp_path)
    assert len(calls) == 2
    assert status["reason"] == "rate_limited" and status["partial"] is True
    assert feed(tmp_path)["rows"]
    common = json.loads((tmp_path / "gmgn-wallet-profit-query-status.json").read_text())
    assert common["retry_after_epoch"] >= EPOCH + 300
    assert feed(tmp_path)["retry_after"] == common["retry_after"]
    assert "fake-key" not in json.dumps(feed(tmp_path)) + json.dumps(common)
    flow.refresh(tmp_path)
    assert len(calls) == 2


def test_shared_cooldown_started_by_other_worker_stops_between_calls(tmp_path, monkeypatch):
    curated(tmp_path, [wallet(i) for i in range(1, 7)])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        write(tmp_path / "gmgn-wallet-profit-query-status.json", {"retry_after_epoch": EPOCH + 1000})
        return reply({"activities": [event()]})
    mock_activity(monkeypatch, run)
    status = flow.refresh(tmp_path)
    assert len(calls) == 1
    assert status["reason"] == "rate_limit_backoff"
    assert status["retry_after_epoch"] == EPOCH + 1000


def test_timeout_and_bad_json_are_bounded_and_sanitized(tmp_path, monkeypatch):
    curated(tmp_path, [wallet(i) for i in range(1, 7)])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("fake-key must not persist", 10)
        return reply(stdout="not-json fake-key")
    mock_activity(monkeypatch, run)
    status = flow.refresh(tmp_path)
    assert len(calls) == 4
    assert [error["reason"] for error in status["errors"]] == ["TimeoutExpired", "invalid_json", "invalid_json", "invalid_json"]
    assert "fake-key" not in json.dumps(feed(tmp_path))


def test_throttle_is_based_on_last_query_not_cache_rewrite(tmp_path, monkeypatch):
    curated(tmp_path, [wallet()])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return reply({"activities": [event()]})
    mock_activity(monkeypatch, run)
    flow.refresh(tmp_path)
    for second in (10, 20):
        monkeypatch.setattr(flow, "utc_now", lambda second=second: f"2026-09-07T12:00:{second}Z")
        assert flow.refresh(tmp_path)["skipped"] is True
    assert len(calls) == 1
    monkeypatch.setattr(flow, "utc_now", lambda: "2026-09-07T12:01:00Z")
    flow.refresh(tmp_path)
    assert len(calls) == 2
    assert feed(tmp_path)["rows"][0]["observed_at"] == EPOCH


def test_normal_trade_value_429_is_not_misread_as_rate_limit(tmp_path, monkeypatch):
    curated(tmp_path, [wallet()])
    mock_activity(monkeypatch, lambda *a, **kw: reply({"activities": [event(cost_usd=429)]}))
    assert flow.refresh(tmp_path)["ok"] is True
    assert not (tmp_path / "gmgn-wallet-profit-query-status.json").exists()


def test_duplicate_page_does_not_renew_existing_event_timestamp(tmp_path, monkeypatch):
    curated(tmp_path, [wallet()])
    retained = flow.normalize_portfolio_event(event(timestamp=EPOCH - 600), "bsc", wallet()["address"])
    write(tmp_path / "meme-source-inbox/gmgn-wallet-flow.json", {"rows": [retained]})
    mock_activity(monkeypatch, lambda *a, **kw: reply({"activities": [event()]}))
    flow.refresh(tmp_path)
    assert feed(tmp_path)["rows"] == [retained]


def test_page_and_qualified_wallet_counts_are_bounded(tmp_path, monkeypatch):
    curated(tmp_path, [wallet(i) for i in range(1, 70)])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return reply({"activities": [event(tx_hash="0x" + f"{i:064x}") for i in range(1, 130)], "next": "must-not-follow"})
    mock_activity(monkeypatch, run)
    status = flow.refresh(tmp_path)
    assert status["qualified_wallet_count"] == 50
    assert status["row_count"] == 80
    assert len(calls) == 4


def test_hidden_config_check_runs_once_before_every_eligible_batch(tmp_path, monkeypatch):
    curated(tmp_path, [wallet(i) for i in range(1, 7)])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"] is True
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert "GMGN_PRIVATE_KEY" not in kwargs["env"]
        if command[1:] == ["config", "--check"]:
            return reply(stdout="fake-key config output must not persist")
        assert command[command.index("--limit") + 1] == "20"
        return reply()
    monkeypatch.setattr(flow.subprocess, "run", run)
    flow.refresh(tmp_path)
    assert len(calls) == 5
    assert calls[0] == ["mock-gmgn", "config", "--check"]
    assert all(command[1:3] == ["portfolio", "activity"] for command in calls[1:])
    assert "fake-key" not in json.dumps(feed(tmp_path))
    flow.refresh(tmp_path)
    assert len(calls) == 5


@pytest.mark.parametrize("failure", ["exit", "timeout", "429"])
def test_config_failure_prevents_portfolio_and_does_not_publish_outputs(tmp_path, monkeypatch, failure):
    curated(tmp_path, [wallet()])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert command[1:] == ["config", "--check"]
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fake-key", 10)
        return reply(code=1, stderr="429 fake-key" if failure == "429" else "fake-key config failed")
    monkeypatch.setattr(flow.subprocess, "run", run)
    status = flow.refresh(tmp_path)
    assert len(calls) == 1
    assert status["reason"] == ("rate_limited" if failure == "429" else "gmgn_config_check_failed")
    assert "fake-key" not in json.dumps(feed(tmp_path))
    flow.refresh(tmp_path)
    assert len(calls) == 1
    if failure == "429":
        assert json.loads((tmp_path / "gmgn-wallet-profit-query-status.json").read_text())["retry_after_epoch"] == EPOCH + 300
