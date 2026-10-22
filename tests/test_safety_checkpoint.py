"""Offline cursor protocol/counterexamples, not native-backup or SQL execution proof."""

import hashlib
import json
import re
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from pipmind.safety.checkpoint_sql import (
    _SCHEMA,
    CHUNK_BYTES,
    COVERED_TABLES,
    MAX_INVENTORY_BYTES,
    PAGE_ROWS,
    CheckpointRefused,
    CheckpointUnavailable,
    NativeBackupReference,
    _scalar,
    capture_in_transaction,
    decode_inventory,
    encode_inventory,
    required_locks,
)
from pipmind.safety.lease_sql import LeaseBinding, _binding
from pipmind.safety.session import SessionBinding, account_key

BINDING = SessionBinding("synthetic-account", "demo", "USD")
LEASE = LeaseBinding(BINDING.account_id, BINDING.environment, "orders")
NOW = datetime(2026, 10, 21, 12, 0, 0, 123456)
SCHEMA = dict(_SCHEMA)


def records(*, count=1, wide="exact original naïve 😀\n"):
    result = {}
    for table, columns in _SCHEMA:
        rows = []
        for index in range(count if "observations" in table else 1):
            row = []
            for name, kind, length, nullable, _ in columns:
                if name == "account_key":
                    value = account_key(BINDING)
                elif name == "binding_key":
                    value = _binding(LEASE)
                elif name == "account_id":
                    value = BINDING.account_id
                elif name == "environment":
                    value = BINDING.environment
                elif name == "currency":
                    value = BINDING.currency
                elif name == "resource":
                    value = LEASE.resource
                elif nullable:
                    value = None
                elif kind == "datetime2":
                    value = NOW + timedelta(microseconds=index)
                elif kind == "bigint":
                    value = 1
                elif kind == "bit":
                    value = False
                elif length == -1:
                    value = wide if name == "body" else "opaque exact retained value"
                elif name in ("sha256", "policy_book_sha256"):
                    value = "b" * 64
                elif length == 64:
                    value = "a" * 64
                else:
                    value = "row-" + f"{index:06d}"
                row.append(value)
            rows.append(tuple(row))
        result[table] = rows
    return result


class Cursor:
    """Small deterministic SQL-protocol double; never returns LOBs in row pages."""

    def __init__(self, rows=None):
        self.rows = records() if rows is None else rows
        self.allowed = set(required_locks(BINDING, LEASE))
        self.result, self.offset = [], 0
        self.fetch_sizes, self.chunks, self.queries, self.metadata = [], [], [], {}
        self.count_calls = {}
        self.bad_schema = self.short_chunk = self.missing_chunk = False
        self.change_count = self.lose_lock = self.fail_driver = False
        self.lock_reads = 0

    def execute(self, query, params=()):
        self.queries.append(query)
        self.offset = 0
        if self.fail_driver:
            raise OSError("private SQL connection and token detail")
        if query == "SELECT @@TRANCOUNT,XACT_STATE()":
            self.result = [(1, 1)]
        elif "APPLOCK_MODE" in query:
            self.lock_reads += 1
            allowed = params[0] in self.allowed and not (self.lose_lock and self.lock_reads > 6)
            self.result = [("Exclusive" if allowed else "NoLock",)]
        elif "FROM sys.columns" in query:
            self.metadata[params[0]] = self.metadata.get(params[0], 0) + 1
            self.result = []
            for name, kind, length, nullable, key in SCHEMA[params[0]]:
                precision, scale = {"bigint": (19, 0), "bit": (1, 0), "datetime2": (27, 6)}.get(
                    kind, (0, 0)
                )
                self.result.append(
                    (
                        name,
                        kind,
                        length,
                        precision,
                        scale,
                        nullable,
                        key,
                        "Latin1_General_100_BIN2" if key else None,
                    )
                )
            if self.bad_schema:
                self.result.append(("new_uncovered_column", "int", 4, 10, 0, False, 0, None))
        else:
            table = re.search(r"FROM (dbo\.pm_safety_\w+)", query)[1]
            columns = SCHEMA[table]
            primary = tuple(i for i, col in enumerate(columns) if col[4])
            if "SUBSTRING(CONVERT(varbinary(max)" in query:
                name = re.search(r"CONVERT\(varbinary\(max\),([a-z_]+)\)", query)[1]
                index = next(i for i, col in enumerate(columns) if col[0] == name)
                matching = [
                    row
                    for row in self.rows[table]
                    if tuple(row[i] for i in primary) == tuple(params[2:])
                ]
                assert len(matching) == 1
                raw = matching[0][index].encode("utf-16-le", "surrogatepass")
                chunk = raw[params[0] - 1 : params[0] - 1 + params[1]]
                if self.short_chunk:
                    chunk = chunk[:-1]
                self.chunks.append(len(chunk))
                self.result = [] if self.missing_chunk else [(chunk,)]
            else:
                selected = self.selected(table, params[0])
                if query.startswith("SELECT COUNT_BIG"):
                    self.count_calls[table] = self.count_calls.get(table, 0) + 1
                    change = int(self.change_count and self.count_calls[table] > 1)
                    self.result = [(len(selected) + change,)]
                else:
                    assert query.startswith("SELECT TOP (64)")
                    if len(params) > 1:
                        last = tuple(params[-len(primary) :])
                        selected = [
                            row for row in selected if tuple(row[i] for i in primary) > last
                        ]
                    self.result = [
                        tuple(
                            len(value.encode("utf-16-le", "surrogatepass"))
                            if col[2] == -1 and value is not None
                            else value
                            for value, col in zip(row, columns, strict=True)
                        )
                        for row in selected[:PAGE_ROWS]
                    ]
        return self

    def selected(self, table, key):
        columns = SCHEMA[table]
        if table.endswith("admission_books_v1"):
            journal = "dbo.pm_safety_admission_journal_v1"
            jcols = [col[0] for col in SCHEMA[journal]]
            referenced = {
                row[jcols.index("policy_book_sha256")]
                for row in self.rows[journal]
                if row[0] == key
            }
            selected = [row for row in self.rows[table] if row[0] in referenced]
        else:
            selected = [row for row in self.rows[table] if row[0] == key]
        primary = tuple(i for i, col in enumerate(columns) if col[4])
        return sorted(selected, key=lambda row: tuple(row[i] for i in primary))

    def fetchmany(self, size):
        assert 0 < size <= PAGE_ROWS
        self.fetch_sizes.append(size)
        value = self.result[self.offset : self.offset + size]
        self.offset += len(value)
        return value

    def fetchone(self):
        value = self.result[self.offset : self.offset + 1]
        self.offset += len(value)
        return value[0] if value else None

    def fetchall(self):
        raise AssertionError("unbounded fetchall forbidden")


