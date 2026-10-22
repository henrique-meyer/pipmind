"""Opt-in restricted metadata reader, never history or recovery authority.

Only trusted administrative setup may execute the returned CREATE text, with
ANSI_NULLS and QUOTED_IDENTIFIER ON. Runtime never initializes schema or grants
permissions. Stable trusted administration/DDL is required: definition checks
cannot attest against a malicious database owner temporarily replacing code.
"""

import hashlib
from dataclasses import dataclass

from pipmind.safety.command_checkpoint_sql import _SCHEMA

MODULE_NAME = "dbo.pm_safety_command_catalog_v1"
_NAME = MODULE_NAME.split(".")[1]
_MAX_OBJECTS = len(_SCHEMA)
_MAX_COLUMNS = sum(len(columns) for _, columns in _SCHEMA)
_PREFIX = "o.name LIKE N'pm[_]safety[_]command[_]%'"
_FILTER = f"{_PREFIX} AND NOT (s.name=N'dbo' AND o.name=N'{_NAME}')"
MODULE_DEFINITION = f"""CREATE PROCEDURE {MODULE_NAME}
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    IF USER_ID() <> 1 OR @@TRANCOUNT < 1 OR XACT_STATE() <> 1
        THROW 50053, 'command_catalog_unavailable', 1;
    SELECT 1 AS protocol_version, DB_ID() AS database_id,
        DB_NAME() AS database_name, USER_ID() AS principal_id,
        @@TRANCOUNT AS transaction_count, XACT_STATE() AS transaction_state;
    SELECT TOP ({_MAX_OBJECTS + 1}) s.name AS schema_name, o.name AS object_name,
        RTRIM(o.type) AS object_type, o.object_id AS object_id
    FROM sys.objects AS o JOIN sys.schemas AS s ON s.schema_id=o.schema_id
    WHERE {_FILTER}
    ORDER BY s.name COLLATE Latin1_General_100_BIN2,
        o.name COLLATE Latin1_General_100_BIN2, o.type;
    SELECT TOP ({_MAX_COLUMNS + 1}) s.name AS schema_name, o.name AS object_name,
        c.column_id AS column_id, c.name AS column_name, t.name AS sql_type,
        c.max_length AS max_length, c.precision AS precision, c.scale AS scale,
        c.is_nullable AS is_nullable, COALESCE(pk.key_ordinal,0) AS primary_key_ordinal,
        c.collation_name AS collation_name
    FROM sys.objects AS o JOIN sys.schemas AS s ON s.schema_id=o.schema_id
    JOIN sys.columns AS c ON c.object_id=o.object_id
    JOIN sys.types AS t ON t.user_type_id=c.user_type_id
    LEFT JOIN (SELECT ic.object_id,ic.column_id,ic.key_ordinal FROM sys.index_columns AS ic
        JOIN sys.indexes AS i ON i.object_id=ic.object_id AND i.index_id=ic.index_id
        WHERE i.is_primary_key=1) AS pk
        ON pk.object_id=c.object_id AND pk.column_id=c.column_id
    WHERE {_FILTER}
    ORDER BY s.name COLLATE Latin1_General_100_BIN2,
        o.name COLLATE Latin1_General_100_BIN2,c.column_id;
END"""
MODULE_DEFINITION_SHA256 = hashlib.sha256(MODULE_DEFINITION.encode("utf-16-le")).hexdigest()
_DEFINITION_BYTES = len(MODULE_DEFINITION.encode("utf-16-le"))

