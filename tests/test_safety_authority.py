"""Offline native-incarnation and external-authority transaction regressions."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from hashlib import sha256

import pytest

from pipmind.safety.authority_sql import (
    MAX_REVISION,
    AuthorityCommitUnknown,
    AuthorityRefused,
    AuthorityState,
    AuthorityUnavailable,
    SqlAuthorityStore,
    bootstrap,
    decode_authority_state,
    encode_authority_state,
)
from pipmind.safety.incarnation import (
    DatabaseIncarnation,
    IncarnationRefused,
    IncarnationUnavailable,
    decode_incarnation,
    encode_incarnation,
    read_incarnation,
)
from pipmind.safety.lease_sql import LeaseBinding, LeaseCommitUnknown

AUTH = DatabaseIncarnation(
    "sql-authority",
    "authority",
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)
APP = DatabaseIncarnation(
    "sql-app",
    "application",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
)
BINDING = LeaseBinding("synthetic-account", "demo", "execution")


class Database:
    def __init__(self):
        self.incarnation = AUTH
        self.engine = 3
        self.rows, self.journal = {}, {}
        self.connections, self.statements = [], []
        self.autocommit = False
        self.outage = self.native_missing = False
        self.commit_failure = None
        self.commit_interrupt = None
        self.fail_on = None
        self.close_error = False
        self.lock_result = 0

    def connect(self):
        if self.outage:
            raise OSError("private SQL endpoint")
        db = Connection(self)
        self.connections.append(db)
        return db


class Connection:
    def __init__(self, database):
        self.database = database
        self.rows, self.journal = deepcopy(database.rows), deepcopy(database.journal)
        self.autocommit = database.autocommit
        self.closed = self.committed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        db = self.database
        if db.commit_failure == "before":
            raise OSError("private before-commit failure")
        if db.commit_interrupt and db.commit_interrupt[0] == "before":
            raise db.commit_interrupt[1]("private commit interrupt")
        db.rows, db.journal = deepcopy(self.rows), deepcopy(self.journal)
        self.committed = True
        if db.commit_failure == "after":
            raise OSError("private lost acknowledgement")
        if db.commit_interrupt:
            raise db.commit_interrupt[1]("private commit interrupt")

    def close(self):
        self.closed = True
        if self.database.close_error:
            raise SystemExit("private cleanup failure")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ("synthetic",)
        self.queue = []

    def execute(self, sql, params=()):
        q, db = " ".join(sql.split()), self.connection.database
        db.statements.append((q, params))
        if db.fail_on and db.fail_on in q:
            raise OSError("private transport text")
        self.queue = []
        if q.startswith("SET ") or q.startswith("IF OBJECT_ID"):
            pass
        elif "sys.database_recovery_status" in q:
            if not db.native_missing:
                inc = db.incarnation
                self.queue = [
                    (db.engine, inc.server, inc.database, inc.database_guid, inc.recovery_fork_guid)
                ]
        elif "sp_getapplock" in q:
            self.queue = [(db.lock_result,)]
        elif q.startswith("SELECT account_id,environment,resource,CASE"):
            row = self.connection.rows.get(params[1])
            if row is not None:
                self.queue = [row]
        elif q.startswith("SELECT TOP(1) operation_id"):
            self.queue = [(op,) for key, op in self.connection.journal if key == params[0]][:1]
        elif q.startswith("INSERT INTO dbo.pm_safety_authority_v1"):
            key, *row = params
            self.connection.rows[key] = tuple(row)
        elif q.startswith("UPDATE dbo.pm_safety_authority_v1"):
            raw, digest, key = params
            self.connection.rows[key] = (*self.connection.rows[key][:3], raw, digest)
        elif "FROM dbo.pm_safety_authority_fences_v1" in q:
            row = self.connection.journal.get((params[2], params[3]))
            if row is not None:
                self.queue = [row]
        elif q.startswith("INSERT INTO dbo.pm_safety_authority_fences_v1"):
            key, op, *row = params
            self.connection.journal[key, op] = tuple(row)
        elif q == "SELECT SYSUTCDATETIME()":
            self.queue = [(datetime(2026, 10, 21, 12),)]
        else:
            raise AssertionError("unexpected synthetic SQL")
        return self

    def fetchone(self):
        return self.queue.pop(0) if self.queue else None

    def nextset(self):
        return False


def setup():
    db = Database()
    store = SqlAuthorityStore(db.connect, BINDING, AUTH)
    return db, store


def activate(store, held, *, generation=1, assignment="assignment-A"):
    with store.guard(held, mode="held") as guard:
        pending = guard.activate(assignment, generation, "a" * 64)
        assert guard.state == pending
    return pending


def fence(store, state, *, op="fence-A", app=APP, reason="Explicit synthetic fence"):
    return store.fence(op, "owner", state.revision, state.epoch, app, reason)


def test_constructor_no_sql_explicit_bootstrap_and_held_initialize_never_reset():
    db, store = setup()
    assert not db.connections
    bootstrap(db.connect, AUTH)
    assert sum(q.startswith("IF OBJECT_ID") for q, _ in db.statements) == 2
    state = store.initialize(APP)
    assert state.mode == "held" and state.revision == state.assignment_generation == 0
    assert len(state.epoch) == 64 and state.assignment_id is state.robot_sha256 is None
    active = activate(store, state)
    assert store.initialize(APP) == active
    assert SqlAuthorityStore(db.connect, BINDING, AUTH).read() == active
    assert all(conn.closed for conn in db.connections)


def test_native_codecs_uuid_and_engine_gates():
    db, _ = setup()
    cur = db.connect().cursor()
    assert read_incarnation(cur) == AUTH
    assert decode_incarnation(encode_incarnation(APP)) == APP
    for edition in (1, 2, 3, 4, 8):
        db.engine = edition
        assert read_incarnation(cur) == AUTH
    db.engine = 5
    with pytest.raises(IncarnationRefused, match="^database_incarnation_unsupported$"):
        read_incarnation(cur)
    db.native_missing = True
    with pytest.raises(IncarnationUnavailable, match="^database_incarnation_unavailable$"):
        read_incarnation(cur)
    cur.connection.close()


@pytest.mark.parametrize(
    "guid",
    [
        None,
        b"x",
        "BAD",
        "0" * 36,
        "11111111-1111-4111-8111-11111111111A",
        "00000000-0000-0000-0000-000000000000",
    ],
)
def test_native_guid_is_canonical_nonzero_text(guid):
    with pytest.raises(IncarnationRefused):
        replace(APP, database_guid=guid)


@pytest.mark.parametrize(
    "inc",
    [
        AUTH,
        replace(AUTH, server="SQL-AUTHORITY", database="AUTHORITY"),
        replace(APP, database_guid=AUTH.database_guid),
    ],
)
def test_same_database_cannot_be_an_external_authority(inc):
    db, store = setup()
    with pytest.raises(AuthorityRefused, match="^authority_database_not_separate$"):
        store.initialize(inc)
    assert not db.connections


def test_state_codec_strict_roundtrip_immutable_and_missing_state_refusal():
    db, store = setup()
    with pytest.raises(AuthorityRefused, match="^authority_uninitialized$"):
        store.read()
    held = store.initialize(APP)
    assert decode_authority_state(encode_authority_state(held)) == held
    with pytest.raises(FrozenInstanceError):
        held.mode = "active"
    for changed in (
        replace(held, revision=True),
        replace(held, assignment_id="orphan"),
        replace(held, epoch="owner"),
        replace(held, mode="active"),
    ):
        with pytest.raises(AuthorityRefused):
            encode_authority_state(changed)
    raw = encode_authority_state(held)
    with pytest.raises(AuthorityRefused):
        decode_authority_state(raw + " ")
    assert len(db.rows) == 1


def test_held_activation_and_fence_keep_monotonic_highwater_and_old_receipt_history():
    db, store = setup()
    held = store.initialize(APP)
    active = activate(store, held, generation=7)
    receipt = fence(store, active)
    current = store.read()
    assert current.mode == "held" and current.epoch != active.epoch
    assert current.revision == active.revision + 1 and current.assignment_generation == 7
    assert current.assignment_id is current.robot_sha256 is None
    assert receipt["historical_receipt_only"] and receipt["dispatch_authorized"] is False
    fresh = activate(store, current, generation=8, assignment="assignment-B")
    assert fence(store, active) == receipt  # Historical retry, even after newer activation.
    assert store.read() == fresh and len(db.journal) == 1
    with pytest.raises(AuthorityRefused, match="^conflicting_authority_operation$"):
        fence(store, active, reason="Changed owner intent")
    with pytest.raises(AuthorityRefused, match="^authority_stale_state$"):
        fence(store, active, op="stale")


def test_guards_require_exact_current_mode_epoch_revision_assignment_and_close():
    _, store = setup()
    held = store.initialize(APP)
    with pytest.raises(AuthorityRefused, match="^authority_mode_mismatch$"):
        with store.guard(held):
            pytest.fail("entered held guard with active requirement")
    active = activate(store, held)
    for stale in (
        held,
        replace(active, revision=active.revision + 1),
        replace(active, epoch="b" * 64),
        replace(active, assignment_id="changed"),
    ):
        with pytest.raises(AuthorityRefused):
            with store.guard(stale, mode=stale.mode):
                pytest.fail("entered stale guard")
    with store.guard(active) as guard:
        assert guard.state == active
    with pytest.raises(AuthorityRefused, match="^authority_guard_closed$"):
        guard.activate("cannot", 2, "b" * 64)


@pytest.mark.parametrize("generation", [0, True, 1, -1, MAX_REVISION + 1])
def test_activation_generation_never_rewinds_or_reuses_after_fencing(generation):
    db, store = setup()
    active = activate(store, store.initialize(APP))
    fence(store, active)
    held = store.read()
    before = deepcopy(db.rows)
    with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
        with store.guard(held, mode="held") as guard:
            guard.activate("new", generation, "b" * 64)
    assert db.rows == before


@pytest.mark.parametrize(
    "exception", [OSError, AuthorityRefused, AuthorityUnavailable, AuthorityCommitUnknown]
)
def test_inner_exception_text_never_leaks_and_staged_activation_rolls_back(exception):
    db, store = setup()
    held = store.initialize(APP)
    before = deepcopy(db.rows)
    with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
        with store.guard(held, mode="held") as guard:
            guard.activate("new", 1, "a" * 64)
            raise exception("PRIVATE sensitive callback data")
    assert db.rows == before and db.connections[-1].closed


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_precommit_baseexception_propagates_and_external_state_stays_held(exception):
    db, store = setup()
    held = store.initialize(APP)
    before = deepcopy(db.rows)
    with pytest.raises(exception):
        with store.guard(held, mode="held") as guard:
            guard.activate("new", 1, "a" * 64)
            raise exception("synthetic interrupt")
    assert db.rows == before and db.connections[-1].closed


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("exception", [None, KeyboardInterrupt, SystemExit])
def test_commit_unknown_exposes_no_activation_or_fence_verdict(phase, exception):
    db, store = setup()
    held = store.initialize(APP)
    if exception:
        db.commit_interrupt = (phase, exception)
    else:
        db.commit_failure = phase
    db.close_error = True
    with pytest.raises(AuthorityCommitUnknown, match="^authority_commit_unknown$"):
        activate(store, held)
    db.commit_failure = db.commit_interrupt = None
    assert store.read().mode == ("active" if phase == "after" else "held")


def test_fence_conflict_also_requires_commit_acknowledgement():
    db, store = setup()
    state = store.initialize(APP)
    receipt = fence(store, state)
    before = deepcopy(db.rows)
    db.commit_failure = "after"
    with pytest.raises(AuthorityCommitUnknown, match="^authority_commit_unknown$"):
        fence(store, state, reason="changed")
    assert db.rows == before and len(db.journal) == 1
    db.commit_failure = None
    assert fence(store, state) == receipt


@pytest.mark.parametrize("phase", ["before", "after"])
def test_explicit_external_native_pin_checked_before_and_after_guard(phase):
    db, store = setup()
    held = store.initialize(APP)
    before = deepcopy(db.rows)
    changed = replace(AUTH, recovery_fork_guid="55555555-5555-4555-8555-555555555555")
    entered = []
    if phase == "before":
        db.incarnation = changed
    with pytest.raises(AuthorityRefused, match="^authority_incarnation_mismatch$"):
        with store.guard(held, mode="held") as guard:
            entered.append(True)
            guard.activate("new", 1, "a" * 64)
            db.incarnation = changed
    assert bool(entered) == (phase == "after") and db.rows == before


def test_initialization_never_automatically_adopts_restored_application():
    db, store = setup()
    held = store.initialize(APP)
    restored = replace(APP, database_guid="66666666-6666-4666-8666-666666666666")
    with pytest.raises(AuthorityRefused, match="^authority_application_mismatch$"):
        store.initialize(restored)
    receipt = fence(store, held, app=restored)
    assert receipt["after"]["application_incarnation"][2] == restored.database_guid
    assert store.read().application_incarnation == restored
    assert len(db.journal) == 1


def test_store_sql_failure_autocommit_and_corruption_fail_closed():
    db, store = setup()
    held = store.initialize(APP)
    db.outage = True
    with pytest.raises(AuthorityUnavailable, match="^authority_store_unavailable$"):
        store.read()
    db.outage = False
    db.autocommit = True
    with pytest.raises(AuthorityRefused, match="^authority_transaction_required$"):
        store.read()
    db.autocommit = False
    db.fail_on = "INSERT INTO dbo.pm_safety_authority_fences_v1"
    before = deepcopy(db.rows)
    with pytest.raises(AuthorityUnavailable):
        fence(store, held)
    assert db.rows == before and not db.journal
    db.fail_on = None
    key = next(iter(db.rows))
    row = db.rows[key]
    db.rows[key] = (*row[:3], row[3], "0" * 64)
    with pytest.raises(AuthorityUnavailable):
        store.read()


def test_guard_preserves_external_hold_when_application_work_may_already_be_committed():
    db, store = setup()
    held = store.initialize(APP)
    application_commits = []
    with pytest.raises(AuthorityUnavailable, match="^authority_operation_failed$"):
        with store.guard(held, mode="held") as guard:
            application_commits.append("independent application commit")
            guard.activate("replacement", 1, "a" * 64)
            raise OSError("after independent application commit")
    assert application_commits and store.read() == held
    assert db.connections[-1].closed


def test_held_guard_cannot_implicitly_activate_and_closed_handle_not_authority():
    _, store = setup()
    held = store.initialize(APP)
    with store.guard(held, mode="held") as guard:
        assert guard.state is not None
    assert store.read() == held


def test_fence_receipt_canonical_hashes_are_retained_exactly():
    db, store = setup()
    held = store.initialize(APP)
    receipt = fence(store, held)
    body, body_hash, raw, receipt_hash = next(iter(db.journal.values()))
    assert sha256(body.encode()).hexdigest() == body_hash
    assert sha256(raw.encode()).hexdigest() == receipt_hash
    assert receipt == fence(store, held)
    # Neither native observation nor fences are broker/dispatch authority.
    assert receipt["dispatch_authorized"] is False
    assert type(store.read()) is AuthorityState


def test_application_commit_unknown_is_preserved_while_external_activation_aborts():
    db, store = setup()
    held = store.initialize(APP)
    original = LeaseCommitUnknown("lease_commit_unknown")
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$") as caught:
        with store.guard(held, mode="held") as guard:
            guard.activate("replacement", 1, "a" * 64)
            raise original
    assert caught.value is original and store.read() == held
    assert db.connections[-1].closed