def capture(cursor=None):
    return capture_in_transaction(cursor or Cursor(), BINDING, LEASE)


def test_complete_inventory_and_exact_codec_are_deterministic_and_frozen():
    first = capture()
    assert first == capture()
    assert tuple(row.name for row in first.tables) == COVERED_TABLES
    assert len(first.tables) == 15 and all(row.count == 1 for row in first.tables)
    encoded = encode_inventory(first)
    assert len(encoded.encode()) < MAX_INVENTORY_BYTES
    assert decode_inventory(encoded) == first
    assert "opaque exact" not in encoded and "token_sha256" not in encoded
    with pytest.raises(FrozenInstanceError):
        first.sha256 = "0" * 64
    with pytest.raises(FrozenInstanceError):
        first.tables[0].count = 0


def test_no_history_prefix_cap_and_mutation_at_final_old_row_changes_digest():
    rows = records(count=2049)
    before = capture(Cursor(rows))
    table = "dbo.pm_safety_session_observations_v1"
    summary = next(row for row in before.tables if row.name == table)
    assert summary.count == 2049
    body = next(i for i, col in enumerate(SCHEMA[table]) if col[0] == "body")
    last = rows[table][-1]
    rows[table][-1] = (*last[:body], "changed final retained event", *last[body + 1 :])
    after = capture(Cursor(rows))
    assert after.sha256 != before.sha256
    assert [a.name for a, b in zip(before.tables, after.tables, strict=True) if a != b] == [table]


def test_lob_transport_is_chunked_and_chunk_boundaries_do_not_change_value_hash(monkeypatch):
    rows = records(wide="😀é\n" * (CHUNK_BYTES + 1))
    cursor = Cursor(rows)
    result = capture(cursor)
    assert result == capture(Cursor(rows))
    assert max(cursor.chunks) == CHUNK_BYTES and len(cursor.chunks) > 20
    assert max(cursor.fetch_sizes) == PAGE_ROWS
    assert all("TOP (64)" in query for query in cursor.queries if "ORDER BY t." in query)
    # Odd byte boundaries may split a UTF-16 code unit/surrogate pair. No
    # intermediate text decoding or replacement may change the exact digest.
    monkeypatch.setattr("pipmind.safety.checkpoint_sql.CHUNK_BYTES", CHUNK_BYTES - 1)
    assert capture(Cursor(rows)) == result


def test_input_row_order_and_unrelated_account_or_unreferenced_books_do_not_change_inventory():
    rows = records(count=70)
    before = capture(Cursor(rows))
    for table, values in rows.items():
        values.reverse()
        if table.endswith("admission_books_v1"):
            values.append(("f" * 64, "unreferenced other account policy"))
        else:
            values.append(("f" * 64, *values[0][1:]))
    assert capture(Cursor(rows)) == before


