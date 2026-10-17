from copy import deepcopy

import pytest

from pipmind.contracts import ContractError, event_id
from pipmind.core import InMemoryStore, ReplayGateway, ScriptedModel
from pipmind.dataset import IMPLEMENTED
from pipmind.replay import replay_episode


class WatchingModel(ScriptedModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.contexts = []

    def decide(self, event, context, triage):
        self.contexts.append(deepcopy(context))
        return super().decide(event, context, triage)


def test_real_loop_passes_all_implemented_scenarios(cases, contracts):
    applicable = [
        case for case in cases.values() if set(case["required_capabilities"]) <= IMPLEMENTED
    ]
    assert len(applicable) == 14
    for case in applicable:
        trace, failures = replay_episode(case, contracts=contracts)
        assert not failures, (case["case_id"], failures)
        assert trace["dispatches"] == []


def test_multi_event_memory_and_broker_context_reach_next_decision(cases, contracts):
    case = cases["D02"]
    model = WatchingModel(case["model_responses"])
    trace, failures = replay_episode(case, model=model, contracts=contracts)
    assert not failures
    assert model.contexts[0]["working_memory"]["views"] == []
    assert model.contexts[1]["working_memory"]["views"] == ["awaiting-confirmation"]
    assert model.contexts[1]["broker_snapshots"]["positions"] == case["initial_positions"]
    assert trace["final_mind"]["working_memory"]["views"] == ["confirmation-reviewed"]
    assert case["initial_mind"]["working_memory"]["views"] == []


def test_revision_and_later_evidence_are_not_in_earlier_context(cases, contracts):
    case = cases["D04"]
    model = WatchingModel(case["model_responses"])
    _, failures = replay_episode(case, model=model, contracts=contracts)
    assert not failures
    earlier = {row["record_id"] for row in model.contexts[0]["recent_journal"]}
    later = {row["record_id"] for row in model.contexts[1]["recent_journal"]}
    assert "d04-revision" not in earlier
    assert "d04-revision" in later


def test_future_evidence_is_refused_without_mutating_working_memory(cases, contracts):
    case = deepcopy(cases["D04"])
    case["model_responses"]["d04-original"]["reply"]["evidence_ids"] = ["d04-revision"]
    trace, failures = replay_episode(case, contracts=contracts)
    assert "decision_count" in failures
    assert trace["refusals"][0]["code"] == "unavailable_evidence"
    assert len(trace["decisions"]) == 1
    assert not trace["dispatches"]


@pytest.mark.parametrize("phase,calls", [("triage", 1), ("decision", 2)])
def test_model_outage_has_no_keyword_or_order_fallback(cases, contracts, phase, calls):
    case = deepcopy(cases["T01"])
    case["model_responses"]["t01-release"]["error"] = phase
    trace, _ = replay_episode(case, contracts=contracts)
    assert trace["decisions"] == []
    assert trace["dispatches"] == []
    assert trace["refusals"][0]["code"] == phase + "_unavailable"
    assert trace["usage"]["model_calls"] == calls
    assert event_id(trace["seen"][0]) == "t01-release"
    assert any(row["kind"] == "refusal" for row in trace["final_mind"]["journal"])


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "x.post", "arguments": {"text": "UNTRUSTED SYNTHETIC"}},
        {"name": "gateway.dispatch", "arguments": {}},
        {"name": "read_mind", "arguments": {"rewrite_identity": True}},
        {"name": "search_memory", "arguments": {"query": 123}},
    ],
)
def test_unregistered_or_invalid_tools_are_refused(cases, contracts, tool):
    case = deepcopy(cases["T01"])
    case["model_responses"]["t01-release"]["reply"]["tool_calls"] = [tool]
    trace, _ = replay_episode(case, contracts=contracts)
    assert not trace["decisions"] and not trace["dispatches"]
    assert trace["refusals"]
    assert not trace["tool_results"]


def test_memory_tool_returns_bounded_copied_records(cases, contracts):
    trace, failures = replay_episode(cases["D05"], contracts=contracts)
    assert not failures
    rows = trace["tool_results"][0]["result"]
    assert [row["record_id"] for row in rows] == ["memory-seed"]
    store = InMemoryStore(cases["D05"]["initial_mind"])
    rows = store.search("inflation")
    rows[0]["body"] = "mutated"
    assert store.search("inflation")[0]["body"] != "mutated"


def test_duplicate_delivery_does_not_repeat_model_calls(cases, contracts):
    trace, failures = replay_episode(cases["T05"], contracts=contracts)
    assert not failures
    assert len(trace["seen"]) == len(trace["decisions"]) == 1
    assert trace["usage"]["model_calls"] == 2
    case = deepcopy(cases["T05"])
    case["events"][1]["payload"]["actual"] = 999
    with pytest.raises(ContractError, match="conflicting_duplicate_event"):
        replay_episode(case, contracts=contracts)


def test_fresh_case_repetition_and_variant_state_never_leak(cases, contracts):
    baseline, _ = replay_episode(cases["D01"], contracts=contracts)
    candidate = deepcopy(cases["D01"])
    candidate["model_responses"]["d01-release"]["reply"]["working_memory"]["views"] = ["candidate"]
    changed, _ = replay_episode(candidate, contracts=contracts)
    repeated, _ = replay_episode(cases["D01"], contracts=contracts)
    other, _ = replay_episode(cases["T01"], contracts=contracts)
    assert changed["final_mind"] != baseline["final_mind"]
    assert baseline["final_mind"] == repeated["final_mind"]
    assert other["final_mind"]["working_memory"]["views"] == []


def test_simulated_gateway_has_no_dispatch_transport(cases):
    gateway = ReplayGateway(cases["T01"]["initial_account"], cases["T01"]["initial_positions"])
    with pytest.raises(RuntimeError, match="broker_dispatch_disabled"):
        gateway.dispatch({"client_order_id": "synthetic"})
    assert len(gateway.attempts) == 1


def test_duplicate_journal_write_cannot_partially_update_working_memory(cases):
    mind = cases["T01"]["initial_mind"]
    store = InMemoryStore(mind)
    with pytest.raises(ContractError, match="duplicate_journal_record"):
        store.commit_decision(mind["journal"][0], {"views": ["should-not-commit"]})
    assert store.snapshot() == mind


def test_equivalent_utc_offsets_do_not_create_false_chronology_failures(cases, contracts):
    case = deepcopy(cases["T01"])
    event = case["events"][0]
    event["available_at"] = event["available_at"].replace("Z", "+00:00")
    for field in ("received_at", "released_at", "scheduled_at"):
        event["payload"][field] = event["payload"][field].replace("Z", "+00:00")
    trace, failures = replay_episode(case, contracts=contracts)
    assert not failures
    assert trace["decisions"][0]["ts"].endswith("Z")
