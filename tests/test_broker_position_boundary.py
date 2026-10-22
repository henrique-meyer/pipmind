"""Independent source-to-real-reconciliation checks; no broker/admission authority."""

import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from pipmind.broker_positions import project_position_read
from pipmind.broker_read_capture import RawPositionRead, decode_positions_read
from pipmind.contracts import stamp, utc
from pipmind.gateway.config import GatewayError, SymbolMap
from pipmind.reconciliation import Binding, Freshness, ProtectiveOrder, SnapshotFacts, reconcile

START = datetime(2026, 10, 22, 12, tzinfo=UTC)
RECEIVED = START + timedelta(seconds=1)
NOW = START + timedelta(seconds=2)
FRESHNESS = Freshness(timedelta(seconds=30), timedelta(seconds=2))
BINDING = Binding("explicit-caller-account", "demo")
GAPS = {
    "account_scope_unknown",
    "positions_scope_unverified",
    "orders_scope_unknown",
    "protection_scope_unknown",
    "environment_unverified",
}


def symbols(factor="100000"):
    return SymbolMap(
        [
            {
                "instrument": "EUR/USD",
                "market_id": 42,
                "unit": "synthetic-unit",
                "quantity_factor": factor,
                "increment": "1",
                "minimum": "1",
            }
        ]
    )


def capture(raw=b'{"OpenPositions":[]}', *, requested=START, received=RECEIVED):
    return RawPositionRead(raw, hashlib.sha256(raw).hexdigest(), 7, requested, received)


def project(raw=b'{"OpenPositions":[]}', *, now=NOW, mapping=None):
    return project_position_read(
        capture(raw),
        binding=BINDING,
        symbols=mapping or symbols(),
        observed_at=now,
        freshness=FRESHNESS,
    )


def row(identity=11, direction="Buy", quantity="1000", extra="", *, lower=False):
    body = (
        f'{{"OrderId":{identity},"TradingAccountId":7,"MarketId":42,'
        f'"Direction":"{direction}","Quantity":{quantity},"Price":1.1001{extra}}}'
    )
    if lower:
        for key in ("OrderId", "TradingAccountId", "MarketId", "Direction", "Quantity", "Price"):
            body = body.replace('"' + key + '"', '"' + key[0].lower() + key[1:] + '"')
    return body


def payload(*rows, wrapper="OpenPositions", tail=""):
    return ('{"' + wrapper + '":[' + ",".join(rows) + "]" + tail + "}").encode()


def expected(rows=(), ids=()):
    return SnapshotFacts(
        BINDING,
        {"schema_version": 1, "as_of": stamp(START), "currency": "USD", "balance": Decimal("1000")},
        {"schema_version": 1, "as_of": stamp(START), "positions": list(rows)},
        tuple(ids),
        True,
        True,
        (),
        stamp(START),
        True,
        (),
        stamp(START),
        True,
    )


def compare(known, produced, protections=()):
    return reconcile(
        known,
        produced,
        observed_at=NOW,
        freshness=FRESHNESS,
        commands=(),
        protective_obligations=protections,
    )


def findings(result):
    return {(item.code, item.subject, item.identity, item.field) for item in result.findings}


def test_flat_projection_is_positive_but_real_comparator_keeps_missing_scope_findings():
    result = project()
    facts = result.snapshot_facts()
    assert result.positions == () and facts.position_ids == ()
    assert facts.positions == {"schema_version": 1, "as_of": stamp(START), "positions": []}
    assert set(result.gaps) == GAPS and result.dispatch_authorized is False
    checked = compare(expected(), facts)
    actual = findings(checked)
    assert not checked.consistent
    assert ("incomplete", "account", "", "") in actual
    assert ("incomplete", "positions", "", "") in actual
    assert ("incomplete", "orders", "", "") in actual
    assert ("incomplete", "observed.account", "", "") in actual
    assert not any(
        item.subject in {"positions", "observed.positions"}
        and item.code in {"missing", "unexpected", "changed", "duplicate"}
        for item in checked.findings
    )


