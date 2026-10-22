"""Explicit held recovery/handover composition for the isolated dry SQL gateway.

No SQL restore, robot import, engine resume, provider call or order transport.
The independent authority guard spans application commit acknowledgement.
"""

import hashlib
from dataclasses import asdict

from pipmind.safety.admission import decode_request, encode_request
from pipmind.safety.authority_sql import SqlAuthorityStore
from pipmind.safety.checkpoint_sql import (
    Inventory,
    NativeBackupReference,
    encode_inventory,
)
from pipmind.safety.checkpoint_sql import (
    capture_complete_in_transaction as capture_in_transaction,
)
from pipmind.safety.checkpoint_sql import (
    complete_required_locks as required_locks,
)
from pipmind.safety.incarnation import encode_incarnation
from pipmind.safety.integration_sql import SqlSafetyGateway, _lock
from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseRefused, SqlLeaseStore
from pipmind.safety.recovery_sql import Assignment, RecoveryCommitUnknown, SqlRecoveryStore


def _need(value, code):
    if not value:
        raise LeaseRefused(code)


def incarnation_sha256(value):
    return hashlib.sha256(encode_incarnation(value).encode("ascii")).hexdigest()


def _scope(authority, expected, leases, recovery):
    _need(
        type(authority) is SqlAuthorityStore
        and type(leases) is SqlLeaseStore
        and type(recovery) is SqlRecoveryStore,
        "recovery_invalid_stores",
    )
    _need(
        authority.binding == leases.binding == expected.binding
        and leases.incarnation is not None
        and leases.incarnation == expected.application_incarnation
        and leases.authority_epoch == expected.epoch
        and (recovery.binding.account_id, recovery.binding.environment)
        == (leases.binding.account_id, leases.binding.environment),
        "recovery_scope_mismatch",
    )


def _all_locks(cur, recovery, leases):
    for resource in required_locks(recovery.binding, leases.binding):
        _lock(cur, resource)


def _current(cur, recovery, expected, assignment):
    state = recovery.read_in_transaction(cur)
    _need(
        type(assignment) is Assignment and state.assignment == assignment,
        "recovery_assignment_changed",
    )
    _need(
        assignment.epoch == expected.epoch and assignment.binding == recovery.binding,
        "recovery_assignment_scope_mismatch",
    )
    if expected.mode == "active":
        _need(
            assignment.assignment_id == expected.assignment_id
            and assignment.generation == expected.assignment_generation
            and assignment.robot.content_sha256 == expected.robot_sha256,
            "recovery_assignment_authority_mismatch",
        )
    else:
        _need(
            assignment.generation > expected.assignment_generation,
            "recovery_assignment_not_new",
        )
    return assignment


def rebind_held(authority, expected, connect, previous_epoch):
    """Owner-only epoch change after an acknowledged external fence.

    No lease row is created and no account data is reset. Exact replay is a
    read-only result; acquiring a new lease remains a separate explicit action.
    """
    _need(type(authority) is SqlAuthorityStore, "recovery_invalid_authority")
    reader = SqlLeaseStore(
        connect,
        expected.binding,
        previous_epoch,
        incarnation=expected.application_incarnation,
    )

    def operation(cur):
        cur.execute(
            "SELECT authority_epoch,generation FROM dbo.pm_safety_leases_v1 "
            "WITH(UPDLOCK,HOLDLOCK) WHERE binding_key=%s",
            (reader._key,),
        )
        row = cur.fetchone()
        _need(row is not None, "recovery_lease_missing")
        if row[0] == expected.epoch:
            # Validate full identity, not just a matching hash key/epoch.
            SqlLeaseStore(
                connect,
                expected.binding,
                expected.epoch,
                incarnation=expected.application_incarnation,
            )._read(cur)
            return {"epoch": expected.epoch, "generation": row[1], "held": True}
        _need(previous_epoch != expected.epoch, "recovery_fence_required")
        old = reader._read(cur)
        _need(
            type(old[4]) is int and 0 <= old[4] < 9223372036854775807,
            "recovery_lease_generation_exhausted",
        )
        cur.execute(
            "UPDATE dbo.pm_safety_leases_v1 SET authority_epoch=%s,generation=%s,"
            "owner_id=NULL,token_sha256=NULL,expires_at=NULL WHERE binding_key=%s",
            (expected.epoch, old[4] + 1, reader._key),
        )
        return {"epoch": expected.epoch, "generation": old[4] + 1, "held": True}

    with authority.guard(expected, mode="held") as guard:
        result = reader._run(operation)
        # Only an acknowledged barrier on this exact physical database drains
        # its old transactions. Persist that proof outside the restore domain;
        # target rebinding cannot substitute for an unfenced original source.
        guard.record_application_barrier(expected.application_incarnation, result["generation"])
    return result


