"""Real pure admission/session paths over explicit supplements; no SQL or RPC."""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, localcontext

import pytest

import pipmind.safety.eval_scenarios as scenarios
from pipmind.safety.admission import assess, decode_policy_book, decode_request, encode_request
from pipmind.safety.eval_scenarios import (
    CASE_IDS,
    ScenarioRefused,
    captured_order,
    expected_order,
    load_scenarios,
    prepare_scenario,
)
from pipmind.safety.session import (
    advance,
    decode_observation,
    decode_spec,
    initial_state,
)

NOW = datetime(2026, 10, 22, 3, 30, 0, 123456, tzinfo=UTC)


def prepare(spec, variant="positive", *, now=NOW):
    return prepare_scenario(
        spec,
        sql_now=now,
        account_id=f"sql-eval-test-{spec.case_id}-{variant}",
        session_id="synthetic-session",
        variant=variant,
    )


def check(prepared):
    state = initial_state(decode_spec(prepared.session_spec_body))
    receipts = []
    for body in prepared.observation_bodies:
        state, receipt = advance(state, decode_observation(body), now=NOW)
        receipts.append(receipt)
    return (
        state,
        receipts,
        assess(
            decode_request(prepared.request_body),
            state,
            decode_policy_book(prepared.policy_book_body),
            (),
            0,
            now=NOW,
        ),
    )


def test_six_frozen_development_specs_bind_exact_unmodified_source_lines_and_supplements():
    before = scenarios.SOURCE_PATH.read_bytes()
    lines = {json.loads(row)["case_id"]: row for row in before.split(b"\n") if row}
    specs = load_scenarios()
    assert tuple(row.case_id for row in specs) == CASE_IDS
    for spec in specs:
        assert spec.split == "development"
        assert spec.source_case_body.encode() == lines[spec.case_id]
        assert spec.source_case_sha256 == hashlib.sha256(lines[spec.case_id]).hexdigest()
        assert spec.supplement_sha256 == hashlib.sha256(spec.supplement_body.encode()).hexdigest()
        assert json.loads(spec.supplement_body)["common"]["synthetic_only"] is True
        with pytest.raises(FrozenInstanceError):
            spec.case_id = "S09"
    assert scenarios.SOURCE_PATH.read_bytes() == before


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_complete_positives_pass_actual_assessment_without_enforcing_expected_flags(case_id):
    spec = next(row for row in load_scenarios() if row.case_id == case_id)
    prepared = prepare(spec)
    state, _, actual = check(prepared)
    assert actual.within_limits and actual.code == "within_limits"
    assert actual.reservation is not None and not actual.dispatch_authorized
    assert not state.daily_loss_latched
    assert prepared.control_action is None and prepared.replace_lease is False
    assert captured_order(prepared.request_body) == expected_order(spec, "positive")
    assert encode_request(decode_request(prepared.request_body)) == prepared.request_body
    with pytest.raises(FrozenInstanceError):
        prepared.request_body = "changed"


@pytest.mark.parametrize(
    "case_id,code",
    [
        ("S02", "stale_quote"),
        ("S03", "reconciliation_failed"),
        ("S04", "order_size_limit"),
        ("S05", "daily_loss_stop"),
    ],
)
def test_targeted_actual_assessments_refuse_for_the_specific_fact(case_id, code):
    spec = next(row for row in load_scenarios() if row.case_id == case_id)
    prepared = prepare(spec, "target")
    _, _, result = check(prepared)
    assert not result.within_limits and result.code == code and result.reservation is None
    assert captured_order(prepared.request_body) == expected_order(spec, "target")
    if case_id == "S03":
        assert {(row.code, row.subject) for row in result.reconciliation_findings} == {
            ("stale", "account"),
            ("stale", "orders"),
            ("stale", "positions"),
        }


def test_original_order_values_preserved_and_s04_positive_size_change_is_explicit():
    for spec in load_scenarios():
        source = json.loads(spec.source_case_body, parse_float=Decimal)
        event = source["events"][0]
        reply = source["model_responses"][event["message_id"]]["reply"]
        variant = "pause" if spec.case_id == "S06" else "target"
        order = decode_request(prepare(spec, variant).request_body).order
        assert order["size"] == Decimal(str(reply["size"]))
        assert order["instrument"] == reply["instrument"] and order["side"] == reply["side"]
        assert order["stop"] == Decimal("1.09") and order["order_type"] == "market"
        assert order["action"] == "open" and reply["action"] == "enter"
        if spec.case_id == "S04":
            positive = decode_request(prepare(spec).request_body).order
            assert positive["size"] == Decimal("0.1") and order["size"] == Decimal("2")
            assert {
                key: value for key, value in order.items() if key not in ("size", "client_order_id")
            } == {
                key: value
                for key, value in positive.items()
                if key not in ("size", "client_order_id")
            }


