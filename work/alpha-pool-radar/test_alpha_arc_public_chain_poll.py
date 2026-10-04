import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest


BASE = Path(__file__).parent
MODULE_PATH = BASE / "alpha_arc_public_chain_poll.py"


def load_module():
    spec = importlib.util.spec_from_file_location("alpha_arc_public_chain_poll", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def address(number: int) -> str:
    return f"0x{number:040x}"


def topic_address(value: str) -> str:
    return "0x" + ("0" * 24) + value[2:]


def word_address(value: str) -> str:
    return ("0" * 24) + value[2:]


def uint_word(value: int) -> str:
    return f"{value:064x}"


def dynamic_string(value: str) -> str:
    raw = value.encode("utf-8").hex()
    padded = raw + ("0" * ((64 - len(raw) % 64) % 64))
    return "0x" + uint_word(32) + uint_word(len(value.encode("utf-8"))) + padded


def bytes32_string(value: str) -> str:
    return "0x" + value.encode("utf-8").hex().ljust(64, "0")


def verified_row(contract: str, *, source: str = "arc_rpc", event_kind: str = "mint"):
    return {
        "contract_address": contract,
        "source": source,
        "event_kind": event_kind,
        "transaction_hash": "0xabc",
        "log_index": "0x1",
        "block_number": 100,
        "name": "Token",
        "symbol": "TKN",
        "decimals": 18,
        "total_supply": 1000,
    }


def test_validate_rpc_urls_requires_arc_chain_id(monkeypatch):
    poll = load_module()
    replies = {"https://wrong": "0x1", "https://arc": "0x13B2"}
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout_seconds: replies[url])

    valid, errors = poll.validate_rpc_urls(["https://wrong", "https://arc"], 1.5)

    assert valid == ["https://arc"]
    assert errors == [{"url": "https://wrong", "error": "chain_id_mismatch:0x1"}]


def test_run_once_falls_back_after_primary_endpoint_failure(monkeypatch, tmp_path):
    poll = load_module()
    fallback = "https://fallback"
    monkeypatch.setattr(
        poll,
        "validate_rpc_urls",
        lambda urls, timeout: ([fallback], [{"url": urls[0], "error": "offline"}]),
    )
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [verified_row(address(1))])
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    status = poll.run_once(tmp_path, ["https://primary", fallback], "https://scan")

    assert status["status"] == "degraded"
    assert status["rpc_url"] == fallback
    assert status["checkpoint_block"] == 100
    assert json.loads((tmp_path / "meme-source-inbox/arc-onchain.json").read_text())["count"] == 1


def test_run_once_rechecks_unresolved_candidate_on_fallback_endpoint(monkeypatch, tmp_path):
    poll = load_module()
    token = address(1)
    primary = "https://primary"
    fallback = "https://fallback"
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: (urls, []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")

    def collect(url, start, end, timeout):
        if url == primary:
            raise poll.UnresolvedCandidateError(
                f"unresolved_candidate:{token}:eth_getCode:HTTP Error 429: Too Many Requests"
            )
        return [verified_row(token)]

    monkeypatch.setattr(poll, "collect_rpc_events", collect)
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    status = poll.run_once(tmp_path, [primary, fallback], "")

    assert status["ok"] is True
    assert status["rpc_url"] == fallback
    assert status["checkpoint_advanced"] is True
    assert status["rpc_errors"] == [{
        "url": primary,
        "error": f"unresolved_candidate:{token}:eth_getCode:HTTP Error 429: Too Many Requests",
    }]


def test_run_once_uses_fallback_for_metadata_without_requiring_duplicate_logs(monkeypatch, tmp_path):
    poll = load_module()
    token = address(1)
    primary = "https://primary"
    fallback = "https://fallback"
    transfer = {
        "address": token,
        "topics": [poll.TRANSFER_TOPIC, poll.ZERO_ADDRESS_TOPIC, topic_address(address(2))],
        "data": "0x" + uint_word(1000),
        "transactionHash": "0xmetadata-fallback",
        "logIndex": "0x1",
        "blockNumber": "0x64",
    }
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: (urls, []))
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getLogs":
            if url == primary and params[0]["topics"][0] == poll.TRANSFER_TOPIC:
                return [transfer]
            return []
        if method == "eth_getCode":
            if url == primary:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            return "0x6000"
        assert method == "eth_call"
        assert url == fallback
        return {
            poll.NAME_SELECTOR: dynamic_string("Token"),
            poll.SYMBOL_SELECTOR: bytes32_string("TKN"),
            poll.DECIMALS_SELECTOR: "0x" + uint_word(18),
            poll.TOTAL_SUPPLY_SELECTOR: "0x" + uint_word(1000),
        }[params[0]["data"]]

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    status = poll.run_once(tmp_path, [primary, fallback], "")

    assert status["ok"] is True
    assert status["rpc_url"] == primary
    assert status["checkpoint_advanced"] is True
    assert json.loads((tmp_path / "meme-source-inbox/arc-onchain.json").read_text())["count"] == 1


