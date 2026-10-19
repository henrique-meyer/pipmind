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
    from pipmind.contracts import ContractError

    with pytest.raises(ContractError):
        mind.accept("invalid-control", [{"sequence": 1, "event": invalid}], MOMENT)
    assert mind.checkpoint("invalid-control") == 0
    assert mind.pending() is None
