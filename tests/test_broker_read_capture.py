"""Independent exact-byte/window counterexamples, with no broker transport."""

import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, localcontext

import pytest

from pipmind.broker_read_capture import (
    MAX_NUMBER_CHARS,
    RawPositionRead,
    decode_positions_read,
)
from pipmind.gateway.ciapi import MAX_BODY
from pipmind.gateway.config import GatewayError
from pipmind.reconciliation import Freshness

NOW = datetime(2026, 10, 22, 5, tzinfo=UTC)
WINDOW = Freshness(timedelta(seconds=30), timedelta(seconds=5))


def capture(body=b'{"OpenPositions":[]}', **changes):
    values = {
        "body": body,
        "sha256": hashlib.sha256(body).hexdigest() if type(body) is bytes else "0" * 64,
        "trading_account_id": 12,
        "requested_at": NOW,
        "received_at": NOW + timedelta(seconds=1),
    }
    return RawPositionRead(**(values | changes))


def decode(value, *, observed_at=NOW + timedelta(seconds=2), freshness=WINDOW):
    return decode_positions_read(value, observed_at=observed_at, freshness=freshness)


def test_exact_bytes_aliases_and_decimal_lexemes_survive_without_float_conversion():
    body = (
        b'{"openPositions":[{"quantity":0.1000000000000000000000000001,'
        b'"price":1.2300,"signedZero":-0.00,"exponent":1E+28,'
        b'"orderId":9007199254740993}],"supplementalOpenOrders":[]}'
    )
    source = capture(body)
    with localcontext() as context:
        context.prec = 2
        before = context.copy()
        result = decode(source)
        assert context.prec == before.prec
        assert context.flags == before.flags
    assert set(result) == {"openPositions", "supplementalOpenOrders"}
    row = result["openPositions"][0]
    assert row["quantity"] == Decimal("0.1000000000000000000000000001")
    assert str(row["price"]) == "1.2300"
    assert str(row["signedZero"]) == "-0.00"
    assert str(row["exponent"]) == "1E+28"
    assert type(row["orderId"]) is int and row["orderId"] == 9007199254740993
    assert source.body is body
    assert source.sha256 == hashlib.sha256(body).hexdigest()


def test_reads_are_isolated_copies_and_capture_is_frozen_and_redacted():
    body = b'{"OpenPositions":[{"private":"PRIVATE-SOURCE","nested":[{"x":1}]}]}'
    source = capture(body)
    first = decode(source)
    first["OpenPositions"][0]["nested"][0]["x"] = 999
    first["OpenPositions"].clear()
    assert decode(source)["OpenPositions"][0]["nested"] == [{"x": 1}]
    assert source.body == body
    assert repr(source) == "RawPositionRead(<private>)"
    with pytest.raises(FrozenInstanceError):
        source.body = b"changed"


@pytest.mark.parametrize(
    "changes",
    [
        {"body": b""},
        {"body": bytearray(b'{"OpenPositions":[]}')},
        {"body": "PRIVATE-SOURCE"},
        {"sha256": "A" * 64},
        {"sha256": "0" * 64},
        {"sha256": None},
        {"trading_account_id": True},
        {"trading_account_id": 0},
        {"trading_account_id": -1},
        {"trading_account_id": Decimal("12")},
        {"requested_at": NOW.replace(tzinfo=None)},
        {"received_at": NOW.replace(tzinfo=None)},
        {"requested_at": NOW.astimezone(timezone(timedelta(hours=1)))},
        {"received_at": NOW - timedelta(microseconds=1)},
        {"requested_at": "PRIVATE-SOURCE"},
    ],
    ids=[
        "empty",
        "mutable-bytes",
        "string",
        "upper-hash",
        "wrong-hash",
        "null-hash",
        "bool-account",
        "zero-account",
        "negative-account",
        "decimal-account",
        "naive-start",
        "naive-receipt",
        "nonutc",
        "reversed",
        "bad-time",
    ],
)
def test_invalid_capture_is_fixed_and_payload_free(changes):
    with pytest.raises(GatewayError, match="^broker_capture_invalid$") as error:
        capture(**changes)
    assert "PRIVATE-SOURCE" not in str(error.value)


def test_existing_transport_byte_bound_is_exact_and_no_truncation_occurs():
    prefix = b'{"OpenPositions":[]}'
    body = prefix + b" " * (MAX_BODY - len(prefix))
    source = capture(body)
    assert len(source.body) == MAX_BODY
    assert decode(source) == {"OpenPositions": []}
    with pytest.raises(GatewayError, match="^broker_capture_invalid$"):
        capture(body + b" ")


