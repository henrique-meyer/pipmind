"""Engine integration uses a real prompt compiler and inert synthetic agent only."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import stamp, utc
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import FoundryModel, ModelFailure

NOW = utc("2030-02-04T12:00:00Z")
BUILD = "a" * 40
WORKING = {
    "views": ["Synthetic dated forecast; not an observed measurement"],
    "waiting_for": ["Each original claim needs the full official text before interpretation"],
    "position_theses": [],
}


class Store:
    def __init__(self, *, mandatory_bytes=15500, commit_error=None):
        self.event = {
            "kind": "ClockEvent",
            "message_id": "synthetic-budget-clock",
            "payload": {"reason": "heartbeat", "ts": stamp(NOW)},
        }
        self.base = {
            "revision": 7,
            "paused": False,
            "identity": "Preserve all synthetic evidence and uncertainty",
            "playbook": {"version": "synthetic-v1", "text": "Read only; no orders"},
            "working_memory": deepcopy(WORKING),
            "broker_snapshots": {"positions": {"positions": []}},
            "feed_status": {"coverage": "mandatory-fact-" + "m" * mandatory_bytes},
        }
        self.evidence = {
            "retrieved_memories": [
                {
                    "metadata": {
                        "record_id": f"synthetic-evidence-{i}",
                        "part": "archive",
                        "ts": stamp(NOW),
                    },
                    "body": f"Synthetic historical uncertainty {i}: " + "x" * 1200,
                    "partial": False,
                }
                for i in range(6)
            ],
            "recent_journal": [],
        }
        self.calls, self.commits, self.holds = [], [], []
        self.commit_error = commit_error

    def prepare_step(self, moment):
        self.calls.append("prepare")
        return deepcopy(self.event), deepcopy(self.base), None

    def context(self, moment, query, *, revision):
        assert moment == NOW and revision == 7
        self.calls.append("evidence")
        return deepcopy(self.evidence)

    def commit(self, event, revision, result, moment):
        self.calls.append("commit")
        assert event == self.event and revision == 7 and moment == NOW
        if self.commit_error:
            raise self.commit_error
        self.commits.append(deepcopy(result))

    def hold(self, event, revision, result, moment, reason):
        self.calls.append("hold")
        assert event == self.event and revision == 7 and moment == NOW
        self.holds.append((deepcopy(result), reason))

    def search(self, *args):
        raise AssertionError("No extra retrieval belongs in evidence budgeting")


class Model(FoundryModel):
    def __init__(self, *, subsequent_failure=None, first_failure=None):
        self.requests, self.refusals = [], []
        self.factories, self.rpcs = 0, 0
        self.subsequent_failure, self.first_failure = subsequent_failure, first_failure
        super().__init__(
            "https://synthetic.invalid/api/projects/test",
            "synthetic-triage",
            "synthetic-decision",
            enabled=True,
            factory=self.factory,
        )

    async def triage(self, event, context, store, moment):
        return {"wake": True, "summary": "Synthetic evidence warrants a decision"}, {
            "phase": "triage",
            "attempted": True,
            "usage_unknown": False,
            "input_tokens": 123,
            "output_tokens": 12,
        }

    async def decide(self, event, context, triage, store, moment):
        self.requests.append(deepcopy((event, context, triage, moment)))
        if self.first_failure is not None and len(self.requests) == 1:
            raise self.first_failure
        if self.subsequent_failure is not None and len(self.requests) > 1:
            raise self.subsequent_failure
        try:
            return await super().decide(event, context, triage, store, moment)
        except ModelFailure as error:
            self.refusals.append(deepcopy(error.trace))
            raise

    def factory(self, **kwargs):
        self.factories += 1
        assert kwargs["phase"] == "decision"
        owner = self

        class Agent:
            async def run(self, message, *, options):
                owner.rpcs += 1
                assert options["max_tokens"] == 4096
                assert len((kwargs["instructions"] + message).encode()) <= 28000
                return SimpleNamespace(
                    text=encoded(
                        {
                            "action": "hold",
                            "reasoning": "Retain every active prerequisite",
                            "evidence_ids": [],
                            "tool_calls": [],
                            "working_memory": deepcopy(WORKING),
                        }
                    ),
                    usage_details={"input_token_count": 234, "output_token_count": 23},
                )

        return Agent()


def run(store, model, monkeypatch):
    monkeypatch.setenv("BUILD_SHA", BUILD)
    return asyncio.run(ShadowEngine(store, model, clock=lambda: NOW).step())


def test_real_pre_rpc_guard_then_one_inert_rpc_keeps_original_context_and_all_usage(monkeypatch):
    store, model = Store(), Model()
    original = deepcopy((store.base, store.evidence))
    assert run(store, model, monkeypatch) is True
    assert len(model.requests) == 2 and model.factories == model.rpcs == 1
    assert len(model.refusals) == 1
    refused = model.refusals[0]
    assert refused["prompt_bytes"] > 28000
    assert refused["attempted"] is False and refused["usage_unknown"] is False
    assert store.calls == ["prepare", "evidence", "commit"]
    assert not store.holds and len(store.commits) == 1
    result = store.commits[0]
    assert result["usage"][0]["input_tokens"] == 123
    assert result["usage"][1] == refused
    assert result["usage"][2]["attempted"] is True
    assert result["usage"][2]["input_tokens"] == 234
    assert result["context"] == {**original[0], **original[1]}
    audit = result["decision_evidence_budget"]
    assert audit["method"] == "decision-evidence-exact-subset-v1"
    assert audit["original_refusal"] == refused
    assert audit["before_context_sha256"] == digest(encoded(result["context"]))
    assert audit["after_context_sha256"] == digest(encoded(model.requests[1][1]))
    actual_request = result["usage"][2]["request"]
    assert audit["after_prompt_sha256"] == digest(
        actual_request["instructions"] + encoded(actual_request["message"])
    )
    assert audit["after_prompt_bytes"] == result["usage"][2]["prompt_bytes"] <= 28000
    assert audit["removed_rows"]
    for removed in audit["removed_rows"]:
        assert removed["row"] == original[1][removed["field"]][removed["index"]]
        assert removed["sha256"] == digest(encoded(removed["row"]))
    assert (store.base, store.evidence) == original
    assert result["reply"]["working_memory"] == WORKING
    assert model.requests[0][0::2] == model.requests[1][0::2]
    assert model.requests[0][3] == model.requests[1][3] == NOW
    for key in store.base:
        assert model.requests[1][1][key] == store.base[key]
    assert len(model.requests[1][1]["retrieved_memories"]) < len(
        store.evidence["retrieved_memories"]
    )
    # Full omitted source strings still exist in the authoritative original context.
    for row in store.evidence["retrieved_memories"]:
        assert row in result["context"]["retrieved_memories"]


def test_mandatory_state_alone_oversize_holds_without_any_factory_or_rpc(monkeypatch):
    store, model = Store(mandatory_bytes=30000), Model()
    assert run(store, model, monkeypatch) is False
    assert len(model.requests) == 1 and model.factories == model.rpcs == 0
    assert not store.commits and len(store.holds) == 1
    result, reason = store.holds[0]
    assert reason == "shadow_context_size"
    assert result["usage"][-1] == model.refusals[0]
    assert result["context"]["retrieved_memories"] == store.evidence["retrieved_memories"]


def test_already_fitting_decision_uses_original_single_call_and_no_budget_audit(monkeypatch):
    store, model = Store(mandatory_bytes=1000), Model()
    assert run(store, model, monkeypatch) is True
    assert len(model.requests) == model.factories == model.rpcs == 1
    assert len(store.commits[0]["usage"]) == 2
    assert "decision_evidence_budget" not in store.commits[0]
    assert not model.refusals


@pytest.mark.parametrize("attempted,unknown", [(True, True), (True, False), (False, True)])
def test_uncertain_or_attempted_first_decision_never_gets_second_invocation(
    monkeypatch, attempted, unknown
):
    failure = ModelFailure(
        {
            "phase": "decision",
            "failure_code": "shadow_context_size",
            "attempted": attempted,
            "usage_unknown": unknown,
            "application_build": BUILD,
        }
    )
    store, model = Store(), Model(first_failure=failure)
    assert run(store, model, monkeypatch) is False
    assert len(model.requests) == 1 and model.factories == model.rpcs == 0
    assert store.holds[0][0]["usage"][-1] == failure.trace


def test_second_pre_rpc_failure_is_held_once_without_recursive_reassembly(monkeypatch):
    second = ModelFailure(
        {
            "phase": "decision",
            "failure_code": "shadow_context_size",
            "attempted": False,
            "usage_unknown": False,
            "application_build": BUILD,
        }
    )
    store, model = Store(), Model(subsequent_failure=second)
    assert run(store, model, monkeypatch) is False
    assert len(model.requests) == 2 and model.factories == model.rpcs == 0
    result, reason = store.holds[0]
    assert reason == "shadow_context_size" and len(result["usage"]) == 3
    assert result["usage"][1] == model.refusals[0]
    assert result["usage"][2] == second.trace
    assert not store.commits


def test_second_unknown_rpc_outcome_stays_unknown_and_is_never_retried(monkeypatch):
    unknown = ModelFailure(
        {
            "phase": "decision",
            "failure_code": "model_error",
            "attempted": True,
            "usage_unknown": True,
            "application_build": BUILD,
        }
    )
    store, model = Store(), Model(subsequent_failure=unknown)
    assert run(store, model, monkeypatch) is True
    assert len(model.requests) == 2
    result = store.commits[0]
    assert result["status"] == "model_refused"
    assert result["usage"][1] == model.refusals[0]
    assert result["usage"][2] == unknown.trace
    assert result["reply"] is None


def test_commit_error_preserves_existing_failure_identity_after_single_decision(monkeypatch):
    failure = ShadowError("shadow_store_busy")
    store, model = Store(commit_error=failure), Model()
    with pytest.raises(ShadowError) as caught:
        run(store, model, monkeypatch)
    assert caught.value is failure
    assert len(model.requests) == 2 and model.factories == model.rpcs == 1
    assert not store.commits and not store.holds


def test_caller_cancellation_during_additional_assembly_propagates_without_replay(monkeypatch):
    failure = asyncio.CancelledError()
    store, model = Store(), Model(subsequent_failure=failure)
    with pytest.raises(asyncio.CancelledError) as caught:
        run(store, model, monkeypatch)
    assert caught.value is failure
    assert len(model.requests) == 2 and model.factories == model.rpcs == 0
    assert not store.commits and not store.holds
