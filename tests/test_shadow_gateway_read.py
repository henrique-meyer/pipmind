"""Owner book and candle hops stay on loopback and cannot dispatch."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pipmind.shadow.gateway_read import loopback_base
from pipmind.shadow.service import create_app

SHADOW = "s" * 32
GATEWAY = "g" * 32


def _app(owner_reads):
    store = SimpleNamespace(status=lambda: {"mode": "shadow", "dispatches": 0})
    return create_app(store, object(), SHADOW, background=False, owner_reads=owner_reads)


def test_loopback_gate_refuses_public_and_credentialed_origins():
    assert loopback_base("http://127.0.0.1:8081") == "http://127.0.0.1:8081"
    assert loopback_base("http://localhost:8081") == "http://localhost:8081"
    assert loopback_base("http://[::1]:8081") == "http://[::1]:8081"
    for value in (
        "https://127.0.0.1:8081",
        "http://169.254.169.254/",
        "http://user:pass@127.0.0.1:8081",
        "http://127.0.0.1:8081/books",
        "http://127.0.0.1:8081?x=1",
        "http://gateway.internal:8081",
    ):
        assert loopback_base(value) is None
    app = _app(("http://169.254.169.254", GATEWAY))
    with TestClient(app) as client:
        assert (
            client.get("/books", headers={"Authorization": "Bearer " + SHADOW}).status_code == 404
        )


def test_owner_reads_force_dispatch_off_and_hide_upstream_failures():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/bars") and "timeframe=1d" in self.path:
                body = b'{"password":"do-not-leak"}'
                self.send_response(500)
            elif self.path.startswith("/instruments"):
                body = b'{"instruments":["EUR/USD"],"dispatch_enabled":true}'
                self.send_response(200)
            elif self.path.startswith("/bars"):
                body = b'{"instrument":"EUR/USD","timeframe":"1h","bars":[]}'
                self.send_response(200)
            else:
                body = b'{"dispatch_enabled":true,"positions":[]}'
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app = _app((f"http://127.0.0.1:{server.server_address[1]}", GATEWAY))
        shadow = {"Authorization": "Bearer " + SHADOW}
        gateway = {"Authorization": "Bearer " + GATEWAY}
        with TestClient(app) as client:
            assert client.get("/books").status_code == 401
            assert client.get("/status", headers=gateway).status_code == 401
            book = client.get("/books", headers=shadow)
            assert book.status_code == 200
            assert book.json()["dispatch_enabled"] is False
            assert seen[-1][1] == "Bearer " + GATEWAY
            assert client.get("/books?instrument=EUR/USD", headers=shadow).status_code == 404
            listed = client.get("/instruments", headers=gateway)
            assert listed.status_code == 200 and listed.json()["dispatch_enabled"] is False
            bars = client.get("/bars?instrument=EUR/USD&timeframe=1h", headers=shadow)
            assert bars.status_code == 200 and bars.json()["dispatch_enabled"] is False
            assert "timeframe=1h" in seen[-1][0]
            assert (
                client.get("/bars?instrument=EUR/USD&timeframe=no", headers=shadow).status_code
                == 404
            )
            failed = client.get("/bars?instrument=EUR/USD&timeframe=1d", headers=shadow)
            assert failed.status_code == 502
            assert failed.json() == {"error": "gateway_read_unavailable"}
            assert "do-not-leak" not in failed.text
            assert client.post("/orders", headers=shadow, json={}).status_code == 404
            assert client.post("/books", headers=shadow, json={}).status_code == 405
            assert client.post("/books", headers=gateway, json={}).status_code == 401
            assert all(
                item[0].split("?", 1)[0] in {"/books", "/instruments", "/bars"} for item in seen
            )
    finally:
        server.shutdown()
        server.server_close()


def test_redirects_are_not_followed():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/private")
            self.end_headers()

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app = _app((f"http://127.0.0.1:{server.server_address[1]}", GATEWAY))
        with TestClient(app) as client:
            response = client.get("/books", headers={"Authorization": "Bearer " + SHADOW})
            assert response.status_code == 502
            assert response.json() == {"error": "gateway_read_unavailable"}
            assert "private" not in response.text
    finally:
        server.shutdown()
        server.server_close()


def test_upstream_non_object_is_unavailable():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"[1]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app = _app((f"http://127.0.0.1:{server.server_address[1]}", GATEWAY))
        with TestClient(app) as client:
            response = client.get("/books", headers={"Authorization": "Bearer " + SHADOW})
            assert response.status_code == 502
            assert "1" not in json.dumps(response.json())
    finally:
        server.shutdown()
        server.server_close()
