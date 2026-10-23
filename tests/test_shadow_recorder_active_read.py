"""Only the recorder's initial hint uses the existing consistent read frame."""

import asyncio
from copy import deepcopy

import pytest

from pipmind.shadow import qualification as q
from pipmind.shadow.common import ShadowError


class Session:
    def __init__(self, enabled):
        self.enabled = enabled
        self.statements = []
        self.closed = self.committed = False
        self.description = ("result",)

    def cursor(self):
        return self

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))

    def fetchone(self):
        return (int(self.enabled) if "snapshot_isolation_state" in self.statements[-1][0] else 0,)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class Store:
    trader = "target-only"

    def __init__(self, enabled=True, failure=None):
        self.sessions, self.connections, self.reads = [], [], []
        self.enabled, self.failure = enabled, failure
        self.value = {"id": "committed-hint", "state": "warming"}

    def connect(self, **options):
        self.connections.append(options)
        if options and self.failure == "snapshot_connect":
            raise ShadowError("sql_unavailable")
        db = Session(self.enabled)
        self.sessions.append(db)
        return db

    def runtime(self, key):
        raise AssertionError("SQL reader must not fall back to coordinated runtime")

    def _head(self, cursor):
        self.reads.append(("head", cursor))
        if isinstance(self.failure, BaseException):
            raise self.failure
        if self.failure == "target":
            raise ShadowError("mind_not_initialized")
        if self.failure == "native":
            raise ValueError("unsupported native row")
        return {"lineage": "native-target"}

    def _runtime(self, cursor, key):
        self.reads.append((key, cursor))
        return deepcopy(self.value)


@pytest.mark.parametrize("enabled", [False, True])
def test_active_hint_checks_target_and_reads_once_inside_existing_frame(enabled):
    store = Store(enabled)
    value = q._active_spec(store)
    assert value == store.value and value is not store.value
    assert [key for key, _ in store.reads] == ["head", q.ACTIVE]
    assert store.reads[0][1] is store.reads[1][1] is store.sessions[-1]
    assert all(db.closed for db in store.sessions)
    assert store.sessions[-1].committed
    statements = [sql for db in store.sessions for sql, _ in db.statements]
    assert any("sp_getapplock" in sql for sql in statements) is (not enabled)
    assert bool(store.connections[-1]) is enabled
    assert not any("ALTER DATABASE" in sql for sql in statements)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("snapshot_connect", "sql_unavailable"),
        ("target", "mind_not_initialized"),
        ("native", "shadow_snapshot_transaction_failed"),
    ],
)
def test_snapshot_failure_never_retries_with_legacy_runtime_or_lock(failure, expected):
    store = Store(failure=failure)
    with pytest.raises(ShadowError, match="^" + expected + "$"):
        q._active_spec(store)
    assert len(store.connections) == 2
    assert all(db.closed and not db.committed for db in store.sessions)
    assert not any("sp_getapplock" in sql for db in store.sessions for sql, _ in db.statements)
    assert not any(key == q.ACTIVE for key, _ in store.reads)


def test_cancellation_disposes_snapshot_and_propagates_without_fallback():
    cancelled = asyncio.CancelledError()
    store = Store(failure=cancelled)
    with pytest.raises(asyncio.CancelledError) as caught:
        q._active_spec(store)
    assert caught.value is cancelled
    assert all(db.closed and not db.committed for db in store.sessions)
    assert len(store.connections) == 2


def test_plain_store_contract_preserves_legacy_read():
    calls = []

    class Legacy:
        def runtime(self, key):
            calls.append(key)
            return {"id": "unchanged"}

    assert q._active_spec(Legacy()) == {"id": "unchanged"}
    assert calls == [q.ACTIVE]


def test_invalid_connect_attribute_is_not_a_legacy_store():
    store = Store()
    store.connect = None
    with pytest.raises(ShadowError, match="^shadow_snapshot_transaction_failed$"):
        q._active_spec(store)
    assert store.reads == []


def test_active_read_failure_keeps_recorder_diagnostic_stage(monkeypatch):
    store = Store(failure="target")
    recorder = q.Recorder(store, None, lambda: pytest.fail("state must not run"))
    with pytest.raises(ShadowError, match="mind_not_initialized"):
        recorder.capture()
    assert len(recorder._diagnostics) == 1
    row = recorder._diagnostics[0]
    assert row["stage"] == "active_read" and row["run"] is None and row["slot"] is None
    assert row["outcome"] == "capture_failed"


@pytest.mark.parametrize("state", [None, {"state": "captured_pending_review"}])
def test_inactive_hint_does_not_enter_capture_writer(monkeypatch, state):
    store = Store()
    store.value = state
    recorder = q.Recorder(store, None, lambda: pytest.fail("inactive capture must return"))
    recorder.capture()
    assert len(store.connections) == 2 and recorder._diagnostics == []


@pytest.mark.parametrize("replace", [False, True])
def test_stale_hint_cannot_write_after_concurrent_close_or_new_arm(monkeypatch, replace):
    from test_shadow_recorder_diagnostics import MOMENT
    from test_shadow_recorder_diagnostics import Store as MutableStore

    store = MutableStore()
    q.arm(store, "original", ["EUR/USD"], "a" * 40, MOMENT)
    hint = deepcopy(store.rows[q.ACTIVE])
    original_request = deepcopy(store.rows["qualification:original:request"])
    monkeypatch.setattr(q, "_active_spec", lambda target: deepcopy(hint))
    monkeypatch.setattr(q, "read_gateway", lambda *args: {})
    monkeypatch.setattr(q, "healthy", lambda *args: ["startup_backlog"])

    def close_between_read_and_write():
        with store.transaction() as cursor:
            active = store._runtime(cursor, q.ACTIVE)
            active["state"] = "warmup_failed"
            store._set_runtime(cursor, q.ACTIVE, active)
        if replace:
            q.arm(store, "replacement", ["EUR/USD"], "a" * 40, MOMENT)
        return {}

    q.Recorder(store, None, close_between_read_and_write).capture(MOMENT)
    active = store.rows[q.ACTIVE]
    assert active["samples"] == 0
    assert active["id"] == ("replacement" if replace else "original")
    assert active["state"] == ("warming" if replace else "warmup_failed")
    assert store.rows["qualification:original:request"] == original_request
    assert not any(":sample:" in key for key in store.rows)
    assert not any(call[0] == "execute" for call in store.calls)


@pytest.mark.parametrize("acknowledged", [False, True])
def test_snapshot_read_commit_failure_never_retries_or_returns_unconfirmed_hint(acknowledged):
    store = Store()
    connect = store.connect

    def failing_connection(**options):
        db = connect(**options)
        if options:

            def commit():
                db.committed = acknowledged
                raise RuntimeError("synthetic lost read commit acknowledgement")

            db.commit = commit
        return db

    store.connect = failing_connection
    with pytest.raises(ShadowError, match="^shadow_snapshot_transaction_failed$"):
        q._active_spec(store)
    assert len(store.connections) == 2
    assert all(db.closed for db in store.sessions)
    assert [key for key, _ in store.reads] == ["head", q.ACTIVE]
    assert not any("sp_getapplock" in sql for db in store.sessions for sql, _ in db.statements)