def test_main_uses_official_fallback_when_circle_endpoint_is_unavailable(monkeypatch, tmp_path):
    poll = load_module()

    def rpc_call(url, method, params, timeout_seconds):
        if method == "eth_chainId":
            if url == "https://rpc.mainnet.arc.io":
                raise urllib.error.URLError("primary unavailable")
            if url == "https://rpc.blockdaemon.mainnet.arc.io":
                return "0x13b2"
            raise urllib.error.URLError("endpoint unavailable")
        if method == "eth_blockNumber":
            return "0x64"
        raise AssertionError(f"unexpected rpc method: {method}")

    monkeypatch.setattr(poll, "rpc_call", rpc_call)
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [])
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))
    monkeypatch.setattr(sys, "argv", ["alpha_arc_public_chain_poll.py", "--out-dir", str(tmp_path)])

    assert poll.main() == 0
    status = json.loads((tmp_path / "arc-public-chain-poll-status.json").read_text())
    assert status["rpc_url"] == "https://rpc.blockdaemon.mainnet.arc.io"


def test_main_prefers_the_log_scan_capable_official_rpc(monkeypatch, tmp_path):
    poll = load_module()
    monkeypatch.setattr(
        poll,
        "rpc_call",
        lambda url, method, params, timeout: "0x13b2" if method == "eth_chainId" else "0x64",
    )
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [])
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))
    monkeypatch.setattr(sys, "argv", ["alpha_arc_public_chain_poll.py", "--out-dir", str(tmp_path)])

    assert poll.main() == 0
    status = json.loads((tmp_path / "arc-public-chain-poll-status.json").read_text())
    assert status["rpc_url"] == "https://rpc.blockdaemon.mainnet.arc.io"


def test_run_once_writes_status_inbox_and_checkpoint_to_the_given_output_directory(monkeypatch, tmp_path):
    poll = load_module()
    output = tmp_path / "outputs"
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [verified_row(address(1))])
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    status = poll.run_once(output, ["https://arc"], "https://scan")

    assert status["chain_id"] == "0x13b2"
    assert (output / "arc-public-chain-poll-status.json").is_file()
    assert (output / "meme-source-inbox/arc-onchain.json").is_file()
    assert (output / "arc-public-chain-poll-state.json").is_file()
    assert not (output / "outputs").exists()


def test_all_rpc_endpoints_failing_preserves_last_good_inbox(monkeypatch, tmp_path):
    poll = load_module()
    inbox = tmp_path / "meme-source-inbox/arc-onchain.json"
    inbox.parent.mkdir(parents=True)
    previous = {"count": 1, "data": [{"tokenAddress": address(9)}]}
    inbox.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(
        poll,
        "validate_rpc_urls",
        lambda urls, timeout: ([], [{"url": url, "error": "offline"} for url in urls]),
    )

    status = poll.run_once(tmp_path, ["https://one", "https://two"], "https://scan")

    assert status["status"] == "error"
    assert status["preserved_previous_rows"] == 1
    assert json.loads(inbox.read_text()) == previous
    assert not (tmp_path / "arc-public-chain-poll-state.json").exists()


def test_run_once_does_not_report_chain_id_without_a_validated_endpoint(monkeypatch, tmp_path):
    poll = load_module()
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([], [{"url": "https://bad", "error": "offline"}]))

    status = poll.run_once(tmp_path, ["https://bad"], "https://scan")

    assert status["status"] == "error"
    assert "chain_id" not in status


def test_cli_defaults_to_the_repository_output_directory(monkeypatch):
    poll = load_module()
    monkeypatch.setattr(sys, "argv", ["alpha_arc_public_chain_poll.py"])

    args = poll.parse_args()

    assert args.out_dir == poll.ROOT / "outputs"


def test_zero_address_transfer_becomes_one_verified_discovery(monkeypatch):
    poll = load_module()
    token = address(1)
    transfer = {
        "address": token,
        "topics": [poll.TRANSFER_TOPIC, poll.ZERO_ADDRESS_TOPIC, topic_address(address(2))],
        "data": "0x" + uint_word(1000),
        "transactionHash": "0xaaa",
        "logIndex": "0x2",
        "blockNumber": "0x10",
    }

    def fake_rpc(url, method, params, timeout):
        if method == "eth_getLogs":
            return [transfer] if params[0]["topics"][0] == poll.TRANSFER_TOPIC else []
        if method == "eth_getCode":
            return "0x6000"
        selector = params[0]["data"]
        return {
            poll.NAME_SELECTOR: dynamic_string("Token"),
            poll.SYMBOL_SELECTOR: bytes32_string("TKN"),
            poll.DECIMALS_SELECTOR: "0x" + uint_word(18),
            poll.TOTAL_SUPPLY_SELECTOR: "0x" + uint_word(1000),
        }[selector]

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    rows = poll.collect_rpc_events("https://arc", 1, 20, 1.0)

    assert [(row["contract_address"], row["event_kind"]) for row in rows] == [(token, "erc20_mint")]