class DryRecoveryCoordinator:
    """Trusted owner operations; all assignment confirmations stay inactive.

    A plan is valid only while its exact complete operational inventory remains
    unchanged. Lease renewal, broker/session updates or another acceptance can
    require preparing a new plan. No table is truncated to make a plan fit.
    """

    def __init__(self, authority, expected, leases, recovery):
        _scope(authority, expected, leases, recovery)
        _need(expected.mode == "held", "recovery_hold_required")
        self.authority, self.expected = authority, expected
        self.leases, self.recovery = leases, recovery

    def _inventory(self, cur):
        _all_locks(cur, self.recovery, self.leases)
        return capture_in_transaction(cur, self.recovery.binding, self.leases.binding)

    def capture(self, grant):
        with self.authority.guard(self.expected, mode="held"):
            result = self.leases.compare_and_act(grant, self._inventory)
        return result

    def verify_restored(self, inventory, backup):
        """Compare complete restored safety state before epoch rebinding.

        The trusted backup provider must verify the actual file/digest and whole
        mind/feed/robot history separately. This checks the supplied sidecar;
        matching metadata alone never proves a native backup exists.
        """
        _need(
            type(inventory) is Inventory and type(backup) is NativeBackupReference,
            "recovery_invalid_checkpoint",
        )
        encode_inventory(inventory)
        backup.__post_init__()
        _need(
            backup.source_inventory_sha256 == inventory.sha256, "recovery_backup_inventory_mismatch"
        )
        with self.authority.guard(self.expected, mode="held"):
            result = self.leases._run(self._inventory)
            _need(result == inventory, "recovery_restored_state_mismatch")
        return result

    def prepare(
        self,
        grant,
        *,
        plan_id,
        actor_id,
        expected_generation,
        target_robot,
        reason,
        position_obligation_ids,
    ):
        _need(
            type(expected_generation) is int
            and expected_generation >= self.expected.assignment_generation,
            "recovery_missing_assignment_history",
        )
        with self.authority.guard(self.expected, mode="held"):
            inventory = self.leases.compare_and_act(grant, self._inventory)
            try:
                record = self.recovery.create_plan(
                    plan_id,
                    actor_id,
                    expected_generation,
                    inventory.sha256,
                    target_robot,
                    self.expected.epoch,
                    incarnation_sha256(self.leases.incarnation),
                    reason,
                    position_obligation_ids,
                )
            except RecoveryCommitUnknown:
                # Preserve application-commit uncertainty through the external
                # guard. A different external commit-unknown remains distinct.
                raise LeaseCommitUnknown("recovery_plan_commit_unknown") from None
        return {**record, "inventory": inventory}

    def confirm(self, grant, plan_id, digest):
        def operation(cur):
            inventory = self._inventory(cur)
            record = self.recovery.plan_in_transaction(cur, plan_id)
            _need(record is not None, "recovery_plan_missing")
            plan = record["plan"]
            _need(
                plan.application_incarnation == incarnation_sha256(self.leases.incarnation),
                "recovery_plan_incarnation_mismatch",
            )
            return self.recovery.confirm_in_transaction(
                cur,
                plan_id,
                digest,
                epoch=self.expected.epoch,
                checkpoint_sha256=inventory.sha256,
                obligation_ids=plan.position_obligation_ids,
            )

        with self.authority.guard(self.expected, mode="held"):
            result = self.leases.compare_and_act(grant, operation)
        return result

    def activate_dry(self, gateway, attribution, grant, assignment, request):
        """Explicit fresh-reconciliation activation of dry routing only.

        The main SQL commit must be acknowledged before external activation.
        An external commit-unknown may leave activation durable; reconcile the
        external store rather than assuming rollback or resending the operation.
        """
        request = decode_request(encode_request(request))
        _need(
            tuple(request.evidence.observed.position_ids) == self._obligations(grant, assignment),
            "recovery_obligations_changed",
        )
        wrapped = RecoveryAwareDryGateway(
            self.authority,
            self.expected,
            gateway,
            self.recovery,
            attribution,
            assignment,
            _held=True,
        )
        with self.authority.guard(self.expected, mode="held") as guard:
            result = wrapped._gateway.reconcile(grant, request)
            _need(
                result["gateway_receipt"]["outcome"] == "dry_ready",
                "recovery_reconciliation_required",
            )
            active = guard.activate(
                assignment.assignment_id, assignment.generation, assignment.robot.content_sha256
            )
        return {
            "authority": active,
            "reconciliation": result,
            "dispatch_authorized": False,
            "engine_resumed": False,
        }

    def _obligations(self, grant, assignment):
        def read(cur):
            _all_locks(cur, self.recovery, self.leases)
            _current(cur, self.recovery, self.expected, assignment)
            record = self.recovery.plan_in_transaction(cur, assignment.assignment_id)
            _need(record is not None, "recovery_plan_missing")
            return record["plan"].position_obligation_ids

        with self.authority.guard(self.expected, mode="held"):
            result = self.leases.compare_and_act(grant, read)
        return result


