import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import ContractError, Contracts
from pipmind.shadow.common import ShadowError, encoded, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.maintenance import (
    entries,
    expand_repair_patch,
    repair_budget,
    repair_scope,
    spacing_repair,
    transition,
    validate_working,
    working_sizes,
)
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


def patch_example():
    context, candidate, correction = example()
    candidate["working_memory"]["views"] = ["EUR uncertainty " + "." * 300]
    candidate["memory_dispositions"] = [correction["memory_dispositions"][0]]
    patch = {
        "replacements": [
            {
                "from": "v0",
                "text": "EUR uncertainty remains",
                "reason": "Shorter wording; same uncertainty",
                "evidence_ids": ["input"],
            }
        ]
    }
    return context, candidate, patch


def total_example():
    before = {
        "views": [f"FX {i}: " + "\\" * 240 for i in range(8)],
        "waiting_for": [f"Await verified prerequisite {i} " + "b" * 150 for i in range(5)],
        "position_theses": [],
    }
    context = {
        "working_memory": before,
        "memory_policy": {
            "version": "bounded-working-v1",
            "checkpoint": "checkpoint",
            "origin_checkpoint": "origin",
        },
    }
    candidate = {
        "action": "hold",
        "reasoning": "Keep every prerequisite",
        "evidence_ids": ["input"],
        "tool_calls": [],
        "memory_dispositions": [],
        "working_memory": deepcopy(before),
    }
    candidate["working_memory"]["waiting_for"].append("New dated prerequisite: " + "c" * 190)
    assert working_sizes(before)["total_bytes"] <= 5000
    assert working_sizes(candidate["working_memory"])["total_bytes"] > 5000
    assert max(working_sizes(candidate["working_memory"])["entry_bytes"].values()) <= 300
    return context, candidate


@pytest.mark.parametrize("reorder", [False, True])
def test_total_budget_patch_composes_literal_before_entries_and_preserves_unedited_state(reorder):
    context, candidate = total_example()
    if reorder:
        views = candidate["working_memory"]["views"]
        views[:] = views[1:] + views[:1]
    original = deepcopy(candidate)
    budget = repair_budget(candidate["working_memory"])
    assert budget["planned_total_bytes"] <= 4600
    selected = set(budget["editable_keys"])
    patch = {
        "replacements": [
            {
                "from": key,
                "text": f"{key}: distinct synthetic uncertainty retained",
                "reason": "Same prerequisite in compact wording",
                "evidence_ids": ["input"],
            }
            for key in budget["editable_keys"]
        ]
    }

    async def repair(*args):
        return patch, {"phase": "memory_repair", "synthetic": True}

    trace = {"usage": []}
    out = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": "input"}, context, candidate, trace, now()
        )
    )
    assert candidate == original
    assert (
        out["working_memory"]["position_theses"] == candidate["working_memory"]["position_theses"]
    )
    for key, text in entries(candidate["working_memory"]).items():
        if key not in selected:
            assert entries(out["working_memory"])[key] == text
    rows = {row["from"]: row for row in out["memory_dispositions"]}
    for key, text in entries(context["working_memory"]).items():
        target = next(
            k
            for k, v in entries(candidate["working_memory"]).items()
            if k[0] == key[0] and v == text
        )
        if target in selected:
            assert rows[key]["to"] == target and rows[key]["disposition"] == "updated"
    transition(context["working_memory"], out["working_memory"], out["memory_dispositions"], now())
    assert trace["memory_repair"]["patch_budget"] == budget
    assert trace["memory_repair"]["response"] == patch
    assert {k: out[k] for k in out if k not in {"working_memory", "memory_dispositions"}} == {
        k: candidate[k] for k in candidate if k not in {"working_memory", "memory_dispositions"}
    }


def test_budget_accounts_for_utf8_json_escapes_and_exact_fixed_theses():
    _, candidate = total_example()
    w = candidate["working_memory"]
    w["views"][0] = 'é\\"' * 90
    w["position_theses"] = [
        {
            "instrument": "EUR/USD",
            "thesis": "Exact confirmed thesis",
            "as_of": "2026-01-01T00:00:00Z",
        }
    ]
    budget = repair_budget(w)
    assert budget and budget["planned_total_bytes"] <= 4600
    expected = deepcopy(w)
    for key, cap in budget["target_json_text_bytes"].items():
        field = "views" if key[0] == "v" else "waiting_for"
        expected[field][int(key[1:])] = "x" * cap
    assert working_sizes(expected)["total_bytes"] == budget["planned_total_bytes"]
    assert expected["position_theses"] == w["position_theses"]
    for key, cap in budget["max_json_text_bytes"].items():
        field = "views" if key[0] == "v" else "waiting_for"
        expected[field][int(key[1:])] = "x" * cap
    assert working_sizes(expected)["total_bytes"] <= 4900


