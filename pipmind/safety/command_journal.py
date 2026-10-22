"""Exact local command/attempt audit facts. No risk admission or send authority."""

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from functools import lru_cache

from pipmind.contracts import Contracts, utc
from pipmind.safety.incarnation import (
    DatabaseIncarnation,
    decode_incarnation,
    encode_incarnation,
)
from pipmind.safety.recovery_sql import (
    Assignment,
    RobotReference,
    _assignment,
    _parse_assignment,
)
from pipmind.safety.session import SessionBinding

MAX_ORDER_BYTES = 16384
MAX_RECORD_BYTES = 65536
MAX_ID_UNITS = 128
MAX_GENERATION = 9223372036854775807
MAX_DECIMAL_DIGITS = 64
MAX_DECIMAL_EXPONENT = 64
RECORDED_CODES = frozenset({"locally_recorded"})
REFUSED_CODES = frozenset({"locally_refused", "unsupported_operation"})
UNKNOWN_CODES = frozenset({"outcome_unresolved"})


class JournalRefused(Exception):
    """Fixed invalid identity/fact/transition code, never dynamic payload text."""


def _need(ok, code="journal_invalid_fact"):
    if not ok:
        raise JournalRefused(code)


def _id(value):
    try:
        valid = (
            type(value) is str
            and bool(value)
            and value == value.strip()
            and "\x00" not in value
            and len(value.encode("utf-16-le")) <= 2 * MAX_ID_UNITS
        )
    except UnicodeError:
        valid = False
    _need(valid, "journal_invalid_id")


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _counter(value, *, positive=False):
    _need(
        type(value) is int and (1 if positive else 0) <= value <= MAX_GENERATION,
        "journal_invalid_counter",
    )


def _time(value):
    _need(type(value) is str, "journal_invalid_time")
    try:
        return utc(value)
    except Exception:
        raise JournalRefused("journal_invalid_time") from None


def fingerprint(body):
    _need(type(body) is str, "journal_invalid_body")
    try:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise JournalRefused("journal_invalid_body") from None


@lru_cache(maxsize=1)
def _contracts():
    # Local normative schemas; no SQL, network or remote-reference resolution.
    return Contracts()


def _number(value):
    if type(value) is int:
        _need(len(str(abs(value))) <= MAX_DECIMAL_DIGITS, "journal_numeric_capacity")
        return str(value)
    _need(type(value) is Decimal and value.is_finite(), "journal_invalid_number")
    parts = value.as_tuple()
    _need(
        len(parts.digits) <= MAX_DECIMAL_DIGITS and abs(parts.exponent) <= MAX_DECIMAL_EXPONENT,
        "journal_numeric_capacity",
    )
    # Decimal.__str__ observes capitals; isolate it to make hashes independent
    # of ambient context without rounding/normalizing supplied representations.
    with localcontext() as context:
        context.capitals = 1
        return str(value)


def encode_order(order):
    """Canonical flat normative v1 JSON, numeric int/Decimal literals, no floats.

    This stricter exact-number representation is an audit codec, not a broker DTO.
    Decimal spelling/negative zero survives; decoded quantities are Decimal.
    """
    try:
        _need(
            type(order) is dict and all(type(key) is str for key in order), "journal_invalid_order"
        )
        _need(
            type(order.get("schema_version")) is int and order["schema_version"] == 1,
            "journal_invalid_order",
        )
        _id(order.get("client_order_id"))
        if "related_client_order_id" in order:
            _id(order["related_client_order_id"])
        pieces = []
        for key in sorted(order):
            value = order[key]
            if value is None:
                raw = "null"
            elif type(value) is str:
                value.encode("utf-8")
                raw = json.dumps(value, ensure_ascii=False)
            else:
                raw = _number(value)
            pieces.append(json.dumps(key, ensure_ascii=False) + ":" + raw)
        body = "{" + ",".join(pieces) + "}"
        _need(len(body.encode("utf-8")) <= MAX_ORDER_BYTES, "journal_order_too_large")
        _captured_order(body)
        return body
    except JournalRefused:
        raise
    except Exception:
        raise JournalRefused("journal_invalid_order") from None


canonical_order = encode_order


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        _need(key not in data, "journal_noncanonical_body")
        data[key] = value
    return data


def _constant(_value):
    raise JournalRefused("journal_invalid_number")


