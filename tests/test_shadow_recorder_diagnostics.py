"""Recorder diagnostics add evidence without retries or qualification gate changes."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta

import pytest

from pipmind.contracts import loads, utc
from pipmind.shadow import qualification as q
from pipmind.shadow.common import ShadowError, encoded

MOMENT = utc("2026-10-20T12:00:00Z")


class Store:
    trader = "synthetic-recorder"

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.fail = None

    def runtime(self, key):
        self.calls.append(("runtime", key))
        if self.fail == "active_read":
            raise ShadowError("sql_unavailable")
        return deepcopy(self.rows.get(key))

    @contextmanager
    def transaction(self):
        self.calls.append(("transaction",))
        if self.fail == "capture_tx_enter":
            raise ShadowError("shadow_store_busy")
        cursor = Cursor(self, deepcopy(self.rows))
        yield cursor
        if self.fail == "commit_before":
            raise ShadowError("shadow_store_transaction_failed")
        self.rows = cursor.rows
        if self.fail == "commit_after":
            raise ShadowError("shadow_store_transaction_failed")

    def _runtime(self, cur, key, default=None):
        self.calls.append(("_runtime", key))
        return deepcopy(cur.rows.get(key, default))

    def _set_runtime(self, cur, key, value):
        self.calls.append(("_set_runtime", key))
        if self.fail == "active_write":
            raise ShadowError("shadow_store_transaction_failed")
        cur.rows[key] = deepcopy(value)


class Cursor:
    def __init__(self, store, rows):
        self.store, self.rows = store, rows
        self.value = None

    def execute(self, statement, parameters):
        self.store.calls.append(("execute", statement, parameters))
        if "SELECT MIN(" in statement:
            if self.store.fail == "pending_age_read":
                raise ShadowError("shadow_store_transaction_failed")
            self.value = None
        elif "SELECT COALESCE(MAX(pk)" in statement:
            self.value = 0
        elif "SELECT TOP(1001)" in statement:
            self.value = None
        elif "INSERT pm_runtime" in statement:
            if self.store.fail == "sample_insert":
                raise ShadowError("shadow_store_transaction_failed")
            _, key, raw = parameters
            assert key not in self.rows
            self.rows[key] = loads(raw)
        else:
            raise AssertionError("unexpected SQL request")

    def fetchone(self):
        return (self.value,)

    def __iter__(self):
        return iter(())


@pytest.fixture
def capture(monkeypatch):
    store = Store()
    q.arm(store, "diagnostic-run", ["EUR/USD"], "a" * 40, MOMENT)
    # Keep this a warmup with an existing readiness refusal; diagnostics cannot
    # make it start, and no broker/model call occurs in this synthetic fixture.
    monkeypatch.setattr(q, "healthy", lambda *args: ["startup_backlog"])
    monkeypatch.setenv("BUILD_SHA", "a" * 40)
    monkeypatch.setattr(q, "read_gateway", lambda *args: {})
    recorder = q.Recorder(store, None, lambda: {})
    store.calls.clear()
    return store, recorder


def sample(store, moment=MOMENT):
    key = "qualification:diagnostic-run:sample:" + str(int(moment.timestamp()) // 60)
    return store.rows[key]


@pytest.mark.parametrize(
    ("failure", "stage", "code"),
    [
        ("active_read", "active_read", "sql_unavailable"),
        ("capture_tx_enter", "capture_tx_enter", "shadow_store_busy"),
        ("pending_age_read", "pending_age_read", "shadow_store_transaction_failed"),
        ("sample_insert", "sample_insert", "shadow_store_transaction_failed"),
        ("active_write", "active_write", "shadow_store_transaction_failed"),
    ],
)
def test_failed_attempt_keeps_state_and_persists_exact_stage_on_next_ordinary_capture(
    capture, failure, stage, code
):
    store, recorder = capture
    original = deepcopy(store.rows)
    store.fail = failure
    with pytest.raises(ShadowError):
        recorder.capture(MOMENT)
    assert store.rows == original
    record = recorder._diagnostics[0]
    assert record["stage"] == stage and record["code"] == code
    assert record["attempt"] == 1 and record["outcome"] == "capture_failed"
    assert record["attempt_seconds"] >= 0 and record["stage_seconds"] >= 0
    if failure == "active_read":
        assert record["run"] is None and record["slot"] is None
    store.fail = None
    recorder.capture(MOMENT)
    assert sample(store)["capture_diagnostics"]["records"] == [record]
    assert sample(store)["errors"] == []
    assert sample(store)["readiness"] == ["startup_backlog"]
    assert store.rows[q.ACTIVE]["state"] == "warming"
    assert recorder._diagnostics == []


@pytest.mark.parametrize("commit", ["commit_before", "commit_after"])
def test_lost_commit_ack_retains_uncertainty_and_uses_only_existing_slot_guard(capture, commit):
    store, recorder = capture
    store.fail = commit
    with pytest.raises(ShadowError):
        recorder.capture(MOMENT)
    record = deepcopy(recorder._diagnostics[0])
    assert record["stage"] == "commit_ack" and record["outcome"] == "commit_unconfirmed"
    assert store.rows[q.ACTIVE]["samples"] == (1 if commit == "commit_after" else 0)
    store.fail = None
    recorder.capture(MOMENT)  # Existing ordinary polling behavior, not a new retry.
    assert store.rows[q.ACTIVE]["samples"] == 1
    if commit == "commit_after":
        assert recorder._diagnostics == [record]  # Same-slot no-op is not an ACK.
        recorder.capture(MOMENT + timedelta(minutes=1))
        retained = sample(store, MOMENT + timedelta(minutes=1))
    else:
        retained = sample(store)
    assert retained["capture_diagnostics"]["records"] == [record]
    assert recorder._diagnostics == []


@pytest.mark.parametrize("stage", ["shadow_status_read", "gateway_state_read", "feed_status_read"])
def test_caught_read_failure_preserves_legacy_errors_and_readiness(capture, monkeypatch, stage):
    store, recorder = capture

    def fail(*args):
        raise TimeoutError("secret credential must never appear in diagnostics")

    if stage == "shadow_status_read":
        recorder.state = fail
    elif stage == "gateway_state_read":
        monkeypatch.setattr(q, "read_gateway", fail)
    else:
        recorder.feed_state = fail
    recorder.capture(MOMENT)
    retained = sample(store)
    assert retained["errors"] == ["TimeoutError"]
    assert retained["readiness"] == ["startup_backlog", "sample_read_failed"]
    record = retained["capture_diagnostics"]["records"][0]
    assert record["stage"] == stage and record["outcome"] == "sample_read_failed"
    assert recorder.last_error is None and recorder._diagnostics == []


def test_fixed_codes_and_logger_never_retain_arbitrary_exception_text(capture, caplog):
    store, recorder = capture

    def fail():
        raise ShadowError("private-token-super-secret")

    recorder.state = fail
    recorder.capture(MOMENT)
    diagnostic = sample(store)["capture_diagnostics"]
    assert diagnostic["records"][0]["code"] == "shadow_error_other"
    assert "private-token-super-secret" not in encoded(diagnostic) + caplog.text
    assert "Traceback" not in caplog.text


def test_read_failure_queue_is_not_cleared_by_unacknowledged_sample_commit(capture):
    store, recorder = capture

    def unavailable():
        raise ShadowError("sql_unavailable")

    recorder.state = unavailable
    store.fail = "commit_before"
    with pytest.raises(ShadowError):
        recorder.capture(MOMENT)
    assert [row["code"] for row in recorder._diagnostics] == [
        "sql_unavailable",
        "shadow_store_transaction_failed",
    ]
    assert recorder._diagnostics[-1]["outcome"] == "commit_unconfirmed"
    assert store.rows[q.ACTIVE]["samples"] == 0


def test_monotonic_stage_and_attempt_durations_are_distinct(capture, monkeypatch):
    store, recorder = capture
    ticks = iter([100.0, 101.0, 103.5, 104.0])
    monkeypatch.setattr(q, "monotonic", lambda: next(ticks))
    store.fail = "active_read"
    with pytest.raises(ShadowError):
        recorder.capture(MOMENT)
    record = recorder._diagnostics[0]
    assert record["stage_seconds"] == 2.5 and record["attempt_seconds"] == 4.0


def test_existing_sample_size_bound_is_not_raised_or_evaded_for_diagnostics(capture):
    store, recorder = capture
    store.rows["feed_status"] = {"coverage": "x" * 2_097_152}
    initial = deepcopy(store.rows)
    with pytest.raises(ShadowError, match="^qualification_sample_bound$"):
        recorder.capture(MOMENT)
    assert store.rows == initial
    assert recorder._diagnostics[0]["code"] == "qualification_sample_bound"
    assert recorder._diagnostics[0]["stage"] == "sample_encode"
    assert len(encoded(recorder._diagnostic_snapshot()).encode()) <= 8192


def test_queue_is_bounded_and_overflow_is_explicit_without_touching_old_samples(capture):
    store, recorder = capture
    recorder.capture(MOMENT)
    first = deepcopy(sample(store))
    store.fail = "active_read"
    for _ in range(30):
        with pytest.raises(ShadowError):
            recorder.capture(MOMENT + timedelta(minutes=1))
    pending = recorder._diagnostic_snapshot()
    assert len(pending["records"]) <= 16 and len(encoded(pending).encode()) <= 8192
    assert pending["overflow"]["count"] == 14
    assert pending["overflow"]["first_at"] and pending["overflow"]["last_at"]
    assert sample(store) == first
    store.fail = None
    recorder.capture(MOMENT + timedelta(minutes=1))
    assert sample(store, MOMENT + timedelta(minutes=1))["capture_diagnostics"] == pending
    assert recorder._overflow["count"] == 0


def test_restart_explicitly_marks_prior_unpersisted_diagnostics_unknown(capture):
    store, first = capture
    store.fail = "active_read"
    with pytest.raises(ShadowError):
        first.capture(MOMENT)
    second = q.Recorder(store, None, lambda: {})
    store.fail = None
    second.capture(MOMENT)
    diagnostic = sample(store)["capture_diagnostics"]
    assert diagnostic["process"] != first._process
    assert diagnostic["prior_unpersisted_errors"] == "unknown_before_process_start"
    assert diagnostic["records"] == []  # No fabricated preservation across restart.


def test_diagnostics_add_no_store_or_provider_calls(capture, monkeypatch):
    store, recorder = capture
    provider_calls = []
    monkeypatch.setattr(q, "read_gateway", lambda *args: provider_calls.append(args[1]) or {})
    recorder.capture(MOMENT)
    assert provider_calls == ["/state", "/evidence"]
    assert sum(call[0] == "runtime" for call in store.calls) == 1
    assert sum(call[0] == "transaction" for call in store.calls) == 1
    assert sum(call[0] == "execute" for call in store.calls) == 3
    assert sum(call[0] == "_set_runtime" for call in store.calls) == 1


def test_run_preserves_exception_type_and_existing_ten_second_cadence(capture, monkeypatch):
    store, recorder = capture
    store.fail = "active_read"
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(q.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(recorder.run())
    assert recorder.last_error == "ShadowError" and sleeps == [10]
    assert len(recorder._diagnostics) == 1


def test_same_slot_noop_never_clears_pending_diagnostics_or_legacy_error(capture):
    store, recorder = capture
    recorder.capture(MOMENT)
    store.fail = "active_read"
    with pytest.raises(ShadowError):
        recorder.capture(MOMENT)
    recorder.last_error = "ShadowError"
    pending = recorder._diagnostic_snapshot()
    store.fail = None
    recorder.capture(MOMENT)
    assert recorder._diagnostic_snapshot() == pending
    assert recorder.last_error == "ShadowError" and store.rows[q.ACTIVE]["samples"] == 1
