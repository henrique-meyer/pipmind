"""Transaction completion must not be changed by a broken cleanup connection."""

import pytest

from pipmind.shadow.common import ShadowError
from pipmind.shadow.sql import stream_transaction, transaction


class BrokenCleanup:
    def __init__(self):
        self.closed = False
        self.committed = False
        self.statement = ""
        self.description = ("result",)

    def cursor(self):
        return self

    def execute(self, statement, *args):
        self.statement = statement

    def fetchone(self):
        return (1 if "snapshot_isolation_state" in self.statement else 0,)

    def commit(self):
        self.committed = True

    def rollback(self):
        raise RuntimeError("synthetic already-closed SQL session")

    def close(self):
        self.closed = True
        raise RuntimeError("synthetic cursor cleanup after transport close")


@pytest.mark.parametrize("snapshot", [False, True])
def test_broken_cleanup_preserves_original_refusal_and_disposes_session(snapshot):
    db = BrokenCleanup()
    context = (
        stream_transaction(lambda **kwargs: db, "synthetic")
        if snapshot
        else transaction(lambda: db)
    )
    with pytest.raises(ShadowError, match="original_refusal"):
        with context:
            raise ShadowError("original_refusal")
    assert db.closed and not db.committed


@pytest.mark.parametrize("snapshot", [False, True])
def test_acknowledged_commit_is_not_reported_as_failure_from_cleanup(snapshot):
    db = BrokenCleanup()
    context = (
        stream_transaction(lambda **kwargs: db, "synthetic")
        if snapshot
        else transaction(lambda: db)
    )
    with context:
        pass
    assert db.closed and db.committed
