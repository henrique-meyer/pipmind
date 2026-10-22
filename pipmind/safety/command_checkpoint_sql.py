"""Read-only complete command inventory, not ownership or settlement evidence.

The caller owns the actual transaction and all seven ordered safety locks.
Historical immutable scopes remain historical: no native/current owner, venue
resolution, terminal status or dispatch authority is inferred from this scan.
"""

from pipmind.safety import checkpoint_sql as checkpoint
from pipmind.safety.command_journal import (
    MAX_RECORD_BYTES,
    decode_attempt,
    decode_command,
    decode_unknown,
    encode_attempt,
    encode_command,
    encode_unknown,
    fingerprint,
    project,
)
from pipmind.safety.lease_sql import _binding
from pipmind.safety.session import account_key

_ACCOUNT, _COMMAND, _ATTEMPT, _UNKNOWN = checkpoint.COMMAND_TABLES
_A = checkpoint._col("account_key", "char", 64, key=1)
_SCHEMA = (
    (
        _ACCOUNT,
        (
            _A,
            checkpoint._col("account_id", "nvarchar", 256),
            checkpoint._col("environment", "varchar", 4),
            checkpoint._col("currency", "char", 3),
        ),
    ),
    *(
        (
            table,
            (
                _A,
                checkpoint._col("command_id", "nvarchar", 256, key=2),
                checkpoint._col("body", "nvarchar", -1),
                checkpoint._col("sha256", "char", 64),
            ),
        )
        for table in (_COMMAND, _ATTEMPT, _UNKNOWN)
    ),
)


def _catalog(cur):
    # SQL metadata visibility is permission filtered. Actual current dbo context
    # is the narrow authoritative reference boundary; a db_owner membership,
    # default schema or VIEW DEFINITION flag is not an equivalent guarantee.
    # Least-privileged hosted qualification needs a separately reviewed reader;
    # this module never grants permissions or changes execution context.
    cur.execute("SELECT USER_ID()")
    principal = cur.fetchone()
    if not (
        principal is not None
        and len(principal) == 1
        and type(principal[0]) is int
        and principal[0] == 1
    ):
        checkpoint._unavailable()
    # Include views/synonyms, other schemas and future versions; an unsupported
    # object cannot masquerade as a wholly absent legacy command schema.
    cur.execute(
        "SELECT s.name,o.name,RTRIM(o.type) FROM sys.objects o "
        "JOIN sys.schemas s ON s.schema_id=o.schema_id "
        "WHERE o.name LIKE 'pm[_]safety[_]command[_]%' "
        "AND o.type IN ('U','V','SN','ET') ORDER BY s.name,o.name,o.type"
    )
    rows = cur.fetchmany(len(_SCHEMA) + 1)
    if cur.fetchmany(1):
        checkpoint._unavailable()
    if not rows:
        return False
    expected = {("dbo", table.removeprefix("dbo."), "U") for table, _ in _SCHEMA}
    if len(rows) != len(expected) or {tuple(row) for row in rows} != expected:
        checkpoint._unavailable()
    return True