def _captured_order(body):
    """Validate exactly captured bytes, never the mutable caller mapping."""
    _need(
        type(body) is str and len(body.encode("utf-8")) <= MAX_ORDER_BYTES, "journal_invalid_body"
    )
    order = json.loads(
        body,
        object_pairs_hook=_pairs,
        parse_int=Decimal,
        parse_float=Decimal,
        parse_constant=_constant,
    )
    _need(type(order) is dict, "journal_invalid_order")
    if type(order.get("schema_version")) is Decimal and order["schema_version"] == 1:
        order["schema_version"] = 1
    _need(
        type(order.get("schema_version")) is int and order["schema_version"] == 1,
        "journal_invalid_order",
    )
    _id(order.get("client_order_id"))
    if "related_client_order_id" in order:
        _id(order["related_client_order_id"])
    # Captured JSON has no user-defined objects. Validate numeric capacity and
    # flat scalar shape here too, rather than trusting the pre-capture source.
    for value in order.values():
        if value is None or type(value) is str:
            continue
        _number(value)
    _contracts().validate("order-request", order)
    return order


def decode_order(body):
    try:
        order = _captured_order(body)
        _need(encode_order(order) == body, "journal_noncanonical_body")
        return order
    except JournalRefused:
        raise
    except Exception:
        raise JournalRefused("journal_invalid_order") from None


def _plain(value, active=None):
    if active is None:
        active = set()
    if value is None or type(value) in (str, int, bool):
        return
    _need(type(value) in (dict, list), "journal_unsupported_type")
    _need(id(value) not in active, "journal_cyclic_fact")
    active.add(id(value))
    try:
        if type(value) is dict:
            _need(all(type(k) is str for k in value), "journal_invalid_mapping")
            for child in value.values():
                _plain(child, active)
        else:
            for child in value:
                _plain(child, active)
    finally:
        active.remove(id(value))


def _json(value):
    try:
        _plain(value)
        body = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        _need(len(body.encode("utf-8")) <= MAX_RECORD_BYTES, "journal_record_too_large")
        return body
    except JournalRefused:
        raise
    except Exception:
        raise JournalRefused("journal_invalid_body") from None


def _load(body):
    try:
        _need(
            type(body) is str and len(body.encode("utf-8")) <= MAX_RECORD_BYTES,
            "journal_invalid_body",
        )
        value = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
        _need(_json(value) == body, "journal_noncanonical_body")
        return value
    except JournalRefused:
        raise
    except Exception:
        raise JournalRefused("journal_invalid_body") from None


def _assignment_payload(value):
    _need(
        type(value) is Assignment
        and type(value.binding) is SessionBinding
        and type(value.robot) is RobotReference,
        "journal_invalid_assignment",
    )
    for item in (
        value.binding.account_id,
        value.binding.environment,
        value.binding.currency,
        value.assignment_id,
        value.epoch,
        value.robot.robot_id,
        value.robot.version,
    ):
        _id(item)
    _need(type(value.activated_at) is str, "journal_invalid_assignment")
    try:
        payload = _assignment(value)
        _need(payload is not None, "journal_invalid_assignment")
        return payload
    except JournalRefused:
        raise
    except Exception:
        raise JournalRefused("journal_invalid_assignment") from None


@dataclass(frozen=True, slots=True)
class JournalScope:
    assignment: Assignment
    owner_id: str
    lease_generation: int
    lease_resource: str
    lease_expires_at: str
    application_incarnation: DatabaseIncarnation
    control_revision: int
    dispatch_authorized: bool = False


@dataclass(frozen=True, slots=True)
class CommandIntent:
    command_id: str
    order_body: str
    scope: JournalScope
    causation_id: str
    local_outcome: str
    code: str
    recorded_at: str
    dispatch_authorized: bool = False


@dataclass(frozen=True, slots=True)
class AttemptIntent:
    command_id: str
    command_sha256: str
    attempt_id: str
    scope: JournalScope
    recorded_at: str
    dispatch_authorized: bool = False


@dataclass(frozen=True, slots=True)
class UnknownOutcome:
    command_id: str
    command_sha256: str
    attempt_sha256: str
    scope: JournalScope
    code: str
    recorded_at: str
    dispatch_authorized: bool = False


@dataclass(frozen=True, slots=True)
class JournalProjection:
    command: CommandIntent
    attempt: AttemptIntent | None
    unknown: UnknownOutcome | None
    state: str
    dispatch_authorized: bool = False

    @property
    def unresolved(self):
        return self.attempt is not None


