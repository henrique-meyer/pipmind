"""Authenticated private shadow service, durable feed consumption and clock wakes."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import FastAPI, HTTPException
from fastapi import Request as WebRequest
from fastapi.responses import JSONResponse

from pipmind.contracts import ContractError, stamp
from pipmind.shadow.common import ShadowError, bounded_json, digest, encoded, now
from pipmind.shadow.engine import ShadowEngine


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FeedConsumer:
    def __init__(self, store, url, token, source_id):
        target = urlsplit(url)
        if (
            not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path.rstrip("/")
            or not source_id
            or len(token) < 32
            or (
                target.scheme != "https"
                and not (
                    target.scheme == "http"
                    and target.hostname in {"127.0.0.1", "localhost", "feeds"}
                )
            )
        ):
            raise ShadowError("invalid_shadow_source")
        self.store, self.url, self.token, self.source = store, url.rstrip("/"), token, source_id

    def poll(self, moment):
        after = self.store.checkpoint(self.source)
        request = Request(
            self.url + "/events?" + urlencode({"after": after, "limit": 100}),
            headers={"Authorization": "Bearer " + self.token},
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(2_097_153)
            page = bounded_json(raw, 2_097_152)
            if page["source_id"] != self.source or not isinstance(page["events"], list):
                raise ValueError
            if page["events"]:
                self.store.accept(self.source, page["events"], moment)
            self.store.set_runtime(
                "feed_status",
                {
                    "coverage": page.get("coverage", "unverified"),
                    "quarantined": page.get("quarantined"),
                    "checked_at": stamp(moment),
                },
            )
            return len(page["events"])
        except Exception:
            raise ShadowError("shadow_source_unavailable_or_changed") from None


class GatewayConsumer(FeedConsumer):
    """Only GET /state. No order URL, broker credential or command method exists."""

    def poll(self, moment):
        request = Request(self.url + "/state", headers={"Authorization": "Bearer " + self.token})
        try:
            with build_opener(NoRedirect()).open(request, timeout=15) as response:
                page = bounded_json(response.read(2_097_153), 2_097_152)
            if page.get("dispatch_enabled") is not False:
                raise ValueError
            events = []
            for payload in page.get("prices", []):
                # /state may mark the same quote stale later. That observation
                # needs its own immutable ID, retaining the gateway's quote ID.
                identifier = "gateway-price:" + digest(encoded(payload))
                observed = {
                    **payload,
                    "source_event_id": payload["event_id"],
                    "event_id": identifier,
                }
                events.append(("PriceEvent", identifier, observed))
            if page.get("snapshots"):
                account, positions = page["snapshots"]
                for kind, payload in (
                    ("AccountSnapshot", account),
                    ("PositionSnapshot", positions),
                ):
                    events.append(
                        (kind, "snapshot:" + kind + ":" + digest(encoded(payload)), payload)
                    )
            start = self.store.checkpoint(self.source)
            if events:
                self.store.accept(
                    self.source,
                    [
                        {
                            "sequence": start + i + 1,
                            "event": {
                                "schema_version": 1,
                                "kind": kind,
                                "message_id": identifier,
                                "available_at": stamp(moment),
                                "payload": payload,
                            },
                        }
                        for i, (kind, identifier, payload) in enumerate(events)
                    ],
                    moment,
                )
            return len(events)
        except Exception:
            raise ShadowError("shadow_gateway_read_unavailable") from None


class LocalFeedConsumer:
    """Same durable handoff inside the engine/feed container, without an HTTP hop."""

    def __init__(self, store, feeds):
        self.store, self.feeds = store, feeds
        self.source = feeds.source_id()

    def poll(self, moment):
        page = self.feeds.events(self.store.checkpoint(self.source), 100)
        if page:
            self.store.accept(self.source, page, moment)
        self.store.set_runtime(
            "feed_status",
            {
                "coverage": self.feeds.state("canonical:quality", {"status": "unverified"}),
                "quarantined": self.feeds.status()["quarantined"],
                "checked_at": stamp(moment),
            },
        )
        return len(page)


def clock_wake(store, moment, interval=900):
    slot = int(moment.timestamp()) // interval
    if store.checkpoint("clock") >= slot:
        return
    identifier = "clock:" + digest(store.trader) + ":" + str(slot)
    event = {
        "schema_version": 1,
        "kind": "ClockEvent",
        "message_id": identifier,
        "available_at": stamp(moment),
        "payload": {
            "schema_version": 1,
            "event_id": identifier,
            "ts": stamp(moment),
            "reason": "heartbeat",
        },
    }
    store.accept("clock", [{"sequence": slot, "event": event}], moment)


def create_app(
    store,
    model,
    token,
    *,
    consumer=None,
    gateway=None,
    background=True,
    feed_app=None,
    scheduled_wakes=True,
    price_interval=60,
    clock_interval=900,
):
    if not isinstance(token, str) or len(token) < 32:
        raise ShadowError("shadow_token_minimum_32_characters")
    if not 60 <= clock_interval <= 86400:
        raise ShadowError("invalid_clock_interval")
    engine = ShadowEngine(store, model, price_interval=price_interval)
    runtime = {"last_progress": None, "last_error": None}

    async def work():
        while True:
            try:
                source_errors = []
                for source in (consumer, gateway):
                    if source:
                        try:
                            await asyncio.to_thread(source.poll, now())
                        except (ShadowError, ContractError):
                            source_errors.append("source_unavailable")
                if source_errors:
                    await asyncio.to_thread(
                        store.set_runtime,
                        "feed_status",
                        {"coverage": "source_unavailable", "checked_at": stamp(now())},
                    )
                if scheduled_wakes:
                    await asyncio.to_thread(clock_wake, store, now(), clock_interval)
                for _ in range(100):
                    # The engine uses synchronous SQL. Keep blocked database
                    # connections off the ASGI loop, including during outages.
                    progressed = await asyncio.to_thread(lambda: asyncio.run(engine.step()))
                    runtime.update(
                        last_progress=stamp(now()),
                        last_error="source_unavailable" if source_errors else None,
                    )
                    if not progressed:
                        break
            except (ShadowError, ContractError):
                runtime["last_error"] = "shadow_step_unavailable"
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        @asynccontextmanager
        async def no_feeds():
            yield

        async with feed_app.router.lifespan_context(feed_app) if feed_app else no_feeds():
            task = asyncio.create_task(work()) if background else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def auth(request, call_next):
        # Mounted feed routes enforce their own bearer/HMAC policy.
        mounted = feed_app is not None and request.url.path.startswith("/feeds/")
        if (
            request.url.path != "/healthz"
            and not mounted
            and not hmac.compare_digest(
                request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
            )
        ):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Robots-Tag"] = "noindex"
        return result

    @app.exception_handler(ShadowError)
    async def unavailable(request, error):
        code = str(error)
        status = 503
        if code in {
            "shadow_invalid_json",
            "invalid_input_page",
            "input_cursor_not_increasing",
            "input_not_yet_available",
            "conflicting_shadow_input",
            "shadow_record_identity_collision",
            "invalid_shadow_input",
        }:
            status = 422
        if code == "reconsideration_owner_operation_required":
            status = 403
        if code == "shadow_payload_size" or code == "shadow_input_bound":
            status = 413
        return JSONResponse(
            {"error": code}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    @app.exception_handler(ContractError)
    async def invalid(request, error):
        return JSONResponse(
            {"error": "invalid_shadow_input"},
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    def health():
        return {"application": "pipmind-shadow", "mode": "shadow", "trading": False}

    @app.get("/status")
    def status():
        state = store.status()
        return {
            **state,
            **runtime,
            "last_error": state.get("reasoning_hold", {}).get("reason")
            if state.get("reasoning_hold")
            else runtime["last_error"],
        }

    @app.post("/inputs")
    async def inputs(request: WebRequest):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 262144:
                raise HTTPException(413, "shadow_request_size")
        page = bounded_json(data)
        if (
            not isinstance(page, dict)
            or set(page) != {"source", "events"}
            or not isinstance(page["source"], str)
            or not page["source"]
            or not isinstance(page["events"], list)
            or not 1 <= len(page["events"]) <= 500
        ):
            raise HTTPException(422, "invalid_shadow_input_page")
        await asyncio.to_thread(store.accept, page["source"], page["events"], now())
        return {"accepted": True, "dispatches": 0}

    if feed_app:
        app.mount("/feeds", feed_app)
    return app


def from_env():
    from pipmind.shadow.model import FoundryModel
    from pipmind.shadow.sql import connector
    from pipmind.shadow.store import SqlMind

    store = SqlMind(connector(), os.environ["PIPMIND_SHADOW_TRADER"])
    store.head()  # Initialization/migrations are explicit owner CLI operations.
    model = FoundryModel.environment()
    consumer = None
    feed_app = None
    if os.environ.get("PIPMIND_SHADOW_RUN_FEEDS") == "true":
        from pipmind.feeds.service import from_env as feeds_from_env

        if os.environ.get("PIPMIND_FEED_BACKEND") != "sql":
            raise ShadowError("integrated_shadow_feeds_require_sql")
        feed_app = feeds_from_env()
        consumer = LocalFeedConsumer(store, feed_app.state.store)
    if os.environ.get("PIPMIND_SHADOW_FEED_URL"):
        if consumer:
            raise ShadowError("choose_one_feed_handoff")
        consumer = FeedConsumer(
            store,
            os.environ["PIPMIND_SHADOW_FEED_URL"],
            os.environ["PIPMIND_FEED_TOKEN"],
            os.environ["PIPMIND_SHADOW_SOURCE_ID"],
        )
    gateway = None
    if os.environ.get("PIPMIND_SHADOW_GATEWAY_URL"):
        gateway = GatewayConsumer(
            store,
            os.environ["PIPMIND_SHADOW_GATEWAY_URL"],
            os.environ["PIPMIND_GATEWAY_TOKEN"],
            os.environ["PIPMIND_SHADOW_GATEWAY_SOURCE_ID"],
        )
    return create_app(
        store,
        model,
        os.environ.get("PIPMIND_SHADOW_TOKEN", ""),
        consumer=consumer,
        gateway=gateway,
        feed_app=feed_app,
        price_interval=int(os.environ.get("PIPMIND_SHADOW_PRICE_INTERVAL", "60")),
        clock_interval=int(os.environ.get("PIPMIND_SHADOW_CLOCK_INTERVAL", "900")),
    )
