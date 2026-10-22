"""Additive inventory profiles and wrapper protocol; no SQL execution proof."""

import hashlib
import json
import sys
from dataclasses import FrozenInstanceError, replace
from types import ModuleType

import pytest

import pipmind.safety.checkpoint_sql as checkpoint
from pipmind.safety.checkpoint_sql import (
    COMMAND_TABLES,
    COVERED_TABLES,
    TABLE_VERSION,
    TABLE_VERSION_COMMANDS,
    CheckpointRefused,
    CheckpointUnavailable,
    CommandCounts,
    Inventory,
    TableDigest,
    _inventory_hash,
    capture_complete_in_transaction,
    complete_required_locks,
    decode_inventory,
    encode_inventory,
    required_locks,
)
from pipmind.safety.lease_sql import LeaseBinding
from pipmind.safety.session import SessionBinding

BINDING = SessionBinding("golden-account", "demo", "USD")
LEASE = LeaseBinding(BINDING.account_id, BINDING.environment, "orders")
LEGACY_NAMES = (
    "dbo.pm_safety_leases_v1",
    "dbo.pm_safety_session_accounts_v1",
    "dbo.pm_safety_session_sessions_v1",
    "dbo.pm_safety_session_observations_v1",
    "dbo.pm_safety_admission_accounts_v1",
    "dbo.pm_safety_admission_books_v1",
    "dbo.pm_safety_admission_journal_v1",
    "dbo.pm_safety_admission_reservations_v1",
    "dbo.pm_safety_admission_floors_v1",
    "dbo.pm_safety_controls_v1",
    "dbo.pm_safety_control_overrides_v1",
    "dbo.pm_safety_control_suggestions_v1",
    "dbo.pm_safety_control_dispositions_v1",
    "dbo.pm_safety_integration_accounts_v1",
    "dbo.pm_safety_integration_journal_v1",
)


def legacy():
    tables = tuple(TableDigest(name, 0, "a" * 64) for name in LEGACY_NAMES)
    return Inventory(
        BINDING, "orders", TABLE_VERSION, tables, _inventory_hash(BINDING, "orders", tables)
    )


def complete(counts=None):
    if counts is None:
        counts = CommandCounts(7, 2, 1, 4, 3)
    rows = (1 if counts.commands else 0, counts.commands, counts.attempted, counts.unknown)
    tables = legacy().tables + tuple(
        TableDigest(name, number, "b" * 64)
        for name, number in zip(COMMAND_TABLES, rows, strict=True)
    )
    return Inventory(
        BINDING,
        "orders",
        TABLE_VERSION_COMMANDS,
        tables,
        _inventory_hash(
            BINDING, "orders", tables, table_version=TABLE_VERSION_COMMANDS, command_counts=counts
        ),
        counts,
    )


def test_legacy_golden_payload_hash_and_exact_encoded_bytes_are_unchanged():
    # Captured from baseline before additive profile implementation. The table
    # order is independently literal above, not inferred from the new registry.
    assert COVERED_TABLES == LEGACY_NAMES and len(required_locks(BINDING, LEASE)) == 6
    inventory = legacy()
    raw = encode_inventory(inventory)
    assert inventory.sha256 == "6425da370e22464873de0b92d8d7baffd51a01e79ba5f3c2e72528650b7caea4"
    assert (
        hashlib.sha256(raw.encode()).hexdigest()
        == "af54b06fad66f2265e2c28d6f6880314cff2e762d58fddff3a255a9d990815fc"
    )
    assert len(raw.encode()) == 2266 and '"command_counts"' not in raw
    assert json.loads(raw)["schema_version"] == 1
    assert decode_inventory(raw) == inventory and inventory.command_counts is None


def test_complete_profile_roundtrip_binds_exact_nineteen_tables_and_immutable_counts():
    inventory = complete()
    raw = encode_inventory(inventory)
    assert decode_inventory(raw) == inventory
    assert tuple(row.name for row in inventory.tables) == LEGACY_NAMES + COMMAND_TABLES
    assert len(inventory.tables) == 19
    data = json.loads(raw)
    assert data["schema_version"] == 2 and data["table_version"] == TABLE_VERSION_COMMANDS
    assert data["command_counts"] == {
        "commands": 7,
        "refused": 2,
        "unattempted": 1,
        "attempted": 4,
        "unknown": 3,
    }
    with pytest.raises(FrozenInstanceError):
        inventory.command_counts.unknown = 0
    changed = complete(CommandCounts(7, 3, 0, 4, 3))
    assert changed.tables == inventory.tables and changed.sha256 != inventory.sha256


@pytest.mark.parametrize(
    "counts",
    [CommandCounts(0, 0, 0, 0, 0), CommandCounts(2, 2, 0, 0, 0), CommandCounts(1, 0, 1, 0, 0)],
)
def test_empty_refused_only_and_unattempted_histories_have_explicit_distinct_counts(counts):
    result = complete(counts)
    assert decode_inventory(encode_inventory(result)).command_counts == counts


