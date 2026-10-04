import json

import alpha_okx_stream_export as stream


def test_missing_credentials_writes_status_and_inboxes(monkeypatch, tmp_path):
    monkeypatch.setattr(stream.alpha_okx_market, "credentials", lambda: ("", "", ""))
    status = stream.stream_once(
        wss_url=stream.WS_URL,
        chains=["56"],
        channels=["signal", "memepump"],
        outputs={
            "okx_signal": tmp_path / "okx-signal-stream.json",
            "okx_trenches": tmp_path / "okx-memepump-stream.json",
        },
        status_path=tmp_path / "status.json",
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is False
    assert status["reason"] == "missing_market_api_credentials"
    assert json.loads((tmp_path / "okx-signal-stream.json").read_text(encoding="utf-8"))["data"] == []
    assert json.loads((tmp_path / "okx-memepump-stream.json").read_text(encoding="utf-8"))["data"] == []


def test_subscribe_and_normalize_signal_memepump(monkeypatch, tmp_path):
    class TimeoutError(Exception):
        pass

    sent = []
    messages = [
        {"event": "login", "code": "0", "msg": "", "connId": "abc"},
        {"event": "subscribe", "arg": {"channel": stream.CHANNELS["signal"]["channel"], "chainIndex": "56"}},
        {
            "arg": {"channel": stream.CHANNELS["signal"]["channel"], "chainIndex": "56"},
            "signal": {
                "timestamp": "1773111278502",
                "token": {
                    "tokenAddress": "0xABC",
                    "symbol": "SIG",
                    "name": "Signal Dog",
                    "marketCapUsd": "65000",
                    "holders": "412",
                    "top10HolderPercent": "18.5",
                },
                "price": "0.00065",
                "walletType": "1",
                "triggerWalletCount": "4",
                "amountUsd": "1900",
            },
        },
        {
            "arg": {"channel": stream.CHANNELS["memepump"]["channel"], "chainIndex": "501"},
            "data": [
                {
                    "chainIndex": "501",
                    "protocolId": "136460",
                    "tokenAddress": "MintPump111111111111111111111111111111111",
                    "symbol": "PUMP",
                    "name": "Pump Launch",
                    "createdTimestamp": "1773111278502",
                    "market": {"marketCapUsd": "32000", "volumeUsd1h": "800", "txCount1h": "12"},
                    "bondingPercent": "41",
                    "tags": {"top10HoldingsPercent": "19", "bundlersPercent": "5", "totalHolders": "94"},
                    "social": {"dexScreenerPaid": True},
                }
            ],
        },
    ]
    ticks = iter([0, 0, 0.1, 0.2, 0.3, 0.4, 10])

    class FakeWebSocket:
        WebSocketTimeoutException = TimeoutError

        class Conn:
            def settimeout(self, _timeout):
                pass

            def send(self, payload):
                sent.append(payload)

            def recv(self):
                if messages:
                    return json.dumps(messages.pop(0))
                raise TimeoutError("empty")

            def close(self):
                pass

        @staticmethod
        def create_connection(*_args, **_kwargs):
            return FakeWebSocket.Conn()

    monkeypatch.setattr(stream.alpha_okx_market, "credentials", lambda: ("key", "secret", "pass"))
    monkeypatch.setattr(stream, "websocket", FakeWebSocket)
    monkeypatch.setattr(stream.time, "time", lambda: 1773111278)
    monkeypatch.setattr(stream.time, "monotonic", lambda: next(ticks, 10))

    outputs = {
        "okx_signal": tmp_path / "okx-signal-stream.json",
        "okx_trenches": tmp_path / "okx-memepump-stream.json",
    }
    status = stream.stream_once(
        wss_url=stream.WS_URL,
        chains=["56", "501"],
        channels=["signal", "memepump"],
        outputs=outputs,
        status_path=tmp_path / "status.json",
        max_rows=10,
        duration_seconds=1,
        timeout_seconds=1,
    )

    assert status["ok"] is True
    assert status["rows_seen"] == 2
    assert len(sent) == 2
    assert json.loads(sent[1])["args"] == [
        {"channel": stream.CHANNELS["signal"]["channel"], "chainIndex": "56"},
        {"channel": stream.CHANNELS["memepump"]["channel"], "chainIndex": "56"},
        {"channel": stream.CHANNELS["signal"]["channel"], "chainIndex": "501"},
        {"channel": stream.CHANNELS["memepump"]["channel"], "chainIndex": "501"},
    ]

    signal_rows = json.loads(outputs["okx_signal"].read_text(encoding="utf-8"))["data"]
    memepump_rows = json.loads(outputs["okx_trenches"].read_text(encoding="utf-8"))["data"]
    assert signal_rows[0]["source_family"] == "okx_signal"
    assert signal_rows[0]["smart_money"] == "4"
    assert signal_rows[0]["tokenAddress"] == "0xabc"
    assert memepump_rows[0]["source_family"] == "okx_trenches"
    assert memepump_rows[0]["chain"] == "solana"
    assert memepump_rows[0]["okx_bonding_percent"] == "41"
    assert memepump_rows[0]["market_data_pending"] is True
