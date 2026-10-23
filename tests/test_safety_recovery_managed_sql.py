"""Actual disposable SQL restricted-coordinator integration; never Azure proof."""

import os
from copy import deepcopy
from uuid import uuid4

import pytest
from test_shadow_sql import (
    _command_journal_call_ci,
    _command_journal_case_ci,
    _p50_reheld_ci,
)
from test_shadow_sql import mind as mind

from pipmind.safety.admission_sql import SqlAdmissionStore
from pipmind.safety.attribution_sql import SqlAttributionStore
from pipmind.safety.authority_sql import AuthorityUnavailable
from pipmind.safety.checkpoint_sql import (
    COMMAND_TABLES,
    COVERED_TABLES,
    TABLE_VERSION_COMMANDS,
    CheckpointUnavailable,
    CommandCounts,
    encode_inventory,
)
from pipmind.safety.command_catalog_sql import MODULE_NAME, bootstrap_sql
from pipmind.safety.command_journal import encode_attempt, encode_command, encode_unknown
from pipmind.safety.integration_sql import SqlSafetyGateway
from pipmind.safety.lease_sql import SqlLeaseStore
from pipmind.safety.recovery_flow import DryRecoveryCoordinator
from pipmind.safety.recovery_sql import RobotReference, SqlRecoveryStore
from pipmind.shadow.common import digest
from pipmind.shadow.sql import connector, migrate
from pipmind.shadow.store import SqlMind

pytestmark = pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_TESTS") != "true", reason="separate SQL integration gate"
)
PROFILE = "restricted-command-catalog-v1"


