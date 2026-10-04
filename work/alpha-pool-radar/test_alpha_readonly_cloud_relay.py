import gzip
import http.client
import json
import threading

import pytest

from alpha_readonly_cloud_relay import make_server


@pytest.fixture
def relay(tmp_path):
    def dex_fetcher(url):
        assert "api.dexscreener.com" in url
        return {
            "pairs": [
                {
                    "chainId": "bsc",
                    "priceUsd": "0.125",
                    "marketCap": 125000,
                    "liquidity": {"usd": 45000},
                    "volume": {"h24": 90000},
                    "priceChange": {"m5": 2.5, "h1": 11},
                }
            ]
        }

    server = make_server(
        0,
        report_path=tmp_path / "report.json",
        write_token="test-token",
        dex_fetcher=dex_fetcher,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, method, path, body=b"", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def test_report_upload_requires_token_and_persists_gzip(relay):
    payload = json.dumps({"meta": {"generated_at": "now"}, "meme_rows": []}).encode()
    compressed = gzip.compress(payload)
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Encoding": "gzip",
        "Content-Length": str(len(compressed)),
    }

    assert request(relay, "POST", "/api/report", compressed, headers)[0] == 200
    status, response_headers, body = request(
        relay, "GET", "/api/report", headers={"Accept-Encoding": "gzip"}
    )

    assert status == 200
    assert response_headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(body)) == json.loads(payload)
    assert relay.report_path.with_suffix(".json.gz").exists()


def test_upload_rejects_wrong_token_and_invalid_shape(relay):
    assert request(relay, "POST", "/api/report", b"{}", {"Content-Length": "2"})[0] == 401
    assert (
        request(
            relay,
            "POST",
            "/api/report",
            b"{}",
            {"Authorization": "Bearer test-token", "Content-Length": "2"},
        )[0]
        == 400
    )


def test_health_and_read_only_boundaries(relay):
    status, _, body = request(relay, "GET", "/api/health")
    assert status == 200
    assert json.loads(body)["report_ready"] is False
    assert request(relay, "POST", "/api/dex-price", b"x", {"Content-Length": "1"})[0] == 405
    assert request(relay, "GET", "/private")[0] == 404


def test_live_dex_quote_proxy(relay):
    status, _, body = request(relay, "GET", "/api/dex-price?chain=bsc&token=0xabc")
    payload = json.loads(body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["priceUsd"] == 0.125
    assert payload["liquidityUsd"] == 45000


def test_batch_quote_proxy_is_retired(relay):
    status, _, body = request(relay, "GET", "/api/dex-prices?targets=token:bsc:0xabc")

    assert status == 410
    assert json.loads(body)["error"] == "batch_quote_endpoint_retired"
