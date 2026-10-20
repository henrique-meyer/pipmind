import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import ContractError, Contracts
from pipmind.shadow.common import ShadowError, encoded, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.maintenance import transition, validate_working, working_sizes
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
    assert captured["options"]["reasoning"] == {"effort": "low"}
    assert trace["reasoning_effort"] == "low"
    prompt = json.loads(captured["message"])
    assert prompt["correction"]["candidate_sizes"] == working_sizes(candidate["working_memory"])
    assert "300 UTF-8 bytes independently" in captured["instructions"]
    assert "up to eight entries" in captured["instructions"]


def test_repair_schema_rejects_oversized_merge_even_when_total_is_under_bound():
    _, _, correction = example()
    correction["working_memory"]["waiting_for"] = ["x" * 311]
    assert working_sizes(correction["working_memory"])["total_bytes"] < 3200
    with pytest.raises(ContractError, match="schema:memory-repair"):
        Contracts().validate("memory-repair", correction)


def test_repair_keeps_utf8_byte_guard_beyond_schema_character_limit():
    _, _, correction = example()
    # JSON Schema counts characters. Runtime must still enforce UTF-8 bytes.
    correction["working_memory"]["waiting_for"] = ["\u00e9" * 151]
    Contracts().validate("memory-repair", correction)
    assert working_sizes(correction["working_memory"])["entry_bytes"]["w0"] == 302
    with pytest.raises(ShadowError, match="working_memory_bound"):
        validate_working(correction["working_memory"], now())


def test_correction_preserves_long_original_resolution_verbatim():
    context, candidate, correction = example()
    context["working_memory"]["waiting_for"].append("Need release vintage")
    candidate["working_memory"]["waiting_for"] = ["Need official text"]
    resolved = {
        "from": "w1",
        "to": None,
        "disposition": "resolved",
        "reason": "The supplied official release confirms the vintage and publication time; "
        "the earlier uncertainty is resolved by that evidence.",
        "evidence_ids": ["input"],
    }
    candidate["memory_dispositions"] = [resolved]
    correction["working_memory"]["waiting_for"] = ["Need official text"]
    correction["memory_dispositions"] = [correction["memory_dispositions"][0], resolved]
    correction["candidate_dispositions"] = correction["candidate_dispositions"][:-1]
    correction["candidate_dispositions"][-1]["disposition"] = "retained"

    async def repair(*args):
        return correction, {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    reply = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": "input"}, context, candidate, result, now()
        )
    )
    assert reply["memory_dispositions"][-1] == resolved


@pytest.mark.parametrize("label", ["updated", "merged"])
@pytest.mark.parametrize("moved", [False, True])
def test_exact_carry_forward_mapping_is_canonicalized_without_changing_raw_reply(label, moved):
    context, candidate, correction = example()
    candidate["working_memory"]["waiting_for"] = ["Need official text"]
    correction["working_memory"]["waiting_for"] = ["Need official text"]
    correction["candidate_dispositions"] = correction["candidate_dispositions"][:-1]
    target = "w1" if moved else "w0"
    if moved:
        correction["working_memory"]["waiting_for"].insert(0, "Need dated confirmation")
    for field in ("memory_dispositions", "candidate_dispositions"):
        row = next(row for row in correction[field] if row["from"] == "w0")
        row.update(to=target, disposition=label)
    raw = deepcopy(correction)
    # The validator itself remains strict about identity mappings.
    with pytest.raises(ShadowError, match="working_memory_dispositions"):
        transition(
            context["working_memory"],
            correction["working_memory"],
            correction["memory_dispositions"],
            now(),
        )

    async def repair(*args):
        return correction, {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    reply = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": "input"}, context, candidate, result, now()
        )
    )
    assert correction == raw and result["memory_repair"]["response"] == raw
    assert reply["working_memory"] == raw["working_memory"]
    assert reply["memory_dispositions"][-1]["disposition"] == "retained"
    assert len(result["memory_repair"]["mapping_normalizations"]) == 2
    transition(
        context["working_memory"], reply["working_memory"], reply["memory_dispositions"], now()
    )


@pytest.mark.parametrize("fault", ["changed_text", "cross_kind", "duplicate", "resolution"])
def test_identity_mapping_normalization_never_repairs_semantic_or_ambiguous_faults(fault):
    context, candidate, correction = example()
    candidate["working_memory"]["waiting_for"] = ["Need official text"]
    correction["working_memory"]["waiting_for"] = ["Need official text"]
    correction["candidate_dispositions"] = correction["candidate_dispositions"][:-1]
    row = correction["memory_dispositions"][1]
    row["disposition"] = "updated"
    if fault == "changed_text":
        correction["working_memory"]["waiting_for"] = ["Need official text "]
        row["disposition"] = "retained"
    elif fault == "cross_kind":
        correction["working_memory"]["views"].append("Need official text")
        row["to"] = "v1"
    elif fault == "duplicate":
        correction["memory_dispositions"].append(deepcopy(row))
    else:
        row.update(to=None, disposition="resolved")

    async def repair(*args):
        return correction, {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    with pytest.raises(ShadowError, match="working_memory_repair_failed"):
        asyncio.run(
            ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
                {"message_id": "input"}, context, candidate, result, now()
            )
        )
    assert result["memory_repair"]["response"] == correction
