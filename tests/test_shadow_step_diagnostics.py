"""Operation diagnostics are bounded observations, never processing authority."""

import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy

import pytest

from pipmind.contracts import ContractError, stamp, utc
from pipmind.shadow.common import ShadowError
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.step_diagnostics import (
    CODES,
    EMIT_INTERVAL_NS,
    MAX_BYTES,
    MAX_COUNTER,
    MAX_FAILURES,
    STAGES,
    StepDiagnostics,
)

NOW = utc("2026-10-21T12:00:00Z")
SECRET = "private-prompt-account-trader-token-do-not-log"


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value


@pytest.fixture
def diagnostics():
    clock, records = Clock(), []
    return StepDiagnostics(clock=clock, emit=records.append), clock, records


def test_exact_monotonic_operation_aggregates_include_no_context(diagnostics):
    diag, clock, records = diagnostics
    with diag.operation("step"):
        clock.value = 2
        with diag.operation("prepare"):
            clock.value = 7
        clock.value = 11
    snapshot = diag.snapshot()
    assert snapshot["stages"]["prepare"] == {
        "attempts": 1,
        "completed": 1,
        "failed": 0,
        "cancelled": 0,
        "timed": 1,
        "unmeasured": 0,
        "wall_ns_total": 5,
        "wall_ns_max": 5,
    }
    assert snapshot["stages"]["step"]["wall_ns_total"] == 11
    assert set(snapshot["stages"]) == set(STAGES) and len(records) == 1
    captured = json.loads(records[0])
    assert captured == snapshot
    captured["stages"]["prepare"]["completed"] = 999
    assert diag.snapshot()["stages"]["prepare"]["completed"] == 1


@pytest.mark.parametrize(
    "error",
    [
        ShadowError("shadow_store_busy"),
        ShadowError(SECRET),
        ShadowError({"raw": SECRET}),
        ContractError(SECRET),
        ValueError(SECRET),
        TimeoutError(SECRET),
    ],
)
def test_failures_rethrow_exact_identity_and_only_allow_fixed_codes(diagnostics, error):
    diag, clock, records = diagnostics
    with pytest.raises(type(error)) as caught:
        with diag.operation("step"):
            with diag.operation("prepare"):
                clock.value = 5
                raise error
    assert caught.value is error
    body = records[0]
    assert SECRET not in body and "raw" not in body
    snapshot = json.loads(body)
    assert snapshot["stages"]["prepare"]["failed"] == 1
    assert snapshot["stages"]["step"]["failed"] == 1
    assert all(
        row["code"]
        in CODES | {"shadow_error_other", "contract_refused", "timeout", "operation_error"}
        for row in snapshot["failures"]
    )


def test_error_stringification_never_runs(diagnostics):
    diag, _, _ = diagnostics

    class Refusal(ShadowError):
        def __str__(self):
            raise AssertionError("diagnostics must not stringify")

    error = Refusal(SECRET)
    with pytest.raises(Refusal) as caught:
        with diag.operation("step"):
            raise error
    assert caught.value is error
    assert diag.snapshot()["failures"][0]["code"] == "shadow_error_other"


def test_unknown_stage_never_becomes_dynamic_output(diagnostics):
    diag, _, records = diagnostics
    with diag.operation("step"):
        with diag.operation(SECRET):
            pass
    assert SECRET not in records[0]
    assert diag.snapshot()["stages"]["other"]["completed"] == 1


def test_failure_ring_and_emission_rate_are_bounded_with_explicit_suppression(diagnostics):
    diag, clock, records = diagnostics
    for _ in range(MAX_FAILURES + 10):
        with pytest.raises(ShadowError):
            with diag.operation("step"):
                raise ShadowError("sql_unavailable")
    snapshot = diag.snapshot()
    assert len(snapshot["failures"]) == MAX_FAILURES and snapshot["dropped_failures"] == 10
    assert snapshot["suppressed_emissions"] == MAX_FAILURES + 9 and len(records) == 1
    clock.value = EMIT_INTERVAL_NS
    with diag.operation("step"):
        pass
    assert len(records) == 2 and len(records[-1].encode()) <= MAX_BYTES
    assert json.loads(records[-1])["dropped_failures"] == 10


@pytest.mark.parametrize(
    "problem", [RuntimeError(SECRET), asyncio.CancelledError(), KeyboardInterrupt()]
)
def test_emitter_failure_cannot_fail_or_cancel_success_or_replace_original_exception(problem):
    calls = []

    def broken(payload):
        calls.append(payload)
        raise problem

    diag = StepDiagnostics(clock=lambda: 0, emit=broken)
    with diag.operation("step"):
        pass
    error = ShadowError("sql_unavailable")
    with pytest.raises(ShadowError) as caught:
        with diag.operation("step"):
            raise error
    assert caught.value is error and len(calls) == 1
    assert diag.snapshot()["failed_emissions"] == 1 and diag.snapshot()["telemetry_errors"] == 1
    assert SECRET not in calls[0]


