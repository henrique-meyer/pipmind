"""Real prompt compilation with synthetic data; never invokes a model factory."""

import asyncio
from copy import deepcopy

import pytest

from pipmind.contracts import stamp, utc
from pipmind.shadow.common import digest, encoded
from pipmind.shadow.context import phase_context
from pipmind.shadow.decision_budget import fit_decision_evidence
from pipmind.shadow.maintenance import POLICY
from pipmind.shadow.model import FoundryModel, ModelFailure

NOW = utc("2030-02-04T12:00:00Z")


def refused(*, mandatory=14500, bodies=None, policy=False, daily=False):
    event = {"kind": "ClockEvent", "message_id": "synthetic", "payload": {"reason": "heartbeat"}}
    context = {
        "revision": 7,
        "paused": False,
        "identity": "Synthetic uncertainty",
        "playbook": {"version": "test", "text": "Read only"},
        "working_memory": {
            "views": ["Preserve every condition"],
            "waiting_for": [],
            "position_theses": [],
        },
        "feed_status": {"coverage": "m" * mandatory},
        "retrieved_memories": [
            {
                "metadata": {"record_id": f"evidence-{i}", "part": "archive", "ts": stamp(NOW)},
                "body": body,
                "partial": False,
            }
            for i, body in enumerate(bodies or ["x" * 1200] * 6)
        ],
        "recent_journal": [],
    }
    triage = {"wake": True, "summary": "Synthetic event warrants inspection"}
    if policy:
        context["memory_policy"] = {"version": POLICY}
    if daily:
        event["payload"]["reason"] = "daily_brief"
        context["feed_status"]["calendar"] = {"record_id": "synthetic-calendar"}

    def forbidden(**kwargs):
        pytest.fail("The original oversized request must never reach a model factory")

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=forbidden
    )
    with pytest.raises(ModelFailure) as error:
        asyncio.run(model.decide(event, context, triage, None, NOW))
    assert error.value.trace["failure_code"] == "shadow_context_size"
    return event, context, triage, deepcopy(error.value.trace)


def fit(values):
    event, context, triage, trace = values
    return fit_decision_evidence(event, context, triage, NOW, trace)


def test_real_compiler_exact_subset_and_complete_audit_without_mutation(monkeypatch):
    monkeypatch.setenv("BUILD_SHA", "a" * 40)
    values = refused()
    before = encoded(values)
    result = fit(values)
    assert result is not None
    candidate, audit = result
    assert audit["before_prompt_bytes"] > 28000 >= audit["after_prompt_bytes"]
    assert audit["original_refusal"] == values[-1]
    assert audit["before_context_sha256"] == digest(encoded(values[1]))
    assert audit["after_context_sha256"] == digest(encoded(candidate))
    assert audit["removed_rows"]
    for removed in audit["removed_rows"]:
        assert removed["row"] == values[1][removed["field"]][removed["index"]]
        assert removed["sha256"] == digest(encoded(removed["row"]))
    original = audit["original_supplied_cards"]
    assert audit["selected_supplied_cards"] == original[: len(audit["selected_supplied_cards"])]
    assert len(audit["projection_steps"]) <= len(original) + 1
    assert encoded(values) == before


def test_daily_brief_and_bounded_policy_instructions_remain_exact():
    values = refused(policy=True, daily=True)
    candidate, audit = fit(values)
    assert candidate["memory_policy"] == values[1]["memory_policy"]
    assert candidate["feed_status"] == values[1]["feed_status"]
    assert audit["after_prompt_bytes"] <= 28000


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("phase", "triage"),
        ("phase", "repair"),
        ("attempted", True),
        ("attempted", 0),
        ("usage_unknown", True),
        ("usage_unknown", 0),
        ("input_tokens", 1),
        ("output_tokens", 0),
        ("invalid_response", "unknown"),
        ("failure_code", "shadow_context_projection"),
        ("error_type", "TimeoutError"),
        ("prompt_version", "old"),
        ("context_version", "old"),
        ("application_build", "wrong"),
        ("max_output_tokens", 4095),
        ("working_memory_encoding", "old"),
        ("tool_names", []),
        ("tools", [{"name": "search_memory"}]),
        ("prompt_bytes", True),
        ("latency_ms", True),
        ("latency_ms", -1),
        ("reasoning_effort", "low"),
    ],
)
def test_refuses_unknown_or_different_attempt_contract(key, value):
    values = refused()
    values[-1][key] = value
    assert fit(values) is None


