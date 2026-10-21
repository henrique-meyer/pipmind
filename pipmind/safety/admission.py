"""Local deterministic admission accounting, never an execution capability.

Reconciliation may read local normative schemas. No SQL, network, model, broker,
implicit clock, release or reservation lifecycle operation is provided here.
"""

import json
import re
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from decimal import Context, Decimal, DecimalException, localcontext
from functools import lru_cache

from pipmind.contracts import ContractError, Contracts
from pipmind.reconciliation import (
    Binding,
    Finding,
    Freshness,
    ProtectiveOrder,
    SnapshotFacts,
    reconcile,
)
from pipmind.safety import boundary
from pipmind.safety.boundary import Conversion, Exposure, ProposalFacts, RiskFacts, RiskPolicy
from pipmind.safety.session import (
    EquityObservation,
    ObservationReceipt,
    SessionBinding,
    SessionRefused,
    SessionSpec,
    SessionState,
    encode_state,
    fingerprint,
    require_current,
)

MAX_REQUEST_BYTES = 1048576
MAX_RECEIPT_BYTES = 262144
MAX_RESERVATION_BYTES = 4096
MAX_POLICY_BOOK_BYTES = 65536
MAX_RESERVATIONS = 1000
MAX_MARKS = 1000
MAX_POLICIES = 128
MAX_COUNT = 9223372036854775807


class AdmissionRefused(Exception):
    """Fixed-code invalid API/codec refusal; no caller data in error text."""