def test_total_patch_preserves_existing_resolution_and_cannot_invent_missing_provenance():
    context, candidate = total_example()
    text = context["working_memory"]["views"][0]
    candidate["working_memory"]["views"][0] = text.replace("FX 0", "FX 9")
    budget = repair_budget(candidate["working_memory"])
    patch = {
        "replacements": [
            {
                "from": key,
                "text": key + " compact uncertainty",
                "reason": "Same distinct prerequisite retained",
                "evidence_ids": ["input"],
            }
            for key in budget["editable_keys"]
        ]
    }
    expanded = expand_repair_patch(candidate, patch, "input", before=context["working_memory"])
    with pytest.raises(ShadowError, match="working_memory_dispositions"):
        transition(
            context["working_memory"],
            expanded["working_memory"],
            expanded["memory_dispositions"],
            now(),
        )
    # A genuine prior resolution is never generated or rewritten by composition.
    candidate["working_memory"]["views"][0] = text
    candidate["memory_dispositions"] = [
        {
            "from": "w4",
            "to": None,
            "disposition": "resolved",
            "reason": "Prior evidence-backed resolution " + "r" * 300,
            "evidence_ids": ["input"],
        }
    ]
    candidate["working_memory"]["waiting_for"].pop(4)
    candidate["working_memory"]["waiting_for"].append("Another distinct prerequisite " + "d" * 190)
    expanded = expand_repair_patch(
        candidate,
        {
            "replacements": [
                {
                    "from": key,
                    "text": key + " compact uncertainty",
                    "reason": "Same distinct prerequisite retained",
                    "evidence_ids": ["input"],
                }
                for key in repair_scope(candidate["working_memory"])
            ]
        },
        "input",
        before=context["working_memory"],
    )
    assert expanded["memory_dispositions"][0] == candidate["memory_dispositions"][0]


@pytest.mark.parametrize("fault", ["count", "duplicate"])
def test_total_budget_never_patches_invalid_topology(fault):
    _, candidate = total_example()
    w = candidate["working_memory"]
    w["views"].append("Distinct ninth view" if fault == "count" else w["views"][0])
    assert repair_budget(w) is None and repair_scope(w) == []


def test_scoped_patch_copies_unedited_state_and_original_mappings_exactly():
    context, candidate, patch = patch_example()
    originals = deepcopy((context, candidate, patch))

    async def repair(*args):
        return patch, {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    out = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": "input"}, context, candidate, result, now()
        )
    )
    assert (context, candidate, patch) == originals
    assert out["working_memory"]["waiting_for"] == candidate["working_memory"]["waiting_for"]
    assert out["memory_dispositions"] == candidate["memory_dispositions"]
    assert result["memory_repair"]["response"] == patch
    assert result["memory_repair"]["expanded_response"]["working_memory"] == out["working_memory"]
    assert repair_scope(candidate["working_memory"]) == ["v0"]


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "extra", "utf8", "evidence", "before_mapping"]
)
def test_scoped_patch_rejects_scope_byte_evidence_and_transition_faults(fault):
    context, candidate, patch = patch_example()
    row = patch["replacements"][0]
    if fault == "missing":
        patch["replacements"] = []
    elif fault == "duplicate":
        patch["replacements"].append(deepcopy(row))
    elif fault == "extra":
        patch["replacements"].append({**row, "from": "w0"})
    elif fault == "utf8":
        row["text"] = "é" * 151
    elif fault == "evidence":
        row["evidence_ids"] = ["invented"]
    else:
        candidate["memory_dispositions"] = []

    async def repair(*args):
        return patch, {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    with pytest.raises(ShadowError, match="working_memory_repair_failed"):
        asyncio.run(
            ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
                {"message_id": "input"}, context, candidate, result, now()
            )
        )
    assert result["memory_repair"]["response"] == patch


