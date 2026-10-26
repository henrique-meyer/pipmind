"""Read-only account book. No order submission, modification, or cancellation."""

from decimal import Decimal, localcontext

from pipmind.contracts import ContractError, stamp, utc
from pipmind.gateway.ciapi import broker_time, fields, number
from pipmind.gateway.config import GatewayError

ORDER_CAP = 200
ORDER_PARSE_CAP = 2000
# Documented ApiOpenPositionDTO / active-order status codes. Type integers are a
# different lookup and are not classified here.
ORDER_STATUS = {
    1: "pending",
    2: "accepted",
    3: "working",
    4: "cancelled",
    5: "rejected",
    6: "suspended",
    11: "triggered",
}


def empty_book(checked_at):
    return {
        "schema_version": 1,
        "checked_at": checked_at,
        "availability": "unavailable",
        "orders_state": "unavailable",
        "stale": False,
        "age_seconds": None,
        "orders_age_seconds": None,
        "account": None,
        "positions": None,
        "annotations": [],
        "working_orders": [],
        "orders_partial": False,
        "orders_as_of": None,
        "orders_scope": "unavailable",
        "marks": [],
        "dispatch_enabled": False,
    }


def _child_trigger(value, present):
    """Named StopOrder/LimitOrder only. Missing and null stay distinct from a bad shape."""
    if not present:
        return None, "absent"
    if value is None:
        return None, "none"
    if not isinstance(value, dict):
        return None, "bad"
    try:
        row = fields(value)
        if "TriggerPrice" not in row or row["TriggerPrice"] is None:
            return None, "bad"
        return float(number(row["TriggerPrice"], positive=True)), "known"
    except (GatewayError, TypeError, ValueError, AttributeError):
        return None, "bad"


def protection_from_row(row):
    """Position stop/limit from named fields. Never guess a role from an integer type code."""
    if not isinstance(row, dict):
        return {"stop": None, "limit": None, "protection": "unavailable"}
    stop, stop_state = _child_trigger(row.get("StopOrder"), "StopOrder" in row)
    limit, limit_state = _child_trigger(row.get("LimitOrder"), "LimitOrder" in row)
    if "bad" in {stop_state, limit_state} or "absent" in {stop_state, limit_state}:
        return {"stop": None, "limit": None, "protection": "unavailable"}
    return {"stop": stop, "limit": limit, "protection": "known"}


def _order_time(row):
    for key in ("LastChangedDateTimeUTC", "CreatedDateTimeUTC"):
        if row.get(key):
            return stamp(broker_time(row[key]))
    raise GatewayError("incomplete_broker_orders")


def _core_size(raw, item):
    size = float(number(raw, positive=True) / item.quantity_factor)
    if not size > 0:
        raise GatewayError("incomplete_broker_orders")
    return size


def normalize_working_orders(rows, symbols, trading_account_id):
    """Project an active stop/limit list. A bad row fails the list instead of disappearing."""
    if not isinstance(rows, list) or len(rows) > ORDER_PARSE_CAP:
        raise GatewayError(
            "broker_orders_bound" if isinstance(rows, list) else "incomplete_broker_orders"
        )
    projected, identifiers = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise GatewayError("incomplete_broker_orders")
        row = fields(raw)
        try:
            if row.get("TradingAccountId") != trading_account_id:
                raise GatewayError("broker_account_mismatch")
            if type(row.get("OrderId")) is not int or row["OrderId"] <= 0:
                raise GatewayError("incomplete_broker_orders")
            if row["OrderId"] in identifiers:
                raise GatewayError("incomplete_broker_orders")
            identifiers.add(row["OrderId"])
            item = symbols.market(row.get("MarketId"))
            side = row.get("Direction")
            side = side.lower() if isinstance(side, str) else ""
            if side not in {"buy", "sell"}:
                raise GatewayError("incomplete_broker_orders")
            status_code = row.get("Status")
            if type(status_code) is not int:
                raise GatewayError("incomplete_broker_orders")
            stop = _child_trigger(row.get("StopOrder"), "StopOrder" in row)
            limit = _child_trigger(row.get("LimitOrder"), "LimitOrder" in row)
            if stop[1] == "bad" or limit[1] == "bad":
                raise GatewayError("incomplete_broker_orders")
            trigger = float(number(row.get("TriggerPrice"), positive=True))
            projected.append(
                {
                    "instrument": item.instrument,
                    "side": side,
                    "size": _core_size(row.get("Quantity"), item),
                    "status": ORDER_STATUS.get(status_code, "unknown"),
                    "broker_order_id": str(row["OrderId"]),
                    "ts": _order_time(row),
                    "stop": stop[0],
                    "limit": limit[0],
                    "trigger": trigger,
                    "role": "if_done"
                    if stop[0] is not None or limit[0] is not None
                    else "unclassified",
                }
            )
        except (KeyError, TypeError, AttributeError):
            raise GatewayError("incomplete_broker_orders") from None
    projected.sort(key=lambda item: int(item["broker_order_id"]))
    partial = len(projected) > ORDER_CAP
    return {"orders": projected[:ORDER_CAP], "partial": partial}


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


