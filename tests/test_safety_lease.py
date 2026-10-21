"""Synthetic DB-API transaction controls, without SQL/provider connections."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from pipmind.safety.lease_sql import (
    LeaseBinding,
    LeaseCommitUnknown,
    LeaseRefused,
    LeaseUnavailable,
    SqlLeaseStore,
    bootstrap,
)

BINDING = LeaseBinding("synthetic-account", "demo", "execution")


class Database:
    def __init__(self):
        self.rows = {}
        self.effects = []
        self.now = datetime(2026, 10, 21)
        self.connections = []
        self.commit_failure = None
        self.outage = False
        self.nextset_missing = False
        self.lock_result = 0
        self.statements = []
        self.autocommit = False
        self.close_failure = False
        self.commit_interrupt = None
        self.close_interrupt = None

    def connect(self):
        if self.outage:
            raise OSError("private driver failure")
        db = Connection(self)
        self.connections.append(db)
        return db


class Connection:
    def __init__(self, database):
        self.database = database
        self.rows = deepcopy(database.rows)
        self.effects = list(database.effects)
        self.autocommit = database.autocommit
        self.closed = False
        self.committed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        if self.database.commit_failure == "before":
            raise OSError("private commit failure")
        if self.database.commit_interrupt and self.database.commit_interrupt[0] == "before":
            raise self.database.commit_interrupt[1]("synthetic commit interruption")
        self.database.rows = deepcopy(self.rows)
        self.database.effects = list(self.effects)
        self.committed = True
        if self.database.commit_failure == "after":
            raise OSError("private acknowledgement lost")
        if self.database.commit_interrupt:
            raise self.database.commit_interrupt[1]("synthetic commit interruption")

    def close(self):
        self.closed = True
        if self.database.close_failure:
            raise OSError("synthetic cleanup failure")
        if self.database.close_interrupt:
            raise self.database.close_interrupt("synthetic cleanup interruption")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ("synthetic",)
        self.result = None

    def execute(self, sql, params=()):
        db = self.connection.database
        db.statements.append((sql, params))
        statement = " ".join(sql.split())
        self.result = None
        if statement.startswith("SET ") or statement.startswith("IF OBJECT_ID"):
            pass
        elif "sp_getapplock" in sql:
            self.description = None if db.nextset_missing else ("result",)
            self.result = (db.lock_result,)
        elif statement == "SELECT SYSUTCDATETIME()":
            self.result = (db.now,)
        elif statement.startswith("SELECT account_id"):
            row = self.connection.rows.get(params[0])
            self.result = tuple(row) if row is not None else None
        elif statement.startswith("INSERT INTO dbo.pm_safety_leases_v1"):
            key, account, env, resource, authority = params
            self.connection.rows[key] = [account, env, resource, authority, 0, None, None, None]
        elif "SET generation=generation+1" in statement:
            owner, token_hash, ttl, key = params
            row = self.connection.rows[key]
            row[4] += 1
            row[5:] = [owner, token_hash, db.now + timedelta(seconds=ttl)]
        elif "SET expires_at=DATEADD" in statement:
            ttl, key = params
            row = self.connection.rows[key]
            if row[7] > db.now:
                row[7] = db.now + timedelta(seconds=ttl)
                self.result = (row[7],)
        elif "SET owner_id=NULL" in statement:
            self.connection.rows[params[0]][5:] = [None, None, None]
        elif statement == "SYNTHETIC EFFECT":
            self.connection.effects.append(params[0])
        else:
            raise AssertionError("unrecognized synthetic SQL")
        return self

    def fetchone(self):
        return self.result

    def nextset(self):
        return False


@pytest.fixture
def owned():
    database = Database()
    bootstrap(database.connect)
    store = SqlLeaseStore(database.connect, BINDING, "trusted-epoch-1")
    store.initialize()
    grant = store.acquire("opaque-owner-A", ttl_seconds=30)
    return database, store, grant


def test_explicit_bootstrap_initialization_and_fresh_instance_never_reset(owned):
    db, store, grant = owned
    before = deepcopy(db.rows)
    fresh = SqlLeaseStore(db.connect, BINDING, "trusted-epoch-1")
    assert db.rows == before  # Construction performs no SQL.
    bootstrap(db.connect)
    fresh.initialize()
    assert db.rows == before
    with pytest.raises(LeaseRefused, match="^lease_held$"):
        fresh.acquire(grant.owner_id, ttl_seconds=30)  # Same owner is not a replay shortcut.
    assert db.rows == before
    store.compare_and_act(grant, lambda cur: cur.execute("SYNTHETIC EFFECT", ("accepted",)))
    assert db.effects == ["accepted"]
    assert all(session.closed for session in db.connections)
    schema_locks = [p[0] for s, p in db.statements if "sp_getapplock" in s]
    assert schema_locks.count("pipmind-safety-lease-schema-v1") == 2


def test_uninitialized_acquire_is_refused_and_does_not_create_binding():
    db = Database()
    store = SqlLeaseStore(db.connect, BINDING, "trusted-epoch-1")
    with pytest.raises(LeaseRefused, match="^lease_not_initialized$"):
        store.acquire("owner", ttl_seconds=10)
    assert db.rows == {}


def test_no_raw_token_is_persisted_or_in_grant_repr(owned):
    db, _, grant = owned
    assert grant.token not in repr(grant)
    assert grant.token not in repr(db.rows)
    assert grant.token not in repr(db.statements)
    assert len(next(iter(db.rows.values()))[6]) == 64


@pytest.mark.parametrize("ttl", [None, True, 0, -1, 301, 1.0, "30"])
def test_ttl_rejection_is_pre_sql_for_acquire_and_renew(owned, ttl):
    db, store, grant = owned
    before = len(db.connections)
    with pytest.raises(LeaseRefused, match="^lease_invalid_ttl$"):
        store.acquire("another-owner", ttl_seconds=ttl)
    with pytest.raises(LeaseRefused, match="^lease_invalid_ttl$"):
        store.renew(grant, ttl_seconds=ttl)
    assert len(db.connections) == before


@pytest.mark.parametrize("ttl", [1, 300])
def test_ttl_boundary_values_and_server_clock(owned, ttl):
    db, store, grant = owned
    renewed = store.renew(grant, ttl_seconds=ttl)
    assert renewed.expires_at.replace(tzinfo=None) == db.now + timedelta(seconds=ttl)
    assert renewed.generation == grant.generation and renewed.token == grant.token


def test_expiry_takeover_increments_generation_and_rejects_prior_grants(owned):
    db, store, grant = owned
    db.now += timedelta(seconds=30)  # Equality is expired, not valid.
    called = []
    with pytest.raises(LeaseRefused, match="^lease_expired$"):
        store.compare_and_act(grant, lambda cur: called.append(True))
    with pytest.raises(LeaseRefused, match="^lease_expired$"):
        store.renew(grant, ttl_seconds=300)
    current = store.acquire("opaque-owner-B", ttl_seconds=30)
    assert current.generation == grant.generation + 1
    assert current.token != grant.token
    for action in [store.release, lambda x: store.renew(x, ttl_seconds=30)]:
        with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
            action(grant)
    with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
        store.compare_and_act(grant, lambda cur: called.append(True))
    assert called == []


def test_release_preserves_generation_and_next_acquire_is_new_ownership(owned):
    db, store, grant = owned
    store.release(grant)
    assert next(iter(db.rows.values()))[4:] == [grant.generation, None, None, None]
    new = store.acquire(grant.owner_id, ttl_seconds=30)
    assert new.generation == grant.generation + 1 and new.token != grant.token


@pytest.mark.parametrize(
    "field,value", [("owner_id", "other"), ("generation", 2), ("token", "0" * 64)]
)
def test_wrong_ownership_never_executes_callback(owned, field, value):
    db, store, grant = owned
    changed = replace(grant, **{field: value})
    with pytest.raises(LeaseRefused, match="^lease_stale_owner$"):
        store.compare_and_act(changed, lambda cur: cur.execute("SYNTHETIC EFFECT", ("unsafe",)))
    assert db.effects == []


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("binding", LeaseBinding("other", "demo", "execution"), "lease_binding_mismatch"),
        ("authority_epoch", "other", "lease_authority_mismatch"),
        ("generation", True, "lease_invalid_grant"),
        ("token", None, "lease_invalid_grant"),
    ],
)
def test_bad_grants_refused_before_connection(owned, field, value, code):
    db, store, grant = owned
    count = len(db.connections)
    with pytest.raises(LeaseRefused, match="^" + code + "$"):
        store.release(replace(grant, **{field: value}))
    assert len(db.connections) == count


def test_epoch_change_never_rebinds_existing_row(owned):
    db, _, _ = owned
    before = deepcopy(db.rows)
    stranger = SqlLeaseStore(db.connect, BINDING, "different-trusted-epoch")
    with pytest.raises(LeaseRefused, match="^lease_authority_mismatch$"):
        stranger.initialize()
    assert db.rows == before


def test_callback_effects_roll_back_on_expiry_and_cannot_return_success(owned):
    db, store, grant = owned
    before = deepcopy(db.rows)

    def callback(cur):
        cur.execute("SYNTHETIC EFFECT", ("expired",))
        db.now += timedelta(seconds=30)
        return "must never escape"

    with pytest.raises(LeaseRefused, match="^lease_expired$"):
        store.compare_and_act(grant, callback)
    assert db.effects == [] and db.rows == before
    assert db.connections[-1].closed and not db.connections[-1].committed


def test_callback_lease_change_is_detected_and_rolled_back(owned):
    db, store, grant = owned
    before = deepcopy(db.rows)

    def callback(cur):
        cur.execute("SYNTHETIC EFFECT", ("uncommitted",))
        next(iter(cur.connection.rows.values()))[7] += timedelta(seconds=20)

    with pytest.raises(LeaseRefused, match="^lease_callback_changed_lease$"):
        store.compare_and_act(grant, callback)
    assert db.rows == before and db.effects == []


def test_callback_failure_preserves_pretransaction_state_and_hides_driver_details(owned):
    db, store, grant = owned

    def callback(cur):
        cur.execute("SYNTHETIC EFFECT", ("uncommitted",))
        raise RuntimeError("private callback details")

    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        store.compare_and_act(grant, callback)
    assert db.effects == [] and db.connections[-1].closed


@pytest.mark.parametrize("error_type", [LeaseRefused, LeaseUnavailable, LeaseCommitUnknown])
def test_callback_domain_exceptions_are_not_internal_verdicts_or_private_error_leaks(
    owned, error_type
):
    db, store, grant = owned
    before = deepcopy(db.rows)
    attempts = []

    def callback(cur):
        attempts.append(True)
        cur.execute("SYNTHETIC EFFECT", ("must rollback",))
        raise error_type("PRIVATE_CALLBACK_DETAILS")

    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$") as raised:
        store.compare_and_act(grant, callback)
    assert type(raised.value) is LeaseUnavailable
    assert "PRIVATE_CALLBACK_DETAILS" not in str(raised.value)
    assert "PRIVATE_CALLBACK_DETAILS" not in repr(raised.value)
    assert raised.value.__suppress_context__
    assert attempts == [True]
    assert db.rows == before and db.effects == []
    assert db.connections[-1].closed and not db.connections[-1].committed


@pytest.mark.parametrize("failure", ["before", "after"])
def test_lost_commit_ack_is_uncertain_in_both_possible_database_states(owned, failure):
    db, store, grant = owned
    db.commit_failure = failure
    calls = []

    def callback(cur):
        calls.append(True)
        cur.execute("SYNTHETIC EFFECT", ("maybe committed",))
        return "must not return"

    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.compare_and_act(grant, callback)
    assert calls == [True]  # No automatic transaction/callback replay.
    assert db.effects == (["maybe committed"] if failure == "after" else [])
    assert db.connections[-1].closed


def test_sql_outage_never_calls_callback_or_yields_detached_authority(owned):
    db, store, grant = owned
    db.outage = True
    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        store.compare_and_act(grant, lambda cur: pytest.fail("callback on unavailable SQL"))


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_commit_interruption_is_unknown_without_result_or_replay(owned, interrupt, phase):
    db, store, grant = owned
    db.commit_interrupt = (phase, interrupt)
    # Cleanup must not replace the unknown-commit classification with an interrupt.
    db.close_interrupt = interrupt
    callbacks = []

    def callback(cur):
        callbacks.append(True)
        cur.execute("SYNTHETIC EFFECT", ("possibly committed",))
        return "must not escape"

    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.compare_and_act(grant, callback)
    assert callbacks == [True]
    assert db.effects == (["possibly committed"] if phase == "after" else [])
    assert db.connections[-1].closed
    assert db.connections[-1].committed is (phase == "after")


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_precommit_callback_interruption_propagates_and_discards_effects(owned, interrupt):
    db, store, grant = owned
    before = deepcopy(db.rows)
    interruption = interrupt("synthetic callback interruption")
    db.close_interrupt = interrupt
    callbacks = []

    def callback(cur):
        callbacks.append(True)
        cur.execute("SYNTHETIC EFFECT", ("uncommitted",))
        raise interruption

    with pytest.raises(interrupt) as raised:
        store.compare_and_act(grant, callback)
    assert raised.value is interruption
    assert callbacks == [True]
    assert db.effects == [] and db.rows == before
    assert db.connections[-1].closed and not db.connections[-1].committed


@pytest.mark.parametrize("failure", ["before", "after"])
def test_unknown_acquire_never_yields_grant_or_retries(owned, failure):
    db, store, prior = owned
    store.release(prior)
    previous_connections = len(db.connections)
    db.commit_failure = failure
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.acquire("uncertain-owner", ttl_seconds=30)
    assert len(db.connections) == previous_connections + 1
    assert next(iter(db.rows.values()))[5] == ("uncertain-owner" if failure == "after" else None)


def test_cleanup_failure_does_not_replace_acknowledged_success_or_unknown_commit(owned):
    db, store, grant = owned
    db.close_failure = True
    assert store.compare_and_act(grant, lambda cur: "acknowledged") == "acknowledged"
    with pytest.raises(LeaseRefused, match="^lease_held$"):
        store.acquire("other", ttl_seconds=30)
    db.commit_failure = "after"
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.compare_and_act(grant, lambda cur: "uncertain")
    assert db.connections[-1].closed


@pytest.mark.parametrize(
    "mode,code", [("busy", "lease_store_busy"), ("missing", "lease_lock_result_missing")]
)
def test_lock_failures_close_session_and_never_write(mode, code):
    db = Database()
    db.lock_result = -1 if mode == "busy" else 0
    db.nextset_missing = mode == "missing"
    with pytest.raises((LeaseRefused, LeaseUnavailable), match="^" + code + "$"):
        bootstrap(db.connect)
    assert db.rows == {} and db.connections[-1].closed


def test_autocommit_connector_is_rejected_before_any_sql():
    db = Database()
    db.autocommit = True
    with pytest.raises(LeaseRefused, match="^lease_transaction_required$"):
        bootstrap(db.connect)
    assert db.statements == [] and db.connections[-1].closed


@pytest.mark.parametrize("value", [None, 3, "", " x", "x ", "x\x00", "😀" * 65, "\ud800"])
def test_invalid_identifiers_fail_without_sql(value):
    db = Database()
    with pytest.raises(LeaseRefused, match="^lease_invalid_binding$"):
        SqlLeaseStore(db.connect, LeaseBinding(value, "demo", "execution"), "epoch")
    with pytest.raises(LeaseRefused, match="^lease_invalid_authority$"):
        SqlLeaseStore(db.connect, BINDING, value)
    assert db.connections == []


def test_case_sensitive_and_delimiter_safe_bindings_are_distinct():
    db = Database()
    for binding in [
        BINDING,
        replace(BINDING, account_id="Synthetic-account"),
        LeaseBinding("a:b", "c", "d"),
        LeaseBinding("a", "b:c", "d"),
    ]:
        SqlLeaseStore(db.connect, binding, "epoch").initialize()
    assert len(db.rows) == 4


def test_generation_exhaustion_is_fail_closed(owned):
    db, store, _ = owned
    row = next(iter(db.rows.values()))
    row[4] = 9223372036854775807
    row[7] = db.now
    with pytest.raises(LeaseRefused, match="^lease_generation_exhausted$"):
        store.acquire("owner", ttl_seconds=1)
