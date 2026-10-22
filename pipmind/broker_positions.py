"""Exact partial CIAPI position projection; no completeness or dispatch proof.

This is a pure consumer of already retained GET /order/openpositions bytes.
Caller account/environment and symbol mappings remain external assumptions.
Account, order and protection evidence is deliberately not manufactured.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, DecimalException, Inexact, Rounded, localcontext

from pipmind.broker_read_capture import RawPositionRead, decode_positions_read
from pipmind.contracts import stamp
from pipmind.gateway.ciapi import broker_time, fields
from pipmind.gateway.config import GatewayError, Instrument, SymbolMap, number
from pipmind.reconciliation import Binding, SnapshotFacts

GAPS = (
    "account_scope_unknown",
    "positions_scope_unverified",
    "orders_scope_unknown",
    "protection_scope_unknown",
    "environment_unverified",
)
_EVENTS = ("LastChangedDateTimeUTC", "CreatedDateTimeUTC", "ExecutedDateTimeUTC")
_PROTECTION = ("StopOrder", "LimitOrder", "AssociatedOrders")
# Primary ApiOpenPositionDTO: deliberately no arbitrary extension fields or
# managed/net-position interpretation. Unsupported raw facts are never dropped.
_ALLOWED = {
    "OrderId",
    "AutoRollover",
    "MarketId",
    "MarketName",
    "Direction",
    "Quantity",
    "Price",
    "TradingAccountId",
    "Currency",
    "Status",
    "TradeReference",
    "FixedInitialMargin",
    "ManagedTrades",
    "AllocationProfileId",
    "AllocationProfileName",
    *_EVENTS,
    *_PROTECTION,
}


def _need(ok, code):
    if not ok:
        raise GatewayError(code)


@dataclass(frozen=True, slots=True, repr=False)
class ProjectedPosition:
    position_id: str
    instrument: str
    side: str
    size: Decimal
    average_price: Decimal | None
    source_path: str
    venue_time: str | None

    def __repr__(self):
        return "ProjectedPosition(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class PositionProjection:
    capture: RawPositionRead
    binding: Binding
    positions: tuple[ProjectedPosition, ...]
    gaps: tuple[str, ...]

    def __repr__(self):
        return "PositionProjection(<private>)"

    @property
    def source_sha256(self):
        return self.capture.sha256

    @property
    def dispatch_authorized(self):
        return False

    def snapshot_facts(self):
        """Return independent v1 containers, with every unknown scope explicit.

        as_of is the retained local request-start, never a venue event time.
        A projected position ID is the broker opening OrderId within the supplied
        trading-account capture, not an invented local command identifier.
        """
        return SnapshotFacts(
            binding=self.binding,
            account=None,
            positions={
                "schema_version": 1,
                "as_of": stamp(self.capture.requested_at),
                "positions": [
                    {
                        "instrument": row.instrument,
                        "side": row.side,
                        "size": row.size,
                        "average_price": row.average_price,
                    }
                    for row in self.positions
                ],
            },
            position_ids=tuple(row.position_id for row in self.positions),
        )


def _decimal(value, *, positive=False):
    _need(type(value) in (int, Decimal), "broker_position_number")
    try:
        return number(value, positive=positive)
    except GatewayError:
        raise GatewayError("broker_position_number") from None


def _size(quantity, factor):
    try:
        with localcontext() as context:
            context.prec, context.Emax, context.Emin = 128, 999999, -999999
            context.clamp, context.capitals = 0, 1
            context.traps[Inexact] = context.traps[Rounded] = True
            original, divisor = _decimal(quantity, positive=True), _decimal(factor, positive=True)
            result = original / divisor
            _need(result * divisor == original, "broker_position_units")
            return _decimal(result, positive=True)
    except DecimalException:
        raise GatewayError("broker_position_units") from None


def _metadata(row, capture):
    _need(set(row) <= _ALLOWED, "broker_position_shape")
    _need(all(row.get(key) is None for key in _PROTECTION), "broker_position_protection")
    _need(
        row.get("ManagedTrades") in (None, [])
        and row.get("AllocationProfileId") is None
        and row.get("AllocationProfileName") in (None, ""),
        "broker_position_managed",
    )
    if "AutoRollover" in row:
        _need(type(row["AutoRollover"]) is bool, "broker_position_shape")
    for key in ("MarketName", "Currency", "TradeReference"):
        if key in row:
            _need(type(row[key]) is str, "broker_position_shape")
    if "Status" in row:
        # Official OrderStatus codes: Open=3. No pending/accepted/closed row is
        # silently reinterpreted as an open exposure in this narrow profile.
        _need(type(row["Status"]) is int and row["Status"] == 3, "broker_position_status")
    if "FixedInitialMargin" in row:
        _need(_decimal(row["FixedInitialMargin"]) >= 0, "broker_position_number")
    changed = None
    for key in _EVENTS:
        raw = row.get(key)
        if raw is None:
            continue
        _need(type(raw) is str, "broker_position_time")
        try:
            moment = broker_time(raw)
        except (GatewayError, ValueError, TypeError):
            raise GatewayError("broker_position_time") from None
        _need(moment <= capture.received_at, "broker_position_time")
        if key == "LastChangedDateTimeUTC":
            changed = stamp(moment)
    return changed


def project_position_read(capture, *, binding, symbols, observed_at, freshness):
    """Project only exact supported rows; unsupported scope never clears.

    Null protection and empty managed placeholders remain unknown. Any actual
    protective/net/managed object refuses the affected capture instead of being
    guessed, netted or silently discarded. All event times are validated; only
    LastChangedDateTimeUTC is projected as venue_time, with no fallback meaning.
    """
    _need(type(binding) is Binding and type(symbols) is SymbolMap, "broker_position_configuration")
    _need(
        type(symbols.by_market) is dict and type(symbols.by_symbol) is dict,
        "broker_position_configuration",
    )
    body = decode_positions_read(capture, observed_at=observed_at, freshness=freshness)
    wrapper_key = next(key for key in body if key[0].upper() + key[1:] == "OpenPositions")
    projected, identities = [], set()
    for index, raw in enumerate(body[wrapper_key]):
        row = fields(raw)
        changed = _metadata(row, capture)
        for key in ("OrderId", "TradingAccountId", "MarketId"):
            _need(type(row.get(key)) is int and row[key] > 0, "broker_position_identity")
        _need(row["TradingAccountId"] == capture.trading_account_id, "broker_position_account")
        identity = str(row["OrderId"])
        _need(identity not in identities, "broker_position_identity")
        identities.add(identity)
        item = symbols.by_market.get(row["MarketId"])
        _need(
            type(item) is Instrument
            and type(item.instrument) is str
            and re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", item.instrument) is not None
            and type(item.market_id) is int
            and item.market_id == row["MarketId"]
            and symbols.by_symbol.get(item.instrument) is item,
            "broker_position_mapping",
        )
        _need(
            type(row.get("Direction")) is str and row["Direction"].lower() in ("buy", "sell"),
            "broker_position_side",
        )
        price = row.get("Price")
        projected.append(
            ProjectedPosition(
                identity,
                item.instrument,
                row["Direction"].lower(),
                _size(row.get("Quantity"), item.quantity_factor),
                None if price is None else _decimal(price, positive=True),
                f"$.{wrapper_key}[{index}]",
                changed,
            )
        )
    return PositionProjection(capture, binding, tuple(projected), GAPS)
