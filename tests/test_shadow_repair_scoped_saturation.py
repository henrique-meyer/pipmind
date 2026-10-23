"""Offline topology/provenance regressions; synthetic text is not a semantic grader."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.shadow.common import ShadowError, encoded, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.maintenance import entries, repair_budget, validate_working, working_sizes
from pipmind.shadow.model import FoundryModel

COMPACT = (
    "Synthetic Nov04 2031 A-bank2.85%,prior2.65%,+20bp if comparable;"
    "pub09:42Z new confirmed/min,old unverified;vintage/basis/forecast/stance pending. "
    "B-bank Apr02 cut1.15%;holds May06/Jun17/Aug05;"
    "full text/prior/effective dates pre-interpretation."
)
EXPANDED = COMPACT.replace(
    "pub09:42Z new confirmed/min,old unverified",
    "publication09:42Z new record confirmed at minute precision,old record unverified",
).replace(
    "full text/prior/effective dates pre-interpretation",
    "full official text,comparable prior and applicable effective dates "
    "required before interpretation",
)


def saturated_case(field="views", count=8, *, multibyte=False):
    before = {
        "views": [
            f"Synthetic view {i}: dated source and policy applicability unresolved"
            for i in range(8)
        ],
        "waiting_for": [
            f"Synthetic condition {i}: EACH source full text and vintage required" for i in range(8)
        ],
        "position_theses": [
            {
                "instrument": "EUR/USD",
                "thesis": "Synthetic exact position thesis: no inferred resolution",
                "as_of": "2026-01-03T00:00:00Z",
            }
        ],
    }
    before[field] = before[field][:count]
    before[field][0] = COMPACT
    candidate = {
        "action": "hold",
        "reasoning": "Synthetic decision and its evidence must remain exact",
        "evidence_ids": ["synthetic-input"],
        "tool_calls": [],
        "memory_dispositions": [
            {
                "from": "v0" if field == "views" else "w0",
                "to": "v0" if field == "views" else "w0",
                "disposition": "updated",
                "reason": "Synthetic dated source wording expanded",
                "evidence_ids": ["synthetic-input"],
            }
        ],
        "working_memory": deepcopy(before),
    }
    candidate["working_memory"][field][0] = "é" * 151 if multibyte else EXPANDED
    context = {
        "working_memory": before,
        "memory_policy": {
            "version": "bounded-working-v1",
            "checkpoint": "synthetic-checkpoint",
            "origin_checkpoint": "synthetic-origin",
        },
    }
    key = "v0" if field == "views" else "w0"
    patch = {
        "replacements": [
            {
                "from": key,
                "text": COMPACT,
                "reason": "Synthetic abbreviated wording proposal",
                "evidence_ids": ["synthetic-input"],
            }
        ]
    }
    assert len(COMPACT.encode()) <= 280 < 300 < len(EXPANDED.encode())
    assert working_sizes(candidate["working_memory"])["total_bytes"] < 5000
    return context, candidate, patch


def capture_request(context, candidate, patch):
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(prompt=json.loads(message), options=options)
            # Schema-selection tests stop at compilation; this synthetic full
            # response is not exercised as a valid semantic transition.
            response = (
                patch
                if captured["prompt"]["schema"]["$id"].endswith(":memory-repair-patch")
                else {
                    "working_memory": context["working_memory"],
                    "memory_dispositions": [],
                    "candidate_dispositions": [],
                }
            )
            return SimpleNamespace(text=encoded(response), usage_details={})

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    asyncio.run(
        model.repair_memory({"message_id": "synthetic-input"}, context, candidate, None, now())
    )
    return model, captured


@pytest.mark.parametrize("field", ["views", "waiting_for"])
@pytest.mark.parametrize("count", [7, 8])
@pytest.mark.parametrize("multibyte", [False, True])
def test_saturation_alone_keeps_repair_scoped(field, count, multibyte):
    context, candidate, patch = saturated_case(field, count, multibyte=multibyte)
    model, captured = capture_request(context, candidate, patch)
    prompt = captured["prompt"]
    key = "v0" if field == "views" else "w0"
    assert prompt["schema"]["$id"] == "urn:pipmind:schema:v1:memory-repair-patch"
    assert prompt["correction"]["editable_keys"] == [key]
    budget = prompt["correction"]["patch_budget"]
    requested = prompt["schema"]["properties"]["replacements"]["items"]["properties"]["text"]
    assert requested["maxLength"] == min(280, budget["max_json_text_bytes"][key])
    assert captured["tools"] == []
    assert captured["options"]["max_tokens"] == 4096 and model.timeout == 90


@pytest.mark.parametrize("field", ["views", "waiting_for"])
def test_scoped_expansion_preserves_every_unselected_entry_thesis_and_decision(field):
    context, candidate, patch = saturated_case(field)
    original = deepcopy((context, candidate, patch))
    calls = []

    async def repair(*args):
        calls.append(args)
        return deepcopy(patch), {"phase": "memory_repair", "synthetic": True}

    trace = {"usage": []}
    reply = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": "synthetic-input"}, context, candidate, trace, now()
        )
    )
    assert len(calls) == 1 and (context, candidate, patch) == original
    selected = set(repair_budget(candidate["working_memory"])["editable_keys"])
    for key, text in entries(candidate["working_memory"]).items():
        if key not in selected:
            assert entries(reply["working_memory"])[key] == text
    assert (
        reply["working_memory"]["position_theses"] == candidate["working_memory"]["position_theses"]
    )
    assert reply["working_memory"][field][0] == COMPACT
    for decision_field in candidate.keys() - {"working_memory", "memory_dispositions"}:
        assert reply[decision_field] == candidate[decision_field]
    assert trace["memory_repair"]["response"] == patch
    assert len(trace["memory_repair"]["expanded_response"]["candidate_dispositions"]) == 16
    validate_working(reply["working_memory"], now())


@pytest.mark.parametrize("fault", ["unselected", "omitted", "duplicate", "oversized", "utf8"])
def test_scoped_expansion_refuses_scope_and_byte_limit_violations(fault):
    context, candidate, patch = saturated_case()
    if fault == "unselected":
        row = deepcopy(patch["replacements"][0])
        row.update({"from": "v1", "text": "Unrelated synthetic overwrite"})
        patch["replacements"].append(row)
    elif fault == "omitted":
        patch["replacements"].clear()
    elif fault == "duplicate":
        patch["replacements"].append(deepcopy(patch["replacements"][0]))
    else:
        patch["replacements"][0]["text"] = "é" * 151 if fault == "utf8" else "x" * 301
    original = deepcopy((context, candidate, patch))

    async def repair(*args):
        return deepcopy(patch), {"phase": "memory_repair", "synthetic": True}

    trace = {"usage": []}
    with pytest.raises(ShadowError, match="working_memory_repair_failed"):
        asyncio.run(
            ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
                {"message_id": "synthetic-input"}, context, candidate, trace, now()
            )
        )
    assert (context, candidate, patch) == original
    assert trace["memory_repair"]["response"] == patch


@pytest.mark.parametrize("fault", ["total", "duplicates", "count"])
def test_non_scoped_topology_or_total_overflow_still_requests_full_consolidation(fault):
    context, candidate, patch = saturated_case()
    working = candidate["working_memory"]
    if fault == "total":
        working["views"] = [f"Synthetic {i} " + "\\" * 235 for i in range(8)]
        working["waiting_for"] = [f"Distinct condition {i} " + "x" * 150 for i in range(8)]
        assert working_sizes(working)["total_bytes"] > 5000
    elif fault == "duplicates":
        working["waiting_for"][1] = working["waiting_for"][0]
    else:
        working["waiting_for"].append("Ninth synthetic prerequisite")
    _, captured = capture_request(context, candidate, patch)
    prompt = captured["prompt"]
    assert prompt["schema"]["$id"] == "urn:pipmind:schema:v1:memory-repair"
    assert "editable_keys" not in prompt["correction"]
    assert "patch_budget" not in prompt["correction"]
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


def test_scoped_request_respects_a_lower_calculated_budget_near_total_limit():
    context, candidate, patch = saturated_case()
    working = candidate["working_memory"]
    padding = 4990 - working_sizes(working)["total_bytes"]
    assert padding > 0
    for state in (context["working_memory"], working):
        state["position_theses"][0]["thesis"] += "x" * padding
    budget = repair_budget(working)
    allocation = budget["max_json_text_bytes"]["v0"]
    assert working_sizes(working)["total_bytes"] == 4990
    assert 1 < allocation < 280
    patch["replacements"][0]["text"] = "Synthetic compact proposal for compilation only"
    _, captured = capture_request(context, candidate, patch)
    properties = captured["prompt"]["schema"]["properties"]["replacements"]["items"]["properties"]
    assert properties["text"]["maxLength"] == allocation
    assert captured["prompt"]["correction"]["patch_budget"] == budget


@pytest.mark.parametrize("allocation", [117, 279, 300])
def test_scoped_generation_uses_280_character_headroom_with_lower_allocation_respected(allocation):
    context, candidate, patch = saturated_case()
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
            {"message_id": "synthetic-input"},
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
    requested = captured["schema"]["properties"]["replacements"]["items"]["properties"]["text"]
    assert requested["maxLength"] == min(280, allocation)
    assert model.contracts.schemas == normative