def test_same_market_same_side_and_opposing_rows_keep_three_distinct_identities_and_exact_sizes():
    raw = payload(row(11), row(12, "Buy", "2000"), row(13, "Sell", "3000"))
    result = project(raw)
    facts = result.snapshot_facts()
    assert tuple(item.position_id for item in result.positions) == ("11", "12", "13")
    assert tuple(item.size for item in result.positions) == tuple(
        map(Decimal, ("0.01", "0.02", "0.03"))
    )
    assert tuple(item.side for item in result.positions) == ("buy", "buy", "sell")
    wanted = expected(
        [
            {
                "instrument": "EUR/USD",
                "side": "buy",
                "size": Decimal("0.01"),
                "average_price": Decimal("1.1001"),
            },
            {
                "instrument": "EUR/USD",
                "side": "buy",
                "size": Decimal("0.02"),
                "average_price": Decimal("1.1001"),
            },
            {
                "instrument": "EUR/USD",
                "side": "sell",
                "size": Decimal("0.03"),
                "average_price": Decimal("1.1001"),
            },
        ],
        ("11", "12", "13"),
    )
    checked = compare(wanted, facts)
    assert not checked.consistent  # Missing account/order scope remains truthful.
    assert not any(
        item.code in {"changed", "missing", "unexpected", "duplicate", "conflicting"}
        and item.subject.endswith("positions")
        for item in checked.findings
    )


def test_projected_position_cannot_supply_missing_individual_protective_evidence():
    facts = project(payload(row())).snapshot_facts()
    stop = ProtectiveOrder(
        "11",
        "observed-stop-id-required",
        {
            "schema_version": 1,
            "client_order_id": "expected-stop",
            "action": "modify",
            "instrument": "EUR/USD",
            "side": "sell",
            "size": Decimal("0.01"),
            "stop": Decimal("1.09"),
        },
    )
    actual = findings(compare(expected(facts.positions["positions"], ("11",)), facts, (stop,)))
    assert ("incomplete", "protective_orders", "", "") in actual
    assert ("missing", "protective_orders", "expected-stop", "") in actual
    assert facts.order_updates is None and facts.protective_orders is None
    assert facts.orders_complete is None and facts.protections_complete is None


def test_repeated_reads_preserve_original_bytes_and_do_not_share_mutable_decodes_or_snapshots():
    raw = payload(row())
    source = capture(raw)
    decoded = decode_positions_read(source, observed_at=NOW, freshness=FRESHNESS)
    decoded["OpenPositions"][0]["Quantity"] = Decimal("99999")
    decoded_again = decode_positions_read(source, observed_at=NOW, freshness=FRESHNESS)
    assert decoded_again["OpenPositions"][0]["Quantity"] == 1000
    result = project_position_read(
        source, binding=BINDING, symbols=symbols(), observed_at=NOW, freshness=FRESHNESS
    )
    first = result.snapshot_facts()
    first.positions["positions"][0]["size"] = Decimal("99999")
    second = result.snapshot_facts()
    assert second.positions["positions"][0]["size"] == Decimal("0.01")
    assert source.body == raw and source.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.source_sha256 == source.sha256
    assert result == project_position_read(
        source, binding=BINDING, symbols=symbols(), observed_at=NOW, freshness=FRESHNESS
    )
    with pytest.raises(FrozenInstanceError):
        source.body = b"{}"


def test_alias_paths_and_raw_hash_domain_are_preserved_instead_of_reserializing_source():
    raw = payload(row(lower=True), wrapper="openPositions")
    result = project(raw)
    assert result.positions[0].source_path == "$.openPositions[0]"
    assert result.source_sha256 == hashlib.sha256(raw).hexdigest()
    spaced = raw.replace(b":", b": ")
    assert project(spaced).positions == result.positions
    assert project(spaced).source_sha256 != result.source_sha256


def test_old_venue_edit_does_not_replace_fresh_scope_or_become_a_liquidation_mark():
    result = project(payload(row(extra=',"LastChangedDateTimeUTC":"2019-01-01T00:00:00Z"')))
    assert utc(result.positions[0].venue_time) == datetime(2019, 1, 1, tzinfo=UTC)
    assert result.positions[0].average_price == Decimal("1.1001")
    assert utc(result.snapshot_facts().positions["as_of"]) == START
    assert "protection_scope_unknown" in result.gaps


def test_missing_venue_time_is_unknown_and_null_price_is_not_invented():
    raw = payload(row().replace('"Price":1.1001', '"Price":null'))
    result = project(raw)
    assert result.positions[0].venue_time is None and result.positions[0].average_price is None
    assert result.snapshot_facts().positions["positions"][0]["average_price"] is None


def test_exact_decimal_projection_does_not_use_or_change_ambient_precision_traps_or_flags():
    raw = payload(row(quantity="1234.000000000000000000001"))
    mapping = symbols()
    with localcontext() as ambient:
        ambient.prec = 3
        ambient.traps[Inexact] = ambient.traps[Rounded] = True
        ambient.clear_flags()
        before = (ambient.prec, dict(ambient.traps), dict(ambient.flags))
        result = project(raw, mapping=mapping)
        assert result.positions[0].size == Decimal("0.01234000000000000000000001")
        assert (ambient.prec, dict(ambient.traps), dict(ambient.flags)) == before