class _AssessmentRefused(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PositionMark:
    position_id: str
    bid: Decimal
    ask: Decimal
    as_of: str
    base_units_per_size: Decimal


@dataclass(frozen=True, slots=True)
class ReservationMark:
    request_id: str
    bid: Decimal
    ask: Decimal
    as_of: str
    conversion: Conversion
    margin_required: Decimal


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    expected: SnapshotFacts
    observed: SnapshotFacts
    freshness: Freshness
    commands: tuple
    command_requests: tuple
    pending_command_ids: tuple
    protective_obligations: tuple
    position_marks: tuple[PositionMark, ...]


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    order: dict
    facts: RiskFacts
    evidence: ReconciliationEvidence
    reservation_marks: tuple[ReservationMark, ...]
    expected_session_revision: int
    expected_observation_sha256: str


@dataclass(frozen=True, slots=True)
class Reservation:
    request_id: str
    session_id: str
    instrument: str
    side: str
    base_units: Decimal
    entry_bound: Decimal
    stop: Decimal
    quote_currency: str
    gross_budget: Decimal
    loss_budget: Decimal
    margin_budget: Decimal


@dataclass(frozen=True, slots=True)
class Assessment:
    within_limits: bool
    code: str
    calculations: tuple[tuple[str, Decimal], ...]
    reservation: Reservation | None
    validated_external_count: int | None
    session_revision: int
    observation_sha256: str | None
    checkpoint_sha256: str
    reconciliation_findings: tuple[Finding, ...]
    effective_session_daily_loss: Decimal | None
    effective_session_latched: bool

    @property
    def dispatch_authorized(self):
        return False


_CLASSES = (
    PositionMark,
    ReservationMark,
    ReconciliationEvidence,
    AdmissionRequest,
    Reservation,
    Assessment,
    RiskPolicy,
    Conversion,
    ProposalFacts,
    Exposure,
    RiskFacts,
    Binding,
    Freshness,
    ProtectiveOrder,
    SnapshotFacts,
    Finding,
    SessionBinding,
    SessionSpec,
    EquityObservation,
    SessionState,
    ObservationReceipt,
)
_REGISTRY = {cls.__name__: cls for cls in _CLASSES}
_BY_TYPE = {cls: name for name, cls in _REGISTRY.items()}
_GLOBAL_FIELDS = (
    "policy_id",
    "account_id",
    "environment",
    "currency",
    "max_gross_exposure",
    "max_total_planned_loss",
    "max_daily_loss",
    "max_daily_risk",
    "min_margin_available",
    "max_session_orders",
)
_RECEIPT_KEYS = frozenset(
    (
        "schema_version",
        "request_id",
        "account_id",
        "environment",
        "currency",
        "session_id",
        "request_sha256",
        "policy_book_sha256",
        "assessment",
        "lease_authority_epoch",
        "lease_owner_id",
        "lease_generation",
        "lease_resource",
        "observed_at",
        "dispatch_authorized",
    )
)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", re.ASCII)


@lru_cache(maxsize=1)
def _contracts():
    return Contracts()


def _need(condition, code="admission_invalid_input"):
    if not condition:
        raise AdmissionRefused(code)


def _risk(condition, code):
    if not condition:
        raise _AssessmentRefused(code)


def _id(value):
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-16-le")) <= 256
    except UnicodeError:
        return False


def _text(value):
    return type(value) is str and bool(value) and value.strip() == value and "\x00" not in value


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def _num(value, *, positive=False, nonnegative=False):
    _need(type(value) is Decimal and value.is_finite(), "admission_invalid_decimal")
    parts = value.as_tuple()
    _need(len(parts.digits) <= 64 and abs(parts.exponent) <= 64, "admission_numeric_capacity")
    _need(not positive or value > 0, "admission_invalid_decimal")
    _need(not nonnegative or value >= 0, "admission_invalid_decimal")
    return value


def _context():
    context = Context(prec=1024, Emin=-999999, Emax=999999)
    for signal in context.traps:
        context.traps[signal] = True
    return context


def _derived(value):
    sign, digits, exponent = value.as_tuple()
    while len(digits) > 64 and digits[-1] == 0 and exponent < 64:
        digits, exponent = digits[:-1], exponent + 1
    result = Decimal((sign, digits, exponent))
    try:
        return _num(result)
    except AdmissionRefused:
        raise _AssessmentRefused("numeric_capacity_exceeded") from None


def _time(value):
    _need(type(value) is str and _TIMESTAMP.fullmatch(value) is not None, "admission_invalid_time")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise AdmissionRefused("admission_invalid_time") from None


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _pack(value, active):
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return value
    if kind is Decimal:
        _num(value)
        return ["decimal", str(value)]
    if kind is timedelta:
        return ["timedelta", value.days, value.seconds, value.microseconds]
    _need(kind in (list, tuple, dict) or kind in _BY_TYPE, "admission_unsupported_type")
    identity = id(value)
    _need(identity not in active, "admission_cyclic_input")
    active.add(identity)
    try:
        if kind in (list, tuple):
            return ["list" if kind is list else "tuple", [_pack(item, active) for item in value]]
        if kind is dict:
            _need(all(type(key) is str for key in value), "admission_invalid_mapping")
            return ["dict", [[key, _pack(value[key], active)] for key in sorted(value)]]
        return [
            "record",
            _BY_TYPE[kind],
            [
                [field.name, _pack(object.__getattribute__(value, field.name), active)]
                for field in fields(kind)
            ],
        ]
    finally:
        active.remove(identity)


def _unpack(value):
    if value is None or type(value) in (str, bool, int):
        return value
    _need(type(value) is list and bool(value), "admission_invalid_codec")
    tag = value[0]
    if tag == "decimal":
        _need(
            len(value) == 2 and type(value[1]) is str and len(value[1]) <= 160,
            "admission_invalid_codec",
        )
        try:
            with localcontext(_context()):
                number = Decimal(value[1])
        except DecimalException:
            raise AdmissionRefused("admission_invalid_decimal") from None
        _num(number)
        _need(str(number) == value[1], "admission_noncanonical_codec")
        return number
    if tag == "timedelta":
        _need(
            len(value) == 4 and all(type(item) is int for item in value[1:]),
            "admission_invalid_codec",
        )
        try:
            return timedelta(days=value[1], seconds=value[2], microseconds=value[3])
        except OverflowError:
            raise AdmissionRefused("admission_invalid_codec") from None
    if tag in ("tuple", "list"):
        _need(len(value) == 2 and type(value[1]) is list, "admission_invalid_codec")
        items = [_unpack(item) for item in value[1]]
        return tuple(items) if tag == "tuple" else items
    if tag == "dict":
        _need(len(value) == 2 and type(value[1]) is list, "admission_invalid_codec")
        result = {}
        for pair in value[1]:
            _need(
                type(pair) is list
                and len(pair) == 2
                and type(pair[0]) is str
                and pair[0] not in result,
                "admission_invalid_codec",
            )
            result[pair[0]] = _unpack(pair[1])
        return result
    _need(
        tag == "record" and len(value) == 3 and type(value[1]) is str and value[1] in _REGISTRY,
        "admission_invalid_codec",
    )
    cls = _REGISTRY[value[1]]
    pairs = value[2]
    _need(type(pairs) is list and len(pairs) == len(fields(cls)), "admission_invalid_codec")
    kwargs = {}
    for pair, field in zip(pairs, fields(cls), strict=True):
        _need(
            type(pair) is list and len(pair) == 2 and pair[0] == field.name,
            "admission_invalid_codec",
        )
        kwargs[field.name] = _unpack(pair[1])
    try:
        return cls(**kwargs)
    except (TypeError, ValueError):
        raise AdmissionRefused("admission_invalid_codec") from None


def _encode(value, limit, validator):
    try:
        packed = _pack(value, set())  # Reject hooks before any domain validation.
        validator(value)
        text = _json(packed)
        _need(len(text.encode("utf-8")) <= limit, "admission_codec_size")
        return text
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AdmissionRefused("admission_invalid_codec") from None


def _reject_constant(_):
    raise AdmissionRefused("admission_invalid_codec")


def _decode(text, encoder, limit):
    _need(type(text) is str, "admission_invalid_codec")
    try:
        _need(len(text.encode("utf-8")) <= limit, "admission_codec_size")
        value = _unpack(json.loads(text, parse_constant=_reject_constant))
        _need(encoder(value) == text, "admission_noncanonical_codec")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, KeyError):
        raise AdmissionRefused("admission_invalid_codec") from None


