"""Opt-in disposable-SQL safety subprofile, with no CLI or runtime wiring.

The caller supplies an explicitly confirmed disposable connector/database and
unique synthetic namespace. This module never loads environment/credentials,
selects a hosted database, contacts a model/venue, sends orders or changes the M1
capability registry. SQL execution and retained evidence are distinct from the
pure grader's consistency checks and from operational qualification.
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from pipmind.contracts import loads, stamp
from pipmind.gateway.config import SymbolMap
from pipmind.safety import admission_sql, controls_sql, integration_sql, lease_sql, session_sql
from pipmind.safety.admission import (
    MAX_RECEIPT_BYTES as ADMISSION_RECEIPT_BYTES,
)
from pipmind.safety.admission import (
    MAX_REQUEST_BYTES,
    MAX_RESERVATION_BYTES,
    decode_policy_book,
    decode_request,
    decode_reservation,
    encode_receipt,
    encode_request,
    encode_reservation,
)
from pipmind.safety.checkpoint_sql import capture_in_transaction, required_locks
from pipmind.safety.controls_sql import SqlControlStore
from pipmind.safety.eval_evidence import (
    CaseEvidence,
    DurableSnapshot,
    Observation,
    canonical_json,
    fingerprint,
)
from pipmind.safety.eval_scenarios import (
    ScenarioSpec,
    captured_order,
    load_scenarios,
    prepare_scenario,
)
from pipmind.safety.integration_sql import MAX_ENVELOPE_BYTES, SqlSafetyGateway, _lock
from pipmind.safety.lease_sql import LeaseBinding, LeaseRefused, SqlLeaseStore
from pipmind.safety.session import (
    MAX_CODEC_BYTES,
    decode_observation,
    decode_spec,
    encode_state,
)
from pipmind.safety.session import (
    decode_receipt as decode_session_receipt,
)
from pipmind.safety.session import (
    encode_receipt as encode_session_receipt,
)
from pipmind.safety.session_sql import SqlSessionStore

CASE_IDS = ("S02", "S03", "S04", "S05", "S06", "S07")


class SQLProfileRefused(Exception):
    """Fixed invalid disposable scope, recipe or evidence refusal."""


class SQLProfileUnavailable(Exception):
    """SQL or evidence cannot be verified; no passing evidence is fabricated."""


class SQLProfileCommitUnknown(Exception):
    """A runner-owned SQL transaction's commit acknowledgement is unknown."""


@dataclass(frozen=True, slots=True)
class DisposableSQLScope:
    namespace: str
    expected_database: str
    confirmed_disposable: bool = False


def _need(ok, code):
    if not ok:
        raise SQLProfileRefused(code)


def _validate_scope(scope):
    _need(
        type(scope) is DisposableSQLScope and scope.confirmed_disposable is True,
        "disposable_sql_confirmation_required",
    )
    _need(
        type(scope.namespace) is str and re.fullmatch(r"[0-9a-f]{32}", scope.namespace) is not None,
        "invalid_synthetic_namespace",
    )
    try:
        valid = (
            type(scope.expected_database) is str
            and bool(scope.expected_database)
            and scope.expected_database.strip() == scope.expected_database
            and "\x00" not in scope.expected_database
            and len(scope.expected_database.encode("utf-16-le")) <= 256
        )
    except UnicodeError:
        valid = False
    _need(valid, "invalid_disposable_database")


def _dispose(db):
    if db is not None:
        try:
            db.close()  # Fresh nonpooled disposal; never a blocking rollback handshake.
        except BaseException:
            pass


def _guarded_connector(connect, scope):
    _validate_scope(scope)
    _need(callable(connect), "invalid_disposable_connector")

    def guarded():
        db = None
        try:
            db = connect()
            _need(getattr(db, "autocommit", None) is False, "disposable_transaction_required")
            cur = db.cursor()
            cur.execute("SELECT DB_NAME(),CONVERT(int,SERVERPROPERTY('EngineEdition'))")
            row = cur.fetchone()
            _need(
                row is not None
                and row[0] == scope.expected_database
                and type(row[1]) is int
                and row[1] in (1, 2, 3, 4, 8),
                "disposable_database_mismatch",
            )
            return db
        except BaseException:
            _dispose(db)
            raise

    return guarded