@pytest.mark.parametrize(
    "raw",
    (
        payload(row(), row()),
        payload(row().replace('"TradingAccountId":7', '"TradingAccountId":8')),
        payload(row().replace('"MarketId":42', '"MarketId":999')),
        payload(row().replace('"OrderId":11', '"OrderId":true')),
        payload(row().replace('"Quantity":1000', '"Quantity":"1000"')),
        payload(row().replace('"Quantity":1000', '"Quantity":NaN')),
        payload(row().replace('"Quantity":1000', '"Quantity":1e99999999')),
        payload(row(extra=',"OrderId":12')),
        payload(row(extra=',"secretUnsupportedField":"NEVER_ECHO_THIS_VALUE"')),
        payload(row(), tail=',"SupplementalOpenOrders":[{}]'),
        payload(row(extra=',"StopOrder":{}')),
        payload(row(extra=',"AssociatedOrders":[]')),
        b'{"OpenPositions":[],"Complete":true}',
        payload(row(extra=',"Status":2')),
    ),
)
def test_unsupported_conflicting_or_incomplete_wire_facts_refuse_without_payload_leak(raw):
    with pytest.raises(GatewayError) as caught:
        project(raw)
    assert "NEVER_ECHO_THIS_VALUE" not in str(caught.value)
    assert "NEVER_ECHO_THIS_VALUE" not in repr(caught.value)


def test_nonterminating_unit_conversion_is_refused_instead_of_rounded():
    with pytest.raises(GatewayError):
        project(payload(row(quantity="1")), mapping=symbols("3"))


def test_null_protection_placeholders_and_empty_supplement_do_not_assert_complete_scopes():
    raw = payload(
        row(extra=',"StopOrder":null,"LimitOrder":null,"AssociatedOrders":null'),
        tail=',"SupplementalOpenOrders":[]',
    )
    result = project(raw)
    assert set(result.gaps) == GAPS
    facts = result.snapshot_facts()
    assert facts.account is None and facts.account_complete is None
    assert facts.positions_complete is None and facts.orders_complete is None
    assert facts.protections_complete is None


def test_binding_remains_an_unverified_caller_claim_for_demo_and_live():
    for environment in ("demo", "live"):
        binding = Binding("explicit-caller-account", environment)
        result = project_position_read(
            capture(), binding=binding, symbols=symbols(), observed_at=NOW, freshness=FRESHNESS
        )
        assert result.binding == binding and result.snapshot_facts().binding == binding
        assert "environment_unverified" in result.gaps and result.dispatch_authorized is False


@pytest.mark.parametrize(
    "requested,received,observed",
    (
        (START, RECEIVED, START),
        (START, START + timedelta(seconds=3), START + timedelta(seconds=4)),
        (START, RECEIVED, START + timedelta(seconds=30, microseconds=1)),
    ),
)
def test_request_receipt_and_observation_windows_do_not_forge_freshness(
    requested, received, observed
):
    with pytest.raises(GatewayError):
        project_position_read(
            capture(requested=requested, received=received),
            binding=BINDING,
            symbols=symbols(),
            observed_at=observed,
            freshness=FRESHNESS,
        )


def test_window_equality_is_supported_and_future_venue_event_refuses():
    result = project_position_read(
        capture(received=START + timedelta(seconds=2)),
        binding=BINDING,
        symbols=symbols(),
        observed_at=START + timedelta(seconds=30),
        freshness=FRESHNESS,
    )
    assert result.positions == ()
    with pytest.raises(GatewayError):
        project(payload(row(extra=',"LastChangedDateTimeUTC":"2026-10-22T12:00:03Z"')))
    # A later local observation cannot make an event from after the captured
    # response receipt belong to that response.
    with pytest.raises(GatewayError):
        project(payload(row(extra=',"LastChangedDateTimeUTC":"2026-10-22T12:00:01.5Z"')))


def test_venue_event_inside_request_window_does_not_change_request_start_scope():
    result = project(payload(row(extra=',"LastChangedDateTimeUTC":"2026-10-22T12:00:00.5Z"')))
    assert utc(result.positions[0].venue_time) == START + timedelta(milliseconds=500)
    assert utc(result.snapshot_facts().positions["as_of"]) == START
    assert "positions_scope_unverified" in result.gaps