def _conversion_shape(value):
    _need(type(value) is Conversion, "admission_invalid_conversion")
    _need(
        all(
            type(code) is str and re.fullmatch(r"[A-Z]{3}", code)
            for code in (value.from_currency, value.to_currency)
        ),
        "admission_invalid_conversion",
    )
    _num(value.rate, positive=True)
    _time(value.as_of)


def _quote_shape(bid, ask, as_of):
    _num(bid, positive=True)
    _num(ask, positive=True)
    _need(bid <= ask, "admission_invalid_quote")
    _time(as_of)


def _facts_shape(facts):
    _need(
        type(facts) is RiskFacts and type(facts.proposal) is ProposalFacts,
        "admission_invalid_facts",
    )
    _need(
        _id(facts.account_id) and facts.environment in ("demo", "live"), "admission_invalid_facts"
    )
    for value in (facts.complete, facts.reconciled, facts.kill_switch, facts.daily_loss_latched):
        _need(type(value) is bool, "admission_invalid_facts")
    _need(_count(facts.orders_this_session), "admission_invalid_count")
    for value in (facts.as_of, facts.session_started_at, facts.session_ends_at):
        _time(value)
    _num(facts.session_start_equity, positive=True)
    _num(facts.external_cash_flow)
    proposal = facts.proposal
    _need(type(proposal.instrument) is str, "admission_invalid_facts")
    _quote_shape(proposal.bid, proposal.ask, proposal.as_of)
    _num(proposal.base_units_per_size, positive=True)
    _num(proposal.margin_required, nonnegative=True)
    _conversion_shape(proposal.conversion)
    _need(
        type(facts.exposures) is tuple and len(facts.exposures) <= MAX_MARKS,
        "admission_invalid_exposures",
    )
    for row in facts.exposures:
        _need(
            type(row) is Exposure
            and _text(row.obligation_id)
            and type(row.instrument) is str
            and row.side in ("buy", "sell"),
            "admission_invalid_exposures",
        )
        _num(row.base_units, positive=True)
        _num(row.reference_price, positive=True)
        _num(row.stop_price, positive=True)
        _time(row.as_of)
        _conversion_shape(row.conversion)


def _evidence_shape(evidence):
    _need(type(evidence) is ReconciliationEvidence, "admission_invalid_evidence")
    _need(
        type(evidence.expected) is SnapshotFacts
        and type(evidence.observed) is SnapshotFacts
        and type(evidence.freshness) is Freshness,
        "admission_invalid_evidence",
    )
    for book in (evidence.expected, evidence.observed):
        _need(book.binding is None or type(book.binding) is Binding, "admission_invalid_evidence")
        _need(
            all(value is None or type(value) is dict for value in (book.account, book.positions)),
            "admission_invalid_evidence",
        )
        _need(
            all(
                value is None or type(value) is bool
                for value in (
                    book.account_complete,
                    book.positions_complete,
                    book.orders_complete,
                    book.protections_complete,
                )
            ),
            "admission_invalid_evidence",
        )
        _need(
            all(
                value is None or type(value) in (list, tuple)
                for value in (book.position_ids, book.order_updates, book.protective_orders)
            ),
            "admission_invalid_evidence",
        )
    for value in (evidence.freshness.max_age, evidence.freshness.max_snapshot_skew):
        _need(type(value) is timedelta and value >= timedelta(0), "admission_invalid_freshness")
    for value in (
        evidence.commands,
        evidence.command_requests,
        evidence.pending_command_ids,
        evidence.protective_obligations,
        evidence.position_marks,
    ):
        _need(type(value) is tuple, "admission_invalid_evidence")
    _need(len(evidence.position_marks) <= MAX_MARKS, "admission_invalid_evidence")
    for mark in evidence.position_marks:
        _need(type(mark) is PositionMark and _text(mark.position_id), "admission_invalid_mark")
        _quote_shape(mark.bid, mark.ask, mark.as_of)
        _num(mark.base_units_per_size, positive=True)


