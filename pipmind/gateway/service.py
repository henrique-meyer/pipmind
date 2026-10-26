"""Internal authenticated dry gateway; never mounted by the public landing app."""

import asyncio
import hmac
import os
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from pipmind.contracts import ContractError, Contracts, loads, stamp, utc
from pipmind.gateway.ciapi import Ciapi
from pipmind.gateway.config import BrokerConfig, GatewayError, symbols_from_env
from pipmind.gateway.engine import DryGateway, PriceRouter
from pipmind.gateway.ledger import DryLedger, canonical
from pipmind.gateway.stream import LightstreamerPrices


class ReadWorker:
    def __init__(
        self,
        broker,
        *,
        stream=None,
        now=None,
        snapshot_interval=30,
        evidence_interval=None,
        evidence_since=None,
        headline_interval=None,
    ):
        if (
            not 30 <= snapshot_interval <= 3600
            or (evidence_interval is not None and not 60 <= evidence_interval <= 3600)
            or (headline_interval is not None and not 60 <= headline_interval <= 3600)
        ):
            raise GatewayError("invalid_broker_read_intervals")
        self.broker = broker
        self.now = now or (lambda: datetime.now(UTC))
        self.stream = stream or LightstreamerPrices(broker)
        self.router = PriceRouter(broker.symbols, broker, self.now, resubscribe=self.recover_stream)
        self.lock, self.stop_event = threading.RLock(), threading.Event()
        self.contracts = Contracts()
        self.snapshots, self.last_snapshot, self.last_error = None, None, None
        self.protections = None
        self.working_orders = None
        self.orders_error = {"code": "working_orders_not_read"}
        self.orders_as_of = None
        self.snapshot_interval, self.evidence_interval = snapshot_interval, evidence_interval
        self.evidence_since = evidence_since or self.now()
        if self.evidence_since > self.now():
            raise GatewayError("invalid_broker_evidence_window")
        self.evidence, self.last_evidence, self.evidence_error = None, None, None
        self.snapshot_attempt, self.evidence_attempt = None, None
        self.verified, self.last_cycle, self.prices = False, None, []
        self.thread = None
        self.headline_interval, self.headline_attempt = headline_interval, None
        self.headlines, self.headlines_as_of, self.headlines_error = [], None, None

    def recover_stream(self):
        # A quiet instrument does not mean the shared transport is broken.
        # Keep its stale flag/fallback, without disconnecting healthy peers.
        if not getattr(self.stream, "connected", lambda: False)():
            self.stream.start()

    def drain_stream(self):
        for raw in self.stream.drain():
            if "error" in raw:
                self.router.stream_times.clear()
                if raw["error"] == "stream_auth":
                    self.broker.login()
                raise GatewayError(raw["error"])
            self.router.stream(raw)

    def cycle(self):
        # Broker I/O never owns the status lock. A slow REST retry must not
        # make the engine's authenticated local GET block behind the network.
        try:
            if self.stream.generation != self.broker.generation:
                self.stream.start()
            self.drain_stream()
            self.router.poll()
            now = self.now()
            if (
                self.snapshot_attempt is None
                or (now - self.snapshot_attempt).total_seconds() >= self.snapshot_interval
            ):
                self.snapshot_attempt = now
                snapshots = self.broker.snapshots()
                self.contracts.validate("account-snapshot", snapshots[0])
                self.contracts.validate("position-snapshot", snapshots[1])
                protections = getattr(self.broker, "protections", None)
                with self.lock:
                    self.snapshots, self.last_snapshot = snapshots, now
                    self.protections = protections
                if hasattr(self.broker, "working_orders"):
                    try:
                        page = self.broker.working_orders()
                        with self.lock:
                            self.working_orders, self.orders_error, self.orders_as_of = (
                                page,
                                None,
                                now,
                            )
                    except ContractError as error:
                        with self.lock:
                            # Keep the previous list and mark it stale. Do not drop it silently.
                            self.orders_error = {"code": str(error), "ts": stamp(self.now())}
                    except Exception:
                        with self.lock:
                            self.orders_error = {
                                "code": "broker_orders_unreadable",
                                "ts": stamp(self.now()),
                            }
            if self.evidence_interval is not None and (
                self.evidence_attempt is None
                or (now - self.evidence_attempt).total_seconds() >= self.evidence_interval
            ):
                self.evidence_attempt = now
                try:
                    evidence = self.broker.evidence(self.evidence_since)
                    with self.lock:
                        self.evidence, self.last_evidence, self.evidence_error = evidence, now, None
                except ContractError as error:
                    with self.lock:
                        self.evidence_error = {"code": str(error), "ts": stamp(self.now())}
            # REST/account/history reads may take seconds. Publish the fresh
            # callbacks received during that I/O, rather than the older batch.
            self.drain_stream()
            if self.headline_interval is not None and (
                self.headline_attempt is None
                or (self.now() - self.headline_attempt).total_seconds() >= self.headline_interval
            ):
                self.headline_attempt = self.now()
                try:
                    headlines = self.broker.headlines()
                    for headline in headlines:
                        self.contracts.validate("headline-relay", headline)
                    with self.lock:
                        self.headlines = headlines
                        self.headlines_as_of, self.headlines_error = stamp(self.now()), None
                except ContractError as error:
                    with self.lock:
                        self.headlines_error = {"code": str(error), "ts": stamp(self.now())}
                self.drain_stream()
            with self.lock:
                self.last_error = None
        except ContractError as error:
            with self.lock:
                self.last_error = {"code": str(error), "ts": stamp(self.now())}
        except Exception:
            with self.lock:
                self.last_error = {"code": "broker_worker_failed", "ts": stamp(self.now())}
        finally:
            prices = self.router.current()
            with self.lock:
                self.prices, self.last_cycle = prices, stamp(self.now())

    def start(self):
        def run():
            try:
                while not self.stop_event.is_set():
                    if not self.verified:
                        try:
                            self.broker.verify_account()
                            self.broker.verify_symbols()
                            self.stream.start()
                            with self.lock:
                                self.verified = True
                        except Exception as error:
                            with self.lock:
                                self.last_error = {
                                    "code": str(error)
                                    if isinstance(error, ContractError)
                                    else "broker_worker_failed",
                                    "ts": stamp(self.now()),
                                }
                            self.stop_event.wait(30)
                            continue
                    self.cycle()
                    self.stop_event.wait(1)
            finally:
                self.stream.stop()

        self.thread = threading.Thread(target=run, name="broker-reads", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=40)

    def state(self):
        from copy import deepcopy

        from pipmind.contracts import utc

        with self.lock:
            checked = self.now()
            values = deepcopy(self.prices)
            for value in values:
                if (checked - utc(value["venue_ts"])).total_seconds() >= (
                    self.broker.symbols.symbol(value["instrument"]).stale_seconds
                ):
                    value["stale"] = True
            return {
                "checked_at": stamp(checked),
                "stream_connected": getattr(self.stream, "connected", lambda: False)(),
                "prices": values,
                "snapshots": deepcopy(self.snapshots),
                "last_error": self.last_error,
                "dispatch_enabled": False,
                "broker_verified": self.verified,
                "worker_alive": bool(self.thread and self.thread.is_alive()),
                "last_cycle": self.last_cycle,
                "evidence_as_of": self.evidence.get("as_of") if self.evidence else None,
                "evidence_error": self.evidence_error,
                "headline_count": len(self.headlines),
                "headlines_as_of": self.headlines_as_of,
                "headlines_error": self.headlines_error,
            }

    def order_evidence(self):
        from copy import deepcopy

        with self.lock:
            return {"evidence": deepcopy(self.evidence), "last_error": self.evidence_error}

    def headline_page(self):
        from copy import deepcopy

        with self.lock:
            return {
                "headlines": deepcopy(self.headlines),
                "headlines_as_of": self.headlines_as_of,
                "headlines_error": deepcopy(self.headlines_error),
            }

    def books(self):
        """Retained owner account book. Does not call the broker or submit orders."""
        from copy import deepcopy

        from pipmind.gateway.books import project_book

        state = self.state()
        with self.lock:
            snapshots = deepcopy(self.snapshots)
            protections = deepcopy(self.protections)
            orders_page = deepcopy(self.working_orders)
            orders_error = deepcopy(self.orders_error)
            orders_as_of = self.orders_as_of
            last_snapshot = self.last_snapshot
            last_error = deepcopy(self.last_error)
        account = positions = None
        if snapshots:
            account, positions = snapshots
        return project_book(
            self.contracts,
            checked_at=state["checked_at"],
            account=account,
            positions=positions,
            protections=protections,
            orders_page=orders_page,
            orders_error=orders_error,
            orders_as_of=stamp(orders_as_of) if orders_as_of else None,
            marks=state.get("prices") or [],
            snapshot_as_of=stamp(last_snapshot) if last_snapshot else None,
            snapshot_error=last_error,
            freshness_seconds=max(120, min(self.snapshot_interval * 2, 7200)),
        )


