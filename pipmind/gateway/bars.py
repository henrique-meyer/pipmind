"""Read-only candle history. No order submission, modification, or cancellation."""

import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from pipmind.contracts import ContractError, stamp, utc
from pipmind.gateway.ciapi import broker_time, fields, number
from pipmind.gateway.config import GatewayError

# Gain Capital Parameters.htm: MINUTE spans 1, 2, 3, 5, 10, 15, 30; HOUR spans
# 1, 2, 4, 8; DAY and WEEK require span 1. All five owner timeframes are native,
# so this read does not aggregate. The bar-history page was current on 07 Nov 2025.
NATIVE = {"MINUTE": {1, 2, 3, 5, 10, 15, 30}, "HOUR": {1, 2, 4, 8}, "DAY": {1}}
TIMEFRAMES = {
    "15m": ("MINUTE", 15, 15 * 60),
    "1h": ("HOUR", 1, 60 * 60),
    "2h": ("HOUR", 2, 2 * 60 * 60),
    "4h": ("HOUR", 4, 4 * 60 * 60),
    "1d": ("DAY", 1, 24 * 60 * 60),
}
MAX_BARS = 240
OPEN_TTL = 60
CLOSED_TTL = 900
BUDGET = 6
BUDGET_WINDOW = 60
NEW_YORK = ZoneInfo("America/New_York")

if any(span not in NATIVE[interval] for interval, span, _ in TIMEFRAMES.values()):
    raise GatewayError("invalid_bar_timeframe")


def fx_market_closed(moment):
    """Weekend close used by the existing New York session window. Holidays are not inferred."""
    local = moment.astimezone(NEW_YORK)
    return (
        local.weekday() == 5
        or (local.weekday() == 4 and local.hour >= 17)
        or (local.weekday() == 6 and local.hour < 17)
    )


def parse_bar_query(params):
    items = list(params.multi_items())
    if len(items) != 2 or {key for key, _ in items} != {"instrument", "timeframe"}:
        raise GatewayError("invalid_bar_query")
    values = dict(items)
    if (
        not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", values["instrument"])
        or values["timeframe"] not in TIMEFRAMES
    ):
        raise GatewayError("invalid_bar_query")
    return values["instrument"], values["timeframe"]


def instrument_list(contracts, names):
    return contracts.validate("instrument-list", {"schema_version": 1, "instruments": list(names)})


def empty_history(contracts, checked_at, instrument, timeframe, closed):
    return contracts.validate(
        "candle-history",
        {
            "schema_version": 1,
            "instrument": instrument,
            "timeframe": timeframe,
            "price": "mid",
            "checked_at": checked_at,
            "fetched_at": None,
            "availability": "unavailable",
            "market_state": "closed" if closed else "open",
            "stale": False,
            "age_seconds": None,
            "history_partial": False,
            "forming": False,
            "bars": [],
        },
    )


def _price(value):
    result = float(number(value, positive=True))
    if result != result or abs(result) == float("inf"):
        raise GatewayError("incomplete_broker_bars")
    return result


def _bar(raw, checked_at):
    row = fields(raw)
    opened = broker_time(row.get("BarDate"))
    if opened > utc(checked_at) + timedelta(minutes=5):
        raise GatewayError("incomplete_broker_bars")
    open_, high, low, close = (_price(row.get(key)) for key in ("Open", "High", "Low", "Close"))
    if low > open_ or low > close or high < open_ or high < close or high < low:
        raise GatewayError("incomplete_broker_bars")
    return {
        "ts": stamp(opened),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    }


def parse_broker_bars(raw, checked_at):
    """Project PriceBars plus an optional unfinished PartialPriceBar. A bad bar fails the series."""
    body = fields(raw)
    rows = body.get("PriceBars")
    if not isinstance(rows, list) or len(rows) > 4000:
        raise GatewayError("incomplete_broker_bars")
    history_partial = len(rows) >= MAX_BARS
    bars = [_bar(row, checked_at) for row in rows[:MAX_BARS]]
    bars.sort(key=lambda item: item["ts"])
    if len({item["ts"] for item in bars}) != len(bars):
        raise GatewayError("incomplete_broker_bars")
    forming = False
    partial = body.get("PartialPriceBar")
    if partial is not None:
        if not isinstance(partial, dict):
            raise GatewayError("incomplete_broker_bars")
        extra = _bar(partial, checked_at)
        if bars and extra["ts"] <= bars[-1]["ts"]:
            raise GatewayError("incomplete_broker_bars")
        bars.append(extra)
        forming = True
    return {"bars": bars, "history_partial": history_partial, "forming": forming}


def _age(checked_at, earlier):
    if not earlier:
        return None
    try:
        seconds = (utc(checked_at) - utc(earlier)).total_seconds()
    except ContractError:
        return None
    if seconds < 0:
        return None
    return int(seconds)


def project_history(contracts, cached, *, checked_at, closed, width, failed):
    bars = cached.get("bars") or []
    age = _age(checked_at, bars[-1]["ts"] if bars else None)
    if not bars:
        availability, stale = "unavailable", False
    elif closed:
        availability, stale = "closed", True
    elif failed or age is None or age > width * 2:
        availability, stale = "stale", True
    else:
        availability, stale = "fresh", False
    fetched = cached.get("fetched_at")
    return contracts.validate(
        "candle-history",
        {
            "schema_version": 1,
            "instrument": cached["instrument"],
            "timeframe": cached["timeframe"],
            "price": "mid",
            "checked_at": checked_at,
            "fetched_at": stamp(fetched) if fetched else None,
            "availability": availability,
            "market_state": "closed" if closed else "open",
            "stale": stale,
            "age_seconds": age,
            "history_partial": bool(cached.get("history_partial")),
            "forming": bool(cached.get("forming")),
            "bars": bars,
        },
    )


def remember(cache, calls, *, key, now, closed, fetch):
    """Serve a short cache. A spent budget returns retained bars as stale, or unavailable."""
    ttl = CLOSED_TTL if closed else OPEN_TTL
    cached = cache.get(key)
    if cached and (now - cached["fetched_at"]).total_seconds() < ttl:
        return cached, False
    fresh_calls = [moment for moment in calls if (now - moment).total_seconds() < BUDGET_WINDOW]
    calls[:] = fresh_calls
    if len(calls) >= BUDGET:
        return cached, True
    calls.append(now)
    try:
        parsed = fetch()
    except (ContractError, GatewayError, TypeError, ValueError):
        return cached, True
    stored = {**parsed, "fetched_at": now, "instrument": key[0], "timeframe": key[1]}
    cache[key] = stored
    if len(cache) > 250:
        oldest = min(cache, key=lambda item: cache[item]["fetched_at"])
        cache.pop(oldest, None)
    return stored, False
