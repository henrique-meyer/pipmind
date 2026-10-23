"""One setup/lock request keeps transaction safety and uncertain-commit behavior."""

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

    def execute(self, statement, parameters=()):
        self.calls.append((statement, parameters))
        if self.fail == "execute":
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
def test_setup_and_parameterized_lock_use_one_request_before_yield(results, lock_result):
    session = Session(results=results, lock_result=lock_result)
    resource = "synthetic-resource'; SELECT 123; --"
    with transaction(lambda: session, resource) as cursor:
        assert cursor is session
        assert session.commits == session.closes == 0
        assert len(session.calls) == 1
        statement, parameters = session.calls[0]
        assert statement.startswith(
            "SET NOCOUNT ON; SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE; "
        )
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
    assert len(session.calls) == 1 and session.commits == 0 and session.closes == 1


@pytest.mark.parametrize("failure", ["cursor", "execute", "nextset", "fetch"])
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
