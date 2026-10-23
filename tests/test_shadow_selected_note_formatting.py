"""Synthetic format proofs; no factual or semantic-quality claim is inferred."""

import asyncio
import re
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.contracts import Contracts, utc
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.maintenance import (
    SELECTED_NOTE_FORMAT_VERSION,
    entries,
    expand_repair_patch,
    punctuation_spacing,
    repair_budget,
    selected_note_formatting,
    transition,
    validate_working,
)

MOMENT = utc("2026-10-18T00:00:00Z")


def note(*, size=302, insert="", boundary="pre-surprise. Example", ending="pre-interpretation."):
    text = (
        "Oct17'26:Synthetic source " + insert + " EACH verify links/vintage/pub time/"
        "comparable prior/basis/current official decision+rationale pre-change/stance;"
        "fcst " + boundary + " Oct16/claim Oct16-17:headlines unver;FULL TEXT/context " + ending
    )
    assert len(text.encode()) <= size
    return text.replace("Synthetic", "Synthetic" + "x" * (size - len(text.encode())), 1)


def synthetic_case(event_id="synthetic-note-event"):
    before = {
        "views": [f"Synthetic view {i};source unverified" for i in range(8)],
        "waiting_for": [f"Synthetic condition {i};unresolved" for i in range(8)],
        "position_theses": [
            {
                "instrument": "EUR/USD",
                "thesis": "Exact confirmed synthetic thesis",
                "as_of": "2026-10-17T00:00:00Z",
            }
        ],
    }
    candidate = {
        "action": "hold",
        "reasoning": "Exact synthetic decision",
        "evidence_ids": [event_id],
        "tool_calls": [],
        "working_memory": deepcopy(before),
        "memory_dispositions": [
            {
                "from": key,
                "to": key,
                "disposition": "updated",
                "reason": "Synthetic source update; no dependency resolved",
                "evidence_ids": [event_id],
            }
            for key in ("v0", "w0")
        ],
    }
    candidate["working_memory"]["views"][0] = "Synthetic dated update;unverified;".ljust(311, "x")
    candidate["working_memory"]["waiting_for"][0] = "Synthetic changed verification prerequisite"
    patch = {
        "replacements": [
            {
                "from": key,
                "text": text,
                "reason": "Synthetic complete meaning retained",
                "evidence_ids": [event_id],
            }
            for key, text in (("v0", "Synthetic dated update remains unverified"), ("w0", note()))
        ]
    }
    context = {
        "working_memory": before,
        "memory_policy": {
            "version": "bounded-working-v1",
            "checkpoint": "synthetic-checkpoint",
            "origin_checkpoint": "synthetic-origin",
        },
    }
    return context, candidate, patch


@pytest.mark.parametrize("size", [301, 302])
def test_exact_ascii_removals_preserve_tokens_nonterminal_punctuation_and_rebuild_raw(size):
    raw = note(size=size)
    applied, proof = selected_note_formatting(raw)
    assert len(applied.encode()) == 300
    assert proof["method"] == SELECTED_NOTE_FORMAT_VERSION
    offsets = proof["removed_utf8_offsets"]
    assert offsets == sorted(set(offsets)) and len(offsets) == size - 300
    assert proof["before_sha256"] == digest(raw) and proof["after_sha256"] == digest(applied)
    assert offsets[-1] == len(raw.encode()) - 1 and raw.encode()[offsets[-1]] == 46
    if size == 302:
        assert raw.encode()[offsets[0]] == 32 and "pre-surprise.Example" in applied
    rebuilt = bytearray(applied.encode())
    for offset in offsets:
        rebuilt.insert(offset, raw.encode()[offset])
    assert bytes(rebuilt) == raw.encode()
    assert re.findall(r"\w+|[^\w\s]", raw)[:-1] == re.findall(r"\w+|[^\w\s]", applied)
    assert re.findall(r"[^\w\s]", raw)[:-1] == re.findall(r"[^\w\s]", applied)
    assert punctuation_spacing(raw) == raw  # Existing blanket quote rule remains unchanged.


@pytest.mark.parametrize(
    "insert",
    [
        "'quoted condition'",
        '"quoted condition"',
        "`code`",
        "‘quoted’",
        "a@example.test",
        "https://example.test",
        "www.example.test",
        "example.test",
        "rate2.25%",
        "date17.10.26",
        "U.S. release",
        "e.g. source",
        "Dr. Example",
        "approx. Example",
        "/tmp/state",
        "C:/tmp/state",
        "../state",
        "folder\\state",
        "SELECT value FROM table;",
        "return value;",
        "value=1",
        "[value]",
        "{value}",
        "call(value)",
        "one\ntwo",
        "one\ttwo",
        "naïve",
        "Oct99'26",
        "Jan1'26x",
        "O'Neil",
    ],
)
def test_quoted_ambiguous_dots_nonprose_and_unrecognized_years_are_unchanged(insert):
    raw = note(insert=insert)
    assert selected_note_formatting(raw) == (raw, None)


@pytest.mark.parametrize("size", [299, 300, 303, 310])
def test_already_valid_or_more_than_two_bytes_overlong_are_unchanged(size):
    raw = note(size=size)
    assert selected_note_formatting(raw) == (raw, None)


