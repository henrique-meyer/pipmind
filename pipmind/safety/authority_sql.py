"""Explicit external SQL recovery authority; never an application-DB epoch shortcut."""

import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from pipmind.contracts import stamp, utc
from pipmind.safety.incarnation import (
    DatabaseIncarnation,
    IncarnationRefused,
    IncarnationUnavailable,
    decode_incarnation,
    encode_incarnation,
    read_incarnation,
    same_database,
)
from pipmind.safety.lease_sql import LeaseBinding, LeaseCommitUnknown

MAX_RECORD_BYTES = 65536
MAX_REVISION = 9223372036854775807
_STATE = "dbo.pm_safety_authority_v1"
_JOURNAL = "dbo.pm_safety_authority_fences_v1"
_BIN = "COLLATE Latin1_General_100_BIN2"
_CONFLICT = object()
_DDL = (
    f"""IF OBJECT_ID('{_STATE}','U') IS NULL CREATE TABLE {_STATE} (
    binding_key char(64) {_BIN} NOT NULL PRIMARY KEY,
    account_id nvarchar(128) {_BIN} NOT NULL,environment nvarchar(128) {_BIN} NOT NULL,
    resource nvarchar(128) {_BIN} NOT NULL,body nvarchar(max) NOT NULL,
    sha256 char(64) {_BIN} NOT NULL)""",
    f"""IF OBJECT_ID('{_JOURNAL}','U') IS NULL CREATE TABLE {_JOURNAL} (
    binding_key char(64) {_BIN} NOT NULL REFERENCES {_STATE}(binding_key),
    operation_id nvarchar(128) {_BIN} NOT NULL,body nvarchar(max) NOT NULL,
    body_sha256 char(64) {_BIN} NOT NULL,receipt nvarchar(max) NOT NULL,
    receipt_sha256 char(64) {_BIN} NOT NULL,
    CONSTRAINT PK_pm_safety_authority_fences_v1 PRIMARY KEY(binding_key,operation_id))""",
)


class AuthorityRefused(Exception):
    """Fixed input, stale-state, unsupported or compare-and-set refusal."""


class AuthorityUnavailable(Exception):
    """Authority cannot be established; no positive permit or rollback claim."""


class AuthorityCommitUnknown(Exception):
    """External authority commit acknowledgement lost; reconcile, never retry automatically."""


def _need(value, code):
    if not value:
        raise AuthorityRefused(code)


def _id(value):
    try:
        valid = (
            type(value) is str
            and bool(value)
            and value.strip() == value
            and "\x00" not in value
            and len(value.encode("utf-16-le")) <= 256
        )
    except UnicodeError:
        valid = False
    _need(valid, "authority_invalid_id")


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _count(value):
    return type(value) is int and 0 <= value <= MAX_REVISION


def _json(value):
    raw = json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    _need(len(raw.encode("utf-8")) <= MAX_RECORD_BYTES, "authority_record_too_large")
    return raw


def _binding(value):
    _need(type(value) is LeaseBinding, "authority_invalid_binding")
    for item in (value.account_id, value.environment, value.resource):
        _id(item)
    return _hash(_json([value.account_id, value.environment, value.resource]))


def _incarnation(value):
    try:
        return encode_incarnation(value)
    except IncarnationRefused:
        raise AuthorityRefused("authority_invalid_incarnation") from None


@dataclass(frozen=True, slots=True)
class AuthorityState:
    binding: LeaseBinding
    revision: int
    epoch: str
    mode: str
    assignment_id: str | None
    assignment_generation: int
    robot_sha256: str | None
    application_incarnation: DatabaseIncarnation


def _validate(value):
    _need(type(value) is AuthorityState, "authority_invalid_state")
    _binding(value.binding)
    _incarnation(value.application_incarnation)
    _need(
        _count(value.revision)
        and _count(value.assignment_generation)
        and _sha(value.epoch)
        and type(value.mode) is str
        and value.mode in ("held", "active"),
        "authority_invalid_state",
    )
    if value.mode == "active":
        _id(value.assignment_id)
        _need(
            value.assignment_generation > 0 and _sha(value.robot_sha256), "authority_invalid_state"
        )
    else:
        _need(value.assignment_id is None and value.robot_sha256 is None, "authority_invalid_state")


