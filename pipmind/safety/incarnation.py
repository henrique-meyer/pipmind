"""Native SQL Server/Managed Instance database identity; no Azure SQL DB fallback."""

import json
from dataclasses import dataclass
from uuid import UUID


class IncarnationRefused(Exception):
    """Fixed invalid/unsupported incarnation refusal."""


class IncarnationUnavailable(Exception):
    """Native database identity cannot be established."""


def _text(value):
    try:
        return (
            type(value) is str
            and bool(value)
            and value == value.strip()
            and "\x00" not in value
            and len(value.encode("utf-16-le")) <= 256
        )
    except UnicodeError:
        return False


@dataclass(frozen=True, slots=True)
class DatabaseIncarnation:
    server: str
    database: str
    database_guid: str
    recovery_fork_guid: str

    def __post_init__(self):
        try:
            valid = (
                _text(self.server)
                and _text(self.database)
                and all(
                    type(value) is str and str(UUID(value)) == value and UUID(value).int != 0
                    for value in (self.database_guid, self.recovery_fork_guid)
                )
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise IncarnationRefused("invalid_database_incarnation")


def encode_incarnation(value):
    if type(value) is not DatabaseIncarnation:
        raise IncarnationRefused("invalid_database_incarnation")
    value.__post_init__()
    return json.dumps(
        [value.server, value.database, value.database_guid, value.recovery_fork_guid],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode_incarnation(body):
    try:
        if type(body) is not str or len(body.encode("utf-8")) > 4096:
            raise ValueError
        value = json.loads(body)
        if type(value) is not list or len(value) != 4:
            raise ValueError
        result = DatabaseIncarnation(*value)
        if encode_incarnation(result) != body:
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, IncarnationRefused):
        raise IncarnationRefused("invalid_database_incarnation") from None


def same_database(left, right):
    """Conservative separation check, not an attestation of independent backup domains."""
    encode_incarnation(left)
    encode_incarnation(right)
    return (left.server.casefold(), left.database.casefold()) == (
        right.server.casefold(),
        right.database.casefold(),
    ) or left.database_guid == right.database_guid


def read_incarnation(cursor):
    """Read native GUID/fork metadata through the supplied cursor; no connection/commit.

    EngineEdition 1-4 are SQL Server editions; 8 is Managed Instance. Azure SQL
    Database (5), Fabric and unknown editions are unsupported, with no fallback.
    """
    try:
        cursor.execute(
            """SELECT CONVERT(int,SERVERPROPERTY('EngineEdition')),
            CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')),DB_NAME(),
            CONVERT(varchar(36),database_guid),CONVERT(varchar(36),recovery_fork_guid)
            FROM sys.database_recovery_status WHERE database_id=DB_ID()"""
        )
        row = cursor.fetchone()
        if row is None or len(row) != 5:
            raise IncarnationUnavailable("database_incarnation_unavailable")
        if type(row[0]) is not int or row[0] not in (1, 2, 3, 4, 8):
            raise IncarnationRefused("database_incarnation_unsupported")
        return DatabaseIncarnation(*row[1:])
    except IncarnationRefused:
        raise
    except Exception:
        raise IncarnationUnavailable("database_incarnation_unavailable") from None
