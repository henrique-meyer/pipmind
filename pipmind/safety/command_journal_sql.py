"""Immutable audit-only command obligations, never a sender or dispatch permit.

Same-cursor mutation requires preheld lease/control/recovery locks and verifies
their current pinned identity/lease/assignment/control facts. The independent ACTIVE
authority guard and final outer callback/commit are trusted caller obligations.
Source completeness, risk admission and venue/final-dispatch checks are NOT
established here. Historical reads require no current ownership or activation.
"""

from datetime import UTC, datetime

from pipmind.contracts import stamp
from pipmind.safety.azure_identity_sql import AzureDatabaseIdentity
from pipmind.safety.command_journal import (
    MAX_RECORD_BYTES,
    AttemptIntent,
    CommandIntent,
    JournalRefused,
    JournalScope,
    UnknownOutcome,
    decode_attempt,
    decode_command,
    decode_order,
    decode_unknown,
    encode_attempt,
    encode_command,
    encode_order,
    encode_scope,
    encode_unknown,
    fingerprint,
    project,
    validate_attempt,
    validate_unknown,
)
from pipmind.safety.controls_sql import SqlControlStore
from pipmind.safety.identity_profiles import (
    IdentityProfileRefused,
    IdentityProfileUnavailable,
    read_expected_identity,
)
from pipmind.safety.incarnation import (
    IncarnationRefused,
    IncarnationUnavailable,
    read_incarnation,
)
from pipmind.safety.lease_sql import LeaseRefused, SqlLeaseStore
from pipmind.safety.recovery_sql import Assignment, SqlRecoveryStore
from pipmind.safety.session import account_key

CommandJournalRefused = JournalRefused
PAGE_ROWS = 64
_ACCOUNT = "dbo.pm_safety_command_accounts_v1"
_COMMAND = "dbo.pm_safety_command_intents_v1"
_ATTEMPT = "dbo.pm_safety_command_attempts_v1"
_UNKNOWN = "dbo.pm_safety_command_unknown_v1"
_BIN = "COLLATE Latin1_General_100_BIN2"
_DDL = (
    f"""IF OBJECT_ID('{_ACCOUNT}','U') IS NULL CREATE TABLE {_ACCOUNT} (
    account_key char(64) {_BIN} NOT NULL PRIMARY KEY,
    account_id nvarchar(128) {_BIN} NOT NULL,environment varchar(4) {_BIN} NOT NULL,
    currency char(3) {_BIN} NOT NULL)""",
    *(
        f"""IF OBJECT_ID('{table}','U') IS NULL CREATE TABLE {table} (
    account_key char(64) {_BIN} NOT NULL,command_id nvarchar(128) {_BIN} NOT NULL,
    body nvarchar(max) NOT NULL,sha256 char(64) {_BIN} NOT NULL,
    CONSTRAINT PK_{table.removeprefix("dbo.")} PRIMARY KEY(account_key,command_id),
    CONSTRAINT FK_{table.removeprefix("dbo.")} FOREIGN KEY({foreign_columns})
      REFERENCES {parent}({foreign_columns}))"""
        for table, parent, foreign_columns in (
            (_COMMAND, _ACCOUNT, "account_key"),
            (_ATTEMPT, _COMMAND, "account_key,command_id"),
            (_UNKNOWN, _ATTEMPT, "account_key,command_id"),
        )
    ),
)


class CommandJournalUnavailable(Exception):
    """Fixed unverifiable/corrupt SQL failure; never supplies usable defaults."""


class CommandJournalCommitUnknown(Exception):
    """Direct historical transaction/DDL commit acknowledgement is uncertain."""


def _need(ok, code):
    if not ok:
        raise CommandJournalRefused(code)


def _unavailable():
    raise CommandJournalUnavailable("command_journal_unavailable")


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
    _need(good, "invalid_command_journal_id")


def _lock(cur, resource):
    cur.execute(
        "DECLARE @r int; EXEC @r=sp_getapplock @Resource=%s,"
        "@LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=5000; SELECT @r",
        (resource,),
    )
    while cur.description is None:
        if not cur.nextset():
            _unavailable()
    row = cur.fetchone()
    _need(row is not None and type(row[0]) is int and row[0] >= 0, "command_journal_busy")


