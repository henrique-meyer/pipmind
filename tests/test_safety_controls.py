"""Synthetic SQL transactions for trusted controls; no configured connector calls."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime

import pytest

from pipmind.safety.controls_sql import (
    ControlCommitUnknown,
    ControlRefused,
    ControlState,
    ControlUnavailable,
    SqlControlStore,
    bootstrap,
)
from pipmind.safety.session import SessionBinding

BINDING = SessionBinding("synthetic-account", "demo", "USD")


class Database:
    def __init__(self):
        self.state = {}
        self.records = {"overrides": {}, "suggestions": {}, "dispositions": {}}
        self.connections, self.statements = [], []
        self.commit_failure = self.commit_interrupt = self.close_interrupt = None
        self.fail_update = self.interrupt_update = self.outage = False

    def connect(self):
        if self.outage:
            raise OSError("private database details")
        connection = Connection(self)
        self.connections.append(connection)
        return connection

    def saved(self):
        return deepcopy((self.state, self.records))


class Connection:
    def __init__(self, database):
        self.database = database
        self.state, self.records = database.saved()
        self.autocommit = False
        self.trancount = 1
        self.locks = set()
        self.closed = self.committed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        db = self.database
        if db.commit_failure == "before":
            raise OSError("private commit failure")
        if db.commit_interrupt and db.commit_interrupt[0] == "before":
            raise db.commit_interrupt[1]("private commit interrupt")
        db.state, db.records = deepcopy(self.state), deepcopy(self.records)
        self.committed = True
        if db.commit_failure == "after":
            raise OSError("private acknowledgement lost")
        if db.commit_interrupt:
            raise db.commit_interrupt[1]("private commit interrupt")

    def close(self):
        self.closed = True
        if self.database.close_interrupt:
            raise self.database.close_interrupt("private cleanup failure")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ("synthetic",)
        self.result = None

    def execute(self, sql, params=()):
        c, q = self.connection, " ".join(sql.split())
        c.database.statements.append((q, params))
        self.result = None
        if q.startswith("SET ") or q.startswith("IF OBJECT_ID"):
            pass
        elif "sp_getapplock" in q:
            c.locks.add(params[0])
            self.result = (0,)
        elif q.startswith("SELECT @@TRANCOUNT"):
            self.result = (c.trancount, "Exclusive" if params[0] in c.locks else "NoLock")
        elif q == "SELECT SYSUTCDATETIME()":
            self.result = (datetime(2026, 10, 21, 12),)
        elif q.startswith("SELECT account_id,environment,currency,revision"):
            self.result = c.state.get(params[0])
        elif q.startswith("SELECT CASE WHEN EXISTS"):
            self.result = (
                int(any(k[0] == params[0] for table in c.records.values() for k in table)),
            )
        elif q.startswith("INSERT INTO dbo.pm_safety_controls_v1"):
            key, *binding = params
            c.state[key] = [*binding, 0, False, True]
        elif q.startswith("UPDATE dbo.pm_safety_controls_v1"):
            if c.database.fail_update:
                raise OSError("private failure after immutable record insert")
            if c.database.interrupt_update:
                raise KeyboardInterrupt("private interrupt after immutable record insert")
            rev, killed, paused, key = params
            c.state[key][3:] = [rev, killed, paused]
        elif q.startswith("SELECT TOP(1) id,revision"):
            found = [
                (key[1], row[4])
                for key, row in c.records["overrides"].items()
                if key[0] == params[0]
            ]
            self.result = max(found, key=lambda row: row[1]) if found else None
        else:
            kind = next((name for name in c.records if f"pm_safety_control_{name}_v1" in q), None)
            if kind is None:
                raise AssertionError("unrecognized synthetic SQL")
            if q.startswith("SELECT CASE WHEN DATALENGTH(body)"):
                body_cap, receipt_cap, key, record_id = params
                row = c.records[kind].get((key, record_id))
                if row:
                    self.result = (
                        row[0] if len(row[0].encode("utf-16-le")) <= body_cap else None,
                        row[1],
                        row[2] if len(row[2].encode("utf-16-le")) <= receipt_cap else None,
                        row[3],
                    )
                    if kind == "overrides":
                        self.result += (row[4],)
            elif q.startswith("INSERT INTO"):
                key, record_id, *body = params
                c.records[kind][key, record_id] = tuple(body)
            else:
                raise AssertionError("unrecognized synthetic record SQL")
        return self

    def fetchone(self):
        return self.result

    def nextset(self):
        return False


@pytest.fixture
def controls():
    db = Database()
    store = SqlControlStore(db.connect, BINDING)
    bootstrap(db.connect)
    assert store.initialize() == ControlState(BINDING, 0, False, True)
    return db, store


def test_constructor_does_no_sql_and_uninitialized_never_defaults_unpaused():
    db = Database()
    store = SqlControlStore(db.connect, BINDING)
    assert db.connections == []
    with pytest.raises(ControlRefused, match="^controls_uninitialized$"):
        store.read()
    with pytest.raises(ControlRefused, match="^controls_uninitialized$"):
        store.apply_override("resume", "owner", "resume", 0, "explicit")
    assert db.state == {} and all(not table for table in db.records.values())


def test_kill_pause_explicit_lifts_and_initialization_never_reset(controls):
    db, store = controls
    first = store.apply_override("kill", "owner", "kill", 0, "stop")
    assert first["origin"] == "trusted_owner_override" and first["review_excluded"] is True
    assert store.read() == ControlState(BINDING, 1, True, True)
    assert store.initialize() == store.read()
    store.apply_override("lift-kill", "owner", "unkill", 1, "explicit unkill")
    assert store.read() == ControlState(BINDING, 2, False, True)
    store.apply_override("lift-pause", "owner", "resume", 2, "explicit resume")
    assert store.read() == ControlState(BINDING, 3, False, False)
    store.apply_override("pause", "owner", "pause", 3, "pause")
    assert store.read() == ControlState(BINDING, 4, False, True)
    assert len(db.records["overrides"]) == 4


def test_resume_does_not_clear_kill_and_flatten_never_changes_flags(controls):
    _, store = controls
    store.apply_override("kill", "owner", "kill", 0, "stop")
    store.apply_override("resume", "owner", "resume", 1, "resume only")
    assert store.read() == ControlState(BINDING, 2, True, False)
    flat = store.apply_override("flatten", "owner", "flatten", 2, "flatten requested")
    assert flat["outcome"] == "unsupported_not_executed"
    assert flat["review_excluded"] and store.read() == ControlState(BINDING, 3, True, False)


def test_exact_retry_precedes_cas_conflict_and_stale_commands_have_no_writes(controls):
    db, store = controls
    receipt = store.apply_override("one", "owner", "kill", 0, "  exact reason\n")
    store.apply_override("two", "owner", "unkill", 1, "lift")
    before = db.saved()
    assert store.apply_override("one", "owner", "kill", 0, "  exact reason\n") == receipt
    assert store.lookup_override("one") == receipt
    with pytest.raises(ControlRefused, match="^conflicting_control_command$"):
        store.apply_override("one", "owner", "kill", 0, "changed")
    with pytest.raises(ControlRefused, match="^stale_control_revision$"):
        store.apply_override("stale", "owner", "resume", 0, "stale")
    assert db.saved() == before


def test_suggestions_and_dispositions_are_separate_immutable_and_not_controls(controls):
    db, store = controls
    original = store.read()
    suggestion = store.submit_suggestion("idea", "owner", "Keep exact\nidea text")
    assert suggestion["origin"] == "owner_suggestion" and not suggestion["review_excluded"]
    assert store.submit_suggestion("idea", "owner", "Keep exact\nidea text") == suggestion
    with pytest.raises(ControlRefused, match="^conflicting_suggestion$"):
        store.submit_suggestion("idea", "owner", "changed")
    decision = store.resolve_suggestion("idea", "reviewer", "trial", "reasoned trial")
    assert decision["outcome"] == "recorded" and decision["origin"] == "suggestion_disposition"
    assert store.resolve_suggestion("idea", "reviewer", "trial", "reasoned trial") == decision
    with pytest.raises(ControlRefused, match="^conflicting_suggestion_disposition$"):
        store.resolve_suggestion("idea", "reviewer", "adopt", "different")
    with pytest.raises(ControlRefused, match="^suggestion_not_found$"):
        store.resolve_suggestion("unknown", "reviewer", "reject", "missing")
    assert store.read() == original
    assert len(db.records["suggestions"]) == len(db.records["dispositions"]) == 1
    assert not db.records["overrides"]


def test_same_cursor_read_locks_and_lookup_requires_that_lock(controls):
    db, store = controls
    receipt = store.apply_override("one", "owner", "kill", 0, "stop")
    connection = db.connect()
    cur = connection.cursor()
    n = len(db.connections)
    with pytest.raises(ControlRefused, match="^control_lock_required$"):
        store.lookup_override_in_transaction(cur, "one")
    assert store.read_in_transaction(cur).killed
    assert store.lookup_override_in_transaction(cur, "one") == receipt
    assert len(db.connections) == n and not connection.committed and not connection.closed
    assert connection.locks == {store._resource}
    connection.close()


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("interrupt", [None, KeyboardInterrupt, SystemExit])
def test_commit_uncertainty_never_returns_receipt_or_replays(controls, phase, interrupt):
    db, store = controls
    if interrupt:
        db.commit_interrupt, db.close_interrupt = (phase, interrupt), interrupt
    else:
        db.commit_failure = phase
    calls = len(db.connections)
    with pytest.raises(ControlCommitUnknown, match="^control_commit_unknown$"):
        store.apply_override("kill", "owner", "kill", 0, "stop")
    assert len(db.connections) == calls + 1 and db.connections[-1].closed
    assert len(db.records["overrides"]) == (phase == "after")
    assert next(iter(db.state.values()))[3:] == (
        [1, True, True] if phase == "after" else [0, False, True]
    )


@pytest.mark.parametrize("interrupt", [False, True])
def test_staged_record_failure_rolls_back_receipt_and_state(controls, interrupt):
    db, store = controls
    before = db.saved()
    db.interrupt_update, db.fail_update = interrupt, not interrupt
    with pytest.raises(KeyboardInterrupt if interrupt else ControlUnavailable):
        store.apply_override("kill", "owner", "kill", 0, "stop")
    assert db.saved() == before and db.connections[-1].closed


def test_corrupt_or_orphan_state_never_recovers_to_defaults(controls):
    db, store = controls
    store.apply_override("kill", "owner", "kill", 0, "stop")
    saved = db.saved()
    db.state.clear()
    with pytest.raises(ControlUnavailable):
        store.initialize()
    db.state, db.records = saved
    row = list(next(iter(db.records["overrides"].values())))
    row[3] = "0" * 64
    db.records["overrides"][store._key, "kill"] = tuple(row)
    with pytest.raises(ControlUnavailable):
        store.lookup_override("kill")


def test_currency_pin_and_environment_separation(controls):
    db, store = controls
    wrong = SqlControlStore(db.connect, replace(BINDING, currency="EUR"))
    with pytest.raises(ControlRefused, match="^control_binding_mismatch$"):
        wrong.initialize()
    other = SqlControlStore(db.connect, replace(BINDING, environment="live"))
    assert other.initialize().paused
    assert len(db.state) == 2 and store.read().paused


@pytest.mark.parametrize("change", ["revision", "flags", "latest_missing", "index"])
def test_effective_state_must_match_latest_indexed_immutable_override(controls, change):
    db, store = controls
    store.apply_override("kill", "owner", "kill", 0, "stop")
    if change == "revision":
        db.state[store._key][3] = 0
    elif change == "flags":
        db.state[store._key][4] = False
    elif change == "latest_missing":
        db.records["overrides"].clear()
    else:
        row = list(db.records["overrides"][store._key, "kill"])
        row[4] = 2
        db.records["overrides"][store._key, "kill"] = tuple(row)
    with pytest.raises(ControlUnavailable, match="^control_store_unavailable$"):
        store.read()


@pytest.mark.parametrize("killed,paused", [(False, False), (True, True), (True, False)])
def test_revision_zero_without_override_requires_exact_initialized_flags(controls, killed, paused):
    db, store = controls
    db.state[store._key][4:] = [killed, paused]
    before = db.saved()
    with pytest.raises(ControlUnavailable, match="^control_store_unavailable$"):
        store.read()
    with pytest.raises(ControlUnavailable, match="^control_store_unavailable$"):
        store.initialize()
    assert db.saved() == before


@pytest.mark.parametrize("identifier", ["", " x", "x ", "x\x00", "\ud800", "😀" * 65, True])
def test_invalid_ids_refuse_before_storage(controls, identifier):
    db, store = controls
    calls = len(db.connections)
    with pytest.raises(ControlRefused, match="^invalid_control_id$"):
        store.apply_override(identifier, "owner", "pause", 0, "reason")
    assert len(db.connections) == calls


def test_oversize_invalid_actions_and_sql_loss_are_fixed_failures(controls):
    db, store = controls
    for action in ("raise_risk", "execute", [], True):
        with pytest.raises(ControlRefused):
            store.apply_override("bad", "owner", action, 0, "reason")
    with pytest.raises(ControlRefused):
        store.submit_suggestion("big", "owner", "x" * 65536)
    db.outage = True
    with pytest.raises(ControlUnavailable, match="^control_store_unavailable$"):
        store.read()