@contextmanager
def _transaction(connect):
    db, committing = None, False
    try:
        db = connect()
        cur = db.cursor()
        cur.execute(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        yield cur
        committing = True
        db.commit()
    except BaseException as error:
        if committing:
            raise SQLProfileCommitUnknown("sql_profile_commit_unknown") from None
        if not isinstance(error, Exception) or isinstance(
            error, (SQLProfileRefused, SQLProfileUnavailable)
        ):
            raise
        raise SQLProfileUnavailable("sql_profile_unavailable") from None
    finally:
        _dispose(db)


@dataclass(frozen=True, slots=True)
class _Stores:
    connect: object
    leases: SqlLeaseStore
    sessions: SqlSessionStore
    controls: SqlControlStore
    admission: admission_sql.SqlAdmissionStore
    gateway: SqlSafetyGateway


def _stores(connect, spec, book, mapping, epoch, base_units_per_size):
    sessions = SqlSessionStore(connect, spec.binding, spec.session_id)
    leases = SqlLeaseStore(
        connect, LeaseBinding(spec.binding.account_id, spec.binding.environment, "eval-dry"), epoch
    )
    controls = SqlControlStore(connect, spec.binding)
    admission = admission_sql.SqlAdmissionStore(connect, leases, sessions, book)
    rows = loads(mapping)
    _need(type(rows) is list and len(book) == 1, "invalid_eval_mapping")
    symbols = SymbolMap(rows)
    _need(set(symbols.by_symbol) == {book[0].instrument}, "invalid_eval_mapping")
    factors = {book[0].instrument: base_units_per_size}
    gateway = SqlSafetyGateway(admission, controls, symbols, base_units_per_size=factors)
    return _Stores(connect, leases, sessions, controls, admission, gateway)


def _safe_lease(binding, epoch, generation, owner, expires):
    return canonical_json(
        {
            "account_id": binding.account_id,
            "environment": binding.environment,
            "resource": binding.resource,
            "authority_epoch": epoch,
            "generation": generation,
            "owner_id": owner,
            "expires_at": expires,
        }
    )


def _grant_body(grant):
    return _safe_lease(
        grant.binding,
        grant.authority_epoch,
        grant.generation,
        grant.owner_id,
        stamp(grant.expires_at),
    )


def _snapshot(stores):
    with _transaction(stores.connect) as cur:
        for resource in required_locks(stores.sessions.binding, stores.leases.binding):
            _lock(cur, resource)
        inventory = capture_in_transaction(cur, stores.sessions.binding, stores.leases.binding)
        control = stores.controls.read_in_transaction(cur)
        state = stores.sessions.read_in_transaction(cur)
        row = stores.leases._read(cur)
        _need(row is not None, "evaluation_lease_missing")
        lease = _safe_lease(
            stores.leases.binding,
            row[3],
            row[4],
            row[5],
            None if row[7] is None else stamp(row[7].replace(tzinfo=UTC)),
        )
        controls = canonical_json(
            {
                "account_id": control.binding.account_id,
                "environment": control.binding.environment,
                "currency": control.binding.currency,
                "revision": control.revision,
                "killed": control.killed,
                "paused": control.paused,
            }
        )
        cur.execute(
            "SELECT scope,hold_code FROM dbo.pm_safety_integration_accounts_v1 "
            "WHERE account_key=%s",
            (stores.gateway._key,),
        )
        hold = cur.fetchone()
        hold_body = canonical_json(
            {
                "present": hold is not None,
                "hold_code": None if hold is None else hold[1],
                "scope": None if hold is None or hold[0] is None else loads(hold[0]),
            }
        )
        return DurableSnapshot(inventory.tables, controls, encode_state(state), lease, hold_body)


def _server_time(connect):
    with _transaction(connect) as cur:
        cur.execute("SELECT SYSUTCDATETIME()")
        value = cur.fetchone()[0]
        _need(type(value) is datetime and value.tzinfo is None, "invalid_sql_clock")
        return value.replace(tzinfo=UTC)


def _stored(stores, request, receipt_kind):
    identity = request.order["client_order_id"]
    with _transaction(stores.connect) as cur:
        if receipt_kind == "admission":
            _lock(cur, stores.admission._resource)
            table, body, id_column = "dbo.pm_safety_admission_journal_v1", "request", "request_id"
            params = (
                2 * MAX_REQUEST_BYTES,
                2 * ADMISSION_RECEIPT_BYTES,
                stores.admission._key,
                identity,
            )
            suffix = ""
        else:
            _lock(cur, stores.gateway._resource)
            table, body, id_column = "dbo.pm_safety_integration_journal_v1", "body", "record_id"
            params = (
                2 * MAX_ENVELOPE_BYTES,
                2 * integration_sql.MAX_RECEIPT_BYTES,
                stores.gateway._key,
                identity,
            )
            suffix = " AND channel='accept'"
        cur.execute(
            f"SELECT CASE WHEN DATALENGTH({body})<=%s THEN {body} END,"
            f"{body}_sha256,CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256 "
            f"FROM {table} WHERE account_key=%s AND {id_column}=%s" + suffix,
            params,
        )
        row = cur.fetchone()
        if row is None:
            return None
        _need(
            type(row[0]) is str
            and type(row[2]) is str
            and fingerprint(row[0]) == row[1]
            and fingerprint(row[2]) == row[3],
            "evaluation_receipt_integrity",
        )
        return row[0], row[2]


def _setup_rows(stores, observations):
    result = []
    with _transaction(stores.connect) as cur:
        _lock(cur, "pipmind-safety-session-v1:" + stores.admission._key)
        for observation in observations:
            cur.execute(
                "SELECT CASE WHEN DATALENGTH(receipt)<=%s THEN receipt END,receipt_sha256 "
                "FROM dbo.pm_safety_session_observations_v1 WHERE account_key=%s "
                "AND session_id=%s AND observation_id=%s",
                (
                    2 * MAX_CODEC_BYTES,
                    stores.admission._key,
                    stores.sessions.session_id,
                    observation.observation_id,
                ),
            )
            row = cur.fetchone()
            _need(
                row is not None and type(row[0]) is str and fingerprint(row[0]) == row[1],
                "evaluation_setup_receipt_missing",
            )
            _need(
                encode_session_receipt(decode_session_receipt(row[0])) == row[0],
                "evaluation_setup_receipt_integrity",
            )
            result.append(row[0])
    return tuple(result)


def _reservation(stores, request):
    with _transaction(stores.connect) as cur:
        _lock(cur, stores.admission._resource)
        cur.execute(
            "SELECT CASE WHEN DATALENGTH(body)<=%s THEN body END,sha256 "
            "FROM dbo.pm_safety_admission_reservations_v1 "
            "WHERE account_key=%s AND request_id=%s",
            (2 * MAX_RESERVATION_BYTES, stores.admission._key, request.order["client_order_id"]),
        )
        row = cur.fetchone()
        if row is None:
            return None
        _need(
            type(row[0]) is str and fingerprint(row[0]) == row[1],
            "evaluation_reservation_integrity",
        )
        value = decode_reservation(row[0])
        _need(
            encode_reservation(value) == row[0]
            and value.request_id == request.order["client_order_id"]
            and value.session_id == stores.sessions.session_id,
            "evaluation_reservation_integrity",
        )
        return row[0]


def _one(connect, scope, scenario, variant):
    now = _server_time(connect)
    account_id = "synthetic-eval-" + scope.namespace + "-" + scenario.case_id + "-" + variant
    prepared = prepare_scenario(
        scenario, sql_now=now, account_id=account_id, session_id="eval-session", variant=variant
    )
    spec, book = (
        decode_spec(prepared.session_spec_body),
        decode_policy_book(prepared.policy_book_body),
    )
    request = decode_request(prepared.request_body)
    _need(
        spec.binding.account_id == account_id and request.facts.account_id == account_id,
        "evaluation_account_mismatch",
    )
    epoch = "eval-" + scope.namespace
    stores = _stores(
        connect,
        spec,
        book,
        prepared.mapping_body,
        epoch,
        request.facts.proposal.base_units_per_size,
    )
    stores.sessions.initialize(spec)
    observations = tuple(decode_observation(body) for body in prepared.observation_bodies)
    setup = tuple(
        encode_session_receipt(stores.sessions.observe(value, now=now)) for value in observations
    )
    stores.controls.initialize()
    stores.controls.apply_override(
        "explicit-test-resume",
        "synthetic-evaluator",
        "resume",
        0,
        "Explicit disposable evaluation positive prerequisite",
    )
    stores.leases.initialize()
    grant = stores.leases.acquire("synthetic-eval-owner-A", ttl_seconds=300)
    attempted = grant
    receipt_kind = "admission" if scenario.case_id in ("S02", "S03", "S04", "S05") else "gateway"
    if receipt_kind == "gateway":
        if prepared.replace_lease:
            stores.leases.release(grant)
            grant = stores.leases.acquire("synthetic-eval-owner-B", ttl_seconds=300)
        ready = stores.gateway.reconcile(grant, request)
        _need(ready["outcome"] == "dry_ready", "evaluation_positive_setup_failed")
        if prepared.control_action is not None:
            control = stores.controls.read()
            stores.controls.apply_override(
                "explicit-target-" + prepared.control_action,
                "synthetic-evaluator",
                prepared.control_action,
                control.revision,
                "Explicit disposable target control",
            )
    before = _snapshot(stores)
    returned, exception = None, None
    try:
        if receipt_kind == "admission":
            returned = encode_receipt(stores.admission.admit(attempted, request))
        else:
            returned = canonical_json(stores.gateway.accept(attempted, request))
    except LeaseRefused as error:
        # An actual stale lease is rejected before its callback; no refusal row
        # is invented to make that correct no-write behavior look different.
        exception = str(error)
        _need(
            exception in ("lease_stale_owner", "lease_expired", "lease_authority_mismatch"),
            "unexpected_eval_lease_refusal",
        )
    after = _snapshot(stores)
    stored = _stored(stores, request, receipt_kind)
    reopened = _stores(
        connect,
        spec,
        book,
        prepared.mapping_body,
        epoch,
        request.facts.proposal.base_units_per_size,
    )
    reopened_snapshot = _snapshot(reopened)
    reopened_stored = _stored(reopened, request, receipt_kind)
    reopened_setup = _setup_rows(reopened, observations)
    reservation = _reservation(stores, request)
    reopened_reservation = _reservation(reopened, request)
    admission_receipt = reopened_admission_receipt = None
    if receipt_kind == "gateway" and variant == "positive":
        original_admission = _stored(stores, request, "admission")
        repeated_admission = _stored(reopened, request, "admission")
        _need(
            original_admission is not None
            and repeated_admission is not None
            and original_admission[0] == repeated_admission[0] == encode_request(request),
            "evaluation_admission_receipt_missing",
        )
        admission_receipt, reopened_admission_receipt = (
            original_admission[1],
            repeated_admission[1],
        )
    if receipt_kind == "admission":
        retained = reopened.admission.lookup(request.order["client_order_id"])
        _need((retained is None) == (reopened_stored is None), "evaluation_reopen_mismatch")
        if retained is not None:
            _need(encode_receipt(retained) == reopened_stored[1], "evaluation_reopen_mismatch")
    envelope = canonical_json(
        {
            "request": encode_request(request),
            "session_id": spec.session_id,
            "book": stores.admission.policy_book_sha256,
            "mapping": stores.gateway._mapping_hash,
        }
    )
    request_body = encode_request(request) if receipt_kind == "admission" else envelope
    if stored is not None:
        _need(stored[0] == request_body, "evaluation_stored_request_mismatch")
    label = (
        "positive"
        if variant == "positive"
        else (variant if scenario.case_id == "S06" else "target")
    )
    return Observation(
        label,
        captured_order(encode_request(request)),
        request_body,
        returned,
        None if stored is None else stored[1],
        None if reopened_stored is None else reopened_stored[1],
        exception,
        before,
        after,
        reopened_snapshot,
        receipt_kind,
        setup,
        reopened_setup,
        clock_body=prepared.metadata_body,
        attempted_lease_body=_grant_body(attempted),
        admission_receipt_body=admission_receipt,
        reopened_admission_receipt_body=reopened_admission_receipt,
        reservation_body=reservation,
        reopened_reservation_body=reopened_reservation,
    )


def run_sql_profile(connect, *, scope, scenarios=None):
    """Execute only an explicit complete six-case disposable SQL subprofile.

    The confirmation/database-name guard is a trusted test-invocation boundary,
    not proof of an administrative restore domain. No environment is consulted.
    Full evidence returned here is private; use the grader's bounded summary for
    publication. An exception never becomes a successful evaluation observation.
    """
    guarded = _guarded_connector(connect, scope)
    canonical = tuple(load_scenarios())
    selected = canonical if scenarios is None else tuple(scenarios)
    _need(
        tuple(getattr(row, "case_id", None) for row in selected) == CASE_IDS
        and all(type(row) is ScenarioSpec and row.split == "development" for row in selected),
        "invalid_sql_profile_cases",
    )
    _need(selected == canonical, "sql_profile_lineage_mismatch")
    for module in (session_sql, lease_sql, admission_sql, controls_sql, integration_sql):
        module.bootstrap(guarded)
    results = []
    for scenario in selected:
        variants = (
            ("positive", "pause", "kill") if scenario.case_id == "S06" else ("positive", "target")
        )
        evidence = tuple(_one(guarded, scope, scenario, variant) for variant in variants)
        results.append(
            CaseEvidence(
                scenario.case_id, scenario.source_case_sha256, scenario.supplement_sha256, evidence
            )
        )
    return tuple(results)
