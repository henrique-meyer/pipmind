"""Recovery composition with real trusted constructors and offline method doubles."""

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256

import pytest
from test_safety_authority import APP, AUTH
from test_safety_integration import make_gateway
from test_safety_lease import Database as LeaseDatabase

from pipmind.safety import authority_sql, lease_sql, recovery_flow
from pipmind.safety.admission_sql import SqlAdmissionStore
from pipmind.safety.attribution_sql import AttributionLink, SqlAttributionStore
from pipmind.safety.authority_sql import AuthorityCommitUnknown, AuthorityState, SqlAuthorityStore
from pipmind.safety.checkpoint_sql import (
    COVERED_TABLES,
    TABLE_VERSION,
    Inventory,
    NativeBackupReference,
    TableDigest,
    _inventory_hash,
)
from pipmind.safety.checkpoint_sql import (
    complete_required_locks as required_locks,
)
from pipmind.safety.integration_sql import SqlSafetyGateway
from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseRefused, SqlLeaseStore
from pipmind.safety.recovery_flow import DryRecoveryCoordinator, RecoveryAwareDryGateway
from pipmind.safety.recovery_sql import (
    Assignment,
    RecoveryCommitUnknown,
    RecoveryPlan,
    RecoveryRefused,
    RecoveryState,
    RobotReference,
    SqlRecoveryStore,
)

REAL_COMPARE_AND_ACT = SqlLeaseStore.compare_and_act


def no_sql():
    raise AssertionError("offline composition must not connect")


@pytest.fixture
def context(monkeypatch):
    gateway, request, _ = make_gateway()
    original = gateway.admission
    leases = SqlLeaseStore(no_sql, original.leases.binding, "e" * 64, incarnation=APP)
    admission = SqlAdmissionStore(no_sql, leases, original._sessions, original._policies)
    gateway = SqlSafetyGateway(
        admission, gateway.controls, gateway._symbols, base_units_per_size=gateway._unit_factors
    )
    binding = admission.binding
    authority = SqlAuthorityStore(no_sql, leases.binding, AUTH)
    expected = AuthorityState(leases.binding, 1, "e" * 64, "held", None, 0, None, APP)
    recovery = SqlRecoveryStore(no_sql, binding)
    attribution = SqlAttributionStore(no_sql, binding)
    robot = RobotReference("robot", "v1", "a" * 64)
    assignment = Assignment(
        binding, 1, "plan", robot, expected.epoch, "b" * 64, "2026-10-21T12:00:00Z"
    )
    tables = tuple(TableDigest(name, 0, sha256(b"").hexdigest()) for name in COVERED_TABLES)
    inventory = Inventory(
        binding,
        leases.binding.resource,
        TABLE_VERSION,
        tables,
        _inventory_hash(binding, leases.binding.resource, tables),
    )
    events = []
    open_guard = []

    class Guard:
        def record_application_barrier(self, pin, generation):
            assert open_guard and events[-1] == "main_ack"
            assert pin == APP and type(generation) is int and generation > 0
            events.append(("application_barrier", pin, generation))

        def activate(self, *args):
            assert open_guard and "main_ack" in events
            events.append(("activate", args))
            return replace(
                expected,
                mode="active",
                assignment_id=assignment.assignment_id,
                assignment_generation=assignment.generation,
                robot_sha256=robot.content_sha256,
            )

    @contextmanager
    def guard(self, state, *, mode="active"):
        assert state.mode == mode and not open_guard
        events.append(("guard_enter", mode))
        open_guard.append(True)
        try:
            yield Guard()
        finally:
            open_guard.pop()
            events.append("guard_exit")

    def compare(self, grant, callback):
        assert open_guard
        result = callback("synthetic-cursor")
        events.append("main_ack")
        return result

    monkeypatch.setattr(SqlAuthorityStore, "guard", guard)
    monkeypatch.setattr(SqlLeaseStore, "compare_and_act", compare)
    monkeypatch.setattr(
        recovery_flow, "_lock", lambda cur, resource: events.append(("lock", resource))
    )
    monkeypatch.setattr(recovery_flow, "capture_in_transaction", lambda *args: inventory)
    monkeypatch.setattr(
        SqlRecoveryStore,
        "read_in_transaction",
        lambda *args: RecoveryState(binding, 1, assignment, True),
    )
    plan = RecoveryPlan(
        binding,
        "plan",
        "owner",
        0,
        inventory.sha256,
        robot,
        expected.epoch,
        recovery_flow.incarnation_sha256(APP),
        "explicit owner plan",
        (),
    )
    record = {"plan": plan, "digest": "d" * 64, "created_at": "2026-10-21T12:00:00Z"}
    monkeypatch.setattr(SqlRecoveryStore, "plan_in_transaction", lambda *args: record)
    return dict(
        authority=authority,
        expected=expected,
        leases=leases,
        recovery=recovery,
        gateway=gateway,
        attribution=attribution,
        assignment=assignment,
        request=request,
        inventory=inventory,
        events=events,
        open_guard=open_guard,
        record=record,
    )