def _request_shape(request):
    _need(
        type(request) is AdmissionRequest and type(request.order) is dict,
        "admission_invalid_request",
    )
    _need(_id(request.order.get("client_order_id")), "admission_invalid_request_id")
    _need(type(request.order.get("schema_version")) is int, "admission_invalid_order")
    try:
        _contracts().validate("order-request", request.order)
    except ContractError:
        raise AdmissionRefused("admission_invalid_order") from None
    _facts_shape(request.facts)
    _evidence_shape(request.evidence)
    _need(
        _count(request.expected_session_revision) and _hash(request.expected_observation_sha256),
        "admission_invalid_checkpoint",
    )
    _need(
        type(request.reservation_marks) is tuple and len(request.reservation_marks) <= MAX_MARKS,
        "admission_invalid_reservation_marks",
    )
    seen = set()
    for mark in request.reservation_marks:
        _need(
            type(mark) is ReservationMark and _id(mark.request_id) and mark.request_id not in seen,
            "admission_invalid_reservation_marks",
        )
        seen.add(mark.request_id)
        _quote_shape(mark.bid, mark.ask, mark.as_of)
        _conversion_shape(mark.conversion)
        _num(mark.margin_required, nonnegative=True)


def _book_shape(policies):
    _need(
        type(policies) is tuple and 0 < len(policies) <= MAX_POLICIES,
        "admission_invalid_policy_book",
    )
    for policy in policies:
        try:
            boundary._validate_policy(policy)
        except boundary._Refusal:
            raise AdmissionRefused("admission_invalid_policy_book") from None
        _need(
            policy.max_session_orders <= MAX_COUNT
            and all(
                getattr(policy, field) <= 2147483647
                for field in (
                    "snapshot_max_age_seconds",
                    "quote_max_age_seconds",
                    "conversion_max_age_seconds",
                )
            ),
            "admission_policy_capacity",
        )
    instruments = [policy.instrument for policy in policies]
    _need(instruments == sorted(set(instruments)), "admission_invalid_policy_book")
    _need(
        all(
            all(getattr(policy, field) == getattr(policies[0], field) for field in _GLOBAL_FIELDS)
            for policy in policies
        ),
        "admission_inconsistent_policy_book",
    )


def _reservation_shape(value):
    _need(
        type(value) is Reservation and _id(value.request_id) and _id(value.session_id),
        "admission_invalid_reservation",
    )
    try:
        quote = boundary._pair(value.instrument)
    except boundary._Refusal:
        raise AdmissionRefused("admission_invalid_reservation") from None
    _need(
        value.side in ("buy", "sell") and value.quote_currency == quote,
        "admission_invalid_reservation",
    )
    for number in (
        value.base_units,
        value.entry_bound,
        value.stop,
        value.gross_budget,
        value.loss_budget,
    ):
        _num(number, positive=True)
    _num(value.margin_budget, nonnegative=True)
    _need(
        value.stop < value.entry_bound if value.side == "buy" else value.stop > value.entry_bound,
        "admission_invalid_reservation",
    )


