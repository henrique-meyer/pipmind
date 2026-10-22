"""Disposable runner invocation boundaries; no SQL execution/quality claim."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pipmind.safety.admission import decode_policy_book, decode_request, encode_request
from pipmind.safety.eval_runner_sql import (
    DisposableSQLScope,
    SQLProfileCommitUnknown,
    SQLProfileRefused,
    SQLProfileUnavailable,
    _grant_body,
    _guarded_connector,
    _reservation,
    _stores,
    _transaction,
    run_sql_profile,
)
from pipmind.safety.eval_scenarios import captured_order, load_scenarios, prepare_scenario
from pipmind.safety.lease_sql import LeaseGrant
from pipmind.safety.session import decode_spec

SCOPE = DisposableSQLScope("a" * 32, "disposable-evaluation", True)


class Connection:
    def __init__(self, *, database="disposable-evaluation", engine=3, autocommit=False):
        self.database, self.engine, self.autocommit = database, engine, autocommit
        self.closed, self.committed, self.queries = False, False, []
        self.commit_error = self.close_error = None

    def cursor(self):
        return self

    def execute(self, query):
        self.queries.append(query)

    def fetchone(self):
        return self.database, self.engine

    def commit(self):
        self.committed = True
        if self.commit_error:
            raise self.commit_error("private synthetic commit details")

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error("private synthetic close details")


@pytest.mark.parametrize(
    "scope",
    [
        None,
        DisposableSQLScope("a" * 32, "disposable-evaluation"),
        DisposableSQLScope("arbitrary-existing-account", "disposable-evaluation", True),
        DisposableSQLScope("A" * 32, "disposable-evaluation", True),
        DisposableSQLScope("a" * 32, "", True),
        DisposableSQLScope("a" * 32, "bad\x00database", True),
    ],
)
def test_no_connector_call_without_exact_confirmed_disposable_scope(scope):
    def forbidden():
        pytest.fail("scope refusal must precede connector access")

    with pytest.raises(SQLProfileRefused):
        run_sql_profile(forbidden, scope=scope)


@pytest.mark.parametrize(
    "options",
    [
        {"database": "other-database"},
        {"engine": 5},
        {"engine": True},
        {"autocommit": True},
        {"autocommit": None},
    ],
)
def test_connector_identity_and_autocommit_checked_before_any_ddl(options):
    db = Connection(**options)
    guarded = _guarded_connector(lambda: db, SCOPE)
    with pytest.raises(SQLProfileRefused):
        guarded()
    assert db.closed and not db.committed
    assert all(not query.startswith(("IF ", "INSERT", "UPDATE")) for query in db.queries)


def test_guard_checks_every_new_connection_instead_of_caching_database_identity():
    first, second = Connection(), Connection(database="switched-database")
    connections = iter((first, second))
    guarded = _guarded_connector(lambda: next(connections), SCOPE)
    assert guarded() is first
    with pytest.raises(SQLProfileRefused, match="^disposable_database_mismatch$"):
        guarded()
    assert second.closed and len(first.queries) == len(second.queries) == 1
    first.close()


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt, SystemExit])
def test_runner_owned_commit_failure_is_unknown_even_when_cleanup_interrupts(error):
    db = Connection()
    db.commit_error, db.close_error = error, KeyboardInterrupt
    with pytest.raises(SQLProfileCommitUnknown, match="^sql_profile_commit_unknown$"):
        with _transaction(lambda: db):
            pass
    assert db.closed and db.committed


def test_precommit_interruption_disposes_without_commit_and_storage_errors_are_fixed():
    db = Connection()
    with pytest.raises(KeyboardInterrupt):
        with _transaction(lambda: db):
            raise KeyboardInterrupt("synthetic interrupted read")
    assert db.closed and not db.committed
    db = Connection()
    with pytest.raises(SQLProfileUnavailable, match="^sql_profile_unavailable$"):
        with _transaction(lambda: db):
            raise OSError("private details must not escape")
    assert db.closed and not db.committed


def test_source_lineage_mismatch_refuses_before_bootstrap_or_connection():
    scenarios = list(load_scenarios())
    scenarios[0] = replace(scenarios[0], supplement_sha256="f" * 64)

    def forbidden():
        pytest.fail("lineage mismatch must precede connector access")

    with pytest.raises(SQLProfileRefused, match="^sql_profile_lineage_mismatch$"):
        run_sql_profile(forbidden, scope=SCOPE, scenarios=scenarios)


def test_all_prepared_stores_construct_without_connecting_or_changing_orders():
    def forbidden():
        pytest.fail("constructing test stores must not contact SQL")

    now = datetime(2026, 10, 22, tzinfo=UTC)
    for scenario in load_scenarios():
        variants = (
            ("positive", "pause", "kill") if scenario.case_id == "S06" else ("positive", "target")
        )
        for variant in variants:
            prepared = prepare_scenario(
                scenario,
                sql_now=now,
                account_id="synthetic-eval-" + scenario.case_id + "-" + variant,
                session_id="test-session",
                variant=variant,
            )
            spec = decode_spec(prepared.session_spec_body)
            book = decode_policy_book(prepared.policy_book_body)
            request = decode_request(prepared.request_body)
            captured = captured_order(prepared.request_body)
            stores = _stores(
                forbidden,
                spec,
                book,
                prepared.mapping_body,
                "test-epoch",
                request.facts.proposal.base_units_per_size,
            )
            assert stores.gateway._mapping_code(request) is None
            assert captured_order(encode_request(request)) == captured
            grant = LeaseGrant(
                stores.leases.binding, "test-epoch", "synthetic-owner", 1, "e" * 64, now
            )
            safe = _grant_body(grant)
            assert grant.token not in safe and "token" not in safe


def test_retained_reservation_absence_is_distinct_from_corrupt_durable_body():
    class ReservationConnection(Connection):
        def __init__(self, row):
            super().__init__()
            self.row = row
            self.description = (("synthetic",),)

        def execute(self, query, params=None):
            self.queries.append((query, params))

        def fetchone(self):
            # Application lock acquisition receives its fixed SQL success code.
            return (0,) if "sp_getapplock" in self.queries[-1][0] else self.row

    request = SimpleNamespace(order={"client_order_id": "synthetic-order"})
    for row in (None, ("private malformed body", "a" * 64)):
        db = ReservationConnection(row)
        stores = SimpleNamespace(
            connect=lambda db=db: db,
            admission=SimpleNamespace(_resource="synthetic-lock", _key="b" * 64),
            sessions=SimpleNamespace(session_id="synthetic-session"),
        )
        if row is None:
            assert _reservation(stores, request) is None
            assert db.committed
        else:
            with pytest.raises(SQLProfileRefused, match="^evaluation_reservation_integrity$"):
                _reservation(stores, request)
            assert not db.committed
        assert db.closed
        query, params = db.queries[-1]
        assert "DATALENGTH(body)" in query and params[-1] == "synthetic-order"