@pytest.mark.parametrize("clock", [lambda: None, lambda: True, lambda: -1, lambda: float("nan")])
def test_malformed_clock_marks_unmeasured_without_affecting_return(clock):
    diag = StepDiagnostics(clock=clock, emit=lambda value: pytest.fail("no valid emission clock"))
    with diag.operation("step"):
        pass
    row = diag.snapshot()["stages"]["step"]
    assert row["completed"] == 1 and row["unmeasured"] == 1 and row["timed"] == 0
    assert diag.snapshot()["telemetry_errors"] == 2


def test_raising_and_reversed_clocks_cannot_change_processing():
    def broken():
        raise RuntimeError(SECRET)

    diag = StepDiagnostics(clock=broken)
    with diag.operation("step"):
        pass
    values = iter([5, 1])
    records = []
    diag = StepDiagnostics(clock=lambda: next(values), emit=records.append)
    with diag.operation("step"):
        pass
    assert diag.snapshot()["stages"]["step"]["unmeasured"] == 1
    assert diag.snapshot()["stages"]["step"]["wall_ns_total"] == 0


def test_size_refusal_and_counter_saturation_are_explicit(diagnostics, monkeypatch):
    from pipmind.shadow import step_diagnostics as module

    diag, clock, records = diagnostics
    monkeypatch.setattr(module, "MAX_BYTES", 1)
    with diag.operation("step"):
        pass
    assert records == [] and diag.snapshot()["size_refused_emissions"] == 1
    row = diag._stages["prepare"]
    row["attempts"] = row["completed"] = row["timed"] = row["wall_ns_total"] = MAX_COUNTER
    with diag.operation("prepare"):
        clock.value = 7
    assert row["attempts"] == row["completed"] == row["wall_ns_total"] == MAX_COUNTER
    assert diag.snapshot()["counters_saturated"] is True


def test_restart_resets_in_memory_counts_and_marks_new_process(diagnostics):
    diag, _, _ = diagnostics
    with diag.operation("step"):
        pass
    restarted = StepDiagnostics()
    assert restarted.snapshot()["process"] != diag.snapshot()["process"]
    assert restarted.snapshot()["stages"]["step"]["attempts"] == 0


@pytest.mark.parametrize("build", [SECRET, "z" * 40, "a" * 41])
def test_build_is_sanitized_not_an_arbitrary_log_field(monkeypatch, build):
    monkeypatch.setenv("BUILD_SHA", build)
    assert StepDiagnostics().snapshot()["build"] is None


class Store:
    def __init__(self, failure=None, committed=False):
        self.failure, self.committed = failure, committed
        self.calls, self.history = [], []
        self.event = {
            "kind": "ClockEvent",
            "message_id": SECRET,
            "payload": {"reason": "heartbeat", "ts": stamp(NOW)},
        }
        self.context_data = {
            "revision": 1,
            "identity": SECRET,
            "working_memory": {"views": [], "waiting_for": [], "position_theses": []},
            "broker_snapshots": {},
            "feed_status": {},
        }

    def prepare_step(self, moment):
        self.calls.append("prepare")
        if self.failure and self.failure[0] == "prepare":
            raise self.failure[1]
        return deepcopy(self.event), deepcopy(self.context_data), None

    def context(self, *args, **kwargs):
        self.calls.append("evidence")
        if self.failure and self.failure[0] == "evidence":
            raise self.failure[1]
        return {"recent_journal": [], "retrieved_memories": []}

    def commit(self, event, revision, result, moment):
        self.calls.append("commit")
        if self.committed:
            self.history.append(deepcopy(result))
        if self.failure and self.failure[0] == "commit":
            raise self.failure[1]
        if not self.committed:
            self.history.append(deepcopy(result))

    def hold(self, *args):
        self.calls.append("hold")
        if self.failure and self.failure[0] == "hold":
            raise self.failure[1]


