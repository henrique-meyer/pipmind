"""SQL-fenced dry gateway integration. There is deliberately no send operation."""

import hashlib
import json
import re
from decimal import Decimal
from uuid import uuid4

from pipmind.contracts import stamp
from pipmind.gateway.config import GatewayError, SymbolMap, number
from pipmind.safety.admission import MAX_REQUEST_BYTES, decode_request, encode_request
from pipmind.safety.admission_sql import SqlAdmissionStore
from pipmind.safety.controls_sql import SqlControlStore
from pipmind.safety.lease_sql import LeaseCommitUnknown
from pipmind.safety.session import account_key

MAX_RECEIPT_BYTES = 8192
MAX_ENVELOPE_BYTES = 8 * MAX_REQUEST_BYTES
_ACCOUNTS = "dbo.pm_safety_integration_accounts_v1"
_JOURNAL = "dbo.pm_safety_integration_journal_v1"
_BIN = "COLLATE Latin1_General_100_BIN2"
_CONFLICT = object()
_DDL = (
    f"""IF OBJECT_ID('{_ACCOUNTS}','U') IS NULL CREATE TABLE {_ACCOUNTS} (
    account_key char(64) {_BIN} NOT NULL PRIMARY KEY,
    account_id nvarchar(128) {_BIN} NOT NULL,environment varchar(4) {_BIN} NOT NULL,
    currency char(3) {_BIN} NOT NULL,scope nvarchar(max) NULL,
    scope_sha256 char(64) NULL,hold_code varchar(64) NOT NULL)""",
    f"""IF OBJECT_ID('{_JOURNAL}','U') IS NULL CREATE TABLE {_JOURNAL} (
    account_key char(64) {_BIN} NOT NULL REFERENCES {_ACCOUNTS}(account_key),
    channel varchar(10) {_BIN} NOT NULL,record_id nvarchar(128) {_BIN} NOT NULL,
    body nvarchar(max) NOT NULL,body_sha256 char(64) NOT NULL,
    receipt nvarchar(max) NOT NULL,receipt_sha256 char(64) NOT NULL,
    CONSTRAINT PK_pm_safety_integration_journal_v1
    PRIMARY KEY(account_key,channel,record_id))""",
)


class IntegrationRefused(Exception):
    """Fixed validation code; never includes submitted data."""


class IntegrationUnavailable(Exception):
    """Fixed storage failure; no fallback authority."""


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identifier(value):
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
    if not valid:
        raise IntegrationRefused("invalid_integration_id")


def _unavailable():
    raise IntegrationUnavailable("integration_store_unavailable")


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
    if row is None or row[0] < 0:
        _unavailable()


def bootstrap(connect):
    """Explicit new tables only; never part of existing shadow migrations."""
    db, committing = None, False
    try:
        db = connect()
        if getattr(db, "autocommit", None) is not False:
            _unavailable()
        cur = db.cursor()
        cur.execute("SET NOCOUNT ON; SET XACT_ABORT ON")
        _lock(cur, "pipmind-safety-integration-schema-v1")
        for statement in _DDL:
            cur.execute(statement)
        committing = True
        db.commit()
    except BaseException as error:
        if committing:
            raise LeaseCommitUnknown("lease_commit_unknown") from None
        if not isinstance(error, Exception) or isinstance(error, IntegrationUnavailable):
            raise
        _unavailable()
    finally:
        if db is not None:
            try:
                db.close()
            except BaseException:
                pass