@pytest.mark.parametrize(
    "values",
    [
        (True, 0, 0, 1, 0),
        (1, False, 0, 1, 0),
        (1, 0, -1, 2, 0),
        (1, 0, 0, 1, 2),
        (2, 0, 0, 1, 0),
        (1, 0, 1, 1, 0),
        (9223372036854775808, 0, 9223372036854775808, 0, 0),
    ],
)
def test_count_types_partition_subset_and_bigint_capacity_are_strict(values):
    with pytest.raises(CheckpointRefused):
        CommandCounts(*values)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "reorder",
        "account",
        "intent-count",
        "attempt-count",
        "unknown-count",
        "legacy-profile",
        "unknown-profile",
        "no-counts",
    ],
)
def test_inconsistent_profile_or_table_coverage_refuses_even_with_rehashed_payload(fault):
    original = complete()
    tables, version, counts = original.tables, original.table_version, original.command_counts
    if fault == "missing":
        tables = tables[:-1]
    elif fault == "reorder":
        tables = tables[:-2] + (tables[-1], tables[-2])
    elif fault in ("account", "intent-count", "attempt-count", "unknown-count"):
        index = {"account": -4, "intent-count": -3, "attempt-count": -2, "unknown-count": -1}[fault]
        rows = list(tables)
        rows[index] = replace(rows[index], count=0)
        tables = tuple(rows)
    elif fault == "legacy-profile":
        version, counts = TABLE_VERSION, None
    elif fault == "unknown-profile":
        with pytest.raises(CheckpointRefused):
            replace(original, table_version="safety-operational-tables-v999")
        return
    else:
        counts = None
    with pytest.raises(CheckpointRefused):
        checksum = _inventory_hash(
            BINDING, "orders", tables, table_version=version, command_counts=counts
        )
        Inventory(BINDING, "orders", version, tables, checksum, counts)


@pytest.mark.parametrize(
    "fault",
    [
        "v1-extra-counts",
        "v2-null",
        "unknown-count-field",
        "schema-bool",
        "schema-profile-mismatch",
        "changed-counts",
        "duplicate",
        "noncanonical",
    ],
)
def test_codec_refuses_ambiguous_or_changed_profile_payloads(fault):
    raw = encode_inventory(complete())
    value = json.loads(raw)
    if fault == "v1-extra-counts":
        value = json.loads(encode_inventory(legacy()))
        value["command_counts"] = None
    elif fault == "v2-null":
        value["command_counts"] = None
    elif fault == "unknown-count-field":
        value["command_counts"]["settled"] = 0
    elif fault == "schema-bool":
        value["schema_version"] = True
    elif fault == "schema-profile-mismatch":
        value["schema_version"] = 1
    elif fault == "changed-counts":
        value["command_counts"].update(refused=3, unattempted=0)
    elif fault == "duplicate":
        raw = raw.replace('"schema_version":2', '"schema_version":2,"schema_version":2')
    else:
        raw += " "
    if fault not in ("duplicate", "noncanonical"):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(CheckpointRefused):
        decode_inventory(raw)


class LocksCursor:
    def __init__(self):
        self.allowed = set(complete_required_locks(BINDING, LEASE))
        self.queries, self.row = [], None

    def execute(self, query, params=()):
        self.queries.append(query)
        self.row = (
            (1, 1)
            if query == "SELECT @@TRANCOUNT,XACT_STATE()"
            else ("Exclusive" if params[0] in self.allowed else "NoLock",)
        )

    def fetchone(self):
        return self.row


def install_helper(monkeypatch, callback):
    module = ModuleType("pipmind.safety.command_checkpoint_sql")
    module.capture_command_tables_in_transaction = callback
    monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.mark.parametrize("present", [False, True])
def test_complete_wrapper_requires_seventh_lock_and_truthfully_selects_absent_or_complete_profile(
    monkeypatch, present
):
    cursor, calls = LocksCursor(), []
    old, new = legacy(), complete()
    monkeypatch.setattr(
        checkpoint, "capture_in_transaction", lambda *args: calls.append("legacy") or old
    )

    def helper(cur, binding, lease_binding):
        assert (cur, binding, lease_binding) == (cursor, BINDING, LEASE)
        calls.append("commands")
        return (new.tables[-4:], new.command_counts) if present else None

    install_helper(monkeypatch, helper)
    assert complete_required_locks(BINDING, LEASE)[:-1] == required_locks(BINDING, LEASE)
    assert complete_required_locks(BINDING, LEASE)[-1].startswith(
        "pipmind-safety-command-journal-v1:"
    )
    assert capture_complete_in_transaction(cursor, BINDING, LEASE) == (new if present else old)
    assert calls == ["legacy", "commands"]
    assert all(query.startswith("SELECT ") for query in cursor.queries)
    cursor.allowed.remove(complete_required_locks(BINDING, LEASE)[-1])
    calls.clear()
    with pytest.raises(CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        capture_complete_in_transaction(cursor, BINDING, LEASE)
    assert calls == []  # Never certifies absence without the complete lock set.


@pytest.mark.parametrize("fault", ["partial", "driver", "lost-lock"])
def test_complete_wrapper_never_falls_back_from_unverifiable_command_schema_or_capture(
    monkeypatch, fault
):
    cursor, original = LocksCursor(), complete()
    monkeypatch.setattr(checkpoint, "capture_in_transaction", lambda *args: legacy())

    def helper(*args):
        if fault == "driver":
            raise OSError("private SQL failure")
        if fault == "lost-lock":
            cursor.allowed.clear()
        return (
            original.tables[-3:] if fault == "partial" else original.tables[-4:],
            original.command_counts,
        )

    install_helper(monkeypatch, helper)
    with pytest.raises(CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        capture_complete_in_transaction(cursor, BINDING, LEASE)
