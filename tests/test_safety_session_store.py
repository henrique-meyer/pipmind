"""Transactional session-store doubles; no connector/provider imports or calls."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from pipmind.contracts import utc
from pipmind.safety.session import (
    EquityObservation,
    SessionBinding,
    SessionCommitUnknown,
    SessionRefused,
    SessionSpec,
    SessionUnavailable,
    encode_observation,
    fingerprint,
)
from pipmind.safety.session_sql import SqlSessionStore, bootstrap

NOW = utc("2026-10-21T12:00:00Z")


def specification(binding=None, **changes):
    return replace(
        SessionSpec(
            binding or SessionBinding("synthetic-account", "demo", "USD"),
            "session-A",
            "synthetic-policy",
            "a" * 64,
            "2026-10-21T00:00:00Z",
            "2026-10-22T00:00:00Z",
            Decimal("1000"),
            "opening-proof",
            Decimal("100"),
            60,
        ),
        **changes,
    )


def observation(spec, observation_id="obs-A", **changes):
    return replace(
        EquityObservation(
            spec.binding,
            spec.session_id,
            observation_id,
            "2026-10-21T12:00:00Z",
            "2026-10-21T12:00:00Z",
            Decimal("900"),
            Decimal("0"),
            True,
            True,
            spec.started_at,
            "2026-10-21T12:00:00Z",
            "account-proof",
            "flow-proof",
        ),
        **changes,
    )


class Database:
    def __init__(self):
        self.accounts, self.sessions, self.observations = {}, {}, {}
        self.connections, self.statements = [], []
        self.outage = self.fail_state_write = False
        self.commit_failure = self.commit_interrupt = self.close_interrupt = None
        self.lock_result = 0
        self.autocommit = False

    def connect(self):
        if self.outage:
            raise OSError("synthetic private connection failure")
        db = Connection(self)
        self.connections.append(db)
        return db

    def retained(self):
        return deepcopy((self.accounts, self.sessions, self.observations))


class Connection:
    def __init__(self, database):
        self.database = database
        self.accounts, self.sessions, self.observations = database.retained()
        self.autocommit = database.autocommit
        self.trancount = 1
        self.closed = self.committed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        if self.database.commit_failure == "before":
            raise OSError("synthetic commit transport failure")
        if self.database.commit_interrupt and self.database.commit_interrupt[0] == "before":
            raise self.database.commit_interrupt[1]("synthetic commit interrupt")
        self.database.accounts = deepcopy(self.accounts)
        self.database.sessions = deepcopy(self.sessions)
        self.database.observations = deepcopy(self.observations)
        self.committed = True
        if self.database.commit_failure == "after":
            raise OSError("synthetic commit acknowledgement lost")
        if self.database.commit_interrupt:
            raise self.database.commit_interrupt[1]("synthetic commit interrupt")

    def close(self):
        self.closed = True
        if self.database.close_interrupt:
            raise self.database.close_interrupt("synthetic cleanup interruption")


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ("synthetic",)
        self.result = None

    def execute(self, sql, params=()):
        self.connection.database.statements.append((sql, params))
        q = " ".join(sql.split())
        self.result = None
        db = self.connection
        if q.startswith("SET ") or q.startswith("IF OBJECT_ID"):
            pass
        elif "sp_getapplock" in q:
            self.result = (db.database.lock_result,)
        elif q == "SELECT @@TRANCOUNT":
            self.result = (db.trancount,)
        elif q.startswith("SELECT account_id,environment,currency"):
            self.result = db.accounts.get(params[0])
        elif q.startswith("INSERT INTO dbo.pm_safety_session_accounts_v1"):
            key, *binding = params
            db.accounts[key] = tuple(binding)
        elif q.startswith("SELECT started_at,ends_at,"):
            spec_bound, state_bound, key, sid = params
            row = deepcopy(db.sessions.get((key, sid)))
            if row:
                for column, bound in ((2, spec_bound), (4, state_bound)):
                    if len(row[column].encode("utf-16-le")) > bound:
                        row[column] = None
                self.result = tuple(row)
        elif q.startswith("SELECT TOP(1) 1 FROM dbo.pm_safety_session_sessions_v1"):
            key, end, start = params
            if any(k[0] == key and r[0] < end and r[1] > start for k, r in db.sessions.items()):
                self.result = (1,)
        elif q.startswith("INSERT INTO dbo.pm_safety_session_sessions_v1"):
            key, sid, *row = params
            db.sessions[key, sid] = row
        elif q.startswith("SELECT as_of,CASE WHEN DATALENGTH(body)"):
            body_bound, receipt_bound, key, sid, oid = params
            row = db.observations.get((key, sid, oid))
            if row:
                as_of, body, body_hash, receipt, receipt_hash = row
                self.result = (
                    as_of,
                    body if len(body.encode("utf-16-le")) <= body_bound else None,
                    body_hash,
                    receipt if len(receipt.encode("utf-16-le")) <= receipt_bound else None,
                    receipt_hash,
                )
        elif q.startswith("SELECT TOP(1) 1 FROM dbo.pm_safety_session_observations_v1"):
            key, sid, moment = params
            if any(k[:2] == (key, sid) and row[0] == moment for k, row in db.observations.items()):
                self.result = (1,)
        elif q.startswith("INSERT INTO dbo.pm_safety_session_observations_v1"):
            key, sid, oid, *row = params
            db.observations[key, sid, oid] = row
        elif q.startswith("UPDATE dbo.pm_safety_session_sessions_v1 SET state="):
            if db.database.fail_state_write:
                failure = db.database.fail_state_write
                error = failure if isinstance(failure, type) else RuntimeError
                raise error("private failure after observation insert")
            body, sha256, key, sid = params
            db.sessions[key, sid][4:] = [body, sha256]
        else:
            raise AssertionError("unrecognized synthetic SQL")
        return self

    def fetchone(self):
        return self.result

    def nextset(self):
        return False


@pytest.fixture
def initialized():
    database = Database()
    spec = specification()
    store = SqlSessionStore(database.connect, spec.binding, spec.session_id)
    bootstrap(database.connect)
    assert store.initialize(spec).revision == 0
    return database, store, spec


def test_construction_is_no_sql_and_bootstrap_is_explicit_serialized():
    db = Database()
    spec = specification()
    SqlSessionStore(db.connect, spec.binding, spec.session_id)
    assert db.connections == []
    bootstrap(db.connect)
    assert db.connections[0].closed and db.connections[0].committed
    locks = [p[0] for q, p in db.statements if "sp_getapplock" in q]
    assert locks == ["pipmind-safety-session-schema-v1"]
    assert sum(q.startswith("IF OBJECT_ID") for q, _ in db.statements) == 3


def test_account_currency_pin_and_initialization_replay_preserve_exact_history(initialized):
    db, store, spec = initialized
    store.observe(observation(spec), now=NOW)
    before = db.retained()
    reopened = SqlSessionStore(db.connect, spec.binding, spec.session_id)
    assert reopened.initialize(spec) == store.read()
    assert reopened.read().daily_loss_latched is True
    assert db.retained() == before
    changed = replace(spec, max_daily_loss=Decimal("101"))
    with pytest.raises(SessionRefused, match="^conflicting_session_spec$"):
        reopened.initialize(changed)
    other_currency = replace(spec.binding, currency="EUR")
    other = SqlSessionStore(db.connect, other_currency, "session-B")
    with pytest.raises(SessionRefused, match="^session_binding_mismatch$"):
        other.initialize(specification(other_currency, session_id="session-B"))
    assert db.retained() == before


def test_overlap_guard_is_account_wide_and_adjacent_session_preserves_latch(initialized):
    db, store, spec = initialized
    store.observe(observation(spec), now=NOW)
    other = SqlSessionStore(db.connect, spec.binding, "session-B")
    with pytest.raises(SessionRefused, match="^overlapping_safety_session$"):
        other.initialize(replace(spec, session_id="session-B"))
    next_spec = replace(
        spec, session_id="session-B", started_at=spec.ends_at, ends_at="2026-10-23T00:00:00Z"
    )
    assert other.initialize(next_spec).daily_loss_latched is False
    assert store.read().daily_loss_latched is True
    assert len(db.sessions) == 2 and len(db.observations) == 1
    assert len(db.accounts) == 1


def test_different_environment_has_independent_explicit_binding(initialized):
    db, _, spec = initialized
    binding = replace(spec.binding, environment="live")
    other = SqlSessionStore(db.connect, binding, spec.session_id)
    other.initialize(replace(spec, binding=binding))
    assert len(db.accounts) == 2 and len(db.sessions) == 2


def test_duplicate_receipt_is_original_and_cumulative_flow_never_double_counted(initialized):
    db, store, spec = initialized
    first = observation(spec, equity=Decimal("950"), external_net_cash_flow=Decimal("20"))
    receipt = store.observe(first, now=NOW)
    assert receipt.daily_loss == Decimal("70")
    second = observation(
        spec,
        "obs-B",
        as_of="2026-10-21T12:00:01Z",
        received_at="2026-10-21T12:00:01Z",
        cash_flow_ends_at="2026-10-21T12:00:01Z",
        equity=Decimal("960"),
        external_net_cash_flow=Decimal("20"),
    )
    store.observe(second, now=utc("2026-10-21T12:00:01Z"))
    before = db.retained()
    statements = len(db.statements)
    assert store.observe(first, now=utc("2026-10-21T12:00:01Z")) == receipt
    assert db.retained() == before
    assert not any(
        q.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for q, _ in db.statements[statements:]
    )
    assert store.read().daily_loss == Decimal("60")
    assert len(db.observations) == 2


def test_same_id_collision_and_same_instant_alias_refuse_without_mutation(initialized):
    db, store, spec = initialized
    first = observation(spec)
    store.observe(first, now=NOW)
    before = db.retained()
    with pytest.raises(SessionRefused, match="^conflicting_session_observation$"):
        store.observe(replace(first, equity=Decimal("899")), now=NOW)
    with pytest.raises(SessionRefused, match="^conflicting_session_time$"):
        store.observe(
            replace(first, observation_id="obs-alias", as_of="2026-10-21T12:00:00+00:00"), now=NOW
        )
    assert db.retained() == before


def test_historical_time_collision_checked_against_entire_history(initialized):
    db, store, spec = initialized
    old = observation(
        spec, "old", as_of="2026-10-21T11:00:00Z", cash_flow_ends_at="2026-10-21T11:00:00Z"
    )
    store.observe(old, now=NOW)
    store.observe(observation(spec), now=NOW)
    before = db.retained()
    with pytest.raises(SessionRefused, match="^conflicting_session_time$"):
        store.observe(replace(old, observation_id="old-other"), now=NOW)
    assert db.retained() == before


def test_late_breach_latches_without_rewinding_effective_loss(initialized):
    db, store, spec = initialized
    latest = observation(spec, equity=Decimal("990"))
    store.observe(latest, now=NOW)
    earlier = observation(
        spec,
        "historical-breach",
        as_of="2026-10-21T11:00:00Z",
        cash_flow_ends_at="2026-10-21T11:00:00Z",
        equity=Decimal("800"),
    )
    receipt = store.observe(earlier, now=NOW)
    state = store.read()
    assert receipt.daily_loss == Decimal("200") and not receipt.became_current
    assert receipt.daily_loss_latched is state.daily_loss_latched is True
    assert state.latest == latest and state.daily_loss == Decimal("10") and state.revision == 2
    assert len(db.observations) == 2


def test_staged_observation_failure_rolls_back_all_state_and_retry_exactly_once(initialized):
    db, store, spec = initialized
    before = db.retained()
    event = observation(spec)
    db.fail_state_write = True
    with pytest.raises(SessionUnavailable, match="^session_store_unavailable$"):
        store.observe(event, now=NOW)
    assert db.retained() == before
    assert db.connections[-1].closed and not db.connections[-1].committed
    db.fail_state_write = False
    receipt = store.observe(event, now=NOW)
    assert store.observe(event, now=NOW) == receipt
    assert len(db.observations) == 1 and store.read().revision == 1


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_precommit_write_interruption_propagates_after_disposal_without_effects(
    initialized, interrupt
):
    db, store, spec = initialized
    before = db.retained()
    db.fail_state_write = db.close_interrupt = interrupt
    with pytest.raises(interrupt):
        store.observe(observation(spec), now=NOW)
    assert db.retained() == before
    assert db.connections[-1].closed and not db.connections[-1].committed


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("interrupt", [None, KeyboardInterrupt, SystemExit])
def test_commit_failures_are_uncertain_without_automatic_replay(initialized, phase, interrupt):
    db, store, spec = initialized
    if interrupt:
        db.commit_interrupt = (phase, interrupt)
        db.close_interrupt = interrupt
    else:
        db.commit_failure = phase
    connections = len(db.connections)
    with pytest.raises(SessionCommitUnknown, match="^session_commit_unknown$"):
        store.observe(observation(spec), now=NOW)
    assert len(db.connections) == connections + 1
    assert len(db.observations) == (1 if phase == "after" else 0)
    assert db.connections[-1].closed


def test_same_cursor_read_uses_caller_connection_lock_and_no_commit_or_freshness(initialized):
    db, store, _ = initialized
    connection = db.connect()
    cur = connection.cursor()
    connection_count = len(db.connections)
    state = store.read_in_transaction(cur)
    assert state.revision == 0 and state.latest is None
    assert len(db.connections) == connection_count
    assert not connection.committed and not connection.closed
    assert any("sp_getapplock" in q and p == (store._resource,) for q, p in db.statements)
    connection.close()


@pytest.mark.parametrize("autocommit,trancount", [(True, 0), (False, 0)])
def test_same_cursor_read_rejects_missing_caller_transaction(initialized, autocommit, trancount):
    db, store, _ = initialized
    connection = db.connect()
    connection.autocommit, connection.trancount = autocommit, trancount
    with pytest.raises(SessionRefused, match="^session_transaction_required$"):
        store.read_in_transaction(connection.cursor())
    assert not connection.committed and not connection.closed
    connection.close()


def test_read_is_bounded_and_detects_corruption_without_driver_or_payload_leak(initialized):
    db, store, _ = initialized
    row = next(iter(db.sessions.values()))
    row[4] = '"' + "private-canary" * 6000 + '"'
    row[5] = fingerprint(row[4])
    with pytest.raises(SessionUnavailable, match="^session_store_unavailable$"):
        store.read()
    assert db.connections[-1].closed
    assert any("DATALENGTH(state)" in q and p[:2] == (65536, 65536) for q, p in db.statements)


def test_corrupt_duplicate_receipt_does_not_acknowledge_retry(initialized):
    db, store, spec = initialized
    event = observation(spec)
    store.observe(event, now=NOW)
    row = next(iter(db.observations.values()))
    row[-1] = "0" * 64
    with pytest.raises(SessionUnavailable, match="^session_store_unavailable$"):
        store.observe(event, now=NOW)
    assert store.read().revision == 1


def test_duplicate_index_timestamp_must_match_immutable_body(initialized):
    db, store, spec = initialized
    event = observation(spec)
    receipt = store.observe(event, now=NOW)
    row = next(iter(db.observations.values()))
    original_time = row[0]
    row[0] += timedelta(seconds=1)
    before = db.retained()
    with pytest.raises(SessionUnavailable, match="^session_store_unavailable$"):
        store.observe(event, now=NOW)
    assert db.retained() == before and store.read().revision == 1
    next(iter(db.observations.values()))[0] = original_time
    assert store.observe(event, now=NOW) == receipt


def test_payload_hash_and_original_strings_are_retained_exactly(initialized):
    db, store, spec = initialized
    event = observation(
        spec,
        as_of="2026-10-21T12:00:00.000000+00:00",
        cash_flow_ends_at="2026-10-21T12:00:00Z",
        equity=Decimal("9E+2"),
    )
    receipt = store.observe(event, now=NOW)
    row = next(iter(db.observations.values()))
    assert row[1] == encode_observation(event)
    assert row[2] == receipt.payload_sha256 == fingerprint(row[1])
    assert store.read().latest == event


def test_outage_and_missing_session_do_not_return_default_state(initialized):
    db, store, spec = initialized
    missing = SqlSessionStore(db.connect, spec.binding, "missing")
    with pytest.raises(SessionRefused, match="^session_not_initialized$"):
        missing.read()
    db.outage = True
    with pytest.raises(SessionUnavailable, match="^session_store_unavailable$"):
        store.read()


def test_store_operations_use_only_session_account_lock(initialized):
    db, store, spec = initialized
    store.observe(observation(spec), now=NOW)
    store.read()
    locks = [p[0] for q, p in db.statements if "sp_getapplock" in q]
    assert set(locks) == {"pipmind-safety-session-schema-v1", store._resource}
    assert "lease" not in store._resource and "admission" not in store._resource