@pytest.mark.parametrize(
    "body",
    [
        b'\xff{"OpenPositions":[]}',
        b'\xef\xbb\xbf{"OpenPositions":[]}',
        b'{"OpenPositions":[],"OpenPositions":[]}',
        b'{"OpenPositions":[{"x":1,"x":2}]}',
        b'{"OpenPositions":[{"x":{"a":1,"\\u0061":2}}]}',
        b'{"OpenPositions":[{"x":NaN}]}',
        b'{"OpenPositions":[{"x":Infinity}]}',
        b'{"OpenPositions":[{"x":-Infinity}]}',
        b'{"OpenPositions":[{"x":"\\ud800"}]}',
        b'{"OpenPositions":[{"\\udfff":1}]}',
        b'{"OpenPositions":[]} trailing PRIVATE-SOURCE',
        b'{"OpenPositions":[{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}]}",
    ],
    ids=[
        "utf8",
        "bom",
        "duplicate-top",
        "duplicate-row",
        "escaped-duplicate",
        "nan",
        "infinity",
        "negative-infinity",
        "lone-value",
        "lone-key",
        "trailing",
        "deep-recursion",
    ],
)
def test_malformed_json_is_always_fixed_without_payload_or_chained_cause(body):
    with pytest.raises(GatewayError, match="^broker_capture_json$") as error:
        decode(capture(body))
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    assert "PRIVATE-SOURCE" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b"null",
        b"true",
        b"1",
        b"[]",
        b'"PRIVATE-SOURCE"',
        b"{}",
        b'{"OpenPositions":null}',
        b'{"OpenPositions":{}}',
        b'{"OpenPositions":[null]}',
        b'{"OpenPositions":[[]]}',
        b'{"OpenPositions":[],"Unknown":[]}',
        b'{"OpenPositions":[],"openPositions":[]}',
        b'{"OpenPositions":[],"SupplementalOpenOrders":null}',
        b'{"OpenPositions":[],"SupplementalOpenOrders":[{}]}',
        b'{"OpenPositions":[],"SupplementalOpenOrders":{}}',
    ],
    ids=[
        "null",
        "bool",
        "number",
        "array",
        "string",
        "missing",
        "null-positions",
        "object-positions",
        "null-row",
        "array-row",
        "unknown-top",
        "ambiguous-alias",
        "null-supplement",
        "unknown-book",
        "object-supplement",
    ],
)
def test_unknown_or_unsupported_scope_is_not_filtered_or_made_complete(body):
    with pytest.raises(GatewayError, match="^broker_capture_shape$"):
        decode(capture(body))


def test_exact_window_boundaries_and_zero_duration_are_accepted():
    source = capture(received_at=NOW + timedelta(seconds=5))
    assert decode(source, observed_at=NOW + timedelta(seconds=30)) == {"OpenPositions": []}
    immediate = capture(received_at=NOW)
    assert decode(immediate, observed_at=NOW, freshness=Freshness(timedelta(0), timedelta(0)))


@pytest.mark.parametrize(
    "source,observation,freshness,code",
    [
        (capture(), NOW, WINDOW, "broker_capture_window"),
        (capture(), NOW + timedelta(seconds=30, microseconds=1), WINDOW, "broker_capture_stale"),
        (
            capture(received_at=NOW + timedelta(seconds=5, microseconds=1)),
            NOW + timedelta(seconds=6),
            WINDOW,
            "broker_capture_window",
        ),
        (capture(), NOW.replace(tzinfo=None), WINDOW, "broker_capture_window"),
        (capture(), NOW + timedelta(seconds=2), None, "broker_capture_window"),
    ],
    ids=["before-receipt", "age-bound", "request-skew", "naive-observation", "no-window"],
)
def test_source_time_is_not_replaced_with_a_fresh_read_clock(source, observation, freshness, code):
    with pytest.raises(GatewayError, match=f"^{code}$"):
        decode(source, observed_at=observation, freshness=freshness)


def test_decode_rechecks_digest_and_capture_window_even_after_unsafe_external_mutation():
    source = capture()
    object.__setattr__(source, "body", b'{"OpenPositions":[{}]}')
    with pytest.raises(GatewayError, match="^broker_capture_invalid$"):
        decode(source)
    source = capture()
    object.__setattr__(source, "received_at", NOW - timedelta(seconds=1))
    with pytest.raises(GatewayError, match="^broker_capture_invalid$"):
        decode(source)
    window = replace(WINDOW)
    object.__setattr__(window, "max_age", timedelta(seconds=-1))
    with pytest.raises(GatewayError, match="^broker_capture_window$"):
        decode(capture(), freshness=window)