def _assessment_shape(value):
    _need(
        type(value) is Assessment and type(value.within_limits) is bool and _text(value.code),
        "admission_invalid_assessment",
    )
    _need((value.reservation is not None) is value.within_limits, "admission_invalid_assessment")
    _need(not value.within_limits or value.code == "within_limits", "admission_invalid_assessment")
    if value.reservation is not None:
        _reservation_shape(value.reservation)
    _need(
        value.validated_external_count is None or _count(value.validated_external_count),
        "admission_invalid_assessment",
    )
    _need(
        _count(value.session_revision)
        and (value.observation_sha256 is None or _hash(value.observation_sha256))
        and _hash(value.checkpoint_sha256),
        "admission_invalid_assessment",
    )
    _need(
        type(value.effective_session_latched) is bool
        and type(value.calculations) is tuple
        and type(value.reconciliation_findings) is tuple,
        "admission_invalid_assessment",
    )
    keys = set()
    for pair in value.calculations:
        _need(
            type(pair) is tuple and len(pair) == 2 and _text(pair[0]) and pair[0] not in keys,
            "admission_invalid_assessment",
        )
        keys.add(pair[0])
        _num(pair[1])
    if value.effective_session_daily_loss is not None:
        _num(value.effective_session_daily_loss, nonnegative=True)
    _need(
        all(
            type(row) is Finding
            and all(
                type(getattr(row, key)) is str for key in ("code", "subject", "identity", "field")
            )
            for row in value.reconciliation_findings
        ),
        "admission_invalid_assessment",
    )


def _receipt_shape(value):
    _need(type(value) is dict and value.keys() == _RECEIPT_KEYS, "admission_invalid_receipt")
    _need(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["dispatch_authorized"] is False,
        "admission_invalid_receipt",
    )
    for key in (
        "request_id",
        "account_id",
        "session_id",
        "lease_authority_epoch",
        "lease_owner_id",
        "lease_resource",
    ):
        _need(_id(value[key]), "admission_invalid_receipt")
    _need(
        value["environment"] in ("demo", "live")
        and type(value["currency"]) is str
        and re.fullmatch(r"[A-Z]{3}", value["currency"]),
        "admission_invalid_receipt",
    )
    _need(
        _hash(value["request_sha256"]) and _hash(value["policy_book_sha256"]),
        "admission_invalid_receipt",
    )
    _need(
        _count(value["lease_generation"]) and value["lease_generation"] > 0,
        "admission_invalid_receipt",
    )
    _time(value["observed_at"])
    _assessment_shape(value["assessment"])
    if value["assessment"].reservation is not None:
        _need(
            value["assessment"].reservation.request_id == value["request_id"]
            and value["assessment"].reservation.session_id == value["session_id"],
            "admission_invalid_receipt",
        )


def encode_request(request):
    return _encode(request, MAX_REQUEST_BYTES, _request_shape)


def decode_request(text):
    return _decode(text, encode_request, MAX_REQUEST_BYTES)


def encode_reservation(reservation):
    return _encode(reservation, MAX_RESERVATION_BYTES, _reservation_shape)


def decode_reservation(text):
    return _decode(text, encode_reservation, MAX_RESERVATION_BYTES)


def encode_policy_book(policies):
    return _encode(policies, MAX_POLICY_BOOK_BYTES, _book_shape)


def decode_policy_book(text):
    return _decode(text, encode_policy_book, MAX_POLICY_BOOK_BYTES)


def policy_book_digest(policies):
    return fingerprint(encode_policy_book(policies))


def checkpoint_digest(evidence, facts):
    source = (
        {
            key: getattr(facts, key)
            for key in (
                "account_id",
                "environment",
                "as_of",
                "complete",
                "reconciled",
                "kill_switch",
                "orders_this_session",
                "exposures",
            )
        }
        if type(facts) is RiskFacts
        else None
    )
    return fingerprint(
        _encode(
            (evidence, source),
            MAX_REQUEST_BYTES,
            lambda _: (_evidence_shape(evidence), _facts_shape(facts)),
        )
    )


def encode_receipt(receipt):
    return _encode(receipt, MAX_RECEIPT_BYTES, _receipt_shape)


def decode_receipt(text):
    return _decode(text, encode_receipt, MAX_RECEIPT_BYTES)


def _fresh(as_of, now, ceiling, seconds, code):
    value = _time(as_of)
    _risk(value <= ceiling <= now, "future_facts")
    age = now - value
    micros = ((age.days * 86400 + age.seconds) * 1000000) + age.microseconds
    _risk(micros <= seconds * 1000000, code)


def _rate(conversion, quote, policy, now, ceiling):
    _risk(
        conversion.from_currency == quote and conversion.to_currency == policy.currency,
        "conversion_mismatch",
    )
    _risk(quote != policy.currency or conversion.rate == 1, "conversion_mismatch")
    _fresh(conversion.as_of, now, ceiling, policy.conversion_max_age_seconds, "stale_conversion")
    return conversion.rate


