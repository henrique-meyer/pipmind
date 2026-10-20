from copy import deepcopy

import pytest

from pipmind.contracts import ContractError, utc
from pipmind.shadow.common import ShadowError
from pipmind.shadow.maintenance import transition, validate_working

MOMENT = utc("2026-10-19T12:00:00Z")


def state():
    return {
        "views": ["Dated uncertain USD view"],
        "waiting_for": ["Wait for CPI and invalidate on revision"],
        "position_theses": [],
    }


def disposition(key="w0", target="w0", kind="updated"):
    return {
        "from": key,
        "to": target,
        "disposition": kind,
        "reason": "Retains the unresolved CPI and revision prerequisites",
        "evidence_ids": ["dated-source"],
    }


def test_changed_conditions_cannot_disappear_without_a_disposition():
    before = state()
    after = {**before, "waiting_for": []}
    with pytest.raises(ShadowError, match="working_memory_dispositions"):
        transition(before, after, [], MOMENT)
    with pytest.raises(ShadowError, match="working_memory_unresolved_condition"):
        transition(before, after, [disposition(target=None, kind="archived")], MOMENT)
    assert transition(before, after, [disposition(target=None, kind="resolved")], MOMENT) == {
        "dated-source"
    }
    assert before == state()


def test_merges_account_for_every_old_condition_and_target():
    before = state()
    before["waiting_for"].append("Wait for comparable prior data")
    after = {
        **before,
        "waiting_for": ["Wait for CPI, comparable prior data; invalidate on revision"],
    }
    rows = [disposition("w0", kind="merged"), disposition("w1", kind="merged")]
    assert transition(before, after, rows, MOMENT) == {"dated-source"}
    for bad in (rows[:1], rows + rows[:1], [rows[0], disposition("w1", "w8")]):
        with pytest.raises(ShadowError):
            transition(before, after, bad, MOMENT)


def test_initial_disposition_includes_unchanged_entries_and_exact_theses():
    before = state()
    with pytest.raises(ShadowError, match="working_memory_dispositions"):
        transition(before, before, [], MOMENT, initial=True)
    rows = [disposition("v0", "v0", "retained"), disposition(kind="retained")]
    assert transition(before, before, rows, MOMENT, initial=True)
    before["position_theses"] = [
        {"instrument": "EUR/USD", "thesis": "Exact stop condition", "as_of": "2026-10-18T00:00:00Z"}
    ]
    after = deepcopy(before)
    after["position_theses"][0]["thesis"] = "Changed"
    with pytest.raises(ShadowError, match="working_memory_initial_theses"):
        transition(before, after, rows, MOMENT, initial=True)


@pytest.mark.parametrize("change", ["count", "bytes", "unicode", "duplicate", "future"])
def test_oversize_or_invalid_current_state_is_refused_never_clipped(change):
    working = state()
    if change == "count":
        working["views"] = [f"Distinct view {i}" for i in range(9)]
    elif change == "bytes":
        working["position_theses"] = [
            {"instrument": "EUR/USD", "thesis": "x" * 5000, "as_of": "2026-10-18T00:00:00Z"}
        ]
    elif change == "unicode":
        working["waiting_for"] = ["€" * 101]
    elif change == "duplicate":
        working["views"] *= 2
    else:
        working["position_theses"] = [
            {"instrument": "EUR/USD", "thesis": "Future", "as_of": "2026-10-20T00:00:00Z"}
        ]
    before = deepcopy(working)
    with pytest.raises((ShadowError, ContractError)):
        validate_working(working, MOMENT)
    assert working == before


def test_budget_version_keeps_legacy_policy_and_dependency_origin():
    from pipmind.shadow.maintenance import budget_context, policy_context

    legacy = {
        "version": "bounded-working-v1",
        "checkpoint": "old-current",
        "origin_checkpoint": "old-origin",
        "as_of": "2026-10-18T00:00:00Z",
    }
    original = deepcopy(legacy)
    projected = policy_context(legacy)
    assert legacy == original and projected["version"] == legacy["version"]
    assert (
        projected["checkpoint"] == "old-current" and projected["origin_checkpoint"] == "old-origin"
    )
    assert budget_context() == {
        "budget_version": "working-budget-v2",
        "max_bytes": 5000,
        "max_entries_per_list": 8,
        "max_text_bytes": 300,
    }
    assert all(projected[k] == v for k, v in budget_context().items())


@pytest.mark.parametrize("extra", [0, 1])
def test_serialized_budget_boundary_counts_json_escapes_without_clipping(extra):
    from pipmind.shadow.common import encoded

    working = {
        "views": [str(i) + "a" * 299 for i in range(8)],
        "waiting_for": [str(i) + "b" * 299 for i in range(8)],
        "position_theses": [],
    }
    index = 0
    while len(encoded(working).encode()) < 5000 + extra:
        row, col = divmod(index, 300)
        text = working["views"][row]
        working["views"][row] = text[:col] + "\\" + text[col + 1 :]
        index += 1
    original = deepcopy(working)
    assert len(encoded(working).encode()) == 5000 + extra
    if extra:
        with pytest.raises(ShadowError, match="working_memory_bound"):
            validate_working(working, MOMENT)
    else:
        validate_working(working, MOMENT)
    assert working == original
