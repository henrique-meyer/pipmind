"""Fit optional decision evidence after a proven local, call-free size refusal.

This selects fewer already-supplied cards. It does not compress mandatory state,
retrieve evidence, or change the retained source context or history.
"""

import os
from copy import deepcopy

from pipmind.contracts import Contracts, stamp
from pipmind.shadow.common import digest, encoded
from pipmind.shadow.context import CONTEXT_VERSION, phase_context
from pipmind.shadow.maintenance import POLICY
from pipmind.shadow.model import (
    CALENDAR_INSTRUCTIONS,
    CLAIM_CATEGORY_INSTRUCTIONS,
    INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
    PROMPT_VERSION,
)
from pipmind.shadow.working_refs import INSTRUCTIONS as REFERENCE_INSTRUCTIONS
from pipmind.shadow.working_refs import SCHEMA_ID, VERSION, wire_schema

LIMIT = 28000
FIELDS = ("retrieved_memories", "recent_journal")


def _request(event, context, triage, moment):
    selected = phase_context(context, "decision")
    contracts = Contracts()
    schema = deepcopy(contracts.schemas["model-reply"])
    schema["$id"] = "urn:pipmind:wire:decision-working-refs-v1"
    schema["properties"]["working_memory"]["$ref"] = SCHEMA_ID
    prompt = {
        "phase": "decision",
        "now": stamp(moment),
        "event": deepcopy(event),
        "context": {k: v for k, v in selected.items() if k not in {"identity", "playbook"}},
        "playbook_version": selected.get("playbook", {}).get("version"),
        "triage": deepcopy(triage),
        "schema": schema,
        "schema_definitions": {
            "working-memory": wire_schema(contracts.schemas["working-memory"]),
            "memory-dispositions": contracts.schemas["memory-dispositions"],
        },
    }
    instructions = (
        INSTRUCTIONS.replace(
            " Responses always use the ordinary working-memory schema with strings.", ""
        ).replace(" Triage returns exactly wake and summary, no other keys.", "")
        + REFERENCE_INSTRUCTIONS
    )
    memory = MEMORY_INSTRUCTIONS.replace(
        " If no state change is justified, copy the supplied strings EXACTLY."
        " Rephrasing even one entry is a change and needs its disposition;"
        " do not silently edit safety qualifiers.",
        " Use references for exact carry-forward; changed text needs its disposition."
        " Never silently edit safety qualifiers.",
    )
    instructions += CLAIM_CATEGORY_INSTRUCTIONS
    if (
        event.get("kind") == "ClockEvent"
        and event.get("payload", {}).get("reason") == "daily_brief"
        and context.get("feed_status", {}).get("calendar", {}).get("record_id")
    ):
        instructions += CALENDAR_INSTRUCTIONS
    if context.get("memory_policy", {}).get("version") == POLICY:
        instructions += memory
    instructions += "\n" + selected.get("identity", "")
    instructions += "\n" + selected.get("playbook", {}).get("text", "")
    request = {"instructions": instructions, "message": prompt}
    return request, len((instructions + encoded(prompt)).encode()), selected


def _cards(selected):
    return [
        (field, card["metadata"]["record_id"], encoded(card))
        for field in FIELDS
        for card in selected[field]
    ]


def _mandatory(selected):
    result = deepcopy(selected)
    for field in FIELDS:
        result.pop(field)
    for key in ("candidate_cards", "budget_omissions"):
        result["coverage"].pop(key)
    return encoded(result)


def _card_pins(cards):
    return [
        {"field": field, "record_id": identifier, "sha256": digest(raw)}
        for field, identifier, raw in cards
    ]


def fit_decision_evidence(event, context, triage, moment, trace):
    """Return copied context and audit, or None when any eligibility/proof fails.

    The caller may attempt one normal decision with the result. This function
    makes no model/store calls, and never certifies the resulting decision.
    """
    try:
        return _fit(event, context, triage, moment, trace)
    except (KeyError, TypeError, ValueError, RecursionError, AttributeError):
        return None


