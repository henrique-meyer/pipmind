# Engine operation diagnostics

`ShadowEngine.step()` retains its return values, exceptions, configured clock,
existing observer API and processing sequence. A private in-process observer
measures fixed operation boundaries: `step`, `prepare`, `evidence`, `triage`,
`decision`, `memory_repair`, `hold` and `commit`. An `other` bucket prevents
unrecognized instrumentation names from becoming dynamic log fields.

These are monotonic **operation wall times**, including waits and local work.
Model-operation intervals can include projection, credentials, tools, SDK
processing and cleanup; they are not inference-server latency or RPC counts.
Preparation/evidence/commit include connection, lock, transport and local work;
they are not SQL execution time. The inclusive `step` total also contains
validation and unclassified engine work. Do not add it to the nested operation
totals. Normal completion includes idle/held returns and committed refusals; it
is not a successful decision, source-health or trading-quality score.

Source, gateway and clock polling, worker dispatch and service scheduling occur
outside this engine and remain unmeasured. These partial timings cannot explain
an earlier capture retroactively or establish sustained backlog drain, a full-day
start estimate, cost, readiness, complete coverage or M4 qualification.

At most one best-effort `shadow_step_diagnostics` line is attempted per 60 seconds,
on completion/failure/cancellation of an engine step; the first step may emit
immediately. There is no timer, endpoint, SQL write, extra provider/model request
or telemetry resource. The full stderr log line is limited to 8 KiB; stdout remains reserved for
existing CLI output. It contains only
fixed stage counts/timing aggregates, safe allowlisted failure codes, a fresh
anonymous process token and a valid build hash or null. No exception text,
traceback, event/trader/account IDs, prompt, context or token/cost data is logged.

Each stage has attempt, normal-completion, failure, cancellation, timed and
unmeasured counts plus total/maximum nanoseconds. Counters saturate explicitly at
signed-64-bit maximum. Up to eight failure records are retained in memory;
dropped records, rate-suppressed emissions, failed emissions and clock/telemetry
errors remain explicit. Failed or backwards clocks produce unmeasured intervals.
A failed commit, including cancellation, is `commit_unconfirmed`: the diagnostics
cannot determine whether it committed and never label it rolled back or retry it.
The original exception/control flow and durable source/audit remain authoritative.

Clock, formatting and stderr errors are isolated from application processing.
An emission attempt that fails still consumes its rate slot. Aggregates are not
durable and reset with a new process token after restart; lost/unemitted prior
process details remain unknown. Absence of a line does not establish health or
absence of failures. Deployment and a later bounded observation are separate
owner-controlled steps; code/tests alone do not qualify hosted throughput.