class Model:
    def __init__(self, store, *, failure=None, wake=True):
        self.store, self.failure, self.wake = store, failure, wake
        self.requests = []

    async def triage(self, event, context, store, moment):
        self.store.calls.append("triage")
        self.requests.append(deepcopy((event, context)))
        if self.failure and self.failure[0] == "triage":
            raise self.failure[1]
        return {"wake": self.wake, "summary": "Observed"}, {"phase": "triage"}

    async def decide(self, event, context, triage, store, moment):
        self.store.calls.append("decision")
        self.requests.append(deepcopy((event, context)))
        if self.failure and self.failure[0] == "decision":
            raise self.failure[1]
        return {
            "action": "hold",
            "reasoning": "Observed",
            "evidence_ids": [],
            "working_memory": {"views": [], "position_theses": [], "waiting_for": []},
            "tool_calls": [],
        }, {"phase": "decision"}


class NoDiagnostics:
    @contextmanager
    def operation(self, stage):
        yield


def engine(store, model=None, diagnostics=None):
    result = ShadowEngine(store, model or Model(store), clock=lambda: NOW)
    if diagnostics is not None:
        result._diagnostics = diagnostics
    return result


def test_engine_wrapper_preserves_exact_calls_requests_history_and_return(diagnostics):
    diag, _, records = diagnostics
    store = Store()
    model = Model(store)
    wrapped = engine(store, model, diag)
    assert asyncio.run(wrapped.step()) is True
    baseline_store = Store()
    baseline_model = Model(baseline_store)
    baseline = engine(baseline_store, baseline_model, NoDiagnostics())
    assert asyncio.run(baseline.step()) is True
    assert (
        store.calls
        == baseline_store.calls
        == ["prepare", "triage", "evidence", "decision", "commit"]
    )
    assert store.history == baseline_store.history and model.requests == baseline_model.requests
    assert SECRET not in records[0]
    assert all(
        diag.snapshot()["stages"][stage]["completed"] == 1
        for stage in ("step", "prepare", "evidence", "triage", "decision", "commit")
    )


@pytest.mark.parametrize("stage", ["prepare", "evidence", "commit"])
def test_escaping_store_error_keeps_exact_identity_and_has_stage(diagnostics, stage):
    diag, _, records = diagnostics
    error = ShadowError("shadow_store_transaction_failed")
    store = Store(failure=(stage, error))
    with pytest.raises(ShadowError) as caught:
        asyncio.run(engine(store, diagnostics=diag).step())
    assert caught.value is error and store.history == []
    assert any(
        row["stage"] == stage and row["code"] == "shadow_store_transaction_failed"
        for row in json.loads(records[0])["failures"]
    )


@pytest.mark.parametrize("committed", [False, True])
def test_failed_commit_ack_never_means_rollback_or_triggers_retry(diagnostics, committed):
    diag, _, records = diagnostics
    error = ShadowError("shadow_store_transaction_failed")
    store = Store(failure=("commit", error), committed=committed)
    with pytest.raises(ShadowError) as caught:
        asyncio.run(engine(store, diagnostics=diag).step())
    assert caught.value is error and store.calls.count("commit") == 1
    assert len(store.history) == int(committed)
    commit = next(row for row in json.loads(records[0])["failures"] if row["stage"] == "commit")
    assert commit["outcome"] == "commit_unconfirmed" and "rollback" not in records[0]


@pytest.mark.parametrize("stage", ["triage", "decision", "commit"])
def test_engine_cancellation_propagates_original_without_extra_calls_or_commit_claim(
    diagnostics, stage
):
    diag, _, records = diagnostics
    cancelled = asyncio.CancelledError()
    store = Store(failure=(stage, cancelled) if stage == "commit" else None)
    model = Model(store, failure=(stage, cancelled) if stage != "commit" else None)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(engine(store, model, diag).step())
    assert caught.value is cancelled and store.history == []
    assert store.calls.count(stage) == 1
    snapshot = json.loads(records[0])
    assert snapshot["stages"][stage]["cancelled"] == snapshot["stages"]["step"]["cancelled"] == 1
    if stage == "commit":
        assert (
            next(row for row in snapshot["failures"] if row["stage"] == "commit")["outcome"]
            == "commit_unconfirmed"
        )


def test_existing_hold_return_and_refusal_commit_remain_distinct(diagnostics):
    diag, _, _ = diagnostics
    store = Store()
    model = Model(store, failure=("triage", ShadowError("shadow_context_size")))
    assert asyncio.run(engine(store, model, diag).step()) is False
    assert store.calls == ["prepare", "triage", "hold"] and store.history == []
    assert diag.snapshot()["stages"]["hold"]["completed"] == 1
    store = Store()
    model = Model(store, failure=("triage", ContractError(SECRET)))
    assert asyncio.run(engine(store, model, NoDiagnostics()).step()) is True
    assert store.calls == ["prepare", "triage", "commit"]
    assert store.history[0]["status"] == "model_refused"


