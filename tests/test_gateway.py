"""Gateway behavior through real adapters with synthetic transports; no broker calls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import ContractError, loads, utc
from pipmind.core import SimulatedClock
from pipmind.gateway.ciapi import Ciapi, Response, broker_time
from pipmind.gateway.config import GatewayError, SymbolMap, endpoint, number
from pipmind.gateway.engine import DryGateway, PriceRouter
from pipmind.gateway.ledger import DryLedger
from pipmind.gateway.recording import (
    SESSION_PATH,
    command,
    run_recording,
    synthetic_config,
    synthetic_symbols,
)
from pipmind.gateway.service import ReadWorker, create_app, from_env
from pipmind.gateway.stream import FIELDS, LightstreamerPrices


@pytest.fixture
def clock():
    return SimulatedClock(utc("2026-10-05T09:00:00Z"))


@pytest.fixture
def gateway(tmp_path, clock):
    return DryGateway(synthetic_symbols(), DryLedger(tmp_path / "ledger.sqlite3"), clock.now)


def quote(clock, **changes):
    return {
        "MarketId": "42",
        "Bid": "1.1",
        "Offer": "1.1002",
        "TickDate": clock.now().isoformat(),
        "StatusSummary": "0",
        **changes,
    }


def test_recording_exercises_real_gateway(tmp_path):
    report = run_recording(directory=tmp_path)
    assert report["passed"], report["checks"]
    assert len(report["checks"]) == 15 and report["dispatches"] == 0
    assert report == run_recording()  # Separate state, same results.


def test_recording_mutation_is_detected(tmp_path):
    data = loads(SESSION_PATH.read_bytes())
    data["timeline"][-1]["payload"]["TickDate"] = "2026-10-05T09:00:00Z"
    path = tmp_path / "session.json"
    import json

    path.write_text(json.dumps(data))
    report = run_recording(path)
    assert not report["passed"]
    assert not report["checks"]["stream_rest_stream"]


def test_command_restart_dedupe_immutable_ack(gateway, clock):
    ack, replayed = gateway.accept(command())
    clock.advance(clock.now() + timedelta(days=1))
    restarted = DryGateway(gateway.symbols, DryLedger(gateway.ledger.path), clock.now)
    assert restarted.accept(command()) == (ack, True)
    assert not replayed and ack["update"]["status"] == "accepted"
    assert ack["mode"] == "dry" and ack["dispatch_state"] == "not_sent"
    assert "broker_order_id" not in ack["update"]
    assert gateway.ledger.counts()["acknowledgements"] == 1


def test_concurrent_command_has_single_durable_ack(gateway, clock):
    def accept(_):
        instance = DryGateway(gateway.symbols, DryLedger(gateway.ledger.path), clock.now)
        return instance.accept(command())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(accept, range(16)))
    assert sum(not replayed for _, replayed in results) == 1
    assert all(ack == results[0][0] for ack, _ in results)
    assert gateway.ledger.counts()["commands"] == 1
    assert gateway.ledger.counts()["acknowledgements"] == 1


def test_collision_does_not_overwrite_command(gateway):
    original, _ = gateway.accept(command())
    with pytest.raises(GatewayError, match="^command_id_collision$"):
        gateway.accept(command(size=2000))
    assert gateway.accept(command()) == (original, True)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"instrument": "USD/UNKNOWN"}, "unsupported_instrument"),
        ({"size": 1001}, "unrepresentable_size"),
        ({"size": 1}, "unrepresentable_size"),
        ({"stop": -1}, "invalid_broker_number"),
        ({"action": "modify"}, "related_command_required"),
        ({"related_client_order_id": "missing"}, "unknown_related_command"),
    ],
)
def test_refusals_are_durable_and_never_round(gateway, changes, reason):
    request = command(**changes)
    ack, _ = gateway.accept(request)
    assert ack["update"]["status"] == "rejected" and ack["update"]["reason"] == reason
    assert gateway.accept(request) == (ack, True)
    assert gateway.ledger.counts()["acknowledgements"] == 1


def test_mapping_and_unknown_outcome_survive_restart(gateway, clock):
    gateway.accept(command())
    modified = command("modify", action="modify", related_client_order_id="recorded-command")
    assert (
        gateway.accept(modified)[0]["update"]["reason"]
        == "related_command_has_no_broker_identifier"
    )
    gateway.ledger.bind_recorded_identifier("recorded-command", "synthetic-77")
    gateway.ledger.bind_recorded_identifier("recorded-command", "synthetic-77")
    accepted = gateway.accept({**modified, "client_order_id": "modify-next"})[0]
    assert accepted["update"]["status"] == "accepted"
    with pytest.raises(GatewayError, match="broker_identifier_conflict"):
        gateway.ledger.bind_recorded_identifier("recorded-command", "synthetic-88")
    gateway.ledger.record_uncertain(command("unknown"), clock.now(), broker_id="synthetic-99")
    restarted = DryGateway(gateway.symbols, DryLedger(gateway.ledger.path), clock.now)
    uncertain, _ = restarted.accept(command("unknown"))
    assert uncertain["dispatch_state"] == "uncertain"
    assert uncertain["update"]["reason"] == "uncertain_outcome_reconciliation_required"
    assert restarted.accept(command("unknown")) == (uncertain, True)
    related = command("related", action="modify", related_client_order_id="unknown")
    assert restarted.accept(related)[0]["dispatch_state"] == "refused"


def test_ledger_failure_and_profile_mismatch_fail_closed(gateway, tmp_path):
    with pytest.raises(GatewayError, match="dry_ledger_profile_mismatch"):
        DryLedger(gateway.ledger.path, profile="another-account")
    with pytest.raises(GatewayError, match="durable_dry_ledger_required"):
        DryLedger(":memory:")
    Path(gateway.ledger.path).unlink()
    Path(gateway.ledger.path).mkdir()
    with pytest.raises(GatewayError, match="dry_ledger_unavailable"):
        gateway.accept(command())


def test_intent_transaction_rolls_back_on_ack_failure(gateway, monkeypatch):
    original = gateway.ledger.contracts.validate

    def invalid(name, value):
        if name == "gateway-acknowledgement":
            raise ContractError("synthetic-crash")
        return original(name, value)

    monkeypatch.setattr(gateway.ledger.contracts, "validate", invalid)
    with pytest.raises(ContractError, match="synthetic-crash"):
        gateway.accept(command())
    assert gateway.ledger.counts() == {"commands": 0, "acknowledgements": 0, "uncertain": 0}


@pytest.mark.parametrize("size", [0, -1, None, True, float("nan"), float("inf")])
def test_invalid_commands_have_no_acceptance(gateway, size):
    with pytest.raises(ContractError):
        gateway.accept(command(size=size))
    assert gateway.ledger.counts()["commands"] == 0


def test_exact_decimal_mapping():
    symbols = SymbolMap(
        [
            {
                "instrument": "EUR/USD",
                "market_id": 42,
                "unit": "lots",
                "quantity_factor": "100000",
                "minimum": "1000",
                "increment": "1000",
            }
        ]
    )
    assert symbols.symbol("EUR/USD").quantity(0.01) == Decimal(1000)
    with pytest.raises(GatewayError, match="unrepresentable_size"):
        symbols.symbol("EUR/USD").quantity(0.01001)


@pytest.mark.parametrize("value", [None, {}, True, "NaN", "Infinity", "1e100000", "-1"])
def test_broker_numbers_are_finite_bounded_positive(value):
    with pytest.raises(GatewayError):
        number(value, positive=True)


@pytest.mark.parametrize(
    "value",
    [
        "http://broker.invalid",
        "https://user:pass@broker.invalid",
        "https://broker.invalid/?session=secret",
        "https://broker.invalid/#x",
        "https://broker.invalid whitespace",
    ],
)
def test_endpoint_configuration_cannot_embed_credentials(value):
    with pytest.raises(GatewayError):
        endpoint(value)


def test_no_credentials_in_config_representation():
    config = synthetic_config()
    assert "password" not in repr(config) and "synthetic" not in repr(config)


def test_time_conversion_and_rejection(clock):
    assert broker_time("/Date(1791190800000)/") == clock.now()
    with pytest.raises(GatewayError):
        broker_time("/Date(999999999999999999999999999)/")
    with pytest.raises(ContractError):
        broker_time("2026-10-05T09:00:00")


@pytest.mark.parametrize("escapes", [0, 1, 2])
def test_stream_escaped_date_keeps_the_exact_venue_instant(clock, escapes):
    wrapper = "\\" * escapes
    raw = wrapper + "/Date(1791190800123)" + wrapper + "/"
    assert broker_time(raw) == clock.now() + timedelta(milliseconds=123)
    with pytest.raises(GatewayError):
        broker_time("\\\\\\/Date(1791190800123)\\\\\\/")


def test_future_stream_quote_preserves_the_specific_refusal(clock):
    router = PriceRouter(synthetic_symbols(), None, clock.now)
    raw = quote(clock, TickDate=(clock.now() + timedelta(seconds=1)).isoformat())
    with pytest.raises(GatewayError, match="^future_broker_quote$"):
        router.stream(raw)
    assert router.current() == []
    clock.advance(clock.now() + timedelta(seconds=2))
    event = router.stream(raw)
    assert utc(event["venue_ts"]) == clock.now() - timedelta(seconds=1)


def test_transport_read_allowlist_and_bounded_retries(clock):
    transport = Mock()
    transport.request.side_effect = [
        Response(200, {"Session": "x" * 36}),
        Response(401, None),
        Response(200, {"Session": "y" * 36}),
        Response(429, None, 3),
        Response(200, {"ok": True}),
    ]
    sleeps = []
    broker = Ciapi(
        synthetic_config(),
        synthetic_symbols(),
        transport=transport,
        sleep=sleeps.append,
        now=clock.now,
    )
    assert broker.read("/v2/margin/clientAccountMargin") == {"ok": True}
    assert broker.generation == 2 and sleeps == [3]
    reads = [c for c in transport.request.call_args_list if c.args[0] == "GET"]
    assert all(not {"Session", "UserName"} & set(c.kwargs.get("query") or {}) for c in reads)
    assert all(set(c.kwargs["headers"]) == {"Session", "UserName"} for c in reads)
    with pytest.raises(GatewayError, match="broker_endpoint_forbidden"):
        broker.read("/order/newtradeorder")
    assert all(
        call.args[0] == "GET" or call.args[1].endswith("/v2/Session")
        for call in transport.request.call_args_list
    )


@pytest.mark.parametrize("status", [401, 429, 503])
def test_retries_terminate(status):
    transport = Mock()
    transport.request.side_effect = lambda method, url, **_: (
        Response(200, {"Session": "x" * 36}) if method == "POST" else Response(status, None)
    )
    broker = Ciapi(
        synthetic_config(), synthetic_symbols(), transport=transport, sleep=lambda _: None
    )
    with pytest.raises(GatewayError):
        broker.read("/v2/margin/clientAccountMargin")
    assert transport.request.call_count <= 5


def test_expired_session_restarts_stream_without_callback_network(clock):
    broker = SimpleNamespace(
        symbols=synthetic_symbols(), generation=2, login=Mock(), snapshots=Mock()
    )
    broker.snapshots.return_value = (
        {
            "schema_version": 1,
            "as_of": clock.now().isoformat(),
            "currency": "USD",
            "balance": 10000,
        },
        {"schema_version": 1, "as_of": clock.now().isoformat(), "positions": []},
    )
    stream = SimpleNamespace(generation=1, start=Mock(), drain=Mock(return_value=[quote(clock)]))
    worker = ReadWorker(broker, stream=stream, now=clock.now)
    worker.cycle()
    stream.start.assert_called_once()
    assert worker.state()["snapshots"] is not None
    before = deepcopy(worker.snapshots)
    clock.advance(clock.now() + timedelta(seconds=31))
    broker.price = Mock(side_effect=GatewayError("broker_read_failed"))
    broker.snapshots.side_effect = GatewayError("broker_read_failed")
    stream.drain.return_value = []
    worker.cycle()
    assert worker.snapshots == before  # No fresh timestamp on failed reads.
    assert worker.state()["last_error"]["code"] == "broker_read_failed"


def test_symbol_verification_mismatch():
    transport = Mock()
    transport.request.side_effect = [
        Response(200, {"Session": "x" * 36}),
        Response(200, {"MarketInformation": {"MarketId": 42, "Name": "GBP/USD"}}),
    ]
    broker = Ciapi(synthetic_config(), synthetic_symbols(), transport=transport)
    with pytest.raises(GatewayError, match="broker_symbol_mismatch"):
        broker.verify_symbols()


@pytest.mark.parametrize(
    "account",
    [
        {"ClientAccountId": 11, "TradingAccountId": 13, "TradingAccountStatus": "Open"},
        {"ClientAccountId": 99, "TradingAccountId": 12, "TradingAccountStatus": "Open"},
        {"ClientAccountId": 11, "TradingAccountId": 12, "TradingAccountStatus": "Closed"},
    ],
)
def test_account_selection_does_not_default_or_cross_accounts(account):
    broker = Ciapi(synthetic_config(), synthetic_symbols())
    broker.read = Mock(return_value={"AccountResult": {"TradingAccounts": [account]}})
    with pytest.raises(GatewayError, match="broker_account_mismatch"):
        broker.verify_account()


@pytest.mark.parametrize("flag", ["PasswordChangeRequired", "Is2FAEnabled"])  # gitleaks:allow
def test_interactive_login_requirements_cannot_be_bypassed(flag):
    transport = Mock()
    transport.request.return_value = Response(200, {"Session": "x" * 36, flag: True})
    broker = Ciapi(synthetic_config(), synthetic_symbols(), transport=transport)
    with pytest.raises(GatewayError, match="broker_login_requires_attention"):
        broker.login()
    assert broker.generation == 0 and broker._session is None


@pytest.mark.parametrize("expiry", [None, 0, -1])
def test_demo_binding_requires_positive_expiry_on_the_selected_operator(expiry):
    broker = Ciapi(synthetic_config(), synthetic_symbols())
    broker.read = Mock(
        return_value={
            "tradingAccounts": [
                {"clientAccountId": 11, "tradingAccountId": 12, "tradingAccountStatus": "Open"}
            ],
            "clientAccounts": [{"clientAccountId": 11, "accountOperatorId": 13}],
            "accountOperators": [{"accountOperatorId": 13, "daysUntilExpiryForDemo": expiry}],
        }
    )
    with pytest.raises(GatewayError):
        broker.verify_account()


def test_false_operator_flag_is_demo_read_only_and_aliases_cannot_conflict():
    cfg = synthetic_config()
    cfg.environment = "live"
    transport = Mock()
    transport.request.return_value = Response(
        200, {"session": "x" * 36, "allowedAccountOperator": False}
    )
    broker = Ciapi(cfg, synthetic_symbols(), transport=transport)
    with pytest.raises(GatewayError, match="broker_login_requires_attention"):
        broker.login()
    cfg.environment = "demo"
    broker.login()
    assert broker.generation == 1
    transport.request.return_value = Response(200, {"session": "x" * 36, "Session": "y" * 36})
    with pytest.raises(GatewayError, match="ambiguous_broker_fields"):
        broker.login()


def test_current_camel_case_market_and_snapshot_dtos(clock):
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=clock.now)
    broker.read = Mock(
        side_effect=[
            {
                "marketInformation": {
                    "marketId": 42,
                    "name": "EUR/USD",
                    "incrementSize": 1000,
                    "webMinSize": 1000,
                }
            },
            {
                "currencyIsoCode": "USD",
                "cash": 10000,
                "netEquity": 10000,
                "margin": 0,
                "tradableFunds": 10000,
            },
            {"openPositions": []},
        ]
    )
    broker.verify_symbols()
    account, positions = broker.snapshots()
    assert account["balance"] == account["equity"] == 10000
    assert positions["positions"] == []


def test_http_transport_bounds_body_and_does_not_follow_redirects():
    from io import BytesIO
    from urllib.error import HTTPError

    from pipmind.gateway.ciapi import MAX_BODY, HttpTransport, NoRedirect

    transport = HttpTransport()
    response = Mock()
    response.read.return_value = b"x" * (MAX_BODY + 1)
    transport.opener = MagicMock()
    transport.opener.open.return_value.__enter__.return_value = response
    with pytest.raises(GatewayError, match="broker_response_too_large"):
        transport.request("GET", "https://broker.invalid")
    raw = HTTPError(
        "https://broker.invalid?secret=DO-NOT-LOG",
        429,
        "DO-NOT-LOG",
        {"Retry-After": "99999"},
        BytesIO(b"DO-NOT-LOG"),
    )
    transport.opener.open.side_effect = raw
    result = transport.request("GET", "https://broker.invalid")
    assert result == Response(429, None, 60)
    assert NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_unknown_position_does_not_disappear_from_snapshot(clock):
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=clock.now)
    broker.read = Mock(
        side_effect=[
            {"CurrencyIsoCode": "USD", "Cash": 1, "NetEquity": 1, "Margin": 0, "TradableFunds": 1},
            {"OpenPositions": [{"MarketId": 999}]},
        ]
    )
    with pytest.raises(GatewayError, match="unknown_broker_market"):
        broker.snapshots()


def test_incoherent_rest_sides_are_never_emitted(clock):
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=clock.now)
    broker.read = Mock(
        side_effect=[
            {"PriceTicks": [{"Price": 1.1, "TickDate": "2026-10-05T09:00:00Z"}]},
            {"PriceTicks": [{"Price": 1.1002, "TickDate": "2026-10-05T09:00:01Z"}]},
        ]
    )
    with pytest.raises(GatewayError, match="incoherent_rest_quote"):
        broker.price(synthetic_symbols().symbol("EUR/USD"))


def test_price_watchdog_stale_repeat_and_fresh_recovery(clock):
    broker, reconnect = Mock(), Mock()
    router = PriceRouter(synthetic_symbols(), broker, clock.now, resubscribe=reconnect)
    assert router.stream(quote(clock))["source"] == "stream"
    old = quote(clock)
    clock.advance(clock.now() + timedelta(seconds=16))
    broker.price.return_value = quote(clock)
    fallback = router.poll()
    assert fallback[0]["stale"] and fallback[-1]["source"] == "rest"
    assert router.stream(old) is None
    assert router.sources["EUR/USD"] == "rest"
    clock.advance(clock.now() + timedelta(seconds=1))
    assert router.stream(quote(clock))["source"] == "stream"
    assert [row["source"] for row in router.transitions] == ["stream", "rest", "stream"]
    reconnect.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"Bid": 2, "Offer": 1},
        {"Bid": "NaN"},
        {"TickDate": "2027-01-01T00:00:00Z"},
        {"MarketId": "unknown"},
    ],
)
def test_bad_stream_ticks_do_not_refresh_watchdog(clock, changes):
    router = PriceRouter(synthetic_symbols(), Mock(), clock.now)
    with pytest.raises(ContractError):
        router.stream(quote(clock, **changes))
    assert router.stream_times == {} and router.latest == {}


def test_closed_market_and_stale_rest_remain_stale(clock):
    broker = Mock()
    router = PriceRouter(synthetic_symbols(), broker, clock.now)
    assert router.stream(quote(clock, StatusSummary="4")) is None
    old = quote(clock)
    clock.advance(clock.now() + timedelta(seconds=30))
    broker.price.return_value = old
    assert router.poll()[-1]["stale"]
    assert router.current()[0]["venue_ts"] == old["TickDate"].replace("+00:00", "Z")


def test_watchdog_is_per_instrument_and_rest_is_throttled(clock):
    row = {
        "instrument": "GBP/USD",
        "market_id": 43,
        "unit": "base_units",
        "quantity_factor": 1,
        "minimum": 1000,
        "increment": 1000,
        "stale_seconds": 30,
    }
    first = {**row, "instrument": "EUR/USD", "market_id": 42, "stale_seconds": 15}
    broker = Mock()
    broker.price.side_effect = GatewayError("broker_transport_unavailable")
    router = PriceRouter(SymbolMap([first, row]), broker, clock.now)
    router.stream(quote(clock))
    router.stream(quote(clock, MarketId="43"))
    clock.advance(clock.now() + timedelta(seconds=16))
    router.poll()
    router.poll()
    assert broker.price.call_count == 1
    assert router.current()[0]["stale"] and not router.current()[1]["stale"]


def test_sdk_subscription_mapping_epoch_and_queue(clock):
    client, subscription = Mock(), Mock()
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=clock.now)
    broker._session, broker.generation = "x" * 36, 1
    stream = LightstreamerPrices(
        broker,
        client_factory=Mock(return_value=client),
        subscription_factory=Mock(return_value=subscription),
    )
    stream.start()
    stream.subscription_factory.assert_called_once_with("MERGE", ["ID.42"], FIELDS)
    subscription.setDataAdapter.assert_called_once_with("PRICES")
    listener = subscription.addListener.call_args.args[0]
    update = SimpleNamespace(getValue=lambda field: quote(clock).get(field))
    listener.onItemUpdate(update)
    assert stream.drain()[0]["MarketId"] == "42"
    old_epoch = stream.epoch
    stream.stop()
    stream._enqueue(old_epoch, quote(clock))
    assert stream.drain() == []
    for _ in range(1001):
        stream._enqueue(stream.epoch, quote(clock))
    with pytest.raises(GatewayError, match="stream_queue_overflow"):
        stream.drain()
    assert stream.queue.qsize() == 1000


@pytest.mark.parametrize("connected", [True, False])
def test_quiet_price_keeps_stale_flags_and_only_recovers_broken_transport(clock, connected):
    original = quote(clock)
    broker = SimpleNamespace(
        symbols=synthetic_symbols(),
        generation=1,
        price=Mock(return_value=original),
        snapshots=Mock(
            return_value=(
                {
                    "schema_version": 1,
                    "as_of": clock.now().isoformat(),
                    "currency": "USD",
                    "balance": 1,
                },
                {"schema_version": 1, "as_of": clock.now().isoformat(), "positions": []},
            )
        ),
    )
    stream = SimpleNamespace(
        generation=1, start=Mock(), drain=Mock(return_value=[]), connected=lambda: connected
    )
    worker = ReadWorker(broker, stream=stream, now=clock.now)
    worker.router.stream(original)
    clock.advance(clock.now() + timedelta(seconds=16))
    worker.cycle()
    assert worker.state()["prices"][0]["stale"]
    assert worker.state()["prices"][0]["venue_ts"] == original["TickDate"].replace("+00:00", "Z")
    assert stream.start.call_count == (0 if connected else 1)


def test_callbacks_during_slow_account_read_are_drained_before_cached_state(clock):
    def snapshots():
        clock.advance(clock.now() + timedelta(seconds=20))
        return (
            {
                "schema_version": 1,
                "as_of": clock.now().isoformat(),
                "currency": "USD",
                "balance": 1,
            },
            {"schema_version": 1, "as_of": clock.now().isoformat(), "positions": []},
        )

    broker = SimpleNamespace(symbols=synthetic_symbols(), generation=1, snapshots=snapshots)
    stream = SimpleNamespace(generation=1, start=Mock(), drain=lambda: [quote(clock)])
    worker = ReadWorker(broker, stream=stream, now=clock.now)
    worker.cycle()
    assert worker.last_error is None
    assert worker.state()["prices"][0]["venue_ts"] == clock.now().isoformat().replace("+00:00", "Z")
    assert worker.state()["prices"][0]["stale"] is False


def test_sdk_transport_status_and_subscription_failure_do_not_forge_quote_freshness(clock):
    client, subscription = Mock(), Mock()
    client.getStatus.return_value = "CONNECTED:HTTP-STREAMING"
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=clock.now)
    broker._session, broker.generation = "x" * 36, 1
    stream = LightstreamerPrices(
        broker, client_factory=lambda *_: client, subscription_factory=lambda *_: subscription
    )
    stream.start()
    assert stream.connected()
    stream._enqueue(stream.epoch - 1, {"error": "stream_subscription_error"})
    assert stream.connected()  # An old connection cannot invalidate the current transport.
    listener = subscription.addListener.call_args.args[0]
    listener.onSubscriptionError(17, "Synthetic refusal")
    assert not stream.connected()
    assert stream.drain() == [{"error": "stream_subscription_error"}]
    client.getStatus.return_value = "STALLED"
    stream.start()
    assert not stream.connected()


def test_internal_http_auth_dedupe_and_no_public_mount(gateway):
    token = "synthetic-local-gateway-token-000000000000"
    with TestClient(create_app(gateway, token)) as client:
        assert client.get("/healthz").json()["dispatch_enabled"] is False
        assert client.get("/state").status_code == 401
        assert client.post("/orders", json=command()).status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        first = client.post("/orders", json=command(), headers=headers)
        assert first.status_code == 200 and not first.json()["replayed"]
        again = client.post("/orders", json=command(), headers=headers)
        assert again.json()["replayed"]
        assert first.json()["acknowledgement"] == again.json()["acknowledgement"]
        assert client.post("/orders", json=command(size=2000), headers=headers).status_code == 409
        assert client.post("/orders", content='{"x":1,"x":2}', headers=headers).status_code == 422
        assert client.post("/orders", content="x" * 16385, headers=headers).status_code == 413
        assert client.get("/docs", headers=headers).status_code == 404
        assert client.get("/state", headers=headers).headers["Cache-Control"] == "no-store"
    from app.main import app

    with TestClient(app) as public:
        assert public.post("/orders", json=command()).status_code == 404
        assert public.get("/state").status_code == 404


def test_configuration_defaults_have_no_broker_calls(gateway, monkeypatch):
    monkeypatch.setenv("PIPMIND_GATEWAY_MODE", "demo")
    with pytest.raises(GatewayError, match="only_dry_mode_is_implemented"):
        from_env()
    with pytest.raises(GatewayError, match="gateway_token_minimum"):
        create_app(gateway, "short")


def test_unconfigured_sidecar_refuses_commands_and_live_reads(monkeypatch):
    monkeypatch.setenv("PIPMIND_GATEWAY_UNCONFIGURED", "true")
    monkeypatch.setenv("PIPMIND_GATEWAY_TOKEN", "s" * 32)
    monkeypatch.delenv("PIPMIND_GATEWAY_SYMBOLS", raising=False)
    monkeypatch.delenv("PIPMIND_BROKER_READS", raising=False)
    with TestClient(from_env()) as client:
        assert client.get("/state").status_code == 401
        headers = {"Authorization": "Bearer " + "s" * 32}
        state = client.get("/state", headers=headers).json()
        assert state["snapshots"] is None and state["prices"] == []
        assert state["dispatch_enabled"] is False
        response = client.post("/orders", json=command(), headers=headers)
        assert response.status_code == 422
        assert response.json()["error"] == "broker_configuration_pending"
    monkeypatch.setenv("PIPMIND_BROKER_READS", "true")
    with pytest.raises(GatewayError, match="broker_configuration_pending"):
        from_env()