class RecoveryAwareDryGateway:
    """Every dry operation holds external authority through its SQL commit.

    Legacy PIP-28 remains independently dry. Future order transport must provide
    an independently qualified recovery-aware boundary; this is never a send
    permit. Hook injection is restricted to this trusted composition root.
    """

    def __init__(
        self, authority, expected, gateway, recovery, attribution, assignment, *, _held=False
    ):
        from pipmind.safety.attribution_sql import SqlAttributionStore

        _need(
            type(gateway) is SqlSafetyGateway and type(attribution) is SqlAttributionStore,
            "recovery_invalid_gateway",
        )
        leases = gateway.admission.leases
        _scope(authority, expected, leases, recovery)
        _need(
            gateway.admission.binding == recovery.binding == attribution.binding,
            "recovery_gateway_binding_mismatch",
        )
        _need(expected.mode == ("held" if _held else "active"), "recovery_gateway_mode_mismatch")
        _need(type(assignment) is Assignment, "recovery_invalid_assignment")
        self.authority, self.expected, self._held = authority, expected, _held

        def before(cur, grant):
            # Acquire earlier locks before the gateway obtains integration/admission.
            for resource in required_locks(recovery.binding, leases.binding)[:4]:
                _lock(cur, resource)
            current = _current(cur, recovery, expected, assignment)
            if _held:
                # Recheck the complete current command history in the same SQL
                # transaction as reconciliation, while the external HELD guard
                # spans its acknowledged commit and subsequent activation.
                _all_locks(cur, recovery, leases)
                inventory = capture_in_transaction(cur, recovery.binding, leases.binding)
                counts = inventory.command_counts
                _need(
                    counts is None or counts.unattempted + counts.attempted == 0,
                    "recovery_command_obligations_pending",
                )
            return current

        def after(cur, current, result):
            link = attribution.record_in_transaction(cur, current, result)
            return {
                "gateway_receipt": result,
                "attribution": asdict(link),
                "dispatch_authorized": False,
                "dispatch_enabled": False,
            }

        self._gateway = SqlSafetyGateway(
            gateway.admission,
            gateway.controls,
            gateway._symbols,
            base_units_per_size=gateway._unit_factors,
            before=before,
            after=after,
        )

    def _call(self, name, *args, **kwargs):
        _need(not self._held, "recovery_hold_required")
        with self.authority.guard(self.expected):
            result = getattr(self._gateway, name)(*args, **kwargs)
        return result

    def reconcile(self, grant, request):
        return self._call("reconcile", grant, request)

    def accept(self, grant, request, *, origin="model", override_id=None):
        return self._call("accept", grant, request, origin=origin, override_id=override_id)

    def check(self, grant, check_id, request):
        return self._call("check", grant, check_id, request)