def test_v2_v3_v4_events_extract_tokens_but_never_pool_addresses(monkeypatch):
    poll = load_module()
    token1, token2, token3, token4, token5, token6 = [address(i) for i in range(1, 7)]
    pair, pool = address(101), address(102)
    logs = {
        poll.PAIR_CREATED_TOPIC: [{
            "address": address(201),
            "topics": [poll.PAIR_CREATED_TOPIC, topic_address(token1), topic_address(token2)],
            "data": "0x" + word_address(pair) + uint_word(1),
            "transactionHash": "0xv2", "logIndex": "0x1", "blockNumber": "0x10",
        }],
        poll.POOL_CREATED_TOPIC: [{
            "address": address(202),
            "topics": [poll.POOL_CREATED_TOPIC, topic_address(token3), topic_address(token4), uint_word(3000)],
            "data": "0x" + uint_word(60) + word_address(pool),
            "transactionHash": "0xv3", "logIndex": "0x2", "blockNumber": "0x11",
        }],
        poll.INITIALIZE_TOPIC: [{
            "address": address(203),
            "topics": [poll.INITIALIZE_TOPIC, "0x" + ("ab" * 32), topic_address(token5), topic_address(token6)],
            "data": "0x" + uint_word(0) * 4,
            "transactionHash": "0xv4", "logIndex": "0x3", "blockNumber": "0x12",
        }],
    }

    def fake_rpc(url, method, params, timeout):
        if method == "eth_getLogs":
            return logs.get(params[0]["topics"][0], [])
        if method == "eth_getCode":
            return "0x6000"
        selector = params[0]["data"]
        return {
            poll.NAME_SELECTOR: dynamic_string("Token"),
            poll.SYMBOL_SELECTOR: bytes32_string("TKN"),
            poll.DECIMALS_SELECTOR: "0x" + uint_word(18),
            poll.TOTAL_SUPPLY_SELECTOR: "0x" + uint_word(1000),
        }[selector]

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    rows = poll.collect_rpc_events("https://arc", 1, 20, 1.0)

    assert {row["contract_address"] for row in rows} == {token1, token2, token3, token4, token5, token6}
    assert pair not in {row["contract_address"] for row in rows}
    assert pool not in {row["contract_address"] for row in rows}
    assert {row.get("pool_address") for row in rows if row.get("pool_address")} == {pair, pool}


def test_arcscan_tokens_and_contracts_normalize_to_arc(monkeypatch):
    poll = load_module()
    token, contract = address(1), address(2)

    def fake_http(url, timeout):
        if "/tokens?" in url:
            return {"items": [{"address_hash": token, "id": "token-1", "holderCount": 7}]}
        assert "/smart-contracts?" in url
        return {"items": [{"address": {"hash": contract}, "id": "contract-2"}]}

    monkeypatch.setattr(poll, "_http_json", fake_http)
    rows, errors = poll.collect_arcscan_candidates("https://explorer.arc.io/api/v2", 25, 1.0)
    normalized = poll.normalize_discoveries([], rows, "2026-09-16T00:00:00Z")

    assert errors == []
    assert {row["chain"] for row in normalized} == {"arc"}
    assert {row["contract_address"] for row in normalized} == {token, contract}
    assert "holders" not in next(row for row in normalized if row["contract_address"] == contract)


def test_disabled_arcscan_source_is_neutral(monkeypatch):
    poll = load_module()
    monkeypatch.setattr(
        poll,
        "_http_json",
        lambda url, timeout: (_ for _ in ()).throw(AssertionError("indexer should stay disabled")),
    )

    rows, errors = poll.collect_arcscan_candidates("", 25, 1.0)

    assert rows == []
    assert errors == []


def test_rpc_and_arcscan_sightings_deduplicate_to_one_onchain_identity():
    poll = load_module()
    token = address(1)
    rpc = verified_row(token)
    scan = {**verified_row(token, source="arc_arcscan", event_kind="arcscan_token"), "arcscan_id": "token-1"}

    rows = poll.normalize_discoveries([rpc], [scan], "2026-09-16T00:00:00Z")

    assert len(rows) == 1
    assert rows[0]["identity"] == f"arc:{token}"
    assert rows[0]["provider_family"] == "onchain"
    assert rows[0]["provider_feeds"] == ["arc_arcscan", "arc_rpc"]
    assert len(rows[0]["event_ids"]) == 2


def test_checkpoint_resume_overlaps_and_recent_event_ids_remove_duplicates(monkeypatch, tmp_path):
    poll = load_module()
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    duplicate = verified_row(address(1))
    duplicate_id = poll._stable_event_id(duplicate)
    state_path.write_text(json.dumps({"last_completed_block": 100, "recent_event_ids": [duplicate_id]}))
    calls = []
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x69")
    monkeypatch.setattr(
        poll,
        "collect_rpc_events",
        lambda url, start, end, timeout: calls.append((start, end)) or [duplicate, {**verified_row(address(2)), "transaction_hash": "0xdef"}],
    )
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")
    inbox = json.loads((tmp_path / "meme-source-inbox/arc-onchain.json").read_text())

    assert calls == [(95, 105)]
    assert status["checkpoint_block"] == 105
    assert [row["contract_address"] for row in inbox["data"]] == [address(2)]


@pytest.mark.parametrize("failing_name", ("arc-onchain.json", "arc-public-chain-poll-status.json"))
def test_checkpoint_does_not_advance_when_inbox_or_status_persistence_fails(monkeypatch, tmp_path, failing_name):
    poll = load_module()
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_completed_block": 50, "recent_event_ids": []}))
    real_atomic = poll._atomic_json

    def failing_atomic(path, payload):
        if path.name == failing_name:
            raise OSError("disk full")
        return real_atomic(path, payload)

    monkeypatch.setattr(poll, "_atomic_json", failing_atomic)
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [verified_row(address(1))])
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    with pytest.raises(OSError, match="disk full"):
        poll.run_once(tmp_path, ["https://arc"], "https://scan")

    assert json.loads(state_path.read_text())["last_completed_block"] == 50


