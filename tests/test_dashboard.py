"""PIP-68 observation boundary exercised through HTTP, without live services."""

import asyncio
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import dashboard as module
from app import main

ORIGIN = "https://dashboard.example"
PASSWORD = "synthetic-test-password-only"


@pytest.fixture
def configured(monkeypatch):
    state = module.Dashboard(
        module.Config(
            ORIGIN,
            "https://internal.example",
            module.password_hash(PASSWORD),
            b"synthetic-session-signing-key-only-0123456789",
            "synthetic-shadow-token",
            "synthetic-feeds-token",
        ),
        main.ROOT,
    )
    # Existing routes close over their original Dashboard; replace only its state.
    current = main.app.state.dashboard
    monkeypatch.setattr(current, "config", state.config)
    for key in ("sessions", "attempts", "reads", "cache"):
        monkeypatch.setattr(current, key, {})
    return current


@pytest.fixture
def client(configured):
    with TestClient(main.app, base_url=ORIGIN, follow_redirects=False) as instance:
        yield instance


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def signin(client):
    page = client.get("/app/login")
    response = client.post(
        "/app/login", data={"csrf": csrf(page), "password": PASSWORD}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 303 and response.headers["location"] == "/app"
    return response


def test_config_disabled_and_invalid_fails_closed(monkeypatch):
    monkeypatch.delenv("PIPMIND_DASHBOARD_ENABLED", raising=False)
    assert module.Config.environment() is None
    monkeypatch.setenv("PIPMIND_DASHBOARD_ENABLED", "true")
    assert module.Config.environment() is None
    monkeypatch.setattr(main.app.state.dashboard, "config", None)
    with TestClient(main.app) as client:
        assert client.get("/app").status_code == 503
        assert client.get("/app/api/status").status_code == 503
        assert client.get("/").status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        "http://internal.example",
        "https://u:p@internal.example",
        "https://internal.example/path",
        "https://internal.example?x=1",
        "https://internal.example#secret",
        "https://internal.example\\evil",
        "https://internal.example\n",
    ],
)
def test_private_origins_are_fixed_https(value):
    with pytest.raises(ValueError):
        module.origin(value)


def test_complete_login_read_logout_flow(client, configured, monkeypatch):
    calls = []

    def read(config, name):
        calls.append(name)
        return {"mode": "shadow", "paused": True, "pending": 3}

    monkeypatch.setattr(module, "fetch_read", read)
    assert client.get("/app").headers["location"] == "/app/login"
    assert client.get("/app/api/status").status_code == 401
    assert not calls
    response = signin(client)
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    page = client.get("/app")
    assert page.status_code == 200 and "Recent reasoning" in page.text
    assert "analytics.js" not in page.text and "og:" not in page.text
    for path in ("/app", "/app/login", "/app/api/status", "/app/resources/dashboard.js"):
        response = client.get(path)
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-robots-tag"] == "noindex"
        assert response.headers["referrer-policy"] == "same-origin"
        assert "google" not in response.headers["content-security-policy"]
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert len(calls) == 1  # Bounded shared 30-second cache.
    assert client.get("/app/api/status").json()["data"]["pending"] == 3
    previous_cookie = client.cookies.get(module.SESSION_COOKIE)
    assert (
        client.post(
            "/app/logout", data={"csrf": csrf(page)}, headers={"Origin": ORIGIN}
        ).status_code
        == 303
    )
    assert client.get("/app/api/status").status_code == 401
    # Explicit logout revokes a stolen copy, not merely the browser cookie.
    assert (
        client.get(
            "/app/api/status", headers={"Cookie": module.SESSION_COOKIE + "=" + previous_cookie}
        ).status_code
        == 401
    )
    assert configured.sessions == {}


