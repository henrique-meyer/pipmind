"""Synthetic lossless request-wire checks, never actual semantic-quality claims."""

import asyncio
from copy import deepcopy

import pytest
from test_shadow_repair_related import EVENT, MOMENT, related_case, request_for, source_card

from pipmind.shadow import model as module
from pipmind.shadow.common import ShadowError, encoded
from pipmind.shadow.context import phase_context
from pipmind.shadow.model import (
    REPAIR_CANDIDATE_ENCODING,
    FoundryModel,
    ModelFailure,
    expand_repair_candidate,
    project_repair_candidate,
)


def test_same_index_request_refs_reconstruct_every_candidate_field_canonically():
    context, candidate, _ = related_case()
    original = deepcopy((context, candidate))
    before = context["working_memory"]
    wire = project_repair_candidate(candidate, before, ["v6", "w5"])
    assert wire["working_memory"]["views"][:6] == list(range(6))
    assert wire["working_memory"]["waiting_for"][:5] == list(range(5))
    assert wire["working_memory"]["views"][6] == candidate["working_memory"]["views"][6]
    assert wire["working_memory"]["waiting_for"][5] == candidate["working_memory"]["waiting_for"][5]
    assert (
        sum(type(x) is int for f in ("views", "waiting_for") for x in wire["working_memory"][f])
        == 14
    )
    assert encoded(expand_repair_candidate(wire, before, ["v6", "w5"])) == encoded(candidate)
    assert len(encoded(wire).encode()) < len(encoded(candidate).encode())
    assert (context, candidate) == original


def test_unicode_escaping_whitespace_and_literal_numeric_text_are_not_normalized():
    context, candidate, _ = related_case()
    before = context["working_memory"]
    texts = ['é e\u0301  😃\\quoted"\tline\nnext', "0", " doubled  spaces "]
    for index, text in enumerate(texts):
        before["views"][index] = candidate["working_memory"]["views"][index] = text
    wire = project_repair_candidate(candidate, before, ["v6", "w5"])
    reconstructed = expand_repair_candidate(wire, before, ["v6", "w5"])
    assert reconstructed["working_memory"]["views"][:3] == texts
    assert encoded(reconstructed) == encoded(candidate)
    # A changed same-kind index must remain a complete literal, never a moved ref.
    candidate["working_memory"]["views"][0] = before["views"][1]
    moved = project_repair_candidate(candidate, before, ["v6", "w5"])
    assert moved["working_memory"]["views"][0] == "0"
    assert encoded(expand_repair_candidate(moved, before, ["v6", "w5"])) == encoded(candidate)


@pytest.mark.parametrize("value", [-1, 1, 99, False, True, 0.0, {}, []])
def test_invalid_cross_index_or_noninteger_refs_refuse(value):
    context, candidate, _ = related_case()
    wire = project_repair_candidate(candidate, context["working_memory"], ["v6", "w5"])
    wire["working_memory"]["views"][0] = value
    with pytest.raises(ShadowError, match="shadow_repair_candidate_reference"):
        expand_repair_candidate(wire, context["working_memory"], ["v6", "w5"])


@pytest.mark.parametrize("fault", ["selected", "count", "before_count", "key", "raw_integer"])
def test_selected_refs_changed_topology_unknown_keys_and_nonliteral_input_refuse(fault):
    context, candidate, _ = related_case()
    before = context["working_memory"]
    keys = ["v6", "w5"]
    wire = project_repair_candidate(candidate, before, keys)
    if fault == "selected":
        wire["working_memory"]["views"][6] = 6
    elif fault == "count":
        wire["working_memory"]["views"].append("ninth")
    elif fault == "before_count":
        before["waiting_for"].pop()
    elif fault == "key":
        keys = ["v99", "w5"]
    else:
        candidate["working_memory"]["views"][0] = 0
        with pytest.raises(ShadowError, match="shadow_repair_candidate_reference"):
            project_repair_candidate(candidate, before, keys)
        return
    with pytest.raises(ShadowError, match="shadow_repair_candidate_reference"):
        expand_repair_candidate(wire, before, keys)


def with_three_cards():
    context, candidate, patch = related_case()
    cards = [
        source_card(
            f"synthetic-cited-{i}",
            f"Synthetic outlook {i}; FULL TEXT still required",
            partial=i == 1,
        )
        for i in range(3)
    ]
    candidate["memory_dispositions"][0]["evidence_ids"].extend(
        card["metadata"]["record_id"] for card in cards
    )
    context["retrieved_memories"] = cards
    return context, candidate, patch, cards


def test_related_wire_supplies_before_once_and_all_three_exact_cards_without_reprojection():
    context, candidate, patch, cards = with_three_cards()
    original = deepcopy((context, candidate, patch))
    captured = request_for(context, candidate, patch)
    prompt = captured["prompt"]
    assert prompt["context"]["working_memory"] == context["working_memory"]
    assert "encoding" not in prompt["context"]["working_memory"]
    assert prompt["context"]["related_source_evidence"] == cards
    correction = prompt["correction"]
    assert correction["candidate_working_memory_encoding"] == REPAIR_CANDIDATE_ENCODING
    assert encoded(
        expand_repair_candidate(
            correction["candidate"],
            prompt["context"]["working_memory"],
            correction["editable_keys"],
        )
    ) == encoded(candidate)
    assert "Each integer occurs only at its own index" in captured["instructions"]
    assert captured["tools"] == [] and captured["options"]["max_tokens"] == 4096
    assert (context, candidate, patch) == original


def test_unrelated_single_key_path_keeps_its_existing_projection_and_literal_candidate():
    context, candidate, patch, _ = with_three_cards()
    candidate["memory_dispositions"][1]["evidence_ids"] = ["synthetic-old-event"]
    patch["replacements"].pop()
    captured = request_for(context, candidate, patch)
    assert "candidate_working_memory_encoding" not in captured["prompt"]["correction"]
    assert captured["prompt"]["correction"]["candidate"] == candidate
    assert (
        captured["prompt"]["context"]["working_memory"]
        == phase_context(context, "triage")["working_memory"]
    )
    assert "same-index-before-refs-v1" not in captured["instructions"]


def test_failed_canonical_roundtrip_refuses_before_factory_or_rpc(monkeypatch):
    context, candidate, _patch, _cards = with_three_cards()
    wrong = deepcopy(candidate)
    wrong["working_memory"]["views"][0] += " changed"
    monkeypatch.setattr(module, "expand_repair_candidate", lambda *args: wrong)
    calls = []
    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test",
        "t",
        "d",
        enabled=True,
        factory=lambda **kw: calls.append(kw),
    )
    with pytest.raises(ModelFailure) as caught:
        asyncio.run(model.repair_memory({"message_id": EVENT}, context, candidate, None, MOMENT))
    assert caught.value.trace["attempted"] is False and calls == []
