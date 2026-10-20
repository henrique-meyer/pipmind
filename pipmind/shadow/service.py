"""Authenticated private shadow service, durable feed consumption and clock wakes."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from time import monotonic
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import FastAPI, HTTPException
from fastapi import Request as WebRequest
from fastapi.responses import JSONResponse

from pipmind.contracts import ContractError, stamp, utc
from pipmind.feeds.common import FeedError
from pipmind.shadow.common import ShadowError, bounded_json, digest, encoded, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import FoundryModel
from pipmind.shadow.worker import ReasoningWorker


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FeedConsumer:
    def __init__(self, store, url, token, source_id, *, completion_clock=None):
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
        self.completion_clock = completion_clock
        self.calendar_checked, self.calendar_info = None, {"status": "unavailable"}

    def calendar(self, cached, moment):
        from pipmind.shadow.calendar import retain_calendar

        self.calendar_info = retain_calendar(self.store, cached, moment)
        self.calendar_checked = moment
        return self.calendar_info

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
            moment = self.completion_clock() if self.completion_clock else moment
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
                    "calendar": self.calendar(page.get("calendar"), moment)
                    if self.calendar_checked is None
                    or (moment - self.calendar_checked).total_seconds() >= 60
                    else self.calendar_info,
                },
            )
            return len(page["events"])
        except Exception:
            raise ShadowError("shadow_source_unavailable_or_changed") from None


class GatewayConsumer(FeedConsumer):
    """Cached state/news GETs only; no broker credentials or command method."""

    def __init__(self, *args, handoff_interval=0, relay_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not 0 <= handoff_interval <= 3600:
            raise ShadowError("invalid_gateway_handoff_interval")
        self.handoff_interval, self.last_handoff = handoff_interval, None
        self.relay_store, self.last_headlines = relay_store, None

    def poll(self, moment):
        request = Request(self.url + "/state", headers={"Authorization": "Bearer " + self.token})
        try:
            with build_opener(NoRedirect()).open(request, timeout=15) as response:
                page = bounded_json(response.read(2_097_153), 2_097_152)
            moment = self.completion_clock() if self.completion_clock else moment
            if page.get("dispatch_enabled") is not False:
                raise ValueError
            if self.relay_store is not None and page.get("headlines_as_of"):
                from pipmind.feeds.news import relay_page

                marker = digest(
                    encoded({k: page.get(k) for k in ("headlines_as_of", "headlines_error")})
                )
                if marker != self.last_headlines:
                    request = Request(
                        self.url + "/headlines", headers={"Authorization": "Bearer " + self.token}
                    )
                    with build_opener(NoRedirect()).open(request, timeout=15) as response:
                        news = bounded_json(response.read(2_097_153), 2_097_152)
                    if utc(news["headlines_as_of"]) < utc(page["headlines_as_of"]):
                        raise ValueError
                    moment = self.completion_clock() if self.completion_clock else moment
                    relay_page(
                        self.relay_store,
                        news.get("headlines", []),
                        moment,
                        checked_at=news["headlines_as_of"],
                        error=news.get("headlines_error"),
                    )
                    self.last_headlines = marker
            if self.handoff_interval:
                if page.get("snapshots"):
                    self.store.refresh_broker_facts(*page["snapshots"], moment)
                if (
                    self.last_handoff is not None
                    and (moment - self.last_handoff).total_seconds() < self.handoff_interval
                ):
                    return 0
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
            self.last_handoff = moment
            return len(events)
        except Exception:
            raise ShadowError("shadow_gateway_read_unavailable") from None


class LocalFeedConsumer:
    """Same durable handoff inside the engine/feed container, without an HTTP hop."""

    def __init__(self, store, feeds, *, completion_clock=None):
        self.store, self.feeds = store, feeds
        self.source = feeds.source_id()
        self.completion_clock = completion_clock
        self.calendar_checked, self.calendar_info = None, {"status": "unavailable"}

    def poll(self, moment):
        try:
            page = self.feeds.events(self.store.checkpoint(self.source), 100)
            moment = self.completion_clock() if self.completion_clock else moment
            if page:
                self.store.accept(self.source, page, moment)
            if (
                self.calendar_checked is None
                or (moment - self.calendar_checked).total_seconds() >= 60
            ):
                from pipmind.shadow.calendar import retain_calendar

                cached = self.feeds.state("calendar:data")
                observed = self.completion_clock() if self.completion_clock else moment
                self.calendar_info = retain_calendar(self.store, cached, observed)
                self.calendar_checked = observed
            self.store.set_runtime(
                "feed_status",
                {
                    "coverage": self.feeds.state("canonical:quality", {"status": "unverified"}),
                    "quarantined": self.feeds.status()["quarantined"],
                    "checked_at": stamp(moment),
                    "calendar": self.calendar_info,
                },
            )
            return len(page)
        except FeedError:
            raise ShadowError("shadow_source_unavailable_or_changed") from None


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
    worker_timeout=120,
    qualification_enabled=False,
):
    if not isinstance(token, str) or len(token) < 32:
        raise ShadowError("shadow_token_minimum_32_characters")
    if not 60 <= clock_interval <= 86400:
        raise ShadowError("invalid_clock_interval")
    if not 0.01 <= worker_timeout <= 600:
        raise ShadowError("invalid_worker_timeout")
    runtime = {"last_progress": None, "last_error": None}
    phase = {"name": "idle", "since": None, "started": None, "error_type": None, "serial": 0}
    active = {}
    lifecycle = {"task": None, "watcher": None, "stopping": False, "failed": False}
    worker = ReasoningWorker() if background else None

    def observe(name, facts):
        if name == "context":
            phase["error_type"] = None
            phase["pause_error_type"] = None
        if name.endswith("_projection"):
            active.pop("trace", None)
            active.pop("response", None)
        phase.update(name=name, since=stamp(now()), started=monotonic(), serial=phase["serial"] + 1)
        active.update(facts)

    engine = ShadowEngine(store, model, price_interval=price_interval, observer=observe)
    if isinstance(model, FoundryModel):
        model.observer = observe
    recorder = None
    if qualification_enabled:
        from pipmind.shadow.qualification import Recorder

        if not background or gateway is None:
            raise ShadowError("qualification_requires_background_gateway")
        recorder = Recorder(
            store, gateway, lambda: status(), feed_app.state.private_status if feed_app else None
        )

    async def hold_worker(reason, expected_phase=None):
        # Only durable pending data is held. A late result loses its revision
        # authority; it cannot overwrite a newer decision or this pause.
        state = await asyncio.to_thread(store.head)
        if expected_phase is not None and phase["serial"] != expected_phase:
            return False
        if state["paused"]:
            return True
        event = await asyncio.to_thread(store.pending)
        if expected_phase is not None and phase["serial"] != expected_phase:
            return False
        if not event:
            await asyncio.to_thread(store.set_paused, True)
            return True
        same = active.get("event", {}).get("message_id") == event["message_id"]
        if same and active.get("revision", state["revision"]) != state["revision"]:
            return False  # A concurrent owner/commit changed the attempt's authority.
        result = deepcopy(active.get("result", {})) if same else {}
        trace = active.get("trace") if same else None
        if trace:
            trace = deepcopy(trace)
            trace["usage_unknown"] = (
                bool(trace.get("attempted")) and trace.get("input_tokens") is None
            )
            result["usage"] = [*result.get("usage", [])[:-1], trace]
        if same and active.get("response") is not None:
            result["model_reply"] = deepcopy(active["response"])
        result.update(
            mode="shadow",
            event_id=event["message_id"],
            dispatches=0,
            worker_fault={
                "phase": phase["name"],
                "since": phase["since"],
                "error_type": phase["error_type"],
            },
        )
        try:
            await asyncio.to_thread(store.hold, event, state["revision"], result, now(), reason)
        except ShadowError as error:
            if str(error) in {"shadow_revision_changed", "shadow_input_not_pending"}:
                return False
            raise
        return True

    async def pause_until_durable(reason):
        # Keep the active attempt/known usage while SQL recovers. A second
        # cleanup/transport error must not terminate the recovery loop itself.
        while not lifecycle["stopping"]:
            try:
                if await hold_worker(reason):
                    return
            except Exception as error:
                runtime["last_error"] = "shadow_worker_pause_unavailable"
                phase["pause_error_type"] = type(error).__name__
            await asyncio.sleep(min(1, worker_timeout / 2))

    async def watch():
        while True:
            await asyncio.sleep(min(1, worker_timeout / 2))
            task = lifecycle["task"]
            stopped = task is not None and task.done() and not lifecycle["stopping"]
            stale = phase["started"] is not None and monotonic() - phase["started"] > worker_timeout
            if (stopped or stale or not worker.alive()) and not lifecycle["stopping"]:
                runtime["last_error"] = (
                    "shadow_worker_stalled" if stale else "shadow_worker_stopped"
                )
                if stopped:
                    exception = None if task.cancelled() else task.exception()
                    phase["error_type"] = (
                        "CancelledError"
                        if task.cancelled()
                        else (type(exception).__name__ if exception else "UnexpectedExit")
                    )
                try:
                    if await hold_worker(runtime["last_error"], phase["serial"]):
                        # Fail the existing liveness probe only after durable
                        # pause succeeds. Ordinary SQL/provider outages retry.
                        lifecycle["failed"] = True
                        return
                except Exception as error:
                    runtime["last_error"] = "shadow_worker_pause_unavailable"
                    phase["pause_error_type"] = type(error).__name__

    async def work():
        while not lifecycle["failed"]:
            progressed = False
            try:
                active.clear()
                observe("source_poll", {})
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
                for _ in range(1 if gateway and getattr(gateway, "handoff_interval", 0) else 100):
                    # The engine uses synchronous SQL. Keep blocked database
                    # connections off the ASGI loop, including during outages.
                    active.clear()
                    observe("step", {})
                    progressed = await worker.run(engine.step())
                    if progressed:
                        runtime["last_progress"] = stamp(now())
                    runtime["last_error"] = "source_unavailable" if source_errors else None
                    phase.update(name="idle", started=None)
                    if not progressed:
                        break
            except (ShadowError, ContractError):
                runtime["last_error"] = "shadow_step_unavailable"
                phase.update(name="idle", started=None)
            except asyncio.CancelledError:
                if lifecycle["stopping"] or asyncio.current_task().cancelling():
                    raise
                phase["error_type"] = "CancelledError"
                runtime["last_error"] = "shadow_worker_cancelled"
                await pause_until_durable(runtime["last_error"])
                phase.update(name="idle", started=None)
            except Exception as error:
                phase["error_type"] = type(error).__name__
                runtime["last_error"] = "shadow_worker_failed"
                await pause_until_durable(runtime["last_error"])
                phase.update(name="idle", started=None)
            # Continue an ordered backlog immediately. Idle/error polling keeps
            # its backoff, and each step still refreshes broker/feed facts.
            await asyncio.sleep(0 if progressed and runtime["last_error"] is None else 2)

    @asynccontextmanager
    async def lifespan(app):
        @asynccontextmanager
        async def no_feeds():
            yield

        async with feed_app.router.lifespan_context(feed_app) if feed_app else no_feeds():
            if worker:
                worker.start()
            task = asyncio.create_task(work()) if background else None
            lifecycle["task"] = task
            watcher = asyncio.create_task(watch()) if background else None
            lifecycle["watcher"] = watcher
            capture_task = asyncio.create_task(recorder.run()) if recorder else None
            lifecycle["qualification"] = capture_task
            try:
                yield
            finally:
                lifecycle["stopping"] = True
                if capture_task:
                    capture_task.cancel()
                    try:
                        await capture_task
                    except asyncio.CancelledError:
                        pass
                if watcher:
                    watcher.cancel()
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        pass
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if worker:
                    await asyncio.to_thread(worker.close)

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
        body = {"application": "pipmind-shadow", "mode": "shadow", "trading": False}
        return JSONResponse(body, status_code=503) if lifecycle["failed"] else body

    @app.get("/status")
    def status():
        state = store.status()
        loop_alive = lifecycle["task"] is not None and not lifecycle["task"].done()
        supervisor_alive = lifecycle["watcher"] is not None and not lifecycle["watcher"].done()
        return {
            **state,
            **runtime,
            **(
                {
                    "qualification": store.runtime("qualification:active"),
                    "qualification_recorder": {
                        "alive": lifecycle.get("qualification") is not None
                        and not lifecycle["qualification"].done(),
                        "last_error": recorder.last_error,
                    },
                }
                if recorder
                else {}
            ),
            "last_error": state.get("reasoning_hold", {}).get("reason")
            if state.get("reasoning_hold")
            else runtime["last_error"],
            **(
                {
                    "reasoning_worker": {
                        "alive": worker.alive()
                        and loop_alive
                        and supervisor_alive
                        and not lifecycle["failed"],
                        "loop_alive": loop_alive,
                        "supervisor_alive": supervisor_alive,
                        "phase": phase["name"],
                        "phase_since": phase["since"],
                        "event_id": active.get("event", {}).get("message_id"),
                        "error_type": phase["error_type"],
                        "pause_error_type": phase.get("pause_error_type"),
                    }
                }
                if worker
                else {}
            ),
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
        consumer = LocalFeedConsumer(store, feed_app.state.store, completion_clock=now)
    if os.environ.get("PIPMIND_SHADOW_FEED_URL"):
        if consumer:
            raise ShadowError("choose_one_feed_handoff")
        consumer = FeedConsumer(
            store,
            os.environ["PIPMIND_SHADOW_FEED_URL"],
            os.environ["PIPMIND_FEED_TOKEN"],
            os.environ["PIPMIND_SHADOW_SOURCE_ID"],
            completion_clock=now,
        )
    gateway = None
    if os.environ.get("PIPMIND_SHADOW_GATEWAY_URL"):
        gateway = GatewayConsumer(
            store,
            os.environ["PIPMIND_SHADOW_GATEWAY_URL"],
            os.environ["PIPMIND_GATEWAY_TOKEN"],
            os.environ["PIPMIND_SHADOW_GATEWAY_SOURCE_ID"],
            handoff_interval=int(os.environ.get("PIPMIND_SHADOW_GATEWAY_INTERVAL", "0")),
            completion_clock=now,
            relay_store=feed_app.state.store if feed_app else None,
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
        qualification_enabled=os.environ.get("PIPMIND_SHADOW_QUALIFICATION_ENABLED") == "true",
    )
