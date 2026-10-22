"""Offline cursor regressions; native SQL/restore proof remains separate CI."""

import re
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_safety_checkpoint as legacy
from test_safety_command_journal import facts

from pipmind.safety import checkpoint_sql as checkpoint
from pipmind.safety import command_checkpoint_sql as capture
from pipmind.safety.command_journal import (
    decode_order,
    encode_attempt,
    encode_command,
    encode_order,
    encode_unknown,
    fingerprint,
)
from pipmind.safety.session import account_key

BINDING, LEASE = legacy.BINDING, legacy.LEASE
KEY = account_key(BINDING)
ACCOUNT, COMMAND, ATTEMPT, UNKNOWN = checkpoint.COMMAND_TABLES


def rows(modes=("refused", "recorded", "attempt_intent", "unknown"), *, mixed=False):
    values = {table: [] for table in checkpoint.COMMAND_TABLES}
    values[ACCOUNT] = [(KEY, BINDING.account_id, BINDING.environment, BINDING.currency)]
    for index, mode in enumerate(modes):
        _, scope, command, attempt, unknown = facts()
        if mixed:
            scope = replace(scope, lease_resource="historical-resource-" + str(index))
        identity = f"command-{index:06d}"
        order = decode_order(command.order_body)
        order["client_order_id"] = identity
        command = replace(command, command_id=identity, order_body=encode_order(order), scope=scope)
        if mode == "refused":
            command = replace(command, local_outcome="refused", code="locally_refused")
        body = encode_command(command)
        values[COMMAND].append((KEY, identity, body, fingerprint(body)))
        if mode in ("attempt_intent", "unknown"):
            attempt = replace(
                attempt, command_id=identity, command_sha256=fingerprint(body), scope=scope
            )
            body = encode_attempt(attempt)
            values[ATTEMPT].append((KEY, identity, body, fingerprint(body)))
            if mode == "unknown":
                unknown = replace(
                    unknown,
                    command_id=identity,
                    command_sha256=attempt.command_sha256,
                    attempt_sha256=fingerprint(body),
                    scope=scope,
                )
                body = encode_unknown(unknown)
                values[UNKNOWN].append((KEY, identity, body, fingerprint(body)))
    return values


class Cursor(legacy.Cursor):
    def __init__(self, values, *, catalog=None):
        super().__init__(values)
        self.connection = SimpleNamespace(autocommit=False)
        self.allowed = set(checkpoint.complete_required_locks(BINDING, LEASE))
        self.catalog = (
            [("dbo", name.removeprefix("dbo."), "U") for name in checkpoint.COMMAND_TABLES]
            if catalog is None
            else catalog
        )
        self.catalog_calls = 0
        self.catalog_after = None
        self.transaction = (1, 1)
        self.principal = (1,)

    def execute(self, query, params=()):
        if query == "SELECT USER_ID()":
            self.result = [] if self.principal is None else [self.principal]
        elif "FROM sys.objects" in query:
            self.catalog_calls += 1
            self.result = list(
                self.catalog_after
                if self.catalog_after is not None and self.catalog_calls > 1
                else self.catalog
            )
        elif query == "SELECT @@TRANCOUNT,XACT_STATE()":
            self.result = [self.transaction]
        elif query.startswith("SELECT CASE WHEN DATALENGTH(body)"):
            table = re.search(r"FROM (dbo\.pm_safety_\w+)", query)[1]
            found = [row for row in self.rows[table] if row[:2] == params[1:]]
            assert len(found) <= 1
            self.result = [
                (row[2] if len(row[2].encode("utf-16-le")) <= params[0] else None, row[3])
                for row in found
            ]
        elif query.startswith("SELECT TOP (64) command_id"):
            selected = sorted(row[1] for row in self.rows[COMMAND] if row[0] == params[0])
            if len(params) > 1:
                selected = [identity for identity in selected if identity > params[1]]
            self.result = [(identity,) for identity in selected[: checkpoint.PAGE_ROWS]]
        else:
            return super().execute(query, params)
        self.queries.append(query)
        self.offset = 0
        return self