@pytest.mark.parametrize(
    "fault",
    [
        "header",
        "abbreviation",
        "unknown_end",
        "unknown_boundary",
        "missing_boundary",
        "two_boundaries",
    ],
)
def test_only_explicit_condition_phrases_are_eligible(fault):
    if fault == "header":
        raw = "x" + note(size=301)[1:]
    elif fault == "abbreviation":
        raw = note(ending="pre-approx.")
    elif fault == "unknown_end":
        raw = note(ending="verification.")
    elif fault == "unknown_boundary":
        raw = note(boundary="surprise. Example")
    elif fault == "missing_boundary":
        raw = note(boundary="pre-surprise;Example")
    else:
        raw = note(insert="pre-surprise. Example ")
    assert selected_note_formatting(raw) == (raw, None)


def test_engine_keeps_raw_and_applied_audits_and_only_normalizes_selected_overlong_related_text():
    context, candidate, patch = synthetic_case()
    original = deepcopy((context, candidate, patch))
    calls = []

    async def repair(*args):
        calls.append(1)
        return deepcopy(patch), {"phase": "memory_repair", "synthetic": True}

    result = {"usage": []}
    reply = asyncio.run(
        ShadowEngine(None, SimpleNamespace(repair_memory=repair))._correct_memory(
            {"message_id": "synthetic-note-event"}, context, candidate, result, MOMENT
        )
    )
    audit = result["memory_repair"]
    assert calls == [1] and (context, candidate, patch) == original
    assert audit["response"] == patch and audit["candidate"] == candidate
    assert audit["expanded_response"]["working_memory"] == reply["working_memory"]
    proof = audit["patch_spacing_normalizations"]
    assert len(proof) == 1 and proof[0]["key"] == "w0"
    assert proof[0]["before_bytes"] == 302 and proof[0]["after_bytes"] == 300
    assert proof[0]["method"] == SELECTED_NOTE_FORMAT_VERSION
    for key, text in entries(candidate["working_memory"]).items():
        if key not in {"v0", "w0"}:
            assert entries(reply["working_memory"])[key] == text
    assert reply["working_memory"]["views"][0] == patch["replacements"][0]["text"]
    assert (
        reply["working_memory"]["position_theses"] == candidate["working_memory"]["position_theses"]
    )
    assert all(
        reply[k] == v
        for k, v in candidate.items()
        if k not in {"working_memory", "memory_dispositions"}
    )
    validate_working(reply["working_memory"], MOMENT)


def test_unrelated_scope_never_uses_new_helper_or_rescues_overlong_text():
    context, candidate, patch = synthetic_case()
    candidate["working_memory"]["waiting_for"][0] = note()
    candidate["memory_dispositions"][1]["evidence_ids"] = ["synthetic-other-event"]
    # Without eligible related provenance both oversized entries are mandatory,
    # but the new normalization must still remain disabled.
    assert not repair_budget(candidate["working_memory"]).get("related_edit_group")
    expanded = expand_repair_patch(
        candidate, patch, "synthetic-note-event", before=context["working_memory"]
    )
    assert expanded["working_memory"]["waiting_for"][0] == note()
    with pytest.raises(ShadowError, match="working_memory_bound"):
        validate_working(expanded["working_memory"], MOMENT)


def test_valid_selected_text_is_exact_and_contract_schema_unchanged():
    context, candidate, patch = synthetic_case()
    patch["replacements"][1]["text"] = note(size=300)
    raw = deepcopy(patch)
    expanded = expand_repair_patch(
        candidate, patch, "synthetic-note-event", before=context["working_memory"]
    )
    assert expanded["working_memory"]["waiting_for"][0] == raw["replacements"][1]["text"]
    Contracts().validate("memory-repair", expanded)
    transition(
        candidate["working_memory"],
        expanded["working_memory"],
        expanded["candidate_dispositions"],
        MOMENT,
        initial=True,
    )


def test_no_partial_normalization_when_another_selected_entry_still_overflows():
    context, candidate, patch = synthetic_case()
    patch["replacements"][0]["text"] = "x" * 311
    expanded = expand_repair_patch(
        candidate, patch, "synthetic-note-event", before=context["working_memory"]
    )
    assert expanded["working_memory"]["waiting_for"][0] == note()
    with pytest.raises(ShadowError, match="working_memory_bound"):
        validate_working(expanded["working_memory"], MOMENT)


def test_no_new_normalization_when_complete_serialized_working_state_would_overflow():
    from test_shadow_repair_related import EVENT, related_case

    context, candidate, patch = related_case()
    theses = [
        {
            "instrument": "EUR/USD",
            "as_of": MOMENT.isoformat(),
            "thesis": "Synthetic exact confirmed thesis;" + "t" * 160,
        }
    ]
    context["working_memory"]["position_theses"] = deepcopy(theses)
    candidate["working_memory"]["position_theses"] = deepcopy(theses)
    assert len(encoded(candidate["working_memory"]).encode()) <= 5000
    patch["replacements"][0]["text"] = "\\" * 300
    patch["replacements"][1]["text"] = note()
    expanded = expand_repair_patch(candidate, patch, EVENT, before=context["working_memory"])
    assert len(encoded(expanded["working_memory"]).encode()) > 5000
    assert expanded["working_memory"]["waiting_for"][5] == note()
    assert expanded["working_memory"]["position_theses"] == theses
    with pytest.raises(ShadowError, match="working_memory_bound"):
        validate_working(expanded["working_memory"], MOMENT)