def test_candidate_validation_rejects_bad_address_empty_code_and_non_erc20(monkeypatch):
    poll = load_module()
    monkeypatch.setattr(poll, "rpc_call", lambda *args: "0x")
    assert poll._verify_erc20("https://arc", "0x1234", 1.0).status == "rejected"
    assert poll._verify_erc20("https://arc", address(1), 1.0).status == "rejected"

    def non_erc20(url, method, params, timeout):
        if method == "eth_getCode":
            return "0x6000"
        return "0x"

    monkeypatch.setattr(poll, "rpc_call", non_erc20)
    assert poll._verify_erc20("https://arc", address(2), 1.0).status == "rejected"


@pytest.mark.parametrize("required_selector", ("code", "decimals", "total_supply"))
def test_transient_required_metadata_failure_is_unknown_and_does_not_advance_checkpoint(
    monkeypatch, tmp_path, required_selector
):
    poll = load_module()
    token = address(1)
    transfer = {
        "address": token,
        "topics": [poll.TRANSFER_TOPIC, poll.ZERO_ADDRESS_TOPIC, topic_address(address(2))],
        "data": "0x" + uint_word(1000),
        "transactionHash": "0xmetadata-timeout",
        "logIndex": "0x2",
        "blockNumber": "0x60",
    }
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_completed_block": 90, "recent_event_ids": []}))
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getLogs":
            return [transfer] if params[0]["topics"][0] == poll.TRANSFER_TOPIC else []
        if method == "eth_getCode":
            if required_selector == "code":
                raise TimeoutError("code timeout")
            return "0x6000"
        selector = params[0]["data"]
        if (required_selector == "decimals" and selector == poll.DECIMALS_SELECTOR) or (
            required_selector == "total_supply" and selector == poll.TOTAL_SUPPLY_SELECTOR
        ):
            raise TimeoutError(f"{required_selector} timeout")
        return {
            poll.NAME_SELECTOR: dynamic_string("Token"),
            poll.SYMBOL_SELECTOR: bytes32_string("TKN"),
            poll.DECIMALS_SELECTOR: "0x" + uint_word(18),
            poll.TOTAL_SUPPLY_SELECTOR: "0x" + uint_word(1000),
        }[selector]

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")

    assert status["status"] == "error"
    assert status["checkpoint_advanced"] is False
    assert "unresolved_candidate" in status["rpc_errors"][-1]["error"]
    assert json.loads(state_path.read_text())["last_completed_block"] == 90


def test_incomplete_log_evidence_is_quarantined_without_checkpoint_advance(monkeypatch, tmp_path):
    poll = load_module()
    malformed = {
        "address": address(1),
        "topics": [poll.TRANSFER_TOPIC, poll.ZERO_ADDRESS_TOPIC, topic_address(address(2))],
        "data": "0x" + uint_word(1000),
    }
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_completed_block": 90, "recent_event_ids": []}))
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getLogs":
            return [malformed] if params[0]["topics"][0] == poll.TRANSFER_TOPIC else []
        raise AssertionError(f"unexpected metadata call: {method}")

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")

    assert status["status"] == "error"
    assert status["checkpoint_advanced"] is False
    assert "incomplete_log_evidence" in status["rpc_errors"][-1]["error"]
    assert json.loads(state_path.read_text())["last_completed_block"] == 90


def test_observed_unresolved_evidence_cannot_be_hidden_by_an_empty_fallback(monkeypatch, tmp_path):
    poll = load_module()
    malformed = {
        "address": address(1),
        "topics": [poll.TRANSFER_TOPIC, poll.ZERO_ADDRESS_TOPIC, topic_address(address(2))],
        "data": "0x" + uint_word(1000),
    }
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_completed_block": 90, "recent_event_ids": []}))
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: (urls, []))
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getLogs":
            if url == "https://one" and params[0]["topics"][0] == poll.TRANSFER_TOPIC:
                return [malformed]
            return []
        raise AssertionError(f"unexpected metadata call: {method}")

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    status = poll.run_once(tmp_path, ["https://one", "https://two"], "https://scan")

    assert status["status"] == "error"
    assert status["checkpoint_advanced"] is False
    assert json.loads(state_path.read_text())["last_completed_block"] == 90


def test_unresolved_arcscan_candidate_degrades_without_checkpoint_advance(monkeypatch, tmp_path):
    poll = load_module()
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_completed_block": 90, "recent_event_ids": []}))
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [])
    monkeypatch.setattr(
        poll,
        "collect_arcscan_candidates",
        lambda base, limit, timeout: ([{"contract_address": address(1), "source": "arc_arcscan"}], []),
    )

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getCode":
            raise TimeoutError("arcscan code timeout")
        raise AssertionError(f"unexpected call: {method}")

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")

    assert status["status"] == "degraded"
    assert status["checkpoint_advanced"] is False
    assert status["arcscan_errors"][-1]["error"].startswith("unresolved_candidate")
    assert json.loads(state_path.read_text())["last_completed_block"] == 90


