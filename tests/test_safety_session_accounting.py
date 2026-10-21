"""Independent acceptance counterexamples for cumulative session risk accounting."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import (
    ROUND_UP,
    Decimal,
    Inexact,
    Overflow,
    Rounded,
    Underflow,
    getcontext,
    localcontext,
)

import pytest

from pipmind.safety.session import (
    EquityObservation,
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

START = datetime(2026, 10, 21, tzinfo=UTC)
BINDING = SessionBinding("synthetic-account", "demo", "USD")


def instant(seconds):
    return START + timedelta(seconds=seconds)


def ts(seconds):
    return instant(seconds).isoformat().replace("+00:00", "Z")


def spec(**changes):
    return replace(
        SessionSpec(
            BINDING,
            "session-one",
            "reviewed-policy",
            "a" * 64,
            ts(0),
            ts(86400),
            Decimal("1000"),
            "opening-evidence",
            Decimal("100"),
            60,
        ),
        **changes,
    )


def observation(policy, name, seconds, equity_text, flow="0", **changes):
    return replace(
        EquityObservation(
            policy.binding,
            policy.session_id,
            name,
            ts(seconds),
            ts(seconds + 10),
            Decimal(equity_text),
            Decimal(flow),
            True,
            True,
            policy.started_at,
            ts(seconds),
            "account-" + name,
            "cash-flow-" + name,
        ),
        **changes,
    )


def apply(state, value):
    return advance(
        state, value, now=datetime.fromisoformat(value.received_at.replace("Z", "+00:00"))
    )


def test_cumulative_deposits_and_withdrawals_are_checkpoints_not_repeated_increments():
    policy = spec()
    state = initial_state(policy)
    balances = [(1200, 200, "0"), (1190, 200, "10"), (1040, 50, "10"), (1030, 50, "20")]
    previous = []
    for index, (equity, flow, loss) in enumerate(balances, 1):
        value = observation(policy, f"checkpoint-{index}", index * 60, str(equity), str(flow))
        previous.append(encode_state(state))
        state, receipt = apply(state, value)
        assert receipt.daily_loss == state.daily_loss == Decimal(loss)
        assert receipt.observation_id == value.observation_id
        assert receipt.payload_sha256 == fingerprint(encode_observation(value))
        assert state.latest_sha256 == receipt.payload_sha256
        assert state.revision == index and receipt.revision == index and receipt.became_current
        assert not state.daily_loss_latched
        assert require_current(state, now=instant(index * 60 + 10)) == state
    assert decode_state(previous[0]).revision == 0
    assert decode_state(previous[1]).latest.observation_id == "checkpoint-1"


@pytest.mark.parametrize(
    "equity,flow,loss,latched",
    [("1200", "300", "100", True), ("700", "-300", "0", False)],
)
def test_deposits_cannot_mask_loss_and_withdrawals_cannot_invent_loss(equity, flow, loss, latched):
    policy = spec()
    state, receipt = apply(
        initial_state(policy), observation(policy, "cash-flow", 60, equity, flow)
    )
    assert state.daily_loss == receipt.daily_loss == Decimal(loss)
    assert state.daily_loss_latched is latched


def test_exact_limit_latches_but_arbitrarily_close_lower_loss_does_not():
    policy = spec()
    state, receipt = apply(
        initial_state(policy), observation(policy, "below", 60, "900.0000000000001")
    )
    assert receipt.daily_loss == Decimal("99.9999999999999") and not state.daily_loss_latched
    state, receipt = apply(state, observation(policy, "equal", 120, "900"))
    assert receipt.daily_loss == Decimal("100") and state.daily_loss_latched
    assert require_current(state, now=instant(130)) is state  # Fresh does not mean all-clear.


def test_late_historical_breach_latches_without_replacing_current_checkpoint_or_loss():
    policy = spec()
    current = observation(policy, "current-recovered", 180, "1050")
    state, _ = apply(initial_state(policy), current)
    before = encode_state(state)
    older = observation(policy, "late-breach", 60, "890", received_at=ts(200))
    state, receipt = apply(state, older)
    assert not receipt.became_current and receipt.daily_loss == Decimal("110")
    assert receipt.daily_loss_latched and state.daily_loss_latched
    assert state.latest == current and state.daily_loss == Decimal("0") and state.revision == 2
    assert state.latest_sha256 == fingerprint(encode_observation(current))
    assert decode_state(before).revision == 1 and not decode_state(before).daily_loss_latched
    assert require_current(state, now=instant(210)) is state
    state, _ = apply(state, observation(policy, "further-recovery", 240, "1100"))
    assert state.daily_loss == Decimal("0") and state.daily_loss_latched


@pytest.mark.parametrize("incomplete", ["account_complete", "cash_flow_complete"])
def test_newer_incomplete_checkpoint_blocks_current_facts_without_inventing_or_clearing_latch(
    incomplete,
):
    policy = spec()
    state, _ = apply(initial_state(policy), observation(policy, "known-breach", 60, "900"))
    unknown = observation(policy, "incomplete", 120, "1", **{incomplete: False})
    state, receipt = apply(state, unknown)
    assert state.latest == unknown and receipt.became_current
    assert state.daily_loss is None and receipt.daily_loss is None and state.daily_loss_latched
    with pytest.raises(SessionRefused):
        require_current(state, now=instant(130))
    state, _ = apply(state, observation(policy, "complete-again", 180, "1100"))
    assert require_current(state, now=instant(190)) is state and state.daily_loss_latched


def test_older_incomplete_report_cannot_poison_newer_facts_or_latch_unsupported_loss():
    policy = spec()
    latest = observation(policy, "latest", 180, "1000")
    state, _ = apply(initial_state(policy), latest)
    incomplete = observation(
        policy,
        "old-incomplete",
        60,
        "1",
        cash_flow_complete=False,
        cash_flow_started_at=None,
        cash_flow_ends_at=None,
        cash_flow_evidence_id=None,
        received_at=ts(200),
    )
    state, receipt = apply(state, incomplete)
    assert not receipt.became_current and receipt.daily_loss is None
    assert (
        state.latest == latest and state.daily_loss == Decimal("0") and not state.daily_loss_latched
    )
    assert require_current(state, now=instant(210)) is state


@pytest.mark.parametrize("flow", ["-1000", "-1001"])
def test_complete_adjusted_opening_equity_must_be_positive(flow):
    policy = spec()
    with pytest.raises(SessionRefused):
        apply(initial_state(policy), observation(policy, "unsupported-opening", 60, "1", flow))


@pytest.mark.parametrize(
    "change",
    [
        {"cash_flow_started_at": ts(1)},
        {"cash_flow_ends_at": ts(59)},
        {"cash_flow_evidence_id": None},
        {"cash_flow_evidence_id": ""},
    ],
)
def test_complete_flow_requires_exact_session_to_observation_coverage_and_evidence(change):
    policy = spec()
    with pytest.raises(SessionRefused):
        apply(initial_state(policy), observation(policy, "bad-coverage", 60, "1000", **change))


def test_equal_time_timestamp_alias_is_a_conflict_even_with_different_utc_spelling():
    policy = spec()
    original = observation(policy, "first", 60, "1000")
    state, _ = apply(initial_state(policy), original)
    conflict = observation(
        policy,
        "different-id",
        60,
        "999",
        as_of=ts(60).replace("Z", "+00:00"),
        cash_flow_ends_at=ts(60).replace("Z", "+00:00"),
    )
    unchanged = encode_state(state)
    with pytest.raises(SessionRefused):
        apply(state, conflict)
    assert encode_state(state) == unchanged


def test_freshness_exact_boundary_and_session_end_are_independent_refusals():
    policy = spec()
    state, _ = apply(initial_state(policy), observation(policy, "freshness", 60, "1000"))
    assert require_current(state, now=instant(120)) is state
    with pytest.raises(SessionRefused):
        require_current(state, now=instant(120) + timedelta(microseconds=1))
    closing, _ = apply(initial_state(policy), observation(policy, "near-close", 86370, "1000"))
    assert require_current(closing, now=instant(86400) - timedelta(microseconds=1)) is closing
    with pytest.raises(SessionRefused):
        require_current(closing, now=instant(86400))


def test_now_must_be_aware_utc_and_no_observation_or_receipt_may_be_future():
    policy = spec()
    value = observation(policy, "future", 60, "1000")
    for moment in (instant(59), instant(69), instant(70).replace(tzinfo=None)):
        with pytest.raises(SessionRefused):
            validate_observation(policy, value, now=moment)
    with pytest.raises(SessionRefused):
        require_current(initial_state(policy), now=instant(60))


def context_settings():
    context = getcontext()
    return (
        context.prec,
        context.Emin,
        context.Emax,
        context.rounding,
        context.clamp,
        context.capitals,
        dict(context.traps),
        dict(context.flags),
    )


def test_exact_cash_flow_math_leaves_tiny_trapping_ambient_decimal_context_unchanged():
    policy = spec(
        opening_equity=Decimal("1000000000000.000000000000000001"),
        max_daily_loss=Decimal("0.000000000000000003"),
    )
    value = observation(policy, "exact-small-loss", 60, "1000000000001.999999999999999998", "2")
    with localcontext() as context:
        context.prec, context.Emin, context.Emax, context.rounding = 3, -2, 2, ROUND_UP
        for signal in (Inexact, Rounded, Overflow, Underflow):
            context.traps[signal] = True
        context.clear_flags()
        before = context_settings()
        state, receipt = apply(initial_state(policy), value)
        assert state.daily_loss == receipt.daily_loss == Decimal("0.000000000000000003")
        assert state.daily_loss_latched
        assert decode_state(encode_state(state)) == state
        assert context_settings() == before


def test_binding_keys_and_session_refusals_do_not_mix_accounts_environments_or_currencies():
    assert account_key(BINDING) == account_key(replace(BINDING, currency="EUR"))
    assert account_key(BINDING) != account_key(replace(BINDING, environment="live"))
    assert account_key(BINDING) != account_key(replace(BINDING, account_id="another-account"))
    policy = spec()
    original = observation(policy, "foreign", 60, "1000")
    for value in (
        replace(original, session_id="another-session"),
        replace(original, binding=replace(BINDING, currency="EUR")),
        replace(original, binding=replace(BINDING, environment="live")),
    ):
        with pytest.raises(SessionRefused):
            apply(initial_state(policy), value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("policy_sha256", "A" * 64),
        ("policy_sha256", "a" * 63),
        ("policy_id", " leading"),
        ("opening_equity", Decimal("0")),
        ("max_daily_loss", Decimal("0")),
        ("opening_equity", 1000),
        ("max_daily_loss", 100.0),
        ("snapshot_max_age_seconds", True),
        ("snapshot_max_age_seconds", 0),
        ("ends_at", ts(0)),
    ],
)
def test_policy_type_and_bound_errors_refuse(field, value):
    with pytest.raises(SessionRefused):
        validate_spec(spec(**{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("equity", Decimal("NaN")),
        ("equity", Decimal("Infinity")),
        ("equity", Decimal("-1")),
        ("equity", True),
        ("external_net_cash_flow", 0.0),
        ("account_complete", 1),
        ("cash_flow_complete", "true"),
        ("observation_id", " padded "),
    ],
)
def test_observation_numeric_and_strict_boolean_types_refuse(field, value):
    policy = spec()
    with pytest.raises(SessionRefused):
        validate_observation(
            policy, observation(policy, "strict", 60, "1000", **{field: value}), now=instant(70)
        )


def test_ids_obey_utf16_units_and_frozen_callers_are_not_changed():
    accepted = replace(BINDING, account_id="😀" * 64)
    validate_binding(accepted)
    with pytest.raises(SessionRefused):
        validate_binding(replace(BINDING, account_id="😀" * 65))
    policy = spec()
    value = observation(policy, "immutable", 60, "950")
    initial = initial_state(policy)
    saved = encode_spec(policy), encode_observation(value), encode_state(initial)
    advance(initial, value, now=instant(70))
    assert saved == (encode_spec(policy), encode_observation(value), encode_state(initial))
    with pytest.raises(FrozenInstanceError):
        value.equity = Decimal("1")


def test_codecs_preserve_decimal_and_timestamp_representation_and_reject_corruption():
    policy = spec(
        opening_equity=Decimal("1E+64"),
        max_daily_loss=Decimal("1E+63"),
        started_at=ts(0).replace("Z", "+00:00"),
    )
    value = observation(policy, "codec", 60, "9E+63", "-0.00")
    state, receipt = apply(initial_state(policy), value)
    for encode, decode, original in (
        (encode_spec, decode_spec, policy),
        (encode_observation, decode_observation, value),
        (encode_state, decode_state, state),
        (encode_receipt, decode_receipt, receipt),
    ):
        text = encode(original)
        assert type(text) is str and encode(decode(text)) == text
        with pytest.raises(SessionRefused):
            decode(text + " " * 32768)
        with pytest.raises(SessionRefused):
            decode("not-json")
    restored = decode_observation(encode_observation(value))
    assert restored.external_net_cash_flow.as_tuple() == Decimal("-0.00").as_tuple()
    assert decode_spec(encode_spec(policy)).opening_equity.as_tuple() == Decimal("1E+64").as_tuple()
    assert decode_spec(encode_spec(policy)).started_at.endswith("+00:00")
    with pytest.raises(SessionRefused):
        encode_state(replace(state, revision=True))
    with pytest.raises(SessionRefused):
        encode_receipt(replace(receipt, revision=-1))


def test_money_bounds_and_technical_revision_overflow_refuse_without_ambient_rounding():
    policy = spec()
    for bad in (Decimal("1E+65"), Decimal("1E-65"), Decimal("1" * 65)):
        with pytest.raises(SessionRefused):
            validate_observation(
                policy, observation(policy, "money-bound", 60, "1000", equity=bad), now=instant(70)
            )
    state, _ = apply(initial_state(policy), observation(policy, "valid", 60, "1000"))
    maximum = replace(state, revision=2**63 - 1)
    saved = encode_state(maximum)
    with pytest.raises(SessionRefused):
        apply(maximum, observation(policy, "cannot-increment", 120, "1000"))
    assert encode_state(maximum) == saved


def test_invalid_identity_refusals_use_fixed_codes_without_echoing_values():
    messages = []
    for value in (" private-account-alpha ", " private-account-beta "):
        with pytest.raises(SessionRefused) as raised:
            validate_binding(replace(BINDING, account_id=value))
        messages.append(str(raised.value))
        assert "private-account" not in str(raised.value)
    assert messages[0] == messages[1]


@pytest.mark.parametrize(
    "change",
    [
        {"latest_sha256": "b" * 64},
        {"daily_loss": Decimal("0")},
        {"daily_loss_latched": False},
    ],
)
def test_corrupted_current_accounting_cannot_become_usable_via_codec_roundtrip(change):
    policy = spec()
    state, _ = apply(initial_state(policy), observation(policy, "visible-breach", 60, "900"))
    assert state.daily_loss == Decimal("100") and state.daily_loss_latched
    with pytest.raises(SessionRefused):
        corrupted = decode_state(encode_state(replace(state, **change)))
        require_current(corrupted, now=instant(70))


def test_recovered_current_loss_does_not_make_a_valid_historical_latch_corrupt():
    policy = spec()
    state, _ = apply(initial_state(policy), observation(policy, "old-breach", 60, "890"))
    state, _ = apply(state, observation(policy, "recovery", 120, "1010"))
    restored = decode_state(encode_state(state))
    assert restored.daily_loss == Decimal("0") and restored.daily_loss_latched
    assert require_current(restored, now=instant(130)) == state


def test_opening_instant_is_inclusive_with_zero_length_cumulative_flow_coverage():
    policy = spec()
    value = observation(policy, "at-opening", 0, "1000", received_at=ts(0))
    state, receipt = apply(initial_state(policy), value)
    assert receipt.daily_loss == Decimal("0") and not state.daily_loss_latched
    assert require_current(state, now=START) is state
