"""Independent admission counterexamples built from public source/session DTOs."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, Rounded, getcontext, localcontext
from hashlib import sha256

import pytest

from pipmind.reconciliation import Binding, Freshness, ProtectiveOrder, SnapshotFacts, reconcile
from pipmind.safety import Conversion, Exposure, ProposalFacts, RiskFacts, RiskPolicy
from pipmind.safety.admission import (
    MAX_REQUEST_BYTES,
    AdmissionRefused,
    AdmissionRequest,
    PositionMark,
    ReconciliationEvidence,
    Reservation,
    ReservationMark,
    assess,
    checkpoint_digest,
    decode_policy_book,
    decode_receipt,
    decode_request,
    decode_reservation,
    encode_policy_book,
    encode_receipt,
    encode_request,
    encode_reservation,
    policy_book_digest,
)
from pipmind.safety.session import (
    EquityObservation,
    SessionBinding,
    SessionSpec,
    advance,
    encode_state,
    initial_state,
)

D = Decimal
STAMP = "2026-10-21T12:00:00Z"
START = "2026-10-21T00:00:00Z"
END = "2026-10-22T00:00:00Z"
NOW = datetime(2026, 10, 21, 12, 0, 10, tzinfo=UTC)
BINDING = Binding("synthetic-account", "demo")


def policy(instrument):
    return RiskPolicy(
        "synthetic-policy",
        "synthetic-account",
        "demo",
        "USD",
        instrument,
        D("2000"),
        D("5000"),
        D("10000"),
        D("50"),
        D("100"),
        D("100"),
        D("200"),
        D("0"),
        10,
        60,
        30,
        30,
    )


def policies(*instruments, **changes):
    return tuple(
        replace(policy(instrument), **changes)
        for instrument in sorted(instruments or ("EUR/USD", "GBP/USD"))
    )


def caps(book, **changes):
    return tuple(replace(row, **changes) for row in book)


def conversion(currency="USD", rate="1", as_of=STAMP):
    return Conversion(currency, "USD", D(rate), as_of)


def held(
    position_id="position-1",
    instrument="GBP/USD",
    side="buy",
    size="10",
    bid="1.30",
    ask="1.3002",
    stop="1.29",
    factor="100",
):
    size, bid, ask, stop, factor = map(D, (size, bid, ask, stop, factor))
    opposite = "sell" if side == "buy" else "buy"
    request = {
        "schema_version": 1,
        "client_order_id": "stop-" + position_id,
        "action": "modify",
        "instrument": instrument,
        "side": opposite,
        "size": size,
        "stop": stop,
    }
    protection = ProtectiveOrder(position_id, "broker-" + position_id, request)
    update = {
        "schema_version": 1,
        "client_order_id": request["client_order_id"],
        "broker_order_id": protection.broker_order_id,
        "status": "working",
        "instrument": instrument,
        "side": opposite,
        "size": size,
        "filled_size": D("0"),
        "ts": STAMP,
    }
    position = {"instrument": instrument, "side": side, "size": size, "average_price": bid}
    mark = PositionMark(position_id, bid, ask, STAMP, factor)
    exposure = Exposure(
        position_id,
        instrument,
        side,
        size * factor,
        bid if side == "buy" else ask,
        stop,
        STAMP,
        conversion(),
    )
    return position_id, position, protection, update, mark, exposure


def bind(request, book, *, historical_breach=False, snapshot_age=60):
    """Use real session transitions, never handcraft a supposedly coherent state."""
    source = request.evidence.observed.account
    baseline = SessionSpec(
        SessionBinding(BINDING.account_id, BINDING.environment, "USD"),
        "session-one",
        book[0].policy_id,
        policy_book_digest(book),
        START,
        END,
        D("10000"),
        "opening-equity-evidence",
        book[0].max_daily_loss,
        snapshot_age,
    )
    state = initial_state(baseline)
    if historical_breach:
        earlier = EquityObservation(
            baseline.binding,
            baseline.session_id,
            "historical-breach",
            "2026-10-21T11:59:00Z",
            "2026-10-21T11:59:01Z",
            D("9890"),
            D("0"),
            True,
            True,
            START,
            "2026-10-21T11:59:00Z",
            "old-account",
            "old-flow",
        )
        state, _ = advance(state, earlier, now=NOW)
    digest = checkpoint_digest(request.evidence, request.facts)
    checkpoint = EquityObservation(
        baseline.binding,
        baseline.session_id,
        "source-" + digest[:16],
        source["as_of"],
        source["as_of"],
        source["equity"],
        D("0"),
        True,
        True,
        START,
        source["as_of"],
        digest,
        "complete-cumulative-flow-evidence",
    )
    state, _ = advance(state, checkpoint, now=NOW)
    return (
        replace(
            request,
            expected_session_revision=state.revision,
            expected_observation_sha256=state.latest_sha256,
        ),
        state,
        book,
    )


def case(*, book=None, holdings=(), count=0):
    book = policies() if book is None else book
    account = {
        "schema_version": 1,
        "as_of": STAMP,
        "currency": "USD",
        "balance": D("10000"),
        "equity": D("10000"),
        "margin_used": D("1000"),
        "margin_available": D("9000"),
    }
    snapshot = SnapshotFacts(
        BINDING,
        account,
        {"schema_version": 1, "as_of": STAMP, "positions": [row[1] for row in holdings]},
        tuple(row[0] for row in holdings),
        True,
        True,
        tuple(row[3] for row in holdings),
        STAMP,
        True,
        tuple(row[2] for row in holdings),
        STAMP,
        True,
    )
    evidence = ReconciliationEvidence(
        deepcopy(snapshot),
        snapshot,
        Freshness(timedelta(seconds=30), timedelta(seconds=2)),
        (),
        (),
        (),
        tuple(row[2] for row in holdings),
        tuple(row[4] for row in holdings),
    )
    quote = ProposalFacts("EUR/USD", D("1.1000"), D("1.1002"), STAMP, D("1"), D("40"), conversion())
    facts = RiskFacts(
        "synthetic-account",
        "demo",
        STAMP,
        True,
        True,
        False,
        False,
        START,
        END,
        D("10000"),
        D("0"),
        count,
        quote,
        tuple(row[5] for row in holdings),
    )
    order = {
        "schema_version": 1,
        "client_order_id": "request-one",
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": D("1000"),
        "order_type": "market",
        "stop": D("1.09"),
    }
    return bind(AdmissionRequest(order, facts, evidence, (), 0, "0" * 64), book)


def result(inputs, *, reservations=(), floor=0, now=NOW):
    request, state, book = inputs
    return assess(request, state, book, reservations, floor, now=now)


def refused(value):
    assert value.within_limits is False and value.reservation is None
    assert value.code != "within_limits" and value.dispatch_authorized is False


def reserve(
    request_id="pending-one",
    *,
    session_id="session-one",
    instrument="EUR/USD",
    side="buy",
    units="1000",
    entry="1.10",
    stop="1.09",
    gross="1100",
    loss="10",
    margin="40",
):
    return Reservation(
        request_id,
        session_id,
        instrument,
        side,
        D(units),
        D(entry),
        D(stop),
        instrument.split("/")[1],
        D(gross),
        D(loss),
        D(margin),
    )


def mark(reservation, *, bid="1.10", ask="1.1002", rate="1", margin="40", as_of=STAMP):
    return ReservationMark(
        reservation.request_id,
        D(bid),
        D(ask),
        as_of,
        conversion(reservation.quote_currency, rate, as_of),
        D(margin),
    )


def with_marks(inputs, *marks):
    request, state, book = inputs
    return replace(request, reservation_marks=tuple(marks)), state, book


def test_flat_first_and_subsequent_reservations_have_real_positive_controls_and_no_authority():
    first = result(case())
    assert first.within_limits and first.code == "within_limits" and not first.dispatch_authorized
    assert first.validated_external_count == 0
    assert first.reservation.request_id == "request-one"
    assert first.reservation.base_units == D("1000")
    assert first.reservation.loss_budget == D("10.2")
    assert first.reservation.gross_budget == D("1100.2")
    request, state, book = case()
    later = replace(
        request,
        order={**request.order, "client_order_id": "request-two"},
        reservation_marks=(mark(first.reservation),),
    )
    second = result((later, state, book), reservations=(first.reservation,))
    assert second.within_limits and second.reservation.request_id == "request-two"
    assert not second.dispatch_authorized


def test_protected_external_position_passes_full_size_opposite_side_and_explicit_unit_mapping():
    inputs = case(holdings=(held(),))
    original = encode_request(inputs[0]), encode_state(inputs[1]), encode_policy_book(inputs[2])
    assessment = result(inputs)
    assert assessment.within_limits and assessment.validated_external_count == 0
    assert dict(assessment.calculations)["total_planned_loss"] == D("20.2")
    assert original == (
        encode_request(inputs[0]),
        encode_state(inputs[1]),
        encode_policy_book(inputs[2]),
    )


@pytest.mark.parametrize("field,value", [("side", "buy"), ("size", D("9"))])
def test_matching_but_wrong_side_or_partial_protection_is_not_sufficient(field, value):
    inputs = case(holdings=(held(),))
    request, _, book = inputs
    expected, observed = deepcopy(request.evidence.expected), deepcopy(request.evidence.observed)
    for snapshot in (expected, observed):
        snapshot.order_updates[0][field] = value
        snapshot.protective_orders[0].request[field] = value
    evidence = replace(
        request.evidence,
        expected=expected,
        observed=observed,
        protective_obligations=observed.protective_orders,
    )
    assert reconcile(
        expected,
        observed,
        observed_at=NOW,
        freshness=evidence.freshness,
        commands=(),
        protective_obligations=evidence.protective_obligations,
    ).consistent
    assessment = result(bind(replace(request, evidence=evidence), book))
    refused(assessment)
    assert assessment.validated_external_count is None


@pytest.mark.parametrize(
    "filled", [None, D("1"), 0], ids=["missing-fill-scope", "already-filled", "nondecimal-zero"]
)
def test_matching_working_stop_needs_explicit_zero_fills_for_full_remaining_protection(filled):
    request, _, book = case(holdings=(held(),))
    expected, observed = deepcopy(request.evidence.expected), deepcopy(request.evidence.observed)
    for snapshot in (expected, observed):
        if filled is None:
            snapshot.order_updates[0].pop("filled_size")
        else:
            snapshot.order_updates[0]["filled_size"] = filled
    evidence = replace(request.evidence, expected=expected, observed=observed)
    assert reconcile(
        expected,
        observed,
        observed_at=NOW,
        freshness=evidence.freshness,
        commands=(),
        protective_obligations=evidence.protective_obligations,
    ).consistent
    assessment = result(bind(replace(request, evidence=evidence), book))
    refused(assessment)
    assert assessment.validated_external_count is None


@pytest.mark.parametrize("fault", ["factor", "units", "price", "stop", "instrument"])
def test_external_exposure_mapping_must_match_observed_geometry_not_caller_assertion(fault):
    request, _, book = case(holdings=(held(),))
    exposure = request.facts.exposures[0]
    evidence = request.evidence
    if fault == "factor":
        evidence = replace(
            evidence,
            position_marks=(replace(evidence.position_marks[0], base_units_per_size=D("99")),),
        )
    else:
        changes = {
            "units": {"base_units": D("999")},
            "price": {"reference_price": D("1.299")},
            "stop": {"stop_price": D("1.28")},
            "instrument": {"instrument": "EUR/USD"},
        }
        exposure = replace(exposure, **changes[fault])
    changed = replace(
        request, evidence=evidence, facts=replace(request.facts, exposures=(exposure,))
    )
    assessment = result(bind(changed, book))
    refused(assessment)
    assert assessment.validated_external_count is None


@pytest.mark.parametrize("missing", ["mark", "exposure", "scope", "protection"])
def test_missing_external_evidence_is_unknown_not_flat_or_protected(missing):
    request, _, book = case(holdings=(held(),))
    evidence, facts = request.evidence, request.facts
    if missing == "mark":
        evidence = replace(evidence, position_marks=())
    elif missing == "exposure":
        facts = replace(facts, exposures=())
    elif missing == "scope":
        evidence = replace(evidence, observed=replace(evidence.observed, positions_complete=None))
    else:
        evidence = replace(evidence, observed=replace(evidence.observed, protective_orders=()))
    assessment = result(bind(replace(request, evidence=evidence, facts=facts), book))
    refused(assessment)
    assert assessment.validated_external_count is None


@pytest.mark.parametrize("stale", ["quote", "conversion"])
def test_nonselected_held_instrument_uses_its_own_stricter_freshness(stale):
    book = tuple(
        replace(p, quote_max_age_seconds=5, conversion_max_age_seconds=5)
        if p.instrument == "GBP/USD"
        else replace(p, quote_max_age_seconds=60, conversion_max_age_seconds=60)
        for p in policies()
    )
    fresh_now = datetime.fromisoformat(STAMP.replace("Z", "+00:00")) + timedelta(seconds=1)
    positive = case(book=book, holdings=(held(),))
    assert result(positive, now=fresh_now).within_limits
    request, _, _ = positive
    old = "2026-10-21T11:59:40Z"
    exposure = request.facts.exposures[0]
    if stale == "quote":
        exposure = replace(exposure, as_of=old)
        evidence = replace(
            request.evidence,
            position_marks=(replace(request.evidence.position_marks[0], as_of=old),),
        )
    else:
        exposure = replace(exposure, conversion=conversion(as_of=old))
        evidence = request.evidence
    request = replace(
        request, facts=replace(request.facts, exposures=(exposure,)), evidence=evidence
    )
    assessment = result(bind(request, book), now=fresh_now)
    refused(assessment)
    assert assessment.validated_external_count is None


def test_nonselected_instrument_combines_held_and_reserved_units_at_exact_cap():
    book = tuple(
        replace(p, max_instrument_base_units=D("1500")) if p.instrument == "GBP/USD" else p
        for p in policies()
    )
    inputs = case(book=book, holdings=(held(),))
    pending = reserve(
        instrument="GBP/USD",
        units="500",
        entry="1.30",
        stop="1.29",
        gross="650",
        loss="5",
        margin="20",
    )
    inputs = with_marks(inputs, mark(pending, bid="1.30", ask="1.3002", margin="20"))
    assert result(inputs, reservations=(pending,)).within_limits
    request, _, _ = inputs
    lower = tuple(
        replace(p, max_instrument_base_units=D("1499.999999")) if p.instrument == "GBP/USD" else p
        for p in book
    )
    refused(result(bind(request, lower), reservations=(pending,)))


def test_caller_session_and_latch_fields_cannot_clear_a_retained_historical_breach():
    request, _, book = case()
    request = replace(
        request,
        facts=replace(
            request.facts,
            daily_loss_latched=False,
            session_start_equity=D("1"),
            external_cash_flow=D("9999"),
            session_started_at="2026-10-21T11:00:00Z",
            session_ends_at="2026-10-23T00:00:00Z",
        ),
    )
    bound = bind(request, book, historical_breach=True)
    assert bound[1].daily_loss == D("0") and bound[1].daily_loss_latched
    assessment = result(bound)
    refused(assessment)
    assert assessment.effective_session_latched and assessment.effective_session_daily_loss == D(
        "0"
    )
    assert assessment.validated_external_count == 0


def test_session_freshness_and_completeness_cannot_be_replaced_by_fresh_request_facts():
    request, state, book = case()
    assert result((request, state, book)).within_limits
    stale = result(bind(request, book, snapshot_age=5))
    refused(stale)
    assert stale.validated_external_count is None
    incomplete, _ = advance(
        initial_state(state.spec), replace(state.latest, account_complete=False), now=NOW
    )
    matched = replace(request, expected_observation_sha256=incomplete.latest_sha256)
    missing = result((matched, incomplete, book))
    refused(missing)
    assert missing.validated_external_count is None


def test_reserved_loss_counts_in_daily_risk_without_becoming_realized_daily_loss():
    book = policies(max_daily_risk=D("115.2"))
    request, _, _ = case(book=book)
    expected, observed = deepcopy(request.evidence.expected), deepcopy(request.evidence.observed)
    for snapshot in (expected, observed):
        snapshot.account.update(balance=D("9905"), equity=D("9905"), margin_available=D("8905"))
    request = replace(
        request, evidence=replace(request.evidence, expected=expected, observed=observed)
    )
    pending = reserve()
    request = replace(request, reservation_marks=(mark(pending),))
    positive = result(bind(request, book), reservations=(pending,))
    assert positive.within_limits and positive.effective_session_daily_loss == D("95")
    lower = caps(book, max_daily_risk=D("115.199999999"))
    refused(result(bind(request, lower), reservations=(pending,)))


@pytest.mark.parametrize("changed", ["revision", "observation", "book", "checkpoint"])
def test_stale_or_wrong_source_session_book_identity_cannot_reserve(changed):
    request, state, book = case()
    if changed == "revision":
        request = replace(request, expected_session_revision=0)
    elif changed == "observation":
        request = replace(request, expected_observation_sha256="f" * 64)
    elif changed == "book":
        book = caps(book, max_gross_exposure=D("9999"))
    else:
        request = replace(request, facts=replace(request.facts, orders_this_session=1))
    assessment = result((request, state, book))
    refused(assessment)
    assert assessment.validated_external_count is None


def test_validated_count_survives_risk_refusal_but_regression_and_unknown_source_do_not():
    request, state, book = case(count=8)
    kill = replace(request, facts=replace(request.facts, kill_switch=True))
    assessment = result(bind(kill, book), floor=7)
    refused(assessment)
    assert assessment.validated_external_count == 8
    regression = result(case(count=7), floor=8)
    refused(regression)
    assert regression.validated_external_count is None
    invalid = replace(
        request,
        evidence=replace(
            request.evidence, observed=replace(request.evidence.observed, account_complete=False)
        ),
    )
    unknown = result(bind(invalid, book), floor=7)
    refused(unknown)
    assert unknown.validated_external_count is None


def test_old_session_reservation_still_charges_margin_and_gross_but_not_current_order_count():
    book = policies(max_session_orders=1, min_margin_available=D("8920"))
    pending = reserve(session_id="prior-session", gross="1100.2")
    inputs = with_marks(case(book=book), mark(pending))
    assert result(inputs, reservations=(pending,)).within_limits
    request, _, _ = inputs
    tight = caps(book, min_margin_available=D("8920.00000001"))
    refused(result(bind(request, tight), reservations=(pending,)))
    current = replace(pending, session_id="session-one")
    refused(result(inputs, reservations=(current,)))


def test_opposite_reservations_are_gross_not_netted():
    long = reserve("pending-long", units="100", gross="110", loss="1", margin="0")
    short = reserve(
        "pending-short",
        side="sell",
        units="100",
        entry="1.10",
        stop="1.11",
        gross="110",
        loss="1",
        margin="0",
    )
    request, _, book = with_marks(
        case(), mark(long, ask="1.10", margin="0"), mark(short, ask="1.10", margin="0")
    )
    exact = caps(book, max_gross_exposure=D("1320.2"))
    assert result(bind(request, exact), reservations=(long, short)).within_limits
    lower = caps(book, max_gross_exposure=D("1320.199999999"))
    refused(result(bind(request, lower), reservations=(long, short)))


@pytest.mark.parametrize(
    "saved_gross,gross_cap,loss_cap,margin_floor",
    [
        ("150", "1420.2", "20.2", "8930"),
        ("500", "1600.2", "20.2", "8930"),
    ],
)
def test_reservation_revaluation_keeps_saved_lower_bounds_and_fresh_rates_margins(
    saved_gross, gross_cap, loss_cap, margin_floor
):
    book = policies(
        "EUR/JPY",
        "EUR/USD",
        "GBP/USD",
        max_gross_exposure=D(gross_cap),
        max_total_planned_loss=D(loss_cap),
        min_margin_available=D(margin_floor),
    )
    pending = reserve(
        instrument="EUR/JPY",
        units="100",
        entry="150",
        stop="145",
        gross=saved_gross,
        loss="5",
        margin="20",
    )
    request, _, _ = with_marks(
        case(book=book), mark(pending, bid="159", ask="160", rate="0.02", margin="30")
    )
    assert result(bind(request, book), reservations=(pending,)).within_limits
    for field, value in (
        ("max_gross_exposure", D(gross_cap) - D("0.0000001")),
        ("max_total_planned_loss", D(loss_cap) - D("0.0000001")),
        ("min_margin_available", D(margin_floor) + D("0.0000001")),
    ):
        refused(result(bind(request, caps(book, **{field: value})), reservations=(pending,)))


@pytest.mark.parametrize("fault", ["absent", "stale", "conversion", "geometry"])
def test_existing_reservation_cannot_be_assessed_without_usable_own_mark(fault):
    pending = reserve()
    inputs = with_marks(case(), mark(pending))
    request, state, book = inputs
    if fault == "absent":
        request = replace(request, reservation_marks=())
    elif fault == "stale":
        request = replace(request, reservation_marks=(mark(pending, as_of="2026-10-21T11:58:00Z"),))
    elif fault == "conversion":
        bad = replace(mark(pending), conversion=Conversion("EUR", "USD", D("1"), STAMP))
        request = replace(request, reservation_marks=(bad,))
    else:
        request = replace(request, reservation_marks=(mark(pending, bid="1.2", ask="1.1"),))
        with pytest.raises(AdmissionRefused):
            result((request, state, book), reservations=(pending,))
        return
    refused(result((request, state, book), reservations=(pending,)))


def test_unused_extra_marks_do_not_create_phantom_reservations_or_consume_budgets():
    extra = reserve("not-durably-reserved", units="100000", gross="1000000")
    exact = policies(max_session_orders=1, min_margin_available=D("8960"))
    assert result(with_marks(case(book=exact), mark(extra))).within_limits


def test_capacity_999_can_reserve_1000_but_existing_1000_cannot_reserve_1001():
    book = policies(max_session_orders=2000)
    pending = tuple(
        reserve(
            f"r{i}",
            units="0.0001",
            entry="1",
            stop="0.9",
            gross="0.0001",
            loss="0.00001",
            margin="0",
        )
        for i in range(1000)
    )
    marks = tuple(mark(row, bid="1", ask="1", margin="0") for row in pending)
    request, state, book = with_marks(case(book=book), *marks)
    assert len(encode_request(request).encode()) < MAX_REQUEST_BYTES
    accepted = result((request, state, book), reservations=pending[:999])
    assert accepted.within_limits and accepted.reservation is not None
    refused(result((request, state, book), reservations=pending))


def decimal_settings():
    context = getcontext()
    return (
        context.prec,
        context.Emin,
        context.Emax,
        context.rounding,
        dict(context.traps),
        dict(context.flags),
    )


def test_exact_accounting_and_codecs_do_not_change_trapping_ambient_decimal_context_or_callers():
    inputs = case(holdings=(held(),))
    original = encode_request(inputs[0]), encode_state(inputs[1]), encode_policy_book(inputs[2])
    with localcontext() as context:
        context.prec, context.Emin, context.Emax, context.rounding = 3, -2, 2, ROUND_UP
        context.traps[Inexact] = context.traps[Rounded] = True
        context.clear_flags()
        before = decimal_settings()
        assessment = result(inputs)
        assert assessment.within_limits
        assert assessment.reservation.loss_budget == D("10.2")
        assert decode_request(encode_request(inputs[0])) == inputs[0]
        assert decimal_settings() == before
    assert original == (
        encode_request(inputs[0]),
        encode_state(inputs[1]),
        encode_policy_book(inputs[2]),
    )


def test_typed_codecs_preserve_containers_decimal_representation_and_exact_policy_digest():
    request, state, book = case()
    request.order["size"] = D("1E+3")
    text = encode_request(request)
    restored = decode_request(text)
    assert type(restored.order) is dict and type(restored.evidence.commands) is tuple
    assert type(restored.evidence.observed.positions["positions"]) is list
    assert restored.order["size"].as_tuple() == D("1E+3").as_tuple()
    assert encode_request(restored) == text
    assert encode_policy_book(decode_policy_book(encode_policy_book(book))) == encode_policy_book(
        book
    )
    changed = tuple(
        replace(p, max_order_base_units=D("2000.0")) if p.instrument == "EUR/USD" else p
        for p in book
    )
    assert policy_book_digest(changed) != policy_book_digest(book)
    assessment = result((request, state, book))
    assert assessment.within_limits
    projection = assessment.reservation
    assert encode_reservation(
        decode_reservation(encode_reservation(projection))
    ) == encode_reservation(projection)
    receipt = {
        "schema_version": 1,
        "request_id": request.order["client_order_id"],
        "account_id": BINDING.account_id,
        "environment": "demo",
        "currency": "USD",
        "session_id": "session-one",
        "request_sha256": sha256(text.encode()).hexdigest(),
        "policy_book_sha256": policy_book_digest(book),
        "assessment": assessment,
        "lease_authority_epoch": "11111111-1111-1111-1111-111111111111",
        "lease_owner_id": "synthetic-owner",
        "lease_generation": 1,
        "lease_resource": "synthetic-resource",
        "observed_at": NOW.isoformat(),
        "dispatch_authorized": False,
    }
    assert encode_receipt(decode_receipt(encode_receipt(receipt))) == encode_receipt(receipt)
    with pytest.raises(AdmissionRefused):
        encode_receipt({**receipt, "dispatch_authorized": True})
    with pytest.raises(AdmissionRefused):
        decode_request(" " + text)


def test_custom_containers_floats_and_cycles_refuse_before_any_custom_hook():
    class Evil(dict):
        def items(self):
            raise AssertionError("custom items hook")

        def __deepcopy__(self, memo):
            raise AssertionError("custom copy hook")

    request, state, book = case()
    for changed in (
        replace(request, order=Evil(request.order)),
        replace(request, order={**request.order, "size": 1000.0}),
    ):
        with pytest.raises(AdmissionRefused):
            assess(changed, state, book, (), 0, now=NOW)
    cycle = {}
    cycle["cycle"] = cycle
    with pytest.raises(AdmissionRefused):
        encode_request(replace(request, order=cycle))