def _payload(state):
    _validate(state)
    return {
        "binding": [state.binding.account_id, state.binding.environment, state.binding.resource],
        "revision": state.revision,
        "epoch": state.epoch,
        "mode": state.mode,
        "assignment_id": state.assignment_id,
        "assignment_generation": state.assignment_generation,
        "robot_sha256": state.robot_sha256,
        "application_incarnation": json.loads(_incarnation(state.application_incarnation)),
    }


def encode_authority_state(state):
    return _json(_payload(state))


def decode_authority_state(raw):
    try:
        _need(
            type(raw) is str and len(raw.encode("utf-8")) <= MAX_RECORD_BYTES,
            "authority_invalid_state",
        )
        data = json.loads(raw)
        _need(
            type(data) is dict
            and data.keys()
            == {
                "binding",
                "revision",
                "epoch",
                "mode",
                "assignment_id",
                "assignment_generation",
                "robot_sha256",
                "application_incarnation",
            },
            "authority_invalid_state",
        )
        binding = data["binding"]
        _need(type(binding) is list and len(binding) == 3, "authority_invalid_state")
        state = AuthorityState(
            LeaseBinding(*binding),
            data["revision"],
            data["epoch"],
            data["mode"],
            data["assignment_id"],
            data["assignment_generation"],
            data["robot_sha256"],
            decode_incarnation(json.dumps(data["application_incarnation"], separators=(",", ":"))),
        )
        _need(encode_authority_state(state) == raw, "authority_invalid_state")
        return state
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, IncarnationRefused):
        raise AuthorityRefused("authority_invalid_state") from None


def _close(db):
    if db is not None:
        try:
            db.close()  # Fresh non-pooled disconnect; no pytds rollback timeout removal.
        except BaseException:
            pass


def _native(cur, expected):
    try:
        observed = read_incarnation(cur)
    except (IncarnationRefused, IncarnationUnavailable):
        raise AuthorityUnavailable("authority_incarnation_unavailable") from None
    _need(observed == expected, "authority_incarnation_mismatch")


def _lock(cur, resource):
    cur.execute(
        """DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,
        @LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=5000; SELECT @r""",
        (resource,),
    )
    while cur.description is None:
        if not cur.nextset():
            raise AuthorityUnavailable("authority_store_unavailable")
    row = cur.fetchone()
    _need(row is not None and type(row[0]) is int and row[0] >= 0, "authority_store_busy")


@contextmanager
def _transaction(connect, expected, resource):
    db, committing = None, False
    try:
        db = connect()
        _need(getattr(db, "autocommit", None) is False, "authority_transaction_required")
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        _native(cur, expected)
        _lock(cur, resource)
        yield cur
        _native(cur, expected)
        committing = True
        db.commit()
    except BaseException as exc:
        if committing:
            raise AuthorityCommitUnknown("authority_commit_unknown") from None
        if not isinstance(exc, Exception) or isinstance(
            exc, (AuthorityRefused, AuthorityUnavailable, LeaseCommitUnknown)
        ):
            raise
        raise AuthorityUnavailable("authority_store_unavailable") from None
    finally:
        _close(db)


def bootstrap(connect, expected_incarnation):
    """Explicit external-only DDL against a supplied trusted native incarnation pin."""
    _need(callable(connect), "authority_invalid_connector")
    _incarnation(expected_incarnation)
    with _transaction(connect, expected_incarnation, "pipmind-safety-authority-schema-v1") as cur:
        for sql in _DDL:
            cur.execute(sql)


class AuthorityGuard:
    """Transaction-scoped authority state, not a token or a broker dispatch permit."""

    def __init__(self, store, cursor, state):
        self._store, self._cursor, self._state = store, cursor, state
        self._open = True

    @property
    def state(self):
        return self._state

    def activate(self, assignment_id, assignment_generation, robot_sha256):
        _need(self._open, "authority_guard_closed")
        _need(self._state.mode == "held", "authority_activation_requires_hold")
        _id(assignment_id)
        _need(
            _count(assignment_generation)
            and assignment_generation > self._state.assignment_generation,
            "authority_assignment_generation_stale",
        )
        _need(_sha(robot_sha256), "authority_invalid_robot_hash")
        _need(self._state.revision < MAX_REVISION, "authority_revision_exhausted")
        changed = replace(
            self._state,
            revision=self._state.revision + 1,
            mode="active",
            assignment_id=assignment_id,
            assignment_generation=assignment_generation,
            robot_sha256=robot_sha256,
        )
        self._store._write(self._cursor, changed)
        self._state = changed
        return changed  # Still pending; caller must wait for context exit acknowledgement.


