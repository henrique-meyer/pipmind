"""Scripted Azure recovery composition only; no actual Azure qualification."""

from contextlib import contextmanager
from dataclasses import replace

import pytest
from test_safety_authority import APP
from test_safety_recovery_flow import (
    _command_inventory,
    fake_reconcile,
    no_sql,
)
from test_safety_recovery_flow import context as context

from pipmind.safety import recovery_flow as flow
from pipmind.safety.admission_sql import SqlAdmissionStore
from pipmind.safety.authority_sql import AuthorityCommitUnknown, SqlAuthorityStore
from pipmind.safety.azure_identity_sql import AzureDatabaseIdentity
from pipmind.safety.checkpoint_sql import CheckpointUnavailable, NativeBackupReference
from pipmind.safety.identity_profiles import identity_sha256
from pipmind.safety.integration_sql import SqlSafetyGateway
from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseRefused, SqlLeaseStore
from pipmind.safety.managed_restore import AzurePitrReference
from pipmind.safety.recovery_sql import RecoveryCommitUnknown, SqlRecoveryStore

SOURCE = AzureDatabaseIdentity("synthetic-source", "source-db", "physical-source")
TARGET = AzureDatabaseIdentity("synthetic-target", "target-db", "physical-target")
AUTH = AzureDatabaseIdentity("synthetic-authority", "authority-db", "physical-authority")
PROFILE = "restricted-command-catalog-v1"


def azure_context(c):
    # The fixtures below prove orchestration only, not SQL metadata or Azure APIs.
    c = dict(c)
    old = c["leases"]
    c["expected"] = replace(
        c["expected"],
        application_incarnation=TARGET,
        source_incarnation=SOURCE,
        source_epoch="d" * 64,
    )
    c["leases"] = SqlLeaseStore(no_sql, old.binding, old.authority_epoch, incarnation=TARGET)
    c["authority"] = SqlAuthorityStore(no_sql, old.binding, AUTH)
    original = c["gateway"]
    prior = original.admission
    admission = SqlAdmissionStore(no_sql, c["leases"], prior._sessions, prior._policies)
    c["gateway"] = SqlSafetyGateway(
        admission,
        original.controls,
        original._symbols,
        base_units_per_size=original._unit_factors,
    )
    c["record"]["plan"] = replace(
        c["record"]["plan"], application_incarnation=flow.incarnation_sha256(TARGET)
    )
    return c


def coordinator(c, profile=PROFILE):
    return flow.DryRecoveryCoordinator(
        c["authority"], c["expected"], c["leases"], c["recovery"], inventory_profile=profile
    )


def reference(c):
    return AzurePitrReference(
        "synthetic-restore",
        SOURCE,
        TARGET,
        "2026-10-21T12:00:00Z",
        "2026-10-21T13:00:00Z",
        c["inventory"].sha256,
        "a" * 64,
        "b" * 64,
    )


def install_restricted_capture(c, monkeypatch):
    calls = []

    def capture(cur, binding, lease_binding, *, expected_database):
        assert c["open_guard"] and cur == "synthetic-cursor"
        assert binding == c["recovery"].binding and lease_binding == c["leases"].binding
        assert expected_database == c["leases"].incarnation.database
        calls.append((cur, expected_database))
        return c["inventory"]

    def default_forbidden(*args):
        pytest.fail("explicit restricted profile must never fall back to dbo capture")

    monkeypatch.setattr(flow, "capture_complete_restricted_in_transaction", capture)
    monkeypatch.setattr(flow, "capture_in_transaction", default_forbidden)
    return calls


@pytest.mark.parametrize("profile", [None, True, 1, {}, object(), "", "restricted", str])
def test_unknown_inventory_profile_is_refused_before_sql_or_guard(context, profile):
    with pytest.raises(LeaseRefused, match="^recovery_invalid_inventory_profile$"):
        coordinator(context, profile)
    assert not context["events"]


def test_native_hash_and_default_are_exact_and_azure_hash_keeps_explicit_profile(context):
    import hashlib

    from pipmind.safety.incarnation import encode_incarnation

    assert (
        flow.incarnation_sha256(APP)
        == hashlib.sha256(encode_incarnation(APP).encode("ascii")).hexdigest()
    )
    assert flow.incarnation_sha256(TARGET) == identity_sha256(TARGET)
    assert flow.incarnation_sha256(SOURCE) != flow.incarnation_sha256(TARGET)
    native = flow.DryRecoveryCoordinator(
        context["authority"], context["expected"], context["leases"], context["recovery"]
    )
    assert native.inventory_profile == "native-dbo-v1"
    with pytest.raises(AttributeError):
        native.inventory_profile = PROFILE


