"""Transactional synthetic SQL doubles; no configured or network connector."""

from copy import deepcopy
from datetime import datetime

import pytest

from pipmind.safety.recovery_sql import (
    RecoveryCommitUnknown,
    RecoveryRefused,
    RecoveryState,
    RecoveryUnavailable,
    RobotReference,
    SqlRecoveryStore,
    bootstrap,
)
from pipmind.safety.session import SessionBinding

BINDING = SessionBinding("synthetic-recovery", "demo", "USD")
ROBOT = RobotReference("robot", "v1", "a" * 64)


class Database:
    def __init__(self):
        self.state, self.plans, self.transitions = {}, {}, {}
        self.connections = []
        self.failure = self.interruption = None
        self.fail_update = self.interrupt_update = self.outage = False

    def saved(self):
        return deepcopy((self.state, self.plans, self.transitions))

    def connect(self):
        if self.outage:
            raise OSError("private database details")
        db = Connection(self)
        self.connections.append(db)
        return db


class Connection:
    def __init__(self, database):
        self.database = database
        self.state, self.plans, self.transitions = database.saved()
        self.autocommit, self.trancount = False, 1
        self.locks, self.closed = set(), False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        d = self.database
        if d.failure == "before":
            raise OSError("private commit details")
        if d.interruption and d.interruption[0] == "before":
            raise d.interruption[1]("private interruption")
        d.state, d.plans, d.transitions = deepcopy((self.state, self.plans, self.transitions))
        if d.failure == "after":
            raise OSError("private acknowledgement details")
        if d.interruption:
            raise d.interruption[1]("private interruption")

    def close(self):
        self.closed = True


class Cursor:
    def __init__(self, connection):
        self.connection, self.result = connection, None
        self.description = ("synthetic",)

    def execute(self, sql, params=()):
        c, q = self.connection, " ".join(sql.split())
        self.result = None
        if q.startswith("SET ") or q.startswith("IF OBJECT_ID"):
            pass
        elif "sp_getapplock" in q:
            c.locks.add(params[0])
            self.result = (0,)
        elif q.startswith("SELECT @@TRANCOUNT"):
            self.result = (c.trancount, "Exclusive" if params[0] in c.locks else "NoLock")
        elif q == "SELECT SYSUTCDATETIME()":
            self.result = (datetime(2026, 10, 21, 12, 0, len(c.transitions)),)
        elif q.startswith("SELECT account_id"):
            self.result = c.state.get(params[-1])
        elif q.startswith("SELECT CASE WHEN EXISTS"):
            self.result = (int(any(k[0] == params[0] for k in (*c.plans, *c.transitions))),)
        elif q.startswith("SELECT TOP(1)"):
            rows = [(row[0], key[1]) for key, row in c.transitions.items() if key[0] == params[0]]
            self.result = max(rows) if rows else None
        elif q.startswith("SELECT generation,CASE"):
            self.result = c.transitions.get((params[-2], params[-1]))
        elif q.startswith("SELECT CASE WHEN DATALENGTH(body)"):
            self.result = c.plans.get((params[-2], params[-1]))
        elif q.startswith("SELECT CASE WHEN DATALENGTH(assignment)"):
            row = next(
                (
                    row
                    for key, row in c.transitions.items()
                    if key[0] == params[-2] and row[0] == params[-1]
                ),
                None,
            )
            self.result = row[1:3] if row else None
        elif q.startswith("INSERT INTO dbo.pm_safety_recovery_accounts"):
            key, *binding = params
            c.state[key] = [*binding, 0, True, None, None]
        elif q.startswith("INSERT INTO dbo.pm_safety_recovery_plans"):
            key, plan_id, *row = params
            c.plans[key, plan_id] = tuple(row)
        elif q.startswith("INSERT INTO dbo.pm_safety_recovery_transitions"):
            key, generation, plan_id, *row = params
            c.transitions[key, plan_id] = (generation, *row)
        elif q.startswith("UPDATE dbo.pm_safety_recovery_accounts"):
            if c.database.fail_update:
                raise OSError("private staged write failure")
            if c.database.interrupt_update:
                raise KeyboardInterrupt("private staged write interruption")
            generation, body, sha, key = params
            c.state[key][3:] = [generation, True, body, sha]
        else:
            raise AssertionError("unrecognized synthetic SQL")
        return self

    def fetchone(self):
        return self.result

    def nextset(self):
        return False