def test_restricted_coordinator_complete_unknown_history_hidden_schema_and_rollback(mind):
    assert os.environ.get("PIPMIND_SQL_LOCAL") == "true"
    name = "managed_recovery_" + uuid4().hex
    admin = mind.connect()
    admin.autocommit = True
    master = admin.cursor()
    master.execute(f"CREATE DATABASE [{name}]")
    setup = None
    try:
        connect = connector({**os.environ, "PIPMIND_SQL_DATABASE": name})
        migrate(connect)
        source = SqlMind(connect, "managed-recovery-source")
        source.initialize(
            "Exact retained managed-recovery source identity",
            {"version": "v1", "text": "Exact retained source playbook"},
            "2026-10-21T00:00:00Z",
            binding="a" * 64,
        )
        with _command_journal_case_ci(source) as (
            flow,
            active,
            journal,
            grant,
            assignment,
            request,
            gateway,
        ):
            order = deepcopy(request.order)
            order["client_order_id"] = "managed-retained-unknown"

            def record_unknown(cur):
                journal.record_in_transaction(
                    cur, grant, assignment, order, causation_id="exact-retained-input"
                )
                journal.attempt_in_transaction(
                    cur, grant, assignment, order["client_order_id"], "retained-attempt"
                )
                journal.unknown_in_transaction(cur, grant, assignment, order["client_order_id"])

            _command_journal_call_ci(flow, active, journal, grant, assignment, record_unknown)
            held, held_gateway, held_grant = _p50_reheld_ci(
                connect, flow, active, gateway, "managed-held"
            )
            original = held.capture(held_grant)
            assert original.table_version == TABLE_VERSION_COMMANDS
            assert original.command_counts == CommandCounts(1, 0, 0, 1, 1)
            original_mind = source.head(), source.retained()

            def history():
                return tuple(
                    (
                        encode_command(row.command),
                        encode_attempt(row.attempt),
                        encode_unknown(row.unknown),
                    )
                    for row in journal.iter_history()
                )

            original_history = history()
            setup = connect()
            setup.autocommit = True
            c = setup.cursor()
            c.execute("SET ANSI_NULLS ON")
            c.execute("SET QUOTED_IDENTIFIER ON")
            c.execute(bootstrap_sql())
            c.execute("CREATE USER p57_recovery_app WITHOUT LOGIN")
            # This is disposable SQL Server metadata visibility for its native
            # pin; Azure current-database physical-pin permissions are separate.
            c.execute("GRANT VIEW DATABASE STATE TO p57_recovery_app")
            c.execute(f"GRANT EXECUTE,VIEW DEFINITION ON OBJECT::{MODULE_NAME} TO p57_recovery_app")
            for table in COVERED_TABLES + COMMAND_TABLES:
                c.execute(f"GRANT SELECT ON OBJECT::{table} TO p57_recovery_app")
            for table in COMMAND_TABLES:
                c.execute(f"DENY VIEW DEFINITION ON OBJECT::{table} TO p57_recovery_app")
            for table in (
                "dbo.pm_safety_recovery_accounts_v1",
                "dbo.pm_safety_recovery_plans_v1",
                "dbo.pm_safety_recovery_transitions_v1",
            ):
                c.execute(f"GRANT SELECT,INSERT,UPDATE ON OBJECT::{table} TO p57_recovery_app")
            c.execute("CREATE TABLE dbo.p57_recovery_probe(value int NOT NULL)")
            c.execute("GRANT SELECT,INSERT ON dbo.p57_recovery_probe TO p57_recovery_app")

            def restricted_connect():
                db = connect()
                db.cursor().execute("EXECUTE AS USER='p57_recovery_app'")
                return db

            leases = SqlLeaseStore(
                restricted_connect,
                held.leases.binding,
                held.expected.epoch,
                incarnation=held.leases.incarnation,
            )
            recovery = SqlRecoveryStore(restricted_connect, held.recovery.binding)
            restricted = DryRecoveryCoordinator(
                held.authority, held.expected, leases, recovery, inventory_profile=PROFILE
            )
            result = restricted.capture(held_grant)
            assert result == original and encode_inventory(result) == encode_inventory(original)
            assert result.command_counts == CommandCounts(1, 0, 0, 1, 1)

            # Real restricted prepare/confirm use the same complete capture
            # policy; retained unknowns remain obligations after confirmation.
            plan = restricted.prepare(
                held_grant,
                plan_id="managed-replacement",
                actor_id="owner",
                expected_generation=recovery.read().generation,
                target_robot=RobotReference("replacement", "v1", digest("replacement")),
                reason="Explicit restricted synthetic handover",
                position_obligation_ids=tuple(request.evidence.observed.position_ids),
            )
            confirmed = restricted.confirm(held_grant, plan["plan"].plan_id, plan["digest"])
            assert confirmed["held"] and restricted.authority.read() == held.expected
            assert restricted.capture(held_grant) == original
            admission = SqlAdmissionStore(
                restricted_connect,
                leases,
                held_gateway.admission.sessions,
                held_gateway.admission._policies,
            )
            restricted_gateway = SqlSafetyGateway(
                admission,
                held_gateway.controls,
                held_gateway._symbols,
                base_units_per_size=held_gateway._unit_factors,
            )
            with pytest.raises(AuthorityUnavailable):
                restricted.activate_dry(
                    restricted_gateway,
                    SqlAttributionStore(restricted_connect, recovery.binding),
                    held_grant,
                    recovery.read().assignment,
                    request,
                )
            assert restricted.authority.read() == held.expected

            def negative(mutate, *, hidden=False):
                db = connect()
                impersonated = False
                try:
                    cur = db.cursor()
                    cur.execute("SET XACT_ABORT OFF")
                    mutate(cur)
                    cur.execute("EXECUTE AS USER='p57_recovery_app'")
                    impersonated = True
                    cur.execute("SELECT USER_ID(),IS_MEMBER('db_owner')")
                    principal, owner = cur.fetchone()
                    assert principal != 1 and owner == 0
                    if hidden:
                        cur.execute("SELECT OBJECT_ID('dbo.pm_safety_command_future_v57','U')")
                        assert cur.fetchone() == (None,)
                    cur.execute("INSERT dbo.p57_recovery_probe VALUES(57)")
                    with pytest.raises(
                        CheckpointUnavailable, match="^safety_inventory_unavailable$"
                    ):
                        restricted._inventory(cur)
                    cur.execute("REVERT")
                    impersonated = False
                finally:
                    if impersonated:
                        db.cursor().execute("REVERT")
                    db.rollback()
                    db.close()
                assert restricted.capture(held_grant) == original
                assert history() == original_history
                assert (source.head(), source.retained()) == original_mind
                c.execute("SELECT COUNT_BIG(*) FROM dbo.p57_recovery_probe")
                assert c.fetchone() == (0,)

            def hidden_future(cur):
                cur.execute("CREATE TABLE dbo.pm_safety_command_future_v57(value int NOT NULL)")
                cur.execute(
                    "DENY VIEW DEFINITION ON OBJECT::dbo.pm_safety_command_future_v57 "
                    "TO p57_recovery_app"
                )

            negative(hidden_future, hidden=True)
            negative(
                lambda cur: cur.execute(
                    f"REVOKE EXECUTE ON OBJECT::{MODULE_NAME} FROM p57_recovery_app"
                )
            )
            negative(
                lambda cur: cur.execute(
                    "REVOKE SELECT ON OBJECT::dbo.pm_safety_command_unknown_v1 "
                    "FROM p57_recovery_app"
                )
            )
            assert history() == original_history
            assert (source.head(), source.retained()) == original_mind
    finally:
        if setup is not None:
            setup.close()
        master.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        master.execute(f"DROP DATABASE [{name}]")
        admin.close()