def create_app(gateway, token, *, worker=None):
    if not isinstance(token, str) or len(token) < 32:
        raise GatewayError("gateway_token_minimum_32_characters")

    @asynccontextmanager
    async def lifespan(app):
        if worker:
            worker.start()
        yield
        if worker:
            await asyncio.to_thread(worker.stop)

    app = FastAPI(
        title="PipMind dry gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/healthz":
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ContractError)
    async def invalid(request, error):
        code = str(error)
        status = 409 if code == "command_id_collision" else 422
        if code == "dry_ledger_unavailable":
            status = 503
        return JSONResponse(
            {"error": code}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    @app.get("/healthz")
    def health():
        body = {"application": "pipmind-gateway", "mode": "dry", "dispatch_enabled": False}
        if worker and worker.thread is not None and not worker.thread.is_alive():
            return JSONResponse(body, status_code=503)
        return body

    @app.post("/orders")
    async def orders(request: Request):
        if gateway is None:
            raise GatewayError(
                "broker_commands_disabled" if worker else "broker_configuration_pending"
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16_384:
                raise HTTPException(413, detail="command_too_large")
        acknowledgement, replayed = await asyncio.to_thread(gateway.accept, loads(bytes(body)))
        return {"acknowledgement": acknowledgement, "replayed": replayed}

    @app.get("/evidence")
    def evidence():
        return (
            worker.order_evidence()
            if worker
            else {"evidence": None, "last_error": "broker_reads_disabled"}
        )

    @app.get("/state")
    def state():
        return (
            worker.state()
            if worker
            else {
                "prices": [],
                "snapshots": None,
                "dispatch_enabled": False,
                "last_error": {"code": "broker_reads_disabled"},
            }
        )

    @app.get("/books")
    def books():
        from pipmind.gateway.books import empty_book

        if not worker:
            return empty_book(stamp(datetime.now(UTC)))
        return worker.books()

    @app.get("/headlines")
    def headlines():
        return (
            worker.headline_page()
            if worker
            else {
                "headlines": [],
                "headlines_as_of": None,
                "headlines_error": "broker_reads_disabled",
            }
        )

    return app


def from_env():
    if os.environ.get("PIPMIND_GATEWAY_MODE", "dry") != "dry":
        raise GatewayError("only_dry_mode_is_implemented")
    if os.environ.get("PIPMIND_GATEWAY_UNCONFIGURED") == "true":
        if os.environ.get("PIPMIND_BROKER_READS") == "true":
            raise GatewayError("broker_configuration_pending")
        # Provision a healthy, authenticated sidecar without inventing venue IDs.
        # No ledger or command acceptance exists until real mapping is supplied.
        return create_app(None, os.environ.get("PIPMIND_GATEWAY_TOKEN", ""))
    symbols = symbols_from_env()
    config = BrokerConfig.from_env() if os.environ.get("PIPMIND_BROKER_READS") == "true" else None
    if os.environ.get("PIPMIND_GATEWAY_READ_ONLY") == "true":
        if config is None or config.environment != "demo":
            raise GatewayError("read_only_demo_configuration_required")
        try:
            worker = ReadWorker(
                Ciapi(config, symbols),
                snapshot_interval=int(os.environ.get("PIPMIND_BROKER_SNAPSHOT_INTERVAL", "60")),
                evidence_interval=int(os.environ.get("PIPMIND_BROKER_EVIDENCE_INTERVAL", "300")),
                evidence_since=utc(os.environ["PIPMIND_BROKER_EVIDENCE_FROM"])
                if os.environ.get("PIPMIND_BROKER_EVIDENCE_FROM")
                else None,
                headline_interval=60,
            )
        except ValueError:
            raise GatewayError("invalid_broker_read_intervals") from None
        # Hosted shadow has no command ledger or accepted OrderRequest path.
        return create_app(None, os.environ.get("PIPMIND_GATEWAY_TOKEN", ""), worker=worker)
    # Account/environment binding is private and cannot cross ledgers on restart.
    profile = canonical(
        {
            "mode": "dry",
            "symbols": [
                {
                    "instrument": item.instrument,
                    "market_id": item.market_id,
                    "unit": item.unit,
                    "quantity_factor": str(item.quantity_factor),
                    "increment": str(item.increment),
                    "minimum": str(item.minimum),
                }
                for item in sorted(symbols.by_symbol.values(), key=lambda item: item.instrument)
            ],
            "binding": [config.environment, config.client_account_id, config.trading_account_id]
            if config
            else "offline",
        }
    )
    ledger = DryLedger(
        os.environ.get("PIPMIND_GATEWAY_LEDGER", ".data/gateway/commands.sqlite3"), profile=profile
    )
    gateway = DryGateway(symbols, ledger, lambda: datetime.now(UTC))
    worker = ReadWorker(Ciapi(config, symbols)) if config else None
    return create_app(gateway, os.environ.get("PIPMIND_GATEWAY_TOKEN", ""), worker=worker)