def test_stale_checkpoint_processes_one_bounded_cycle_and_commits_only_processed_block(monkeypatch, tmp_path):
    poll = load_module()
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_completed_block": 100, "recent_event_ids": []}))
    ranges = []
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x1388")
    monkeypatch.setattr(
        poll,
        "collect_rpc_events",
        lambda url, start, end, timeout: ranges.append((start, end)) or [],
    )
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")
    state = json.loads(state_path.read_text())
    expected_end = 95 + poll.MAX_BLOCKS_PER_CYCLE - 1

    assert ranges == [(95, expected_end)]
    assert status["head_block"] == 5000
    assert status["checkpoint_block"] == expected_end
    assert state["last_completed_block"] == expected_end


def test_each_normalized_identity_bounds_retained_event_ids():
    poll = load_module()
    token = address(1)
    rows = [
        {**verified_row(token), "transaction_hash": f"0x{index:x}", "log_index": hex(index)}
        for index in range(poll.MAX_EVENT_IDS_PER_IDENTITY + 1)
    ]

    normalized = poll.normalize_discoveries(rows, [], "2026-09-16T00:00:00Z")

    assert len(normalized) == 1
    assert len(normalized[0]["event_ids"]) == poll.MAX_EVENT_IDS_PER_IDENTITY
    assert normalized[0]["event_id"] in normalized[0]["event_ids"]


def test_dynamic_and_bytes32_erc20_strings_both_decode():
    poll = load_module()
    assert poll._decode_abi_string(dynamic_string("Arc Token")) == "Arc Token"
    assert poll._decode_abi_string(bytes32_string("ARC")) == "ARC"


def test_arcscan_empty_indexes_are_reported_as_lag_and_degrade_status(monkeypatch, tmp_path):
    poll = load_module()
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [verified_row(address(1))])
    monkeypatch.setattr(
        poll,
        "collect_arcscan_candidates",
        lambda base, limit, timeout: ([], [{"url": base, "error": "arcscan_lag"}]),
    )

    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")

    assert status["status"] == "degraded"
    assert status["row_count"] == 1
    assert status["arcscan_errors"] == [{"url": "https://scan", "error": "arcscan_lag"}]


def test_monitor_artifacts_are_bounded_to_recent_event_limit(monkeypatch, tmp_path):
    poll = load_module()
    rows = [
        {**verified_row(address(index + 1)), "transaction_hash": f"0x{index:x}"}
        for index in range(poll.RECENT_EVENT_LIMIT + 1)
    ]
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: rows)
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    poll.run_once(tmp_path, ["https://arc"], "https://scan")
    inbox = json.loads((tmp_path / "meme-source-inbox/arc-onchain.json").read_text())
    state = json.loads((tmp_path / "arc-public-chain-poll-state.json").read_text())

    assert inbox["count"] == poll.RECENT_EVENT_LIMIT
    assert len(inbox["data"]) == poll.RECENT_EVENT_LIMIT
    assert len(state["recent_event_ids"]) == poll.RECENT_EVENT_LIMIT


def test_cli_surface_is_one_shot_and_monitor_only(monkeypatch, tmp_path):
    poll = load_module()
    monkeypatch.setattr(
        sys,
        "argv",
        ["poll", "--out-dir", str(tmp_path), "--rpc-url", "https://one", "--rpc-url", "https://two", "--timeout-seconds", "2.5"],
    )

    args = poll.parse_args()

    assert args.out_dir == tmp_path
    assert args.rpc_urls == ["https://one", "https://two"]
    assert args.timeout_seconds == 2.5
    assert set(vars(args)) == {"out_dir", "rpc_urls", "arcscan_base_url", "timeout_seconds"}


def test_rpc_call_preserves_structured_json_rpc_error(monkeypatch):
    poll = load_module()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": 3, "message": "execution reverted", "data": "0x08c379a0"},
            }).encode()

    monkeypatch.setattr(poll.urllib.request, "urlopen", lambda request, timeout: Response())

    with pytest.raises(poll.RpcResponseError) as captured:
        poll.rpc_call("https://arc", "eth_call", [], 1.0)

    assert captured.value.code == 3
    assert captured.value.message == "execution reverted"
    assert captured.value.data == "0x08c379a0"


@pytest.mark.parametrize(
    "error_envelope",
    (
        "query exceeds max results 2000",
        {"message": "query exceeds max results 2000"},
        {"code": "-32602", "message": "query exceeds max results 2000"},
        {"code": -32602, "message": ["query exceeds max results 2000"]},
    ),
)
def test_malformed_rpc_error_envelopes_are_not_range_split(monkeypatch, error_envelope):
    poll = load_module()
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "error": error_envelope}).encode()

    def urlopen(request, timeout):
        calls.append(json.loads(request.data.decode()))
        return Response()

    monkeypatch.setattr(poll.urllib.request, "urlopen", urlopen)

    with pytest.raises(poll.MalformedRpcResponseError, match="invalid_rpc_error"):
        poll.collect_rpc_events("https://arc", 100, 109, 1.0)
    assert len(calls) == 1