def test_actual_scoped_request_uses_patch_schema_and_single_low_effort_tool_free_call():
    context, candidate, patch = patch_example()
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(message=json.loads(message), options=options)
            return SimpleNamespace(text=encoded(patch), usage_details={"output_token_count": 80})

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    out, trace = asyncio.run(
        model.repair_memory({"message_id": "input"}, context, candidate, None, now())
    )
    assert out == patch and captured["tools"] == []
    assert captured["message"]["schema"]["$id"].endswith(":memory-repair-patch")
    assert captured["message"]["correction"]["editable_keys"] == ["v0"]
    requested = captured["message"]["schema"]["properties"]["replacements"]
    assert requested["minItems"] == requested["maxItems"] == 1
    assert requested["items"]["properties"]["from"]["enum"] == ["v0"]
    assert requested["items"]["properties"]["text"]["maxLength"] == 280
    assert captured["message"]["correction"]["patch_budget"]["target_json_text_bytes"] == {
        "v0": 220
    }
    assert (
        Contracts().schemas["memory-repair-patch"]["properties"]["replacements"]["items"][
            "properties"
        ]["text"]["maxLength"]
        == 1000
    )
    assert captured["options"]["reasoning"] == {"effort": "low"}
    assert trace["output_tokens"] == 80 and trace["max_output_tokens"] == 4096


@pytest.mark.parametrize("full_views", [False, True])
def test_punctuation_spacing_repairs_small_overflow_without_rpc_or_lexical_loss(full_views):
    import re

    context, candidate, _ = patch_example()
    text = "EUR: " + "x" * 274 + "; risk; wait; uncertainty"
    assert len(text.encode()) > 300
    candidate["working_memory"]["views"] = [text]
    if full_views:
        carry = [f"Distinct unchanged uncertainty {i}" for i in range(7)]
        context["working_memory"]["views"].extend(carry)
        candidate["working_memory"]["views"].extend(carry)

    async def forbidden(*args):
        pytest.fail("A representation-only correction must not call a model")

    result = {"usage": []}
    out = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=forbidden))._correct_memory(
            {"message_id": "input"}, context, candidate, result, now()
        )
    )
    compact = out["working_memory"]["views"][0]
    assert len(compact.encode()) <= 300
    assert re.findall(r"\w+|[^\w\s]", text) == re.findall(r"\w+|[^\w\s]", compact)
    assert out["working_memory"]["waiting_for"] == candidate["working_memory"]["waiting_for"]
    assert result["memory_repair"]["candidate"] == candidate
    assert result["memory_repair"]["method"] == "punctuation_spacing"
    assert result["usage"] == [
        {
            "phase": "memory_repair",
            "attempted": False,
            "input_tokens": 0,
            "output_tokens": 0,
            "method": "punctuation_spacing",
        }
    ]


def test_total_correction_requests_full_consolidation_with_both_mapping_sets():
    context, candidate = total_example()
    _, _, correction = example()
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(message=json.loads(message), options=options)
            return SimpleNamespace(text=encoded(correction), usage_details={})

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    asyncio.run(model.repair_memory({"message_id": "input"}, context, candidate, None, now()))
    prompt = captured["message"]
    assert prompt["schema"]["$id"] == "urn:pipmind:schema:v1:memory-repair"
    assert set(prompt["schema"]["properties"]) == {
        "working_memory",
        "memory_dispositions",
        "candidate_dispositions",
    }
    assert "editable_keys" not in prompt["correction"]
    assert "patch_budget" not in prompt["correction"]
    assert prompt["correction"]["candidate_sizes"] == working_sizes(candidate["working_memory"])
    assert "schema_definitions" in prompt
    for field in ("views", "waiting_for"):
        assert (
            prompt["schema"]["properties"]["working_memory"]["allOf"][1]["properties"][field][
                "items"
            ]["maxLength"]
            == 200
        )
        assert (
            prompt["schema_definitions"]["working-memory"]["properties"][field]["items"][
                "maxLength"
            ]
            == 200
        )
        assert (
            model.contracts.schemas["memory-repair"]["properties"]["working_memory"]["allOf"][1][
                "properties"
            ][field]["items"]["maxLength"]
            == 300
        )
    assert "factoring repeated" in captured["instructions"]
    assert "Reconsideration means" not in captured["instructions"]
    assert "monetary-policy versus broader-policy" in captured["instructions"]
    assert captured["tools"] == [] and captured["options"]["max_tokens"] == 4096
    assert model.timeout == 90
    assert captured["options"]["reasoning"] == {"effort": "low"}
    assert "never clip text or omit meaning" in captured["instructions"]
    assert (
        model.contracts.schemas["memory-repair-patch"]["properties"]["replacements"]["maxItems"]
        == 16
    )


