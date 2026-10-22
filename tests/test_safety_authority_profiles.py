"""Closed Azure authority composition; synthetic SQL, no hosted permission claim."""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from test_safety_authority import APP, AUTH, BINDING, Connection, Cursor, Database

from pipmind.safety import authority_sql as authority
from pipmind.safety.azure_identity_sql import AzureDatabaseIdentity
from pipmind.safety.lease_sql import LeaseCommitUnknown

AUTH_AZ = AzureDatabaseIdentity("sql-authority", "authority", "authority-physical")
APP_AZ = AzureDatabaseIdentity("sql-app", "application", "application-physical")
TARGET = AzureDatabaseIdentity("sql-app", "restored", "restored-physical")


class AzureCursor(Cursor):
    def execute(self, sql, params=()):
        q, db = " ".join(sql.split()), self.connection.database
        if "SERVERPROPERTY('EngineEdition')" in q or "FROM sys.databases" in q:
            db.statements.append((q, params))
            db.identity_cursors.append(self)
            if db.identity_outage:
                raise OSError("PRIVATE identity endpoint")
            pin = db.identity
            if "SERVERPROPERTY('EngineEdition')" in q:
                self.queue = [(db.engine, pin.logical_server, pin.database, 7, 1, 1)]
            else:
                self.queue = [(7, pin.database, pin.physical_database_name)]
            return self
        return super().execute(sql, params)


class AzureConnection(Connection):
    def cursor(self):
        return AzureCursor(self)


class AzureDatabase(Database):
    def __init__(self):
        super().__init__()
        self.identity = AUTH_AZ
        self.engine = 5
        self.identity_outage = False
        self.identity_cursors = []

    def connect(self):
        if self.outage:
            raise OSError("PRIVATE endpoint")
        conn = AzureConnection(self)
        self.connections.append(conn)
        return conn


def setup():
    db = AzureDatabase()
    return db, authority.SqlAuthorityStore(db.connect, BINDING, AUTH_AZ)


def activate(store, held, generation=1):
    with store.guard(held, mode="held") as guard:
        guard.record_application_barrier(held.application_incarnation, generation)
        pending = guard.activate("synthetic-assignment", generation, "a" * 64)
    return pending


def fence(store, state, target=APP_AZ, op="synthetic-fence"):
    return store.fence(op, "owner", state.revision, state.epoch, target, "Synthetic recovery")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def test_native_authority_state_stays_byte_identical_with_array_pin():
    state = authority.AuthorityState(BINDING, 0, "a" * 64, "held", None, 0, None, APP)
    expected = (
        '{"application_incarnation":["sql-app","application",'
        '"33333333-3333-4333-8333-333333333333","44444444-4444-4444-8444-444444444444"],'
        '"assignment_generation":0,"assignment_id":null,'
        '"binding":["synthetic-account","demo","execution"],"epoch":"'
        + "a"
        * 64
        + '","mode":"held","revision":0,"robot_sha256":null,'
        '"source_epoch":null,"source_incarnation":null}'
    )
    assert authority.encode_authority_state(state) == expected
    assert authority.decode_authority_state(expected) == state


def test_azure_state_nested_envelope_strict_roundtrip_and_no_mixed_source():
    state = authority.AuthorityState(
        BINDING, 2, "a" * 64, "held", None, 1, None, TARGET, APP_AZ, "b" * 64
    )
    raw = authority.encode_authority_state(state)
    assert authority.decode_authority_state(raw) == state
    parsed = json.loads(raw)
    assert parsed["application_incarnation"] == {
        "profile": "azure-sql-physical-v1",
        "logical_server": "sql-app",
        "database": "restored",
        "physical_database_name": "restored-physical",
    }
    for bad in (
        replace(state, source_incarnation=APP),
        replace(state, application_incarnation=APP),
        replace(state, application_incarnation={"profile": "azure-sql-physical-v1"}),
    ):
        with pytest.raises(authority.AuthorityRefused):
            authority.encode_authority_state(bad)
    for changed in (
        raw + " ",
        raw.replace('"profile":"azure-sql-physical-v1"', '"profile":"unknown"'),
        raw.replace(
            '"profile":"azure-sql-physical-v1"',
            '"profile":"azure-sql-physical-v1","profile":"azure-sql-physical-v1"',
        ),
    ):
        with pytest.raises(authority.AuthorityRefused):
            authority.decode_authority_state(changed)


@pytest.mark.parametrize("own,application", [(AUTH, APP_AZ), (AUTH_AZ, APP)])
def test_mixed_authority_and_application_profiles_refuse_before_connect(own, application):
    db = AzureDatabase()
    store = authority.SqlAuthorityStore(db.connect, BINDING, own)
    with pytest.raises(authority.AuthorityRefused, match="^authority_identity_profile_mismatch$"):
        store.initialize(application)
    assert not db.connections


@pytest.mark.parametrize(
    "application",
    [
        AUTH_AZ,
        replace(
            AUTH_AZ,
            logical_server="SQL-AUTHORITY",
            database="AUTHORITY",
            physical_database_name="different",
        ),
        replace(APP_AZ, physical_database_name=AUTH_AZ.physical_database_name),
    ],
)
def test_azure_location_or_physical_match_cannot_claim_separation(application):
    db, store = setup()
    with pytest.raises(authority.AuthorityRefused, match="^authority_database_not_separate$"):
        store.initialize(application)
    assert not db.connections


