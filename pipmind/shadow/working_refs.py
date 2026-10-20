"""Lossless wire references to the supplied BEFORE state, never persistent state."""

from copy import deepcopy

from pipmind.shadow.common import ShadowError

VERSION = "working-refs-v1"
SCHEMA_ID = "urn:pipmind:wire:working-refs-v1"
INSTRUCTIONS = (
    " Decision working_memory.views/waiting_for may use integer i to copy the COMPLETE"
    " expanded BEFORE entry at the same-kind index i EXACTLY. Prefer references for"
    " unchanged entries; strings are new/changed text. Return complete ordered arrays."
    " Theses and dispositions keep their ordinary schemas. Limits apply AFTER expansion."
)


def wire_schema(schema):
    result = deepcopy(schema)
    result["$id"] = SCHEMA_ID
    for field in ("views", "waiting_for"):
        result["properties"][field]["items"] = {
            "anyOf": [
                result["properties"][field]["items"],
                {"type": "integer", "minimum": 0, "maximum": 7},
            ]
        }
    return result


def expand_reply(reply, before):
    """Expand literal references only; the caller validates every normative guard."""
    result = deepcopy(reply)
    references = {}
    working = result.get("working_memory")
    if not isinstance(working, dict):
        return result, references  # Let normative validation reject the original shape.
    for field in ("views", "waiting_for"):
        values = working.get(field)
        if not isinstance(values, list):
            continue
        original = before.get(field)
        for index, value in enumerate(values):
            if type(value) is not int:  # bools and floats are never references.
                continue
            if not isinstance(original, list) or not 0 <= value < len(original):
                raise ShadowError("working_memory_reference")
            if not isinstance(original[value], str):
                raise ShadowError("working_memory_reference")
            values[index] = original[value]
            references.setdefault(field, []).append({"after": index, "before": value})
    return result, references
