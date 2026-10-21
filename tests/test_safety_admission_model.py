"""Pure admission representation and concrete positive budget invariants."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from decimal import getcontext, localcontext

import pytest

from pipmind.reconciliation import Binding, Freshness, ProtectiveOrder, SnapshotFacts, reconcile
from pipmind.safety.admission import (
    MAX_COUNT,
    MAX_POLICY_BOOK_BYTES,
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
from pipmind.safety.boundary import Conversion, Exposure, ProposalFacts, RiskFacts, RiskPolicy
from pipmind.safety.session import (
    EquityObservation,
    SessionBinding,
    SessionSpec,
    advance,
    fingerprint,
    initial_state,
)

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
STAMP = "2026-10-21T12:00:00Z"
START = "2026-10-21T00:00:00Z"
END = "2026-10-22T00:00:00Z"


def make_case(*, caps=None, request_id="proposal", count=0):
    policy = RiskPolicy(
        "explicit-policy",
        "synthetic-account",
        "demo",
        "USD",
        "EUR/USD",
        D("1000"),
        D("10000"),
        D("20000"),
        D("100"),
        D("1000"),
        D("1000"),
        D("2000"),
        D("0"),
        10000,
        60,
        30,
        30,
    )
    book = (replace(policy, **(caps or {})),)
    account = {
        "schema_version": 1,
        "as_of": STAMP,
        "currency": "USD",
        "balance": D("10000"),
        "equity": D("10000"),
        "margin_used": D("1000"),
        "margin_available": D("9000"),
    }
    observed = SnapshotFacts(
        Binding("synthetic-account", "demo"),
        account,
        {"schema_version": 1, "as_of": STAMP, "positions": []},
        (),
        True,
        True,
        (),
        STAMP,
        True,
        (),
        STAMP,
        True,
    )
    evidence = ReconciliationEvidence(
        deepcopy(observed),
        observed,
        Freshness(timedelta(seconds=30), timedelta(seconds=1)),
        (),
        (),
        (),
        (),
        (),
    )
    rate = Conversion("USD", "USD", D("1"), STAMP)
    proposal = ProposalFacts("EUR/USD", D("1.10"), D("1.1002"), STAMP, D("1"), D("40"), rate)
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
        proposal,
        (),
    )
    order = {
        "schema_version": 1,
        "client_order_id": request_id,
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": D("1000"),
        "order_type": "market",
        "stop": D("1.09"),
    }
    request = AdmissionRequest(order, facts, evidence, (), 0, "0" * 64)
    spec = SessionSpec(
        SessionBinding("synthetic-account", "demo", "USD"),
        "session",
        book[0].policy_id,
        policy_book_digest(book),
        START,
        END,
        D("10000"),
        "opening",
        book[0].max_daily_loss,
        60,
    )
    snapshot = EquityObservation(
        spec.binding,
        spec.session_id,
        "snapshot",
        STAMP,
        STAMP,
        D("10000"),
        D("0"),
        True,
        True,
        START,
        STAMP,
        checkpoint_digest(evidence, facts),
        "flow",
    )
    state, _ = advance(initial_state(spec), snapshot, now=NOW)
    return (
        replace(
            request,
            expected_session_revision=state.revision,
            expected_observation_sha256=state.latest_sha256,
        ),
        state,
        book,
    )


def pending(
    request_id="older",
    *,
    session_id="session",
    units="1000",
    gross="1100.2",
    loss="10.2",
    margin="40",
    side="buy",
):
    return Reservation(
        request_id,
        session_id,
        "EUR/USD",
        side,
        D(units),
        D("1.1002"),
        D("1.09") if side == "buy" else D("1.11"),
        "USD",
        D(gross),
        D(loss),
        D(margin),
    )


def mark(row, *, ask="1.1002", margin="40"):
    return ReservationMark(
        row.request_id, D("1.10"), D(ask), STAMP, Conversion("USD", "USD", D("1"), STAMP), D(margin)
    )


def run(values, reservations=(), floor=0):
    request, state, book = values
    return assess(request, state, book, reservations, floor, now=NOW)


def test_exact_positive_limits_return_named_calculations_and_no_dispatch_authority():
    values = make_case(
        caps={
            "max_gross_exposure": D("1100.2"),
            "max_planned_order_loss": D("10.2"),
            "max_total_planned_loss": D("10.2"),
            "max_daily_risk": D("10.2"),
            "max_session_orders": 1,
            "min_margin_available": D("8960"),
        }
    )
    result = run(values)
    assert result.within_limits and result.code == "within_limits"
    assert result.dispatch_authorized is False and result.reservation is not None
    assert result.validated_external_count == 0
    calculations = dict(result.calculations)
    assert calculations["gross_exposure"] == result.reservation.gross_budget == D("1100.2")
    assert calculations["total_planned_loss"] == result.reservation.loss_budget == D("10.2")
    assert calculations["remaining_margin"] == D("8960")
    assert result.reconciliation_findings == ()
    with pytest.raises(FrozenInstanceError):
        result.code = "changed"


@pytest.mark.parametrize(
    "cap,value,code",
    [
        ("max_gross_exposure", "2200.39", "gross_exposure_limit"),
        ("max_total_planned_loss", "20.39", "total_loss_limit"),
        ("max_daily_risk", "20.39", "daily_risk_limit"),
        ("min_margin_available", "8920.01", "insufficient_margin"),
    ],
)
def test_genuine_existing_reservation_makes_combined_limit_refuse(cap, value, code):
    request, state, book = make_case(caps={cap: D(value)})
    row = pending()
    request = replace(request, reservation_marks=(mark(row),))
    result = run((request, state, book), (row,))
    assert not result.within_limits and result.code == code
    assert result.validated_external_count == 0 and result.reservation is None


def test_subsequent_funded_reservation_and_old_session_count_are_distinct():
    request, state, book = make_case(caps={"max_session_orders": 1})
    row = pending(session_id="previous-session")
    request = replace(request, reservation_marks=(mark(row),))
    result = run((request, state, book), (row,))
    assert result.within_limits
    assert dict(result.calculations)["gross_exposure"] == D("2200.4")
    assert dict(result.calculations)["reserved_opens_this_session"] == 0
    same_day = replace(row, session_id=state.spec.session_id)
    refused = run((request, state, book), (same_day,))
    assert refused.code == "session_order_limit" and not refused.within_limits


def test_larger_fresh_reservation_price_and_margin_never_reduce_saved_budgets():
    request, state, book = make_case()
    row = pending(gross="2000", loss="50", margin="300")
    request = replace(request, reservation_marks=(mark(row, ask="1.2", margin="10"),))
    result = run((request, state, book), (row,))
    assert result.within_limits
    metrics = dict(result.calculations)
    assert metrics["reserved_gross_exposure"] == 2000
    assert metrics["reserved_planned_loss"] == 50
    assert metrics["reserved_margin"] == 300
    assert metrics["remaining_margin"] == 8660


def test_checkpoint_hash_ignores_proposal_and_untrusted_session_fields_but_keeps_source_count():
    request, _, _ = make_case()
    facts = request.facts
    changed = replace(
        facts,
        proposal=replace(facts.proposal, margin_required=D("100")),
        daily_loss_latched=True,
        session_start_equity=D("123"),
    )
    assert checkpoint_digest(request.evidence, facts) == checkpoint_digest(
        request.evidence, changed
    )
    assert checkpoint_digest(request.evidence, facts) != checkpoint_digest(
        request.evidence, replace(facts, orders_this_session=1)
    )


def test_source_count_is_retained_on_kill_refusal_but_not_checkpoint_mismatch():
    request, state, book = make_case(count=2)
    # A matching coherent checkpoint for a kill flag is created using the real
    # session initializer, rather than spoofing an already-bound evidence hash.
    facts = replace(request.facts, kill_switch=True)
    spec = state.spec
    snapshot = replace(state.latest, account_evidence_id=checkpoint_digest(request.evidence, facts))
    stopped, _ = advance(initial_state(spec), snapshot, now=NOW)
    request = replace(request, facts=facts, expected_observation_sha256=stopped.latest_sha256)
    result = run((request, stopped, book))
    assert result.code == "kill_switch_active" and result.validated_external_count == 2
    unbound = run((request, state, book))
    assert not unbound.within_limits and unbound.validated_external_count is None


def test_caller_mutation_cannot_change_frozen_results_or_reservation_geometry():
    values = make_case()
    before = encode_request(values[0]), encode_policy_book(values[2])
    result = run(values)
    assert (encode_request(values[0]), encode_policy_book(values[2])) == before
    values[0].order["size"] = D("1")
    assert result.reservation.base_units == 1000


@pytest.mark.parametrize(
    "field",
    [
        "max_session_orders",
        "snapshot_max_age_seconds",
        "quote_max_age_seconds",
        "conversion_max_age_seconds",
    ],
)
def test_oversized_policy_integer_refuses_before_timedelta_or_sql(field):
    _, _, book = make_case()
    bound = MAX_COUNT if field == "max_session_orders" else 2147483647
    with pytest.raises(AdmissionRefused, match="admission_policy_capacity"):
        encode_policy_book((replace(book[0], **{field: bound + 1}),))


def test_policy_book_rejects_unsorted_duplicates_and_cross_pair_global_cap_bypass():
    _, _, one = make_case()
    second = replace(one[0], instrument="GBP/USD")
    valid = (one[0], second)
    assert decode_policy_book(encode_policy_book(valid)) == valid
    for invalid in (
        (second, one[0]),
        (one[0], one[0]),
        (one[0], replace(second, max_daily_loss=D("1"))),
    ):
        with pytest.raises(AdmissionRefused):
            encode_policy_book(invalid)
    assert policy_book_digest(valid) != policy_book_digest(
        (replace(one[0], max_daily_loss=D("1000.0")), replace(second, max_daily_loss=D("1000.0")))
    )


def test_request_codecs_preserve_container_decimal_and_timestamp_representation():
    request, _, book = make_case()
    decoded = decode_request(encode_request(request))
    assert decoded == request and decoded is not request
    assert type(decoded.evidence.observed.positions["positions"]) is list
    assert type(decoded.evidence.observed.position_ids) is tuple
    assert str(decoded.facts.proposal.bid) == "1.10"
    assert policy_book_digest(book) == fingerprint(encode_policy_book(book))
    row = pending()
    assert decode_reservation(encode_reservation(row)) == row
    assert encode_reservation(row) != encode_reservation(replace(row, margin_budget=D("40.00")))


def test_custom_hooks_and_raw_float_cannot_run_through_request_freeze():
    class Evil(dict):
        def items(self):
            raise AssertionError("hook invoked")

        def __deepcopy__(self, memo):
            raise AssertionError("copy hook invoked")

    request, _, _ = make_case()
    for changed in (
        replace(request, order=Evil(request.order)),
        replace(
            request, facts=replace(request.facts, proposal=replace(request.facts.proposal, bid=1.1))
        ),
    ):
        with pytest.raises(AdmissionRefused):
            encode_request(changed)
    cycle = []
    cycle.append(cycle)
    request.order["unexpected"] = cycle
    with pytest.raises(AdmissionRefused, match="admission_cyclic_input"):
        encode_request(request)


def test_malformed_codec_and_decode_do_not_expose_values_or_change_ambient_decimal_flags():
    request, _, _ = make_case()
    body = encode_request(request)
    for invalid in (
        body.encode(),
        " " + body,
        body.replace('"record"', '"UNKNOWN_PRIVATE_TAG"', 1),
        "x" * (MAX_REQUEST_BYTES + 1),
    ):
        with pytest.raises(AdmissionRefused) as raised:
            decode_request(invalid)
        assert "UNKNOWN_PRIVATE_TAG" not in str(raised.value)
    with localcontext() as context:
        context.prec, context.Emin, context.Emax = 2, -2, 2
        context.clear_flags()
        before = dict(context.flags), dict(context.traps), context.prec, context.Emin, context.Emax
        with pytest.raises(AdmissionRefused):
            decode_request(body.replace('"1.10"', '"BAD_PRIVATE_DECIMAL"', 1))
        after = getcontext()
        assert (dict(after.flags), dict(after.traps), after.prec, after.Emin, after.Emax) == before


def test_private_receipt_roundtrip_refuses_raw_token_or_authority_true():
    values = make_case()
    request, state, book = values
    receipt = {
        "schema_version": 1,
        "request_id": request.order["client_order_id"],
        "account_id": state.spec.binding.account_id,
        "environment": "demo",
        "currency": "USD",
        "session_id": state.spec.session_id,
        "request_sha256": fingerprint(encode_request(request)),
        "policy_book_sha256": policy_book_digest(book),
        "assessment": run(values),
        "lease_authority_epoch": "trusted-epoch",
        "lease_owner_id": "owner",
        "lease_generation": 1,
        "lease_resource": "execution",
        "observed_at": STAMP,
        "dispatch_authorized": False,
    }
    assert decode_receipt(encode_receipt(receipt)) == receipt
    for invalid in (
        {**receipt, "raw_token": "PRIVATE_TOKEN"},
        {**receipt, "dispatch_authorized": True},
    ):
        with pytest.raises(AdmissionRefused) as raised:
            encode_receipt(invalid)
        assert "PRIVATE_TOKEN" not in str(raised.value)


def test_large_policy_and_request_codecs_enforce_exact_technical_byte_bounds():
    request, _, book = make_case()
    assert len(encode_request(request).encode()) < MAX_REQUEST_BYTES
    assert len(encode_policy_book(book).encode()) < MAX_POLICY_BOOK_BYTES
    oversized = replace(request, order={**request.order, "client_order_id": "x" * 129})
    with pytest.raises(AdmissionRefused):
        encode_request(oversized)


def held_case(action):
    request, old, book = make_case()
    book = (*book, replace(book[0], instrument="GBP/USD"))
    protective_request = {
        "schema_version": 1,
        "client_order_id": "observed-stop",
        "action": action,
        "instrument": "GBP/USD",
        "side": "sell",
        "size": D("10"),
        "stop": D("1.29"),
    }
    if action == "open":
        protective_request["order_type"] = "market"
    protection = ProtectiveOrder("held", "broker-stop", protective_request)
    update = {
        "schema_version": 1,
        "client_order_id": "observed-stop",
        "broker_order_id": "broker-stop",
        "status": "working",
        "instrument": "GBP/USD",
        "side": "sell",
        "size": D("10"),
        "filled_size": D("0"),
        "ts": STAMP,
    }
    observed = replace(
        request.evidence.observed,
        positions={
            "schema_version": 1,
            "as_of": STAMP,
            "positions": [
                {"instrument": "GBP/USD", "side": "buy", "size": D("10")},
            ],
        },
        position_ids=("held",),
        order_updates=(update,),
        protective_orders=(protection,),
    )
    evidence = replace(
        request.evidence,
        observed=observed,
        expected=deepcopy(observed),
        protective_obligations=(deepcopy(protection),),
        position_marks=(PositionMark("held", D("1.30"), D("1.3002"), STAMP, D("100")),),
    )
    facts = replace(
        request.facts,
        exposures=(
            Exposure(
                "held",
                "GBP/USD",
                "buy",
                D("1000"),
                D("1.30"),
                D("1.29"),
                STAMP,
                Conversion("USD", "USD", D("1"), STAMP),
            ),
        ),
    )
    spec = replace(old.spec, policy_sha256=policy_book_digest(book))
    checkpoint = replace(old.latest, account_evidence_id=checkpoint_digest(evidence, facts))
    state, _ = advance(initial_state(spec), checkpoint, now=NOW)
    return (
        replace(
            request, evidence=evidence, facts=facts, expected_observation_sha256=state.latest_sha256
        ),
        state,
        book,
    )


@pytest.mark.parametrize("action", ["modify", "close", "open"])
def test_protective_instruction_role_is_verified_beyond_matching_reconciliation(action):
    request, state, book = held_case(action)
    evidence = request.evidence
    assert reconcile(
        evidence.expected,
        evidence.observed,
        observed_at=NOW,
        freshness=evidence.freshness,
        commands=evidence.commands,
        command_requests=evidence.command_requests,
        pending_command_ids=evidence.pending_command_ids,
        protective_obligations=evidence.protective_obligations,
    ).consistent
    result = run((request, state, book))
    if action == "open":
        assert not result.within_limits and result.code == "unsupported_protection_action"
        assert result.validated_external_count is None
    else:
        assert result.within_limits and result.validated_external_count == 0


def test_source_schema_discrepancies_retain_actual_findings_without_advancing_count():
    request, old, book = make_case(count=5)
    observed = replace(
        request.evidence.observed,
        account={
            **request.evidence.observed.account,
            "balance": "unknown",
        },
    )
    evidence = replace(request.evidence, expected=deepcopy(observed), observed=observed)
    request = replace(request, evidence=evidence)
    snapshot = replace(old.latest, account_evidence_id=checkpoint_digest(evidence, request.facts))
    state, _ = advance(initial_state(old.spec), snapshot, now=NOW)
    result = run((replace(request, expected_observation_sha256=state.latest_sha256), state, book))
    assert not result.within_limits and result.code == "reconciliation_failed"
    assert result.validated_external_count is None
    assert any(
        row.code == "invalid" and row.subject == "observed.account"
        for row in result.reconciliation_findings
    )