_UNSAFE = (
    *(
        f"HAS_PERMS_BY_NAME(N'{MODULE_NAME}',N'OBJECT',N'{permission}')"
        for permission in ("ALTER", "CONTROL", "TAKE OWNERSHIP")
    ),
    *(
        f"HAS_PERMS_BY_NAME(N'dbo',N'SCHEMA',N'{permission}')"
        for permission in ("ALTER", "CONTROL", "TAKE OWNERSHIP")
    ),
    *(
        f"HAS_PERMS_BY_NAME(DB_NAME(),N'DATABASE',N'{permission}')"
        for permission in ("CONTROL", "ALTER", "CREATE PROCEDURE", "ALTER ANY SCHEMA")
    ),
    "HAS_PERMS_BY_NAME(N'dbo',N'USER',N'IMPERSONATE')",
)
_PROOF_NAMES = (
    "database_id",
    "database_name",
    "caller_principal_id",
    "transaction_count",
    "transaction_state",
    "object_id",
    "object_type",
    "schema_name",
    "object_name",
    "owner_id",
    "execute_as_principal_id",
    "uses_ansi_nulls",
    "uses_quoted_identifier",
    "definition",
    "parameter_count",
    "can_execute",
    "can_view_definition",
    *(f"unsafe_{index}" for index in range(len(_UNSAFE))),
)
_PROOF_SQL = (
    "SELECT DB_ID() AS database_id,DB_NAME() AS database_name,USER_ID() AS caller_principal_id,"
    "@@TRANCOUNT AS transaction_count,XACT_STATE() AS transaction_state,"
    "o.object_id AS object_id,RTRIM(o.type) AS object_type,s.name AS schema_name,"
    "o.name AS object_name,COALESCE(o.principal_id,s.principal_id) AS owner_id,"
    "m.execute_as_principal_id AS execute_as_principal_id,m.uses_ansi_nulls AS uses_ansi_nulls,"
    "m.uses_quoted_identifier AS uses_quoted_identifier,"
    f"CASE WHEN DATALENGTH(m.definition)={_DEFINITION_BYTES} THEN m.definition END AS definition,"
    "(SELECT COUNT(*) FROM sys.parameters p WHERE p.object_id=o.object_id) AS parameter_count,"
    f"HAS_PERMS_BY_NAME(N'{MODULE_NAME}',N'OBJECT',N'EXECUTE') AS can_execute,"
    f"HAS_PERMS_BY_NAME(N'{MODULE_NAME}',N'OBJECT',N'VIEW DEFINITION') AS can_view_definition,"
    + ",".join(f"{sql} AS unsafe_{index}" for index, sql in enumerate(_UNSAFE))
    + " FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id "
    "LEFT JOIN sys.sql_modules m ON m.object_id=o.object_id "
    f"WHERE s.name=N'dbo' AND o.name=N'{_NAME}'"
)
_HEADER_NAMES = (
    "protocol_version",
    "database_id",
    "database_name",
    "principal_id",
    "transaction_count",
    "transaction_state",
)
_OBJECT_NAMES = ("schema_name", "object_name", "object_type", "object_id")
_COLUMN_NAMES = (
    "schema_name",
    "object_name",
    "column_id",
    "column_name",
    "sql_type",
    "max_length",
    "precision",
    "scale",
    "is_nullable",
    "primary_key_ordinal",
    "collation_name",
)


class CommandCatalogRefused(Exception):
    """Fixed invalid explicit-input error; no database action occurred."""


class CommandCatalogUnavailable(Exception):
    """Fixed incomplete/untrusted metadata error; no partial success."""


@dataclass(frozen=True, slots=True)
class CatalogObject:
    schema_name: str
    object_name: str
    object_type: str
    object_id: int


@dataclass(frozen=True, slots=True)
class CatalogColumn:
    schema_name: str
    object_name: str
    column_id: int
    name: str
    sql_type: str
    max_length: int
    precision: int
    scale: int
    is_nullable: bool
    primary_key_ordinal: int
    collation_name: str | None


@dataclass(frozen=True, slots=True, repr=False)
class CommandCatalog:
    database_name: str
    database_id: int
    module_object_id: int
    module_definition_sha256: str
    caller_principal_id: int
    classification: str
    objects: tuple[CatalogObject, ...]
    columns: tuple[CatalogColumn, ...]

    @property
    def dispatch_authorized(self):
        return False


def bootstrap_sql():
    """Return CREATE text only; never execute DDL, SET options or permissions."""
    return MODULE_DEFINITION


def _unavailable():
    raise CommandCatalogUnavailable("command_catalog_unavailable")


def _int(value, *, minimum=None):
    return type(value) is int and (minimum is None or value >= minimum)


def _bit(value):
    return type(value) in (int, bool) and value in (0, 1)


def _rows(cur, names, limit):
    if cur.description is None or tuple(column[0] for column in cur.description) != names:
        _unavailable()
    rows = cur.fetchmany(limit + 1)
    if len(rows) > limit or cur.fetchmany(1):
        _unavailable()
    if any(len(row) != len(names) for row in rows):
        _unavailable()
    return tuple(tuple(row) for row in rows)


