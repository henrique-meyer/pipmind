"""Explicitly constructed private dry safety API; no environment or transport wiring."""

import asyncio
import hmac
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from pipmind.contracts import ContractError, loads
from pipmind.safety.admission import MAX_REQUEST_BYTES, AdmissionRefused, decode_request
from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseGrant, LeaseRefused, LeaseUnavailable
from pipmind.safety.session import SessionCommitUnknown, SessionRefused, SessionUnavailable

# The envelope may escape every character in the existing canonical request.
MAX_BODY_BYTES = 4 * MAX_REQUEST_BYTES
MAX_CONTROL_BYTES = 65536
MAX_RESPONSE_BYTES = 524288
MAX_COUNT = 9223372036854775807


class _RequestRefused(Exception):
    def __init__(self, status=422, code="invalid_safety_request"):
        self.status, self.code = status, code


def _identifier(value):
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-16-le")) <= 256
    except UnicodeError:
        return False


def _text(value):
    if type(value) is not str or not value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_CONTROL_BYTES
    except UnicodeError:
        return False


def _need(condition):
    if not condition:
        raise _RequestRefused()


def _keys(value, keys):
    _need(type(value) is dict and value.keys() == set(keys))
    return value


def _response(body, status=200):
    # Do not let framework fallback encoders introspect a lease or arbitrary object.
    try:
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        _need(len(raw.encode("utf-8")) <= MAX_RESPONSE_BYTES)
    except (TypeError, ValueError, UnicodeError, RecursionError, _RequestRefused):
        body, status = {"error": "safety_store_unavailable"}, 503
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def create_app(gateway, controls, token, *, grant_provider, owner_id):
    """Use trusted local stores, a lease supplier and an owner identity, never JSON authority."""
    from pipmind.safety.controls_sql import ControlCommitUnknown, ControlRefused, ControlUnavailable
    from pipmind.safety.integration_sql import IntegrationRefused, IntegrationUnavailable

    if (
        type(token) is not str
        or not 32 <= len(token) <= 512
        or any(not 33 <= ord(char) <= 126 for char in token)
        or not _identifier(owner_id)
        or not callable(grant_provider)
    ):
        raise ValueError("invalid_safety_service_configuration")
    expected = ("Bearer " + token).encode("ascii")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def boundary(request, call_next):
        if not (request.method == "GET" and request.url.path == "/healthz"):
            supplied = request.headers.getlist("authorization")
            if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode(), expected):
                return _response({"error": "unauthorized"}, 401)
        try:
            result = await call_next(request)
        except _RequestRefused as error:
            return _response({"error": error.code}, error.status)
        except Exception:
            return _response({"error": "safety_store_unavailable"}, 503)
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        code = {404: "safety_not_found", 405: "safety_method_not_allowed"}.get(
            error.status_code, "invalid_safety_request"
        )
        return _response({"error": code}, error.status_code)

    @app.exception_handler(_RequestRefused)
    async def request_error(request, error):
        return _response({"error": error.code}, error.status)

    async def body(request, bound):
        _need(
            request.headers.get("content-type", "").split(";", 1)[0].strip() == "application/json"
        )
        size = request.headers.get("content-length")
        if size is not None:
            _need(size.isascii() and size.isdecimal())
            if len(size) > 10 or int(size) > bound:
                raise _RequestRefused(413, "safety_request_too_large")
        data = bytearray()
        async for chunk in request.stream():
            if len(chunk) > bound - len(data):
                raise _RequestRefused(413, "safety_request_too_large")
            data.extend(chunk)
        try:
            return loads(data.decode("utf-8"))
        except (ContractError, UnicodeError, RecursionError):
            raise _RequestRefused() from None

    def lease():
        grant = grant_provider()
        if type(grant) is not LeaseGrant:
            raise LeaseUnavailable("lease_store_unavailable")
        return grant

    async def invoke(operation):
        try:
            return _response(await asyncio.to_thread(operation))
        except AdmissionRefused as error:
            status = 409 if str(error) == "conflicting_admission_request" else 422
            return _response({"error": "invalid_safety_request"}, status)
        except LeaseCommitUnknown:
            return _response({"error": "lease_commit_unknown"}, 503)
        except (SessionCommitUnknown, ControlCommitUnknown):
            return _response({"error": "safety_commit_unknown"}, 503)
        except IntegrationRefused as error:
            # Only an exact fixed code is inspected, never returned caller/SQL text.
            status = 409 if str(error) == "conflicting_integration_request" else 422
            return _response({"error": "safety_request_refused"}, status)
        except (LeaseRefused, SessionRefused, ControlRefused):
            return _response({"error": "safety_state_refused"}, 409)
        except (LeaseUnavailable, SessionUnavailable, ControlUnavailable, IntegrationUnavailable):
            return _response({"error": "safety_store_unavailable"}, 503)

    def decoded(value):
        _need(type(value) is str)
        return decode_request(value)

    def gateway_call(method, value, *, check_id=None):
        request = decoded(value)
        grant = lease()
        if method == "accept":
            return gateway.accept(grant, request, origin="model")
        if method == "check":
            return gateway.check(grant, check_id, request)
        return gateway.reconcile(grant, request)

    @app.get("/healthz")
    async def health():
        return {"dispatch_enabled": False}

    @app.post("/reconcile")
    async def reconcile(request: Request):
        value = _keys(await body(request, MAX_BODY_BYTES), ("request",))
        return await invoke(lambda: gateway_call("reconcile", value["request"]))

    @app.post("/orders")
    async def orders(request: Request):
        value = _keys(await body(request, MAX_BODY_BYTES), ("request",))
        return await invoke(lambda: gateway_call("accept", value["request"]))

    @app.post("/checks")
    async def checks(request: Request):
        value = _keys(await body(request, MAX_BODY_BYTES), ("check_id", "request"))
        _need(_identifier(value["check_id"]))
        return await invoke(
            lambda: gateway_call("check", value["request"], check_id=value["check_id"])
        )

    @app.post("/controls/overrides")
    async def override(request: Request):
        value = _keys(
            await body(request, MAX_CONTROL_BYTES),
            ("command_id", "action", "expected_revision", "reason"),
        )
        _need(
            _identifier(value["command_id"])
            and value["action"] in ("kill", "pause", "resume", "unkill", "flatten")
            and type(value["expected_revision"]) is int
            and 0 <= value["expected_revision"] <= MAX_COUNT
            and _text(value["reason"])
        )
        return await invoke(
            lambda: controls.apply_override(
                value["command_id"],
                owner_id,
                value["action"],
                value["expected_revision"],
                value["reason"],
            )
        )

    @app.post("/suggestions")
    async def suggestion(request: Request):
        value = _keys(await body(request, MAX_CONTROL_BYTES), ("suggestion_id", "text"))
        _need(_identifier(value["suggestion_id"]) and _text(value["text"]))
        return await invoke(
            lambda: controls.submit_suggestion(value["suggestion_id"], owner_id, value["text"])
        )

    @app.post("/suggestions/disposition")
    async def disposition(request: Request):
        value = _keys(
            await body(request, MAX_CONTROL_BYTES), ("suggestion_id", "disposition", "reason")
        )
        _need(
            _identifier(value["suggestion_id"])
            and value["disposition"] in ("adopt", "trial", "reject")
            and _text(value["reason"])
        )
        return await invoke(
            lambda: controls.resolve_suggestion(
                value["suggestion_id"], owner_id, value["disposition"], value["reason"]
            )
        )

    @app.get("/state")
    async def state():
        def read():
            value = controls.read()
            return {
                "binding": {
                    "account_id": value.binding.account_id,
                    "environment": value.binding.environment,
                    "currency": value.binding.currency,
                },
                "revision": value.revision,
                "killed": value.killed,
                "paused": value.paused,
                "dispatch_enabled": False,
            }

        return await invoke(read)

    return app
