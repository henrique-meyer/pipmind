"""Bounded in-process engine operation diagnostics; no authority or source writes."""

import asyncio
import json
import os
import re
import sys
from collections import deque
from contextlib import contextmanager
from time import monotonic_ns
from uuid import uuid4

from pipmind.contracts import ContractError
from pipmind.shadow.common import ShadowError

STAGES = (
    "step",
    "prepare",
    "evidence",
    "triage",
    "decision",
    "memory_repair",
    "hold",
    "commit",
    "other",
)
CODES = frozenset(
    {
        "shadow_store_busy",
        "sql_unavailable",
        "shadow_store_transaction_failed",
        "shadow_context_size",
        "shadow_context_projection",
        "working_memory_repair_failed",
        "working_memory_bound",
        "working_memory_policy",
        "unavailable_shadow_evidence",
        "future_shadow_memory",
        "shadow_revision_changed",
        "shadow_input_not_pending",
        "mind_not_initialized",
        "future_shadow_input",
        "model_error",
        "model_cancelled",
    }
)
MAX_COUNTER = 2**63 - 1
MAX_FAILURES = 8
MAX_BYTES = 8192
PREFIX = "shadow_step_diagnostics "
EMIT_INTERVAL_NS = 60_000_000_000


def emit_line(payload):
    print(PREFIX + payload, file=sys.stderr, flush=True)


def failure_code(error):
    """Never stringify exceptions, inspect traces or admit dynamic type names."""
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, ShadowError):
        try:
            args = error.args
            if (
                type(args) is tuple
                and len(args) == 1
                and type(args[0]) is str
                and len(args[0]) <= 64
                and args[0] in CODES
            ):
                return args[0]
        except BaseException:
            pass
        return "shadow_error_other"

    if isinstance(error, ContractError):
        return "contract_refused"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "operation_error"


class StepDiagnostics:
    """Single ordered engine's bounded aggregates; emission never changes its work."""

    def __init__(self, *, clock=monotonic_ns, emit=emit_line):
        self._clock, self._emit = clock, emit
        self._process = uuid4().hex
        build = os.environ.get("BUILD_SHA", "")
        self._build = build if re.fullmatch(r"[a-fA-F0-9]{40}", build) else None
        self._stages = {
            stage: {
                key: 0
                for key in (
                    "attempts",
                    "completed",
                    "failed",
                    "cancelled",
                    "timed",
                    "unmeasured",
                    "wall_ns_total",
                    "wall_ns_max",
                )
            }
            for stage in STAGES
        }
        self._failures = deque(maxlen=MAX_FAILURES)
        self._counts = {
            key: 0
            for key in (
                "telemetry_errors",
                "dropped_failures",
                "suppressed_emissions",
                "failed_emissions",
                "size_refused_emissions",
            )
        }
        self._saturated = False
        self._last_emit = None

    def _add(self, row, key, amount=1):
        value = row[key] + amount
        self._saturated |= value > MAX_COUNTER
        row[key] = min(value, MAX_COUNTER)

    def _telemetry_error(self):
        try:
            self._add(self._counts, "telemetry_errors")
        except BaseException:
            pass  # Even broken diagnostics cannot change application control flow.

    def _now(self):
        try:
            value = self._clock()
            if type(value) is not int or not 0 <= value <= MAX_COUNTER:
                raise ValueError
            return value
        except BaseException:
            self._telemetry_error()
            return None

    def _begin(self, stage):
        try:
            stage = stage if type(stage) is str and stage in STAGES else "other"
            self._add(self._stages[stage], "attempts")
            return stage, self._now()
        except BaseException:
            self._telemetry_error()
            return None

    def snapshot(self):
        """Private in-memory observation, not an HTTP API or readiness assertion."""
        return {
            "version": 1,
            "process": self._process,
            "build": self._build,
            "stages": {stage: dict(self._stages[stage]) for stage in STAGES},
            "failures": [dict(row) for row in self._failures],
            **self._counts,
            "counters_saturated": self._saturated,
        }

    def _publish(self, moment):
        if moment is None:
            return
        if self._last_emit is not None and moment - self._last_emit < EMIT_INTERVAL_NS:
            self._add(self._counts, "suppressed_emissions")
            return
        # Rate-limit attempts, including failed/partially written emissions.
        self._last_emit = moment
        try:
            payload = json.dumps(
                self.snapshot(), sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if len((PREFIX + payload).encode()) > MAX_BYTES:
                self._add(self._counts, "size_refused_emissions")
                return
            self._emit(payload)
        except BaseException:
            self._add(self._counts, "failed_emissions")
            self._telemetry_error()

    def _finish(self, token, error):
        try:
            if token is None:
                return
            stage, started = token
            moment = self._now()
            row = self._stages[stage]
            cancelled = isinstance(error, asyncio.CancelledError)
            outcome = "completed" if error is None else "cancelled" if cancelled else "failed"
            self._add(row, outcome)
            elapsed = None
            if started is not None and moment is not None and moment >= started:
                elapsed = moment - started
                self._add(row, "timed")
                self._add(row, "wall_ns_total", elapsed)
                row["wall_ns_max"] = max(row["wall_ns_max"], elapsed)
            else:
                self._add(row, "unmeasured")
            if error is not None:
                if len(self._failures) == MAX_FAILURES:
                    self._add(self._counts, "dropped_failures")
                self._failures.append(
                    {
                        "stage": stage,
                        "code": failure_code(error),
                        "outcome": "commit_unconfirmed" if stage == "commit" else outcome,
                        "wall_ns": elapsed,
                    }
                )
            if stage == "step":
                self._publish(moment)
        except BaseException:
            self._telemetry_error()

    @contextmanager
    def operation(self, stage):
        token = self._begin(stage)
        try:
            yield
        except BaseException as error:
            self._finish(token, error)
            raise
        else:
            self._finish(token, None)
