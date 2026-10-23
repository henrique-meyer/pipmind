"""Actual disposable SQL coverage for recorder error retention and lock refusal."""

import os
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import pytest

from pipmind.contracts import utc
from pipmind.shadow import qualification as q
from pipmind.shadow.common import ShadowError, digest
from pipmind.shadow.sql import connector, migrate, transaction
from pipmind.shadow.store import SqlMind

pytestmark = pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_TESTS") != "true", reason="separate SQL integration gate"
)
MOMENT = utc("2026-10-20T12:00:00Z")


@pytest.fixture
def recorder(monkeypatch):
    connect = connector()
    migrate(connect)
    store = SqlMind(connect, "diagnostic-" + uuid4().hex)
    store.initialize(
        "Synthetic recorder identity\n",
        {"version": "v1", "text": "Synthetic no-trading playbook\n"},
        "2026-10-01T00:00:00Z",
        binding=digest("synthetic-recorder-binding"),
    )
    q.arm(store, "sql-diagnostics", ["EUR/USD"], "b" * 40, MOMENT)
    monkeypatch.setenv("BUILD_SHA", "b" * 40)
    monkeypatch.setattr(q, "read_gateway", lambda *args: {})
    # This is diagnostic SQL validation, not broker/quality/day validation.
    monkeypatch.setattr(q, "healthy", lambda *args: ["startup_backlog"])
    return store, q.Recorder(store, None, lambda: {})


