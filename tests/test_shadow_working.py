from copy import deepcopy

import pytest

from pipmind.shadow.common import encoded
from pipmind.shadow.context import phase_context
from pipmind.shadow.working import factor_working


def expand(value):
    if value.get("encoding") != "working-phrases-v1":
        return value
    result = {key: item for key, item in value.items() if key not in {"encoding", "phrases"}}
    for field in ("views", "waiting_for"):
        result[field] = [
            item
            if isinstance(item, str)
            else " ".join(p if isinstance(p, str) else value["phrases"][p] for p in item)
            for item in value[field]
        ]
    return result


def working():
    return {
        "views": [
            f"Synthetic institution headline {i} names an unavailable document. "
            "Its contents remain unavailable in supplied evidence and policy implications "
            "are undetermined; the headline establishes no actual policy change. "
            f"Retain its distinct evidence synthetic-{i} and date 2026-10-{i + 1:02}."
            for i in range(12)
        ],
        "waiting_for": [
            f"Contents and findings of synthetic document {i} before assessing any monetary, "
            "market or operational implications; no absence of evidence proves absence."
            for i in range(12)
        ],
        "position_theses": [
            {
                "instrument": "EUR/USD",
                "thesis": "Keep this exact thesis",
                "as_of": "2026-10-01T00:00:00Z",
            }
        ],
    }


def test_shared_phrases_reconstruct_every_view_wait_and_thesis_without_state_changes():
    original = working()
    before = deepcopy(original)
    factored = factor_working(original)
    assert factored["encoding"] == "working-phrases-v1"
    assert len(encoded(factored).encode()) < len(encoded(original).encode()) * 0.8
    assert expand(factored) == original == before
    assert factored["position_theses"] == original["position_theses"]
    factored["views"].append("Do not mutate the cache or source")
    assert expand(factor_working(original)) == original == before


@pytest.mark.parametrize("value", [" leading  whitespace\n stays\t", "é 東京 naïve ≠", "", "a a a"])
def test_odd_whitespace_unicode_empty_and_duplicate_strings_remain_exact(value):
    original = working()
    original["views"].extend([value, value])
    original["waiting_for"].append(value)
    assert encoded(expand(factor_working(original))) == encoded(original)


def test_both_phases_retain_exact_current_state_with_a_smaller_projection():
    original = working()
    context = {"working_memory": original, "identity": "Immutable mandate"}
    for phase in ("triage", "decision"):
        selected = phase_context(context, phase)
        assert expand(selected["working_memory"]) == original
        assert selected["identity"] == "Immutable mandate"
    assert context["working_memory"] == original


@pytest.mark.parametrize("kind", ["bytes", "entries", "small", "partial"])
def test_bounds_or_small_unprofitable_inputs_fall_back_without_clipping(kind):
    original = working()
    if kind == "bytes":
        original["views"] = ["Retain every character " * 2000]
    elif kind == "entries":
        original["views"] = ["Keep every unresolved view"] * 65
    elif kind == "small":
        original = {"views": ["Current view"], "waiting_for": [], "position_theses": []}
    else:
        original = {"views": ["Partial caller context"]}
    assert factor_working(original) == original
