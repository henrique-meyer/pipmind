"""Complete, streaming safety-table inventory; never a backup or recovery permit."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pipmind.contracts import ContractError, loads, utc
from pipmind.safety.lease_sql import LeaseRefused, _binding
from pipmind.safety.session import SessionBinding, SessionRefused, account_key, validate_binding

TABLE_VERSION = "safety-operational-tables-v1"
MAX_INVENTORY_BYTES = 65536
PAGE_ROWS = 64
CHUNK_BYTES = 65536


class CheckpointRefused(Exception):
    """Fixed invalid inventory/reference input; no authority is conferred."""


class CheckpointUnavailable(Exception):
    """Missing locks/schema or incomplete SQL read; no partial inventory returned."""


def _need(value, code="invalid_safety_inventory"):
    if not value:
        raise CheckpointRefused(code)


def _unavailable():
    raise CheckpointUnavailable("safety_inventory_unavailable")


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _id(value):
    try:
        return (
            type(value) is str
            and bool(value)
            and len(value) <= 128
            and value.strip() == value
            and "\x00" not in value
            and len(value.encode("utf-16-le")) <= 256
        )
    except UnicodeError:
        return False


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class TableDigest:
    name: str
    count: int
    sha256: str

    def __post_init__(self):
        _need(type(self.name) is str and self.name in COVERED_TABLES)
        _need(type(self.count) is int and 0 <= self.count <= 9223372036854775807)
        _need(_hash(self.sha256))


@dataclass(frozen=True, slots=True)
class Inventory:
    binding: SessionBinding
    resource: str
    table_version: str
    tables: tuple[TableDigest, ...]
    sha256: str

    def __post_init__(self):
        try:
            validate_binding(self.binding)
        except SessionRefused:
            raise CheckpointRefused("invalid_safety_inventory") from None
        _need(
            _id(self.resource)
            and type(self.table_version) is str
            and self.table_version == TABLE_VERSION
        )
        _need(
            type(self.tables) is tuple
            and all(type(row) is TableDigest for row in self.tables)
            and tuple(row.name for row in self.tables) == COVERED_TABLES
            and _hash(self.sha256)
        )
        _need(self.sha256 == _inventory_hash(self.binding, self.resource, self.tables))


@dataclass(frozen=True, slots=True)
class NativeBackupReference:
    """Independently supplied identity, not verification that a backup file exists."""

    backup_id: str
    sha256: str
    created_at: str
    source_inventory_sha256: str

    def __post_init__(self):
        _need(
            _id(self.backup_id) and _hash(self.sha256) and _hash(self.source_inventory_sha256),
            "invalid_native_backup_reference",
        )
        try:
            _need(type(self.created_at) is str, "invalid_native_backup_reference")
            utc(self.created_at)
        except ContractError:
            raise CheckpointRefused("invalid_native_backup_reference") from None


# (column, SQL type, max_length bytes, nullable, primary-key ordinal).
# Only datetime2 uses scale=6; all remaining precision/scale facts are derived
# below from SQL Server's fixed built-in types, never arbitrary archive metadata.
def _col(name, kind, length, nullable=False, key=0):
    return name, kind, length, nullable, key


A = _col("account_key", "char", 64, key=1)
IDENTITY = (
    _col("account_id", "nvarchar", 256),
    _col("environment", "varchar", 4),
    _col("currency", "char", 3),
)
BODY = (
    _col("body", "nvarchar", -1),
    _col("body_sha256", "char", 64),
    _col("receipt", "nvarchar", -1),
    _col("receipt_sha256", "char", 64),
)

_SCHEMA = (
    (
        "dbo.pm_safety_leases_v1",
        (
            _col("binding_key", "char", 64, key=1),
            _col("account_id", "nvarchar", 256),
            _col("environment", "nvarchar", 256),
            _col("resource", "nvarchar", 256),
            _col("authority_epoch", "nvarchar", 256),
            _col("generation", "bigint", 8),
            _col("owner_id", "nvarchar", 256, True),
            _col("token_sha256", "char", 64, True),
            _col("expires_at", "datetime2", 8, True),
        ),
    ),
    ("dbo.pm_safety_session_accounts_v1", (A, *IDENTITY)),
    (
        "dbo.pm_safety_session_sessions_v1",
        (
            A,
            _col("session_id", "nvarchar", 256, key=2),
            _col("started_at", "datetime2", 8),
            _col("ends_at", "datetime2", 8),
            _col("spec", "nvarchar", -1),
            _col("spec_sha256", "char", 64),
            _col("state", "nvarchar", -1),
            _col("state_sha256", "char", 64),
        ),
    ),
    (
        "dbo.pm_safety_session_observations_v1",
        (
            A,
            _col("session_id", "nvarchar", 256, key=2),
            _col("observation_id", "nvarchar", 256, key=3),
            _col("as_of", "datetime2", 8),
            *BODY,
        ),
    ),
    ("dbo.pm_safety_admission_accounts_v1", (A, *IDENTITY)),
    (
        "dbo.pm_safety_admission_books_v1",
        (
            _col("sha256", "char", 64, key=1),
            _col("body", "nvarchar", -1),
        ),
    ),
    (
        "dbo.pm_safety_admission_journal_v1",
        (
            A,
            _col("request_id", "nvarchar", 256, key=2),
            _col("session_id", "nvarchar", 256),
            _col("policy_book_sha256", "char", 64),
            _col("request", "nvarchar", -1),
            _col("request_sha256", "char", 64),
            _col("receipt", "nvarchar", -1),
            _col("receipt_sha256", "char", 64),
            _col("reserved", "bit", 1),
        ),
    ),
    (
        "dbo.pm_safety_admission_reservations_v1",
        (
            A,
            _col("request_id", "nvarchar", 256, key=2),
            _col("session_id", "nvarchar", 256),
            _col("body", "nvarchar", -1),
            _col("sha256", "char", 64),
        ),
    ),
    (
        "dbo.pm_safety_admission_floors_v1",
        (
            A,
            _col("session_id", "nvarchar", 256, key=2),
            _col("external_count", "bigint", 8),
        ),
    ),
    (
        "dbo.pm_safety_controls_v1",
        (
            A,
            *IDENTITY,
            _col("revision", "bigint", 8),
            _col("killed", "bit", 1),
            _col("paused", "bit", 1),
        ),
    ),
    (
        "dbo.pm_safety_control_overrides_v1",
        (
            A,
            _col("id", "nvarchar", 256, key=2),
            *BODY,
            _col("revision", "bigint", 8),
        ),
    ),
    ("dbo.pm_safety_control_suggestions_v1", (A, _col("id", "nvarchar", 256, key=2), *BODY)),
    ("dbo.pm_safety_control_dispositions_v1", (A, _col("id", "nvarchar", 256, key=2), *BODY)),
    (
        "dbo.pm_safety_integration_accounts_v1",
        (
            A,
            *IDENTITY,
            _col("scope", "nvarchar", -1, True),
            _col("scope_sha256", "char", 64, True),
            _col("hold_code", "varchar", 64),
        ),
    ),
    (
        "dbo.pm_safety_integration_journal_v1",
        (
            A,
            _col("channel", "varchar", 10, key=2),
            _col("record_id", "nvarchar", 256, key=3),
            *BODY,
        ),
    ),
)
COVERED_TABLES = tuple(name for name, _ in _SCHEMA)


def _payload(binding, resource, tables):
    return {
        "schema_version": 1,
        "kind": "safety-operational-inventory",
        "binding": {
            "account_id": binding.account_id,
            "environment": binding.environment,
            "currency": binding.currency,
        },
        "resource": resource,
        "table_version": TABLE_VERSION,
        "tables": [{"name": row.name, "count": row.count, "sha256": row.sha256} for row in tables],
    }


def _inventory_hash(binding, resource, tables):
    return hashlib.sha256(_json(_payload(binding, resource, tables)).encode()).hexdigest()


def encode_inventory(value):
    _need(type(value) is Inventory)
    value.__post_init__()
    raw = _json({**_payload(value.binding, value.resource, value.tables), "sha256": value.sha256})
    _need(len(raw.encode()) <= MAX_INVENTORY_BYTES)
    return raw


def decode_inventory(raw):
    _need(type(raw) is str and len(raw) <= MAX_INVENTORY_BYTES)
    try:
        _need(len(raw.encode()) <= MAX_INVENTORY_BYTES)
        value = loads(raw)
        _need(
            type(value) is dict
            and set(value)
            == {
                "schema_version",
                "kind",
                "binding",
                "resource",
                "table_version",
                "tables",
                "sha256",
            }
        )
        _need(
            type(value["schema_version"]) is int
            and value["schema_version"] == 1
            and value["kind"] == "safety-operational-inventory"
        )
        _need(
            type(value["binding"]) is dict
            and set(value["binding"]) == {"account_id", "environment", "currency"}
        )
        _need(type(value["tables"]) is list and len(value["tables"]) == len(COVERED_TABLES))
        for row in value["tables"]:
            _need(type(row) is dict and set(row) == {"name", "count", "sha256"})
        result = Inventory(
            SessionBinding(**value["binding"]),
            value["resource"],
            value["table_version"],
            tuple(TableDigest(**row) for row in value["tables"]),
            value["sha256"],
        )
        _need(encode_inventory(result) == raw)
        return result
    except (ContractError, UnicodeError, ValueError, TypeError, RecursionError):
        raise CheckpointRefused("invalid_safety_inventory") from None


def _frame(digest, tag, data):
    digest.update(tag + str(len(data)).encode("ascii") + b":" + data)


def _scalar(digest, value):
    """Exact typed SQL-native values; no float coercion or ambient Decimal math."""
    kind = type(value)
    if value is None:
        _frame(digest, b"N", b"")
    elif kind is bool:
        _frame(digest, b"B", b"1" if value else b"0")
    elif kind is int:
        _frame(digest, b"I", str(value).encode("ascii"))
    elif kind is str:
        _frame(digest, b"S", value.encode("utf-16-le", errors="surrogatepass"))
    elif kind is bytes:
        _frame(digest, b"Y", value)
    elif kind is datetime and value.tzinfo is None:
        _frame(digest, b"T", value.isoformat(timespec="microseconds").encode("ascii"))
    elif kind is Decimal and value.is_finite():
        _frame(digest, b"D", str(value).encode("ascii"))
    elif kind is UUID:
        _frame(digest, b"U", value.bytes)
    else:
        _unavailable()


def _schema(cur, table, columns):
    cur.execute(
        "SELECT c.name,t.name,c.max_length,c.precision,c.scale,c.is_nullable,"
        "COALESCE(pk.key_ordinal,0),c.collation_name FROM sys.columns c "
        "JOIN sys.types t ON t.user_type_id=c.user_type_id "
        "LEFT JOIN (SELECT ic.object_id,ic.column_id,ic.key_ordinal FROM sys.index_columns ic "
        "JOIN sys.indexes i ON i.object_id=ic.object_id AND i.index_id=ic.index_id "
        "WHERE i.is_primary_key=1) AS pk "
        "ON pk.object_id=c.object_id AND pk.column_id=c.column_id "
        "WHERE c.object_id=OBJECT_ID(%s,'U') ORDER BY c.column_id",
        (table,),
    )
    rows = cur.fetchmany(PAGE_ROWS)
    if len(rows) != len(columns) or cur.fetchmany(PAGE_ROWS):
        _unavailable()
    for row, (name, kind, length, nullable, key) in zip(rows, columns, strict=True):
        precision, scale = {"bigint": (19, 0), "bit": (1, 0), "datetime2": (26, 6)}.get(
            kind, (0, 0)
        )
        if tuple(row[:7]) != (name, kind, length, precision, scale, nullable, key):
            _unavailable()
        if key and row[7] != "Latin1_General_100_BIN2":
            _unavailable()


def required_locks(binding, lease_binding):
    try:
        key = account_key(binding)
        lease_key = _binding(lease_binding)
        _need(
            (binding.account_id, binding.environment)
            == (lease_binding.account_id, lease_binding.environment),
            "checkpoint_binding_mismatch",
        )
    except (SessionRefused, LeaseRefused):
        raise CheckpointRefused("checkpoint_binding_mismatch") from None
    return (
        "pipmind-safety-lease-v1:" + lease_key,
        "pipmind-safety-session-v1:" + key,
        "pipmind-safety-controls-v1:" + key,
        "pipmind-safety-recovery-v1:" + key,
        "pipmind-safety-integration-v1:" + key,
        "pipmind-safety-admission-v1:" + key,
    )


def _locks(cur, resources):
    cur.execute("SELECT @@TRANCOUNT,XACT_STATE()")
    row = cur.fetchone()
    if row is None or type(row[0]) is not int or row[0] < 1 or row[1] != 1:
        _unavailable()
    for resource in resources:
        cur.execute("SELECT APPLOCK_MODE('public',%s,'Transaction')", (resource,))
        if cur.fetchone() != ("Exclusive",):
            _unavailable()


def _where(table, key, lease_key):
    if table == "dbo.pm_safety_leases_v1":
        return "t.binding_key=%s", (lease_key,)
    if table == "dbo.pm_safety_admission_books_v1":
        return (
            "EXISTS(SELECT 1 FROM dbo.pm_safety_admission_journal_v1 j WITH(HOLDLOCK) "
            "WHERE j.account_key=%s AND j.policy_book_sha256=t.sha256)",
            (key,),
        )
    return "t.account_key=%s", (key,)


def _count(cur, table, where, params):
    cur.execute(f"SELECT COUNT_BIG(*) FROM {table} t WITH(HOLDLOCK) WHERE {where}", params)
    row = cur.fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        _unavailable()
    return row[0]


def _capture_table(cur, table, columns, key, lease_key, binding, lease_binding):
    where, params = _where(table, key, lease_key)
    expected = _count(cur, table, where, params)
    primary = tuple(col[0] for col in sorted(columns, key=lambda col: col[4]) if col[4])
    selected = ",".join(
        f"DATALENGTH(t.{name})" if length == -1 else f"t.{name}"
        for name, _, length, _, _ in columns
    )
    positions = tuple(index for index, col in enumerate(columns) if col[0] in primary)
    order = ",".join("t." + name for name in primary)
    digest = hashlib.sha256(b"pipmind-safety-table-v1\x00")
    _frame(digest, b"H", _json([table, columns]).encode())
    seen, last = 0, None
    while True:
        suffix, page_params = "", params
        if last is not None:
            terms, extra = [], []
            for index, name in enumerate(primary):
                terms.append(
                    "("
                    + " AND ".join(
                        [f"t.{prior}=%s" for prior in primary[:index]] + [f"t.{name}>%s"]
                    )
                    + ")"
                )
                extra.extend(last[: index + 1])
            suffix, page_params = " AND (" + " OR ".join(terms) + ")", params + tuple(extra)
        cur.execute(
            f"SELECT TOP ({PAGE_ROWS}) {selected} FROM {table} t WITH(HOLDLOCK) "
            f"WHERE {where}{suffix} ORDER BY {order}",
            page_params,
        )
        rows = cur.fetchmany(PAGE_ROWS)
        if cur.fetchmany(PAGE_ROWS):
            _unavailable()
        if not rows:
            break
        for row in rows:
            if len(row) != len(columns):
                _unavailable()
            values = dict(zip((col[0] for col in columns), row, strict=True))
            if "account_id" in values:
                expected_binding = {
                    "account_id": binding.account_id,
                    "environment": binding.environment,
                }
                if "currency" in values:
                    expected_binding["currency"] = binding.currency
                else:
                    expected_binding["resource"] = lease_binding.resource
                if any(values[name] != value for name, value in expected_binding.items()):
                    _unavailable()
            identity = tuple(row[index] for index in positions)
            if identity == last or any(type(value) is not str for value in identity):
                _unavailable()
            row_hash = hashlib.sha256(b"pipmind-safety-row-v1\x00")
            for value, (name, kind, length, nullable, _) in zip(row, columns, strict=True):
                if value is None:
                    if not nullable:
                        _unavailable()
                    _scalar(row_hash, None)
                elif length != -1:
                    native = {
                        "char": str,
                        "varchar": str,
                        "nvarchar": str,
                        "bigint": int,
                        "bit": bool,
                        "datetime2": datetime,
                    }[kind]
                    if type(value) is not native:
                        _unavailable()
                    if native is str and len(value.encode("utf-16-le", "surrogatepass")) > (
                        length if kind == "nvarchar" else 2 * length
                    ):
                        _unavailable()
                    if native is int and not -9223372036854775808 <= value <= 9223372036854775807:
                        _unavailable()
                    _scalar(row_hash, value)
                else:
                    if type(value) is not int or value < 0 or (kind == "nvarchar" and value % 2):
                        _unavailable()
                    row_hash.update(b"S" + str(value).encode("ascii") + b":")
                    offset = 0
                    while offset < value:
                        amount = min(CHUNK_BYTES, value - offset)
                        predicate = " AND ".join(f"{part}=%s" for part in primary)
                        cur.execute(
                            f"SELECT SUBSTRING(CONVERT(varbinary(max),{name}),%s,%s) "
                            f"FROM {table} WITH(HOLDLOCK) WHERE {predicate}",
                            (offset + 1, amount, *identity),
                        )
                        chunk = cur.fetchone()
                        if chunk is None or type(chunk[0]) is not bytes or len(chunk[0]) != amount:
                            _unavailable()
                        row_hash.update(chunk[0])
                        offset += amount
            _frame(digest, b"R", row_hash.digest())
            seen += 1
            if seen > expected:
                _unavailable()
            last = identity
        if len(rows) < PAGE_ROWS:
            break
    if seen != expected or _count(cur, table, where, params) != expected:
        _unavailable()
    return TableDigest(table, seen, digest.hexdigest())


def capture_in_transaction(cur, binding, lease_binding):
    """Inventory every covered row under caller-owned locks; never connect or commit."""
    resources = required_locks(binding, lease_binding)
    key, lease_key = account_key(binding), _binding(lease_binding)
    try:
        _locks(cur, resources)
        for table, columns in _SCHEMA:
            _schema(cur, table, columns)
        tables = tuple(
            _capture_table(cur, table, columns, key, lease_key, binding, lease_binding)
            for table, columns in _SCHEMA
        )
        # Detect a schema change during the complete scan rather than accepting it.
        for table, columns in _SCHEMA:
            _schema(cur, table, columns)
        _locks(cur, resources)
        result = Inventory(
            binding,
            lease_binding.resource,
            TABLE_VERSION,
            tables,
            _inventory_hash(binding, lease_binding.resource, tables),
        )
        encode_inventory(result)
        return result
    except CheckpointUnavailable:
        raise
    except Exception:
        raise CheckpointUnavailable("safety_inventory_unavailable") from None