@pytest.mark.parametrize("field", ["views", "waiting_for"])
@pytest.mark.parametrize("count", [7, 8])
@pytest.mark.parametrize("unicode_text", [False, True])
def test_overlong_entry_stays_scoped_when_total_and_topology_fit(field, count, unicode_text):
    context, candidate, _ = example()
    candidate["working_memory"].update(
        views=[f"Distinct uncertainty {i}" for i in range(8)],
        waiting_for=[f"Unresolved prerequisite {i}" for i in range(8)],
    )
    candidate["working_memory"][field] = candidate["working_memory"][field][:count]
    candidate["working_memory"][field][0] = "é" * 151 if unicode_text else "x" * 301
    assert working_sizes(candidate["working_memory"])["total_bytes"] < 5000
    assert len(candidate["working_memory"][field][0].encode()) > 300
    patch = {
        "replacements": [
            {
                "from": "v0" if field == "views" else "w0",
                "text": "Synthetic same-kind uncertainty remains",
                "reason": "Synthetic concise wording preserves uncertainty",
                "evidence_ids": ["input"],
            }
        ]
    }
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(message=json.loads(message), options=options)
            return SimpleNamespace(text=encoded(patch), usage_details={})

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    _, trace = asyncio.run(
        model.repair_memory({"message_id": "input"}, context, candidate, None, now())
    )
    prompt = captured["message"]
    expected = "memory-repair-patch"
    assert prompt["schema"]["$id"] == "urn:pipmind:schema:v1:" + expected
    assert "editable_keys" in prompt["correction"]
    assert "patch_budget" in prompt["correction"]
    assert captured["tools"] == []
    assert captured["options"]["max_tokens"] == 4096 and model.timeout == 90
    assert captured["options"]["reasoning"] == {"effort": "low"}
    assert trace["prompt_version"] == "shadow-repair-v20"


def test_repair_generation_headroom_does_not_mutate_later_normal_requests_or_hard_limits():
    context, candidate = total_example()
    _, _, correction = example()
    prompts = []

    class Agent:
        async def run(self, message, *, options):
            prompt = json.loads(message)
            prompts.append(prompt)
            if prompt["phase"] == "memory_repair":
                response = correction
            elif prompt["phase"] == "triage":
                response = {"wake": True, "summary": "Synthetic relevant input"}
            else:
                response = {
                    "action": "hold",
                    "reasoning": "Synthetic uncertainty remains",
                    "evidence_ids": [],
                    "tool_calls": [],
                    "working_memory": context["working_memory"],
                }
            return SimpleNamespace(text=encoded(response), usage_details={})

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test",
        "t",
        "d",
        enabled=True,
        factory=lambda **kwargs: Agent(),
    )
    normative = deepcopy(model.contracts.schemas)
    asyncio.run(model.repair_memory({"message_id": "input"}, context, candidate, None, now()))
    asyncio.run(
        model.decide(
            {"message_id": "input"}, context, {"wake": True, "summary": "Synthetic"}, None, now()
        )
    )
    asyncio.run(model.triage({"message_id": "input"}, context, None, now()))
    assert model.contracts.schemas == normative == Contracts().schemas
    assert (
        prompts[0]["schema_definitions"]["working-memory"]["properties"]["views"]["items"][
            "maxLength"
        ]
        == 200
    )
    normal_items = prompts[1]["schema_definitions"]["working-memory"]["properties"]["views"][
        "items"
    ]
    assert all("maxLength" not in option for option in normal_items["anyOf"])
    assert "schema_definitions" not in prompts[2]
    assert prompts[2]["schema"] == normative["triage-result"]
    assert (
        prompts[1]["schema"]["properties"]["working_memory"]["$ref"]
        == "urn:pipmind:wire:working-refs-v1"
    )

    # Generation headroom is advisory; the established local bound remains300 UTF-8 bytes.
    for text in ("x" * 201, "x" * 300):
        state = {"views": [text], "waiting_for": [], "position_theses": []}
        validate_working(state, now())
    with pytest.raises(ShadowError, match="working_memory_bound"):
        validate_working({"views": ["é" * 151], "waiting_for": [], "position_theses": []}, now())


