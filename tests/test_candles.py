"""Read-only candle history. Synthetic broker rows only; no venue calls."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from pipmind.contracts import Contracts, stamp, utc
from pipmind.core import SimulatedClock
from pipmind.gateway.bars import (
    NATIVE,
    TIMEFRAMES,
    empty_history,
    fx_market_closed,
    parse_broker_bars,
    project_history,
    remember,
)
from pipmind.gateway.ciapi import Ciapi, Response
from pipmind.gateway.config import GatewayError
from pipmind.gateway.recording import synthetic_config, synthetic_symbols
from pipmind.gateway.service import ReadWorker, create_app


def wcf(moment):
    return f"/Date({int(moment.timestamp() * 1000)})/"


def bar(moment, **prices):
    values = {"Open": 1.1, "High": 1.2, "Low": 1.05, "Close": 1.11, **prices}
    return {"BarDate": wcf(moment), **values}


def test_owner_timeframes_are_native_ciapi_intervals():
    assert set(TIMEFRAMES) == {"15m", "1h", "2h", "4h", "1d"}
    for interval, span, _width in TIMEFRAMES.values():
        assert span in NATIVE[interval]


def test_weekend_window_matches_the_new_york_close():
    assert fx_market_closed(utc("2026-10-09T20:30:00Z")) is False
    assert fx_market_closed(utc("2026-10-09T21:00:00Z")) is True
    assert fx_market_closed(utc("2026-10-10T15:00:00Z")) is True
    assert fx_market_closed(utc("2026-10-11T20:30:00Z")) is True
    assert fx_market_closed(utc("2026-10-11T21:00:00Z")) is False


def test_bad_bar_fails_the_series_and_partial_bar_stays_labeled():
    contracts = Contracts()
    moment = utc("2026-10-05T14:00:00Z")
    try:
        parse_broker_bars(
            {"PriceBars": [bar(moment), bar(moment + timedelta(hours=1), High=1.0)]},
            stamp(moment),
        )
    except GatewayError as error:
        assert str(error) == "incomplete_broker_bars"
    else:
        raise AssertionError("incoherent bar was kept")
    parsed = parse_broker_bars(
        {
            "PriceBars": [bar(moment - timedelta(hours=1))],
            "PartialPriceBar": bar(moment, Close=1.12),
        },
        stamp(moment),
    )
    assert parsed["forming"] is True and len(parsed["bars"]) == 2
    history = project_history(
        contracts,
        {**parsed, "instrument": "EUR/USD", "timeframe": "1h", "fetched_at": moment},
        checked_at=stamp(moment),
        closed=False,
        width=TIMEFRAMES["1h"][2],
        failed=False,
    )
    assert history["availability"] == "fresh" and history["stale"] is False
    assert history["price"] == "mid" and "market_id" not in history
    closed = project_history(
        contracts,
        {**parsed, "instrument": "EUR/USD", "timeframe": "1h", "fetched_at": moment},
        checked_at=stamp(moment),
        closed=True,
        width=TIMEFRAMES["1h"][2],
        failed=False,
    )
    assert closed["availability"] == "closed" and closed["stale"] is True
    assert empty_history(contracts, stamp(moment), "EUR/USD", "1h", False)["bars"] == []


def test_cache_and_budget_do_not_call_the_broker_again():
    moment = utc("2026-10-05T14:00:00Z")
    cache, calls, fetched = {}, [], []

    def fetch():
        fetched.append(1)
        return {
            "bars": [
                {
                    "ts": stamp(moment - timedelta(minutes=5)),
                    "open": 1.1,
                    "high": 1.2,
                    "low": 1.05,
                    "close": 1.11,
                }
            ],
            "history_partial": False,
            "forming": False,
        }

    key = ("EUR/USD", "15m")
    first, failed = remember(cache, calls, key=key, now=moment, closed=False, fetch=fetch)
    assert failed is False and fetched == [1] and first["bars"]
    remember(cache, calls, key=key, now=moment + timedelta(seconds=30), closed=False, fetch=fetch)
    assert fetched == [1]
    calls[:] = [moment + timedelta(seconds=61)] * 6
    cached, blocked = remember(
        cache,
        calls,
        key=key,
        now=moment + timedelta(seconds=61),
        closed=False,
        fetch=fetch,
    )
    assert blocked is True and cached["bars"] and fetched == [1]
    missing, blocked = remember(
        {},
        [moment] * 6,
        key=("GBP/JPY", "1d"),
        now=moment,
        closed=False,
        fetch=fetch,
    )
    assert missing is None and blocked is True and fetched == [1]


def test_bar_history_get_is_allowlisted_and_uses_unix_seconds():
    transport = Mock()
    transport.request.side_effect = [
        Response(200, {"Session": "x" * 36}),
        Response(200, {"PriceBars": []}),
    ]
    broker = Ciapi(
        synthetic_config(),
        synthetic_symbols(),
        transport=transport,
        now=lambda: utc("2026-10-05T14:00:00Z"),
    )
    moment = 1_700_000_000
    assert broker.bar_history(broker.symbols.symbol("EUR/USD"), "MINUTE", 15, moment, 240) == {
        "PriceBars": []
    }
    get = [call for call in transport.request.call_args_list if call.args[0] == "GET"]
    assert len(get) == 1
    assert get[0].args[1].endswith("/market/42/barhistorybefore")
    assert get[0].kwargs["query"] == {
        "interval": "MINUTE",
        "span": 15,
        "toTimestampUTC": moment,
        "maxResults": 240,
        "priceType": "MID",
    }
    assert all(
        call.args[0] == "GET" or call.args[1].endswith("/v2/Session")
        for call in transport.request.call_args_list
    )


def test_instruments_and_bars_routes_are_read_only():
    moment = SimulatedClock(utc("2026-10-05T14:00:00Z"))
    broker = Ciapi(synthetic_config(), synthetic_symbols(), now=moment.now)
    broker.bar_history = Mock(
        return_value={
            "PriceBars": [bar(moment.now() - timedelta(minutes=10))],
            "PartialPriceBar": None,
        }
    )
    stream = SimpleNamespace(generation=1, start=Mock(), stop=Mock(), drain=lambda: [])
    worker = ReadWorker(broker, stream=stream, now=moment.now, snapshot_interval=60)
    worker.stop_event.set()
    token = "s" * 32
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(None, token, worker=worker)) as client:
        assert client.get("/instruments").status_code == 401
        assert client.get("/bars").status_code == 401
        listed = client.get("/instruments", headers=headers).json()
        assert listed == {"schema_version": 1, "instruments": ["EUR/USD"]}
        assert "42" not in str(listed) and "market" not in listed
        assert (
            client.get("/bars?instrument=EUR/USD&timeframe=3h", headers=headers).status_code == 404
        )
        assert (
            client.get(
                "/bars?instrument=EUR/USD&timeframe=1h&priceType=ASK", headers=headers
            ).status_code
            == 404
        )
        assert broker.bar_history.call_count == 0
        body = client.get("/bars?instrument=EUR/USD&timeframe=15m", headers=headers).json()
        assert body["availability"] == "fresh" and body["stale"] is False
        assert body["price"] == "mid" and body["bars"][0]["close"] == 1.11
        assert client.post("/bars", headers=headers).status_code == 405
        client.get("/bars?instrument=EUR/USD&timeframe=15m", headers=headers)
        assert broker.bar_history.call_count == 1
        broker.bar_history.side_effect = GatewayError("incomplete_broker_bars")
        moment.advance(moment.now() + timedelta(seconds=61))
        retained = client.get("/bars?instrument=EUR/USD&timeframe=15m", headers=headers).json()
        assert retained["availability"] == "stale" and retained["stale"] is True
        assert retained["bars"][0]["close"] == 1.11
