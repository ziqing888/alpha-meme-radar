"""Expose the terminal's realtime read models without exposing operator APIs."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


READ_ROUTES = frozenset(
    {"/api/terminal/snapshot", "/api/terminal/runtime", "/api/radar/report"}
)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def fetch_upstream(upstream: str, path: str) -> object:
    request = Request(f"{upstream.rstrip('/')}{path}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=8) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("upstream_response_too_large")
    return json.loads(body)


class ReadOnlyHandler(BaseHTTPRequestHandler):
    server_version = "TerminalReadOnly/1.0"

    def _allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        return origin if origin in self.server.allowed_origins else None

    def _send_json(self, status: int, payload: object, cors_origin: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Vary", "Origin")
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        origin = self._allowed_origin()
        if path not in READ_ROUTES:
            self._send_json(404, {"error": "not_found"}, origin)
            return
        if not origin:
            self._send_json(403, {"error": "origin_forbidden"})
            return
        try:
            payload = self.server.fetch(path)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
            self._send_json(502, {"error": "upstream_unavailable"}, origin)
            return
        self._send_json(200, payload, origin)

    def do_OPTIONS(self) -> None:
        path = urlsplit(self.path).path
        origin = self._allowed_origin()
        if path not in READ_ROUTES:
            self._send_json(404, {"error": "not_found"}, origin)
            return
        if not origin:
            self._send_json(403, {"error": "origin_forbidden"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_POST(self) -> None:
        self._send_json(405, {"error": "read_only"}, self._allowed_origin())

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, _format: str, *_args: object) -> None:
        return


def make_server(
    port: int,
    *,
    upstream: str,
    allowed_origin: str,
    radar_upstream: str = "http://127.0.0.1:8765",
    fetch=None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), ReadOnlyHandler)
    server.upstream = upstream
    server.radar_upstream = radar_upstream
    server.allowed_origins = frozenset(
        origin.strip().rstrip("/") for origin in allowed_origin.split(",") if origin.strip()
    )

    def default_fetch(path: str) -> object:
        if path == "/api/radar/report":
            return fetch_upstream(server.radar_upstream, "/report.json")
        return fetch_upstream(server.upstream, path)

    server.fetch = fetch or default_fetch
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--upstream", default="http://127.0.0.1:8771")
    parser.add_argument("--radar-upstream", default="http://127.0.0.1:8765")
    parser.add_argument("--allow-origin", required=True)
    args = parser.parse_args()
    server = make_server(
        args.port,
        upstream=args.upstream,
        radar_upstream=args.radar_upstream,
        allowed_origin=args.allow_origin,
    )
    print(json.dumps({"ready": True, "port": server.server_port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
