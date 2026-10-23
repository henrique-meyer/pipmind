"""Session setup stays outside parameterized RPC scope; lock safety is unchanged."""

from types import SimpleNamespace

import pytest

from pipmind.shadow.common import ShadowError
from pipmind.shadow.sql import transaction


class Session:
    def __init__(self, *, results=0, lock_result=0, fail=None):
        self.remaining = results
        self.lock_result = lock_result
        self.fail = fail
        self.calls = []
        self.commits = self.closes = self.nextsets = 0
        self.description = None if results else ("lock_result",)

    def cursor(self):
        if self.fail == "cursor":
            raise RuntimeError("synthetic cursor failure")
        return self

    def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        if self.fail == "execute" or (self.fail == "lock_execute" and "sp_getapplock" in statement):
            raise RuntimeError("synthetic setup failure")

    def nextset(self):
        self.nextsets += 1
        if self.fail == "nextset":
            raise RuntimeError("synthetic result navigation failure")
        if self.fail == "missing_result":
            return False
        self.remaining -= 1
        if self.remaining == 0:
            self.description = ("lock_result",)
        return True

    def fetchone(self):
        if self.fail == "fetch":
            raise RuntimeError("synthetic lock read failure")
        return (self.lock_result,)

    def commit(self):
        self.commits += 1
        if self.fail == "commit":
            raise RuntimeError("synthetic unacknowledged commit")

    def close(self):
        self.closes += 1
        if self.fail == "close":
            raise RuntimeError("synthetic cleanup failure")

    def rollback(self):
        raise AssertionError("non-pooled disposal must not run rollback handshake")


@pytest.mark.parametrize("results", [0, 1, 3])
@pytest.mark.parametrize("lock_result", [0, 1])
def test_parameter_free_session_setup_precedes_exact_parameterized_lock(results, lock_result):
    session = Session(results=results, lock_result=lock_result)
    resource = "synthetic-resource'; SELECT 123; --"
    with transaction(lambda: session, resource) as cursor:
        assert cursor is session
        assert session.commits == session.closes == 0
        assert len(session.calls) == 2
        setup, setup_parameters = session.calls[0]
        assert setup == (
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        # pytds sends parameter-free text as a session batch. Parameters use
        # sp_executesql, whose SET options are restored when that batch returns.
        assert setup_parameters is None
        assert "sp_getapplock" not in setup
        statement, parameters = session.calls[1]
        assert statement.startswith("DECLARE @r int;") and "SET " not in statement
        assert statement.count("sp_getapplock") == 1
        assert "@Resource=%s" in statement and parameters == (resource,)
        assert resource not in statement
        assert "@LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=5000" in statement
        assert "SELECT @r" in statement
        assert session.nextsets == results
    assert session.commits == session.closes == 1


@pytest.mark.parametrize("lock_result", [-1, -2, -3, -999])
def test_negative_application_lock_never_yields_or_commits(lock_result):
    session = Session(results=2, lock_result=lock_result)
    with pytest.raises(ShadowError, match="^shadow_store_busy$"):
        with transaction(lambda: session):
            pytest.fail("must not expose an unlocked cursor")
    assert len(session.calls) == 2 and session.commits == 0 and session.closes == 1


@pytest.mark.parametrize("failure", ["cursor", "execute", "lock_execute", "nextset", "fetch"])
def test_setup_failure_disposes_without_exposing_or_committing(failure):
    session = Session(results=1, fail=failure)
    with pytest.raises(ShadowError, match="^shadow_store_transaction_failed$"):
        with transaction(lambda: session):
            pytest.fail("must not expose an uncertain lock")
    assert session.commits == 0 and session.closes == 1


def test_missing_lock_result_preserves_specific_refusal():
    session = Session(results=1, fail="missing_result")
    with pytest.raises(ShadowError, match="^shadow_lock_result_missing$"):
        with transaction(lambda: session):
            pytest.fail("must not yield without a lock result")
    assert session.commits == 0 and session.closes == 1


def test_connect_failure_is_not_retried():
    attempts = []

    def connect():
        attempts.append(1)
        raise RuntimeError("synthetic unavailable transport")

    with pytest.raises(ShadowError, match="^shadow_store_transaction_failed$"):
        with transaction(connect):
            pytest.fail("must not yield")
    assert attempts == [1]


class DomainRefusal(Exception):
    pass


@pytest.mark.parametrize("error", [ShadowError("original_refusal"), DomainRefusal("domain")])
def test_caller_refusal_preserves_exact_exception_and_never_commits(error):
    session = Session(fail="close")
    with pytest.raises(type(error)) as caught:
        with transaction(lambda: session, passthrough=(DomainRefusal,)):
            raise error
    assert caught.value is error
    assert session.commits == 0 and session.closes == 1


def test_unexpected_caller_failure_normalizes_without_commit():
    session = Session()
    with pytest.raises(ShadowError, match="^shadow_store_transaction_failed$"):
        with transaction(lambda: session):
            raise RuntimeError("synthetic caller failure")
    assert session.commits == 0 and session.closes == 1


def test_unacknowledged_commit_is_failure_without_retry():
    session = Session(fail="commit")
    attempts = []

    def connect():
        attempts.append(1)
        return session

    with pytest.raises(ShadowError, match="^shadow_store_transaction_failed$"):
        with transaction(connect):
            pass
    assert attempts == [1] and session.commits == session.closes == 1


def test_acknowledged_commit_survives_cleanup_failure():
    session = Session(fail="close")
    with transaction(lambda: session):
        pass
    assert session.commits == session.closes == 1


def test_pinned_driver_routes_combined_parameterized_setup_into_separate_rpc_scope():
    from pytds import tds_base
    from pytds.tds_session import _TdsSession

    class RouteProbe:
        def __init__(self):
            self.routes = []

        def _ensure_transaction(self):
            pass

        def _convert_params(self, named):
            return [
                SimpleNamespace(
                    name=name, type=SimpleNamespace(get_declaration=lambda: "NVARCHAR(128)")
                )
                for name in named
            ]

        def make_param(self, name, value):
            return name, value

        def submit_rpc(self, procedure, params, flags):
            self.routes.append(("rpc", procedure))

        def submit_plain_query(self, query):
            self.routes.append(("session_batch", None))

        def begin_response(self):
            pass

        def find_result_or_done(self):
            pass

    setup = "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
    safe = RouteProbe()
    _TdsSession.execute(safe, setup)
    assert safe.routes == [("session_batch", None)]
    unsafe = RouteProbe()
    _TdsSession.execute(unsafe, setup + "; SELECT %s", ("synthetic-resource",))
    assert unsafe.routes == [("rpc", tds_base.SP_EXECUTESQL)]
    # The actual SQL regression checks effective @@OPTIONS/isolation AFTER
    # setup; this driver-routing proof alone is not SQL Server qualification.
