"""Owner-only observation BFF. This module has no backend write operation."""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from fastapi import Request as WebRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SESSION_COOKIE = "__Host-pipmind_session"
LOGIN_COOKIE = "__Host-pipmind_login"
FLASH_COOKIE = "__Host-pipmind_message"
SESSION_SECONDS = 3600
MAX_RESPONSE = 524288
PATHS = {
    "status": "/status",
    "coverage": "/feeds/coverage",
    "observation": "/feeds/observation",
    "books": "/books",
    "instruments": "/instruments",
}
TIMEFRAMES = {"15m", "1h", "2h", "4h", "1d"}
BOOK_KEYS = (
    "schema_version",
    "checked_at",
    "availability",
    "orders_state",
    "stale",
    "age_seconds",
    "orders_age_seconds",
    "account",
    "positions",
    "annotations",
    "working_orders",
    "orders_partial",
    "orders_as_of",
    "orders_scope",
    "marks",
    "dispatch_enabled",
)
PRIVATE_CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def password_hash(password, salt=None):
    """Generate privately; the plaintext password is never persisted by this module."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=67108864)
    return f"scrypt$32768$8$1${b64(salt)}${b64(digest)}"


def valid_hash(value):
    try:
        method, n, r, p, salt, digest = value.split("$")
        return (
            (method, n, r, p) == ("scrypt", "32768", "8", "1")
            and len(unb64(salt)) == 16
            and len(unb64(digest)) == 64
        )
    except (ValueError, TypeError):
        return False


def origin(value):
    parsed = urlsplit(value)
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or any(ord(char) < 33 for char in value)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.netloc.endswith(":")
    ):
        raise ValueError("dashboard_origin_invalid")
    # Accessing port validates malformed ports without echoing private configuration.
    _ = parsed.port
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    origin: str
    backend: str
    password: str
    secret: bytes
    shadow_token: str
    feeds_token: str
    gateway: str | None = None
    gateway_token: str | None = None

    @classmethod
    def environment(cls):
        if os.environ.get("PIPMIND_DASHBOARD_ENABLED") != "true":
            return None
        try:
            prefix = "PIPMIND_DASHBOARD_"
            values = {
                key: os.environ.get(prefix + key, "")
                for key in (
                    "ORIGIN",
                    "BACKEND_URL",
                    "PASSWORD_HASH",
                    "SESSION_SECRET",
                    "SHADOW_TOKEN",
                    "FEEDS_TOKEN",
                )
            }
            if (
                not valid_hash(values["PASSWORD_HASH"])
                or len(values["SESSION_SECRET"].encode()) < 32
                or len(values["SESSION_SECRET"].encode()) > 512
                or not all(
                    re.fullmatch(r"[\x21-\x7e]{16,4096}", values[key])
                    for key in ("SHADOW_TOKEN", "FEEDS_TOKEN")
                )
            ):
                return None
            gateway, gateway_token = None, None
            raw_gateway = os.environ.get(prefix + "GATEWAY_URL", "")
            raw_gateway_token = os.environ.get(prefix + "GATEWAY_TOKEN", "")
            # Optional. Invalid gateway settings disable the book read only.
            if raw_gateway or raw_gateway_token:
                try:
                    if re.fullmatch(r"[\x21-\x7e]{32,4096}", raw_gateway_token):
                        gateway, gateway_token = origin(raw_gateway), raw_gateway_token
                except ValueError:
                    gateway, gateway_token = None, None
            return cls(
                origin(values["ORIGIN"]),
                origin(values["BACKEND_URL"]),
                values["PASSWORD_HASH"],
                values["SESSION_SECRET"].encode(),
                values["SHADOW_TOKEN"],
                values["FEEDS_TOKEN"],
                gateway,
                gateway_token,
            )
        except (ValueError, UnicodeError):
            return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _pick(value, keys):
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in keys}


def _rows(value, keys, limit):
    if not isinstance(value, list):
        return []
    return [_pick(row, keys) for row in value[:limit] if isinstance(row, dict)]


def _book_projection(result):
    account_keys = (
        "schema_version",
        "as_of",
        "currency",
        "balance",
        "equity",
        "margin_used",
        "margin_available",
    )
    position_keys = ("instrument", "side", "size", "average_price", "unrealized_pnl")
    order_keys = (
        "instrument",
        "side",
        "size",
        "status",
        "broker_order_id",
        "ts",
        "stop",
        "limit",
        "trigger",
        "role",
    )
    annotation_keys = ("stop", "limit", "protection", "quote_pnl", "mark_stale")
    mark_keys = ("instrument", "bid", "ask", "venue_ts", "stale", "source")
    positions = result.get("positions")
    projected = {key: result.get(key) for key in BOOK_KEYS}
    projected["account"] = _pick(result.get("account"), account_keys)
    projected["positions"] = (
        {
            "schema_version": positions.get("schema_version"),
            "as_of": positions.get("as_of"),
            "positions": _rows(positions.get("positions"), position_keys, 500),
        }
        if isinstance(positions, dict)
        else None
    )
    projected["annotations"] = _rows(result.get("annotations"), annotation_keys, 500)
    projected["working_orders"] = _rows(result.get("working_orders"), order_keys, 200)
    projected["marks"] = _rows(result.get("marks"), mark_keys, 50)
    projected["dispatch_enabled"] = False
    return projected


def chart_query(raw):
    """Rebuild the only accepted chart query. The browser string is never forwarded."""
    if not raw or len(raw) > 80:
        raise ValueError("dashboard_chart_query")
    try:
        parsed = parse_qs(raw, strict_parsing=True, max_num_fields=2)
    except ValueError:
        raise ValueError("dashboard_chart_query") from None
    if set(parsed) != {"instrument", "timeframe"} or any(
        len(values) != 1 for values in parsed.values()
    ):
        raise ValueError("dashboard_chart_query")
    instrument, timeframe = parsed["instrument"][0], parsed["timeframe"][0]
    if not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", instrument) or timeframe not in TIMEFRAMES:
        raise ValueError("dashboard_chart_query")
    return instrument, timeframe


def _finite_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("dashboard_upstream_shape")
    if value != value or abs(value) == float("inf") or not value > 0:
        raise ValueError("dashboard_upstream_shape")
    return value


def _instrument_projection(result):
    rows = result.get("instruments") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError("dashboard_upstream_shape")
    names = []
    for item in rows:
        if (
            not isinstance(item, str)
            or not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", item)
            or item in names
        ):
            raise ValueError("dashboard_upstream_shape")
        names.append(item)
    return {"schema_version": 1, "instruments": names}


def _candle_projection(result, instrument, timeframe):
    if not isinstance(result, dict):
        raise ValueError("dashboard_upstream_shape")
    bars = []
    raw_bars = result.get("bars")
    if not isinstance(raw_bars, list) or len(raw_bars) > 240:
        raise ValueError("dashboard_upstream_shape")
    for row in raw_bars:
        if not isinstance(row, dict):
            raise ValueError("dashboard_upstream_shape")
        bars.append(
            {
                "ts": row.get("ts"),
                "open": _finite_price(row.get("open")),
                "high": _finite_price(row.get("high")),
                "low": _finite_price(row.get("low")),
                "close": _finite_price(row.get("close")),
            }
        )
    availability = result.get("availability")
    market_state = result.get("market_state")
    if availability not in {"fresh", "stale", "unavailable", "closed"} or market_state not in {
        "open",
        "closed",
    }:
        raise ValueError("dashboard_upstream_shape")
    if result.get("price") != "mid" or result.get("instrument") != instrument:
        raise ValueError("dashboard_upstream_shape")
    if result.get("timeframe") != timeframe or not isinstance(result.get("stale"), bool):
        raise ValueError("dashboard_upstream_shape")
    return {
        "schema_version": 1,
        "instrument": instrument,
        "timeframe": timeframe,
        "price": "mid",
        "checked_at": result.get("checked_at"),
        "fetched_at": result.get("fetched_at"),
        "availability": availability,
        "market_state": market_state,
        "stale": result.get("stale"),
        "age_seconds": result.get("age_seconds"),
        "history_partial": result.get("history_partial") is True,
        "forming": result.get("forming") is True,
        "bars": bars,
    }


def broker_endpoint(config, path):
    """Direct gateway origin when both settings exist; otherwise the shadow ingress hop."""
    if config.gateway and config.gateway_token:
        return config.gateway + path, config.gateway_token
    return config.backend + path, config.shadow_token


def fetch_read(config, name):
    """Only fixed GET paths; no proxies, redirect following, or request-derived headers."""
    if name in {"books", "instruments"}:
        target, token = broker_endpoint(config, PATHS[name])
    else:
        token = config.shadow_token if name == "status" else config.feeds_token
        target = config.backend + PATHS[name]
    request = Request(
        target,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        method="GET",
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("dashboard_upstream_response")
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("dashboard_upstream_bound")

    def invalid_constant(value):
        raise ValueError("dashboard_upstream_nonfinite")

    result = json.loads(data, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("dashboard_upstream_shape")
    if name == "status":
        # Never publish qualification bundles, configuration, or unbounded runtime fields.
        result = {
            key: result.get(key)
            for key in (
                "mode",
                "paused",
                "revision",
                "outcomes",
                "pending",
                "dispatches",
                "reasoning_hold",
                "last_progress",
                "last_error",
                "reasoning_worker",
            )
        }
    if name == "books":
        # Fixed projection. Nested broker fields outside these keys never reach the browser.
        result = _book_projection(result)
    if name == "instruments":
        result = _instrument_projection(result)
    return result


def fetch_bars(config, instrument, timeframe):
    """Candle read. The upstream query is built from validated values only."""
    origin, token = broker_endpoint(config, "")
    target = origin + "/bars?" + urlencode({"instrument": instrument, "timeframe": timeframe})
    request = Request(
        target,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        },
        method="GET",
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("dashboard_upstream_response")
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise ValueError("dashboard_upstream_bound")

    def invalid_constant(value):
        raise ValueError("dashboard_upstream_nonfinite")

    result = json.loads(data, parse_constant=invalid_constant)
    return _candle_projection(result, instrument, timeframe)


def _unavailable_message(name):
    if name == "books":
        return "Positions and orders are unavailable. No order was placed, modified, or cancelled."
    if name == "instruments":
        return "Configured currency pairs are unavailable."
    if name == "bars":
        return "Price history is unavailable. No order was placed, modified, or cancelled."
    return "Saved observations are unavailable. No backend action was taken."


class Dashboard:
    def __init__(self, config, root):
        self.config, self.root = config, root
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()
        self.hash_gate = threading.BoundedSemaphore(1)
        self.sessions, self.attempts, self.reads, self.cache = {}, {}, {}, {}

    async def hash_candidate(self, password, salt):
        # Reject excess work immediately; do not queue memory-hard hashes behind
        # the general ASGI threadpool or release capacity on caller cancellation.
        if not self.hash_gate.acquire(blocking=False):
            return None
        result = Future()

        def work():
            candidate = None
            try:
                candidate = password_hash(password, salt)
            except Exception:
                # Fail closed without leaking credentials through exception logs.
                pass
            finally:
                # This worker owns the slot until the actual hash has finished.
                self.hash_gate.release()
            result.set_result(candidate)

        try:
            threading.Thread(target=work, name="dashboard-password", daemon=True).start()
        except RuntimeError:
            self.hash_gate.release()
            return None
        return await asyncio.shield(asyncio.wrap_future(result))

    def sign(self, body):
        encoded = b64(json.dumps(body, separators=(",", ":")).encode())
        return encoded + "." + b64(hmac.digest(self.config.secret, encoded.encode(), "sha256"))

    def signed(self, value, purpose):
        try:
            if not value or len(value) > 2048:
                return None
            encoded, signature = value.split(".")
            expected = b64(hmac.digest(self.config.secret, encoded.encode(), "sha256"))
            if not hmac.compare_digest(signature, expected):
                return None
            body = json.loads(unb64(encoded))
            if body["purpose"] != purpose or body["exp"] <= time.time():
                return None
            return body
        except (ValueError, KeyError, TypeError, UnicodeError):
            return None

    def session(self, request):
        body = self.signed(request.cookies.get(SESSION_COOKIE), "session")
        if not body:
            return None
        with self.lock:
            entry = self.sessions.get(body.get("id"))
            return body if entry and entry == body and entry["exp"] > time.time() else None

    def allowed(self, buckets, key, limit, window, maximum=256):
        now = time.time()
        with self.lock:
            for expired in [item for item, hits in buckets.items() if hits[-1] <= now - window]:
                del buckets[expired]
            hits = [hit for hit in buckets.get(key, []) if hit > now - window]
            if len(hits) >= limit or (key not in buckets and len(buckets) >= maximum):
                return False
            buckets[key] = [*hits, now]
            return True

    def read(self, name):
        with self.read_lock:
            now = time.time()
            cached = self.cache.get(name)
            if cached and now - cached[0] < 30:
                return cached[1]
            try:
                data = fetch_read(self.config, name)
                result = {"status": "ok", "fetched_at": datetime.now(UTC).isoformat(), "data": data}
            except (ValueError, TypeError, OSError, HTTPError, URLError, RecursionError):
                # Do not return private hostnames, token-bearing exceptions, or stale success.
                message = _unavailable_message(name)
                result = {
                    "status": "unavailable",
                    "data": None,
                    "fetched_at": None,
                    "message": message,
                }
            self.cache[name] = (now, result)
            return result

    def read_chart(self, instrument, timeframe):
        key = f"bars\n{instrument}\n{timeframe}"
        with self.read_lock:
            now = time.time()
            cached = self.cache.get(key)
            if cached and now - cached[0] < 30:
                return cached[1]
            try:
                data = fetch_bars(self.config, instrument, timeframe)
                result = {"status": "ok", "fetched_at": datetime.now(UTC).isoformat(), "data": data}
            except (ValueError, TypeError, OSError, HTTPError, URLError, RecursionError):
                result = {
                    "status": "unavailable",
                    "data": None,
                    "fetched_at": None,
                    "message": _unavailable_message("bars"),
                }
            self.cache[key] = (now, result)
            return result

    def page(self, name, csrf="", message="", status=200):
        template = (self.root / "web" / name).read_text(encoding="utf-8")
        return HTMLResponse(
            template.replace("<!-- csrf -->", html.escape(csrf, quote=True)).replace(
                "<!-- message -->", html.escape(message)
            ),
            status_code=status,
        )


def cookie(response, name, value, seconds):
    response.set_cookie(
        name, value, max_age=seconds, path="/", secure=True, httponly=True, samesite="strict"
    )


def clear_cookie(response, name):
    cookie(response, name, "", 0)


async def form(request):
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        return {}
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 4096:
            return {}
    try:
        result = parse_qs(data.decode("utf-8"), strict_parsing=True, max_num_fields=4)
        return {key: values[0] for key, values in result.items() if len(values) == 1}
    except (ValueError, UnicodeError):
        return {}


def install_dashboard(app, root):
    dashboard = Dashboard(Config.environment(), root)
    app.state.dashboard = dashboard

    def unavailable():
        return HTMLResponse(
            "<!doctype html><title>PipMind · Private workspace</title>"
            "<h1>Private workspace unavailable</h1><p>Owner setup is required.</p>",
            status_code=503,
        )

    def valid_origin(request):
        return request.headers.get("origin") == dashboard.config.origin

    @app.api_route("/app", methods=["GET", "HEAD"])
    def workspace(request: WebRequest):
        if not dashboard.config:
            return unavailable()
        session = dashboard.session(request)
        if not session:
            return RedirectResponse("/app/login", status_code=303)
        return dashboard.page("dashboard.html", session["csrf"])

    @app.get("/app/login")
    def login_page(request: WebRequest):
        if not dashboard.config:
            return unavailable()
        if dashboard.session(request):
            return RedirectResponse("/app", status_code=303)
        token = secrets.token_urlsafe(24)
        flash = dashboard.signed(request.cookies.get(FLASH_COOKIE), "message")
        response = dashboard.page(
            "dashboard-login.html", token, "Sign-in failed. Try again later." if flash else ""
        )
        cookie(
            response,
            LOGIN_COOKIE,
            dashboard.sign({"purpose": "login", "csrf": token, "exp": int(time.time()) + 600}),
            600,
        )
        clear_cookie(response, FLASH_COOKIE)
        return response

    @app.post("/app/login")
    async def login(request: WebRequest):
        if not dashboard.config:
            return unavailable()
        if not valid_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        client = request.client.host if request.client else "unknown"
        if not dashboard.allowed(dashboard.attempts, "global", 30, 300) or not dashboard.allowed(
            dashboard.attempts, "client:" + client, 5, 300
        ):
            return JSONResponse(
                {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "300"}
            )
        data = await form(request)
        signed = dashboard.signed(request.cookies.get(LOGIN_COOKIE), "login")
        if not signed or not hmac.compare_digest(
            data.get("csrf", "").encode(), signed["csrf"].encode()
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        password = data.get("password", "")
        # A fixed-cost hash; only the configured approved parameters are accepted.
        salt = unb64(dashboard.config.password.split("$")[4])
        candidate = await dashboard.hash_candidate(password, salt)
        if candidate is None:
            return JSONResponse(
                {"error": "login_busy"}, status_code=429, headers={"Retry-After": "1"}
            )
        valid = bool(password) and hmac.compare_digest(candidate, dashboard.config.password)
        response = RedirectResponse("/app" if valid else "/app/login", status_code=303)
        clear_cookie(response, LOGIN_COOKIE)
        if not valid:
            cookie(
                response,
                FLASH_COOKIE,
                dashboard.sign({"purpose": "message", "exp": int(time.time()) + 60}),
                60,
            )
            return response
        session = {
            "purpose": "session",
            "id": secrets.token_urlsafe(32),
            "csrf": secrets.token_urlsafe(24),
            "exp": int(time.time()) + SESSION_SECONDS,
        }
        with dashboard.lock:
            dashboard.sessions = {
                key: value
                for key, value in dashboard.sessions.items()
                if value["exp"] > time.time()
            }
            if len(dashboard.sessions) >= 128:
                return JSONResponse({"error": "session_capacity"}, status_code=429)
            dashboard.sessions[session["id"]] = session
        cookie(response, SESSION_COOKIE, dashboard.sign(session), SESSION_SECONDS)
        return response

    @app.post("/app/logout")
    async def logout(request: WebRequest):
        if not dashboard.config:
            return unavailable()
        session = dashboard.session(request)
        data = await form(request)
        if (
            not session
            or not valid_origin(request)
            or not hmac.compare_digest(data.get("csrf", "").encode(), session["csrf"].encode())
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        with dashboard.lock:
            dashboard.sessions.pop(session["id"], None)
        response = RedirectResponse("/app/login", status_code=303)
        clear_cookie(response, SESSION_COOKIE)
        return response

    @app.get("/app/api/{name}")
    def read(name: str, request: WebRequest):
        if not dashboard.config:
            return JSONResponse({"error": "workspace_unavailable"}, status_code=503)
        session = dashboard.session(request)
        if not session:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        chart = None
        if name == "bars":
            try:
                chart = chart_query(request.url.query)
            except ValueError:
                return JSONResponse({"error": "not_found"}, status_code=404)
        elif name not in PATHS or request.url.query:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if not dashboard.allowed(dashboard.reads, session["id"], 18, 60, maximum=128):
            return JSONResponse(
                {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"}
            )
        if chart:
            instrument, timeframe = chart
            listed = dashboard.read("instruments")
            names = None
            if listed["status"] == "ok":
                names = (listed.get("data") or {}).get("instruments")
            if not isinstance(names, list):
                return JSONResponse(
                    {
                        "status": "unavailable",
                        "data": None,
                        "fetched_at": None,
                        "message": _unavailable_message("bars"),
                    },
                    status_code=503,
                )
            if instrument not in names:
                return JSONResponse({"error": "not_found"}, status_code=404)
            result = dashboard.read_chart(instrument, timeframe)
        else:
            result = dashboard.read(name)
        return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)

    @app.api_route("/app/resources/{name}", methods=["GET", "HEAD"])
    def resource(name: str, request: WebRequest):
        if not dashboard.config:
            return unavailable()
        if name not in {"dashboard.css", "dashboard.js", "lightweight-charts.js"}:
            return Response(status_code=404)
        # Login needs CSS before authentication; neither static file contains private state.
        if name.endswith(".js") and not dashboard.session(request):
            return Response(status_code=401)
        return Response(
            (root / "web" / name).read_text(encoding="utf-8"),
            media_type="text/css" if name.endswith(".css") else "text/javascript",
        )