@pytest.mark.parametrize("target", ["instructions", "schema", "event", "triage", "now", "context"])
def test_reconstructs_exact_baseline_not_just_size(target):
    values = refused()
    request = values[-1]["request"]
    if target == "instructions":
        request[target] += " Changed"
    else:
        request["message"][target] = "tampered"
    assert fit(values) is None


def test_argument_event_triage_and_time_are_bound():
    event, context, triage, trace = refused()
    assert (
        fit_decision_evidence({**event, "message_id": "other"}, context, triage, NOW, trace) is None
    )
    assert fit_decision_evidence(event, context, {**triage, "wake": False}, NOW, trace) is None
    assert fit_decision_evidence(event, context, triage, utc("2030-02-04T12:01:00Z"), trace) is None


def test_mandatory_alone_oversized_stays_refused():
    values = refused(mandatory=24000)
    assert fit(values) is None


def test_originally_omitted_card_never_backfills():
    values = refused(mandatory=17000, bodies=["a" * 2200] * 6)
    original_ids = {
        card["metadata"]["record_id"]
        for card in phase_context(values[1], "decision")["retrieved_memories"]
    }
    result = fit(values)
    assert result is not None
    candidate, audit = result
    assert any(row["reason"] == "not_originally_supplied" for row in audit["removed_rows"])
    assert all(
        row["metadata"]["record_id"] in original_ids for row in candidate["retrieved_memories"]
    )


def test_duplicate_occurrences_removed_together_across_fields():
    values = refused()
    # Recompile a genuine refusal after adding a cross-field duplicate.
    event, context, triage, _ = values
    context["recent_journal"] = [deepcopy(context["retrieved_memories"][-1])]

    def forbidden(**kwargs):
        pytest.fail("Synthetic duplicate test must not instantiate a model agent")

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=forbidden
    )
    with pytest.raises(ModelFailure) as error:
        asyncio.run(model.decide(event, context, triage, None, NOW))
    result = fit((event, context, triage, error.value.trace))
    assert result is not None
    candidate, audit = result
    removed_ids = {row["record_id"] for row in audit["removed_rows"]}
    assert all(
        row["metadata"]["record_id"] not in removed_ids
        for field in ("retrieved_memories", "recent_journal")
        for row in candidate[field]
    )


def test_multibyte_byte_guard_and_mandatory_exact():
    values = refused(mandatory=17000, bodies=["é" * 1100] * 6)
    candidate, audit = fit(values)
    assert audit["after_prompt_bytes"] <= 28000
    for key in values[1].keys() - {"retrieved_memories", "recent_journal"}:
        assert encoded(candidate[key]) == encoded(values[1][key])


def test_bounds_and_malformed_rows_refuse_without_touching_inputs():
    values = refused()
    values[1]["retrieved_memories"].append(deepcopy(values[1]["retrieved_memories"][0]))
    assert fit(values) is None
    values = refused()
    values[1]["retrieved_memories"][0]["partial"] = 1
    assert fit(values) is None


@pytest.mark.parametrize("mutation", ["body", "partial", "metadata", "order", "mandatory"])
def test_independently_rejects_projection_drift_or_resurrection(monkeypatch, mutation):
    values = refused()
    calls = 0

    def hostile_projection(context, phase):
        nonlocal calls
        calls += 1
        projected = phase_context(context, phase)
        if calls == 1:
            return projected
        cards = projected["retrieved_memories"]
        if mutation == "body":
            cards[0]["body"] += " altered"
        elif mutation == "partial":
            cards[0]["partial"] = not cards[0]["partial"]
        elif mutation == "metadata":
            cards[0]["metadata"]["ts"] = "2030-02-05T00:00:00Z"
        elif mutation == "order":
            cards.reverse()
        else:
            projected["coverage"]["missing_evidence"] = "absent"
        return projected

    monkeypatch.setattr("pipmind.shadow.decision_budget.phase_context", hostile_projection)
    assert fit(values) is None


@pytest.mark.parametrize("key", ["attempted", "usage_unknown", "input_tokens", "invalid_response"])
def test_missing_known_no_call_fact_is_not_assumed(key):
    values = refused()
    values[-1].pop(key)
    assert fit(values) is None