def test_depth_limit_is_explicit_with_a_genuine_supported_boundary():
    prefix = b'{"OpenPositions":[{"value":'
    body = prefix + b"[" * 125 + b"0" + b"]" * 125 + b"}]}"
    assert decode(capture(body))["OpenPositions"]
    body = prefix + b"[" * 126 + b"0" + b"]" * 126 + b"}]}"
    with pytest.raises(GatewayError, match="^broker_capture_json$"):
        decode(capture(body))


@pytest.mark.parametrize(
    "number",
    [
        b"9" * (MAX_NUMBER_CHARS + 1),
        b"0." + b"1" * MAX_NUMBER_CHARS,
        b"1e" + b"9" * 19,
        b"1e-" + b"0" * 18 + b"1",
    ],
    ids=["long-int", "long-fraction", "long-exponent", "padded-exponent"],
)
def test_numeric_lexemes_have_bounded_structure_before_conversion(number):
    with pytest.raises(GatewayError, match="^broker_capture_json$"):
        decode(capture(b'{"OpenPositions":[{"value":' + number + b"}]}"))


def test_numeric_structure_bound_does_not_round_or_apply_position_semantics():
    number = b"1." + b"2" * (MAX_NUMBER_CHARS - 2)
    value = decode(capture(b'{"OpenPositions":[{"value":' + number + b"}]}"))
    assert str(value["OpenPositions"][0]["value"]).encode() == number
    # Field-specific 28-digit/range validation belongs to the position consumer.
    value = decode(capture(b'{"OpenPositions":[{"value":1e999999999999999999}]}'))
    assert value["OpenPositions"][0]["value"].is_finite()


def test_malformed_timezone_error_cannot_expose_private_exception_text():
    class BrokenTimezone(tzinfo):
        def utcoffset(self, value):
            raise RuntimeError("PRIVATE-SOURCE")

    with pytest.raises(GatewayError, match="^broker_capture_invalid$"):
        capture(requested_at=NOW.replace(tzinfo=BrokenTimezone()))


class ZeroOnceTimezone(tzinfo):
    def __init__(self):
        self.calls = 0

    def utcoffset(self, value):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("PRIVATE-STATEFUL-TIMEZONE")
        return timedelta(0)


def test_distinct_stateful_zero_offset_timezones_are_copied_before_comparison():
    start_zone, receipt_zone = ZeroOnceTimezone(), ZeroOnceTimezone()
    requested = NOW.replace(tzinfo=start_zone)
    received = (NOW + timedelta(seconds=1)).replace(tzinfo=receipt_zone)
    source = capture(requested_at=requested, received_at=received)
    assert source.requested_at.tzinfo is UTC and source.received_at.tzinfo is UTC
    assert source.requested_at == NOW and source.received_at == NOW + timedelta(seconds=1)
    assert requested.tzinfo is start_zone and received.tzinfo is receipt_zone
    assert start_zone.calls == receipt_zone.calls == 1
    assert decode(source) == decode(source) == {"OpenPositions": []}
    assert start_zone.calls == receipt_zone.calls == 1


def test_observation_timezone_is_copied_before_every_window_operation():
    source = capture()
    zone = ZeroOnceTimezone()
    observation = (NOW + timedelta(seconds=2)).replace(tzinfo=zone)
    assert decode(source, observed_at=observation) == {"OpenPositions": []}
    assert zone.calls == 1
    assert observation.tzinfo is zone
    with pytest.raises(GatewayError, match="^broker_capture_window$") as caught:
        decode(source, observed_at=observation)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert "PRIVATE-STATEFUL-TIMEZONE" not in str(caught.value)


def test_unsafe_external_clock_replacement_does_not_reintroduce_timezone_callbacks():
    source = capture()
    zone = ZeroOnceTimezone()
    object.__setattr__(source, "received_at", source.received_at.replace(tzinfo=zone))
    with pytest.raises(GatewayError, match="^broker_capture_invalid$"):
        decode(source)
    assert zone.calls == 1


def test_valid_named_zero_offset_clocks_are_not_arbitrarily_rejected():
    from zoneinfo import ZoneInfo

    source = capture(requested_at=NOW.replace(tzinfo=ZoneInfo("Etc/GMT")))
    assert source.requested_at.tzinfo is UTC and source.requested_at == NOW
    observation = (NOW + timedelta(seconds=2)).replace(tzinfo=timezone(timedelta(0), "named UTC"))
    assert decode(source, observed_at=observation) == {"OpenPositions": []}
