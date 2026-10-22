"""Explicit Azure SQL Database physical pin; no native-profile fallback or authority."""

import hashlib
import json
from dataclasses import dataclass

PROFILE = "azure-sql-physical-v1"
MAX_ID_UNITS = 128
MAX_BODY_BYTES = 4096


class AzureIdentityRefused(Exception):
    """Fixed invalid, unsupported or mismatched identity refusal."""


class AzureIdentityUnavailable(Exception):
    """Identity or existing transaction evidence could not be established."""


def _identifier(value):
    try:
        return (
            type(value) is str
            and bool(value)
            and "\x00" not in value
            and len(value.encode("utf-16-le")) <= MAX_ID_UNITS * 2
        )
    except UnicodeError:
        return False


@dataclass(frozen=True, slots=True, repr=False)
class AzureDatabaseIdentity:
    logical_server: str
    database: str
    physical_database_name: str

    def __post_init__(self):
        if not all(
            _identifier(value)
            for value in (
                self.logical_server,
                self.database,
                self.physical_database_name,
            )
        ):
            raise AzureIdentityRefused("azure_identity_invalid")

    def __repr__(self):
        return "AzureDatabaseIdentity(<redacted>)"


def _capture(value):
    if type(value) is not AzureDatabaseIdentity:
        raise AzureIdentityRefused("azure_identity_invalid")
    return AzureDatabaseIdentity(value.logical_server, value.database, value.physical_database_name)


def encode_identity(value):
    """Canonical, profile-separated private evidence; never a native GUID/fork."""
    pin = _capture(value)
    return json.dumps(
        {
            "profile": PROFILE,
            "logical_server": pin.logical_server,
            "database": pin.database,
            "physical_database_name": pin.physical_database_name,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def decode_identity(body):
    try:
        if type(body) is not str or len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError
        value = json.loads(body, object_pairs_hook=_unique_object)
        if (
            type(value) is not dict
            or set(value) != {"profile", "logical_server", "database", "physical_database_name"}
            or value["profile"] != PROFILE
        ):
            raise ValueError
        result = AzureDatabaseIdentity(
            value["logical_server"], value["database"], value["physical_database_name"]
        )
        if encode_identity(result) != body:
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, AzureIdentityRefused):
        raise AzureIdentityRefused("azure_identity_invalid") from None


def identity_sha256(value):
    """Digest includes the distinct profile and all exact identifier strings."""
    return hashlib.sha256(encode_identity(value).encode("utf-8")).hexdigest()


_CONTEXT_SQL = """SELECT CONVERT(int,SERVERPROPERTY('EngineEdition')),
CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME(),DB_ID(),
XACT_STATE(),@@TRANCOUNT"""
_PHYSICAL_SQL = """SELECT database_id,name,physical_database_name
FROM sys.databases WHERE database_id=DB_ID()"""


def _row(cursor, query, columns):
    cursor.execute(query)
    row = cursor.fetchone()
    if type(row) not in (tuple, list) or len(row) != columns or cursor.fetchone() is not None:
        raise AzureIdentityUnavailable("azure_identity_unavailable")
    return tuple(row)


def _context(cursor, expected):
    if getattr(getattr(cursor, "connection", None), "autocommit", None) is not False:
        raise AzureIdentityUnavailable("azure_identity_transaction_required")
    row = _row(cursor, _CONTEXT_SQL, 6)
    edition, server, database, database_id, state, depth = row
    if type(edition) is not int:
        raise AzureIdentityUnavailable("azure_identity_unavailable")
    if edition != 5:
        raise AzureIdentityRefused("azure_identity_unsupported")
    if (
        not _identifier(server)
        or not _identifier(database)
        or type(database_id) is not int
        or database_id <= 0
        or type(state) is not int
        or state != 1
        or type(depth) is not int
        or depth <= 0
    ):
        raise AzureIdentityUnavailable("azure_identity_unavailable")
    if (server, database) != (expected.logical_server, expected.database):
        raise AzureIdentityRefused("azure_identity_mismatch")
    return row


def _physical(cursor, context, expected):
    row = _row(cursor, _PHYSICAL_SQL, 3)
    database_id, database, physical = row
    if (
        type(database_id) is not int
        or database_id <= 0
        or not _identifier(database)
        or not _identifier(physical)
    ):
        raise AzureIdentityUnavailable("azure_identity_unavailable")
    if (database_id, database) != (context[3], context[2]):
        raise AzureIdentityRefused("azure_identity_mismatch")
    if physical != expected.physical_database_name:
        raise AzureIdentityRefused("azure_identity_mismatch")
    return row


def read_expected_identity(cursor, expected):
    """Check an explicit pin twice through an existing active transaction cursor.

    No connection, transaction control, adoption, protected callback or authority
    assertion occurs. Context and physical rows are checked before and after the
    observation only; callers must bracket their own future protected operation.
    Trusted connector endpoint/TLS binding remains a caller prerequisite. Changes
    between observations (including ABA) are not excluded by this reader.
    """
    expected = _capture(expected)
    try:
        before = _context(cursor, expected)
        physical_before = _physical(cursor, before, expected)
        after = _context(cursor, expected)
        physical_after = _physical(cursor, after, expected)
        if before != after or physical_before != physical_after:
            raise AzureIdentityRefused("azure_identity_mismatch")
        if getattr(getattr(cursor, "connection", None), "autocommit", None) is not False:
            raise AzureIdentityUnavailable("azure_identity_transaction_required")
        return expected
    except (AzureIdentityRefused, AzureIdentityUnavailable):
        raise
    except Exception:
        raise AzureIdentityUnavailable("azure_identity_unavailable") from None
