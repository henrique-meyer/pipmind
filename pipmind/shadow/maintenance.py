"""Bounded current state, with explicit dispositions instead of silent clipping."""

from functools import lru_cache

from pipmind.contracts import Contracts, utc
from pipmind.shadow.common import ShadowError, digest, encoded

POLICY = "bounded-working-v1"
MAX_BYTES = 3200
MAX_ENTRIES = 8
MAX_TEXT_BYTES = 300


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
