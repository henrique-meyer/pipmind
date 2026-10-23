"""Actual SQL source-locked status/ACTIVE coherence and read-only behavior."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from pipmind.contracts import utc
from pipmind.shadow.common import ShadowError, digest
from pipmind.shadow.sql import connector, migrate
from pipmind.shadow.store import SqlMind

pytestmark = pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_TESTS") != "true", reason="separate actual SQL integration gate"
)
NOW = utc("2026-10-23T15:00:00Z")
ACTIVE = "qualification:active"


@pytest.fixture
def mind():
    connect = connector()
    migrate(connect)
    store = SqlMind(connect, "status-frame-" + uuid4().hex)
    store.initialize(
        "Synthetic exact identity",
        {"version": "v1", "text": "Exact playbook"},
        "2026-10-01T00:00:00Z",
        binding=digest("synthetic-binding"),
    )
    with store.transaction() as cursor:
        store._set_runtime(cursor, ACTIVE, {"id": "synthetic", "state": "warming", "samples": 7})
    return store


def event(identifier):
    return {
        "schema_version": 1,
        "kind": "ClockEvent",
        "message_id": identifier,
        "available_at": "2026-10-23T14:00:00Z",
        "payload": {
            "schema_version": 1,
            "event_id": identifier,
            "ts": "2026-10-23T14:00:00Z",
            "reason": "heartbeat",
        },
    }


def inventory(mind):
    with mind.transaction() as cursor:
        result = {"head": mind._head(cursor)}
        for table, order in (
            ("pm_records", "pk"),
            ("pm_inputs", "pk"),
            ("pm_outcomes", "pk"),
            ("pm_runtime", "k"),
            ("pm_cursors", "source"),
        ):
            cursor.execute(
                f"SELECT * FROM {table} WHERE trader=%s ORDER BY {order}", (mind.trader,)
            )
            result[table] = [tuple(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT t.term,t.record_pk FROM pm_terms t JOIN pm_records r ON r.pk=t.record_pk "
            "WHERE r.trader=%s ORDER BY t.term,t.record_pk",
            (mind.trader,),
        )
        result["pm_terms"] = [tuple(row) for row in cursor.fetchall()]
        return result


class CursorProxy:
    def __init__(self, cursor, statements, about_lock=None):
        self.cursor, self.statements, self.about_lock = cursor, statements, about_lock

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "sp_getapplock" in sql and self.about_lock is not None:
            self.about_lock.set()
        return self.cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class ConnectionProxy:
    def __init__(self, database, statements, about_lock=None):
        self.database, self.statements, self.about_lock = database, statements, about_lock
        self.closed, self.committed = False, False

    def cursor(self):
        return CursorProxy(self.database.cursor(), self.statements, self.about_lock)

    def commit(self):
        self.database.commit()
        self.committed = True

    def close(self):
        self.closed = True
        return self.database.close()


def instrument(connect, *, about_lock=None):
    connections, statements = [], []

    def observed():
        database = ConnectionProxy(connect(), statements, about_lock)
        connections.append(database)
        return database

    return observed, connections, statements


def test_frame_equals_legacy_status_plus_active_with_one_existing_locked_transaction(
    mind, monkeypatch
):
    mind.accept("synthetic-source", [{"sequence": 1, "event": event("clock:first")}], NOW)
    before = inventory(mind)
    connect, connections, statements = instrument(mind.connect)
    monkeypatch.setattr(mind, "connect", connect)
    expected = {"status": mind.status(), "qualification": mind.runtime(ACTIVE)}
    assert len(connections) == 2 and len(statements) == 9
    connections.clear()
    statements.clear()
    assert mind.status_frame() == expected
    assert len(connections) == 1 and len(statements) == 7
    assert all(database.closed and database.committed for database in connections)
    assert sum("sp_getapplock" in sql for sql, _ in statements) == 1
    assert "SERIALIZABLE" in statements[0][0]
    assert not any(
        sql.lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}
        for sql, _ in statements
    )
    assert expected["status"]["pending"] == 1 and expected["status"]["dispatches"] == 0
    assert inventory(mind) == before


def test_status_frame_keeps_source_binding_and_missing_active_is_none(mind):
    other = SqlMind(mind.connect, "status-peer-" + uuid4().hex)
    other.initialize(
        "Other identity",
        {"version": "v1", "text": "Other playbook"},
        "2026-10-01T00:00:00Z",
        binding=digest("other-binding"),
    )
    other.accept("peer-source", [{"sequence": 1, "event": event("clock:other")}], NOW)
    assert mind.status_frame()["status"]["pending"] == 0
    assert other.status_frame()["status"]["pending"] == 1
    assert other.status_frame()["qualification"] is None
    assert mind.status_frame()["qualification"]["id"] == "synthetic"
    assert set(mind.status()) == {
        "mode",
        "paused",
        "revision",
        "outcomes",
        "pending",
        "dispatches",
        "reasoning_hold",
    }


def test_status_frame_waits_for_writer_and_returns_one_committed_status_active_view(mind):
    old = mind.status_frame()
    about_lock = Event()
    observed, connections, _statements = instrument(mind.connect, about_lock=about_lock)
    reader = SqlMind(observed, mind.trader)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with mind.transaction() as cursor:
            cursor.execute(
                "UPDATE pm_minds SET paused=0,revision=revision+1 WHERE trader=%s", (mind.trader,)
            )
            mind._set_runtime(
                cursor, ACTIVE, {"id": "new-committed", "state": "warming", "samples": 8}
            )
            future = pool.submit(reader.status_frame)
            # The hook signals before the actual same-resource lock acquisition.
            # No timing/SLO claim: only release the writer once its reader starts.
            assert about_lock.wait(10), "status reader did not request the source lock"
        frame = future.result(timeout=30)
    assert frame == {
        "status": {**old["status"], "paused": False, "revision": old["status"]["revision"] + 1},
        "qualification": {"id": "new-committed", "state": "warming", "samples": 8},
    }
    assert len(connections) == 1 and connections[0].closed


def test_status_frame_failure_releases_transaction_without_state_change(mind, monkeypatch):
    before = inventory(mind)
    read_runtime = mind._runtime
    observed, connections, _statements = instrument(mind.connect)

    def fail_active(cursor, key, default=None):
        if key == ACTIVE:
            raise ShadowError("synthetic_status_active_refusal")
        return read_runtime(cursor, key, default)

    with monkeypatch.context() as patch:
        patch.setattr(mind, "connect", observed)
        patch.setattr(mind, "_runtime", fail_active)
        with pytest.raises(ShadowError, match="^synthetic_status_active_refusal$"):
            mind.status_frame()
    assert len(connections) == 1 and connections[0].closed and not connections[0].committed
    assert inventory(mind) == before
    assert mind.status_frame() == {"status": mind.status(), "qualification": mind.runtime(ACTIVE)}