def coordinator(c):
    return DryRecoveryCoordinator(c["authority"], c["expected"], c["leases"], c["recovery"])


def test_scope_requires_native_pin_and_exact_account_environment(context):
    c = context
    unpinned = SqlLeaseStore(no_sql, c["leases"].binding, c["expected"].epoch)
    with pytest.raises(LeaseRefused, match="^recovery_scope_mismatch$"):
        DryRecoveryCoordinator(c["authority"], c["expected"], unpinned, c["recovery"])
    wrong = replace(c["recovery"].binding, environment="live")
    with pytest.raises(LeaseRefused, match="^recovery_scope_mismatch$"):
        DryRecoveryCoordinator(
            c["authority"], c["expected"], c["leases"], SqlRecoveryStore(no_sql, wrong)
        )
    with pytest.raises(LeaseRefused, match="^recovery_invalid_stores$"):
        DryRecoveryCoordinator(object(), c["expected"], c["leases"], c["recovery"])


def test_active_authority_cannot_prepare_held_recovery(context):
    c = context
    active = replace(
        c["expected"],
        mode="active",
        assignment_id="plan",
        assignment_generation=1,
        robot_sha256=c["assignment"].robot.content_sha256,
    )
    with pytest.raises(LeaseRefused, match="^recovery_hold_required$"):
        DryRecoveryCoordinator(c["authority"], active, c["leases"], c["recovery"])


@pytest.mark.parametrize("generation", [0, True])
def test_prepare_never_recreates_missing_assignment_history_below_external_highwater(
    context, generation
):
    c = context
    c["expected"] = replace(c["expected"], assignment_generation=7)
    with pytest.raises(LeaseRefused, match="^recovery_missing_assignment_history$"):
        coordinator(c).prepare(
            "grant",
            plan_id="new",
            actor_id="owner",
            expected_generation=generation,
            target_robot=c["assignment"].robot,
            reason="explicit",
            position_obligation_ids=(),
        )
    assert not c["events"]


def test_capture_uses_all_ordered_locks_and_full_inventory(context):
    c = context
    assert coordinator(c).capture("grant") is c["inventory"]
    assert [item[1] for item in c["events"] if type(item) is tuple and item[0] == "lock"] == list(
        required_locks(c["recovery"].binding, c["leases"].binding)
    )
    assert c["events"][-2:] == ["main_ack", "guard_exit"]


def _command_inventory(c, values):
    from pipmind.safety.checkpoint_sql import (
        COMMAND_TABLES,
        TABLE_VERSION_COMMANDS,
        CommandCounts,
    )

    counts = CommandCounts(*values)
    command_tables = tuple(
        TableDigest(name, count, sha256(f"{name}:{count}".encode()).hexdigest())
        for name, count in zip(
            COMMAND_TABLES,
            (int(counts.commands > 0), counts.commands, counts.attempted, counts.unknown),
            strict=True,
        )
    )
    old = c["inventory"]
    tables = old.tables + command_tables
    return Inventory(
        old.binding,
        old.resource,
        TABLE_VERSION_COMMANDS,
        tables,
        _inventory_hash(
            old.binding,
            old.resource,
            tables,
            table_version=TABLE_VERSION_COMMANDS,
            command_counts=counts,
        ),
        counts,
    )


@pytest.mark.parametrize("counts", [(1, 0, 1, 0, 0), (1, 0, 0, 1, 0), (1, 0, 0, 1, 1)])
def test_current_command_obligations_refuse_before_reconcile_or_activation(
    context, monkeypatch, counts
):
    c = context
    inventory = _command_inventory(c, counts)
    monkeypatch.setattr(recovery_flow, "capture_in_transaction", lambda *args: inventory)
    fake_reconcile(c, monkeypatch)

    def forbidden_attribution(*args):
        pytest.fail("pending command must fail before any attribution write")

    monkeypatch.setattr(SqlAttributionStore, "record_in_transaction", forbidden_attribution)
    with pytest.raises(LeaseRefused, match="^recovery_command_obligations_pending$"):
        coordinator(c).activate_dry(
            c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
        )
    assert c["events"].count("main_ack") == 1  # The earlier obligations read only.
    assert not any(type(event) is tuple and event[0] == "activate" for event in c["events"])