def _scope_payload(scope):
    _need(
        type(scope) is JournalScope and scope.dispatch_authorized is False, "journal_invalid_scope"
    )
    assignment = _assignment_payload(scope.assignment)
    _id(scope.owner_id)
    _id(scope.lease_resource)
    _counter(scope.lease_generation, positive=True)
    _counter(scope.control_revision)
    _time(scope.lease_expires_at)
    try:
        pin = json.loads(encode_incarnation(scope.application_incarnation))
    except Exception:
        raise JournalRefused("journal_invalid_incarnation") from None
    return {
        "assignment": assignment,
        "owner_id": scope.owner_id,
        "lease_generation": scope.lease_generation,
        "lease_resource": scope.lease_resource,
        "lease_expires_at": scope.lease_expires_at,
        "application_incarnation": pin,
        "control_revision": scope.control_revision,
        "dispatch_authorized": False,
    }


def _parse_scope(data):
    _need(
        type(data) is dict
        and set(data)
        == {
            "assignment",
            "owner_id",
            "lease_generation",
            "lease_resource",
            "lease_expires_at",
            "application_incarnation",
            "control_revision",
            "dispatch_authorized",
        },
        "journal_invalid_scope",
    )
    try:
        scope = JournalScope(
            _parse_assignment(data["assignment"]),
            data["owner_id"],
            data["lease_generation"],
            data["lease_resource"],
            data["lease_expires_at"],
            decode_incarnation(json.dumps(data["application_incarnation"], separators=(",", ":"))),
            data["control_revision"],
            data["dispatch_authorized"],
        )
        _scope_payload(scope)
        return scope
    except JournalRefused:
        raise
    except Exception:
        raise JournalRefused("journal_invalid_scope") from None


def encode_scope(scope):
    return _json(_scope_payload(scope))


def decode_scope(body):
    scope = _parse_scope(_load(body))
    _need(encode_scope(scope) == body, "journal_noncanonical_body")
    return scope


def _event_time(scope, recorded_at):
    at = _time(recorded_at)
    _need(
        _time(scope.assignment.activated_at) <= at < _time(scope.lease_expires_at),
        "journal_invalid_chronology",
    )
    return at


def encode_command(command):
    _need(
        type(command) is CommandIntent and command.dispatch_authorized is False,
        "journal_invalid_command",
    )
    _id(command.command_id)
    _id(command.causation_id)
    scope = _scope_payload(command.scope)
    order = decode_order(command.order_body)
    _need(order["client_order_id"] == command.command_id, "journal_command_identity_mismatch")
    _need(
        type(command.local_outcome) is str and type(command.code) is str,
        "journal_invalid_local_outcome",
    )
    _need(
        (command.local_outcome == "recorded" and command.code in RECORDED_CODES)
        or (command.local_outcome == "refused" and command.code in REFUSED_CODES),
        "journal_invalid_local_outcome",
    )
    _event_time(command.scope, command.recorded_at)
    return _json(
        {
            "schema_version": 1,
            "kind": "command_intent",
            "command_id": command.command_id,
            "order_body": command.order_body,
            "scope": scope,
            "causation_id": command.causation_id,
            "local_outcome": command.local_outcome,
            "code": command.code,
            "recorded_at": command.recorded_at,
            "dispatch_authorized": False,
        }
    )


def encode_attempt(attempt):
    _need(
        type(attempt) is AttemptIntent and attempt.dispatch_authorized is False,
        "journal_invalid_attempt",
    )
    _id(attempt.command_id)
    _id(attempt.attempt_id)
    _need(_sha(attempt.command_sha256), "journal_invalid_reference")
    scope = _scope_payload(attempt.scope)
    _event_time(attempt.scope, attempt.recorded_at)
    return _json(
        {
            "schema_version": 1,
            "kind": "attempt_intent",
            "command_id": attempt.command_id,
            "command_sha256": attempt.command_sha256,
            "attempt_id": attempt.attempt_id,
            "scope": scope,
            "recorded_at": attempt.recorded_at,
            "dispatch_authorized": False,
        }
    )


def encode_unknown(unknown):
    _need(
        type(unknown) is UnknownOutcome and unknown.dispatch_authorized is False,
        "journal_invalid_unknown",
    )
    _id(unknown.command_id)
    _need(
        _sha(unknown.command_sha256) and _sha(unknown.attempt_sha256), "journal_invalid_reference"
    )
    _need(type(unknown.code) is str and unknown.code in UNKNOWN_CODES, "journal_invalid_unknown")
    scope = _scope_payload(unknown.scope)
    _event_time(unknown.scope, unknown.recorded_at)
    return _json(
        {
            "schema_version": 1,
            "kind": "unknown_outcome",
            "command_id": unknown.command_id,
            "command_sha256": unknown.command_sha256,
            "attempt_sha256": unknown.attempt_sha256,
            "scope": scope,
            "code": unknown.code,
            "recorded_at": unknown.recorded_at,
            "dispatch_authorized": False,
        }
    )


