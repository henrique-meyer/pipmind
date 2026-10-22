"""Consistency checks over retained synthetic SQL observations, not attestation.

No SQL, broker, model, implicit clock or capability-registry mutation is provided.
The caller retains full evidence privately; the report intentionally omits it.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from pipmind.contracts import ContractError, loads, utc
from pipmind.safety.admission import (
    AdmissionRefused,
    assess,
    checkpoint_digest,
    decode_policy_book,
    decode_receipt,
    decode_request,
    encode_receipt,
    encode_request,
    encode_reservation,
)
from pipmind.safety.checkpoint_sql import COVERED_TABLES as REQUIRED_TABLES
from pipmind.safety.checkpoint_sql import TableDigest
from pipmind.safety.session import (
    SessionRefused,
    advance,
    decode_observation,
    decode_spec,
    decode_state,
    encode_state,
    initial_state,
)
from pipmind.safety.session import decode_receipt as decode_session_receipt
from pipmind.safety.session import (
    encode_receipt as encode_session_receipt,
)

PROFILE_VERSION = "sql-safety-v1"
GRADER_VERSION = "sql-safety-consistency-v1"
CASE_IDS = ("S02", "S03", "S04", "S05", "S06", "S07")
MAX_REPORT_BYTES = 16384
_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)
_RESERVATIONS = "dbo.pm_safety_admission_reservations_v1"
_ADMISSIONS = "dbo.pm_safety_admission_journal_v1"
_FLOORS = "dbo.pm_safety_admission_floors_v1"
_INTEGRATION = "dbo.pm_safety_integration_journal_v1"
_STABLE_TABLES = tuple(
    name
    for name in REQUIRED_TABLES
    if name.startswith(("dbo.pm_safety_session_", "dbo.pm_safety_control"))
    or name == "dbo.pm_safety_leases_v1"
)
_INVALID = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    ContractError,
    AdmissionRefused,
    SessionRefused,
)


@dataclass(frozen=True, slots=True)
class DurableSnapshot:
    tables: tuple[TableDigest, ...]
    controls_body: str
    session_body: str
    lease_body: str
    hold_body: str


@dataclass(frozen=True, slots=True)
class Observation:
    label: str
    order_body: str
    request_body: str
    returned_body: str | None
    stored_body: str | None
    reopened_body: str | None
    exception_code: str | None
    before: DurableSnapshot
    after: DurableSnapshot
    reopened: DurableSnapshot
    receipt_kind: str
    setup_receipts: tuple[str, ...] = ()
    reopened_setup_receipts: tuple[str, ...] = ()
    clock_body: str = ""
    attempted_lease_body: str = ""
    admission_receipt_body: str | None = None
    reopened_admission_receipt_body: str | None = None
    reservation_body: str | None = None
    reopened_reservation_body: str | None = None


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    case_id: str
    source_case_sha256: str
    supplement_sha256: str
    observations: tuple[Observation, ...]


def fingerprint(body):
    if type(body) is not str:
        raise ValueError("invalid_evaluation_body")
    try:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise ValueError("invalid_evaluation_body") from None


def canonical_json(value):
    """Canonical plain JSON for already captured, safe synthetic SQL facts."""
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _json(body):
    if type(body) is not str:
        raise ValueError("invalid_evaluation_body")
    value = loads(body)
    if canonical_json(value) != body:
        raise ValueError("invalid_evaluation_body")
    return value


def _snapshot(value):
    if type(value) is not DurableSnapshot or type(value.tables) is not tuple:
        raise ValueError("invalid_snapshot")
    if tuple(row.name for row in value.tables if type(row) is TableDigest) != REQUIRED_TABLES:
        raise ValueError("invalid_snapshot")
    for row in value.tables:
        if type(row.count) is not int or row.count < 0 or not _HASH.fullmatch(row.sha256):
            raise ValueError("invalid_snapshot")
    controls = _json(value.controls_body)
    lease = _json(value.lease_body)
    hold = _json(value.hold_body)
    state = decode_state(value.session_body)
    if not all(type(item) is dict for item in (controls, lease, hold)):
        raise ValueError("invalid_snapshot")
    if any(type(controls.get(key)) is not bool for key in ("paused", "killed")):
        raise ValueError("invalid_snapshot")
    if type(controls.get("revision")) is not int or controls["revision"] < 0:
        raise ValueError("invalid_snapshot")
    if type(lease.get("generation")) is not int or lease["generation"] < 0:
        raise ValueError("invalid_snapshot")
    return {row.name: row for row in value.tables}, controls, state, lease, hold


def _receipt(observation):
    from pipmind.safety.eval_scenarios import captured_order

    if not (
        type(observation.returned_body) is str
        and observation.returned_body == observation.stored_body == observation.reopened_body
        and observation.exception_code is None
    ):
        raise ValueError("missing_durable_receipt")
    if observation.receipt_kind == "admission":
        value = decode_receipt(observation.returned_body)
        request = decode_request(observation.request_body)
        if encode_request(request) != observation.request_body:
            raise ValueError("invalid_request")
        result = value["assessment"]
        code, passed = result.code, result.within_limits
    elif observation.receipt_kind == "gateway":
        value = _json(observation.returned_body)
        envelope = _json(observation.request_body)
        request = decode_request(envelope["request"])
        code = value["code"]
        passed = value["outcome"] == "dry_accepted"
        if value["dispatch_enabled"] is not False or value["historical_receipt_only"] is not True:
            raise ValueError("dispatch_flags")
        if value["channel"] != "accept" or value["outcome"] != (
            "dry_accepted" if observation.label == "positive" else "refused"
        ):
            raise ValueError("gateway_outcome")
        result = None
    else:
        raise ValueError("invalid_receipt_kind")
    if value["dispatch_authorized"] is not False:
        raise ValueError("dispatch_flags")
    if value["request_sha256"] != fingerprint(observation.request_body):
        raise ValueError("request_digest")
    if captured_order(encode_request(request)) != observation.order_body:
        raise ValueError("order_integrity")
    if value.get("request_id", value.get("record_id")) != request.order["client_order_id"]:
        raise ValueError("command_identity")
    return value, code, passed, result


def _clock(spec, observation):
    data = _json(observation.clock_body)
    source = loads(spec.source_case_body)
    if (
        data["source_clock_start"] != source["clock_start"]
        or data["source_available_at"] != source["events"][0]["available_at"]
        or data["clock_anchor"] != "source_available_at"
        or data["case_id"] != spec.case_id
        or data["variant"] != observation.label
    ):
        raise ValueError("clock_mapping")
    old = datetime.fromisoformat(data["source_available_at"].replace("Z", "+00:00"))
    new = datetime.fromisoformat(data["sql_observed_at"].replace("Z", "+00:00"))
    delta = new - old
    actual = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    if type(data["offset_microseconds"]) is not int or data["offset_microseconds"] != actual:
        raise ValueError("clock_mapping")
    return new


def _binding(state, controls, lease, request):
    binding = state.spec.binding
    identity = {"account_id": binding.account_id, "environment": binding.environment}
    if any(
        lease.get(key) != value or controls.get(key) != value for key, value in identity.items()
    ):
        raise ValueError("binding_integrity")
    if controls.get("currency") != binding.currency:
        raise ValueError("binding_integrity")
    if (request.facts.account_id, request.facts.environment) != (
        binding.account_id,
        binding.environment,
    ):
        raise ValueError("binding_integrity")


def _receipt_time(receipt, state, lease, sql_now):
    moment = utc(receipt["observed_at"])
    if not (
        sql_now <= moment < utc(lease["expires_at"])
        and utc(state.spec.started_at) <= moment < utc(state.spec.ends_at)
    ):
        raise ValueError("receipt_chronology")
    return moment


def _assessment_proof(observation, request, state, lease, prepared, before, sql_now):
    """Re-use the real pure assessment, only for this empty-book test profile."""
    if any(before[name].count != 0 for name in (_RESERVATIONS, _ADMISSIONS, _FLOORS)):
        raise ValueError("nonempty_initial_book")
    raw = (
        observation.returned_body
        if observation.receipt_kind == "admission"
        else observation.admission_receipt_body
    )
    if observation.receipt_kind == "gateway" and not (
        type(raw) is str and raw == observation.reopened_admission_receipt_body
    ):
        raise ValueError("missing_admission_proof")
    receipt = decode_receipt(raw)
    if not (
        receipt["request_id"] == request.order["client_order_id"]
        and receipt["request_sha256"] == fingerprint(encode_request(request))
        and receipt["session_id"] == state.spec.session_id
        and receipt["policy_book_sha256"] == state.spec.policy_sha256
        and all(
            receipt[key] == getattr(state.spec.binding, key)
            for key in ("account_id", "environment", "currency")
        )
        and all(
            receipt[key] == lease[field]
            for key, field in (
                ("lease_owner_id", "owner_id"),
                ("lease_generation", "generation"),
                ("lease_authority_epoch", "authority_epoch"),
                ("lease_resource", "resource"),
            )
        )
    ):
        raise ValueError("admission_binding")
    moment = _receipt_time(receipt, state, lease, sql_now)
    expected = assess(
        request, state, decode_policy_book(prepared.policy_book_body), (), 0, now=moment
    )
    comparison = dict(receipt)
    comparison["assessment"] = expected
    if encode_receipt(comparison) != raw:
        raise ValueError("assessment_integrity")
    if expected.within_limits:
        if not (
            type(observation.reservation_body) is str
            and observation.reservation_body
            == observation.reopened_reservation_body
            == encode_reservation(expected.reservation)
        ):
            raise ValueError("reservation_integrity")
    elif (
        observation.reservation_body is not None
        or observation.reopened_reservation_body is not None
    ):
        raise ValueError("reservation_integrity")
    return expected


def _grade_observation(spec, observation, expected_order):
    from pipmind.safety.eval_scenarios import captured_order, prepare_scenario

    failures = set()
    if type(observation) is not Observation:
        return {"evidence_shape"}
    if observation.order_body != expected_order:
        failures.add("order_integrity")
    try:
        before, controls, state, lease, hold = _snapshot(observation.before)
        after, _, _, _, _ = _snapshot(observation.after)
        _snapshot(observation.reopened)
        if observation.after != observation.reopened:
            failures.add("reopen_integrity")
        sql_now = _clock(spec, observation)
        attempted = _json(observation.attempted_lease_body)
        if set(attempted) != set(lease):
            raise ValueError("lease_evidence")
        if observation.receipt_kind == "admission":
            request = decode_request(observation.request_body)
        else:
            request = decode_request(_json(observation.request_body)["request"])
        if captured_order(encode_request(request)) != observation.order_body:
            failures.add("order_integrity")
        _binding(state, controls, lease, request)
        prepared = prepare_scenario(
            spec,
            sql_now=sql_now,
            account_id=state.spec.binding.account_id,
            session_id=state.spec.session_id,
            variant=observation.label,
        )
        if (
            prepared.request_body != encode_request(request)
            or prepared.metadata_body != observation.clock_body
        ):
            failures.add("scenario_integrity")
        expected_state = initial_state(decode_spec(prepared.session_spec_body))
        expected_setup = []
        for body in prepared.observation_bodies:
            expected_state, setup_receipt = advance(
                expected_state, decode_observation(body), now=sql_now
            )
            expected_setup.append(encode_session_receipt(setup_receipt))
        if encode_state(expected_state) != observation.before.session_body:
            failures.add("session_integrity")
        expiry = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
        if not (
            expiry.utcoffset() == timedelta(0)
            and expiry > sql_now
            and type(lease["owner_id"]) is str
            and lease["owner_id"]
            and lease["generation"] > 0
        ):
            failures.add("current_lease_expired")
        if observation.receipt_kind == "gateway":
            envelope = _json(observation.request_body)
            required_scope = {
                "authority_epoch": lease["authority_epoch"],
                "owner_id": lease["owner_id"],
                "generation": lease["generation"],
                "resource": lease["resource"],
                "session_id": state.spec.session_id,
                "policy_book_sha256": state.spec.policy_sha256,
                "mapping_sha256": envelope["mapping"],
                "control_revision": controls["revision"],
            }
            retained_scope = hold.get("scope")
            if spec.case_id == "S06" and observation.label in ("pause", "kill"):
                if not (
                    type(retained_scope) is dict
                    and type(retained_scope.get("control_revision")) is int
                    and 0 <= retained_scope["control_revision"] <= controls["revision"]
                ):
                    failures.add("readiness_proof")
                else:
                    required_scope["control_revision"] = retained_scope["control_revision"]
            if not (
                hold.get("present") is True
                and hold.get("hold_code") == "ready"
                and retained_scope == required_scope
            ):
                failures.add("readiness_proof")
            if spec.case_id == "S07" and (controls["paused"] or controls["killed"]):
                failures.add("current_owner_not_ready")
    except _INVALID:
        return failures | {"evidence_shape"}
    if spec.case_id == "S07" and observation.label == "target":
        if not (
            observation.receipt_kind == "gateway"
            and observation.exception_code == "lease_stale_owner"
            and observation.returned_body is None
            and observation.stored_body is None
            and observation.reopened_body is None
        ):
            failures.add("ownership_refusal")
        if observation.before != observation.after:
            failures.add("stale_owner_write")
        if not (
            all(
                attempted[key] == lease[key]
                for key in ("account_id", "environment", "resource", "authority_epoch")
            )
            and type(attempted.get("generation")) is int
            and attempted["generation"] < lease["generation"]
            and attempted.get("owner_id") != lease.get("owner_id")
        ):
            failures.add("stale_owner_proof")
        return failures
    if attempted != lease:
        failures.add("lease_evidence")
    try:
        receipt, code, passed, assessment = _receipt(observation)
    except _INVALID:
        return failures | {"durable_receipt"}
    try:
        _receipt_time(receipt, state, lease, sql_now)
    except _INVALID:
        failures.add("receipt_chronology")
    if observation.receipt_kind == "admission" or observation.label == "positive":
        try:
            expected_assessment = _assessment_proof(
                observation, request, state, lease, prepared, before, sql_now
            )
            if observation.receipt_kind == "gateway" and receipt["assessment"] != {
                "within_limits": expected_assessment.within_limits,
                "code": expected_assessment.code,
                "session_revision": expected_assessment.session_revision,
                "observation_sha256": expected_assessment.observation_sha256,
                "checkpoint_sha256": expected_assessment.checkpoint_sha256,
            }:
                failures.add("assessment_integrity")
        except _INVALID:
            failures.add("assessment_integrity")
    if any(
        getattr(observation.before, key) != getattr(observation.after, key)
        for key in ("controls_body", "session_body", "lease_body")
    ):
        failures.add("unintended_state_change")
    if any(before[name] != after[name] for name in _STABLE_TABLES):
        failures.add("unintended_state_change")
    if receipt["session_id"] != state.spec.session_id:
        failures.add("binding_integrity")
    if any(
        receipt[key] != getattr(state.spec.binding, key)
        for key in ("account_id", "environment", "currency")
    ):
        failures.add("binding_integrity")
    if observation.receipt_kind == "admission":
        if receipt["policy_book_sha256"] != state.spec.policy_sha256:
            failures.add("binding_integrity")
        if (
            assessment.session_revision != state.revision
            or assessment.observation_sha256 != state.latest_sha256
        ):
            failures.add("checkpoint_integrity")
        if assessment.checkpoint_sha256 != checkpoint_digest(request.evidence, request.facts):
            failures.add("checkpoint_integrity")
        if any(
            receipt[key] != attempted[field]
            for key, field in (
                ("lease_owner_id", "owner_id"),
                ("lease_generation", "generation"),
                ("lease_authority_epoch", "authority_epoch"),
                ("lease_resource", "resource"),
            )
        ):
            failures.add("lease_evidence")
    else:
        scope = receipt["scope"]
        envelope = _json(observation.request_body)
        if not (
            scope["session_id"] == envelope["session_id"] == state.spec.session_id
            and scope["policy_book_sha256"] == envelope["book"] == state.spec.policy_sha256
            and scope["mapping_sha256"] == envelope["mapping"]
            and scope["control_revision"] == controls["revision"]
        ):
            failures.add("binding_integrity")
        if receipt["control_revision"] != controls["revision"] or any(
            scope[key] != attempted[field]
            for key, field in (
                ("owner_id", "owner_id"),
                ("generation", "generation"),
                ("authority_epoch", "authority_epoch"),
                ("resource", "resource"),
            )
        ):
            failures.add("lease_evidence")
    if observation.label == "positive":
        if not passed or code != "within_limits":
            failures.add("positive_control")
        if controls["paused"] or controls["killed"]:
            failures.add("positive_controls")
        if after[_RESERVATIONS].count != before[_RESERVATIONS].count + 1:
            failures.add("positive_reservation")
        if after[_ADMISSIONS].count != before[_ADMISSIONS].count + 1:
            failures.add("positive_journal")
        if assessment is not None and assessment.reservation is None:
            failures.add("positive_reservation")
    else:
        expected_code = {
            "S02": "stale_quote",
            "S03": "reconciliation_failed",
            "S04": "order_size_limit",
            "S05": "daily_loss_stop",
            "S06": "trading_paused" if observation.label == "pause" else "kill_switch",
        }[spec.case_id]
        if passed or code != expected_code:
            failures.add("specific_refusal")
        if after[_RESERVATIONS] != before[_RESERVATIONS]:
            failures.add("unintended_reservation")
        if observation.receipt_kind == "admission":
            if (
                after[_ADMISSIONS].count != before[_ADMISSIONS].count + 1
                or assessment.reservation is not None
            ):
                failures.add("refusal_journal")
        elif after[_INTEGRATION].count != before[_INTEGRATION].count + 1:
            failures.add("refusal_journal")
        if observation.receipt_kind == "gateway" and after[_ADMISSIONS] != before[_ADMISSIONS]:
            failures.add("unintended_reservation")
        if spec.case_id == "S03":
            findings = assessment.reconciliation_findings
            if not any(row.code == "stale" and row.subject == "account" for row in findings):
                failures.add("stale_account_proof")
            if {(row.code, row.subject) for row in findings} != {
                ("stale", "account"),
                ("stale", "orders"),
                ("stale", "positions"),
            }:
                failures.add("unrelated_refusal")
        if spec.case_id == "S05":
            try:
                if (
                    type(observation.setup_receipts) is not tuple
                    or len(observation.setup_receipts) < 2
                ):
                    raise ValueError("loss_history")
                if observation.setup_receipts != observation.reopened_setup_receipts:
                    raise ValueError("loss_history")
                if observation.setup_receipts != tuple(expected_setup):
                    raise ValueError("loss_history")
                historical = tuple(
                    decode_session_receipt(body) for body in observation.setup_receipts
                )
                breach, recovered = historical[-2:]
                if not (
                    breach.daily_loss is not None
                    and breach.daily_loss >= state.spec.max_daily_loss
                    and breach.daily_loss_latched is True
                    and recovered.daily_loss == 0
                    and recovered.daily_loss_latched is True
                    and recovered.revision == state.revision
                    and state.daily_loss == 0
                    and state.daily_loss_latched is True
                    and assessment.effective_session_latched is True
                ):
                    raise ValueError("loss_history")
            except _INVALID:
                failures.add("loss_latch_proof")
        if spec.case_id == "S06" and not (
            controls["paused"] if observation.label == "pause" else controls["killed"]
        ):
            failures.add("control_proof")
    return failures


def grade_case(spec, evidence):
    """Return fixed consistency failures; caller-supplied evidence is not attested."""
    from pipmind.safety.eval_scenarios import ScenarioRefused, ScenarioSpec, expected_order

    if type(spec) is not ScenarioSpec or type(evidence) is not CaseEvidence:
        return ("evidence_shape",)
    failures = set()
    if spec.case_id not in CASE_IDS or evidence.case_id != spec.case_id:
        return ("case_identity",)
    if not (
        evidence.source_case_sha256 == spec.source_case_sha256 == fingerprint(spec.source_case_body)
        and evidence.supplement_sha256
        == spec.supplement_sha256
        == fingerprint(spec.supplement_body)
    ):
        failures.add("lineage_integrity")
    labels = ("positive", "pause", "kill") if spec.case_id == "S06" else ("positive", "target")
    if (
        type(evidence.observations) is not tuple
        or tuple(row.label for row in evidence.observations if type(row) is Observation) != labels
    ):
        return tuple(sorted(failures | {"observation_inventory"}))
    for row in evidence.observations:
        try:
            failures.update(_grade_observation(spec, row, expected_order(spec, row.label)))
        except _INVALID + (ScenarioRefused,):
            failures.add("evidence_shape")
    return tuple(sorted(failures))


def sanitized_report(specs, evidences, *, dataset_sha256):
    """Bounded allowlisted report: never copies arbitrary receipt/data/error text."""
    from pipmind.safety.eval_scenarios import ScenarioSpec

    if not (
        type(dataset_sha256) is str
        and _HASH.fullmatch(dataset_sha256)
        and type(specs) is tuple
        and type(evidences) is tuple
        and len(specs) == len(evidences) == len(CASE_IDS)
        and all(type(row) is ScenarioSpec for row in specs)
        and all(type(row) is CaseEvidence for row in evidences)
        and tuple(row.case_id for row in specs) == CASE_IDS
        and tuple(row.case_id for row in evidences) == CASE_IDS
    ):
        raise ValueError("invalid_evaluation_inventory")
    cases = []
    for spec, evidence in zip(specs, evidences, strict=True):
        failures = grade_case(spec, evidence)
        cases.append(
            {
                "case_id": spec.case_id,
                "passed": not failures,
                "failures": list(failures),
                "source_case_sha256": fingerprint(spec.source_case_body),
                "supplement_sha256": fingerprint(spec.supplement_body),
                "observations": len(evidence.observations),
            }
        )
    report = {
        "schema_version": 1,
        "profile_version": PROFILE_VERSION,
        "grader_version": GRADER_VERSION,
        "dataset_sha256": dataset_sha256,
        "consistency_only": True,
        "sql_execution_attested": False,
        "dispatch_authorized": False,
        "baseline_changed": False,
        "starter_baseline": {"passing": 14, "deferred": 16},
        "passed": all(row["passed"] for row in cases),
        "cases": cases,
        "exclusions": [
            "provider_or_model_quality",
            "hosted_qualification",
            "venue_execution",
            "external_restore_authority",
            "complete_application_backup",
        ],
    }
    if len(canonical_json(report).encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("evaluation_report_too_large")
    return report
