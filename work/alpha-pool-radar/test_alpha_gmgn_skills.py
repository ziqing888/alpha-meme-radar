import json
from types import SimpleNamespace

import pytest

import alpha_gmgn_skills as skills


NOW = "2026-09-10T03:00:00+00:00"
TOKEN = "0x" + "a" * 40
WALLET = "0x" + "b" * 40
TX = "0x" + "c" * 64


def reply(payload=None, *, code=0, stderr=""):
    return payload, SimpleNamespace(returncode=code, stdout=json.dumps(payload or {}), stderr=stderr)


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch):
    monkeypatch.setattr(skills, "_iso", lambda: NOW)
    monkeypatch.setattr(skills.time, "time", lambda: 1789009200.0)
    monkeypatch.setattr(skills, "chain_list", lambda: ["bsc", "robinhood"])
    monkeypatch.setattr(skills, "resolve_gmgn_runner", lambda: (["mock-gmgn"], {"mode": "test"}))
    monkeypatch.setattr(skills, "gmgn_env", lambda: ({"GMGN_API_KEY": "test", "GMGN_PRIVATE_KEY": "drop-me"}, {"configured": True}))
    monkeypatch.setattr(
        skills.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="configuration ready", stderr=""),
    )
    monkeypatch.setattr(skills.time, "sleep", lambda _seconds: None)


def test_refresh_rotates_discovery_flow_and_context_under_four_calls_per_cycle(monkeypatch, tmp_path):
    calls = []

    def fake_run(_runner, env, args, _timeout):
        calls.append(args)
        assert "GMGN_PRIVATE_KEY" not in env
        chain = args[args.index("--chain") + 1] if "--chain" in args else "bsc"
        if args[:2] == ["market", "signal"]:
            return reply({"data": [{
                "token_address": TOKEN,
                "signal_type": 12,
                "trigger_at": 1789009190,
                "trigger_mc": 12000,
                "market_cap": 15000,
                "cur_data": {"liquidity": 9000, "holder_count": 88, "top_10_holder_rate": 0.18},
            }]})
        if args[:2] == ["market", "trenches"]:
            return reply({"data": {
                "new_creation": [{"address": TOKEN, "symbol": "NEW", "market_cap": 8000}],
                "pump": [{"address": "0x" + "d" * 40, "symbol": "NEAR", "market_cap": 22000}],
                "completed": [],
            }})
        if args[:2] == ["market", "trending"]:
            return reply({"data": {"rank": [{"address": TOKEN, "symbol": "HOT", "smart_degen_count": 4}]}})
        if args[:2] in (["track", "smartmoney"], ["track", "kol"]):
            return reply({"list": [{
                "base_address": TOKEN,
                "maker": WALLET,
                "side": "buy",
                "amount_usd": 250,
                "transaction_hash": TX,
                "timestamp": 1789009190,
            }]})
        if args[:2] == ["market", "hot-searches"]:
            return reply({"data": [{
                "chain": chain,
                "interval": "5m",
                "tokens": [{"address": TOKEN, "symbol": "SEARCH", "visiting_count": 321, "rank": 1}],
            }]})
        raise AssertionError(args)

    monkeypatch.setattr(skills, "_run_cli", fake_run)
    monkeypatch.setattr(skills, "_assess_wallets", lambda *args, **kwargs: ([], {"ok": True, "mode": "deferred", "count": 0}))

    discovery = skills.refresh(tmp_path, limit=20)
    discovery_calls = list(calls)
    calls.clear()
    flow = skills.refresh(tmp_path, limit=20)
    flow_calls = list(calls)
    calls.clear()
    context = skills.refresh(tmp_path, limit=20)
    context_calls = list(calls)

    assert discovery["ok"] is True
    assert discovery["lane"] == "discovery"
    assert discovery["next_lane"] == "flow"
    assert discovery["request_count"] == 4
    assert discovery["signal_rows"] == 2
    assert discovery["trenches_rows"] == 4
    assert all(call[:2] in (["market", "signal"], ["market", "trenches"]) for call in discovery_calls)
    assert flow["lane"] == "flow"
    assert flow["next_lane"] == "context"
    assert flow["request_count"] == 4
    assert flow["smartmoney_rows"] == 2
    assert flow["trending_rows"] == 2
    assert all(call[:2] in (["track", "smartmoney"], ["market", "trending"]) for call in flow_calls)
    assert context["lane"] == "context"
    assert context["next_lane"] == "discovery"
    assert context["request_count"] == 3
    assert context["kol_rows"] == 2
    assert context["hot_search_rows"] == 1
    assert all(call[:2] in (["track", "kol"], ["market", "hot-searches"]) for call in context_calls)
    assert max(discovery["request_count"], flow["request_count"], context["request_count"]) <= 4
    signal = json.loads((tmp_path / "meme-source-inbox/gmgn-skills-signal.json").read_text())["rows"][0]
    assert signal["gmgn_signal_name"] == "smart_money_buy"
    assert signal["liquidity"] == 9000
    trenches = json.loads((tmp_path / "meme-source-inbox/gmgn-skills-trenches.json").read_text())["rows"]
    assert {row["gmgn_trenches_stage"] for row in trenches} == {"new_creation", "near_completion"}