def _proof(cur, database):
    cur.execute(_PROOF_SQL)
    rows = _rows(cur, _PROOF_NAMES, 1)
    if len(rows) != 1 or cur.nextset():
        _unavailable()
    row = rows[0]
    if not (
        _int(row[0], minimum=1)
        and type(row[1]) is str
        and row[1] == database
        and _int(row[2], minimum=2)
        and _int(row[3], minimum=1)
        and _int(row[4])
        and row[4] == 1
        and _int(row[5], minimum=1)
        and row[6:9] == ("P", "dbo", _NAME)
        and _int(row[9])
        and row[9] == 1
        and _int(row[10])
        and row[10] == -2
        and all(_bit(value) and value == 1 for value in row[11:13])
        and type(row[13]) is str
        and row[13] == MODULE_DEFINITION
        and _int(row[14])
        and row[14] == 0
        and all(_int(value) and value == 1 for value in row[15:17])
        and all(_int(value) and value == 0 for value in row[17:])
    ):
        _unavailable()
    return row


def _catalog(objects, columns):
    if not objects:
        if columns:
            _unavailable()
        return "absent", (), ()
    expected = sorted(_SCHEMA)
    if len(objects) != len(expected):
        _unavailable()
    for row, (table, _) in zip(objects, expected, strict=True):
        schema, name = table.split(".")
        if row[:3] != (schema, name, "U") or not _int(row[3], minimum=1):
            _unavailable()
    if len({row[3] for row in objects}) != len(objects) or len(columns) != _MAX_COLUMNS:
        _unavailable()
    offset = 0
    for table, declared in expected:
        schema, table_name = table.split(".")
        for index, (name, kind, length, nullable, ordinal) in enumerate(declared, 1):
            row = columns[offset]
            precision, scale = {"bigint": (19, 0), "bit": (1, 0), "datetime2": (26, 6)}.get(
                kind, (0, 0)
            )
            if not (
                row[:2] == (schema, table_name)
                and _int(row[2])
                and row[2] == index
                and row[3:5] == (name, kind)
                and all(_int(value) for value in row[5:8])
                and row[5:8] == (length, precision, scale)
                and _bit(row[8])
                and row[8] == nullable
                and _int(row[9])
                and row[9] == ordinal
                and (row[10] is None or type(row[10]) is str)
                and (not ordinal or row[10] == "Latin1_General_100_BIN2")
            ):
                _unavailable()
            offset += 1
    return (
        "supported",
        tuple(CatalogObject(*row) for row in objects),
        tuple(CatalogColumn(*row[:8], bool(row[8]), *row[9:]) for row in columns),
    )


def read_command_catalog_in_transaction(cur, *, expected_database):
    """Read supported command metadata using one existing transaction/cursor.

    Trusted stable DDL is a prerequisite. Pre/post checks detect observed drift,
    not transient malicious administrator substitution. Unsafe-permission checks
    are conservative, not an exhaustive audit of signing/ownership escalation.
    No command history, profile migration, locks, connections or commits occur.
    """
    try:
        valid_database = (
            type(expected_database) is str
            and bool(expected_database)
            and "\x00" not in expected_database
            and len(expected_database.encode("utf-16-le")) <= 256
        )
    except UnicodeError:
        valid_database = False
    if not valid_database:
        raise CommandCatalogRefused("invalid_command_catalog_request")
    try:
        if getattr(getattr(cur, "connection", None), "autocommit", None) is not False:
            _unavailable()
        before = _proof(cur, expected_database)
        cur.execute(f"EXEC {MODULE_NAME}")
        headers = _rows(cur, _HEADER_NAMES, 1)
        expected_header = (1, before[0], before[1], 1, before[3], 1)
        if (
            len(headers) != 1
            or headers[0] != expected_header
            or not all(_int(headers[0][index]) for index in (0, 1, 3, 4, 5))
            or not cur.nextset()
        ):
            _unavailable()
        objects = _rows(cur, _OBJECT_NAMES, _MAX_OBJECTS)
        if not cur.nextset():
            _unavailable()
        columns = _rows(cur, _COLUMN_NAMES, _MAX_COLUMNS)
        if cur.nextset():
            _unavailable()
        classification, objects, columns = _catalog(objects, columns)
        after = _proof(cur, expected_database)
        if before != after:
            _unavailable()
        return CommandCatalog(
            before[1],
            before[0],
            before[5],
            MODULE_DEFINITION_SHA256,
            before[2],
            classification,
            objects,
            columns,
        )
    except CommandCatalogUnavailable:
        raise
    except Exception:
        raise CommandCatalogUnavailable("command_catalog_unavailable") from None
