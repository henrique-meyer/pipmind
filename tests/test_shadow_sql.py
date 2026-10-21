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
