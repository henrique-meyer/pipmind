import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.shadow.common import ShadowError, encoded, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import FoundryModel, ModelFailure


def example():
    before = {
        "views": ["EUR uncertainty"],
        "waiting_for": ["Need official text"],
        "position_theses": [],
    }
    candidate = {
        "action": "hold",
        "reasoning": "Uncertain; keep every dependency",
        "evidence_ids": ["input"],
        "tool_calls": [],
        "working_memory": {
            "views": [f"EUR headline {i}: uncertainty remains" for i in range(9)],
            "waiting_for": ["Need official text", "Need release vintage"],
            "position_theses": [],
        },
    }
    after = {
        "views": ["EUR headlines 0,1,2,3,4,5,6,7,8: uncertainty remains"],
        "waiting_for": ["Need official text and release vintage"],
        "position_theses": [],
    }

    def row(source, target, disposition="merged"):
        return {
            "from": source,
            "to": target,
            "disposition": disposition,
            "reason": "Same explicit dependencies in concise wording",
            "evidence_ids": ["input"],
        }

    correction = {
        "working_memory": after,
        "memory_dispositions": [row("v0", "v0", "updated"), row("w0", "w0", "updated")],
        "candidate_dispositions": [
            *[row(f"v{i}", "v0") for i in range(9)],
            row("w0", "w0"),
            row("w1", "w0"),
        ],
    }
    context = {
        "working_memory": before,
        "memory_policy": {
            "version": "bounded-working-v1",
            "checkpoint": "checkpoint",
            "origin_checkpoint": "origin",
        },
    }
    return context, candidate, correction


def test_correction_keeps_decision_exact_and_audits_candidate_usage_and_both_mappings():
    context, candidate, correction = example()
    originals = deepcopy((context, candidate))
    calls = []
    usage = {"phase": "memory_repair", "input_tokens": 789, "output_tokens": 456}

    async def repair(*args):
        calls.append(args)
        return deepcopy(correction), usage

    engine = ShadowEngine(None, SimpleNamespace(repair_memory=repair))
    result = {"usage": [{"phase": "decision", "input_tokens": 123}]}
    reply = asyncio.run(
        engine._correct_memory({"message_id": "input"}, context, candidate, result, now())
    )
    assert len(calls) == 1 and (context, candidate) == originals
    for key in candidate.keys() - {"working_memory", "memory_dispositions"}:
        assert reply[key] == candidate[key]
    assert reply["working_memory"] == correction["working_memory"]
    assert result["memory_repair"]["candidate"] == candidate
    assert result["memory_repair"]["response"] == correction
    assert result["usage"] == [{"phase": "decision", "input_tokens": 123}, usage]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_dependency",
        "new_resolution",
        "changed_thesis",
        "decision_edit",
        "oversized_again",
        "unknown_evidence",
        "timeout",
    ],
)
def test_bad_correction_is_bounded_and_preserves_original_attempt(fault):
    context, candidate, correction = example()
    if fault == "missing_dependency":
        correction["candidate_dispositions"].pop()
    if fault == "new_resolution":
        correction["memory_dispositions"][1].update(to=None, disposition="resolved")
    if fault == "changed_thesis":
        correction["working_memory"]["position_theses"] = [
            {"instrument": "EUR/USD", "thesis": "Invented", "as_of": "2026-10-01T00:00:00Z"}
        ]
    if fault == "decision_edit":
        correction["action"] = "enter"
    if fault == "oversized_again":
        correction["working_memory"]["views"] = ["x" * 301]
    if fault == "unknown_evidence":
        correction["candidate_dispositions"][0]["evidence_ids"] = ["not-supplied"]
    calls = []

    async def repair(*args):
        calls.append(args)
        if fault == "timeout":
            raise ModelFailure(
                {
                    "phase": "memory_repair",
                    "attempted": True,
                    "usage_unknown": True,
                    "failure_code": "model_error",
                }
            )
        return correction, {"phase": "memory_repair", "input_tokens": 200}

    result = {"usage": [{"phase": "decision", "input_tokens": 123}]}
    with pytest.raises(ShadowError, match="working_memory_repair_failed"):
        asyncio.run(
            ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
                {"message_id": "input"}, context, candidate, result, now()
            )
        )
    assert len(calls) == 1 and result["memory_repair"]["candidate"] == candidate
    assert result["usage"][0]["input_tokens"] == 123
    assert result["memory_repair"]["error"]


def test_actual_request_compiler_uses_a_tool_free_bounded_correction_schema():
    context, candidate, correction = example()
    context.update(
        identity="Standing identity",
        playbook={"version": "v1", "text": "Standing rules"},
        recent_journal=[{"untrusted": "MUST_NOT_COPY_ARCHIVE"}],
    )
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(message=message, options=options)
            return SimpleNamespace(
                text=encoded(correction), usage_details={"input_token_count": 456}
            )

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    reply, trace = asyncio.run(
        model.repair_memory({"message_id": "input"}, context, candidate, None, now())
    )
    assert reply == correction and captured["tools"] == []
    assert "MUST_NOT_COPY_ARCHIVE" not in captured["message"]
    assert (
        "candidate_dispositions" in captured["message"] and "CORRECTION" in captured["instructions"]
    )
    assert trace["phase"] == "memory_repair" and trace["prompt_bytes"] < 28000
    assert trace["input_tokens"] == 456 and trace["max_output_tokens"] == 4096
