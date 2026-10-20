"""The wire saves copying, never state, evidence, limits or audit information."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import ContractError, Contracts
from pipmind.shadow.common import ShadowError, encoded, now
from pipmind.shadow.maintenance import transition, validate_working
from pipmind.shadow.model import FoundryModel, ModelFailure
from pipmind.shadow.working_refs import SCHEMA_ID, VERSION, expand_reply, wire_schema


def state():
    return {
        "views": ["USD: reported 2.5%; vintage unverified — not proof of current policy."],
        "position_theses": [],
        "waiting_for": [
            "EACH official full text, effective date and comparable prior are required."
        ],
    }


def reply(working):
    return {
        "action": "hold",
        "reasoning": "Evidence remains unverified.",
        "evidence_ids": ["event:current"],
        "tool_calls": [],
        "working_memory": working,
    }


def test_exact_carry_forward_cannot_rephrase_qualifiers_or_mutate_its_sources():
    before = state()
    wire = reply({"views": [0], "waiting_for": [0], "position_theses": []})
    originals = deepcopy((before, wire))
    decoded, references = expand_reply(wire, before)
    assert decoded == reply(before)
    assert references == {
        "views": [{"after": 0, "before": 0}],
        "waiting_for": [{"after": 0, "before": 0}],
    }
    assert (before, wire) == originals
    Contracts().validate("model-reply", decoded)
    assert transition(before, decoded["working_memory"], [], now()) == set()


@pytest.mark.parametrize("value", [-1, 1, 99], ids=["negative", "missing", "large"])
def test_invalid_before_index_refuses_instead_of_dropping_or_clamping(value):
    wire = reply({**state(), "views": [value]})
    with pytest.raises(ShadowError, match="working_memory_reference"):
        expand_reply(wire, state())


@pytest.mark.parametrize("value", [False, 0.0, {"keep": 0}], ids=["bool", "float", "object"])
def test_other_json_types_are_not_silently_interpreted_as_references(value):
    decoded, references = expand_reply(reply({**state(), "views": [value]}), state())
    assert references == {}
    with pytest.raises(ContractError):
        Contracts().validate("model-reply", decoded)


def test_complete_arrays_and_supported_resolutions_are_still_required():
    before = state()
    decoded, _ = expand_reply(reply({**before, "waiting_for": []}), before)
    with pytest.raises(ShadowError, match="working_memory_dispositions"):
        transition(before, decoded["working_memory"], [], now())
    duplicated, _ = expand_reply(reply({**before, "views": [0, 0]}), before)
    with pytest.raises(ShadowError):
        validate_working(duplicated["working_memory"], now())
    too_many, _ = expand_reply(reply({**before, "views": [0] * 9}), before)
    with pytest.raises(ShadowError):
        validate_working(too_many["working_memory"], now())


def test_literal_strings_and_position_theses_keep_their_normative_meaning():
    before = state()
    wire = reply({**before, "views": ["0"], "position_theses": [0]})
    decoded, _ = expand_reply(wire, before)
    assert decoded["working_memory"]["views"] == ["0"]
    with pytest.raises(ContractError):
        Contracts().validate("model-reply", decoded)


def test_wire_schema_is_separate_from_the_unchanged_persistent_contract():
    contracts = Contracts()
    original = deepcopy(contracts.schemas["working-memory"])
    wire = wire_schema(original)
    assert wire["$id"] == SCHEMA_ID
    assert original == contracts.schemas["working-memory"]
    assert wire["properties"]["position_theses"] == original["properties"]["position_theses"]
    with pytest.raises(ContractError):
        contracts.validate("working-memory", {**state(), "views": [0]})


def test_foundry_adapter_retains_raw_wire_and_decoded_reply_with_existing_call_limits():
    before = state()
    wire = reply({"views": [0], "waiting_for": [0], "position_theses": []})
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(message=message, options=options)
            return SimpleNamespace(text=encoded(wire), usage_details={"output_token_count": 70})

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    result, trace = asyncio.run(model.decide({}, {"working_memory": before}, {}, None, now()))
    assert result == reply(before) and trace["wire_response"] == wire
    assert trace["wire_response_text"] == encoded(wire)
    assert trace["working_memory_encoding"] == VERSION
    assert trace["working_memory_references"]["waiting_for"] == [{"after": 0, "before": 0}]
    assert captured["options"]["max_tokens"] == 4096
    assert "reasoning" not in captured["options"]
    assert "COMPLETE expanded BEFORE" in captured["instructions"]
    assert "Responses always use the ordinary working-memory schema" not in captured["instructions"]
    assert "Triage returns exactly wake" not in captured["instructions"]
    assert (
        trace["request"]["message"]["schema"]["properties"]["working_memory"]["$ref"] == SCHEMA_ID
    )
    assert before == state()

    wire["working_memory"]["views"] = [99]
    with pytest.raises(ModelFailure) as failure:
        asyncio.run(model.decide({}, {"working_memory": before}, {}, None, now()))
    assert failure.value.trace["failure_code"] == "working_memory_reference"
    assert failure.value.trace["working_memory_encoding"] == VERSION
    assert failure.value.trace["invalid_response"] == encoded(wire)