def test_breach_then_recovery_preserves_real_session_latch_and_original_receipts():
    spec = next(row for row in load_scenarios() if row.case_id == "S05")
    prepared = prepare(spec, "target")
    state, receipts, result = check(prepared)
    assert len(receipts) == 2 and receipts[0].daily_loss == Decimal("1000")
    assert state.latest.equity == Decimal("10000") and state.daily_loss == Decimal("0")
    assert all(row.daily_loss_latched for row in receipts) and state.daily_loss_latched
    assert result.code == "daily_loss_stop"


def test_controls_and_stale_generation_require_actual_sql_operations_not_false_source_facts():
    for case_id, variants in (("S06", ("pause", "kill")), ("S07", ("target",))):
        spec = next(row for row in load_scenarios() if row.case_id == case_id)
        for variant in variants:
            prepared = prepare(spec, variant)
            _, _, result = check(prepared)
            assert result.within_limits  # SQL runner must perform the real mutation.
            assert prepared.control_action == (variant if case_id == "S06" else None)
            assert prepared.replace_lease is (case_id == "S07")
            request = decode_request(prepared.request_body)
            assert request.facts.kill_switch is False and request.facts.daily_loss_latched is False


def test_declared_clock_maps_original_available_at_not_midnight_into_observed_sql_time():
    for spec in load_scenarios():
        prepared = prepare(spec)
        clock = json.loads(prepared.metadata_body)
        original = json.loads(spec.source_case_body)
        assert clock["source_clock_start"] == original["clock_start"]
        assert clock["source_available_at"] == original["events"][0]["available_at"]
        mapped = datetime.fromisoformat(clock["source_available_at"]) + timedelta(
            microseconds=clock["offset_microseconds"]
        )
        assert mapped == NOW == datetime.fromisoformat(clock["sql_observed_at"])
        assert datetime.fromisoformat(clock["source_clock_start"]) != mapped
        shifted = prepare(spec, now=NOW + timedelta(seconds=7))
        assert captured_order(shifted.request_body) == captured_order(prepared.request_body)


def test_immutable_captured_order_projection_uses_existing_admission_typed_codec():
    prepared = prepare(load_scenarios()[0])
    original = captured_order(prepared.request_body)
    mutable = decode_request(prepared.request_body)
    mutable.order["size"] = Decimal("0.01")
    assert captured_order(prepared.request_body) == original
    assert captured_order(encode_request(mutable)) != original
    assert json.loads(original)[0] == "dict" and '["decimal","0.1"]' in original


@pytest.mark.parametrize(
    "fault", ["source-hash", "supplement-hash", "unknown-variant", "naive", "nonutc"]
)
def test_invalid_lineage_variant_or_clock_refuses_before_prepared_inputs(fault):
    spec = load_scenarios()[0]
    variant, now = "positive", NOW
    if fault == "source-hash":
        spec = replace(spec, source_case_sha256="0" * 64)
    elif fault == "supplement-hash":
        spec = replace(spec, supplement_sha256="0" * 64)
    elif fault == "unknown-variant":
        variant = "already-passed"
    elif fault == "naive":
        now = NOW.replace(tzinfo=None)
    else:
        from datetime import timezone

        now = NOW.astimezone(timezone(timedelta(hours=-4)))
    with pytest.raises(ScenarioRefused, match="^invalid_sql_eval_scenario$"):
        prepare(spec, variant, now=now)


def test_profile_loader_refuses_changed_original_bytes_and_false_synthetic_claim(
    tmp_path, monkeypatch
):
    original = scenarios.SOURCE_PATH.read_bytes()
    copied = tmp_path / "source.jsonl"
    copied.write_bytes(original + b"\n")
    monkeypatch.setattr(scenarios, "SOURCE_PATH", copied)
    with pytest.raises(ScenarioRefused):
        load_scenarios()
    copied.write_bytes(original)
    profile = json.loads(scenarios.PROFILE_PATH.read_text())
    profile["common"]["synthetic_only"] = False
    changed = tmp_path / "profile.json"
    changed.write_text(json.dumps(profile))
    monkeypatch.setattr(scenarios, "PROFILE_PATH", changed)
    with pytest.raises(ScenarioRefused):
        load_scenarios()


def test_preparation_preserves_low_ambient_decimal_context_and_exact_values():
    spec = load_scenarios()[2]
    expected = prepare(spec, "target")
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        before = context.copy()
        assert prepare(spec, "target") == expected
        assert context.prec == before.prec and context.traps == before.traps
        assert context.flags == before.flags


def test_profile_bad_decimal_refuses_with_fixed_error(tmp_path, monkeypatch):
    profile = json.loads(scenarios.PROFILE_PATH.read_text())
    profile["common"]["policy"]["max_daily_loss"] = "PRIVATE-invalid-number"
    path = tmp_path / "bad-profile.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(scenarios, "PROFILE_PATH", path)
    with pytest.raises(ScenarioRefused, match="^invalid_sql_eval_scenario$"):
        load_scenarios()