def test_restricted_choice_is_used_for_capture_prepare_and_confirm(context, monkeypatch):
    c = azure_context(context)
    calls = install_restricted_capture(c, monkeypatch)
    current = coordinator(c)
    assert current.capture("grant") == c["inventory"]
    planned = []

    def create(self, *args):
        planned.append(args)
        return c["record"]

    monkeypatch.setattr(SqlRecoveryStore, "create_plan", create)
    result = current.prepare(
        "grant",
        plan_id="plan",
        actor_id="owner",
        expected_generation=0,
        target_robot=c["assignment"].robot,
        reason="explicit",
        position_obligation_ids=(),
    )
    assert result["inventory"] == c["inventory"]
    assert planned[0][6] == identity_sha256(TARGET)
    confirmed = []
    monkeypatch.setattr(
        SqlRecoveryStore, "confirm_in_transaction", lambda *args, **kw: confirmed.append(kw)
    )
    current.confirm("grant", "plan", "d" * 64)
    assert confirmed[0]["checkpoint_sha256"] == c["inventory"].sha256
    assert len(calls) == 3


def test_managed_verification_requires_actual_full_inventory_after_receipt_bindings(
    context, monkeypatch
):
    c = azure_context(context)
    calls = install_restricted_capture(c, monkeypatch)
    monkeypatch.setattr(SqlLeaseStore, "_run", lambda self, op: op("synthetic-cursor"))
    result = coordinator(c).verify_managed_restored(
        c["inventory"],
        reference(c),
        expected_source_identity=SOURCE,
        management_receipt_sha256="a" * 64,
        history_receipt_sha256="b" * 64,
    )
    assert result == c["inventory"] and len(calls) == 1
    assert not any(isinstance(e, tuple) and e[0] == "activate" for e in c["events"])


@pytest.mark.parametrize("field", ["management_receipt_sha256", "history_receipt_sha256"])
@pytest.mark.parametrize("value", [None, True, "c" * 64])
def test_receipt_substitution_cannot_reach_target_sql(context, monkeypatch, field, value):
    c = azure_context(context)
    calls = install_restricted_capture(c, monkeypatch)
    kwargs = dict(
        expected_source_identity=SOURCE,
        management_receipt_sha256="a" * 64,
        history_receipt_sha256="b" * 64,
    )
    kwargs[field] = value
    with pytest.raises(LeaseRefused, match="^recovery_managed_receipt_mismatch$"):
        coordinator(c).verify_managed_restored(c["inventory"], reference(c), **kwargs)
    assert not calls and not c["events"]


@pytest.mark.parametrize(
    "substitution", ["source", "target", "inventory", "lineage", "absent-lineage"]
)
def test_source_target_inventory_or_lineage_substitution_refuses(
    context, monkeypatch, substitution
):
    c = azure_context(context)
    calls = install_restricted_capture(c, monkeypatch)
    ref = reference(c)
    if substitution == "source":
        ref = replace(ref, source_identity=replace(SOURCE, physical_database_name="other-source"))
    elif substitution == "target":
        ref = replace(ref, target_identity=replace(TARGET, physical_database_name="other-target"))
    elif substitution == "inventory":
        ref = replace(ref, source_inventory_sha256="f" * 64)
    else:
        c["expected"] = replace(
            c["expected"], source_incarnation=None if substitution == "absent-lineage" else TARGET
        )
    with pytest.raises(LeaseRefused):
        coordinator(c).verify_managed_restored(
            c["inventory"],
            ref,
            expected_source_identity=SOURCE,
            management_receipt_sha256="a" * 64,
            history_receipt_sha256="b" * 64,
        )
    assert not calls and not c["events"]


def test_matching_receipt_sidecar_cannot_hide_different_target_inventory(context, monkeypatch):
    c = azure_context(context)
    ref = reference(c)
    changed = _command_inventory(c, (1, 0, 0, 1, 1))
    monkeypatch.setattr(
        flow, "capture_complete_restricted_in_transaction", lambda *a, **kw: changed
    )
    monkeypatch.setattr(SqlLeaseStore, "_run", lambda self, op: op("synthetic-cursor"))
    with pytest.raises(LeaseRefused, match="^recovery_restored_state_mismatch$"):
        coordinator(c).verify_managed_restored(
            c["inventory"],
            ref,
            expected_source_identity=SOURCE,
            management_receipt_sha256="a" * 64,
            history_receipt_sha256="b" * 64,
        )


def test_native_backup_route_refuses_azure_or_managed_references(context):
    c = azure_context(context)
    native = NativeBackupReference(
        "native", "a" * 64, "2026-10-21T12:00:00Z", c["inventory"].sha256
    )
    for ref in (native, reference(c)):
        with pytest.raises(LeaseRefused, match="^recovery_invalid_checkpoint$"):
            coordinator(c).verify_restored(c["inventory"], ref)
    with pytest.raises(LeaseRefused, match="^recovery_invalid_managed_checkpoint$"):
        coordinator(context).verify_managed_restored(
            context["inventory"],
            reference(c),
            expected_source_identity=SOURCE,
            management_receipt_sha256="a" * 64,
            history_receipt_sha256="b" * 64,
        )
    assert not context["events"]