def test_zero_padded_decimal_range_hint_is_parsed_as_decimal(monkeypatch):
    poll = load_module()
    ranges = []

    def fake_rpc(url, method, params, timeout):
        request = params[0]
        start = int(request["fromBlock"], 16)
        end = int(request["toBlock"], 16)
        ranges.append((start, end))
        if (start, end) == (100, 109):
            raise poll.RpcResponseError(
                -32602,
                "query exceeds max results 2000, retry with the range 0100-0103",
            )
        return []

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    assert poll.collect_rpc_events("https://arc", 100, 109, 1.0) == []
    assert ranges[:3] == [(100, 109), (100, 103), (104, 109)]


def test_unusable_numeric_range_hint_falls_back_to_ascending_bisection(monkeypatch):
    poll = load_module()
    ranges = []
    unusable_number = "9" * 5000

    def fake_rpc(url, method, params, timeout):
        request = params[0]
        start = int(request["fromBlock"], 16)
        end = int(request["toBlock"], 16)
        ranges.append((start, end))
        if (start, end) == (100, 109):
            raise poll.RpcResponseError(
                -32602,
                f"query exceeds max results 2000, retry with the range {unusable_number}-0103",
            )
        return []

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    assert poll.collect_rpc_events("https://arc", 100, 109, 1.0) == []
    assert ranges[:3] == [(100, 109), (100, 104), (105, 109)]


def test_explicit_result_limit_uses_strict_prefix_hint_in_ascending_order(monkeypatch):
    poll = load_module()
    token = address(1)
    transfer = {
        "address": token,
        "topics": [poll.TRANSFER_TOPIC, poll.ZERO_ADDRESS_TOPIC, topic_address(address(2))],
        "data": "0x" + uint_word(1000),
        "transactionHash": "0xhinted",
        "logIndex": "0x1",
        "blockNumber": "0x64",
    }
    log_ranges = []

    def fake_rpc(url, method, params, timeout):
        if method == "eth_getLogs":
            request = params[0]
            start = int(request["fromBlock"], 16)
            end = int(request["toBlock"], 16)
            topic = request["topics"][0]
            log_ranges.append((topic, start, end))
            if topic == poll.TRANSFER_TOPIC and (start, end) == (100, 109):
                raise poll.RpcResponseError(
                    -32602,
                    "request exceeded max allowed range: query exceeds max results 2000, "
                    "retry with the range 100-103",
                    {"limit": 2000},
                )
            return [transfer] if topic == poll.TRANSFER_TOPIC and (start, end) == (100, 103) else []
        if method == "eth_getCode":
            return "0x6000"
        return {
            poll.NAME_SELECTOR: dynamic_string("Token"),
            poll.SYMBOL_SELECTOR: bytes32_string("TKN"),
            poll.DECIMALS_SELECTOR: "0x" + uint_word(18),
            poll.TOTAL_SUPPLY_SELECTOR: "0x" + uint_word(1000),
        }[params[0]["data"]]

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    rows = poll.collect_rpc_events("https://arc", 100, 109, 1.0)

    assert [(start, end) for topic, start, end in log_ranges if topic == poll.TRANSFER_TOPIC] == [
        (100, 109),
        (100, 103),
        (104, 109),
    ]
    assert [(row["contract_address"], row["transaction_hash"]) for row in rows] == [(token, "0xhinted")]


@pytest.mark.parametrize(
    "hint",
    (
        "retry with the range 101-103",
        "retry with the range 100-109",
        "retry with the range 99-103",
    ),
)
def test_invalid_or_non_prefix_provider_hint_falls_back_to_bisection(monkeypatch, hint):
    poll = load_module()
    ranges = []

    def fake_rpc(url, method, params, timeout):
        request = params[0]
        start = int(request["fromBlock"], 16)
        end = int(request["toBlock"], 16)
        ranges.append((start, end))
        if (start, end) == (100, 109):
            raise poll.RpcResponseError(-32602, f"query exceeds max results 2000, {hint}")
        return []

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    assert poll.collect_rpc_events("https://arc", 100, 109, 1.0) == []
    assert ranges[:3] == [(100, 109), (100, 104), (105, 109)]


@pytest.mark.parametrize("failure_kind", ("execution", "timeout", "http", "malformed"))
def test_non_range_log_failures_are_not_split(monkeypatch, failure_kind):
    poll = load_module()
    ranges = []

    def fake_rpc(url, method, params, timeout):
        request = params[0]
        ranges.append((int(request["fromBlock"], 16), int(request["toBlock"], 16)))
        if failure_kind == "execution":
            raise poll.RpcResponseError(3, "execution reverted", "0x08c379a0")
        if failure_kind == "timeout":
            raise TimeoutError("timed out")
        if failure_kind == "http":
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        return {"unexpected": "shape"}

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    with pytest.raises((poll.RpcResponseError, TimeoutError, urllib.error.HTTPError, RuntimeError)):
        poll.collect_rpc_events("https://arc", 100, 109, 1.0)
    assert ranges == [(100, 109)]


