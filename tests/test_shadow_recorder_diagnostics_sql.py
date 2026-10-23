"""Actual disposable SQL coverage for recorder error retention and lock refusal."""

import os
from copy import deepcopy
from uuid import uuid4

import pytest

from pipmind.contracts import utc
from pipmind.shadow import qualification as q
from pipmind.shadow.common import ShadowError, digest
from pipmind.shadow.sql import connector, migrate, transaction
from pipmind.shadow.store import SqlMind

pytestmark = pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_TESTS") != "true", reason="separate SQL integration gate"
)
MOMENT = utc("2026-10-20T12:00:00Z")


@pytest.fixture
def recorder(monkeypatch):
    connect = connector()
    migrate(connect)
    store = SqlMind(connect, "diagnostic-" + uuid4().hex)
    store.initialize(
        "Synthetic recorder identity\n",
        {"version": "v1", "text": "Synthetic no-trading playbook\n"},
        "2026-10-01T00:00:00Z",
        binding=digest("synthetic-recorder-binding"),
    )
    q.arm(store, "sql-diagnostics", ["EUR/USD"], "b" * 40, MOMENT)
    monkeypatch.setenv("BUILD_SHA", "b" * 40)
    monkeypatch.setattr(q, "read_gateway", lambda *args: {})
    # This is diagnostic SQL validation, not broker/quality/day validation.
    monkeypatch.setattr(q, "healthy", lambda *args: ["startup_backlog"])
    return store, q.Recorder(store, None, lambda: {})


def test_busy_source_lock_retains_diagnostic_without_sample_then_next_capture_persists(recorder):
    store, capture = recorder
    initial_head = store.head()
    initial_active = store.runtime(q.ACTIVE)
    sample_key = "qualification:sql-diagnostics:sample:" + str(int(MOMENT.timestamp()) // 60)
    with transaction(store.connect, "shadow:" + store.trader):
        with pytest.raises(ShadowError, match="^shadow_store_busy$"):
            capture.capture(MOMENT)
    # Lock refusal is from the FIRST active runtime read, not a fabricated INSERT failure.
    failure = deepcopy(capture._diagnostics[0])
    assert failure["stage"] == "active_read" and failure["code"] == "shadow_store_busy"
    assert store.head() == initial_head and store.runtime(q.ACTIVE) == initial_active
    assert store.runtime(sample_key) is None and store.status()["outcomes"] == 0
    capture.capture(MOMENT)
    retained = store.runtime(sample_key)
    assert retained["capture_diagnostics"]["records"] == [failure]
    assert retained["errors"] == [] and retained["readiness"] == ["startup_backlog"]
    assert store.runtime(q.ACTIVE)["samples"] == 1 and capture._diagnostics == []
    assert store.head() == initial_head and store.status()["outcomes"] == 0
    capture.capture(MOMENT)
    assert store.runtime(sample_key) == retained and store.runtime(q.ACTIVE)["samples"] == 1
