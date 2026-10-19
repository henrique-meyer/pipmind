import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pipmind.shadow.common import encoded, now
from pipmind.shadow.context import event_query, phase_context, record_view
from pipmind.shadow.model import FoundryModel, ModelFailure
from pipmind.shadow.store import metadata


def row(identifier, part="journal", body=None):
    return {
        "metadata": metadata(identifier, part, "2026-09-15T00:00:00Z", "synthetic"),
        "body": encoded(body) if isinstance(body, dict) else body,
    }


def legacy():
    return row(
        "old-decision",
        body={
            "event_id": "old-print",
            "status": "observed",
            "context": {"recent_journal": "AUDIT_ONLY_" * 10000},
            "usage": [{"request": "AUDIT_ONLY_" * 10000}],
            "triage": {"wake": True, "summary": "Inflation surprise"},
            "reply": {
                "action": "hold",
                "reasoning": "USD inflation increased; wait for confirmation",
                "evidence_ids": ["old-print"],
                "working_memory": {"views": ["AUDIT_ONLY_"]},
            },
        },
    )


def context():
    return {
        "identity": "Standing mandate",
        "playbook": {"version": "v1", "text": "Standing rules"},
        "working_memory": {
            "views": ["Month-old inflation shift is still active"],
            "position_theses": [
                {
                    "instrument": "EUR/USD",
                    "thesis": "Await confirmation",
                    "as_of": "2026-09-15T00:00:00Z",
                }
            ],
            "waiting_for": ["Contrary inflation evidence"],
        },
        "broker_snapshots": {"positions": {"positions": [{"instrument": "EUR/USD", "size": 1}]}},
        "feed_status": {"coverage": "unverified"},
        "recent_journal": [legacy()],
        "retrieved_memories": [legacy()],
    }


def test_projection_removes_recursive_prompts_without_mutating_audit_and_preserves_facts():
    original = legacy()
    saved = deepcopy(original)
    card = record_view(original["metadata"], original["body"])
    assert original == saved
    assert "AUDIT_ONLY_" not in encoded(card)
    assert "inflation increased" in card["body"] and "old-print" in card["body"]
    assert card["partial"] and card["metadata"]["ts"].startswith("2026-09-15")
    assert len(encoded(card).encode()) < 1500


def test_phase_views_keep_continuing_state_without_resending_history_to_triage():
    source = context()
    triage, decision = [phase_context(source, phase) for phase in ("triage", "decision")]
    for view in (triage, decision):
        assert view["working_memory"] == source["working_memory"]
        assert view["broker_snapshots"] == source["broker_snapshots"]
        assert view["coverage"]["age_cutoff"] is None
        assert "AUDIT_ONLY_" not in encoded(view)
    assert "recent_journal" not in triage and "retrieved_memories" not in triage
    assert decision["retrieved_memories"][0]["metadata"]["record_id"] == "old-decision"
    assert decision["recent_journal"] == []  # Same evidence only once.


@pytest.mark.parametrize("phase", ["triage", "decision"])
def test_expanded_feed_facts_fit_context_without_losing_types_values_or_series(phase):
    source = context()
    quality = {
        "point_in_time_safe": False,
        "source_monitoring_complete": False,
        "source_freshness": "monitoring_unknown",
        "known_source_gap": False,
        "is_fallback": False,
        "is_proxy": False,
        "is_derived": False,
        "is_official": True,
        "has_assumed_release_times": False,
        "unknown_publication_time_count": 0,
        "unverified_vintage_count": 1,
        "requested_window_has_data": True,
        "publication_time_status": "unverified",
        "publication_time_precision": "minute",
        "page_includes_latest_available": True,
        "quality_scope": "full_result",
        "row_count": 1,
        "stale_after_days": 95,
        "bounded_release_time_count": 0,
        "missing_announcement_datetime_count": 0,
    }
    coverage = {
        f"CUR:indicator{i}": {
            "complete_history_proven": False,
            "status": "retained_window_captured",
            "checked_at": f"2026-10-18T00:00:{i:02}Z",
            "quality": {
                **quality,
                "source_name": f"Synthetic publisher {i}",
                "latest_available_date": "2026-09-30",
            },
        }
        for i in range(33)
    }
    # Values equal under Python's loose equality must remain distinct JSON facts.
    coverage["CUR:indicator0"]["quality"]["unknown_publication_time_count"] = False
    coverage["CUR:indicator1"]["quality"]["known_source_gap"] = True
    coverage["CUR:indicator2"]["quality"]["source_name"] = "naïve ≠ unsafe"
    source["feed_status"]["coverage"] = coverage
    saved = deepcopy(source)
    assert len(encoded(coverage).encode()) > 16000
    projected = phase_context(source, phase)
    packed = projected["feed_status"]["coverage"]
    assert packed["encoding"] == "coverage-table-v1"

    def overlay(base, delta):
        for index, value in delta.items():
            path = packed["columns"][int(index)]
            target = base
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = deepcopy(value)
        return base

    restored = {
        k: overlay(deepcopy(packed["defaults"]), delta) for k, delta in packed["series"].items()
    }
    assert encoded(restored) == encoded(coverage) and source == saved
    assert projected["working_memory"] == source["working_memory"]
    assert projected["broker_snapshots"] == source["broker_snapshots"]
    assert len(encoded(projected).encode()) < (16000 if phase == "triage" else 28000)


