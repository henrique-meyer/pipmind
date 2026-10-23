"""Actual disposable SQL proof of coherent, non-blocking feed read frames."""

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.fixtures import pulse
from pipmind.feeds.sql_store import SqlFeedStore
from pipmind.feeds.store import encoded
from pipmind.shadow.sql import connector

pytestmark = pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_TESTS") != "true" or os.environ.get("PIPMIND_SQL_LOCAL") != "true",
    reason="requires an actual disposable local SQL integration database",
)
NOW = utc("2026-10-23T14:00:00Z")


@pytest.fixture
def store():
    """Each case owns a disposable database/profile; never change shared options."""
    name = "feed_frame_" + uuid4().hex
    admin = connector()()
    admin.autocommit = True
    control = admin.cursor()
    control.execute(f"CREATE DATABASE [{name}]")
    try:
        # Test setup only. Application/library reads never alter this option.
        control.execute(f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
        yield SqlFeedStore(connect, profile="frame-" + uuid4().hex)
    finally:
        control.execute(f"DROP DATABASE [{name}]")
        admin.close()


def inventory(store):
    with store.transaction() as db:
        result = {}
        for table, order in (
            ("pf_meta", "k"),
            ("pf_state", "k"),
            ("pf_inbox", "seq"),
            ("pf_events", "seq"),
            ("pf_alerts", "seq"),
        ):
            db.execute(f"SELECT * FROM {table} ORDER BY {order}")
            result[table] = [tuple(row) for row in db.fetchall()]
        return result


def populate(store):
    with store.transaction() as db:
        store.emit(db, "news", pulse("news", NOW), NOW)
        store.save("canonical:quality", {"status": "unverified", "retained": "exact"}, db=db)
        store.save("calendar:data", {"source": "synthetic", "events": []}, db=db)
        store.save("cursor:exact", {"offset": 123}, db=db)
        store.save("worker:news", {"heartbeat": stamp(NOW), "error": "known_failure"}, db=db)
        for number in range(105):
            db.execute(
                "INSERT pf_alerts(producer,code,ts) VALUES(%s,%s,%s)",
                ("news", f"failure-{number}", stamp(NOW)),
            )
    store.ingest("news", "retained-capture", {"raw": "private-canary"}, "f" * 64, NOW)
    store.quarantine("retained-capture")


def test_status_and_handoff_exact_schema_counts_and_read_only_inventory(store):
    populate(store)
    before = inventory(store)
    expected = {
        "source_id": store.source_id(),
        **store.status(),
        "worker_heartbeats": {
            name: store.state("worker:" + name) for name in ("macro", "news", "social")
        },
    }
    frame = store.status_frame()
    assert frame == expected
    assert frame["events"] == {"news": 1} and frame["quarantined"] == 1
    assert len(frame["alerts"]) == 100 and frame["alerts"][0]["code"] == "failure-104"
    assert frame["worker_heartbeats"]["macro"] is None
    assert frame["worker_heartbeats"]["news"]["error"] == "known_failure"
    assert "private-canary" not in str(frame)
    assert store.handoff(include_calendar=True) == {
        "events": store.events(),
        "coverage": store.state("canonical:quality"),
        "quarantined": 1,
        "calendar": store.state("calendar:data"),
    }
    assert "calendar" not in store.handoff()
    assert inventory(store) == before


def test_snapshot_status_and_handoff_do_not_take_held_writer_lock_or_see_uncommitted_data(store):
    populate(store)
    before_frame = store.status_frame()
    before_handoff = store.handoff(include_calendar=True)
    before = inventory(store)
    payload = deepcopy(pulse("news", NOW))
    payload["event_id"] = "new-committed-event"
    # A read which requests the writer lock cannot finish while this transaction
    # remains open. The future timeout is a deadlock safety bound, not an SLO.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.transaction() as db:
            store.emit(db, "news", payload, NOW)
            store.save("cursor:exact", {"offset": 456}, db=db)
            store.save("worker:news", {"heartbeat": stamp(NOW), "error": None}, db=db)
            store.save("canonical:quality", {"status": "new-committed"}, db=db)
            future = pool.submit(
                lambda: (store.status_frame(), store.handoff(include_calendar=True))
            )
            frame, handoff = future.result(timeout=20)
            assert frame == before_frame
            assert handoff == before_handoff
        # The same next read sees the newly committed version, without caching.
        assert store.status_frame()["events"] == {"news": 2}
        assert store.status_frame()["worker_heartbeats"]["news"]["error"] is None
        rows = store.handoff(include_calendar=True)
        assert len(rows["events"]) == 2 and rows["coverage"] == {"status": "new-committed"}
    assert store.state("cursor:exact") == {"offset": 456}
    after = inventory(store)
    assert after["pf_meta"] == before["pf_meta"]
    assert after["pf_inbox"] == before["pf_inbox"]
    assert after["pf_alerts"] == before["pf_alerts"]
    assert after["pf_events"][:1] == before["pf_events"]


def test_status_frame_keeps_one_snapshot_when_commit_occurs_between_selects(store, monkeypatch):
    populate(store)
    before = store.status_frame()
    status = store.status
    writes = []

    def commit_between_source_and_status(*, db=None):
        assert db is not None
        with store.transaction() as writer:
            store.save("worker:news", {"heartbeat": stamp(NOW), "error": None}, db=writer)
            store.emit(writer, "social", pulse("social", NOW), NOW)
        writes.append(True)
        return status(db=db)

    with monkeypatch.context() as patch:
        patch.setattr(store, "status", commit_between_source_and_status)
        assert store.status_frame() == before
    assert writes == [True]
    current = store.status_frame()
    assert current["events"] == {"news": 1, "social": 1}
    assert current["worker_heartbeats"]["news"]["error"] is None


@pytest.mark.parametrize("method", ["status_frame", "handoff"])
def test_snapshot_read_refuses_changed_profile_without_any_state_rewrite(store, method):
    populate(store)
    changed = identity("replacement-profile")
    with store.transaction() as db:
        store.save("configuration:active", changed, db=db)
    # Capture directly because the old-profile writer helper must now refuse.
    connect = store.connect
    connection = connect()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT k,v FROM pf_state ORDER BY k")
        before = [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()
    with pytest.raises(FeedError, match="^feed_store_profile_changed$"):
        getattr(store, method)()
    connection = connect()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT k,v FROM pf_state ORDER BY k")
        assert [tuple(row) for row in cursor.fetchall()] == before
        cursor.execute("SELECT v FROM pf_state WHERE k='configuration:active'")
        assert cursor.fetchone()[0] == encoded(changed)
    finally:
        connection.close()
