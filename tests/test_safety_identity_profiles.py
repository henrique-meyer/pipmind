"""Closed profile contracts and real fixed-reader SQL sequencing with synthetic SQL."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_safety_authority import APP
from test_safety_lease import BINDING, Connection, Cursor, Database

from pipmind.safety import azure_identity_sql, identity_profiles, lease_sql
from pipmind.safety.azure_identity_sql import AzureDatabaseIdentity
from pipmind.safety.identity_profiles import (
    IdentityProfileRefused,
    IdentityProfileUnavailable,
    decode_identity,
    encode_identity,
    identity_sha256,
    read_expected_identity,
    same_database,
)
from pipmind.safety.incarnation import DatabaseIncarnation, encode_incarnation
from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseRefused, SqlLeaseStore

AZURE = AzureDatabaseIdentity("synthetic-server", "synthetic-app", "opaque physical α")


def test_native_bytes_and_hash_are_exact_legacy_not_relabelled():
    assert encode_identity(APP) == encode_incarnation(APP)
    assert decode_identity(encode_incarnation(APP)) == APP
    assert identity_sha256(APP) == hashlib.sha256(encode_incarnation(APP).encode()).hexdigest()


def test_azure_bytes_and_hash_are_exact_existing_envelope():
    body = azure_identity_sql.encode_identity(AZURE)
    assert encode_identity(AZURE) == body
    assert decode_identity(body) == AZURE
    assert identity_sha256(AZURE) == azure_identity_sql.identity_sha256(AZURE)
    assert json.loads(body)["profile"] == "azure-sql-physical-v1"
    exact = replace(AZURE, logical_server=" Synthetic-Server ", physical_database_name=" α ")
    assert decode_identity(encode_identity(exact)) == exact
    assert identity_sha256(exact) != identity_sha256(AZURE)


@pytest.mark.parametrize("value", [None, False, [], {}, "native-v1", object()])
def test_only_exact_known_dtos_are_valid(value):
    for operation in (encode_identity, identity_sha256):
        with pytest.raises(IdentityProfileRefused, match="^identity_profile_invalid$"):
            operation(value)


def test_subclass_is_not_an_extension_point():
    class AzureSubclass(AzureDatabaseIdentity):
        pass

    value = AzureSubclass(AZURE.logical_server, AZURE.database, AZURE.physical_database_name)
    with pytest.raises(IdentityProfileRefused, match="^identity_profile_invalid$"):
        encode_identity(value)


@pytest.mark.parametrize("dto", [DatabaseIncarnation, AzureDatabaseIdentity])
def test_forged_uninitialized_exact_dto_is_a_fixed_refusal(dto):
    value = object.__new__(dto)
    with pytest.raises(IdentityProfileRefused, match="^identity_profile_invalid$"):
        encode_identity(value)


@pytest.mark.parametrize("body", [None, b"[]", "null", " {}", "[]", "{}", "x" * 4097])
def test_malformed_or_noncanonical_body_is_fixed_refusal(body):
    with pytest.raises(IdentityProfileRefused, match="^identity_profile_invalid$"):
        decode_identity(body)


def test_unknown_profile_duplicate_keys_and_reordered_envelope_are_refused():
    body = encode_identity(AZURE)
    unknown = body.replace("azure-sql-physical-v1", "future-profile")
    duplicate = body[:-1] + ',"profile":"azure-sql-physical-v1"}'
    reordered = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    for candidate in (unknown, duplicate, reordered):
        with pytest.raises(IdentityProfileRefused):
            decode_identity(candidate)


def test_location_comparison_is_conservative_and_mixed_profiles_cannot_prove_separation():
    assert same_database(APP, replace(APP, server=APP.server.upper()))
    assert same_database(AZURE, replace(AZURE, logical_server=AZURE.logical_server.upper()))
    assert same_database(AZURE, replace(AZURE, database="other-db"))
    assert not same_database(
        AZURE, replace(AZURE, database="other-db", physical_database_name="other physical")
    )
    for left, right in ((APP, AZURE), (AZURE, APP)):
        with pytest.raises(IdentityProfileRefused, match="^identity_profile_mixed$"):
            same_database(left, right)


class AzureCursor(Cursor):
    def __init__(self, connection):
        super().__init__(connection)
        self.metadata = None

    def execute(self, sql, params=()):
        self.metadata = None
        db = self.connection.database
        if sql == azure_identity_sql._CONTEXT_SQL:
            db.observations.append((id(self), tuple(self.connection.effects)))
            if db.identity_outage:
                raise OSError("private catalog details")
            self.metadata = [(5, AZURE.logical_server, AZURE.database, 7, 1, 1)]
        elif sql == azure_identity_sql._PHYSICAL_SQL:
            self.metadata = [(7, AZURE.database, self.connection.physical)]
        else:
            return super().execute(sql, params)
        return self

    def fetchone(self):
        if self.metadata is not None:
            return self.metadata.pop(0) if self.metadata else None
        return super().fetchone()


class AzureConnection(Connection):
    def __init__(self, database):
        super().__init__(database)
        self.physical = database.physical

    def cursor(self):
        return AzureCursor(self)


class AzureDatabase(Database):
    def __init__(self):
        super().__init__()
        self.physical = AZURE.physical_database_name
        self.observations = []
        self.identity_outage = False

    def connect(self):
        db = AzureConnection(self)
        self.connections.append(db)
        return db


@pytest.fixture
def azure_owned(monkeypatch):
    def forbidden_native_reader(cur):
        raise AssertionError("an explicit Azure pin must never use the native alias")

    monkeypatch.setattr(lease_sql, "read_incarnation", forbidden_native_reader)
    db = AzureDatabase()
    store = SqlLeaseStore(db.connect, BINDING, "epoch", incarnation=AZURE)
    store.initialize()
    grant = store.acquire("owner", ttl_seconds=30)
    db.observations.clear()
    return db, store, grant


def test_azure_lease_brackets_callback_using_same_cursor_and_acknowledged_commit(azure_owned):
    db, store, grant = azure_owned
    cursors = []

    def operation(cur):
        cursors.append(id(cur))
        cur.execute("SYNTHETIC EFFECT", ("retained",))
        return "accepted"

    assert store.compare_and_act(grant, operation) == "accepted"
    assert [cursor for cursor, _ in db.observations] == cursors * 4
    assert [effects for _, effects in db.observations] == [(), (), ("retained",), ("retained",)]
    assert db.effects == ["retained"] and db.connections[-1].committed


@pytest.mark.parametrize("phase", ["before", "after"])
def test_azure_pin_change_never_commits_or_adopts_and_closes_transaction(azure_owned, phase):
    db, store, grant = azure_owned
    if phase == "before":
        db.physical = "different physical"
    calls = []

    def operation(cur):
        calls.append(True)
        cur.execute("SYNTHETIC EFFECT", ("must rollback",))
        cur.connection.physical = "different physical"

    with pytest.raises(LeaseRefused, match="^lease_incarnation_unavailable$"):
        store.compare_and_act(grant, operation)
    assert calls == ([] if phase == "before" else [True])
    assert store.incarnation == AZURE
    assert db.effects == [] and not db.connections[-1].committed and db.connections[-1].closed


def test_azure_metadata_outage_has_fixed_error_and_no_callback(azure_owned):
    db, store, grant = azure_owned
    db.identity_outage = True
    called = []
    with pytest.raises(LeaseRefused, match="^lease_incarnation_unavailable$"):
        store.compare_and_act(grant, lambda cur: called.append(True))
    assert not called and not db.effects and db.connections[-1].closed


def test_callback_transaction_mode_change_is_refused_and_effects_rolled_back(azure_owned):
    db, store, grant = azure_owned

    def operation(cur):
        cur.execute("SYNTHETIC EFFECT", ("must rollback",))
        cur.connection.autocommit = True

    with pytest.raises(LeaseRefused, match="^lease_incarnation_unavailable$"):
        store.compare_and_act(grant, operation)
    assert not db.effects and not db.connections[-1].committed and db.connections[-1].closed


@pytest.mark.parametrize("phase", ["before", "after"])
def test_azure_identity_success_cannot_mask_commit_uncertainty(azure_owned, phase):
    db, store, grant = azure_owned
    db.commit_failure = phase
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.compare_and_act(grant, lambda cur: cur.execute("SYNTHETIC EFFECT", ("uncertain",)))
    assert db.effects == ([] if phase == "before" else ["uncertain"])
    assert db.connections[-1].closed


def test_fixed_profile_reader_keeps_native_alias_separate_and_normalizes_failures(monkeypatch):
    sentinel = object()
    seen = []
    monkeypatch.setattr(
        identity_profiles.incarnation, "read_incarnation", lambda cur: seen.append(cur) or APP
    )
    assert read_expected_identity(sentinel, APP) == APP and seen == [sentinel]
    monkeypatch.setattr(identity_profiles.incarnation, "read_incarnation", lambda cur: AZURE)
    with pytest.raises(IdentityProfileRefused, match="^identity_profile_mismatch$"):
        read_expected_identity(sentinel, APP)

    def unavailable(cur):
        raise identity_profiles.incarnation.IncarnationUnavailable("private details")

    monkeypatch.setattr(identity_profiles.incarnation, "read_incarnation", unavailable)
    with pytest.raises(IdentityProfileUnavailable, match="^identity_profile_unavailable$"):
        read_expected_identity(sentinel, APP)