def _read(cur, table, key, identity, binding, decoder, encoder):
    cur.execute(
        f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,sha256 FROM {table} "
        "WITH(HOLDLOCK) WHERE account_key=%s AND command_id=%s",
        (2 * MAX_RECORD_BYTES, key, identity),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if (
        len(row) != 2
        or type(row[0]) is not str
        or len(row[0].encode("utf-8")) > MAX_RECORD_BYTES
        or fingerprint(row[0]) != row[1]
    ):
        checkpoint._unavailable()
    value = decoder(row[0])
    if (
        encoder(value) != row[0]
        or value.command_id != identity
        or value.scope.assignment.binding != binding
    ):
        checkpoint._unavailable()
    return value


def _projections(cur, key, binding):
    commands = refused = unattempted = attempted = unknowns = 0
    last = None
    while True:
        suffix = "" if last is None else " AND command_id>%s"
        params = (key,) if last is None else (key, last)
        cur.execute(
            f"SELECT TOP ({checkpoint.PAGE_ROWS}) command_id FROM {_COMMAND} WITH(HOLDLOCK) "
            f"WHERE account_key=%s{suffix} ORDER BY command_id",
            params,
        )
        rows = cur.fetchmany(checkpoint.PAGE_ROWS)
        if cur.fetchmany(1):
            checkpoint._unavailable()
        if not rows:
            break
        for row in rows:
            if len(row) != 1 or type(row[0]) is not str or row[0] == last:
                checkpoint._unavailable()
            identity = row[0]
            command = _read(cur, _COMMAND, key, identity, binding, decode_command, encode_command)
            attempt = _read(cur, _ATTEMPT, key, identity, binding, decode_attempt, encode_attempt)
            unknown = _read(cur, _UNKNOWN, key, identity, binding, decode_unknown, encode_unknown)
            if command is None:
                checkpoint._unavailable()
            value = project(command, attempt, unknown)
            commands += 1
            refused += int(value.state == "refused")
            unattempted += int(value.state == "recorded")
            attempted += int(attempt is not None)
            unknowns += int(unknown is not None)
            last = identity
        if len(rows) < checkpoint.PAGE_ROWS:
            break
    return checkpoint.CommandCounts(commands, refused, unattempted, attempted, unknowns)


def capture_command_tables_in_transaction(cur, binding, lease_binding):
    """Return exact four-table digests/counts or explicit wholly absent legacy.

    Reads are bounded per record/page while complete history has no row cap.
    Actual current database dbo context is required for catalog visibility,
    including legacy absence. This reference restriction is not hosted
    least-privilege qualification or permission to elevate an application user.
    No schema/state initialization, connection, commit, callback or current
    incarnation check occurs. Unsupported/incomplete/corrupt evidence refuses.
    Cooperative account locks and rechecks match the legacy checkpoint model;
    this is not attestation against arbitrary privileged catalog/data tampering.
    """
    try:
        if getattr(getattr(cur, "connection", None), "autocommit", None) is not False:
            checkpoint._unavailable()
        cur.execute("SELECT @@TRANCOUNT,XACT_STATE()")
        transaction = cur.fetchone()
        if not (
            transaction is not None
            and len(transaction) == 2
            and type(transaction[0]) is int
            and transaction[0] >= 1
            and type(transaction[1]) is int
            and transaction[1] == 1
        ):
            checkpoint._unavailable()
        resources = checkpoint.complete_required_locks(binding, lease_binding)
        checkpoint._locks(cur, resources)
        if not _catalog(cur):
            # Recheck even the absence path rather than declare a partial profile.
            if _catalog(cur):
                checkpoint._unavailable()
            checkpoint._locks(cur, resources)
            return None
        key, lease_key = account_key(binding), _binding(lease_binding)
        for table, columns in _SCHEMA:
            checkpoint._schema(cur, table, columns)
        tables = tuple(
            checkpoint._capture_table(cur, table, columns, key, lease_key, binding, lease_binding)
            for table, columns in _SCHEMA
        )
        counts = _projections(cur, key, binding)
        physical = tuple(row.count for row in tables)
        if not (
            physical[0] in (0, 1)
            and physical[1:] == (counts.commands, counts.attempted, counts.unknown)
            and (physical[0] == 1 or not any(physical[1:]))
        ):
            checkpoint._unavailable()
        if not _catalog(cur):
            checkpoint._unavailable()
        for table, columns in _SCHEMA:
            checkpoint._schema(cur, table, columns)
        for (table, _), value in zip(_SCHEMA, tables, strict=True):
            if checkpoint._count(cur, table, "t.account_key=%s", (key,)) != value.count:
                checkpoint._unavailable()
        checkpoint._locks(cur, resources)
        return tables, counts
    except checkpoint.CheckpointUnavailable:
        raise
    except Exception:
        raise checkpoint.CheckpointUnavailable("safety_inventory_unavailable") from None
