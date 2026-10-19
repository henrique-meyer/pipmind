"""Broker-origin evidence, pacing and responsive read-only HTTP regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import utc
from pipmind.gateway.ciapi import Ciapi
from pipmind.gateway.config import GatewayError
from pipmind.gateway.recording import synthetic_config, synthetic_symbols
from pipmind.gateway.service import ReadWorker, create_app, from_env
from pipmind.shadow.qualification import healthy, open_fx_window

MOMENT = utc("2026-10-19T22:00:00Z")


def broker():
    b = Ciapi(synthetic_config(), synthetic_symbols(), now=lambda: MOMENT)

    def read(path, **kw):
        key = {
            "/order/openpositions": "OpenPositions",
            "/order/activestoplimitorders": "ActiveStopLimitOrders",
            "/order/tradehistory": "TradeHistory",
            "/order/stoplimitorderhistory": "StopLimitOrderHistory",
        }[path]
        return {key: []}

    b.read = Mock(side_effect=read)
    return b


def test_evidence_preserves_broker_responses_and_different_history_date_units():
    b = broker()
    since = MOMENT - timedelta(hours=1)
    r = b.evidence(since)
    calls = b.read.call_args_list
    assert calls[2].kwargs["query"]["from"] == int(since.timestamp())
    assert calls[3].kwargs["query"]["from"] == int(since.timestamp()) * 1000
    assert all(c.kwargs["legacy"] for c in calls)
    assert r["complete"] and r["activity_count"] == 0 and len(r["responses"]) == 4
    assert r["responses"]["tradehistory"]["body"] == {"TradeHistory": []}


@pytest.mark.parametrize(
    "key", ["OpenPositions", "ActiveStopLimitOrders", "TradeHistory", "StopLimitOrderHistory"]
)
def test_evidence_never_silently_discards_foreign_account_rows(key):
    b = broker()
    b.read = Mock(return_value={key: [{"TradingAccountId": 99999}]})
    with pytest.raises(GatewayError):
        b.evidence(MOMENT)


def test_history_full_page_is_not_a_complete_zero_activity_claim():
    b = broker()
    original = b.read.side_effect

    def read(path, **kw):
        return (
            {"TradeHistory": [{"TradingAccountId": 12}] * 200}
            if path.endswith("tradehistory")
            else original(path, **kw)
        )

    b.read.side_effect = read
    r = b.evidence(MOMENT)
    assert not r["complete"] and r["activity_count"] == 200
    assert len(r["responses"]["tradehistory"]["body"]["TradeHistory"]) == 200


def worker():
    b = broker()
    b.generation = 1
    snapshots = (
        {"schema_version": 1, "as_of": MOMENT.isoformat(), "currency": "USD", "balance": 100},
        {"schema_version": 1, "as_of": MOMENT.isoformat(), "positions": []},
    )
    b.snapshots = Mock(return_value=snapshots)
    stream = SimpleNamespace(generation=1, start=Mock(), stop=Mock(), drain=Mock(return_value=[]))
    w = ReadWorker(b, stream=stream, now=lambda: MOMENT, evidence_interval=300)
    w.router.poll = Mock()
    return w


def test_status_does_not_wait_for_blocked_broker_snapshot_refresh():
    w = worker()
    entered, release = Event(), Event()

    def slow():
        entered.set()
        release.wait(3)
        return w.broker.snapshots.return_value

    w.broker.snapshots.side_effect = slow
    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(w.cycle)
        try:
            assert entered.wait(1)
            result = pool.submit(w.state).result(timeout=0.3)
            assert result["dispatch_enabled"] is False and result["snapshots"] is None
        finally:
            release.set()
            running.result(timeout=2)


def test_failed_evidence_is_paced_and_never_gets_a_fresh_success_timestamp():
    w = worker()
    w.broker.evidence = Mock(side_effect=GatewayError("broker_read_failed"))
    w.cycle()
    w.cycle()
    assert w.broker.evidence.call_count == 1
    assert w.order_evidence()["evidence"] is None
    assert w.order_evidence()["last_error"]["code"] == "broker_read_failed"


def test_authenticated_read_only_profile_rejects_commands_without_ledger():
    w = worker()
    w.start = Mock()
    w.stop = Mock()
    w.state = Mock(return_value={"dispatch_enabled": False})
    with TestClient(create_app(None, "x" * 32, worker=w)) as c:
        assert c.get("/evidence").status_code == 401
        assert (
            c.post("/orders", headers={"Authorization": "Bearer " + "x" * 32}, json={}).json()[
                "error"
            ]
            == "broker_commands_disabled"
        )


def test_hosted_factory_read_only_does_not_construct_a_dry_ledger(monkeypatch):
    import pipmind.gateway.service as service

    monkeypatch.setenv("PIPMIND_BROKER_READS", "true")
    monkeypatch.setenv("PIPMIND_GATEWAY_READ_ONLY", "true")
    monkeypatch.setenv("PIPMIND_GATEWAY_UNCONFIGURED", "false")
    monkeypatch.setenv("PIPMIND_GATEWAY_TOKEN", "x" * 32)
    monkeypatch.setattr(service, "symbols_from_env", synthetic_symbols)
    monkeypatch.setattr(service.BrokerConfig, "from_env", synthetic_config)
    monkeypatch.setattr(service, "ReadWorker", lambda *a, **kw: worker())
    monkeypatch.setattr(
        service, "DryLedger", Mock(side_effect=AssertionError("must not construct ledger"))
    )
    assert from_env() is not None


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        ("2026-10-19T22:00:00Z", True),
        ("2026-10-23T18:00:00Z", False),
        ("2026-10-24T22:00:00Z", False),
        ("2026-10-25T21:00:00Z", True),
        ("2026-11-01T22:00:00Z", True),
    ],
)
def test_full_fx_window_cannot_include_closed_weekend_hours(start, expected):
    assert open_fx_window(utc(start)) is expected


def test_missing_evidence_and_startup_backlog_cannot_pass_readiness():
    r = healthy(
        {"shadow": {"pending": 2100}, "oldest_pending_age": 3600},
        {"instruments": ["EUR/USD"], "max_input_lag": 300, "max_pending": 100},
        MOMENT,
    )
    assert {"startup_backlog", "broker_evidence_gap", "broker_not_ready"} <= set(r)


@pytest.mark.parametrize("wrapped", [False, True])
def test_symbol_secret_accepts_arm_literal_object_and_legacy_array(tmp_path, monkeypatch, wrapped):
    import json

    from pipmind.gateway.config import symbols_from_env

    rows = [
        {
            "instrument": "EUR/USD",
            "market_id": 42,
            "unit": "base_units",
            "quantity_factor": "1",
            "minimum": "1000",
            "increment": "1",
        }
    ]
    path = tmp_path / "symbols.json"
    path.write_text(json.dumps({"symbols": rows} if wrapped else rows))
    monkeypatch.setenv("PIPMIND_GATEWAY_SYMBOLS", str(path))
    assert symbols_from_env().symbol("EUR/USD").market_id == 42


def test_symbol_wrapper_rejects_unexpected_metadata(tmp_path, monkeypatch):
    from pipmind.gateway.config import symbols_from_env

    path = tmp_path / "symbols.json"
    path.write_text('{"symbols": [], "unexpected": true}')
    monkeypatch.setenv("PIPMIND_GATEWAY_SYMBOLS", str(path))
    with pytest.raises(GatewayError, match="invalid_symbol_map"):
        symbols_from_env()