@pytest.fixture
def recovery():
    db = Database()
    store = SqlRecoveryStore(db.connect, BINDING)
    bootstrap(db.connect)
    assert store.initialize() == RecoveryState(BINDING, 0, None, True)
    return db, store


def plan(store, name="first", generation=0, **changes):
    args = dict(
        plan_id=name,
        actor_id="owner",
        expected_generation=generation,
        source_checkpoint_sha256="b" * 64,
        target_robot=ROBOT,
        target_epoch="epoch-one",
        application_incarnation="c" * 64,
        reason="exact owner reason\n",
        position_obligation_ids=("long-one", "short-two"),
    )
    args.update(changes)
    return store.create_plan(**args)


def confirm(db, store, record, **changes):
    connection = db.connect()
    try:
        cur = connection.cursor()
        store.read_in_transaction(cur)
        args = dict(
            epoch="epoch-one", checkpoint_sha256="b" * 64, obligation_ids=("long-one", "short-two")
        )
        args.update(changes)
        receipt = store.confirm_in_transaction(
            cur, record["plan"].plan_id, record["digest"], **args
        )
        connection.commit()
        return receipt
    finally:
        connection.close()


def test_constructor_uninitialized_and_explicit_bootstrap():
    db = Database()
    store = SqlRecoveryStore(db.connect, BINDING)
    assert db.connections == []
    with pytest.raises(RecoveryRefused, match="^recovery_uninitialized$"):
        store.read()
    assert db.saved() == ({}, {}, {})
    assert db.connections[-1].closed


def test_two_transitions_keep_previous_robot_and_open_obligations(recovery):
    db, store = recovery
    first = plan(store)
    receipt = confirm(db, store, first)
    assert receipt["position_obligation_ids"] == ["long-one", "short-two"]
    assert receipt["previous_assignment"] is None
    assert receipt["origin"] == "owner_confirmed_recovery" and receipt["review_excluded"]
    assert store.read().assignment.robot == ROBOT and store.read().held
    other = RobotReference("replacement", "v2", "d" * 64)
    second = plan(store, "second", 1, target_robot=other)
    changed = confirm(db, store, second)
    assert changed["previous_assignment"] == receipt["assignment"]
    assert changed["assignment"]["robot"]["robot_id"] == "replacement"
    assert changed["held"] and store.initialize().generation == 2
    before = db.saved()
    assert confirm(db, store, first) == receipt  # Historical receipt, not new authority.
    assert plan(store) == first
    assert db.saved() == before and len(db.transitions) == 2


def test_full_plan_identity_replay_conflict_and_generation_cas(recovery):
    db, store = recovery
    original = plan(store)
    stale = plan(store, "competing")
    confirm(db, store, original)
    before = db.saved()
    for changes in (
        {"reason": "changed"},
        {"actor_id": "other"},
        {"application_incarnation": "d" * 64},
        {"position_obligation_ids": ("short-two", "long-one")},
    ):
        with pytest.raises(RecoveryRefused, match="^conflicting_recovery_plan$"):
            plan(store, **changes)
    with pytest.raises(RecoveryRefused, match="^stale_assignment_generation$"):
        confirm(db, store, stale)
    with pytest.raises(RecoveryRefused, match="^stale_assignment_generation$"):
        plan(store, "new-stale")
    assert db.saved() == before


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"epoch": "old"}, "recovery_epoch_mismatch"),
        ({"checkpoint_sha256": "e" * 64}, "recovery_checkpoint_mismatch"),
        ({"obligation_ids": ("short-two", "long-one")}, "recovery_obligations_mismatch"),
        ({"obligation_ids": ("long-one",)}, "recovery_obligations_mismatch"),
    ],
)
def test_confirmation_requires_exact_reconciled_facts(recovery, changes, code):
    db, store = recovery
    record, before = plan(store), db.saved()
    before = db.saved()
    with pytest.raises(RecoveryRefused, match="^" + code + "$"):
        confirm(db, store, record, **changes)
    assert db.saved() == before