def test_legacy_hot_search_lane_resumes_as_context_lane(monkeypatch, tmp_path):
    (tmp_path / "gmgn-skills-status.json").write_text(json.dumps({"next_slow_lane": "hot_search"}))
    calls = []

    def fake_run(_runner, _env, args, _timeout):
        calls.append(args)
        if args[:2] == ["market", "hot-searches"]:
            return reply({"data": [{"chain": "bsc", "tokens": [{"address": TOKEN, "visiting_count": 99}]}]})
        if args[:2] == ["market", "trenches"]:
            return reply({"data": {"new_creation": [], "pump": [], "completed": []}})
        return reply({"data": {"rank": []}} if args[:2] == ["market", "trending"] else {"list": []} if args[0] == "track" else {"data": []})

    monkeypatch.setattr(skills, "_run_cli", fake_run)
    monkeypatch.setattr(skills, "_assess_wallets", lambda *args, **kwargs: ([], {"ok": True, "mode": "deferred", "count": 0}))

    status = skills.refresh(tmp_path, limit=20)

    assert status["lane"] == "context"
    assert status["hot_search_rows"] == 1
    assert status["kol_rows"] == 0
    assert status["next_lane"] == "discovery"
    assert status["request_count"] <= 4
    assert status["request_weight"] <= 20


def test_default_shared_key_uses_one_request_and_resumes_lane_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(
        skills,
        "gmgn_env",
        lambda: (
            {"GMGN_API_KEY": "shared"},
            {"configured": True, "default_read_only_key_used": True},
        ),
    )
    calls = []
    assessment_limits = []

    def fake_run(_runner, _env, args, _timeout):
        calls.append(args)
        return reply({"data": []})

    monkeypatch.setattr(skills, "_run_cli", fake_run)
    monkeypatch.setattr(
        skills,
        "_assess_wallets",
        lambda *args, **kwargs: assessment_limits.append(kwargs["request_limit"]) or ([], {"ok": True, "mode": "cached", "count": 0, "request_count": 0}),
    )

    first = skills.refresh(tmp_path)
    second = skills.refresh(tmp_path)

    assert len(calls) == 2
    assert calls[0][:2] == ["market", "signal"]
    assert calls[0][calls[0].index("--chain") + 1] == "bsc"
    assert calls[1][:2] == ["market", "signal"]
    assert calls[1][calls[1].index("--chain") + 1] == "robinhood"
    assert first["request_count"] == 1
    assert first["next_lane"] == "discovery"
    assert first["next_lane_cursor"] == 1
    assert second["request_count"] == 1
    assert second["next_lane_cursor"] == 2
    assert assessment_limits == [0, 0]


