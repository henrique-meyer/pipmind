"""Actual assess/lease/session composition over synthetic transactional SQL doubles."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from hashlib import sha256

import pytest

from pipmind.contracts import stamp
from pipmind.reconciliation import Binding, Freshness, SnapshotFacts
from pipmind.safety import Conversion, ProposalFacts, RiskFacts, RiskPolicy
from pipmind.safety.admission import (
    AdmissionRefused,
    AdmissionRequest,
    ReconciliationEvidence,
    ReservationMark,
    checkpoint_digest,
    encode_policy_book,
    encode_receipt,
    encode_reservation,
    policy_book_digest,
)
from pipmind.safety.admission_sql import SqlAdmissionStore, bootstrap
from pipmind.safety.lease_sql import (
    LeaseBinding,
    LeaseCommitUnknown,
    LeaseGrant,
    LeaseRefused,
    LeaseUnavailable,
    SqlLeaseStore,
)
from pipmind.safety.session import (
    EquityObservation,
    SessionBinding,
    SessionSpec,
    advance,
    encode_spec,
    encode_state,
    fingerprint,
    initial_state,
)
from pipmind.safety.session_sql import SqlSessionStore

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def make_case(
    *,
    now=NOW,
    request_id="request-A",
    account="synthetic-account",
    session_id="session-A",
    caps=None,
    margin=D("40"),
    marks=(),
    count=0,
):
    when, start, end = (
        stamp(now),
        stamp(now - timedelta(hours=12)),
        stamp(now + timedelta(hours=12)),
    )
    p = RiskPolicy(
        "synthetic-policy",
        account,
        "demo",
        "USD",
        "EUR/USD",
        D("2000"),
        D("5000"),
        D("10000"),
        D("50"),
        D("100"),
        D("100"),
        D("200"),
        D("0"),
        10,
        60,
        30,
        30,
    )
    book = (replace(p, **(caps or {})),)
    account_body = {
        "schema_version": 1,
        "as_of": when,
        "currency": "USD",
        "balance": D("10000"),
        "equity": D("10000"),
        "margin_used": D("1000"),
        "margin_available": D("9000"),
    }
    observed = SnapshotFacts(
        Binding(account, "demo"),
        account_body,
        {"schema_version": 1, "as_of": when, "positions": []},
        (),
        True,
        True,
        (),
        when,
        True,
        (),
        when,
        True,
    )
    evidence = ReconciliationEvidence(
        deepcopy(observed),
        observed,
        Freshness(timedelta(seconds=30), timedelta(seconds=2)),
        (),
        (),
        (),
        (),
        (),
    )
    conversion = Conversion("USD", "USD", D("1"), when)
    quote = ProposalFacts("EUR/USD", D("1.1"), D("1.1002"), when, D("1"), margin, conversion)
    facts = RiskFacts(
        account,
        "demo",
        when,
        True,
        True,
        False,
        False,
        start,
        end,
        D("10000"),
        D("0"),
        count,
        quote,
        (),
    )
    spec = SessionSpec(
        SessionBinding(account, "demo", "USD"),
        session_id,
        book[0].policy_id,
        policy_book_digest(book),
        start,
        end,
        D("10000"),
        "opening-evidence",
        book[0].max_daily_loss,
        60,
    )
    observation = EquityObservation(
        spec.binding,
        session_id,
        "source-observation",
        when,
        when,
        D("10000"),
        D("0"),
        True,
        True,
        start,
        when,
        checkpoint_digest(evidence, facts),
        "flow-evidence",
    )
    state, _ = advance(initial_state(spec), observation, now=now)
    order = {
        "schema_version": 1,
        "client_order_id": request_id,
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": D("1000"),
        "order_type": "market",
        "stop": D("1.09"),
    }
    pending = tuple(
        ReservationMark(key, D("1.1"), D("1.1002"), when, conversion, margin) for key in marks
    )
    request = AdmissionRequest(order, facts, evidence, pending, state.revision, state.latest_sha256)
    return request, state, book


class Database:
    def __init__(self, state):
        self.tables = {
            name: {} for name in ("accounts", "books", "journal", "reservations", "floors")
        }
        self.state = state
        self.now = NOW.replace(tzinfo=None)
        self.connections, self.statements = [], []
        self.commit_failure = self.commit_interrupt = self.close_interrupt = None
        self.fail_on = self.interrupt_on = None
        self.after_stage_seconds = 0
        self.outage = False
        self.session_missing = False
        self.before_lock = None
        self.lease_generation = 1
        self.token = "a" * 64
        self.lease_expires = self.now + timedelta(seconds=30)

    def connect(self):
        if self.outage:
            raise OSError("private transport details")
        value = Connection(self)
        self.connections.append(value)
        return value


class Connection:
    def __init__(self, database):
        self.database = database
        self.tables = deepcopy(database.tables)
        self.autocommit = False
        self.closed = self.committed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        db = self.database
        if db.commit_failure == "before":
            raise OSError("private commit error")
        if db.commit_interrupt and db.commit_interrupt[0] == "before":
            raise db.commit_interrupt[1]("private commit interruption")
        self.database.tables = deepcopy(self.tables)
        self.committed = True
        if db.commit_failure == "after":
            raise OSError("private ack lost")
        if db.commit_interrupt:
            raise db.commit_interrupt[1]("private commit interruption")

    def close(self):
        self.closed = True
        if self.database.close_interrupt:
            raise self.database.close_interrupt("private cleanup interruption")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ("synthetic",)
        self.queue = []

    def execute(self, sql, params=()):
        db, q = self.connection.database, " ".join(sql.split())
        db.statements.append((q, params))
        if db.fail_on and db.fail_on in q:
            raise OSError("private staged failure")
        if db.interrupt_on and db.interrupt_on in q:
            raise KeyboardInterrupt("private staged interrupt")
        self.queue = []
        tables = self.connection.tables
        if q.startswith("SET ") or q.startswith("IF OBJECT_ID"):
            pass
        elif "sp_getapplock" in q:
            if db.before_lock is not None:
                hook, db.before_lock = db.before_lock, None
                hook()
            self.queue = [(0,)]
        elif q == "SELECT @@TRANCOUNT":
            self.queue = [(1,)]
        elif q == "SELECT SYSUTCDATETIME()":
            self.queue = [(db.now,)]
        elif q.startswith("SELECT account_id,environment,resource,authority_epoch"):
            spec = db.state.spec
            self.queue = [
                (
                    spec.binding.account_id,
                    "demo",
                    "admission",
                    "synthetic-epoch",
                    db.lease_generation,
                    "synthetic-owner",
                    sha256(db.token.encode()).hexdigest(),
                    db.lease_expires,
                )
            ]
        elif "FROM dbo.pm_safety_session_accounts_v1" in q:
            b = db.state.spec.binding
            self.queue = [(b.account_id, b.environment, b.currency)]
        elif "FROM dbo.pm_safety_session_sessions_v1" in q:
            if not db.session_missing:
                spec, state = encode_spec(db.state.spec), encode_state(db.state)
                self.queue = [
                    (
                        datetime.fromisoformat(
                            db.state.spec.started_at.replace("Z", "+00:00")
                        ).replace(tzinfo=None),
                        datetime.fromisoformat(
                            db.state.spec.ends_at.replace("Z", "+00:00")
                        ).replace(tzinfo=None),
                        spec,
                        fingerprint(spec),
                        state,
                        fingerprint(state),
                    )
                ]
        elif "SELECT CASE WHEN EXISTS" in q:
            key = params[0]
            orphan = any(
                k[0] == key for name in ("journal", "reservations", "floors") for k in tables[name]
            )
            self.queue = [(int(orphan),)]
        elif "FROM dbo.pm_safety_admission_accounts_v1" in q:
            row = tables["accounts"].get(params[0])
            self.queue = [] if row is None else [row]
        elif q.startswith("INSERT INTO dbo.pm_safety_admission_accounts_v1"):
            key, *row = params
            tables["accounts"][key] = tuple(row)
        elif "FROM dbo.pm_safety_admission_books_v1" in q:
            bound, digest = params
            body = tables["books"].get(digest)
            self.queue = (
                []
                if body is None
                else [(body if len(body.encode("utf-16-le")) <= bound else None,)]
            )
        elif q.startswith("INSERT INTO dbo.pm_safety_admission_books_v1"):
            tables["books"][params[0]] = params[1]
        elif "FROM dbo.pm_safety_admission_journal_v1" in q:
            _, _, key, request_id = params
            row = tables["journal"].get((key, request_id))
            self.queue = [] if row is None else [row]
        elif q.startswith("INSERT INTO dbo.pm_safety_admission_journal_v1"):
            key, request_id, *row = params
            tables["journal"][key, request_id] = tuple(row)
        elif "FROM dbo.pm_safety_admission_reservations_v1" in q:
            if "TOP(" in q:
                key = params[1]
                self.queue = [
                    (rid, row[0], row[1], row[2])
                    for (acct, rid), row in sorted(tables["reservations"].items())
                    if acct == key
                ][:1001]
            else:
                _, key, request_id = params
                row = tables["reservations"].get((key, request_id))
                self.queue = [] if row is None else [row]
        elif q.startswith("INSERT INTO dbo.pm_safety_admission_reservations_v1"):
            key, rid, sid, body, digest = params
            tables["reservations"][key, rid] = (sid, body, digest)
            db.now += timedelta(seconds=db.after_stage_seconds)
        elif "FROM dbo.pm_safety_admission_floors_v1" in q:
            row = tables["floors"].get(tuple(params))
            self.queue = [] if row is None else [(1 if q.startswith("SELECT 1 ") else row,)]
        elif q.startswith("INSERT INTO dbo.pm_safety_admission_floors_v1"):
            key, sid, count = params
            tables["floors"][key, sid] = count
        elif q.startswith("UPDATE dbo.pm_safety_admission_floors_v1"):
            count, key, sid = params
            tables["floors"][key, sid] = count
        else:
            raise AssertionError("unrecognized synthetic SQL")
        return self

    def fetchone(self):
        return self.queue.pop(0) if self.queue else None

    def nextset(self):
        return False


def setup_case(**kwargs):
    request, state, book = make_case(**kwargs)
    database = Database(state)
    sessions = SqlSessionStore(database.connect, state.spec.binding, state.spec.session_id)
    leases = SqlLeaseStore(
        database.connect,
        LeaseBinding(state.spec.binding.account_id, "demo", "admission"),
        "synthetic-epoch",
    )
    grant = LeaseGrant(
        leases.binding,
        "synthetic-epoch",
        "synthetic-owner",
        1,
        database.token,
        database.lease_expires.replace(tzinfo=UTC),
    )
    store = SqlAdmissionStore(database.connect, leases, sessions, book)
    return database, store, grant, request


def test_no_sql_constructor_explicit_bootstrap_and_empty_evidence():
    db, store, _, _ = setup_case()
    assert db.connections == []
    bootstrap(db.connect)
    assert sum(q.startswith("IF OBJECT_ID") for q, _ in db.statements) == 5
    assert store.snapshot() == ((), 0) and store.lookup("missing") is None


def test_positive_real_assessment_exact_journal_lookup_snapshot_and_token_exclusion():
    db, store, grant, request = setup_case()
    before = deepcopy(request)
    receipt = store.admit(grant, request)
    assert receipt["assessment"].within_limits and not receipt["dispatch_authorized"]
    assert request == before
    assert len(db.tables["journal"]) == len(db.tables["reservations"]) == 1
    assert len(db.tables["books"]) == 1
    assert store.lookup(request.order["client_order_id"]) == receipt
    projections, floor = store.snapshot()
    assert projections == (receipt["assessment"].reservation,) and floor == 0
    assert db.token not in repr(db.tables) and "token" not in receipt
    locks = [p[0] for q, p in db.statements if "sp_getapplock" in q]
    lease, session, admission, book = [
        next(i for i, value in enumerate(locks) if value.startswith(prefix))
        for prefix in (
            "pipmind-safety-lease-v1:",
            "pipmind-safety-session-v1:",
            "pipmind-safety-admission-v1:",
            "pipmind-safety-admission-book-v1:",
        )
    ]
    assert lease < session < admission < book


def test_same_cursor_admission_never_uses_own_or_session_connector():
    db, store, grant, request = setup_case()
    store._connect = lambda: pytest.fail("admit used own connector")
    store._sessions.connect = lambda: pytest.fail("admit opened independent session connection")
    assert store.admit(grant, request)["assessment"].within_limits
    assert len(db.connections) == 1 and db.connections[0].closed


def test_request_bytes_are_frozen_before_lock_or_caller_mutation():
    db, store, grant, request = setup_case()
    db.before_lock = lambda: request.order.update(size=D("999"))
    receipt = store.admit(grant, request)
    assert request.order["size"] == D("999")  # Concurrent caller edit remains its own edit.
    assert receipt["assessment"].reservation.base_units == D("1000")
    saved = next(iter(db.tables["journal"].values()))[2]
    assert '"1000"' in saved
    assert store.lookup(receipt["request_id"]) == receipt


def test_duplicate_original_receipt_precedes_staleness_and_conflict_is_distinct():
    db, store, grant, request = setup_case()
    receipt = store.admit(grant, request)
    before = deepcopy(db.tables)
    db.now += timedelta(seconds=31)
    db.lease_expires += timedelta(seconds=60)
    assert store.admit(grant, request) == receipt
    assert db.tables == before
    changed = replace(request, order={**request.order, "size": D("999")})
    with pytest.raises(AdmissionRefused, match="^conflicting_admission_request$"):
        store.admit(grant, changed)
    assert db.tables == before


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("interrupt", [None, KeyboardInterrupt, SystemExit])
def test_commit_uncertainty_never_replays_or_returns_receipt(phase, interrupt):
    db, store, grant, request = setup_case()
    if interrupt:
        db.commit_interrupt = (phase, interrupt)
        db.close_interrupt = interrupt
    else:
        db.commit_failure = phase
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.admit(grant, request)
    assert len(db.connections) == 1 and db.connections[0].closed
    assert len(db.tables["journal"]) == (phase == "after")


@pytest.mark.parametrize("failure", ["error", "interrupt", "lease_expiry", "source_expiry"])
def test_staged_admission_rolls_back_every_table(failure):
    db, store, grant, request = setup_case()
    before = deepcopy(db.tables)
    if failure == "error":
        db.fail_on = "INSERT INTO dbo.pm_safety_admission_reservations_v1"
    elif failure == "interrupt":
        db.interrupt_on = "INSERT INTO dbo.pm_safety_admission_reservations_v1"
    else:
        db.after_stage_seconds = 2 if failure == "lease_expiry" else 31
        db.lease_expires = db.now + timedelta(seconds=1)
        if failure == "source_expiry":
            db.lease_expires += timedelta(seconds=100)
    expected = {"interrupt": KeyboardInterrupt, "lease_expiry": LeaseRefused}.get(
        failure, LeaseUnavailable
    )
    with pytest.raises(expected):
        store.admit(grant, request)
    assert db.tables == before and db.connections[-1].closed


def test_missing_session_aborts_without_pin_or_synthetic_refusal_receipt():
    db, store, grant, request = setup_case()
    db.session_missing = True
    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        store.admit(grant, request)
    assert all(not value for value in db.tables.values())


def test_genuine_two_proposal_combined_cap_and_funded_positive_controls():
    for cap, expected in ((D("1500"), False), (D("10000"), True)):
        db, store, grant, first = setup_case(
            caps={"max_gross_exposure": cap}, marks=("request-A", "request-B")
        )
        second, _, _ = make_case(
            request_id="request-B",
            caps={"max_gross_exposure": cap},
            marks=("request-A", "request-B"),
        )
        assert store.admit(grant, first)["assessment"].within_limits
        receipt = store.admit(grant, second)
        assert receipt["assessment"].within_limits is expected
        assert len(db.tables["journal"]) == 2
        assert len(store.snapshot()[0]) == (2 if expected else 1)


def test_count_floor_only_advances_for_validated_source_and_refusal_replays_exactly():
    db, store, grant, request = setup_case(count=3)
    denied = replace(request, facts=replace(request.facts, kill_switch=True))
    # Changed checkpoint source must be explicitly rebound to matching session evidence;
    # an unbound source cannot advance a count floor.
    bad = store.admit(grant, denied)
    assert not bad["assessment"].within_limits and store.snapshot()[1] == 0
    good = store.admit(grant, replace(request, order={**request.order, "client_order_id": "good"}))
    assert good["assessment"].within_limits and store.snapshot()[1] == 3
    assert store.lookup(denied.order["client_order_id"]) == bad


def test_orphaned_projection_missing_pin_and_corrupt_receipt_are_not_empty_evidence():
    db, store, grant, request = setup_case()
    store.admit(grant, request)
    before = deepcopy(db.tables)
    db.tables["accounts"].clear()
    with pytest.raises(LeaseUnavailable):
        store.snapshot()
    db.tables = before
    key = next(iter(db.tables["journal"]))
    row = list(db.tables["journal"][key])
    row[5] = "0" * 64
    db.tables["journal"][key] = tuple(row)
    with pytest.raises(LeaseUnavailable):
        store.lookup(request.order["client_order_id"])


def test_invalid_id_and_oversize_input_refuse_before_sql():
    db, store, grant, request = setup_case()
    for request_id in ("", "x" * 129, "\ud800", "x\x00"):
        with pytest.raises(AdmissionRefused):
            store.lookup(request_id)
    invalid = replace(request, order={**request.order, "untrusted": "x" * 1100000})
    with pytest.raises(AdmissionRefused):
        store.admit(grant, invalid)
    assert db.connections == []


def test_conflict_result_requires_acknowledged_commit_without_rewriting_audit():
    db, store, grant, request = setup_case()
    store.admit(grant, request)
    before = deepcopy(db.tables)
    db.commit_failure = "after"
    changed = replace(request, order={**request.order, "size": D("999")})
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.admit(grant, changed)
    assert db.tables == before


def test_overcapacity_projection_never_returns_prefix_as_complete():
    db, store, grant, request = setup_case()
    receipt = store.admit(grant, request)
    original = receipt["assessment"].reservation
    db.tables["reservations"].clear()
    for i in range(1001):
        item = replace(original, request_id=f"synthetic-{i:04}")
        body = encode_reservation(item)
        db.tables["reservations"][store._key, item.request_id] = (
            item.session_id,
            body,
            sha256(body.encode()).hexdigest(),
        )
    with pytest.raises(LeaseUnavailable, match="^admission_store_unavailable$"):
        store.snapshot()


def test_lookup_rejects_foreign_book_reference_with_individually_valid_hashes():
    db, store, grant, request = setup_case()
    receipt = store.admit(grant, request)
    foreign = tuple(replace(p, account_id="foreign-account") for p in store._policies)
    body, digest = encode_policy_book(foreign), policy_book_digest(foreign)
    db.tables["books"][digest] = body
    key = next(iter(db.tables["journal"]))
    row = list(db.tables["journal"][key])
    changed = {**receipt, "policy_book_sha256": digest}
    encoded = encode_receipt(changed)
    row[1], row[4], row[5] = digest, encoded, sha256(encoded.encode()).hexdigest()
    db.tables["journal"][key] = tuple(row)
    with pytest.raises(LeaseUnavailable, match="^admission_store_unavailable$"):
        store.lookup(request.order["client_order_id"])
