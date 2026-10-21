"""Pure cumulative session accounting; supplied evidence is not source attestation."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Context, Decimal, DecimalException, localcontext

MAX_CODEC_BYTES = 32768
MAX_REVISION = 9223372036854775807
MAX_SNAPSHOT_AGE_SECONDS = 2147483647
_HASH = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", re.ASCII)


class SessionRefused(Exception):
    """A fixed-code refusal, with no mutation or I/O."""


class SessionUnavailable(Exception):
    """Fixed-code storage failure; no state authority is inferred."""


class SessionCommitUnknown(Exception):
    """Commit acknowledgement unknown; never automatically replay."""


@dataclass(frozen=True, slots=True)
class SessionBinding:
    account_id: str
    environment: str
    currency: str


@dataclass(frozen=True, slots=True)
class SessionSpec:
    binding: SessionBinding
    session_id: str
    policy_id: str
    policy_sha256: str
    started_at: str
    ends_at: str
    opening_equity: Decimal
    opening_evidence_id: str
    max_daily_loss: Decimal
    snapshot_max_age_seconds: int


@dataclass(frozen=True, slots=True)
class EquityObservation:
    binding: SessionBinding
    session_id: str
    observation_id: str
    as_of: str
    received_at: str
    equity: Decimal
    external_net_cash_flow: Decimal
    account_complete: bool
    cash_flow_complete: bool
    cash_flow_started_at: str | None
    cash_flow_ends_at: str | None
    account_evidence_id: str
    cash_flow_evidence_id: str | None


@dataclass(frozen=True, slots=True)
class SessionState:
    spec: SessionSpec
    revision: int
    latest: EquityObservation | None
    latest_sha256: str | None
    daily_loss: Decimal | None
    daily_loss_latched: bool


@dataclass(frozen=True, slots=True)
class ObservationReceipt:
    observation_id: str
    payload_sha256: str
    revision: int
    became_current: bool
    daily_loss: Decimal | None
    daily_loss_latched: bool


def _need(condition, code):
    if not condition:
        raise SessionRefused(code)


def _identifier(value):
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-16-le")) <= 256
    except UnicodeError:
        return False


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


def _decimal(value, *, positive=False, nonnegative=False, code="session_invalid_decimal"):
    _need(type(value) is Decimal and value.is_finite(), code)
    parts = value.as_tuple()
    _need(len(parts.digits) <= 64 and abs(parts.exponent) <= 64, code)
    if positive:
        _need(value > 0, code)
    if nonnegative:
        _need(value >= 0, code)


def _timestamp(value):
    _need(type(value) is str and _TIME.fullmatch(value) is not None, "session_invalid_time")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise SessionRefused("session_invalid_time") from None


def _now(value):
    _need(type(value) is datetime and value.tzinfo is not None, "session_invalid_now")
    try:
        offset = value.utcoffset()
    except Exception:
        raise SessionRefused("session_invalid_now") from None
    _need(offset == timedelta(0), "session_invalid_now")
    return value


def validate_binding(binding):
    _need(
        type(binding) is SessionBinding
        and _identifier(binding.account_id)
        and type(binding.environment) is str
        and binding.environment in ("demo", "live")
        and type(binding.currency) is str
        and re.fullmatch(r"[A-Z]{3}", binding.currency, re.ASCII) is not None,
        "session_invalid_binding",
    )


def fingerprint(text):
    _need(type(text) is str, "session_invalid_fingerprint")
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise SessionRefused("session_invalid_fingerprint") from None


def account_key(binding):
    validate_binding(binding)
    return fingerprint(_json([binding.account_id, binding.environment]))


def validate_spec(spec):
    _need(type(spec) is SessionSpec, "session_invalid_spec")
    validate_binding(spec.binding)
    _need(
        all(
            _identifier(value)
            for value in (spec.session_id, spec.policy_id, spec.opening_evidence_id)
        )
        and _hash(spec.policy_sha256),
        "session_invalid_spec",
    )
    _need(_timestamp(spec.started_at) < _timestamp(spec.ends_at), "session_invalid_interval")
    _decimal(spec.opening_equity, positive=True)
    _decimal(spec.max_daily_loss, positive=True)
    _need(
        type(spec.snapshot_max_age_seconds) is int
        and 0 < spec.snapshot_max_age_seconds <= MAX_SNAPSHOT_AGE_SECONDS,
        "session_invalid_snapshot_age",
    )


def _loss(spec, observation):
    if not (observation.account_complete and observation.cash_flow_complete):
        return None
    # Explicit context prevents caller precision, exponent bounds and traps from
    # rounding or changing this arithmetic. Inputs bound the exact work far below1024.
    context = Context(prec=1024, Emin=-999999, Emax=999999)
    for signal in context.traps:
        context.traps[signal] = True
    try:
        with localcontext(context):
            adjusted = spec.opening_equity + observation.external_net_cash_flow
            _need(adjusted > 0, "session_invalid_adjusted_equity")
            loss = max(Decimal(0), adjusted - observation.equity)
            # Arithmetic may retain redundant fractional zeros from an input,
            # growing a representable result beyond the coefficient envelope.
            # Reduce only exact trailing zeros needed for this DERIVED output;
            # supplied Decimal values and their codecs are never normalized.
            sign, digits, exponent = loss.as_tuple()
            while len(digits) > 64 and digits[-1] == 0 and exponent < 64:
                digits, exponent = digits[:-1], exponent + 1
            loss = Decimal((sign, digits, exponent))
            _decimal(loss, nonnegative=True, code="session_arithmetic_overflow")
            return loss
    except DecimalException:
        raise SessionRefused("session_arithmetic_overflow") from None


def validate_observation(spec, observation, *, now):
    validate_spec(spec)
    _validate_observation_shape(observation)
    _need(observation.binding == spec.binding, "session_binding_mismatch")
    _need(observation.session_id == spec.session_id, "session_binding_mismatch")
    _need(
        _identifier(observation.observation_id) and _identifier(observation.account_evidence_id),
        "session_invalid_observation",
    )
    as_of, received_at = _timestamp(observation.as_of), _timestamp(observation.received_at)
    _need(
        _timestamp(spec.started_at) <= as_of < _timestamp(spec.ends_at),
        "session_observation_outside_interval",
    )
    _need(as_of <= received_at <= _now(now), "session_future_observation")
    _decimal(observation.equity, positive=True)
    _decimal(observation.external_net_cash_flow)
    _need(
        type(observation.account_complete) is bool and type(observation.cash_flow_complete) is bool,
        "session_invalid_completeness",
    )
    for value in (observation.cash_flow_started_at, observation.cash_flow_ends_at):
        if value is not None:
            _timestamp(value)
    if observation.cash_flow_evidence_id is not None:
        _need(_identifier(observation.cash_flow_evidence_id), "session_invalid_flow_evidence")
    if observation.cash_flow_complete:
        _need(
            observation.cash_flow_started_at is not None
            and observation.cash_flow_ends_at is not None
            and observation.cash_flow_evidence_id is not None,
            "session_incomplete_cash_flow",
        )
        _need(
            _timestamp(observation.cash_flow_started_at) == _timestamp(spec.started_at)
            and _timestamp(observation.cash_flow_ends_at) == as_of,
            "session_incoherent_cash_flow",
        )
    _loss(spec, observation)


def initial_state(spec):
    validate_spec(spec)
    return SessionState(spec, 0, None, None, None, False)


def _validate_state(state):
    _need(type(state) is SessionState, "session_invalid_state")
    validate_spec(state.spec)
    _need(
        type(state.revision) is int
        and 0 <= state.revision <= MAX_REVISION
        and type(state.daily_loss_latched) is bool,
        "session_invalid_state",
    )
    if state.latest is None:
        _need(
            state.revision == 0
            and state.latest_sha256 is None
            and state.daily_loss is None
            and not state.daily_loss_latched,
            "session_invalid_state",
        )
        return
    _need(type(state.latest) is EquityObservation and state.revision > 0, "session_invalid_state")
    validate_observation(state.spec, state.latest, now=_timestamp(state.latest.received_at))
    _need(
        _hash(state.latest_sha256)
        and state.latest_sha256 == fingerprint(encode_observation(state.latest)),
        "session_state_hash_mismatch",
    )
    expected_loss = _loss(state.spec, state.latest)
    if state.daily_loss is not None:
        _decimal(state.daily_loss, nonnegative=True)
    _need(state.daily_loss == expected_loss, "session_incoherent_state_loss")
    _need(
        expected_loss is None
        or expected_loss < state.spec.max_daily_loss
        or state.daily_loss_latched,
        "session_incoherent_state_latch",
    )


def advance(state, observation, *, now):
    _validate_state(state)
    validate_observation(state.spec, observation, now=now)
    _need(state.revision < MAX_REVISION, "session_revision_exhausted")
    if state.latest is not None:
        _need(
            observation.observation_id != state.latest.observation_id,
            "conflicting_session_observation",
        )
        _need(
            _timestamp(observation.as_of) != _timestamp(state.latest.as_of),
            "conflicting_session_time",
        )
    loss = _loss(state.spec, observation)
    latched = state.daily_loss_latched or (loss is not None and loss >= state.spec.max_daily_loss)
    became_current = state.latest is None or _timestamp(observation.as_of) > _timestamp(
        state.latest.as_of
    )
    observation_sha256 = fingerprint(encode_observation(observation))
    updated = SessionState(
        state.spec,
        state.revision + 1,
        observation if became_current else state.latest,
        observation_sha256 if became_current else state.latest_sha256,
        loss if became_current else state.daily_loss,
        latched,
    )
    receipt = ObservationReceipt(
        observation.observation_id,
        observation_sha256,
        updated.revision,
        became_current,
        loss,
        latched,
    )
    return updated, receipt


def require_current(state, *, now):
    _validate_state(state)
    now = _now(now)
    _need(
        _timestamp(state.spec.started_at) <= now < _timestamp(state.spec.ends_at),
        "session_not_current",
    )
    _need(state.latest is not None, "session_no_checkpoint")
    validate_observation(state.spec, state.latest, now=now)
    _need(
        state.latest.account_complete and state.latest.cash_flow_complete,
        "session_incomplete_checkpoint",
    )
    age = timedelta(seconds=state.spec.snapshot_max_age_seconds)
    _need(
        now - _timestamp(state.latest.as_of) <= age
        and now - _timestamp(state.latest.received_at) <= age,
        "session_stale_checkpoint",
    )
    return state  # Includes the latch; this is deliberately not an all-clear flag.


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _binding_data(binding):
    return {
        "account_id": binding.account_id,
        "environment": binding.environment,
        "currency": binding.currency,
    }


def _spec_data(spec):
    return {
        "binding": _binding_data(spec.binding),
        "session_id": spec.session_id,
        "policy_id": spec.policy_id,
        "policy_sha256": spec.policy_sha256,
        "started_at": spec.started_at,
        "ends_at": spec.ends_at,
        "opening_equity": str(spec.opening_equity),
        "opening_evidence_id": spec.opening_evidence_id,
        "max_daily_loss": str(spec.max_daily_loss),
        "snapshot_max_age_seconds": spec.snapshot_max_age_seconds,
    }


def _observation_data(observation):
    return {
        "binding": _binding_data(observation.binding),
        "session_id": observation.session_id,
        "observation_id": observation.observation_id,
        "as_of": observation.as_of,
        "received_at": observation.received_at,
        "equity": str(observation.equity),
        "external_net_cash_flow": str(observation.external_net_cash_flow),
        "account_complete": observation.account_complete,
        "cash_flow_complete": observation.cash_flow_complete,
        "cash_flow_started_at": observation.cash_flow_started_at,
        "cash_flow_ends_at": observation.cash_flow_ends_at,
        "account_evidence_id": observation.account_evidence_id,
        "cash_flow_evidence_id": observation.cash_flow_evidence_id,
    }


def _encoded(value):
    try:
        text = _json(value)
        _need(len(text.encode("utf-8")) <= MAX_CODEC_BYTES, "session_codec_size")
        return text
    except (ValueError, UnicodeError, RecursionError):
        raise SessionRefused("session_invalid_codec") from None


def encode_spec(spec):
    validate_spec(spec)
    return _encoded(_spec_data(spec))


def encode_observation(observation):
    # No spec or wall clock is invented by a standalone payload codec.
    _validate_observation_shape(observation)
    return _encoded(_observation_data(observation))


def encode_state(state):
    _validate_state(state)
    return _encoded(
        {
            "spec": _spec_data(state.spec),
            "revision": state.revision,
            "latest": _observation_data(state.latest) if state.latest is not None else None,
            "latest_sha256": state.latest_sha256,
            "daily_loss": str(state.daily_loss) if state.daily_loss is not None else None,
            "daily_loss_latched": state.daily_loss_latched,
        }
    )


def _validate_receipt(receipt):
    _need(
        type(receipt) is ObservationReceipt
        and _identifier(receipt.observation_id)
        and _hash(receipt.payload_sha256)
        and type(receipt.revision) is int
        and 0 < receipt.revision <= MAX_REVISION
        and type(receipt.became_current) is bool
        and type(receipt.daily_loss_latched) is bool,
        "session_invalid_receipt",
    )
    if receipt.daily_loss is not None:
        _decimal(receipt.daily_loss, nonnegative=True)


def encode_receipt(receipt):
    _validate_receipt(receipt)
    return _encoded(
        {
            "observation_id": receipt.observation_id,
            "payload_sha256": receipt.payload_sha256,
            "revision": receipt.revision,
            "became_current": receipt.became_current,
            "daily_loss": str(receipt.daily_loss) if receipt.daily_loss is not None else None,
            "daily_loss_latched": receipt.daily_loss_latched,
        }
    )


def _object(value, keys):
    _need(type(value) is dict and value.keys() == set(keys.split()), "session_invalid_codec")
    return value


def _binding_from(value):
    row = _object(value, "account_id environment currency")
    result = SessionBinding(**row)
    validate_binding(result)
    return result


def _number(value):
    _need(type(value) is str and len(value) <= 160, "session_invalid_decimal")
    try:
        context = Context(prec=1024, Emin=-999999, Emax=999999)
        for signal in context.traps:
            context.traps[signal] = True
        with localcontext(context):
            number = Decimal(value)
    except DecimalException:
        raise SessionRefused("session_invalid_decimal") from None
    _decimal(number)
    _need(str(number) == value, "session_noncanonical_codec")
    return number


def _spec_from(value):
    row = _object(
        value,
        "binding session_id policy_id policy_sha256 started_at ends_at opening_equity "
        "opening_evidence_id max_daily_loss snapshot_max_age_seconds",
    )
    return SessionSpec(
        **{
            **row,
            "binding": _binding_from(row["binding"]),
            "opening_equity": _number(row["opening_equity"]),
            "max_daily_loss": _number(row["max_daily_loss"]),
        }
    )


def _observation_from(value):
    row = _object(
        value,
        "binding session_id observation_id as_of received_at equity external_net_cash_flow "
        "account_complete cash_flow_complete cash_flow_started_at cash_flow_ends_at "
        "account_evidence_id cash_flow_evidence_id",
    )
    return EquityObservation(
        **{
            **row,
            "binding": _binding_from(row["binding"]),
            "equity": _number(row["equity"]),
            "external_net_cash_flow": _number(row["external_net_cash_flow"]),
        }
    )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, "session_invalid_codec")
        result[key] = value
    return result


def _decoded(text, parser, encoder):
    _need(type(text) is str, "session_invalid_codec")
    try:
        _need(len(text.encode("utf-8")) <= MAX_CODEC_BYTES, "session_codec_size")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda _: None)
        result = parser(value)
        _need(encoder(result) == text, "session_noncanonical_codec")
        return result
    except (ValueError, UnicodeError, RecursionError, TypeError, KeyError):
        raise SessionRefused("session_invalid_codec") from None


def decode_spec(text):
    return _decoded(text, _spec_from, encode_spec)


def decode_observation(text):
    return _decoded(text, _observation_from, encode_observation)


def _state_from(value):
    row = _object(value, "spec revision latest latest_sha256 daily_loss daily_loss_latched")
    return SessionState(
        **{
            **row,
            "spec": _spec_from(row["spec"]),
            "latest": _observation_from(row["latest"]) if row["latest"] is not None else None,
            "daily_loss": _number(row["daily_loss"]) if row["daily_loss"] is not None else None,
        }
    )


def decode_state(text):
    return _decoded(text, _state_from, encode_state)


def _receipt_from(value):
    row = _object(
        value, "observation_id payload_sha256 revision became_current daily_loss daily_loss_latched"
    )
    return ObservationReceipt(
        **{
            **row,
            "daily_loss": _number(row["daily_loss"]) if row["daily_loss"] is not None else None,
        }
    )


def decode_receipt(text):
    return _decoded(text, _receipt_from, encode_receipt)


def _validate_observation_shape(observation):
    _need(type(observation) is EquityObservation, "session_invalid_observation")
    validate_binding(observation.binding)
    _need(
        all(
            _identifier(value)
            for value in (
                observation.session_id,
                observation.observation_id,
                observation.account_evidence_id,
            )
        ),
        "session_invalid_observation",
    )
    as_of, received_at = _timestamp(observation.as_of), _timestamp(observation.received_at)
    _need(as_of <= received_at, "session_future_observation")
    _decimal(observation.equity, positive=True)
    _decimal(observation.external_net_cash_flow)
    _need(
        type(observation.account_complete) is bool and type(observation.cash_flow_complete) is bool,
        "session_invalid_completeness",
    )
    start = (
        _timestamp(observation.cash_flow_started_at)
        if observation.cash_flow_started_at is not None
        else None
    )
    end = (
        _timestamp(observation.cash_flow_ends_at)
        if observation.cash_flow_ends_at is not None
        else None
    )
    if observation.cash_flow_evidence_id is not None:
        _need(_identifier(observation.cash_flow_evidence_id), "session_invalid_flow_evidence")
    if observation.cash_flow_complete:
        _need(
            start is not None and end is not None and observation.cash_flow_evidence_id is not None,
            "session_incomplete_cash_flow",
        )
        _need(start <= end and end == as_of, "session_incoherent_cash_flow")