@pytest.mark.parametrize(
    "fault",
    ["bad_schema", "short_chunk", "missing_chunk", "change_count", "lose_lock", "fail_driver"],
)
def test_incomplete_or_unknown_reads_refuse_instead_of_returning_a_partial_inventory(fault):
    cursor = Cursor()
    setattr(cursor, fault, True)
    with pytest.raises(CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        capture(cursor)


@pytest.mark.parametrize("index", range(6))
def test_each_required_exclusive_transaction_lock_is_mandatory(index):
    cursor = Cursor()
    cursor.allowed.remove(required_locks(BINDING, LEASE)[index])
    with pytest.raises(CheckpointUnavailable, match="^safety_inventory_unavailable$"):
        capture(cursor)


def test_binding_mismatch_and_corrupt_pin_cannot_be_attested_as_the_requested_account():
    cursor = Cursor()
    with pytest.raises(CheckpointRefused, match="^checkpoint_binding_mismatch$"):
        capture_in_transaction(cursor, BINDING, replace(LEASE, account_id="other"))
    assert cursor.queries == []
    table = "dbo.pm_safety_session_accounts_v1"
    row = cursor.rows[table][0]
    cursor.rows[table][0] = (*row[:-1], "EUR")
    with pytest.raises(CheckpointUnavailable):
        capture(cursor)


@pytest.mark.parametrize(
    "position,replacement",
    [
        (1, "varchar"),
        (2, 127),
        (3, 99),
        (4, 7),
        (5, True),
        (6, 0),
        (7, "SQL_Latin1_General_CP1_CI_AS"),
    ],
)
def test_exact_column_metadata_is_checked_not_only_table_row_counts(position, replacement):
    class ChangedSchema(Cursor):
        def execute(self, query, params=()):
            super().execute(query, params)
            if "FROM sys.columns" in query:
                original = self.result[0]
                self.result[0] = (*original[:position], replacement, *original[position + 1 :])
            return self

    with pytest.raises(CheckpointUnavailable):
        capture(ChangedSchema())


def test_schema_change_after_the_complete_read_and_wrong_native_type_refuse():
    class LateSchema(Cursor):
        def execute(self, query, params=()):
            super().execute(query, params)
            if "FROM sys.columns" in query and self.metadata[params[0]] > 1:
                self.result = []
            return self

    with pytest.raises(CheckpointUnavailable):
        capture(LateSchema())
    cursor = Cursor()
    table = "dbo.pm_safety_leases_v1"
    row = cursor.rows[table][0]
    cursor.rows[table][0] = (*row[:5], "1", *row[6:])
    with pytest.raises(CheckpointUnavailable):
        capture(cursor)


@pytest.mark.parametrize("value", [True, 1, "1", b"1", Decimal("1"), Decimal("1.0"), None, "", b""])
def test_native_scalar_types_and_decimal_representations_are_distinct(value):
    def digest(item):
        result = hashlib.sha256()
        _scalar(result, item)
        return result.hexdigest()

    peers = [True, 1, "1", b"1", Decimal("1"), Decimal("1.0"), None, "", b""]
    assert sum(digest(value) == digest(peer) for peer in peers) == 1


def test_native_datetime_uuid_binary_and_unicode_are_exact_without_decimal_context_mutation():
    values = [NOW, NOW.replace(microsecond=123457), UUID(int=42), bytes(range(256)), "naïve 😀\x00"]
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        before = context.copy()
        hashes = []
        for value in values + [Decimal("-0.00"), Decimal("1E+64")]:
            digest = hashlib.sha256()
            _scalar(digest, value)
            hashes.append(digest.hexdigest())
        assert context.prec == before.prec and context.traps == before.traps
        assert context.flags == before.flags and len(set(hashes)) == len(hashes)
    for value in (1.0, Decimal("NaN"), object()):
        with pytest.raises(CheckpointUnavailable):
            _scalar(hashlib.sha256(), value)


def test_inventory_codec_rejects_changed_hash_missing_table_and_noncanonical_or_duplicate_json():
    raw = encode_inventory(capture())
    for text in (
        raw + " ",
        raw.replace('"schema_version":1', '"schema_version":true'),
        raw.replace('"kind":', '"kind":"duplicate","kind":'),
    ):
        with pytest.raises(CheckpointRefused):
            decode_inventory(text)
    value = json.loads(raw)
    value["sha256"] = "0" * 64
    with pytest.raises(CheckpointRefused):
        decode_inventory(json.dumps(value))
    value["tables"].pop()
    with pytest.raises(CheckpointRefused):
        decode_inventory(json.dumps(value))


def test_native_backup_reference_is_strict_metadata_not_a_file_or_authority_check():
    inventory = capture()
    reference = NativeBackupReference(
        "backup-private-id", "a" * 64, "2026-10-21T12:00:00Z", inventory.sha256
    )
    assert reference.source_inventory_sha256 == inventory.sha256
    with pytest.raises(FrozenInstanceError):
        reference.backup_id = "other"
    for change in (
        {"sha256": "bad"},
        {"backup_id": ""},
        {"created_at": "2026-10-21T12:00:00-04:00"},
        {"source_inventory_sha256": False},
    ):
        with pytest.raises(CheckpointRefused):
            replace(reference, **change)