def test_log_query_budget_exhaustion_is_explicit_and_bounded(monkeypatch):
    poll = load_module()
    monkeypatch.setattr(poll, "MAX_LOG_QUERIES_PER_CYCLE", 2)
    ranges = []

    def fake_rpc(url, method, params, timeout):
        request = params[0]
        ranges.append((int(request["fromBlock"], 16), int(request["toBlock"], 16)))
        raise poll.RpcResponseError(-32602, "query exceeds max results 2000")

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    with pytest.raises(poll.LogRangeProcessingError, match="log_query_budget_exhausted"):
        poll.collect_rpc_events("https://arc", 1, 4, 1.0)
    assert ranges == [(1, 4), (1, 2)]


def test_single_block_result_limit_is_explicit_and_not_retried(monkeypatch):
    poll = load_module()
    ranges = []

    def fake_rpc(url, method, params, timeout):
        request = params[0]
        ranges.append((int(request["fromBlock"], 16), int(request["toBlock"], 16)))
        raise poll.RpcResponseError(-32602, "query exceeds max results 2000")

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    with pytest.raises(poll.LogRangeProcessingError, match="log_range_unresolved_single_block:7"):
        poll.collect_rpc_events("https://arc", 7, 7, 1.0)
    assert ranges == [(7, 7)]


def test_run_once_shares_log_query_budget_and_preserves_committed_checkpoint(monkeypatch, tmp_path):
    poll = load_module()
    monkeypatch.setattr(poll, "MAX_LOG_QUERIES_PER_CYCLE", 2)
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.write_text(json.dumps({"last_completed_block": 90, "recent_event_ids": []}))
    log_calls = []
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: (urls, []))

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getLogs":
            request = params[0]
            log_calls.append((url, int(request["fromBlock"], 16), int(request["toBlock"], 16)))
            raise poll.RpcResponseError(-32602, "query exceeds max results 2000")
        raise AssertionError(f"unexpected call: {method}")

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)

    status = poll.run_once(tmp_path, ["https://one", "https://two"], "https://scan")

    assert status["status"] == "error"
    assert status["checkpoint_advanced"] is False
    assert status["checkpoint_block"] == 90
    assert status["log_queries_used"] == 2
    assert status["log_query_budget"] == 2
    assert "log_query_budget_exhausted" in status["rpc_errors"][-1]["error"]
    assert len(log_calls) == 2
    assert json.loads(state_path.read_text())["last_completed_block"] == 90
    assert not (tmp_path / "meme-source-inbox/arc-onchain.json").exists()


@pytest.mark.parametrize("reverting_selector", ("decimals", "total_supply"))
def test_reverting_contract_is_rejected_while_valid_token_publishes_and_checkpoint_advances(
    monkeypatch, tmp_path, reverting_selector
):
    poll = load_module()
    valid = address(1)
    reverting = address(2)

    def transfer(contract, tx_hash, log_index):
        return {
            "address": contract,
            "topics": [poll.TRANSFER_TOPIC, poll.ZERO_ADDRESS_TOPIC, topic_address(address(9))],
            "data": "0x" + uint_word(1000),
            "transactionHash": tx_hash,
            "logIndex": log_index,
            "blockNumber": "0x64",
        }

    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getLogs":
            if params[0]["topics"][0] == poll.TRANSFER_TOPIC:
                return [transfer(valid, "0xvalid", "0x1"), transfer(reverting, "0xrevert", "0x2")]
            return []
        if method == "eth_getCode":
            return "0x6000"
        contract = params[0]["to"]
        selector = params[0]["data"]
        rejected_selector = {
            "decimals": poll.DECIMALS_SELECTOR,
            "total_supply": poll.TOTAL_SUPPLY_SELECTOR,
        }[reverting_selector]
        if contract == reverting and selector == rejected_selector:
            raise poll.RpcResponseError(3, "execution reverted: selector not recognized", "0x")
        return {
            poll.NAME_SELECTOR: dynamic_string("Token"),
            poll.SYMBOL_SELECTOR: bytes32_string("TKN"),
            poll.DECIMALS_SELECTOR: "0x" + uint_word(18),
            poll.TOTAL_SUPPLY_SELECTOR: "0x" + uint_word(1000),
        }[selector]

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")
    inbox = json.loads((tmp_path / "meme-source-inbox/arc-onchain.json").read_text())
    state = json.loads((tmp_path / "arc-public-chain-poll-state.json").read_text())

    assert status["ok"] is True
    assert status["checkpoint_advanced"] is True
    assert status["checkpoint_block"] == 100
    assert [row["contract_address"] for row in inbox["data"]] == [valid]
    assert state["last_completed_block"] == 100