def assess(request, state, policies, reservations, external_count_floor, *, now):
    """Assess a frozen proposal and supplied durable projections, without authority."""
    request = decode_request(encode_request(request))
    policies = decode_policy_book(encode_policy_book(policies))
    _need(
        type(reservations) is tuple and len(reservations) <= MAX_RESERVATIONS,
        "admission_reservation_capacity",
    )
    reservations = tuple(decode_reservation(encode_reservation(row)) for row in reservations)
    _need(
        len({row.request_id for row in reservations}) == len(reservations),
        "admission_duplicate_reservation",
    )
    _need(_count(external_count_floor), "admission_invalid_count")
    try:
        # Freeze validated state using its existing canonical codec, not hooks.
        from pipmind.safety.session import decode_state

        state = decode_state(encode_state(state))
    except SessionRefused:
        raise AdmissionRefused("admission_invalid_session_state") from None
    checksum = checkpoint_digest(request.evidence, request.facts)
    progress = {"findings": (), "validated_count": None, "calculations": ()}

    def result(passed, code, reservation=None):
        value = Assessment(
            passed,
            code,
            progress["calculations"],
            reservation,
            progress["validated_count"],
            state.revision,
            state.latest_sha256,
            checksum,
            progress["findings"],
            state.daily_loss,
            state.daily_loss_latched,
        )
        _assessment_shape(value)
        return value

    try:
        with localcontext(_context()):
            return _assess(
                request,
                state,
                policies,
                reservations,
                external_count_floor,
                now,
                checksum,
                result,
                progress,
            )
    except _AssessmentRefused as error:
        return result(False, str(error))
    except SessionRefused as error:
        return result(False, str(error))
    except DecimalException:
        return result(False, "numeric_capacity_exceeded")
    except AdmissionRefused:
        return result(False, "invalid_source_facts")


def _source_number(value, *, positive=False, nonnegative=False):
    if type(value) is int:
        value = Decimal(value)
    try:
        return _num(value, positive=positive, nonnegative=nonnegative)
    except AdmissionRefused:
        raise _AssessmentRefused("invalid_source_number") from None