def _quote_difference(position, mark):
    """Quote-currency distance from entry. Hidden when the mark is missing or stale."""
    if (
        mark is None
        or mark.get("stale")
        or position.get("average_price") is None
        or position.get("side") not in {"buy", "sell"}
        or not position.get("size")
    ):
        return None
    try:
        if mark["ask"] < mark["bid"]:
            return None
        with localcontext() as context:
            context.prec = 128
            context.clear_flags()
            direction = Decimal(1) if position["side"] == "buy" else Decimal(-1)
            mid = (Decimal(str(mark["bid"])) + Decimal(str(mark["ask"]))) / Decimal(2)
            value = (mid - Decimal(str(position["average_price"]))) * Decimal(str(position["size"]))
            result = float((value * direction).quantize(Decimal("0.00000001")))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if result != result or abs(result) == float("inf"):
        return None
    return result


def _clean_mark(raw):
    try:
        utc(raw["venue_ts"])
        if raw["ask"] < raw["bid"] or raw["bid"] <= 0 or raw["source"] not in {"stream", "rest"}:
            return None
        if not isinstance(raw["stale"], bool):
            return None
        return {
            "instrument": raw["instrument"],
            "bid": raw["bid"],
            "ask": raw["ask"],
            "venue_ts": raw["venue_ts"],
            "stale": raw["stale"],
            "source": raw["source"],
        }
    except (ContractError, KeyError, TypeError):
        return None


def _annotation(position, protection, mark):
    known = (
        isinstance(protection, dict)
        and protection.get("protection") == "known"
        and (protection.get("stop") is None or isinstance(protection.get("stop"), (int, float)))
        and (protection.get("limit") is None or isinstance(protection.get("limit"), (int, float)))
    )
    return {
        "stop": protection.get("stop") if known else None,
        "limit": protection.get("limit") if known else None,
        "protection": "known" if known else "unavailable",
        "quote_pnl": _quote_difference(position, mark),
        "mark_stale": None if mark is None else bool(mark["stale"]),
    }


def project_book(
    contracts,
    *,
    checked_at,
    account,
    positions,
    protections,
    orders_page,
    orders_error,
    orders_as_of,
    marks,
    snapshot_as_of,
    snapshot_error,
    freshness_seconds,
):
    """Compose retained reads. Stale rows stay flagged and are never relabeled fresh."""
    age = _age(checked_at, snapshot_as_of) if account and positions else None
    if not account or not positions:
        availability = "unavailable"
        account = positions = None
        annotations = []
        usable_marks = []
    else:
        availability = (
            "stale" if snapshot_error or age is None or age > freshness_seconds else "fresh"
        )
        rows = positions.get("positions") if isinstance(positions, dict) else None
        if not isinstance(rows, list) or len(rows) > 500:
            return _validated(contracts, empty_book(checked_at))
        mark_by_instrument = {}
        usable_marks = []
        for raw in marks or []:
            mark = _clean_mark(raw)
            if mark and mark["instrument"] not in mark_by_instrument:
                mark_by_instrument[mark["instrument"]] = mark
        protections = protections if isinstance(protections, list) else None
        aligned = protections if protections is not None and len(protections) == len(rows) else None
        annotations = []
        for index, position in enumerate(rows):
            row = position if isinstance(position, dict) else {}
            mark = mark_by_instrument.get(row.get("instrument"))
            if mark and mark not in usable_marks:
                usable_marks.append(mark)
            protection = aligned[index] if aligned else None
            annotations.append(_annotation(row, protection, mark))
    orders_age = _age(checked_at, orders_as_of)
    if not isinstance(orders_page, dict) or not isinstance(orders_page.get("orders"), list):
        orders_state = "unavailable"
        working_orders, orders_partial, orders_as_of = [], False, None
        orders_scope = "unavailable"
    else:
        working_orders = orders_page["orders"]
        orders_partial = bool(orders_page.get("partial"))
        orders_scope = "broker_active_list"
        orders_fresh = (
            not orders_error and orders_age is not None and orders_age <= freshness_seconds
        )
        orders_state = "fresh" if orders_fresh else "stale"
    document = {
        "schema_version": 1,
        "checked_at": checked_at,
        "availability": availability,
        "orders_state": orders_state,
        "stale": availability == "stale" or orders_state == "stale",
        "age_seconds": age,
        "orders_age_seconds": orders_age if orders_state != "unavailable" else None,
        "account": account,
        "positions": positions,
        "annotations": annotations,
        "working_orders": working_orders if orders_state != "unavailable" else [],
        "orders_partial": orders_partial if orders_state != "unavailable" else False,
        "orders_as_of": orders_as_of if orders_state != "unavailable" else None,
        "orders_scope": orders_scope if orders_state != "unavailable" else "unavailable",
        "marks": usable_marks,
        "dispatch_enabled": False,
    }
    return _validated(contracts, document)


def _validated(contracts, document):
    try:
        return contracts.validate("account-book", document)
    except ContractError:
        fallback = empty_book(document.get("checked_at"))
        return contracts.validate("account-book", fallback)
