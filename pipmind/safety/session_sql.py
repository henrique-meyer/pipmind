"""Isolated account-locked SQL session accounting; no orders or runtime wiring."""

from pipmind.contracts import utc
from pipmind.safety.session import (
    MAX_CODEC_BYTES,
    SessionCommitUnknown,
    SessionRefused,
    SessionUnavailable,
    account_key,
    advance,
    decode_observation,
    decode_receipt,
    decode_spec,
    decode_state,
    encode_observation,
    encode_receipt,
    encode_spec,
    encode_state,
    fingerprint,
    initial_state,
    validate_binding,
    validate_observation,
    validate_spec,
)

_ACCOUNTS = "dbo.pm_safety_session_accounts_v1"
_SESSIONS = "dbo.pm_safety_session_sessions_v1"
_OBSERVATIONS = "dbo.pm_safety_session_observations_v1"
_COLLATION = "COLLATE Latin1_General_100_BIN2"
_DDL = (
    f"""IF OBJECT_ID('{_ACCOUNTS}','U') IS NULL CREATE TABLE {_ACCOUNTS} (
    account_key char(64) {_COLLATION} NOT NULL PRIMARY KEY,
    account_id nvarchar(128) {_COLLATION} NOT NULL,
    environment varchar(4) {_COLLATION} NOT NULL,
    currency char(3) {_COLLATION} NOT NULL)""",
    f"""IF OBJECT_ID('{_SESSIONS}','U') IS NULL CREATE TABLE {_SESSIONS} (
    account_key char(64) {_COLLATION} NOT NULL REFERENCES {_ACCOUNTS}(account_key),
    session_id nvarchar(128) {_COLLATION} NOT NULL,
    started_at datetime2(6) NOT NULL, ends_at datetime2(6) NOT NULL,
    spec nvarchar(max) NOT NULL, spec_sha256 char(64) NOT NULL,
    state nvarchar(max) NOT NULL, state_sha256 char(64) NOT NULL,
    CONSTRAINT PK_pm_safety_session_sessions_v1 PRIMARY KEY(account_key,session_id),
    CONSTRAINT CK_pm_safety_session_interval_v1 CHECK(started_at<ends_at))""",
    f"""IF OBJECT_ID('{_OBSERVATIONS}','U') IS NULL CREATE TABLE {_OBSERVATIONS} (
    account_key char(64) {_COLLATION} NOT NULL,
    session_id nvarchar(128) {_COLLATION} NOT NULL,
    observation_id nvarchar(128) {_COLLATION} NOT NULL, as_of datetime2(6) NOT NULL,
    body nvarchar(max) NOT NULL, body_sha256 char(64) NOT NULL,
    receipt nvarchar(max) NOT NULL, receipt_sha256 char(64) NOT NULL,
    CONSTRAINT PK_pm_safety_session_observations_v1
      PRIMARY KEY(account_key,session_id,observation_id),
    CONSTRAINT UQ_pm_safety_session_time_v1 UNIQUE(account_key,session_id,as_of),
    CONSTRAINT FK_pm_safety_session_observations_v1 FOREIGN KEY(account_key,session_id)
      REFERENCES {_SESSIONS}(account_key,session_id))""",
)


def _need(condition, code):
    if not condition:
        raise SessionRefused(code)


def _dispose(db):
    if db is not None:
        try:
            db.close()
        except BaseException:
            pass  # Cleanup cannot mask acknowledged commit, refusal or uncertainty.


def _lock(cur, resource):
    cur.execute(
        """DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,
        @LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=5000; SELECT @r""",
        (resource,),
    )
    while cur.description is None:
        if not cur.nextset():
            raise SessionUnavailable("session_store_unavailable")
    row = cur.fetchone()
    _need(row is not None and row[0] >= 0, "session_store_busy")


def _run(connect, resource, operation):
    db, committing = None, False
    try:
        db = connect()
        _need(getattr(db, "autocommit", None) is False, "session_transaction_required")
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
            raise SessionCommitUnknown("session_commit_unknown") from None
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, (SessionRefused, SessionUnavailable)):
            raise
        raise SessionUnavailable("session_store_unavailable") from None
    finally:
        # Pinned pytds rollback removes its transport timeout. Non-pooled close
        # aborts uncommitted work without that potentially unbounded handshake.
        _dispose(db)