@pytest.mark.parametrize("allocation", [120, 300])
def test_scoped_generation_headroom_never_relaxes_supplied_allocation(allocation):
    context, candidate, patch = patch_example()
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(json.loads(message))
            return SimpleNamespace(text=encoded(patch), usage_details={})

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test",
        "t",
        "d",
        enabled=True,
        factory=lambda **kwargs: Agent(),
    )
    normative = deepcopy(model.contracts.schemas)
    asyncio.run(
        model._call(
            "memory_repair",
            {"message_id": "input"},
            context,
            None,
            now(),
            repair={
                "candidate": candidate,
                "editable_keys": ["v0"],
                "patch_budget": {"max_json_text_bytes": {"v0": allocation}},
            },
        )
    )
    text = captured["schema"]["properties"]["replacements"]["items"]["properties"]["text"]
    assert text["maxLength"] == min(280, allocation)
    assert model.contracts.schemas == normative


@pytest.mark.parametrize("full_field", ["views", "waiting_for"])
def test_full_list_at_exact_utf8_limit_does_not_force_other_kind_to_consolidate(full_field):
    context, candidate, _ = example()
    other_field = "waiting_for" if full_field == "views" else "views"
    candidate["working_memory"][full_field] = ["é" * 150] + [
        f"Unchanged synthetic entry {i}" for i in range(7)
    ]
    candidate["working_memory"][other_field] = ["é" * 151]
    key = "w0" if other_field == "waiting_for" else "v0"
    patch = {
        "replacements": [
            {
                "from": key,
                "text": "Synthetic uncertainty retained",
                "reason": "Concise synthetic condition retained",
                "evidence_ids": ["input"],
            }
        ]
    }
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(json.loads(message))
            return SimpleNamespace(text=encoded(patch), usage_details={})

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test",
        "t",
        "d",
        enabled=True,
        factory=lambda **kwargs: Agent(),
    )
    asyncio.run(model.repair_memory({"message_id": "input"}, context, candidate, None, now()))
    assert captured["schema"]["$id"] == "urn:pipmind:schema:v1:memory-repair-patch"
    assert captured["correction"]["editable_keys"] == [key]


@pytest.mark.parametrize("recoverable", [False, True])
def test_total_patch_spacing_preserves_lexical_content_and_refuses_residual_overflow(recoverable):
    import re

    context, candidate = total_example()
    if recoverable:
        views = [f"FX {i}: " + "\\" * 200 + "; word" * 12 for i in range(8)]
        views[0] = '"' + views[0][1:]  # Quoted text must remain untouched.
        context["working_memory"]["views"] = deepcopy(views)
        candidate["working_memory"]["views"] = deepcopy(views)
    original = deepcopy(candidate)
    patch = {
        "replacements": [
            {
                "from": key,
                "text": entries(candidate["working_memory"])[key],
                "reason": "Exact candidate wording retained",
                "evidence_ids": ["input"],
            }
            for key in repair_scope(candidate["working_memory"])
        ]
    }
    calls = []

    async def repair(*args):
        calls.append(1)
        return deepcopy(patch), {"phase": "memory_repair", "synthetic": True}

    trace = {"usage": []}
    operation = ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
        {"message_id": "input"}, context, candidate, trace, now()
    )
    if not recoverable:
        with pytest.raises(ShadowError, match="working_memory_repair_failed"):
            asyncio.run(operation)
    else:
        out = asyncio.run(operation)
        assert out["working_memory"]["views"][0] == candidate["working_memory"]["views"][0]
        assert working_sizes(out["working_memory"])["total_bytes"] <= 5000
        assert trace["memory_repair"]["method"] == "punctuation_spacing"
        for key, text in entries(candidate["working_memory"]).items():
            assert re.findall(r"\w+|[^\w\s]", text) == re.findall(
                r"\w+|[^\w\s]", entries(out["working_memory"])[key]
            )
    assert calls == ([] if recoverable else [1]) and candidate == original
    if not recoverable:
        assert trace["memory_repair"]["response"] == patch


