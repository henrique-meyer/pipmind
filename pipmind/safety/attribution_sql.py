"""Immutable dry-gateway receipt attribution; no assignment authority or relabeling."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pipmind.contracts import loads, stamp, utc
from pipmind.safety.admission import decode_request
from pipmind.safety.integration_sql import MAX_ENVELOPE_BYTES, MAX_RECEIPT_BYTES
from pipmind.safety.recovery_sql import Assignment, _assignment, _parse_assignment
from pipmind.safety.session import SessionBinding, account_key, validate_binding

MAX_LINK_BYTES = 65536
MAX_REVISION = 9223372036854775807
_TABLE = "dbo.pm_safety_attribution_v1"
_GATEWAY = "dbo.pm_safety_integration_journal_v1"
_BIN = "COLLATE Latin1_General_100_BIN2"
_DDL = f"""IF OBJECT_ID('{_TABLE}','U') IS NULL CREATE TABLE {_TABLE} (
account_key char(64) {_BIN} NOT NULL,channel varchar(10) {_BIN} NOT NULL,
record_id nvarchar(128) {_BIN} NOT NULL,body nvarchar(max) NOT NULL,sha256 char(64) {_BIN} NOT NULL,
CONSTRAINT PK_pm_safety_attribution_v1 PRIMARY KEY(account_key,channel,record_id))"""


class AttributionRefused(Exception):
    """Fixed validation refusal; no dynamic private payload text."""


class AttributionUnavailable(Exception):
    """Retained identity, caller lock scope or SQL cannot be verified."""


class AttributionCommitUnknown(Exception):
    """Own bootstrap/read transaction acknowledgement lost; no automatic retry."""


def _need(ok, code="invalid_attribution"):
    if not ok:
        raise AttributionRefused(code)


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
    _need(valid)


def _hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _plain(value, active=None):
    if active is None:
        active = set()
    if value is None or type(value) in (str, int, bool):
        return
    _need(type(value) in (dict, list))
    _need(id(value) not in active)
    active.add(id(value))
    try:
        if type(value) is dict:
            _need(all(type(key) is str for key in value))
            for child in value.values():
                _plain(child, active)
        else:
            for child in value:
                _plain(child, active)
    finally:
        active.remove(id(value))


def _json(value, ceiling=MAX_LINK_BYTES, *, gateway=False):
    try:
        _plain(value)
        body = json.dumps(
            value, ensure_ascii=not gateway, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        _need(len(body.encode("utf-8")) <= ceiling, "attribution_too_large")
        return body
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AttributionRefused("invalid_attribution") from None


def _binding(value):
    try:
        validate_binding(value)
        return {
            "account_id": value.account_id,
            "environment": value.environment,
            "currency": value.currency,
        }
    except Exception:
        raise AttributionRefused("invalid_attribution_binding") from None


def _assignment_payload(value):
    try:
        return _assignment(value)
    except Exception:
        raise AttributionRefused("invalid_attribution_assignment") from None


@dataclass(frozen=True, slots=True)
class AttributionLink:
    binding: SessionBinding
    channel: str
    record_id: str
    request_sha256: str
    receipt_sha256: str
    scope_sha256: str
    assignment: Assignment
    observed_at: str
    schema_version: int = 1
    dispatch_authorized: bool = False


def encode_link(link):
    _need(type(link) is AttributionLink)
    _need(
        type(link.schema_version) is int
        and link.schema_version == 1
        and link.dispatch_authorized is False
    )
    _need(type(link.channel) is str and link.channel in ("accept", "check", "reconcile"))
    _id(link.record_id)
    _need(all(_sha(v) for v in (link.request_sha256, link.receipt_sha256, link.scope_sha256)))
    binding = _binding(link.binding)
    assignment = _assignment_payload(link.assignment)
    _need(link.assignment is not None and link.assignment.binding == link.binding)
    try:
        _need(type(link.observed_at) is str)
        utc(link.observed_at)
    except Exception:
        raise AttributionRefused("invalid_attribution_time") from None
    return _json(
        {
            "schema_version": link.schema_version,
            "binding": binding,
            "channel": link.channel,
            "record_id": link.record_id,
            "request_sha256": link.request_sha256,
            "receipt_sha256": link.receipt_sha256,
            "scope_sha256": link.scope_sha256,
            "assignment": assignment,
            "observed_at": link.observed_at,
            "dispatch_authorized": False,
        }
    )


def decode_link(raw):
    try:
        _need(type(raw) is str and len(raw.encode("utf-8")) <= MAX_LINK_BYTES)
        data = loads(raw)
        _need(
            type(data) is dict
            and set(data)
            == {
                "schema_version",
                "binding",
                "channel",
                "record_id",
                "request_sha256",
                "receipt_sha256",
                "scope_sha256",
                "assignment",
                "observed_at",
                "dispatch_authorized",
            }
        )
        _need(
            type(data["binding"]) is dict
            and set(data["binding"]) == {"account_id", "environment", "currency"}
        )
        link = AttributionLink(
            SessionBinding(**data["binding"]),
            data["channel"],
            data["record_id"],
            data["request_sha256"],
            data["receipt_sha256"],
            data["scope_sha256"],
            _parse_assignment(data["assignment"]),
            data["observed_at"],
            data["schema_version"],
            data["dispatch_authorized"],
        )
        _need(encode_link(link) == raw)
        return link
    except Exception:
        raise AttributionRefused("invalid_attribution") from None


def _fail():
    raise AttributionUnavailable("attribution_store_unavailable")


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
    if row is None or type(row[0]) is not int or row[0] < 0:
        _fail()


def _run(connect, resources, operation):
    db, committing = None, False
    try:
        db = connect()
        if getattr(db, "autocommit", None) is not False:
            _fail()
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        for resource in resources:
            _lock(cur, resource)
        result = operation(cur)
        committing = True
        db.commit()
        return result
    except BaseException as exc:
        if committing:
            raise AttributionCommitUnknown("attribution_commit_unknown") from None
        if not isinstance(exc, Exception) or isinstance(exc, AttributionUnavailable):
            raise
        raise AttributionUnavailable("attribution_store_unavailable") from None
    finally:
        if db is not None:
            try:
                db.close()
            except BaseException:
                pass


def bootstrap(connect):
    _need(callable(connect), "invalid_attribution_connector")
    _run(connect, ("pipmind-safety-attribution-schema-v1",), lambda cur: cur.execute(_DDL))


class SqlAttributionStore:
    """Trusted same-DB caller locks bind immutable records; no assignment activation."""

    def __init__(self, connect, binding):
        _need(callable(connect), "invalid_attribution_connector")
        _binding(binding)
        self._connect, self._binding, self._key = connect, binding, account_key(binding)
        self._resources = (
            "pipmind-safety-recovery-v1:" + self._key,
            "pipmind-safety-integration-v1:" + self._key,
        )

    @property
    def binding(self):
        return self._binding

    def _scope(self, cur):
        if getattr(getattr(cur, "connection", None), "autocommit", None) is True:
            _fail()
        cur.execute("SELECT @@TRANCOUNT")
        row = cur.fetchone()
        if row is None or type(row[0]) is not int or row[0] < 1:
            _fail()
        # Verify, never acquire, earlier caller locks after admission/book work.
        for resource in self._resources:
            cur.execute("SELECT APPLOCK_MODE('public',%s,'Transaction')", (resource,))
            row = cur.fetchone()
            if row is None or row[0] != "Exclusive":
                _fail()

    def _read(self, cur, channel, record_id):
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,sha256 FROM {_TABLE} "
            "WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s AND channel=%s AND record_id=%s",
            (2 * MAX_LINK_BYTES, self._key, channel, record_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            if type(row[0]) is not str or _hash(row[0]) != row[1]:
                _fail()
            link = decode_link(row[0])
            if (
                link.binding != self.binding
                or link.channel != channel
                or link.record_id != record_id
            ):
                _fail()
            return link
        except Exception:
            raise AttributionUnavailable("attribution_store_unavailable") from None

    def _gateway(self, cur, channel, record_id, result=None):
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,body_sha256,"
            f"CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256 FROM {_GATEWAY} "
            "WHERE account_key=%s AND channel=%s AND record_id=%s",
            (2 * MAX_ENVELOPE_BYTES, 2 * MAX_RECEIPT_BYTES, self._key, channel, record_id),
        )
        row = cur.fetchone()
        try:
            if row is None or type(row[0]) is not str or type(row[2]) is not str:
                _fail()
            if (
                len(row[0].encode()) > MAX_ENVELOPE_BYTES
                or len(row[2].encode()) > MAX_RECEIPT_BYTES
                or _hash(row[0]) != row[1]
                or _hash(row[2]) != row[3]
            ):
                _fail()
            body, receipt = loads(row[0]), loads(row[2])
            if (
                _json(body, MAX_ENVELOPE_BYTES, gateway=True) != row[0]
                or _json(receipt, MAX_RECEIPT_BYTES, gateway=True) != row[2]
                or (result is not None and _json(result, MAX_RECEIPT_BYTES, gateway=True) != row[2])
            ):
                _fail()
            scope = receipt["scope"]
            if (
                receipt["channel"] != channel
                or receipt["record_id"] != record_id
                or tuple(receipt[k] for k in ("account_id", "environment", "currency"))
                != (self.binding.account_id, self.binding.environment, self.binding.currency)
                or receipt["request_sha256"] != row[1]
                or receipt["dispatch_authorized"] is not False
                or receipt["dispatch_enabled"] is not False
                or receipt["historical_receipt_only"] is not True
                or set(scope)
                != {
                    "authority_epoch",
                    "owner_id",
                    "generation",
                    "resource",
                    "session_id",
                    "policy_book_sha256",
                    "mapping_sha256",
                    "control_revision",
                }
                or receipt["session_id"] != scope["session_id"]
                or body["session_id"] != scope["session_id"]
                or body["book"] != scope["policy_book_sha256"]
                or body["mapping"] != scope["mapping_sha256"]
            ):
                _fail()
            for name in ("authority_epoch", "owner_id", "resource", "session_id"):
                _id(scope[name])
            if (
                not all(_sha(scope[k]) for k in ("policy_book_sha256", "mapping_sha256"))
                or type(scope["generation"]) is not int
                or not 0 < scope["generation"] <= MAX_REVISION
                or type(scope["control_revision"]) is not int
                or not 0 <= scope["control_revision"] <= MAX_REVISION
            ):
                _fail()
            request = decode_request(body["request"])
            if channel == "accept" and request.order["client_order_id"] != record_id:
                _fail()
            return row[1], row[3], _hash(_json(scope, gateway=True)), scope
        except Exception:
            raise AttributionUnavailable("attribution_store_unavailable") from None

    def record_in_transaction(self, cur, assignment, result):
        # Freeze local caller output before any SQL read. Existing attribution is
        # checked before comparing the supplied CURRENT assignment to old scope.
        try:
            frozen = loads(_json(result, MAX_RECEIPT_BYTES, gateway=True))
            channel, record_id = frozen["channel"], frozen["record_id"]
            _need(type(channel) is str and channel in ("accept", "check", "reconcile"))
            _id(record_id)
            self._scope(cur)
            old = self._read(cur, channel, record_id)
            request_hash, receipt_hash, scope_hash, scope = self._gateway(
                cur, channel, record_id, frozen
            )
            if old is not None:
                if (old.request_sha256, old.receipt_sha256, old.scope_sha256) != (
                    request_hash,
                    receipt_hash,
                    scope_hash,
                ):
                    _fail()
                return old  # NEVER relabel an exact historical receipt.
            _assignment_payload(assignment)
            if (
                assignment is None
                or assignment.binding != self.binding
                or assignment.epoch != scope["authority_epoch"]
            ):
                _fail()
            cur.execute("SELECT SYSUTCDATETIME()")
            now = cur.fetchone()[0]
            if type(now) is not datetime or now.tzinfo is not None:
                _fail()
            link = AttributionLink(
                self.binding,
                channel,
                record_id,
                request_hash,
                receipt_hash,
                scope_hash,
                assignment,
                stamp(now.replace(tzinfo=UTC)),
            )
            raw = encode_link(link)
            cur.execute(
                f"INSERT INTO {_TABLE} VALUES(%s,%s,%s,%s,%s)",
                (self._key, channel, record_id, raw, _hash(raw)),
            )
            return link  # Provisional until outer application lease commit acknowledgement.
        except Exception:
            raise AttributionUnavailable("attribution_store_unavailable") from None

    def lookup(self, channel, record_id):
        _need(type(channel) is str and channel in ("accept", "check", "reconcile"))
        _id(record_id)

        def operation(cur):
            link = self._read(cur, channel, record_id)
            if link is not None:
                hashes = self._gateway(cur, channel, record_id)[:3]
                if hashes != (link.request_sha256, link.receipt_sha256, link.scope_sha256):
                    _fail()
            return link

        return _run(self._connect, self._resources, operation)
