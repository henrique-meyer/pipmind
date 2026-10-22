"""Native identity checks around synthetic SQL effects, never a real connector."""

from dataclasses import replace

import pytest
from test_safety_authority import APP
from test_safety_lease import BINDING, Database

from pipmind.safety import lease_sql
from pipmind.safety.incarnation import IncarnationRefused, IncarnationUnavailable
from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseRefused, SqlLeaseStore


@pytest.fixture
def pinned(monkeypatch):
    db = Database()
    monkeypatch.setattr(lease_sql, "read_incarnation", lambda cur: APP)
    store = SqlLeaseStore(db.connect, BINDING, "epoch", incarnation=APP)
    store.initialize()
    grant = store.acquire("owner", ttl_seconds=30)
    return db, store, grant


def test_native_pin_checked_before_and_after_callback(pinned, monkeypatch):
    db, store, grant = pinned
    seen = []

    def native(cur):
        seen.append(tuple(cur.connection.effects))
        return APP

    monkeypatch.setattr(lease_sql, "read_incarnation", native)
    store.compare_and_act(grant, lambda cur: cur.execute("SYNTHETIC EFFECT", ("kept",)))
    assert seen == [(), ("kept",)] and db.effects == ["kept"]


@pytest.mark.parametrize("phase", ["before", "after"])
def test_changed_native_fork_refuses_without_committing_effect(pinned, monkeypatch, phase):
    db, store, grant = pinned
    changed = replace(APP, recovery_fork_guid="55555555-5555-4555-8555-555555555555")
    responses = iter([changed] if phase == "before" else [APP, changed])
    monkeypatch.setattr(lease_sql, "read_incarnation", lambda cur: next(responses))
    called = []

    def operation(cur):
        called.append(True)
        cur.execute("SYNTHETIC EFFECT", ("must roll back",))

    with pytest.raises(LeaseRefused, match="^lease_incarnation_mismatch$"):
        store.compare_and_act(grant, operation)
    assert called == ([] if phase == "before" else [True])
    assert db.effects == [] and db.connections[-1].closed


@pytest.mark.parametrize("error", [IncarnationRefused, IncarnationUnavailable])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_native_identity_unavailable_or_unsupported_is_fixed_refusal(
    pinned, monkeypatch, error, phase
):
    db, store, grant = pinned
    calls = []

    def native(cur):
        calls.append(True)
        if phase == "after" and len(calls) == 1:
            return APP
        raise error("private native metadata details")

    monkeypatch.setattr(lease_sql, "read_incarnation", native)
    with pytest.raises(LeaseRefused, match="^lease_incarnation_unavailable$"):
        store.compare_and_act(grant, lambda cur: cur.execute("SYNTHETIC EFFECT", ("uncommitted",)))
    assert db.effects == [] and db.connections[-1].closed


@pytest.mark.parametrize("failure", ["before", "after"])
def test_pin_success_does_not_mask_commit_unknown(pinned, failure):
    db, store, grant = pinned
    db.commit_failure = failure
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.compare_and_act(grant, lambda cur: cur.execute("SYNTHETIC EFFECT", ("uncertain",)))
    assert db.effects == ([] if failure == "before" else ["uncertain"])
    assert db.connections[-1].closed


def test_legacy_unpinned_store_never_reads_native_identity(monkeypatch):
    db = Database()

    def unexpected(cur):
        raise AssertionError("legacy SQL lease must remain independently usable")

    monkeypatch.setattr(lease_sql, "read_incarnation", unexpected)
    store = SqlLeaseStore(db.connect, BINDING, "epoch")
    store.initialize()
    assert store.acquire("owner", ttl_seconds=30).generation == 1
