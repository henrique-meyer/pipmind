"""Isolated SQL ownership evidence, never a detached broker-dispatch permit.

All callbacks are trusted, synchronous SQL work in this module's transaction.
An external trusted authority and reconciliation are required after DB restore.
"""

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime

MIN_TTL_SECONDS = 1
MAX_TTL_SECONDS = 300
_MAX_GENERATION = 9223372036854775807
_TABLE = "dbo.pm_safety_leases_v1"
_DDL = f"""IF OBJECT_ID('{_TABLE}', 'U') IS NULL CREATE TABLE {_TABLE} (
 binding_key char(64) COLLATE Latin1_General_100_BIN2 NOT NULL PRIMARY KEY,
 account_id nvarchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
 environment nvarchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
 resource nvarchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
 authority_epoch nvarchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
 generation bigint NOT NULL CHECK(generation >= 0),
 owner_id nvarchar(128) COLLATE Latin1_General_100_BIN2 NULL,
 token_sha256 char(64) COLLATE Latin1_General_100_BIN2 NULL,
 expires_at datetime2(6) NULL,
 CONSTRAINT CK_pm_safety_leases_v1_holder CHECK (
 (owner_id IS NULL AND token_sha256 IS NULL AND expires_at IS NULL) OR
 (owner_id IS NOT NULL AND token_sha256 IS NOT NULL AND expires_at IS NOT NULL)))"""


class LeaseRefused(Exception):
    """A fixed-code refusal, with no callback effects committed."""


class LeaseUnavailable(Exception):
    """SQL/callback failed before a commit was attempted. No authority is granted."""


class LeaseCommitUnknown(Exception):
    """Commit acknowledgement was lost: reconcile; never automatically replay."""


@dataclass(frozen=True, slots=True)
class LeaseBinding:
    account_id: str
    environment: str
    resource: str


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    binding: LeaseBinding
    authority_epoch: str
    owner_id: str
    generation: int
    token: str = field(repr=False)
    expires_at: datetime


def _need(condition, code):
    if not condition:
        raise LeaseRefused(code)


def _text(value):
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-16-le")) <= 256
    except UnicodeError:
        return False


