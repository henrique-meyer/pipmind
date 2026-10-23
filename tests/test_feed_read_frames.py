"""Feed observation keeps a coherent committed frame without writer mutations."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import stamp, utc
from pipmind.feeds.common import FeedError, identity
from pipmind.feeds.fixtures import pulse
from pipmind.feeds.service import create_app
from pipmind.feeds.sql_store import SqlFeedStore
from pipmind.feeds.store import FeedStore

NOW = utc("2026-10-23T14:00:00Z")
TOKEN = "synthetic-feed-frame-" + "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def store(tmp_path):
    return FeedStore(tmp_path / "feeds.sqlite3")


def test_sqlite_frame_matches_existing_status_with_one_transaction_and_no_writes(
    store, monkeypatch
):
    store.ingest("news", "capture", {"raw": "private-canary"}, "fingerprint", NOW)
    store.quarantine("capture")
    with store.transaction() as db:
        store.emit(db, "news", pulse("news", NOW), NOW)
    store.alert("news", "synthetic_failure", NOW)
    store.save("worker:news", {"heartbeat": stamp(NOW), "error": "known_failure"})
    store.save("unrelated:cursor", {"offset": 123})
    expected = {
        "source_id": store.source_id(),
        **store.status(),
        "worker_heartbeats": {
            name: store.state("worker:" + name) for name in ("macro", "news", "social")
        },
    }
    before = open(store.path, "rb").read()
    original, transactions, statements = store.transaction, [], []

    @contextmanager
    def observed():
        transactions.append(True)
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "transaction", observed)
    assert store.status_frame() == expected
    assert len(transactions) == 1
    assert not any(
        statement.lstrip().split()[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER"}
        for statement in statements
    )
    assert open(store.path, "rb").read() == before
    assert "private-canary" not in str(expected)


def test_status_endpoint_uses_only_frame_and_preserves_schema_and_supervisor(store, monkeypatch):
    frame = store.status_frame()
    calls = []

    def frame_once():
        calls.append(True)
        return frame

    def forbidden(*args, **kwargs):
        raise AssertionError("separate store read")

    monkeypatch.setattr(store, "status_frame", frame_once)
    for name in ("source_id", "status", "state"):
        monkeypatch.setattr(store, name, forbidden)
    workers = {"news": {"alive": True}}
    supervisor = SimpleNamespace(status=lambda: workers)
    # Calling private_status directly avoids starting a synthetic supervisor.
    app = create_app(store, TOKEN, supervisor=supervisor)
    assert app.state.private_status() == {**frame, "workers": workers}
    assert calls == [True]
    with TestClient(create_app(store, TOKEN)) as session:
        response = session.get("/status", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {**frame, "workers": {}}
    assert response.headers["Cache-Control"] == "no-store"


def test_status_auth_precedes_frame_and_unavailable_is_never_cached(store, monkeypatch):
    calls = []

    def unavailable():
        calls.append(True)
        raise FeedError("feed_store_unavailable")

    monkeypatch.setattr(store, "status_frame", unavailable)
    with TestClient(create_app(store, TOKEN)) as session:
        for auth in ({}, {"Authorization": "Bearer wrong"}):
            response = session.get("/status", headers=auth)
            assert response.status_code == 401
        assert calls == []
        for _ in range(2):
            response = session.get("/status", headers=AUTH)
            assert response.status_code == 503
            assert response.json() == {"error": "feed_store_unavailable"}
            assert response.headers["Cache-Control"] == "no-store"
    assert len(calls) == 2


def test_frame_alert_bound_and_missing_or_failed_heartbeats_are_preserved(store):
    with store.transaction() as db:
        db.executemany(
            "INSERT INTO alerts(producer,code,ts) VALUES(?,?,?)",
            [("news", f"failure-{number}", stamp(NOW)) for number in range(105)],
        )
    store.save("worker:news", {"heartbeat": stamp(NOW), "error": "feed_worker_step_failed"})
    frame = store.status_frame()
    assert len(frame["alerts"]) == 100
    assert frame["alerts"][0]["code"] == "failure-104"
    assert frame["worker_heartbeats"] == {
        "macro": None,
        "news": {"heartbeat": stamp(NOW), "error": "feed_worker_step_failed"},
        "social": None,
    }


@pytest.mark.parametrize("method", ["status_frame", "handoff"])
def test_sqlite_frame_refuses_stale_profile_without_rewriting_state(store, method):
    with store.transaction() as db:
        db.execute("UPDATE metadata SET value=? WHERE key='profile'", (identity("changed"),))
    before = open(store.path, "rb").read()
    with pytest.raises(FeedError, match="^feed_store_profile_changed$"):
        getattr(store, method)()
    assert open(store.path, "rb").read() == before


class Cursor:
    description = ("synthetic",)

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        self.connection.statements.append((sql, params))
        self.sql = sql

    def fetchone(self):
        if "snapshot_isolation_state" in self.sql:
            return (self.connection.snapshot,)
        if "sp_getapplock" in self.sql:
            return (self.connection.lock_result,)
        if "pf_state" in self.sql:
            return None
        if "pf_meta" in self.sql:
            return (self.connection.profile,)
        raise AssertionError(self.sql)


class Connection:
    def __init__(self, snapshot=1, profile=None, lock_result=0):
        self.snapshot, self.profile, self.lock_result = snapshot, profile, lock_result
        self.statements, self.committed, self.closed = [], False, False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def sql_store(connect, profile=None):
    store = object.__new__(SqlFeedStore)
    store.connect, store.profile = connect, profile or identity("synthetic")
    return store


@pytest.mark.parametrize("snapshot", [0, 1])
def test_read_frame_uses_enabled_snapshot_or_original_disabled_lock(snapshot):
    connections, kwargs = [], []
    profile = identity("synthetic")

    def connect(**options):
        kwargs.append(options)
        db = Connection(snapshot=snapshot, profile=profile)
        connections.append(db)
        return db

    with sql_store(connect).read_frame() as cursor:
        assert isinstance(cursor, Cursor)
    assert len(connections) == 2
    assert all(db.closed for db in connections)
    assert connections[1].committed
    statements = [sql for db in connections for sql, _ in db.statements]
    assert not any("ALTER DATABASE" in sql for sql in statements)
    if snapshot:
        from pytds.extensions import ISOLATION_LEVEL_SNAPSHOT

        assert kwargs == [{}, {"isolation_level": ISOLATION_LEVEL_SNAPSHOT}]
        assert not any("sp_getapplock" in sql for sql in statements)
    else:
        assert kwargs == [{}, {}]
        locked = [sql for sql in statements if "sp_getapplock" in sql]
        assert len(locked) == 1 and "@LockTimeout=5000" in locked[0]
        assert "@LockMode='Exclusive'" in locked[0]


def test_failed_snapshot_never_retries_at_another_isolation():
    capability = Connection()
    calls = []

    def connect(**options):
        calls.append(options)
        if options:
            raise RuntimeError("synthetic snapshot unavailable")
        return capability

    with pytest.raises(FeedError, match="^feed_store_unavailable$"):
        with sql_store(connect).read_frame():
            pytest.fail("failed snapshot yielded a frame")
    assert len(calls) == 2 and calls[1].get("isolation_level") is not None
    assert capability.closed


@pytest.mark.parametrize("snapshot", [0, 1])
def test_sql_read_frame_refuses_stale_profile_and_closes_sessions(snapshot):
    connections = []

    def connect(**options):
        db = Connection(snapshot=snapshot, profile=identity("changed"))
        connections.append(db)
        return db

    with pytest.raises(FeedError, match="^feed_store_profile_changed$"):
        with sql_store(connect).read_frame():
            pytest.fail("stale profile yielded a frame")
    assert all(db.closed for db in connections)
    assert not connections[-1].committed


def test_disabled_snapshot_lock_failure_remains_a_refusal():
    connections = []

    def connect(**options):
        db = Connection(snapshot=0, lock_result=-1)
        connections.append(db)
        return db

    with pytest.raises(FeedError, match="^feed_store_unavailable$"):
        with sql_store(connect).read_frame():
            pytest.fail("busy writer lock yielded a frame")
    assert all(db.closed for db in connections)
    assert not connections[-1].committed


@pytest.mark.parametrize("after,limit", [(-1, 100), (True, 100), (0, 0), (0, 501), (0, True)])
def test_handoff_preserves_invalid_page_refusal_before_connect(after, limit):
    def forbidden(**kwargs):
        pytest.fail("invalid page opened SQL")

    with pytest.raises(FeedError, match="^invalid_event_page$"):
        sql_store(forbidden).handoff(after, limit)