def test_long_article_selects_a_relevant_passage_beyond_the_leading_excerpt():
    article = (
        "Unrelated opening paragraphs. " * 300
        + "Inflation fell, contradicting the earlier outlook. " * 20
    )
    card = record_view(
        row("older-article", "memory", article)["metadata"], article, "inflation outlook"
    )
    assert "Inflation fell" in card["body"] and card["partial"]
    assert len(card["body"]) <= 2400


def test_cut_legacy_json_fragment_cannot_reintroduce_a_historical_prompt():
    source = legacy()
    card = record_view(source["metadata"], source["body"][:8000])
    assert "AUDIT_ONLY_" not in encoded(card)
    assert "requires_source_lookup" in card["body"] and card["partial"]


def test_unstructured_archival_journal_cannot_break_context_projection():
    source = row(
        "unstructured", body={"reply": "action", "triage": ["summary"], "context": "AUDIT_ONLY_"}
    )
    card = record_view(source["metadata"], source["body"])
    assert "AUDIT_ONLY_" not in encoded(card)


def test_retrieval_query_uses_event_meaning_not_json_fields_timestamps_or_ids():
    query = event_query(
        {
            "payload": {
                "currency": "USD",
                "headline": "Inflation surprise",
                "received_at": "2026-10-18T00:00:00Z",
                "event_id": "irrelevant-id",
                "url": "https://example.invalid",
            }
        }
    )
    assert "USD" in query and "Inflation" in query
    assert "received_at" not in query and "irrelevant-id" not in query


def test_wire_context_is_compact_audited_and_tools_cannot_reinflate_old_traces():
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured.update(message=message, options=options)
            return SimpleNamespace(
                text='{"wake":false,"summary":"Observed"}', usage_details={"input_token_count": 123}
            )

    def factory(**kw):
        captured.update(kw)
        return Agent()

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    _, usage = asyncio.run(model.triage({"message_id": "current"}, context(), None, now()))
    assert "AUDIT_ONLY_" not in captured["message"]
    assert "recent_journal" not in captured["message"]
    assert "Standing mandate" not in captured["message"]  # Present once in instructions.
    assert captured["instructions"].count("Standing mandate") == 1
    assert "one sentence of at most 240 characters" in captured["instructions"]
    assert "Do not restate the mind" in captured["instructions"]
    assert captured["options"]["max_tokens"] == 512
    assert usage["request"]["instructions"] == captured["instructions"]
    assert usage["prompt_bytes"] < 6000 and usage["input_tokens"] == 123

    async def decide(message, *, options):
        assert "one sentence of at most 240 characters" not in captured["instructions"]
        tools = {fn.__name__: fn for fn in captured["tools"]}
        assert set(tools) == {"search_memory"}
        assert "Month-old inflation shift" in message
        assert "AUDIT_ONLY_" not in tools["search_memory"]("inflation")
        return SimpleNamespace(
            text=encoded(
                {
                    "action": "hold",
                    "reasoning": "Observed",
                    "evidence_ids": [],
                    "working_memory": {"views": [], "position_theses": [], "waiting_for": []},
                    "tool_calls": [],
                }
            )
        )

    Agent.run = staticmethod(decide)
    asyncio.run(
        model.decide({}, context(), {}, SimpleNamespace(search=lambda *args: [legacy()]), now())
    )


def test_oversized_mandatory_state_is_refused_without_truncation_or_model_request():
    called = []
    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test",
        "t",
        "d",
        enabled=True,
        factory=lambda **kw: called.append(kw),
    )
    with pytest.raises(ModelFailure) as error:
        asyncio.run(model.triage({}, {"identity": "x" * 17000}, None, now()))
    assert not called and error.value.trace["attempted"] is False
    assert error.value.trace["request"]["instructions"].endswith("x" * 17000 + "\n")


def test_larger_continuing_state_remains_complete_and_does_not_hit_the_old_guard():
    captured = {}

    class Agent:
        async def run(self, message, *, options):
            captured["message"] = message
            return SimpleNamespace(text='{"wake":false,"summary":"Observed"}')

    model = FoundryModel(
        "https://synthetic.invalid/api/projects/test",
        "t",
        "d",
        enabled=True,
        factory=lambda **kw: Agent(),
    )
    state = {
        "views": ["Continuing view " * 750],
        "position_theses": [],
        "waiting_for": ["New evidence"],
    }
    _, usage = asyncio.run(model.triage({}, {"working_memory": state}, None, now()))
    assert state["views"][0] in captured["message"]
    assert 10000 < usage["prompt_bytes"] < 16000