def _assess(request, state, policies, reservations, floor, now, checksum, result, progress):
    facts, evidence, order = request.facts, request.evidence, request.order
    book = {policy.instrument: policy for policy in policies}
    _risk(order.get("instrument") in book, "instrument_not_configured")
    policy = book[order["instrument"]]
    binding = state.spec.binding
    _risk(
        (policy.account_id, policy.environment, policy.currency)
        == (binding.account_id, binding.environment, binding.currency)
        and (facts.account_id, facts.environment) == (binding.account_id, binding.environment),
        "account_binding_mismatch",
    )
    _risk(
        state.spec.policy_id == policy.policy_id
        and state.spec.policy_sha256 == policy_book_digest(policies)
        and state.spec.max_daily_loss == policy.max_daily_loss,
        "session_policy_mismatch",
    )
    _risk(
        request.expected_session_revision == state.revision
        and request.expected_observation_sha256 == state.latest_sha256,
        "session_checkpoint_changed",
    )
    require_current(state, now=now)
    age = timedelta(seconds=policy.snapshot_max_age_seconds)
    _risk(
        evidence.freshness.max_age <= age and evidence.freshness.max_snapshot_skew <= age,
        "unbounded_reconciliation_freshness",
    )
    reconciled = reconcile(
        evidence.expected,
        evidence.observed,
        observed_at=now,
        freshness=evidence.freshness,
        commands=evidence.commands,
        command_requests=evidence.command_requests,
        pending_command_ids=evidence.pending_command_ids,
        protective_obligations=evidence.protective_obligations,
    )
    progress["findings"] = reconciled.findings
    _risk(reconciled.consistent, "reconciliation_failed")
    account = evidence.observed.account
    _risk(type(account) is dict, "incomplete_account")
    _risk(
        type(account.get("schema_version")) is int and account["schema_version"] == 1,
        "invalid_account",
    )
    _risk(
        type(evidence.observed.binding) is Binding
        and (evidence.observed.binding.account_id, evidence.observed.binding.environment)
        == (binding.account_id, binding.environment),
        "account_binding_mismatch",
    )
    _risk(
        facts.complete
        and facts.reconciled
        and evidence.observed.account_complete is True
        and evidence.observed.positions_complete is True
        and evidence.observed.orders_complete is True
        and evidence.observed.protections_complete is True
        and type(evidence.observed.protective_orders) in (list, tuple),
        "incomplete_source_scope",
    )
    _risk(
        account.get("currency") == binding.currency and facts.as_of == account.get("as_of"),
        "account_snapshot_mismatch",
    )
    _risk(
        state.latest.account_evidence_id == checksum
        and _time(state.latest.as_of) == _time(account["as_of"])
        and state.latest.equity == account.get("equity"),
        "source_checkpoint_mismatch",
    )
    ceiling = _time(account["as_of"])
    _fresh(account["as_of"], now, now, policy.snapshot_max_age_seconds, "stale_account")
    for key in ("balance", "equity", "margin_used", "margin_available"):
        _risk(type(account.get(key)) is Decimal, "invalid_account")
        _source_number(account[key], positive=key == "equity", nonnegative=key != "equity")
    _risk(
        account["equity"] == account["margin_used"] + account["margin_available"],
        "inconsistent_account",
    )
    positions = evidence.observed.positions
    _risk(
        type(positions) is dict
        and type(positions.get("schema_version")) is int
        and positions["schema_version"] == 1,
        "invalid_positions",
    )
    rows, ids = positions["positions"], evidence.observed.position_ids
    _risk(
        type(rows) in (list, tuple) and type(ids) in (list, tuple) and len(rows) == len(ids),
        "incomplete_position_identities",
    )
    active = {
        identity: row for identity, row in zip(ids, rows, strict=True) if row["side"] != "flat"
    }
    exposures = {row.obligation_id: row for row in facts.exposures}
    marks = {row.position_id: row for row in evidence.position_marks}
    _risk(
        len(exposures) == len(facts.exposures) and len(marks) == len(evidence.position_marks),
        "duplicate_position_evidence",
    )
    _risk(exposures.keys() == active.keys() == marks.keys(), "position_coverage_mismatch")
    protections = {}
    for protection in evidence.observed.protective_orders:
        protections.setdefault(protection.position_id, []).append(protection)
    units_by_instrument = {}
    for identity, row in active.items():
        exposure, mark = exposures[identity], marks[identity]
        _risk(row["instrument"] in book, "instrument_not_configured")
        own_policy = book[row["instrument"]]
        _risk(
            exposure.instrument == row["instrument"] and exposure.side == row["side"],
            "exposure_binding_mismatch",
        )
        units = _source_number(row["size"], positive=True) * mark.base_units_per_size
        _risk(exposure.base_units == units, "exposure_units_mismatch")
        _risk(
            exposure.reference_price == (mark.bid if row["side"] == "buy" else mark.ask)
            and _time(exposure.as_of) == _time(mark.as_of),
            "exposure_mark_mismatch",
        )
        _fresh(mark.as_of, now, ceiling, own_policy.quote_max_age_seconds, "stale_position_mark")
        _fresh(exposure.as_of, now, ceiling, policy.quote_max_age_seconds, "stale_exposure")
        _rate(exposure.conversion, row["instrument"].split("/")[1], own_policy, now, ceiling)
        _rate(exposure.conversion, row["instrument"].split("/")[1], policy, now, ceiling)
        observed_protections = protections.get(identity, [])
        _risk(len(observed_protections) == 1, "ambiguous_position_protection")
        protection = observed_protections[0].request
        _risk(protection.get("action") in ("modify", "close"), "unsupported_protection_action")
        order_updates = [
            update
            for update in evidence.observed.order_updates
            if update["client_order_id"] == protection["client_order_id"]
        ]
        _risk(
            len(order_updates) == 1
            and type(order_updates[0].get("filled_size")) is Decimal
            and order_updates[0]["filled_size"] == 0,
            "incomplete_protection_fill",
        )
        _risk(
            protection.get("side") == ("sell" if row["side"] == "buy" else "buy"),
            "wrong_protection_side",
        )
        _risk(
            protection.get("size") is not None
            and _source_number(protection["size"], positive=True)
            == _source_number(row["size"], positive=True),
            "incomplete_protection_size",
        )
        _risk(
            type(protection.get("stop")) is Decimal and protection["stop"] == exposure.stop_price,
            "protection_stop_mismatch",
        )
        _risk(
            exposure.stop_price < exposure.reference_price
            if row["side"] == "buy"
            else exposure.stop_price > exposure.reference_price,
            "invalid_exposure_stop",
        )
        units_by_instrument[row["instrument"]] = (
            units_by_instrument.get(row["instrument"], Decimal(0)) + units
        )
    # Pending venue opens have no terminal-ledger integration in this slice.
    protective_ids = {row.request["client_order_id"] for row in evidence.observed.protective_orders}
    _risk(
        all(
            row["status"] in ("filled", "cancelled", "rejected")
            or row["client_order_id"] in protective_ids
            for row in evidence.observed.order_updates
        ),
        "unsupported_pending_order",
    )
    _risk(facts.orders_this_session >= floor, "external_count_regression")
    progress["validated_count"] = facts.orders_this_session
    _risk(len(reservations) < MAX_RESERVATIONS, "reservation_capacity")
    _risk(
        order["client_order_id"] not in {row.request_id for row in reservations},
        "request_already_reserved",
    )
    reservation_marks = {mark.request_id: mark for mark in request.reservation_marks}
    reserved_gross = reserved_loss = reserved_margin = Decimal(0)
    own_count = 0
    for row in reservations:
        _risk(row.instrument in book, "instrument_not_configured")
        _risk(row.request_id in reservation_marks, "missing_reservation_mark")
        own_policy, mark = book[row.instrument], reservation_marks[row.request_id]
        _fresh(mark.as_of, now, ceiling, own_policy.quote_max_age_seconds, "stale_reservation_mark")
        rate = _rate(mark.conversion, row.quote_currency, own_policy, now, ceiling)
        reserved_gross += max(
            row.gross_budget, row.base_units * max(row.entry_bound, mark.ask) * rate
        )
        reserved_loss += max(
            row.loss_budget, abs(row.entry_bound - row.stop) * row.base_units * rate
        )
        reserved_margin += max(row.margin_budget, mark.margin_required)
        units_by_instrument[row.instrument] = (
            units_by_instrument.get(row.instrument, Decimal(0)) + row.base_units
        )
        own_count += row.session_id == state.spec.session_id
    progress["calculations"] = tuple(
        (name, _derived(value))
        for name, value in (
            ("reserved_gross_exposure", reserved_gross),
            ("reserved_planned_loss", reserved_loss),
            ("reserved_margin", reserved_margin),
            ("reserved_opens_this_session", Decimal(own_count)),
        )
    )
    _risk(account["margin_available"] >= reserved_margin, "insufficient_margin")
    _risk(facts.orders_this_session + own_count <= MAX_COUNT, "count_capacity_exceeded")
    effective = replace(
        facts,
        daily_loss_latched=state.daily_loss_latched,
        session_started_at=state.spec.started_at,
        session_ends_at=state.spec.ends_at,
        session_start_equity=state.spec.opening_equity,
        external_cash_flow=state.latest.external_net_cash_flow,
        orders_this_session=facts.orders_this_session + own_count,
    )
    risk_account = {
        **account,
        "margin_used": _derived(account["margin_used"] + reserved_margin),
        "margin_available": _derived(account["margin_available"] - reserved_margin),
    }
    checked = boundary.preflight(order, risk_account, effective, policy, now=now)
    computed = dict(checked.calculations)
    if computed:
        computed["gross_exposure"] += reserved_gross
        computed["total_planned_loss"] += reserved_loss
        computed["daily_risk"] += reserved_loss
        computed["instrument_gross_base_units"] += sum(
            (row.base_units for row in reservations if row.instrument == policy.instrument),
            Decimal(0),
        )
        computed.update(dict(progress["calculations"]))
        progress["calculations"] = tuple(
            (name, _derived(value)) for name, value in computed.items()
        )
    _risk(checked.within_limits, checked.code)
    units_by_instrument[policy.instrument] = (
        units_by_instrument.get(policy.instrument, Decimal(0)) + computed["order_base_units"]
    )
    _risk(
        all(
            units <= book[instrument].max_instrument_base_units
            for instrument, units in units_by_instrument.items()
        ),
        "instrument_exposure_limit",
    )
    for condition, code in (
        (computed["gross_exposure"] <= policy.max_gross_exposure, "gross_exposure_limit"),
        (computed["total_planned_loss"] <= policy.max_total_planned_loss, "total_loss_limit"),
        (computed["daily_risk"] <= policy.max_daily_risk, "daily_risk_limit"),
    ):
        _risk(condition, code)
    reservation = Reservation(
        order["client_order_id"],
        state.spec.session_id,
        policy.instrument,
        order["side"],
        _derived(computed["order_base_units"]),
        _derived(computed["order_entry_price"]),
        order["stop"],
        policy.instrument.split("/")[1],
        _derived(
            computed["order_base_units"]
            * computed["order_gross_price"]
            * facts.proposal.conversion.rate
        ),
        _derived(computed["order_planned_loss"]),
        facts.proposal.margin_required,
    )
    return result(True, "within_limits", reservation)
