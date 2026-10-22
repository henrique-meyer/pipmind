"""Closed Azure journal scopes; synthetic transactions, no cloud access."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC
from hashlib import sha256

import pytest
from test_safety_command_journal import facts
from test_safety_command_journal_sql import (
    BINDING,
    TOKEN,
    Connection,
    Cursor,
    Database,
    order,
    record,
    transact,
)

from pipmind.safety import command_journal as codec
from pipmind.safety import command_journal_sql as sql
from pipmind.safety import lease_sql
from pipmind.safety.azure_identity_sql import AzureDatabaseIdentity
from pipmind.safety.controls_sql import SqlControlStore
from pipmind.safety.lease_sql import LeaseBinding, LeaseGrant, SqlLeaseStore
from pipmind.safety.recovery_sql import RecoveryState, SqlRecoveryStore

AZURE = AzureDatabaseIdentity("synthetic-azure", "synthetic-db", "opaque physical λ")


def azure_facts():
    value, scope, command, attempt, unknown = facts()
    scope = replace(scope, application_incarnation=AZURE)
    command = replace(command, scope=scope)
    attempt = replace(
        attempt, scope=scope, command_sha256=codec.fingerprint(codec.encode_command(command))
    )
    unknown = replace(
        unknown,
        scope=scope,
        command_sha256=codec.fingerprint(codec.encode_command(command)),
        attempt_sha256=codec.fingerprint(codec.encode_attempt(attempt)),
    )
    return value, scope, command, attempt, unknown


def test_native_original_record_bytes_have_unchanged_golden_hashes():
    # Captured from main's original native codecs before this extension.
    expected = (
        "640c5a035cb3c5ca6cb0b427ef3febe27ca2585477b3cb721aee7f2b6751bf86",
        "85e0e2568406a76cbf5fbf52f5cb8f613e9d971d898a7107cd66baeb5cf3e91a",
        "0499c1240e6d9542930b50a5cb60d48dbce2377d4c0a5697a76bcc9d51ae4221",
        "1f5f0d2c1f5dc0057a5251b023b2a96f943543f9ffa5857ae9ea97af3ad79ff4",
    )
    encoders = (
        codec.encode_scope,
        codec.encode_command,
        codec.encode_attempt,
        codec.encode_unknown,
    )
    assert (
        tuple(codec.fingerprint(enc(v)) for enc, v in zip(encoders, facts()[1:], strict=True))
        == expected
    )


def test_azure_scopes_and_unknown_roundtrip_exactly_immutable_without_authority():
    _, scope, command, attempt, unknown = azure_facts()
    for value, enc, dec in (
        (scope, codec.encode_scope, codec.decode_scope),
        (command, codec.encode_command, codec.decode_command),
        (attempt, codec.encode_attempt, codec.decode_attempt),
        (unknown, codec.encode_unknown, codec.decode_unknown),
    ):
        raw = enc(value)
        assert dec(raw) == value and enc(dec(raw)) == raw
        assert '"profile":"azure-sql-physical-v1"' in raw
        assert value.dispatch_authorized is False
        with pytest.raises(FrozenInstanceError):
            value.dispatch_authorized = True
    projected = codec.project(command, attempt, unknown)
    assert projected.state == "unknown" and projected.unresolved
    assert not projected.dispatch_authorized
    assert projected.command.scope.assignment == scope.assignment


@pytest.mark.parametrize(
    "change", ["profile", "extra", "missing", "null", "native-like", "duplicate"]
)
def test_closed_envelope_rejects_forged_shapes_and_duplicates(change):
    raw = codec.encode_scope(azure_facts()[1])
    if change == "duplicate":
        bad = raw.replace('"profile":', '"profile":"azure-sql-physical-v1","profile":')
    else:
        data = json.loads(raw)
        pin = data["application_incarnation"]
        if change == "profile":
            pin["profile"] = "native"
        elif change == "extra":
            pin["accepted"] = True
        elif change == "missing":
            del pin["physical_database_name"]
        elif change == "null":
            data["application_incarnation"] = None
        else:
            data["application_incarnation"] = list(pin.values())
        bad = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(codec.JournalRefused):
        codec.decode_scope(bad)


@pytest.mark.parametrize("pin", [None, {}, ["a", "b", "c"], object()])
def test_scope_never_adopts_untyped_identity(pin):
    with pytest.raises(codec.JournalRefused, match="^journal_invalid_incarnation$"):
        codec.encode_scope(replace(azure_facts()[1], application_incarnation=pin))


@pytest.mark.parametrize(
    "pin", [facts()[1].application_incarnation, replace(AZURE, physical_database_name="rewound")]
)
def test_attempt_cannot_adopt_command_across_profile_or_physical_pin(pin):
    _, _, command, attempt, _ = azure_facts()
    changed = replace(attempt, scope=replace(attempt.scope, application_incarnation=pin))
    with pytest.raises(codec.JournalRefused, match="^journal_attempt_scope_mismatch$"):
        codec.validate_attempt(command, changed)


class AzureCursor(Cursor):
    def execute(self, query, params=()):
        q = " ".join(query.split())
        db = self.connection.database
        if "SERVERPROPERTY('EngineEdition')" in q:
            if db.identity_outage:
                raise OSError("private Azure identity failure")
            db.queries.append(q)
            db.identity_cursors.append(self)
            self.result, self.offset = (
                [
                    (
                        5,
                        db.native.logical_server,
                        db.native.database,
                        7,
                        self.connection.xact_state,
                        self.connection.trancount,
                    )
                ],
                0,
            )
            return self
        if "physical_database_name" in q:
            db.queries.append(q)
            self.result, self.offset = (
                [(7, db.native.database, db.native.physical_database_name)],
                0,
            )
            return self
        return super().execute(query, params)


class AzureConnection(Connection):
    def cursor(self):
        return AzureCursor(self)


class AzureDatabase(Database):
    def __init__(self):
        super().__init__()
        self.native = AZURE
        self.identity_outage = False
        self.identity_cursors = []

    def connect(self):
        if self.outage:
            raise OSError("private connector failure")
        connection = AzureConnection(self)
        self.connections.append(connection)
        return connection


@pytest.fixture
def azure_journal(monkeypatch):
    db = AzureDatabase()
    leases = SqlLeaseStore(
        db.connect, LeaseBinding(BINDING.account_id, "demo", "orders"), "e" * 64, incarnation=AZURE
    )
    recovery, controls = SqlRecoveryStore(db.connect, BINDING), SqlControlStore(db.connect, BINDING)
    store = sql.SqlCommandJournal(db.connect, leases, recovery, controls)
    monkeypatch.setattr(SqlControlStore, "read_in_transaction", lambda *args: db.control)
    monkeypatch.setattr(
        SqlRecoveryStore,
        "read_in_transaction",
        lambda *args: RecoveryState(BINDING, db.assignment.generation, db.assignment, True),
    )

    def native_forbidden(cur):
        raise AssertionError("native fallback is forbidden")

    monkeypatch.setattr(sql, "read_incarnation", native_forbidden)
    monkeypatch.setattr(lease_sql, "read_incarnation", native_forbidden)
    grant = LeaseGrant(
        leases.binding, leases.authority_epoch, "owner-1", 1, TOKEN, db.lease[7].replace(tzinfo=UTC)
    )
    sql.bootstrap(db.connect)
    return db, store, grant


def test_transaction_replay_attempt_unknown_and_complete_original_history(azure_journal):
    db, store, grant = azure_journal
    command = record(db, store, grant)
    assert command.scope.application_incarnation == AZURE
    assert record(db, store, grant) == command
    attempt = transact(db, store, grant, store.attempt_in_transaction, "command-1", "attempt-1")
    unknown = transact(db, store, grant, store.unknown_in_transaction, "command-1")
    before = deepcopy(db.tables)
    projection = store.lookup("command-1")
    assert (projection.command, projection.attempt, projection.unknown) == (
        command,
        attempt,
        unknown,
    )
    assert projection.unresolved and not projection.dispatch_authorized
    assert list(store.iter_history()) == [projection]
    assert db.tables == before and TOKEN not in repr(db.tables)
    assert db.identity_cursors and all(c.connection.closed for c in db.identity_cursors)
    assert not any("sys.database_recovery_status" in q for q in db.queries)


@pytest.mark.parametrize("fault", ["physical", "server", "outage"])
def test_pre_and_post_staging_identity_failure_discards_effects(azure_journal, monkeypatch, fault):
    db, store, grant = azure_journal
    before = deepcopy(db.tables)
    connection = db.connect()
    connection.locks.update(store._prior_locks)
    cur = connection.cursor()
    original = store._save

    def staged(*args):
        original(*args)
        if fault == "outage":
            db.identity_outage = True
        elif fault == "physical":
            db.native = replace(AZURE, physical_database_name="restored-after-staging")
        else:
            db.native = replace(AZURE, logical_server="other-server")

    monkeypatch.setattr(store, "_save", staged)
    error = sql.CommandJournalUnavailable if fault == "outage" else sql.CommandJournalRefused
    try:
        with pytest.raises(error):
            store.record_in_transaction(cur, grant, db.assignment, order(), causation_id="cause")
    finally:
        connection.close()
    assert db.tables == before
    assert db.identity_cursors and all(c is cur for c in db.identity_cursors)
    direct = db.connect()
    direct.locks.update(store._prior_locks)
    try:
        with pytest.raises(error):
            store.record_in_transaction(
                direct.cursor(), grant, db.assignment, order(), causation_id="cause"
            )
    finally:
        direct.close()
    assert db.tables == before


@pytest.mark.parametrize("phase", ["before", "after"])
def test_outer_commit_uncertainty_returns_no_permit_or_automatic_attempt(azure_journal, phase):
    db, store, grant = azure_journal
    db.failure = phase
    with pytest.raises(lease_sql.LeaseCommitUnknown):
        record(db, store, grant)
    db.failure = None
    historical = store.lookup("command-1")
    assert (historical is not None) == (phase == "after")
    if historical:
        assert historical.command.scope.application_incarnation == AZURE
        assert historical.attempt is None and not historical.dispatch_authorized


def test_later_unknown_observer_retains_original_attribution_and_exact_hashes(azure_journal):
    db, store, grant = azure_journal
    command = record(db, store, grant)
    attempt = transact(db, store, grant, store.attempt_in_transaction, "command-1", "attempt-1")
    old_bodies = deepcopy((db.tables["intents"], db.tables["attempts"]))
    epoch = "f" * 64
    db.native = replace(AZURE, physical_database_name="separately-approved-restored-pin")
    db.assignment = replace(db.assignment, generation=2, assignment_id="assignment-2", epoch=epoch)
    db.lease[3:7] = [epoch, 2, "owner-2", sha256(TOKEN.encode()).hexdigest()]
    leases = SqlLeaseStore(db.connect, store.leases.binding, epoch, incarnation=db.native)
    current = sql.SqlCommandJournal(db.connect, leases, store.recovery, store.controls)
    new_grant = replace(grant, authority_epoch=epoch, generation=2, owner_id="owner-2")
    unknown = transact(db, current, new_grant, current.unknown_in_transaction, "command-1")
    result = current.lookup("command-1")
    assert result.command == command and result.attempt == attempt
    assert unknown.scope.application_incarnation == db.native
    assert unknown.scope.assignment == db.assignment
    assert unknown.command_sha256 == codec.fingerprint(codec.encode_command(command))
    assert unknown.attempt_sha256 == codec.fingerprint(codec.encode_attempt(attempt))
    assert (db.tables["intents"], db.tables["attempts"]) == old_bodies
    assert result.unresolved and not result.dispatch_authorized


def test_azure_missing_locks_and_held_controls_still_refuse_attempt(azure_journal):
    db, store, grant = azure_journal
    connection = db.connect()
    try:
        with pytest.raises(sql.CommandJournalRefused, match="^command_journal_lock_required$"):
            store.record_in_transaction(
                connection.cursor(), grant, db.assignment, order(), causation_id="cause"
            )
    finally:
        connection.close()
    record(db, store, grant)
    db.control = replace(db.control, killed=True, paused=True)
    with pytest.raises(lease_sql.LeaseUnavailable):
        transact(db, store, grant, store.attempt_in_transaction, "command-1", "held")
    assert store.lookup("command-1").attempt is None


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_azure_commit_interruptions_remain_uncertain(azure_journal, phase, error):
    db, store, grant = azure_journal
    db.interruption = (phase, error)
    with pytest.raises(lease_sql.LeaseCommitUnknown):
        record(db, store, grant)
    db.interruption = None
    assert (store.lookup("command-1") is not None) == (phase == "after")
    assert not db.tables["attempts"]
