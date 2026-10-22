"""Exact supplied GET /order/openpositions bytes; no I/O or source attestation.

The trusted caller must retain the actual endpoint/scope bytes and local request
and receipt times. A matching digest proves content identity, not authenticity,
complete scope, demo identity, venue time or permission to trade.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException

from pipmind.gateway.ciapi import MAX_BODY, fields
from pipmind.gateway.config import GatewayError
from pipmind.reconciliation import Freshness

MAX_JSON_DEPTH = 128
MAX_NUMBER_CHARS = 4300
MAX_EXPONENT_DIGITS = 18


def _utc_copy(value, code):
    try:
        if not (
            type(value) is datetime
            and value.tzinfo is not None
            and value.utcoffset() == timedelta(0)
        ):
            raise ValueError
        # The zero offset was checked once. Replacing tzinfo retains the exact
        # UTC wall time without invoking a caller-supplied timezone again.
        return value.replace(tzinfo=UTC)
    except Exception:
        raise GatewayError(code) from None


@dataclass(frozen=True, slots=True, repr=False)
class RawPositionRead:
    """Caller-supplied exact bytes for fixed GET /order/openpositions only."""

    body: bytes = field(repr=False)
    sha256: str
    trading_account_id: int
    requested_at: datetime
    received_at: datetime

    def __post_init__(self):
        requested, received = _validate_capture(self)
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "received_at", received)

    def __repr__(self):
        return "RawPositionRead(<private>)"


def _validate_capture(capture, *, canonical=False):
    if (
        type(capture) is not RawPositionRead
        or type(capture.body) is not bytes
        or not 0 < len(capture.body) <= MAX_BODY
        or type(capture.sha256) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", capture.sha256)
        or hashlib.sha256(capture.body).hexdigest() != capture.sha256
        or type(capture.trading_account_id) is not int
        or capture.trading_account_id <= 0
    ):
        raise GatewayError("broker_capture_invalid")
    requested = _utc_copy(capture.requested_at, "broker_capture_invalid")
    received = _utc_copy(capture.received_at, "broker_capture_invalid")
    if requested > received or (
        canonical
        and (capture.requested_at.tzinfo is not UTC or capture.received_at.tzinfo is not UTC)
    ):
        # Every legitimate constructor normalizes these fields. Reject unsafe
        # external replacement so later projection consumers can rely on UTC.
        raise GatewayError("broker_capture_invalid")
    return requested, received


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def _decimal(text):
    if len(text) > MAX_NUMBER_CHARS:
        raise ValueError
    parts = text.lower().split("e")
    if len(parts) == 2 and len(parts[1].lstrip("+-")) > MAX_EXPONENT_DIGITS:
        raise ValueError
    return Decimal(text)


def _integer(text):
    if len(text) > MAX_NUMBER_CHARS:
        raise ValueError
    return int(text)


def _json(body):
    try:
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_float=_decimal,
            parse_int=_integer,
            parse_constant=_constant,
        )
        # Escaped lone surrogates are accepted by json.loads but are not valid
        # Unicode source data. Iterate rather than recurse through parsed data.
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > MAX_JSON_DEPTH:
                raise ValueError
            if type(item) is dict:
                for key, child in item.items():
                    key.encode("utf-8", errors="strict")
                    pending.append((child, depth + 1))
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
            elif type(item) is str:
                item.encode("utf-8", errors="strict")
            elif type(item) is Decimal:
                if not item.is_finite():
                    raise ValueError
            elif type(item) not in (int, bool, type(None)):
                raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError, DecimalException, OverflowError):
        raise GatewayError("broker_capture_json") from None


def decode_positions_read(capture, *, observed_at, freshness):
    """Decode a fresh private copy, preserving raw keys and decimal lexemes.

    Request-start is conservative scope time, never venue/event time. Freshness
    checks do not establish complete scope or authenticate the supplied bytes.
    """
    requested, received = _validate_capture(capture, canonical=True)
    observed = _utc_copy(observed_at, "broker_capture_window")
    if (
        type(freshness) is not Freshness
        or type(freshness.max_age) is not timedelta
        or type(freshness.max_snapshot_skew) is not timedelta
        or freshness.max_age < timedelta(0)
        or freshness.max_snapshot_skew < timedelta(0)
        or observed < received
        or received - requested > freshness.max_snapshot_skew
    ):
        raise GatewayError("broker_capture_window")
    if observed - requested > freshness.max_age:
        raise GatewayError("broker_capture_stale")
    value = _json(capture.body)
    try:
        wrapper = fields(value)
        if (
            not set(wrapper) <= {"OpenPositions", "SupplementalOpenOrders"}
            or "OpenPositions" not in wrapper
            or type(wrapper["OpenPositions"]) is not list
            or any(type(row) is not dict for row in wrapper["OpenPositions"])
            or ("SupplementalOpenOrders" in wrapper and wrapper["SupplementalOpenOrders"] != [])
        ):
            raise ValueError
    except (GatewayError, ValueError):
        raise GatewayError("broker_capture_shape") from None
    return value
