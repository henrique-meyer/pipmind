"""Actual catalog/streaming code on scripted cursors, never SQL permission proof."""

from copy import deepcopy
from dataclasses import replace

import pytest
import test_safety_checkpoint as legacy
import test_safety_command_catalog_sql as catalog_protocol
import test_safety_command_checkpoint_sql as command_protocol

from pipmind.safety import checkpoint_sql as checkpoint
from pipmind.safety import command_catalog_sql as catalog
from pipmind.safety import command_checkpoint_sql as command
from pipmind.safety.command_journal import (
    decode_attempt,
    decode_command,
    encode_attempt,
    encode_command,
    fingerprint,
)

BINDING, LEASE = legacy.BINDING, legacy.LEASE
DATABASE = catalog_protocol.DATABASE
ACCOUNT, COMMAND, ATTEMPT, UNKNOWN = checkpoint.COMMAND_TABLES


@pytest.fixture(autouse=True)
def combined_schema(monkeypatch):
    # Production metadata checks, canonical codecs, semantic projections and
    # typed streaming hashes are deliberately not patched.
    monkeypatch.setattr(legacy, "SCHEMA", dict(checkpoint._SCHEMA + command._SCHEMA))


class Cursor(command_protocol.Cursor):
    """One cursor serving actual P53 protocol and P50 bounded row queries."""

    def __init__(self, values, *, absent=False):
        super().__init__(values, catalog=[] if absent else None)
        self.principal = (5,)  # The ordinary catalog is not authoritative.
        self.protocol = catalog_protocol.Cursor(absent=absent)
        self.protocol.connection = self.connection
        self.proof_rows = [deepcopy(catalog_protocol.proof()) for _ in range(4)]
        self.object_sets = [deepcopy(self.protocol.objects) for _ in range(2)]
        self.column_sets = [deepcopy(self.protocol.columns) for _ in range(2)]
        self.proof_reads = self.module_reads = 0
        self.in_protocol = False
        self.timeline = []

    @property
    def description(self):
        return self.protocol.description if self.in_protocol else None

    def execute(self, query, params=()):
        if query == catalog._PROOF_SQL:
            row = self.proof_rows[self.proof_reads]
            self.proof_reads += 1
            self.protocol.before = self.protocol.after = row
            self.timeline.append("proof")
        elif query == f"EXEC {catalog.MODULE_NAME}":
            self.protocol.objects = self.object_sets[self.module_reads]
            self.protocol.columns = self.column_sets[self.module_reads]
            self.module_reads += 1
            self.timeline.append("module")
        else:
            self.in_protocol = False
            if "FROM dbo.pm_safety_" in query:
                self.timeline.append("history")
            return super().execute(query, params)
        self.in_protocol = True
        self.queries.append(query)
        self.protocol.execute(query)
        return self

    def fetchmany(self, size):
        if self.in_protocol:
            self.fetch_sizes.append(size)
            return self.protocol.fetchmany(size)
        return super().fetchmany(size)

    def nextset(self):
        assert self.in_protocol
        return self.protocol.nextset()


def read(cur, **kwargs):
    return command.capture_complete_restricted_in_transaction(
        cur, BINDING, LEASE, expected_database=DATABASE, **kwargs
    )


def dataset(kind="unresolved"):
    result = legacy.records(count=70 if kind == "paged" else 1)
    if kind == "absent":
        return result
    if kind == "empty":
        return result | {table: [] for table in checkpoint.COMMAND_TABLES}
    modes = (
        ("refused",)
        if kind == "refused"
        else ("recorded",) * 66
        if kind == "paged"
        else ("refused", "recorded", "attempt_intent", "unknown")
    )
    return result | command_protocol.rows(modes, mixed=True)


def default_capture(values, *, absent=False):
    cur = command_protocol.Cursor(deepcopy(values), catalog=[] if absent else None)
    return checkpoint.capture_complete_in_transaction(cur, BINDING, LEASE)


