from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pipmind.reconciliation import (
    Binding,
    Freshness,
    ProtectiveOrder,
    SnapshotFacts,
    reconcile,
)

NOW = datetime(2026, 1, 12, 14, tzinfo=UTC)
STAMP = NOW.isoformat()
FRESHNESS = Freshness(timedelta(seconds=30), timedelta(seconds=2))
BINDING = Binding("synthetic-account", "demo")


def position(side="buy", size=10, instrument="EUR/USD"):
    return {"instrument": instrument, "side": side, "size": size, "average_price": 1.1}


def book(rows=(), ids=()):
    return SnapshotFacts(
        BINDING,
        {"schema_version": 1, "as_of": STAMP, "currency": "USD", "balance": 1000},
        {"schema_version": 1, "as_of": STAMP, "positions": list(rows)},
        position_ids=ids,
        account_complete=True,
        positions_complete=True,
        order_updates=(),
        orders_as_of=STAMP,
        orders_complete=True,
    )


def compare(expected=None, observed=None, **kwargs):
    return reconcile(
        book() if expected is None else expected,
        book() if observed is None else observed,
        observed_at=NOW,
        freshness=FRESHNESS,
        commands=kwargs.pop("commands", ()),
        protective_obligations=kwargs.pop("protective_obligations", ()),
        **kwargs,
    )


def codes(result):
    return {(f.code, f.subject, f.identity, f.field) for f in result.findings}


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_flat_long_and_short_positive_controls_do_not_require_a_model_or_gateway(side):
    assert compare().consistent
    state = book([position(side)], ["position-1"])
    assert compare(state, deepcopy(state)).consistent


def test_decimal_quantities_are_not_rounded_or_binary_subtracted():
    expected = book([position(size=Decimal("0.1"))], ["p"])
    observed = book([position(size=0.1)], ["p"])
    assert compare(expected, observed).consistent
    observed.positions["positions"][0]["size"] = Decimal("0.10000000000000000001")
    assert ("changed", "positions", "p", "size") in codes(compare(expected, observed))


def test_distinct_opposing_positions_cannot_net_to_flat_or_merge_by_instrument():
    expected = book([position("buy"), position("sell")], ["long-1", "short-2"])
    result = compare(expected, book())
    assert {("missing", "positions", p, "") for p in ("long-1", "short-2")} <= codes(result)
    swapped = book([position("sell"), position("buy")], ["long-1", "short-2"])
    assert (
        sum(f.code == "changed" and f.field == "side" for f in compare(expected, swapped).findings)
        == 2
    )


@pytest.mark.parametrize(
    "binding",
    [None, "demo", [], Binding("other-account", "demo"), Binding("synthetic-account", "live")],
)
def test_binding_absence_or_exact_account_environment_mismatch_cannot_clear(binding):
    assert not compare(observed=replace(book(), binding=binding)).consistent


def test_missing_extra_and_changed_positions_keep_exact_instrument_and_identity():
    expected = book([position(), position(instrument="GBP/USD")], ["one", "two"])
    observed = book([position(instrument="eur/usd"), position()], ["one", "three"])
    assert {
        ("changed", "positions", "one", "instrument"),
        ("missing", "positions", "two", ""),
        ("unexpected", "positions", "three", ""),
    } <= codes(compare(expected, observed))


def test_duplicate_and_conflicting_position_ids_are_not_silently_deduplicated():
    state = book([position(), position("sell")], ["same", "same"])
    result = compare(book(), state)
    assert ("duplicate", "observed.positions", "same", "") in codes(result)
    assert ("conflicting", "observed.positions", "same", "") in codes(result)


def test_legacy_position_payload_without_external_ids_is_incomplete_not_flat():
    state = replace(book([position()], ["p"]), position_ids=None)
    assert ("incomplete", "observed.position_identities", "", "") in codes(compare(book(), state))


@pytest.mark.parametrize(
    "field", ["balance", "currency", "equity", "margin_used", "margin_available"]
)
def test_account_obligations_are_compared_exactly(field):
    expected, observed = book(), book()
    expected.account[field] = "USD" if field == "currency" else 5
    observed.account[field] = "EUR" if field == "currency" else 6
    assert ("changed", "account", "", field) in codes(compare(expected, observed))


