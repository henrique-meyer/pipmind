# Durable safety-session accounting

PIP-45 adds an isolated session-accounting primitive. It does not connect to the
shadow mind, owner controls, gateway, lease or order path. It neither chooses risk
limits nor grants dispatch authority. Explicit synthetic policies are enough for
offline tests; deployment/activation still requires approved owner limits,
session boundaries and trustworthy account/cash-flow evidence.

## Explicit session and observation facts

`pipmind.safety.session` defines frozen `SessionBinding`, `SessionSpec`,
`EquityObservation`, `SessionState` and `ObservationReceipt` values. A binding
contains an account ID, demo/live environment and account currency. A session
specifies a stable session ID, policy ID/hash, opening equity and its evidence,
UTC start/end, maximum daily loss and snapshot-age limit. No local midnight,
time-zone or risk defaults are supplied.

Each equity observation provides its own stable ID, original UTC observation and
receipt times, equity, **cumulative external net cash flow since session start**,
account/cash-flow completeness and corresponding evidence/coverage. A repeated
cash-flow checkpoint is not an incremental deposit. Complete coverage must run
from the session start through the observation instant. Evidence identifiers and
completeness flags are caller-supplied provenance, not independent attestation.

For a complete coherent observation the exact loss is
`max(0, opening_equity + external_net_cash_flow - equity)`.
Adjusted opening equity must remain positive. A loss at or above the explicitly
supplied stop latches the session. Later equity recovery never clears that latch.
An accepted older complete breach also latches it without replacing the newer
effective checkpoint. Incomplete facts cannot establish a mathematically
unsupported loss; a newer incomplete observation nevertheless replaces the
effective checkpoint and keeps current risk facts unavailable.

Accepted observations advance the revision, including historical audit-only
observations. `SessionState.daily_loss` belongs to its latest effective
observation. A receipt's `daily_loss` belongs to **that submitted observation**
(null when incomplete); its latch records the resulting session latch.
`require_current(state, *, now)` separately checks the session interval,
completeness, coherence and freshness. It returns a latched state so the caller
can retain its loss refusal; it never declares authorization or clears a stop.

All prices/amounts use finite Decimal values within the documented pure-model
domain, without converting through float or changing the ambient Decimal
context. Timestamp comparisons use UTC instants with microsecond precision, while
canonical payloads preserve their original strings. Canonical codecs retain
Decimal coefficient/exponent/negative zero through strings and admit at most
32 KiB UTF-8 per spec, observation, state or receipt. This is a technical bound,
not an owner-selected numerical risk value. Invalid or oversized inputs refuse;
there is no clipping of account facts or audit history.

## SQL API and table boundaries

`pipmind.safety.session_sql.bootstrap(connect)` explicitly creates only:

- `pm_safety_session_accounts_v1`: exact account/environment binding and pinned
  currency;
- `pm_safety_session_sessions_v1`: exact canonical spec/state, their hashes and
  typed session interval;
- `pm_safety_session_observations_v1`: append-only accepted observation bodies,
  original receipts and their hashes, with ID and instant uniqueness.

DDL is serialized by a dedicated transaction-owned SQL application lock. No
constructor, read or observe operation bootstraps or changes shared migrations.
Construct `SqlSessionStore(connect, binding, session_id)` without SQL, then use:

- `initialize(spec) -> SessionState`: pin the account currency and create an
  absent non-overlapping session. Identical initialization returns its current
  state, preserving the latch and history; changed same-ID specs refuse.
- `observe(observation, *, now) -> ObservationReceipt`: validate against the
  stored spec and append/update atomically. An identical ID/body retry returns
  its original receipt without writes; an altered ID payload conflicts. A
  different ID at the same UTC instant conflicts across the entire retained
  session, including older rows, rather than silently deduplicating it.
- `read() -> SessionState`: return one bounded verified state without implicitly
  asserting freshness, completeness or an all-clear.
- `read_in_transaction(cursor) -> SessionState`: use the caller's existing SQL
  transaction and cursor, acquiring the same account lock. It opens no connection,
  commits nothing, changes no isolation and performs no freshness check. It
  checks `@@TRANCOUNT >= 1` and refuses detectable autocommit usage. This trusted
  internal API is not a transaction/network sandbox.

Every operation uses an exclusive transaction-owned lock named
`pipmind-safety-session-v1:<account_key>`. `account_key` hashes only the exact
account/environment, **excluding currency**, so changing currency cannot bypass
the account pin or overlap guard. Session intervals are half-open: adjacent
intervals are allowed; overlapping intervals refuse. New sessions never delete
prior history, clear another session's latch or release another module's pending
obligations. There is no reset/close/rollover/release API.

The reviewed future lock order is **lease -> session(account) -> admission(account)**.
Session writers never acquire lease or admission locks. Future admission must use
the same-cursor reader, not open a nested independent session transaction from a
lease callback. An older successful read is evidence, not a reusable authorization
for a later or changed account checkpoint.

Reads transfer a single projected spec/state or observation/receipt, with a
64 KiB UTF-16 SQL preguard followed by the exact 32 KiB UTF-8 codec guard. They do
not load or truncate the complete audit. Stored hash, canonical-codec, binding
and interval disagreements fail as unavailable state, never a healthy/default
session. Normal reads and duplicate retries perform no account-state writes.

## Failure and recovery boundary

Connections must be fresh, non-pooled and `autocommit=False`. The adapter disposes
each connection on every outcome; disconnect aborts uncommitted work. It follows
the pinned pytds disposal pattern rather than issuing a rollback handshake that
removes the transport timeout and can block on broken I/O.

Domain refusals have fixed `SessionRefused` codes, including binding mismatch,
missing session, changed initialization/observation, instant collision and
overlap. SQL/corrupt-record failures become
`SessionUnavailable("session_store_unavailable")`, without driver text or raw
payloads. No unavailable/default state can be used for current risk facts.

Any exception or interruption once commit is attempted becomes
`SessionCommitUnknown("session_commit_unknown")`; the database may have committed.
No result or automatic replay is returned. Reopen/read or retry an identical
stable observation ID through the durable idempotency check only as a deliberate
reconciliation step. A lookup/receipt does not establish a broker outcome.
Pre-commit non-Exception interruptions propagate after disposal; cleanup failure
cannot replace the original refusal or commit uncertainty.

This account state is excluded from robot imports/learned policy. Database restore
can rewind it and the separate SQL lease counter. External trusted authority
renewal, old-owner fencing and current broker/pending-obligation reconciliation
remain mandatory recovery integration, outside this module. Nothing here
implements PIP-29 original-trader recovery/handover, PIP-30 demo transport or
PIP-31 learning, and there is no hosted activation or order permission.

## Verification

The pure accounting suites exercise cumulative cash flow, incomplete/late facts,
exact boundary arithmetic, chronology and codec preservation. The SQL-store unit
suite uses transactional doubles only. Required actual SQL CI additionally proves
concurrent overlap/idempotency/time-conflict behavior, account pinning, interrupted
append rollback, acknowledged commit with lost acknowledgement, same-cursor lease
composition, persistence and bounded corruption refusal. Local offline success
does not claim SQL CI, provider truth, production readiness or a qualified FX day.