def bootstrap(connect):
    """Explicit, serialized DDL; never called by construction or reads."""

    def operation(cur):
        for statement in _DDL:
            cur.execute(statement)

    _run(connect, "pipmind-safety-session-schema-v1", operation)


def _decode(raw, sha256, decoder, encoder):
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > MAX_CODEC_BYTES:
            raise ValueError
        if fingerprint(raw) != sha256:
            raise ValueError
        value = decoder(raw)
        if encoder(value) != raw:
            raise ValueError
        return value
    except (SessionRefused, ValueError, TypeError, OverflowError, RecursionError):
        raise SessionUnavailable("session_store_unavailable") from None


def _sql_time(value):
    return utc(value).replace(tzinfo=None)


class SqlSessionStore:
    """Account/environment-locked state; currency is pinned, not a lock namespace.

    The connector must return fresh non-pooled autocommit=False connections.
    Same-transaction callers lock lease -> session(account) -> admission(account).
    Session operations never acquire lease/admission locks or call a provider.
    """

    def __init__(self, connect, binding, session_id):
        validate_binding(binding)
        _need(callable(connect), "session_invalid_connector")
        # Use the same public ID domain without manufacturing a SessionSpec.
        try:
            valid_id = (
                type(session_id) is str
                and bool(session_id)
                and session_id.strip() == session_id
                and "\x00" not in session_id
                and len(session_id.encode("utf-16-le")) <= 256
            )
        except UnicodeError:
            valid_id = False
        _need(valid_id, "session_invalid_id")
        self.connect, self.binding, self.session_id = connect, binding, session_id
        self._key = account_key(binding)
        self._resource = "pipmind-safety-session-v1:" + self._key

    def _run(self, operation):
        return _run(self.connect, self._resource, operation)

    def _account(self, cur, *, create=False):
        cur.execute(
            f"""SELECT account_id,environment,currency FROM {_ACCOUNTS}
            WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s""",
            (self._key,),
        )
        row = cur.fetchone()
        expected = (self.binding.account_id, self.binding.environment, self.binding.currency)
        if row is None:
            _need(create, "session_not_initialized")
            cur.execute(f"INSERT INTO {_ACCOUNTS} VALUES(%s,%s,%s,%s)", (self._key, *expected))
        else:
            _need(tuple(row) == expected, "session_binding_mismatch")

    def _read(self, cur, *, absent_ok=False):
        cur.execute(
            f"""SELECT started_at,ends_at,
            CASE WHEN DATALENGTH(spec)<=%s THEN spec END,spec_sha256,
            CASE WHEN DATALENGTH(state)<=%s THEN state END,state_sha256
            FROM {_SESSIONS} WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s AND session_id=%s""",
            (2 * MAX_CODEC_BYTES, 2 * MAX_CODEC_BYTES, self._key, self.session_id),
        )
        row = cur.fetchone()
        if row is None:
            _need(absent_ok, "session_not_initialized")
            return None
        spec = _decode(row[2], row[3], decode_spec, encode_spec)
        state = _decode(row[4], row[5], decode_state, encode_state)
        if (
            spec.binding != self.binding
            or spec.session_id != self.session_id
            or encode_spec(state.spec) != row[2]
            or row[0] != _sql_time(spec.started_at)
            or row[1] != _sql_time(spec.ends_at)
        ):
            raise SessionUnavailable("session_store_unavailable")
        return state

    def initialize(self, spec):
        validate_spec(spec)
        _need(
            spec.binding == self.binding and spec.session_id == self.session_id,
            "session_binding_mismatch",
        )
        body = encode_spec(spec)

        def operation(cur):
            self._account(cur, create=True)
            state = self._read(cur, absent_ok=True)
            if state is not None:
                _need(encode_spec(state.spec) == body, "conflicting_session_spec")
                return state
            cur.execute(
                f"""SELECT TOP(1) 1 FROM {_SESSIONS} WITH(UPDLOCK,HOLDLOCK)
                WHERE account_key=%s AND started_at<%s AND ends_at>%s""",
                (self._key, _sql_time(spec.ends_at), _sql_time(spec.started_at)),
            )
            _need(cur.fetchone() is None, "overlapping_safety_session")
            state = initial_state(spec)
            state_body = encode_state(state)
            cur.execute(
                f"""INSERT INTO {_SESSIONS}
                (account_key,session_id,started_at,ends_at,spec,spec_sha256,state,state_sha256)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    self._key,
                    self.session_id,
                    _sql_time(spec.started_at),
                    _sql_time(spec.ends_at),
                    body,
                    fingerprint(body),
                    state_body,
                    fingerprint(state_body),
                ),
            )
            return state

        return self._run(operation)

    def observe(self, observation, *, now):
        body = encode_observation(observation)
        sha256 = fingerprint(body)
        _need(
            observation.binding == self.binding and observation.session_id == self.session_id,
            "session_binding_mismatch",
        )

        def operation(cur):
            self._account(cur)
            state = self._read(cur)
            validate_observation(state.spec, observation, now=now)
            cur.execute(
                f"""SELECT as_of,CASE WHEN DATALENGTH(body)<=%s THEN body END,body_sha256,
                CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256
                FROM {_OBSERVATIONS} WITH(UPDLOCK,HOLDLOCK)
                WHERE account_key=%s AND session_id=%s AND observation_id=%s""",
                (
                    2 * MAX_CODEC_BYTES,
                    2 * MAX_CODEC_BYTES,
                    self._key,
                    self.session_id,
                    observation.observation_id,
                ),
            )
            retained = cur.fetchone()
            if retained is not None:
                old = _decode(retained[1], retained[2], decode_observation, encode_observation)
                if retained[0] != _sql_time(old.as_of):
                    raise SessionUnavailable("session_store_unavailable")
                _need(
                    retained[1] == body and retained[2] == sha256, "conflicting_session_observation"
                )
                receipt = _decode(retained[3], retained[4], decode_receipt, encode_receipt)
                if (
                    old != observation
                    or receipt.observation_id != observation.observation_id
                    or receipt.payload_sha256 != sha256
                    or receipt.revision > state.revision
                ):
                    raise SessionUnavailable("session_store_unavailable")
                return receipt
            cur.execute(
                f"""SELECT TOP(1) 1 FROM {_OBSERVATIONS} WITH(UPDLOCK,HOLDLOCK)
                WHERE account_key=%s AND session_id=%s AND as_of=%s""",
                (self._key, self.session_id, _sql_time(observation.as_of)),
            )
            _need(cur.fetchone() is None, "conflicting_session_time")
            advanced, receipt = advance(state, observation, now=now)
            state_body, receipt_body = encode_state(advanced), encode_receipt(receipt)
            cur.execute(
                f"""INSERT INTO {_OBSERVATIONS}
                (account_key,session_id,observation_id,as_of,body,body_sha256,receipt,receipt_sha256)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    self._key,
                    self.session_id,
                    observation.observation_id,
                    _sql_time(observation.as_of),
                    body,
                    sha256,
                    receipt_body,
                    fingerprint(receipt_body),
                ),
            )
            cur.execute(
                f"UPDATE {_SESSIONS} SET state=%s,state_sha256=%s "
                "WHERE account_key=%s AND session_id=%s",
                (state_body, fingerprint(state_body), self._key, self.session_id),
            )
            return receipt

        return self._run(operation)

    def read(self):
        """Bounded state read without implicit freshness/all-clear assertions."""

        def operation(cur):
            self._account(cur)
            return self._read(cur)

        return self._run(operation)

    def read_in_transaction(self, cursor):
        """Acquire account lock in caller's transaction; no connect/commit/freshness."""
        try:
            connection = getattr(cursor, "connection", None)
            _need(
                getattr(connection, "autocommit", None) is not True, "session_transaction_required"
            )
            cursor.execute("SELECT @@TRANCOUNT")
            row = cursor.fetchone()
            _need(row is not None and row[0] >= 1, "session_transaction_required")
            _lock(cursor, self._resource)
            self._account(cursor)
            return self._read(cursor)
        except (SessionRefused, SessionUnavailable):
            raise
        except Exception:
            raise SessionUnavailable("session_store_unavailable") from None
