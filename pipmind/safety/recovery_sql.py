"""Held assignment metadata and exact owner plans; no robot loader or authority."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pipmind.contracts import loads, stamp, utc
from pipmind.safety.session import SessionBinding, account_key, validate_binding

MAX_BODY_BYTES = 65536
MAX_GENERATION = 9223372036854775807
_ACCOUNTS = "dbo.pm_safety_recovery_accounts_v1"
_PLANS = "dbo.pm_safety_recovery_plans_v1"
_TRANSITIONS = "dbo.pm_safety_recovery_transitions_v1"
_BIN = "COLLATE Latin1_General_100_BIN2"
_DDL = (
    f"""IF OBJECT_ID('{_ACCOUNTS}','U') IS NULL CREATE TABLE {_ACCOUNTS} (
    account_key char(64) {_BIN} NOT NULL PRIMARY KEY,
    account_id nvarchar(128) {_BIN} NOT NULL,environment varchar(4) {_BIN} NOT NULL,
    currency char(3) {_BIN} NOT NULL,generation bigint NOT NULL CHECK(generation>=0),
    held bit NOT NULL,assignment nvarchar(max) NULL,assignment_sha256 char(64) NULL)""",
    f"""IF OBJECT_ID('{_PLANS}','U') IS NULL CREATE TABLE {_PLANS} (
    account_key char(64) {_BIN} NOT NULL REFERENCES {_ACCOUNTS}(account_key),
    plan_id nvarchar(128) {_BIN} NOT NULL,body nvarchar(max) NOT NULL,
    sha256 char(64) NOT NULL,created_at varchar(32) NOT NULL,
    CONSTRAINT PK_pm_safety_recovery_plans_v1 PRIMARY KEY(account_key,plan_id))""",
    f"""IF OBJECT_ID('{_TRANSITIONS}','U') IS NULL CREATE TABLE {_TRANSITIONS} (
    account_key char(64) {_BIN} NOT NULL,generation bigint NOT NULL CHECK(generation>0),
    plan_id nvarchar(128) {_BIN} NOT NULL,assignment nvarchar(max) NOT NULL,
    assignment_sha256 char(64) NOT NULL,receipt nvarchar(max) NOT NULL,
    receipt_sha256 char(64) NOT NULL,
    CONSTRAINT PK_pm_safety_recovery_transitions_v1 PRIMARY KEY(account_key,generation),
    CONSTRAINT UQ_pm_safety_recovery_plan_v1 UNIQUE(account_key,plan_id),
    CONSTRAINT FK_pm_safety_recovery_transition_v1 FOREIGN KEY(account_key,plan_id)
      REFERENCES {_PLANS}(account_key,plan_id))""",
)


class RecoveryRefused(Exception):
    """Fixed validation/CAS refusal."""


class RecoveryUnavailable(Exception):
    """Unverifiable or unavailable SQL state, never a usable default."""


class RecoveryCommitUnknown(Exception):
    """Commit acknowledgement lost; never a new authority or automatic replay."""


@dataclass(frozen=True, slots=True)
class RobotReference:
    robot_id: str
    version: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class Assignment:
    binding: SessionBinding
    generation: int
    assignment_id: str
    robot: RobotReference
    epoch: str
    checkpoint_sha256: str
    activated_at: str


@dataclass(frozen=True, slots=True)
class RecoveryState:
    binding: SessionBinding
    generation: int
    assignment: Assignment | None
    held: bool


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    binding: SessionBinding
    plan_id: str
    actor_id: str
    expected_generation: int
    source_checkpoint_sha256: str
    target_robot: RobotReference
    target_epoch: str
    application_incarnation: str
    reason: str
    position_obligation_ids: tuple[str, ...]


def _need(ok, code):
    if not ok:
        raise RecoveryRefused(code)


def _id(value):
    try:
        good = (
            type(value) is str
            and bool(value)
            and value.strip() == value
            and "\x00" not in value
            and len(value.encode("utf-16-le")) <= 256
        )
    except UnicodeError:
        good = False
    _need(good, "invalid_recovery_id")


def _hash_id(value):
    _need(
        type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None,
        "invalid_recovery_hash",
    )


def _generation(value):
    _need(type(value) is int and 0 <= value <= MAX_GENERATION, "invalid_assignment_generation")


def _obligations(value):
    _need(type(value) is tuple, "invalid_recovery_obligations")
    for item in value:
        _id(item)
    _need(len(set(value)) == len(value), "invalid_recovery_obligations")


def _binding(value):
    try:
        validate_binding(value)
    except Exception:
        raise RecoveryRefused("invalid_recovery_binding") from None
    return {
        "account_id": value.account_id,
        "environment": value.environment,
        "currency": value.currency,
    }


def _robot(value):
    _need(type(value) is RobotReference, "invalid_robot_reference")
    _id(value.robot_id)
    _id(value.version)
    _hash_id(value.content_sha256)
    return {
        "robot_id": value.robot_id,
        "version": value.version,
        "content_sha256": value.content_sha256,
    }


def _encode(value):
    try:
        body = json.dumps(
            value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        _need(len(body.encode()) <= MAX_BODY_BYTES, "recovery_body_too_large")
        return body
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise RecoveryRefused("invalid_recovery_body") from None


def _sha(body):
    return hashlib.sha256(body.encode()).hexdigest()


def _fail():
    raise RecoveryUnavailable("recovery_store_unavailable")


def _decode(raw, digest):
    try:
        if type(raw) is not str or len(raw.encode()) > MAX_BODY_BYTES or _sha(raw) != digest:
            _fail()
        data = loads(raw)
        if _encode(data) != raw:
            _fail()
        return data
    except (RecoveryRefused, ValueError, TypeError, UnicodeError, RecursionError):
        raise RecoveryUnavailable("recovery_store_unavailable") from None


def _assignment(value):
    if value is None:
        return None
    _need(type(value) is Assignment, "invalid_assignment")
    _generation(value.generation)
    _need(value.generation > 0, "invalid_assignment")
    _id(value.assignment_id)
    _id(value.epoch)
    _hash_id(value.checkpoint_sha256)
    utc(value.activated_at)
    return {
        "binding": _binding(value.binding),
        "generation": value.generation,
        "assignment_id": value.assignment_id,
        "robot": _robot(value.robot),
        "epoch": value.epoch,
        "checkpoint_sha256": value.checkpoint_sha256,
        "activated_at": value.activated_at,
    }


def _parse_assignment(data):
    _need(
        type(data) is dict
        and set(data)
        == {
            "binding",
            "generation",
            "assignment_id",
            "robot",
            "epoch",
            "checkpoint_sha256",
            "activated_at",
        },
        "invalid_assignment",
    )
    _need(
        type(data["binding"]) is dict
        and set(data["binding"]) == {"account_id", "environment", "currency"},
        "invalid_assignment",
    )
    _need(
        type(data["robot"]) is dict
        and set(data["robot"]) == {"robot_id", "version", "content_sha256"},
        "invalid_assignment",
    )
    value = Assignment(
        SessionBinding(**data["binding"]),
        data["generation"],
        data["assignment_id"],
        RobotReference(**data["robot"]),
        data["epoch"],
        data["checkpoint_sha256"],
        data["activated_at"],
    )
    _assignment(value)
    return value


def _plan(value):
    _need(type(value) is RecoveryPlan, "invalid_recovery_plan")
    for item in (value.plan_id, value.actor_id, value.target_epoch):
        _id(item)
    _generation(value.expected_generation)
    for item in (value.source_checkpoint_sha256, value.application_incarnation):
        _hash_id(item)
    _obligations(value.position_obligation_ids)
    _need(
        type(value.reason) is str and bool(value.reason) and "\x00" not in value.reason,
        "invalid_recovery_reason",
    )
    try:
        value.reason.encode()
    except UnicodeError:
        raise RecoveryRefused("invalid_recovery_reason") from None
    return {
        "binding": _binding(value.binding),
        "plan_id": value.plan_id,
        "actor_id": value.actor_id,
        "expected_generation": value.expected_generation,
        "source_checkpoint_sha256": value.source_checkpoint_sha256,
        "target_robot": _robot(value.target_robot),
        "target_epoch": value.target_epoch,
        "application_incarnation": value.application_incarnation,
        "reason": value.reason,
        "position_obligation_ids": list(value.position_obligation_ids),
    }


def _parse_plan(data):
    _need(
        type(data) is dict
        and set(data)
        == {
            "binding",
            "plan_id",
            "actor_id",
            "expected_generation",
            "source_checkpoint_sha256",
            "target_robot",
            "target_epoch",
            "application_incarnation",
            "reason",
            "position_obligation_ids",
        },
        "invalid_recovery_plan",
    )
    _need(
        type(data["binding"]) is dict
        and set(data["binding"]) == {"account_id", "environment", "currency"},
        "invalid_recovery_plan",
    )
    _need(
        type(data["target_robot"]) is dict
        and set(data["target_robot"]) == {"robot_id", "version", "content_sha256"}
        and type(data["position_obligation_ids"]) is list,
        "invalid_recovery_plan",
    )
    value = RecoveryPlan(
        SessionBinding(**data["binding"]),
        data["plan_id"],
        data["actor_id"],
        data["expected_generation"],
        data["source_checkpoint_sha256"],
        RobotReference(**data["target_robot"]),
        data["target_epoch"],
        data["application_incarnation"],
        data["reason"],
        tuple(data["position_obligation_ids"]),
    )
    _plan(value)
    return value


def _lock(cur, resource):
    cur.execute(
        """DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,
        @LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=5000; SELECT @r""",
        (resource,),
    )
    while cur.description is None:
        if not cur.nextset():
            _fail()
    row = cur.fetchone()
    _need(row is not None and row[0] >= 0, "recovery_store_busy")


def _run(connect, resource, operation):
    db, committing = None, False
    try:
        db = connect()
        _need(getattr(db, "autocommit", None) is False, "recovery_transaction_required")
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
            raise RecoveryCommitUnknown("recovery_commit_unknown") from None
        if not isinstance(error, Exception) or isinstance(
            error, (RecoveryRefused, RecoveryUnavailable)
        ):
            raise
        raise RecoveryUnavailable("recovery_store_unavailable") from None
    finally:
        if db is not None:
            try:
                db.close()
            except BaseException:
                pass


def bootstrap(connect):
    def operation(cur):
        for statement in _DDL:
            cur.execute(statement)

    _run(connect, "pipmind-safety-recovery-schema-v1", operation)


class SqlRecoveryStore:
    """Authority-held guard/reconciliation are caller prerequisites, not SQL claims."""

    def __init__(self, connect, binding):
        _binding(binding)
        _need(callable(connect), "invalid_recovery_connector")
        self.connect, self.binding = connect, binding
        self._key = account_key(binding)
        self._resource = "pipmind-safety-recovery-v1:" + self._key

    def _transaction(self, cur, *, held=False):
        _need(
            getattr(getattr(cur, "connection", None), "autocommit", None) is not True,
            "recovery_transaction_required",
        )
        cur.execute("SELECT @@TRANCOUNT,APPLOCK_MODE('public',%s,'Transaction')", (self._resource,))
        row = cur.fetchone()
        _need(row is not None and row[0] >= 1, "recovery_transaction_required")
        if held:
            _need(row[1] == "Exclusive", "recovery_lock_required")
        else:
            _lock(cur, self._resource)

    @staticmethod
    def _now(cur):
        cur.execute("SELECT SYSUTCDATETIME()")
        value = cur.fetchone()[0]
        if type(value) is not datetime or value.tzinfo is not None:
            _fail()
        return stamp(value.replace(tzinfo=UTC))

    def _plan_record(self, cur, plan_id):
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,sha256,created_at "
            f"FROM {_PLANS} WHERE account_key=%s AND plan_id=%s",
            (2 * MAX_BODY_BYTES, self._key, plan_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            plan = _parse_plan(_decode(row[0], row[1]))
            utc(row[2])
            if plan.binding != self.binding or plan.plan_id != plan_id:
                _fail()
            return {"plan": plan, "digest": row[1], "created_at": row[2]}
        except (RecoveryRefused, ValueError, TypeError, KeyError):
            raise RecoveryUnavailable("recovery_store_unavailable") from None

    def _transition(self, cur, plan_id):
        cur.execute(
            f"""SELECT generation,CASE WHEN DATALENGTH(assignment)<=%s THEN assignment END,
        assignment_sha256,CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256
        FROM {_TRANSITIONS} WHERE account_key=%s AND plan_id=%s""",
            (2 * MAX_BODY_BYTES, 2 * MAX_BODY_BYTES, self._key, plan_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            assignment = _parse_assignment(_decode(row[1], row[2]))
            receipt = _decode(row[3], row[4])
            plan_record = self._plan_record(cur, plan_id)
            if plan_record is None:
                _fail()
            plan = plan_record["plan"]
            expected_keys = {
                "schema_version",
                "plan_id",
                "plan_digest",
                "actor_id",
                "origin",
                "review_excluded",
                "previous_assignment",
                "assignment",
                "position_obligation_ids",
                "application_incarnation",
                "confirmed_at",
                "held",
            }
            if (
                type(receipt) is not dict
                or set(receipt) != expected_keys
                or type(receipt["schema_version"]) is not int
                or receipt["schema_version"] != 1
                or receipt["plan_id"] != plan_id
                or receipt["plan_digest"] != plan_record["digest"]
                or receipt["actor_id"] != plan.actor_id
                or receipt["origin"] != "owner_confirmed_recovery"
                or receipt["review_excluded"] is not True
                or receipt["held"] is not True
                or receipt["assignment"] != _assignment(assignment)
                or receipt["position_obligation_ids"] != list(plan.position_obligation_ids)
                or receipt["application_incarnation"] != plan.application_incarnation
                or receipt["confirmed_at"] != assignment.activated_at
                or assignment.binding != self.binding
                or assignment.assignment_id != plan_id
                or assignment.generation != row[0]
                or row[0] != plan.expected_generation + 1
                or assignment.robot != plan.target_robot
                or assignment.epoch != plan.target_epoch
                or assignment.checkpoint_sha256 != plan.source_checkpoint_sha256
            ):
                _fail()
            prior = receipt["previous_assignment"]
            if assignment.generation == 1:
                if prior is not None:
                    _fail()
            else:
                previous = _parse_assignment(prior)
                if (
                    previous.binding != self.binding
                    or previous.generation != assignment.generation - 1
                ):
                    _fail()
                cur.execute(
                    f"SELECT CASE WHEN DATALENGTH(assignment)<=%s THEN assignment END,"
                    f"assignment_sha256 FROM {_TRANSITIONS} WHERE account_key=%s AND generation=%s",
                    (2 * MAX_BODY_BYTES, self._key, previous.generation),
                )
                old = cur.fetchone()
                if old is None or _decode(old[0], old[1]) != prior:
                    _fail()
            return assignment, receipt
        except (RecoveryRefused, ValueError, TypeError, KeyError):
            raise RecoveryUnavailable("recovery_store_unavailable") from None

    def _state(self, cur, *, absent=False):
        cur.execute(
            f"SELECT account_id,environment,currency,generation,held,"
            f"CASE WHEN DATALENGTH(assignment)<=%s THEN assignment END,assignment_sha256 "
            f"FROM {_ACCOUNTS} WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s",
            (2 * MAX_BODY_BYTES, self._key),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"SELECT CASE WHEN EXISTS(SELECT 1 FROM {_PLANS} WHERE account_key=%s) OR "
                f"EXISTS(SELECT 1 FROM {_TRANSITIONS} WHERE account_key=%s) THEN 1 ELSE 0 END",
                (self._key, self._key),
            )
            if cur.fetchone()[0]:
                _fail()
            _need(absent, "recovery_uninitialized")
            return None
        _need(
            tuple(row[:3])
            == (self.binding.account_id, self.binding.environment, self.binding.currency),
            "recovery_binding_mismatch",
        )
        if type(row[3]) is not int or not 0 <= row[3] <= MAX_GENERATION or row[4] is not True:
            _fail()
        cur.execute(
            f"SELECT TOP(1) generation,plan_id FROM {_TRANSITIONS} "
            "WHERE account_key=%s ORDER BY generation DESC",
            (self._key,),
        )
        latest = cur.fetchone()
        if row[3] == 0:
            if row[5] is not None or row[6] is not None or latest is not None:
                _fail()
            return RecoveryState(self.binding, 0, None, True)
        if latest is None or latest[0] != row[3]:
            _fail()
        transition = self._transition(cur, latest[1])
        try:
            assignment = _parse_assignment(_decode(row[5], row[6]))
        except (RecoveryRefused, ValueError, TypeError, KeyError):
            raise RecoveryUnavailable("recovery_store_unavailable") from None
        if transition is None or transition[0] != assignment or assignment.generation != row[3]:
            _fail()
        return RecoveryState(self.binding, row[3], assignment, True)

    def initialize(self):
        def operation(cur):
            state = self._state(cur, absent=True)
            if state is None:
                cur.execute(
                    f"INSERT INTO {_ACCOUNTS} VALUES(%s,%s,%s,%s,0,1,NULL,NULL)",
                    (
                        self._key,
                        self.binding.account_id,
                        self.binding.environment,
                        self.binding.currency,
                    ),
                )
                state = RecoveryState(self.binding, 0, None, True)
            return state

        return _run(self.connect, self._resource, operation)

    def read(self):
        return _run(self.connect, self._resource, self._state)

    def read_in_transaction(self, cur):
        try:
            self._transaction(cur)
            return self._state(cur)
        except (RecoveryRefused, RecoveryUnavailable):
            raise
        except Exception:
            raise RecoveryUnavailable("recovery_store_unavailable") from None

    def plan_in_transaction(self, cur, plan_id):
        _id(plan_id)
        try:
            self._transaction(cur)
            self._state(cur)
            return self._plan_record(cur, plan_id)
        except (RecoveryRefused, RecoveryUnavailable):
            raise
        except Exception:
            raise RecoveryUnavailable("recovery_store_unavailable") from None

    def create_plan(
        self,
        plan_id,
        actor_id,
        expected_generation,
        source_checkpoint_sha256,
        target_robot,
        target_epoch,
        application_incarnation,
        reason,
        position_obligation_ids,
    ):
        plan = RecoveryPlan(
            self.binding,
            plan_id,
            actor_id,
            expected_generation,
            source_checkpoint_sha256,
            target_robot,
            target_epoch,
            application_incarnation,
            reason,
            position_obligation_ids,
        )
        body = _encode(_plan(plan))
        digest = _sha(body)

        def operation(cur):
            state = self._state(cur)
            old = self._plan_record(cur, plan_id)
            if old is not None:
                _need(_encode(_plan(old["plan"])) == body, "conflicting_recovery_plan")
                return old
            _need(state.generation == expected_generation, "stale_assignment_generation")
            created_at = self._now(cur)
            cur.execute(
                f"INSERT INTO {_PLANS} VALUES(%s,%s,%s,%s,%s)",
                (self._key, plan_id, body, digest, created_at),
            )
            return {"plan": plan, "digest": digest, "created_at": created_at}

        return _run(self.connect, self._resource, operation)

    def confirm_in_transaction(
        self, cur, plan_id, expected_digest, *, epoch, checkpoint_sha256, obligation_ids
    ):
        _id(plan_id)
        _hash_id(expected_digest)
        _id(epoch)
        _hash_id(checkpoint_sha256)
        _obligations(obligation_ids)
        try:
            self._transaction(cur, held=True)
            state = self._state(cur)
            record = self._plan_record(cur, plan_id)
            _need(record is not None, "recovery_plan_not_found")
            plan = record["plan"]
            _need(record["digest"] == expected_digest, "recovery_plan_digest_mismatch")
            _need(plan.target_epoch == epoch, "recovery_epoch_mismatch")
            _need(
                plan.source_checkpoint_sha256 == checkpoint_sha256, "recovery_checkpoint_mismatch"
            )
            _need(plan.position_obligation_ids == obligation_ids, "recovery_obligations_mismatch")
            previous = self._transition(cur, plan_id)
            if previous is not None:
                return previous[1]
            _need(state.generation == plan.expected_generation, "stale_assignment_generation")
            _need(state.generation < MAX_GENERATION, "assignment_generation_exhausted")
            moment = self._now(cur)
            assigned = Assignment(
                self.binding,
                state.generation + 1,
                plan_id,
                plan.target_robot,
                epoch,
                checkpoint_sha256,
                moment,
            )
            assignment_body = _encode(_assignment(assigned))
            receipt = {
                "schema_version": 1,
                "plan_id": plan_id,
                "plan_digest": expected_digest,
                "actor_id": plan.actor_id,
                "origin": "owner_confirmed_recovery",
                "review_excluded": True,
                "previous_assignment": _assignment(state.assignment),
                "assignment": _assignment(assigned),
                "position_obligation_ids": list(obligation_ids),
                "application_incarnation": plan.application_incarnation,
                "confirmed_at": moment,
                "held": True,
            }
            receipt_body = _encode(receipt)
            cur.execute(
                f"INSERT INTO {_TRANSITIONS} VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    self._key,
                    assigned.generation,
                    plan_id,
                    assignment_body,
                    _sha(assignment_body),
                    receipt_body,
                    _sha(receipt_body),
                ),
            )
            cur.execute(
                f"UPDATE {_ACCOUNTS} SET generation=%s,held=1,assignment=%s,assignment_sha256=%s "
                "WHERE account_key=%s",
                (assigned.generation, assignment_body, _sha(assignment_body), self._key),
            )
            return receipt
        except (RecoveryRefused, RecoveryUnavailable):
            raise
        except Exception:
            raise RecoveryUnavailable("recovery_store_unavailable") from None