def _binding(value):
    _need(
        type(value) is LeaseBinding
        and all(_text(x) for x in (value.account_id, value.environment, value.resource)),
        "lease_invalid_binding",
    )
    return hashlib.sha256(
        json.dumps(
            [value.account_id, value.environment, value.resource],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _ttl(value):
    _need(
        type(value) is int and MIN_TTL_SECONDS <= value <= MAX_TTL_SECONDS,
        "lease_invalid_ttl",
    )


def _close(db):
    if db is not None:
        try:
            db.close()  # Non-pooled disconnect aborts any uncommitted SQL work.
        except BaseException:
            pass  # Acknowledged commits remain acknowledged even on cleanup failure.


def _run(connect, resource, operation):
    db = None
    committing = False
    try:
        db = connect()
        _need(getattr(db, "autocommit", None) is False, "lease_transaction_required")
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        cur.execute(
            """DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,
            @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=5000; SELECT @r""",
            (resource,),
        )
        while cur.description is None:
            if not cur.nextset():
                raise LeaseUnavailable("lease_lock_result_missing")
        row = cur.fetchone()
        if row is None or row[0] < 0:
            raise LeaseRefused("lease_store_busy")
        result = operation(cur)
        committing = True
        db.commit()
        return result
    except BaseException as exc:
        if committing:
            raise LeaseCommitUnknown("lease_commit_unknown") from None
        if not isinstance(exc, Exception):
            raise  # Pre-commit interruption propagates after session disposal.
        if isinstance(exc, (LeaseRefused, LeaseUnavailable)):
            raise
        raise LeaseUnavailable("lease_store_unavailable") from None
    finally:
        _close(db)


def bootstrap(connect):
    """Explicit isolated DDL, serialized independently of lease acquisition."""

    def operation(cur):
        cur.execute(_DDL)

    return _run(connect, "pipmind-safety-lease-schema-v1", operation)


class SqlLeaseStore:
    """Account/environment/resource ownership in one SQL database incarnation.

    ``connect`` must produce fresh, non-pooled, autocommit=False connections.
    ``authority_epoch`` is supplied by a trusted external operational authority;
    storing it here does not verify that authority or detect a restored DB.
    """

    def __init__(self, connect, binding, authority_epoch):
        self._key = _binding(binding)
        _need(_text(authority_epoch), "lease_invalid_authority")
        _need(callable(connect), "lease_invalid_connector")
        self._connect = connect
        self.binding = binding
        self.authority_epoch = authority_epoch

    def _run(self, operation):
        return _run(self._connect, "pipmind-safety-lease-v1:" + self._key, operation)

    def _read(self, cur):
        cur.execute(
            f"""SELECT account_id,environment,resource,authority_epoch,generation,
            owner_id,token_sha256,expires_at FROM {_TABLE} WITH (UPDLOCK,HOLDLOCK)
            WHERE binding_key=%s""",
            (self._key,),
        )
        row = cur.fetchone()
        if row is not None:
            _need(
                tuple(row[:3])
                == (self.binding.account_id, self.binding.environment, self.binding.resource),
                "lease_binding_mismatch",
            )
            _need(row[3] == self.authority_epoch, "lease_authority_mismatch")
        return row

    def initialize(self):
        """Explicitly bind an absent row. Never reset ownership or change epoch."""

        def operation(cur):
            if self._read(cur) is None:
                cur.execute(
                    f"""INSERT INTO {_TABLE}
                    (binding_key,account_id,environment,resource,authority_epoch,generation)
                    VALUES(%s,%s,%s,%s,%s,0)""",
                    (
                        self._key,
                        self.binding.account_id,
                        self.binding.environment,
                        self.binding.resource,
                        self.authority_epoch,
                    ),
                )

        self._run(operation)

    @staticmethod
    def _now(cur):
        cur.execute("SELECT SYSUTCDATETIME()")
        value = cur.fetchone()[0]
        _need(type(value) is datetime and value.tzinfo is None, "lease_invalid_server_time")
        return value

    def _grant(self, row, token):
        return LeaseGrant(
            self.binding,
            self.authority_epoch,
            row[5],
            row[4],
            token,
            row[7].replace(tzinfo=UTC),
        )

    def _validate_grant(self, grant):
        _need(type(grant) is LeaseGrant, "lease_invalid_grant")
        _need(grant.binding == self.binding, "lease_binding_mismatch")
        _need(grant.authority_epoch == self.authority_epoch, "lease_authority_mismatch")
        _need(
            _text(grant.owner_id)
            and type(grant.generation) is int
            and 0 < grant.generation <= _MAX_GENERATION
            and type(grant.token) is str
            and re.fullmatch(r"[0-9a-f]{64}", grant.token) is not None,
            "lease_invalid_grant",
        )

    def _checked(self, cur, grant):
        row = self._read(cur)
        _need(row is not None, "lease_not_initialized")
        _need(
            row[4] == grant.generation
            and row[5] == grant.owner_id
            and secrets.compare_digest(
                row[6] or "", hashlib.sha256(grant.token.encode("ascii")).hexdigest()
            ),
            "lease_stale_owner",
        )
        _need(row[7] is not None and row[7] > self._now(cur), "lease_expired")
        return row

    def acquire(self, owner_id, *, ttl_seconds):
        _need(_text(owner_id), "lease_invalid_owner")
        _ttl(ttl_seconds)
        token = secrets.token_hex(32)

        def operation(cur):
            row = self._read(cur)
            _need(row is not None, "lease_not_initialized")
            now = self._now(cur)
            _need(row[7] is None or row[7] <= now, "lease_held")
            _need(row[4] < _MAX_GENERATION, "lease_generation_exhausted")
            cur.execute(
                f"""UPDATE {_TABLE} SET generation=generation+1,owner_id=%s,
                token_sha256=%s,expires_at=DATEADD(second,%s,SYSUTCDATETIME())
                WHERE binding_key=%s""",
                (
                    owner_id,
                    hashlib.sha256(token.encode("ascii")).hexdigest(),
                    ttl_seconds,
                    self._key,
                ),
            )
            # Recheck expiry after writes as well; do not return an already expired grant.
            row = self._read(cur)
            _need(row[7] > self._now(cur), "lease_expired")
            return self._grant(row, token)

        return self._run(operation)

    def renew(self, grant, *, ttl_seconds):
        self._validate_grant(grant)
        _ttl(ttl_seconds)

        def operation(cur):
            self._checked(cur, grant)
            cur.execute(
                f"""UPDATE {_TABLE} SET expires_at=DATEADD(second,%s,SYSUTCDATETIME())
                OUTPUT INSERTED.expires_at WHERE binding_key=%s AND expires_at>SYSUTCDATETIME()""",
                (ttl_seconds, self._key),
            )
            _need(cur.fetchone() is not None, "lease_expired")
            return self._grant(self._checked(cur, grant), grant.token)

        return self._run(operation)

    def release(self, grant):
        self._validate_grant(grant)

        def operation(cur):
            row = self._checked(cur, grant)
            cur.execute(
                f"""UPDATE {_TABLE} SET owner_id=NULL,token_sha256=NULL,expires_at=NULL
                WHERE binding_key=%s""",
                (self._key,),
            )
            _need(row[7] > self._now(cur), "lease_expired")

        self._run(operation)

    def compare_and_act(self, grant, sql_callback):
        """Run trusted SQL-only callback atomically while this grant is current.

        No network calls, nested lease operations, transaction control, unrelated
        ownership writes or returning lazy cursor results are allowed. Consume
        results inside the callback. Python cannot sandbox a trusted callback.
        """
        self._validate_grant(grant)
        _need(callable(sql_callback), "lease_invalid_callback")

        def operation(cur):
            before = self._checked(cur, grant)
            try:
                result = sql_callback(cur)
            except Exception:
                # Callback-owned exception classes/text are not internal lease
                # verdicts. Preserve only our fixed pre-commit failure code.
                raise LeaseUnavailable("lease_store_unavailable") from None
            after = self._checked(cur, grant)  # Expiry rolls back callback effects.
            _need(before == after, "lease_callback_changed_lease")
            return result

        return self._run(operation)