class SqlSafetyGateway:
    """Trusted same-database composition; receipts are never execution permits.

    Connectors, policy/source evidence and symbol mapping are trusted deployment
    inputs. This class cannot attest their broker provenance. No HTTP, environment
    loading, migration, broker callback, reservation release or SQL restore reset
    is performed by construction or by any method.
    """

    def __init__(
        self, admission, controls, symbols, *, base_units_per_size, before=None, after=None
    ):
        if (before is None) != (after is None) or (
            before is not None and (not callable(before) or not callable(after))
        ):
            raise IntegrationRefused("invalid_integration_hooks")
        self._before, self._after = before, after
        if (
            type(admission) is not SqlAdmissionStore
            or type(controls) is not SqlControlStore
            or type(symbols) is not SymbolMap
            or admission.binding != controls.binding
        ):
            raise IntegrationRefused("integration_binding_mismatch")
        if (
            type(base_units_per_size) is not dict
            or set(base_units_per_size) != set(symbols.by_symbol)
            or any(
                type(v) is not Decimal
                or not v.is_finite()
                or v <= 0
                or len(v.as_tuple().digits) > 64
                or abs(v.as_tuple().exponent) > 64
                for v in base_units_per_size.values()
            )
        ):
            raise IntegrationRefused("invalid_integration_unit_factors")
        self._unit_factors = dict(base_units_per_size)
        rows = [
            {
                "instrument": item.instrument,
                "market_id": item.market_id,
                "unit": item.unit,
                "quantity_factor": str(item.quantity_factor),
                "increment": str(item.increment),
                "minimum": str(item.minimum),
                "stale_seconds": str(item.stale_seconds),
            }
            for item in sorted(symbols.by_symbol.values(), key=lambda item: item.instrument)
        ]
        self._symbols = SymbolMap(rows)
        self._mapping_hash = _hash(
            _json(
                {
                    "symbols": rows,
                    "base_units_per_size": {k: str(v) for k, v in self._unit_factors.items()},
                }
            )
        )
        self.admission, self.controls = admission, controls
        self._binding = admission.binding
        self._key = account_key(self._binding)
        self._resource = "pipmind-safety-integration-v1:" + self._key

    def _scope(self, grant, state):
        return {
            "authority_epoch": grant.authority_epoch,
            "owner_id": grant.owner_id,
            "generation": grant.generation,
            "resource": grant.binding.resource,
            "session_id": self.admission.session_id,
            "policy_book_sha256": self.admission.policy_book_sha256,
            "mapping_sha256": self._mapping_hash,
            "control_revision": state.revision,
        }

    def _account(self, cur):
        _lock(cur, self._resource)
        cur.execute(
            "SELECT account_id,environment,currency,"
            "CASE WHEN DATALENGTH(scope)<=8192 THEN scope END,scope_sha256,hold_code "
            f"FROM {_ACCOUNTS} WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s",
            (self._key,),
        )
        row = cur.fetchone()
        binding = (self._binding.account_id, self._binding.environment, self._binding.currency)
        if row is None:
            cur.execute(
                f"SELECT CASE WHEN EXISTS(SELECT 1 FROM {_JOURNAL} WHERE account_key=%s) "
                "THEN 1 ELSE 0 END",
                (self._key,),
            )
            if cur.fetchone()[0]:
                _unavailable()
            cur.execute(
                f"INSERT INTO {_ACCOUNTS} VALUES(%s,%s,%s,%s,NULL,NULL,%s)",
                (self._key, *binding, "startup_reconciliation_required"),
            )
            return None, "startup_reconciliation_required"
        if tuple(row[:3]) != binding or row[5] not in {
            "ready",
            "startup_reconciliation_required",
            "safety_revalidation_required",
        }:
            _unavailable()
        scope = None
        if row[3] is not None:
            try:
                scope = json.loads(row[3])
                if (
                    type(scope) is not dict
                    or len(row[3].encode()) > 4096
                    or _json(scope) != row[3]
                    or _hash(row[3]) != row[4]
                ):
                    _unavailable()
                if set(scope) != {
                    "authority_epoch",
                    "owner_id",
                    "generation",
                    "resource",
                    "session_id",
                    "policy_book_sha256",
                    "mapping_sha256",
                    "control_revision",
                }:
                    _unavailable()
                for name in ("authority_epoch", "owner_id", "resource", "session_id"):
                    _identifier(scope[name])
                if any(
                    type(scope[name]) is not int
                    or not minimum <= scope[name] <= 9223372036854775807
                    for name, minimum in (("generation", 1), ("control_revision", 0))
                ):
                    _unavailable()
                if any(
                    type(scope[name]) is not str
                    or re.fullmatch("[0-9a-f]{64}", scope[name]) is None
                    for name in ("policy_book_sha256", "mapping_sha256")
                ):
                    _unavailable()
            except (ValueError, TypeError, UnicodeError, IntegrationRefused):
                _unavailable()
        elif row[4] is not None or row[5] == "ready":
            _unavailable()
        return scope, row[5]

    def _hold(self, cur, scope, code):
        body = None if scope is None else _json(scope)
        cur.execute(
            f"UPDATE {_ACCOUNTS} SET scope=%s,scope_sha256=%s,hold_code=%s WHERE account_key=%s",
            (body, None if body is None else _hash(body), code, self._key),
        )

    def _prior(self, cur, channel, record_id, body):
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,body_sha256,"
            f"CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256 FROM {_JOURNAL} "
            "WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s AND channel=%s AND record_id=%s",
            (2 * MAX_ENVELOPE_BYTES, 2 * MAX_RECEIPT_BYTES, self._key, channel, record_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(row[2])
            if (
                type(row[0]) is not str
                or type(row[2]) is not str
                or len(row[0].encode()) > MAX_ENVELOPE_BYTES
                or _hash(row[0]) != row[1]
                or _hash(row[2]) != row[3]
                or _json(receipt) != row[2]
                or len(row[2].encode()) > MAX_RECEIPT_BYTES
                or receipt["record_id"] != record_id
                or receipt["channel"] != channel
                or receipt["account_id"] != self._binding.account_id
                or receipt["environment"] != self._binding.environment
                or receipt["currency"] != self._binding.currency
                or receipt["dispatch_authorized"] is not False
            ):
                _unavailable()
        except (KeyError, ValueError, TypeError, UnicodeError, AttributeError):
            _unavailable()
        return receipt if row[0] == body else _CONFLICT

    def _record(self, cur, channel, record_id, body, scope, state, outcome, code, assessment=None):
        receipt = {
            "schema_version": 1,
            "channel": channel,
            "record_id": record_id,
            "account_id": self._binding.account_id,
            "environment": self._binding.environment,
            "currency": self._binding.currency,
            "session_id": self.admission.session_id,
            "scope": scope,
            "request_sha256": _hash(body),
            "control_revision": state.revision,
            "observed_at": stamp(self.admission.server_time_in_transaction(cur)),
            "origin": "model" if channel in {"accept", "check"} else "source_reconciliation",
            "review_excluded": False,
            "outcome": outcome,
            "code": code,
            "assessment": None
            if assessment is None
            else {
                "within_limits": assessment.within_limits,
                "code": assessment.code,
                "session_revision": assessment.session_revision,
                "observation_sha256": assessment.observation_sha256,
                "checkpoint_sha256": assessment.checkpoint_sha256,
            },
            "dispatch_authorized": False,
            "dispatch_enabled": False,
            "historical_receipt_only": True,
        }
        raw = _json(receipt)
        if len(raw.encode()) > MAX_RECEIPT_BYTES:
            _unavailable()
        cur.execute(
            f"INSERT INTO {_JOURNAL} VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (self._key, channel, record_id, body, _hash(body), raw, _hash(raw)),
        )
        return json.loads(raw)

    def _mapping_code(self, request):
        try:
            order = request.order
            if order["action"] != "open" or order.get("related_client_order_id"):
                return "unsupported_dry_action"
            self._symbols.symbol(order["instrument"]).quantity(order["size"])
            if (
                request.facts.proposal.instrument != order["instrument"]
                or request.facts.proposal.base_units_per_size
                != self._unit_factors[order["instrument"]]
            ):
                return "gateway_unit_factor_mismatch"
            observed = request.evidence.observed
            if request.evidence.position_marks:
                rows, ids = observed.positions["positions"], observed.position_ids
                positions = dict(zip(ids, rows, strict=True))
                for mark in request.evidence.position_marks:
                    instrument = positions[mark.position_id]["instrument"]
                    if mark.base_units_per_size != self._unit_factors[instrument]:
                        return "gateway_unit_factor_mismatch"
            for name in ("stop", "limit"):
                if order.get(name) is not None:
                    number(order[name], positive=True)
        except (GatewayError, KeyError, TypeError, ValueError):
            return "gateway_mapping_refused"
        return None

    def _operate(self, channel, record_id, grant, request):
        _identifier(record_id)
        frozen = decode_request(encode_request(request))
        body = _json(
            {
                "request": encode_request(frozen),
                "session_id": self.admission.session_id,
                "book": self.admission.policy_book_sha256,
                "mapping": self._mapping_hash,
            }
        )
        if len(body.encode()) > MAX_ENVELOPE_BYTES:
            raise IntegrationRefused("integration_request_too_large")

        def operation(cur):
            # Consistent lock order. No control writer takes an earlier lock.
            self.admission.sessions.read_in_transaction(cur)
            state = self.controls.read_in_transaction(cur)
            retained_scope, hold_code = self._account(cur)
            scope = self._scope(grant, state)
            if channel != "reconcile":
                prior = self._prior(cur, channel, record_id, body)
                if prior is not None:
                    return prior  # Historical result only, after outer lease checks/commit.
            if channel == "reconcile":
                recheck = (
                    frozen.order["client_order_id"]
                    if self._accepted(cur, frozen.order["client_order_id"])
                    else None
                )
                assessment = self.admission.preview_in_transaction(
                    cur, frozen, recheck_request_id=recheck
                )
                mapping_code = self._mapping_code(frozen)
                ready = assessment.within_limits and mapping_code is None
                self._hold(
                    cur,
                    scope if ready else None,
                    "ready" if ready else "safety_revalidation_required",
                )
                result = self._record(
                    cur,
                    channel,
                    record_id,
                    body,
                    scope,
                    state,
                    "dry_ready" if ready else "held",
                    mapping_code or assessment.code,
                    assessment,
                )
                final = self.admission.preview_in_transaction(
                    cur, frozen, recheck_request_id=recheck
                )
                if ready and not final.within_limits:
                    raise IntegrationUnavailable("integration_facts_expired")
                return result
            code = (
                "kill_switch"
                if state.killed
                else "trading_paused"
                if state.paused
                else "reconciliation_required"
                if hold_code != "ready" or retained_scope != scope
                else self._mapping_code(frozen)
            )
            if code:
                return self._record(cur, channel, record_id, body, scope, state, "refused", code)
            if channel == "accept":
                admitted = self.admission.admit_in_transaction(cur, grant, frozen)
                if self.admission.is_conflict(admitted):
                    return _CONFLICT
                assessment = admitted["assessment"]
                # A retained admission may be old; recheck rather than consuming its result.
                if assessment.within_limits:
                    assessment = self.admission.preview_in_transaction(
                        cur, frozen, recheck_request_id=frozen.order["client_order_id"]
                    )
                    if not assessment.within_limits:
                        # No newly staged positive reservation may survive an
                        # execution-boundary proof failure. Existing retained
                        # reservations remain unchanged when this aborts.
                        raise IntegrationUnavailable("integration_facts_expired")
            else:
                accepted = self._accepted(cur, frozen.order["client_order_id"])
                if not accepted:
                    return self._record(
                        cur,
                        channel,
                        record_id,
                        body,
                        scope,
                        state,
                        "refused",
                        "dry_command_not_accepted",
                    )
                assessment = self.admission.preview_in_transaction(
                    cur, frozen, recheck_request_id=frozen.order["client_order_id"]
                )
            if not assessment.within_limits:
                self._hold(cur, None, "safety_revalidation_required")
            outcome = (
                ("dry_accepted" if channel == "accept" else "dry_checked")
                if assessment.within_limits
                else "refused"
            )
            result = self._record(
                cur, channel, record_id, body, scope, state, outcome, assessment.code, assessment
            )
            if assessment.within_limits:
                final = self.admission.preview_in_transaction(
                    cur, frozen, recheck_request_id=frozen.order["client_order_id"]
                )
                if not final.within_limits:
                    raise IntegrationUnavailable("integration_facts_expired")
            return result

        def composed(cur):
            # Trusted SQL-only hooks run on this SAME lease cursor. The before
            # hook may acquire earlier session/control/recovery locks, never a
            # later connection or nested lease callback. Neither hook commits.
            context = self._before(cur, grant) if self._before is not None else None
            result = operation(cur)
            if result is not _CONFLICT and self._after is not None:
                return self._after(cur, context, result)
            return result

        result = self.admission.leases.compare_and_act(grant, composed)
        if result is _CONFLICT:
            raise IntegrationRefused("conflicting_integration_request")
        return result

    def _accepted(self, cur, request_id):
        cur.execute(
            "SELECT CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256 "
            f"FROM {_JOURNAL} "
            "WHERE account_key=%s AND channel='accept' AND record_id=%s",
            (2 * MAX_RECEIPT_BYTES, self._key, request_id),
        )
        row = cur.fetchone()
        if row is None:
            return False
        try:
            value = json.loads(row[0])
            if (
                type(row[0]) is not str
                or len(row[0].encode()) > MAX_RECEIPT_BYTES
                or _hash(row[0]) != row[1]
                or _json(value) != row[0]
                or value["channel"] != "accept"
                or value["record_id"] != request_id
                or value["account_id"] != self._binding.account_id
                or value["environment"] != self._binding.environment
                or value["currency"] != self._binding.currency
                or value["session_id"] != self.admission.session_id
                or value["scope"]["mapping_sha256"] != self._mapping_hash
                or value["scope"]["policy_book_sha256"] != self.admission.policy_book_sha256
                or value["dispatch_authorized"] is not False
            ):
                _unavailable()
            return value["outcome"] == "dry_accepted"
        except (ValueError, TypeError, KeyError, AttributeError):
            _unavailable()

    def reconcile(self, grant, request):
        return self._operate("reconcile", uuid4().hex, grant, request)

    def accept(self, grant, request, *, origin="model", override_id=None):
        # Manual order execution belongs to the separately authorized transport path.
        if origin != "model" or override_id is not None:
            raise IntegrationRefused("unsupported_order_origin")
        frozen = decode_request(encode_request(request))
        return self._operate("accept", frozen.order["client_order_id"], grant, frozen)

    def check(self, grant, check_id, request):
        return self._operate("check", check_id, grant, request)
