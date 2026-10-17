"""Known-good controls plus deliberately broken traces test each grader independently."""

from copy import deepcopy

import pytest

from pipmind.graders import grade
from pipmind.replay import replay_episode


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("unknown_evidence", "evidence_availability"),
        ("future_decision", "decision_chronology"),
        ("drop_event", "event_retention"),
        ("duplicate_event", "duplicate_event"),
        ("wrong_wake", "triage_labels"),
        ("refuse_everything", "decision_count"),
        ("private_dispatch", "forbidden_dispatch"),
        ("duplicate_command", "duplicate_command"),
        ("rewrite_identity", "immutable_mind"),
        ("rewrite_playbook", "immutable_mind"),
        ("wrong_version", "playbook_version"),
        ("invent_position", "position_consistency"),
        ("drop_journal", "journal_retention"),
        ("malformed", "trace_schema"),
        ("x_write", "x_mutation_attempt"),
        ("invalid_tool", "tool_arguments"),
    ],
)
def test_seeded_core_failures_are_not_hidden_by_aggregate_scores(cases, contracts, mutation, code):
    case = cases["T01"]
    trace, failures = replay_episode(case, contracts=contracts)
    assert not failures
    if mutation == "unknown_evidence":
        trace["decisions"][0]["evidence_ids"].append("does-not-exist")
    elif mutation == "future_decision":
        trace["decisions"][0]["ts"] = "2026-10-06T00:00:00Z"
    elif mutation == "drop_event":
        trace["seen"] = []
    elif mutation == "duplicate_event":
        trace["seen"].append(deepcopy(trace["seen"][0]))
    elif mutation == "wrong_wake":
        trace["triage"][0]["wake"] = False
    elif mutation == "refuse_everything":
        trace["decisions"] = []
    elif mutation == "private_dispatch":
        trace["dispatches"] = [{"command_id": "synthetic"}]
    elif mutation == "duplicate_command":
        trace["commands"] = [{"command_id": "synthetic", "dispatch_count": 2}]
    elif mutation == "rewrite_identity":
        trace["final_mind"]["identity"] = "rewritten"
    elif mutation == "rewrite_playbook":
        trace["final_mind"]["playbook"]["text"] = "rewritten"
    elif mutation == "wrong_version":
        trace["decisions"][0]["playbook_version"] = "unknown-version"
    elif mutation == "invent_position":
        trace["final_mind"]["working_memory"]["position_theses"] = [
            {
                "instrument": "UNKNOWN/PAIR",
                "thesis": "invented",
                "as_of": trace["decisions"][0]["ts"],
            }
        ]
    elif mutation == "drop_journal":
        trace["final_mind"]["journal"] = []
    elif mutation == "malformed":
        trace["decisions"][0]["size"] = "many"
    else:
        trace["tool_requests"] = [
            {
                "event_id": "t01-release",
                "ts": trace["decisions"][0]["ts"],
                "name": "x.post" if mutation == "x_write" else "read_mind",
                "arguments": {"unexpected": True},
                "allowed": True,
            }
        ]
    assert code in grade(case, trace, contracts)


def test_future_revision_cannot_be_laundered_by_altering_trace_availability(cases, contracts):
    case = cases["D04"]
    trace, failures = replay_episode(case, contracts=contracts)
    assert not failures
    trace["decisions"][0]["evidence_ids"].append("d04-revision")
    trace["seen"][1]["available_at"] = trace["seen"][0]["available_at"]
    assert {"event_integrity", "evidence_availability"} <= set(grade(case, trace, contracts))


def test_expected_memory_change_is_required(cases, contracts):
    trace, failures = replay_episode(cases["D01"], contracts=contracts)
    assert not failures
    trace["final_mind"]["working_memory"]["views"] = []
    assert "working_memory" in grade(cases["D01"], trace, contracts)


def test_later_timestamp_cannot_launder_future_evidence_for_an_earlier_cause(cases, contracts):
    case = cases["D04"]
    trace, failures = replay_episode(case, contracts=contracts)
    assert not failures
    trace["decisions"][0]["evidence_ids"].append("d04-revision")
    trace["decisions"][0]["ts"] = trace["decisions"][1]["ts"]
    assert "decision_causation" in grade(case, trace, contracts)


def test_historical_journal_and_override_tags_cannot_be_rewritten(cases, contracts):
    case = cases["T01"]
    trace, failures = replay_episode(case, contracts=contracts)
    assert not failures
    trace["final_mind"]["journal"][0]["override"] = True
    assert "journal_integrity" in grade(case, trace, contracts)


def test_refusal_must_be_journaled(cases, contracts):
    case = deepcopy(cases["T01"])
    case["model_responses"]["t01-release"]["error"] = "triage"
    case["expected"].update(
        wake_labels={},
        minimum_decisions=0,
        maximum_decisions=0,
        required_evidence=[],
        refusal_codes=["triage_unavailable"],
    )
    trace, failures = replay_episode(case, contracts=contracts)
    assert not failures
    trace["final_mind"]["journal"] = [
        row for row in trace["final_mind"]["journal"] if row["kind"] != "refusal"
    ]
    assert "refusal_journal" in grade(case, trace, contracts)


def test_control_grader_positive_and_negative_are_not_runtime_certification(cases, contracts):
    case = cases["S02"]
    trace, failures = replay_episode(case, contracts=contracts)
    assert "guard_refusal" in failures  # The core has no M5 boundary implementation.
    trace["guard_records"] = [{"control": "stale_quote", "refused": True, "journaled": True}]
    assert not grade(case, trace, contracts)
    trace["guard_records"][0]["journaled"] = False
    assert "guard_refusal" in grade(case, trace, contracts)


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("version", "review_version"),
        ("single", "review_retirement"),
        ("override", "review_override"),
        ("unknown", "review_evidence"),
        ("empty", "review_evidence"),
    ],
)
def test_review_grader_rejects_known_bad_fake_reviews(cases, contracts, mutation, code):
    case = cases["R04"]
    trace, _ = replay_episode(case, contracts=contracts)
    trace["reviews"] = [
        {
            "action": "retire",
            "previous_version": 1,
            "version": 2,
            "evidence_ids": ["journal-seed", "journal-second"],
            "refused": False,
        }
    ]
    assert not grade(case, trace, contracts)
    row = trace["reviews"][0]
    if mutation == "version":
        row["version"] = 1
    else:
        row["evidence_ids"] = {
            "single": ["journal-seed"],
            "override": ["override-row"],
            "unknown": ["missing"],
            "empty": [],
        }[mutation]
    assert code in grade(case, trace, contracts)


def test_public_grader_rejects_private_prose_and_raw_quotes(cases, contracts, messages):
    case = cases["P01"]
    trace, _ = replay_episode(case, contracts=contracts)
    trace["public_snapshots"] = [deepcopy(messages["public-snapshot"])]
    assert not grade(case, trace, contracts)
    trace["public_snapshots"][0]["disclaimer"] += " SYNTHETIC-PRIVATE-MARKER"
    assert "public_private_value" in grade(case, trace, contracts)
    trace["public_snapshots"][0]["bid"] = 1.1
    assert "trace_schema" in grade(case, trace, contracts)
