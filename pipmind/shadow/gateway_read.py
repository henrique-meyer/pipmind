"""Owner book and candle reads through the loopback gateway. No order transport."""

import hmac
import json
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import Request as WebRequest
from fastapi.responses import JSONResponse

READS = {"/books", "/instruments", "/bars"}
MAX_BODY = 524288
TIMEFRAMES = {"15m", "1h", "2h", "4h", "1d"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def loopback_base(url):
    """Plain HTTP on a loopback host, or None when the hop must stay disabled."""
    if (
        not isinstance(url, str)
        or not url
        or url != url.strip()
        or any(ord(char) < 33 for char in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if host == "127.0.0.1":
        normalized = "127.0.0.1"
    elif host == "localhost":
        normalized = "localhost"
    elif host == "::1":
        normalized = "[::1]"
    else:
        return None
    if (
        parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or (port is not None and not 1 <= port <= 65535)
    ):
        return None
    return f"http://{normalized}" if port is None else f"http://{normalized}:{port}"


def owner_read_target(url, token):
    base = loopback_base(url)
    if base and isinstance(token, str) and len(token) >= 32 and " " not in token:
        return base, token
    return None


def _authorized(supplied, shadow_token, gateway_token):
    raw = supplied.encode()
    shadow = hmac.compare_digest(raw, f"Bearer {shadow_token}".encode())
    gateway = hmac.compare_digest(raw, f"Bearer {gateway_token}".encode())
    return shadow or gateway


def _target(base, path, query):
    items = list(query.multi_items())
    if path == "/bars":
        if len(items) != 2 or {key for key, _ in items} != {"instrument", "timeframe"}:
            return None
        values = dict(items)
        instrument, timeframe = values["instrument"], values["timeframe"]
        if not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", instrument) or timeframe not in TIMEFRAMES:
            return None
        return base + "/bars?" + urlencode({"instrument": instrument, "timeframe": timeframe})
    if path not in {"/books", "/instruments"} or items:
        return None
    return base + path


def fetch(base, gateway_token, path, query):
    """GET one owner read. The caller supplies the gateway bearer, never a client header."""
    target = _target(base, path, query)
    if target is None:
        return None
    request = Request(
        target,
        data=None,
        headers={"Authorization": "Bearer " + gateway_token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=7) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                return False
            raw = response.read(MAX_BODY + 1)
    except Exception:
        return False
    if len(raw) > MAX_BODY:
        return False
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(body, dict):
        return False
    body["dispatch_enabled"] = False
    return body


def mount(app, base, shadow_token, gateway_token):
    @app.get("/books")
    @app.get("/instruments")
    @app.get("/bars")
    def owner_gateway_read(request: WebRequest):
        if not _authorized(request.headers.get("authorization", ""), shadow_token, gateway_token):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        body = fetch(base, gateway_token, request.url.path, request.query_params)
        if body is None:
            return JSONResponse(
                {"error": "not_found"}, status_code=404, headers={"Cache-Control": "no-store"}
            )
        if body is False:
            return JSONResponse(
                {"error": "gateway_read_unavailable"},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
        return body
