"""Isolated, lease-fenced SQL reservations. No order transport or runtime wiring."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

from pipmind.contracts import stamp
from pipmind.safety.admission import (
    MAX_COUNT,
    MAX_POLICY_BOOK_BYTES,
    MAX_RECEIPT_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESERVATION_BYTES,
    MAX_RESERVATIONS,
    AdmissionRefused,
    assess,
    decode_policy_book,
    decode_receipt,
    decode_request,
    decode_reservation,
    encode_policy_book,
    encode_receipt,
    encode_request,
    encode_reservation,
    policy_book_digest,
)
from pipmind.safety.lease_sql import (
    LeaseCommitUnknown,
    LeaseRefused,
    LeaseUnavailable,
    SqlLeaseStore,
)
from pipmind.safety.session import SessionRefused, SessionUnavailable, account_key
from pipmind.safety.session_sql import SqlSessionStore

_ACCOUNTS = "dbo.pm_safety_admission_accounts_v1"
_BOOKS = "dbo.pm_safety_admission_books_v1"
_JOURNAL = "dbo.pm_safety_admission_journal_v1"
_RESERVATIONS = "dbo.pm_safety_admission_reservations_v1"
_FLOORS = "dbo.pm_safety_admission_floors_v1"
_BIN = "COLLATE Latin1_General_100_BIN2"
# A verdict may be normalized only after the OUTER lease operation acknowledges commit.
ADMISSION_CONFLICT = object()
_CONFLICT = ADMISSION_CONFLICT  # Preserve the existing internal identity.
_DDL = (
    f"""IF OBJECT_ID('{_ACCOUNTS}','U') IS NULL CREATE TABLE {_ACCOUNTS} (
    account_key char(64) {_BIN} NOT NULL PRIMARY KEY,
    account_id nvarchar(128) {_BIN} NOT NULL,environment varchar(4) {_BIN} NOT NULL,
    currency char(3) {_BIN} NOT NULL)""",
    f"""IF OBJECT_ID('{_BOOKS}','U') IS NULL CREATE TABLE {_BOOKS} (
    sha256 char(64) {_BIN} NOT NULL PRIMARY KEY,body nvarchar(max) NOT NULL)""",
    f"""IF OBJECT_ID('{_JOURNAL}','U') IS NULL CREATE TABLE {_JOURNAL} (
    account_key char(64) {_BIN} NOT NULL REFERENCES {_ACCOUNTS}(account_key),
    request_id nvarchar(128) {_BIN} NOT NULL,session_id nvarchar(128) {_BIN} NOT NULL,
    policy_book_sha256 char(64) {_BIN} NOT NULL REFERENCES {_BOOKS}(sha256),
    request nvarchar(max) NOT NULL,request_sha256 char(64) NOT NULL,
    receipt nvarchar(max) NOT NULL,receipt_sha256 char(64) NOT NULL,reserved bit NOT NULL,
    CONSTRAINT PK_pm_safety_admission_journal_v1 PRIMARY KEY(account_key,request_id))""",
    f"""IF OBJECT_ID('{_RESERVATIONS}','U') IS NULL CREATE TABLE {_RESERVATIONS} (
    account_key char(64) {_BIN} NOT NULL,request_id nvarchar(128) {_BIN} NOT NULL,
    session_id nvarchar(128) {_BIN} NOT NULL,body nvarchar(max) NOT NULL,
    sha256 char(64) NOT NULL,
    CONSTRAINT PK_pm_safety_admission_reservations_v1 PRIMARY KEY(account_key,request_id),
    CONSTRAINT FK_pm_safety_admission_reservations_v1 FOREIGN KEY(account_key,request_id)
      REFERENCES {_JOURNAL}(account_key,request_id))""",
    f"""IF OBJECT_ID('{_FLOORS}','U') IS NULL CREATE TABLE {_FLOORS} (
    account_key char(64) {_BIN} NOT NULL REFERENCES {_ACCOUNTS}(account_key),
    session_id nvarchar(128) {_BIN} NOT NULL,
    external_count bigint NOT NULL CHECK(external_count>=0),
    CONSTRAINT PK_pm_safety_admission_floors_v1 PRIMARY KEY(account_key,session_id))""",
)


def _hash(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _unavailable():
    raise LeaseUnavailable("admission_store_unavailable")


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
    if not valid:
        raise AdmissionRefused("invalid_admission_request_id")


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
        raise LeaseRefused("admission_store_busy")


def _run(connect, resource, operation):
    db, committing = None, False
    try:
        db = connect()
        if getattr(db, "autocommit", None) is not False:
            raise LeaseRefused("admission_transaction_required")
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        _lock(cur, resource)
        result = operation(cur)
        committing = True
        db.commit()
        return result
    except BaseException as exc:
        if committing:
            raise LeaseCommitUnknown("lease_commit_unknown") from None
        if not isinstance(exc, Exception) or isinstance(exc, (LeaseRefused, LeaseUnavailable)):
            raise
        raise LeaseUnavailable("admission_store_unavailable") from None
    finally:
        if db is not None:
            try:
                db.close()  # Fresh non-pooled disconnect; no unbounded pytds rollback handshake.
            except BaseException:
                pass


def bootstrap(connect):
    """Explicit isolated DDL, never implicit in admission or read operations."""

    def operation(cur):
        for statement in _DDL:
            cur.execute(statement)

    _run(connect, "pipmind-safety-admission-schema-v1", operation)


def _decode(raw, digest, decoder, encoder, ceiling):
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > ceiling or _hash(raw) != digest:
            _unavailable()
        value = decoder(raw)
        if encoder(value) != raw:
            _unavailable()
        return value
    except (AdmissionRefused, ValueError, TypeError, RecursionError, OverflowError):
        raise LeaseUnavailable("admission_store_unavailable") from None


class SqlAdmissionStore:
    """Trusted connectors must all target the same physical SQL database.

    Constructor validation/freeze is local only. Admit uses only the lease's
    cursor; the own connector is for bootstrap/lookup/snapshot, never admission.
    """

    def __init__(self, connect, leases, sessions, policies):
        if (
            not callable(connect)
            or type(leases) is not SqlLeaseStore
            or type(sessions) is not SqlSessionStore
        ):
            raise AdmissionRefused("invalid_admission_store")
        book = encode_policy_book(policies)
        self._policies = decode_policy_book(book)
        binding = sessions.binding
        if (
            leases.binding.account_id != binding.account_id
            or leases.binding.environment != binding.environment
            or any(
                (p.account_id, p.environment, p.currency)
                != (binding.account_id, binding.environment, binding.currency)
                for p in self._policies
            )
        ):
            raise AdmissionRefused("admission_binding_mismatch")
        self._connect, self._leases, self._sessions = connect, leases, sessions
        self._binding, self._session_id = binding, sessions.session_id
        self._key = account_key(binding)
        self._resource = "pipmind-safety-admission-v1:" + self._key
        self._book_body, self._book_hash = book, policy_book_digest(self._policies)

    def _pin(self, cur, *, create=False):
        cur.execute(
            f"SELECT account_id,environment,currency FROM {_ACCOUNTS} "
            "WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s",
            (self._key,),
        )
        row = cur.fetchone()
        expected = (self._binding.account_id, self._binding.environment, self._binding.currency)
        if row is not None:
            if tuple(row) != expected:
                _unavailable()
            return True
        cur.execute(
            f"""SELECT CASE WHEN EXISTS(SELECT 1 FROM {_JOURNAL} WHERE account_key=%s)
            OR EXISTS(SELECT 1 FROM {_RESERVATIONS} WHERE account_key=%s)
            OR EXISTS(SELECT 1 FROM {_FLOORS} WHERE account_key=%s) THEN 1 ELSE 0 END""",
            (self._key, self._key, self._key),
        )
        if cur.fetchone()[0]:
            _unavailable()
        if create:
            cur.execute(f"INSERT INTO {_ACCOUNTS} VALUES(%s,%s,%s,%s)", (self._key, *expected))
        return create

    def _book(self, cur, digest, *, create=None):
        _lock(cur, "pipmind-safety-admission-book-v1:" + digest)
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END FROM {_BOOKS} "
            "WITH(UPDLOCK,HOLDLOCK) WHERE sha256=%s",
            (2 * MAX_POLICY_BOOK_BYTES, digest),
        )
        row = cur.fetchone()
        if row is None:
            if create is None:
                _unavailable()
            cur.execute(f"INSERT INTO {_BOOKS} VALUES(%s,%s)", (digest, create))
        else:
            book = _decode(
                row[0], digest, decode_policy_book, encode_policy_book, MAX_POLICY_BOOK_BYTES
            )
            if (
                policy_book_digest(book) != digest
                or any(
                    (p.account_id, p.environment, p.currency)
                    != (self._binding.account_id, self._binding.environment, self._binding.currency)
                    for p in book
                )
                or (create is not None and row[0] != create)
            ):
                _unavailable()

    def _floor(self, cur):
        cur.execute(
            f"SELECT external_count FROM {_FLOORS} WITH(UPDLOCK,HOLDLOCK) "
            "WHERE account_key=%s AND session_id=%s",
            (self._key, self._session_id),
        )
        row = cur.fetchone()
        floor = 0 if row is None else row[0]
        if type(floor) is not int or not 0 <= floor <= MAX_COUNT:
            _unavailable()
        return floor

    def _reservations(self, cur):
        cur.execute(
            f"""SELECT TOP({MAX_RESERVATIONS + 1}) request_id,session_id,
            CASE WHEN DATALENGTH(body)<=%s THEN body END,sha256
            FROM {_RESERVATIONS} WITH(UPDLOCK,HOLDLOCK)
            WHERE account_key=%s ORDER BY request_id""",
            (2 * MAX_RESERVATION_BYTES, self._key),
        )
        result = []
        for _ in range(MAX_RESERVATIONS + 1):
            row = cur.fetchone()
            if row is None:
                break
            if len(result) == MAX_RESERVATIONS:
                _unavailable()  # Never call a prefix the complete reservation book.
            item = _decode(
                row[2], row[3], decode_reservation, encode_reservation, MAX_RESERVATION_BYTES
            )
            if item.request_id != row[0] or item.session_id != row[1]:
                _unavailable()
            result.append(item)
        return tuple(result)

    def _journal(self, cur, request_id):
        cur.execute(
            f"""SELECT session_id,policy_book_sha256,
            CASE WHEN DATALENGTH(request)<=%s THEN request END,request_sha256,
            CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256,reserved
            FROM {_JOURNAL} WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s AND request_id=%s""",
            (2 * MAX_REQUEST_BYTES, 2 * MAX_RECEIPT_BYTES, self._key, request_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        request = _decode(row[2], row[3], decode_request, encode_request, MAX_REQUEST_BYTES)
        receipt = _decode(row[4], row[5], decode_receipt, encode_receipt, MAX_RECEIPT_BYTES)
        if (
            request.order["client_order_id"] != request_id
            or receipt["request_id"] != request_id
            or receipt["session_id"] != row[0]
            or receipt["request_sha256"] != row[3]
            or receipt["policy_book_sha256"] != row[1]
            or (receipt["account_id"], receipt["environment"], receipt["currency"])
            != (self._binding.account_id, self._binding.environment, self._binding.currency)
            or receipt["assessment"].within_limits != bool(row[6])
        ):
            _unavailable()
        self._book(cur, row[1])
        cur.execute(
            f"SELECT session_id,CASE WHEN DATALENGTH(body)<=%s THEN body END,sha256 "
            f"FROM {_RESERVATIONS} WHERE account_key=%s AND request_id=%s",
            (2 * MAX_RESERVATION_BYTES, self._key, request_id),
        )
        projection = cur.fetchone()
        reserved = receipt["assessment"].reservation
        if reserved is None:
            if projection is not None:
                _unavailable()
        else:
            if projection is None:
                _unavailable()
            value = _decode(
                projection[1],
                projection[2],
                decode_reservation,
                encode_reservation,
                MAX_RESERVATION_BYTES,
            )
            if (
                encode_reservation(value) != encode_reservation(reserved)
                or value.request_id != request_id
                or value.session_id != row[0]
                or projection[0] != row[0]
            ):
                _unavailable()
        return row, receipt

    @staticmethod
    def _now(cur):
        cur.execute("SELECT SYSUTCDATETIME()")
        value = cur.fetchone()[0]
        if type(value) is not datetime or value.tzinfo is not None:
            _unavailable()
        return value.replace(tzinfo=UTC)

    @property
    def leases(self):
        return self._leases

    @property
    def sessions(self):
        return self._sessions

    @property
    def binding(self):
        return self._binding

    @property
    def session_id(self):
        return self._session_id

    @property
    def policy_book_sha256(self):
        return self._book_hash

    @staticmethod
    def freeze_request(request):
        """Return independent canonical request facts before entering SQL/awaits."""
        return decode_request(encode_request(request))

    @staticmethod
    def is_conflict(result):
        return result is ADMISSION_CONFLICT

    @staticmethod
    def server_time_in_transaction(cur):
        """Observe SQL UTC time using only the caller's existing cursor."""
        return SqlAdmissionStore._now(cur)

    @staticmethod
    def finalize_admission(result):
        """Normalize ONLY after an outer lease check and acknowledged commit.

        This pure helper cannot verify transaction acknowledgement itself.
        """
        if result is ADMISSION_CONFLICT:
            raise AdmissionRefused("conflicting_admission_request")
        return result

    def admit(self, grant, request):
        # Freeze before the lease lock: caller mutation while waiting cannot alter intent.
        frozen = self.freeze_request(request)
        result = self._leases.compare_and_act(
            grant, lambda cur: self.admit_in_transaction(cur, grant, frozen)
        )
        return self.finalize_admission(result)

    def admit_in_transaction(self, cur, grant, request):
        """Stage/replay admission on a trusted caller's already leased transaction.

        Caller owns lease -> session -> optional controls/integration locks before
        calling; this method reacquires session, then admission and book. It never
        opens, commits or closes a connection, or invokes a lease callback. Its
        returned value is provisional until the OUTER lease checks and commit.
        ADMISSION_CONFLICT must likewise not escape as a certain refusal before
        that acknowledgement. Transaction/lease ownership is a trusted contract,
        not an independent authorization or transport capability.
        """
        body = encode_request(request)
        frozen = decode_request(body)
        digest = _hash(body)
        request_id = frozen.order["client_order_id"]
        if (
            grant.binding.account_id != self._binding.account_id
            or grant.binding.environment != self._binding.environment
        ):
            raise LeaseRefused("admission_binding_mismatch")
        try:
            state = self._sessions.read_in_transaction(cur)
        except (SessionRefused, SessionUnavailable):
            _unavailable()  # Missing/corrupt state never becomes a synthetic session.
        if state.spec.binding != self._binding or state.spec.session_id != self._session_id:
            _unavailable()
        _lock(cur, self._resource)
        self._pin(cur, create=True)
        retained = self._journal(cur, request_id)
        if retained is not None:
            row, receipt = retained
            if (
                row[0] != self._session_id
                or row[1] != self._book_hash
                or row[2] != body
                or row[3] != digest
            ):
                return _CONFLICT  # Return only through final lease checks + acknowledged commit.
            return receipt
        reservations, floor = self._reservations(cur), self._floor(cur)
        now = self._now(cur)
        assessment = assess(frozen, state, self._policies, reservations, floor, now=now)
        if assessment.within_limits and len(reservations) >= MAX_RESERVATIONS:
            _unavailable()
        receipt = {
            "schema_version": 1,
            "request_id": request_id,
            "account_id": self._binding.account_id,
            "environment": self._binding.environment,
            "currency": self._binding.currency,
            "session_id": self._session_id,
            "request_sha256": digest,
            "policy_book_sha256": self._book_hash,
            "assessment": assessment,
            "lease_authority_epoch": grant.authority_epoch,
            "lease_owner_id": grant.owner_id,
            "lease_generation": grant.generation,
            "lease_resource": grant.binding.resource,
            "observed_at": stamp(now),
            "dispatch_authorized": False,
        }
        receipt_body = encode_receipt(receipt)  # All bounds checked before any audit/budget write.
        reservation = assessment.reservation
        if reservation is not None and (
            reservation.request_id != request_id or reservation.session_id != self._session_id
        ):
            _unavailable()
        reservation_body = encode_reservation(reservation) if reservation is not None else None
        count = assessment.validated_external_count
        if count is not None and (type(count) is not int or not floor <= count <= MAX_COUNT):
            _unavailable()
        self._book(cur, self._book_hash, create=self._book_body)
        cur.execute(
            f"""INSERT INTO {_JOURNAL}
            (account_key,request_id,session_id,policy_book_sha256,request,request_sha256,
             receipt,receipt_sha256,reserved) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                self._key,
                request_id,
                self._session_id,
                self._book_hash,
                body,
                digest,
                receipt_body,
                _hash(receipt_body),
                assessment.within_limits,
            ),
        )
        if reservation_body is not None:
            cur.execute(
                f"INSERT INTO {_RESERVATIONS} VALUES(%s,%s,%s,%s,%s)",
                (
                    self._key,
                    request_id,
                    self._session_id,
                    reservation_body,
                    _hash(reservation_body),
                ),
            )
        if count is not None:
            cur.execute(
                f"SELECT 1 FROM {_FLOORS} WHERE account_key=%s AND session_id=%s",
                (self._key, self._session_id),
            )
            if cur.fetchone() is None:
                cur.execute(
                    f"INSERT INTO {_FLOORS} VALUES(%s,%s,%s)",
                    (self._key, self._session_id, count),
                )
            elif count > floor:
                cur.execute(
                    f"UPDATE {_FLOORS} SET external_count=%s "
                    "WHERE account_key=%s AND session_id=%s",
                    (count, self._key, self._session_id),
                )
        if assessment.within_limits:
            final = assess(frozen, state, self._policies, reservations, floor, now=self._now(cur))
            if not final.within_limits:
                raise LeaseRefused("admission_facts_expired")
        return decode_receipt(receipt_body)

    def preview_in_transaction(self, cur, request, *, recheck_request_id=None):
        """Fresh assessment; no reservation, journal or count-floor updates.

        Same trusted outer transaction/lock contract as admit_in_transaction.
        A returned Assessment is not a persisted receipt or dispatch authority.
        A recheck excludes only an exactly verified accepted original projection.
        """
        frozen = self.freeze_request(request)
        if recheck_request_id is not None:
            _id(recheck_request_id)
        try:
            state = self._sessions.read_in_transaction(cur)
        except (SessionRefused, SessionUnavailable):
            _unavailable()
        if state.spec.binding != self._binding or state.spec.session_id != self._session_id:
            _unavailable()
        _lock(cur, self._resource)
        self._pin(cur, create=True)
        reservations, floor = self._reservations(cur), self._floor(cur)
        if recheck_request_id is not None:
            retained = self._journal(cur, recheck_request_id)
            if retained is None:
                _unavailable()
            row, receipt = retained
            original = decode_request(row[2])
            reserved = receipt["assessment"].reservation
            if (
                row[0] != self._session_id
                or row[1] != self._book_hash
                or not receipt["assessment"].within_limits
                or reserved is None
                or frozen.order["client_order_id"] != recheck_request_id
                or encode_request(replace(original, order=frozen.order)) != row[2]
                or frozen.facts.proposal.instrument != original.facts.proposal.instrument
                or str(frozen.facts.proposal.base_units_per_size)
                != str(original.facts.proposal.base_units_per_size)
            ):
                _unavailable()
            matches = tuple(r for r in reservations if r.request_id == recheck_request_id)
            if len(matches) != 1 or encode_reservation(matches[0]) != encode_reservation(reserved):
                _unavailable()
            reservations = tuple(r for r in reservations if r.request_id != recheck_request_id)
        assessment = assess(frozen, state, self._policies, reservations, floor, now=self._now(cur))
        if recheck_request_id is not None and assessment.within_limits:
            candidate = assessment.reservation
            if candidate is None or any(
                getattr(candidate, field) < getattr(reserved, field)
                for field in ("gross_budget", "loss_budget", "margin_budget")
            ):
                # No replacement/release API: cheaper refreshed evidence cannot
                # silently discount immutable resources already reserved.
                return replace(
                    assessment, within_limits=False, code="recheck_budget_shrink", reservation=None
                )
        return assessment

    def lookup(self, request_id):
        _id(request_id)

        def operation(cur):
            if not self._pin(cur):
                return None
            result = self._journal(cur, request_id)
            return None if result is None else result[1]

        return _run(self._connect, self._resource, operation)

    def snapshot(self):
        def operation(cur):
            if not self._pin(cur):
                return (), 0
            return self._reservations(cur), self._floor(cur)

        return _run(self._connect, self._resource, operation)
