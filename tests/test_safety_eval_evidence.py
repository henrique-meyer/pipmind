"""Pure consistency counterexamples; these fixtures do not pretend to execute SQL."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pipmind.safety.admission import (
    assess,
    decode_policy_book,
    decode_receipt,
    decode_request,
    encode_receipt,
    encode_request,
    encode_reservation,
    policy_book_digest,
)
from pipmind.safety.eval_evidence import (
    CASE_IDS,
    MAX_REPORT_BYTES,
    REQUIRED_TABLES,
    CaseEvidence,
    DurableSnapshot,
    Observation,
    TableDigest,
    canonical_json,
    fingerprint,
    grade_case,
    sanitized_report,
)
from pipmind.safety.eval_scenarios import captured_order, load_scenarios, prepare_scenario
from pipmind.safety.session import (
    advance,
    decode_observation,
    decode_spec,
    decode_state,
    encode_state,
    initial_state,
)
from pipmind.safety.session import (
    encode_receipt as encode_session_receipt,
)

NOW = datetime(2026, 10, 22, 3, 30, tzinfo=UTC)
RESERVATIONS = "dbo.pm_safety_admission_reservations_v1"
ADMISSIONS = "dbo.pm_safety_admission_journal_v1"
INTEGRATION = "dbo.pm_safety_integration_journal_v1"


def _tables(*, reservations=0, admissions=0, integration=0):
    counts = {
        RESERVATIONS: reservations,
        ADMISSIONS: admissions,
        INTEGRATION: integration,
        "dbo.pm_safety_admission_floors_v1": 0,
    }
    return tuple(
        TableDigest(name, counts.get(name, 1), fingerprint(f"{name}:{counts.get(name, 1)}"))
        for name in REQUIRED_TABLES
    )


def _admission_body(request, state, lease, assessment, policies):
    return encode_receipt(
        {
            "schema_version": 1,
            "request_id": request.order["client_order_id"],
            "account_id": state.spec.binding.account_id,
            "environment": "demo",
            "currency": "USD",
            "session_id": state.spec.session_id,
            "request_sha256": fingerprint(encode_request(request)),
            "policy_book_sha256": policy_book_digest(policies),
            "assessment": assessment,
            "lease_authority_epoch": lease["authority_epoch"],
            "lease_owner_id": lease["owner_id"],
            "lease_generation": lease["generation"],
            "lease_resource": lease["resource"],
            "observed_at": "2026-10-22T03:30:00Z",
            "dispatch_authorized": False,
        }
    )


def _observation(spec, label):
    """Construct a coherent fake SQL observation from the actual pure boundary."""
    prepared = prepare_scenario(
        spec,
        sql_now=NOW,
        account_id=f"private-{spec.case_id}-{label}",
        session_id="private-session",
        variant=label,
    )
    request = decode_request(prepared.request_body)
    state = initial_state(decode_spec(prepared.session_spec_body))
    setups = []
    for body in prepared.observation_bodies:
        state, receipt = advance(state, decode_observation(body), now=NOW)
        setups.append(encode_session_receipt(receipt))
    policies = decode_policy_book(prepared.policy_book_body)
    assessment = assess(request, state, policies, (), 0, now=NOW)
    controls = {
        "account_id": state.spec.binding.account_id,
        "environment": "demo",
        "currency": "USD",
        "revision": 1,
        "paused": label == "pause",
        "killed": label == "kill",
    }
    lease = {
        "account_id": state.spec.binding.account_id,
        "environment": "demo",
        "resource": "dry",
        "authority_epoch": "synthetic-epoch",
        "generation": 2 if spec.case_id == "S07" and label == "target" else 1,
        "owner_id": "new" if spec.case_id == "S07" and label == "target" else "old",
        "expires_at": "2026-10-22T03:40:00Z",
    }
    attempted = dict(lease)
    if spec.case_id == "S07" and label == "target":
        attempted.update(generation=1, owner_id="old")
    ready_scope = {
        "authority_epoch": lease["authority_epoch"],
        "owner_id": lease["owner_id"],
        "generation": lease["generation"],
        "resource": lease["resource"],
        "session_id": state.spec.session_id,
        "policy_book_sha256": policy_book_digest(policies),
        "mapping_sha256": fingerprint("mapping"),
        "control_revision": controls["revision"],
    }
    before = DurableSnapshot(
        _tables(),
        canonical_json(controls),
        encode_state(state),
        canonical_json(lease),
        canonical_json({"present": True, "hold_code": "ready", "scope": ready_scope}),
    )
    kind = "admission" if spec.case_id in ("S02", "S03", "S04", "S05") else "gateway"
    raw_request = prepared.request_body
    if kind == "admission":
        receipt = {
            "schema_version": 1,
            "request_id": request.order["client_order_id"],
            "account_id": state.spec.binding.account_id,
            "environment": "demo",
            "currency": "USD",
            "session_id": state.spec.session_id,
            "request_sha256": fingerprint(raw_request),
            "policy_book_sha256": policy_book_digest(policies),
            "assessment": assessment,
            "lease_authority_epoch": lease["authority_epoch"],
            "lease_owner_id": lease["owner_id"],
            "lease_generation": lease["generation"],
            "lease_resource": lease["resource"],
            "observed_at": "2026-10-22T03:30:00Z",
            "dispatch_authorized": False,
        }
        body = encode_receipt(receipt)
        after = replace(before, tables=_tables(reservations=int(label == "positive"), admissions=1))
    else:
        raw_request = canonical_json(
            {
                "request": raw_request,
                "session_id": state.spec.session_id,
                "book": policy_book_digest(policies),
                "mapping": fingerprint("mapping"),
            }
        )
        if label == "target":
            body, after = None, before
        else:
            receipt = {
                "schema_version": 1,
                "channel": "accept",
                "record_id": request.order["client_order_id"],
                "account_id": state.spec.binding.account_id,
                "environment": "demo",
                "currency": "USD",
                "session_id": state.spec.session_id,
                "request_sha256": fingerprint(raw_request),
                "scope": {
                    "owner_id": lease["owner_id"],
                    "generation": lease["generation"],
                    "authority_epoch": lease["authority_epoch"],
                    "resource": lease["resource"],
                    "session_id": state.spec.session_id,
                    "policy_book_sha256": policy_book_digest(policies),
                    "mapping_sha256": fingerprint("mapping"),
                    "control_revision": controls["revision"],
                },
                "control_revision": controls["revision"],
                "observed_at": "2026-10-22T03:30:00Z",
                "assessment": None
                if label != "positive"
                else {
                    "within_limits": assessment.within_limits,
                    "code": assessment.code,
                    "session_revision": assessment.session_revision,
                    "observation_sha256": assessment.observation_sha256,
                    "checkpoint_sha256": assessment.checkpoint_sha256,
                },
                "outcome": "dry_accepted" if label == "positive" else "refused",
                "code": "within_limits"
                if label == "positive"
                else "trading_paused"
                if label == "pause"
                else "kill_switch",
                "dispatch_authorized": False,
                "dispatch_enabled": False,
                "historical_receipt_only": True,
            }
            body = canonical_json(receipt)
            after = replace(
                before,
                tables=_tables(
                    reservations=int(label == "positive"),
                    admissions=int(label == "positive"),
                    integration=1,
                ),
            )
    return Observation(
        label,
        captured_order(prepared.request_body),
        raw_request,
        body,
        body,
        body,
        "lease_stale_owner" if spec.case_id == "S07" and label == "target" else None,
        before,
        after,
        after,
        kind,
        tuple(setups),
        tuple(setups),
        prepared.metadata_body,
        canonical_json(attempted),
        admission_receipt_body=_admission_body(request, state, lease, assessment, policies)
        if kind == "gateway" and label == "positive"
        else None,
        reopened_admission_receipt_body=_admission_body(request, state, lease, assessment, policies)
        if kind == "gateway" and label == "positive"
        else None,
        reservation_body=encode_reservation(assessment.reservation)
        if label == "positive"
        else None,
        reopened_reservation_body=encode_reservation(assessment.reservation)
        if label == "positive"
        else None,
    )


def coherent_evidence(spec):
    labels = ("positive", "pause", "kill") if spec.case_id == "S06" else ("positive", "target")
    return CaseEvidence(
        spec.case_id,
        spec.source_case_sha256,
        spec.supplement_sha256,
        tuple(_observation(spec, label) for label in labels),
    )


@pytest.fixture(scope="module")
def packet():
    specs = load_scenarios()
    return specs, tuple(coherent_evidence(spec) for spec in specs)


@pytest.mark.parametrize("index", range(6))
def test_complete_coherent_observation_has_positive_and_specific_target(packet, index):
    specs, evidence = packet
    assert grade_case(specs[index], evidence[index]) == ()


@pytest.mark.parametrize("index", range(6))
def test_generic_or_wrong_reason_never_passes(packet, index):
    specs, evidence = packet
    case = evidence[index]
    target = case.observations[-1]
    if target.receipt_kind == "admission":
        receipt = decode_receipt(target.returned_body)
        receipt["assessment"] = replace(receipt["assessment"], code="invalid_source_facts")
        body = encode_receipt(receipt)
        target = replace(target, returned_body=body, stored_body=body, reopened_body=body)
    elif target.exception_code:
        target = replace(target, exception_code="lease_store_busy")
    else:
        receipt = json.loads(target.returned_body)
        receipt["code"] = "reconciliation_required"
        body = canonical_json(receipt)
        target = replace(target, returned_body=body, stored_body=body, reopened_body=body)
    mutated = replace(case, observations=case.observations[:-1] + (target,))
    assert grade_case(specs[index], mutated)


@pytest.mark.parametrize("index", range(6))
def test_missing_reopened_proof_fails(packet, index):
    specs, evidence = packet
    case = evidence[index]
    row = case.observations[0]
    assert grade_case(
        specs[index],
        replace(case, observations=(replace(row, reopened_body=None),) + case.observations[1:]),
    )


@pytest.mark.parametrize("index", range(6))
def test_new_reservation_on_target_refusal_fails(packet, index):
    specs, evidence = packet
    case = evidence[index]
    target = case.observations[-1]
    changed = replace(target.after, tables=_tables(reservations=1, admissions=1, integration=1))
    target = replace(target, after=changed, reopened=changed)
    failures = grade_case(
        specs[index], replace(case, observations=case.observations[:-1] + (target,))
    )
    assert "stale_owner_write" in failures if index == 5 else "unintended_reservation" in failures


def test_shrunk_oversized_order_and_rehashed_request_still_fails(packet):
    specs, evidence = packet
    spec, case = specs[2], evidence[2]
    target = case.observations[-1]
    request = decode_request(target.request_body)
    request.order["size"] /= 100
    raw = encode_request(request)
    receipt = decode_receipt(target.returned_body)
    receipt["request_sha256"] = fingerprint(raw)
    body = encode_receipt(receipt)
    target = replace(
        target,
        request_body=raw,
        order_body=captured_order(raw),
        returned_body=body,
        stored_body=body,
        reopened_body=body,
    )
    assert "order_integrity" in grade_case(
        spec, replace(case, observations=(case.observations[0], target))
    )


def test_correct_code_without_actual_stale_account_finding_fails(packet):
    specs, evidence = packet
    case = evidence[1]
    target = case.observations[-1]
    receipt = decode_receipt(target.returned_body)
    receipt["assessment"] = replace(receipt["assessment"], reconciliation_findings=())
    body = encode_receipt(receipt)
    target = replace(target, returned_body=body, stored_body=body, reopened_body=body)
    assert "stale_account_proof" in grade_case(
        specs[1], replace(case, observations=(case.observations[0], target))
    )


def test_latch_flag_without_durable_breach_recovery_is_insufficient(packet):
    specs, evidence = packet
    case = evidence[3]
    target = replace(case.observations[-1], setup_receipts=(), reopened_setup_receipts=())
    assert "loss_latch_proof" in grade_case(
        specs[3], replace(case, observations=(case.observations[0], target))
    )


def test_controls_cannot_silently_resume_during_refusal(packet):
    specs, evidence = packet
    case = evidence[4]
    target = case.observations[1]
    data = json.loads(target.after.controls_body)
    data["paused"] = False
    after = replace(target.after, controls_body=canonical_json(data))
    target = replace(target, after=after, reopened=after)
    assert "unintended_state_change" in grade_case(
        specs[4], replace(case, observations=(case.observations[0], target, case.observations[-1]))
    )


def test_stale_exception_with_current_grant_is_not_fencing_proof(packet):
    specs, evidence = packet
    case = evidence[-1]
    target = replace(
        case.observations[-1], attempted_lease_body=case.observations[-1].before.lease_body
    )
    assert "stale_owner_proof" in grade_case(
        specs[-1], replace(case, observations=(case.observations[0], target))
    )


@pytest.mark.parametrize("change", ("held", "absent", "wrong_owner", "wrong_book"))
def test_stale_refusal_does_not_pass_with_unready_current_owner(packet, change):
    specs, evidence = packet
    case = evidence[-1]
    target = case.observations[-1]
    hold = json.loads(target.before.hold_body)
    if change == "held":
        hold["hold_code"] = "safety_revalidation_required"
    elif change == "absent":
        hold["present"] = False
    elif change == "wrong_owner":
        hold["scope"]["owner_id"] = "some-other-owner"
    else:
        hold["scope"]["policy_book_sha256"] = "0" * 64
    changed = replace(target.before, hold_body=canonical_json(hold))
    target = replace(target, before=changed, after=changed, reopened=changed)
    assert "readiness_proof" in grade_case(
        specs[-1], replace(case, observations=(case.observations[0], target))
    )


def test_expired_current_lease_is_not_a_valid_stale_owner_positive_scope(packet):
    specs, evidence = packet
    case = evidence[-1]
    target = case.observations[-1]
    lease = json.loads(target.before.lease_body)
    lease["expires_at"] = "2026-10-22T03:29:59Z"
    changed = replace(target.before, lease_body=canonical_json(lease))
    target = replace(target, before=changed, after=changed, reopened=changed)
    assert "current_lease_expired" in grade_case(
        specs[-1], replace(case, observations=(case.observations[0], target))
    )


def test_valid_but_different_session_spec_cannot_relabel_the_prepared_scenario(packet):
    specs, evidence = packet
    case = evidence[0]
    target = case.observations[-1]
    state = decode_state(target.before.session_body)
    state = replace(state, spec=replace(state.spec, ends_at="2026-10-22T06:30:00Z"))
    changed = replace(target.before, session_body=encode_state(state))
    target = replace(target, before=changed, after=changed, reopened=changed)
    assert "session_integrity" in grade_case(
        specs[0], replace(case, observations=(case.observations[0], target))
    )


def test_valid_codec_with_altered_current_source_proof_fails_reconstruction(packet):
    specs, evidence = packet
    case = evidence[0]
    target = case.observations[-1]
    state = decode_state(target.before.session_body)
    state = replace(state, latest=replace(state.latest, account_evidence_id="0" * 64))
    from pipmind.safety.session import encode_observation

    state = replace(state, latest_sha256=fingerprint(encode_observation(state.latest)))
    changed = replace(target.before, session_body=encode_state(state))
    target = replace(target, before=changed, after=changed, reopened=changed)
    assert "session_integrity" in grade_case(
        specs[0], replace(case, observations=(case.observations[0], target))
    )


def test_pause_refusal_retains_ready_scope_from_preceding_control_revision(packet):
    specs, evidence = packet
    case = evidence[-2]
    target = case.observations[1]
    hold = json.loads(target.before.hold_body)
    hold["scope"]["control_revision"] = 0
    changed = replace(target.before, hold_body=canonical_json(hold))
    after = replace(target.after, hold_body=canonical_json(hold))
    target = replace(target, before=changed, after=after, reopened=after)
    assert (
        grade_case(
            specs[-2],
            replace(case, observations=(case.observations[0], target, case.observations[2])),
        )
        == ()
    )


def test_positive_control_cannot_be_a_generic_refusal(packet):
    specs, evidence = packet
    case = evidence[0]
    positive = case.observations[0]
    raw = decode_receipt(positive.returned_body)
    raw["assessment"] = replace(
        raw["assessment"], within_limits=False, code="stale_quote", reservation=None
    )
    body = encode_receipt(raw)
    positive = replace(positive, returned_body=body, stored_body=body, reopened_body=body)
    assert "positive_control" in grade_case(
        specs[0], replace(case, observations=(positive, case.observations[-1]))
    )


@pytest.mark.parametrize("field", ("base_units", "loss_budget", "gross_budget", "margin_budget"))
@pytest.mark.parametrize("index", (0, 4, 5))
def test_consistently_reencoded_shrunken_positive_reservation_fails(packet, field, index):
    specs, evidence = packet
    case = evidence[index]
    positive = case.observations[0]
    receipt = decode_receipt(
        positive.returned_body if index == 0 else positive.admission_receipt_body
    )
    reservation = receipt["assessment"].reservation
    shrunk = replace(reservation, **{field: getattr(reservation, field) / 10})
    receipt["assessment"] = replace(receipt["assessment"], reservation=shrunk)
    body = encode_receipt(receipt)
    changes = (
        {"returned_body": body, "stored_body": body, "reopened_body": body}
        if index == 0
        else {
            "admission_receipt_body": body,
            "reopened_admission_receipt_body": body,
        }
    )
    positive = replace(
        positive,
        **changes,
        reservation_body=encode_reservation(shrunk),
        reopened_reservation_body=encode_reservation(shrunk),
    )
    assert "assessment_integrity" in grade_case(
        specs[index], replace(case, observations=(positive,) + case.observations[1:])
    )


@pytest.mark.parametrize("change", ("external_count", "exposure", "scope"))
def test_exact_positive_assessment_must_match_policy_state_and_calculations(packet, change):
    specs, evidence = packet
    case = evidence[0]
    positive = case.observations[0]
    receipt = decode_receipt(positive.returned_body)
    assessment = receipt["assessment"]
    if change == "external_count":
        receipt["assessment"] = replace(assessment, validated_external_count=1)
    elif change == "exposure":
        assert "gross_exposure" in dict(assessment.calculations)
        receipt["assessment"] = replace(
            assessment,
            calculations=tuple(
                (key, value + Decimal("1") if key == "gross_exposure" else value)
                for key, value in assessment.calculations
            ),
        )
    else:
        receipt["lease_owner_id"] = "some-other-owner"
    body = encode_receipt(receipt)
    positive = replace(positive, returned_body=body, stored_body=body, reopened_body=body)
    assert "assessment_integrity" in grade_case(
        specs[0], replace(case, observations=(positive, case.observations[-1]))
    )


@pytest.mark.parametrize("index", (4, 5))
def test_thin_gateway_positive_requires_full_reopened_admission_and_reservation(packet, index):
    specs, evidence = packet
    case = evidence[index]
    positive = replace(
        case.observations[0], admission_receipt_body=None, reopened_admission_receipt_body=None
    )
    assert "assessment_integrity" in grade_case(
        specs[index], replace(case, observations=(positive,) + case.observations[1:])
    )
    positive = replace(case.observations[0], reservation_body=None, reopened_reservation_body=None)
    assert "assessment_integrity" in grade_case(
        specs[index], replace(case, observations=(positive,) + case.observations[1:])
    )


@pytest.mark.parametrize("index", (0, 4))
@pytest.mark.parametrize(
    "moment", ("2099-01-01T00:00:00Z", "2026-10-22T03:29:59Z", "2026-10-22T03:40:00Z")
)
def test_coherently_changed_receipt_time_must_fit_clock_and_live_lease(packet, index, moment):
    specs, evidence = packet
    case = evidence[index]
    positive = case.observations[0]
    receipt = (
        decode_receipt(positive.returned_body) if index == 0 else json.loads(positive.returned_body)
    )
    receipt["observed_at"] = moment
    body = encode_receipt(receipt) if index == 0 else canonical_json(receipt)
    positive = replace(positive, returned_body=body, stored_body=body, reopened_body=body)
    assert "receipt_chronology" in grade_case(
        specs[index], replace(case, observations=(positive,) + case.observations[1:])
    )


def test_existing_floor_or_reservation_cannot_be_assumed_away(packet):
    specs, evidence = packet
    case = evidence[0]
    positive = case.observations[0]
    before = replace(
        positive.before,
        tables=tuple(
            TableDigest(row.name, 1, row.sha256)
            if row.name == "dbo.pm_safety_admission_floors_v1"
            else row
            for row in positive.before.tables
        ),
    )
    positive = replace(positive, before=before)
    assert "assessment_integrity" in grade_case(
        specs[0], replace(case, observations=(positive, case.observations[-1]))
    )


def test_empty_missing_duplicate_or_reordered_inventory_does_not_pass(packet):
    specs, evidence = packet
    case = evidence[0]
    assert grade_case(specs[0], replace(case, observations=())) == ("observation_inventory",)
    assert grade_case(specs[0], replace(case, observations=case.observations[::-1]))
    row = case.observations[0]
    invalid = replace(row.before, tables=row.before.tables[:-1])
    assert grade_case(
        specs[0], replace(case, observations=(replace(row, before=invalid), case.observations[-1]))
    )


def test_clock_offset_and_source_lineage_cannot_be_silently_rewritten(packet):
    specs, evidence = packet
    case = evidence[0]
    assert "lineage_integrity" in grade_case(specs[0], replace(case, source_case_sha256="0" * 64))
    target = case.observations[-1]
    clock = json.loads(target.clock_body)
    clock["offset_microseconds"] += 1
    target = replace(target, clock_body=canonical_json(clock))
    assert grade_case(specs[0], replace(case, observations=(case.observations[0], target)))


def test_report_is_allowlisted_bounded_and_never_sql_or_model_attestation(packet):
    specs, evidence = packet
    report = sanitized_report(specs, evidence, dataset_sha256="a" * 64)
    assert report["passed"] is True
    assert tuple(row["case_id"] for row in report["cases"]) == CASE_IDS
    assert report["consistency_only"] is True and report["sql_execution_attested"] is False
    assert report["baseline_changed"] is False and report["dispatch_authorized"] is False
    raw = canonical_json(report)
    assert len(raw.encode()) <= MAX_REPORT_BYTES
    assert "private-session" not in raw and "private-S" not in raw and "synthetic-epoch" not in raw
    assert "receipt" not in raw and "order_body" not in raw
    with pytest.raises(ValueError, match="invalid_evaluation_inventory"):
        sanitized_report((), (), dataset_sha256="a" * 64)


def test_evidence_is_frozen_and_grading_does_not_mutate_inputs(packet):
    specs, evidence = packet
    before = repr(evidence)
    assert grade_case(specs[0], evidence[0]) == ()
    assert repr(evidence) == before
    with pytest.raises(FrozenInstanceError):
        evidence[0].case_id = "S99"
