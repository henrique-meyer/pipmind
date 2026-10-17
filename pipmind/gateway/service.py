"""Internal authenticated dry gateway; never mounted by the public landing app."""

import asyncio
import hmac
import os
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from pipmind.contracts import ContractError, Contracts, loads, stamp
from pipmind.gateway.ciapi import Ciapi
from pipmind.gateway.config import BrokerConfig, GatewayError, symbols_from_env
from pipmind.gateway.engine import DryGateway, PriceRouter
from pipmind.gateway.ledger import DryLedger, canonical
from pipmind.gateway.stream import LightstreamerPrices


class ReadWorker:
    def __init__(self, broker, *, stream=None, now=None):
        self.broker = broker
        self.now = now or (lambda: datetime.now(UTC))
        self.stream = stream or LightstreamerPrices(broker)
        self.router = PriceRouter(broker.symbols, broker, self.now, resubscribe=self.stream.start)
        self.lock, self.stop_event = threading.RLock(), threading.Event()
        self.contracts = Contracts()
        self.snapshots, self.last_snapshot, self.last_error = None, None, None
        self.thread = None

    def cycle(self):
        with self.lock:
            try:
                if self.stream.generation != self.broker.generation:
                    self.stream.start()
                for raw in self.stream.drain():
                    if "error" in raw:
                        self.router.stream_times.clear()
                        if raw["error"] == "stream_auth":
                            self.broker.login()
                        raise GatewayError(raw["error"])
                    self.router.stream(raw)
                self.router.poll()
                now = self.now()
                if self.last_snapshot is None or (now - self.last_snapshot).total_seconds() >= 30:
                    snapshots = self.broker.snapshots()
                    self.contracts.validate("account-snapshot", snapshots[0])
                    self.contracts.validate("position-snapshot", snapshots[1])
                    self.snapshots, self.last_snapshot = snapshots, now
                self.last_error = None
            except ContractError as error:
                self.last_error = {"code": str(error), "ts": stamp(self.now())}

    def start(self):
        def run():
            initialized = False
            try:
                while not self.stop_event.is_set():
                    if not initialized:
                        try:
                            self.broker.verify_account()
                            self.broker.verify_symbols()
                            self.stream.start()
                            initialized = True
                        except ContractError as error:
                            with self.lock:
                                self.last_error = {"code": str(error), "ts": stamp(self.now())}
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
        with self.lock:
            return {
                "prices": self.router.current(),
                "snapshots": self.snapshots,
                "last_error": self.last_error,
                "dispatch_enabled": False,
            }


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
        return {"application": "pipmind-gateway", "mode": "dry", "dispatch_enabled": False}

    @app.post("/orders")
    async def orders(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16_384:
                raise HTTPException(413, detail="command_too_large")
        acknowledgement, replayed = await asyncio.to_thread(gateway.accept, loads(bytes(body)))
        return {"acknowledgement": acknowledgement, "replayed": replayed}

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

    return app


def from_env():
    if os.environ.get("PIPMIND_GATEWAY_MODE", "dry") != "dry":
        raise GatewayError("only_dry_mode_is_implemented")
    symbols = symbols_from_env()
    config = BrokerConfig.from_env() if os.environ.get("PIPMIND_BROKER_READS") == "true" else None
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
