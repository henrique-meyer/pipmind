"""Original synthetic CIAPI session played through the real dry adapter, offline."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from pipmind.contracts import ROOT, ContractError, Contracts, loads, utc
from pipmind.core import SimulatedClock
from pipmind.gateway.ciapi import Ciapi, Response
from pipmind.gateway.config import BrokerConfig, GatewayError, SymbolMap
from pipmind.gateway.engine import DryGateway, PriceRouter
from pipmind.gateway.ledger import DryLedger

SESSION_PATH = ROOT / "evals" / "gateway" / "session-v1.json"


class RecordedTransport:
    def __init__(self, exchanges):
        self.exchanges, self.calls = deepcopy(exchanges), []

    def request(self, method, url, *, query=None, body=None):
        path = urlsplit(url).path
        self.calls.append((method, path))  # Auth/query/body are deliberately absent.
        if not self.exchanges:
            raise GatewayError("unexpected_recorded_request")
        expected = self.exchanges.pop(0)
        supplied = query or {}
        if (
            method != expected["method"]
            or path != expected["path"]
            or any(supplied.get(key) != value for key, value in expected.get("query", {}).items())
        ):
            raise GatewayError("recorded_request_mismatch")
        return Response(expected["status"], expected.get("body"), expected.get("retry_after", 0))


def synthetic_symbols():
    return SymbolMap(
        [
            {
                "instrument": "EUR/USD",
                "market_id": 42,
                "unit": "base_units",
                "quantity_factor": "1",
                "increment": "1000",
                "minimum": "1000",
                "stale_seconds": 15,
            }
        ]
    )


def synthetic_config():
    return BrokerConfig(
        "https://broker.invalid",
        "https://broker.invalid/TradingAPI",
        "https://stream.invalid",
        "synthetic",
        "synthetic",
        "synthetic",
        11,
        12,
        "demo",
    )


def command(key="recorded-command", **changes):
    return {
        "schema_version": 1,
        "client_order_id": key,
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": 1000,
        "order_type": "market",
        **changes,
    }


def run_recording(path=SESSION_PATH, *, directory=None):
    if directory is None:
        with TemporaryDirectory(prefix="pipmind-dry-") as temp:
            return run_recording(path, directory=Path(temp))
    data = loads(Path(path).read_bytes())
    if data.get("format") != "synthetic-ciapi-v1":
        raise GatewayError("invalid_gateway_recording")
    clock = SimulatedClock(utc(data["clock_start"]))
    transport = RecordedTransport(data["exchanges"])
    sleeps, resubscriptions = [], []
    symbols = synthetic_symbols()
    broker = Ciapi(
        synthetic_config(), symbols, transport=transport, sleep=sleeps.append, now=clock.now
    )
    broker.verify_account()
    broker.verify_symbols()
    account, positions = broker.snapshots()
    contracts = Contracts()
    contracts.validate("account-snapshot", account)
    contracts.validate("position-snapshot", positions)
    router = PriceRouter(
        symbols, broker, clock.now, resubscribe=lambda: resubscriptions.append(True)
    )
    events = []
    for step in data["timeline"]:
        clock.advance(utc(step["at"]))
        if step["kind"] == "stream":
            result = router.stream(step["payload"])
            events.extend([result] if result else [])
        elif step["kind"] == "poll":
            events.extend(router.poll())
        else:
            raise GatewayError("invalid_gateway_recording")
    ledger_path = Path(directory) / "dry-commands.sqlite3"
    ledger = DryLedger(ledger_path)
    gateway = DryGateway(symbols, ledger, clock.now)
    first, duplicate = gateway.accept(command())
    repeated, replayed = gateway.accept(command())
    restarted = DryGateway(symbols, DryLedger(ledger_path), clock.now)
    after_restart, restart_replayed = restarted.accept(command())
    collision = False
    try:
        restarted.accept(command(size=2000))
    except GatewayError as error:
        collision = str(error) == "command_id_collision"
    unsupported, _ = gateway.accept(command("unknown", instrument="UNKNOWN/PAIR"))
    size, _ = gateway.accept(command("size", size=1001))
    ledger.bind_recorded_identifier("recorded-command", "synthetic-broker-42")
    modified, _ = gateway.accept(
        command("modify", action="modify", related_client_order_id="recorded-command")
    )
    ledger.record_uncertain(command("uncertain"), clock.now(), broker_id="synthetic-unknown")
    uncertain, _ = restarted.accept(command("uncertain"))
    uncertain_retry, uncertain_replayed = restarted.accept(command("uncertain"))
    counts = ledger.counts()
    checks = {
        "stream_rest_stream": [row["source"] for row in router.transitions]
        == ["stream", "rest", "stream"],
        "stale_tick_does_not_recover": sum(
            row["source"] == "stream" and not row["stale"] for row in events
        )
        == 2,
        "stale_marked": any(row["stale"] for row in events),
        "venue_time_preserved": all(
            utc(row["venue_ts"]) <= utc(row["received_at"]) for row in events
        ),
        "session_relogin": broker.generation == 2,
        "rate_limit_backoff": sleeps == [2],
        "snapshot_mapping": account["balance"] == 10000
        and positions["positions"][0]["size"] == 1000
        and positions["positions"][0]["instrument"] == "EUR/USD",
        "durable_single_ack": not duplicate
        and replayed
        and restart_replayed
        and first == repeated == after_restart
        and counts["acknowledgements"] == 5,
        "payload_collision": collision,
        "unsupported_instrument": unsupported["update"]["reason"] == "unsupported_instrument",
        "exact_size": size["update"]["reason"] == "unrepresentable_size",
        "broker_identifier_mapping": modified["update"]["status"] == "accepted",
        "uncertain_no_resend": uncertain["dispatch_state"] == "uncertain"
        and uncertain == uncertain_retry
        and uncertain_replayed
        and counts["uncertain"] == 1,
        "read_only_transport": all(
            method == "GET" or path == "/v2/Session" for method, path in transport.calls
        ),
        "recording_consumed": not transport.exchanges and bool(resubscriptions),
    }
    # Full events/account data stay local. CI receives only these fixed booleans/counts.
    return {
        "version": "gateway-session-v1",
        "mode": "dry",
        "dispatches": 0,
        "checks": checks,
        "passed": all(checks.values()),
    }


def exercise_mapping_case(case, trace):
    """M1 S01 now exercises durable gateway refusal, not a fabricated guard trace."""
    with TemporaryDirectory(prefix="pipmind-mapping-") as temp:
        ledger = DryLedger(Path(temp) / "ledger.sqlite3")
        gateway = DryGateway(synthetic_symbols(), ledger, lambda: utc(trace["decisions"][-1]["ts"]))
        for proposal in trace["proposals"]:
            request = command(
                proposal["decision_id"],
                instrument=proposal["instrument"],
                size=proposal["size"],
                side=proposal["side"],
            )
            ack, _ = gateway.accept(request)
            trace["commands"].append(
                {"command_id": request["client_order_id"], "dispatch_count": 0}
            )
            refused = ack["update"]["status"] == "rejected"
            trace["guard_records"].append(
                {
                    "control": ack["update"]["reason"],
                    "refused": refused,
                    "journaled": ledger.counts()["acknowledgements"] == 1,
                }
            )
            if not refused:
                raise ContractError("gateway_mapping_refusal_missing")
