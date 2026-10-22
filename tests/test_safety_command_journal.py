"""Exact-number audit codecs and local transitions; no SQL or simulated venue."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D
from decimal import Inexact, Rounded, getcontext, localcontext

import pytest

import pipmind.safety.command_journal as journal
from pipmind.safety.command_journal import (
    AttemptIntent,
    CommandIntent,
    JournalRefused,
    JournalScope,
    UnknownOutcome,
    canonical_order,
    decode_attempt,
    decode_command,
    decode_order,
    decode_scope,
    decode_unknown,
    encode_attempt,
    encode_command,
    encode_order,
    encode_scope,
    encode_unknown,
    fingerprint,
    project,
    validate_attempt,
    validate_unknown,
)
from pipmind.safety.incarnation import DatabaseIncarnation
from pipmind.safety.recovery_sql import Assignment, RobotReference
from pipmind.safety.session import SessionBinding

PIN = DatabaseIncarnation(
    "synthetic-sql",
    "synthetic-db",
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)


def facts():
    binding = SessionBinding("synthetic-account", "demo", "USD")
    assignment = Assignment(
        binding,
        1,
        "assignment-A",
        RobotReference("robot-A", "v1", "a" * 64),
        "e" * 64,
        "c" * 64,
        "2026-10-22T01:00:00Z",
    )
    scope = JournalScope(assignment, "owner-A", 3, "execution", "2026-10-22T02:00:00Z", PIN, 4)
    order = {
        "schema_version": 1,
        "client_order_id": "command-A",
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": D("1000.00"),
        "order_type": "market",
        "stop": D("1.09000"),
    }
    command = CommandIntent(
        "command-A",
        encode_order(order),
        scope,
        "cause-A",
        "recorded",
        "locally_recorded",
        "2026-10-22T01:01:00Z",
    )
    attempt = AttemptIntent(
        command.command_id,
        fingerprint(encode_command(command)),
        "attempt-A",
        scope,
        "2026-10-22T01:02:00Z",
    )
    unknown = UnknownOutcome(
        command.command_id,
        fingerprint(encode_command(command)),
        fingerprint(encode_attempt(attempt)),
        scope,
        "outcome_unresolved",
        "2026-10-22T01:03:00Z",
    )
    return order, scope, command, attempt, unknown


def test_positive_records_roundtrip_exactly_without_mutation_or_authority():
    order, scope, command, attempt, unknown = facts()
    before = deepcopy((order, scope, command, attempt, unknown))
    for value, encoder, decoder in (
        (scope, encode_scope, decode_scope),
        (command, encode_command, decode_command),
        (attempt, encode_attempt, decode_attempt),
        (unknown, encode_unknown, decode_unknown),
    ):
        raw = encoder(value)
        assert decoder(raw) == value and encoder(decoder(raw)) == raw
        assert value.dispatch_authorized is False
        with pytest.raises(FrozenInstanceError):
            value.dispatch_authorized = True
    assert (order, scope, command, attempt, unknown) == before
    assert validate_attempt(command, attempt) is attempt
    assert validate_unknown(command, attempt, unknown) is unknown
    assert project(command).state == "recorded" and not project(command).unresolved
    pending = project(command, attempt)
    assert pending.state == "attempt_intent" and pending.unresolved
    observed = project(command, attempt, unknown)
    assert observed.state == "unknown" and observed.unresolved
    assert not observed.dispatch_authorized
    assert observed.command.scope.assignment == command.scope.assignment


@pytest.mark.parametrize(
    "value", [D("1000.00"), D("1E+64"), D("1.2345678901234567890123456789"), D("1E-64"), 1000]
)
def test_order_exact_numeric_json_not_strings_or_float_rounding(value):
    order, *_ = facts()
    order["size"] = value
    raw = canonical_order(order)
    decoded = decode_order(raw)
    assert type(decoded["size"]) is D and decoded["size"] == value
    assert encode_order(decoded) == raw and '"size":"' not in raw
    if type(value) is D:
        assert decoded["size"].as_tuple() == value.as_tuple()


@pytest.mark.parametrize("value", [D("-0"), D("-0.00"), D("0E+3")])
def test_signed_zero_and_exponent_are_retained_without_claiming_valid_risk(value):
    order, *_ = facts()
    # Normative v1 permits numeric modify stop facts; this is not risk preflight.
    modified = {
        "schema_version": 1,
        "client_order_id": "modify-A",
        "action": "modify",
        "instrument": "EUR/USD",
        "stop": value,
    }
    raw = encode_order(modified)
    assert decode_order(raw)["stop"].as_tuple() == value.as_tuple()
    assert encode_order(decode_order(raw)) == raw
    assert order["size"] == D("1000.00")


def test_numeric_codec_independent_of_low_trapping_ambient_decimal_context():
    order, scope, command, attempt, unknown = facts()
    order["size"] = D("1E+64")
    expected = encode_order(order)
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.Emin = -2
        context.capitals = 0
        context.traps[Inexact] = context.traps[Rounded] = True
        context.clear_flags()
        before = getcontext().copy()
        assert encode_order(order) == expected
        assert encode_order(decode_order(expected)) == expected
        for value, encoder in (
            (scope, encode_scope),
            (command, encode_command),
            (attempt, encode_attempt),
            (unknown, encode_unknown),
        ):
            assert encoder(value)
        after = getcontext()
        assert after.prec == before.prec and after.Emax == before.Emax and after.Emin == before.Emin
        assert after.capitals == before.capitals and after.flags == before.flags
        assert after.traps == before.traps


@pytest.mark.parametrize(
    "value", [True, 1.1, D("NaN"), D("sNaN"), D("Infinity"), D("1E+65"), 10**64, D("1." + "2" * 64)]
)
def test_invalid_or_overcapacity_numbers_refuse_without_input_change(value):
    order, *_ = facts()
    order["size"] = value
    before = dict(order)
    with pytest.raises(JournalRefused):
        encode_order(order)
    assert order.keys() == before.keys() and order["size"] is value


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": True},
        {"side": "unknown"},
        {"untrusted": "PRIVATE payload"},
        {"action": "send"},
        {"size": None},
        {"client_order_id": " x"},
        {"client_order_id": "x" * 129},
        {"client_order_id": "\ud800"},
    ],
)
def test_normative_order_and_technical_identity_refusals_are_fixed(mutation):
    order, *_ = facts()
    order.update(mutation)
    with pytest.raises(JournalRefused) as error:
        encode_order(order)
    assert "PRIVATE" not in str(error.value) and "untrusted" not in str(error.value)


def test_normative_modify_omit_null_and_close_are_distinct_audit_bodies():
    base = {
        "schema_version": 1,
        "client_order_id": "modify-A",
        "action": "modify",
        "instrument": "EUR/USD",
        "related_client_order_id": "command-A",
    }
    omitted = encode_order(base)
    explicit_null = encode_order({**base, "stop": None})
    assert omitted != explicit_null and "stop" not in decode_order(omitted)
    closed = {
        "schema_version": 1,
        "client_order_id": "close-A",
        "action": "close",
        "instrument": "EUR/USD",
        "size": None,
    }
    assert decode_order(encode_order(closed))["size"] is None


def test_custom_hooks_never_run_before_type_refusal():
    class UnsafeDict(dict):
        def items(self):
            pytest.fail("custom mapping hook executed")

        def __iter__(self):
            pytest.fail("custom iterator executed")

    class UnsafeStr(str):
        def replace(self, *args):
            pytest.fail("custom timestamp hook executed")

    order, scope, command, *_ = facts()
    with pytest.raises(JournalRefused):
        encode_order(UnsafeDict(order))
    with pytest.raises(JournalRefused):
        encode_order({**order, "client_order_id": UnsafeStr("command-A")})
    with pytest.raises(JournalRefused):
        encode_scope(
            replace(
                scope,
                assignment=replace(
                    scope.assignment, activated_at=UnsafeStr(scope.assignment.activated_at)
                ),
            )
        )
    with pytest.raises(JournalRefused):
        encode_command(replace(command, recorded_at=UnsafeStr(command.recorded_at)))


@pytest.mark.parametrize(
    "encoder,decoder,index",
    [
        (encode_scope, decode_scope, 1),
        (encode_command, decode_command, 2),
        (encode_attempt, decode_attempt, 3),
        (encode_unknown, decode_unknown, 4),
    ],
)
def test_metadata_codec_rejects_unknown_duplicate_noncanonical_and_authority_fields(
    encoder, decoder, index
):
    value = facts()[index]
    raw = encoder(value)
    for bad in (
        raw + " ",
        raw.replace("{", '{"unexpected":true,', 1),
        raw.replace('"dispatch_authorized":false', '"dispatch_authorized":true', 1),
        raw.replace(
            '"dispatch_authorized":false',
            '"dispatch_authorized":false,"dispatch_authorized":false',
            1,
        ),
    ):
        with pytest.raises(JournalRefused):
            decoder(bad)
    with pytest.raises(JournalRefused):
        encoder(replace(value, dispatch_authorized=True))


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"schema_version":1,"client_order_id":"a","action":"close","instrument":"EUR/USD","size":NaN}',
        "[]",
        "{}",
        '{"schema_version":true}',
    ],
)
def test_order_decode_refuses_invalid_duplicate_or_nonfinite_json(raw):
    with pytest.raises(JournalRefused):
        decode_order(raw)


def test_technical_codec_size_equality_overflow_and_id_utf16_bounds(monkeypatch):
    order, scope, command, *_ = facts()
    order["client_order_id"] = "😀" * 64
    assert decode_order(encode_order(order))["client_order_id"] == order["client_order_id"]
    with pytest.raises(JournalRefused):
        encode_order({**order, "client_order_id": "😀" * 65})
    order["client_order_id"] = command.command_id
    raw = encode_order(order)
    monkeypatch.setattr(journal, "MAX_ORDER_BYTES", len(raw.encode()))
    assert encode_order(order) == raw
    with pytest.raises(JournalRefused, match="^journal_order_too_large$"):
        encode_order({**order, "instrument": order["instrument"] + "X"})
    scope_raw = encode_scope(scope)
    monkeypatch.setattr(journal, "MAX_RECORD_BYTES", len(scope_raw.encode()))
    assert encode_scope(scope) == scope_raw
    with pytest.raises(JournalRefused, match="^journal_record_too_large$"):
        encode_scope(replace(scope, owner_id=scope.owner_id + "X"))


def test_command_identity_and_local_vocabulary_cannot_claim_venue_outcomes():
    _, _, command, *_ = facts()
    with pytest.raises(JournalRefused, match="^journal_command_identity_mismatch$"):
        encode_command(replace(command, command_id="different"))
    for outcome in ("accepted", "working", "filled", "cancelled", "reconciled"):
        with pytest.raises(JournalRefused):
            encode_command(replace(command, local_outcome=outcome))
    for code in ("PRIVATE error details", "filled", "dry_accepted"):
        with pytest.raises(JournalRefused):
            encode_command(replace(command, code=code))


def test_refused_local_fact_has_no_eligible_attempt_or_unknown_projection():
    _, _, command, attempt, unknown = facts()
    refused = replace(command, local_outcome="refused", code="locally_refused")
    assert project(refused).state == "refused" and not project(refused).unresolved
    changed = replace(attempt, command_sha256=fingerprint(encode_command(refused)))
    with pytest.raises(JournalRefused, match="^journal_refused_command$"):
        validate_attempt(refused, changed)
    with pytest.raises(JournalRefused):
        project(refused, changed, unknown)


@pytest.mark.parametrize(
    "change",
    [
        "id",
        "hash",
        "assignment",
        "native",
        "resource",
        "lease-regression",
        "same-gen-owner",
        "control-regression",
        "time",
    ],
)
def test_attempt_refuses_identity_scope_and_chronology_changes(change):
    _, _, command, attempt, _ = facts()
    if change == "id":
        attempt = replace(attempt, command_id="other")
    elif change == "hash":
        attempt = replace(attempt, command_sha256="0" * 64)
    elif change == "time":
        attempt = replace(attempt, recorded_at="2026-10-22T01:00:01Z")
    else:
        scope = attempt.scope
        if change == "assignment":
            scope = replace(
                scope, assignment=replace(scope.assignment, assignment_id="replacement")
            )
        elif change == "native":
            scope = replace(scope, application_incarnation=replace(PIN, database="restored"))
        elif change == "resource":
            scope = replace(scope, lease_resource="other-resource")
        elif change == "lease-regression":
            scope = replace(scope, lease_generation=2)
        elif change == "same-gen-owner":
            scope = replace(scope, owner_id="other-owner")
        else:
            scope = replace(scope, control_revision=3)
        attempt = replace(attempt, scope=scope)
    with pytest.raises(JournalRefused):
        validate_attempt(command, attempt)


def test_new_verified_owner_same_assignment_may_record_one_marker_without_relabelling_command():
    _, _, command, attempt, _ = facts()
    refreshed = replace(
        attempt,
        scope=replace(
            attempt.scope,
            owner_id="owner-B",
            lease_generation=4,
            control_revision=5,
            lease_expires_at="2026-10-22T03:00:00Z",
        ),
    )
    assert validate_attempt(command, refreshed) is refreshed
    view = project(command, refreshed)
    assert view.command.scope.owner_id == "owner-A" and view.attempt.scope.owner_id == "owner-B"
    assert view.command.scope.assignment == view.attempt.scope.assignment
    # Global one-attempt uniqueness belongs to SQL, not a pure pairwise validator.
    another = replace(refreshed, attempt_id="competing-intent")
    assert validate_attempt(command, another) is another


def test_unknown_observer_may_change_assignment_native_and_owner_without_rewriting_originals():
    _, _, command, attempt, unknown = facts()
    later_assignment = replace(
        command.scope.assignment,
        generation=2,
        assignment_id="assignment-B",
        robot=RobotReference("robot-B", "v2", "f" * 64),
        epoch="f" * 64,
        activated_at="2026-10-22T01:02:30Z",
    )
    observer = replace(
        unknown.scope,
        assignment=later_assignment,
        owner_id="owner-B",
        lease_generation=7,
        application_incarnation=replace(
            PIN, database="restored", database_guid="33333333-3333-4333-8333-333333333333"
        ),
    )
    observed = replace(unknown, scope=observer)
    before = encode_command(command), encode_attempt(attempt)
    assert validate_unknown(command, attempt, observed) is observed
    view = project(command, attempt, observed)
    assert view.state == "unknown" and view.unresolved
    assert view.command.scope.assignment.assignment_id == "assignment-A"
    assert view.unknown.scope.assignment.assignment_id == "assignment-B"
    assert before == (encode_command(command), encode_attempt(attempt))


@pytest.mark.parametrize(
    "change", ["id", "command-hash", "attempt-hash", "binding", "resource", "time", "terminal"]
)
def test_unknown_never_fabricates_missing_attempt_or_accepts_mismatched_evidence(change):
    _, _, command, attempt, unknown = facts()
    if change == "id":
        unknown = replace(unknown, command_id="other")
    elif change == "command-hash":
        unknown = replace(unknown, command_sha256="0" * 64)
    elif change == "attempt-hash":
        unknown = replace(unknown, attempt_sha256="0" * 64)
    elif change == "resource":
        unknown = replace(unknown, scope=replace(unknown.scope, lease_resource="other"))
    elif change == "time":
        unknown = replace(unknown, recorded_at="2026-10-22T01:01:30Z")
    elif change == "terminal":
        unknown = replace(unknown, code="filled")
    else:
        changed_binding = replace(unknown.scope.assignment.binding, account_id="other")
        unknown = replace(
            unknown,
            scope=replace(
                unknown.scope, assignment=replace(unknown.scope.assignment, binding=changed_binding)
            ),
        )
    with pytest.raises(JournalRefused):
        validate_unknown(command, attempt, unknown)
    with pytest.raises(JournalRefused, match="^journal_attempt_missing$"):
        project(command, unknown=unknown)


@pytest.mark.parametrize(
    "field,value",
    [
        ("lease_generation", True),
        ("lease_generation", 0),
        ("control_revision", False),
        ("control_revision", -1),
        ("control_revision", journal.MAX_GENERATION + 1),
    ],
)
def test_scope_counters_are_exact_bounded_ints(field, value):
    _, scope, *_ = facts()
    with pytest.raises(JournalRefused):
        encode_scope(replace(scope, **{field: value}))


def test_event_time_requires_current_finite_scope_without_reinterpreting_old_receipt():
    _, _, command, attempt, unknown = facts()
    before = encode_command(command)
    for bad in (
        "2026-10-22T00:59:59Z",
        command.scope.lease_expires_at,
        "2026-10-22T01:01:00-04:00",
        "not-a-time",
    ):
        with pytest.raises(JournalRefused):
            encode_command(replace(command, recorded_at=bad))
    assert encode_command(decode_command(before)) == before
    assert project(command, attempt, unknown).unresolved


@pytest.mark.parametrize("captured_valid", [True, False])
def test_order_schema_validation_uses_captured_body_not_mutated_caller(monkeypatch, captured_valid):
    order, *_ = facts()
    original_side = "buy" if captured_valid else "not-a-side"
    order["side"] = original_side
    real = journal._contracts()
    seen = []

    class ValidateCaptured:
        def validate(self, name, captured):
            # Deterministically switch the original mapping immediately before
            # normative validation. Older code validates the wrong content.
            order["side"] = "not-a-side" if captured_valid else "buy"
            seen.append(captured["side"])
            return real.validate(name, captured)

    monkeypatch.setattr(journal, "_contracts", lambda: ValidateCaptured())
    if captured_valid:
        raw = encode_order(order)
        assert json.loads(raw)["side"] == "buy" and order["side"] == "not-a-side"
    else:
        with pytest.raises(JournalRefused):
            encode_order(order)
        assert order["side"] == "buy"
    assert seen == [original_side]
