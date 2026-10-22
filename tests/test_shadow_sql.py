"""Real SQL Server integration. CI runs these separately with a private local database."""

import asyncio
import os
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from pipmind.contracts import ROOT, loads, utc
from pipmind.portability import BundleError, validate_bundle
from pipmind.shadow.bundle import export_experience, import_experience
from pipmind.shadow.common import ShadowError, digest
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import FixtureModel
from pipmind.shadow.sql import connector, fulltext, migrate
from pipmind.shadow.store import SqlMind, metadata

pytestmark = pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_TESTS") != "true", reason="separate SQL integration gate"
)
MOMENT = utc("2026-10-18T00:00:00Z")
CASE = loads((ROOT / "evals/fixtures/starter-v1.jsonl").read_text().splitlines()[0])
SHADOW_CASES = loads((ROOT / "evals/shadow/scenario-v1.json").read_bytes())["starter_cases"]


@pytest.fixture
def mind():
    connect = connector()
    migrate(connect)
    fulltext(connect)
    store = SqlMind(connect, "test-" + uuid4().hex)
    store.initialize(
        "Immutable identity\nnaïve ≠ unsafe\n",
        {"version": "v1", "text": "# Exact playbook\n"},
        "2026-10-01T00:00:00Z",
        binding=digest("destination-only"),
    )
    return store


def test_expanded_feed_status_retains_raw_facts_before_prompt_factoring(mind):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.context import phase_context

    status = {
        "coverage": {
            f"series:{i}": {"quality": {"detail": "retained fact " * 80}, "value": i}
            for i in range(33)
        },
        "checked_at": MOMENT.isoformat(),
    }
    assert 16384 < len(encoded(status).encode()) < 131072
    mind.set_runtime("feed_status", status)
    assert mind.runtime("feed_status") == status
    projected = phase_context({"feed_status": mind.runtime("feed_status")}, "triage")
    assert len(encoded(projected).encode()) < 16384
    with pytest.raises(ShadowError, match="invalid_runtime_status"):
        mind.set_runtime("feed_status", {"coverage": "x" * 131072})
    assert mind.runtime("feed_status") == status


def test_calendar_snapshots_keep_full_history_and_are_native_search_evidence(mind):
    from pipmind.shadow.calendar import retain_calendar
    from pipmind.shadow.common import encoded

    cached = {
        "fetched_at": MOMENT.isoformat(),
        "rows": [
            {
                "title": "Synthetic future CPI",
                "country": "USD",
                "impact": "High",
                "date": (MOMENT + timedelta(days=1)).isoformat(),
                "forecast": "2.0%",
                "previous": "1.9%",
            }
        ],
    }
    baseline = mind.head()
    first = retain_calendar(mind, cached, MOMENT)
    assert retain_calendar(mind, cached, MOMENT) == first
    second = retain_calendar(
        mind, {**cached, "rows": [{**cached["rows"][0], "forecast": "2.1%"}]}, MOMENT
    )
    assert first["record_id"] != second["record_id"]
    assert mind.head() == baseline  # no working revision, account, or authority mutation
    found = mind.search(first["record_id"] + " | USD CPI", MOMENT)
    assert len(found) == 1 and found[0]["metadata"]["record_id"] == first["record_id"]
    assert '"forecast":"2.0%"' in found[0]["body"]
    assert '"publication_time":null' in found[0]["body"]
    assert encoded(first).count("forecast") == 0  # only a reference enters every prompt
    assert (
        retain_calendar(mind, {**cached, "fetched_at": "2099-01-01T00:00:00Z"}, MOMENT)["status"]
        == "future_snapshot"
    )
    from pipmind.shadow.service import LocalFeedConsumer

    class Feeds:
        def source_id(self):
            return "synthetic-calendar-source"

        def events(self, *args):
            return []

        def state(self, key, default=None):
            return cached if key == "calendar:data" else default

        def status(self):
            return {"quarantined": 0}

    consumer = LocalFeedConsumer(mind, Feeds())
    assert consumer.poll(MOMENT) == 0
    assert mind.runtime("feed_status")["calendar"]["record_id"] == first["record_id"]
    consumer.poll(MOMENT + timedelta(seconds=1))
    assert mind.runtime("feed_status")["calendar"]["record_id"] == first["record_id"]


@pytest.mark.parametrize("case_id", SHADOW_CASES)
def test_starter_cases_through_actual_sql_shadow_runtime(mind, case_id):
    cases = [
        loads(line) for line in (ROOT / "evals/fixtures/starter-v1.jsonl").read_text().splitlines()
    ]
    case = next(row for row in cases if row["case_id"] == case_id)
    initial = case["initial_mind"]
    store = SqlMind(mind.connect, "starter-" + uuid4().hex)
    records = [
        (
            metadata(
                row["record_id"],
                part,
                row["ts"],
                row["source_trader"],
                row["evidence_ids"],
                row["override"],
            ),
            row["body"],
        )
        for part, rows in (("journal", initial["journal"]), ("memory", initial["long_term"]))
        for row in rows
    ]
    store.initialize(
        initial["identity"],
        initial["playbook"],
        case["clock_start"],
        binding=digest("synthetic-starter"),
        records=records,
    )
    responses = deepcopy(case["model_responses"])
    # M1's sequential decision IDs have a different wire representation in SQL.
    causes = list(dict.fromkeys(e["message_id"] for e in case["events"]))
    for step in responses.values():
        if "reply" in step:
            step["reply"]["evidence_ids"] = [
                "outcome:" + digest(causes[int(ref.rsplit("d", 1)[1]) - 1])
                if ref.startswith(case_id + ":d")
                else ref
                for ref in step["reply"]["evidence_ids"]
            ]

    class NativeReads(FixtureModel):
        async def decide(self, event, context, triage, store, moment):
            reply, usage = await super().decide(event, context, triage, store, moment)
            reply = deepcopy(reply)
            reads = {
                "read_mind": lambda: context,
                "search_memory": lambda query: store.search(query, moment),
            }
            usage["tools"] = [
                {**call, "result": reads[call["name"]](**call["arguments"])}
                for call in reply["tool_calls"]
            ]
            reply["tool_calls"] = []
            return reply, usage

    model = NativeReads(responses)
    store.set_paused(False)
    for sequence, event in enumerate(case["events"], 1):
        moment = utc(event["available_at"])
        store.accept("starter", [{"sequence": sequence, "event": event}], moment)
        engine = ShadowEngine(store, model, clock=lambda moment=moment: moment)
        while asyncio.run(engine.step()):
            pass
    outcomes = [store.outcome(identifier) for identifier in causes]
    expected = case["expected"]
    decisions = [row["decision"] for row in outcomes if row["decision"]]
    assert expected["minimum_decisions"] <= len(decisions) <= expected["maximum_decisions"]
    assert all(row["dispatches"] == 0 for row in outcomes)
    assert all(
        row["triage"]["wake"] == expected["wake_labels"][row["event_id"]] for row in outcomes
    )
    assert set(expected["required_evidence"]) <= {
        ref for row in decisions for ref in row["evidence_ids"]
    }
    assert set(expected["memory_views"]) <= set(store.head()["working"]["views"])
    retained = store.retained()[1]
    assert (
        next(body for meta, body in retained if meta["part"] == "identity") == initial["identity"]
    )
    assert (
        next(body for meta, body in retained if meta["part"] == "playbook")
        == initial["playbook"]["text"]
    )
    assert store.status()["outcomes"] == len(causes)


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("formatting_only", [False, True])
def test_sql_total_budget_patch_keeps_history_and_derives_only_literal_mappings(
    mind, failed, formatting_only
):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.maintenance import (
        entries,
        punctuation_spacing,
        repair_budget,
        working_sizes,
    )

    before = {
        "views": [f"FX {i}: " + "\\" * 240 for i in range(8)],
        "waiting_for": [f"Await verified prerequisite {i} " + "b" * 150 for i in range(5)],
        "position_theses": [],
    }
    if formatting_only:
        old = before["waiting_for"][0]
        text = "Await: A; B; C" + "x" * (len(old) - len("Await: A; B; C"))
        before["waiting_for"][0] = '"' + text[1:] if failed else text
    with mind.transaction() as cur:
        mind._insert(
            cur,
            metadata("before-proof", "memory", "2026-10-01T00:00:00Z", mind.trader),
            "Every synthetic prerequisite remains unresolved",
        )
        mind._write_working(cur, before, MOMENT)
    activate_bounded(mind)
    events, model = episode(mind)
    event = events[0]
    candidate = deepcopy(model.responses[event["message_id"]]["reply"])
    candidate.update(
        evidence_ids=[event["message_id"]],
        tool_calls=[],
        memory_dispositions=[],
        working_memory=deepcopy(before),
    )
    candidate["working_memory"]["waiting_for"].append("New dated prerequisite: " + "c" * 190)
    assert working_sizes(candidate["working_memory"])["total_bytes"] > 5000
    assert max(working_sizes(candidate["working_memory"])["entry_bytes"].values()) <= 300
    budget = repair_budget(candidate["working_memory"])
    model.responses[event["message_id"]]["reply"] = candidate
    model.responses[event["message_id"]]["triage"]["wake"] = True
    patch = {
        "replacements": [
            {
                "from": key,
                "text": entries(candidate["working_memory"])[key]
                if failed
                else key + " compact distinct uncertainty",
                "reason": "Retain the same distinct prerequisite",
                "evidence_ids": [event["message_id"]],
            }
            for key in budget["editable_keys"]
        ]
    }
    if formatting_only:
        assert "w0" not in budget["editable_keys"]
        trial = deepcopy(candidate["working_memory"])
        for key in budget["editable_keys"]:
            field = "views" if key[0] == "v" else "waiting_for"
            trial[field][int(key[1:])] = '"' + key + ' "'
        remaining = 5003 - len(encoded(trial).encode())
        patch["replacements"] = []
        for key in budget["editable_keys"]:
            amount = min(2 * (300 - len(key) - 3), remaining)
            remaining -= amount
            patch["replacements"].append(
                {
                    "from": key,
                    "text": '"' + key + " " + "\\" * (amount // 2) + "x" * (amount % 2) + '"',
                    "reason": "Synthetic scoped wording proposal",
                    "evidence_ids": [event["message_id"]],
                }
            )
        assert remaining == 0

    class Repair(FixtureModel):
        calls = 0

        async def repair_memory(self, *args):
            self.calls += 1
            return deepcopy(patch), {
                "phase": "memory_repair",
                "input_tokens": 100,
                "output_tokens": 80,
            }

    live = Repair(model.responses)
    old = mind.retained()[1]
    assert asyncio.run(ShadowEngine(mind, live, clock=lambda: MOMENT).step()) is not failed
    assert live.calls == 1
    reopened = SqlMind(mind.connect, mind.trader)
    if failed:
        assert reopened.head()["paused"] and reopened.head()["working"] == before
        assert reopened.pending() == event and reopened.outcome(event["message_id"]) is None
        journal = [
            loads(body) for meta, body in reopened.retained()[1] if meta["part"] == "journal"
        ][-1]
        assert journal["status"] == "held" and journal["error"] == "working_memory_repair_failed"
    else:
        assert not reopened.head()["paused"] and reopened.pending() is None
        journal = reopened.outcome(event["message_id"])
        after = reopened.head()["working"]
        for key, text in entries(candidate["working_memory"]).items():
            if key not in budget["editable_keys"]:
                assert entries(after)[key] == (
                    punctuation_spacing(text) if formatting_only and key == "w0" else text
                )
        if formatting_only:
            assert working_sizes(after)["total_bytes"] == 5000
            assert [v["key"] for v in journal["memory_repair"]["format_only_normalizations"]] == [
                "w0"
            ]
        assert all(row["to"] is not None for row in journal["reply"]["memory_dispositions"])
        changed = {
            key
            for key, text in entries(before).items()
            if text not in after["views" if key[0] == "v" else "waiting_for"]
        }
        assert {row["from"] for row in journal["reply"]["memory_dispositions"]} == changed
        assert working_sizes(reopened.head()["working"])["total_bytes"] <= 5000
    assert journal["dispatches"] == 0 and journal["memory_repair"]["candidate"] == candidate
    assert journal["memory_repair"]["response"] == patch
    assert journal["memory_repair"]["patch_budget"] == budget
    retained = {meta["record_id"]: (meta, body) for meta, body in reopened.retained()[1]}
    assert all(
        retained[meta["record_id"]] == (meta, body)
        for meta, body in old
        if meta["part"] != "working_memory"
    )


def episode(mind):
    events = deepcopy(CASE["events"])
    mind.accept(
        "fixture-source",
        [{"sequence": i + 1, "event": event} for i, event in enumerate(events)],
        MOMENT,
    )
    mind.set_paused(False)
    return events, FixtureModel(deepcopy(CASE["model_responses"]))


def test_prepared_step_matches_legacy_reads_and_closes_its_single_session(mind, monkeypatch):
    events, model = episode(mind)
    mind.set_runtime("feed_status", {"coverage": "unverified", "checked_at": "first"})
    legacy = mind.context(MOMENT, history=False)
    legacy["broker_snapshots"] = mind.runtime("broker_snapshots", {})
    legacy["feed_status"] = mind.runtime("feed_status", {"coverage": "unverified"})
    counts = {"opened": 0, "active": 0}
    connect = mind.connect

    class Connection:
        def __init__(self, db):
            self.db = db

        def close(self):
            try:
                self.db.close()
            finally:
                counts["active"] -= 1

        def __getattr__(self, key):
            return getattr(self.db, key)

    def tracked(**kwargs):
        db = connect(**kwargs)
        counts["opened"] += 1
        counts["active"] += 1
        return Connection(db)

    monkeypatch.setattr(mind, "connect", tracked)
    assert mind.prepare_step(MOMENT) == (events[0], legacy, None)
    assert counts == {"opened": 1, "active": 0}

    class Inspect(FixtureModel):
        async def triage(self, event, context, store, moment):
            assert counts["active"] == 0  # No transaction/lock spans a model await.
            assert context == legacy
            return {"wake": False, "summary": "Observed"}, {"phase": "triage"}

    assert asyncio.run(ShadowEngine(mind, Inspect(model.responses), clock=lambda: MOMENT).step())
    assert counts["active"] == 0
    assert mind.outcome(events[0]["message_id"])["status"] == "observed"
    assert mind.prepare_step(MOMENT) is None


def test_prepared_step_pause_future_and_changed_facts_do_not_use_a_stale_frame(mind):
    assert mind.prepare_step(MOMENT) is None
    events, _ = episode(mind)
    first = mind.prepare_step(MOMENT)
    mind.set_runtime("feed_status", {"coverage": "new-current-fact"})
    second = mind.prepare_step(MOMENT)
    assert first[0] == second[0] == events[0]
    assert second[1]["feed_status"] == {"coverage": "new-current-fact"}
    mind.set_paused(True)
    assert mind.prepare_step(MOMENT) is None
    mind.set_paused(False)
    before = mind.head()
    with pytest.raises(ShadowError, match="future_shadow_input"):
        mind.prepare_step(utc(events[0]["available_at"]) - timedelta(seconds=1))
    assert mind.head() == before and mind.pending() == events[0]


@pytest.mark.parametrize("invalid", [False, True], ids=["exact-carry", "invalid-index"])
def test_real_sql_wire_reply_preserves_full_state_audit_and_dependency_guards(mind, invalid):
    from types import SimpleNamespace

    from pipmind.shadow.common import encoded
    from pipmind.shadow.model import FoundryModel

    events, model = episode(mind)
    first = events[0]
    working = {
        "views": ["USD CPI reported 2.5%; vintage remains unverified."],
        "waiting_for": ["EACH official full text and comparable prior remain required."],
        "position_theses": [],
    }
    model.responses[first["message_id"]]["triage"]["wake"] = True
    model.responses[first["message_id"]]["reply"]["working_memory"] = working
    assert asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    moment = MOMENT + timedelta(seconds=1)
    activate_bounded(mind, moment)
    original = mind.retained()[1]
    event = deepcopy(first)
    event["message_id"] = event["payload"]["event_id"] = "wire-next"
    moment += timedelta(seconds=1)
    event["available_at"] = moment.isoformat()
    mind.accept("wire", [{"sequence": 1, "event": event}], moment)
    mind.set_paused(False)
    wire = {
        "action": "hold",
        "reasoning": "No new verified evidence resolves these prerequisites.",
        "evidence_ids": ["wire-next"],
        "tool_calls": [],
        "working_memory": {
            "views": [99 if invalid else 0],
            "waiting_for": [0],
            "position_theses": [],
        },
        "memory_dispositions": [],
    }
    calls = []

    def factory(phase, **kwargs):
        class Agent:
            async def run(self, message, **options):
                calls.append(phase)
                value = (
                    {"wake": True, "summary": "Inspect current evidence"}
                    if phase == "triage"
                    else wire
                )
                return SimpleNamespace(
                    text=encoded(value),
                    usage_details={"input_token_count": 100, "output_token_count": 20},
                )

        return Agent()

    live = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    progressed = asyncio.run(ShadowEngine(mind, live, clock=lambda: moment).step())
    assert calls == ["triage", "decision"] and progressed is not invalid
    assert mind.head()["working"] == working
    if invalid:
        assert mind.head()["paused"] and mind.pending() == event
        assert mind.outcome("wire-next") is None
        assert mind.runtime("reasoning_hold")["reason"] == "working_memory_reference"
    else:
        result = mind.outcome("wire-next")
        assert result["reply"]["working_memory"] == working
        assert result["usage"][-1]["wire_response"] == wire
        assert result["usage"][-1]["working_memory_encoding"] == "working-refs-v1"
        assert not mind.head()["paused"] and mind.pending() is None
    current = {meta["record_id"]: (meta, body) for meta, body in mind.retained()[1]}
    assert all(
        current[meta["record_id"]] == (meta, body)
        for meta, body in original
        if meta["part"] != "working_memory"
    )


def test_restart_persistence_and_exactly_once_consumption(mind):
    events, model = episode(mind)
    engine = ShadowEngine(mind, model, clock=lambda: MOMENT)
    assert asyncio.run(engine.step())
    reopened = SqlMind(mind.connect, mind.trader)
    assert reopened.status()["outcomes"] == 1
    assert reopened.outcome(events[0]["message_id"])["reply"]["action"] in {
        "hold",
        "enter",
        "add",
        "exit",
    }
    assert not asyncio.run(ShadowEngine(reopened, model, clock=lambda: MOMENT).step())
    assert reopened.checkpoint("fixture-source") == len(events)
    assert reopened.status()["dispatches"] == 0


@pytest.mark.parametrize("analysis", [False, True])
def test_quiet_and_analysis_inputs_do_not_read_the_archive(mind, monkeypatch, analysis):
    if analysis:
        cases = [
            loads(line)
            for line in (ROOT / "evals/fixtures/starter-v1.jsonl").read_text().splitlines()
        ]
        event = next(
            deepcopy(event)
            for case in cases
            for event in case["events"]
            if event["kind"] == "NewsEvent" and event["payload"]["kind"] == "analysis"
        )
        mind.accept("analysis", [{"sequence": 1, "event": event}], MOMENT)
        mind.set_paused(False)
    else:
        events, _ = episode(mind)
        event = events[0]

    def unavailable_archive(*args):
        pytest.fail("quiet/analysis input attempted archive retrieval")

    monkeypatch.setattr(mind, "_context_evidence", unavailable_archive)

    class Quiet:
        async def triage(self, incoming, context, store, moment):
            assert incoming == event
            assert context["identity"] == "Immutable identity\nnaïve ≠ unsafe\n"
            assert context["playbook"]["version"] == "v1"
            assert context["working_memory"] == mind.head()["working"]
            assert not context["recent_journal"] and not context["retrieved_memories"]
            return {"wake": analysis, "summary": "Retain without a decision"}, {
                "phase": "triage",
                "synthetic": True,
            }

        async def decide(self, *args):
            pytest.fail("quiet/analysis input requested a decision")

    assert asyncio.run(ShadowEngine(mind, Quiet(), clock=lambda: MOMENT).step())
    outcome = mind.outcome(event["message_id"])
    assert outcome["status"] == ("analysis_retained" if analysis else "observed")
    assert len(outcome["usage"]) == 1 and outcome["dispatches"] == 0


def test_decision_loads_same_revision_evidence_after_triage(mind):
    from pipmind.shadow.common import encoded

    events, model = episode(mind)
    event = events[0]
    old = "2026-09-15T00:00:00Z"
    with mind.transaction() as cur:
        mind._insert(
            cur, metadata("older-source", "memory", old, mind.trader), "USD inflation rose"
        )
        mind._insert(
            cur, metadata("contrary-source", "memory", old, mind.trader), "USD inflation fell"
        )
        cur.execute(
            "UPDATE pm_minds SET working=%s WHERE trader=%s",
            (
                encoded(
                    {
                        "views": ["USD inflation unresolved"],
                        "waiting_for": ["Contrary inflation evidence"],
                        "position_theses": [],
                    }
                ),
                mind.trader,
            ),
        )
    model.responses[event["message_id"]]["triage"]["wake"] = True
    before = mind.retained()

    class Inspect(FixtureModel):
        async def triage(self, incoming, context, store, moment):
            assert context["retrieved_memories"] == context["recent_journal"] == []
            self.revision = context["revision"]
            return await super().triage(incoming, context, store, moment)

        async def decide(self, incoming, context, triage, store, moment):
            assert context["revision"] == self.revision
            assert {"older-source", "contrary-source"} <= {
                row["metadata"]["record_id"] for row in context["retrieved_memories"]
            }
            expected = mind.context(moment, "USD inflation", revision=self.revision)
            assert context["working_memory"] == expected["working_memory"]
            return await super().decide(incoming, context, triage, store, moment)

    assert asyncio.run(ShadowEngine(mind, Inspect(model.responses), clock=lambda: MOMENT).step())
    outcome = mind.outcome(event["message_id"])
    assert len(outcome["usage"]) == 2
    assert all(row in mind.retained()[1] for row in before[1] if row[0]["part"] != "working_memory")


@pytest.mark.parametrize("failure", ["pause", "revision", "store_busy"])
def test_evidence_failure_keeps_input_pending_and_does_not_call_decision(
    mind, monkeypatch, failure
):
    events, model = episode(mind)
    event = events[0]
    known_usage = {"phase": "triage", "input_tokens": 123, "output_tokens": 12}
    captured = {}
    original = mind.context

    class Interrupted(FixtureModel):
        async def triage(self, *args):
            if failure == "pause":
                mind.set_paused(True)
            elif failure == "revision":
                mind.set_paused(True)
                mind.set_paused(False)
            return {"wake": True, "summary": "Relevant"}, known_usage

        async def decide(self, *args):
            pytest.fail("decision used stale or unavailable evidence")

    def context(*args, **kwargs):
        if failure == "store_busy" and kwargs.get("revision") is not None:
            raise ShadowError("shadow_store_busy")
        return original(*args, **kwargs)

    monkeypatch.setattr(mind, "context", context)
    engine = ShadowEngine(
        mind,
        Interrupted(model.responses),
        clock=lambda: MOMENT,
        observer=lambda phase, facts: captured.update(facts) if phase == "ready" else None,
    )
    with pytest.raises(
        ShadowError,
        match="shadow_store_busy" if failure == "store_busy" else "shadow_revision_changed",
    ):
        asyncio.run(engine.step())
    assert mind.pending() == event and mind.outcome(event["message_id"]) is None
    assert captured["result"]["usage"] == [known_usage]
    assert captured["result"]["triage"]["wake"]


def test_conflicting_or_future_page_rolls_back_cursor_and_all_inputs(mind):
    event = deepcopy(CASE["events"][0])
    altered = deepcopy(event)
    altered["payload"]["event_id"] = altered["message_id"] = "future-id"
    altered["available_at"] = "2099-01-01T00:00:00Z"
    with pytest.raises(ShadowError, match="input_not_yet_available"):
        mind.accept(
            "fixture", [{"sequence": 1, "event": event}, {"sequence": 2, "event": altered}], MOMENT
        )
    assert mind.checkpoint("fixture") == 0
    assert mind.pending() is None
    mind.accept("fixture", [{"sequence": 1, "event": event}], MOMENT)
    changed = deepcopy(event)
    changed["payload"]["name"] = "Changed facts"
    with pytest.raises(ShadowError):
        mind.accept("fixture", [{"sequence": 2, "event": changed}], MOMENT)
    assert mind.checkpoint("fixture") == 1


def test_event_identities_remain_case_sensitive_in_sql(mind):
    events = []
    for identifier in ("CaseSensitive", "casesensitive"):
        event = deepcopy(CASE["events"][0])
        event["message_id"] = event["payload"]["event_id"] = identifier
        events.append(event)
    mind.accept(
        "binary-identity",
        [{"sequence": i + 1, "event": event} for i, event in enumerate(events)],
        MOMENT,
    )
    assert mind.status()["pending"] == 2


def test_stale_revision_and_pause_do_not_commit_model_results(mind):
    events, _ = episode(mind)
    rev = mind.head()["revision"]
    mind.set_paused(True)
    with pytest.raises(ShadowError, match="shadow_revision_changed"):
        mind.commit(events[0], rev, {"reply": None}, MOMENT)
    assert mind.status()["outcomes"] == 0
    assert mind.pending() == events[0]


def test_archive_relevance_preserves_month_old_and_contrary_evidence_amid_legacy_traces(mind):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.context import phase_context

    old = "2026-09-15T00:00:00Z"
    with mind.transaction() as cur:
        for identifier, text in (
            ("older-inflation", "USD inflation rose; previous outlook"),
            ("contrary-inflation", "USD inflation fell; contrary evidence"),
        ):
            mind._insert(cur, metadata(identifier, "memory", old, mind.trader), text)
        for number in range(60):
            mind._insert(
                cur,
                metadata(f"noise-{number}", "journal", "2026-10-17T00:00:00Z", mind.trader),
                encoded(
                    {
                        "context": {"old_article": "USD inflation " * 100},
                        "triage": {"wake": False, "summary": "An unrelated bank application"},
                    }
                ),
            )
            # Simulate a legacy index that includes recursively copied context.
            cur.execute(
                "SELECT pk FROM pm_records WHERE trader=%s AND id=%s",
                (mind.trader, f"noise-{number}"),
            )
            pk = cur.fetchone()[0]
            for term in ("usd", "inflation"):
                cur.execute("INSERT pm_terms VALUES(%s,%s)", (term, pk))
        mind._insert(
            cur,
            metadata("future-inflation", "memory", "2026-10-19T00:00:00Z", mind.trader),
            "USD inflation future",
        )
        cur.execute(
            "UPDATE pm_minds SET working=%s WHERE trader=%s",
            (
                encoded(
                    {
                        "views": ["USD inflation outlook remains unresolved"],
                        "position_theses": [],
                        "waiting_for": ["Contrary evidence"],
                    }
                ),
                mind.trader,
            ),
        )
    other = SqlMind(mind.connect, "other-" + uuid4().hex)
    other.initialize(
        "Other mandate",
        {"version": "v1", "text": "Other rules"},
        old,
        binding=digest("other"),
        records=[(metadata("private-other", "memory", old, other.trader), "USD inflation private")],
    )
    before = mind.retained()
    found = mind.search("USD inflation", MOMENT)
    ids = {row["metadata"]["record_id"] for row in found}
    assert {"older-inflation", "contrary-inflation"} <= ids
    assert not {"future-inflation", "private-other"} & ids
    assert all(not identifier.startswith("noise-") for identifier in ids)
    assert "old_article" not in mind.search("noise-0", MOMENT)[0]["body"]
    assert "future-inflation" not in {
        row["metadata"]["record_id"] for row in mind.search("future-inflation", MOMENT)
    }
    context = mind.context(MOMENT, "unrelated current headline")
    selected = phase_context(context, "decision")
    assert "USD inflation outlook" in selected["working_memory"]["views"][0]
    assert {"older-inflation", "contrary-inflation"} <= {
        row["metadata"]["record_id"] for row in selected["retrieved_memories"]
    }
    assert mind.retained() == before  # No audit rewrite/deletion during retrieval.
    assert "old_article" in dict((meta["record_id"], body) for meta, body in before[1])["noise-0"]


def test_clock_wake_checkpoint_survives_restart_without_duplicate_inputs(mind):
    from pipmind.shadow.service import ClockConsumer, clock_wake

    clock_wake(mind, MOMENT)
    cursor = mind.checkpoint("clock")
    reopened = SqlMind(mind.connect, mind.trader)
    clock_wake(reopened, MOMENT + timedelta(seconds=1))
    assert reopened.checkpoint("clock") == cursor
    assert reopened.status()["pending"] == 1
    clock_wake(reopened, MOMENT + timedelta(minutes=16))
    assert reopened.status()["pending"] == 2
    cached = ClockConsumer(reopened)
    cached.poll(MOMENT + timedelta(minutes=16))
    cached.poll(MOMENT + timedelta(minutes=16, seconds=1))
    ClockConsumer(SqlMind(mind.connect, mind.trader)).poll(MOMENT + timedelta(minutes=16))
    assert reopened.status()["pending"] == 2
    cached.poll(MOMENT + timedelta(minutes=31))
    assert reopened.status()["pending"] == 3


def test_invalid_model_evidence_is_durably_refused_without_memory_mutation(mind):
    events, model = episode(mind)
    model.responses[events[0]["message_id"]]["reply"]["evidence_ids"] = ["invented-future-record"]
    before = mind.head()["working"]
    asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    assert mind.head()["working"] == before
    result = mind.outcome(events[0]["message_id"])
    assert result["reply"] is None and result["status"] == "model_refused"
    assert result["dispatches"] == 0


@pytest.mark.parametrize("failure", ["position", "proposal", "evidence", "future_memory"])
def test_semantic_refusal_retains_candidate_and_known_usage_without_authority(mind, failure):
    events, model = episode(mind)
    candidate = model.responses[events[0]["message_id"]]["reply"]
    if failure == "position":
        candidate["working_memory"]["position_theses"] = [
            {"instrument": "EUR/USD", "thesis": "Invented holding", "as_of": MOMENT.isoformat()}
        ]
    elif failure == "proposal":
        candidate.update(action="enter", side="buy", size=1)
        candidate.pop("instrument", None)
    elif failure == "evidence":
        candidate["evidence_ids"] = ["invented-future-record"]
    else:
        from pipmind.shadow.common import encoded

        positions = {
            "schema_version": 1,
            "as_of": MOMENT.isoformat(),
            "positions": [{"instrument": "EUR/USD", "side": "buy", "size": 1}],
        }
        mind.contracts.validate("position-snapshot", positions)
        with mind.transaction() as cur:
            cur.execute(
                "INSERT pm_runtime VALUES(%s,%s,%s)",
                (mind.trader, "broker_snapshots", encoded({"positions": positions})),
            )
        candidate["working_memory"]["position_theses"] = [
            {"instrument": "EUR/USD", "thesis": "Future thesis", "as_of": "2099-01-01T00:00:00Z"}
        ]
    usage = {"phase": "decision", "attempted": True, "input_tokens": 123, "output_tokens": 45}

    class Metered(FixtureModel):
        async def decide(self, *args):
            reply, _ = await super().decide(*args)
            return reply, usage

    before = mind.head()["working"]
    asyncio.run(ShadowEngine(mind, Metered(model.responses), clock=lambda: MOMENT).step())
    result = mind.outcome(events[0]["message_id"])
    assert result["status"] == "model_refused" and result["reply"] is None
    assert result["model_reply"] == candidate and result["usage"][-1] == usage
    assert result.get("decision") is None and result["dispatches"] == 0
    assert mind.head()["working"] == before


def test_consistent_export_clone_preserves_history_and_rebuilds_retrieval(mind):
    events, model = episode(mind)
    asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    data = export_experience(mind, MOMENT)
    bundle = validate_bundle(data)
    clone = SqlMind(mind.connect, "clone-" + uuid4().hex)
    report = import_experience(
        clone,
        data,
        MOMENT + timedelta(seconds=1),
        digest("different-account"),
        "Immutable identity\nnaïve ≠ unsafe\n",
    )
    assert report["paused"] and clone.head()["lineage"] != mind.head()["lineage"]
    assert clone.head()["binding"] != mind.head()["binding"]
    assert clone.head()["working"]["position_theses"] == []
    assert clone.status()["outcomes"] == clone.status()["pending"] == 0
    context = clone.context(MOMENT + timedelta(seconds=2))
    assert context["identity"] == "Immutable identity\nnaïve ≠ unsafe\n"
    assert context["playbook"]["text"] == "# Exact playbook\n"
    source = {
        meta["record_id"]: (meta, body)
        for meta, body in mind.retained()[1]
        if meta["part"] != "working_memory"
    }
    destination = {meta["record_id"]: (meta, body) for meta, body in clone.retained()[1]}
    assert all(destination[key] == value for key, value in source.items())
    assert clone.search(events[0]["payload"]["currency"], MOMENT + timedelta(seconds=2))
    assert bundle.manifest["checkpoint"]["checkpoint_id"] == str(mind.head()["revision"])
    validate_bundle(export_experience(clone, MOMENT + timedelta(seconds=2)))


def test_interrupted_or_invalid_clone_changes_neither_source_nor_destination(mind):
    data = export_experience(mind, MOMENT)
    clone = SqlMind(mind.connect, "clone-" + uuid4().hex)
    before = mind.retained()

    def interrupt():
        raise ShadowError("injected_import_interruption")

    with pytest.raises(ShadowError, match="injected_import_interruption"):
        import_experience(
            clone,
            data,
            MOMENT,
            digest("new"),
            "Immutable identity\nnaïve ≠ unsafe\n",
            interrupt=interrupt,
        )
    with pytest.raises(ShadowError, match="mind_not_initialized"):
        clone.head()
    with pytest.raises(BundleError):
        import_experience(
            clone, data[:-30], MOMENT, digest("new"), "Immutable identity\nnaïve ≠ unsafe\n"
        )
    assert before == mind.retained()
    with pytest.raises(ShadowError, match="clone_identity_policy_mismatch"):
        import_experience(clone, data, MOMENT, digest("new"), "Different mandate")


@pytest.mark.parametrize(
    "versions",
    [
        ("shadow-context-v1", "sql-memory-v1"),
        ("shadow-context-v2", "sql-memory-v2"),
        ("shadow-context-v3", "sql-memory-v2"),
        ("shadow-context-v4", "sql-memory-v2"),
        ("shadow-context-v5", "sql-memory-v2"),
        ("shadow-context-v6", "sql-memory-v2"),
        ("shadow-context-v7", "sql-memory-v2"),
        ("shadow-context-v8", "sql-memory-v2"),
        ("shadow-context-v9", "sql-memory-v2"),
        ("shadow-context-v10", "sql-memory-v2"),
    ],
)
def test_legacy_context_bundle_rebuilds_retrieval_without_rewriting_history(mind, versions):
    import io
    import json
    import zipfile

    original = validate_bundle(export_experience(mind, MOMENT))
    manifest = deepcopy(original.manifest)
    manifest["compatibility"].update(prompt_version=versions[0], retrieval_version=versions[1])

    def archive():
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as target:
            for name, content in original.files.items():
                target.writestr(
                    name, json.dumps(manifest).encode() if name == "manifest.json" else content
                )
        return output.getvalue()

    clone = SqlMind(mind.connect, "legacy-clone-" + uuid4().hex)
    report = import_experience(
        clone,
        archive(),
        MOMENT + timedelta(seconds=1),
        digest("legacy-clone"),
        "Immutable identity\nnaïve ≠ unsafe\n",
    )
    assert "legacy_context_retrieval_rebuilt" in report["transformations"]
    assert clone.head()["parent"]["compatibility"] == manifest["compatibility"]
    source = {
        meta["record_id"]: (meta, body)
        for meta, body in mind.retained()[1]
        if meta["part"] != "working_memory"
    }
    destination = {meta["record_id"]: (meta, body) for meta, body in clone.retained()[1]}
    assert all(destination[key] == value for key, value in source.items())
    current = validate_bundle(export_experience(clone, MOMENT + timedelta(seconds=2)))
    assert current.manifest["compatibility"]["retrieval_version"] == "sql-memory-v2"
    manifest["compatibility"]["prompt_version"] = "unknown-context"
    with pytest.raises(BundleError, match="unsupported_experience_compatibility"):
        import_experience(
            SqlMind(mind.connect, "unknown-" + uuid4().hex),
            archive(),
            MOMENT,
            digest("unknown"),
            "Immutable identity\nnaïve ≠ unsafe\n",
        )


def test_sql_search_filters_future_and_other_traders(mind):
    with mind.transaction() as cur:
        mind._insert(
            cur, metadata("old", "memory", "2026-10-01T00:00:00Z", mind.trader), "payroll evidence"
        )
        mind._insert(
            cur, metadata("future", "memory", "2026-10-19T00:00:00Z", mind.trader), "payroll future"
        )
    assert [r["metadata"]["record_id"] for r in mind.search('payroll " OR 1=1', MOMENT)] == ["old"]


def test_sql_feed_inbox_outbox_cursor_atomicity_and_reprocessing(mind):
    from pipmind.feeds.common import FeedError, identity
    from pipmind.feeds.fixtures import pulse
    from pipmind.feeds.sql_store import SqlFeedStore

    store = SqlFeedStore(mind.connect)
    key = "sql-test:" + uuid4().hex
    assert not store.ingest(
        "macro", key, {"synthetic": True}, identity(key), MOMENT, cursor=(key, "accepted")
    )
    assert store.ingest("macro", key, {"synthetic": True}, identity(key), MOMENT)
    assert store.state(key) == "accepted"
    capture = "rest-page:" + uuid4().hex
    store.ingest("macro", capture, {"canonical_page": {}}, identity(capture), MOMENT)
    pending = {row["key"]: row for row in store.pending("macro")}
    assert pending[key]["raw"] == {"synthetic": True}
    assert capture not in pending  # Canonical batches have their own normalization path.
    store.quarantine(key)
    assert store.reprocess(key)
    good = pulse("macro", MOMENT)
    good["event_id"] = key
    bad = {**good, "actual": "invalid-number"}
    with pytest.raises(FeedError):
        store.complete_batch("macro", key, [good, bad], MOMENT, cursor=(key, "complete"))
    assert store.state(key) == "accepted"
    assert not any(row["event"]["message_id"] == key for row in store.events())
    patch_key = "atomic-patch:" + uuid4().hex
    store.complete_batch(
        "macro",
        key,
        [good],
        MOMENT,
        cursor=(key, "complete"),
        state_patch=(patch_key, {"checked": True}),
    )
    store.complete_batch(
        "macro",
        key,
        [good],
        MOMENT,
        cursor=(key, "accepted"),
        state_patch=(patch_key, {"checked": False}),
    )
    assert store.state(patch_key) == {"checked": True}
    recovery_key = "recovery:" + key
    before = store.events()
    store.complete_batch(
        "macro",
        key,
        [good],
        MOMENT,
        cursor=(recovery_key, "complete"),
        state_patch=(patch_key, {"recovered": True}),
    )
    store.complete_batch(
        "macro",
        key,
        [good],
        MOMENT,
        cursor=(recovery_key, "rewound"),
        state_patch=(patch_key, {"recovered": False}),
    )
    assert store.state(recovery_key) == "complete"
    assert store.state(patch_key) == {"checked": True, "recovered": True}
    assert store.events() == before
    reopened = SqlFeedStore(mind.connect)
    assert reopened.source_id() == store.source_id()
    assert reopened.state(key) == "complete"
    rows = reopened.events()
    connections = []
    connect = reopened.connect

    def counted():
        connections.append(True)
        return connect()

    reopened.connect = counted
    frame = reopened.handoff(0, 100, include_calendar=True)
    assert len(connections) == 1
    reopened.connect = connect
    assert frame == {
        "events": rows,
        "coverage": reopened.state("canonical:quality", {"status": "unverified"}),
        "quarantined": reopened.status()["quarantined"],
        "calendar": reopened.state("calendar:data"),
    }
    assert "calendar" not in reopened.handoff(rows[-1]["sequence"], 1)
    assert reopened.events() == rows and reopened.state(key) == "complete"
    mind.accept(store.source_id(), rows, MOMENT)
    assert mind.checkpoint(store.source_id()) == rows[-1]["sequence"]


def test_feed_configuration_cutover_preserves_history_and_fences_stale_workers(mind):
    from pipmind.feeds.common import FeedError, identity
    from pipmind.feeds.fixtures import configuration
    from pipmind.feeds.sql_store import SqlFeedStore, reconfigure
    from pipmind.feeds.store import encoded
    from pipmind.shadow.sql import transaction

    original = SqlFeedStore(mind.connect)
    source, events = original.source_id(), original.events()
    previous = configuration()
    replacement = deepcopy(previous)
    replacement["news"].append(
        {"id": "test-" + uuid4().hex, "url": "https://news.invalid/feed", "kind": "analysis"}
    )
    cursor = "test-cursor:" + uuid4().hex
    user = "fixture_migration_" + uuid4().hex
    original.save(cursor, {"since": "123", "next": "retained-page"})
    with original.transaction() as db:
        db.execute("SELECT v FROM pf_meta WHERE k='profile'")
        saved = db.fetchone()[0]
        # Isolate this test's configuration binding in the synthetic SQL database.
        db.execute(
            "UPDATE pf_meta SET v=%s WHERE k='profile'", (identity(identity(encoded(previous))),)
        )
        db.execute(f"CREATE USER [{user}] WITHOUT LOGIN")
        db.execute(f"GRANT SELECT ON dbo.pf_meta TO [{user}]")
        db.execute(f"DENY UPDATE ON dbo.pf_meta TO [{user}]")
        db.execute(f"GRANT SELECT,INSERT,UPDATE ON dbo.pf_state TO [{user}]")

    def limited_connect():
        db = mind.connect()
        with db.cursor() as cur:
            cur.execute(f"EXECUTE AS USER = '{user}'")
        return db

    try:
        old = SqlFeedStore(mind.connect, profile=identity(encoded(previous)))
        reconfigure(limited_connect, previous, replacement, MOMENT)
        fresh = SqlFeedStore(mind.connect, profile=identity(encoded(replacement)))
        assert fresh.source_id() == source and fresh.events() == events
        assert fresh.state(cursor) == {"since": "123", "next": "retained-page"}
        with pytest.raises(FeedError, match="profile_changed"):
            old.save(cursor, {"since": "0"})
        with pytest.raises(FeedError, match="profile_changed"):
            old.handoff(include_calendar=True)
        assert fresh.state(cursor)["since"] == "123"
        with fresh.transaction() as db:
            db.execute("SELECT v FROM pf_meta WHERE k='profile'")
            assert db.fetchone()[0] == identity(identity(encoded(previous)))
        with pytest.raises(FeedError, match="profile_mismatch"):
            reconfigure(mind.connect, previous, replacement, MOMENT)
        altered = deepcopy(replacement)
        altered["macro"][0]["value_scale"] = 1000
        with pytest.raises(FeedError, match="must_be_additive"):
            reconfigure(mind.connect, replacement, altered, MOMENT)
        assert fresh.events() == events
    finally:
        with transaction(mind.connect, "pipmind-feed-store") as db:
            db.execute("DELETE pf_state WHERE k='configuration:active'")
            db.execute("UPDATE pf_meta SET v=%s WHERE k='profile'", (saved,))
            db.execute(f"DROP USER [{user}]")


def test_sql_expected_evaluation_mismatch_has_no_connector_access_or_durable_writes(mind):
    import asyncio
    from copy import deepcopy

    import pytest

    from pipmind.dataset import load_cases
    from pipmind.shadow.common import ShadowError
    from pipmind.shadow.evaluation_preflight import expected_local_provenance
    from pipmind.shadow.evaluation_provenance import capture_provenance
    from pipmind.shadow.evaluations import GRADER_VERSION, evaluate
    from pipmind.shadow.model import FixtureModel

    cases = [case for case in load_cases() if case["case_id"] == "T01"]
    model = FixtureModel(deepcopy(cases[0]["model_responses"]))
    facts = capture_provenance(cases, model, repetitions=2, grader_version=GRADER_VERSION)
    expected = expected_local_provenance(
        facts, model, observed_memory_policy={"present": False, "version": None, "sha256": None}
    )

    def counts():
        with mind.transaction() as cur:
            result = []
            for table in (
                "pm_minds",
                "pm_records",
                "pm_terms",
                "pm_inputs",
                "pm_outcomes",
                "pm_cursors",
                "pm_runtime",
            ):
                cur.execute("SELECT COUNT_BIG(*) FROM " + table)
                result.append(cur.fetchone()[0])
            return tuple(result)

    before = counts(), mind.head(), mind.retained()
    expected["effective_local_settings"]["capabilities"]["working_budget"]["max_bytes"] = 3200
    connections = []

    def forbidden_connect():
        connections.append(True)
        raise AssertionError("Mismatch reached SQL connector")

    with pytest.raises(ShadowError, match="evaluation_preflight_mismatch"):
        asyncio.run(
            evaluate(
                forbidden_connect, cases, model, repetitions=2, expected_local_provenance=expected
            )
        )
    assert connections == []
    assert (counts(), mind.head(), mind.retained()) == before


def test_sql_expected_evaluation_match_freezes_cohort_and_links_precreation_proof(mind):
    import asyncio
    from copy import deepcopy

    from pipmind.dataset import load_cases
    from pipmind.shadow.common import digest, encoded
    from pipmind.shadow.evaluation_preflight import expected_local_provenance
    from pipmind.shadow.evaluation_provenance import capture_provenance
    from pipmind.shadow.evaluations import GRADER_VERSION, evaluate
    from pipmind.shadow.model import FixtureModel
    from pipmind.shadow.store import SqlMind

    cases = [case for case in load_cases() if case["case_id"] == "T01"]
    original_cases = deepcopy(cases)
    model = FixtureModel(deepcopy(cases[0]["model_responses"]))
    facts = capture_provenance(cases, model, repetitions=2, grader_version=GRADER_VERSION)
    expected = expected_local_provenance(
        facts, model, observed_memory_policy={"present": False, "version": None, "sha256": None}
    )
    expected_hash = digest(encoded(expected))
    source_before = mind.head(), mind.retained()

    def progress(partial):
        cases.clear()
        expected.clear()
        partial["runtime_provenance"]["cases"].clear()

    report = asyncio.run(
        evaluate(
            mind.connect,
            cases,
            model,
            repetitions=2,
            expected_local_provenance=expected,
            progress=progress,
        )
    )
    assert report["summary"]["episodes"] == 2
    assert report["summary"]["hard_check_failures"] == 0
    assert report["dataset_sha256"] == digest(encoded(original_cases))
    assert report["local_preflight"]["expected_sha256"] == expected_hash
    assert report["local_preflight"]["policy_observation"] == "absent_namespace_before_creation"
    assert report["local_preflight"]["namespaces_checked"] == 2
    assert not report["local_preflight"]["provider_attested"]
    assert not report["local_preflight"]["loaded_code_attested"]
    for episode in report["results"]:
        store = SqlMind(mind.connect, episode["trader"])
        assert store.head()["paused"] and store.runtime("memory_policy") is None
        linked = store.runtime("evaluation")
        assert linked["local_preflight"] == report["local_preflight"]
        assert linked["runtime_provenance_sha256"] == report["runtime_provenance_sha256"]
        assert linked["observed_memory_policy"] == {
            "observation": "isolated_runtime_before_processing",
            "present": False,
            "version": None,
            "sha256": None,
        }
    assert (mind.head(), mind.retained()) == source_before


def test_sql_expected_evaluation_postinit_policy_refusal_retains_paused_initialized_mind(
    mind, monkeypatch
):
    import asyncio
    from copy import deepcopy

    import pytest

    from pipmind.dataset import load_cases
    from pipmind.shadow.common import ShadowError, encoded
    from pipmind.shadow.evaluation_preflight import expected_local_provenance
    from pipmind.shadow.evaluation_provenance import capture_provenance
    from pipmind.shadow.evaluations import GRADER_VERSION, evaluate
    from pipmind.shadow.model import FixtureModel
    from pipmind.shadow.store import SqlMind

    cases = [case for case in load_cases() if case["case_id"] == "T01"]
    model = FixtureModel(deepcopy(cases[0]["model_responses"]))
    facts = capture_provenance(cases, model, repetitions=2, grader_version=GRADER_VERSION)
    expected = expected_local_provenance(
        facts, model, observed_memory_policy={"present": False, "version": None, "sha256": None}
    )
    original_initialize = SqlMind.initialize
    initialized = []
    calls = []

    def unexpected_policy(store, *args, **kwargs):
        original_initialize(store, *args, **kwargs)
        initialized.append(store.trader)
        with store.transaction() as cur:
            cur.execute(
                "INSERT pm_runtime VALUES(%s,%s,%s)",
                (
                    store.trader,
                    "memory_policy",
                    encoded({"version": "unexpected-synthetic-policy"}),
                ),
            )

    async def forbidden_model(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Post-init policy refusal called model")

    monkeypatch.setattr(SqlMind, "initialize", unexpected_policy)
    monkeypatch.setattr(FixtureModel, "triage", forbidden_model)
    source_before = mind.head(), mind.retained()
    partials = []

    def retain_after_unlock(report):
        # A fresh SQL connection must reacquire the trader lock after the
        # refusal observation has exited its transaction, without timing out.
        saved = SqlMind(mind.connect, report["preflight_refusal"]["trader"])
        assert saved.head()["paused"] and saved.head()["revision"] == 0
        partials.append(deepcopy(report))

    with pytest.raises(ShadowError, match="evaluation_preflight_policy_mismatch") as raised:
        asyncio.run(
            evaluate(
                mind.connect,
                cases,
                model,
                repetitions=2,
                expected_local_provenance=expected,
                progress=retain_after_unlock,
            )
        )
    assert len(initialized) == 1 and calls == []
    retained = SqlMind(mind.connect, initialized[0])
    assert retained.head()["paused"] and retained.head()["revision"] == 0
    assert retained.runtime("evaluation") is None
    assert retained.runtime("memory_policy") == {"version": "unexpected-synthetic-policy"}
    with retained.transaction() as cur:
        for table in ("pm_inputs", "pm_outcomes", "pm_cursors"):
            cur.execute(
                "SELECT COUNT_BIG(*) FROM " + table + " WHERE trader=%s", (retained.trader,)
            )
            assert cur.fetchone()[0] == 0
    refusal = partials[-1]["preflight_refusal"]
    assert refusal["phase"] == "post_initialization"
    assert refusal["trader"] == retained.trader and refusal["initialized_mind_retained"]
    assert refusal["failure_audit"] == "report-only" and refusal["unpause_attempted"] is False
    assert raised.value.partial_report["preflight_refusal"]["checkpoint_persistence"] == (
        "callback_returned"
    )
    assert (mind.head(), mind.retained()) == source_before


def test_repeated_evaluations_use_fresh_paused_sql_minds_without_changing_primary(mind):
    from pipmind.shadow.evaluations import evaluate

    baseline = mind.status()
    case = deepcopy(CASE)
    report = asyncio.run(
        evaluate(
            mind.connect,
            [case],
            FixtureModel(case["model_responses"]),
            repetitions=2,
        )
    )
    assert report["summary"]["episodes"] == 2
    assert report["summary"]["hard_check_failures"] == 0
    assert report["summary"]["model_refusals"] == 0
    assert len({r["trader"] for r in report["results"]}) == 2
    assert all(SqlMind(mind.connect, r["trader"]).head()["paused"] for r in report["results"])
    assert mind.status() == baseline
    assert report["human_review"].startswith("pending")


def test_evaluation_provenance_freezes_cases_manifest_and_links_isolated_sql_episodes(mind):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.evaluations import evaluate

    baseline = mind.status(), mind.head(), mind.retained()
    caller_cases = [
        loads(line)
        for line in (ROOT / "evals/fixtures/starter-v1.jsonl").read_bytes().splitlines()[:2]
    ]
    frozen_cases = deepcopy(caller_cases)
    caller_manifest = {
        "prompt_version": "unverified-caller-claim",
        "runtime_provenance": {"kind": "caller-must-not-override-local-facts"},
        "notes": {"original": True},
    }
    frozen_manifest = deepcopy(caller_manifest)
    responses = {
        key: deepcopy(value)
        for case in caller_cases
        for key, value in case["model_responses"].items()
    }
    checkpoints = []

    def progress(snapshot):
        checkpoints.append(deepcopy(snapshot))
        if len(checkpoints) == 1:
            # Mutating caller-owned inputs after the first await cannot alter
            # later repetitions/cases, their digests or descriptive metadata.
            caller_cases.reverse()
            caller_cases[0]["case_id"] = "S10"
            caller_cases[0]["initial_mind"]["identity"] = "Caller changed identity"
            caller_cases[0]["events"].clear()
            caller_manifest["notes"]["original"] = False
            caller_manifest["runtime_provenance"]["kind"] = "changed-caller-claim"
        snapshot["runtime_provenance"]["kind"] = "callback-copy-only"
        snapshot["model_manifest"]["notes"]["original"] = "callback-copy-only"

    report = asyncio.run(
        evaluate(
            mind.connect,
            caller_cases,
            FixtureModel(responses),
            repetitions=2,
            manifest=caller_manifest,
            progress=progress,
        )
    )
    facts = report["runtime_provenance"]
    provenance_sha256 = digest(encoded(facts))
    assert facts["kind"] == "local-evidence-only"
    assert facts["runner_mode"] == "fixture-working-state"
    assert (
        facts["selected_cases_sha256"] == report["dataset_sha256"] == digest(encoded(frozen_cases))
    )
    assert facts["cases"] == [
        {"case_id": case["case_id"], "sha256": digest(encoded([case]))} for case in frozen_cases
    ]
    assert report["runtime_provenance_sha256"] == provenance_sha256
    assert report["model_manifest"] == frozen_manifest
    assert report["model_manifest_status"] == "descriptive_unverified"
    assert report["summary"]["episodes"] == len(checkpoints) == 4
    assert report["summary"]["hard_check_failures"] == 0
    assert [row["case_id"] for row in report["results"]] == [
        case["case_id"] for case in frozen_cases for _ in range(2)
    ]
    absent_policy = {
        "observation": "isolated_runtime_before_processing",
        "present": False,
        "version": None,
        "sha256": None,
    }
    for snapshot in checkpoints:
        assert snapshot["runtime_provenance"] == facts
        assert snapshot["runtime_provenance_sha256"] == provenance_sha256
        assert snapshot["model_manifest"] == frozen_manifest
    for episode in report["results"]:
        store = SqlMind(mind.connect, episode["trader"])
        case = next(case for case in frozen_cases if case["case_id"] == episode["case_id"])
        assert store.head()["paused"] and store.runtime("memory_policy") is None
        metadata = store.runtime("evaluation")
        assert metadata == {
            "run_id": report["run_id"],
            "case_id": case["case_id"],
            "case_sha256": digest(encoded([case])),
            "repetition": episode["repetition"],
            "runtime_provenance_sha256": provenance_sha256,
            "observed_memory_policy": absent_policy,
        }
        assert episode["runtime_provenance_sha256"] == provenance_sha256
        assert episode["case_sha256"] == metadata["case_sha256"]
        assert episode["observed_memory_policy"] == absent_policy
        for outcome in episode["outcomes"]:
            assert store.outcome(outcome["event_id"])["usage"] == outcome["usage"]
    assert (mind.status(), mind.head(), mind.retained()) == baseline


def test_evaluation_commit_failure_retains_known_attempt_and_fails_completion(mind, monkeypatch):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.evaluations import evaluate

    original = SqlMind.commit
    calls = 0
    partial = []

    def fail_once(store, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ShadowError("shadow_store_transaction_failed")
        return original(store, *args, **kwargs)

    monkeypatch.setattr(SqlMind, "commit", fail_once)
    case = deepcopy(CASE)
    report = asyncio.run(
        evaluate(
            mind.connect,
            [case],
            FixtureModel(case["model_responses"]),
            repetitions=2,
            progress=partial.append,
        )
    )
    first, second = report["results"]
    assert not first["hard_checks"]["complete_episode"]
    assert first["failure_audit"] == "sql-and-report"
    assert first["uncommitted_attempt"]["result"]["model_reply"]
    assert first["outcomes"][0]["status"] == "not_completed"
    assert first["model_refusals"] == 0 and second["hard_checks"]["complete_episode"]
    assert report["summary"]["operational_failures"] == 1 and len(partial) == 2
    provenance_sha256 = digest(encoded(report["runtime_provenance"]))
    assert report["runtime_provenance_sha256"] == provenance_sha256
    for snapshot in partial:
        assert snapshot["runtime_provenance"] == report["runtime_provenance"]
        assert snapshot["runtime_provenance_sha256"] == provenance_sha256
    for episode in report["results"]:
        linked = SqlMind(mind.connect, episode["trader"]).runtime("evaluation")
        assert linked["runtime_provenance_sha256"] == provenance_sha256
        assert linked["case_id"] == case["case_id"]
        assert linked["case_sha256"] == digest(encoded([case]))
        assert linked["observed_memory_policy"]["present"] is False
    assert all(SqlMind(mind.connect, r["trader"]).head()["paused"] for r in report["results"])
    assert any(
        '"evaluation-operational-failure"' in body
        for meta, body in SqlMind(mind.connect, first["trader"]).retained()[1]
    )


def test_evaluation_unconfirmed_pause_checkpoint_keeps_provenance_and_sql_link(mind, monkeypatch):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.evaluations import evaluate

    baseline = mind.status(), mind.retained()
    original_pause = SqlMind.set_paused
    checkpoints = []
    case = deepcopy(CASE)

    def pause(store, value):
        if value:
            raise ShadowError("synthetic_pause_acknowledgement_unconfirmed")
        return original_pause(store, value)

    monkeypatch.setattr(SqlMind, "set_paused", pause)
    try:
        with pytest.raises(ShadowError, match="synthetic_pause_acknowledgement_unconfirmed"):
            asyncio.run(
                evaluate(
                    mind.connect,
                    [case],
                    FixtureModel(case["model_responses"]),
                    repetitions=2,
                    progress=checkpoints.append,
                )
            )
        assert len(checkpoints) == 1
        report = checkpoints[0]
        facts = report["runtime_provenance"]
        provenance_sha256 = digest(encoded(facts))
        assert report["runtime_provenance_sha256"] == provenance_sha256
        assert facts["selected_cases_sha256"] == digest(encoded([case]))
        episode = report["incomplete_episode"]
        assert episode["pause_verified"] is False
        assert episode["runtime_provenance_sha256"] == provenance_sha256
        assert episode["case_sha256"] == digest(encoded([case]))
        assert episode["observed_memory_policy"]["present"] is False
        store = SqlMind(mind.connect, episode["trader"])
        linked = store.runtime("evaluation")
        assert linked["runtime_provenance_sha256"] == provenance_sha256
        assert linked["case_id"] == case["case_id"] and linked["repetition"] == 1
        assert store.runtime("memory_policy") is None
        event_id = case["events"][0]["message_id"]
        assert store.outcome(event_id)["usage"] == episode["attempt"]["result"]["usage"]
    finally:
        # The intentionally unconfirmed pause must not leave this SQL fixture
        # running after the assertion, even if another assertion fails.
        for report in checkpoints:
            store = SqlMind(mind.connect, report["incomplete_episode"]["trader"])
            original_pause(store, True)
            assert store.head()["paused"]
    assert (mind.status(), mind.retained()) == baseline


@pytest.mark.parametrize("commit_before_error", [False, True])
def test_evaluation_usage_accounts_for_retained_and_uncommitted_traces_exactly_once(
    mind, monkeypatch, commit_before_error
):
    from pipmind.shadow.evaluations import evaluate

    baseline = mind.status()
    original_commit = SqlMind.commit
    original_traces = {}
    checkpoints = []
    commits = 0

    def commit(store, envelope, revision, result, moment):
        nonlocal commits
        commits += 1
        # These are test-only accounting records, not actual RPCs or activation
        # of the starter runner's absent memory_policy/repair behavior.
        assert result["usage"] == [
            {"phase": "triage", "synthetic": True},
            {"phase": "decision", "synthetic": True},
        ]
        result["usage"].extend(
            [
                {
                    "phase": "triage",
                    "attempted": True,
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "fixture_trace": "known-triage",
                },
                {
                    "phase": "decision",
                    "attempted": True,
                    "input_tokens": 20,
                    "output_tokens": 3,
                    "usage_unknown": True,
                    "fixture_trace": "explicitly-unknown-with-known-sides",
                },
                {
                    "phase": "memory_repair",
                    "attempted": True,
                    "input_tokens": 30,
                    "output_tokens": 4,
                    "fixture_trace": "attempted-repair-shaped-record",
                },
                {
                    "phase": "decision",
                    "attempted": True,
                    "input_tokens": 40,
                    "output_tokens": None,
                    "fixture_trace": "one-known-side",
                },
                {
                    "phase": "triage",
                    "attempted": False,
                    "input_tokens": 999,
                    "output_tokens": 999,
                    "failure_code": "shadow_context_projection",
                    "fixture_trace": "pre-rpc-must-not-contribute",
                },
                {
                    "phase": "memory_repair",
                    "attempted": False,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "method": "punctuation_spacing",
                    "fixture_trace": "zero-rpc-repair-must-not-contribute",
                },
                {
                    "phase": "decision",
                    "synthetic": True,
                    "attempted": True,
                    "input_tokens": 9999,
                    "output_tokens": 9999,
                    "fixture_trace": "synthetic-must-not-contribute",
                },
                {
                    "phase": "legacy_phase",
                    "input_tokens": 5,
                    "output_tokens": 1,
                    "fixture_trace": "unconfirmed-attempt-marker",
                },
            ]
        )
        original_traces[store.trader] = deepcopy(result["usage"])
        if commits == 1 and not commit_before_error:
            raise ShadowError("shadow_store_transaction_failed")
        original_commit(store, envelope, revision, result, moment)
        if commits == 1:
            # Acknowledgement failure after a real SQL commit leaves an outcome.
            # Its still-live attempt copy must not be counted a second time.
            raise ShadowError("shadow_store_transaction_failed")

    monkeypatch.setattr(SqlMind, "commit", commit)
    case = deepcopy(CASE)
    assert len(case["events"]) == 1
    report = asyncio.run(
        evaluate(
            mind.connect,
            [case],
            FixtureModel(case["model_responses"]),
            repetitions=2,
            progress=checkpoints.append,
        )
    )
    first, second = report["results"]
    assert commits == 2 and len(checkpoints) == 2
    assert first["operational_error"] == "shadow_store_transaction_failed"
    assert first["failure_audit"] == "sql-and-report"
    assert not first["hard_checks"]["complete_episode"]
    assert second["hard_checks"]["complete_episode"]
    if commit_before_error:
        assert first["uncommitted_attempt"] is None
        assert first["outcomes"][0]["usage"] == original_traces[first["trader"]]
    else:
        assert first["outcomes"][0]["status"] == "not_completed"
        assert first["outcomes"][0]["usage"] == []
        assert first["uncommitted_attempt"]["result"]["usage"] == original_traces[first["trader"]]
    assert second["outcomes"][0]["usage"] == original_traces[second["trader"]]

    summary = report["summary"]
    expected = {
        "episodes": 2,
        "operational_failures": 1,
        "trace_records": 20,
        "model_calls": 10,
        "input_tokens": 210,
        "output_tokens": 20,
        "unknown_usage_calls": 4,
        "non_rpc_traces": 4,
        "synthetic_traces": 6,
        "zero_rpc_repairs": 2,
        "unconfirmed_attempt_calls": 2,
        "missing_usage_calls": 2,
        "partially_missing_usage_calls": 2,
    }
    assert {key: summary[key] for key in expected} == expected
    assert summary["usage_accounting_version"] == "shadow-usage-v1"
    phases = summary["usage_by_phase"]
    assert set(phases) == {"triage", "decision", "memory_repair", "other"}
    assert phases["memory_repair"]["calls"] == 2
    assert phases["memory_repair"]["input_tokens"] == 60
    assert phases["memory_repair"]["output_tokens"] == 8
    assert phases["memory_repair"]["zero_rpc_repairs"] == 2
    assert phases["decision"]["unknown_usage_calls"] == 4
    assert phases["other"]["calls"] == phases["other"]["unconfirmed_attempt_calls"] == 2
    assert sum(row["calls"] for row in phases.values()) == summary["model_calls"]
    assert sum(row["input_tokens"] for row in phases.values()) == summary["input_tokens"]
    assert sum(row["output_tokens"] for row in phases.values()) == summary["output_tokens"]

    # Aggregation never rewrites the evidence it classifies. Reopen SQL stores
    # and compare committed and failed-attempt journal copies to original lists.
    for episode in report["results"]:
        store = SqlMind(mind.connect, episode["trader"])
        assert store.head()["paused"] and store.runtime("memory_policy") is None
        outcome = store.outcome(case["events"][0]["message_id"])
        if outcome is not None:
            assert outcome["usage"] == original_traces[episode["trader"]]
        if episode is first:
            failures = [
                loads(body)
                for meta, body in store.retained()[1]
                if meta["part"] == "journal" and '"evaluation-operational-failure"' in body
            ]
            assert len(failures) == 1
            assert failures[0]["attempt"]["result"]["usage"] == original_traces[episode["trader"]]
    assert checkpoints[0]["results"][0] == first
    assert mind.status() == baseline


def test_broker_headline_page_commits_events_and_coverage_atomically_in_sql(mind):
    from pipmind.feeds.common import FeedError
    from pipmind.feeds.news import relay_page
    from pipmind.feeds.sql_store import SqlFeedStore

    feeds = SqlFeedStore(mind.connect, profile="synthetic")
    payload = {
        "schema_version": 1,
        "relay_id": "sql-relay-" + uuid4().hex,
        "headline": "Synthetic policy statement",
        "published_at": MOMENT.isoformat(),
        "observed_at": MOMENT.isoformat(),
    }
    relay_page(feeds, [payload], MOMENT, checked_at=MOMENT.isoformat())
    baseline = feeds.events(0, 500)
    relay_page(feeds, [payload], MOMENT, checked_at=MOMENT.isoformat())
    assert feeds.events(0, 500) == baseline
    state = feeds.state("relay:status")
    with pytest.raises(FeedError):
        relay_page(
            feeds,
            [
                {**payload, "relay_id": "never-committed"},
                {**payload, "published_at": "2099-01-01T00:00:00Z"},
            ],
            MOMENT,
            checked_at=MOMENT.isoformat(),
        )
    assert feeds.events(0, 500) == baseline and feeds.state("relay:status") == state


@pytest.mark.parametrize("operation", ["feed", "inputs", "terms"])
def test_sql_batches_leave_peer_progress_possible_under_network_latency(mind, operation):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from pipmind.contracts import stamp
    from pipmind.feeds.fixtures import pulse
    from pipmind.feeds.sql_store import SqlFeedStore

    acquired = threading.Event()
    enabled = False

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, key):
            return getattr(self.cursor, key)

        def execute(self, sql, *args):
            time.sleep(0.1)  # Representative network latency, not a database benchmark.
            self.cursor.execute(sql, *args)
            if enabled and sql.startswith("DECLARE @r"):
                acquired.set()
            return self

    class Connection:
        def __init__(self):
            self.db = mind.connect()

        def __getattr__(self, key):
            return getattr(self.db, key)

        def cursor(self):
            return Cursor(self.db.cursor())

    if operation == "feed":
        slow, peer = SqlFeedStore(Connection), SqlFeedStore(mind.connect)
        payloads = [{**pulse("news", MOMENT), "event_id": uuid4().hex} for _ in range(100)]
        before = max((r["sequence"] for r in peer.events(limit=500)), default=0)
        key = "batch:" + uuid4().hex

        def action():
            slow.page("news", payloads, key, 100, MOMENT)

        query = peer.status
    else:
        slow = SqlMind(Connection, mind.trader)
        query = mind.status
        if operation == "inputs":
            events = []
            for sequence in range(1, 101):
                identifier = uuid4().hex
                events.append(
                    {
                        "sequence": sequence,
                        "event": {
                            "schema_version": 1,
                            "kind": "ClockEvent",
                            "message_id": identifier,
                            "available_at": stamp(MOMENT),
                            "payload": {
                                "schema_version": 1,
                                "event_id": identifier,
                                "ts": stamp(MOMENT),
                                "reason": "daily_brief",
                            },
                        },
                    }
                )

            def action():
                slow.accept("batch-latency", events, MOMENT)
        else:

            def action():
                with slow.transaction() as cur:
                    slow._insert(
                        cur,
                        metadata("latency-memory", "memory", stamp(MOMENT), mind.trader),
                        " ".join("token" + str(i) for i in range(80)),
                    )

    enabled = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(action)
        assert acquired.wait(10)
        query()  # Must obtain the same application lock within its normal five seconds.
        future.result(timeout=15)
    if operation == "feed":
        assert peer.state(key) == 100
        assert [r["event"]["message_id"] for r in peer.events(after=before, limit=500)] == [
            p["event_id"] for p in payloads
        ]
    elif operation == "inputs":
        assert mind.status()["pending"] == 100
    else:
        assert mind.search("token11", MOMENT)[0]["metadata"]["record_id"] == "latency-memory"


def test_sql_feed_bulk_collision_rolls_back_page_and_cursor(mind):
    from pipmind.feeds.common import FeedError
    from pipmind.feeds.fixtures import pulse
    from pipmind.feeds.sql_store import SqlFeedStore

    store = SqlFeedStore(mind.connect)
    original = {**pulse("news", MOMENT), "event_id": uuid4().hex, "headline": "Case Sensitive"}
    store.page("news", [original], "seed:" + uuid4().hex, 1, MOMENT)
    before = store.events(limit=500)
    key = "rollback:" + uuid4().hex
    new = {**original, "event_id": uuid4().hex}
    with pytest.raises(FeedError, match="conflicting_normalized_event"):
        store.page("news", [new, {**original, "headline": "case sensitive"}], key, 2, MOMENT)
    assert store.state(key) is None and store.events(limit=500) == before


def test_status_reports_committed_progress_before_a_backlog_batch_finishes(mind):
    import threading

    from fastapi.testclient import TestClient

    from pipmind.contracts import stamp
    from pipmind.shadow.service import create_app

    started, release = threading.Event(), threading.Event()
    identifiers = [uuid4().hex for _ in range(2)]
    events = [
        {
            "sequence": i + 1,
            "event": {
                "schema_version": 1,
                "kind": "ClockEvent",
                "message_id": key,
                "available_at": stamp(MOMENT),
                "payload": {
                    "schema_version": 1,
                    "event_id": key,
                    "ts": stamp(MOMENT),
                    "reason": "daily_brief",
                },
            },
        }
        for i, key in enumerate(identifiers)
    ]
    mind.accept("heartbeat-test", events, MOMENT)
    mind.set_paused(False)

    class Model:
        async def triage(self, event, *args):
            if event["message_id"] == identifiers[1]:
                started.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
            return {"wake": False, "summary": "Synthetic heartbeat check"}, {"synthetic": True}

    token = "synthetic-heartbeat-token-" + uuid4().hex
    with TestClient(create_app(mind, Model(), token, scheduled_wakes=False)) as client:
        try:
            assert started.wait(15)
            status = client.get("/status", headers={"Authorization": "Bearer " + token}).json()
            assert status["outcomes"] == 1 and status["last_progress"] is not None
            assert status["last_error"] is None
        finally:
            release.set()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError, "stall"])
def test_worker_fault_preserves_pending_input_and_durable_hold(mind, failure):
    import threading
    import time

    from fastapi.testclient import TestClient

    from pipmind.shadow.service import create_app

    events, _ = episode(mind)
    entered = threading.Event()

    class Model:
        async def triage(self, *args):
            entered.set()
            if failure == "stall":
                await asyncio.sleep(3600)
            raise failure

    token = "synthetic-worker-token-" + uuid4().hex
    app = create_app(mind, Model(), token, scheduled_wakes=False, worker_timeout=2)
    with TestClient(app) as client:
        assert entered.wait(15)
        deadline = time.monotonic() + 15
        status = {}
        while time.monotonic() < deadline:
            status = client.get("/status", headers={"Authorization": "Bearer " + token}).json()
            if status.get("reasoning_hold"):
                break
            time.sleep(0.02)
        assert status["paused"] and status["pending"] == len(events)
        assert status["outcomes"] == 0 and status["dispatches"] == 0
        assert status["reasoning_hold"]["event_id"] == events[0]["message_id"]
        assert status["reasoning_hold"]["input_pending"]
        assert mind.pending() == events[0]
        assert mind.outcome(events[0]["message_id"]) is None
        if failure == "stall":
            deadline = time.monotonic() + 5
            while client.get("/healthz").status_code != 503 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert client.get("/healthz").status_code == 503
        else:
            assert client.get("/healthz").status_code == 200
            assert status["reasoning_worker"]["alive"]
            assert status["reasoning_worker"]["error_type"] == failure.__name__


def test_persistent_worker_advances_sql_after_child_cancellation_and_stays_healthy(mind):
    import time

    from fastapi.testclient import TestClient

    from pipmind.contracts import stamp
    from pipmind.shadow.model import FoundryModel
    from pipmind.shadow.service import create_app

    identifiers = ["worker-" + uuid4().hex for _ in range(80)]
    mind.accept(
        "worker-test",
        [
            {
                "sequence": i + 1,
                "event": {
                    "schema_version": 1,
                    "kind": "ClockEvent",
                    "message_id": key,
                    "available_at": stamp(MOMENT),
                    "payload": {
                        "schema_version": 1,
                        "event_id": key,
                        "ts": stamp(MOMENT),
                        "reason": "heartbeat",
                    },
                },
            }
            for i, key in enumerate(identifiers)
        ],
        MOMENT,
    )
    mind.set_paused(False)
    loops = set()
    calls = []

    class Agent:
        async def run(self, *args, **kwargs):
            from types import SimpleNamespace

            loops.add(id(asyncio.get_running_loop()))
            calls.append(1)
            if len(calls) == 36:
                raise asyncio.CancelledError
            return SimpleNamespace(
                text='{"wake":false,"summary":"Synthetic observation"}',
                usage_details={"input_token_count": 10, "output_token_count": 5},
            )

    model = FoundryModel(
        "https://synthetic.services.ai.azure.com/api/projects/shadow",
        "synthetic-triage",
        "synthetic-decision",
        enabled=True,
        factory=lambda **kwargs: Agent(),
    )
    token = "synthetic-worker-token-" + uuid4().hex
    with TestClient(create_app(mind, model, token, scheduled_wakes=False)) as client:
        deadline = time.monotonic() + 60
        while mind.status()["pending"] and time.monotonic() < deadline:
            assert client.get("/healthz").status_code == 200
            time.sleep(0.05)
        assert mind.status()["outcomes"] == 80 and mind.status()["pending"] == 0
        assert mind.status()["dispatches"] == 0
        assert len(loops) == 1 and len(calls) == 80
        refusal = mind.outcome(identifiers[35])
        assert refusal["status"] == "model_refused"
        assert refusal["usage"][0]["failure_code"] == "model_cancelled"
        assert mind.outcome(identifiers[36])["status"] == "observed"
        assert client.get("/status", headers={"Authorization": "Bearer " + token}).json()[
            "reasoning_worker"
        ]["alive"]


def test_many_retained_records_use_runtime_bundle_and_keep_override_provenance(mind):
    with mind.transaction() as cur:
        for i in range(150):
            meta = metadata(
                "record-" + str(i),
                "review" if i % 2 else "memory",
                "2026-10-01T00:00:00Z",
                "ancestor",
                override=i % 3 == 0,
            )
            mind._insert(cur, meta, "Retained record " + str(i))
    data = export_experience(mind, MOMENT)
    bundle = validate_bundle(data)
    assert len(bundle.records) == 153 and bundle.manifest["schema_version"] == 2
    destination = SqlMind(mind.connect, "many-" + uuid4().hex)
    import_experience(
        destination, data, MOMENT, digest("new"), "Immutable identity\nnaïve ≠ unsafe\n"
    )
    assert sum(meta["override"] for meta, body in destination.retained()[1]) == 50
    assert len(destination.search("retained", MOMENT)) == 5


def test_analysis_and_quote_coalescing_retain_inputs_without_strong_model_calls(mind):
    from pipmind.contracts import stamp
    from pipmind.feeds.fixtures import pulse

    class Model:
        decisions = 0

        async def triage(self, *a):
            return {"wake": True, "summary": "Observed"}, {}

        async def decide(self, *a):
            self.decisions += 1
            return {
                "action": "hold",
                "reasoning": "Observed",
                "evidence_ids": [],
                "working_memory": {"views": [], "position_theses": [], "waiting_for": []},
                "tool_calls": [],
            }, {}

    news = pulse("news", MOMENT)
    news["kind"] = "analysis"
    payloads = [
        {
            "schema_version": 1,
            "message_id": news["event_id"],
            "kind": "NewsEvent",
            "available_at": stamp(MOMENT),
            "payload": news,
        }
    ]
    for i in range(2):
        price = {
            "schema_version": 1,
            "event_id": "price-" + str(i),
            "instrument": "EUR/USD",
            "bid": 1.1,
            "ask": 1.2,
            "venue_ts": stamp(MOMENT),
            "received_at": stamp(MOMENT),
            "stale": False,
            "source": "stream",
        }
        payloads.append(
            {
                "schema_version": 1,
                "message_id": price["event_id"],
                "kind": "PriceEvent",
                "available_at": stamp(MOMENT),
                "payload": price,
            }
        )
    mind.accept("test", [{"sequence": i + 1, "event": e} for i, e in enumerate(payloads)], MOMENT)
    mind.set_paused(False)
    model = Model()
    engine = ShadowEngine(mind, model, clock=lambda: MOMENT)
    for _ in payloads:
        assert asyncio.run(engine.step())
    assert model.decisions == 1 and mind.status()["outcomes"] == 3
    assert mind.outcome("price-1")["status"] == "price_coalesced"


def activate_bounded(mind, moment=MOMENT):
    from pipmind.shadow.maintenance import entries, working_digest

    head = mind.head()
    mind.set_paused(True)
    head = mind.head()
    refs = [meta["record_id"] for meta, _ in mind.retained()[1] if meta["part"] == "memory"]
    plan = {
        "revision": head["revision"],
        "before_sha256": working_digest(head["working"]),
        "working_memory": head["working"],
        "dispositions": [
            {
                "from": key,
                "to": key,
                "disposition": "retained",
                "reason": "Retain exact condition and provenance",
                "evidence_ids": refs[:1],
            }
            for key in entries(head["working"])
        ],
        "review": "no-unresolved-ambiguity",
    }
    return plan, mind.maintain_working(plan, moment)


def test_bounded_activation_requires_complete_review_and_revision_and_retains_exact_state(mind):
    from pipmind.shadow.maintenance import working_digest

    events, model = episode(mind)
    assert asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    original = mind.retained()[1]
    old_working = mind.head()["working"]
    plan, report = activate_bounded(mind)
    assert report["paused"] and mind.head()["working"] == old_working
    checkpoint = next(
        body for meta, body in mind.retained()[1] if meta["record_id"] == report["checkpoint"]
    )
    assert loads(checkpoint)["before"] == old_working
    retained = {meta["record_id"]: (meta, body) for meta, body in mind.retained()[1]}
    assert all(
        retained[meta["record_id"]] == (meta, body)
        for meta, body in original
        if meta["part"] != "working_memory"
    )
    plan["revision"] = mind.head()["revision"]
    plan["before_sha256"] = working_digest(old_working)
    plan["review"] = "unreviewed"
    with pytest.raises(ShadowError, match="working_memory_review_required"):
        mind.maintain_working(plan, MOMENT)
    plan["review"] = "no-unresolved-ambiguity"
    plan["before_sha256"] = "0" * 64
    with pytest.raises(ShadowError, match="working_memory_source_changed"):
        mind.maintain_working(plan, MOMENT)
    plan["before_sha256"] = working_digest(old_working)
    if plan["dispositions"]:
        plan["dispositions"][0]["evidence_ids"] = ["unknown-evidence"]
        with pytest.raises(ShadowError, match="working_memory_evidence"):
            mind.maintain_working(plan, MOMENT)
    assert mind.head()["working"] == old_working and mind.head()["paused"]
    assert mind.outcome(events[0]["message_id"])["reply"] is not None


def test_pre_rpc_overflow_holds_pending_and_pause_survives_restart_while_feeds_accept(mind):
    from pipmind.shadow.model import ModelFailure

    events, _ = episode(mind)
    before = mind.head()["working"]

    class Overflow:
        calls = 0

        async def triage(self, *args):
            self.calls += 1
            raise ModelFailure(
                {
                    "phase": "triage",
                    "attempted": False,
                    "prompt_bytes": 16001,
                    "failure_code": "shadow_context_size",
                    "usage_unknown": False,
                }
            )

    model = Overflow()
    assert not asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    restarted = SqlMind(mind.connect, mind.trader)
    assert restarted.head()["paused"] and restarted.pending() == events[0]
    assert restarted.outcome(events[0]["message_id"]) is None
    assert restarted.head()["working"] == before
    assert restarted.status()["reasoning_hold"]["reason"] == "shadow_context_size"
    assert not asyncio.run(ShadowEngine(restarted, model, clock=lambda: MOMENT).step())
    assert model.calls == 1
    new = deepcopy(events[0])
    new["message_id"] = new["payload"]["event_id"] = "new-during-hold"
    restarted.accept("continues", [{"sequence": 1, "event": new}], MOMENT)
    assert restarted.status()["pending"] == 2
    held = [loads(body) for meta, body in restarted.retained()[1] if meta["part"] == "journal"]
    assert held[0]["input"] == events[0] and held[0]["decision"] is None


def test_rejected_memory_transition_is_held_with_candidate_and_usage_then_retried(mind):
    from pipmind.shadow.common import encoded

    activate_bounded(mind)
    events, model = episode(mind)
    candidate = model.responses[events[0]["message_id"]]["reply"]
    candidate["working_memory"] = {"views": ["x" * 301], "waiting_for": [], "position_theses": []}
    assert not asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    assert mind.head()["paused"] and mind.outcome(events[0]["message_id"]) is None
    held = [loads(body) for meta, body in mind.retained()[1] if meta["part"] == "journal"][-1]
    assert held["model_reply"] == candidate and held["usage"][-1]["phase"] == "decision"
    candidate["working_memory"] = mind.head()["working"]
    mind.set_paused(False)
    assert asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    assert mind.status()["reasoning_hold"] is None
    assert mind.outcome(events[0]["message_id"])["status"] == "observed"
    assert encoded(held) in [body for _, body in mind.retained()[1]]


def test_reconsideration_has_new_identity_original_outcome_exact_and_no_cursor_rewind(mind):
    from pipmind.shadow.common import encoded

    events, _ = episode(mind)
    event = events[0]
    refused = {
        "event_id": event["message_id"],
        "status": "model_refused",
        "reply": None,
        "dispatches": 0,
        "usage": [{"phase": "triage", "attempted": False, "prompt_bytes": 17000}],
    }
    mind.commit(event, mind.head()["revision"], refused, MOMENT)
    original = encoded(mind.outcome(event["message_id"]))
    mind.set_paused(True)
    moment = MOMENT + timedelta(minutes=5)
    identifier = mind.queue_reconsideration(event["message_id"], moment)
    assert mind.queue_reconsideration(event["message_id"], moment) == identifier
    queued = mind.pending()
    assert queued["message_id"] == identifier and identifier != event["message_id"]
    assert utc(queued["available_at"]) == moment
    with pytest.raises(ShadowError, match="reconsideration_owner_operation_required"):
        mind.accept("forged", [{"sequence": 1, "event": queued}], moment)
    reply = deepcopy(CASE["model_responses"][event["message_id"]]["reply"])
    model = FixtureModel(
        {
            identifier: {
                "triage": {"wake": True, "summary": "Present-time reconsideration"},
                "reply": reply,
            }
        }
    )
    mind.set_paused(False)
    assert asyncio.run(ShadowEngine(mind, model, clock=lambda: moment).step())
    assert encoded(mind.outcome(event["message_id"])) == original
    outcome = mind.outcome(identifier)
    assert outcome["context"]["reconsideration"]["original_input"] == event
    assert outcome["ts"] == moment.isoformat().replace("+00:00", "Z")
    assert mind.checkpoint("fixture-source") == len(events)
    assert mind.status()["outcomes"] == 2 and mind.status()["dispatches"] == 0
    with pytest.raises(ShadowError, match="reconsideration_requires_pause"):
        mind.queue_reconsideration(identifier, moment)
    mind.set_paused(True)
    with pytest.raises(ShadowError, match="not_a_context_refusal"):
        mind.queue_reconsideration(identifier, moment)


def test_bounded_state_multistep_growth_retains_every_checkpoint_and_complete_clone(mind):
    from pipmind.shadow.common import encoded

    activate_bounded(mind)
    mind.set_paused(False)
    model_responses = {}
    previous = None
    for i in range(20):
        moment = MOMENT + timedelta(seconds=i)
        event = deepcopy(CASE["events"][0])
        identifier = f"growth-{i}"
        event["message_id"] = event["payload"]["event_id"] = identifier
        mind.accept("growth", [{"sequence": i + 1, "event": event}], moment)
        reply = deepcopy(CASE["model_responses"][CASE["events"][0]["message_id"]]["reply"])
        reply["evidence_ids"] = [identifier]
        reply["working_memory"] = {
            "views": [f"USD dated observation {i}; uncertainty remains"],
            "waiting_for": ["Await verified CPI; invalidate on revision"],
            "position_theses": [],
        }
        reply["memory_dispositions"] = (
            []
            if previous is None
            else [
                {
                    "from": "v0",
                    "to": "v0",
                    "disposition": "updated",
                    "reason": "Latest verified observation; preserve uncertainty",
                    "evidence_ids": [identifier],
                }
            ]
        )
        model_responses[identifier] = {
            "triage": {"wake": True, "summary": "Current evidence"},
            "reply": reply,
        }
        assert asyncio.run(
            ShadowEngine(
                mind, FixtureModel(model_responses), clock=lambda moment=moment: moment
            ).step()
        )
        assert mind.outcome(identifier)["status"] == "observed"
        assert len(encoded(mind.head()["working"]).encode()) < 3200
        previous = reply
    records = mind.retained()[1]
    checkpoints = [
        loads(body)
        for meta, body in records
        if meta["part"] == "memory" and meta["record_id"].startswith("working-checkpoint:")
    ]
    assert len(checkpoints) == 21
    assert all(
        cp["after"]["waiting_for"] == ["Await verified CPI; invalidate on revision"]
        for cp in checkpoints[1:]
    )
    moment += timedelta(seconds=1)
    bundle = export_experience(mind, moment)
    target = SqlMind(mind.connect, "bounded-clone-" + uuid4().hex)
    import_experience(
        target, bundle, moment, digest("separate"), "Immutable identity\nnaïve ≠ unsafe\n"
    )
    originals = {
        meta["record_id"]: body for meta, body in records if meta["part"] != "working_memory"
    }
    cloned = {meta["record_id"]: body for meta, body in target.retained()[1]}
    assert all(cloned[key] == value for key, value in originals.items())
    assert target.head()["paused"] and target.status()["dispatches"] == 0


def test_failed_maintenance_rolls_back_policy_record_and_working_and_stale_hold_refuses(
    mind, monkeypatch
):
    from pipmind.shadow.maintenance import working_digest

    events, model = episode(mind)
    asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    mind.set_paused(True)
    head, records = mind.retained()
    before = head["working"]
    refs = [events[0]["message_id"]]
    from pipmind.shadow.maintenance import entries

    plan = {
        "revision": head["revision"],
        "before_sha256": working_digest(before),
        "working_memory": before,
        "dispositions": [
            {
                "from": key,
                "to": key,
                "disposition": "retained",
                "reason": "Preserve every condition",
                "evidence_ids": refs,
            }
            for key in entries(before)
        ],
        "review": "no-unresolved-ambiguity",
    }
    real = mind._working_checkpoint

    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise ShadowError("interrupted")

    monkeypatch.setattr(mind, "_working_checkpoint", interrupted)
    with pytest.raises(ShadowError, match="interrupted"):
        mind.maintain_working(plan, MOMENT)
    assert mind.head()["working"] == before
    assert mind.runtime("memory_policy") is None
    assert mind.retained()[1] == records
    with pytest.raises(ShadowError, match="shadow_revision_changed"):
        mind.hold(events[0], head["revision"] - 1, {}, MOMENT, "shadow_context_size")
    assert mind.retained()[1] == records


def test_exact_checkpoint_focus_retrieves_old_condition_after_newer_revisions(mind):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.maintenance import entries, working_digest

    old = {
        "views": [f"Institution view {i}" for i in range(8)],
        "waiting_for": [
            f"Specific condition {i}: confirm quartz methodology and invalidation {i}"
            for i in range(8)
        ],
        "position_theses": [],
    }
    with mind.transaction() as cur:
        mind._write_working(cur, old, MOMENT)
        mind._insert(
            cur, metadata("source", "memory", MOMENT.isoformat(), mind.trader), "Known dated source"
        )
    plan = {
        "revision": mind.head()["revision"],
        "before_sha256": working_digest(old),
        "working_memory": {
            "views": ["Institution views remain uncertain"],
            "waiting_for": [
                "Confirm every individual quartz methodology/invalidation in the source checkpoint"
            ],
            "position_theses": [],
        },
        "dispositions": [
            {
                "from": key,
                "to": key[0] + "0",
                "disposition": "merged",
                "reason": "Retain every unresolved prerequisite in named group",
                "evidence_ids": ["source"],
            }
            for key in entries(old)
        ],
        "review": "no-unresolved-ambiguity",
    }
    report = mind.maintain_working(plan, MOMENT)
    result = mind.search(
        report["checkpoint"] + " | w7 Specific condition 7 invalidation 7", MOMENT
    )[0]
    assert result["partial"] and result["metadata"]["record_id"] == report["checkpoint"]
    assert old["waiting_for"][7] in result["body"]
    assert old["waiting_for"][7] in encoded(mind.retained()[1])


def test_bounded_sql_retrieval_reaches_actual_request_compiler_and_native_tool_repeatedly(mind):
    from types import SimpleNamespace

    from pipmind.shadow.common import encoded
    from pipmind.shadow.model import FoundryModel

    activate_bounded(mind)
    requests = []
    reads = []
    working = {
        "views": ["USD CPI uncertainty remains"],
        "waiting_for": ["Confirm CPI vintage"],
        "position_theses": [],
    }

    def factory(phase, instructions, tools):
        class Agent:
            async def run(self, message, **kwargs):
                request = loads(message)
                requests.append(request)
                if phase == "triage":
                    result = {"wake": True, "summary": "USD CPI observation"}
                else:
                    policy = request["context"]["memory_policy"]
                    reads.append(loads(tools[0](policy["checkpoint"] + " | w0 CPI")))
                    result = {
                        "action": "hold",
                        "reasoning": "Preserve current CPI uncertainty",
                        "evidence_ids": [request["event"]["message_id"]],
                        "working_memory": working,
                        "tool_calls": [],
                    }
                return SimpleNamespace(
                    text=encoded(result),
                    usage_details={"input_token_count": 10, "output_token_count": 5},
                )

        return Agent()

    model = FoundryModel(
        "https://synthetic.services.ai.azure.com/api/projects/shadow",
        "t",
        "d",
        enabled=True,
        factory=factory,
    )
    mind.set_paused(False)
    for i in range(3):
        event = deepcopy(CASE["events"][0])
        event["message_id"] = event["payload"]["event_id"] = f"compiler-cpi-{i}"
        mind.accept("compiler", [{"sequence": i + 1, "event": event}], MOMENT)
        assert asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
        assert mind.outcome(event["message_id"])["status"] == "observed"
    assert len(requests) == 6 and len(reads) == 3
    assert reads[-1][0]["partial"]
    assert "Confirm CPI vintage" in reads[-1][0]["body"]
    assert any(
        "working-memory-transition" in card["body"]
        for request in requests
        if request["phase"] == "decision"
        for card in request["context"]["retrieved_memories"]
    )
    assert mind.head()["working"] == working


def test_request_assembly_bug_holds_actual_input_without_claiming_an_rpc(mind, monkeypatch):
    from pipmind.shadow.model import FoundryModel

    events, _ = episode(mind)

    def broken(*args):
        raise KeyError("before")

    monkeypatch.setattr("pipmind.shadow.model.phase_context", broken)
    calls = []
    model = FoundryModel(
        "https://synthetic.services.ai.azure.com/api/projects/shadow",
        "t",
        "d",
        enabled=True,
        factory=lambda **kwargs: calls.append(kwargs),
    )
    assert not asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    assert calls == [] and mind.pending() == events[0]
    assert mind.head()["paused"] and mind.outcome(events[0]["message_id"]) is None
    held = [loads(body) for meta, body in mind.retained()[1] if meta["part"] == "journal"][-1]
    assert held["usage"][0]["attempted"] is False
    assert held["usage"][0]["usage_unknown"] is False
    assert mind.status()["reasoning_hold"]["reason"] == "shadow_context_projection"


@pytest.mark.parametrize("invalid", [None, [], {"kind": "ClockEvent", "payload": None}])
def test_owner_control_check_validates_envelope_before_inspecting_its_payload(mind, invalid):
    with pytest.raises(ShadowError, match="invalid_shadow_input"):
        mind.accept("invalid-control", [{"sequence": 1, "event": invalid}], MOMENT)
    assert mind.checkpoint("invalid-control") == 0
    assert mind.pending() is None


def test_private_http_rejects_malformed_and_forged_owner_inputs_without_advancing(mind):
    from fastapi.testclient import TestClient

    from pipmind.shadow.service import create_app

    token = "synthetic-private-token-32-characters"
    with TestClient(create_app(mind, None, token, background=False)) as client:
        headers = {"Authorization": "Bearer " + token}
        response = client.post(
            "/inputs",
            headers=headers,
            json={"source": "bad-input", "events": [{"sequence": 1, "event": None}]},
        )
        assert response.status_code == 422
        assert response.json() == {"error": "invalid_shadow_input"}
        event = {
            "schema_version": 1,
            "kind": "ClockEvent",
            "message_id": "forged",
            "available_at": MOMENT.isoformat(),
            "payload": {
                "schema_version": 1,
                "event_id": "forged",
                "ts": MOMENT.isoformat(),
                "reason": "reconsideration",
                "reconsideration_of": "unknown",
            },
        }
        response = client.post(
            "/inputs",
            headers=headers,
            json={"source": "forged", "events": [{"sequence": 1, "event": event}]},
        )
        assert response.status_code == 403
        assert mind.checkpoint("bad-input") == mind.checkpoint("forged") == 0
        assert mind.pending() is None


@pytest.mark.parametrize(
    "invalid_candidate,failed_correction",
    [
        (kind, failed)
        for kind in ("oversized", "missing_dispositions", "entry_patch")
        for failed in (False, True)
    ]
    + [("entry_spacing", False)],
)
def test_sql_bounded_correction_commits_once_or_holds_with_all_attempts(
    mind, failed_correction, invalid_candidate
):
    from pipmind.shadow.common import encoded

    before = {
        "views": ["USD uncertain"],
        "waiting_for": ["Wait for verified vintage"],
        "position_theses": [],
    }
    with mind.transaction() as cur:
        mind._insert(
            cur,
            metadata("before-proof", "memory", "2026-10-01T00:00:00Z", mind.trader),
            "USD uncertainty; wait for verified vintage",
        )
        mind._write_working(cur, before, MOMENT)
    activate_bounded(mind)
    events, model = episode(mind)
    event = events[0]
    candidate = deepcopy(model.responses[event["message_id"]]["reply"])
    candidate.update(
        evidence_ids=[event["message_id"]],
        tool_calls=[],
        working_memory={**before, "views": ["USD uncertain " + "." * 301]},
    )
    if invalid_candidate == "missing_dispositions":
        candidate["working_memory"]["views"] = ["USD uncertainty remains"]
        candidate.pop("memory_dispositions", None)
    if invalid_candidate in {"entry_patch", "entry_spacing"}:
        candidate["memory_dispositions"] = [
            {
                "from": "v0",
                "to": "v0",
                "disposition": "updated",
                "reason": "New wording for the same uncertainty",
                "evidence_ids": [event["message_id"]],
            }
        ]
    if invalid_candidate == "entry_spacing":
        candidate["working_memory"]["views"] = ["USD: " + "x" * 274 + "; risk; wait; uncertainty"]
    model.responses[event["message_id"]]["reply"] = candidate
    model.responses[event["message_id"]]["triage"]["wake"] = True
    after = {**before, "views": ["USD uncertainty retained"]}
    if invalid_candidate == "entry_spacing":
        after["views"] = [
            candidate["working_memory"]["views"][0].replace(": ", ":").replace("; ", ";")
        ]

    def disposition(key, state):
        return {
            "from": key,
            "to": key,
            "disposition": state,
            "reason": "Same explicit dependency in concise wording",
            "evidence_ids": [event["message_id"]],
        }

    correction = {
        "working_memory": after,
        "memory_dispositions": [disposition("v0", "updated"), disposition("w0", "updated")],
        "candidate_dispositions": [disposition("v0", "updated"), disposition("w0", "merged")],
    }

    class Repair(FixtureModel):
        calls = 0

        async def repair_memory(self, *args):
            self.calls += 1
            if invalid_candidate == "entry_patch":
                return {
                    "replacements": [
                        {
                            "from": "v0",
                            "text": (
                                candidate["working_memory"]["views"][0]
                                if failed_correction
                                else after["views"][0]
                            ),
                            "reason": "Short wording with uncertainty retained",
                            "evidence_ids": [event["message_id"]],
                        }
                    ]
                }, {"phase": "memory_repair", "input_tokens": 123, "output_tokens": 45}
            reply = deepcopy(correction)
            if failed_correction:
                reply["working_memory"] = deepcopy(candidate["working_memory"])
                if invalid_candidate == "missing_dispositions":
                    reply["candidate_dispositions"] = []
            return reply, {"phase": "memory_repair", "input_tokens": 123, "output_tokens": 45}

    live = Repair(model.responses)
    old = mind.retained()[1]
    assert (
        asyncio.run(ShadowEngine(mind, live, clock=lambda: MOMENT).step()) is not failed_correction
    )
    assert live.calls == (0 if invalid_candidate == "entry_spacing" else 1)
    if failed_correction:
        assert mind.head()["paused"] and mind.pending() == event
        assert mind.head()["working"] == before and mind.outcome(event["message_id"]) is None
        journal = [loads(body) for meta, body in mind.retained()[1] if meta["part"] == "journal"][
            -1
        ]
        assert journal["status"] == "held" and journal["error"] == "working_memory_repair_failed"
    else:
        journal = mind.outcome(event["message_id"])
        assert journal["status"] == "observed" and not mind.head()["paused"]
        assert mind.head()["working"] == after and mind.pending() is None
        if invalid_candidate in {"entry_patch", "entry_spacing"}:
            assert journal["reply"]["memory_dispositions"] == candidate["memory_dispositions"]
            assert (
                journal["memory_repair"]["response"]["replacements"][0]["text"] == after["views"][0]
            )
            assert journal["memory_repair"]["expanded_response"]["working_memory"] == after
            if invalid_candidate == "entry_spacing":
                assert journal["memory_repair"]["method"] == "punctuation_spacing"
        else:
            assert journal["reply"]["memory_dispositions"][-1]["disposition"] == "retained"
            assert journal["memory_repair"]["response"] == correction
            assert len(journal["memory_repair"]["mapping_normalizations"]) == 2
        for key in candidate.keys() - {"working_memory", "memory_dispositions"}:
            assert journal["reply"][key] == candidate[key]
    assert journal["memory_repair"]["candidate"] == candidate
    assert journal["memory_repair"]["reason"] == (
        "working_memory_bound"
        if invalid_candidate != "missing_dispositions"
        else "working_memory_dispositions"
    )
    assert [row["phase"] for row in journal["usage"]] == ["triage", "decision", "memory_repair"]
    assert journal["usage"][-1]["input_tokens"] == (
        0 if invalid_candidate == "entry_spacing" else 123
    )
    assert journal["dispatches"] == 0
    retained = {meta["record_id"]: (meta, body) for meta, body in mind.retained()[1]}
    assert all(
        retained[meta["record_id"]] == (meta, body)
        for meta, body in old
        if meta["part"] != "working_memory"
    )
    assert encoded(before) in [
        encoded(loads(body)["before"])
        for meta, body in mind.retained()[1]
        if meta["part"] == "memory" and '"working-memory-transition"' in body
    ]


def test_repeated_owner_maintenance_keeps_original_checkpoint_and_exact_prior_records(mind):
    from pipmind.shadow.maintenance import entries, working_digest

    _, first = activate_bounded(mind)
    original_policy = mind.runtime("memory_policy")
    original_records = mind.retained()[1]
    head = mind.head()
    second = mind.maintain_working(
        {
            "revision": head["revision"],
            "before_sha256": working_digest(head["working"]),
            "working_memory": head["working"],
            "dispositions": [
                {
                    "from": key,
                    "to": key,
                    "disposition": "retained",
                    "reason": "Preserve every current dependency",
                    "evidence_ids": [first["checkpoint"]],
                }
                for key in entries(head["working"])
            ],
            "review": "no-unresolved-ambiguity",
        },
        MOMENT + timedelta(seconds=1),
    )
    policy = mind.runtime("memory_policy")
    assert policy["origin_checkpoint"] == original_policy["origin_checkpoint"]
    assert policy["checkpoint"] == second["checkpoint"] != first["checkpoint"]
    after = {meta["record_id"]: (meta, body) for meta, body in mind.retained()[1]}
    assert all(
        after[meta["record_id"]] == (meta, body)
        for meta, body in original_records
        if meta["part"] != "working_memory"
    )
    checkpoint = loads(after[second["checkpoint"]][1])
    assert not checkpoint["initial"] and checkpoint["previous_checkpoint"] == first["checkpoint"]
    assert mind.head()["paused"] and mind.status()["dispatches"] == 0


def test_stream_experience_large_sql_checkpoint_clone_and_atomic_rollback(mind, tmp_path):
    from pipmind.shadow.stream_bundle import export_experience_file, import_experience_file
    from pipmind.stream_portability import validate_stream

    # Generated payloads stay in the private SQL/container test environment.
    with mind.transaction() as cur:
        for i in range(36):
            mind._insert(
                cur,
                metadata(
                    f"large:{i}", "journal", MOMENT.isoformat(), mind.trader, ["mind:identity"]
                ),
                f"Exact retained entry {i}: " + "a" * 1000000,
            )
    path = tmp_path / "complete.zip"
    before = mind.head()
    report = export_experience_file(mind, path, MOMENT)
    assert report["bytes"] > 33554432 and report["records"] == 39
    assert mind.head() == before

    identity = "Immutable identity\nnaïve ≠ unsafe\n"
    destination = SqlMind(mind.connect, "stream-clone-" + uuid4().hex)
    calls = 0

    def interrupted():
        nonlocal calls
        calls += 1
        if calls == 10:
            raise RuntimeError("simulated interruption")

    with pytest.raises(ShadowError):
        import_experience_file(
            destination, path, MOMENT, digest("new-binding"), identity, interrupt=interrupted
        )
    with destination.transaction() as cur:
        for table in ("pm_minds", "pm_records", "pm_runtime", "pm_inputs", "pm_outcomes"):
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE trader=%s", (destination.trader,))
            assert cur.fetchone()[0] == 0
    result = import_experience_file(destination, path, MOMENT, digest("new-binding"), identity)
    assert result["paused"] and destination.head()["binding"] == digest("new-binding")
    assert destination.head()["lineage"] != before["lineage"]
    assert destination.head()["working"]["views"] == []
    assert destination.status()["outcomes"] == destination.status()["pending"] == 0
    with validate_stream(path) as bundle:
        with destination.stream_retained() as (_, actual):
            old = iter(bundle.records())
            for index, (meta, body) in enumerate(actual):
                if index == 39:
                    assert meta["part"] == "working_memory"
                    continue
                expected_meta, expected_body = next(old)
                if expected_meta["part"] == "working_memory":
                    expected_meta = {**expected_meta, "part": "memory"}
                assert (meta, body) == (expected_meta, expected_body)
            assert next(old, None) is None
    with pytest.raises(ShadowError, match="clone_requires_empty_destination"):
        import_experience_file(destination, path, MOMENT, digest("new-binding"), identity)
    assert mind.head() == before


def test_stream_checkpoint_serializes_source_writer_without_losing_new_history(mind):
    import threading

    entered, finished = threading.Event(), threading.Event()
    errors = []

    def writer():
        entered.set()
        try:
            with mind.transaction() as cur:
                mind._insert(
                    cur,
                    metadata("after-checkpoint", "journal", MOMENT.isoformat(), mind.trader),
                    "New history after the captured checkpoint",
                )
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            finished.set()

    with mind.stream_retained(snapshot=False) as (_, records):
        worker = threading.Thread(target=writer)
        worker.start()
        assert entered.wait(1) and not finished.wait(0.25)
        captured = list(records)
        assert "after-checkpoint" not in {meta["record_id"] for meta, _ in captured}
    worker.join(10)
    assert finished.is_set() and not errors
    assert "after-checkpoint" in {meta["record_id"] for meta, _ in mind.retained()[1]}


def test_stream_corruption_during_sql_consumption_rolls_back_every_row(mind, tmp_path, monkeypatch):
    import zipfile

    from pipmind.shadow.stream_bundle import export_experience_file, import_experience_file

    with mind.transaction() as cur:
        for identifier in ("padding", "corrupt-later"):
            mind._insert(
                cur,
                metadata(identifier, "journal", MOMENT.isoformat(), mind.trader),
                "Exact retained history " * 3000,
            )
    path = tmp_path / "source.zip"
    export_experience_file(mind, path, MOMENT)
    destination = SqlMind(mind.connect, "corruption-" + uuid4().hex)
    clone = destination.clone_stream

    def corrupt(bundle, *args, **kwargs):
        with zipfile.ZipFile(bundle.archive.fp.name) as archive:
            item = archive.getinfo("mind/journal/" + digest("corrupt-later") + ".md")
            offset = item.header_offset + 30 + len(item.filename.encode())
        modified = False

        def interrupt():
            nonlocal modified
            if not modified:
                modified = True
                with open(bundle.archive.fp.name, "r+b") as stream:
                    stream.seek(offset)
                    stream.write(b"x")

        return clone(bundle, *args, interrupt=interrupt)

    monkeypatch.setattr(destination, "clone_stream", corrupt)
    with pytest.raises(ShadowError):
        import_experience_file(
            destination, path, MOMENT, digest("new"), "Immutable identity\nnaïve ≠ unsafe\n"
        )
    with destination.transaction() as cur:
        cur.execute("SELECT COUNT(*) FROM pm_minds WHERE trader=%s", (destination.trader,))
        assert cur.fetchone()[0] == 0


@pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_LOCAL") != "true",
    reason="database option setup is restricted to disposable local SQL",
)
def test_stream_snapshot_preserves_checkpoint_while_source_writer_commits(mind, monkeypatch):
    import threading

    from pipmind.shadow.common import encoded

    name = "stream_snapshot_" + uuid4().hex
    admin = mind.connect()
    admin.autocommit = True
    control = admin.cursor()
    control.execute(f"CREATE DATABASE [{name}]")
    try:
        control.execute(f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
        migrate(connect)
        source = SqlMind(connect, "snapshot-source")
        source.initialize(
            "Exact identity",
            {"version": "v1", "text": "Exact playbook"},
            MOMENT.isoformat(),
            binding=digest("source"),
        )
        finished, errors = threading.Event(), []

        def writer():
            try:
                with source.transaction() as cur:
                    source._insert(
                        cur,
                        metadata("new-history", "journal", MOMENT.isoformat(), source.trader),
                        "Committed after snapshot",
                    )
                    source._write_working(
                        cur,
                        {"views": ["New state"], "position_theses": [], "waiting_for": []},
                        MOMENT,
                    )
                    cur.execute(
                        "UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (source.trader,)
                    )
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                finished.set()

        read_head = source._head
        worker = threading.Thread(target=writer)

        def head_then_commit(cur):
            head = read_head(cur)
            # Commit BETWEEN checkpoint queries, not after all SELECTs ran.
            # Statement-level RCSI would now mix the old head with new records.
            worker.start()
            assert finished.wait(4), "snapshot reader blocked the source writer"
            return head

        with monkeypatch.context() as patch:
            patch.setattr(source, "_head", head_then_commit)
            with source.stream_retained() as (head, records):
                assert head["checkpoint_isolation"] == "snapshot" and head["revision"] == 0
                captured = list(records)
                assert len(captured) == 3 and not errors
                original_working = next(
                    body for meta, body in captured if meta["part"] == "working_memory"
                )
                assert original_working == encoded(head["working"])
                assert loads(original_working)["views"] == []
        worker.join(5)
        assert head["checkpoint_lock_seconds"] == 0
        assert source.head()["revision"] == 1
        assert source.head()["working"]["views"] == ["New state"]
        assert len(source.retained()[1]) == 4
    finally:
        control.execute(f"DROP DATABASE [{name}]")
        admin.close()


def test_closed_sql_session_keeps_original_error_and_rolls_back_staged_data(mind):
    from pipmind.shadow.sql import transaction

    key = "closed-session-" + uuid4().hex
    db = mind.connect()
    with pytest.raises(ShadowError, match="original_operation_refused"):
        with transaction(lambda: db, "closed-session-test") as cur:
            cur.execute("INSERT pm_runtime VALUES(%s,%s,%s)", (mind.trader, key, "true"))
            db.close()  # Actual disconnected SQL session, not a mock rollback.
            raise ShadowError("original_operation_refused")
    assert mind.runtime(key) is None
    assert mind.status()["outcomes"] == 0


@pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_LOCAL") != "true",
    reason="short-timeout fault connection is disposable local SQL only",
)
def test_sql_lock_timeout_disposes_connection_and_followup_transaction_recovers(mind):
    import time

    import pytds

    from pipmind.shadow.sql import transaction

    assert os.environ["PIPMIND_SQL_SERVER"] in {"localhost", "127.0.0.1", "sql"}
    key = "lock-timeout-" + uuid4().hex
    blocker = mind.connect()
    cursor = blocker.cursor()
    cursor.execute("SELECT COUNT(*) FROM pm_records WITH (TABLOCKX,HOLDLOCK)")
    cursor.fetchone()

    def short_connection():
        return pytds.connect(
            dsn=os.environ["PIPMIND_SQL_SERVER"],
            database=os.environ["PIPMIND_SQL_DATABASE"],
            user=os.environ["PIPMIND_SQL_USER"],
            password=os.environ["PIPMIND_SQL_PASSWORD"],
            port=int(os.environ.get("PIPMIND_SQL_PORT", "1433")),
            timeout=0.2,
            autocommit=False,
            use_mars=False,
            pooling=False,
        )

    try:
        started = time.monotonic()
        with pytest.raises(ShadowError, match="shadow_store_transaction_failed"):
            with transaction(short_connection, "timeout-fault-test") as cur:
                cur.execute("INSERT pm_runtime VALUES(%s,%s,%s)", (mind.trader, key, "true"))
                cur.execute("SELECT COUNT(*) FROM pm_records")
                cur.fetchone()
        assert time.monotonic() - started < 3
    finally:
        blocker.close()
    assert mind.runtime(key) is None
    assert mind.head()["paused"]


def test_fresh_broker_facts_survive_processing_an_older_account_input_without_model_rpc(mind):
    from unittest.mock import Mock

    old = {"schema_version": 1, "as_of": MOMENT.isoformat(), "currency": "USD", "balance": 100}
    fresh = {**old, "as_of": (MOMENT + timedelta(seconds=60)).isoformat(), "balance": 110}
    positions = {"schema_version": 1, "as_of": fresh["as_of"], "positions": []}
    mind.refresh_broker_facts(fresh, positions, MOMENT + timedelta(seconds=60))
    event = {
        "schema_version": 1,
        "kind": "AccountSnapshot",
        "message_id": "snapshot-old",
        "available_at": MOMENT.isoformat(),
        "payload": old,
    }
    mind.accept("gateway", [{"sequence": 1, "event": event}], MOMENT)
    mind.set_paused(False)
    model = Mock()
    assert asyncio.run(
        ShadowEngine(mind, model, clock=lambda: MOMENT + timedelta(seconds=60)).step()
    )
    assert mind.runtime("broker_snapshots")["account"] == fresh
    assert mind.outcome("snapshot-old")["status"] == "snapshot_retained"
    assert mind.outcome("snapshot-old")["usage"] == []
    model.triage.assert_not_called()
    assert mind.status()["outcomes"] == 1


@pytest.mark.parametrize("quote_policy", ["fresh_ticks", "observed_staleness"])
def test_qualification_slots_survive_restart_and_cannot_erase_a_capture_gap(
    mind, monkeypatch, quote_policy
):
    from pipmind.shadow import qualification as q

    moment = utc("2026-10-19T22:00:00Z")
    clock = [moment]
    mind.set_paused(False)
    monkeypatch.setenv("BUILD_SHA", "synthetic-build")

    def state():
        return {
            **mind.status(),
            "last_error": None,
            "reasoning_worker": {"alive": True, "loop_alive": True, "supervisor_alive": True},
        }

    def read(gateway, path):
        at = clock[0].isoformat()
        if path == "/evidence":
            return {
                "last_error": None,
                "evidence": {
                    "as_of": at,
                    "from": moment.isoformat(),
                    "binding_sha256": "b" * 64,
                    "environment": "demo",
                    "complete": True,
                    "activity_count": 0,
                    "responses": {},
                },
            }
        return {
            "checked_at": at,
            "stream_connected": True,
            "dispatch_enabled": False,
            "broker_verified": True,
            "worker_alive": True,
            "last_cycle": at,
            "prices": [
                {
                    "instrument": "EUR/USD",
                    "stale": quote_policy == "observed_staleness",
                    "venue_ts": (
                        clock[0] - timedelta(seconds=30)
                        if quote_policy == "observed_staleness"
                        else clock[0]
                    ).isoformat(),
                    "received_at": at,
                    "source": "stream",
                }
            ],
            "snapshots": [{"as_of": at}, {"as_of": at}],
            "last_error": None,
        }

    monkeypatch.setattr(q, "read_gateway", read)
    q.arm(mind, "synthetic-day", ["EUR/USD"], "synthetic-build", moment, quote_policy=quote_policy)
    recorder = q.Recorder(mind, None, state)
    recorder.capture(moment)
    first = mind.runtime(q.ACTIVE)
    assert first["state"] == "running" and first["samples"] == 1
    recorder.capture(moment)
    assert mind.runtime(q.ACTIVE) == first
    clock[0] += timedelta(minutes=1)
    q.Recorder(mind, None, state).capture(clock[0])
    assert mind.runtime(q.ACTIVE)["samples"] == 2
    # Elapsed wall time without intermediate samples can never pass the day.
    clock[0] += timedelta(hours=24)
    q.Recorder(mind, None, state).capture(clock[0])
    final = mind.runtime(q.ACTIVE)
    assert final["state"] == "capture_failed" and "capture_gap" in final["failures"]
    with mind.transaction() as c:
        c.execute(
            "SELECT COUNT(*) FROM pm_runtime WHERE trader=%s AND k LIKE %s",
            (mind.trader, "qualification:synthetic-day:sample:%"),
        )
        assert c.fetchone()[0] == 3
    assert mind.status()["outcomes"] == 0  # Capture never invents reasoning.


@pytest.mark.parametrize("build_changed", [False, True])
def test_qualification_warmup_retains_read_failures_without_starting_the_day(
    mind, monkeypatch, build_changed
):
    from pipmind.shadow import qualification as q

    moment = utc("2026-10-19T22:00:00Z")
    monkeypatch.setenv("BUILD_SHA", "synthetic-build")
    mind.set_paused(False)
    q.arm(mind, "failed-warmup", ["EUR/USD"], "synthetic-build", moment)
    if build_changed:
        monkeypatch.setenv("BUILD_SHA", "another-build")

    def outage(*args):
        raise TimeoutError("synthetic")

    monkeypatch.setattr(q, "read_gateway", outage)
    q.Recorder(mind, None, mind.status).capture(moment)
    r = mind.runtime(q.ACTIVE)
    assert r["state"] == ("warmup_failed" if build_changed else "warming") and r["start"] is None
    assert r["failures"] == (["build_changed"] if build_changed else [])
    sample = mind.runtime(
        "qualification:failed-warmup:sample:" + str(int(moment.timestamp()) // 60)
    )
    assert sample["errors"] == ["TimeoutError"] and "sample_read_failed" in sample["readiness"]
    if build_changed:
        q.arm(mind, "replacement-day", ["EUR/USD"], "another-build", moment)
        assert mind.runtime("qualification:failed-warmup:request")["build"] == "synthetic-build"
    else:
        with pytest.raises(ShadowError, match="qualification_already_active"):
            q.arm(mind, "other-day", ["EUR/USD"], "synthetic-build", moment)


def test_qualification_freshness_uses_read_completion_not_earlier_request_time(mind, monkeypatch):
    from pipmind.shadow import qualification as q

    clock = [utc("2026-10-19T22:00:00Z")]
    start = clock[0]
    monkeypatch.setenv("BUILD_SHA", "synthetic-build")
    monkeypatch.setattr(q, "now", lambda: clock[0])
    mind.set_paused(False)
    q.arm(mind, "read-completion", ["EUR/USD"], "synthetic-build", start)

    def state():
        return {
            **mind.status(),
            "last_error": None,
            "reasoning_worker": {"alive": True, "loop_alive": True, "supervisor_alive": True},
        }

    def read(gateway, path):
        clock[0] += timedelta(seconds=1)
        at = clock[0].isoformat()
        if path == "/evidence":
            return {
                "last_error": None,
                "evidence": {
                    "as_of": at,
                    "from": start.isoformat(),
                    "binding_sha256": "b" * 64,
                    "environment": "demo",
                    "complete": True,
                    "activity_count": 0,
                    "responses": {},
                },
            }
        return {
            "dispatch_enabled": False,
            "broker_verified": True,
            "worker_alive": True,
            "last_cycle": at,
            "prices": [{"instrument": "EUR/USD", "stale": False}],
            "snapshots": [{"as_of": at}, {"as_of": at}],
            "last_error": None,
        }

    monkeypatch.setattr(q, "read_gateway", read)
    q.Recorder(mind, None, state).capture()
    result = mind.runtime(q.ACTIVE)
    assert result["state"] == "running" and result["last_readiness"] == []
    assert utc(result["start"]) == clock[0] > start


@pytest.mark.parametrize("failed", [False, True])
def test_sql_total_overflow_uses_full_consolidation_and_preserves_every_attempt(mind, failed):
    from types import SimpleNamespace

    from pipmind.shadow.common import encoded
    from pipmind.shadow.maintenance import entries, working_sizes
    from pipmind.shadow.model import FoundryModel

    before = {
        "views": [f"USD condition {i}: " + "\\" * 240 for i in range(8)],
        "waiting_for": [f"Verified prerequisite {i}: " + "b" * 150 for i in range(5)],
        "position_theses": [],
    }
    with mind.transaction() as c:
        mind._insert(
            c,
            metadata("before-proof", "memory", "2026-10-01T00:00:00Z", mind.trader),
            encoded(before),
        )
        mind._write_working(c, before, MOMENT)
    activate_bounded(mind)
    events, fixture = episode(mind)
    event = events[0]
    candidate = deepcopy(fixture.responses[event["message_id"]]["reply"])
    candidate.update(
        evidence_ids=[event["message_id"]],
        tool_calls=[],
        memory_dispositions=[],
        working_memory=deepcopy(before),
    )
    candidate["working_memory"]["waiting_for"].append("New prerequisite: " + "c" * 200)
    assert (
        working_sizes(before)["total_bytes"]
        <= 5000
        < working_sizes(candidate["working_memory"])["total_bytes"]
    )
    assert max(working_sizes(candidate["working_memory"])["entry_bytes"].values()) <= 300
    after = {
        "views": ["USD conditions 0,1,2,3,4,5,6,7 remain uncertain"],
        "waiting_for": ["Verify prerequisites 0,1,2,3,4 and new prerequisite; none resolved"],
        "position_theses": [],
    }

    def mapping(source):
        return [
            {
                "from": key,
                "to": key[0] + "0",
                "disposition": "merged",
                "reason": "Every named prerequisite remains unresolved",
                "evidence_ids": [event["message_id"]],
            }
            for key in entries(source)
        ]

    correction = {
        "working_memory": after,
        "memory_dispositions": mapping(before),
        "candidate_dispositions": mapping(candidate["working_memory"]),
    }
    if failed:
        correction["candidate_dispositions"].pop()
    requests = []

    class Agent:
        async def run(self, message, *, options):
            requests.append((loads(message), options))
            return SimpleNamespace(
                text=encoded(correction),
                usage_details={"input_token_count": 12, "output_token_count": 34},
            )

    class Live(FixtureModel):
        async def repair_memory(self, *args):
            return await FoundryModel(
                "https://synthetic.invalid/api/projects/test",
                "t",
                "d",
                enabled=True,
                factory=lambda **kwargs: Agent(),
            ).repair_memory(*args)

    fixture.responses[event["message_id"]].update(
        reply=candidate, triage={"wake": True, "summary": "Synthetic relevant input"}
    )
    original = {
        m["record_id"]: (m, b) for m, b in mind.retained()[1] if m["part"] != "working_memory"
    }
    assert (
        asyncio.run(ShadowEngine(mind, Live(fixture.responses), clock=lambda: MOMENT).step())
        is not failed
    )
    assert len(requests) == 1 and "editable_keys" not in requests[0][0]["correction"]
    assert requests[0][0]["schema"]["$id"] == "urn:pipmind:schema:v1:memory-repair"
    assert requests[0][1]["max_tokens"] == 4096
    if failed:
        assert (
            mind.head()["paused"] and mind.head()["working"] == before and mind.pending() == event
        )
        result = [loads(body) for meta, body in mind.retained()[1] if meta["part"] == "journal"][-1]
        assert result["status"] == "held"
    else:
        assert (
            not mind.head()["paused"] and mind.head()["working"] == after and mind.pending() is None
        )
        result = mind.outcome(event["message_id"])
        assert result["status"] == "observed"
    assert (
        result["memory_repair"]["candidate"] == candidate
        and result["memory_repair"]["response"] == correction
    )
    retained = {
        m["record_id"]: (m, b) for m, b in mind.retained()[1] if m["part"] != "working_memory"
    }
    assert all(retained[k] == v for k, v in original.items())
    assert result["dispatches"] == 0


def test_sql_sequential_consolidations_recover_after_restart_and_use_current_wire_indices(mind):
    from types import SimpleNamespace

    from pipmind.shadow.common import encoded
    from pipmind.shadow.maintenance import entries, working_digest, working_sizes
    from pipmind.shadow.model import FoundryModel

    full_text = (
        "EACH official CPI full text and methodology remain required; no prerequisite resolved."
    )
    prior = (
        "Comparable prior and revision-vintage evidence remain required; no prerequisite resolved."
    )
    before = {
        "views": [f"USD condition {i}: " + "\\" * 250 for i in range(8)],
        "waiting_for": [
            full_text,
            prior,
            *[f"Other prerequisite {i}: " + "b" * 150 for i in range(3)],
        ],
        "position_theses": [],
    }
    after_first = {
        "views": [f"USD condition {i}: " + "\\" * 260 for i in range(8)],
        "waiting_for": [
            prior,
            full_text,
            "Other prerequisites 0,1,2 and first added prerequisite remain unresolved: "
            + "d" * 210,
        ],
        "position_theses": [],
    }
    after_retry = {
        "views": ["USD conditions 0,1,2,3,4,5,6,7 remain uncertain."],
        "waiting_for": [
            full_text,
            prior,
            "Other prerequisites 0,1,2 plus both added prerequisites remain unresolved.",
        ],
        "position_theses": [],
    }
    with mind.transaction() as cur:
        mind._insert(
            cur,
            metadata("sequential-before-proof", "memory", "2026-10-01T00:00:00Z", mind.trader),
            encoded(before),
        )
        mind._write_working(cur, before, MOMENT)
    activate_bounded(mind)
    origin = mind.runtime("memory_policy")["origin_checkpoint"]
    original_head = mind.head()
    originals = {
        m["record_id"]: (m, b) for m, b in mind.retained()[1] if m["part"] != "working_memory"
    }
    events, fixture = episode(mind)
    first = events[0]
    second = deepcopy(first)
    second["message_id"] = second["payload"]["event_id"] = "sequential-second"
    second["available_at"] = (MOMENT + timedelta(seconds=1)).isoformat()
    candidates = {}
    for event, state, text in (
        (first, before, "First added prerequisite: " + "c" * 260),
        (second, after_first, "Second added prerequisite: " + "e" * 270),
    ):
        candidate = deepcopy(fixture.responses[first["message_id"]]["reply"])
        candidate.update(
            action="hold",
            evidence_ids=[event["message_id"]],
            tool_calls=[],
            memory_dispositions=[],
            working_memory={**deepcopy(state), "waiting_for": [*state["waiting_for"], text]},
        )
        candidates[event["message_id"]] = candidate
    for state in (before, after_first, after_retry):
        assert working_sizes(state)["total_bytes"] <= 5000
        assert max(working_sizes(state)["entry_bytes"].values()) <= 300
        assert len(state["views"]) <= 8 and len(state["waiting_for"]) <= 8
    for candidate in candidates.values():
        state = candidate["working_memory"]
        assert working_sizes(state)["total_bytes"] > 5000
        assert max(working_sizes(state)["entry_bytes"].values()) <= 300
        assert len(state["views"]) <= 8 and len(state["waiting_for"]) <= 8

    def mappings(source, target, identifier):
        rows = []
        for key, text in entries(source).items():
            field = "views" if key[0] == "v" else "waiting_for"
            if text in target[field]:
                destination, disposition = key[0] + str(target[field].index(text)), "retained"
            elif key[0] == "v" and len(source["views"]) == len(target["views"]):
                destination, disposition = key, "updated"
            else:
                destination, disposition = ("v0" if key[0] == "v" else "w2"), "merged"
            rows.append(
                {
                    "from": key,
                    "to": destination,
                    "disposition": disposition,
                    "reason": "Keep every named uncertainty and unresolved prerequisite",
                    "evidence_ids": [identifier],
                }
            )
        return rows

    corrections = {}
    for event, source, target in ((first, before, after_first), (second, after_first, after_retry)):
        identifier = event["message_id"]
        corrections[identifier] = {
            "working_memory": target,
            "memory_dispositions": mappings(source, target, identifier),
            "candidate_dispositions": mappings(
                candidates[identifier]["working_memory"], target, identifier
            ),
        }
    invalid = deepcopy(corrections[second["message_id"]])
    invalid["candidate_dispositions"].pop()
    calls, repair_usage = [], {}

    class Repair(FixtureModel):
        async def triage(self, event, *args):
            calls.append(("triage", event["message_id"]))
            return {"wake": True, "summary": "Synthetic growth input"}, {
                "phase": "triage",
                "synthetic": True,
            }

        async def decide(self, event, *args):
            calls.append(("decision", event["message_id"]))
            return deepcopy(candidates[event["message_id"]]), {
                "phase": "decision",
                "synthetic": True,
            }

        async def repair_memory(self, event, context, candidate, *args):
            identifier = event["message_id"]
            calls.append(("memory_repair", identifier))
            assert context["working_memory"] == (
                before if identifier == first["message_id"] else after_first
            )
            assert candidate == candidates[identifier]
            usage = {
                "phase": "memory_repair",
                "synthetic": True,
                "input_tokens": 12,
                "output_tokens": 34,
            }
            repair_usage.setdefault(identifier, []).append(deepcopy(usage))
            value = (
                invalid
                if identifier == second["message_id"] and len(repair_usage[identifier]) == 1
                else corrections[identifier]
            )
            return deepcopy(value), usage

    model = Repair({})
    assert asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    assert mind.head()["working"] == after_first and len(repair_usage[first["message_id"]]) == 1
    first_outcome = mind.outcome(first["message_id"])
    assert first_outcome["memory_repair"]["candidate"] == candidates[first["message_id"]]
    assert first_outcome["memory_repair"]["response"] == corrections[first["message_id"]]
    assert first_outcome["memory_repair"]["before_sha256"] == working_digest(before)
    first_policy = mind.runtime("memory_policy")
    moment = MOMENT + timedelta(seconds=1)
    mind.accept("sequential", [{"sequence": 1, "event": second}], moment)
    assert not asyncio.run(ShadowEngine(mind, model, clock=lambda: moment).step())
    held_id = mind.runtime("reasoning_hold")["attempt_id"]
    held_records = {
        m["record_id"]: (m, b) for m, b in mind.retained()[1] if m["part"] != "working_memory"
    }
    held_body = held_records[held_id][1]
    held = loads(held_body)
    assert held["memory_repair"]["candidate"] == candidates[second["message_id"]]
    assert held["memory_repair"]["response"] == invalid
    assert held["memory_repair"]["before_sha256"] == working_digest(after_first)
    assert held["usage"][-1] == repair_usage[second["message_id"]][0]
    reopened = SqlMind(mind.connect, mind.trader)
    assert reopened.head()["paused"] and reopened.head()["working"] == after_first
    assert reopened.runtime("memory_policy") == first_policy and reopened.pending() == second
    assert reopened.outcome(second["message_id"]) is None
    paused_calls = deepcopy(calls)
    assert not asyncio.run(ShadowEngine(reopened, model, clock=lambda: moment).step())
    assert calls == paused_calls and len(repair_usage[second["message_id"]]) == 1
    assert (held_records[held_id][0], held_body) in reopened.retained()[1]
    reopened.set_paused(False)  # Explicit fixture-owner retry, not automatic hosted recovery.
    assert asyncio.run(ShadowEngine(reopened, model, clock=lambda: moment).step())
    assert reopened.head()["working"] == after_retry and reopened.pending() is None
    assert (
        reopened.runtime("reasoning_hold") is None and len(repair_usage[second["message_id"]]) == 2
    )
    assert reopened.status()["outcomes"] == 2
    retry = reopened.outcome(second["message_id"])
    assert retry["memory_repair"]["response"] == corrections[second["message_id"]]
    assert retry["memory_repair"]["before_sha256"] == working_digest(after_first)
    assert [row["phase"] for row in retry["usage"]] == ["triage", "decision", "memory_repair"]

    fresh = deepcopy(second)
    fresh["message_id"] = fresh["payload"]["event_id"] = "sequential-fresh"
    fresh["available_at"] = (MOMENT + timedelta(seconds=2)).isoformat()
    moment += timedelta(seconds=1)
    reopened.accept("sequential", [{"sequence": 2, "event": fresh}], moment)
    wire = {
        "action": "hold",
        "reasoning": "Carry unresolved prerequisites from the persisted after-state.",
        "evidence_ids": [fresh["message_id"]],
        "tool_calls": [],
        "memory_dispositions": [],
        "working_memory": {"views": [0], "waiting_for": [1, 0, 2], "position_theses": []},
    }
    wire_calls = []

    def factory(phase, **kwargs):
        class Agent:
            async def run(self, message, **options):
                wire_calls.append(phase)
                value = (
                    {"wake": True, "summary": "Synthetic post-restart carry"}
                    if phase == "triage"
                    else wire
                )
                return SimpleNamespace(
                    text=encoded(value),
                    usage_details={"input_token_count": 10, "output_token_count": 5},
                )

        return Agent()

    adapter = FoundryModel(
        "https://synthetic.invalid/api/projects/test", "t", "d", enabled=True, factory=factory
    )
    assert asyncio.run(ShadowEngine(reopened, adapter, clock=lambda: moment).step())
    result = reopened.outcome(fresh["message_id"])
    assert wire_calls == ["triage", "decision"] and "memory_repair" not in result
    assert result["context"]["working_memory"] == after_retry
    assert result["reply"]["working_memory"]["waiting_for"] == [
        prior,
        full_text,
        after_retry["waiting_for"][2],
    ]
    assert result["usage"][-1]["wire_response"] == wire
    assert result["reply"]["working_memory"]["views"] == after_retry["views"]
    assert result["usage"][-1]["working_memory_references"]["views"] == [{"after": 0, "before": 0}]
    assert result["usage"][-1]["working_memory_references"]["waiting_for"][0] == {
        "after": 0,
        "before": 1,
    }
    final = SqlMind(mind.connect, mind.trader)
    assert (
        not final.head()["paused"] and final.pending() is None and final.status()["outcomes"] == 3
    )
    assert final.checkpoint("sequential") == 2 and final.status()["dispatches"] == 0
    assert final.head()["binding"] == original_head["binding"]
    retained = {
        m["record_id"]: (m, b) for m, b in final.retained()[1] if m["part"] != "working_memory"
    }
    assert all(retained[key] == value for key, value in originals.items())
    assert all(retained[key] == value for key, value in held_records.items())
    assert retained[held_id][1] == held_body
    policy = final.runtime("memory_policy")
    assert policy["origin_checkpoint"] == origin
    checkpoint, chain = policy["checkpoint"], []
    while checkpoint is not None:
        assert checkpoint not in chain
        chain.append(checkpoint)
        checkpoint_meta, body = retained[checkpoint]
        transition = loads(body)
        assert working_sizes(transition["after"])["total_bytes"] <= 5000
        previous = transition["previous_checkpoint"]
        assert previous is None or previous in checkpoint_meta["evidence_ids"]
        checkpoint = previous
    assert len(chain) == 4 and chain[-1] == origin
    assert [len(repair_usage[event["message_id"]]) for event in (first, second)] == [1, 2]
    found = final.search(origin + " | w0 EACH official CPI full text methodology", moment)
    assert found and found[0]["metadata"]["record_id"] == origin and full_text in found[0]["body"]


def test_sql_approved_budget_accepts_retained_shape_without_repair_or_rewriting(mind):
    from pipmind.shadow.common import encoded
    from pipmind.shadow.maintenance import working_sizes

    before = {
        "views": [f"FX {i}: " + "a" * 260 for i in range(8)],
        "waiting_for": [f"Await prerequisite {i}: " + "b" * 150 for i in range(5)],
        "position_theses": [],
    }
    with mind.transaction() as c:
        mind._insert(
            c,
            metadata("budget-before-proof", "memory", "2026-10-01T00:00:00Z", mind.trader),
            encoded(before),
        )
        mind._write_working(c, before, MOMENT)
    activate_bounded(mind)
    old = {m["record_id"]: (m, b) for m, b in mind.retained()[1] if m["part"] != "working_memory"}
    events, fixture = episode(mind)
    event = events[0]
    candidate = deepcopy(fixture.responses[event["message_id"]]["reply"])
    candidate.update(
        evidence_ids=[event["message_id"]],
        tool_calls=[],
        memory_dispositions=[],
        working_memory=deepcopy(before),
    )
    candidate["working_memory"]["waiting_for"].append("New prerequisite: " + "c" * 200)
    assert 3200 < working_sizes(candidate["working_memory"])["total_bytes"] < 5000
    fixture.responses[event["message_id"]].update(
        reply=candidate, triage={"wake": True, "summary": "Relevant synthetic condition"}
    )

    class Live(FixtureModel):
        async def repair_memory(self, *args):
            raise AssertionError("Valid approved state must not require another model call")

    assert asyncio.run(ShadowEngine(mind, Live(fixture.responses), clock=lambda: MOMENT).step())
    reopened = SqlMind(mind.connect, mind.trader)
    assert (
        reopened.head()["working"] == candidate["working_memory"]
        and not reopened.head()["paused"]
        and reopened.pending() is None
    )
    result = reopened.outcome(event["message_id"])
    assert result["reply"] == candidate and "memory_repair" not in result
    retained = {
        m["record_id"]: (m, b) for m, b in reopened.retained()[1] if m["part"] != "working_memory"
    }
    assert all(retained[k] == v for k, v in old.items())
    checkpoint = reopened.runtime("memory_policy")["checkpoint"]
    body = loads(retained[checkpoint][1])
    assert body["budget"] == {
        "budget_version": "working-budget-v2",
        "max_bytes": 5000,
        "max_entries_per_list": 8,
        "max_text_bytes": 300,
    }
    assert (
        reopened.context(MOMENT, "FX", history=False)["memory_policy"]["budget_version"]
        == "working-budget-v2"
    )
    assert [v["phase"] for v in result["usage"]] == ["triage", "decision"] and result[
        "dispatches"
    ] == 0


def checked_resume_request(mind):
    from pipmind.shadow.common import encoded

    events, _ = episode(mind)
    identifier = "owner-pause:" + uuid4().hex
    body = encoded({"mode": "shadow", "status": "owner_control", "operation": "pause_for_release"})
    with mind.transaction() as c:
        mind._insert(c, metadata(identifier, "journal", MOMENT.isoformat(), mind.trader), body)
        c.execute(
            "UPDATE pm_minds SET paused=1,revision=revision+1 WHERE trader=%s", (mind.trader,)
        )
    return {
        "expected_head": mind.head(),
        "expected_pending": events[0],
        "owner_control_id": identifier,
        "owner_control_sha256": digest(body),
        "request_id": "release-resume:" + uuid4().hex,
        "moment": MOMENT,
    }


def checked_resume_state(mind):
    retained = mind.retained()
    with mind.transaction() as c:
        result = {"retained": retained}
        for name, query in {
            "inputs": "SELECT id,facts,envelope,status FROM pm_inputs WHERE trader=%s ORDER BY pk",
            "outcomes": "SELECT event_id,result FROM pm_outcomes WHERE trader=%s ORDER BY pk",
            "cursors": "SELECT source,seq FROM pm_cursors WHERE trader=%s ORDER BY source",
            "runtime": "SELECT k,v FROM pm_runtime WHERE trader=%s ORDER BY k",
        }.items():
            c.execute(query, (mind.trader,))
            result[name] = c.fetchall()
    return result


def test_checked_resume_commits_one_audited_revision_and_is_idempotent(mind):
    kwargs = checked_resume_request(mind)
    before = checked_resume_state(mind)
    result = mind.resume_checked(**kwargs)
    after = checked_resume_state(mind)
    assert result == {
        "operation_id": "owner-resume:" + digest(kwargs["request_id"]),
        "request_sha256": result["request_sha256"],
        "before_revision": kwargs["expected_head"]["revision"],
        "after_revision": kwargs["expected_head"]["revision"] + 1,
        "pending_event": kwargs["expected_pending"]["message_id"],
        "resumed": True,
        "dispatches": 0,
        "idempotent": False,
    }
    assert mind.head() == {
        **kwargs["expected_head"],
        "paused": False,
        "revision": result["after_revision"],
    }
    assert all(after[key] == before[key] for key in ["inputs", "outcomes", "cursors", "runtime"])
    assert after["retained"][1][:-1] == before["retained"][1]
    meta, raw = after["retained"][1][-1]
    audit = loads(raw)
    assert meta["record_id"] == result["operation_id"]
    assert meta["evidence_ids"] == [kwargs["owner_control_id"]]
    assert audit["operation"] == "checked_resume" and audit["status"] == "owner_control"
    assert audit["before"] == kwargs["expected_head"] and audit["after"] == mind.head()
    assert audit["request"]["expected_pending"] == kwargs["expected_pending"]
    assert audit["request"]["owner_control_sha256"] == kwargs["owner_control_sha256"]
    assert audit["request_sha256"] == result["request_sha256"]
    repeat = mind.resume_checked(**{**kwargs, "moment": MOMENT + timedelta(seconds=1)})
    assert repeat == {**result, "idempotent": True}
    assert checked_resume_state(mind) == after


def test_checked_resume_rejects_moment_before_owner_control_without_mutation(mind):
    kwargs = checked_resume_request(mind)
    kwargs["moment"] = MOMENT - timedelta(seconds=1)
    # The pending event is available; only the later owner-control dependency
    # makes this timestamp invalid for an audited resume.
    assert utc(kwargs["expected_pending"]["available_at"]) <= kwargs["moment"]
    before = checked_resume_state(mind)
    with pytest.raises(ShadowError, match="checked_resume_owner_control_future"):
        mind.resume_checked(**kwargs)
    assert checked_resume_state(mind) == before


@pytest.mark.parametrize("field,value", [("revision", -1), ("working", {}), ("lineage", "other")])
def test_checked_resume_rejects_stale_or_changed_head_without_mutation(mind, field, value):
    kwargs = checked_resume_request(mind)
    # A stale request still has an individually valid revision.
    kwargs["expected_head"][field] = (
        kwargs["expected_head"][field] + 1 if field == "revision" else value
    )
    before = checked_resume_state(mind)
    with pytest.raises(ShadowError, match="checked_resume_state_changed"):
        mind.resume_checked(**kwargs)
    assert checked_resume_state(mind) == before


@pytest.mark.parametrize(
    "fault", ["hold", "pending", "hash", "missing_control", "not_owner_control"]
)
def test_checked_resume_rejects_changed_guards_without_mutation(mind, fault):
    from pipmind.shadow.common import encoded

    kwargs = checked_resume_request(mind)
    if fault == "hold":
        with mind.transaction() as c:
            mind._set_runtime(c, "reasoning_hold", {"reason": "new_worker_hold"})
        expected = "checked_resume_reasoning_hold"
    elif fault == "pending":
        changed = deepcopy(kwargs["expected_pending"])
        changed["message_id"] += ":different-pending"
        if "event_id" in changed["payload"]:
            changed["payload"]["event_id"] = changed["message_id"]
        kwargs["expected_pending"] = changed
        expected = "checked_resume_pending_changed"
    elif fault == "hash":
        kwargs["owner_control_sha256"] = "0" * 64
        expected = "checked_resume_owner_control"
    elif fault == "missing_control":
        kwargs["owner_control_id"] = "missing-owner-control"
        expected = "checked_resume_owner_control"
    else:
        body = encoded({"mode": "shadow", "status": "observed", "operation": "pause_for_release"})
        identifier = "not-owner-control:" + uuid4().hex
        with mind.transaction() as c:
            mind._insert(c, metadata(identifier, "journal", MOMENT.isoformat(), mind.trader), body)
        kwargs.update(owner_control_id=identifier, owner_control_sha256=digest(body))
        expected = "checked_resume_owner_control"
    before = checked_resume_state(mind)
    with pytest.raises(ShadowError, match=expected):
        mind.resume_checked(**kwargs)
    assert checked_resume_state(mind) == before


def test_checked_resume_refuses_changed_request_id_reuse_and_new_stale_request(mind):
    kwargs = checked_resume_request(mind)
    mind.resume_checked(**kwargs)
    before = checked_resume_state(mind)
    changed = deepcopy(kwargs)
    changed["expected_head"]["working"]["views"].append("Changed retry request")
    with pytest.raises(ShadowError, match="checked_resume_request_conflict"):
        mind.resume_checked(**changed)
    with pytest.raises(ShadowError, match="checked_resume_state_changed"):
        mind.resume_checked(**{**kwargs, "request_id": "another-owner-request"})
    assert checked_resume_state(mind) == before


@pytest.mark.parametrize("change", ["advanced", "held"])
def test_checked_resume_idempotent_retry_cannot_resume_after_progress_or_new_hold(mind, change):
    kwargs = checked_resume_request(mind)
    mind.resume_checked(**kwargs)
    if change == "advanced":
        engine = ShadowEngine(
            mind, FixtureModel(deepcopy(CASE["model_responses"])), clock=lambda: MOMENT
        )
        assert asyncio.run(engine.step())
        expected = "checked_resume_state_changed"
    else:
        mind.set_paused(True)
        with mind.transaction() as c:
            mind._set_runtime(c, "reasoning_hold", {"reason": "new_hold_after_resume"})
        expected = "checked_resume_reasoning_hold"
    before = checked_resume_state(mind)
    with pytest.raises(ShadowError, match=expected):
        mind.resume_checked(**kwargs)
    assert checked_resume_state(mind) == before


@pytest.mark.parametrize("failure", ["audit", "update"])
def test_checked_resume_rolls_back_audit_and_revision_on_failure(mind, monkeypatch, failure):
    kwargs = checked_resume_request(mind)
    before = checked_resume_state(mind)
    injected = []
    if failure == "audit":
        original = mind._insert

        def insert(*args):
            original(*args)
            injected.append("after_actual_audit_insert")
            raise ShadowError("synthetic_resume_failure")

        monkeypatch.setattr(mind, "_insert", insert)
    else:
        connect = mind.connect

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __getattr__(self, key):
                return getattr(self.cursor, key)

            def execute(self, sql, *args):
                self.cursor.execute(sql, *args)
                if sql.startswith("UPDATE pm_minds SET paused=0"):
                    injected.append("after_actual_resume_update")
                    raise ShadowError("synthetic_resume_failure")
                return self

        class Connection:
            def __init__(self):
                self.db = connect()

            def __getattr__(self, key):
                return getattr(self.db, key)

            def cursor(self):
                return Cursor(self.db.cursor())

        monkeypatch.setattr(mind, "connect", Connection)
    with pytest.raises(ShadowError, match="synthetic_resume_failure"):
        mind.resume_checked(**kwargs)
    monkeypatch.undo()
    assert len(injected) == 1 and checked_resume_state(mind) == before


def test_checked_resume_serializes_concurrent_owner_requests(mind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    kwargs = checked_resume_request(mind)
    barrier = Barrier(2)

    def resume(identifier):
        barrier.wait(timeout=10)
        try:
            return mind.resume_checked(**{**kwargs, "request_id": identifier})
        except ShadowError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resume, ["concurrent-owner-one", "concurrent-owner-two"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "checked_resume_state_changed" in results
    assert mind.head()["revision"] == kwargs["expected_head"]["revision"] + 1
    audits = [
        loads(body)
        for meta, body in mind.retained()[1]
        if meta["record_id"].startswith("owner-resume:")
    ]
    assert len(audits) == 1 and not mind.head()["paused"]


def test_checked_resume_observes_hold_created_by_a_writer_holding_source_lock(mind):
    from threading import Event, Thread

    kwargs = checked_resume_request(mind)
    entered, finished = Event(), Event()
    results = []

    def resume():
        entered.set()
        try:
            mind.resume_checked(**kwargs)
        except ShadowError as error:
            results.append(str(error))
        finally:
            finished.set()

    with mind.transaction() as c:
        worker = Thread(target=resume)
        worker.start()
        assert entered.wait(5) and not finished.wait(0.05)
        mind._set_runtime(c, "reasoning_hold", {"reason": "concurrent_writer_hold"})
        c.execute("UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (mind.trader,))
    worker.join(timeout=10)
    assert not worker.is_alive() and results == ["checked_resume_reasoning_hold"]
    assert mind.head()["paused"] and mind.pending() == kwargs["expected_pending"]
    assert not any(meta["record_id"].startswith("owner-resume:") for meta, _ in mind.retained()[1])


def stream_size_source_state(store):
    """Small synthetic inventories, including authority/queue state excluded from bundles."""
    result = {}
    with store.transaction() as cursor:
        for table, order in (
            ("pm_minds", "trader"),
            ("pm_records", "pk"),
            ("pm_inputs", "pk"),
            ("pm_outcomes", "pk"),
            ("pm_cursors", "source"),
            ("pm_runtime", "k"),
        ):
            cursor.execute(
                f"SELECT * FROM {table} WHERE trader=%s ORDER BY {order}", (store.trader,)
            )
            result[table] = cursor.fetchall()
        cursor.execute(
            "SELECT t.term,t.record_pk FROM pm_terms t JOIN pm_records r ON t.record_pk=r.pk "
            "WHERE r.trader=%s ORDER BY t.term,t.record_pk",
            (store.trader,),
        )
        result["pm_terms"] = cursor.fetchall()
    return result


def test_stream_size_inspection_matches_unicode_export_and_preserves_complete_source(
    mind, tmp_path
):
    import zipfile

    from pipmind.shadow.common import encoded
    from pipmind.shadow.stream_bundle import export_experience_file, inspect_experience_size

    events, model = episode(mind)
    assert len(events) == 1
    assert asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    mind.set_paused(True)
    pending = deepcopy(events[0])
    pending["message_id"] = pending["payload"]["event_id"] = "size:pending"
    mind.accept("size-pending-source", [{"sequence": 1, "event": pending}], MOMENT)
    mind.set_runtime("feed_status", {"coverage": "unverified", "detail": "日銀 / naïve / 😀"})
    with mind.transaction() as cursor:
        mind._insert(
            cursor,
            metadata("size:東京:😀", "memory", MOMENT.isoformat(), mind.trader, ["mind:identity"]),
            "Exact full text: café ≠ café? 東京とUSD; 😀\n" * 17,
        )
    frozen = MOMENT + timedelta(seconds=1)
    before = stream_size_source_state(mind)
    inspection = inspect_experience_size(mind, frozen)
    assert stream_size_source_state(mind) == before
    assert not list(tmp_path.iterdir())  # Inspection has not produced an archive.
    assert inspection["inspection"] == "size_only" and inspection["complete_scan"] is True
    assert inspection["size_limits_met"] is True and inspection["violations"] == []
    assert inspection["validity_verified"] is inspection["export_created"] is False
    assert inspection["clone_verified"] is False

    path = tmp_path / "unicode-checkpoint.pipmind"
    exported = export_experience_file(mind, path, frozen)
    with zipfile.ZipFile(path) as archive:
        manifest_raw = archive.read("manifest.json")
        manifest = loads(manifest_raw)
        records = [item for item in archive.infolist() if item.filename != "manifest.json"]
        assert inspection["records"] == exported["records"] == len(records)
        assert inspection["record_counts"] == manifest["coverage"]["record_counts"]
        assert inspection["archive_bytes"] == exported["bytes"] == path.stat().st_size
        assert inspection["manifest_bytes"] == len(manifest_raw)
        assert inspection["inventory_bytes"] == len(encoded(manifest["files"]).encode())
        assert inspection["record_content_bytes"] == sum(item.file_size for item in records)
        assert inspection["expanded_bytes"] == sum(item.file_size for item in archive.infolist())
        assert inspection["maximum_record_bytes"] == max(item.file_size for item in records)
        assert inspection["entries"] == len(archive.infolist()) == inspection["records"] + 1
        assert inspection["checkpoint"] == manifest["checkpoint"]
    assert stream_size_source_state(mind) == before
    assert (
        mind.pending() == pending and mind.outcome(events[0]["message_id"])["status"] == "observed"
    )


@pytest.mark.parametrize("failure", ["interruption", "future_clock"])
def test_stream_size_failed_inspection_closes_sql_and_keeps_all_source_rows(
    mind, monkeypatch, failure
):
    from pipmind.shadow.stream_bundle import inspect_experience_size

    with mind.transaction() as cursor:
        mind._insert(
            cursor,
            metadata("size:late", "journal", MOMENT.isoformat(), mind.trader),
            "Retained at the checkpoint, not before it.",
        )
    before = stream_size_source_state(mind)
    connect = mind.connect
    counts = {"opened": 0, "active": 0}

    class Connection:
        def __init__(self, database):
            self.database, self.closed = database, False

        def __getattr__(self, key):
            return getattr(self.database, key)

        def close(self):
            try:
                self.database.close()
            finally:
                if not self.closed:
                    self.closed = True
                    counts["active"] -= 1

    def tracked(**kwargs):
        database = connect(**kwargs)
        counts["opened"] += 1
        counts["active"] += 1
        return Connection(database)

    monkeypatch.setattr(mind, "connect", tracked)
    calls = 0

    def interrupt():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic size scan interruption")

    if failure == "interruption":
        with pytest.raises(ShadowError, match="^shadow_(?:store|snapshot)_transaction_failed$"):
            inspect_experience_size(mind, MOMENT, interrupt=interrupt)
        assert calls == 2
    else:
        with pytest.raises(ShadowError, match="^shadow_(?:store|snapshot)_transaction_failed$"):
            inspect_experience_size(mind, MOMENT - timedelta(seconds=1))
    assert counts["opened"] > 0 and counts["active"] == 0
    assert stream_size_source_state(mind) == before
    assert counts["active"] == 0
    # The released transaction/lock permits a fresh owner read after either failure.
    assert mind.head()["paused"]


@pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_LOCAL") != "true",
    reason="database option setup is restricted to disposable local SQL",
)
def test_stream_size_snapshot_does_not_mix_a_concurrent_committed_writer(mind, monkeypatch):
    import threading

    from pipmind.shadow.stream_bundle import inspect_experience_size

    name = "size_snapshot_" + uuid4().hex
    admin = mind.connect()
    admin.autocommit = True
    control = admin.cursor()
    control.execute(f"CREATE DATABASE [{name}]")
    worker = None
    try:
        control.execute(f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
        migrate(connect)
        source = SqlMind(connect, "size-snapshot-source")
        source.initialize(
            "Exact Unicode identity: 東京 😀",
            {"version": "v1", "text": "Exact playbook"},
            MOMENT.isoformat(),
            binding=digest("size-source"),
        )
        expected = inspect_experience_size(source, MOMENT)
        finished, errors = threading.Event(), []

        def writer():
            try:
                with source.transaction() as cursor:
                    source._insert(
                        cursor,
                        metadata("size:new", "journal", MOMENT.isoformat(), source.trader),
                        "Committed after the reader's checkpoint, with a different record count.",
                    )
                    source._write_working(
                        cursor,
                        {"views": ["New longer state"], "waiting_for": [], "position_theses": []},
                        MOMENT,
                    )
                    cursor.execute(
                        "UPDATE pm_minds SET revision=revision+1 WHERE trader=%s",
                        (source.trader,),
                    )
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                finished.set()

        read_head = source._head
        worker = threading.Thread(target=writer)

        def head_then_commit(cursor):
            head = read_head(cursor)
            worker.start()  # Commit between the head SELECT and retained-record SELECT.
            assert finished.wait(4), "size snapshot reader blocked the source writer"
            return head

        with monkeypatch.context() as patch:
            patch.setattr(source, "_head", head_then_commit)
            inspection = inspect_experience_size(source, MOMENT)
        worker.join(5)
        assert not worker.is_alive() and not errors
        assert inspection["checkpoint_isolation"] == "snapshot"
        assert inspection["checkpoint_lock_seconds"] == 0
        for key in (
            "checkpoint",
            "records",
            "record_counts",
            "record_content_bytes",
            "inventory_bytes",
            "expanded_bytes",
            "manifest_bytes",
            "archive_bytes",
            "maximum_record_bytes",
            "entries",
        ):
            assert inspection[key] == expected[key]
        assert inspection["records"] == 3 and source.head()["revision"] == 1
        assert len(source.retained()[1]) == 4
        assert source.head()["working"]["views"] == ["New longer state"]
        after = inspect_experience_size(source, MOMENT)
        assert after["records"] == 4 and after["checkpoint"]["checkpoint_id"] == "1"
    finally:
        if worker is not None and worker.ident is not None:
            worker.join(10)
        control.execute(f"DROP DATABASE [{name}]")
        admin.close()


@pytest.mark.parametrize(
    "change",
    [{"val": 2.6}, {"previous": 1.2}, {"records_written": 2}],
    ids=["legacy-hash-conflict", "same-hash-previous", "same-hash-multirow"],
)
def test_sql_signed_webhook_collision_is_409_but_storage_failure_is_503(mind, monkeypatch, change):
    from fastapi.testclient import TestClient

    from pipmind.feeds.fixtures import signature
    from pipmind.feeds.service import create_app
    from pipmind.feeds.sql_store import SqlFeedStore

    store = SqlFeedStore(mind.connect)
    data = {
        "event_id": "sql-webhook-" + uuid4().hex,
        "currency": "USD",
        "indicator": "synthetic_cpi",
        "date": "2026-10-18",
        "val": 2.5,
    }
    token, secret = "synthetic-" + "a" * 32, "synthetic-webhook-key"
    with TestClient(create_app(store, token, secret=secret, now=lambda: MOMENT)) as client:
        headers, raw = signature(data, MOMENT, secret)
        first = client.post("/webhooks/fxmacrodata", headers=headers, content=raw)
        assert first.status_code == 200 and first.json() == {"accepted": True, "duplicate": False}
        capture = store.capture("fxmd:" + data["event_id"])
        assert client.post("/webhooks/fxmacrodata", headers=headers, content=raw).json() == {
            "accepted": True,
            "duplicate": True,
        }
        changed_headers, changed_raw = signature({**data, **change}, MOMENT, secret)
        collision = client.post(
            "/webhooks/fxmacrodata", headers=changed_headers, content=changed_raw
        )
        assert collision.status_code == 409
        assert collision.json() == {"error": "conflicting_feed_delivery"}
        assert store.capture("fxmd:" + data["event_id"]) == capture
        recovered = client.post("/webhooks/fxmacrodata", headers=headers, content=raw)
        assert recovered.status_code == 200 and recovered.json() == {
            "accepted": True,
            "duplicate": True,
        }
        assert store.capture("fxmd:" + data["event_id"]) == capture

        connect, closed, inserted = store.connect, [], []

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __getattr__(self, name):
                return getattr(self.cursor, name)

            def execute(self, statement, *args):
                self.cursor.execute(statement, *args)
                if statement.startswith("INSERT pf_inbox("):
                    inserted.append(True)
                    raise OSError("synthetic private driver detail must never reach HTTP")
                return self

        class Connection:
            def __init__(self):
                self.database = connect()

            def __getattr__(self, name):
                return getattr(self.database, name)

            def cursor(self):
                return Cursor(self.database.cursor())

            def close(self):
                self.database.close()
                closed.append(True)

        failure = {**data, "event_id": "sql-webhook-failure-" + uuid4().hex}
        failure_headers, failure_raw = signature(failure, MOMENT, secret)
        with monkeypatch.context() as patch:
            patch.setattr(store, "connect", Connection)
            unavailable = client.post(
                "/webhooks/fxmacrodata",
                headers=failure_headers,
                content=failure_raw,
            )
        assert unavailable.status_code == 503
        assert unavailable.json() == {"error": "feed_store_unavailable"}
        assert inserted == closed == [True]
        assert store.capture("fxmd:" + failure["event_id"]) is None
        assert store.capture("fxmd:" + data["event_id"]) == capture
        retry = client.post("/webhooks/fxmacrodata", headers=failure_headers, content=failure_raw)
        assert retry.status_code == 200 and retry.json() == {"accepted": True, "duplicate": False}
        accepted_after_failure = store.capture("fxmd:" + failure["event_id"])
        reopened = SqlFeedStore(mind.connect)
        with TestClient(
            create_app(reopened, token, secret=secret, now=lambda: MOMENT)
        ) as restarted:
            duplicate = restarted.post(
                "/webhooks/fxmacrodata",
                headers=failure_headers,
                content=failure_raw,
            )
        assert duplicate.status_code == 200 and duplicate.json() == {
            "accepted": True,
            "duplicate": True,
        }
        assert reopened.capture("fxmd:" + failure["event_id"]) == accepted_after_failure
        with reopened.transaction() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM pf_inbox WHERE k=%s", ("fxmd:" + failure["event_id"],)
            )
            assert cursor.fetchone()[0] == 1


def test_sql_macro_semantic_conflict_preserves_original_raw_fingerprint_and_stream_cursor(mind):
    from pipmind.feeds.common import FeedError
    from pipmind.feeds.macro import accept
    from pipmind.feeds.sql_store import SqlFeedStore

    store = SqlFeedStore(mind.connect)
    data = {
        "event_id": "semantic-cursor-" + uuid4().hex,
        "currency": "USD",
        "indicator": "synthetic_cpi",
        "date": "2026-10-18",
        "val": 2.5,
    }
    assert accept(store, data, MOMENT, stream_id=data["event_id"]) is False
    with store.transaction() as cursor:
        cursor.execute(
            "SELECT fingerprint,raw,received,status FROM pf_inbox WHERE k=%s",
            ("fxmd:" + data["event_id"],),
        )
        original = cursor.fetchone()
    later_cursor = "retained-newer-" + uuid4().hex
    store.save("fxmd:last_event_id", later_cursor)
    # `previous` is absent from the legacy hash but changes normalized semantics.
    with pytest.raises(FeedError, match="^conflicting_feed_delivery$"):
        accept(
            store,
            {**data, "previous": 1.2},
            MOMENT + timedelta(seconds=1),
            stream_id=data["event_id"],
        )
    reopened = SqlFeedStore(mind.connect)
    assert reopened.state("fxmd:last_event_id") == later_cursor
    with reopened.transaction() as cursor:
        cursor.execute(
            "SELECT fingerprint,raw,received,status FROM pf_inbox WHERE k=%s",
            ("fxmd:" + data["event_id"],),
        )
        assert cursor.fetchone() == original
    # An unchanged HTTP-style replay remains idempotent without a stream checkpoint write.
    assert accept(reopened, data, MOMENT + timedelta(seconds=2)) is True
    assert reopened.state("fxmd:last_event_id") == later_cursor


def test_sql_quality_save_failure_rolls_back_valid_page_cursor_and_inbox_completion(
    mind, monkeypatch
):
    from pipmind.feeds.common import FeedError, identity
    from pipmind.feeds.fixtures import pulse
    from pipmind.feeds.sql_store import SqlFeedStore

    store = SqlFeedStore(mind.connect)
    key, cursor_key = "quality-capture:" + uuid4().hex, "quality-cursor:" + uuid4().hex
    payload = {**pulse("macro", MOMENT), "event_id": "quality-event:" + uuid4().hex}
    raw = {"synthetic_original_capture": ["exact", "café", None]}
    store.ingest("macro", key, raw, identity(key), MOMENT, cursor=(cursor_key, "accepted"))
    original_capture = store.capture(key)
    original_quality = store.state("canonical:quality")
    quarantined = store.status()["quarantined"]
    saved, staged = store.save, []
    patch_key = "synthetic-quality:" + uuid4().hex
    patch_value = {patch_key: {"point_in_time_safe": False, "monitoring": "unknown"}}

    def fail_after_valid_writes(name, value, *, db=None):
        if name == "canonical:quality":
            assert db is not None
            assert store.state(cursor_key, db=db) == "complete"
            db.execute("SELECT status FROM pf_inbox WHERE k=%s", (key,))
            assert db.fetchone()[0] == "complete"
            db.execute("SELECT COUNT(*) FROM pf_events WHERE k=%s", (payload["event_id"],))
            assert db.fetchone()[0] == 1
            staged.append(True)
            raise OSError("synthetic transient SQL write interruption")
        return saved(name, value, db=db)

    with monkeypatch.context() as patch:
        patch.setattr(store, "save", fail_after_valid_writes)
        with pytest.raises(FeedError, match="^feed_store_unavailable$"):
            store.complete_batch(
                "macro",
                key,
                [payload],
                MOMENT,
                cursor=(cursor_key, "complete"),
                state_patch=("canonical:quality", patch_value),
            )
    assert staged == [True]
    assert store.capture(key) == original_capture
    assert store.state(cursor_key) == "accepted"
    assert store.state("canonical:quality") == original_quality
    assert store.status()["quarantined"] == quarantined
    with store.transaction() as cursor:
        cursor.execute("SELECT COUNT(*) FROM pf_events WHERE k=%s", (payload["event_id"],))
        assert cursor.fetchone()[0] == 0
    store.complete_batch(
        "macro",
        key,
        [payload],
        MOMENT,
        cursor=(cursor_key, "complete"),
        state_patch=("canonical:quality", patch_value),
    )
    expected_quality = {**(original_quality or {}), **patch_value}
    reopened = SqlFeedStore(mind.connect)
    assert reopened.state(cursor_key) == "complete"
    assert reopened.state("canonical:quality") == expected_quality
    assert reopened.capture(key) == {**original_capture, "status": "complete"}
    with reopened.transaction() as cursor:
        cursor.execute("SELECT envelope FROM pf_events WHERE k=%s", (payload["event_id"],))
        rows = cursor.fetchall()
    assert len(rows) == 1 and loads(rows[0][0])["payload"] == payload


def test_sql_local_handoff_recovers_a_committed_input_page_after_lost_acknowledgement(
    mind, monkeypatch
):
    from pipmind.feeds.fixtures import pulse
    from pipmind.feeds.sql_store import SqlFeedStore
    from pipmind.shadow.service import LocalFeedConsumer

    feeds = SqlFeedStore(mind.connect)
    source = feeds.source_id()
    # pf_events is shared by the disposable test database. This new synthetic mind
    # begins at the prior fixture boundary; never delete or replay earlier tests.
    with feeds.transaction() as cursor:
        cursor.execute("SELECT COALESCE(MAX(seq),0) FROM pf_events")
        baseline = cursor.fetchone()[0]
    with mind.transaction() as cursor:
        cursor.execute(
            "INSERT pm_cursors(trader,source,seq) VALUES(%s,%s,%s)", (mind.trader, source, baseline)
        )
    first = {**pulse("news", MOMENT), "event_id": "handoff-first:" + uuid4().hex}
    second = {**first, "event_id": "handoff-second:" + uuid4().hex}
    provider_cursor = "handoff-provider:" + uuid4().hex
    feeds.page("news", [first, second], provider_cursor, "first-page", MOMENT)
    expected = feeds.events(baseline, 100)
    assert [row["event"]["message_id"] for row in expected] == [
        first["event_id"],
        second["event_id"],
    ]
    consumer = LocalFeedConsumer(mind, feeds)
    accept, connect = mind.accept, mind.connect
    committed, closed = [], []

    class LostAcknowledgement:
        def __init__(self):
            self.database = connect()

        def __getattr__(self, name):
            return getattr(self.database, name)

        def commit(self):
            self.database.commit()  # Real durability precedes the lost response.
            committed.append(True)
            raise OSError("synthetic acknowledgement lost after SQL commit")

        def close(self):
            self.database.close()
            closed.append(True)

    def uncertain_accept(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(mind, "connect", LostAcknowledgement)
            return accept(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mind, "accept", uncertain_accept)
        with pytest.raises(ShadowError, match="^shadow_store_transaction_failed$"):
            consumer.poll(MOMENT)
    assert committed == closed == [True]
    reopened = SqlMind(connect, mind.trader)
    assert reopened.checkpoint(source) == expected[-1]["sequence"]
    assert reopened.status()["pending"] == 2 and reopened.status()["outcomes"] == 0
    before = checked_resume_state(reopened)
    restarted = LocalFeedConsumer(reopened, SqlFeedStore(connect))
    assert restarted.poll(MOMENT) == 0
    after = checked_resume_state(reopened)
    assert all(after[key] == before[key] for key in ["inputs", "outcomes", "cursors"])
    assert after["retained"][0] == before["retained"][0]
    current_records = {meta["record_id"]: (meta, body) for meta, body in after["retained"][1]}
    assert all(
        current_records[meta["record_id"]] == (meta, body) for meta, body in before["retained"][1]
    )
    assert reopened.runtime("feed_status")["coverage"] == feeds.state(
        "canonical:quality",
        {"status": "unverified"},
    )
    assert feeds.state(provider_cursor) == "first-page"
    later = {**first, "event_id": "handoff-later:" + uuid4().hex}
    feeds.page("news", [later], provider_cursor, "next-page", MOMENT + timedelta(seconds=1))
    assert restarted.poll(MOMENT + timedelta(seconds=1)) == 1
    assert reopened.status()["pending"] == 3 and reopened.status()["outcomes"] == 0
    with reopened.transaction() as cursor:
        cursor.execute(
            "SELECT id,envelope FROM pm_inputs WHERE trader=%s ORDER BY pk", (mind.trader,)
        )
        accepted = cursor.fetchall()
    assert [identifier for identifier, _ in accepted] == [
        first["event_id"],
        second["event_id"],
        later["event_id"],
    ]
    assert [loads(body) for _, body in accepted[:2]] == [row["event"] for row in expected]
    assert feeds.events(baseline, 2) == expected and feeds.state(provider_cursor) == "next-page"


def test_sql_private_feed_coverage_is_one_readonly_frame_with_utf8_parity(monkeypatch):
    from contextlib import contextmanager
    from uuid import uuid4

    from pipmind.contracts import stamp, utc
    from pipmind.feeds.coverage import project
    from pipmind.feeds.fixtures import configuration
    from pipmind.feeds.sql_store import SqlFeedStore
    from pipmind.feeds.store import COVERAGE_STATE_BYTES, encoded
    from pipmind.shadow.sql import connector

    store = SqlFeedStore(connector())
    moment = utc("2026-10-21T12:00:00Z")
    sid = "coverage-" + uuid4().hex
    config = configuration()
    config["news"] = [{"id": sid, "url": "https://rss.invalid/private", "kind": "breaking"}]
    http_key, last_key = "rss:http:" + sid, "rss:last:" + sid
    private_state = {"etag": "A" * 40000, "failures": 0}
    assert 32768 < len(encoded(private_state).encode()) <= COVERAGE_STATE_BYTES
    store.save(http_key, private_state)
    store.save(last_key, stamp(moment))
    unrelated = "coverage-cursor:" + uuid4().hex
    store.save(unrelated, {"next": "preserved"})
    source_id = store.source_id()
    with store.transaction() as db:
        db.execute("SELECT k,v FROM pf_meta")
        binding = dict(db.fetchall())
        db.execute("SELECT COUNT(*) FROM pf_events")
        event_count = db.fetchone()[0]
        db.execute("SELECT COUNT(*) FROM pf_inbox")
        inbox_count = db.fetchone()[0]

    original, calls, statements = store.transaction, [], []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, query, *args, **kwargs):
            statements.append(query)
            return self.cursor.execute(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    @contextmanager
    def observed():
        calls.append(True)
        with original() as db:
            yield Cursor(db)

    monkeypatch.setattr(store, "transaction", observed)
    report = project(store, config, moment)
    assert len(calls) == 1
    assert statements and all(query.lstrip().startswith("SELECT") for query in statements)
    row = next(row for row in report["sources"] if row["channel"] == "news")
    assert row["state_availability"] == "available"
    assert row["read"]["state"] == "successful_read"
    assert row["validators_retained"]["etag"] is True
    assert "A" * 100 not in encoded(report)
    monkeypatch.setattr(store, "transaction", original)

    reopened = SqlFeedStore(store.connect)
    assert project(reopened, config, moment) == report
    assert reopened.state(http_key) == private_state
    assert reopened.state(last_key) == stamp(moment)
    assert reopened.state(unrelated) == {"next": "preserved"}
    assert reopened.source_id() == source_id
    with reopened.transaction() as db:
        db.execute("SELECT k,v FROM pf_meta")
        assert dict(db.fetchall()) == binding
        db.execute("SELECT COUNT(*) FROM pf_events")
        assert db.fetchone()[0] == event_count
        db.execute("SELECT COUNT(*) FROM pf_inbox")
        assert db.fetchone()[0] == inbox_count


def test_sql_news_page_and_validators_rollback_then_restart_revalidates_without_duplicates(
    monkeypatch,
):
    from pipmind.contracts import stamp
    from pipmind.core import SimulatedClock
    from pipmind.feeds.common import FeedError, ReadResult
    from pipmind.feeds.news import News
    from pipmind.feeds.sql_store import SqlFeedStore

    connect = connector()
    store = SqlFeedStore(connect)
    clock = SimulatedClock(utc("2026-10-20T10:00:00Z"))
    sid = "news-" + uuid4().hex
    source = {"id": sid, "url": "https://rss.invalid/feed", "kind": "breaking"}
    keys = {kind: "rss:" + kind + ":" + sid for kind in ("http", "last", "poll")}
    unrelated = "news-checkpoint:" + uuid4().hex
    store.save(unrelated, {"source_cursor": 123, "pending_page": "retained"})
    source_id = store.source_id()
    with store.transaction() as db:
        db.execute("SELECT COALESCE(MAX(seq),0) FROM pf_events")
        baseline = db.fetchone()[0]
        db.execute("SELECT k,v FROM pf_meta")
        binding = dict(db.fetchall())
    modified = "Tue, 20 Oct 2026 09:00:00 GMT"
    xml = (
        b"<rss><channel><item><guid>one</guid><title>Original</title>"
        b"<link>https://news.invalid/one</link><pubDate>Tue, 20 Oct 2026 09:00:00 GMT</pubDate>"
        b"</item></channel></rss>"
    )

    class HTTP:
        def __init__(self, *replies):
            self.replies, self.calls = list(replies), []

        def get(self, target, *, headers=None):
            self.calls.append((target, dict(headers or {})))
            return self.replies.pop(0)

    first = HTTP(ReadResult(200, xml, {"ETag": '"original"', "Last-Modified": modified}))
    News(store, first, [source], clock.now).step()
    original = store.events(after=baseline)
    assert len(original) == 1
    last_success = store.state(keys["last"])
    clock.advance(clock.now() + timedelta(seconds=300))
    real_save, injected = store.save, False

    def fail_after_last_success_write(key, value, *, db=None):
        nonlocal injected
        real_save(key, value, db=db)
        if key == keys["last"] and not injected:
            injected = True
            # Events and new validators have already been written in this SQL
            # transaction; the thrown domain error must roll all of them back.
            raise FeedError("feed_store_unavailable")

    monkeypatch.setattr(store, "save", fail_after_last_success_write)
    changed = xml.replace(b"Original", b"Revised")
    failed = HTTP(ReadResult(200, changed, {"ETag": '"revised"'}))
    News(store, failed, [source], clock.now).step()
    assert injected
    assert store.events(after=baseline) == original
    assert store.state(keys["last"]) == last_success
    assert store.state(keys["http"]) == {
        "etag": '"original"',
        "last_modified": modified,
        "failures": 1,
    }
    monkeypatch.setattr(store, "save", real_save)

    # Reopen the real SQL adapter: only the successful representation's validators
    # can be used. Revalidation is a successful read with no new event effect.
    reopened = SqlFeedStore(connect)
    assert reopened.source_id() == source_id
    assert reopened.state(unrelated) == {"source_cursor": 123, "pending_page": "retained"}
    clock.advance(clock.now() + timedelta(seconds=300))
    unchanged = HTTP(ReadResult(304, b"", {}))
    News(reopened, unchanged, [source], clock.now).step()
    assert unchanged.calls == [
        (source["url"], {"If-None-Match": '"original"', "If-Modified-Since": modified})
    ]
    assert reopened.events(after=baseline) == original
    assert reopened.state(keys["last"]) == stamp(clock.now())
    assert reopened.state(keys["http"]) == {
        "etag": '"original"',
        "last_modified": modified,
        "failures": 0,
    }

    clock.advance(clock.now() + timedelta(seconds=300))
    recovered = HTTP(ReadResult(200, changed, {"ETag": '"revised"'}))
    News(reopened, recovered, [source], clock.now).step()
    final = reopened.events(after=baseline)
    assert len(final) == 2 and final[0] == original[0]
    assert final[1]["event"]["payload"]["headline"] == "Revised"
    assert reopened.state(keys["http"]) == {"etag": '"revised"', "failures": 0}
    assert reopened.state(unrelated) == {"source_cursor": 123, "pending_page": "retained"}
    with reopened.transaction() as db:
        db.execute("SELECT k,v FROM pf_meta")
        assert dict(db.fetchall()) == binding


def _safety_lease_store_for_ci(mind):
    from uuid import uuid4

    from pipmind.safety.lease_sql import LeaseBinding, SqlLeaseStore, bootstrap

    bootstrap(mind.connect)
    binding = LeaseBinding("synthetic-" + uuid4().hex, "demo", "execution")
    store = SqlLeaseStore(mind.connect, binding, "synthetic-trusted-epoch")
    store.initialize()
    return store


def _safety_lease_effect_for_ci(mind):
    from uuid import uuid4

    key = "lease-proof-" + uuid4().hex
    with mind.transaction() as cur:
        cur.execute(
            "INSERT INTO pm_runtime(trader,k,v) VALUES(%s,%s,%s)",
            (mind.trader, key, '{"value":"before"}'),
        )
    return key


def test_safety_lease_sql_serializes_competing_owners_and_preserves_restart(mind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import pytest

    from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore, bootstrap

    store = _safety_lease_store_for_ci(mind)
    barrier = Barrier(2)

    def acquire(owner):
        barrier.wait(timeout=10)
        try:
            return store.acquire(owner, ttl_seconds=30)
        except LeaseRefused as exc:
            assert str(exc) == "lease_held"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(acquire, "owner-A")
        second = pool.submit(acquire, "owner-B")
        grants = [first.result(timeout=20), second.result(timeout=20)]
    assert sum(grant is not None for grant in grants) == 1
    grant = next(grant for grant in grants if grant is not None)
    assert grant.generation == 1
    assert grant.expires_at.utcoffset().total_seconds() == 0

    fresh = SqlLeaseStore(mind.connect, store.binding, store.authority_epoch)
    bootstrap(mind.connect)
    fresh.initialize()
    with pytest.raises(LeaseRefused, match="^lease_held$"):
        fresh.acquire(grant.owner_id, ttl_seconds=30)

    def locked_observation(cur):
        cur.execute(
            """SELECT SYSUTCDATETIME(),expires_at,SQL_VARIANT_PROPERTY(expires_at,'Scale'),
            APPLOCK_MODE('public',%s,'Transaction') FROM dbo.pm_safety_leases_v1
            WHERE binding_key=%s""",
            ("pipmind-safety-lease-v1:" + store._key, store._key),
        )
        return cur.fetchone()

    server_now, stored_expiry, scale, lock_mode = fresh.compare_and_act(grant, locked_observation)
    assert server_now.tzinfo is None
    assert grant.expires_at.replace(tzinfo=None) == stored_expiry > server_now
    assert scale == 6
    assert lock_mode == "Exclusive"
    fresh.release(grant)
    next_grant = store.acquire("owner-C", ttl_seconds=30)
    assert next_grant.generation == 2 and next_grant.token != grant.token
    with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
        fresh.compare_and_act(grant, lambda cur: pytest.fail("stale callback executed"))


def test_safety_lease_sql_callback_error_and_expiry_rollback_exact_effects(mind):
    import pytest

    from pipmind.safety.lease_sql import LeaseRefused, LeaseUnavailable, SqlLeaseStore

    store = _safety_lease_store_for_ci(mind)
    key = _safety_lease_effect_for_ci(mind)
    grant = store.acquire("owner-A", ttl_seconds=30)
    callbacks = []

    def failed(cur):
        callbacks.append("failure")
        cur.execute(
            "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k=%s",
            ('{"value":"must rollback"}', mind.trader, key),
        )
        raise RuntimeError("synthetic callback failure, never surfaced")

    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        store.compare_and_act(grant, failed)
    assert callbacks == ["failure"]
    assert mind.runtime(key) == {"value": "before"}

    # Drop the actual SQL connection after a staged write, before the final
    # ownership read. An unavailable store must return no callback result and
    # disconnect must abort the uncommitted effect. No server-wide outage or
    # shared service state is changed by this disposable-session fault.
    connections = []

    def disconnectable():
        db = mind.connect()
        connections.append(db)
        return db

    interrupted = SqlLeaseStore(disconnectable, store.binding, store.authority_epoch)

    def disconnected(cur):
        callbacks.append("connection_loss")
        cur.execute(
            "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k=%s",
            ('{"value":"lost connection must rollback"}', mind.trader, key),
        )
        connections[-1].close()
        return "must not escape unavailable SQL"

    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        interrupted.compare_and_act(grant, disconnected)
    assert len(connections) == 1 and callbacks == ["failure", "connection_loss"]
    assert mind.runtime(key) == {"value": "before"}
    store.release(grant)
    short = store.acquire("owner-B", ttl_seconds=2)

    def expired(cur):
        callbacks.append("expiry")
        cur.execute(
            "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k=%s",
            ('{"value":"also must rollback"}', mind.trader, key),
        )
        cur.execute("WAITFOR DELAY '00:00:02.100'")
        return "must not escape expired transaction"

    with pytest.raises(LeaseRefused, match="^lease_expired$"):
        store.compare_and_act(short, expired)
    assert callbacks == ["failure", "connection_loss", "expiry"]
    assert mind.runtime(key) == {"value": "before"}
    with pytest.raises(LeaseRefused, match="^lease_expired$"):
        store.renew(short, ttl_seconds=300)
    takeover = store.acquire("owner-C", ttl_seconds=30)
    assert takeover.generation == short.generation + 1
    with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
        store.release(short)
    assert mind.runtime(key) == {"value": "before"}


def test_safety_lease_sql_exact_bindings_epoch_and_token_refusals(mind):
    from dataclasses import replace

    import pytest

    from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore

    store = _safety_lease_store_for_ci(mind)
    grant = store.acquire("owner-A", ttl_seconds=30)
    wrong_epoch = SqlLeaseStore(mind.connect, store.binding, "different-epoch")
    with pytest.raises(LeaseRefused, match="^lease_authority_mismatch$"):
        wrong_epoch.initialize()
    for changed in (
        replace(grant, generation=grant.generation + 1),
        replace(grant, owner_id="different-owner"),
        replace(grant, token="0" * 64),
    ):
        with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
            store.compare_and_act(changed, lambda cur: pytest.fail("wrong holder executed"))
    for binding in (
        replace(store.binding, account_id=store.binding.account_id.upper()),
        replace(store.binding, environment="live"),
        replace(store.binding, resource="other-execution-resource"),
    ):
        other = SqlLeaseStore(mind.connect, binding, store.authority_epoch)
        other.initialize()
        with pytest.raises(LeaseRefused, match="^lease_binding_mismatch$"):
            other.release(grant)
        independent = other.acquire("different-owner", ttl_seconds=30)
        assert independent.generation == 1
    assert store.renew(grant, ttl_seconds=30).generation == grant.generation


def test_safety_lease_sql_lost_ack_retains_committed_effect_without_replay(mind):
    import pytest

    from pipmind.safety.lease_sql import LeaseCommitUnknown, SqlLeaseStore

    store = _safety_lease_store_for_ci(mind)
    key = _safety_lease_effect_for_ci(mind)
    grant = store.acquire("owner-A", ttl_seconds=30)
    commits = []
    closes = []
    callbacks = []

    class LostAck:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return self.db.cursor()

        def commit(self):
            self.db.commit()
            commits.append(True)
            raise OSError("synthetic lost acknowledgement")

        def close(self):
            closes.append(True)
            self.db.close()

    uncertain = SqlLeaseStore(LostAck, store.binding, store.authority_epoch)

    def callback(cur):
        callbacks.append(True)
        cur.execute(
            "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k=%s",
            ('{"value":"committed"}', mind.trader, key),
        )
        return "must not return as success"

    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        uncertain.compare_and_act(grant, callback)
    assert callbacks == commits == closes == [True]
    assert mind.runtime(key) == {"value": "committed"}
    # A separately checked known grant can still be renewed; callback was NOT replayed.
    assert store.renew(grant, ttl_seconds=30).generation == grant.generation
    assert callbacks == [True]


def _safety_session_case_ci(mind, *, initialize=True):
    from decimal import Decimal
    from uuid import uuid4

    from pipmind.contracts import utc
    from pipmind.safety.session import EquityObservation, SessionBinding, SessionSpec
    from pipmind.safety.session_sql import SqlSessionStore, bootstrap

    spec = SessionSpec(
        SessionBinding("synthetic-" + uuid4().hex, "demo", "USD"),
        "session-A",
        "synthetic-policy",
        "a" * 64,
        "2026-10-21T00:00:00Z",
        "2026-10-22T00:00:00Z",
        Decimal("1000"),
        "opening-proof",
        Decimal("100"),
        60,
    )
    observed = EquityObservation(
        spec.binding,
        spec.session_id,
        "observation-A",
        "2026-10-21T12:00:00Z",
        "2026-10-21T12:00:00Z",
        Decimal("950"),
        Decimal("20"),
        True,
        True,
        spec.started_at,
        "2026-10-21T12:00:00Z",
        "account-proof",
        "cash-flow-proof",
    )
    bootstrap(mind.connect)
    store = SqlSessionStore(mind.connect, spec.binding, spec.session_id)
    if initialize:
        store.initialize(spec)
    return store, spec, observed, utc("2026-10-21T12:00:00Z")


def _safety_session_observation_count_ci(mind, store):
    with mind.transaction() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM dbo.pm_safety_session_observations_v1 "
            "WHERE account_key=%s AND session_id=%s",
            (store._key, store.session_id),
        )
        return cur.fetchone()[0]


def test_safety_session_sql_concurrent_overlap_and_currency_pin(mind):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier

    import pytest

    from pipmind.safety.session import SessionRefused
    from pipmind.safety.session_sql import SqlSessionStore

    store, spec, _, _ = _safety_session_case_ci(mind, initialize=False)
    barrier = Barrier(2)

    def initialize(sid):
        candidate = SqlSessionStore(mind.connect, spec.binding, sid)
        barrier.wait(timeout=10)
        try:
            return candidate.initialize(replace(spec, session_id=sid))
        except SessionRefused as exc:
            assert str(exc) == "overlapping_safety_session"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = [pool.submit(initialize, sid) for sid in ("session-A", "session-B")]
        results = [a.result(timeout=20), b.result(timeout=20)]
    assert sum(value is not None for value in results) == 1
    winner = next(value for value in results if value is not None)
    actual = SqlSessionStore(mind.connect, spec.binding, winner.spec.session_id)
    assert actual.read() == winner
    assert actual.initialize(winner.spec) == winner
    currency = replace(spec.binding, currency="EUR")
    changed = SqlSessionStore(mind.connect, currency, "different-currency")
    with pytest.raises(SessionRefused, match="^session_binding_mismatch$"):
        changed.initialize(replace(spec, binding=currency, session_id=changed.session_id))
    assert actual.read() == winner
    with mind.transaction() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM dbo.pm_safety_session_sessions_v1 WHERE account_key=%s",
            (store._key,),
        )
        assert cur.fetchone()[0] == 1


def test_safety_session_sql_duplicate_collision_historical_latch_and_reopen(mind):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from decimal import Decimal
    from threading import Barrier

    import pytest

    from pipmind.safety.session import SessionRefused
    from pipmind.safety.session_sql import SqlSessionStore

    store, spec, event, now = _safety_session_case_ci(mind)
    barrier = Barrier(2)

    def append():
        barrier.wait(timeout=10)
        return store.observe(event, now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(append), pool.submit(append)
        first, retry = a.result(timeout=20), b.result(timeout=20)
    assert first == retry and first.revision == 1 and first.daily_loss == Decimal("70")
    assert _safety_session_observation_count_ci(mind, store) == 1
    original = store.read()
    with pytest.raises(SessionRefused, match="^conflicting_session_observation$"):
        store.observe(replace(event, equity=Decimal("949")), now=now)
    with pytest.raises(SessionRefused, match="^conflicting_session_time$"):
        store.observe(
            replace(event, observation_id="instant-alias", as_of="2026-10-21T12:00:00+00:00"),
            now=now,
        )
    assert store.read() == original
    historical = replace(
        event,
        observation_id="historical-breach",
        as_of="2026-10-21T11:00:00Z",
        cash_flow_ends_at="2026-10-21T11:00:00Z",
        equity=Decimal("800"),
    )
    breach = store.observe(historical, now=now)
    assert breach.daily_loss == Decimal("220") and not breach.became_current
    assert breach.daily_loss_latched
    current = store.read()
    assert current.latest == event and current.daily_loss == Decimal("70")
    assert current.daily_loss_latched and current.revision == 2
    with pytest.raises(SessionRefused, match="^conflicting_session_time$"):
        store.observe(replace(historical, observation_id="old-time-collision"), now=now)
    reopened = SqlSessionStore(mind.connect, spec.binding, spec.session_id)
    assert reopened.read() == current and reopened.initialize(spec) == current
    assert reopened.observe(event, now=now) == first  # Original pre-latch receipt stays exact.
    assert _safety_session_observation_count_ci(mind, store) == 2
    adjacent = replace(
        spec, session_id="next-session", started_at=spec.ends_at, ends_at="2026-10-23T00:00:00Z"
    )
    next_store = SqlSessionStore(mind.connect, adjacent.binding, adjacent.session_id)
    assert not next_store.initialize(adjacent).daily_loss_latched
    assert reopened.read() == current  # New session cannot erase preceding latch/history.


def test_safety_session_sql_staged_write_error_and_interrupt_rollback(mind):
    import pytest

    from pipmind.safety.session import SessionUnavailable
    from pipmind.safety.session_sql import SqlSessionStore

    for failure in (RuntimeError, KeyboardInterrupt):
        store, spec, event, now = _safety_session_case_ci(mind)
        before = store.read()
        staged = []
        closes = []

        class FailingCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def execute(self, query, *args, _failure=failure, _staged=staged):
                if query.startswith("UPDATE dbo.pm_safety_session_sessions_v1 SET state="):
                    _staged.append(True)  # Observation INSERT already completed on real SQL.
                    raise _failure("synthetic interruption after staged observation")
                return self.cursor.execute(query, *args)

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class FailingConnection:
            def __init__(self):
                self.db = mind.connect()

            @property
            def autocommit(self):
                return self.db.autocommit

            def cursor(self):
                return FailingCursor(self.db.cursor())

            def commit(self):
                pytest.fail("failed transaction attempted commit")

            def close(self, _closes=closes):
                _closes.append(True)
                self.db.close()

        interrupted = SqlSessionStore(FailingConnection, spec.binding, spec.session_id)
        expected = SessionUnavailable if failure is RuntimeError else KeyboardInterrupt
        with pytest.raises(expected):
            interrupted.observe(event, now=now)
        assert staged == closes == [True]
        assert store.read() == before
        assert _safety_session_observation_count_ci(mind, store) == 0
        receipt = store.observe(event, now=now)
        assert store.observe(event, now=now) == receipt
        assert _safety_session_observation_count_ci(mind, store) == 1


def test_safety_session_sql_lost_ack_preserves_effect_and_stable_retry(mind):
    import pytest

    from pipmind.safety.session import SessionCommitUnknown
    from pipmind.safety.session_sql import SqlSessionStore

    store, spec, event, now = _safety_session_case_ci(mind)
    commits, closes = [], []

    class LostAck:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return self.db.cursor()

        def commit(self):
            self.db.commit()
            commits.append(True)
            raise SystemExit("synthetic interruption after SQL committed")

        def close(self):
            closes.append(True)
            self.db.close()

    uncertain = SqlSessionStore(LostAck, spec.binding, spec.session_id)
    with pytest.raises(SessionCommitUnknown, match="^session_commit_unknown$"):
        uncertain.observe(event, now=now)
    assert commits == closes == [True]
    reopened = SqlSessionStore(mind.connect, spec.binding, spec.session_id)
    assert reopened.read().revision == 1
    assert _safety_session_observation_count_ci(mind, store) == 1
    receipt = reopened.observe(event, now=now)
    assert receipt.revision == 1 and reopened.read().revision == 1
    assert _safety_session_observation_count_ci(mind, store) == 1


def test_safety_session_sql_same_cursor_lease_composition_and_bounded_corruption(mind):
    import pytest

    from pipmind.contracts import utc
    from pipmind.safety.lease_sql import LeaseBinding, SqlLeaseStore
    from pipmind.safety.lease_sql import bootstrap as lease_bootstrap
    from pipmind.safety.session import SessionUnavailable, fingerprint
    from pipmind.safety.session_sql import SqlSessionStore

    store, spec, event, now = _safety_session_case_ci(mind)
    original_receipt = store.observe(event, now=now)
    before = store.read()
    lease_bootstrap(mind.connect)
    leases = SqlLeaseStore(
        mind.connect,
        LeaseBinding(spec.binding.account_id, "demo", "session-read"),
        "synthetic-trusted-epoch",
    )
    leases.initialize()
    grant = leases.acquire("synthetic-owner", ttl_seconds=30)

    def forbidden_connection():
        pytest.fail("same-cursor session read opened an independent connection")

    same_cursor = SqlSessionStore(forbidden_connection, spec.binding, spec.session_id)
    assert leases.compare_and_act(grant, same_cursor.read_in_transaction) == before
    assert store.read() == before
    with mind.transaction() as cur:
        cur.execute(
            "UPDATE dbo.pm_safety_session_observations_v1 SET as_of=DATEADD(second,1,as_of) "
            "WHERE account_key=%s AND session_id=%s AND observation_id=%s",
            (store._key, store.session_id, event.observation_id),
        )
    with pytest.raises(SessionUnavailable, match="^session_store_unavailable$"):
        store.observe(event, now=now)
    assert store.read() == before
    with mind.transaction() as cur:
        cur.execute(
            "UPDATE dbo.pm_safety_session_observations_v1 SET as_of=%s "
            "WHERE account_key=%s AND session_id=%s AND observation_id=%s",
            (
                utc(event.as_of).replace(tzinfo=None),
                store._key,
                store.session_id,
                event.observation_id,
            ),
        )
    assert store.observe(event, now=now) == original_receipt
    malformed = '"' + "synthetic-private-canary" * 4000 + '"'
    with mind.transaction() as cur:
        cur.execute(
            "UPDATE dbo.pm_safety_session_sessions_v1 SET state=%s,state_sha256=%s "
            "WHERE account_key=%s AND session_id=%s",
            (malformed, fingerprint(malformed), store._key, store.session_id),
        )
    with pytest.raises(SessionUnavailable, match="^session_store_unavailable$"):
        store.read()
    assert _safety_session_observation_count_ci(mind, store) == 1


def _safety_admission_request_ci(
    *,
    now,
    request_id="request-A",
    account="synthetic-account",
    session_id="session-A",
    caps=None,
    margin=None,
    marks=(),
    count=0,
):
    from copy import deepcopy
    from dataclasses import replace
    from datetime import timedelta
    from decimal import Decimal as D

    from pipmind.contracts import stamp
    from pipmind.reconciliation import Binding, Freshness, SnapshotFacts
    from pipmind.safety import Conversion, ProposalFacts, RiskFacts, RiskPolicy
    from pipmind.safety.admission import (
        AdmissionRequest,
        ReconciliationEvidence,
        ReservationMark,
        checkpoint_digest,
        policy_book_digest,
    )
    from pipmind.safety.session import (
        EquityObservation,
        SessionBinding,
        SessionSpec,
        advance,
        initial_state,
    )

    if margin is None:
        margin = D("40")
    when, start, end = (
        stamp(now),
        stamp(now - timedelta(hours=12)),
        stamp(now + timedelta(hours=12)),
    )
    p = RiskPolicy(
        "synthetic-policy",
        account,
        "demo",
        "USD",
        "EUR/USD",
        D("2000"),
        D("5000"),
        D("10000"),
        D("50"),
        D("100"),
        D("100"),
        D("200"),
        D("0"),
        10,
        60,
        30,
        30,
    )
    book = (replace(p, **(caps or {})),)
    account_body = {
        "schema_version": 1,
        "as_of": when,
        "currency": "USD",
        "balance": D("10000"),
        "equity": D("10000"),
        "margin_used": D("1000"),
        "margin_available": D("9000"),
    }
    observed = SnapshotFacts(
        Binding(account, "demo"),
        account_body,
        {"schema_version": 1, "as_of": when, "positions": []},
        (),
        True,
        True,
        (),
        when,
        True,
        (),
        when,
        True,
    )
    evidence = ReconciliationEvidence(
        deepcopy(observed),
        observed,
        Freshness(timedelta(seconds=30), timedelta(seconds=2)),
        (),
        (),
        (),
        (),
        (),
    )
    conversion = Conversion("USD", "USD", D("1"), when)
    quote = ProposalFacts("EUR/USD", D("1.1"), D("1.1002"), when, D("1"), margin, conversion)
    facts = RiskFacts(
        account,
        "demo",
        when,
        True,
        True,
        False,
        False,
        start,
        end,
        D("10000"),
        D("0"),
        count,
        quote,
        (),
    )
    spec = SessionSpec(
        SessionBinding(account, "demo", "USD"),
        session_id,
        book[0].policy_id,
        policy_book_digest(book),
        start,
        end,
        D("10000"),
        "opening-evidence",
        book[0].max_daily_loss,
        60,
    )
    observation = EquityObservation(
        spec.binding,
        session_id,
        "source-observation",
        when,
        when,
        D("10000"),
        D("0"),
        True,
        True,
        start,
        when,
        checkpoint_digest(evidence, facts),
        "flow-evidence",
    )
    state, _ = advance(initial_state(spec), observation, now=now)
    order = {
        "schema_version": 1,
        "client_order_id": request_id,
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": D("1000"),
        "order_type": "market",
        "stop": D("1.09"),
    }
    pending = tuple(
        ReservationMark(key, D("1.1"), D("1.1002"), when, conversion, margin) for key in marks
    )
    request = AdmissionRequest(order, facts, evidence, pending, state.revision, state.latest_sha256)
    return request, state, book


def _admission_sql_case_ci(mind, *, caps=None, margin=None):
    from datetime import UTC
    from decimal import Decimal
    from uuid import uuid4

    from pipmind.safety.admission_sql import SqlAdmissionStore, bootstrap
    from pipmind.safety.lease_sql import LeaseBinding, SqlLeaseStore
    from pipmind.safety.lease_sql import bootstrap as lease_bootstrap
    from pipmind.safety.session_sql import SqlSessionStore
    from pipmind.safety.session_sql import bootstrap as session_bootstrap

    # Create explicit isolated schema before sampling the synthetic source clock.
    session_bootstrap(mind.connect)
    lease_bootstrap(mind.connect)
    bootstrap(mind.connect)
    with mind.transaction() as cur:
        cur.execute("SELECT SYSUTCDATETIME()")
        now = cur.fetchone()[0].replace(tzinfo=UTC)
    factory = _safety_admission_request_ci
    request, state, book = factory(
        now=now,
        account="synthetic-" + uuid4().hex,
        caps=caps,
        margin=Decimal("40") if margin is None else margin,
        marks=("request-A", "request-B"),
    )
    sessions = SqlSessionStore(mind.connect, state.spec.binding, state.spec.session_id)
    sessions.initialize(state.spec)
    sessions.observe(state.latest, now=now)
    assert sessions.read() == state
    leases = SqlLeaseStore(
        mind.connect,
        LeaseBinding(state.spec.binding.account_id, "demo", "admission"),
        "synthetic-trusted-epoch",
    )
    leases.initialize()
    grant = leases.acquire("synthetic-owner", ttl_seconds=30)
    store = SqlAdmissionStore(mind.connect, leases, sessions, book)
    return store, leases, sessions, grant, request, book, now


def _admission_sql_counts_ci(mind, store):
    with mind.transaction() as cur:
        result = []
        for name in ("accounts", "journal", "reservations", "floors"):
            cur.execute(
                "SELECT COUNT(*) FROM dbo.pm_safety_admission_" + name + "_v1 WHERE account_key=%s",
                (store._key,),
            )
            result.append(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM dbo.pm_safety_admission_books_v1 WHERE sha256=%s",
            (store._book_hash,),
        )
        return tuple(result) + (cur.fetchone()[0],)


def test_admission_sql_genuine_concurrent_budgets_and_funded_both(mind):
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal as D
    from threading import Barrier

    from pipmind.safety.admission_sql import SqlAdmissionStore

    cases = (
        ({}, D("40"), 2),
        ({"max_gross_exposure": D("1500")}, D("40"), 1),
        ({"max_total_planned_loss": D("15")}, D("40"), 1),
        ({"max_session_orders": 1}, D("40"), 1),
        ({}, D("5000"), 1),
    )
    for caps, margin, expected in cases:
        store, leases, sessions, grant, first, book, now = _admission_sql_case_ci(
            mind,
            caps=caps,
            margin=margin,
        )
        second, _, _ = _safety_admission_request_ci(
            now=now,
            account=book[0].account_id,
            request_id="request-B",
            caps=caps,
            margin=margin,
            marks=("request-A", "request-B"),
        )
        barrier = Barrier(2)

        def admit(request, _store=store, _grant=grant, _barrier=barrier):
            _barrier.wait(timeout=10)
            return _store.admit(_grant, request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(admit, first), pool.submit(admit, second)
            receipts = (a.result(timeout=20), b.result(timeout=20))
        assert sum(item["assessment"].within_limits for item in receipts) == expected
        assert all(item["dispatch_authorized"] is False for item in receipts)
        assert len(store.snapshot()[0]) == expected
        assert _admission_sql_counts_ci(mind, store) == (1, 2, expected, 1, 1)
        reopened = SqlAdmissionStore(mind.connect, leases, sessions, book)
        assert tuple(reopened.lookup(item["request_id"]) for item in receipts) == receipts
        assert reopened.snapshot() == store.snapshot()
        assert reopened.admit(grant, first) in receipts


def test_admission_sql_staged_failure_interrupt_and_lease_or_source_expiry(mind):
    import pytest

    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.lease_sql import LeaseRefused, LeaseUnavailable, SqlLeaseStore

    for mode in ("error", "interrupt", "disconnect", "lease_expiry", "source_expiry"):
        caps = {"quote_max_age_seconds": 3} if mode == "source_expiry" else None
        store, leases, sessions, grant, request, book, _ = _admission_sql_case_ci(mind, caps=caps)
        if mode == "lease_expiry":
            grant = leases.renew(grant, ttl_seconds=2)
        before = _admission_sql_counts_ci(mind, store)
        staged, closes, disconnects = [], [], []
        delay_markers, delay_errors = [], []

        class Cursor:
            def __init__(self, cur, db):
                self.cur, self.db = cur, db

            def execute(
                self,
                query,
                *args,
                _mode=mode,
                _staged=staged,
                _disconnects=disconnects,
                _delay_markers=delay_markers,
                _delay_errors=delay_errors,
            ):
                result = self.cur.execute(query, *args)
                if query.startswith("INSERT INTO dbo.pm_safety_admission_reservations_v1"):
                    _staged.append(True)
                    if _mode == "error":
                        raise OSError("synthetic private failure after SQL insert")
                    if _mode == "interrupt":
                        raise KeyboardInterrupt("synthetic interruption after SQL insert")
                    if _mode == "disconnect":
                        # Real SQL socket closes with the staged transaction uncommitted.
                        self.db.close()
                        _disconnects.append(True)
                        raise OSError("synthetic disconnect after SQL insert")
                    # Pinned pytds infers string parameters as NVARCHAR(MAX).
                    # Use fixed SQL literals, matching the proven lease expiry test.
                    delay_sql = (
                        "WAITFOR DELAY '00:00:03.100'"
                        if _mode == "source_expiry"
                        else "WAITFOR DELAY '00:00:02.100'"
                    )
                    _delay_markers.append("entered")
                    try:
                        self.cur.execute(delay_sql)
                    except Exception as error:
                        number = getattr(error, "number", None)
                        _delay_errors.append(
                            (type(error).__name__, number if type(number) is int else None)
                        )
                        raise
                    _delay_markers.append("completed")
                return result

            def __getattr__(self, name):
                return getattr(self.cur, name)

        class Connection:
            def __init__(self):
                self.db = mind.connect()

            @property
            def autocommit(self):
                return self.db.autocommit

            def cursor(self):
                return Cursor(self.db.cursor(), self.db)

            def commit(self):
                pytest.fail("staged failed/expired admission committed")

            def close(self, _closes=closes):
                _closes.append(True)
                self.db.close()

        guarded_lease = SqlLeaseStore(Connection, leases.binding, leases.authority_epoch)
        guarded = SqlAdmissionStore(mind.connect, guarded_lease, sessions, book)
        expected = {"interrupt": KeyboardInterrupt, "lease_expiry": LeaseRefused}.get(
            mode,
            LeaseUnavailable,
        )
        try:
            with pytest.raises(
                expected, match="^lease_expired$" if mode == "lease_expiry" else None
            ):
                guarded.admit(grant, request)
        except BaseException:
            # Synthetic mode/stage/type/number only; never print driver text or tokens.
            print("admission_expiry_diagnostic", mode, staged, delay_markers, delay_errors)
            raise
        assert staged == closes == [True], mode
        if mode in ("lease_expiry", "source_expiry"):
            assert delay_markers == ["entered", "completed"] and not delay_errors, mode
        assert disconnects == ([True] if mode == "disconnect" else [])
        assert _admission_sql_counts_ci(mind, store) == before
        assert store.lookup(request.order["client_order_id"]) is None
        assert store.snapshot() == ((), 0)


def test_admission_sql_session_latch_writer_serializes_before_admission(mind):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from datetime import timedelta
    from decimal import Decimal
    from threading import Event

    from pipmind.contracts import stamp
    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.lease_sql import SqlLeaseStore
    from pipmind.safety.session_sql import SqlSessionStore

    store, leases, sessions, grant, request, book, now = _admission_sql_case_ci(mind)
    staged, release, admission_waiting = Event(), Event(), Event()

    class Cursor:
        def __init__(self, cur, writer):
            self.cur, self.writer = cur, writer

        def execute(self, query, *args):
            if (
                not self.writer
                and "sp_getapplock" in query
                and args
                and args[0][0].startswith("pipmind-safety-session-v1:")
            ):
                admission_waiting.set()
            result = self.cur.execute(query, *args)
            if self.writer and query.startswith(
                "UPDATE dbo.pm_safety_session_sessions_v1 SET state="
            ):
                staged.set()
                if not release.wait(timeout=8):
                    raise RuntimeError("synthetic writer release timeout")
            return result

        def __getattr__(self, name):
            return getattr(self.cur, name)

    class Connection:
        def __init__(self, writer):
            self.db, self.writer = mind.connect(), writer

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return Cursor(self.db.cursor(), self.writer)

        def commit(self):
            return self.db.commit()

        def close(self):
            self.db.close()

    writer = SqlSessionStore(lambda: Connection(True), sessions.binding, sessions.session_id)
    guarded_lease = SqlLeaseStore(lambda: Connection(False), leases.binding, leases.authority_epoch)
    guarded = SqlAdmissionStore(mind.connect, guarded_lease, sessions, book)
    latest = sessions.read().latest
    older = stamp(now - timedelta(seconds=1))
    breach = replace(
        latest,
        observation_id="historical-breach",
        as_of=older,
        cash_flow_ends_at=older,
        equity=Decimal("9000"),
        account_evidence_id="historical-breach-source",
    )
    after_writer = replace(request, expected_session_revision=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(writer.observe, breach, now=now)
        try:
            assert staged.wait(timeout=5)
            attempt = pool.submit(guarded.admit, grant, after_writer)
            assert admission_waiting.wait(timeout=5)
            assert not write.done() and not attempt.done()
        finally:
            release.set()
        assert write.result(timeout=10).daily_loss_latched
        receipt = attempt.result(timeout=10)
    assert not receipt["assessment"].within_limits
    assert receipt["assessment"].effective_session_latched is True
    assert store.snapshot()[0] == ()
    assert sessions.read().revision == 2 and sessions.read().daily_loss_latched
    assert store.lookup(request.order["client_order_id"]) == receipt


def test_admission_sql_lost_ack_exact_lookup_retry_conflict_and_uncertainty(mind):
    from dataclasses import replace

    import pytest

    from pipmind.safety.admission import AdmissionRefused
    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.lease_sql import LeaseCommitUnknown, SqlLeaseStore

    store, leases, sessions, grant, request, book, _ = _admission_sql_case_ci(mind)
    commits, closes = [], []

    class LostAck:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return self.db.cursor()

        def commit(self):
            self.db.commit()
            commits.append(True)
            raise SystemExit("synthetic lost commit acknowledgement")

        def close(self):
            closes.append(True)
            self.db.close()

    uncertain_lease = SqlLeaseStore(LostAck, leases.binding, leases.authority_epoch)
    uncertain = SqlAdmissionStore(mind.connect, uncertain_lease, sessions, book)
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        uncertain.admit(grant, request)
    assert commits == closes == [True]
    assert _admission_sql_counts_ci(mind, store) == (1, 1, 1, 1, 1)
    original = store.lookup(request.order["client_order_id"])
    assert original["assessment"].within_limits
    assert store.admit(grant, request) == original
    changed = replace(request, expected_session_revision=0)
    with pytest.raises(AdmissionRefused, match="^conflicting_admission_request$"):
        store.admit(grant, changed)
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        uncertain.admit(grant, changed)
    assert _admission_sql_counts_ci(mind, store) == (1, 1, 1, 1, 1)
    assert store.lookup(request.order["client_order_id"]) == original
    assert len(store.snapshot()[0]) == 1


def test_admission_sql_new_owner_cannot_erase_pending_budget_or_rewrite_receipt(mind):
    from decimal import Decimal

    import pytest

    from pipmind.safety.lease_sql import LeaseRefused

    caps = {"max_gross_exposure": Decimal("1500")}
    store, leases, _, old_grant, first, book, now = _admission_sql_case_ci(mind, caps=caps)
    original = store.admit(old_grant, first)
    assert original["assessment"].within_limits
    second, _, _ = _safety_admission_request_ci(
        now=now,
        account=book[0].account_id,
        request_id="request-B",
        caps=caps,
        marks=("request-A", "request-B"),
    )
    before = _admission_sql_counts_ci(mind, store)
    leases.release(old_grant)
    current = leases.acquire("replacement-owner", ttl_seconds=30)
    assert current.generation == old_grant.generation + 1
    with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
        store.admit(old_grant, second)
    assert _admission_sql_counts_ci(mind, store) == before
    assert store.admit(current, first) == original
    assert original["lease_owner_id"] == old_grant.owner_id
    assert original["lease_generation"] == old_grant.generation
    denied = store.admit(current, second)
    assert not denied["assessment"].within_limits
    assert denied["lease_owner_id"] == current.owner_id
    assert _admission_sql_counts_ci(mind, store) == (1, 2, 1, 1, 1)
    assert len(store.snapshot()[0]) == 1


def _safety_gateway_case_ci(mind, *, ready=True, caps=None):
    from decimal import Decimal

    from pipmind.gateway.config import SymbolMap
    from pipmind.safety.controls_sql import SqlControlStore
    from pipmind.safety.controls_sql import bootstrap as controls_bootstrap
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.integration_sql import bootstrap as integration_bootstrap

    controls_bootstrap(mind.connect)
    integration_bootstrap(mind.connect)
    admission, leases, sessions, grant, request, book, now = _admission_sql_case_ci(mind, caps=caps)
    controls = SqlControlStore(mind.connect, sessions.binding)
    controls.initialize()
    controls.apply_override("resume-test", "synthetic-owner", "resume", 0, "Synthetic test only")
    symbols = SymbolMap(
        [
            {
                "instrument": "EUR/USD",
                "market_id": 1,
                "unit": "unit",
                "quantity_factor": "1",
                "increment": "1",
                "minimum": "1",
            }
        ]
    )
    gateway = SqlSafetyGateway(
        admission, controls, symbols, base_units_per_size={"EUR/USD": Decimal("1")}
    )
    if ready:
        receipt = gateway.reconcile(grant, request)
        assert receipt["outcome"] == "dry_ready", receipt
    return gateway, admission, leases, sessions, controls, grant, request, book, now


def _safety_gateway_journal_ci(mind, gateway):
    import json

    with mind.transaction() as cur:
        cur.execute(
            "SELECT channel,record_id,receipt FROM dbo.pm_safety_integration_journal_v1 "
            "WHERE account_key=%s ORDER BY channel,record_id",
            (gateway._key,),
        )
        return [(row[0], row[1], json.loads(row[2])) for row in cur.fetchall()]


def test_safety_gateway_sql_authenticated_flow_retains_exact_receipts_and_never_dispatches(mind):
    import json

    import httpx

    from pipmind.safety.admission import encode_request
    from pipmind.safety.service import create_app

    gateway, admission, _, _, controls, grant, request, _, _ = _safety_gateway_case_ci(mind)
    token = "synthetic-private-token-01234567890123456789"
    app = create_app(gateway, controls, token, grant_provider=lambda: grant, owner_id="owner")

    async def flow():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post("/orders", json={"request": encode_request(request)})
            assert denied.status_code == 401 and admission.snapshot()[0] == ()
            headers = {"authorization": "Bearer " + token}
            first = await client.post(
                "/orders", json={"request": encode_request(request)}, headers=headers
            )
            assert first.status_code == 200, first.text
            receipt = first.json()
            assert receipt["outcome"] == "dry_accepted", receipt
            assert receipt["dispatch_authorized"] is receipt["dispatch_enabled"] is False
            again = await client.post(
                "/orders", json={"request": encode_request(request)}, headers=headers
            )
            assert again.json() == receipt
            check = await client.post(
                "/checks",
                json={"check_id": "check-A", "request": encode_request(request)},
                headers=headers,
            )
            assert check.status_code == 200, check.text
            assert check.json()["outcome"] == "dry_checked", check.text
            assert check.headers["cache-control"] == "no-store"
            override = await client.post(
                "/controls/overrides",
                json={
                    "command_id": "kill-A",
                    "action": "kill",
                    "expected_revision": 1,
                    "reason": "Synthetic halt",
                },
                headers=headers,
            )
            assert override.status_code == 200, override.text
            assert override.json()["review_excluded"] is True
            after = await client.post(
                "/checks",
                json={"check_id": "check-B", "request": encode_request(request)},
                headers=headers,
            )
            assert after.json()["code"] == "kill_switch", after.text
            return receipt

    receipt = asyncio.run(flow())
    assert len(admission.snapshot()[0]) == 1
    assert admission.lookup(request.order["client_order_id"])["assessment"].within_limits
    assert grant.token not in json.dumps(_safety_gateway_journal_ci(mind, gateway))
    assert receipt["origin"] == "model" and receipt["review_excluded"] is False


def test_safety_gateway_sql_startup_control_generation_holds_and_id_collisions(mind):
    from dataclasses import replace

    from pipmind.safety.integration_sql import IntegrationRefused
    from pipmind.safety.lease_sql import LeaseRefused

    gateway, admission, leases, _, controls, old, request, _, _ = _safety_gateway_case_ci(
        mind, ready=False
    )
    refused = gateway.accept(old, request)
    assert refused["code"] == "reconciliation_required"
    assert admission.snapshot()[0] == ()
    # Original IDs retain refusal; a deliberate new proposal is required.
    assert gateway.reconcile(old, request)["outcome"] == "dry_ready"
    assert gateway.accept(old, request) == refused
    new = replace(request, order={**request.order, "client_order_id": "new-A"})
    assert gateway.accept(old, new)["outcome"] == "dry_accepted"
    with pytest.raises(IntegrationRefused, match="^conflicting_integration_request$"):
        gateway.accept(old, replace(new, expected_session_revision=0))
    leases.release(old)
    current = leases.acquire("new-owner", ttl_seconds=30)
    with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
        gateway.check(old, "stale-owner", new)
    assert gateway.check(current, "new-owner-hold", new)["code"] == "reconciliation_required"
    assert gateway.reconcile(current, new)["outcome"] == "dry_ready"
    assert gateway.check(current, "new-owner-ready", new)["outcome"] == "dry_checked"
    assert gateway.accept(current, new)["scope"]["generation"] == old.generation
    assert controls.read().revision == 1 and len(admission.snapshot()[0]) == 1


def test_safety_gateway_sql_stale_source_holds_new_risk_and_exact_history(mind):
    from dataclasses import replace
    from datetime import timedelta

    from pipmind.contracts import stamp

    gateway, admission, _, _, _, grant, request, _, now = _safety_gateway_case_ci(mind)
    original = gateway.accept(grant, request)
    old = stamp(now - timedelta(minutes=2))
    stale = replace(request, facts=replace(request.facts, as_of=old))
    refused = gateway.check(grant, "stale-proof", stale)
    assert refused["outcome"] == "refused"
    assert gateway.check(grant, "after-stale", request)["code"] == "reconciliation_required"
    assert gateway.accept(grant, request) == original
    assert len(admission.snapshot()[0]) == 1
    assert gateway.reconcile(grant, request)["outcome"] == "dry_ready"
    assert gateway.check(grant, "fresh-again", request)["outcome"] == "dry_checked"
    history = _safety_gateway_journal_ci(mind, gateway)
    assert any(row[1] == "stale-proof" and row[2] == refused for row in history)


def test_safety_gateway_sql_lease_expiry_rolls_back_staged_admission_and_gateway_ack(
    mind, monkeypatch
):
    from pipmind.safety.lease_sql import LeaseRefused

    gateway, admission, leases, _, _, grant, request, _, _ = _safety_gateway_case_ci(mind)
    leases.release(grant)
    grant = leases.acquire("short-lived-owner", ttl_seconds=2)
    assert gateway.reconcile(grant, request)["outcome"] == "dry_ready"
    before = _safety_gateway_journal_ci(mind, gateway)
    staged = []
    original = gateway._record

    def delay(cur, *args, **kwargs):
        value = original(cur, *args, **kwargs)
        if args[0] == "accept":
            cur.execute(
                "SELECT COUNT(*) FROM dbo.pm_safety_admission_reservations_v1 WHERE account_key=%s",
                (gateway._key,),
            )
            assert cur.fetchone()[0] == 1
            staged.append(True)
            cur.execute("WAITFOR DELAY '00:00:02.100'")
        return value

    monkeypatch.setattr(gateway, "_record", delay)
    with pytest.raises(LeaseRefused, match="^lease_expired$"):
        gateway.accept(grant, request)
    assert staged == [True]
    assert admission.lookup(request.order["client_order_id"]) is None
    assert admission.snapshot()[0] == ()
    assert _safety_gateway_journal_ci(mind, gateway) == before


def test_safety_gateway_sql_ack_uncertainty_keeps_atomic_durable_receipt_and_reservation(mind):
    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.lease_sql import LeaseCommitUnknown, SqlLeaseStore

    gateway, admission, leases, sessions, controls, grant, request, book, _ = (
        _safety_gateway_case_ci(mind)
    )
    closed = []

    class LostAck:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return self.db.cursor()

        def commit(self):
            self.db.commit()
            raise SystemExit("synthetic lost acknowledgement")

        def close(self):
            closed.append(True)
            self.db.close()

    uncertain_leases = SqlLeaseStore(LostAck, leases.binding, leases.authority_epoch)
    uncertain_admission = SqlAdmissionStore(mind.connect, uncertain_leases, sessions, book)
    uncertain = SqlSafetyGateway(
        uncertain_admission, controls, gateway._symbols, base_units_per_size=gateway._unit_factors
    )
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        uncertain.accept(grant, request)
    assert closed == [True] and len(admission.snapshot()[0]) == 1
    receipt = gateway.accept(grant, request)
    assert receipt["outcome"] == "dry_accepted"
    assert sum(row[0] == "accept" for row in _safety_gateway_journal_ci(mind, gateway)) == 1


def test_safety_gateway_sql_control_writer_serializes_with_inflight_acceptance(mind, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    gateway, admission, _, _, controls, grant, request, _, _ = _safety_gateway_case_ci(mind)
    staged, release, writing = Event(), Event(), Event()
    original = gateway._record

    def wait(cur, *args, **kwargs):
        receipt = original(cur, *args, **kwargs)
        if args[0] == "accept":
            staged.set()
            assert release.wait(timeout=5)
        return receipt

    def kill():
        writing.set()
        return controls.apply_override(
            "racing-kill", "owner", "kill", 1, "Synthetic concurrent halt"
        )

    monkeypatch.setattr(gateway, "_record", wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        accepting = pool.submit(gateway.accept, grant, request)
        try:
            assert staged.wait(timeout=5)
            stopping = pool.submit(kill)
            assert writing.wait(timeout=5)
            assert not accepting.done() and not stopping.done()
        finally:
            release.set()
        assert accepting.result(timeout=10)["outcome"] == "dry_accepted"
        assert stopping.result(timeout=10)["state"]["killed"] is True
    assert gateway.check(grant, "after-racing-kill", request)["code"] == "kill_switch"
    assert len(admission.snapshot()[0]) == 1


def test_safety_gateway_sql_quote_only_staleness_cannot_clear_hold(mind):
    from dataclasses import replace

    from pipmind.contracts import stamp

    gateway, admission, _, _, _, grant, request, _, now = _safety_gateway_case_ci(mind, ready=False)
    # Account/reconciliation/cash-flow are all current. Only the proposal quote is old.
    stale = replace(
        request,
        facts=replace(
            request.facts,
            proposal=replace(request.facts.proposal, as_of=stamp(now - timedelta(seconds=31))),
        ),
    )
    receipt = gateway.reconcile(grant, stale)
    assert receipt["outcome"] == "held" and receipt["code"] == "stale_quote", receipt
    assert gateway.accept(grant, request)["code"] == "reconciliation_required"
    assert admission.snapshot()[0] == ()


def test_safety_gateway_sql_unit_understatement_refused_before_reserving(mind):
    from dataclasses import replace
    from decimal import Decimal

    gateway, admission, _, _, _, grant, request, _, _ = _safety_gateway_case_ci(mind)
    malicious = replace(
        request,
        order={**request.order, "size": Decimal("2500")},
        facts=replace(
            request.facts,
            proposal=replace(request.facts.proposal, base_units_per_size=Decimal("0.01")),
        ),
    )
    receipt = gateway.accept(grant, malicious)
    assert receipt["outcome"] == "refused" and receipt["code"] == "gateway_unit_factor_mismatch"
    assert admission.snapshot()[0] == ()
    assert admission.lookup(request.order["client_order_id"]) is None


def test_safety_gateway_sql_post_admission_proof_failure_aborts_entire_intake(mind, monkeypatch):
    from dataclasses import replace

    from pipmind.safety.lease_sql import LeaseUnavailable

    gateway, admission, _, _, _, grant, request, _, _ = _safety_gateway_case_ci(mind)
    before = _safety_gateway_journal_ci(mind, gateway)
    original = admission.preview_in_transaction
    staged = []

    def expire(cur, proof, *, recheck_request_id=None):
        assert recheck_request_id == request.order["client_order_id"]
        cur.execute(
            "SELECT COUNT(*) FROM dbo.pm_safety_admission_reservations_v1 WHERE account_key=%s",
            (gateway._key,),
        )
        assert cur.fetchone()[0] == 1
        result = original(cur, proof, recheck_request_id=recheck_request_id)
        assert result.within_limits
        staged.append(True)
        # Inject the failure at the post-stage boundary, not the initial admission.
        return replace(result, within_limits=False, code="stale_quote", reservation=None)

    monkeypatch.setattr(admission, "preview_in_transaction", expire)
    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        gateway.accept(grant, request)
    assert staged == [True]
    assert admission.lookup(request.order["client_order_id"]) is None
    assert admission.snapshot()[0] == ()
    assert _safety_gateway_journal_ci(mind, gateway) == before


@pytest.mark.parametrize("failure", ["error", "interrupt", "disconnect"])
def test_safety_gateway_sql_staged_failure_reopens_without_partial_intake(
    mind, monkeypatch, failure
):
    from pipmind.safety.lease_sql import LeaseUnavailable

    gateway, admission, _, _, _, grant, request, _, _ = _safety_gateway_case_ci(mind)
    before_history = _safety_gateway_journal_ci(mind, gateway)
    before_admission = _admission_sql_counts_ci(mind, admission)
    before_controls = gateway.controls.read()
    original = gateway._record
    staged = []

    def fail(cur, *args, **kwargs):
        receipt = original(cur, *args, **kwargs)
        if args[0] == "accept":
            assert receipt["outcome"] == "dry_accepted"
            cur.execute(
                "SELECT COUNT(*) FROM dbo.pm_safety_admission_reservations_v1 WHERE account_key=%s",
                (gateway._key,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT COUNT(*) FROM dbo.pm_safety_integration_journal_v1 "
                "WHERE account_key=%s AND channel='accept'",
                (gateway._key,),
            )
            assert cur.fetchone()[0] == 1
            staged.append(True)
            if failure == "disconnect":
                cur.connection.close()
            if failure == "interrupt":
                raise KeyboardInterrupt("synthetic precommit integration interruption")
            raise OSError("synthetic post-stage integration failure")
        return receipt

    monkeypatch.setattr(gateway, "_record", fail)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else LeaseUnavailable):
        gateway.accept(grant, request)
    assert staged == [True]
    assert _safety_gateway_journal_ci(mind, gateway) == before_history
    assert _admission_sql_counts_ci(mind, admission) == before_admission
    assert gateway.controls.read() == before_controls
    assert admission.lookup(request.order["client_order_id"]) is None
    monkeypatch.setattr(gateway, "_record", original)
    assert gateway.accept(grant, request)["outcome"] == "dry_accepted"
    assert len(admission.snapshot()[0]) == 1


def _safety_controls_case_ci(mind):
    from uuid import uuid4

    from pipmind.safety.controls_sql import SqlControlStore, bootstrap
    from pipmind.safety.session import SessionBinding

    bootstrap(mind.connect)
    store = SqlControlStore(mind.connect, SessionBinding("synthetic-" + uuid4().hex, "demo", "USD"))
    assert store.initialize().paused
    return store


def test_safety_controls_sql_cas_race_and_exact_replay(mind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import pytest

    from pipmind.safety.controls_sql import ControlRefused

    store = _safety_controls_case_ci(mind)
    barrier = Barrier(2)

    def apply(action):
        barrier.wait(timeout=10)
        try:
            return store.apply_override(action, "owner", action, 0, "explicit synthetic command")
        except ControlRefused as error:
            assert str(error) == "stale_control_revision"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(apply, "kill"), pool.submit(apply, "resume")
        receipts = [a.result(timeout=20), b.result(timeout=20)]
    assert sum(item is not None for item in receipts) == 1
    original = next(item for item in receipts if item is not None)
    assert store.read().revision == 1
    body = original["body"]
    assert (
        store.apply_override(
            body["command_id"], body["actor_id"], body["action"], 0, body["reason"]
        )
        == original
    )
    with pytest.raises(ControlRefused, match="^conflicting_control_command$"):
        store.apply_override(body["command_id"], "owner", body["action"], 0, "changed")
    assert store.lookup_override(body["command_id"]) == original
    assert store.initialize() == store.read()


def test_safety_controls_sql_same_cursor_read_serializes_racing_kill(mind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from pipmind.safety.controls_sql import SqlControlStore

    store = _safety_controls_case_ci(mind)
    store.apply_override("resume", "owner", "resume", 0, "explicit resume")
    attempted = Event()

    class Cursor:
        def __init__(self, cur):
            self.cur = cur

        def execute(self, query, *args):
            if "sp_getapplock" in query:
                attempted.set()
            return self.cur.execute(query, *args)

        def __getattr__(self, name):
            return getattr(self.cur, name)

    class Connection:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return Cursor(self.db.cursor())

        def commit(self):
            return self.db.commit()

        def close(self):
            self.db.close()

    writer = SqlControlStore(Connection, store.binding)
    db = mind.connect()
    try:
        cur = db.cursor()
        state = store.read_in_transaction(cur)
        assert not state.paused and not state.killed
        original = store.lookup_override_in_transaction(cur, "resume")
        assert original["review_excluded"] and original["origin"] == "trusted_owner_override"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer.apply_override, "kill", "owner", "kill", 1, "stop")
            try:
                assert attempted.wait(timeout=5)
                assert not future.done()
                assert store.read_in_transaction(cur) == state
            finally:
                db.commit()  # Release control lock only after the consistent reader finishes.
            assert future.result(timeout=10)["state"]["killed"]
        assert store.read().killed and store.read().paused and store.read().revision == 2
    finally:
        db.close()


def test_safety_controls_sql_staged_error_interrupt_disconnect_preserve_state(mind):
    import pytest

    from pipmind.safety.controls_sql import ControlUnavailable, SqlControlStore

    for mode in ("error", "interrupt", "disconnect"):
        store = _safety_controls_case_ci(mind)
        before = store.read()
        staged, closed, disconnected = [], [], []

        class Cursor:
            def __init__(self, cur, db):
                self.cur, self.db = cur, db

            def execute(self, query, *args, _mode=mode, _staged=staged, _disconnected=disconnected):
                result = self.cur.execute(query, *args)
                if query.startswith("UPDATE dbo.pm_safety_controls_v1 SET revision="):
                    _staged.append(True)  # Both receipt INSERT and state UPDATE reached actual SQL.
                    if _mode == "disconnect":
                        self.db.close()
                        _disconnected.append(True)
                    if _mode == "interrupt":
                        raise KeyboardInterrupt("synthetic interruption after control writes")
                    raise OSError("synthetic transport failure after control writes")
                return result

            def __getattr__(self, name):
                return getattr(self.cur, name)

        class Connection:
            def __init__(self):
                self.db = mind.connect()

            @property
            def autocommit(self):
                return self.db.autocommit

            def cursor(self):
                return Cursor(self.db.cursor(), self.db)

            def commit(self):
                pytest.fail("failed control transaction attempted commit")

            def close(self, _closed=closed):
                _closed.append(True)
                self.db.close()

        failing = SqlControlStore(Connection, store.binding)
        with pytest.raises(KeyboardInterrupt if mode == "interrupt" else ControlUnavailable):
            failing.apply_override("kill", "owner", "kill", 0, "stop")
        assert staged == closed == [True]
        assert disconnected == ([True] if mode == "disconnect" else [])
        assert store.read() == before and store.lookup_override("kill") is None
        receipt = store.apply_override("kill", "owner", "kill", 0, "stop")
        assert store.apply_override("kill", "owner", "kill", 0, "stop") == receipt


def test_safety_controls_sql_unknown_commit_restart_flatten_and_suggestion_journal(mind):
    import pytest

    from pipmind.safety.controls_sql import ControlCommitUnknown, ControlRefused, SqlControlStore

    store = _safety_controls_case_ci(mind)
    commits, closes = [], []

    class LostAck:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return self.db.cursor()

        def commit(self):
            self.db.commit()
            commits.append(True)
            raise SystemExit("synthetic lost acknowledgement")

        def close(self):
            closes.append(True)
            self.db.close()

    uncertain = SqlControlStore(LostAck, store.binding)
    with pytest.raises(ControlCommitUnknown, match="^control_commit_unknown$"):
        uncertain.apply_override("kill", "owner", "kill", 0, "stop")
    assert commits == closes == [True]
    reopened = SqlControlStore(mind.connect, store.binding)
    assert reopened.read().killed and reopened.read().paused
    original = reopened.lookup_override("kill")
    assert original == reopened.apply_override("kill", "owner", "kill", 0, "stop")
    with pytest.raises(ControlRefused, match="^conflicting_control_command$"):
        reopened.apply_override("kill", "owner", "kill", 0, "altered")
    flattened = reopened.apply_override("flatten", "owner", "flatten", 1, "explicit flatten")
    assert flattened["outcome"] == "unsupported_not_executed"
    assert flattened["review_excluded"] and reopened.read().killed and reopened.read().paused
    before = reopened.read()
    suggestion = reopened.submit_suggestion("idea", "owner", "Exact original suggestion")
    disposition = reopened.resolve_suggestion("idea", "reviewer", "trial", "Explicit reason")
    assert not suggestion["review_excluded"] and not disposition["review_excluded"]
    assert reopened.submit_suggestion("idea", "owner", "Exact original suggestion") == suggestion
    assert (
        reopened.resolve_suggestion("idea", "reviewer", "trial", "Explicit reason") == disposition
    )
    assert reopened.read() == before


def test_safety_controls_sql_latest_override_receipt_and_state_must_agree(mind):
    import pytest

    from pipmind.safety.controls_sql import ControlUnavailable

    store = _safety_controls_case_ci(mind)
    receipt = store.apply_override("kill", "owner", "kill", 0, "stop")
    state = store.read()
    with mind.transaction() as cur:
        cur.execute(
            "SELECT MAX(revision) FROM dbo.pm_safety_control_overrides_v1 WHERE account_key=%s",
            (store._key,),
        )
        assert cur.fetchone()[0] == state.revision == 1
        cur.execute(
            "UPDATE dbo.pm_safety_controls_v1 SET killed=0,paused=0 WHERE account_key=%s",
            (store._key,),
        )
    with pytest.raises(ControlUnavailable, match="^control_store_unavailable$"):
        store.read()
    with mind.transaction() as cur:
        cur.execute(
            "UPDATE dbo.pm_safety_controls_v1 SET killed=1,paused=1 WHERE account_key=%s",
            (store._key,),
        )
    assert store.lookup_override("kill") == receipt
    with mind.transaction() as cur:
        cur.execute(
            "UPDATE dbo.pm_safety_control_overrides_v1 SET revision=2 "
            "WHERE account_key=%s AND id=%s",
            (store._key, "kill"),
        )
    with pytest.raises(ControlUnavailable, match="^control_store_unavailable$"):
        store.read()


"""PRIVATE append-only real SQL CI fragment; never run against hosted SQL."""


def _authority_case_ci(mind):
    import os
    from contextlib import contextmanager
    from uuid import uuid4

    from pipmind.safety.authority_sql import SqlAuthorityStore, bootstrap
    from pipmind.safety.incarnation import read_incarnation
    from pipmind.safety.lease_sql import LeaseBinding
    from pipmind.shadow.sql import connector

    @contextmanager
    def case():
        if os.environ.get("PIPMIND_SQL_LOCAL") != "true":
            raise AssertionError("Authority CI requires explicitly disposable local SQL")
        name = "authority_ci_" + uuid4().hex
        admin = mind.connect()
        admin.autocommit = True
        cur = admin.cursor()
        cur.execute(f"CREATE DATABASE [{name}]")
        try:
            connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
            source, external = mind.connect(), connect()
            try:
                app, pin = read_incarnation(source.cursor()), read_incarnation(external.cursor())
            finally:
                source.close()
                external.close()
            bootstrap(connect, pin)
            binding = LeaseBinding("authority-test-" + uuid4().hex, "demo", "execution")
            store = SqlAuthorityStore(connect, binding, pin)
            held = store.initialize(app)
            yield store, held, connect, pin, app
        finally:
            cur.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(f"DROP DATABASE [{name}]")
            admin.close()

    return case()


def test_external_authority_sql_exact_fence_reopen_and_stale_guard(mind):
    import pytest

    from pipmind.safety.authority_sql import AuthorityRefused, SqlAuthorityStore

    with _authority_case_ci(mind) as (store, held, connect, pin, app):
        with store.guard(held, mode="held") as guard:
            guard.record_application_barrier(guard.state.application_incarnation, 1)
            active = guard.activate("assignment-A", 1, "a" * 64)
        assert store.read() == active
        receipt = store.fence(
            "fence-A", "owner", active.revision, active.epoch, app, "Explicit fence"
        )
        reopened = SqlAuthorityStore(connect, store.binding, pin)
        fenced = reopened.read()
        assert fenced.mode == "held" and fenced.epoch != active.epoch
        assert fenced.assignment_generation == 1 and fenced.assignment_id is None
        with pytest.raises(AuthorityRefused, match="^authority_stale_state$"):
            with reopened.guard(active):
                pytest.fail("stale owner entered external guard")
        with reopened.guard(fenced, mode="held") as guard:
            guard.record_application_barrier(guard.state.application_incarnation, 1)
            replacement = guard.activate("assignment-B", 2, "b" * 64)
        assert (
            reopened.fence("fence-A", "owner", active.revision, active.epoch, app, "Explicit fence")
            == receipt
        )
        assert reopened.read() == replacement
        with pytest.raises(AuthorityRefused, match="^conflicting_authority_operation$"):
            reopened.fence("fence-A", "owner", active.revision, active.epoch, app, "Changed")
        assert reopened.read() == replacement


def test_external_authority_sql_fence_waits_for_existing_guard_and_denies_old_state(mind):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    import pytest

    from pipmind.safety.authority_sql import AuthorityRefused

    with _authority_case_ci(mind) as (store, held, _, _, app):
        with store.guard(held, mode="held") as guard:
            guard.record_application_barrier(guard.state.application_incarnation, 1)
            active = guard.activate("assignment-A", 1, "a" * 64)
        entered, release, fencing = Event(), Event(), Event()

        def old_operation():
            with store.guard(active) as guard:
                assert guard.state == active
                entered.set()
                assert release.wait(timeout=4)
            return True

        def fence():
            fencing.set()
            return store.fence(
                "concurrent-fence",
                "owner",
                active.revision,
                active.epoch,
                app,
                "Explicit concurrent fence",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(old_operation)
            try:
                assert entered.wait(timeout=3)
                stopping = pool.submit(fence)
                assert fencing.wait(timeout=3)
                with pytest.raises(TimeoutError):
                    stopping.result(timeout=0.2)
            finally:
                release.set()
            assert running.result(timeout=5)
            receipt = stopping.result(timeout=5)
        assert receipt["after"]["mode"] == "held"
        with pytest.raises(AuthorityRefused, match="^authority_stale_state$"):
            with store.guard(active):
                pytest.fail("fenced state reentered")


def test_external_authority_sql_staged_failure_interrupt_and_disconnect_keep_hold(
    mind, monkeypatch
):
    import pytest

    from pipmind.safety.authority_sql import AuthorityUnavailable, SqlAuthorityStore

    with _authority_case_ci(mind) as (store, held, connect, pin, _):
        original = store._write
        for mode in ("error", "interrupt", "disconnect"):
            staged = []

            def fail(cur, changed, _mode=mode, _staged=staged):
                original(cur, changed)
                _staged.append(True)
                if _mode == "interrupt":
                    raise KeyboardInterrupt("synthetic precommit authority interruption")
                if _mode == "disconnect":
                    cur.connection.close()
                raise OSError("synthetic failure after staged authority activation")

            monkeypatch.setattr(store, "_write", fail)
            expected = KeyboardInterrupt if mode == "interrupt" else AuthorityUnavailable
            with pytest.raises(expected):
                with store.guard(held, mode="held") as guard:
                    guard.record_application_barrier(guard.state.application_incarnation, 1)
                    guard.activate("assignment-A", 1, "a" * 64)
            assert staged == [True]
            assert SqlAuthorityStore(connect, store.binding, pin).read() == held
        monkeypatch.setattr(store, "_write", original)
        with store.guard(held, mode="held") as guard:
            guard.record_application_barrier(guard.state.application_incarnation, 1)
            active = guard.activate("assignment-A", 1, "a" * 64)
        assert store.read() == active


def test_external_authority_sql_external_lost_ack_and_inner_application_uncertainty(mind):
    import pytest

    from pipmind.safety.authority_sql import (
        AuthorityCommitUnknown,
        AuthorityRefused,
        SqlAuthorityStore,
    )
    from pipmind.safety.lease_sql import LeaseCommitUnknown

    with _authority_case_ci(mind) as (store, held, connect, pin, _):
        uncertain_app = LeaseCommitUnknown("lease_commit_unknown")
        with pytest.raises(LeaseCommitUnknown) as caught:
            with store.guard(held, mode="held") as guard:
                guard.record_application_barrier(guard.state.application_incarnation, 1)
                guard.activate("not-acknowledged", 1, "a" * 64)
                raise uncertain_app
        assert caught.value is uncertain_app and store.read() == held

        class LostAck:
            def __init__(self):
                self.db = connect()

            @property
            def autocommit(self):
                return self.db.autocommit

            def cursor(self):
                return self.db.cursor()

            def commit(self):
                self.db.commit()
                raise SystemExit("synthetic external commit acknowledgement loss")

            def close(self):
                self.db.close()

        uncertain = SqlAuthorityStore(LostAck, store.binding, pin)
        with pytest.raises(AuthorityCommitUnknown, match="^authority_commit_unknown$"):
            with uncertain.guard(held, mode="held") as guard:
                guard.record_application_barrier(guard.state.application_incarnation, 1)
                guard.activate("persisted-with-lost-ack", 1, "a" * 64)
        reopened = SqlAuthorityStore(connect, store.binding, pin)
        current = reopened.read()
        assert current.mode == "active" and current.assignment_id == "persisted-with-lost-ack"
        with pytest.raises(AuthorityRefused, match="^authority_stale_state$"):
            with reopened.guard(held, mode="held"):
                pytest.fail("old held state survived activation")


"""Root integration into mandatory tests/test_shadow_sql.py; do not execute standalone."""


def _safety_inventory_sql_ci(mind, binding, lease_binding):
    from pipmind.safety.checkpoint_sql import capture_in_transaction, required_locks

    db = mind.connect()
    try:
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        for resource in required_locks(binding, lease_binding):
            cur.execute(
                "DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,"
                "@LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=5000; SELECT @r",
                (resource,),
            )
            while cur.description is None:
                assert cur.nextset()
            assert cur.fetchone()[0] >= 0
        first = capture_in_transaction(cur, binding, lease_binding)
        assert capture_in_transaction(cur, binding, lease_binding) == first
        db.commit()
        return first
    finally:
        db.close()


def test_safety_checkpoint_sql_complete_history_scoped_lobs_and_readonly_restart(mind):
    from dataclasses import replace
    from datetime import timedelta

    from pipmind.contracts import stamp
    from pipmind.safety.checkpoint_sql import COVERED_TABLES, decode_inventory, encode_inventory

    gateway, admission, leases, sessions, controls, grant, request, _, now = (
        _safety_gateway_case_ci(mind)  # noqa: F821 -- root-owned shared SQL fixture
    )
    assert gateway.accept(grant, request)["outcome"] == "dry_accepted"
    controls.submit_suggestion("wide-checkpoint", "owner", "exact" * 8000)
    latest = sessions.read().latest
    for index in range(65):
        older = stamp(now - timedelta(seconds=index + 1))
        sessions.observe(
            replace(
                latest,
                observation_id=f"old-checkpoint-{index}",
                as_of=older,
                cash_flow_ends_at=older,
                account_evidence_id=f"historical-source-{index}",
            ),
            now=now,
        )
    before_controls, before_session = controls.read(), sessions.read()
    before_reservations = admission.snapshot()
    inventory = _safety_inventory_sql_ci(mind, sessions.binding, leases.binding)
    assert tuple(row.name for row in inventory.tables) == COVERED_TABLES
    by_table = {row.name: row for row in inventory.tables}
    assert by_table["dbo.pm_safety_session_observations_v1"].count == 66
    assert by_table["dbo.pm_safety_admission_reservations_v1"].count == 1
    assert by_table["dbo.pm_safety_control_suggestions_v1"].count == 1
    assert decode_inventory(encode_inventory(inventory)) == inventory
    assert controls.read() == before_controls and sessions.read() == before_session
    assert admission.snapshot() == before_reservations
    # A second fully initialized account and its referenced policy books do not
    # change the selected account's inventory. Each capture uses a fresh connection.
    _safety_gateway_case_ci(mind)  # noqa: F821 -- root-owned shared SQL fixture
    assert _safety_inventory_sql_ci(mind, sessions.binding, leases.binding) == inventory


def test_safety_checkpoint_sql_missing_lock_refuses_without_operational_mutation(mind):
    import pytest

    from pipmind.safety.checkpoint_sql import CheckpointUnavailable, capture_in_transaction

    _, admission, leases, sessions, controls, _, _, _, _ = _safety_gateway_case_ci(mind)  # noqa: F821
    before = (controls.read(), sessions.read(), admission.snapshot())
    db = mind.connect()
    try:
        cur = db.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
        with pytest.raises(CheckpointUnavailable, match="^safety_inventory_unavailable$"):
            capture_in_transaction(cur, sessions.binding, leases.binding)
    finally:
        db.close()
    assert (controls.read(), sessions.read(), admission.snapshot()) == before


"""Self-contained functions for root's mandatory SQL suite; never run locally."""


def _safety_recovery_case_ci(mind):
    from uuid import uuid4

    from pipmind.safety.recovery_sql import RobotReference, SqlRecoveryStore, bootstrap
    from pipmind.safety.session import SessionBinding

    bootstrap(mind.connect)
    store = SqlRecoveryStore(mind.connect, SessionBinding("recovery-" + uuid4().hex, "demo", "USD"))
    assert store.initialize().generation == 0

    def plan(name, generation=0):
        return store.create_plan(
            name,
            "trusted-owner",
            generation,
            "a" * 64,
            RobotReference(name, "v1", "b" * 64),
            "epoch-ci",
            "c" * 64,
            "explicit synthetic recovery",
            ("position-long", "position-short"),
        )

    return store, plan


def _safety_recovery_confirm_ci(store, record, connect=None):
    db = (connect or store.connect)()
    try:
        cur = db.cursor()
        store.read_in_transaction(cur)
        receipt = store.confirm_in_transaction(
            cur,
            record["plan"].plan_id,
            record["digest"],
            epoch="epoch-ci",
            checkpoint_sha256="a" * 64,
            obligation_ids=("position-long", "position-short"),
        )
        db.commit()
        return receipt
    finally:
        db.close()


def test_safety_recovery_sql_generation_race_and_historical_attribution(mind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import pytest

    from pipmind.safety.recovery_sql import RecoveryRefused

    store, plan = _safety_recovery_case_ci(mind)
    records = [plan("first"), plan("competing")]
    barrier = Barrier(2)

    def confirm(record):
        barrier.wait(timeout=10)
        try:
            return _safety_recovery_confirm_ci(store, record)
        except RecoveryRefused as error:
            assert str(error) == "stale_assignment_generation"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, record) for record in records]
        receipts = [future.result(timeout=20) for future in futures]
    assert sum(item is not None for item in receipts) == 1
    original = next(item for item in receipts if item is not None)
    winner = next(record for record in records if record["plan"].plan_id == original["plan_id"])
    assert store.read().held and store.read().generation == 1
    next_plan = plan("replacement", 1)
    changed = _safety_recovery_confirm_ci(store, next_plan)
    assert changed["previous_assignment"] == original["assignment"]
    assert changed["position_obligation_ids"] == ["position-long", "position-short"]
    assert _safety_recovery_confirm_ci(store, winner) == original
    assert store.initialize().generation == 2
    with pytest.raises(RecoveryRefused, match="^stale_assignment_generation$"):
        plan("stale-new")


def test_safety_recovery_sql_same_cursor_lock_blocks_competing_confirmation(mind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from pipmind.safety.recovery_sql import SqlRecoveryStore

    store, plan = _safety_recovery_case_ci(mind)
    record = plan("held")
    attempted = Event()

    class Cursor:
        def __init__(self, cur):
            self.cur = cur

        def execute(self, query, *args):
            if "sp_getapplock" in query:
                attempted.set()
            return self.cur.execute(query, *args)

        def __getattr__(self, name):
            return getattr(self.cur, name)

    class Connection:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return Cursor(self.db.cursor())

        def commit(self):
            self.db.commit()

        def close(self):
            self.db.close()

    writer = SqlRecoveryStore(Connection, store.binding)
    db = mind.connect()
    try:
        cur = db.cursor()
        initial = store.read_in_transaction(cur)
        assert store.plan_in_transaction(cur, "held") == record
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_safety_recovery_confirm_ci, writer, record)
            try:
                assert attempted.wait(timeout=5) and not future.done()
                assert store.read_in_transaction(cur) == initial
            finally:
                db.commit()
            assert future.result(timeout=10)["assignment"]["generation"] == 1
    finally:
        db.close()


def test_safety_recovery_sql_staged_error_interrupt_disconnect_roll_back(mind):
    import pytest

    from pipmind.safety.recovery_sql import RecoveryUnavailable
    from pipmind.safety.session import account_key

    for mode in ("failure", "interrupt", "disconnect"):
        store, plan = _safety_recovery_case_ci(mind)
        record = plan(mode)
        db = mind.connect()
        staged = []

        class Cursor:
            def __init__(self, cur, mode, db, staged):
                self.cur, self.mode, self.db, self.staged = cur, mode, db, staged

            def execute(self, query, *args):
                if query.startswith("UPDATE dbo.pm_safety_recovery_accounts_v1"):
                    self.staged.append(True)
                    if self.mode == "disconnect":
                        self.db.close()
                    if self.mode == "interrupt":
                        raise KeyboardInterrupt("synthetic interruption")
                    raise OSError("synthetic write failure")
                return self.cur.execute(query, *args)

            def __getattr__(self, name):
                return getattr(self.cur, name)

        try:
            cur = Cursor(db.cursor(), mode, db, staged)
            store.read_in_transaction(cur)
            expected = KeyboardInterrupt if mode == "interrupt" else RecoveryUnavailable
            with pytest.raises(expected):
                store.confirm_in_transaction(
                    cur,
                    mode,
                    record["digest"],
                    epoch="epoch-ci",
                    checkpoint_sha256="a" * 64,
                    obligation_ids=("position-long", "position-short"),
                )
        finally:
            db.close()
        assert staged == [True] and store.read().generation == 0
        check = mind.connect()
        try:
            cur = check.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM dbo.pm_safety_recovery_transitions_v1 WHERE account_key=%s",
                (account_key(store.binding),),
            )
            assert cur.fetchone()[0] == 0
        finally:
            check.close()
        assert _safety_recovery_confirm_ci(store, record)["held"]


def test_safety_recovery_sql_lost_commit_ack_and_restart_lookup(mind):
    import pytest

    from pipmind.safety.recovery_sql import RecoveryCommitUnknown, RobotReference, SqlRecoveryStore

    store, plan = _safety_recovery_case_ci(mind)

    class Connection:
        def __init__(self):
            self.db = mind.connect()

        @property
        def autocommit(self):
            return self.db.autocommit

        def cursor(self):
            return self.db.cursor()

        def commit(self):
            self.db.commit()
            raise KeyboardInterrupt("synthetic lost acknowledgement after effect")

        def close(self):
            self.db.close()

    uncertain = SqlRecoveryStore(Connection, store.binding)
    with pytest.raises(RecoveryCommitUnknown, match="^recovery_commit_unknown$"):
        uncertain.create_plan(
            "ack",
            "trusted-owner",
            0,
            "a" * 64,
            RobotReference("ack", "v1", "b" * 64),
            "epoch-ci",
            "c" * 64,
            "explicit synthetic recovery",
            ("position-long", "position-short"),
        )
    record = plan("ack")
    receipt = _safety_recovery_confirm_ci(store, record)
    restarted = SqlRecoveryStore(mind.connect, store.binding)
    assert restarted.read().generation == 1
    assert _safety_recovery_confirm_ci(restarted, record) == receipt


def test_safety_recovery_sql_index_or_body_corruption_refuses(mind):
    import pytest

    from pipmind.safety.recovery_sql import RecoveryUnavailable
    from pipmind.safety.session import account_key

    store, plan = _safety_recovery_case_ci(mind)
    record = plan("corruption")
    _safety_recovery_confirm_ci(store, record)
    db = mind.connect()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE dbo.pm_safety_recovery_accounts_v1 SET generation=0 WHERE account_key=%s",
            (account_key(store.binding),),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(RecoveryUnavailable, match="^recovery_store_unavailable$"):
        store.read()
    with pytest.raises(RecoveryUnavailable, match="^recovery_store_unavailable$"):
        store.initialize()


def _recovery_gateway_sql_ci(mind, external, external_pin):
    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.attribution_sql import SqlAttributionStore
    from pipmind.safety.attribution_sql import bootstrap as attribution_bootstrap
    from pipmind.safety.authority_sql import SqlAuthorityStore
    from pipmind.safety.incarnation import read_incarnation
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.lease_sql import SqlLeaseStore
    from pipmind.safety.recovery_flow import DryRecoveryCoordinator, rebind_held
    from pipmind.safety.recovery_sql import SqlRecoveryStore
    from pipmind.safety.recovery_sql import bootstrap as recovery_bootstrap

    gateway, _, old, sessions, controls, old_grant, request, book, _ = _safety_gateway_case_ci(
        mind, ready=False
    )
    with mind.transaction() as cur:
        pin = read_incarnation(cur)
    authority = SqlAuthorityStore(external, old.binding, external_pin)
    held = authority.initialize(pin)
    recovery_bootstrap(mind.connect)
    attribution_bootstrap(mind.connect)
    recovery = SqlRecoveryStore(mind.connect, sessions.binding)
    recovery.initialize()
    rebind_held(authority, held, mind.connect, old.authority_epoch)
    leases = SqlLeaseStore(mind.connect, old.binding, held.epoch, incarnation=pin)
    grant = leases.acquire("recovery-owner", ttl_seconds=300)
    admission = SqlAdmissionStore(mind.connect, leases, sessions, book)
    gateway = SqlSafetyGateway(
        admission, controls, gateway._symbols, base_units_per_size=gateway._unit_factors
    )
    attribution = SqlAttributionStore(mind.connect, sessions.binding)
    flow = DryRecoveryCoordinator(authority, held, leases, recovery)
    return flow, gateway, attribution, recovery, grant, request, old, old_grant, book


def _confirm_recovery_sql_ci(flow, grant, request, suffix="A"):
    from pipmind.safety.recovery_sql import RobotReference

    record = flow.prepare(
        grant,
        plan_id="handover-" + suffix,
        actor_id="owner",
        expected_generation=flow.recovery.read().generation,
        target_robot=RobotReference("robot-" + suffix, "v1", digest("robot-" + suffix)),
        reason="Explicit synthetic handover with existing obligations",
        position_obligation_ids=tuple(request.evidence.observed.position_ids),
    )
    receipt = flow.confirm(grant, record["plan"].plan_id, record["digest"])
    assert receipt["held"] is True and flow.authority.read().mode == "held"
    return flow.recovery.read().assignment


def test_recovery_flow_sql_handover_preserves_account_and_original_attribution(mind):
    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.authority_sql import AuthorityRefused
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore
    from pipmind.safety.recovery_flow import (
        DryRecoveryCoordinator,
        RecoveryAwareDryGateway,
        rebind_held,
    )

    with _authority_case_ci(mind) as (_, _, external, external_pin, _):
        flow, gateway, attribution, recovery, grant, request, old, old_grant, book = (
            _recovery_gateway_sql_ci(mind, external, external_pin)
        )
        with pytest.raises(LeaseRefused):
            old.compare_and_act(old_grant, lambda cur: None)
        first = _confirm_recovery_sql_ci(flow, grant, request)
        active = flow.activate_dry(gateway, attribution, grant, first, request)["authority"]
        wrapper = RecoveryAwareDryGateway(
            flow.authority, active, gateway, recovery, attribution, first
        )
        accepted = wrapper.accept(grant, request)
        assert accepted["gateway_receipt"]["outcome"] == "dry_accepted", accepted
        before = (gateway.admission.snapshot(), gateway.controls.read(), mind.head())
        flow.authority.fence(
            "fence-B",
            "owner",
            active.revision,
            active.epoch,
            active.application_incarnation,
            "Confirmed robot replacement",
        )
        held = flow.authority.read()
        with pytest.raises(AuthorityRefused):
            wrapper.accept(grant, request)
        rebind_held(flow.authority, held, mind.connect, active.epoch)
        leases = SqlLeaseStore(
            mind.connect, old.binding, held.epoch, incarnation=held.application_incarnation
        )
        new_grant = leases.acquire("replacement-owner", ttl_seconds=300)
        admission = SqlAdmissionStore(mind.connect, leases, gateway.admission.sessions, book)
        next_gateway = SqlSafetyGateway(
            admission, gateway.controls, gateway._symbols, base_units_per_size=gateway._unit_factors
        )
        next_flow = DryRecoveryCoordinator(flow.authority, held, leases, recovery)
        second = _confirm_recovery_sql_ci(next_flow, new_grant, request, "B")
        next_active = next_flow.activate_dry(next_gateway, attribution, new_grant, second, request)[
            "authority"
        ]
        replacement = RecoveryAwareDryGateway(
            flow.authority, next_active, next_gateway, recovery, attribution, second
        )
        assert replacement.accept(new_grant, request) == accepted
        link = attribution.lookup("accept", request.order["client_order_id"])
        assert link.assignment == first and first.robot.robot_id == "robot-A"
        assert (admission.snapshot(), gateway.controls.read(), mind.head()) == before
        assert before[2]["paused"] is True and accepted["dispatch_authorized"] is False


def test_recovery_flow_sql_checkpoint_change_refuses_confirmation_without_reset(mind):
    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.recovery_sql import RobotReference

    with _authority_case_ci(mind) as (_, _, external, external_pin, _):
        flow, gateway, _, recovery, grant, request, _, _, _ = _recovery_gateway_sql_ci(
            mind, external, external_pin
        )
        record = flow.prepare(
            grant,
            plan_id="stale-plan",
            actor_id="owner",
            expected_generation=0,
            target_robot=RobotReference("robot", "v1", "c" * 64),
            reason="Explicit test",
            position_obligation_ids=tuple(request.evidence.observed.position_ids),
        )
        gateway.controls.submit_suggestion("preserved-tail", "owner", "Exact retained history")
        changed = gateway.controls.read()
        with pytest.raises(AuthorityUnavailable):
            flow.confirm(grant, "stale-plan", record["digest"])
        assert recovery.read().assignment is None and gateway.controls.read() == changed
        assert flow.authority.read() == flow.expected


def _recovery_saved_mind_ci(store, moment=MOMENT):
    bundle = validate_bundle(export_experience(store, moment))
    # A new export deliberately has a fresh export_id and ZIP entry timestamps.
    # Compare every retained file byte, all other manifest fields and exact SQL
    # head/record state instead of treating the archive container as history.
    return (
        {k: v for k, v in bundle.manifest.items() if k != "export_id"},
        {k: v for k, v in bundle.files.items() if k != "manifest.json"},
        store.head(),
        store.retained(),
    )


@pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_LOCAL") != "true",
    reason="Native restore requires disposable local SQL",
)
def test_recovery_native_restore_exact_history_and_stale_pin_refusal(mind):
    import re

    from pipmind.feeds.common import identity
    from pipmind.feeds.fixtures import pulse
    from pipmind.feeds.sql_store import SqlFeedStore
    from pipmind.safety.attribution_sql import SqlAttributionStore
    from pipmind.safety.authority_sql import AuthorityRefused
    from pipmind.safety.checkpoint_sql import NativeBackupReference
    from pipmind.safety.incarnation import read_incarnation
    from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore
    from pipmind.safety.recovery_flow import (
        DryRecoveryCoordinator,
        RecoveryAwareDryGateway,
        rebind_held,
    )
    from pipmind.safety.recovery_sql import SqlRecoveryStore

    names = ["recovery_" + uuid4().hex for _ in range(2)]
    assert all(re.fullmatch(r"recovery_[0-9a-f]{32}", n) for n in names)
    source_name, target_name = names
    backup_path = "/var/opt/mssql/data/" + source_name + ".bak"
    admin = mind.connect()
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"CREATE DATABASE [{source_name}]")
    try:
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": source_name})
        migrate(connect)
        source = SqlMind(connect, "native-original")
        source.initialize(
            "Exact original identity",
            {"version": "v1", "text": "Exact"},
            MOMENT.isoformat(),
            binding=digest("same-account"),
        )
        with _authority_case_ci(mind) as (_, _, external, external_pin, _):
            flow, gateway, attribution, recovery, grant, request, _, _, _ = (
                _recovery_gateway_sql_ci(source, external, external_pin)
            )
            assignment = _confirm_recovery_sql_ci(flow, grant, request)
            active = flow.activate_dry(gateway, attribution, grant, assignment, request)[
                "authority"
            ]
            wrapper = RecoveryAwareDryGateway(
                flow.authority, active, gateway, recovery, attribution, assignment
            )
            assert wrapper.accept(grant, request)["gateway_receipt"]["outcome"] == "dry_accepted"
            exact_link = attribution.lookup("accept", request.order["client_order_id"])
            flow.authority.fence(
                "backup-fence",
                "owner",
                active.revision,
                active.epoch,
                active.application_incarnation,
                "Explicit consistent backup checkpoint",
            )
            held = flow.authority.read()
            rebind_held(flow.authority, held, connect, active.epoch)
            pinned = SqlLeaseStore(
                connect, flow.leases.binding, held.epoch, incarnation=held.application_incarnation
            )
            grant = pinned.acquire("backup-owner", ttl_seconds=300)
            flow = DryRecoveryCoordinator(flow.authority, held, pinned, recovery)
            feeds = SqlFeedStore(connect)
            key = "native-backup-event"
            feeds.ingest("macro", key, {"exact": "source payload"}, identity(key), MOMENT)
            event = pulse("macro", MOMENT)
            event["event_id"] = key
            feeds.complete_batch("macro", key, [event], MOMENT, cursor=(key, "complete"))
            exact_feed = feeds.handoff(include_calendar=True)
            with source.transaction() as record_cursor:
                source._insert(
                    record_cursor,
                    metadata("native-journal", "journal", MOMENT.isoformat(), source.trader),
                    "Exact original-trader journal and provenance retained by native backup.",
                )
                source._write_working(
                    record_cursor,
                    {"views": ["Exact dated belief"], "waiting_for": [], "position_theses": []},
                    MOMENT,
                )
                record_cursor.execute(
                    "UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (source.trader,)
                )
            inventory = flow.capture(grant)
            exact_mind = _recovery_saved_mind_ci(source)
            exact_runtime = source.runtime("feed_status")
            exact_assignment = recovery.read()
            cur.execute(
                f"BACKUP DATABASE [{source_name}] TO DISK=N'{backup_path}' "
                "WITH INIT,COPY_ONLY,CHECKSUM"
            )
            while cur.nextset():
                pass
            cur.execute(f"RESTORE VERIFYONLY FROM DISK=N'{backup_path}' WITH CHECKSUM")
            while cur.nextset():
                pass
            cur.execute(
                f"RESTORE DATABASE [{target_name}] FROM DISK=N'{backup_path}' "
                f"WITH MOVE N'{source_name}' TO N'/var/opt/mssql/data/{target_name}.mdf',"
                f"MOVE N'{source_name}_log' TO N'/var/opt/mssql/data/{target_name}_log.ldf',"
                "CHECKSUM,RECOVERY"
            )
            while cur.nextset():
                pass
            restored_connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": target_name})
            restored = SqlMind(restored_connect, source.trader)
            assert _recovery_saved_mind_ci(restored) == exact_mind
            restored_recovery = SqlRecoveryStore(restored_connect, recovery.binding)
            assert restored_recovery.read() == exact_assignment
            restored_feeds = SqlFeedStore(restored_connect)
            assert restored_feeds.handoff(include_calendar=True) == exact_feed
            assert restored_feeds.state(key) == "complete"
            with restored_feeds.transaction() as feed_cursor:
                feed_cursor.execute("SELECT status,raw FROM pf_inbox WHERE k=%s", (key,))
                inbox = feed_cursor.fetchone()
                assert inbox[0] == "complete" and loads(inbox[1]) == {"exact": "source payload"}
            assert (
                SqlAttributionStore(restored_connect, recovery.binding).lookup(
                    "accept", request.order["client_order_id"]
                )
                == exact_link
            )
            db = restored_connect()
            try:
                restored_pin = read_incarnation(db.cursor())
            finally:
                db.close()
            assert restored_pin.database_guid != flow.leases.incarnation.database_guid
            stale = SqlLeaseStore(
                restored_connect,
                flow.leases.binding,
                flow.expected.epoch,
                incarnation=flow.leases.incarnation,
            )
            with pytest.raises(LeaseRefused, match="^lease_incarnation_mismatch$"):
                stale.compare_and_act(grant, lambda cursor: None)
            with pytest.raises(LeaseRefused):
                DryRecoveryCoordinator(
                    flow.authority,
                    flow.expected,
                    SqlLeaseStore(
                        restored_connect,
                        stale.binding,
                        flow.expected.epoch,
                        incarnation=restored_pin,
                    ),
                    restored_recovery,
                )
            flow.authority.fence(
                "restore-fence",
                "owner",
                flow.expected.revision,
                flow.expected.epoch,
                restored_pin,
                "Explicit native restore",
            )
            held = flow.authority.read()
            restored_flow = DryRecoveryCoordinator(
                flow.authority,
                held,
                SqlLeaseStore(
                    restored_connect, stale.binding, held.epoch, incarnation=restored_pin
                ),
                restored_recovery,
            )
            # Only a syntactically valid sidecar; file identity/provenance remains
            # a trusted backup-provider responsibility, not a claim of this API.
            reference = NativeBackupReference(
                "test-backup", "a" * 64, MOMENT.isoformat(), inventory.sha256
            )
            assert restored_flow.verify_restored(inventory, reference) == inventory
            assert restored.head()["paused"] is True and assignment == exact_assignment.assignment
            # Resume the original-trader dry path on the restored database only
            # after fresh source evidence, a new native-pinned lease and plan.
            from dataclasses import replace
            from datetime import UTC

            from pipmind.safety.admission_sql import SqlAdmissionStore
            from pipmind.safety.controls_sql import SqlControlStore
            from pipmind.safety.integration_sql import SqlSafetyGateway
            from pipmind.safety.session_sql import SqlSessionStore

            rebind_held(flow.authority, held, restored_connect, flow.expected.epoch)
            restored_leases = restored_flow.leases
            new_grant = restored_leases.acquire("restored-owner", ttl_seconds=300)
            sessions = SqlSessionStore(
                restored_connect, recovery.binding, gateway.admission.session_id
            )
            with restored.transaction() as clock_cursor:
                clock_cursor.execute("SELECT SYSUTCDATETIME()")
                now = clock_cursor.fetchone()[0].replace(tzinfo=UTC)
            fresh, fresh_state, _ = _safety_admission_request_ci(
                now=now,
                account=recovery.binding.account_id,
                request_id="recovered-proposal",
                marks=(request.order["client_order_id"],),
            )
            previous_state = sessions.read()
            sessions.observe(
                replace(
                    fresh_state.latest,
                    observation_id="restored-fresh-observation",
                    cash_flow_started_at=previous_state.spec.started_at,
                ),
                now=now,
            )
            current_state = sessions.read()
            fresh = replace(
                fresh,
                expected_session_revision=current_state.revision,
                expected_observation_sha256=current_state.latest_sha256,
            )
            restored_admission = SqlAdmissionStore(
                restored_connect, restored_leases, sessions, gateway.admission._policies
            )
            controls = SqlControlStore(restored_connect, recovery.binding)
            restored_gateway = SqlSafetyGateway(
                restored_admission,
                controls,
                gateway._symbols,
                base_units_per_size=gateway._unit_factors,
            )
            record = restored_flow.prepare(
                new_grant,
                plan_id="recover-original",
                actor_id="owner",
                expected_generation=exact_assignment.generation,
                target_robot=assignment.robot,
                reason="Confirmed original-trader recovery",
                position_obligation_ids=tuple(fresh.evidence.observed.position_ids),
            )
            restored_flow.confirm(new_grant, "recover-original", record["digest"])
            recovered_assignment = restored_recovery.read().assignment
            restored_attribution = SqlAttributionStore(restored_connect, recovery.binding)
            resumed = restored_flow.activate_dry(
                restored_gateway, restored_attribution, new_grant, recovered_assignment, fresh
            )
            assert resumed["authority"].mode == "active" and resumed["engine_resumed"] is False
            assert restored_admission.snapshot()[0] == gateway.admission.snapshot()[0]
            assert controls.read() == gateway.controls.read()
            assert (
                restored_attribution.lookup("accept", request.order["client_order_id"])
                == exact_link
            )
            with pytest.raises(AuthorityRefused):
                flow.capture(grant)
            # Also prove an in-place database rewind changes native identity,
            # not merely a restore under a different database name.
            source.set_runtime("feed_status", {"exact": "retained until native rewind"})
            assert source.runtime("feed_status") != exact_runtime
            original_pin = flow.leases.incarnation
            cur.execute(f"ALTER DATABASE [{source_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(
                f"RESTORE DATABASE [{source_name}] FROM DISK=N'{backup_path}' "
                "WITH REPLACE,CHECKSUM,RECOVERY"
            )
            while cur.nextset():
                pass
            cur.execute(f"ALTER DATABASE [{source_name}] SET MULTI_USER")
            db = connect()
            try:
                rewound_pin = read_incarnation(db.cursor())
            finally:
                db.close()
            assert rewound_pin != original_pin
            with pytest.raises(LeaseRefused, match="^lease_incarnation_mismatch$"):
                flow.leases.compare_and_act(grant, lambda cursor: None)
            assert _recovery_saved_mind_ci(source) == exact_mind
            assert source.runtime("feed_status") == exact_runtime
    finally:
        for name in reversed(names):
            cur.execute(
                f"IF DB_ID(N'{name}') IS NOT NULL BEGIN "
                f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                f"DROP DATABASE [{name}]; END"
            )
        admin.close()


"""PRIVATE actual SQL attribution hook proof for serial root integration."""


def test_recovery_attribution_sql_atomic_hook_links_and_original_assignment_replay(
    mind, monkeypatch
):
    from dataclasses import asdict, replace
    from uuid import uuid4

    import pytest

    from pipmind.contracts import stamp
    from pipmind.safety.attribution_sql import SqlAttributionStore, _lock, bootstrap
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.lease_sql import LeaseUnavailable
    from pipmind.safety.recovery_sql import Assignment, RobotReference
    from pipmind.safety.session import account_key

    gateway, admission, _, sessions, controls, grant, request, _, now = _safety_gateway_case_ci(
        mind
    )
    bootstrap(mind.connect)
    attribution = SqlAttributionStore(mind.connect, sessions.binding)
    current = Assignment(
        sessions.binding,
        1,
        "assignment-A",
        RobotReference("robot-A", "v1", "a" * 64),
        grant.authority_epoch,
        "b" * 64,
        stamp(now),
    )
    context = [current]

    def before(cur, given):
        assert given == grant
        sessions.read_in_transaction(cur)
        controls.read_in_transaction(cur)
        _lock(cur, "pipmind-safety-recovery-v1:" + account_key(sessions.binding))
        return context[0]

    def after(cur, assignment, result):
        link = attribution.record_in_transaction(cur, assignment, result)
        return {
            "gateway_receipt": result,
            "attribution": asdict(link),
            "dispatch_authorized": False,
        }

    hooked = SqlSafetyGateway(
        admission,
        controls,
        gateway._symbols,
        base_units_per_size=gateway._unit_factors,
        before=before,
        after=after,
    )
    original = hooked.accept(grant, request)
    assert original["attribution"]["assignment"]["assignment_id"] == "assignment-A"
    assert len(admission.snapshot()[0]) == 1
    # This is attribution-only replay proof: an enclosing recovery coordinator
    # separately verifies the real current assignment/authority before calling.
    context[0] = replace(
        current,
        assignment_id="assignment-B",
        generation=2,
        robot=RobotReference("robot-B", "v2", "c" * 64),
    )
    assert hooked.accept(grant, request) == original
    reopened = SqlAttributionStore(mind.connect, sessions.binding)
    assert reopened.lookup("accept", request.order["client_order_id"]).assignment == current

    check_id = "staged-check-" + uuid4().hex
    journal_before = _safety_gateway_journal_ci(mind, gateway)
    real_record = attribution.record_in_transaction
    staged = []

    def fail(cur, assignment, result):
        link = real_record(cur, assignment, result)
        staged.append(link)
        raise OSError("synthetic failure after attribution SQL insert")

    monkeypatch.setattr(attribution, "record_in_transaction", fail)
    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        hooked.check(grant, check_id, request)
    assert len(staged) == 1
    assert reopened.lookup("check", check_id) is None
    assert _safety_gateway_journal_ci(mind, gateway) == journal_before
    assert len(admission.snapshot()[0]) == 1
    monkeypatch.setattr(attribution, "record_in_transaction", real_record)
    assert hooked.check(grant, check_id, request)["gateway_receipt"]["outcome"] == "dry_checked"


"""Root integrates this real-SQL regression; never execute it against hosted SQL."""


def test_recovery_sql_authority_disconnect_fence_is_not_application_quiescence(mind, monkeypatch):
    import os
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    import pytest

    from pipmind.safety.authority_sql import AuthorityUnavailable, SqlAuthorityStore
    from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore
    from pipmind.safety.recovery_flow import (
        DryRecoveryCoordinator,
        RecoveryAwareDryGateway,
        rebind_held,
    )

    assert os.environ.get("PIPMIND_SQL_LOCAL") == "true"
    with _authority_case_ci(mind) as (_, _, external, external_pin, _):  # noqa: F821
        flow, gateway, attribution, recovery, grant, request, _, _, _ = _recovery_gateway_sql_ci(  # noqa: F821
            mind, external, external_pin
        )
        first = _confirm_recovery_sql_ci(flow, grant, request, "disconnect-A")  # noqa: F821
        active = flow.activate_dry(gateway, attribution, grant, first, request)["authority"]
        staged, release, rebind_attempted = Event(), Event(), Event()
        guard_spids, staged_assignments = [], []

        class GuardConnection:
            def __init__(self):
                self.db = external()
                cur = self.db.cursor()
                cur.execute("SELECT @@SPID")
                spid = cur.fetchone()[0]
                assert type(spid) is int and 1 <= spid <= 32767
                guard_spids.append(spid)

            @property
            def autocommit(self):
                return self.db.autocommit

            def cursor(self):
                return self.db.cursor()

            def commit(self):
                self.db.commit()

            def close(self):
                self.db.close()

        worker_authority = SqlAuthorityStore(GuardConnection, flow.leases.binding, external_pin)
        wrapper = RecoveryAwareDryGateway(
            worker_authority, active, gateway, recovery, attribution, first
        )
        original_compare = flow.leases.compare_and_act

        def delayed_compare(current_grant, callback):
            def operation(cur):
                result = callback(cur)
                assert result["gateway_receipt"]["outcome"] == "dry_accepted"
                assert result["attribution"]["assignment"]["assignment_id"] == first.assignment_id
                staged_assignments.append(result["attribution"]["assignment"]["assignment_id"])
                # All acceptance/attribution writes have executed on the lease
                # transaction. No main commit or returned permit exists yet.
                staged.set()
                assert release.wait(timeout=10)
                return result

            return original_compare(current_grant, operation)

        monkeypatch.setattr(flow.leases, "compare_and_act", delayed_compare)

        class RebindCursor:
            def __init__(self, cur):
                self.cur = cur

            def execute(self, query, *args):
                if "sp_getapplock" in query:
                    rebind_attempted.set()
                return self.cur.execute(query, *args)

            def __getattr__(self, name):
                return getattr(self.cur, name)

        class RebindConnection:
            def __init__(self):
                self.db = mind.connect()

            @property
            def autocommit(self):
                return self.db.autocommit

            def cursor(self):
                return RebindCursor(self.db.cursor())

            def commit(self):
                self.db.commit()

            def close(self):
                self.db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(wrapper.accept, grant, request)
            try:
                assert staged.wait(timeout=5)
                assert len(guard_spids) == 1 and staged_assignments == [first.assignment_id]
                # KILL is explicitly restricted to the captured live guard SPID
                # on the disposable local CI server. The admin session differs.
                admin = external()
                try:
                    admin.autocommit = True
                    cur = admin.cursor()
                    cur.execute("SELECT @@SPID")
                    assert cur.fetchone()[0] != guard_spids[0]
                    cur.execute("KILL " + str(guard_spids[0]))
                    while cur.nextset():
                        pass
                finally:
                    admin.close()

                fence = flow.authority.fence(
                    "disconnect-fence",
                    "owner",
                    active.revision,
                    active.epoch,
                    active.application_incarnation,
                    "Explicit fence after loss of an in-flight external guard connection",
                )
                held = flow.authority.read()
                assert fence["after"]["epoch"] == held.epoch and held.mode == "held"
                assert held.assignment_generation == first.generation
                assert not running.done()  # Fence acknowledged; app transaction still staged.
                rebinding = pool.submit(
                    rebind_held, flow.authority, held, RebindConnection, active.epoch
                )
                assert rebind_attempted.wait(timeout=3)
                with pytest.raises(TimeoutError):
                    rebinding.result(timeout=0.2)
            finally:
                release.set()
            # The application write may commit, but the severed external guard
            # must prevent the outer operation from returning a positive result.
            with pytest.raises(AuthorityUnavailable):
                running.result(timeout=5)
            rebound = rebinding.result(timeout=5)

        assert rebound["generation"] > grant.generation and rebound["held"] is True
        assert flow.authority.read() == held
        assert len(gateway.admission.snapshot()[0]) == 1
        original_link = attribution.lookup("accept", request.order["client_order_id"])
        assert original_link.assignment == first and original_link.dispatch_authorized is False
        with pytest.raises(LeaseRefused):
            original_compare(grant, lambda cur: pytest.fail("old owner reached SQL callback"))

        current = SqlLeaseStore(
            mind.connect, flow.leases.binding, held.epoch, incarnation=held.application_incarnation
        )
        replacement_grant = current.acquire("replacement-owner", ttl_seconds=300)
        assert replacement_grant.generation > rebound["generation"]
        replacement_flow = DryRecoveryCoordinator(flow.authority, held, current, recovery)
        second = _confirm_recovery_sql_ci(  # noqa: F821
            replacement_flow, replacement_grant, request, "disconnect-B"
        )
        assert second.generation == first.generation + 1 and second.epoch == held.epoch
        assert attribution.lookup("accept", request.order["client_order_id"]) == original_link
        assert len(gateway.admission.snapshot()[0]) == 1
        assert recovery.read().held and flow.authority.read() == held and mind.head()["paused"]


def test_recovery_sql_target_barrier_does_not_substitute_for_missing_original_source(mind):
    import os
    import re
    from uuid import uuid4

    import pytest

    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.incarnation import read_incarnation
    from pipmind.safety.lease_sql import SqlLeaseStore, bootstrap
    from pipmind.safety.recovery_flow import rebind_held
    from pipmind.shadow.sql import connector

    assert os.environ.get("PIPMIND_SQL_LOCAL") == "true"
    name = "barrier_target_" + uuid4().hex
    assert re.fullmatch(r"barrier_target_[0-9a-f]{32}", name)
    admin = mind.connect()
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"CREATE DATABASE [{name}]")
    try:
        target_connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
        target = target_connect()
        try:
            target_pin = read_incarnation(target.cursor())
        finally:
            target.close()
        bootstrap(target_connect)
        with _authority_case_ci(mind) as (_, _, external, external_pin, _):  # noqa: F821
            flow, gateway, attribution, recovery, grant, request, _, _, _ = (
                _recovery_gateway_sql_ci(  # noqa: F821
                    mind, external, external_pin
                )
            )
            first = _confirm_recovery_sql_ci(flow, grant, request, "missing-source")  # noqa: F821
            active = flow.activate_dry(gateway, attribution, grant, first, request)["authority"]
            assert active.application_incarnation != target_pin
            # Initial source rebind's E1 barrier predates ACTIVE E1 work. A
            # direct target fence E2 must require a NEW original-source proof.
            flow.authority.fence(
                "target-before-source",
                "owner",
                active.revision,
                active.epoch,
                target_pin,
                "Deliberate negative test: source has not been drained",
            )
            held = flow.authority.read()
            source_pointer = (held.source_incarnation, held.source_epoch)
            assert source_pointer == (active.application_incarnation, held.epoch)
            target_leases = SqlLeaseStore(
                target_connect, flow.leases.binding, held.epoch, incarnation=target_pin
            )
            target_leases.initialize()  # Fresh, empty target; never resets an existing owner.
            target_barrier = rebind_held(flow.authority, held, target_connect, held.epoch)
            assert target_barrier["held"] and target_barrier["generation"] == 0
            with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
                with flow.authority.guard(held, mode="held") as guard:
                    guard.activate("must-not-activate", first.generation + 1, "a" * 64)
            assert flow.authority.read() == held
            # Another HELD target fence must not overwrite the original source
            # pointer or make the absent source acknowledgement disappear.
            flow.authority.fence(
                "target-again",
                "owner",
                held.revision,
                held.epoch,
                target_pin,
                "Repeated target fence must preserve required original source",
            )
            next_held = flow.authority.read()
            assert (next_held.source_incarnation, next_held.source_epoch) == source_pointer
            rebind_held(flow.authority, next_held, target_connect, held.epoch)
            with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
                with flow.authority.guard(next_held, mode="held") as guard:
                    guard.activate("still-must-not-activate", first.generation + 1, "a" * 64)
            assert flow.authority.read() == next_held and recovery.read().assignment == first
    finally:
        cur.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        cur.execute(f"DROP DATABASE [{name}]")
        admin.close()


"""Focused root-integrated SQL proof; explicit synthetic facts, no venue calls."""


def _open_recovery_position_request_ci(request, state, now):
    from copy import deepcopy
    from dataclasses import replace
    from decimal import Decimal as D

    from pipmind.contracts import stamp
    from pipmind.reconciliation import ProtectiveOrder
    from pipmind.safety import Conversion, Exposure
    from pipmind.safety.admission import (
        PositionMark,
        ReservationMark,
        checkpoint_digest,
        decode_request,
        encode_request,
    )
    from pipmind.safety.session import advance

    when = stamp(now)
    conversion = Conversion("USD", "USD", D("1"), when)
    positions, protections, updates, marks, exposures = [], [], [], [], []
    for identity, side, size, stop in (
        ("open-long", "buy", D("100"), D("1.09")),
        ("open-short", "sell", D("60"), D("1.11")),
    ):
        opposite = "sell" if side == "buy" else "buy"
        protection_request = {
            "schema_version": 1,
            "client_order_id": "stop-" + identity,
            "action": "modify",
            "instrument": "EUR/USD",
            "side": opposite,
            "size": size,
            "stop": stop,
        }
        positions.append(
            {"instrument": "EUR/USD", "side": side, "size": size, "average_price": D("1.1")}
        )
        protections.append(ProtectiveOrder(identity, "broker-" + identity, protection_request))
        updates.append(
            {
                "schema_version": 1,
                "client_order_id": protection_request["client_order_id"],
                "broker_order_id": "broker-" + identity,
                "status": "working",
                "instrument": "EUR/USD",
                "side": opposite,
                "size": size,
                "filled_size": D("0"),
                "ts": when,
            }
        )
        marks.append(PositionMark(identity, D("1.1"), D("1.1002"), when, D("1")))
        exposures.append(
            Exposure(
                identity,
                "EUR/USD",
                side,
                size,
                D("1.1") if side == "buy" else D("1.1002"),
                stop,
                when,
                conversion,
            )
        )
    observed = replace(
        request.evidence.observed,
        account={**request.evidence.observed.account, "as_of": when},
        positions={"schema_version": 1, "as_of": when, "positions": positions},
        position_ids=("open-long", "open-short"),
        account_complete=True,
        positions_complete=True,
        order_updates=tuple(updates),
        orders_as_of=when,
        orders_complete=True,
        protective_orders=tuple(protections),
        protections_as_of=when,
        protections_complete=True,
    )
    evidence = replace(
        request.evidence,
        expected=deepcopy(observed),
        observed=observed,
        protective_obligations=tuple(protections),
        position_marks=tuple(marks),
    )
    facts = replace(
        request.facts,
        as_of=when,
        exposures=tuple(exposures),
        proposal=replace(request.facts.proposal, as_of=when, conversion=conversion),
    )
    proposal = replace(
        request,
        order={**request.order, "client_order_id": "open-position-proposal"},
        facts=facts,
        evidence=evidence,
        reservation_marks=(
            ReservationMark(
                "open-position-proposal", D("1.1"), D("1.1002"), when, conversion, D("40")
            ),
        ),
    )
    observation = replace(
        state.latest,
        observation_id="open-position-source",
        as_of=when,
        received_at=when,
        equity=observed.account["equity"],
        cash_flow_ends_at=when,
        account_evidence_id=checkpoint_digest(evidence, facts),
    )
    updated, _ = advance(state, observation, now=now)
    proposal = replace(
        proposal,
        expected_session_revision=updated.revision,
        expected_observation_sha256=updated.latest_sha256,
    )
    assert encode_request(decode_request(encode_request(proposal))) == encode_request(proposal)
    return proposal, updated


def test_recovery_sql_open_long_short_handover_preserves_obligations_and_original_attribution(mind):
    from dataclasses import replace
    from datetime import UTC

    import pytest

    from pipmind.safety.admission import encode_request
    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore
    from pipmind.safety.recovery_flow import (
        DryRecoveryCoordinator,
        RecoveryAwareDryGateway,
        rebind_held,
    )

    with _authority_case_ci(mind) as (_, _, external, external_pin, _):  # noqa: F821
        flow, gateway, attribution, recovery, grant, base, _, _, book = _recovery_gateway_sql_ci(  # noqa: F821
            mind, external, external_pin
        )
        sessions = gateway.admission.sessions
        with mind.transaction() as cur:
            cur.execute("SELECT SYSUTCDATETIME()")
            now = cur.fetchone()[0].replace(tzinfo=UTC)
        request, updated = _open_recovery_position_request_ci(base, sessions.read(), now)
        sessions.observe(updated.latest, now=now)
        assert sessions.read() == updated
        raw_request = encode_request(request)
        first = _confirm_recovery_sql_ci(flow, grant, request, "positions-A")  # noqa: F821
        active = flow.activate_dry(gateway, attribution, grant, first, request)["authority"]
        wrapper = RecoveryAwareDryGateway(
            flow.authority, active, gateway, recovery, attribution, first
        )
        accepted = wrapper.accept(grant, request)
        assert accepted["gateway_receipt"]["outcome"] == "dry_accepted"
        original_link = attribution.lookup("accept", request.order["client_order_id"])
        assert original_link.assignment == first
        before = (
            gateway.admission.snapshot(),
            gateway.controls.read(),
            sessions.read(),
            mind.head(),
        )
        assert len(before[0][0]) == 1

        flow.authority.fence(
            "open-position-handover",
            "owner",
            active.revision,
            active.epoch,
            active.application_incarnation,
            "Explicit replacement retaining both protected open positions and pending risk",
        )
        held = flow.authority.read()
        rebind_held(flow.authority, held, mind.connect, active.epoch)
        leases = SqlLeaseStore(
            mind.connect, flow.leases.binding, held.epoch, incarnation=held.application_incarnation
        )
        new_grant = leases.acquire("open-position-replacement", ttl_seconds=300)
        admission = SqlAdmissionStore(mind.connect, leases, sessions, book)
        next_gateway = SqlSafetyGateway(
            admission,
            gateway.controls,
            gateway._symbols,
            base_units_per_size=gateway._unit_factors,
        )
        next_flow = DryRecoveryCoordinator(flow.authority, held, leases, recovery)
        second = _confirm_recovery_sql_ci(next_flow, new_grant, request, "positions-B")  # noqa: F821
        assert second.generation == first.generation + 1
        # Supplementary broker identity loss is incomplete evidence, never a
        # reason to drop the remaining position, flatten, or activate anyway.
        missing = replace(
            request,
            evidence=replace(
                request.evidence,
                observed=replace(request.evidence.observed, position_ids=("open-long",)),
            ),
        )
        with pytest.raises(LeaseRefused, match="^recovery_obligations_changed$"):
            next_flow.activate_dry(next_gateway, attribution, new_grant, second, missing)
        assert flow.authority.read() == held
        result = next_flow.activate_dry(next_gateway, attribution, new_grant, second, request)
        assert result["authority"].mode == "active"
        assert result["engine_resumed"] is result["dispatch_authorized"] is False
        replacement = RecoveryAwareDryGateway(
            flow.authority, result["authority"], next_gateway, recovery, attribution, second
        )
        assert replacement.accept(new_grant, request) == accepted
        assert attribution.lookup("accept", request.order["client_order_id"]) == original_link
        assert (
            admission.snapshot(),
            gateway.controls.read(),
            sessions.read(),
            mind.head(),
        ) == before
        assert encode_request(request) == raw_request
        assert request.evidence.observed.position_ids == ("open-long", "open-short")
        assert tuple(row["side"] for row in request.evidence.observed.positions["positions"]) == (
            "buy",
            "sell",
        )
        assert len(request.evidence.observed.protective_orders) == 2 and recovery.read().held


def _command_journal_reopen_ci(connect, flow):
    from pipmind.safety.command_journal_sql import SqlCommandJournal
    from pipmind.safety.controls_sql import SqlControlStore
    from pipmind.safety.lease_sql import SqlLeaseStore
    from pipmind.safety.recovery_sql import SqlRecoveryStore

    leases = SqlLeaseStore(
        connect,
        flow.leases.binding,
        flow.leases.authority_epoch,
        incarnation=flow.leases.incarnation,
    )
    return SqlCommandJournal(
        connect,
        leases,
        SqlRecoveryStore(connect, flow.recovery.binding),
        SqlControlStore(connect, flow.recovery.binding),
    )


def _command_journal_case_ci(mind):
    from contextlib import contextmanager

    from pipmind.safety.command_journal_sql import bootstrap

    @contextmanager
    def case():
        with _authority_case_ci(mind) as (_, _, external, pin, _):  # noqa: F821
            flow, gateway, attribution, recovery, grant, request, _, _, _ = (
                _recovery_gateway_sql_ci(mind, external, pin)  # noqa: F821
            )
            assignment = _confirm_recovery_sql_ci(  # noqa: F821
                flow, grant, request, "command-original"
            )
            active = flow.activate_dry(gateway, attribution, grant, assignment, request)[
                "authority"
            ]
            bootstrap(mind.connect)
            journal = _command_journal_reopen_ci(mind.connect, flow)
            yield flow, active, journal, grant, assignment, request, gateway

    return case()


def _command_journal_call_ci(flow, active, journal, grant, assignment, callback):
    # This tests the real local boundary inside an actual ACTIVE guard, not a
    # claim that the command store independently verifies external authority.
    with flow.authority.guard(active):

        def operation(cur):
            journal.controls.read_in_transaction(cur)
            assert journal.recovery.read_in_transaction(cur).assignment == assignment
            return callback(cur)

        return journal.leases.compare_and_act(grant, operation)


def test_command_journal_sql_exact_replay_collision_reopen_and_original_scope(mind):
    from copy import deepcopy
    from dataclasses import replace

    import pytest

    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.command_journal import encode_command, encode_order, fingerprint

    with _command_journal_case_ci(mind) as (flow, active, journal, grant, assignment, request, _):
        order = deepcopy(request.order)
        command_id = order["client_order_id"]

        def record(cur):
            return journal.record_in_transaction(
                cur, grant, assignment, order, causation_id="retained-input-A"
            )

        original = _command_journal_call_ci(flow, active, journal, grant, assignment, record)
        exact = encode_command(original)
        assert original.order_body == encode_order(order)
        assert original.scope.assignment == assignment
        assert original.scope.owner_id == grant.owner_id
        assert original.scope.lease_generation == grant.generation
        assert original.scope.control_revision == journal.controls.read().revision
        assert original.dispatch_authorized is False
        before = journal.lookup(command_id)
        assert before.command == original and before.attempt is None and before.unknown is None
        assert (
            _command_journal_call_ci(flow, active, journal, grant, assignment, record) == original
        )

        # Changes under the same immutable ID cannot replace body or causation.
        order["side"] = "sell"
        with pytest.raises(AuthorityUnavailable):
            _command_journal_call_ci(flow, active, journal, grant, assignment, record)
        order["side"] = "buy"
        with pytest.raises(AuthorityUnavailable):
            _command_journal_call_ci(
                flow,
                active,
                journal,
                grant,
                assignment,
                lambda cur: journal.record_in_transaction(
                    cur, grant, assignment, order, causation_id="changed-cause"
                ),
            )
        assert encode_command(journal.lookup(command_id).command) == exact

        # A new verified local lease owner may retrieve the original scope;
        # replay is not relabelled as a new owner/robot decision.
        journal.leases.release(grant)
        newer = journal.leases.acquire("command-new-owner", ttl_seconds=300)
        assert newer.generation > grant.generation
        reopened = _command_journal_reopen_ci(mind.connect, flow)
        replay = _command_journal_call_ci(
            flow,
            active,
            reopened,
            newer,
            assignment,
            lambda cur: reopened.record_in_transaction(
                cur, newer, assignment, order, causation_id="retained-input-A"
            ),
        )
        assert replay == original and fingerprint(encode_command(replay)) == fingerprint(exact)
        assert reopened.lookup(command_id) == before
        assert list(reopened.iter_history()) == [before]
        # A fabricated caller assignment cannot rewrite the retained one.
        with pytest.raises(AuthorityUnavailable):
            _command_journal_call_ci(
                flow,
                active,
                reopened,
                newer,
                assignment,
                lambda cur: reopened.record_in_transaction(
                    cur,
                    newer,
                    replace(assignment, assignment_id="fabricated-replacement"),
                    order,
                    causation_id="retained-input-A",
                ),
            )
        assert reopened.lookup(command_id) == before


def test_command_journal_sql_concurrent_ids_and_competing_attempts_preserve_one_marker(mind):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy

    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.command_journal import encode_attempt, encode_command, fingerprint

    with _command_journal_case_ci(mind) as (flow, active, journal, grant, assignment, request, _):
        order = deepcopy(request.order)
        command_id = order["client_order_id"]

        def record(body):
            return _command_journal_call_ci(
                flow,
                active,
                journal,
                grant,
                assignment,
                lambda cur: journal.record_in_transaction(
                    cur, grant, assignment, body, causation_id="concurrent-input"
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(record, order), pool.submit(record, deepcopy(order))
            assert first.result(timeout=15) == second.result(timeout=15)
        original = journal.lookup(command_id).command

        def collide(body):
            try:
                return record(body)
            except AuthorityUnavailable:
                return None

        changed = deepcopy(order)
        changed["side"] = "sell"
        with ThreadPoolExecutor(max_workers=2) as pool:
            same, different = pool.submit(collide, order), pool.submit(collide, changed)
            assert same.result(timeout=15) == original
            assert different.result(timeout=15) is None
        assert journal.lookup(command_id).command == original

        def attempt(attempt_id):
            try:
                return _command_journal_call_ci(
                    flow,
                    active,
                    journal,
                    grant,
                    assignment,
                    lambda cur: journal.attempt_in_transaction(
                        cur, grant, assignment, command_id, attempt_id
                    ),
                )
            except AuthorityUnavailable:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, value) for value in ("attempt-A", "attempt-B")]
            results = [future.result(timeout=15) for future in futures]
        assert sum(value is not None for value in results) == 1
        retained = journal.lookup(command_id)
        assert retained.command == original and retained.unknown is None
        assert retained.attempt == next(value for value in results if value is not None)
        assert retained.attempt.command_sha256 == fingerprint(encode_command(original))
        assert attempt(retained.attempt.attempt_id) == retained.attempt
        unknown = _command_journal_call_ci(
            flow,
            active,
            journal,
            grant,
            assignment,
            lambda cur: journal.unknown_in_transaction(cur, grant, assignment, command_id),
        )
        assert unknown.attempt_sha256 == fingerprint(encode_attempt(retained.attempt))
        assert unknown.dispatch_authorized is False
        final = journal.lookup(command_id)
        assert final.command == original and final.attempt == retained.attempt
        assert final.unknown == unknown
        assert final.unresolved is True and final.state == "unknown"
        reopened = _command_journal_reopen_ci(mind.connect, flow)
        assert reopened.lookup(command_id) == final and list(reopened.iter_history()) == [final]


def test_command_journal_sql_staged_rollback_interrupt_disconnect_and_commit_unknown(mind):
    from copy import deepcopy

    import pytest

    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.lease_sql import LeaseCommitUnknown

    with _command_journal_case_ci(mind) as (
        flow,
        active,
        journal,
        grant,
        assignment,
        request,
        gateway,
    ):
        original_controls = journal.controls.read()
        original_session = gateway.admission._sessions.read()
        original_reservations = gateway.admission.snapshot()
        # A retained prefix ensures rollback is checked against existing history.
        prefix_order = deepcopy(request.order)
        prefix_order["client_order_id"] = "immutable-prefix"
        _command_journal_call_ci(
            flow,
            active,
            journal,
            grant,
            assignment,
            lambda cur: journal.record_in_transaction(
                cur, grant, assignment, prefix_order, causation_id="prefix-cause"
            ),
        )
        prefix = list(journal.iter_history())

        def run_fault(fault):
            opened, closed = [], []

            class Connection:
                def __init__(self):
                    self.db = mind.connect()
                    opened.append(self.db)

                @property
                def autocommit(self):
                    return self.db.autocommit

                def cursor(self):
                    return self.db.cursor()

                def commit(self):
                    self.db.commit()
                    if fault == "commit-ack":
                        raise SystemExit("synthetic committed acknowledgement loss")

                def close(self):
                    closed.append(True)
                    self.db.close()

            failing = _command_journal_reopen_ci(Connection, flow)
            order = deepcopy(request.order)
            order["client_order_id"] = "staged-" + fault
            staged = []

            def operation(cur):
                command = failing.record_in_transaction(
                    cur, grant, assignment, order, causation_id="staged-cause"
                )
                attempt = failing.attempt_in_transaction(
                    cur, grant, assignment, command.command_id, "one-attempt"
                )
                unknown = failing.unknown_in_transaction(cur, grant, assignment, command.command_id)
                staged.append((command, attempt, unknown))
                if fault == "interrupt":
                    raise KeyboardInterrupt
                if fault == "disconnect":
                    opened[0].close()
                if fault != "commit-ack":
                    raise OSError("synthetic failure after all three staged records")
                return command

            expected = (
                LeaseCommitUnknown
                if fault == "commit-ack"
                else KeyboardInterrupt
                if fault == "interrupt"
                else AuthorityUnavailable
            )
            with pytest.raises(expected):
                _command_journal_call_ci(flow, active, failing, grant, assignment, operation)
            assert closed == [True] and len(staged) == 1
            retained = _command_journal_reopen_ci(mind.connect, flow).lookup(
                order["client_order_id"]
            )
            if fault == "commit-ack":
                assert (retained.command, retained.attempt, retained.unknown) == staged[0]
                assert retained.dispatch_authorized is False
            else:
                assert retained is None
                assert list(journal.iter_history()) == prefix

        for fault in ("error", "interrupt", "disconnect", "commit-ack"):
            run_fault(fault)
        assert journal.controls.read() == original_controls
        assert gateway.admission._sessions.read() == original_session
        assert gateway.admission.snapshot() == original_reservations


def test_command_journal_sql_native_backup_restores_exact_unresolved_history_and_attribution(mind):
    import os
    import re
    from copy import deepcopy
    from uuid import uuid4

    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.attribution_sql import SqlAttributionStore
    from pipmind.safety.command_journal import encode_attempt, encode_command, encode_unknown
    from pipmind.safety.command_journal_sql import SqlCommandJournal
    from pipmind.safety.controls_sql import SqlControlStore
    from pipmind.safety.lease_sql import SqlLeaseStore
    from pipmind.safety.recovery_flow import RecoveryAwareDryGateway
    from pipmind.safety.recovery_sql import SqlRecoveryStore
    from pipmind.safety.session_sql import SqlSessionStore
    from pipmind.shadow.sql import connector, migrate
    from pipmind.shadow.store import SqlMind

    assert os.environ.get("PIPMIND_SQL_LOCAL") == "true"
    names = ["command_backup_" + uuid4().hex for _ in range(2)]
    assert all(re.fullmatch(r"command_backup_[0-9a-f]{32}", name) for name in names)
    source_name, target_name = names
    backup_path = "/var/opt/mssql/data/" + source_name + ".bak"
    admin = mind.connect()
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"CREATE DATABASE [{source_name}]")
    try:
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": source_name})
        migrate(connect)
        source = SqlMind(connect, "command-source")
        source.initialize(
            "Exact original command-source mind",
            {"version": "v1", "text": "Exact"},
            "2026-10-21T00:00:00Z",
            binding="a" * 64,
        )
        with _command_journal_case_ci(source) as (
            flow,
            active,
            journal,
            grant,
            assignment,
            request,
            gateway,
        ):
            attribution = SqlAttributionStore(connect, journal.binding)
            dry = RecoveryAwareDryGateway(
                flow.authority, active, gateway, flow.recovery, attribution, assignment
            )
            assert dry.accept(grant, request)["gateway_receipt"]["outcome"] == "dry_accepted"
            original_link = attribution.lookup("accept", request.order["client_order_id"])
            order = deepcopy(request.order)
            command_id = order["client_order_id"]

            def operation(cursor):
                command = journal.record_in_transaction(
                    cursor, grant, assignment, order, causation_id="backup-input"
                )
                attempt = journal.attempt_in_transaction(
                    cursor, grant, assignment, command_id, "backup-attempt"
                )
                unknown = journal.unknown_in_transaction(cursor, grant, assignment, command_id)
                return command, attempt, unknown

            command, attempt, unknown = _command_journal_call_ci(
                flow, active, journal, grant, assignment, operation
            )
            expected = journal.lookup(command_id)
            exact = (encode_command(command), encode_attempt(attempt), encode_unknown(unknown))
            controls_before = journal.controls.read()
            recovery_before = journal.recovery.read()
            reservations_before = gateway.admission.snapshot()
            assert len(reservations_before[0]) == 1 and original_link.assignment == assignment
            cur.execute(
                f"BACKUP DATABASE [{source_name}] TO DISK=N'{backup_path}' "
                "WITH INIT,COPY_ONLY,CHECKSUM"
            )
            while cur.nextset():
                pass
            cur.execute(f"RESTORE VERIFYONLY FROM DISK=N'{backup_path}' WITH CHECKSUM")
            while cur.nextset():
                pass
            cur.execute(
                f"RESTORE DATABASE [{target_name}] FROM DISK=N'{backup_path}' "
                f"WITH MOVE N'{source_name}' TO N'/var/opt/mssql/data/{target_name}.mdf',"
                f"MOVE N'{source_name}_log' TO N'/var/opt/mssql/data/{target_name}_log.ldf',"
                "CHECKSUM,RECOVERY"
            )
            while cur.nextset():
                pass
            restored_connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": target_name})
            # Historical reads deliberately do not adopt/activate the new DB.
            # The supplied old pin remains old, never a recovered send permit.
            restored = SqlCommandJournal(
                restored_connect,
                SqlLeaseStore(
                    restored_connect,
                    flow.leases.binding,
                    flow.leases.authority_epoch,
                    incarnation=flow.leases.incarnation,
                ),
                SqlRecoveryStore(restored_connect, journal.binding),
                SqlControlStore(restored_connect, journal.binding),
            )
            actual = restored.lookup(command_id)
            assert actual == expected and list(restored.iter_history()) == [expected]
            assert (
                encode_command(actual.command),
                encode_attempt(actual.attempt),
                encode_unknown(actual.unknown),
            ) == exact
            assert actual.command.scope.assignment == assignment
            assert actual.attempt.scope.assignment == assignment
            assert actual.unknown.scope.assignment == assignment
            assert actual.dispatch_authorized is False
            assert restored.controls.read() == controls_before
            assert restored.recovery.read() == recovery_before
            restored_sessions = SqlSessionStore(
                restored_connect, journal.binding, gateway.admission.session_id
            )
            restored_admission = SqlAdmissionStore(
                restored_connect, restored.leases, restored_sessions, gateway.admission._policies
            )
            assert restored_sessions.read() == gateway.admission._sessions.read()
            assert restored_admission.snapshot() == reservations_before
            assert (
                SqlAttributionStore(restored_connect, journal.binding).lookup(
                    "accept", request.order["client_order_id"]
                )
                == original_link
            )
            assert gateway.admission.snapshot() == reservations_before
            assert SqlMind(restored_connect, source.trader).head()["paused"] is True
    finally:
        for name in reversed(names):
            cur.execute(
                f"IF DB_ID(N'{name}') IS NOT NULL BEGIN "
                f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                f"DROP DATABASE [{name}]; END"
            )
        admin.close()


def test_command_journal_sql_current_controls_released_lease_and_assignment_boundaries(mind):
    from dataclasses import replace

    import pytest

    from pipmind.safety.authority_sql import AuthorityUnavailable

    with _command_journal_case_ci(mind) as (flow, active, journal, grant, assignment, request, _):  # noqa: F821
        order = {**request.order, "client_order_id": "control-original"}

        def call(callback, current_grant=grant):
            return _command_journal_call_ci(  # noqa: F821
                flow, active, journal, current_grant, assignment, callback
            )

        original = call(
            lambda cur: journal.record_in_transaction(
                cur, grant, assignment, order, causation_id="control-source"
            )
        )
        attempt = call(
            lambda cur: journal.attempt_in_transaction(
                cur, grant, assignment, "control-original", "original-attempt"
            )
        )
        for action in ("pause", "kill"):
            before = journal.controls.read()
            held = journal.controls.apply_override(
                "command-test-" + action,
                "owner",
                action,
                before.revision,
                "Explicit synthetic control-boundary proof",
            )["state"]
            assert held["paused"] is True
            new_id = "held-audit-" + action
            audit = call(
                lambda cur, identity=new_id: journal.record_in_transaction(
                    cur,
                    grant,
                    assignment,
                    {**request.order, "client_order_id": identity},
                    causation_id="held-source",
                )
            )
            assert audit.scope.control_revision == held["revision"]
            assert audit.local_outcome == "recorded" and audit.dispatch_authorized is False
            refused = call(
                lambda cur, identity=new_id + "-refused": journal.record_in_transaction(
                    cur,
                    grant,
                    assignment,
                    {**request.order, "client_order_id": identity},
                    causation_id="held-refusal-source",
                    local_outcome="refused",
                    code="locally_refused",
                )
            )
            assert (
                refused.local_outcome == "refused"
                and refused.scope.control_revision == held["revision"]
            )
            # Registration is an audit fact, never permission to create a marker
            # under either pause or kill. Nothing is implicitly lifted.
            with pytest.raises(AuthorityUnavailable):
                call(
                    lambda cur, identity=new_id: journal.attempt_in_transaction(
                        cur, grant, assignment, identity, "must-not-mark"
                    )
                )
            assert journal.lookup(new_id).attempt is None
            unknown = call(
                lambda cur: journal.unknown_in_transaction(
                    cur, grant, assignment, "control-original"
                )
            )
            assert unknown.scope.control_revision >= original.scope.control_revision
            retained = journal.lookup("control-original")
            assert (
                retained.command == original and retained.attempt == attempt and retained.unresolved
            )

        journal.leases.release(grant)
        with pytest.raises(AuthorityUnavailable):
            call(
                lambda cur: journal.record_in_transaction(
                    cur,
                    grant,
                    assignment,
                    {**request.order, "client_order_id": "released-owner"},
                    causation_id="released-source",
                )
            )
        assert journal.lookup("released-owner") is None
        current = journal.leases.acquire("new-current-owner", ttl_seconds=300)
        with pytest.raises(AuthorityUnavailable):
            call(
                lambda cur: journal.record_in_transaction(
                    cur,
                    current,
                    replace(assignment, assignment_id="unretained-assignment"),
                    {**request.order, "client_order_id": "wrong-assignment"},
                    causation_id="wrong-source",
                ),
                current,
            )
        assert journal.lookup("wrong-assignment") is None
        assert journal.lookup("control-original").command == original


def test_command_journal_sql_poststaging_lease_expiry_rolls_back_account_and_intent(
    mind, monkeypatch
):
    import pytest

    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.session import account_key

    with _command_journal_case_ci(mind) as (flow, active, journal, old, assignment, request, _):  # noqa: F821
        journal.leases.release(old)
        grant = journal.leases.acquire("short-command-owner", ttl_seconds=2)
        staged = []
        original = journal._save

        def delayed(cur, table, record, encoder):
            original(cur, table, record, encoder)
            staged.append(record.command_id)
            cur.execute("WAITFOR DELAY '00:00:02.100'")

        monkeypatch.setattr(journal, "_save", delayed)
        with pytest.raises(AuthorityUnavailable):
            _command_journal_call_ci(  # noqa: F821
                flow,
                active,
                journal,
                grant,
                assignment,
                lambda cur: journal.record_in_transaction(
                    cur, grant, assignment, request.order, causation_id="expiry-source"
                ),
            )
        assert staged == [request.order["client_order_id"]]
        assert journal.lookup(request.order["client_order_id"]) is None
        with mind.transaction() as cur:
            for table in ("accounts", "intents", "attempts", "unknown"):
                cur.execute(
                    "SELECT COUNT(*) FROM dbo.pm_safety_command_"
                    + table
                    + "_v1 WHERE account_key=%s",
                    (account_key(journal.binding),),
                )
                assert cur.fetchone()[0] == 0


def test_command_journal_sql_confirmed_handover_preserves_A_and_B_only_observes_unknown(mind):
    import pytest

    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.attribution_sql import SqlAttributionStore
    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.command_journal import encode_attempt, encode_command, fingerprint
    from pipmind.safety.command_journal_sql import SqlCommandJournal
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.lease_sql import SqlLeaseStore
    from pipmind.safety.recovery_flow import (
        DryRecoveryCoordinator,
        RecoveryAwareDryGateway,
        rebind_held,
    )

    with _command_journal_case_ci(mind) as (flow, active, journal, grant, first, request, gateway):  # noqa: F821
        order = request.order
        command_id = order["client_order_id"]
        original = _command_journal_call_ci(  # noqa: F821
            flow,
            active,
            journal,
            grant,
            first,
            lambda cur: journal.record_in_transaction(
                cur, grant, first, order, causation_id="original-A"
            ),
        )
        attempt = _command_journal_call_ci(  # noqa: F821
            flow,
            active,
            journal,
            grant,
            first,
            lambda cur: journal.attempt_in_transaction(cur, grant, first, command_id, "attempt-A"),
        )
        untouched_id = "unattempted-A"
        untouched = _command_journal_call_ci(  # noqa: F821
            flow,
            active,
            journal,
            grant,
            first,
            lambda cur: journal.record_in_transaction(
                cur,
                grant,
                first,
                {**order, "client_order_id": untouched_id},
                causation_id="unattempted-A",
            ),
        )
        attribution = SqlAttributionStore(mind.connect, journal.binding)
        wrapped = RecoveryAwareDryGateway(
            flow.authority, active, gateway, flow.recovery, attribution, first
        )
        accepted = wrapped.accept(grant, request)
        assert accepted["gateway_receipt"]["outcome"] == "dry_accepted"
        original_link = attribution.lookup("accept", command_id)
        before = (
            gateway.admission.snapshot(),
            gateway.controls.read(),
            gateway.admission.sessions.read(),
            mind.head(),
        )
        assert len(before[0][0]) == 1

        flow.authority.fence(
            "command-handover-B",
            "owner",
            active.revision,
            active.epoch,
            active.application_incarnation,
            "Explicit confirmed dry handover retaining unresolved command obligations",
        )
        held = flow.authority.read()
        rebind_held(flow.authority, held, mind.connect, active.epoch)
        leases = SqlLeaseStore(
            mind.connect, flow.leases.binding, held.epoch, incarnation=held.application_incarnation
        )
        new_grant = leases.acquire("command-owner-B", ttl_seconds=300)
        admission = SqlAdmissionStore(
            mind.connect, leases, gateway.admission.sessions, gateway.admission._policies
        )
        next_gateway = SqlSafetyGateway(
            admission, gateway.controls, gateway._symbols, base_units_per_size=gateway._unit_factors
        )
        next_flow = DryRecoveryCoordinator(flow.authority, held, leases, flow.recovery)
        second = _confirm_recovery_sql_ci(next_flow, new_grant, request, "command-B")  # noqa: F821
        # A confirmed metadata assignment stays HELD while original commands
        # remain unresolved. The real external guard deliberately normalizes
        # callback refusals, so assert its actual public error and retained hold.
        with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
            next_flow.activate_dry(next_gateway, attribution, new_grant, second, request)
        assert flow.authority.read() == held and second != first
        current = SqlCommandJournal(mind.connect, leases, flow.recovery, gateway.controls)

        def call(callback):
            # Explicit trusted-owner audit under the HELD authority; this does
            # not establish ACTIVE routing or permission to send another order.
            with next_flow.authority.guard(held, mode="held"):

                def operation(cur):
                    current.controls.read_in_transaction(cur)
                    assert current.recovery.read_in_transaction(cur).assignment == second
                    return callback(cur)

                return leases.compare_and_act(new_grant, operation)

        unknown = call(
            lambda cur: current.unknown_in_transaction(cur, new_grant, second, command_id)
        )
        assert unknown.scope.assignment == second and unknown.scope.owner_id == new_grant.owner_id
        assert unknown.command_sha256 == fingerprint(encode_command(original))
        assert unknown.attempt_sha256 == fingerprint(encode_attempt(attempt))
        assert (
            call(
                lambda cur: current.attempt_in_transaction(
                    cur, new_grant, second, command_id, "attempt-A"
                )
            )
            == attempt
        )  # Original historical marker, never a second attempt.
        with pytest.raises(AuthorityUnavailable):
            call(
                lambda cur: current.attempt_in_transaction(
                    cur, new_grant, second, untouched_id, "must-not-adopt-A"
                )
            )
        retained = current.lookup(command_id)
        assert (
            retained.command == original
            and retained.attempt == attempt
            and retained.unknown == unknown
        )
        assert (
            current.lookup(untouched_id).command == untouched
            and current.lookup(untouched_id).attempt is None
        )
        assert (
            attribution.lookup("accept", command_id) == original_link
            and original_link.assignment == first
        )
        assert (
            admission.snapshot(),
            gateway.controls.read(),
            gateway.admission.sessions.read(),
            mind.head(),
        ) == before
        assert flow.authority.read() == held and flow.recovery.read().held
        complete = next_flow.capture(new_grant)
        assert complete.command_counts.commands == 2
        assert complete.command_counts.unattempted == complete.command_counts.attempted == 1
        assert complete.command_counts.unknown == 1


def test_command_journal_sql_complete_history_crosses_native_keyset_page(mind):
    from pipmind.safety.command_journal import encode_command

    with _command_journal_case_ci(mind) as (flow, active, journal, grant, assignment, request, _):  # noqa: F821

        def batch(cur):
            return [
                journal.record_in_transaction(
                    cur,
                    grant,
                    assignment,
                    {**request.order, "client_order_id": f"page-{index:03d}"},
                    causation_id="page-source",
                )
                for index in range(65)
            ]

        original = _command_journal_call_ci(  # noqa: F821
            flow, active, journal, grant, assignment, batch
        )
        history = list(journal.iter_history())
        assert len(history) == 65 and [row.command for row in history] == original
        assert [row.command.command_id for row in history] == [
            f"page-{index:03d}" for index in range(65)
        ]
        assert all(
            row.attempt is None and row.unknown is None and not row.dispatch_authorized
            for row in history
        )
        assert [encode_command(row.command) for row in journal.iter_history()] == [
            encode_command(row) for row in original
        ]
        # The complete checkpoint must also traverse every physical command
        # beyond its 64-row page boundary, rather than certifying a prefix.
        with flow.authority.guard(active):
            inventory = _p50_capture_ci(  # noqa: F821
                mind.connect, journal.binding, flow.leases.binding
            )
        counts = inventory.command_counts
        assert counts.commands == counts.unattempted == 65
        assert counts.refused == counts.attempted == counts.unknown == 0
        assert [row.count for row in inventory.tables[-4:]] == [1, 65, 0, 0]
        assert [row.command for row in journal.iter_history()] == original


def _p50_capture_ci(connect, binding, lease_binding, *, legacy=False):
    from pipmind.safety.checkpoint_sql import (
        capture_complete_in_transaction,
        capture_in_transaction,
        complete_required_locks,
    )
    from pipmind.safety.integration_sql import _lock

    db = connect()
    try:
        assert db.autocommit is False
        cur = db.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        for resource in complete_required_locks(binding, lease_binding):
            _lock(cur, resource)
        capture = capture_in_transaction if legacy else capture_complete_in_transaction
        return capture(cur, binding, lease_binding)
    finally:
        db.rollback()
        db.close()


def _p50_reheld_ci(connect, flow, active, gateway, suffix):
    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.integration_sql import SqlSafetyGateway
    from pipmind.safety.lease_sql import SqlLeaseStore
    from pipmind.safety.recovery_flow import DryRecoveryCoordinator, rebind_held

    flow.authority.fence(
        suffix,
        "owner",
        active.revision,
        active.epoch,
        active.application_incarnation,
        "Explicit synthetic held checkpoint",
    )
    held = flow.authority.read()
    rebind_held(flow.authority, held, connect, active.epoch)
    leases = SqlLeaseStore(
        connect,
        flow.leases.binding,
        held.epoch,
        incarnation=held.application_incarnation,
    )
    grant = leases.acquire("owner-" + suffix, ttl_seconds=300)
    admission = SqlAdmissionStore(
        connect, leases, gateway.admission.sessions, gateway.admission._policies
    )
    next_gateway = SqlSafetyGateway(
        admission,
        gateway.controls,
        gateway._symbols,
        base_units_per_size=gateway._unit_factors,
    )
    return (
        DryRecoveryCoordinator(flow.authority, held, leases, flow.recovery),
        next_gateway,
        grant,
    )


def _p50_extra_ci(connect, binding):
    from pipmind.safety.session import account_key

    db = connect()
    try:
        cur = db.cursor()
        cur.execute(
            "SELECT channel,record_id,body,sha256 FROM dbo.pm_safety_attribution_v1 "
            "WHERE account_key=%s ORDER BY channel,record_id",
            (account_key(binding),),
        )
        return tuple(tuple(row) for row in cur.fetchall())
    finally:
        db.rollback()
        db.close()


def _p50_unchanged_ci(mind, flow, gateway):
    return (
        _p50_capture_ci(mind.connect, flow.recovery.binding, flow.leases.binding),
        flow.recovery.read(),
        flow.authority.read(),
        gateway.admission.snapshot(),
        gateway.controls.read(),
        gateway.admission.sessions.read(),
        _p50_extra_ci(mind.connect, flow.recovery.binding),
    )


@pytest.mark.parametrize("stage", ("unattempted", "attempted", "unknown"))  # noqa: F821
def test_command_recovery_sql_each_pending_state_holds_before_gateway_or_attribution(mind, stage):
    import pytest

    from pipmind.safety.attribution_sql import SqlAttributionStore
    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.command_journal import encode_command

    with _command_journal_case_ci(mind) as (  # noqa: F821
        flow,
        active,
        journal,
        grant,
        first,
        request,
        gateway,
    ):

        def seed(cur):
            command = journal.record_in_transaction(
                cur, grant, first, request.order, causation_id="pending-" + stage
            )
            if stage != "unattempted":
                journal.attempt_in_transaction(cur, grant, first, command.command_id, "one-attempt")
            if stage == "unknown":
                journal.unknown_in_transaction(cur, grant, first, command.command_id)
            return command

        original = _command_journal_call_ci(flow, active, journal, grant, first, seed)  # noqa: F821
        held_flow, held_gateway, new_grant = _p50_reheld_ci(
            mind.connect, flow, active, gateway, "pending-" + stage
        )
        assignment = _confirm_recovery_sql_ci(  # noqa: F821
            held_flow, new_grant, request, "pending-" + stage
        )
        before = _p50_unchanged_ci(mind, held_flow, held_gateway)
        counts = before[0].command_counts
        assert counts.commands == 1 and counts.refused == 0
        assert counts.unattempted == int(stage == "unattempted")
        assert counts.attempted == int(stage != "unattempted")
        assert counts.unknown == int(stage == "unknown")
        # The real external guard sanitizes inner semantic refusals. Its fixed
        # failure is not an ordinary activation or an acknowledged permission.
        with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
            held_flow.activate_dry(
                held_gateway,
                SqlAttributionStore(mind.connect, journal.binding),
                new_grant,
                assignment,
                request,
            )
        assert _p50_unchanged_ci(mind, held_flow, held_gateway) == before
        reopened = _command_journal_reopen_ci(mind.connect, held_flow)  # noqa: F821
        assert encode_command(reopened.lookup(original.command_id).command) == encode_command(
            original
        )
        assert held_flow.authority.read().mode == "held"


def test_command_recovery_sql_native_restore_complete_profile_and_fail_closed_mutations(
    mind,
):
    import os
    import re
    from dataclasses import replace
    from uuid import uuid4

    import pytest

    from pipmind.safety.admission_sql import SqlAdmissionStore
    from pipmind.safety.attribution_sql import SqlAttributionStore
    from pipmind.safety.authority_sql import AuthorityUnavailable
    from pipmind.safety.checkpoint_sql import (
        COMMAND_TABLES,
        COVERED_TABLES,
        TABLE_VERSION,
        TABLE_VERSION_COMMANDS,
        CheckpointUnavailable,
        CommandCounts,
        NativeBackupReference,
        capture_complete_in_transaction,
        complete_required_locks,
        encode_inventory,
    )
    from pipmind.safety.command_journal import (
        encode_attempt,
        encode_command,
        encode_unknown,
        fingerprint,
    )
    from pipmind.safety.command_journal_sql import bootstrap
    from pipmind.safety.controls_sql import SqlControlStore
    from pipmind.safety.incarnation import read_incarnation
    from pipmind.safety.integration_sql import SqlSafetyGateway, _lock
    from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore
    from pipmind.safety.recovery_flow import (
        DryRecoveryCoordinator,
        RecoveryAwareDryGateway,
        rebind_held,
    )
    from pipmind.safety.session import account_key
    from pipmind.safety.session_sql import SqlSessionStore
    from pipmind.shadow.sql import connector, migrate
    from pipmind.shadow.store import SqlMind

    assert os.environ.get("PIPMIND_SQL_LOCAL") == "true"
    names = ["commands_recovery_" + uuid4().hex for _ in range(2)]
    assert all(re.fullmatch(r"commands_recovery_[0-9a-f]{32}", name) for name in names)
    source_name, target_name = names
    backup_path = "/var/opt/mssql/data/" + source_name + ".bak"
    admin = mind.connect()
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"CREATE DATABASE [{source_name}]")
    try:
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": source_name})
        migrate(connect)
        source = SqlMind(connect, "commands-recovery-source")
        source.initialize(
            "Exact command recovery identity",
            {"version": "v1", "text": "Exact"},
            "2026-10-21T00:00:00Z",
            binding="a" * 64,
        )
        with _authority_case_ci(source) as (_, _, external, external_pin, _):  # noqa: F821
            flow, gateway, attribution, recovery, grant, request, _, _, _ = (
                _recovery_gateway_sql_ci(  # noqa: F821
                    source, external, external_pin
                )
            )
            legacy = flow.capture(grant)
            assert legacy.table_version == TABLE_VERSION and legacy.command_counts is None
            assert tuple(row.name for row in legacy.tables) == COVERED_TABLES
            first = _confirm_recovery_sql_ci(  # noqa: F821
                flow, grant, request, "legacy-command-schema"
            )
            active = flow.activate_dry(gateway, attribution, grant, first, request)["authority"]
            bootstrap(connect)
            journal = _command_journal_reopen_ci(connect, flow)  # noqa: F821
            empty = _p50_capture_ci(connect, recovery.binding, flow.leases.binding)
            assert empty.table_version == TABLE_VERSION_COMMANDS
            assert empty.command_counts == CommandCounts(0, 0, 0, 0, 0)
            flow, gateway, grant = _p50_reheld_ci(connect, flow, active, gateway, "empty-profile")
            first = _confirm_recovery_sql_ci(  # noqa: F821
                flow, grant, request, "empty-profile"
            )
            active = flow.activate_dry(gateway, attribution, grant, first, request)["authority"]
            journal = _command_journal_reopen_ci(connect, flow)  # noqa: F821

            refused = _command_journal_call_ci(  # noqa: F821
                flow,
                active,
                journal,
                grant,
                first,
                lambda c: journal.record_in_transaction(
                    c,
                    grant,
                    first,
                    {**request.order, "client_order_id": "retained-refused"},
                    causation_id="original-refusal-A",
                    local_outcome="refused",
                    code="locally_refused",
                ),
            )
            flow, gateway, grant = _p50_reheld_ci(connect, flow, active, gateway, "refused-only")
            second = _confirm_recovery_sql_ci(flow, grant, request, "refused-only")  # noqa: F821
            assert flow.capture(grant).command_counts == CommandCounts(1, 1, 0, 0, 0)
            active = flow.activate_dry(gateway, attribution, grant, second, request)["authority"]
            journal = _command_journal_reopen_ci(connect, flow)  # noqa: F821
            dry = RecoveryAwareDryGateway(
                flow.authority, active, gateway, recovery, attribution, second
            )
            assert dry.accept(grant, request)["gateway_receipt"]["outcome"] == "dry_accepted"

            def seed(c):
                for label in ("unattempted", "attempted", "unknown"):
                    command = journal.record_in_transaction(
                        c,
                        grant,
                        second,
                        {**request.order, "client_order_id": "retained-" + label},
                        causation_id="original-input-B-" + label,
                    )
                    if label != "unattempted":
                        journal.attempt_in_transaction(
                            c, grant, second, command.command_id, "attempt-B-" + label
                        )
                    if label == "unknown":
                        journal.unknown_in_transaction(c, grant, second, command.command_id)

            _command_journal_call_ci(flow, active, journal, grant, second, seed)  # noqa: F821
            history = tuple(journal.iter_history())
            assert {row.command.scope.assignment for row in history} == {first, second}
            assert (
                next(row.command for row in history if row.command.command_id == refused.command_id)
                == refused
            )
            exact_commands = tuple(
                (
                    encode_command(row.command),
                    None if row.attempt is None else encode_attempt(row.attempt),
                    None if row.unknown is None else encode_unknown(row.unknown),
                )
                for row in history
            )
            flow, gateway, grant = _p50_reheld_ci(connect, flow, active, gateway, "native-backup")
            inventory = flow.capture(grant)
            assert inventory.command_counts == CommandCounts(4, 1, 1, 2, 1)
            assert tuple(row.name for row in inventory.tables) == COVERED_TABLES + COMMAND_TABLES
            reservations = gateway.admission.snapshot()
            assert len(reservations[0]) == 1
            original_assignment = recovery.read()
            original_links = _p50_extra_ci(connect, recovery.binding)
            export_clock = utc("2026-10-21T00:00:00Z")
            original_mind = _recovery_saved_mind_ci(source, export_clock)  # noqa: F821
            cur.execute(
                f"BACKUP DATABASE [{source_name}] TO DISK=N'{backup_path}' "
                "WITH INIT,COPY_ONLY,CHECKSUM"
            )
            while cur.nextset():
                pass
            cur.execute(f"RESTORE VERIFYONLY FROM DISK=N'{backup_path}' WITH CHECKSUM")
            while cur.nextset():
                pass
            cur.execute(
                f"RESTORE DATABASE [{target_name}] FROM DISK=N'{backup_path}' "
                f"WITH MOVE N'{source_name}' TO N'/var/opt/mssql/data/{target_name}.mdf',"
                f"MOVE N'{source_name}_log' TO N'/var/opt/mssql/data/{target_name}_log.ldf',"
                "CHECKSUM,RECOVERY"
            )
            while cur.nextset():
                pass
            restored_connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": target_name})
            assert (
                _p50_capture_ci(restored_connect, recovery.binding, flow.leases.binding)
                == inventory
            )
            restored = _command_journal_reopen_ci(restored_connect, flow)  # noqa: F821
            actual = tuple(restored.iter_history())
            assert actual == history
            assert (
                tuple(
                    (
                        encode_command(row.command),
                        None if row.attempt is None else encode_attempt(row.attempt),
                        None if row.unknown is None else encode_unknown(row.unknown),
                    )
                    for row in actual
                )
                == exact_commands
            )
            assert restored.recovery.read() == original_assignment
            assert _p50_extra_ci(restored_connect, recovery.binding) == original_links
            assert (
                _recovery_saved_mind_ci(  # noqa: F821
                    SqlMind(restored_connect, source.trader), export_clock
                )
                == original_mind
            )

            db = restored_connect()
            try:
                target_pin = read_incarnation(db.cursor())
            finally:
                db.close()
            assert target_pin != flow.leases.incarnation
            flow.authority.fence(
                "restored-command-target",
                "owner",
                flow.expected.revision,
                flow.expected.epoch,
                target_pin,
                "Explicit restored target",
            )
            held = flow.authority.read()
            target_leases = SqlLeaseStore(
                restored_connect,
                flow.leases.binding,
                held.epoch,
                incarnation=target_pin,
            )
            restored_flow = DryRecoveryCoordinator(
                flow.authority, held, target_leases, restored.recovery
            )
            reference = NativeBackupReference(
                "synthetic-file-sidecar",
                "a" * 64,
                "2026-10-21T00:00:00Z",
                inventory.sha256,
            )
            # Native BACKUP/RESTORE CHECKSUM is actual above; the supplied file
            # SHA identity remains a deliberately synthetic provider claim.
            assert restored_flow.verify_restored(inventory, reference) == inventory
            legacy_target = _p50_capture_ci(
                restored_connect, recovery.binding, flow.leases.binding, legacy=True
            )
            with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
                restored_flow.verify_restored(
                    legacy_target,
                    replace(reference, source_inventory_sha256=legacy_target.sha256),
                )
            with pytest.raises(LeaseRefused, match="^recovery_backup_inventory_mismatch$"):
                restored_flow.verify_restored(
                    inventory, replace(reference, source_inventory_sha256="0" * 64)
                )
            assert (
                _p50_capture_ci(restored_connect, recovery.binding, flow.leases.binding)
                == inventory
            )

            rebind_held(flow.authority, held, restored_connect, flow.expected.epoch)
            target_grant = target_leases.acquire("restored-command-owner", ttl_seconds=300)
            sessions = SqlSessionStore(
                restored_connect, recovery.binding, gateway.admission.session_id
            )
            target_gateway = SqlSafetyGateway(
                SqlAdmissionStore(
                    restored_connect,
                    target_leases,
                    sessions,
                    gateway.admission._policies,
                ),
                SqlControlStore(restored_connect, recovery.binding),
                gateway._symbols,
                base_units_per_size=gateway._unit_factors,
            )
            target_assignment = _confirm_recovery_sql_ci(  # noqa: F821
                restored_flow, target_grant, request, "restored-held"
            )
            target_mind = SqlMind(restored_connect, source.trader)
            before = _p50_unchanged_ci(target_mind, restored_flow, target_gateway)
            assert target_gateway.admission.snapshot() == reservations
            with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
                restored_flow.activate_dry(
                    target_gateway,
                    SqlAttributionStore(restored_connect, recovery.binding),
                    target_grant,
                    target_assignment,
                    request,
                )
            assert _p50_unchanged_ci(target_mind, restored_flow, target_gateway) == before

            baseline = before[0]
            original_attempt = next(
                row.attempt for row in actual if row.command.command_id == "retained-attempted"
            )
            bad_ref = encode_attempt(replace(original_attempt, command_sha256="0" * 64))
            mutations = (
                ("DROP TABLE dbo.pm_safety_command_unknown_v1", ()),
                ("CREATE TABLE dbo.pm_safety_command_future_v2(dummy int)", ()),
                (
                    "UPDATE dbo.pm_safety_command_intents_v1 SET sha256=%s "
                    "WHERE account_key=%s AND command_id=%s",
                    ("0" * 64, account_key(recovery.binding), "retained-unattempted"),
                ),
                (
                    "UPDATE dbo.pm_safety_command_attempts_v1 SET body=%s,sha256=%s "
                    "WHERE account_key=%s AND command_id=%s",
                    (
                        bad_ref,
                        fingerprint(bad_ref),
                        account_key(recovery.binding),
                        "retained-attempted",
                    ),
                ),
            )
            for sql, params in mutations:
                db = restored_connect()
                try:
                    c = db.cursor()
                    c.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    for lock in complete_required_locks(recovery.binding, target_leases.binding):
                        _lock(c, lock)
                    c.execute(sql, params)
                    with pytest.raises(
                        CheckpointUnavailable, match="^safety_inventory_unavailable$"
                    ):
                        capture_complete_in_transaction(c, recovery.binding, target_leases.binding)
                finally:
                    db.rollback()
                    db.close()
                assert (
                    _p50_capture_ci(restored_connect, recovery.binding, target_leases.binding)
                    == baseline
                )
                assert (
                    tuple(
                        _command_journal_reopen_ci(  # noqa: F821
                            restored_connect, restored_flow
                        ).iter_history()
                    )
                    == history
                )
            # Metadata visibility is permission-filtered: an apparently absent
            # command schema under a restricted user must not select legacy v1.
            db = restored_connect()
            impersonated = False
            try:
                c = db.cursor()
                c.execute("CREATE USER p50_inventory_limited WITHOUT LOGIN")
                for table in COVERED_TABLES:
                    c.execute(f"GRANT SELECT ON OBJECT::{table} TO p50_inventory_limited")
                c.execute("EXECUTE AS USER='p50_inventory_limited'")
                impersonated = True
                c.execute("SELECT USER_ID()")
                assert c.fetchone()[0] != 1
                c.execute(
                    "SELECT name FROM sys.objects WHERE name LIKE 'pm[_]safety[_]command[_]%' "
                    "AND type IN ('U','V','SN','ET')"
                )
                assert c.fetchall() == []
                for lock in complete_required_locks(recovery.binding, target_leases.binding):
                    _lock(c, lock)
                with pytest.raises(CheckpointUnavailable, match="^safety_inventory_unavailable$"):
                    capture_complete_in_transaction(c, recovery.binding, target_leases.binding)
            finally:
                if impersonated:
                    db.cursor().execute("REVERT")
                db.rollback()
                db.close()
            assert (
                _p50_capture_ci(restored_connect, recovery.binding, target_leases.binding)
                == baseline
            )
            assert encode_inventory(baseline) == encode_inventory(before[0])
    finally:
        for name in reversed(names):
            cur.execute(
                f"IF DB_ID(N'{name}') IS NOT NULL BEGIN "
                f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                f"DROP DATABASE [{name}]; END"
            )
        admin.close()


"""Root appends this self-contained test to the mandatory disposable SQL suite."""


def test_sql_safety_profile_actual_stores_reopen_and_counterexamples(mind):
    import hashlib
    import json
    import time
    from dataclasses import replace
    from uuid import uuid4

    from pipmind.safety.admission import (
        decode_receipt,
        decode_request,
        encode_receipt,
        encode_request,
    )
    from pipmind.safety.eval_evidence import (
        canonical_json,
        fingerprint,
        grade_case,
        sanitized_report,
    )
    from pipmind.safety.eval_runner_sql import DisposableSQLScope, run_sql_profile
    from pipmind.safety.eval_scenarios import SOURCE_PATH, captured_order, load_scenarios

    with mind.transaction() as cur:
        cur.execute("SELECT DB_NAME()")
        database = cur.fetchone()[0]
    specs = load_scenarios()
    started = time.monotonic()
    evidence = run_sql_profile(mind.connect, scope=DisposableSQLScope(uuid4().hex, database, True))
    elapsed = time.monotonic() - started
    for spec, case in zip(specs, evidence, strict=True):
        assert grade_case(spec, case) == (), spec.case_id
    report = sanitized_report(
        specs, evidence, dataset_sha256=hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
    )
    assert report["passed"] is True
    assert report["consistency_only"] is True
    assert report["sql_execution_attested"] is False
    assert report["starter_baseline"] == {"passing": 14, "deferred": 16}
    assert sum(row["observations"] for row in report["cases"]) == 13

    # Mutations below operate on the actual returned/reopened SQL evidence,
    # never on manufactured successful store results or additional SQL calls.
    for spec, case in zip(specs, evidence, strict=True):
        target = case.observations[-1]
        if target.receipt_kind == "admission":
            receipt = decode_receipt(target.returned_body)
            receipt["assessment"] = replace(receipt["assessment"], code="invalid_source_facts")
            body = encode_receipt(receipt)
            changed = replace(target, returned_body=body, stored_body=body, reopened_body=body)
            expected = "specific_refusal"
        elif target.exception_code is not None:
            changed = replace(target, exception_code="lease_store_busy")
            expected = "ownership_refusal"
        else:
            receipt = json.loads(target.returned_body)
            receipt["code"] = "reconciliation_required"
            body = canonical_json(receipt)
            changed = replace(target, returned_body=body, stored_body=body, reopened_body=body)
            expected = "specific_refusal"
        assert expected in grade_case(
            spec, replace(case, observations=case.observations[:-1] + (changed,))
        )
        positive = replace(case.observations[0], reopened_body=None)
        assert "durable_receipt" in grade_case(
            spec, replace(case, observations=(positive,) + case.observations[1:])
        )
        positive = replace(case.observations[0], reservation_body=None)
        assert "assessment_integrity" in grade_case(
            spec, replace(case, observations=(positive,) + case.observations[1:])
        )
        if positive.receipt_kind == "gateway":
            positive = replace(case.observations[0], admission_receipt_body=None)
            assert "assessment_integrity" in grade_case(
                spec, replace(case, observations=(positive,) + case.observations[1:])
            )

        tables = tuple(
            replace(row, count=row.count + 1, sha256="a" * 64)
            if row.name == "dbo.pm_safety_admission_reservations_v1"
            else row
            for row in target.after.tables
        )
        after = replace(target.after, tables=tables)
        changed = replace(target, after=after, reopened=after)
        expected = "stale_owner_write" if spec.case_id == "S07" else "unintended_reservation"
        assert expected in grade_case(
            spec, replace(case, observations=case.observations[:-1] + (changed,))
        )

    # Rehashing an impermissibly shrunk source order cannot disguise fixture drift.
    case, spec = evidence[2], specs[2]
    target = case.observations[-1]
    request = decode_request(target.request_body)
    request.order["size"] /= 100
    raw = encode_request(request)
    receipt = decode_receipt(target.returned_body)
    receipt["request_sha256"] = fingerprint(raw)
    body = encode_receipt(receipt)
    changed = replace(
        target,
        request_body=raw,
        order_body=captured_order(raw),
        returned_body=body,
        stored_body=body,
        reopened_body=body,
    )
    assert "order_integrity" in grade_case(
        spec, replace(case, observations=(case.observations[0], changed))
    )

    case, spec = evidence[1], specs[1]
    target = case.observations[-1]
    receipt = decode_receipt(target.returned_body)
    receipt["assessment"] = replace(receipt["assessment"], reconciliation_findings=())
    body = encode_receipt(receipt)
    changed = replace(target, returned_body=body, stored_body=body, reopened_body=body)
    assert "stale_account_proof" in grade_case(
        spec, replace(case, observations=(case.observations[0], changed))
    )

    case, spec = evidence[3], specs[3]
    changed = replace(case.observations[-1], setup_receipts=(), reopened_setup_receipts=())
    assert "loss_latch_proof" in grade_case(
        spec, replace(case, observations=(case.observations[0], changed))
    )
    # This bounded allowlist is the only emitted material; raw receipts stay private.
    print(
        "SQL_SAFETY_PROFILE "
        + canonical_json({"elapsed_seconds": round(elapsed, 3), "report": report})
    )


def test_sql_azure_identity_candidate_refuses_reference_platform(mind):
    from pipmind.safety.azure_identity_sql import (
        AzureDatabaseIdentity,
        AzureIdentityRefused,
        read_expected_identity,
    )

    with mind.transaction() as cur:
        cur.execute("SELECT CONVERT(int, SERVERPROPERTY('EngineEdition')), DB_NAME()")
        edition, database = cur.fetchone()
        assert edition in (1, 2, 3, 4, 8)
        pin = AzureDatabaseIdentity("synthetic-server", database, "synthetic-physical-pin")
        # The real SQL Server cursor must refuse its observed edition before
        # querying an Azure-only column, not manufacture a supported S0 result.
        with pytest.raises(AzureIdentityRefused, match="^azure_identity_unsupported$"):
            read_expected_identity(cur, pin)
