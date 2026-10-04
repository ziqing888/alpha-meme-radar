import http.client
import json
import threading

import pytest

from alpha_terminal_readonly_bridge import make_server


@pytest.fixture
def bridge():
    calls = []

    def fetch(path):
        calls.append(path)
        return {"path": path, "live": True}

    server = make_server(
        0,
        upstream="http://127.0.0.1:8771",
        allowed_origin=(
            "https://terminal-ui-iota.vercel.app,"
            "https://vercel-site-eta-blush.vercel.app"
        ),
        fetch=fetch,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, calls
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, path, method="GET", origin=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    headers = {"Origin": origin} if origin else {}
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


@pytest.mark.parametrize("path", ["/api/terminal/snapshot", "/api/terminal/runtime"])
def test_allows_only_realtime_read_routes(bridge, path):
    server, calls = bridge
    status, headers, body = request(
        server, path, origin="https://terminal-ui-iota.vercel.app"
    )

    assert status == 200
    assert json.loads(body) == {"path": path, "live": True}
    assert headers["Access-Control-Allow-Origin"] == "https://terminal-ui-iota.vercel.app"
    assert headers["Cache-Control"] == "no-store"
    assert calls == [path]


def test_allows_radar_report_for_monitor_origin(bridge):
    server, calls = bridge
    status, headers, body = request(
        server,
        "/api/radar/report",
        origin="https://vercel-site-eta-blush.vercel.app",
    )

    assert status == 200
    assert json.loads(body) == {"path": "/api/radar/report", "live": True}
    assert headers["Access-Control-Allow-Origin"] == "https://vercel-site-eta-blush.vercel.app"
    assert calls == ["/api/radar/report"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/terminal/session",
        "/api/terminal/wallet/import",
        "/api/terminal/strategy/command",
        "/.env",
    ],
)
def test_rejects_every_non_read_route(bridge, path):
    server, calls = bridge
    assert request(
        server, path, origin="https://terminal-ui-iota.vercel.app"
    )[0] == 404
    assert calls == []


def test_rejects_mutations_and_wrong_origins(bridge):
    server, calls = bridge
    allowed = "https://terminal-ui-iota.vercel.app"

    assert request(server, "/api/terminal/snapshot", "POST", allowed)[0] == 405
    assert request(server, "/api/terminal/snapshot", origin="https://evil.example")[0] == 403
    assert request(server, "/api/terminal/snapshot")[0] == 403
    assert calls == []