@pytest.mark.parametrize("fault", [None, "quoted", "missing_provenance"])
def test_format_only_carry_forward_is_mapped_but_never_invents_provenance(fault):
    import re

    context, candidate = total_example()
    old = candidate["working_memory"]["waiting_for"][0]
    text = "Await: A; B; C" + "x" * (len(old) - len("Await: A; B; C"))
    if fault == "quoted":
        text = '"' + text[1:]
    context["working_memory"]["waiting_for"][0] = text
    candidate["working_memory"]["waiting_for"][0] = (
        text.replace("Await", "Other") if fault == "missing_provenance" else text
    )
    budget = repair_budget(candidate["working_memory"])
    assert "w0" not in budget["editable_keys"]
    after = deepcopy(candidate["working_memory"])
    for key in budget["editable_keys"]:
        field = "views" if key[0] == "v" else "waiting_for"
        after[field][int(key[1:])] = '"' + key + ' "'
    remaining = 5003 - len(encoded(after).encode())
    replacements = []
    for key in budget["editable_keys"]:
        amount = min(2 * (300 - len(key) - 3), remaining)
        remaining -= amount
        replacements.append(
            {
                "from": key,
                "text": '"' + key + " " + "\\" * (amount // 2) + "x" * (amount % 2) + '"',
                "reason": "Synthetic scoped wording proposal",
                "evidence_ids": ["input"],
            }
        )
    assert remaining == 0
    patch = {"replacements": replacements}
    original = deepcopy((context, candidate, patch))

    async def repair(*args):
        return deepcopy(patch), {"phase": "memory_repair", "synthetic": True}

    trace = {"usage": []}
    operation = ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
        {"message_id": "input"}, context, candidate, trace, now()
    )
    if fault:
        with pytest.raises(ShadowError, match="working_memory_repair_failed"):
            asyncio.run(operation)
    else:
        out = asyncio.run(operation)
        assert working_sizes(out["working_memory"])["total_bytes"] == 5000
        audit = trace["memory_repair"]["format_only_normalizations"]
        assert [v["key"] for v in audit] == ["w0"]
        applied = out["working_memory"]["waiting_for"][0]
        assert re.findall(r"\w+|[^\w\s]", applied) == re.findall(r"\w+|[^\w\s]", text)
        assert any(row["from"] == "w0" and row["to"] == "w0" for row in out["memory_dispositions"])
    assert (context, candidate, patch) == original
    assert trace["memory_repair"]["response"] == patch


@pytest.mark.parametrize(
    "text,expected",
    [
        ("EUR, USD; wait: source", "EUR,USD;wait:source"),
        ("1, 234; 10: 20", "1, 234; 10: 20"),
        ('"quoted, text"', '"quoted, text"'),
        ("`code, text`", "`code, text`"),
        (
            "Links: https://a.example/x, https://b.example/y",
            "Links: https://a.example/x, https://b.example/y",
        ),
        ("\u201cEUR, USD\u201d", "\u201cEUR, USD\u201d"),
    ],
)
def test_optional_punctuation_spacing_preserves_numbers_quotes_and_code(text, expected):
    from pipmind.shadow.maintenance import punctuation_spacing

    assert punctuation_spacing(text) == expected


@pytest.mark.parametrize("fault", ["quote", "code", "numeric", "mapping", "still_large"])
def test_spacing_compaction_cannot_bypass_literal_or_transition_constraints(fault):
    context, candidate, _ = patch_example()
    text = "EUR: " + "x" * 274 + "; risk; wait; uncertainty"
    if fault == "quote":
        text += "'"
    elif fault == "code":
        text += "`"
    elif fault == "numeric":
        text = "x" * 290 + "; 10: 20; 30: 40"
    elif fault == "mapping":
        candidate["memory_dispositions"] = []
    else:
        text += "x" * 30
    candidate["working_memory"]["views"] = [text]
    assert spacing_repair(context["working_memory"], candidate, "input", now()) is None


def test_model_patch_spacing_keeps_raw_proposal_and_enforces_final_byte_limit():
    context, candidate, patch = patch_example()
    raw = "EUR: " + "x" * 274 + "; risk; wait; uncertainty"
    patch["replacements"][0]["text"] = raw

    async def repair(*args):
        return patch, {"phase": "memory_repair", "input_tokens": 200}

    trace = {"usage": []}
    out = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": "input"}, context, candidate, trace, now()
        )
    )
    assert trace["memory_repair"]["response"] == patch
    assert trace["memory_repair"]["patch_spacing_normalizations"] == [
        {"key": "v0", "before_bytes": 304, "after_bytes": 300}
    ]
    assert len(out["working_memory"]["views"][0].encode()) == 300