@pytest.mark.parametrize("kind", ["absent", "empty", "refused", "unresolved", "paged"])
def test_opt_in_matches_existing_dbo_inventory_exactly_without_promoting_history(kind):
    values = dataset(kind)
    original = deepcopy(values)
    cur = Cursor(values, absent=kind == "absent")
    expected = default_capture(values, absent=kind == "absent")
    actual = read(cur)
    assert actual == expected
    assert checkpoint.encode_inventory(actual) == checkpoint.encode_inventory(expected)
    assert actual.sha256 == expected.sha256 and actual.tables == expected.tables
    assert values == original and cur.connection.autocommit is False
    assert cur.proof_reads == 4 and cur.module_reads == 2
    assert cur.timeline.index("history") > cur.timeline.index("module")
    assert cur.timeline[-3:] == ["proof", "module", "proof"]
    assert all(
        query.startswith(("SELECT", "EXEC dbo.pm_safety_command_catalog_v1"))
        for query in cur.queries
    )
    assert "SELECT USER_ID()" not in cur.queries
    assert max(cur.fetch_sizes) <= checkpoint.PAGE_ROWS
    if kind == "absent":
        assert actual.table_version == checkpoint.TABLE_VERSION
        assert actual.command_counts is None and len(actual.tables) == 15
    else:
        assert actual.table_version == checkpoint.TABLE_VERSION_COMMANDS
        assert len(actual.tables) == 19
    if kind == "unresolved":
        assert actual.command_counts == checkpoint.CommandCounts(4, 1, 1, 2, 1)
        assert actual.command_counts.unattempted + actual.command_counts.attempted == 3
    if kind == "paged":
        assert actual.command_counts == checkpoint.CommandCounts(66, 0, 66, 0, 0)
        assert sum(query.startswith("SELECT TOP (64) command_id") for query in cur.queries) == 2


def test_exact_unicode_lob_bytes_use_existing_chunked_hash_domain():
    values = dataset("refused")
    table = "dbo.pm_safety_session_observations_v1"
    columns = dict(checkpoint._SCHEMA)[table]
    index = next(i for i, column in enumerate(columns) if column[0] == "body")
    row = list(values[table][0])
    row[index] = "naïve 😀\n" * checkpoint.CHUNK_BYTES
    values[table] = [tuple(row)]
    cur = Cursor(values)
    assert read(cur) == default_capture(values)
    assert cur.chunks and max(cur.chunks) == checkpoint.CHUNK_BYTES


def test_existing_default_api_still_refuses_restricted_principal_without_fallback():
    cur = Cursor(dataset())
    with pytest.raises(checkpoint.CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        checkpoint.capture_complete_in_transaction(cur, BINDING, LEASE)
    assert cur.module_reads == cur.proof_reads == 0


def test_no_caller_catalog_or_reader_override_can_supply_a_success_dto():
    cur = Cursor(dataset())
    with pytest.raises(TypeError):
        read(cur, catalog=object())
    with pytest.raises(TypeError):
        read(cur, catalog_reader=lambda: object())
    assert cur.queries == []


@pytest.mark.parametrize("missing", range(7))
def test_all_seven_preheld_exclusive_transaction_locks_are_required(missing):
    cur = Cursor(dataset())
    cur.allowed.remove(checkpoint.complete_required_locks(BINDING, LEASE)[missing])
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)
    assert "history" not in cur.timeline


@pytest.mark.parametrize("phase", [0, 3])
@pytest.mark.parametrize("field,value", [(1, "other-database"), (9, 5), (13, "altered module")])
def test_real_catalog_proof_failure_is_not_bypassed_before_or_after_scan(phase, field, value):
    cur = Cursor(dataset())
    cur.proof_rows[phase][field] = value
    with pytest.raises(checkpoint.CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        read(cur)
    if phase == 0:
        assert "history" not in cur.timeline
    else:
        assert "history" in cur.timeline


@pytest.mark.parametrize("change", ["future", "partial", "view", "key"])
def test_owner_catalog_rejects_hidden_unsupported_objects_or_columns(change):
    cur = Cursor(dataset())
    cur.catalog = []  # A filtered caller catalog would falsely imply absence.
    if change == "future":
        cur.object_sets[0].append(("dbo", "pm_safety_command_intents_v2", "U", 901))
    elif change == "partial":
        cur.object_sets[0].pop()
    elif change == "view":
        old = cur.object_sets[0][0]
        cur.object_sets[0][0] = (*old[:2], "V", old[3])
    else:
        old = cur.column_sets[0][0]
        cur.column_sets[0][0] = (*old[:9], 0, old[10])
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)
    assert "history" not in cur.timeline


@pytest.mark.parametrize("initial", ["absent", "supported"])
def test_changed_full_catalog_between_scan_boundaries_is_not_accepted(initial):
    cur = Cursor(dataset("absent" if initial == "absent" else "empty"), absent=initial == "absent")
    if initial == "absent":
        cur.object_sets[1] = deepcopy(catalog_protocol.OBJECTS)
        cur.column_sets[1] = catalog_protocol.native_columns()
    else:
        cur.object_sets[1] = []
        cur.column_sets[1] = []
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)
    assert cur.module_reads == 2


