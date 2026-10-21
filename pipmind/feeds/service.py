"""Private feed handoff API. Only the HMAC webhook route bypasses bearer auth."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from pipmind.contracts import ContractError, loads
from pipmind.feeds import config as recipes
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.coverage import project
from pipmind.feeds.macro import webhook
from pipmind.feeds.news import relay
from pipmind.feeds.runtime import Supervisor
from pipmind.feeds.sql_store import configured_store
from pipmind.feeds.store import encoded


def create_app(store, token, *, secret="", supervisor=None, now=None, configuration=None):
    if not isinstance(token, str) or len(token) < 32:
        raise FeedError("feed_token_minimum_32_characters")
    now = now or (lambda: datetime.now(UTC))
    configuration = deepcopy(recipes.validate(configuration)) if configuration is not None else None

    @asynccontextmanager
    async def lifespan(app):
        if supervisor:
            supervisor.start()
        yield
        if supervisor:
            await asyncio.to_thread(supervisor.stop)

    app = FastAPI(
        title="PipMind private information feeds",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.store = store

    @app.middleware("http")
    async def auth(request, call_next):
        route_path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        if route_path not in {"/healthz", "/webhooks/fxmacrodata"}:
            if not hmac.compare_digest(
                request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
            ):
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"Cache-Control": "no-store"},
                )
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(ContractError)
    async def invalid(request, error):
        code = str(error)
        status = (
            503
            if code
            in {
                "feed_store_unavailable",
                "feed_coverage_configuration_unavailable",
                "feed_coverage_response_bound",
            }
            else 422
        )
        if code in {"invalid_webhook_signature", "stale_webhook"}:
            status = 403
        if code == "conflicting_feed_delivery":
            status = 409
        return JSONResponse(
            {"error": code}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 262_144:
                raise HTTPException(413, "feed_request_too_large")
        return bytes(data)

    @app.get("/healthz")
    def health():
        return {"application": "pipmind-feeds", "trading": False}

    @app.post("/webhooks/fxmacrodata")
    async def receive(request: Request):
        raw = await body(request)
        duplicate = await asyncio.to_thread(
            webhook, store, dict(request.headers), raw, secret, now()
        )
        return {"accepted": True, "duplicate": duplicate}

    @app.post("/relays/headlines")
    async def receive_relay(request: Request):
        raw = loads(await body(request))
        duplicate = await asyncio.to_thread(relay, store, raw, now())
        return {"accepted": True, "duplicate": duplicate}

    @app.get("/events")
    def events(after: int = 0, limit: int = 100):
        if after < 0 or not 1 <= limit <= 500:
            raise HTTPException(422, "invalid_event_page")
        return {
            "source_id": store.source_id(),
            "events": store.events(after, limit),
            "coverage": store.state("canonical:quality", {"status": "unverified"}),
            "quarantined": store.status()["quarantined"],
            "calendar": store.state("calendar:data"),
        }

    @app.get("/status")
    def status():
        return {
            "source_id": store.source_id(),
            **store.status(),
            "workers": supervisor.status() if supervisor else {},
            "worker_heartbeats": {
                name: store.state("worker:" + name) for name in ("macro", "news", "social")
            },
        }

    @app.get("/coverage")
    def coverage(offset: int = 0, limit: int = 100):
        return project(store, configuration, now(), offset=offset, limit=limit)

    app.state.private_status = status
    return app


def from_env():
    mode = os.environ.get("PIPMIND_FEED_MODE", "local-read")
    if mode == "fixture":
        from pipmind.feeds.fixtures import configuration

        config = configuration()
    else:
        config = recipes.load(os.environ["PIPMIND_FEED_CONFIG"])
    default_path = (
        "/tmp/pipmind-feeds/runtime"
        if os.environ.get("PIPMIND_FEED_BACKEND") == "sql"
        else ".data/feeds/events.sqlite3"
    )
    path = os.environ.get("PIPMIND_FEED_STORE", default_path)
    profile = identity(encoded(config))
    store = configured_store(path, mode=mode, profile=profile)
    supervisor = Supervisor(path, config, mode=mode, profile=profile)
    return create_app(
        store,
        os.environ.get("PIPMIND_FEED_TOKEN", ""),
        secret=os.environ.get("PIPMIND_FXMD_SIGNING_SECRET", ""),
        supervisor=supervisor,
        configuration=config,
    )
