"""Read-only account book. Synthetic broker rows only; no venue calls."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from pipmind.contracts import Contracts, stamp, utc
from pipmind.core import SimulatedClock
from pipmind.gateway.books import empty_book, normalize_working_orders, project_book
from pipmind.gateway.ciapi import Ciapi
from pipmind.gateway.recording import synthetic_config, synthetic_symbols
from pipmind.gateway.service import ReadWorker, create_app


def quote(moment):
    return {
        "MarketId": "42",
        "Bid": "1.1",
        "Offer": "1.1002",
        "TickDate": moment.now().isoformat(),
        "StatusSummary": "0",
    }


def clock():
    return SimulatedClock(utc("2026-10-05T09:00:00Z"))


def position_row(**extra):
    return {
        "OrderId": 77,
        "TradingAccountId": 12,
        "MarketId": 42,
        "Direction": "Buy",
        "Quantity": 1000,
        "Price": 1.1,
        **extra,
    }


def order_row(**extra):
    return {
        "OrderId": 88,
        "TradingAccountId": 12,
        "MarketId": 42,
        "Direction": "Sell",
        "Quantity": 1000,
        "TriggerPrice": 1.05,
        "Status": 3,
        "LastChangedDateTimeUTC": "2026-10-05T09:00:00Z",
        **extra,
    }


def snapshots(moment):
    stamped = stamp(moment)
    return (
        {
            "schema_version": 1,
            "as_of": stamped,
            "currency": "USD",
            "balance": 10000,
            "equity": 10050,
            "margin_used": 200,
            "margin_available": 9800,
        },
        {
            "schema_version": 1,
            "as_of": stamped,
            "positions": [
                {
                    "instrument": "EUR/USD",
                    "side": "buy",
                    "size": 1000,
                    "average_price": 1.1,
                }
            ],
        },
    )


def test_named_protection_and_unclassified_trigger_do_not_guess_roles():
    moment = utc("2026-10-05T09:00:00Z")
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=lambda: moment)
    broker.read = Mock(
        side_effect=[
            {
                "CurrencyIsoCode": "USD",
                "Cash": 10000,
                "NetEquity": 10050,
                "Margin": 200,
                "TradableFunds": 9800,
            },
            {
                "OpenPositions": [
                    position_row(
                        StopOrder={"TriggerPrice": 1.05},
                        LimitOrder=None,
                    )
                ]
            },
            {
                "ActiveStopLimitOrders": [
                    order_row(Type=2),
                    order_row(
                        OrderId=89,
                        TriggerPrice=1.2,
                        StopOrder=None,
                        LimitOrder={"TriggerPrice": 1.25, "OrderId": 90},
                    ),
                ]
            },
        ]
    )
    account, positions = broker.snapshots()
    assert positions["positions"][0]["average_price"] == 1.1
    assert "stop" not in positions["positions"][0]
    assert broker.protections == [{"stop": 1.05, "limit": None, "protection": "known"}]
    page = broker.working_orders()
    assert page["partial"] is False
    assert page["orders"][0]["role"] == "unclassified"
    assert page["orders"][0]["stop"] is None and page["orders"][0]["trigger"] == 1.05
    assert page["orders"][0]["status"] == "working"
    assert page["orders"][1]["role"] == "if_done"
    assert page["orders"][1]["limit"] == 1.25 and page["orders"][1]["stop"] is None
    assert account["equity"] == 10050


def test_bad_protection_does_not_drop_the_position_or_invent_a_stop():
    moment = utc("2026-10-05T09:00:00Z")
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=lambda: moment)
    broker.read = Mock(
        side_effect=[
            {"CurrencyIsoCode": "USD", "Cash": 1, "NetEquity": 1, "Margin": 0, "TradableFunds": 1},
            {"OpenPositions": [position_row(StopOrder={"TriggerPrice": "not-a-price"})]},
        ]
    )
    _account, positions = broker.snapshots()
    assert positions["positions"][0]["instrument"] == "EUR/USD"
    assert broker.protections[0]["protection"] == "unavailable"
    assert broker.protections[0]["stop"] is None


def test_unknown_working_order_fails_the_list_instead_of_disappearing():
    try:
        normalize_working_orders(
            [order_row(), order_row(OrderId=90, MarketId=999)],
            synthetic_symbols(),
            12,
        )
    except Exception as error:
        assert str(error) == "unknown_broker_market"
    else:
        raise AssertionError("unknown order was dropped")


def test_book_hides_stale_mark_pnl_and_refuses_dispatch():
    contracts = Contracts()
    moment = utc("2026-10-05T09:00:00Z")
    account, positions = snapshots(moment)
    fresh = {
        "instrument": "EUR/USD",
        "bid": 1.1,
        "ask": 1.1002,
        "venue_ts": stamp(moment),
        "stale": False,
        "source": "stream",
    }
    book = project_book(
        contracts,
        checked_at=stamp(moment),
        account=account,
        positions=positions,
        protections=[{"stop": 1.05, "limit": None, "protection": "known"}],
        orders_page={"orders": [], "partial": False},
        orders_error=None,
        orders_as_of=stamp(moment),
        marks=[fresh],
        snapshot_as_of=stamp(moment),
        snapshot_error=None,
        freshness_seconds=120,
    )
    assert book["availability"] == "fresh" and book["stale"] is False
    assert book["dispatch_enabled"] is False
    assert book["annotations"][0]["quote_pnl"] == 0.1
    assert book["annotations"][0]["stop"] == 1.05
    assert book["annotations"][0]["limit"] is None
    stale_mark = {**fresh, "stale": True}
    stale = project_book(
        contracts,
        checked_at=stamp(moment),
        account=account,
        positions=positions,
        protections=[{"stop": None, "limit": None, "protection": "known"}],
        orders_page={"orders": [], "partial": False},
        orders_error=None,
        orders_as_of=stamp(moment),
        marks=[stale_mark],
        snapshot_as_of=stamp(moment),
        snapshot_error=None,
        freshness_seconds=120,
    )
    assert stale["annotations"][0]["quote_pnl"] is None
    assert stale["annotations"][0]["mark_stale"] is True
    assert stale["marks"][0]["stale"] is True
    aged = project_book(
        contracts,
        checked_at=stamp(moment + timedelta(seconds=121)),
        account=account,
        positions=positions,
        protections=None,
        orders_page={"orders": [], "partial": False},
        orders_error=None,
        orders_as_of=stamp(moment),
        marks=[fresh],
        snapshot_as_of=stamp(moment),
        snapshot_error=None,
        freshness_seconds=120,
    )
    assert aged["availability"] == "stale" and aged["stale"] is True
    assert aged["positions"]["positions"][0]["size"] == 1000
    assert empty_book(stamp(moment))["dispatch_enabled"] is False


def test_books_http_is_authenticated_get_and_absent_from_the_public_app():
    moment = clock()
    account, positions = snapshots(moment.now())
    broker = SimpleNamespace(
        symbols=synthetic_symbols(),
        generation=1,
        snapshots=Mock(return_value=(account, positions)),
        protections=[{"stop": 1.05, "limit": 1.2, "protection": "known"}],
        working_orders=Mock(
            return_value={
                "orders": [
                    {
                        "instrument": "EUR/USD",
                        "side": "sell",
                        "size": 1000,
                        "status": "working",
                        "broker_order_id": "88",
                        "ts": stamp(moment.now()),
                        "stop": None,
                        "limit": None,
                        "trigger": 1.05,
                        "role": "unclassified",
                    }
                ],
                "partial": False,
            }
        ),
    )
    stream = SimpleNamespace(generation=1, start=Mock(), stop=Mock(), drain=lambda: [quote(moment)])
    worker = ReadWorker(broker, stream=stream, now=moment.now, snapshot_interval=60)
    worker.cycle()
    worker.stop_event.set()
    token = "s" * 32
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(None, token, worker=worker)) as client:
        assert client.get("/books").status_code == 401
        assert client.post("/books", headers=headers).status_code == 405
        body = client.get("/books", headers=headers).json()
        assert body["dispatch_enabled"] is False
        assert body["availability"] == "fresh"
        assert body["orders_state"] == "fresh"
        assert body["working_orders"][0]["role"] == "unclassified"
        assert body["annotations"][0]["quote_pnl"] == 0.1
        assert "client_order_id" not in body["working_orders"][0]
        moment.advance(moment.now() + timedelta(seconds=121))
        aged = client.get("/books", headers=headers).json()
        assert aged["availability"] == "stale" and aged["stale"] is True
        assert aged["account"]["balance"] == 10000
    from app.main import app

    with TestClient(app) as public:
        assert public.get("/books").status_code == 404
        assert public.post("/books").status_code == 404