class SqlAuthorityStore:
    """Reference authority DB outside application restore/failure domain.

    Configuration and independent operational protection are trusted. A separate
    database name alone does not prove a separate administrative restore domain.
    """

    def __init__(self, connect, binding, expected_incarnation):
        self._key = _binding(binding)
        _incarnation(expected_incarnation)
        _need(callable(connect), "authority_invalid_connector")
        self._connect, self._binding, self._incarnation = connect, binding, expected_incarnation
        self._resource = "pipmind-safety-authority-v1:" + self._key

    @property
    def binding(self):
        return self._binding

    @property
    def expected_incarnation(self):
        return self._incarnation

    def _separate(self, application):
        _incarnation(application)
        _need(not same_database(self._incarnation, application), "authority_database_not_separate")

    def _read(self, cur):
        cur.execute(
            f"SELECT account_id,environment,resource,CASE WHEN DATALENGTH(body)<=%s "
            f"THEN body END,sha256 FROM {_STATE} WITH(UPDLOCK,HOLDLOCK) WHERE binding_key=%s",
            (2 * MAX_RECORD_BYTES, self._key),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"SELECT TOP(1) operation_id FROM {_JOURNAL} WHERE binding_key=%s", (self._key,)
            )
            if cur.fetchone() is not None:
                raise AuthorityUnavailable("authority_store_unavailable")
            return None
        try:
            if tuple(row[:3]) != (
                self.binding.account_id,
                self.binding.environment,
                self.binding.resource,
            ):
                raise ValueError
            if type(row[3]) is not str or _hash(row[3]) != row[4]:
                raise ValueError
            state = decode_authority_state(row[3])
            if state.binding != self.binding:
                raise ValueError
            self._separate(state.application_incarnation)
            return state
        except (AuthorityRefused, ValueError, TypeError, UnicodeError):
            raise AuthorityUnavailable("authority_store_unavailable") from None

    def _write(self, cur, state):
        raw = encode_authority_state(state)
        cur.execute(
            f"UPDATE {_STATE} SET body=%s,sha256=%s WHERE binding_key=%s",
            (raw, _hash(raw), self._key),
        )

    def initialize(self, application_incarnation):
        self._separate(application_incarnation)
        with _transaction(self._connect, self._incarnation, self._resource) as cur:
            state = self._read(cur)
            if state is None:
                state = AuthorityState(
                    self.binding,
                    0,
                    secrets.token_hex(32),
                    "held",
                    None,
                    0,
                    None,
                    application_incarnation,
                )
                raw = encode_authority_state(state)
                cur.execute(
                    f"INSERT INTO {_STATE} VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        self._key,
                        self.binding.account_id,
                        self.binding.environment,
                        self.binding.resource,
                        raw,
                        _hash(raw),
                    ),
                )
            _need(
                state.application_incarnation == application_incarnation,
                "authority_application_mismatch",
            )
        return state

    def read(self):
        with _transaction(self._connect, self._incarnation, self._resource) as cur:
            state = self._read(cur)
            _need(state is not None, "authority_uninitialized")
        return state

    def _journal(self, cur, operation_id):
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,body_sha256,"
            f"CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256 FROM {_JOURNAL} "
            "WITH(UPDLOCK,HOLDLOCK) WHERE binding_key=%s AND operation_id=%s",
            (2 * MAX_RECORD_BYTES, 2 * MAX_RECORD_BYTES, self._key, operation_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            if type(row[0]) is not str or type(row[2]) is not str:
                raise ValueError
            if _hash(row[0]) != row[1] or _hash(row[2]) != row[3]:
                raise ValueError
            body, receipt = json.loads(row[0]), json.loads(row[2])
            if _json(body) != row[0] or _json(receipt) != row[2]:
                raise ValueError
            if set(receipt) != {
                "schema_version",
                "kind",
                "body",
                "before",
                "after",
                "observed_at",
                "historical_receipt_only",
                "dispatch_authorized",
            }:
                raise ValueError
            if (
                receipt["schema_version"] != 1
                or type(receipt["schema_version"]) is not int
                or receipt["kind"] != "authority_fence"
                or receipt["body"] != body
                or body["operation_id"] != operation_id
                or receipt["historical_receipt_only"] is not True
                or receipt["dispatch_authorized"] is not False
            ):
                raise ValueError
            before = decode_authority_state(_json(receipt["before"]))
            after = decode_authority_state(_json(receipt["after"]))
            if (
                before.binding != self.binding
                or after.binding != self.binding
                or after.mode != "held"
                or after.revision != before.revision + 1
                or after.epoch == before.epoch
                or after.assignment_generation != before.assignment_generation
                or body["expected_revision"] != before.revision
                or body["expected_epoch"] != before.epoch
                or body["application_incarnation"] != receipt["after"]["application_incarnation"]
            ):
                raise ValueError
            utc(receipt["observed_at"])
            return row[0], receipt
        except (ValueError, TypeError, KeyError, AuthorityRefused):
            raise AuthorityUnavailable("authority_store_unavailable") from None

    def fence(
        self,
        operation_id,
        actor_id,
        expected_revision,
        expected_epoch,
        application_incarnation,
        reason,
    ):
        _id(operation_id)
        _id(actor_id)
        _need(
            _count(expected_revision) and _sha(expected_epoch), "authority_invalid_expected_state"
        )
        self._separate(application_incarnation)
        _need(
            type(reason) is str and bool(reason) and "\x00" not in reason,
            "authority_invalid_reason",
        )
        try:
            reason.encode("utf-8")
        except UnicodeError:
            raise AuthorityRefused("authority_invalid_reason") from None
        body = _json(
            {
                "operation_id": operation_id,
                "actor_id": actor_id,
                "expected_revision": expected_revision,
                "expected_epoch": expected_epoch,
                "application_incarnation": json.loads(_incarnation(application_incarnation)),
                "reason": reason,
            }
        )
        with _transaction(self._connect, self._incarnation, self._resource) as cur:
            state = self._read(cur)
            _need(state is not None, "authority_uninitialized")
            retained = self._journal(cur, operation_id)
            if retained is not None:
                result = retained[1] if retained[0] == body else _CONFLICT
            else:
                _need(
                    state.revision == expected_revision and state.epoch == expected_epoch,
                    "authority_stale_state",
                )
                _need(state.revision < MAX_REVISION, "authority_revision_exhausted")
                epoch = secrets.token_hex(32)
                while epoch == state.epoch:
                    epoch = secrets.token_hex(32)
                changed = replace(
                    state,
                    revision=state.revision + 1,
                    epoch=epoch,
                    mode="held",
                    assignment_id=None,
                    robot_sha256=None,
                    application_incarnation=application_incarnation,
                )
                cur.execute("SELECT SYSUTCDATETIME()")
                now = cur.fetchone()[0]
                _need(type(now) is datetime and now.tzinfo is None, "authority_invalid_server_time")
                result = {
                    "schema_version": 1,
                    "kind": "authority_fence",
                    "body": json.loads(body),
                    "before": _payload(state),
                    "after": _payload(changed),
                    "observed_at": stamp(now.replace(tzinfo=UTC)),
                    "historical_receipt_only": True,
                    "dispatch_authorized": False,
                }
                receipt = _json(result)
                self._write(cur, changed)
                cur.execute(
                    f"INSERT INTO {_JOURNAL} VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        self._key,
                        operation_id,
                        body,
                        _hash(body),
                        receipt,
                        _hash(receipt),
                    ),
                )
        if result is _CONFLICT:
            raise AuthorityRefused("conflicting_authority_operation")
        return result

    @contextmanager
    def guard(self, expected_state, *, mode="active"):
        _validate(expected_state)
        _need(expected_state.binding == self.binding, "authority_binding_mismatch")
        _need(type(mode) is str and mode in ("active", "held"), "authority_invalid_mode")
        _need(expected_state.mode == mode, "authority_mode_mismatch")
        guard = None
        try:
            with _transaction(self._connect, self._incarnation, self._resource) as cur:
                state = self._read(cur)
                _need(state is not None and state == expected_state, "authority_stale_state")
                guard = AuthorityGuard(self, cur, state)
                try:
                    yield guard
                except LeaseCommitUnknown:
                    # The application commit may have succeeded; never downgrade
                    # its uncertainty when aborting this external transaction.
                    raise
                except Exception:
                    # Caller exceptions/classes/text are not internal authority verdicts.
                    raise AuthorityUnavailable("authority_operation_failed") from None
                _need(self._read(cur) == guard.state, "authority_guard_changed_state")
        finally:
            if guard is not None:
                guard._open = False
