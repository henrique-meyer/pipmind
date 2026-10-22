"""Provider-shaped partial projections only; no broker or risk-admission claim."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from pipmind.broker_positions import GAPS, project_position_read
from pipmind.broker_read_capture import RawPositionRead
from pipmind.contracts import Contracts
from pipmind.gateway.config import GatewayError, SymbolMap
from pipmind.reconciliation import Binding, Freshness

NOW = datetime(2026, 10, 22, 5, tzinfo=UTC)
BINDING = Binding("synthetic-bound-account", "demo")
FRESH = Freshness(timedelta(seconds=30), timedelta(seconds=2))


def symbols(factor="1000"):
    return SymbolMap(
        [
            {
                "instrument": "EUR/USD",
                "market_id": 42,
                "unit": "explicit-synthetic-units",
                "quantity_factor": factor,
                "increment": "1",
                "minimum": "1",
            }
        ]
    )


def row(**changes):
    return {
        "OrderId": 41,
        "TradingAccountId": 12,
        "MarketId": 42,
        "Direction": "Buy",
        "Quantity": 1250,
        "Price": 1,
        **changes,
    }


def capture(rows=(), *, raw=None, wrapper="OpenPositions"):
    body = raw if raw is not None else json.dumps({wrapper: list(rows)}).encode()
    return RawPositionRead(
        body, hashlib.sha256(body).hexdigest(), 12, NOW, NOW + timedelta(seconds=1)
    )


def project(value, *, mapping=None):
    return project_position_read(
        value,
        binding=BINDING,
        symbols=symbols() if mapping is None else mapping,
        observed_at=NOW + timedelta(seconds=2),
        freshness=FRESH,
    )


def test_flat_and_opposing_positions_project_exactly_without_netted_identity():
    assert project(capture()).positions == ()
    original = capture([row(), row(OrderId=42, Direction="sell", Quantity=2500)])
    result = project(original)
    assert [(r.position_id, r.side, r.size) for r in result.positions] == [
        ("41", "buy", Decimal("1.25")),
        ("42", "sell", Decimal("2.5")),
    ]
    assert all(type(r.size) is Decimal for r in result.positions)
    assert result.gaps == GAPS and result.dispatch_authorized is False
    assert result.source_sha256 == hashlib.sha256(original.body).hexdigest()
    assert project(original) == result


def test_snapshot_payload_is_fresh_and_conforms_without_fabricating_missing_facts():
    result = project(capture([row()]))
    left, right = result.snapshot_facts(), result.snapshot_facts()
    Contracts().validate("position-snapshot", left.positions)
    assert left.account is None and left.binding == BINDING and left.position_ids == ("41",)
    for field in (
        "account_complete",
        "positions_complete",
        "order_updates",
        "orders_as_of",
        "orders_complete",
        "protective_orders",
        "protections_as_of",
        "protections_complete",
    ):
        assert getattr(left, field) is None
    left.positions["positions"][0]["size"] = Decimal(999)
    left.positions["as_of"] = "private mutated time"
    assert right.positions["positions"][0]["size"] == Decimal("1.25")
    assert result.snapshot_facts() == right
    with pytest.raises(FrozenInstanceError):
        result.positions = ()
    with pytest.raises(FrozenInstanceError):
        result.positions[0].position_id = "changed"
    assert "synthetic-bound-account" not in repr(result) and "1.25" not in repr(result.positions[0])


def test_aliases_retain_actual_source_path_and_optional_opening_price():
    lower = {key[0].lower() + key[1:]: value for key, value in row().items()}
    lower.pop("price")
    result = project(capture([lower], wrapper="openPositions"))
    assert result.positions[0].source_path == "$.openPositions[0]"
    assert result.positions[0].average_price is None
    assert result.positions[0].venue_time is None


def test_raw_decimal_lexeme_survives_without_float_or_ambient_context_dependence():
    raw = b'{"OpenPositions":[{"OrderId":41,"TradingAccountId":12,"MarketId":42,'
    raw += b'"Direction":"Buy","Quantity":0.1000000000000000000000000001,'
    raw += b'"Price":1.123456789012345678901234567}]}'
    original, mapping = capture(raw=raw), symbols("1")
    expected = project(original, mapping=mapping)
    assert expected.positions[0].size == Decimal("0.1000000000000000000000000001")
    assert expected.positions[0].average_price == Decimal("1.123456789012345678901234567")
    with localcontext() as context:
        context.prec, context.Emax, context.Emin, context.capitals = 2, 2, -2, 0
        context.traps[Inexact] = context.traps[Rounded] = True
        assert project(original, mapping=mapping) == expected
        assert (
            project(capture([row(Quantity=1000000)]), mapping=symbols("8")).positions[0].size
            == 125000
        )
    assert original.body == raw


@pytest.mark.parametrize("factor", ["3", "1237940039285380274899124224"])
def test_nonterminating_or_domain_unrepresentable_conversion_refuses_without_rounding(factor):
    with pytest.raises(GatewayError, match="^broker_position_(units|number)$"):
        project(capture([row(Quantity=1)]), mapping=symbols(factor))


@pytest.mark.parametrize(
    "key,value",
    [
        ("OrderId", True),
        ("OrderId", 0),
        ("TradingAccountId", "12"),
        ("MarketId", 42.0),
        ("Quantity", "1250"),
        ("Quantity", False),
        ("Quantity", 0),
        ("Price", "1.1"),
        ("Price", -1),
        ("Direction", "flat"),
        ("Direction", 1),
        ("Status", 2),
        ("Status", True),
        ("AutoRollover", 0),
        ("Currency", 11),
        ("FixedInitialMargin", -1),
    ],
)
def test_contradictory_identity_number_side_status_or_metadata_refuses(key, value):
    with pytest.raises(GatewayError):
        project(capture([row(**{key: value})]))


def test_foreign_account_unknown_market_duplicates_and_unknown_shape_are_not_omitted():
    for values in (
        [row(TradingAccountId=99)],
        [row(MarketId=99)],
        [row(), row()],
        [row(UnrecognizedExposure={"size": 999})],
    ):
        with pytest.raises(GatewayError):
            project(capture(values))


@pytest.mark.parametrize("field", ["StopOrder", "LimitOrder", "AssociatedOrders"])
@pytest.mark.parametrize("value", [{}, {"OrderId": 88, "Quantity": 1250}, []])
def test_protection_bearing_objects_are_not_guessed_or_silently_discarded(field, value):
    with pytest.raises(GatewayError, match="^broker_position_protection$"):
        project(capture([row(**{field: value})]))


@pytest.mark.parametrize(
    "changes",
    [
        {"ManagedTrades": [{"OrderId": 4}]},
        {"ManagedTrades": {}},
        {"AllocationProfileId": 0},
        {"AllocationProfileName": "profile"},
    ],
)
def test_managed_shapes_remain_explicitly_unsupported(changes):
    with pytest.raises(GatewayError, match="^broker_position_managed$"):
        project(capture([row(**changes)]))


def test_null_and_empty_placeholders_do_not_claim_protection_or_scope_completeness():
    result = project(
        capture(
            [
                row(
                    StopOrder=None,
                    LimitOrder=None,
                    AssociatedOrders=None,
                    ManagedTrades=[],
                    AllocationProfileId=None,
                    AllocationProfileName="",
                    Status=3,
                )
            ]
        )
    )
    assert result.gaps == GAPS and result.snapshot_facts().protective_orders is None


def test_old_unchanged_event_and_missing_event_do_not_refresh_or_invent_venue_time():
    original = capture(
        [
            row(
                LastChangedDateTimeUTC="2020-01-01T00:00:00Z",
                CreatedDateTimeUTC="2019-01-01T00:00:00Z",
                ExecutedDateTimeUTC="2019-02-01T00:00:00Z",
            )
        ]
    )
    result = project(original)
    assert result.positions[0].venue_time == "2020-01-01T00:00:00Z"
    assert result.snapshot_facts().positions["as_of"] == "2026-10-22T05:00:00Z"
    assert (
        project(capture([row(CreatedDateTimeUTC="2019-01-01T00:00:00Z")])).positions[0].venue_time
        is None
    )


@pytest.mark.parametrize(
    "field", ["LastChangedDateTimeUTC", "CreatedDateTimeUTC", "ExecutedDateTimeUTC"]
)
def test_future_or_malformed_event_time_is_not_repaired(field):
    for value in ("2026-10-22T05:00:02Z", "bad-private-provider-time", 123):
        with pytest.raises(GatewayError, match="^broker_position_time$"):
            project(capture([row(**{field: value})]))


def test_alias_collision_hash_corruption_and_invalid_configuration_do_not_project():
    with pytest.raises(GatewayError, match="^ambiguous_broker_fields$"):
        project(capture([row(orderId=41)]))
    with pytest.raises(GatewayError, match="^broker_capture_invalid$"):
        replace(capture(), sha256="0" * 64)
    with pytest.raises(GatewayError, match="^broker_position_configuration$"):
        project_position_read(
            capture(), binding=None, symbols=symbols(), observed_at=NOW, freshness=FRESH
        )
