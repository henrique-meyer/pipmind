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


def episode(mind):
    events = deepcopy(CASE["events"])
    mind.accept(
        "fixture-source",
        [{"sequence": i + 1, "event": event} for i, event in enumerate(events)],
        MOMENT,
    )
    mind.set_paused(False)
    return events, FixtureModel(deepcopy(CASE["model_responses"]))


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
    from pipmind.shadow.service import clock_wake

    clock_wake(mind, MOMENT)
    cursor = mind.checkpoint("clock")
    reopened = SqlMind(mind.connect, mind.trader)
    clock_wake(reopened, MOMENT + timedelta(seconds=1))
    assert reopened.checkpoint("clock") == cursor
    assert reopened.status()["pending"] == 1
    clock_wake(reopened, MOMENT + timedelta(minutes=16))
    assert reopened.status()["pending"] == 2


def test_invalid_model_evidence_is_durably_refused_without_memory_mutation(mind):
    events, model = episode(mind)
    model.responses[events[0]["message_id"]]["reply"]["evidence_ids"] = ["invented-future-record"]
    before = mind.head()["working"]
    asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    assert mind.head()["working"] == before
    result = mind.outcome(events[0]["message_id"])
    assert result["reply"] is None and result["status"] == "model_refused"
    assert result["dispatches"] == 0


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
    "versions", [("shadow-context-v1", "sql-memory-v1"), ("shadow-context-v2", "sql-memory-v2")]
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
    store.complete_batch("macro", key, [good], MOMENT, cursor=(key, "complete"))
    reopened = SqlFeedStore(mind.connect)
    assert reopened.source_id() == store.source_id()
    assert reopened.state(key) == "complete"
    rows = reopened.events()
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