@pytest.fixture(autouse=True)
def schema(monkeypatch):
    # Reuse the original streaming SQL protocol double; production capture,
    # typed hashing, schema checking, codecs and projections remain unmocked.
    monkeypatch.setattr(legacy, "SCHEMA", dict(capture._SCHEMA))


def result(cur):
    return capture.capture_command_tables_in_transaction(cur, BINDING, LEASE)


def test_complete_exact_projections_preserve_all_historical_resources_and_bodies():
    data = rows(mixed=True)
    before = deepcopy(data)
    cur = Cursor(data)
    tables, counts = result(cur)
    assert counts == checkpoint.CommandCounts(4, 1, 1, 2, 1)
    assert tuple(row.name for row in tables) == checkpoint.COMMAND_TABLES
    assert tuple(row.count for row in tables) == (1, 4, 2, 1)
    assert data == before
    assert all(query.startswith("SELECT") for query in cur.queries)
    assert max(cur.fetch_sizes) <= checkpoint.PAGE_ROWS
    assert cur.chunks and max(cur.chunks) <= checkpoint.CHUNK_BYTES
    assert all(cur.metadata[table] == 2 for table in checkpoint.COMMAND_TABLES)


def test_supported_empty_schema_still_has_four_digests_and_explicit_zero_counts():
    data = {table: [] for table in checkpoint.COMMAND_TABLES}
    tables, counts = result(Cursor(data))
    assert counts == checkpoint.CommandCounts(0, 0, 0, 0, 0)
    assert tuple(row.count for row in tables) == (0, 0, 0, 0)


def test_only_wholly_absent_catalog_returns_legacy_none():
    cur = Cursor({}, catalog=[])
    assert result(cur) is None and cur.catalog_calls == 2
    assert not cur.metadata