def test_busy_source_lock_retains_diagnostic_without_sample_then_next_capture_persists(recorder):
    store, capture = recorder
    initial_head = store.head()
    initial_active = store.runtime(q.ACTIVE)
    sample_key = "qualification:sql-diagnostics:sample:" + str(int(MOMENT.timestamp()) // 60)
    with transaction(store.connect, "shadow:" + store.trader):
        with pytest.raises(ShadowError, match="^shadow_store_busy$"):
            capture.capture(MOMENT)
    # Lock refusal is from the FIRST active runtime read, not a fabricated INSERT failure.
    failure = deepcopy(capture._diagnostics[0])
    assert failure["stage"] == "active_read" and failure["code"] == "shadow_store_busy"
    assert store.head() == initial_head and store.runtime(q.ACTIVE) == initial_active
    assert store.runtime(sample_key) is None and store.status()["outcomes"] == 0
    capture.capture(MOMENT)
    retained = store.runtime(sample_key)
    assert retained["capture_diagnostics"]["records"] == [failure]
    assert retained["errors"] == [] and retained["readiness"] == ["startup_backlog"]
    assert store.runtime(q.ACTIVE)["samples"] == 1 and capture._diagnostics == []
    assert store.head() == initial_head and store.status()["outcomes"] == 0
    capture.capture(MOMENT)
    assert store.runtime(sample_key) == retained and store.runtime(q.ACTIVE)["samples"] == 1


@pytest.mark.parametrize("snapshot", [False, True])
def test_active_hint_preserves_target_and_committed_state_during_source_writer_lock(snapshot):
    """The enabled read sees committed history; disabled reads keep lock refusal."""
    admin = connector()()
    admin.autocommit = True
    control = admin.cursor()
    name = "recorder_hint_" + uuid4().hex
    control.execute(f"CREATE DATABASE [{name}]")
    try:
        if snapshot:
            control.execute(f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
        migrate(connect)
        stores = [SqlMind(connect, key) for key in ("target", "other-target")]
        for index, store in enumerate(stores):
            store.initialize(
                "Synthetic exact identity",
                {"version": "v1", "text": "Synthetic exact playbook"},
                "2026-10-01T00:00:00Z",
                binding=digest("target-binding-" + str(index)),
            )
            q.arm(store, "committed-" + str(index), ["EUR/USD"], "b" * 40, MOMENT)
        store, other = stores
        before = store.runtime(q.ACTIVE)
        before_head, before_records = store.retained()
        with store.transaction() as cursor:
            uncommitted = {**before, "id": "uncommitted", "samples": 9}
            store._set_runtime(cursor, q.ACTIVE, uncommitted)
            cursor.execute(
                "UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (store.trader,)
            )
            # This connection owns the exclusive app lock until this block exits.
            # No elapsed-time assertion or second writer/controller is needed.
            if snapshot:
                assert q._active_spec(store) == before
                assert q._active_spec(other)["id"] == "committed-1"
            else:
                with pytest.raises(ShadowError, match="^shadow_store_busy$"):
                    q._active_spec(store)
        assert q._active_spec(store) == uncommitted
        after_head, after_records = store.retained()
        assert after_head["revision"] == before_head["revision"] + 1
        assert after_records == before_records
        with pytest.raises(ShadowError, match="^mind_not_initialized$"):
            q._active_spec(SqlMind(connect, "unknown-target"))
        assert store.status()["outcomes"] == 0
    finally:
        control.execute(f"DROP DATABASE [{name}]")
        admin.close()


@pytest.fixture
def snapshot_recorder(monkeypatch):
    """Own the snapshot capability locally; never change the shared test database."""
    admin = connector()()
    admin.autocommit = True
    control = admin.cursor()
    name = "recorder_race_" + uuid4().hex
    control.execute(f"CREATE DATABASE [{name}]")
    try:
        control.execute(f"ALTER DATABASE [{name}] SET ALLOW_SNAPSHOT_ISOLATION ON")
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
        migrate(connect)
        db = connect()
        try:
            cursor = db.cursor()
            cursor.execute(
                "SELECT snapshot_isolation_state FROM sys.databases WHERE database_id=DB_ID()"
            )
            enabled = cursor.fetchone()[0]
        finally:
            db.close()
        assert enabled == 1
        # Existing native SqlMind target checks are not M5 authority infrastructure.
        store = SqlMind(connect, "snapshot-race-target")
        store.initialize(
            "Synthetic recorder identity\n",
            {"version": "v1", "text": "Synthetic no-trading playbook\n"},
            "2026-10-01T00:00:00Z",
            binding=digest("synthetic-recorder-binding"),
        )
        q.arm(store, "sql-diagnostics", ["EUR/USD"], "b" * 40, MOMENT)
        monkeypatch.setenv("BUILD_SHA", "b" * 40)
        monkeypatch.setattr(q, "read_gateway", lambda *args: {})
        monkeypatch.setattr(q, "healthy", lambda *args: ["startup_backlog"])
        yield store, q.Recorder(store, None, lambda: {})
    finally:
        control.execute(f"DROP DATABASE [{name}]")
        admin.close()


@pytest.mark.parametrize("replacement", [False, True])
def test_snapshot_active_hint_cannot_commit_after_concurrent_close_or_arm(
    snapshot_recorder, monkeypatch, replacement
):
    store, capture = snapshot_recorder
    original = store.runtime(q.ACTIVE)
    original_head, original_records = store.retained()
    before_read = q._active_spec
    read_frame = q.stream_transaction
    seen, modes = [], []

    @contextmanager
    def retain_read_mode(*args, **kwargs):
        with read_frame(*args, **kwargs) as (cursor, mode):
            modes.append(mode)
            yield cursor, mode

    monkeypatch.setattr(q, "stream_transaction", retain_read_mode)

    def retain_hint(target):
        hint = before_read(target)
        seen.append(hint)
        return hint

    def close_or_replace():
        with store.transaction() as cursor:
            closed = store._runtime(cursor, q.ACTIVE)
            closed["state"] = "warmup_failed"
            store._set_runtime(cursor, q.ACTIVE, closed)
        if replacement:
            q.arm(store, "new-sql-run", ["EUR/USD"], "b" * 40, MOMENT)
        return {}

    monkeypatch.setattr(q, "_active_spec", retain_hint)
    capture.state = close_or_replace
    capture.capture(MOMENT)
    assert seen == [original] and modes == ["snapshot"]
    current = store.runtime(q.ACTIVE)
    assert current["samples"] == 0
    assert current["id"] == ("new-sql-run" if replacement else original["id"])
    assert current["state"] == ("warming" if replacement else "warmup_failed")
    with store.transaction() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM pm_runtime WHERE trader=%s AND k LIKE %s",
            (store.trader, "qualification:%:sample:%"),
        )
        sample_count = cursor.fetchone()[0]
    assert sample_count == 0
    assert store.retained() == (original_head, original_records)
    assert store.runtime("qualification:sql-diagnostics:request") == original