def test_broken_diagnostic_clock_and_emitter_leave_engine_success_exact():
    def fail(*args):
        raise RuntimeError(SECRET)

    store = Store()
    assert (
        asyncio.run(engine(store, diagnostics=StepDiagnostics(clock=fail, emit=fail)).step())
        is True
    )
    assert (
        store.calls == ["prepare", "triage", "evidence", "decision", "commit"]
        and len(store.history) == 1
    )


def test_worst_case_bounded_metadata_fits_full_log_line(diagnostics):
    from pipmind.shadow.step_diagnostics import PREFIX

    diag, _, records = diagnostics
    for row in diag._stages.values():
        row.update({key: MAX_COUNTER for key in row})
    diag._counts.update({key: MAX_COUNTER for key in diag._counts})
    diag._build = "a" * 40
    for _ in range(MAX_FAILURES):
        diag._failures.append(
            {
                "stage": "memory_repair",
                "code": "shadow_store_transaction_failed",
                "outcome": "commit_unconfirmed",
                "wall_ns": MAX_COUNTER,
            }
        )
    diag._publish(0)
    assert len(records) == 1 and len((PREFIX + records[0]).encode()) <= MAX_BYTES


def test_idle_return_keeps_false_and_does_not_call_model_or_commit(diagnostics):
    diag, _, _ = diagnostics
    store = Store()
    calls = []
    store.prepare_step = lambda moment: calls.append(moment)
    assert asyncio.run(engine(store, diagnostics=diag).step()) is False
    assert calls == [NOW] and store.calls == [] and store.history == []
    assert diag.snapshot()["stages"]["step"]["completed"] == 1
    assert diag.snapshot()["stages"]["commit"]["attempts"] == 0


def test_legacy_paused_prepare_keeps_original_early_return_and_clock_calls(diagnostics):
    from types import SimpleNamespace

    diag, _, _ = diagnostics
    calls = []
    store = SimpleNamespace(head=lambda: calls.append("head") or {"paused": True})

    def configured_clock():
        calls.append("clock")
        return NOW

    instance = ShadowEngine(store, object(), clock=configured_clock)
    instance._diagnostics = diag
    assert asyncio.run(instance.step()) is False
    assert calls == ["clock", "head"]
    assert diag.snapshot()["stages"]["prepare"]["completed"] == 1


def test_wrapper_preserves_return_identity_without_imposing_a_new_contract(diagnostics):
    diag, _, _ = diagnostics
    marker = object()

    class ExistingVariant(ShadowEngine):
        async def _step(self):
            return marker

    variant = ExistingVariant(None, None)
    variant._diagnostics = diag
    assert asyncio.run(variant.step()) is marker


def test_existing_refusal_commit_retry_stays_exactly_two_operations(diagnostics):
    diag, _, _ = diagnostics
    store = Store()
    original = store.commit
    attempts = []

    def commit(*args):
        attempts.append(True)
        if len(attempts) == 1:
            store.calls.append("commit")
            raise ShadowError("unavailable_shadow_evidence")
        original(*args)

    store.commit = commit
    assert asyncio.run(engine(store, diagnostics=diag).step()) is True
    assert store.calls == ["prepare", "triage", "evidence", "decision", "commit", "commit"]
    assert len(store.history) == 1 and store.history[0]["status"] == "model_refused"
    row = diag.snapshot()["stages"]["commit"]
    assert row["attempts"] == 2 and row["failed"] == row["completed"] == 1


def test_diagnostic_markers_never_change_existing_observer_api_or_exception(diagnostics):
    diag, _, _ = diagnostics
    seen = []
    error = RuntimeError(SECRET)

    def observer(name, facts):
        seen.append(name)
        if name == "ready":
            raise error

    instance = ShadowEngine(Store(), Model(Store()), clock=lambda: NOW, observer=observer)
    instance._diagnostics = diag
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(instance.step())
    assert caught.value is error and seen == ["preparation", "context", "ready"]


def test_real_engine_default_diagnostics_preserve_single_json_stdout(capsys):
    from pipmind.shadow.step_diagnostics import PREFIX

    store = Store()
    # Exercise the real default sink and engine, not a stubbed CLI evaluator.
    assert asyncio.run(engine(store).step()) is True
    print(json.dumps({"passed": True, "outcomes": len(store.history)}))
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"passed": True, "outcomes": 1}
    lines = captured.err.splitlines()
    assert len(lines) == 1 and lines[0].startswith(PREFIX)
    assert len(lines[0].encode()) <= MAX_BYTES
    diagnostics = json.loads(lines[0][len(PREFIX) :])
    assert diagnostics["stages"]["step"]["completed"] == 1
    assert SECRET not in captured.err
