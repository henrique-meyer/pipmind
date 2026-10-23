"""Synthetic geometry/provenance tests; these do not grade actual semantic quality."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import utc
from pipmind.shadow.common import ShadowError, encoded
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.maintenance import (
    entries,
    repair_budget,
    validate_working,
    working_sizes,
)
from pipmind.shadow.model import FoundryModel, ModelFailure, expand_repair_candidate

EVENT = "synthetic-p62-input"
MOMENT = utc("2031-11-05T12:00:00Z")
VIEW = (
    "Nov05'31:A-bank2.85%,prior2.65%,+20bp if comparable;pub09:42Z new conf/min,"
    "old unver. B-bank cut1.15%Apr02;holds1.15%May06/Jun17. Nov04 official:"
    "inflation OUTLOOK worse;Dec hike unsure;not CPI actual/commitment."
)
WAIT = (
    "Nov05'31:A/B-bank EACH:verify vintage/pub time/basis/comp prior/official "
    "decision+rationale pre-change/stance;fcst pre-surprise. Nov04 official:"
    "FULL TEXT/context pre-interpretation;new timing conf not for old records."
)


def sized(text, size):
    # Filler models byte geometry ONLY; no claim that it is real market state.
    assert len(text.encode()) < size
    return text + "|" + "x" * (size - len(text.encode()) - 1)


def related_case():
    working = {
        "views": [
            sized(f"Synthetic independent view{i};vintage unresolved.", 280) for i in range(8)
        ],
        "waiting_for": [
            sized(f"Synthetic EACH full-text condition{i};unresolved.", 280) for i in range(8)
        ],
        "position_theses": [],
    }
    working["views"][6] = sized(VIEW, 311)
    working["waiting_for"][5] = sized(WAIT, 299)
    delta = 4626 - len(encoded(working).encode())
    working["views"][0] = sized("Synthetic independent view0;vintage unresolved.", 280 + delta)
    before = deepcopy(working)
    before["views"][6] = sized(VIEW, 298)
    before["waiting_for"][5] = sized(WAIT, 296)
    candidate = {
        "action": "hold",
        "reasoning": "Synthetic original decision remains exact",
        "evidence_ids": [EVENT],
        "tool_calls": [],
        "working_memory": working,
        "memory_dispositions": [
            {
                "from": key,
                "to": key,
                "disposition": "updated",
                "reason": "Synthetic dated claim and associated prerequisite updated",
                "evidence_ids": [EVENT],
            }
            for key in ("v6", "w5")
        ],
    }
    context = {
        "working_memory": before,
        "memory_policy": {
            "version": "bounded-working-v1",
            "checkpoint": "synthetic-checkpoint",
            "origin_checkpoint": "synthetic-origin",
        },
    }
    patch = {
        "replacements": [
            {
                "from": key,
                "text": text,
                "reason": "Synthetic scoped qualifier factoring with explicit named scope",
                "evidence_ids": [EVENT],
            }
            for key, text in (("v6", VIEW), ("w5", WAIT))
        ]
    }
    assert len(encoded(working).encode()) == 4626
    assert len(encoded(before).encode()) == 4610
    assert len(VIEW.encode()) <= 280 and len(WAIT.encode()) <= 280
    return context, candidate, patch


def budget_for(context, candidate, event=EVENT):
    return repair_budget(
        candidate["working_memory"],
        before=context["working_memory"],
        dispositions=candidate.get("memory_dispositions"),
        event_ref=event,
    )


def request_for(context, candidate, patch):
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(prompt=json.loads(message), options=options)
            return SimpleNamespace(text=encoded(patch), usage_details={})

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    asyncio.run(model.repair_memory({"message_id": EVENT}, context, candidate, None, MOMENT))
    return captured


def test_related_budget_matches_retained_geometry_without_increasing_any_limit():
    context, candidate, _ = related_case()
    assert repair_budget(candidate["working_memory"])["editable_keys"] == ["v6"]
    budget = budget_for(context, candidate)
    assert budget == {
        "editable_keys": ["v6", "w5"],
        "fixed_bytes": 4016,
        "target_json_text_bytes": {"v6": 220, "w5": 220},
        "max_json_text_bytes": {"v6": 300, "w5": 300},
        "planned_total_bytes": 4456,
        "hard_total_bytes": 5000,
        "related_edit_group": True,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "old_event",
        "missing_current_evidence",
        "missing_mapping",
        "duplicate_mapping",
        "cross_kind",
        "merged",
        "retained_changed",
        "third_edit",
        "literal_move",
        "count_change",
        "thesis_change",
        "unknown_mapping",
        "nonlist_evidence",
    ],
)
def test_unsupported_related_group_falls_back_to_existing_single_key_scope(fault):
    context, candidate, _ = related_case()
    rows = candidate["memory_dispositions"]
    if fault == "old_event":
        rows[1]["evidence_ids"] = ["synthetic-older-input"]
    elif fault == "missing_current_evidence":
        rows[0]["evidence_ids"] = []
    elif fault == "missing_mapping":
        rows.pop()
    elif fault == "duplicate_mapping":
        rows.append(deepcopy(rows[0]))
    elif fault == "cross_kind":
        rows[1]["to"] = "v5"
    elif fault == "merged":
        rows[1]["disposition"] = "merged"
    elif fault == "retained_changed":
        rows[1]["disposition"] = "retained"
    elif fault == "third_edit":
        candidate["working_memory"]["views"][1] = "Synthetic third changed entry"
    elif fault == "literal_move":
        context["working_memory"]["waiting_for"][0] = candidate["working_memory"]["waiting_for"][5]
    elif fault == "count_change":
        context["working_memory"]["waiting_for"].pop()
    elif fault == "thesis_change":
        context["working_memory"]["position_theses"] = [{"synthetic": "different"}]
    elif fault == "unknown_mapping":
        rows.append({"from": "w99", "to": "w99", "disposition": "retained"})
    elif fault == "nonlist_evidence":
        rows[0]["evidence_ids"] = EVENT
    assert budget_for(context, candidate) == repair_budget(candidate["working_memory"])
    assert budget_for(context, candidate)["editable_keys"] == ["v6"]


@pytest.mark.parametrize("fault", ["total", "duplicate", "count"])
def test_total_or_invalid_topology_keeps_existing_full_mode(fault):
    context, candidate, patch = related_case()
    if fault == "total":
        candidate["working_memory"]["position_theses"] = [
            {"instrument": "EUR/USD", "as_of": MOMENT.isoformat(), "thesis": "t" * 900}
        ]
    elif fault == "duplicate":
        candidate["working_memory"]["views"][0] = candidate["working_memory"]["views"][1]
    else:
        candidate["working_memory"]["views"].append("Synthetic ninth entry")
    # Stop the synthetic full-mode call before result validation; mode selection
    # is independently visible in its retained request even for this patch stub.
    with pytest.raises(ShadowError) as caught:
        request_for(context, candidate, patch)
    request = caught.value.trace["request"]["message"]
    assert request["schema"]["$id"].endswith(":memory-repair")
    assert "editable_keys" not in request["correction"]


def test_prompt_and_engine_audit_share_exact_two_key_budget_and_preserve_fixed_state():
    context, candidate, patch = related_case()
    original = deepcopy((context, candidate, patch))
    captured = request_for(context, candidate, patch)
    budget = budget_for(context, candidate)
    assert captured["prompt"]["correction"]["patch_budget"] == budget
    schema = captured["prompt"]["schema"]["properties"]["replacements"]
    assert schema["minItems"] == schema["maxItems"] == 2
    assert schema["items"]["properties"]["from"]["enum"] == ["v6", "w5"]
    assert schema["items"]["properties"]["text"]["maxLength"] == 280
    assert captured["options"]["response_format"] == {"type": "json_object"}
    assert captured["options"]["max_tokens"] == 4096 and captured["tools"] == []

    async def repair(*args):
        return deepcopy(patch), {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    after = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": EVENT}, context, candidate, result, MOMENT
        )
    )
    assert result["memory_repair"]["patch_budget"] == budget
    assert (context, candidate, patch) == original
    for key, text in entries(candidate["working_memory"]).items():
        if key not in {"v6", "w5"}:
            assert entries(after["working_memory"])[key] == text
    for key in candidate.keys() - {"working_memory", "memory_dispositions"}:
        assert after[key] == candidate[key]
    assert (
        after["working_memory"]["position_theses"] == candidate["working_memory"]["position_theses"]
    )
    validate_working(after["working_memory"], MOMENT)


@pytest.mark.parametrize("fault", ["omitted", "extra", "wrong_kind", "306_ascii", "utf8"])
def test_related_patch_remains_fail_closed_with_full_raw_response(fault):
    context, candidate, patch = related_case()
    if fault == "omitted":
        patch["replacements"].pop()
    elif fault == "extra":
        row = deepcopy(patch["replacements"][0])
        row["from"] = "v0"
        patch["replacements"].append(row)
    elif fault == "wrong_kind":
        patch["replacements"][1]["from"] = "v5"
    else:
        patch["replacements"][0]["text"] = "x" * 306 if fault == "306_ascii" else "é" * 151

    async def repair(*args):
        return deepcopy(patch), {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    with pytest.raises(ShadowError, match="^working_memory_repair_failed$"):
        asyncio.run(
            ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
                {"message_id": EVENT}, context, candidate, result, MOMENT
            )
        )
    assert result["memory_repair"]["response"] == patch


def test_related_instruction_explicitly_limits_factoring_and_claim_category():
    context, candidate, patch = related_case()
    instructions = request_for(context, candidate, patch)["instructions"]
    for phrase in (
        "related_edit_group=true ONLY",
        "collectively within the two selected entries",
        "facts stay in views",
        "active prerequisites stay in waiting_for",
        "Factor ONLY duplicated verification",
        "explicit named scope",
        "EACH-item and FULL TEXT/context",
        "no-confirmation-transfer",
        "an inflation OUTLOOK is not an observed CPI actual",
        "a forecast is not a measurement",
        "uncertainty about a future policy action is not a commitment",
    ):
        assert phrase in instructions
    assert working_sizes(candidate["working_memory"])["total_bytes"] < 5000


def test_unrelated_single_key_prompt_never_gets_collective_factoring_permission():
    context, candidate, patch = related_case()
    candidate["memory_dispositions"][1]["evidence_ids"] = ["synthetic-old-input"]
    patch["replacements"].pop()
    captured = request_for(context, candidate, patch)
    assert "related_edit_group=true ONLY" not in captured["instructions"]
    assert captured["prompt"]["correction"]["editable_keys"] == ["v6"]


def test_no_overlong_entry_never_creates_a_related_repair_permission():
    context, candidate, _ = related_case()
    candidate["working_memory"]["views"][6] = VIEW
    assert budget_for(context, candidate) is None


def test_normal_decision_gets_claim_precision_without_collective_repair_permission():
    context, candidate, _ = related_case()
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            return SimpleNamespace(text=encoded(candidate), usage_details={})

    def factory(**kwargs):
        captured.update(kwargs)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    asyncio.run(
        model.decide(
            {"message_id": EVENT},
            context,
            {"wake": True, "summary": "Synthetic event"},
            None,
            MOMENT,
        )
    )
    assert "an inflation OUTLOOK is not an observed CPI actual" in captured["instructions"]
    assert "related_edit_group=true ONLY" not in captured["instructions"]


def source_card(identifier, headline, *, partial=False):
    return {
        "metadata": {"record_id": identifier, "part": "memory", "ts": "2031-11-04T12:00:00Z"},
        "body": encoded({"kind": "NewsEvent", "payload": {"headline": headline}}),
        "partial": partial,
    }


def case_with_ambiguous_older_claim():
    context, candidate, patch = related_case()
    older = "synthetic-older-outlook"
    vague = "Nov04:price pressures worse;Nov05:inflation outlook worse;unverified headlines."
    context["working_memory"]["views"][6] = sized(vague, 298)
    candidate["working_memory"]["views"][6] = sized(vague, 311)
    candidate["evidence_ids"].append(older)
    candidate["memory_dispositions"][0]["evidence_ids"].append(older)
    card = source_card(older, "Nov04 bank official: inflation OUTLOOK worse; no current CPI actual")
    context["retrieved_memories"] = [
        card,
        source_card("synthetic-unrelated", "Unrelated historical source must not enter correction"),
        source_card(EVENT, "Current event already appears separately"),
    ]
    context["recent_journal"] = [
        source_card(older, "Journal text must not be injected through this evidence path")
    ]
    return context, candidate, patch, card


def test_each_dated_claim_restoration_receives_only_its_exact_cited_supplied_card():
    context, candidate, patch, card = case_with_ambiguous_older_claim()
    original = deepcopy((context, candidate, patch))
    captured = request_for(context, candidate, patch)
    selected = captured["prompt"]["context"]["related_source_evidence"]
    assert selected == [card]
    assert (context, candidate, patch) == original
    correction = captured["prompt"]["correction"]
    assert captured["prompt"]["context"]["working_memory"] == context["working_memory"]
    assert encoded(
        expand_repair_candidate(
            correction["candidate"], context["working_memory"], correction["editable_keys"]
        )
    ) == encoded(candidate)
    text = encoded(captured["prompt"])
    assert "Unrelated historical source" not in text and "Journal text must not" not in text
    assert "Current event already appears separately" not in text
    for phrase in (
        "For EACH dated or attributed claim being edited",
        "if BEFORE/candidate shorthand is vague",
        "supplied retained source evidence explicitly establishes its category",
        "retain or restore that category for THAT claim",
        "rather than inheriting the ambiguity",
        "An adjacent dated claim's category does not label an earlier claim",
        "evidence is missing, partial or contradictory, preserve uncertainty",
        "never guess a category",
    ):
        assert phrase in captured["instructions"]


def test_partial_card_is_carried_exactly_without_reprojection_or_verification_upgrade():
    context, candidate, patch, card = case_with_ambiguous_older_claim()
    card["partial"] = True
    card["body"] = 'Partial source: "outlook"; unexplained remainder unavailable'
    captured = request_for(context, candidate, patch)
    assert captured["prompt"]["context"]["related_source_evidence"] == [card]
    assert captured["prompt"]["context"]["related_source_evidence"][0]["partial"] is True


def test_missing_supplied_source_does_not_fetch_or_fabricate_category_evidence():
    context, candidate, patch, _ = case_with_ambiguous_older_claim()
    context["retrieved_memories"] = []
    captured = request_for(context, candidate, patch)
    assert captured["prompt"]["context"]["related_source_evidence"] == []
    assert "Nov04 bank official" not in encoded(captured["prompt"])
    assert "never guess a category" in captured["instructions"] and captured["tools"] == []


def test_ineligible_single_entry_repair_keeps_source_cards_out_of_compact_context():
    context, candidate, patch, _ = case_with_ambiguous_older_claim()
    candidate["memory_dispositions"][1]["evidence_ids"] = ["synthetic-old-event"]
    patch["replacements"].pop()
    captured = request_for(context, candidate, patch)
    assert "related_source_evidence" not in captured["prompt"]["context"]


@pytest.mark.parametrize("fault", ["card_size", "aggregate_size", "conflicting_duplicate"])
def test_related_source_bounds_or_conflicts_refuse_before_any_rpc(fault):
    context, candidate, patch, card = case_with_ambiguous_older_claim()
    if fault == "card_size":
        card["body"] = "x" * 2401
    elif fault == "aggregate_size":
        context["retrieved_memories"] = []
        for index in range(4):
            identifier = f"synthetic-cited-card-{index}"
            candidate["memory_dispositions"][0]["evidence_ids"].append(identifier)
            extra = source_card(identifier, "Synthetic source")
            extra["body"] = "x" * 2400
            context["retrieved_memories"].append(extra)
    else:
        other = deepcopy(card)
        other["body"] += " conflicting content"
        context["retrieved_memories"].append(other)
    original = deepcopy((context, candidate, patch))
    with pytest.raises(ModelFailure) as caught:
        request_for(context, candidate, patch)
    trace = caught.value.trace
    assert trace["attempted"] is False and trace["usage_unknown"] is False
    assert trace["failure_code"] == "shadow_context_projection" and not trace["tools"]
    assert (context, candidate, patch) == original


def test_related_card_cannot_push_total_prompt_past_guard_before_factory_or_rpc():
    context, candidate, patch, card = case_with_ambiguous_older_claim()
    context["playbook"] = {"version": "synthetic", "text": ""}
    without_card = deepcopy(context)
    without_card["retrieved_memories"] = []
    base = request_for(without_card, candidate, patch)
    base_bytes = len((base["instructions"] + encoded(base["prompt"])).encode())
    padding = 27950 - base_bytes
    assert padding > 0
    context["playbook"]["text"] = "p" * padding
    without_card["playbook"] = deepcopy(context["playbook"])
    fits = request_for(without_card, candidate, patch)
    assert len((fits["instructions"] + encoded(fits["prompt"])).encode()) == 27950
    factory_calls = []
    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test",
        "t",
        "d",
        enabled=True,
        factory=lambda **kwargs: factory_calls.append(kwargs),
    )
    with pytest.raises(ModelFailure) as caught:
        asyncio.run(model.repair_memory({"message_id": EVENT}, context, candidate, None, MOMENT))
    trace = caught.value.trace
    assert trace["attempted"] is False and trace["usage_unknown"] is False
    assert trace["failure_code"] == "shadow_context_size" and trace["prompt_bytes"] > 28000
    assert trace["request"]["message"]["context"]["related_source_evidence"] == [card]
    assert factory_calls == []