def _transaction(cur):
    _need(
        getattr(getattr(cur, "connection", None), "autocommit", None) is False,
        "command_journal_transaction_required",
    )
    cur.execute("SELECT @@TRANCOUNT,XACT_STATE()")
    row = cur.fetchone()
    _need(
        row is not None
        and type(row[0]) is int
        and row[0] >= 1
        and type(row[1]) is int
        and row[1] == 1,
        "command_journal_transaction_required",
    )


def _close(db):
    if db is not None:
        try:
            db.close()  # Fresh nonpooled disposal; no blocking pytds rollback handshake.
        except BaseException:
            pass


def _run(connect, resource, operation):
    db, committing = None, False
    try:
        db = connect()
        _need(getattr(db, "autocommit", None) is False, "command_journal_transaction_required")
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
            raise CommandJournalCommitUnknown("command_journal_commit_unknown") from None
        if not isinstance(error, Exception) or isinstance(
            error, (CommandJournalRefused, CommandJournalUnavailable)
        ):
            raise
        raise CommandJournalUnavailable("command_journal_unavailable") from None
    finally:
        _close(db)


def bootstrap(connect):
    """Explicit isolated DDL only; never creates ownership, assignments or a worker."""
    _need(callable(connect), "invalid_command_journal_connector")

    def create(cur):
        for sql in _DDL:
            cur.execute(sql)

    _run(connect, "pipmind-safety-command-journal-schema-v1", create)