def _decode_record(body, kind, keys):
    data = _load(body)
    _need(
        type(data) is dict
        and set(data) == keys
        and type(data["schema_version"]) is int
        and data["schema_version"] == 1
        and data["kind"] == kind,
        "journal_invalid_record",
    )
    return data


def decode_command(body):
    data = _decode_record(
        body,
        "command_intent",
        {
            "schema_version",
            "kind",
            "command_id",
            "order_body",
            "scope",
            "causation_id",
            "local_outcome",
            "code",
            "recorded_at",
            "dispatch_authorized",
        },
    )
    command = CommandIntent(
        data["command_id"],
        data["order_body"],
        _parse_scope(data["scope"]),
        data["causation_id"],
        data["local_outcome"],
        data["code"],
        data["recorded_at"],
        data["dispatch_authorized"],
    )
    _need(encode_command(command) == body, "journal_noncanonical_body")
    return command


def decode_attempt(body):
    data = _decode_record(
        body,
        "attempt_intent",
        {
            "schema_version",
            "kind",
            "command_id",
            "command_sha256",
            "attempt_id",
            "scope",
            "recorded_at",
            "dispatch_authorized",
        },
    )
    attempt = AttemptIntent(
        data["command_id"],
        data["command_sha256"],
        data["attempt_id"],
        _parse_scope(data["scope"]),
        data["recorded_at"],
        data["dispatch_authorized"],
    )
    _need(encode_attempt(attempt) == body, "journal_noncanonical_body")
    return attempt


def decode_unknown(body):
    data = _decode_record(
        body,
        "unknown_outcome",
        {
            "schema_version",
            "kind",
            "command_id",
            "command_sha256",
            "attempt_sha256",
            "scope",
            "code",
            "recorded_at",
            "dispatch_authorized",
        },
    )
    unknown = UnknownOutcome(
        data["command_id"],
        data["command_sha256"],
        data["attempt_sha256"],
        _parse_scope(data["scope"]),
        data["code"],
        data["recorded_at"],
        data["dispatch_authorized"],
    )
    _need(encode_unknown(unknown) == body, "journal_noncanonical_body")
    return unknown


def validate_attempt(command, attempt):
    command_hash = fingerprint(encode_command(command))
    encode_attempt(attempt)
    _need(command.local_outcome == "recorded", "journal_refused_command")
    _need(
        attempt.command_id == command.command_id and attempt.command_sha256 == command_hash,
        "journal_command_reference_mismatch",
    )
    prior, current = command.scope, attempt.scope
    _need(
        current.assignment == prior.assignment
        and current.application_incarnation == prior.application_incarnation
        and current.lease_resource == prior.lease_resource,
        "journal_attempt_scope_mismatch",
    )
    _need(
        current.lease_generation >= prior.lease_generation
        and (
            current.lease_generation != prior.lease_generation or current.owner_id == prior.owner_id
        )
        and current.control_revision >= prior.control_revision,
        "journal_attempt_scope_regression",
    )
    _need(_time(attempt.recorded_at) >= _time(command.recorded_at), "journal_invalid_chronology")
    return attempt


def validate_unknown(command, attempt, unknown):
    _need(attempt is not None, "journal_attempt_missing")
    validate_attempt(command, attempt)
    encode_unknown(unknown)
    _need(
        unknown.command_id == command.command_id
        and unknown.command_sha256 == fingerprint(encode_command(command))
        and unknown.attempt_sha256 == fingerprint(encode_attempt(attempt)),
        "journal_unknown_reference_mismatch",
    )
    _need(
        unknown.scope.assignment.binding == command.scope.assignment.binding
        and unknown.scope.lease_resource == command.scope.lease_resource,
        "journal_unknown_scope_mismatch",
    )
    _need(_time(unknown.recorded_at) >= _time(attempt.recorded_at), "journal_invalid_chronology")
    return unknown


def project(command, attempt=None, unknown=None):
    """Immutable audit projection only; unresolved marker never implies resend."""
    encode_command(command)
    if attempt is not None:
        validate_attempt(command, attempt)
    if unknown is not None:
        validate_unknown(command, attempt, unknown)
    state = (
        "unknown"
        if unknown is not None
        else "attempt_intent"
        if attempt is not None
        else command.local_outcome
    )
    return JournalProjection(command, attempt, unknown, state)
