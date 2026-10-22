"""Native-bound SQL protocol doubles only; no configured or live connector."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest
from test_safety_authority import APP

from pipmind.safety import command_journal_sql as journal_sql
from pipmind.safety import lease_sql
from pipmind.safety.command_journal import encode_command, encode_order, fingerprint
from pipmind.safety.command_journal_sql import (
    CommandJournalCommitUnknown,
    CommandJournalRefused,
    CommandJournalUnavailable,
    SqlCommandJournal,
    bootstrap,
)
from pipmind.safety.controls_sql import ControlState, SqlControlStore
from pipmind.safety.lease_sql import LeaseBinding, LeaseCommitUnknown, LeaseGrant, SqlLeaseStore
from pipmind.safety.recovery_sql import Assignment, RecoveryState, RobotReference, SqlRecoveryStore
from pipmind.safety.session import SessionBinding

NOW = datetime(2026, 10, 22, tzinfo=UTC)
BINDING = SessionBinding("synthetic-command", "demo", "USD")
TOKEN = "a" * 64


def order(identity="command-1"):
    return {
        "schema_version": 1,
        "client_order_id": identity,
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": Decimal("100.00"),
        "order_type": "market",
        "stop": Decimal("1.09"),
    }


class Database:
    def __init__(self):
        self.now = NOW.replace(tzinfo=None)
        self.native = APP
        self.tables = {name: {} for name in ("accounts", "intents", "attempts", "unknown")}
        self.lease = [
            BINDING.account_id,
            "demo",
            "orders",
            "e" * 64,
            1,
            "owner-1",
            sha256(TOKEN.encode()).hexdigest(),
            self.now + timedelta(seconds=30),
        ]
        self.assignment = Assignment(
            BINDING,
            1,
            "assignment-1",
            RobotReference("robot", "v1", "b" * 64),
            "e" * 64,
            "c" * 64,
            NOW.isoformat(),
        )
        self.control = ControlState(BINDING, 1, False, False)
        self.connections, self.queries = [], []
        self.failure = self.interruption = self.fail_insert = None
        self.outage = False

    def connect(self):
        if self.outage:
            raise OSError("private connection details")
        db = Connection(self)
        self.connections.append(db)
        return db


class Connection:
    def __init__(self, db):
        self.database = db
        self.tables = deepcopy(db.tables)
        self.autocommit = False
        self.closed, self.locks = False, set()
        self.trancount, self.xact_state = 1, 1

    def cursor(self):
        return Cursor(self)

    def commit(self):
        d = self.database
        if d.failure == "before":
            raise OSError("private commit details")
        if d.interruption and d.interruption[0] == "before":
            raise d.interruption[1]("private interruption")
        d.tables = deepcopy(self.tables)
        if d.failure == "after":
            raise OSError("private lost acknowledgement")
        if d.interruption:
            raise d.interruption[1]("private interruption")

    def close(self):
        self.closed = True


class Cursor:
    def __init__(self, connection):
        self.connection, self.result, self.offset = connection, [], 0
        self.description = ("synthetic",)

    def execute(self, query, params=()):
        c, q = self.connection, " ".join(query.split())
        c.database.queries.append(q)
        self.result, self.offset = [], 0
        if q.startswith("SET ") or q.startswith("IF OBJECT_ID"):
            pass
        elif "sp_getapplock" in q:
            c.locks.add(params[0])
            self.result = [(0,)]
        elif q == "SELECT @@TRANCOUNT,XACT_STATE()":
            self.result = [(c.trancount, c.xact_state)]
        elif q.startswith("SELECT APPLOCK_MODE"):
            self.result = [("Exclusive" if params[0] in c.locks else "NoLock",)]
        elif q == "SELECT SYSUTCDATETIME()":
            self.result = [(c.database.now,)]
        elif q.startswith("SELECT account_id,environment,resource,authority_epoch"):
            self.result = [tuple(c.database.lease)]
        elif q.startswith("SELECT account_id,environment,currency"):
            found = c.tables["accounts"].get(params[0])
            self.result = [found] if found is not None else []
        elif q.startswith("SELECT CASE WHEN EXISTS"):
            self.result = [
                (
                    int(
                        any(
                            k[0] == params[0]
                            for name, rows in c.tables.items()
                            if name != "accounts"
                            for k in rows
                        )
                    ),
                )
            ]
        else:
            kind = next((name for name in c.tables if f"pm_safety_command_{name}_v1" in q), None)
            if kind is None:
                raise AssertionError("unrecognized synthetic journal SQL")
            if q.startswith("INSERT"):
                if kind == "accounts":
                    c.tables[kind][params[0]] = tuple(params[1:])
                else:
                    key, identity, body, digest = params
                    c.tables[kind][key, identity] = (body, digest)
                    if c.database.fail_insert == kind:
                        raise OSError("private staged insert failure")
                    if c.database.fail_insert == "interrupt-" + kind:
                        raise KeyboardInterrupt("private staged insert interruption")
            elif q.startswith("SELECT TOP"):
                ids = sorted(
                    key[1]
                    for key in c.tables[kind]
                    if key[0] == params[0] and (len(params) == 1 or key[1] > params[1])
                )
                self.result = [(identity,) for identity in ids[:64]]
            elif q.startswith("SELECT CASE WHEN DATALENGTH(body)"):
                row = c.tables[kind].get((params[-2], params[-1]))
                if row:
                    self.result = [
                        (row[0] if len(row[0].encode("utf-16-le")) <= params[0] else None, row[1])
                    ]
            else:
                raise AssertionError("unrecognized synthetic journal operation")
        return self

    def fetchone(self):
        result = self.fetchmany(1)
        return result[0] if result else None

    def fetchmany(self, count):
        result = self.result[self.offset : self.offset + count]
        self.offset += len(result)
        return result

    def nextset(self):
        return False


@pytest.fixture
def journal(monkeypatch):
    db = Database()
    leases = SqlLeaseStore(
        db.connect, LeaseBinding(BINDING.account_id, "demo", "orders"), "e" * 64, incarnation=APP
    )
    recovery = SqlRecoveryStore(db.connect, BINDING)
    controls = SqlControlStore(db.connect, BINDING)
    store = SqlCommandJournal(db.connect, leases, recovery, controls)
    monkeypatch.setattr(journal_sql, "read_incarnation", lambda cur: db.native)
    monkeypatch.setattr(lease_sql, "read_incarnation", lambda cur: db.native)
    monkeypatch.setattr(SqlControlStore, "read_in_transaction", lambda *args: db.control)
    monkeypatch.setattr(
        SqlRecoveryStore,
        "read_in_transaction",
        lambda *args: RecoveryState(BINDING, db.assignment.generation, db.assignment, True),
    )
    grant = LeaseGrant(
        leases.binding, leases.authority_epoch, "owner-1", 1, TOKEN, db.lease[7].replace(tzinfo=UTC)
    )
    bootstrap(db.connect)
    return db, store, grant


def transact(db, store, grant, action, *args, **kwargs):
    def callback(cur):
        for resource in store._prior_locks[1:]:
            cur.connection.locks.add(resource)
        return action(cur, grant, db.assignment, *args, **kwargs)

    return store.leases.compare_and_act(grant, callback)


def record(db, store, grant, value=None, **kwargs):
    return transact(
        db,
        store,
        grant,
        store.record_in_transaction,
        order() if value is None else value,
        causation_id="causal-source",
        **kwargs,
    )


def test_constructor_does_no_sql_and_requires_native_pin_and_binding():
    db = Database()
    leases = SqlLeaseStore(db.connect, LeaseBinding(BINDING.account_id, "demo", "orders"), "epoch")
    recovery, controls = SqlRecoveryStore(db.connect, BINDING), SqlControlStore(db.connect, BINDING)
    with pytest.raises(CommandJournalRefused, match="^command_journal_native_pin_required$"):
        SqlCommandJournal(db.connect, leases, recovery, controls)
    pinned = SqlLeaseStore(db.connect, leases.binding, "epoch", incarnation=APP)
    SqlCommandJournal(db.connect, pinned, recovery, controls)
    assert not db.connections


def test_immutable_intent_attempt_unknown_and_original_attribution(journal):
    db, store, grant = journal
    first = record(db, store, grant)
    assert first.order_body == encode_order(order()) and first.scope.control_revision == 1
    assert first.scope.lease_expires_at == grant.expires_at.isoformat().replace("+00:00", "Z")
    assert first.dispatch_authorized is False and len(db.connections) == 2
    assert record(db, store, grant) == first
    attempt = transact(db, store, grant, store.attempt_in_transaction, "command-1", "attempt-1")
    assert attempt.command_sha256 == fingerprint(encode_command(first))
    assert (
        transact(db, store, grant, store.attempt_in_transaction, "command-1", "attempt-1")
        == attempt
    )
    unknown = transact(db, store, grant, store.unknown_in_transaction, "command-1")
    projection = store.lookup("command-1")
    assert (
        projection.command == first
        and projection.attempt == attempt
        and projection.unknown == unknown
    )
    assert projection.unresolved and projection.dispatch_authorized is False
    assert list(store.iter_history()) == [projection]
    assert TOKEN not in repr(db.tables)


def test_command_identity_comes_from_frozen_order_not_later_mutable_caller_mapping(
    journal, monkeypatch
):
    db, store, grant = journal
    caller_order = order()
    real_encode = journal_sql.encode_order

    def mutate_after_capture(value):
        captured = real_encode(value)
        value["client_order_id"] = "changed-after-capture"
        return captured

    monkeypatch.setattr(journal_sql, "encode_order", mutate_after_capture)
    result = record(db, store, grant, caller_order)
    assert result.command_id == "command-1"
    assert result.order_body == real_encode(order())
    assert caller_order["client_order_id"] == "changed-after-capture"
    assert store.lookup("command-1").command == result
    assert store.lookup("changed-after-capture") is None


def test_collisions_do_not_rewrite_retained_intent_or_create_second_attempt(journal):
    db, store, grant = journal
    first = record(db, store, grant)
    with pytest.raises(lease_sql.LeaseUnavailable):
        record(db, store, grant, {**order(), "size": Decimal("101")})
    transact(db, store, grant, store.attempt_in_transaction, "command-1", "attempt-1")
    before = deepcopy(db.tables)
    with pytest.raises(lease_sql.LeaseUnavailable):
        transact(db, store, grant, store.attempt_in_transaction, "command-1", "another-attempt")
    assert db.tables == before and store.lookup("command-1").command == first


def test_held_controls_allow_audit_registration_but_never_attempt_marker(journal):
    db, store, grant = journal
    db.control = replace(db.control, revision=2, killed=True, paused=True)
    first = record(db, store, grant)
    assert first.scope.control_revision == 2
    with pytest.raises(lease_sql.LeaseUnavailable):
        transact(db, store, grant, store.attempt_in_transaction, "command-1", "blocked")
    assert store.lookup("command-1").attempt is None
    db.control = replace(db.control, revision=3, killed=False, paused=False)
    transact(db, store, grant, store.attempt_in_transaction, "command-1", "attempt-1")
    db.control = replace(db.control, revision=4, killed=True)
    result = transact(db, store, grant, store.unknown_in_transaction, "command-1")
    assert result.scope.control_revision == 4


@pytest.mark.parametrize(
    "autocommit,trancount,xact_state",
    [(None, 1, 1), (True, 1, 1), (False, 0, 1), (False, 1, 0), (False, 1, True)],
)
def test_unverifiable_transaction_context_refuses_before_write(
    journal, autocommit, trancount, xact_state
):
    db, store, grant = journal
    connection = db.connect()
    connection.autocommit, connection.trancount, connection.xact_state = (
        autocommit,
        trancount,
        xact_state,
    )
    connection.locks.update(store._prior_locks)
    try:
        with pytest.raises(CommandJournalRefused, match="^command_journal_transaction_required$"):
            store.record_in_transaction(
                connection.cursor(), grant, db.assignment, order(), causation_id="cause"
            )
    finally:
        connection.close()
    assert not db.tables["intents"]


def test_missing_earlier_locks_stale_assignment_and_native_change_refuse(journal):
    db, store, grant = journal
    connection = db.connect()
    cur = connection.cursor()
    try:
        with pytest.raises(CommandJournalRefused, match="^command_journal_lock_required$"):
            store.record_in_transaction(cur, grant, db.assignment, order(), causation_id="cause")
        connection.locks.update(store._prior_locks)
        with pytest.raises(CommandJournalRefused, match="^command_journal_assignment_mismatch$"):
            store.record_in_transaction(
                cur,
                grant,
                replace(db.assignment, assignment_id="wrong"),
                order(),
                causation_id="cause",
            )
        db.native = replace(APP, database="restored")
        with pytest.raises(CommandJournalRefused, match="^command_journal_incarnation_mismatch$"):
            store.record_in_transaction(cur, grant, db.assignment, order(), causation_id="cause")
    finally:
        connection.close()
    assert not db.tables["intents"]


@pytest.mark.parametrize(
    "change,code",
    [
        ("native", "command_journal_incarnation_mismatch"),
        ("expiry", "lease_expired"),
        ("control", "command_journal_scope_changed"),
    ],
)
def test_local_post_staging_scope_check_refuses_without_relying_on_outer_callback(
    journal, monkeypatch, change, code
):
    db, store, grant = journal
    connection = db.connect()
    connection.locks.update(store._prior_locks)
    original = store._save

    def staged(*args):
        original(*args)
        if change == "native":
            db.native = replace(APP, database="new-native-after-stage")
        elif change == "expiry":
            db.now = db.lease[7]
        else:
            db.control = replace(db.control, revision=db.control.revision + 1)

    monkeypatch.setattr(store, "_save", staged)
    try:
        with pytest.raises((CommandJournalRefused, lease_sql.LeaseRefused), match="^" + code + "$"):
            store.record_in_transaction(
                connection.cursor(), grant, db.assignment, order(), causation_id="cause"
            )
    finally:
        connection.close()
    assert not db.tables["intents"] and connection.closed


def test_locally_refused_intent_is_audit_only_and_unknown_requires_retained_attempt(journal):
    db, store, grant = journal
    value = record(db, store, grant, local_outcome="refused", code="locally_refused")
    assert value.local_outcome == "refused" and value.dispatch_authorized is False
    with pytest.raises(lease_sql.LeaseUnavailable):
        transact(db, store, grant, store.attempt_in_transaction, "command-1", "forbidden")
    with pytest.raises(lease_sql.LeaseUnavailable):
        transact(db, store, grant, store.unknown_in_transaction, "command-1")
    assert store.lookup("command-1").attempt is None


@pytest.mark.parametrize("failure", ["intents", "interrupt-intents"])
def test_staged_precommit_error_and_interruption_discard_records(journal, failure):
    db, store, grant = journal
    before = deepcopy(db.tables)
    db.fail_insert = failure
    expected = KeyboardInterrupt if failure.startswith("interrupt") else lease_sql.LeaseUnavailable
    with pytest.raises(expected):
        record(db, store, grant)
    assert db.tables == before and db.connections[-1].closed


@pytest.mark.parametrize("phase", ["before", "after"])
def test_outer_commit_unknown_never_returns_a_command_or_automatic_attempt(journal, phase):
    db, store, grant = journal
    db.failure = phase
    with pytest.raises(LeaseCommitUnknown):
        record(db, store, grant)
    db.failure = None
    historical = store.lookup("command-1")
    assert (historical is not None) == (phase == "after")
    if historical:
        assert historical.attempt is None and historical.dispatch_authorized is False
    assert len(db.tables["intents"]) == (1 if phase == "after" else 0)


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_direct_read_commit_interrupt_stays_unknown(journal, error):
    db, store, grant = journal
    record(db, store, grant)
    db.interruption = ("after", error)
    with pytest.raises(CommandJournalCommitUnknown, match="^command_journal_commit_unknown$"):
        store.lookup("command-1")
    assert db.connections[-1].closed


def test_replaced_assignment_preserves_replay_but_cannot_adopt_unattempted_command(journal):
    db, store, grant = journal
    first = record(db, store, grant)
    new_epoch = "f" * 64
    db.assignment = replace(
        db.assignment, generation=2, assignment_id="assignment-2", epoch=new_epoch
    )
    db.lease[3:7] = [new_epoch, 2, "owner-2", sha256(TOKEN.encode()).hexdigest()]
    new_leases = SqlLeaseStore(db.connect, store.leases.binding, new_epoch, incarnation=APP)
    current = SqlCommandJournal(db.connect, new_leases, store.recovery, store.controls)
    new_grant = replace(grant, authority_epoch=new_epoch, generation=2, owner_id="owner-2")
    assert record(db, current, new_grant) == first
    with pytest.raises(lease_sql.LeaseUnavailable):
        transact(db, current, new_grant, current.attempt_in_transaction, "command-1", "adoption")
    assert current.lookup("command-1").attempt is None


def test_later_unknown_observer_keeps_original_command_and_attempt_hashes(journal):
    db, store, grant = journal
    first = record(db, store, grant)
    attempt = transact(db, store, grant, store.attempt_in_transaction, "command-1", "attempt-1")
    new_epoch = "f" * 64
    db.native = replace(APP, database="restored-native")
    db.assignment = replace(
        db.assignment, generation=2, assignment_id="assignment-2", epoch=new_epoch
    )
    db.lease[3:7] = [new_epoch, 2, "owner-2", sha256(TOKEN.encode()).hexdigest()]
    leases = SqlLeaseStore(db.connect, store.leases.binding, new_epoch, incarnation=db.native)
    current = SqlCommandJournal(db.connect, leases, store.recovery, store.controls)
    new_grant = replace(grant, authority_epoch=new_epoch, generation=2, owner_id="owner-2")
    unknown = transact(db, current, new_grant, current.unknown_in_transaction, "command-1")
    result = current.lookup("command-1")
    assert result.command == first and result.attempt == attempt
    assert (
        unknown.scope.assignment == db.assignment
        and unknown.scope.application_incarnation == db.native
    )
    assert result.unresolved and result.dispatch_authorized is False


def test_history_crosses_pages_and_held_lookup_never_requires_current_authority(journal):
    db, store, grant = journal
    for index in range(130):
        record(db, store, grant, order(f"command-{index:03d}"))
    db.control = replace(db.control, killed=True, paused=True)
    db.lease[3] = "stale-for-current-store"
    result = list(store.iter_history())
    assert len(result) == 130 and all(row.attempt is None for row in result)
    assert [row.command.command_id for row in result] == [f"command-{i:03d}" for i in range(130)]
    assert store.lookup("command-000") == result[0]
    pages = [query for query in db.queries if query.startswith("SELECT TOP")]
    assert len(pages) == 3 and all("command_id FROM" in query for query in pages)


def test_corrupt_retained_body_and_binding_do_not_default_or_get_rewritten(journal):
    db, store, grant = journal
    record(db, store, grant)
    key = next(iter(db.tables["intents"]))
    body, digest = db.tables["intents"][key]
    db.tables["intents"][key] = (body + " ", digest)
    with pytest.raises(CommandJournalUnavailable, match="^command_journal_unavailable$"):
        store.lookup("command-1")
    db.tables["intents"][key] = (body, digest)
    account = next(iter(db.tables["accounts"]))
    db.tables["accounts"][account] = (BINDING.account_id, "demo", "EUR")
    with pytest.raises(CommandJournalUnavailable):
        list(store.iter_history())
