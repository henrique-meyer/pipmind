"""Trusted-owner SQL controls and separate suggestion audit; no model/order path."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from pipmind.contracts import loads, stamp, utc
from pipmind.safety.session import SessionBinding, account_key, validate_binding

MAX_RECORD_BYTES = 65536
MAX_REVISION = 9223372036854775807
_STATE = "dbo.pm_safety_controls_v1"
_TABLES = {
    "override": "dbo.pm_safety_control_overrides_v1",
    "suggestion": "dbo.pm_safety_control_suggestions_v1",
    "disposition": "dbo.pm_safety_control_dispositions_v1",
}
_BIN = "COLLATE Latin1_General_100_BIN2"
_DDL = (
    f"""IF OBJECT_ID('{_STATE}','U') IS NULL CREATE TABLE {_STATE} (
    account_key char(64) {_BIN} NOT NULL PRIMARY KEY,
    account_id nvarchar(128) {_BIN} NOT NULL,environment varchar(4) {_BIN} NOT NULL,
    currency char(3) {_BIN} NOT NULL,revision bigint NOT NULL CHECK(revision>=0),
    killed bit NOT NULL,paused bit NOT NULL)""",
    *(
        f"""IF OBJECT_ID('{table}','U') IS NULL CREATE TABLE {table} (
    account_key char(64) {_BIN} NOT NULL REFERENCES {_STATE}(account_key),
    id nvarchar(128) {_BIN} NOT NULL,body nvarchar(max) NOT NULL,body_sha256 char(64) NOT NULL,
    receipt nvarchar(max) NOT NULL,receipt_sha256 char(64) NOT NULL,
    {"revision bigint NOT NULL CHECK(revision>0)," if table == _TABLES["override"] else ""}
    CONSTRAINT PK_{table.removeprefix("dbo.")} PRIMARY KEY(account_key,id)
    {
            ",CONSTRAINT UQ_pm_safety_control_revision_v1 UNIQUE(account_key,revision)"
            if table == _TABLES["override"]
            else ""
        })"""
        for table in _TABLES.values()
    ),
)


class ControlRefused(Exception):
    """Fixed input/CAS/domain refusal, never a model decision."""


class ControlUnavailable(Exception):
    """State cannot be read/verified, or SQL failed before commit attempt."""


class ControlCommitUnknown(Exception):
    """Commit acknowledgement lost; no result or automatic replay is safe."""


@dataclass(frozen=True, slots=True)
class ControlState:
    binding: SessionBinding
    revision: int
    killed: bool
    paused: bool


def _need(value, code):
    if not value:
        raise ControlRefused(code)


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
    _need(valid, "invalid_control_id")


def _text(value):
    try:
        valid = type(value) is str and bool(value) and "\x00" not in value
        if valid:
            value.encode("utf-8")
    except UnicodeError:
        valid = False
    _need(valid, "invalid_control_text")


def _encode(value):
    try:
        raw = json.dumps(
            value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        _need(len(raw.encode("utf-8")) <= MAX_RECORD_BYTES, "control_record_too_large")
        return raw
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ControlRefused("invalid_control_record") from None


def _hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _unavailable():
    raise ControlUnavailable("control_store_unavailable")


def _lock(cur, resource):
    cur.execute(
        """DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,
        @LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=5000; SELECT @r""",
        (resource,),
    )
    while cur.description is None:
        if not cur.nextset():
            _unavailable()
    row = cur.fetchone()
    _need(row is not None and row[0] >= 0, "control_store_busy")


def _run(connect, resource, operation):
    db, committing = None, False
    try:
        db = connect()
        _need(getattr(db, "autocommit", None) is False, "control_transaction_required")
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        _lock(cur, resource)
        result = operation(cur)
        committing = True
        db.commit()
        return result
    except BaseException as error:
        if committing:
            raise ControlCommitUnknown("control_commit_unknown") from None
        if not isinstance(error, Exception) or isinstance(
            error, (ControlRefused, ControlUnavailable)
        ):
            raise
        raise ControlUnavailable("control_store_unavailable") from None
    finally:
        if db is not None:
            try:
                db.close()  # Non-pooled disconnect avoids unbounded pytds rollback handshake.
            except BaseException:
                pass


def bootstrap(connect):
    """Explicit serialized isolated DDL, never constructor/read recovery."""

    def operation(cur):
        for statement in _DDL:
            cur.execute(statement)

    _run(connect, "pipmind-safety-control-schema-v1", operation)


class SqlControlStore:
    """All mutation callers must already be trusted/authorized, never a model tool."""

    def __init__(self, connect, binding):
        try:
            validate_binding(binding)
        except Exception:
            raise ControlRefused("invalid_control_binding") from None
        _need(callable(connect), "invalid_control_connector")
        self.connect, self.binding = connect, binding
        self._key = account_key(binding)
        self._resource = "pipmind-safety-controls-v1:" + self._key

    def _binding(self):
        return {
            "account_id": self.binding.account_id,
            "environment": self.binding.environment,
            "currency": self.binding.currency,
        }

    def _run(self, operation):
        return _run(self.connect, self._resource, operation)

    def _state(self, cur, *, absent_ok=False):
        cur.execute(
            f"SELECT account_id,environment,currency,revision,killed,paused FROM {_STATE} "
            "WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s",
            (self._key,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT CASE WHEN "
                + " OR ".join(
                    f"EXISTS(SELECT 1 FROM {table} WHERE account_key=%s)"
                    for table in _TABLES.values()
                )
                + " THEN 1 ELSE 0 END",
                (self._key,) * 3,
            )
            if cur.fetchone()[0]:
                _unavailable()
            _need(absent_ok, "controls_uninitialized")
            return None
        _need(
            tuple(row[:3])
            == (self.binding.account_id, self.binding.environment, self.binding.currency),
            "control_binding_mismatch",
        )
        if (
            type(row[3]) is not int
            or not 0 <= row[3] <= MAX_REVISION
            or type(row[4]) is not bool
            or type(row[5]) is not bool
        ):
            _unavailable()
        state = ControlState(self.binding, row[3], row[4], row[5])
        cur.execute(
            f"SELECT TOP(1) id,revision FROM {_TABLES['override']} "
            "WHERE account_key=%s ORDER BY revision DESC",
            (self._key,),
        )
        latest = cur.fetchone()
        if latest is None:
            if state != ControlState(self.binding, 0, False, True):
                _unavailable()
        else:
            if latest[1] != state.revision or state.revision == 0:
                _unavailable()
            retained = self._record(cur, "override", latest[0], state)
            if retained is None or (
                retained[1]["state"]["killed"],
                retained[1]["state"]["paused"],
            ) != (state.killed, state.paused):
                _unavailable()
        return state

    def initialize(self):
        def operation(cur):
            state = self._state(cur, absent_ok=True)
            if state is None:
                cur.execute(
                    f"INSERT INTO {_STATE} VALUES(%s,%s,%s,%s,0,0,1)",
                    (
                        self._key,
                        self.binding.account_id,
                        self.binding.environment,
                        self.binding.currency,
                    ),
                )
                state = ControlState(self.binding, 0, False, True)
            return state

        return self._run(operation)

    def read(self):
        return self._run(self._state)

    def _transaction(self, cur, *, require_lock=False):
        _need(
            getattr(getattr(cur, "connection", None), "autocommit", None) is not True,
            "control_transaction_required",
        )
        cur.execute("SELECT @@TRANCOUNT,APPLOCK_MODE('public',%s,'Transaction')", (self._resource,))
        row = cur.fetchone()
        _need(row is not None and row[0] >= 1, "control_transaction_required")
        if require_lock:
            _need(row[1] == "Exclusive", "control_lock_required")
        else:
            _lock(cur, self._resource)

    def read_in_transaction(self, cur):
        try:
            self._transaction(cur)
            return self._state(cur)
        except (ControlRefused, ControlUnavailable):
            raise
        except Exception:
            raise ControlUnavailable("control_store_unavailable") from None

    def _record(self, cur, kind, record_id, state):
        cur.execute(
            "SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,body_sha256,"
            "CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256"
            + (",revision " if kind == "override" else " ")
            + f"FROM {_TABLES[kind]} WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s AND id=%s",
            (2 * MAX_RECORD_BYTES, 2 * MAX_RECORD_BYTES, self._key, record_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            _id(record_id)
            if any(
                type(raw) is not str or len(raw.encode()) > MAX_RECORD_BYTES or _hash(raw) != digest
                for raw, digest in ((row[0], row[1]), (row[2], row[3]))
            ):
                _unavailable()
            body, receipt = loads(row[0]), loads(row[2])
            if (
                _encode(body) != row[0]
                or _encode(receipt) != row[2]
                or type(body) is not dict
                or body.get("binding") != self._binding()
                or type(receipt) is not dict
                or set(receipt)
                != {
                    "schema_version",
                    "kind",
                    "record_id",
                    "body",
                    "body_sha256",
                    "observed_at",
                    "origin",
                    "review_excluded",
                    "outcome",
                    "state",
                }
                or type(receipt["schema_version"]) is not int
                or receipt["schema_version"] != 1
                or receipt["kind"] != kind
                or receipt["record_id"] != record_id
                or receipt["body"] != body
                or receipt["body_sha256"] != row[1]
                or receipt["origin"]
                != {
                    "override": "trusted_owner_override",
                    "suggestion": "owner_suggestion",
                    "disposition": "suggestion_disposition",
                }[kind]
                or type(receipt["review_excluded"]) is not bool
                or receipt["review_excluded"] is not (kind == "override")
            ):
                _unavailable()
            utc(receipt["observed_at"])
            retained = receipt["state"]
            if (
                type(retained) is not dict
                or set(retained) != {"binding", "revision", "killed", "paused"}
                or retained["binding"] != self._binding()
                or type(retained["revision"]) is not int
                or not 0 <= retained["revision"] <= state.revision
                or type(retained["killed"]) is not bool
                or type(retained["paused"]) is not bool
            ):
                _unavailable()
            expected_keys = {"binding", "actor_id"} | {
                "override": {"command_id", "action", "expected_revision", "reason"},
                "suggestion": {"suggestion_id", "text"},
                "disposition": {"suggestion_id", "disposition", "reason"},
            }[kind]
            if set(body) != expected_keys:
                _unavailable()
            _id(body["actor_id"])
            if body["command_id" if kind == "override" else "suggestion_id"] != record_id:
                _unavailable()
            _text(body["text"] if kind == "suggestion" else body["reason"])
            if kind == "override":
                if (
                    body["action"] not in ("kill", "pause", "resume", "unkill", "flatten")
                    or type(body["expected_revision"]) is not int
                    or not 0 <= body["expected_revision"] < MAX_REVISION
                    or retained["revision"] != body["expected_revision"] + 1
                    or row[4] != retained["revision"]
                    or receipt["outcome"]
                    != ("unsupported_not_executed" if body["action"] == "flatten" else "applied")
                ):
                    _unavailable()
            elif receipt["outcome"] != ("submitted" if kind == "suggestion" else "recorded") or (
                kind == "disposition" and body["disposition"] not in ("adopt", "trial", "reject")
            ):
                _unavailable()
            return row[0], receipt
        except (ControlRefused, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            raise ControlUnavailable("control_store_unavailable") from None

    def _save(self, cur, kind, record_id, body, state, outcome):
        cur.execute("SELECT SYSUTCDATETIME()")
        now = cur.fetchone()[0]
        if type(now) is not datetime or now.tzinfo is not None:
            _unavailable()
        raw = _encode(body)
        receipt = {
            "schema_version": 1,
            "kind": kind,
            "record_id": record_id,
            "body": body,
            "body_sha256": _hash(raw),
            "observed_at": stamp(now.replace(tzinfo=UTC)),
            "origin": {
                "override": "trusted_owner_override",
                "suggestion": "owner_suggestion",
                "disposition": "suggestion_disposition",
            }[kind],
            "review_excluded": kind == "override",
            "outcome": outcome,
            "state": {
                "binding": self._binding(),
                "revision": state.revision,
                "killed": state.killed,
                "paused": state.paused,
            },
        }
        encoded = _encode(receipt)
        values = (self._key, record_id, raw, _hash(raw), encoded, _hash(encoded))
        if kind == "override":
            values += (state.revision,)
        cur.execute(
            f"INSERT INTO {_TABLES[kind]} VALUES(" + ",".join("%s" for _ in values) + ")", values
        )
        return receipt

    def apply_override(self, command_id, actor_id, action, expected_revision, reason):
        _id(command_id)
        _id(actor_id)
        _text(reason)
        _need(
            type(action) is str and action in ("kill", "pause", "resume", "unkill", "flatten"),
            "unsupported_control_action",
        )
        _need(
            type(expected_revision) is int and 0 <= expected_revision <= MAX_REVISION,
            "invalid_control_revision",
        )
        body = {
            "binding": self._binding(),
            "command_id": command_id,
            "actor_id": actor_id,
            "action": action,
            "expected_revision": expected_revision,
            "reason": reason,
        }
        raw = _encode(body)

        def operation(cur):
            state = self._state(cur)
            old = self._record(cur, "override", command_id, state)
            if old is not None:
                _need(old[0] == raw, "conflicting_control_command")
                return old[1]
            _need(state.revision == expected_revision, "stale_control_revision")
            _need(state.revision < MAX_REVISION, "control_revision_exhausted")
            after = ControlState(
                self.binding,
                state.revision + 1,
                True if action == "kill" else False if action == "unkill" else state.killed,
                True
                if action in ("kill", "pause")
                else False
                if action == "resume"
                else state.paused,
            )
            receipt = self._save(
                cur,
                "override",
                command_id,
                body,
                after,
                "unsupported_not_executed" if action == "flatten" else "applied",
            )
            cur.execute(
                f"UPDATE {_STATE} SET revision=%s,killed=%s,paused=%s WHERE account_key=%s",
                (after.revision, after.killed, after.paused, self._key),
            )
            return receipt

        return self._run(operation)

    def submit_suggestion(self, id, actor, text):
        _id(id)
        _id(actor)
        _text(text)
        body = {"binding": self._binding(), "suggestion_id": id, "actor_id": actor, "text": text}
        raw = _encode(body)

        def operation(cur):
            state = self._state(cur)
            old = self._record(cur, "suggestion", id, state)
            if old is not None:
                _need(old[0] == raw, "conflicting_suggestion")
                return old[1]
            return self._save(cur, "suggestion", id, body, state, "submitted")

        return self._run(operation)

    def resolve_suggestion(self, id, actor, disposition, reason):
        _id(id)
        _id(actor)
        _text(reason)
        _need(
            type(disposition) is str and disposition in ("adopt", "trial", "reject"),
            "invalid_suggestion_disposition",
        )
        body = {
            "binding": self._binding(),
            "suggestion_id": id,
            "actor_id": actor,
            "disposition": disposition,
            "reason": reason,
        }
        raw = _encode(body)

        def operation(cur):
            state = self._state(cur)
            _need(self._record(cur, "suggestion", id, state) is not None, "suggestion_not_found")
            old = self._record(cur, "disposition", id, state)
            if old is not None:
                _need(old[0] == raw, "conflicting_suggestion_disposition")
                return old[1]
            return self._save(cur, "disposition", id, body, state, "recorded")

        return self._run(operation)

    def lookup_override_in_transaction(self, cur, command_id):
        _id(command_id)
        try:
            self._transaction(cur, require_lock=True)
            old = self._record(cur, "override", command_id, self._state(cur))
            return None if old is None else old[1]
        except (ControlRefused, ControlUnavailable):
            raise
        except Exception:
            raise ControlUnavailable("control_store_unavailable") from None

    def lookup_override(self, command_id):
        _id(command_id)

        def operation(cur):
            old = self._record(cur, "override", command_id, self._state(cur))
            return None if old is None else old[1]

        return self._run(operation)