def test_arcscan_activity_aliases_reach_final_inbox_with_field_provenance(monkeypatch, tmp_path):
    poll = load_module()
    token = address(3)
    raw_arcscan = {
        "contractAddress": token,
        "id": "arcscan-token-3",
        "name": "Scan Token",
        "symbol": "SCAN",
        "holderCount": 41,
        "totalSupply": 777_000,
        "firstTransferBlock": 11,
        "last_transfer_block": 99,
        "transferCount": 120,
        "transfer_count_24h": 20,
        "activityCount": 140,
    }

    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [])
    monkeypatch.setattr(
        poll,
        "_http_json",
        lambda url, timeout: {"data": [raw_arcscan]} if "/tokens?" in url else {"data": []},
    )

    def fake_rpc(url, method, params, timeout):
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getCode":
            return "0x6000"
        selector = params[0]["data"]
        return {
            poll.NAME_SELECTOR: dynamic_string("Scan Token"),
            poll.SYMBOL_SELECTOR: bytes32_string("SCAN"),
            poll.DECIMALS_SELECTOR: "0x" + uint_word(18),
            poll.TOTAL_SUPPLY_SELECTOR: "0x" + uint_word(777_000),
        }[selector]

    monkeypatch.setattr(poll, "rpc_call", fake_rpc)
    status = poll.run_once(tmp_path, ["https://arc"], "https://scan")
    row = json.loads((tmp_path / "meme-source-inbox/arc-onchain.json").read_text())["data"][0]

    assert status["ok"] is True
    assert row["holders"] == 41
    assert row["holder_count"] == 41
    assert row["total_supply"] == 777_000
    assert row["first_transfer_block"] == 11
    assert row["last_transfer_block"] == 99
    assert row["transfer_count"] == 120
    assert row["transfer_count_24h"] == 20
    assert row["activity_count"] == 140
    for field in (
        "holders",
        "holder_count",
        "total_supply",
        "first_transfer_block",
        "last_transfer_block",
        "transfer_count",
        "transfer_count_24h",
        "activity_count",
    ):
        assert row["field_sources"][field] == "arc_arcscan"
    assert "arc_arcscan" in row["provider_feeds"]


@pytest.mark.parametrize(
    ("adapter_status", "expected"),
    [
        ({"status": "ok", "ok": True}, 0),
        ({"status": "degraded", "ok": True}, 0),
        ({"status": "degraded", "ok": False}, 1),
        ({"status": "error", "ok": False}, 1),
    ],
)
def test_cli_exit_code_depends_on_ok_not_status_label(monkeypatch, tmp_path, adapter_status, expected):
    poll = load_module()
    monkeypatch.setattr(
        poll,
        "parse_args",
        lambda: poll.argparse.Namespace(
            out_dir=tmp_path,
            rpc_urls=["https://arc"],
            arcscan_base_url="https://scan",
            timeout_seconds=1.0,
        ),
    )
    monkeypatch.setattr(poll, "run_once", lambda **kwargs: adapter_status)

    assert poll.main() == expected


def test_state_write_failure_leaves_durable_status_at_committed_checkpoint(monkeypatch, tmp_path):
    poll = load_module()
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_completed_block": 50, "recent_event_ids": []}))
    real_atomic = poll._atomic_json

    def failing_state_write(path, payload):
        if path.name == "arc-public-chain-poll-state.json":
            raise OSError("disk full")
        return real_atomic(path, payload)

    monkeypatch.setattr(poll, "_atomic_json", failing_state_write)
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [verified_row(address(1))])
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    with pytest.raises(OSError, match="disk full"):
        poll.run_once(tmp_path, ["https://arc"], "https://scan")

    status = json.loads((tmp_path / "arc-public-chain-poll-status.json").read_text())
    assert status["status"] == "degraded"
    assert status["ok"] is False
    assert status["error"] == "checkpoint_commit_pending"
    assert status["checkpoint_advanced"] is False
    assert status["checkpoint_block"] == 50
    assert json.loads(state_path.read_text())["last_completed_block"] == 50


def test_precommit_status_write_failure_preserves_prior_inbox_status_and_checkpoint(monkeypatch, tmp_path):
    poll = load_module()
    inbox_path = tmp_path / "meme-source-inbox/arc-onchain.json"
    status_path = tmp_path / "arc-public-chain-poll-status.json"
    state_path = tmp_path / "arc-public-chain-poll-state.json"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    previous_inbox = {
        "source": "arc_onchain",
        "observed_at": "2026-09-16T02:00:00Z",
        "count": 1,
        "data": [verified_row(address(9))],
    }
    previous_status = {
        "status": "ok",
        "ok": True,
        "checkpoint_advanced": True,
        "checkpoint_block": 50,
    }
    previous_state = {"last_completed_block": 50, "recent_event_ids": []}
    inbox_path.write_text(json.dumps(previous_inbox))
    status_path.write_text(json.dumps(previous_status))
    state_path.write_text(json.dumps(previous_state))
    real_atomic = poll._atomic_json
    status_attempts = 0

    def fail_first_status_write(path, payload):
        nonlocal status_attempts
        if path.name == "arc-public-chain-poll-status.json":
            status_attempts += 1
            if status_attempts == 1:
                raise OSError("status disk full")
        return real_atomic(path, payload)

    monkeypatch.setattr(poll, "_atomic_json", fail_first_status_write)
    monkeypatch.setattr(poll, "validate_rpc_urls", lambda urls, timeout: ([urls[0]], []))
    monkeypatch.setattr(poll, "rpc_call", lambda url, method, params, timeout: "0x64")
    monkeypatch.setattr(poll, "collect_rpc_events", lambda url, start, end, timeout: [verified_row(address(1))])
    monkeypatch.setattr(poll, "collect_arcscan_candidates", lambda base, limit, timeout: ([], []))

    with pytest.raises(OSError, match="status disk full"):
        poll.run_once(tmp_path, ["https://arc"], "https://scan")

    assert json.loads(inbox_path.read_text()) == previous_inbox
    assert json.loads(status_path.read_text()) == previous_status
    assert json.loads(state_path.read_text()) == previous_state