@pytest.mark.parametrize("counts", [(0, 0, 0, 0, 0), (1, 1, 0, 0, 0)])
def test_empty_or_refused_only_complete_command_profile_can_activate_dry(
    context, monkeypatch, counts
):
    c = context
    inventory = _command_inventory(c, counts)
    monkeypatch.setattr(recovery_flow, "capture_in_transaction", lambda *args: inventory)
    fake_reconcile(c, monkeypatch)
    result = coordinator(c).activate_dry(
        c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
    )
    assert result["authority"].mode == "active"
    assert result["dispatch_authorized"] is False


def test_incomplete_command_profile_cannot_activate_or_write_attribution(context, monkeypatch):
    from pipmind.safety.checkpoint_sql import CheckpointUnavailable

    c = context
    fake_reconcile(c, monkeypatch)

    def incomplete(*args):
        raise CheckpointUnavailable("safety_inventory_unavailable")

    monkeypatch.setattr(recovery_flow, "capture_in_transaction", incomplete)
    with pytest.raises(CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        coordinator(c).activate_dry(
            c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
        )
    assert c["events"].count("main_ack") == 1
    assert not any(type(event) is tuple and event[0] == "activate" for event in c["events"])


def test_prepare_binds_inventory_native_incarnation_and_exact_owner_obligations(
    context, monkeypatch
):
    c, calls = context, []

    def create(self, *args):
        assert c["open_guard"] and c["events"][-1] == "main_ack"
        calls.append(args)
        return c["record"]

    monkeypatch.setattr(SqlRecoveryStore, "create_plan", create)
    result = coordinator(c).prepare(
        "grant",
        plan_id="plan",
        actor_id="owner",
        expected_generation=0,
        target_robot=c["assignment"].robot,
        reason="why",
        position_obligation_ids=("long", "short"),
    )
    assert result["inventory"] is c["inventory"]
    args = calls[0]
    assert args[:4] == ("plan", "owner", 0, c["inventory"].sha256)
    assert args[5:9] == (
        c["expected"].epoch,
        recovery_flow.incarnation_sha256(APP),
        "why",
        ("long", "short"),
    )


def test_confirm_forwards_current_full_inventory_and_never_activates(context, monkeypatch):
    c, calls = context, []

    def confirm(self, cur, plan_id, digest, **facts):
        assert c["open_guard"]
        calls.append((cur, plan_id, digest, facts))
        return {"held": True, "assignment": "retained-evidence"}

    monkeypatch.setattr(SqlRecoveryStore, "confirm_in_transaction", confirm)
    result = coordinator(c).confirm("grant", "plan", "d" * 64)
    assert result["held"] and calls[0][0:3] == ("synthetic-cursor", "plan", "d" * 64)
    assert calls[0][3] == dict(
        epoch=c["expected"].epoch, checkpoint_sha256=c["inventory"].sha256, obligation_ids=()
    )
    assert not any(type(item) is tuple and item[0] == "activate" for item in c["events"])


def test_confirm_changed_inventory_refusal_has_no_acknowledged_result(context, monkeypatch):
    c = context

    def confirm(self, cur, plan_id, digest, **facts):
        assert facts["checkpoint_sha256"] != "f" * 64
        raise RecoveryRefused("recovery_checkpoint_mismatch")

    monkeypatch.setattr(SqlRecoveryStore, "confirm_in_transaction", confirm)
    with pytest.raises(RecoveryRefused, match="^recovery_checkpoint_mismatch$"):
        coordinator(c).confirm("grant", "plan", "d" * 64)
    assert "main_ack" not in c["events"]


def test_confirm_rejects_plan_from_different_native_incarnation(context):
    c = context
    c["record"]["plan"] = replace(c["record"]["plan"], application_incarnation="f" * 64)
    with pytest.raises(LeaseRefused, match="^recovery_plan_incarnation_mismatch$"):
        coordinator(c).confirm("grant", "plan", "d" * 64)
    assert "main_ack" not in c["events"]


def fake_reconcile(c, monkeypatch, outcome="dry_ready", failure=None):
    def reconcile(self, grant, request):
        assert c["open_guard"]
        current = self._before("synthetic-cursor", grant)
        receipt = {"outcome": outcome, "dispatch_authorized": False}
        result = self._after("synthetic-cursor", current, receipt)
        if failure:
            raise failure("fixed_uncertain")
        c["events"].append("main_ack")
        return result

    def link(self, cur, assignment, receipt):
        assert assignment == c["assignment"] and c["open_guard"]
        return AttributionLink(
            self.binding,
            "reconcile",
            "receipt",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            assignment,
            "2026-10-21T12:00:00Z",
        )

    monkeypatch.setattr(SqlSafetyGateway, "reconcile", reconcile)
    monkeypatch.setattr(SqlAttributionStore, "record_in_transaction", link)


def test_explicit_activation_follows_acknowledged_fresh_dry_reconciliation(context, monkeypatch):
    c = context
    fake_reconcile(c, monkeypatch)
    result = coordinator(c).activate_dry(
        c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
    )
    assert result["authority"].mode == "active"
    assert result["dispatch_authorized"] is False and result["engine_resumed"] is False
    activation = next(
        i for i, event in enumerate(c["events"]) if type(event) is tuple and event[0] == "activate"
    )
    assert c["events"][activation - 1] == "main_ack"
    assert result["reconciliation"]["attribution"]["assignment"]["assignment_id"] == "plan"


def test_activation_request_is_frozen_before_first_guarded_read(context, monkeypatch):
    c = context
    original_id = c["request"].order["client_order_id"]
    compare = SqlLeaseStore.compare_and_act

    def mutate(self, grant, callback):
        result = compare(self, grant, callback)
        c["request"].order["client_order_id"] = "caller-mutation"
        return result

    monkeypatch.setattr(SqlLeaseStore, "compare_and_act", mutate)
    fake_reconcile(c, monkeypatch)
    reconcile = SqlSafetyGateway.reconcile

    def inspect(self, grant, request):
        assert request is not c["request"]
        assert request.order["client_order_id"] == original_id
        return reconcile(self, grant, request)

    monkeypatch.setattr(SqlSafetyGateway, "reconcile", inspect)
    assert (
        coordinator(c)
        .activate_dry(c["gateway"], c["attribution"], "grant", c["assignment"], c["request"])[
            "authority"
        ]
        .mode
        == "active"
    )


@pytest.mark.parametrize("failure", [LeaseCommitUnknown, AuthorityCommitUnknown])
def test_uncertain_reconciliation_never_calls_authority_activation(context, monkeypatch, failure):
    c = context
    fake_reconcile(c, monkeypatch, failure=failure)
    with pytest.raises(failure, match="^fixed_uncertain$"):
        coordinator(c).activate_dry(
            c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
        )
    assert not any(type(item) is tuple and item[0] == "activate" for item in c["events"])


def test_missing_obligation_identity_or_nonready_reconciliation_never_activates(
    context, monkeypatch
):
    c = context
    c["record"]["plan"] = replace(c["record"]["plan"], position_obligation_ids=("missing",))
    with pytest.raises(LeaseRefused, match="^recovery_obligations_changed$"):
        coordinator(c).activate_dry(
            c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
        )
    c["record"]["plan"] = replace(c["record"]["plan"], position_obligation_ids=())
    fake_reconcile(c, monkeypatch, outcome="reconciliation_held")
    with pytest.raises(LeaseRefused, match="^recovery_reconciliation_required$"):
        coordinator(c).activate_dry(
            c["gateway"], c["attribution"], "grant", c["assignment"], c["request"]
        )
    assert not any(type(item) is tuple and item[0] == "activate" for item in c["events"])


def test_held_gateway_cannot_be_used_as_active_routing(context):
    c = context
    wrapped = RecoveryAwareDryGateway(
        c["authority"],
        c["expected"],
        c["gateway"],
        c["recovery"],
        c["attribution"],
        c["assignment"],
        _held=True,
    )
    with pytest.raises(LeaseRefused, match="^recovery_hold_required$"):
        wrapped.reconcile("grant", c["request"])
    assert not c["events"]


def test_active_gateway_rejects_changed_assignment_authority(context, monkeypatch):
    c = context
    active = replace(
        c["expected"],
        mode="active",
        assignment_id="different",
        assignment_generation=1,
        robot_sha256=c["assignment"].robot.content_sha256,
    )
    fake_reconcile(c, monkeypatch)
    wrapped = RecoveryAwareDryGateway(
        c["authority"], active, c["gateway"], c["recovery"], c["attribution"], c["assignment"]
    )
    with pytest.raises(LeaseRefused, match="^recovery_assignment_authority_mismatch$"):
        wrapped.reconcile("grant", c["request"])


def test_restore_sidecar_mismatch_is_not_native_backup_proof(context, monkeypatch):
    c = context
    wrong = NativeBackupReference("backup", "a" * 64, "2026-10-21T12:00:00Z", "f" * 64)
    with pytest.raises(LeaseRefused, match="^recovery_backup_inventory_mismatch$"):
        coordinator(c).verify_restored(c["inventory"], wrong)
    assert not c["events"]
    correct = replace(wrong, source_inventory_sha256=c["inventory"].sha256)
    monkeypatch.setattr(SqlLeaseStore, "_run", lambda self, callback: callback("synthetic-cursor"))
    assert coordinator(c).verify_restored(c["inventory"], correct) is c["inventory"]


def test_held_epoch_rebind_clears_old_owner_advances_generation_and_exact_replays(
    context, monkeypatch
):
    c, db = context, LeaseDatabase()
    monkeypatch.setattr(lease_sql, "read_incarnation", lambda cur: APP)
    monkeypatch.setattr(SqlLeaseStore, "compare_and_act", REAL_COMPARE_AND_ACT)

    class Cursor:
        def __init__(self, cur):
            self.cur, self.result = cur, None

        def execute(self, query, params=()):
            self.result = None
            if query.startswith("SELECT authority_epoch,generation"):
                row = self.cur.connection.rows.get(params[0])
                self.result = None if row is None else (row[3], row[4])
            elif query.startswith("UPDATE dbo.pm_safety_leases_v1 SET authority_epoch"):
                epoch, generation, key = params
                self.cur.connection.rows[key][3:] = [epoch, generation, None, None, None]
            else:
                self.cur.execute(query, params)
            return self

        def fetchone(self):
            return self.result if self.result is not None else self.cur.fetchone()

        def __getattr__(self, name):
            return getattr(self.cur, name)

    def connect():
        connection = db.connect()
        original = connection.cursor
        original_commit = connection.commit
        connection.cursor = lambda: Cursor(original())

        def acknowledged_commit():
            original_commit()
            c["events"].append("main_ack")

        connection.commit = acknowledged_commit
        return connection

    old = SqlLeaseStore(connect, c["leases"].binding, "old-epoch", incarnation=APP)
    old.initialize()
    grant = old.acquire("old-owner", ttl_seconds=30)
    before = next(iter(db.rows.values()))
    assert before[5] == "old-owner"
    result = recovery_flow.rebind_held(c["authority"], c["expected"], connect, "old-epoch")
    assert result == {
        "epoch": c["expected"].epoch,
        "generation": grant.generation + 1,
        "held": True,
    }
    row = next(iter(db.rows.values()))
    assert row[5:] == [None, None, None]
    assert recovery_flow.rebind_held(c["authority"], c["expected"], connect, "old-epoch") == result
    barriers = [
        event for event in c["events"] if type(event) is tuple and event[0] == "application_barrier"
    ]
    assert barriers == [("application_barrier", APP, result["generation"])] * 2
    db.commit_failure = "after"
    with pytest.raises(LeaseCommitUnknown):
        recovery_flow.rebind_held(c["authority"], c["expected"], connect, "old-epoch")
    assert [
        event for event in c["events"] if type(event) is tuple and event[0] == "application_barrier"
    ] == barriers
    db.commit_failure = None
    current = SqlLeaseStore(connect, c["leases"].binding, c["expected"].epoch, incarnation=APP)
    with pytest.raises(LeaseRefused):
        current.compare_and_act(grant, lambda cur: pytest.fail("old grant must not reach callback"))
    fresh = current.acquire("new-owner", ttl_seconds=30)
    assert fresh.generation == grant.generation + 2
    assert len(db.rows) == 1 and db.effects == []


def test_real_authority_guard_preserves_application_plan_commit_unknown(context, monkeypatch):
    c = context

    @contextmanager
    def transaction(*args):
        yield "authority-cursor"

    # Use the actual guard implementation, not the fixture's orchestration double.
    monkeypatch.undo()
    monkeypatch.setattr(authority_sql, "_transaction", transaction)
    monkeypatch.setattr(SqlAuthorityStore, "_read", lambda *args: c["expected"])
    monkeypatch.setattr(SqlLeaseStore, "compare_and_act", lambda *args: c["inventory"])

    def uncertain(*args):
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