def test_azure_operations_keep_same_cursor_bracketing_and_native_alias_unused(monkeypatch):
    monkeypatch.setattr(authority, "read_incarnation", lambda cur: pytest.fail("native reader"))
    db, store = setup()
    authority.bootstrap(db.connect, AUTH_AZ)
    held = store.initialize(APP_AZ)
    active = activate(store, held, 7)
    receipt = fence(store, active)
    assert receipt["historical_receipt_only"] is True
    assert receipt["dispatch_authorized"] is False
    assert store.read().assignment_generation == 7
    assert (
        authority.decode_authority_state(authority.encode_authority_state(store.read()))
        == store.read()
    )
    for conn in db.connections:
        cursors = [cur for cur in db.identity_cursors if cur.connection is conn]
        assert len(cursors) == 8 and all(cur is cursors[0] for cur in cursors)
        assert conn.committed and conn.closed
    assert all("sys.database_recovery_status" not in q for q, _ in db.statements)


def test_azure_restore_cannot_autoadopt_and_target_barrier_does_not_erase_source():
    db, store = setup()
    active = activate(store, store.initialize(APP_AZ))
    before = deepcopy(db.rows)
    with pytest.raises(authority.AuthorityRefused, match="^authority_application_mismatch$"):
        store.initialize(TARGET)
    assert db.rows == before
    fence(store, active, TARGET)
    held = store.read()
    required_epoch = held.epoch
    assert held.source_incarnation == APP_AZ
    with store.guard(held, mode="held") as guard:
        target_receipt = guard.record_application_barrier(TARGET, 2)
    history = deepcopy(db.barriers)
    with pytest.raises(authority.AuthorityUnavailable):
        with store.guard(held, mode="held") as guard:
            guard.activate("target-only", 2, "b" * 64)
    assert db.barriers == history and store.read() == held
    fence(store, held, APP_AZ, "return-source")
    source = store.read()
    with store.guard(source, mode="held") as guard:
        source_receipt = guard.record_application_barrier(APP_AZ, 3)
    assert source_receipt["acknowledged_under_epoch"] == source.epoch
    fence(store, source, TARGET, "return-target")
    ready = store.read()
    assert ready.source_epoch == required_epoch
    with store.guard(ready, mode="held") as guard:
        guard.record_application_barrier(TARGET, 2)
        pending = guard.activate("ready-target", 2, "b" * 64)
    assert store.read() == pending
    assert all(db.barriers[key] == row for key, row in history.items())
    assert target_receipt["historical_receipt_only"] is True


@pytest.mark.parametrize("phase", ["before", "after"])
def test_azure_own_physical_change_refuses_before_commit_without_repin(phase):
    db, store = setup()
    held = store.initialize(APP_AZ)
    before = deepcopy(db.rows)
    changed = replace(AUTH_AZ, physical_database_name="restored-authority")
    entered = []
    if phase == "before":
        db.identity = changed
    with pytest.raises(authority.AuthorityUnavailable, match="^authority_incarnation_unavailable$"):
        with store.guard(held, mode="held") as guard:
            entered.append(True)
            guard.record_application_barrier(APP_AZ, 1)
            guard.activate("staged", 1, "a" * 64)
            db.identity = changed
    assert bool(entered) == (phase == "after")
    assert db.rows == before and not db.barriers
    assert store.expected_incarnation == AUTH_AZ
    assert db.connections[-1].closed and not db.connections[-1].committed


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("exception", [None, KeyboardInterrupt, SystemExit])
def test_azure_commit_ambiguity_never_returns_positive_activation(phase, exception):
    db, store = setup()
    held = store.initialize(APP_AZ)
    if exception is None:
        db.commit_failure = phase
    else:
        db.commit_interrupt = (phase, exception)
    with pytest.raises(authority.AuthorityCommitUnknown, match="^authority_commit_unknown$"):
        activate(store, held)
    db.commit_failure = db.commit_interrupt = None
    assert store.read().mode == ("active" if phase == "after" else "held")


def test_azure_application_commit_unknown_and_identity_outage_stay_uncertain():
    db, store = setup()
    held = store.initialize(APP_AZ)
    original = LeaseCommitUnknown("lease_commit_unknown")
    with pytest.raises(LeaseCommitUnknown) as caught:
        with store.guard(held, mode="held") as guard:
            guard.record_application_barrier(APP_AZ, 1)
            raise original
    assert caught.value is original and not db.barriers
    db.identity_outage = True
    with pytest.raises(authority.AuthorityUnavailable, match="^authority_incarnation_unavailable$"):
        store.read()
    db.identity_outage = False
    assert store.read() == held


def test_azure_journal_mixed_historical_before_refuses_without_repairing_receipt():
    db, store = setup()
    held = store.initialize(APP_AZ)
    fence(store, held)
    key, row = next(iter(db.journal.items()))
    receipt = json.loads(row[2])
    receipt["before"]["application_incarnation"] = json.loads(authority._incarnation(APP))
    raw = canonical(receipt)
    db.journal[key] = (*row[:2], raw, sha256(raw.encode()).hexdigest())
    before = deepcopy(db.journal)
    with pytest.raises(authority.AuthorityUnavailable, match="^authority_store_unavailable$"):
        fence(store, held)
    assert db.journal == before