@pytest.mark.parametrize("catalog_visible", [False, True])
@pytest.mark.parametrize("principal", [(2,), (None,), (True,), ("1",), (1, 2), None])
def test_non_authoritative_or_malformed_identity_cannot_classify_filtered_catalog(
    catalog_visible, principal
):
    # The underlying rows exist, even when metadata filtering returns no rows.
    cur = Cursor(rows(), catalog=None if catalog_visible else [])
    cur.principal = principal
    with pytest.raises(checkpoint.CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        result(cur)
    assert cur.catalog_calls == 0 and not cur.metadata


def test_authoritative_context_is_rechecked_before_postscan_catalog_claim():
    cur = Cursor(rows())
    execute = cur.execute
    principal_reads = 0

    def changed_context(query, params=()):
        nonlocal principal_reads
        if query == "SELECT USER_ID()":
            principal_reads += 1
            if principal_reads == 2:
                cur.principal = (2,)
        return execute(query, params)

    cur.execute = changed_context
    with pytest.raises(checkpoint.CheckpointUnavailable):
        result(cur)
    assert principal_reads == 2 and cur.catalog_calls == 1


@pytest.mark.parametrize("change", ["partial", "future", "view", "other_schema"])
def test_unsupported_catalog_cannot_downgrade_to_legacy(change):
    cur = Cursor(rows())
    if change == "partial":
        cur.catalog.pop()
    elif change == "future":
        cur.catalog.append(("dbo", "pm_safety_command_intents_v2", "U"))
    elif change == "view":
        cur.catalog[0] = (*cur.catalog[0][:2], "V")
    else:
        cur.catalog[0] = ("other", *cur.catalog[0][1:])
    with pytest.raises(checkpoint.CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        result(cur)


@pytest.mark.parametrize(
    "change", ["autocommit", "missing_connection", "no_transaction", "boolean_state", "lock"]
)
def test_unverifiable_transaction_or_missing_seventh_lock_refuses(change):
    cur = Cursor(rows())
    if change == "autocommit":
        cur.connection.autocommit = True
    elif change == "missing_connection":
        del cur.connection
    elif change == "no_transaction":
        cur.transaction = (0, 0)
    elif change == "boolean_state":
        cur.transaction = (1, True)
    else:
        cur.allowed.remove(checkpoint.complete_required_locks(BINDING, LEASE)[-1])
    with pytest.raises(checkpoint.CheckpointUnavailable):
        result(cur)


@pytest.mark.parametrize("change", ["bad_hash", "bad_body", "index_id", "foreign_binding", "ref"])
def test_corrupt_body_reference_or_index_never_produces_counts(change):
    data = rows()
    table, index = (ATTEMPT, 0) if change == "ref" else (COMMAND, 1)
    key, identity, body, sha = data[table][index]
    if change == "bad_hash":
        sha = "0" * 64
    elif change == "bad_body":
        body, sha = "{}", fingerprint("{}")
    elif change == "index_id":
        identity = "different-index-identity"
    elif change == "foreign_binding":
        from pipmind.safety.command_journal import decode_command

        record = decode_command(body)
        binding = replace(record.scope.assignment.binding, account_id="foreign-account")
        assignment = replace(record.scope.assignment, binding=binding)
        body = encode_command(replace(record, scope=replace(record.scope, assignment=assignment)))
        sha = fingerprint(body)
    else:
        from pipmind.safety.command_journal import decode_attempt

        body = encode_attempt(replace(decode_attempt(body), command_sha256="0" * 64))
        sha = fingerprint(body)
    data[table][index] = (key, identity, body, sha)
    with pytest.raises(checkpoint.CheckpointUnavailable):
        result(Cursor(data))


@pytest.mark.parametrize(
    "change", ["missing_pin", "orphan_attempt", "orphan_unknown", "schema", "drift"]
)
def test_pin_orphan_schema_and_postscan_drift_refuse(change):
    data = rows()
    if change == "missing_pin":
        data[ACCOUNT] = []
    elif change == "orphan_attempt":
        data[COMMAND].pop(2)
    elif change == "orphan_unknown":
        data[ATTEMPT].pop()
    cur = Cursor(data)
    if change == "schema":
        cur.bad_schema = True
    elif change == "drift":
        cur.catalog_after = []
    with pytest.raises(checkpoint.CheckpointUnavailable):
        result(cur)


def test_full_history_crosses_page_boundary_without_count_cap_or_mutation():
    data = rows(("recorded",) * 66)
    before = deepcopy(data)
    cur = Cursor(data)
    tables, counts = result(cur)
    assert counts == checkpoint.CommandCounts(66, 0, 66, 0, 0)
    assert tables[1].count == 66 and data == before
    assert sum(query.startswith("SELECT TOP (64) command_id") for query in cur.queries) == 2


def test_catalog_appearing_during_legacy_absence_refuses():
    cur = Cursor({}, catalog=[])
    cur.catalog_after = [("dbo", "pm_safety_command_intents_v1", "U")]
    with pytest.raises(checkpoint.CheckpointUnavailable):
        result(cur)


@pytest.mark.parametrize("flag", ["short_chunk", "change_count", "fail_driver"])
def test_incomplete_chunk_count_or_driver_failure_cannot_return_partial_inventory(flag):
    cur = Cursor(rows())
    setattr(cur, flag, True)
    with pytest.raises(checkpoint.CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        result(cur)


def test_read_interruption_propagates_without_mutation_or_healthy_absence():
    data = rows()
    original = deepcopy(data)
    cur = Cursor(data)
    execute = cur.execute

    def interrupted(query, params=()):
        if query.startswith("SELECT CASE WHEN DATALENGTH(body)"):
            raise KeyboardInterrupt("synthetic read interruption")
        return execute(query, params)

    cur.execute = interrupted
    with pytest.raises(KeyboardInterrupt):
        result(cur)
    assert data == original
    assert all(query.startswith("SELECT") for query in cur.queries)