def test_valid_individual_module_proofs_with_different_object_id_still_refuse():
    cur = Cursor(dataset())
    cur.proof_rows[2][5] = cur.proof_rows[3][5] = 100
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)
    assert cur.proof_reads == 4


def test_same_supported_columns_but_replaced_table_object_identity_refuses():
    cur = Cursor(dataset())
    schema, name, kind, identity = cur.object_sets[1][0]
    cur.object_sets[1][0] = (schema, name, kind, identity + 1000)
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)
    assert cur.module_reads == 2 and cur.proof_reads == 4


@pytest.mark.parametrize("value", [True, None, 0])
def test_nontransactional_connector_configuration_refuses_before_any_query(value):
    cur = Cursor(dataset())
    cur.connection.autocommit = value
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)
    assert cur.queries == []


def test_account_binding_mismatch_refuses_before_catalog_or_user_rows():
    cur = Cursor(dataset())
    with pytest.raises(checkpoint.CheckpointUnavailable):
        command.capture_complete_restricted_in_transaction(
            cur, BINDING, replace(LEASE, account_id="other-account"), expected_database=DATABASE
        )
    assert cur.queries == []


@pytest.mark.parametrize("change", ["hash", "body", "reference", "index", "missing_pin", "orphan"])
def test_full_command_semantics_remain_mandatory_after_valid_owner_module(change):
    values = dataset()
    if change == "missing_pin":
        values[ACCOUNT] = []
    elif change == "orphan":
        values[COMMAND].pop(2)
    else:
        table, index = (ATTEMPT, 0) if change == "reference" else (COMMAND, 1)
        key, identity, body, sha = values[table][index]
        if change == "hash":
            sha = "0" * 64
        elif change == "body":
            body = "{}"
            sha = fingerprint(body)
        elif change == "reference":
            body = encode_attempt(replace(decode_attempt(body), command_sha256="0" * 64))
            sha = fingerprint(body)
        else:
            identity = "changed-index"
        values[table][index] = (key, identity, body, sha)
    original = deepcopy(values)
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(Cursor(values))
    assert values == original


def test_foreign_account_scope_cannot_be_hidden_behind_valid_table_hashes():
    values = dataset()
    key, identity, body, _ = values[COMMAND][1]
    record = decode_command(body)
    foreign = replace(record.scope.assignment.binding, account_id="foreign-account")
    scope = replace(record.scope, assignment=replace(record.scope.assignment, binding=foreign))
    body = encode_command(replace(record, scope=scope))
    values[COMMAND][1] = (key, identity, body, fingerprint(body))
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(Cursor(values))


@pytest.mark.parametrize("flag", ["short_chunk", "change_count", "bad_schema"])
def test_existing_streaming_schema_chunk_and_count_failures_do_not_become_partial_success(flag):
    cur = Cursor(dataset())
    setattr(cur, flag, True)
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)


@pytest.mark.parametrize("table", ["dbo.pm_safety_session_observations_v1", COMMAND])
def test_denied_user_history_read_is_fixed_failure_and_never_absent(table):
    cur = Cursor(dataset())
    execute = cur.execute

    def deny(query, params=()):
        if f"FROM {table}" in query:
            raise PermissionError("PRIVATE-SQL-CREDENTIAL-DETAIL")
        return execute(query, params)

    cur.execute = deny
    with pytest.raises(checkpoint.CheckpointUnavailable) as caught:
        read(cur)
    assert str(caught.value) == "safety_inventory_unavailable"
    assert caught.value.__suppress_context__


def test_lost_lock_after_successful_scan_cannot_return_a_complete_inventory():
    cur = Cursor(dataset())
    execute = cur.execute

    def lose(query, params=()):
        if query == catalog._PROOF_SQL and cur.proof_reads == 3:
            cur.allowed.clear()
        return execute(query, params)

    cur.execute = lose
    with pytest.raises(checkpoint.CheckpointUnavailable):
        read(cur)
    assert "history" in cur.timeline


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_baseexception_during_actual_command_read_propagates_without_mutating_history(error):
    values = dataset()
    original = deepcopy(values)
    cur = Cursor(values)
    execute = cur.execute

    def interrupt(query, params=()):
        if query.startswith("SELECT CASE WHEN DATALENGTH(body)"):
            raise error("synthetic interruption")
        return execute(query, params)

    cur.execute = interrupt
    with pytest.raises(error):
        read(cur)
    assert values == original and cur.module_reads == 1
