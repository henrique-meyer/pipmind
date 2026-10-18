"""Bounded model views of retained evidence; SQL audit records are never rewritten."""

import re
from copy import deepcopy

from pipmind.contracts import ContractError, loads
from pipmind.shadow.common import ShadowError, encoded

CONTEXT_VERSION = "selected-evidence-v1"
STOP = frozenset(
    (
        "a an and are as at be by for from has have in is it of on or "
        "that the this to was were with"
    ).split()
)


def query_terms(text):
    # Retrieval vocabulary, not a trading/wake filter. Preserve query order.
    words = re.findall(r"[a-z][a-z0-9]{1,39}", text.casefold())
    return list(dict.fromkeys(word for word in words if word not in STOP))[:12]


def event_query(event):
    payload = event.get("payload", {})
    fields = (
        "instrument",
        "currency",
        "currencies",
        "indicator",
        "headline",
        "text",
        "summary",
        "role",
        "reason",
    )
    return " ".join(encoded(payload[key]) for key in fields if key in payload)[:4000]


def clipped(text, limit):
    return text[:limit] if isinstance(text, str) else text


def excerpt(text, query, limit):
    if len(text) <= limit or not query:
        return text[:limit]
    words = set(query_terms(query))
    windows = [text[start : start + limit] for start in range(0, len(text), limit // 2)]
    return max(windows, key=lambda window: len(words & set(query_terms_all(window))))


def query_terms_all(text):
    return re.findall(r"[a-z][a-z0-9]{1,39}", text.casefold())


def semantic_text(body):
    try:
        data = loads(body)
    except ContractError:
        return body

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    return " ".join(strings(data))


def record_view(meta, body, query=""):
    """Extract meaning before clipping. Never return historical context/tools/usage."""
    try:
        data = loads(body)
    except ContractError:
        data = None
    partial = False
    if meta["part"] == "journal" and data is None and body.lstrip().startswith("{"):
        # Old prompt snapshots may contain a cut JSON audit record. Its raw
        # prefix is not a narrative; resolve the original SQL record instead.
        text = encoded({"status": "legacy_fragment_requires_source_lookup"})
        partial = True
    elif meta["part"] == "journal" and isinstance(data, dict):
        view = {key: data[key] for key in ("event_id", "ts", "status", "error") if key in data}
        triage = data.get("triage") or {}
        if triage:
            view["triage"] = {
                key: clipped(triage[key], 800) for key in ("wake", "summary") if key in triage
            }
        reply = data.get("decision") or data.get("reply") or {}
        if reply:
            view["decision"] = {
                key: excerpt(reply[key], query, 1200) if isinstance(reply[key], str) else reply[key]
                for key in ("action", "instrument", "side", "size", "reasoning", "evidence_ids")
                if key in reply
            }
        text = encoded(view)
        partial = True  # A semantic projection, never the complete audit trace.
    elif isinstance(data, dict) and "kind" in data and isinstance(data.get("payload"), dict):
        # Original normalized event evidence; never an embedded journal context.
        data = deepcopy(data)
        for key in ("summary", "text", "headline"):
            value = data["payload"].get(key)
            if isinstance(value, str) and len(value) > 1200:
                data["payload"][key] = excerpt(value, query, 1200)
                partial = True
        text = encoded(
            {
                key: data[key]
                for key in ("kind", "message_id", "available_at", "payload")
                if key in data
            }
        )
    else:
        text = excerpt(body, query, 2400)
        partial = len(text) < len(body)
    limit = 2400
    partial = partial or len(text) > limit
    return {"metadata": deepcopy(meta), "body": text[:limit], "partial": partial}


def relevance(row, query):
    words = set(query_terms(query))
    content = set(query_terms_all(semantic_text(row["body"])))
    return len(words & content)


def phase_context(context, phase):
    # Keep mandatory current state intact. Oversize state is refused, not silently lost.
    keys = (
        "revision",
        "paused",
        "identity",
        "playbook",
        "working_memory",
        "broker_snapshots",
        "feed_status",
        "price_aggregate",
    )
    view = {key: deepcopy(context[key]) for key in keys if key in context}
    view["context_version"] = CONTEXT_VERSION
    view["coverage"] = {
        "history": "retained in SQL; selected evidence is not exhaustive",
        "age_cutoff": None,
        "missing_evidence": "unknown, not evidence of absence",
    }
    if phase == "decision":
        remaining = 8000
        seen = set()
        candidates = omitted = 0
        for key in ("retrieved_memories", "recent_journal"):
            selected = []
            for row in context.get(key, []):
                meta = row.get("metadata")
                if not meta:
                    continue
                card = record_view(meta, row["body"])
                card["partial"] = card["partial"] or row.get("partial", False)
                identifier = meta["record_id"]
                size = len(encoded(card).encode())
                candidates += 1
                if identifier not in seen and size <= remaining:
                    selected.append(card)
                    seen.add(identifier)
                    remaining -= size
                elif identifier not in seen:
                    omitted += 1
            view[key] = selected
        view["coverage"].update(candidate_cards=candidates, budget_omissions=omitted)
    elif phase != "triage":
        raise ShadowError("invalid_context_phase")
    return view