@pytest.mark.parametrize("counts", [(0, 0, 0, 0, 0), (1, 1, 0, 0, 0), (1, 0, 0, 1, 1)])
def test_activation_wrapper_inherits_restricted_profile_and_never_omits_unknowns(
    context, monkeypatch, counts
):
    c = azure_context(context)
    c["inventory"] = _command_inventory(c, counts)
    calls = install_restricted_capture(c, monkeypatch)
    fake_reconcile(c, monkeypatch)
    if counts[-1]:
        with pytest.raises(LeaseRefused, match="^recovery_command_obligations_pending$"):
            coordinator(c).activate_dry(
                c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
            )
        assert not any(isinstance(e, tuple) and e[0] == "activate" for e in c["events"])
    else:
        result = coordinator(c).activate_dry(
            c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
        )
        assert not result["dispatch_authorized"] and not result["engine_resumed"]
        at = next(
            i for i, e in enumerate(c["events"]) if isinstance(e, tuple) and e[0] == "activate"
        )
        assert c["events"][at - 1] == "main_ack"
    assert len(calls) == 1


def test_restricted_failure_and_uncertain_plan_never_fall_back_or_activate(context, monkeypatch):
    c = azure_context(context)

    def unavailable(*a, **kw):
        raise CheckpointUnavailable("safety_inventory_unavailable")

    monkeypatch.setattr(flow, "capture_complete_restricted_in_transaction", unavailable)
    with pytest.raises(CheckpointUnavailable):
        coordinator(c).capture("grant")
    install_restricted_capture(c, monkeypatch)

    def uncertain(*a, **kw):
        raise RecoveryCommitUnknown("recovery_commit_unknown")

    monkeypatch.setattr(SqlRecoveryStore, "create_plan", uncertain)
    with pytest.raises(LeaseCommitUnknown, match="^recovery_plan_commit_unknown$"):
        coordinator(c).prepare(
            "grant",
            plan_id="plan",
            actor_id="owner",
            expected_generation=0,
            target_robot=c["assignment"].robot,
            reason="explicit",
            position_obligation_ids=(),
        )
    assert not any(isinstance(e, tuple) and e[0] == "activate" for e in c["events"])


def test_azure_rebind_requires_acknowledged_target_and_does_not_invent_source_barrier(
    context, monkeypatch
):
    from copy import deepcopy

    from test_safety_identity_profiles import AZURE as LEASE_PIN
    from test_safety_identity_profiles import AzureDatabase

    from pipmind.safety import lease_sql

    c = azure_context(context)
    expected = replace(c["expected"], application_incarnation=LEASE_PIN)
    db, events, barriers = AzureDatabase(), [], []
    original_connect = db.connect

    class Cursor:
        def __init__(self, inner):
            self.inner = inner
            self.result = None

        def execute(self, query, params=()):
            self.result = None
            if query.startswith("SELECT authority_epoch,generation"):
                row = self.inner.connection.rows.get(params[0])
                self.result = (row[3], row[4])
            elif query.startswith("UPDATE dbo.pm_safety_leases_v1 SET authority_epoch"):
                epoch, generation, key = params
                self.inner.connection.rows[key][3:] = [epoch, generation, None, None, None]
            else:
                self.inner.execute(query, params)
            return self

        def fetchone(self):
            return self.result if self.result is not None else self.inner.fetchone()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    def connect():
        connection = original_connect()
        cursor, commit = connection.cursor, connection.commit
        connection.cursor = lambda: Cursor(cursor())

        def acknowledged():
            commit()
            events.append("application_ack")

        connection.commit = acknowledged
        return connection

    class Guard:
        def record_application_barrier(self, pin, generation):
            assert events[-1] == "application_ack"
            assert pin == LEASE_PIN and generation > 0
            barriers.append((pin, generation))

    fail_authority_ack = []

    @contextmanager
    def guard(self, state, *, mode="active"):
        assert state is expected and mode == "held"
        yield Guard()
        if fail_authority_ack:
            raise AuthorityCommitUnknown("authority_commit_unknown")
        events.append("authority_ack")

    def native_forbidden(cur):
        pytest.fail("explicit Azure rebind cannot use native identity fallback")

    monkeypatch.setattr(SqlAuthorityStore, "guard", guard)
    monkeypatch.setattr(lease_sql, "read_incarnation", native_forbidden)
    old = SqlLeaseStore(connect, c["leases"].binding, "old-epoch", incarnation=LEASE_PIN)
    old.initialize()
    old_grant = old.acquire("old-owner", ttl_seconds=30)
    events.clear()
    result = flow.rebind_held(c["authority"], expected, connect, "old-epoch")
    assert result == {"epoch": expected.epoch, "generation": old_grant.generation + 1, "held": True}
    assert events == ["application_ack", "authority_ack"]
    assert barriers == [(LEASE_PIN, result["generation"])]
    assert all(pin != SOURCE for pin, _ in barriers)
    assert next(iter(db.rows.values()))[5:] == [None, None, None]
    exact = deepcopy(db.rows)
    db.commit_failure = "after"
    with pytest.raises(LeaseCommitUnknown):
        flow.rebind_held(c["authority"], expected, connect, "old-epoch")
    assert len(barriers) == 1 and db.rows == exact
    db.commit_failure = None
    fail_authority_ack.append(True)
    with pytest.raises(AuthorityCommitUnknown):
        flow.rebind_held(c["authority"], expected, connect, "old-epoch")
    assert db.rows == exact and len(barriers) == 2