def test_login_origin_csrf_limits_and_clean_failure_redirect(client):
    page = client.get("/app/login")
    data = {"csrf": csrf(page), "password": PASSWORD}
    assert client.post("/app/login", data=data).status_code == 403
    assert client.post("/app/login", data=data, headers={"Origin": "null"}).status_code == 403
    assert (
        client.post("/app/login", data=data, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert (
        client.post(
            "/app/login", data={**data, "csrf": "é"}, headers={"Origin": ORIGIN}
        ).status_code
        == 403
    )
    response = client.post(
        "/app/login", data={**data, "password": "wrong"}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 303 and response.headers["location"] == "/app/login"
    page = client.get("/app/login")
    assert "Sign-in failed" in page.text and "wrong" not in page.text
    assert "Sign-in failed" not in client.get("/app/login").text
    for _ in range(4):
        page = client.get("/app/login")
        response = client.post(
            "/app/login", data={"csrf": csrf(page), "password": "wrong"}, headers={"Origin": ORIGIN}
        )
    assert response.status_code == 429 and response.headers["Retry-After"] == "300"


def test_session_tampering_expiry_and_logout_csrf(client, configured, monkeypatch):
    signin(client)
    page = client.get("/app")
    assert client.post("/app/logout", data={"csrf": csrf(page)}).status_code == 403
    assert (
        client.post(
            "/app/logout", data={"csrf": csrf(page)}, headers={"Origin": "null"}
        ).status_code
        == 403
    )
    assert (
        client.post("/app/logout", data={"csrf": "wrong"}, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    assert (
        client.get(
            "/app/api/status", headers={"Cookie": module.SESSION_COOKIE + "=malformed"}
        ).status_code
        == 401
    )
    assert configured.signed("é", "session") is None
    now = time.time()
    monkeypatch.setattr(module.time, "time", lambda: now + module.SESSION_SECONDS + 1)
    assert client.get("/app/api/status").status_code == 401
    assert client.get("/app").headers["location"] == "/app/login"


def test_allowlist_query_path_and_method_refusal(client, monkeypatch):
    calls = []
    monkeypatch.setattr(module, "fetch_read", lambda config, name: calls.append(name) or {})
    signin(client)
    for path in (
        "/app/api/owner",
        "/app/api/status?url=https://evil.example",
        "/app/api/status?path=/pause",
        "/app/api/..%2Fowner",
        "/app/api/https:%2F%2Fevil.example",
    ):
        assert client.get(path).status_code == 404
    assert client.post("/app/api/status").status_code == 405
    assert not calls
    for name in module.PATHS:
        assert client.get("/app/api/" + name).status_code == 200
    assert calls == list(module.PATHS)


def test_upstream_failure_redacted_cached_and_recovered(client, configured, monkeypatch):
    calls = []

    def failed(config, name):
        calls.append(name)
        raise OSError("private-host-and-token-not-for-browser")

    monkeypatch.setattr(module, "fetch_read", failed)
    signin(client)
    for _ in range(2):
        response = client.get("/app/api/observation")
        assert response.status_code == 503 and response.json()["data"] is None
        assert "private-host" not in response.text
    assert len(calls) == 1
    now = time.time()
    monkeypatch.setattr(module.time, "time", lambda: now + 31)
    monkeypatch.setattr(module, "fetch_read", lambda config, name: {"status": "partial"})
    assert client.get("/app/api/observation").json()["data"]["status"] == "partial"


def test_fixed_transport_no_redirect_proxy_or_end_user_forwarding(configured, monkeypatch):
    requests = []

    class Headers:
        def get_content_type(self):
            return "application/json"

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, bound):
            return json.dumps({"paused": True, "secret_config": "never"}).encode()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 8
            return Response()

    def opener(*handlers):
        assert any(isinstance(handler, module.NoRedirect) for handler in handlers)
        assert any(
            isinstance(handler, module.ProxyHandler) and handler.proxies == {}
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr(module, "build_opener", opener)
    result = module.fetch_read(configured.config, "status")
    assert "secret_config" not in result
    module.fetch_read(configured.config, "coverage")
    assert [request.full_url for request in requests] == [
        "https://internal.example/status",
        "https://internal.example/feeds/coverage",
    ]
    assert all(request.get_method() == "GET" for request in requests)
    assert requests[0].headers["Authorization"] == "Bearer synthetic-shadow-token"
    assert requests[1].headers["Authorization"] == "Bearer synthetic-feeds-token"
    assert (
        module.NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.example")
        is None
    )


def test_books_panel_is_read_only_and_private(client, monkeypatch):
    monkeypatch.setattr(
        module,
        "fetch_read",
        lambda config, name: {
            "availability": "stale",
            "orders_state": "stale",
            "stale": True,
            "account": None,
            "positions": None,
            "working_orders": [],
        },
    )
    signin(client)
    page = client.get("/app")
    assert "Positions and orders" in page.text
    assert "cannot place, change, or cancel" in page.text
    assert page.text.count("<form") == 1 and 'action="/app/logout"' in page.text
    assert page.text.count('type="submit"') == 1
    landing = client.get("/")
    assert "Positions and orders" not in landing.text
    assert "/app/api/books" not in landing.text
    assert client.get("/app/api/books?instrument=EUR/USD").status_code == 404
    assert client.post("/app/api/books").status_code == 405
    script = client.get("/app/resources/dashboard.js").text
    assert "innerHTML" not in script and "STALE · not a live book" in script
    assert "No order was placed, modified, or cancelled." in script


def test_books_transport_strips_secrets_and_uses_the_gateway(configured, monkeypatch):
    configured.config = module.Config(
        ORIGIN,
        "https://internal.example",
        configured.config.password,
        configured.config.secret,
        "synthetic-shadow-token",
        "synthetic-feeds-token",
        "https://gateway.example",
        "synthetic-gateway-token-0123456789",
    )
    requests = []

    class Headers:
        def get_content_type(self):
            return "application/json"

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, bound):
            return json.dumps(
                {
                    "availability": "stale",
                    "dispatch_enabled": True,
                    "account": {"balance": 1, "password": "DO-NOT-LEAK"},
                    "secret": "DO-NOT-LEAK",
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 8
            return Response()

    def opener(*handlers):
        assert any(isinstance(handler, module.NoRedirect) for handler in handlers)
        assert any(
            isinstance(handler, module.ProxyHandler) and handler.proxies == {}
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr(module, "build_opener", opener)
    result = module.fetch_read(configured.config, "books")
    assert requests[0].full_url == "https://gateway.example/books"
    assert requests[0].get_method() == "GET"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-gateway-token-0123456789"
    assert result["dispatch_enabled"] is False
    assert "password" not in result["account"]
    assert "secret" not in result
    assert "DO-NOT-LEAK" not in json.dumps(result)


def test_books_without_gateway_stays_unavailable_and_unforwarded(configured):
    try:
        module.fetch_read(configured.config, "books")
    except ValueError as error:
        assert str(error) == "dashboard_upstream_response"
    else:
        raise AssertionError("missing gateway was contacted")


def test_private_content_not_discovered_and_script_uses_safe_dom(client):
    assert "/app" not in client.get("/sitemap.xml").text
    assert client.get("/app/resources/dashboard.js").status_code == 401
    signin(client)
    script = client.get("/app/resources/dashboard.js").text
    assert "innerHTML" not in script and "textContent" in script
    assert "setInterval(refresh, 30000)" in script
    assert "document.hidden" in script and "AbortSignal.timeout" in script
    assert "https://" not in script and "Bearer" not in script


def test_read_rate_limit_and_memory_bounds(client, configured, monkeypatch):
    monkeypatch.setattr(module, "fetch_read", lambda config, name: {})
    signin(client)
    for _ in range(18):
        assert client.get("/app/api/status").status_code == 200
    assert client.get("/app/api/status").status_code == 429
    for index in range(256):
        configured.allowed(configured.attempts, "synthetic-" + str(index), 5, 300)
    assert len(configured.attempts) <= 256
    assert not configured.allowed(configured.attempts, "new-key-after-capacity", 5, 300)


def test_real_socket_login_observe_logout(configured, monkeypatch):
    """Run a real ASGI socket as behind a TLS terminator, passing Secure cookies explicitly."""
    monkeypatch.setattr(module, "fetch_read", lambda config, name: {"paused": True})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", follow_redirects=False) as client:
            page = client.get("/app/login")
            cookies = SimpleCookie()
            for value in page.headers.get_list("set-cookie"):
                cookies.load(value)
            login_cookie = cookies[module.LOGIN_COOKIE].value
            response = client.post(
                "/app/login",
                data={"password": PASSWORD, "csrf": csrf(page)},
                headers={"Origin": ORIGIN, "Cookie": module.LOGIN_COOKIE + "=" + login_cookie},
            )
            assert response.status_code == 303
            for value in response.headers.get_list("set-cookie"):
                cookies.load(value)
            headers = {"Cookie": module.SESSION_COOKIE + "=" + cookies[module.SESSION_COOKIE].value}
            page = client.get("/app", headers=headers)
            assert page.status_code == 200
            assert client.get("/app/api/status", headers=headers).json()["data"]["paused"] is True
            assert (
                client.post(
                    "/app/logout", data={"csrf": csrf(page)}, headers={**headers, "Origin": ORIGIN}
                ).status_code
                == 303
            )
            assert client.get("/app/api/status", headers=headers).status_code == 401
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()


def test_concurrent_http_logins_reject_without_queued_hashes(configured, monkeypatch):
    """A stalled production hasher cannot multiply memory use on concurrent HTTP logins."""
    real_hash = module.password_hash
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    counts = {"active": 0, "maximum": 0, "calls": 0}

    def blocked_hash(password, salt):
        with guard:
            counts["active"] += 1
            counts["calls"] += 1
            counts["maximum"] = max(counts["maximum"], counts["active"])
        entered.set()
        try:
            assert release.wait(5)
            return real_hash(password, salt)
        finally:
            with guard:
                counts["active"] -= 1

    monkeypatch.setattr(module, "password_hash", blocked_hash)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", follow_redirects=False) as client:
            page = client.get("/app/login")
            cookies = SimpleCookie()
            for value in page.headers.get_list("set-cookie"):
                cookies.load(value)
            headers = {
                "Origin": ORIGIN,
                "Cookie": module.LOGIN_COOKIE + "=" + cookies[module.LOGIN_COOKIE].value,
            }
            data = {"password": PASSWORD, "csrf": csrf(page)}
            with ThreadPoolExecutor(max_workers=1) as requests:
                first = requests.submit(client.post, "/app/login", data=data, headers=headers)
                assert entered.wait(3)
                try:
                    for _ in range(2):
                        response = client.post("/app/login", data=data, headers=headers)
                        assert response.status_code == 429
                        assert response.json() == {"error": "login_busy"}
                    with guard:
                        assert counts == {"active": 1, "maximum": 1, "calls": 1}
                finally:
                    release.set()
                assert first.result(timeout=5).status_code == 303
            # Busy attempts never queued another hash, and a later valid login recovers.
            response = client.post("/app/login", data=data, headers=headers)
            assert response.status_code == 303 and response.headers["location"] == "/app"
            assert counts == {"active": 0, "maximum": 1, "calls": 2}
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()


def test_hash_slot_survives_caller_cancellation_and_recovers(configured, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = module.password_hash

    def blocked_hash(password, salt):
        entered.set()
        try:
            assert release.wait(5)
            return original(password, salt)
        finally:
            finished.set()

    monkeypatch.setattr(module, "password_hash", blocked_hash)

    async def scenario():
        task = asyncio.create_task(configured.hash_candidate(PASSWORD, b"synthetic-salt16"))
        deadline = time.monotonic() + 3
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await configured.hash_candidate(PASSWORD, b"synthetic-salt16") is None
        assert not configured.hash_gate.acquire(blocking=False)
        release.set()
        deadline = time.monotonic() + 3
        while not finished.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert finished.is_set()
        # Worker-finally releases after this stub returns, independently of its canceled caller.
        for _ in range(100):
            if configured.hash_gate.acquire(blocking=False):
                configured.hash_gate.release()
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("password slot was not released by the completed worker")
        candidate = await configured.hash_candidate(PASSWORD, b"synthetic-salt16")
        assert candidate == original(PASSWORD, b"synthetic-salt16")

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_chart_is_private_and_rebuilds_its_query(client, configured, monkeypatch):
    configured.config = module.Config(
        ORIGIN,
        "https://internal.example",
        configured.config.password,
        configured.config.secret,
        "synthetic-shadow-token",
        "synthetic-feeds-token",
        "https://gateway.example",
        "synthetic-gateway-token-0123456789",
    )
    calls = []

    def fetch_read(config, name):
        calls.append(name)
        if name == "instruments":
            return {"schema_version": 1, "instruments": ["GBP/JPY", "EUR/USD"]}
        raise ValueError("dashboard_upstream_response")

    def fetch_bars(config, instrument, timeframe):
        calls.append((config.gateway, instrument, timeframe))
        return {
            "schema_version": 1,
            "instrument": instrument,
            "timeframe": timeframe,
            "price": "mid",
            "checked_at": "2026-10-05T14:00:00Z",
            "fetched_at": "2026-10-05T14:00:00Z",
            "availability": "closed",
            "market_state": "closed",
            "stale": True,
            "age_seconds": 30,
            "history_partial": False,
            "forming": False,
            "bars": [
                {
                    "ts": "2026-10-05T13:00:00Z",
                    "open": 1.1,
                    "high": 1.2,
                    "low": 1.05,
                    "close": 1.11,
                }
            ],
        }

    monkeypatch.setattr(module, "fetch_read", fetch_read)
    monkeypatch.setattr(module, "fetch_bars", fetch_bars)
    signin(client)
    page = client.get("/app")
    landing = client.get("/")
    assert "Price chart" in page.text and 'data-timeframe="2h"' in page.text
    assert "/app/resources/lightweight-charts.js" in page.text
    assert "unpkg" not in page.text and "jsdelivr" not in page.text
    assert "Price chart" not in landing.text and "lightweight-charts" not in landing.text
    assert "/app/api/bars" not in landing.text
    assert client.get("/app/api/bars").status_code == 404
    assert client.get("/app/api/bars?instrument=EUR/USD&timeframe=3h").status_code == 404
    assert client.get("/app/api/bars?instrument=EUR/USD&timeframe=1h&extra=1").status_code == 404
    assert client.get("/app/api/bars?instrument=USD/JPY&timeframe=1h").status_code == 404
    body = client.get("/app/api/bars?instrument=EUR/USD&timeframe=1h").json()
    assert body["status"] == "ok" and body["data"]["availability"] == "closed"
    assert calls[-1] == ("https://gateway.example", "EUR/USD", "1h")
    assert "instruments" in calls
    script = client.get("/app/resources/dashboard.js").text
    library = client.get("/app/resources/lightweight-charts.js").text
    assert "innerHTML" not in script and "https://" not in script
    assert "Market closed · not a live quote" in script and "EUR/USD" not in script
    assert "createChart" in library and "unpkg" not in library and "jsdelivr" not in library


def test_candle_transport_strips_fields_and_builds_the_gateway_query(configured, monkeypatch):
    configured.config = module.Config(
        ORIGIN,
        "https://internal.example",
        configured.config.password,
        configured.config.secret,
        "synthetic-shadow-token",
        "synthetic-feeds-token",
        "https://gateway.example",
        "synthetic-gateway-token-0123456789",
    )
    requests = []

    class Headers:
        def get_content_type(self):
            return "application/json"

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, bound):
            return json.dumps(
                {
                    "instrument": "EUR/USD",
                    "timeframe": "4h",
                    "price": "mid",
                    "checked_at": "2026-10-05T14:00:00Z",
                    "fetched_at": "2026-10-05T14:00:00Z",
                    "availability": "stale",
                    "market_state": "open",
                    "stale": True,
                    "age_seconds": 90,
                    "history_partial": False,
                    "forming": False,
                    "bars": [
                        {
                            "ts": "2026-10-05T12:00:00Z",
                            "open": 1.1,
                            "high": 1.2,
                            "low": 1.05,
                            "close": 1.11,
                            "volume": 9,
                        }
                    ],
                    "market_id": 42,
                    "password": "DO-NOT-LEAK",
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 8
            return Response()

    def opener(*handlers):
        assert any(isinstance(handler, module.NoRedirect) for handler in handlers)
        assert any(
            isinstance(handler, module.ProxyHandler) and handler.proxies == {}
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr(module, "build_opener", opener)
    result = module.fetch_bars(configured.config, "EUR/USD", "4h")
    assert requests[0].full_url == "https://gateway.example/bars?instrument=EUR%2FUSD&timeframe=4h"
    assert requests[0].get_method() == "GET"
    assert requests[0].headers["Authorization"] == "Bearer synthetic-gateway-token-0123456789"
    assert result["bars"] == [
        {
            "ts": "2026-10-05T12:00:00Z",
            "open": 1.1,
            "high": 1.2,
            "low": 1.05,
            "close": 1.11,
        }
    ]
    assert "market_id" not in result and "password" not in result
    assert "DO-NOT-LEAK" not in json.dumps(result)
    try:
        module.fetch_bars(
            module.Config(
                ORIGIN,
                "https://internal.example",
                configured.config.password,
                configured.config.secret,
                "synthetic-shadow-token",
                "synthetic-feeds-token",
            ),
            "EUR/USD",
            "4h",
        )
    except ValueError as error:
        assert str(error) == "dashboard_upstream_response"
    else:
        raise AssertionError("missing gateway was contacted")
