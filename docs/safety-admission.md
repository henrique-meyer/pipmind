# Offline SQL admission reservations

PIP-46 composes the numerical preflight, read-only reconciliation, durable session
accounting and SQL ownership lease. It records private open-proposal assessments
and reservations. It has no order transport, outbox worker, owner HTTP route,
shadow-runtime wiring or dispatch capability. `dispatch_authorized` is always
false. M4 qualification, owner risk choices, recovery and demo-order permission
remain separate gates.

## Inputs and resource accounting

`pipmind.safety.admission` defines a typed `AdmissionRequest`, explicit position
and pending-reservation marks, `ReconciliationEvidence`, immutable `Reservation`
projections and named `Assessment` results. Inputs are frozen through exact-type
canonical codecs before SQL. Raw floats, custom serialization hooks, unknown
fields, noncanonical/corrupt JSON and over-bound values refuse. Decimal source
representations and original strings remain in the immutable encoded request.

The explicit, sorted policy book has unique instruments, one account/environment/
currency/policy identity and common global caps. Its exact canonical bytes and
digest bind the session. No risk numbers, units, conversion rates, session
boundaries or freshness defaults are selected for the owner.

Assessment recomputes reconciliation from supplied complete snapshot, command and
protection scopes; a caller-created all-clear flag is insufficient. Distinct
held positions require exact identities, fresh liquidation-side marks, explicit
unit factors/conversions and matching full-size broker-observed working stops.
Those stops must use modify/close actions, opposite side, and explicit
`Decimal(0)` filled quantity; missing/partial fills and open orders with attached
stops are unsupported. Matching wanted/observed records alone is insufficient.
Binding, completeness and source hashes are still caller-provided evidence,
not independent venue attestation. Pending non-protective venue opens remain
unsupported. Required missing, stale, future, incomplete or mismatched facts
produce explicit refusals; protected long/short and valid flat positive controls
must also pass.

Every existing reservation requires a fresh own-instrument mark and conversion.
Immutable SQL geometry/quantity is used; marks cannot shrink it. Gross, planned
loss and margin charges are the greater of retained budgets and the specified
fresh revaluation. Opposite directions never net. Reservations from old sessions
still consume exposure, loss and margin; only current-session reserved opens add
to this session's order count. Reserved loss is not realized daily loss. The SQL
session supplies current opening equity/flow/latch; request fields cannot clear
it. The external order-count floor advances only from a validated coherent source;
later ordinary risk refusals can preserve that validated count, while invalid
sources cannot poison the floor.

Arithmetic uses the existing exact Decimal preflight and isolated trapped
arithmetic for combined reservation charges. Declared entry/stop/margin budgets
do not guarantee fills, losses, broker representability or execution-time prices.
There is no smaller substitute order, implicit rounding or automatic retry.

## SQL adapter and transaction boundary

Call `pipmind.safety.admission_sql.bootstrap(connect)` explicitly to create only
the isolated `pm_safety_admission_*_v1` tables:

- `accounts`: permanent exact account/environment/currency pin;
- `books`: immutable complete canonical policy books keyed by digest;
- `journal`: immutable exact requests, receipts and hashes, linked to session/book;
- `reservations`: small immutable accepted geometry/budget projections;
- `floors`: monotonic external counts for each explicit account/session.

No constructor/read implicitly creates schema or alters shared migrations.
`SqlAdmissionStore(connect, leases, sessions, policies)` validates and freezes its
book locally. Its three connectors must target the **same physical database**;
that is trusted configuration, not something the no-SQL constructor attests.

`admit(grant, request)` freezes the request first, then uses only
`leases.compare_and_act` and its cursor. It opens no independent session or own
connection. Lock order is **lease -> session(account) -> admission(account) -> book**.
The final book lock serializes immutable book insertion/verification. No book
operation acquires an earlier lock afterward. Account keys exclude currency;
changing currency cannot create a separate reservation namespace.

Within that transaction it reads the actual session, checks original-ID replay,
loads every account-wide unresolved projection and this session's count floor,
and assesses against aware UTC time read from SQL `SYSUTCDATETIME()`. It retains
the exact request/book and a receipt containing named assessment, checkpoint
provenance, policy digest, lease authority/owner/generation/resource and server
observation time. Raw lease tokens are never stored or returned. Positive
admission additionally stages the reservation; validated counts update the
monotonic floor. All bounds are checked before journal/budget writes.

After staging a positive admission, assessment is repeated using another SQL
time against the same frozen checkpoint and pre-admission reservation book. If
facts expired, all staged work rolls back. The lease then performs its own final
identity/expiry check before commit. No receipt escapes until commit is
acknowledged. Missing/corrupt session state aborts; it never produces a fabricated
healthy session or special minimal receipt.

Request identity is `order.client_order_id`, unique across sessions within the
account/environment. An exact request/session/book replay returns the original
receipt before freshness/latch reassessment, under a current valid lease. Changed
envelopes/bodies conflict. That conflict is returned internally as an identity
sentinel through the lease's final checks and acknowledged no-write transaction,
then raised as `AdmissionRefused("conflicting_admission_request")`. A lost commit
acknowledgement prevents even that conflict verdict from escaping as certain.

`lookup(request_id)` returns the exact original private receipt or null.
`snapshot()` returns `(tuple_of_account_wide_reservations, external_count_floor)`
for the constructor's explicit session. These acquire only the admission lock
(and book lock when verifying a journal reference); they never acquire session/
lease locks or assert freshness/authority. An empty result is an observation,
not an authorization. Missing account pins with orphaned rows fail as corruption.

## Bounds, failures and remaining integration

The technical request bound is **1 MiB**, allowing up to 1,000 fresh reservation
marks. Receipts remain 256 KiB, full policy books 64 KiB / 128 policies and each
reservation projection 4 KiB. At most 1,000 unresolved projections are admitted;
queries fetch up to 1,001 to detect overflow and never certify a prefix as the
complete book. Per-row SQL UTF-16 guards precede exact UTF-8 codec checks. Full
journal/history is not capped or truncated. These new module bounds do not change
existing HTTP, prompt, model-call or experience-export limits.

Fresh, non-pooled, autocommit-disabled connections are required. Disconnect
disposes uncommitted transactions; no potentially unbounded pytds rollback
handshake is added. Native lease refusals and commit uncertainty retain their
existing types. Inside the lease callback, exceptions are normalized by the
existing lease API to fixed `LeaseUnavailable("lease_store_unavailable")`; direct
lookup/snapshot storage failures use fixed `admission_store_unavailable`. Driver
text and raw request values never become error messages.

Any commit exception/interruption remains `LeaseCommitUnknown`, not an ordinary
refusal, rollback proof or permission to replay. Reopen/lookup and deliberately
retry only through the stable ID/body idempotency gate when reconciling durable
state. A persisted admission receipt is not a broker command outcome.

There is no reservation release, expiry, cancellation, cleanup or session-reset
API. Terminal broker reconciliation and release are separate work; pending and
ambiguous resources remain charged across restart/session change. Database
restore still requires trusted external authority renewal, old-owner fencing and
broker/pending-obligation reconciliation. PIP-29 recovery/handover, PIP-30 demo
transport and PIP-31 learning are not implemented here; PIP-17 shared state is
untouched.

Offline tests use actual assessment/lease/session code with transaction doubles.
Required disposable SQL CI covers funded concurrent positives and genuine combined
gross/loss/count/margin limits, staged expiry/error/interruption/disconnect rollback,
a racing session latch, owner-generation changes retaining pending budgets,
immutable lookup/retry and lost commit acknowledgement. No local
SQL, model, broker or provider call is implied by passing the offline suites.