class SqlCommandJournal:
    """Full immutable audit; caller must separately fence the ACTIVE authority.

    No same-cursor method opens/commits a connection. Its values are provisional
    until caller acknowledgement and always dispatch_authorized=False. Direct
    lookup/iteration are historical, not an ownership or current-safety check.
    """

    def __init__(self, connect, leases, recovery, controls):
        _need(callable(connect), "invalid_command_journal_connector")
        _need(
            type(leases) is SqlLeaseStore
            and type(recovery) is SqlRecoveryStore
            and type(controls) is SqlControlStore,
            "invalid_command_journal_stores",
        )
        _need(leases.incarnation is not None, "command_journal_native_pin_required")
        _need(
            recovery.binding == controls.binding
            and (leases.binding.account_id, leases.binding.environment)
            == (recovery.binding.account_id, recovery.binding.environment),
            "command_journal_binding_mismatch",
        )
        self._connect, self.leases, self.recovery, self.controls = (
            connect,
            leases,
            recovery,
            controls,
        )
        self.binding = recovery.binding
        self._key = account_key(self.binding)
        self._resource = "pipmind-safety-command-journal-v1:" + self._key
        self._prior_locks = (
            "pipmind-safety-lease-v1:" + leases._key,
            "pipmind-safety-controls-v1:" + self._key,
            "pipmind-safety-recovery-v1:" + self._key,
        )

    def _pin(self, cur, *, create=False):
        cur.execute(
            f"SELECT account_id,environment,currency FROM {_ACCOUNT} WITH(UPDLOCK,HOLDLOCK) "
            "WHERE account_key=%s",
            (self._key,),
        )
        row = cur.fetchone()
        expected = (self.binding.account_id, self.binding.environment, self.binding.currency)
        if row is not None:
            if tuple(row) != expected:
                _unavailable()
            return True
        cur.execute(
            "SELECT CASE WHEN "
            + " OR ".join(
                f"EXISTS(SELECT 1 FROM {table} WHERE account_key=%s)"
                for table in (_COMMAND, _ATTEMPT, _UNKNOWN)
            )
            + " THEN 1 ELSE 0 END",
            (self._key,) * 3,
        )
        if cur.fetchone()[0]:
            _unavailable()
        if create:
            cur.execute(f"INSERT INTO {_ACCOUNT} VALUES(%s,%s,%s,%s)", (self._key, *expected))
        return create

    def _scope(self, cur, grant, assignment, *, attempt=False):
        self.leases._validate_grant(grant)
        _transaction(cur)
        for resource in self._prior_locks:
            cur.execute("SELECT APPLOCK_MODE('public',%s,'Transaction')", (resource,))
            _need(cur.fetchone() == ("Exclusive",), "command_journal_lock_required")
        try:
            # Keep the native reader alias and its original failure behavior.
            # Azure is opt-in only through the lease's explicit physical pin.
            native = (
                read_expected_identity(cur, self.leases.incarnation)
                if type(self.leases.incarnation) is AzureDatabaseIdentity
                else read_incarnation(cur)
            )
        except IdentityProfileRefused:
            raise CommandJournalRefused("command_journal_incarnation_mismatch") from None
        except (IncarnationRefused, IncarnationUnavailable, IdentityProfileUnavailable):
            raise CommandJournalUnavailable("command_journal_incarnation_unavailable") from None
        _need(native == self.leases.incarnation, "command_journal_incarnation_mismatch")
        row = self.leases._checked(cur, grant)
        control = self.controls.read_in_transaction(cur)
        state = self.recovery.read_in_transaction(cur)
        _need(
            type(assignment) is Assignment
            and assignment.binding == self.binding
            and state.assignment == assignment
            and assignment.epoch == grant.authority_epoch,
            "command_journal_assignment_mismatch",
        )
        if attempt:
            _need(not control.killed and not control.paused, "command_journal_controls_held")
        return JournalScope(
            assignment,
            grant.owner_id,
            grant.generation,
            grant.binding.resource,
            stamp(row[7].replace(tzinfo=UTC)),
            native,
            control.revision,
        )

    @staticmethod
    def _now(cur):
        cur.execute("SELECT SYSUTCDATETIME()")
        row = cur.fetchone()
        if row is None or type(row[0]) is not datetime or row[0].tzinfo is not None:
            _unavailable()
        return stamp(row[0].replace(tzinfo=UTC))

    def _row(self, cur, table, command_id, decoder, encoder):
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,sha256 FROM {table} "
            "WITH(UPDLOCK,HOLDLOCK) WHERE account_key=%s AND command_id=%s",
            (2 * MAX_RECORD_BYTES, self._key, command_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            if type(row[0]) is not str or len(row[0].encode()) > MAX_RECORD_BYTES:
                _unavailable()
            if fingerprint(row[0]) != row[1]:
                _unavailable()
            record = decoder(row[0])
            if (
                encoder(record) != row[0]
                or record.command_id != command_id
                or record.scope.assignment.binding != self.binding
            ):
                _unavailable()
            return record
        except Exception:
            raise CommandJournalUnavailable("command_journal_unavailable") from None

    def _read(self, cur, command_id):
        command = self._row(cur, _COMMAND, command_id, decode_command, encode_command)
        attempt = self._row(cur, _ATTEMPT, command_id, decode_attempt, encode_attempt)
        unknown = self._row(cur, _UNKNOWN, command_id, decode_unknown, encode_unknown)
        if command is None:
            if attempt is not None or unknown is not None:
                _unavailable()
            return None
        try:
            return project(command, attempt, unknown)
        except Exception:
            raise CommandJournalUnavailable("command_journal_unavailable") from None

    def _save(self, cur, table, record, encoder):
        raw = encoder(record)
        cur.execute(
            f"INSERT INTO {table} VALUES(%s,%s,%s,%s)",
            (self._key, record.command_id, raw, fingerprint(raw)),
        )

    def _mutation(self, cur, grant, assignment, operation, *, attempt=False):
        try:
            before = self._scope(cur, grant, assignment, attempt=attempt)
            _lock(cur, self._resource)
            result = operation(before)
            after = self._scope(cur, grant, assignment, attempt=attempt)
            _need(encode_scope(before) == encode_scope(after), "command_journal_scope_changed")
            return result
        except (CommandJournalRefused, CommandJournalUnavailable, LeaseRefused):
            raise
        except Exception:
            raise CommandJournalUnavailable("command_journal_unavailable") from None

    def record_in_transaction(
        self,
        cur,
        grant,
        current_assignment,
        order,
        *,
        causation_id,
        local_outcome="recorded",
        code="locally_recorded",
    ):
        raw_order = encode_order(order)
        command_id = decode_order(raw_order)["client_order_id"]
        _id(command_id)
        _id(causation_id)
        _need(
            (local_outcome == "recorded" and code == "locally_recorded")
            or (
                local_outcome == "refused" and code in ("locally_refused", "unsupported_operation")
            ),
            "invalid_command_journal_outcome",
        )

        def record(scope):
            self._pin(cur, create=True)
            old = self._read(cur, command_id)
            if old is not None:
                prior = old.command
                _need(
                    (prior.order_body, prior.causation_id, prior.local_outcome, prior.code)
                    == (raw_order, causation_id, local_outcome, code),
                    "conflicting_command_intent",
                )
                return prior
            intent = CommandIntent(
                command_id, raw_order, scope, causation_id, local_outcome, code, self._now(cur)
            )
            self._save(cur, _COMMAND, intent, encode_command)
            return intent

        return self._mutation(cur, grant, current_assignment, record)

    def attempt_in_transaction(self, cur, grant, current_assignment, command_id, attempt_id):
        _id(command_id)
        _id(attempt_id)

        def mark(scope):
            self._pin(cur)
            old = self._read(cur, command_id)
            _need(old is not None, "command_intent_missing")
            if old.attempt is not None:
                _need(old.attempt.attempt_id == attempt_id, "command_attempt_already_recorded")
                return old.attempt
            value = AttemptIntent(
                command_id,
                fingerprint(encode_command(old.command)),
                attempt_id,
                scope,
                self._now(cur),
            )
            validate_attempt(old.command, value)
            self._save(cur, _ATTEMPT, value, encode_attempt)
            return value

        return self._mutation(cur, grant, current_assignment, mark, attempt=True)

    def unknown_in_transaction(
        self, cur, grant, current_assignment, command_id, *, code="outcome_unresolved"
    ):
        _id(command_id)
        _need(code == "outcome_unresolved", "invalid_command_unknown_code")

        def observe(scope):
            self._pin(cur)
            old = self._read(cur, command_id)
            _need(old is not None and old.attempt is not None, "command_attempt_missing")
            if old.unknown is not None:
                return old.unknown
            value = UnknownOutcome(
                command_id,
                fingerprint(encode_command(old.command)),
                fingerprint(encode_attempt(old.attempt)),
                scope,
                code,
                self._now(cur),
            )
            validate_unknown(old.command, old.attempt, value)
            self._save(cur, _UNKNOWN, value, encode_unknown)
            return value

        return self._mutation(cur, grant, current_assignment, observe)

    def lookup_in_transaction(self, cur, command_id):
        """Historical read only: no external/native/lease/assignment attestation."""
        _id(command_id)
        try:
            _transaction(cur)
            _lock(cur, self._resource)
            self._pin(cur)
            return self._read(cur, command_id)
        except (CommandJournalRefused, CommandJournalUnavailable):
            raise
        except Exception:
            raise CommandJournalUnavailable("command_journal_unavailable") from None

    def lookup(self, command_id):
        _id(command_id)
        return _run(
            self._connect, self._resource, lambda cur: self.lookup_in_transaction(cur, command_id)
        )

    def iter_history(self):
        """Complete keyset stream with bounded IDs and one bounded projection at a time.

        No total history cap/truncation or completeness certificate. Consumers
        stopping early must close this generator; disposal releases the read lock.
        """
        db, committing = None, False
        try:
            db = self._connect()
            _need(getattr(db, "autocommit", None) is False, "command_journal_transaction_required")
            cur = db.cursor()
            cur.execute(
                "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
            )
            _lock(cur, self._resource)
            self._pin(cur)
            last = None
            while True:
                suffix, args = (
                    ("", (self._key,))
                    if last is None
                    else (" AND command_id>%s", (self._key, last))
                )
                cur.execute(
                    f"SELECT TOP ({PAGE_ROWS}) command_id FROM {_COMMAND} WITH(HOLDLOCK) "
                    "WHERE account_key=%s" + suffix + " ORDER BY command_id",
                    args,
                )
                rows = cur.fetchmany(PAGE_ROWS)
                if len(rows) > PAGE_ROWS or cur.fetchmany(PAGE_ROWS):
                    _unavailable()
                if not rows:
                    break
                for row in rows:
                    _id(row[0])
                    if row[0] == last:
                        _unavailable()
                    value = self._read(cur, row[0])
                    if value is None:
                        _unavailable()
                    yield value
                    last = row[0]
                if len(rows) < PAGE_ROWS:
                    break
            committing = True
            db.commit()
        except BaseException as error:
            if committing:
                raise CommandJournalCommitUnknown("command_journal_commit_unknown") from None
            if not isinstance(error, Exception) or isinstance(
                error, (CommandJournalRefused, CommandJournalUnavailable)
            ):
                raise
            raise CommandJournalUnavailable("command_journal_unavailable") from None
        finally:
            _close(db)
