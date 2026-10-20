"""Bounded model views of retained evidence; SQL audit records are never rewritten."""

import re
from copy import deepcopy

from pipmind.contracts import ContractError, loads
from pipmind.shadow.common import ShadowError, encoded
from pipmind.shadow.working import factor_working

CONTEXT_VERSION = "selected-evidence-v5"
STOP = frozenset(
    (
        "a an and are as at be by for from has have in is it of on or "
        "that the this to was were with"
    ).split()
)


def factor_coverage(coverage):
    """Lossless defaults and indexed field paths for repeated per-series facts."""
    if (
        not isinstance(coverage, dict)
        or not coverage
        or not all(isinstance(row, dict) for row in coverage.values())
    ):
        return coverage

    def factor(rows):
        shared = {}
        for key in sorted(set.intersection(*(set(row) for row in rows))):
            values = [row[key] for row in rows]
            if all(encoded(value) == encoded(values[0]) for value in values[1:]):
                shared[key] = values[0]
                for row in rows:
                    row.pop(key)
            elif all(isinstance(value, dict) for value in values):
                defaults = factor(values)
                if defaults:
                    shared[key] = defaults
                    for row in rows:
                        if not row[key]:
                            row.pop(key)
        return shared

    series = deepcopy(coverage)
    defaults = factor(list(series.values()))

    def leaves(value, path=()):
        for key, child in value.items():
            if isinstance(child, dict) and child:
                yield from leaves(child, (*path, key))
            else:
                yield (*path, key), child

    flattened = {key: dict(leaves(row)) for key, row in series.items()}
    columns = sorted({path for row in flattened.values() for path in row})
    indexes = {path: str(i) for i, path in enumerate(columns)}
    candidate = {
        "encoding": "coverage-table-v1",
        "defaults": defaults,
        "columns": [list(path) for path in columns],
        "series": {
            key: {indexes[path]: value for path, value in row.items()}
            for key, row in flattened.items()
        },
    }
    return (
        candidate
        if len(encoded(candidate).encode()) < len(encoded(coverage).encode())
        else coverage
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
    except (ContractError, RecursionError):
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
    except (ContractError, RecursionError):
        data = None
    partial = False
    if (
        isinstance(data, dict)
        and data.get("kind") == "calendar-snapshot-v1"
        and isinstance(data.get("snapshot"), dict)
        and isinstance(data["snapshot"].get("rows"), list)
        and len(data["snapshot"]["rows"]) <= 5000
    ):
        # Index the reference, not a week's keywords into every archive search.
        # Exact-ID native reads can request currency/title details with a focus.
        snapshot = data["snapshot"]
        rows = snapshot["rows"]
        selected = []
        invalid = False
        if query:
            from pipmind.feeds.macro import calendar_date

            try:
                observed = calendar_date(meta["ts"])
                words = set(query_terms_all(query))
                selected = sorted(
                    rows,
                    key=lambda row: (
                        len(words & set(query_terms_all(encoded(row)))),
                        calendar_date(row["date"]) >= observed,
                        -abs((calendar_date(row["date"]) - observed).total_seconds()),
                    ),
                    reverse=True,
                )[:6]
            except (KeyError, TypeError, ValueError):
                invalid = True
        text = encoded(
            {
                "kind": data["kind"],
                "source": data.get("source", "unknown"),
                "scope": data.get("scope", "Unverified calendar evidence; not actual releases."),
                "publication_time": None,
                "fetched_at": snapshot.get("fetched_at"),
                "rows": selected,
                "total_rows": len(rows),
                "details": "search_memory with this record_id | currency and event title",
                "invalid_rows": invalid,
            }
        )
        partial = len(selected) < len(rows)
    elif meta["part"] == "journal" and data is None and body.lstrip().startswith("{"):
        # Old prompt snapshots may contain a cut JSON audit record. Its raw
        # prefix is not a narrative; resolve the original SQL record instead.
        text = encoded({"status": "legacy_fragment_requires_source_lookup"})
        partial = True
    elif meta["part"] == "journal" and isinstance(data, dict):
        view = {key: data[key] for key in ("event_id", "ts", "status", "error") if key in data}
        triage = data.get("triage") or {}
        if isinstance(triage, dict) and triage:
            view["triage"] = {
                key: clipped(triage[key], 800) for key in ("wake", "summary") if key in triage
            }
        reply = data.get("decision") or data.get("reply") or {}
        if isinstance(reply, dict) and reply:
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
    elif (
        isinstance(data, dict)
        and data.get("kind") == "working-memory-transition"
        and isinstance(data.get("before"), dict)
    ):
        rows = [
            {"key": f"{prefix}{i}", "before": value}
            for field, prefix in (("views", "v"), ("waiting_for", "w"))
            for i, value in enumerate(data["before"][field])
        ]
        words = set(query_terms_all(query))
        rows.sort(
            key=lambda row: (
                row["key"] in query.split(),
                len(words & set(query_terms_all(row["before"]))),
            ),
            reverse=True,
        )
        selected = rows[:2]
        for row in selected:
            row["disposition"] = next(
                (d for d in data["dispositions"] if d["from"] == row["key"]), None
            )
        text = encoded(
            {
                "kind": data["kind"],
                "as_of": data["as_of"],
                "previous_checkpoint": data.get("previous_checkpoint"),
                "entries": selected,
                "total_entries": len(rows),
            }
        )
        partial = True
    elif isinstance(data, dict) and data.get("kind") == "working-memory-transition":
        # SQL search already returns selected cards. Request assembly and native
        # tools may project them again; never interpret a card as the raw record.
        text = encoded(
            {
                key: data[key]
                for key in ("kind", "as_of", "previous_checkpoint", "entries", "total_entries")
                if key in data
            }
        )
        partial = True
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
        "memory_policy",
        "reconsideration",
    )
    view = {key: deepcopy(context[key]) for key in keys if key in context}
    if "working_memory" in view:
        view["working_memory"] = factor_working(view["working_memory"])
    if isinstance(view.get("feed_status"), dict) and "coverage" in view["feed_status"]:
        view["feed_status"]["coverage"] = factor_coverage(view["feed_status"]["coverage"])
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