def test_same_cursor_lock_and_transaction_required(recovery):
    db, store = recovery
    record = plan(store)
    connection = db.connect()
    cur = connection.cursor()
    with pytest.raises(RecoveryRefused, match="^recovery_lock_required$"):
        store.confirm_in_transaction(
            cur,
            "first",
            record["digest"],
            epoch="epoch-one",
            checkpoint_sha256="b" * 64,
            obligation_ids=(),
        )
    connection.trancount = 0
    with pytest.raises(RecoveryRefused, match="^recovery_transaction_required$"):
        store.read_in_transaction(cur)
    connection.close()


@pytest.mark.parametrize(
    "mode,error", [("fail_update", RecoveryUnavailable), ("interrupt_update", KeyboardInterrupt)]
)
def test_staged_assignment_and_journal_rollback_on_failure(recovery, mode, error):
    db, store = recovery
    record = plan(store)
    before = db.saved()
    setattr(db, mode, True)
    with pytest.raises(error):
        confirm(db, store, record)
    assert db.saved() == before and db.connections[-1].closed
    setattr(db, mode, False)
    assert confirm(db, store, record)["assignment"]["generation"] == 1


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_direct_commit_interruption_is_unknown_and_exact_retry_resolves(recovery, phase, error):
    db, store = recovery
    db.interruption = (phase, error)
    with pytest.raises(RecoveryCommitUnknown, match="^recovery_commit_unknown$"):
        plan(store)
    assert db.connections[-1].closed
    db.interruption = None
    saved = plan(store)
    assert saved["plan"].plan_id == "first" and len(db.plans) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_generation": True},
        {"plan_id": "bad\x00"},
        {"target_robot": {"robot_id": "robot"}},
        {"application_incarnation": "C" * 64},
        {"position_obligation_ids": ["one"]},
        {"position_obligation_ids": ("one", "one")},
        {"reason": "x" * 65536},
        {"actor_id": "😀" * 65},
    ],
)
def test_invalid_plan_never_connects(recovery, changes):
    db, store = recovery
    count = len(db.connections)
    with pytest.raises(RecoveryRefused):
        plan(store, **changes)
    assert len(db.connections) == count


def test_currency_pin_outage_and_corruption_never_default(recovery):
    db, store = recovery
    with pytest.raises(RecoveryRefused, match="^recovery_binding_mismatch$"):
        SqlRecoveryStore(db.connect, SessionBinding(BINDING.account_id, "demo", "EUR")).initialize()
    record = plan(store)
    confirm(db, store, record)
    key = next(iter(db.state))
    db.state[key][3] = 0
    with pytest.raises(RecoveryUnavailable, match="^recovery_store_unavailable$"):
        store.read()
    db.outage = True
    with pytest.raises(RecoveryUnavailable, match="^recovery_store_unavailable$"):
        store.read()


def test_changed_plan_or_transition_body_and_unheld_state_are_unavailable(recovery):
    db, store = recovery
    record = plan(store)
    confirm(db, store, record)
    original = db.saved()
    key = next(iter(db.plans))
    db.plans[key] = (db.plans[key][0] + " ", *db.plans[key][1:])
    with pytest.raises(RecoveryUnavailable):
        store.read()
    db.state, db.plans, db.transitions = deepcopy(original)
    key = next(iter(db.transitions))
    row = db.transitions[key]
    db.transitions[key] = (*row[:3], row[3] + " ", row[4])
    with pytest.raises(RecoveryUnavailable):
        store.read()
    db.state, db.plans, db.transitions = deepcopy(original)
    db.state[next(iter(db.state))][4] = False
    with pytest.raises(RecoveryUnavailable):
        store.read()