def _fit(event, context, triage, moment, trace):
    if not all(isinstance(value, dict) for value in (event, context, triage, trace)):
        return None
    expected = {
        "phase": "decision",
        "attempted": False,
        "usage_unknown": False,
        "failure_code": "shadow_context_size",
        "error_type": "ShadowError",
        "prompt_version": PROMPT_VERSION,
        "context_version": CONTEXT_VERSION,
        "application_build": os.environ.get("BUILD_SHA", "development"),
        "max_output_tokens": 4096,
        "working_memory_encoding": VERSION,
        "tool_names": ["search_memory"],
        "tools": [],
        "input_tokens": None,
        "output_tokens": None,
        "invalid_response": None,
    }
    if any(
        key not in trace or encoded(trace[key]) != encoded(value) for key, value in expected.items()
    ):
        return None
    if (
        type(trace.get("latency_ms")) is not int
        or trace["latency_ms"] < 0
        or type(trace.get("prompt_bytes")) is not int
        or "reasoning_effort" in trace
        or "source_request" in trace
    ):
        return None
    for field, maximum in zip(FIELDS, (6, 3), strict=True):
        rows = context.get(field, [])
        if not isinstance(rows, list) or len(rows) > maximum:
            return None
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("metadata"), dict)
                or not isinstance(row["metadata"].get("record_id"), str)
                or not row["metadata"]["record_id"]
                or not isinstance(row.get("body"), str)
                or type(row.get("partial", False)) is not bool
            ):
                return None
    request, before_bytes, original = _request(event, context, triage, moment)
    if (
        before_bytes <= LIMIT
        or before_bytes != trace["prompt_bytes"]
        or encoded(request) != encoded(trace.get("request"))
    ):
        return None
    original_cards = _cards(original)
    if not original_cards or len(original_cards) > 9:
        return None
    original_ids = {identifier for _, identifier, _ in original_cards}
    kept = set(original_ids)
    mandatory = _mandatory(original)
    steps = []
    # First exclude originally unadmitted rows; otherwise projection may backfill
    # them as room becomes available. Then drop whole IDs in reverse priority.
    for removed_id in [None, *(identifier for _, identifier, _ in reversed(original_cards))]:
        if removed_id is not None:
            kept.remove(removed_id)
        candidate = deepcopy(context)
        removed = []
        for field in FIELDS:
            if field not in context:
                continue
            candidate[field] = []
            for index, row in enumerate(context[field]):
                identifier = row["metadata"]["record_id"]
                if identifier in kept:
                    candidate[field].append(deepcopy(row))
                else:
                    removed.append(
                        {
                            "field": field,
                            "index": index,
                            "record_id": identifier,
                            "reason": "not_originally_supplied"
                            if identifier not in original_ids
                            else "decision_prompt_budget",
                            "row": deepcopy(row),
                            "sha256": digest(encoded(row)),
                        }
                    )
        after, size, projected = _request(event, candidate, triage, moment)
        cards = _cards(projected)
        expected_cards = [card for card in original_cards if card[1] in kept]
        if cards != expected_cards or _mandatory(projected) != mandatory:
            return None
        steps.append(
            {
                "removed_record_id": removed_id,
                "prompt_bytes": size,
                "prompt_sha256": digest(after["instructions"] + encoded(after["message"])),
                "supplied_cards": _card_pins(cards),
            }
        )
        if size <= LIMIT:
            return candidate, {
                "method": "decision-evidence-exact-subset-v1",
                "original_refusal": deepcopy(trace),
                "original_supplied_cards": _card_pins(original_cards),
                "selected_supplied_cards": _card_pins(cards),
                "removed_rows": removed,
                "before_prompt_bytes": before_bytes,
                "after_prompt_bytes": size,
                "before_prompt_sha256": digest(
                    request["instructions"] + encoded(request["message"])
                ),
                "after_prompt_sha256": steps[-1]["prompt_sha256"],
                "before_context_sha256": digest(encoded(context)),
                "after_context_sha256": digest(encoded(candidate)),
                "projection_steps": steps,
            }
    return None