@pytest.mark.parametrize("seconds,code", [(-31, "stale"), (1, "future"), (-1, "late")])
def test_observation_time_and_expected_checkpoint_prevent_stale_future_or_late_clear(seconds, code):
    observed = book()
    observed.positions["as_of"] = (NOW + timedelta(seconds=seconds)).isoformat()
    assert (code, "positions", "", "") in codes(compare(book(), observed))


def test_freshness_boundary_is_inclusive_and_snapshot_skew_is_independent():
    expected, observed = book(), book()
    for state in (expected, observed):
        state.account["as_of"] = (NOW - timedelta(seconds=30)).isoformat()
        state.positions["as_of"] = (NOW - timedelta(seconds=30)).isoformat()
    assert compare(expected, observed).consistent
    observed.account["as_of"] = STAMP
    assert ("conflicting_time", "snapshots", "", "") in codes(compare(expected, observed))


@pytest.mark.parametrize("field", ["account_complete", "positions_complete", "orders_complete"])
@pytest.mark.parametrize("flag", [None, False, 1])
def test_unknown_or_nonboolean_scope_never_becomes_all_clear(field, flag):
    assert not compare(observed=replace(book(), **{field: flag})).consistent


def request(identity="command-1"):
    return {
        "schema_version": 1,
        "client_order_id": identity,
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": 10,
        "order_type": "market",
    }


def update(identity="command-1", status="filled"):
    return {
        "schema_version": 1,
        "client_order_id": identity,
        "broker_order_id": "broker-1",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": 10,
        "status": status,
        "ts": STAMP,
    }


def command(state="reconciled", outcome="filled"):
    return {
        "command_id": "command-1",
        "state": state,
        "broker_order_id": "broker-1",
        "known_outcome": outcome,
    }


def test_known_command_matches_broker_order_identity_instrument_and_quantity():
    observed = replace(book(), order_updates=[update()])
    assert compare(observed=observed, commands=[command()], command_requests=[request()]).consistent
    observed.order_updates[0]["broker_order_id"] = "different"
    observed.order_updates[0]["size"] = 11
    findings = codes(compare(observed=observed, commands=[command()], command_requests=[request()]))
    assert ("changed", "command_outcome", "command-1", "broker_order_id") in findings
    assert ("changed", "command_outcome", "command-1", "size") in findings


@pytest.mark.parametrize(
    "state,code", [("accepted", "pending"), ("dispatching", "pending"), ("unknown", "ambiguous")]
)
def test_pending_and_uncertain_commands_are_not_resolved_by_late_broker_updates(state, code):
    observed = replace(book(), order_updates=[update()])
    result = compare(observed=observed, commands=[command(state)], command_requests=[request()])
    assert (code, "commands", "command-1", "") in codes(result)


def test_pending_ids_duplicates_and_conflicting_order_facts_stay_explicit():
    observed = replace(book(), order_updates=[update(), update(status="rejected")])
    result = compare(
        observed=observed,
        commands=[command(), command()],
        command_requests=[request()],
        pending_command_ids=["command-1", "missing"],
    )
    assert ("duplicate", "commands", "command-1", "") in codes(result)
    assert ("conflicting", "orders", "command-1", "") in codes(result)
    assert ("missing", "commands", "missing", "") in codes(result)


def test_unknown_commands_orders_or_missing_observed_account_cannot_clear():
    assert not compare(commands=None).consistent
    assert not compare(observed=replace(book(), order_updates=None)).consistent
    assert not compare(observed=replace(book(), account=None)).consistent
    assert not compare(observed=replace(book(), order_updates=[update()])).consistent


def protection(stop=Decimal("1.05"), broker_id="stop-broker"):
    payload = {
        "schema_version": 1,
        "client_order_id": "stop-1",
        "action": "modify",
        "instrument": "EUR/USD",
        "side": "sell",
        "size": 10,
        "stop": stop,
    }
    return ProtectiveOrder("position-1", broker_id, payload)


def protected_book():
    order = update("stop-1", "working")
    order.update(broker_order_id="stop-broker", side="sell")
    return replace(
        book([position()], ["position-1"]),
        order_updates=[order],
        protective_orders=[protection()],
        protections_as_of=STAMP,
        protections_complete=True,
    )


