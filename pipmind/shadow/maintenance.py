"""Bounded current state, with explicit dispositions instead of silent clipping."""

import re
from copy import deepcopy
from functools import lru_cache

from pipmind.contracts import ContractError, Contracts, utc
from pipmind.shadow.common import ShadowError, digest, encoded

POLICY = "bounded-working-v1"
MAX_BYTES = 3200
MAX_ENTRIES = 8
MAX_TEXT_BYTES = 300


def punctuation_spacing(text):
    if any(mark in text for mark in "'\"`"):
        return text
    return re.sub(r"([;:]) (?=[A-Za-z])", r"\1", text)


def repair_budget(working):
    """Calculate a text-only repair; never clip state or alter its topology.

    JSON-escaped content bytes include quotes/backslashes that UTF-8 text size
    alone misses. Budgets guide the single correction; final strict validation
    remains authoritative even if an individual target budget is missed.
    """
    sizes = working_sizes(working)
    if any(
        len(working[field]) > MAX_ENTRIES or len(set(working[field])) != len(working[field])
        for field in ("views", "waiting_for")
    ):
        return None
    texts = entries(working)
    content = {key: len(encoded(text).encode()) - 2 for key, text in texts.items()}
    mandatory = [key for key, size in sizes["entry_bytes"].items() if size > MAX_TEXT_BYTES]

    def plan(keys, cap):
        fixed = sizes["total_bytes"] - sum(content[key] for key in keys)
        maximum = (
            min(MAX_TEXT_BYTES, max(1, (MAX_BYTES - fixed - 100) // len(keys)))
            if keys
            else MAX_TEXT_BYTES
        )
        target = min(cap, maximum)
        return {
            "editable_keys": keys,
            "fixed_bytes": fixed,
            "target_json_text_bytes": {key: target for key in keys},
            "max_json_text_bytes": {key: maximum for key in keys},
            "planned_total_bytes": fixed + len(keys) * target,
            "hard_total_bytes": MAX_BYTES,
        }

    if sizes["total_bytes"] <= MAX_BYTES:
        return plan(mandatory, 220) if mandatory else None
    ranked = sorted(content, key=lambda key: (-content[key], key))
    for cap in (220, 200, 180, 160, 120):
        selected = list(mandatory)
        budget = plan(selected, cap)
        for key in ranked:
            if budget["planned_total_bytes"] <= MAX_BYTES - 400:
                break
            if key not in selected and content[key] > cap:
                selected.append(key)
                budget = plan(selected, cap)
        if selected and budget["planned_total_bytes"] <= MAX_BYTES - 400:
            return budget
    return None  # Fixed theses/topology need the existing full-consolidation path.


def repair_scope(working):
    budget = repair_budget(working)
    return budget["editable_keys"] if budget else []


def expand_repair_patch(candidate, patch, event_ref, *, before=None):
    """Expand only the named edits; preserve the decision's BEFORE mappings."""
    original = candidate["working_memory"]
    scope = repair_scope(original)
    rows = patch["replacements"]
    edits = {row["from"]: row for row in rows}
    if not scope or len(edits) != len(rows) or set(edits) != set(scope):
        raise ShadowError("working_memory_repair_scope")
    after = deepcopy(original)
    for key, row in edits.items():
        field = "views" if key[0] == "v" else "waiting_for"
        text = row["text"]
        after[field][int(key[1:])] = (
            punctuation_spacing(text) if len(text.encode()) > MAX_TEXT_BYTES else text
        )
    if len(encoded(after).encode()) > MAX_BYTES:
        # A model can miss the total by a few formatting bytes even though
        # every entry fits. Compact only the explicitly selected patch texts;
        # leave every unselected entry literal and retain the raw patch audit.
        for key in edits:
            field = "views" if key[0] == "v" else "waiting_for"
            index = int(key[1:])
            after[field][index] = punctuation_spacing(after[field][index])
    proposed = entries(original)
    dispositions = deepcopy(candidate.get("memory_dispositions", []))
    if before is not None:
        old = entries(before)
        mapped = {row["from"] for row in dispositions}
        for row in dispositions:
            edit = edits.get(row["to"])
            if edit and entries(after)[row["to"]] != proposed[row["to"]]:
                if row["disposition"] == "retained":
                    row["disposition"] = "updated"
                    row["reason"] = edit["reason"]
                row["evidence_ids"].extend(
                    ref for ref in edit["evidence_ids"] if ref not in row["evidence_ids"]
                )
        for key, text in old.items():
            if key in mapped or text in after["views" if key[0] == "v" else "waiting_for"]:
                continue
            # An omitted original mapping is legitimate only for a literal
            # candidate carry-forward. Never invent provenance for another edit.
            target = next(
                (k for k, value in proposed.items() if k[0] == key[0] and value == text), None
            )
            if target in edits:
                edit = edits[target]
                dispositions.append(
                    {
                        "from": key,
                        "to": target,
                        "disposition": "updated",
                        "reason": edit["reason"],
                        "evidence_ids": deepcopy(edit["evidence_ids"]),
                    }
                )
    mappings = []
    for key in proposed:
        edit = edits.get(key)
        mappings.append(
            {
                "from": key,
                "to": key,
                "disposition": "updated" if edit else "retained",
                "reason": edit["reason"] if edit else "Exact candidate entry carried forward",
                "evidence_ids": edit["evidence_ids"] if edit else [event_ref],
            }
        )
    return {
        "working_memory": after,
        "memory_dispositions": dispositions,
        "candidate_dispositions": mappings,
    }


def spacing_repair(before, candidate, event_ref, moment):
    """Try punctuation spacing only: no words, punctuation or numbers change.

    Leave quoted/code-like text and numeric colon separators untouched. This
    representation-only repair is used only if all strict transitions qualify.
    """
    edits = []
    for key in repair_scope(candidate["working_memory"]):
        text = entries(candidate["working_memory"])[key]
        compact = punctuation_spacing(text)
        if compact == text or len(compact.encode()) > MAX_TEXT_BYTES:
            return None
        edits.append(
            {
                "from": key,
                "text": compact,
                "reason": "Optional punctuation spacing removed; lexical content unchanged",
                "evidence_ids": [event_ref],
            }
        )
    if not edits:
        return None
    patch = {"replacements": edits}
    expanded = expand_repair_patch(candidate, patch, event_ref, before=before)
    try:
        transition(before, expanded["working_memory"], expanded["memory_dispositions"], moment)
        transition(
            candidate["working_memory"],
            expanded["working_memory"],
            expanded["candidate_dispositions"],
            moment,
            initial=True,
        )
    except (ShadowError, ContractError, KeyError):
        return None
    return patch


def working_sizes(working):
    """Exact UTF-8 sizes, including JSON overhead, without duplicating state text."""
    return {
        "total_bytes": len(encoded(working).encode()),
        "entry_bytes": {key: len(text.encode()) for key, text in entries(working).items()},
    }


@lru_cache(maxsize=1)
def contracts():
    return Contracts()


def entries(working):
    return {
        f"{prefix}{i}": text
        for field, prefix in (("views", "v"), ("waiting_for", "w"))
        for i, text in enumerate(working[field])
    }


def validate_working(working, moment):
    contracts().validate("working-memory", working)
    if len(encoded(working).encode()) > MAX_BYTES or any(
        len(working[field]) > MAX_ENTRIES
        or len(set(working[field])) != len(working[field])
        or any(len(text.encode()) > MAX_TEXT_BYTES for text in working[field])
        for field in ("views", "waiting_for")
    ):
        raise ShadowError("working_memory_bound")
    if any(utc(row["as_of"]) > moment for row in working["position_theses"]):
        raise ShadowError("working_memory_future")


def transition(before, after, dispositions, moment, *, initial=False):
    """Return evidence that SQL must verify in the same commit as the update.

    Dispositions are mandatory for every changed/removed entry (all entries on
    initial activation). A replacement must exist in the new state; conditions
    cannot be archived as resolved without an explicit evidence-backed reason.
    These structural checks do not claim to prove semantic fidelity.
    """
    validate_working(after, moment)
    contracts().validate("memory-dispositions", dispositions)
    old, new = entries(before), entries(after)
    needed = {
        key
        for key, text in old.items()
        if initial or text not in after["views" if key[0] == "v" else "waiting_for"]
    }
    mapping = {row["from"]: row for row in dispositions}
    if len(mapping) != len(dispositions) or not needed <= set(mapping) <= set(old):
        raise ShadowError("working_memory_dispositions")
    refs = set()
    for key, row in mapping.items():
        target = row["to"]
        if target is not None and (target not in new or target[0] != key[0]):
            raise ShadowError("working_memory_replacement")
        if key not in needed and (
            row["disposition"] != "retained" or target is None or new[target] != old[key]
        ):
            raise ShadowError("working_memory_dispositions")
        if row["disposition"] == "retained" and (target is None or new[target] != old[key]):
            raise ShadowError("working_memory_replacement")
        if key[0] == "w" and target is None and row["disposition"] != "resolved":
            raise ShadowError("working_memory_unresolved_condition")
        if (target is None) != (row["disposition"] in {"archived", "resolved"}):
            raise ShadowError("working_memory_replacement")
        refs.update(row["evidence_ids"])
    old_theses = {row["instrument"] for row in before["position_theses"]}
    if not old_theses <= {row["instrument"] for row in after["position_theses"]}:
        raise ShadowError("working_memory_missing_thesis")
    if initial and before["position_theses"] != after["position_theses"]:
        raise ShadowError("working_memory_initial_theses")
    if len(refs) > 256 or len(encoded(dispositions).encode()) > 32768:
        raise ShadowError("working_memory_bound")
    return refs


def policy_context(policy):
    return {
        "version": policy["version"],
        "max_bytes": MAX_BYTES,
        "max_entries_per_list": MAX_ENTRIES,
        "max_text_bytes": MAX_TEXT_BYTES,
        "checkpoint": policy["checkpoint"],
        "origin_checkpoint": policy["origin_checkpoint"],
        "as_of": policy["as_of"],
        "entry_keys": "v0,v1,... for current views; w0,w1,... for current waiting_for",
    }


def working_digest(working):
    return digest(encoded(working))
