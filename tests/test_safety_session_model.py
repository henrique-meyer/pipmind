"""Pure representation, arithmetic and transition invariants; no external reads."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext, localcontext

import pytest

from pipmind.safety.session import (
    MAX_CODEC_BYTES,
    EquityObservation,
    ObservationReceipt,
    SessionBinding,
    SessionRefused,
    SessionSpec,
    account_key,
    advance,
    decode_observation,
    decode_receipt,
    decode_spec,
    decode_state,
    encode_observation,
    encode_receipt,
    encode_spec,
    encode_state,
    fingerprint,
    initial_state,
    require_current,
    validate_binding,
    validate_observation,
    validate_spec,
)

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
START = "2026-10-21T00:00:00Z"
END = "2026-10-22T00:00:00Z"


def spec(**changes):
    return replace(
        SessionSpec(
            SessionBinding("synthetic-account", "demo", "USD"),
            "day-1",
            "explicit-policy",
            "a" * 64,
            START,
            END,
            Decimal("1000.00"),
            "opening-evidence",
            Decimal("100.00"),
            60,
        ),
        **changes,
    )


def observation(policy, **changes):
    return replace(
        EquityObservation(
            policy.binding,
            policy.session_id,
            "snapshot-1",
            "2026-10-21T12:00:00Z",
            "2026-10-21T12:00:00Z",
            Decimal("900.00"),
            Decimal("0.00"),
            True,
            True,
            policy.started_at,
            "2026-10-21T12:00:00Z",
            "account-evidence",
            "flow-evidence",
        ),
        **changes,
    )


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_new_state_and_transitions_are_frozen_and_do_not_mutate_supplied_values():
    policy = spec()
    value = observation(policy)
    old = initial_state(policy)
    before = encode_spec(policy), encode_observation(value), encode_state(old)
    state, receipt = advance(old, value, now=NOW)
    assert (encode_spec(policy), encode_observation(value), encode_state(old)) == before
    assert state.spec is policy and state.latest is value
    assert state.revision == 1 and state.daily_loss == Decimal("100.00")
    assert receipt.became_current and receipt.daily_loss_latched
    assert receipt.payload_sha256 == state.latest_sha256 == fingerprint(encode_observation(value))
    for item, field in (
        (policy, "session_id"),
        (value, "equity"),
        (state, "revision"),
        (receipt, "revision"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(item, field, None)
        assert not hasattr(item, "__dict__")


def test_receipt_loss_is_historical_observation_while_state_loss_stays_current():
    policy = spec()
    current = observation(policy, equity=Decimal("1100"), external_net_cash_flow=Decimal("100"))
    state, _ = advance(initial_state(policy), current, now=NOW)
    historical = observation(
        policy,
        observation_id="historical",
        as_of="2026-10-21T11:00:00Z",
        cash_flow_ends_at="2026-10-21T11:00:00Z",
        equity=Decimal("790"),
    )
    state, receipt = advance(state, historical, now=NOW)
    assert not receipt.became_current and receipt.daily_loss == 210
    assert state.latest is current and state.daily_loss == 0
    assert state.daily_loss_latched and receipt.daily_loss_latched
    assert require_current(state, now=NOW) is state


def test_current_incomplete_snapshot_replaces_known_loss_without_unsupported_new_latch():
    policy = spec()
    value = observation(policy, equity=Decimal("950"))
    state, _ = advance(initial_state(policy), value, now=NOW)
    incomplete = observation(
        policy,
        observation_id="unknown-flow",
        as_of="2026-10-21T12:00:01Z",
        received_at="2026-10-21T12:00:01Z",
        equity=Decimal("1"),
        cash_flow_complete=False,
        cash_flow_started_at=None,
        cash_flow_ends_at=None,
        cash_flow_evidence_id=None,
    )
    state, receipt = advance(state, incomplete, now=NOW + timedelta(seconds=1))
    assert state.latest is incomplete and state.daily_loss is receipt.daily_loss is None
    assert not state.daily_loss_latched and not receipt.daily_loss_latched
    with pytest.raises(SessionRefused, match="session_incomplete_checkpoint"):
        require_current(state, now=NOW + timedelta(seconds=1))


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", ""),
        ("account_id", " x"),
        ("account_id", "x\x00"),
        ("account_id", "😀" * 65),
        ("account_id", "\ud800"),
        ("environment", "paper"),
        ("environment", True),
        ("currency", "usd"),
    ],
)
def test_invalid_binding_fields_refuse(field, value):
    with pytest.raises(SessionRefused):
        validate_binding(replace(spec().binding, **{field: value}))


def test_account_lock_key_excludes_currency_but_pins_account_and_environment():
    binding = spec().binding
    assert account_key(binding) == account_key(replace(binding, currency="EUR"))
    assert account_key(binding) != account_key(replace(binding, environment="live"))
    assert account_key(binding) != account_key(
        replace(binding, account_id=binding.account_id.upper())
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1000,
        1000.0,
        "1000",
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("1E+65"),
        Decimal("1E-65"),
        Decimal("9" * 65),
        Decimal("0"),
        Decimal("-1"),
    ],
)
def test_opening_equity_requires_bounded_positive_exact_decimal(value):
    with pytest.raises(SessionRefused):
        validate_spec(spec(opening_equity=value))


def test_preserve_original_decimal_representations_and_utc_strings_in_codecs():
    policy = spec(opening_equity=Decimal("1E+64"), max_daily_loss=Decimal("1E+63"))
    value = observation(
        policy,
        equity=Decimal("9E+63"),
        external_net_cash_flow=Decimal("-0.00"),
        as_of="2026-10-21T12:00:00+00:00",
        cash_flow_ends_at="2026-10-21T12:00:00+00:00",
    )
    state, receipt = advance(initial_state(policy), value, now=NOW)
    assert state.daily_loss == Decimal("1E+63") and receipt.daily_loss_latched
    assert str(decode_spec(encode_spec(policy)).opening_equity) == "1E+64"
    decoded = decode_observation(encode_observation(value))
    assert str(decoded.external_net_cash_flow) == "-0.00"
    assert decoded.as_of.endswith("+00:00")
    assert decode_state(encode_state(state)) == state
    assert decode_receipt(encode_receipt(receipt)) == receipt
    assert all(
        len(text.encode()) < MAX_CODEC_BYTES
        for text in (
            encode_spec(policy),
            encode_observation(value),
            encode_state(state),
            encode_receipt(receipt),
        )
    )


def test_exact_derived_result_refuses_when_significant_digits_cannot_fit():
    policy = spec(opening_equity=Decimal("1E+64"), max_daily_loss=Decimal("1E+63"))
    with pytest.raises(SessionRefused, match="session_arithmetic_overflow"):
        advance(initial_state(policy), observation(policy, equity=Decimal("0.1")), now=NOW)


def test_arithmetic_and_bad_decimal_decode_never_change_ambient_context_or_flags():
    policy = spec()
    with localcontext() as context:
        context.prec, context.Emin, context.Emax = 2, -2, 2
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        before = context.copy()
        state, _ = advance(
            initial_state(policy), observation(policy, equity=Decimal("899.99")), now=NOW
        )
        assert state.daily_loss == Decimal("100.01")
        payload = json.loads(encode_spec(policy))
        payload["opening_equity"] = "not-a-number"
        with pytest.raises(SessionRefused):
            decode_spec(canonical(payload))
        after = getcontext()
        assert (after.prec, after.Emin, after.Emax, after.rounding) == (
            before.prec,
            before.Emin,
            before.Emax,
            before.rounding,
        )
        assert dict(after.flags) == dict(before.flags)
        assert dict(after.traps) == dict(before.traps)


@pytest.mark.parametrize(
    "time",
    [
        "2026-10-21",
        "2026-10-21 12:00:00Z",
        "2026-10-21T12:00:00",
        "2026-10-21T12:00:00-00:00",
        "2026-10-21T12:00:00.1234567Z",
        "2026-10-21T13:00:00+01:00",
        "2026-02-30T12:00:00Z",
    ],
)
def test_nonrepresentable_non_utc_or_invalid_timestamp_refuses(time):
    policy = spec()
    with pytest.raises(SessionRefused):
        validate_observation(policy, observation(policy, as_of=time), now=NOW)


def test_same_instant_alias_refuses_new_id_and_duplicate_hashes_keep_original_payload():
    policy = spec()
    value = observation(policy)
    state, _ = advance(initial_state(policy), value, now=NOW)
    alias = replace(value, observation_id="alias", as_of="2026-10-21T12:00:00+00:00")
    assert fingerprint(encode_observation(value)) != fingerprint(encode_observation(alias))
    with pytest.raises(SessionRefused, match="conflicting_session_time"):
        advance(state, alias, now=NOW)


def test_current_uses_exact_microsecond_age_boundary_and_returns_latched_state():
    policy = spec()
    state, _ = advance(initial_state(policy), observation(policy), now=NOW)
    assert require_current(state, now=NOW + timedelta(seconds=60)) is state
    assert state.daily_loss_latched
    with pytest.raises(SessionRefused, match="session_stale_checkpoint"):
        require_current(state, now=NOW + timedelta(seconds=60, microseconds=1))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(extra=True),
        lambda data: data.update(snapshot_max_age_seconds=True),
        lambda data: data.update(opening_equity=1000),
        lambda data: data.update(opening_equity="+1000.00"),
        lambda data: data["binding"].update(currency="US"),
    ],
)
def test_spec_codec_refuses_corrupt_shape_types_or_noncanonical_decimal(mutation):
    data = json.loads(encode_spec(spec()))
    mutation(data)
    with pytest.raises(SessionRefused):
        decode_spec(canonical(data))


@pytest.mark.parametrize(
    "codec,decoder",
    [
        (lambda: encode_spec(spec()), decode_spec),
        (lambda: encode_observation(observation(spec())), decode_observation),
        (lambda: encode_state(initial_state(spec())), decode_state),
        (
            lambda: encode_receipt(ObservationReceipt("id", "b" * 64, 1, True, Decimal(0), False)),
            decode_receipt,
        ),
    ],
)
def test_codecs_refuse_duplicate_keys_noncanonical_bytes_and_wrong_input_type(codec, decoder):
    body = codec()
    assert isinstance(body, str)
    with pytest.raises(SessionRefused):
        decoder(body.encode())
    with pytest.raises(SessionRefused):
        decoder(" " + body)
    with pytest.raises(SessionRefused):
        decoder(body[:-1] + ',"extra":1,"extra":2}')
    with pytest.raises(SessionRefused):
        decoder("x" * (MAX_CODEC_BYTES + 1))


def test_revision_overflow_refuses_without_altering_valid_state():
    policy = spec()
    state, _ = advance(initial_state(policy), observation(policy), now=NOW)
    exhausted = replace(state, revision=9223372036854775807)
    later = observation(
        policy,
        observation_id="later",
        as_of="2026-10-21T12:00:01Z",
        received_at="2026-10-21T12:00:01Z",
        cash_flow_ends_at="2026-10-21T12:00:01Z",
    )
    before = encode_state(exhausted)
    with pytest.raises(SessionRefused, match="session_revision_exhausted"):
        advance(exhausted, later, now=NOW + timedelta(seconds=1))
    assert encode_state(exhausted) == before