def test_supported_protective_price_quantity_identity_and_working_obligations():
    observed = protected_book()
    assert compare(observed, observed, protective_obligations=[protection()]).consistent
    changed = replace(observed, protective_orders=[protection(Decimal("1.04"))])
    assert ("changed", "protective_orders", "stop-1", "stop") in codes(
        compare(observed, changed, protective_obligations=[protection()])
    )
    absent = replace(observed, protective_orders=None, protections_complete=None)
    assert not compare(observed, absent, protective_obligations=[protection()]).consistent


def test_old_working_order_event_can_be_in_a_fresh_observation_but_partial_is_not_proof():
    observed = protected_book()
    observed.order_updates[0]["ts"] = (NOW - timedelta(days=1)).isoformat()
    assert compare(observed, observed, protective_obligations=[protection()]).consistent
    observed.order_updates[0]["status"] = "partial"
    assert not compare(observed, observed, protective_obligations=[protection()]).consistent


@pytest.mark.parametrize("field,value", [("instrument", "GBP/USD"), ("side", "buy"), ("size", 9)])
def test_protective_supplement_cannot_disagree_with_its_broker_order_update(field, value):
    observed = protected_book()
    observed.order_updates[0][field] = value
    assert ("changed", "protective_orders", "stop-1", field) in codes(
        compare(observed, observed, protective_obligations=[protection()])
    )


def test_unknown_protective_obligations_are_not_an_explicit_empty_scope():
    assert not compare(protective_obligations=None).consistent


@pytest.mark.parametrize("bad", ["position-1", b"p", 1, {}])
def test_malformed_supplementary_identity_sequence_does_not_become_a_match(bad):
    state = replace(book([position()], ["p"]), position_ids=bad)
    assert ("invalid", "observed.position_identities", "", "") in codes(compare(book(), state))


def test_malformed_collections_and_null_expected_price_produce_findings():
    assert not compare(commands=1).consistent
    assert not compare(command_requests=None).consistent
    assert not compare(protective_obligations=1).consistent
    assert not compare(observed=replace(book(), order_updates=1)).consistent
    state = book([position()], ["p"])
    state.positions["positions"][0]["average_price"] = None
    assert ("incomplete", "positions", "p", "average_price") in codes(compare(state, state))


def test_missing_margin_and_nonfinite_optional_observation_cannot_clear():
    expected, observed = book(), book()
    expected.account["margin_available"] = 10
    assert ("missing", "account", "", "margin_available") in codes(compare(expected, observed))
    observed.account["equity"] = Decimal("Infinity")
    assert not compare(book(), observed).consistent


def test_broker_order_aliasing_and_order_observation_future_or_staleness_are_findings():
    observed = replace(book(), order_updates=[update(), update("another")])
    assert ("conflicting", "order_broker_identity", "command-1", "") in codes(
        compare(observed=observed)
    )
    observed = replace(book(), orders_as_of=(NOW - timedelta(seconds=31)).isoformat())
    assert ("stale", "orders", "", "") in codes(compare(observed=observed))
    observed = replace(book(), order_updates=[update()])
    observed.order_updates[0]["ts"] = (NOW + timedelta(seconds=1)).isoformat()
    assert ("future", "orders", "command-1", "") in codes(compare(observed=observed))


def test_invalid_numbers_and_contradictory_side_size_fail_without_mutating_inputs():
    expected, observed = book([position()], ["p"]), book([position()], ["p"])
    observed.positions["positions"][0]["size"] = float("nan")
    result = compare(expected, observed)
    assert any(f.code == "invalid" for f in result.findings)
    observed = book([position("flat", 10)], ["p"])
    assert any(f.code == "conflicting" for f in compare(expected, observed).findings)
    observed = protected_book()
    raw = deepcopy(observed)
    wanted = [protection()]
    raw_wanted = deepcopy(wanted)
    result = compare(observed, observed, protective_obligations=wanted)
    assert observed == raw and wanted == raw_wanted
    assert result.findings == tuple(sorted(result.findings))


def test_configuration_has_no_implicit_clock_or_freshness_policy():
    with pytest.raises(ValueError, match="reconciliation_freshness"):
        Freshness(timedelta(seconds=-1), timedelta(0))
    with pytest.raises(ValueError, match="reconciliation_configuration"):
        reconcile(
            book(), book(), observed_at=NOW.replace(tzinfo=None), freshness=FRESHNESS, commands=()
        )
