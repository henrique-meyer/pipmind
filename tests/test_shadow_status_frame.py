"""Coherent status reads preserve the legacy status and private HTTP contract."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import utc
from pipmind.shadow.common import ShadowError, encoded
from pipmind.shadow.service import create_app
from pipmind.shadow.store import SqlMind

TOKEN = "synthetic-status-frame-" + "s" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


class Cursor:
    description = ("synthetic",)

    def __init__(self, db):
        self.db, self.sql, self.params = db, None, None

    def execute(self, sql, params=()):
        self.sql, self.params = sql, params
        self.db.owner.queries.append((sql, params))
        if self.db.owner.failure and self.db.owner.failure in sql:
            raise RuntimeError("synthetic private driver detail")

    def fetchone(self):
        state = self.db.owner.state
        if "sp_getapplock" in self.sql:
            return (0,)
        if "SELECT lineage,binding,revision" in self.sql:
            return (
                "lineage",
                "binding",
                state["revision"],
                state["paused"],
                utc("2026-10-01T00:00:00Z"),
                encoded({"views": []}),
                "v1",
                None,
            )
        if "COUNT(*) FROM pm_outcomes" in self.sql:
            return (state["outcomes"],)
        if "COUNT(*) FROM pm_inputs" in self.sql:
            return (state["pending"],)
        if "SELECT v FROM pm_runtime" in self.sql:
            value = state.get(self.params[1])
            return (encoded(value),) if value is not None else None
        raise AssertionError(self.sql)


class Database:
    def __init__(self, owner):
        self.owner, self.closed, self.committed = owner, False, False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class Driver:
    def __init__(self):
        self.state = {
            "revision": 42,
            "paused": True,
            "outcomes": 123,
            "pending": 5,
            "reasoning_hold": {"reason": "retained_hold"},
            "qualification:active": {"id": "synthetic", "state": "warming"},
        }
        self.connections, self.queries, self.failure = [], [], None

    def connect(self):
        db = Database(self)
        self.connections.append(db)
        return db


def test_status_frame_reuses_status_queries_with_one_connection_and_existing_lock():
    driver = Driver()
    mind = SqlMind(driver.connect, "synthetic-source")
    expected = {"status": mind.status(), "qualification": mind.runtime("qualification:active")}
    old_connections, old_queries = len(driver.connections), len(driver.queries)
    driver.connections.clear()
    driver.queries.clear()
    before = deepcopy(driver.state)
    assert mind.status_frame() == expected
    assert old_connections == 2 and len(driver.connections) == 1
    assert old_queries == 9 and len(driver.queries) == 7
    assert driver.connections[0].closed and driver.connections[0].committed
    assert driver.state == before
    statements = [sql for sql, _ in driver.queries]
    lock = [sql for sql in statements if "sp_getapplock" in sql]
    assert len(lock) == 1 and "@LockMode='Exclusive'" in lock[0] and "@LockTimeout=5000" in lock[0]
    assert "SERIALIZABLE" in statements[0]
    assert not any(
        sql.lstrip().split()[0] in {"INSERT", "UPDATE", "DELETE", "ALTER", "CREATE"}
        for sql in statements
    )
    for sql, params in driver.queries:
        if sql.startswith("SELECT"):
            assert params[0] == mind.trader


def test_missing_active_value_is_none_and_legacy_status_shape_is_exact():
    driver = Driver()
    del driver.state["qualification:active"]
    mind = SqlMind(driver.connect, "synthetic-source")
    frame = mind.status_frame()
    assert frame["qualification"] is None
    assert frame["status"] == {
        "mode": "shadow",
        "paused": True,
        "revision": 42,
        "outcomes": 123,
        "pending": 5,
        "dispatches": 0,
        "reasoning_hold": {"reason": "retained_hold"},
    }
    assert mind.status() == frame["status"]
    assert "qualification" not in mind.status()


def test_status_frame_failure_closes_without_committing_or_falling_back():
    driver = Driver()
    driver.failure = "SELECT v FROM pm_runtime"
    mind = SqlMind(driver.connect, "synthetic-source")
    before = deepcopy(driver.state)
    with pytest.raises(ShadowError, match="^shadow_store_transaction_failed$"):
        mind.status_frame()
    assert len(driver.connections) == 1
    assert driver.connections[0].closed and not driver.connections[0].committed
    assert driver.state == before


def service_store(*, frame=True, fail=False):
    counts = {"frame": 0, "status": 0, "active": 0}
    status = {
        "mode": "shadow",
        "paused": True,
        "revision": 9,
        "outcomes": 2,
        "pending": 1,
        "dispatches": 0,
        "reasoning_hold": {"reason": "retained_hold"},
    }
    qualification = {"id": "synthetic-window", "state": "warming", "samples": 4}

    def frame_read():
        counts["frame"] += 1
        if fail:
            raise ShadowError("shadow_store_transaction_failed")
        return {"status": deepcopy(status), "qualification": deepcopy(qualification)}

    def status_read():
        counts["status"] += 1
        return deepcopy(status)

    def active_read(key):
        assert key == "qualification:active"
        counts["active"] += 1
        return deepcopy(qualification)

    store = SimpleNamespace(status=status_read, runtime=active_read)
    if frame:
        store.status_frame = frame_read
    return store, counts, status, qualification


@pytest.mark.parametrize("frame", [True, False])
def test_private_status_preserves_payload_and_auth_with_qualification_and_legacy_fallback(frame):
    store, counts, status, qualification = service_store(frame=frame)
    app = create_app(store, object(), TOKEN, gateway=object(), qualification_enabled=True)
    # No lifespan is entered: no worker/recorder/model/network operation starts.
    client = TestClient(app)
    try:
        assert client.get("/status").status_code == 401
        assert counts == {"frame": 0, "status": 0, "active": 0}
        response = client.get("/status", headers=AUTH)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        result = response.json()
        assert result == {
            **status,
            "last_progress": None,
            "last_error": "retained_hold",
            "qualification": qualification,
            "qualification_recorder": {"alive": False, "last_error": None},
            "reasoning_worker": {
                "alive": False,
                "loop_alive": False,
                "supervisor_alive": False,
                "phase": "idle",
                "phase_since": None,
                "event_id": None,
                "error_type": None,
                "pause_error_type": None,
            },
        }
        assert counts == (
            {"frame": 1, "status": 0, "active": 0}
            if frame
            else {"frame": 0, "status": 1, "active": 1}
        )
    finally:
        client.close()


@pytest.mark.parametrize("frame", [True, False])
def test_disabled_qualification_does_not_add_public_fields_or_need_legacy_runtime(frame):
    store, counts, status, _qualification = service_store(frame=frame)
    del store.runtime
    with TestClient(create_app(store, object(), TOKEN, background=False)) as client:
        result = client.get("/status", headers=AUTH).json()
    assert result == {**status, "last_progress": None, "last_error": "retained_hold"}
    assert counts["active"] == 0


def test_frame_failure_is_private_503_and_does_not_fall_back_to_incoherent_reads():
    store, counts, _status, _qualification = service_store(fail=True)
    with TestClient(create_app(store, object(), TOKEN, background=False)) as client:
        response = client.get("/status", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"error": "shadow_store_transaction_failed"}
    assert response.headers["Cache-Control"] == "no-store"
    assert counts == {"frame": 1, "status": 0, "active": 0}


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        {},
        {"status": {}},
        {"qualification": None},
        {"status": None, "qualification": None},
    ],
)
def test_callable_malformed_frame_refuses_without_any_legacy_reads(malformed):
    store, counts, _status, _qualification = service_store()

    def malformed_frame():
        counts["frame"] += 1
        return malformed

    store.status_frame = malformed_frame
    with TestClient(create_app(store, object(), TOKEN, background=False)) as client:
        response = client.get("/status", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"error": "shadow_store_transaction_failed"}
    assert counts == {"frame": 1, "status": 0, "active": 0}