def test_default_shared_key_context_uses_cached_wallet_assessment(monkeypatch, tmp_path):
    (tmp_path / "gmgn-skills-status.json").write_text(json.dumps({"next_lane": "context"}))
    monkeypatch.setattr(
        skills,
        "gmgn_env",
        lambda: (
            {"GMGN_API_KEY": "shared"},
            {"configured": True, "default_read_only_key_used": True},
        ),
    )
    assessment_limits = []
    monkeypatch.setattr(skills, "_run_cli", lambda *_args, **_kwargs: reply({"list": []}))
    monkeypatch.setattr(
        skills,
        "_assess_wallets",
        lambda *args, **kwargs: assessment_limits.append(kwargs["request_limit"]) or ([], {"ok": True, "mode": "cached", "count": 0, "request_count": 0}),
    )

    status = skills.refresh(tmp_path)

    assert status["request_count"] == 1
    assert assessment_limits == [0]


def test_rate_limit_preserves_previous_snapshot_and_sets_shared_cooldown(monkeypatch, tmp_path):
    inbox = tmp_path / "meme-source-inbox"
    inbox.mkdir(parents=True)
    old = {"source": skills.SIGNAL_SOURCE, "updated_at": "old", "rows": [{"tokenAddress": TOKEN}]}
    (inbox / "gmgn-skills-signal.json").write_text(json.dumps(old))

    def fake_run(_runner, _env, args, _timeout):
        if args[:2] == ["market", "signal"]:
            return reply({"code": 429, "reset_at": 1789009500}, code=1, stderr="RATE_LIMIT_BANNED 429")
        raise AssertionError("collector must stop on first 429")

    monkeypatch.setattr(skills, "_run_cli", fake_run)

    status = skills.refresh(tmp_path)

    assert status["reason"] == "rate_limited"
    assert json.loads((inbox / "gmgn-skills-signal.json").read_text()) == old
    shared = json.loads((tmp_path / "gmgn-wallet-profit-query-status.json").read_text())
    assert shared["rate_limited"] is True
    assert shared["retry_after_epoch"] >= 1789009500


def test_shared_cooldown_skips_all_calls_without_overwriting(monkeypatch, tmp_path):
    (tmp_path / "gmgn-wallet-profit-query-status.json").write_text(json.dumps({"retry_after_epoch": 1789009500}))
    monkeypatch.setattr(skills, "_run_cli", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no call expected")))

    status = skills.refresh(tmp_path)

    assert status["skipped"] is True
    assert status["reason"] == "rate_limit_backoff"


def test_dedicated_key_spaces_requests_instead_of_bursting(monkeypatch, tmp_path):
    pauses = []
    monkeypatch.setattr(skills.time, "sleep", pauses.append)
    monkeypatch.setattr(skills, "_run_cli", lambda *_args, **_kwargs: reply({"data": []}))
    monkeypatch.setattr(skills, "_assess_wallets", lambda *args, **kwargs: ([], {
        "ok": True, "mode": "deferred", "count": 0, "request_count": 0,
    }))

    status = skills.refresh(tmp_path, request_interval_seconds=0.75)

    assert status["request_count"] == 4
    assert pauses == [0.75, 0.75, 0.75]


def test_fast_hot_search_refreshes_once_then_uses_interval_cache(monkeypatch, tmp_path):
    calls = []

    def fake_run(_runner, env, args, _timeout):
        calls.append(args)
        assert "GMGN_PRIVATE_KEY" not in env
        return reply({
            "data": [{
                "chain": "robinhood",
                "tokens": [{
                    "address": TOKEN,
                    "symbol": "FAST",
                    "market_cap": 25_000,
                    "liquidity": 12_000,
                    "rank": 1,
                }],
            }],
        })

    monkeypatch.setattr(skills, "_run_cli", fake_run)

    first = skills.refresh_fast_hot_search(tmp_path, min_interval_seconds=15)
    second = skills.refresh_fast_hot_search(tmp_path, min_interval_seconds=15)

    assert first["ok"] is True
    assert first["row_count"] == 1
    assert second["ok"] is True
    assert second["skipped"] is True
    assert second["reason"] == "min_interval"
    assert len(calls) == 1
    rows = json.loads(
        (tmp_path / "meme-source-inbox/gmgn-skills-hot-searches.json").read_text()
    )["rows"]
    assert rows[0]["symbol"] == "FAST"
    assert rows[0]["observed_at"] == NOW
