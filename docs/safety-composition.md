# Composing admission in a trusted SQL lease transaction

`SqlAdmissionStore` now exposes same-cursor operations for a larger SQL-only
transaction. They do not open independent connections, invoke nested lease
callbacks, commit, close a connection, send orders or confer dispatch authority.
Existing `admit(grant, request)` retains its replay, refusal and uncertainty
behavior.

## Admission and acknowledgement

The composing caller must use the same physical SQL database and an existing
`leases.compare_and_act(grant, callback)` transaction. Acquire locks in order:
lease, session/account, optional controls and integration/account, admission/account,
then book. Calling the admission method reacquires the already held session lock;
it must not be used to acquire a different session after later locks. The existing
session reader checks transaction presence. Actual outer lease ownership,
transaction scope and trusted callback discipline remain caller obligations.
Python does not sandbox the callback or independently attest those obligations.

`admit_in_transaction(cursor, grant, request)` freezes the request locally and
performs the existing admission algorithm on that cursor. A new assessment,
reservation and validated count floor are staged together; positive admission
still reassesses expiry after staging. Exact replay returns the original receipt
before freshness reassessment. A changed request/session/book returns the identity
sentinel `ADMISSION_CONFLICT`. Neither receipt nor conflict is certain before the
outer lease's final identity/expiry check and acknowledged commit.

After the outer operation returns successfully, call
`store.finalize_admission(result)`. It raises the original
`AdmissionRefused("conflicting_admission_request")` for that sentinel and otherwise
returns the receipt. `store.is_conflict(result)` is an identity predicate for a
composing caller that needs to encode its own internal outcome. Do not finalize
inside the callback. These helpers cannot attest that commit was acknowledged.
Any commit error/interruption retains `LeaseCommitUnknown`; do not convert it into
a known refusal, successful receipt or automatic retry.

`admit()` freezes before obtaining the lease lock, delegates to the same-cursor
method, and finalizes only after acknowledgement. A composer that might wait or
await before entering SQL should similarly call `store.freeze_request(request)`
first. That helper returns independent canonical facts. Read-only properties
`leases`, `sessions`, `binding`, `session_id` and `policy_book_sha256` expose
constructor bindings; they do not assert database/provider authority.
`server_time_in_transaction(cursor)` reads UTC SQL time through the supplied cursor.

## Fresh preview and reserved-order recheck

`preview_in_transaction(cursor, request, *, recheck_request_id=None)` returns a
fresh `Assessment`, never an original admission receipt. It reads the actual
session, all account-wide reservations, current external count floor and SQL time.
It may create the account pin inside the caller's transaction, but writes no
journal, policy book, reservation or count floor. Its result is a point-in-time
assessment, not an execution permit or durable release. A composing operation
that needs a final time-sensitive check must explicitly call it at that point;
the outer lease check alone does not refresh source facts.

Without `recheck_request_id`, an already reserved ID fails fresh assessment rather
than returning the original successful receipt. With it, recheck requires a valid
accepted original journal entry, verified canonical body/receipt/projection,
constructor session and policy book, matching request ID, exactly the same
canonical OrderRequest and unchanged proposal instrument and exact Decimal unit
factor representation. Missing, corrupt, mismatched or refused originals fail
with fixed storage errors; a caller cannot exclude an arbitrary other obligation.

Only that verified original projection is removed from the assessment's temporary
book. Other reservations, including old-session reservations, remain charged.
The prospective proposal adds itself once. External source counts and the durable
floor are never decremented; this API assumes no executed-order lifecycle and
cannot infer that an external count includes this dry proposal.

Refreshed quote, conversion rate, margin and source/session evidence may change.
The original persisted geometry and budgets are never rewritten. To prevent a
replacement preview from discounting already reserved resources, an otherwise
positive candidate whose gross, planned-loss or margin budget is below its
original budget is refused with `recheck_budget_shrink`. This conservative first
slice does not implement budget release or a flexible replacement valuation API.
A refusal retains the fresh assessment's validated count and evidence fields,
but has no proposed reservation. Higher budgets must still pass every ordinary
combined risk/exposure/count/source check.

## Verification and limits

Offline composition tests run the actual pure assessment, session reader and
lease implementation over transactional SQL doubles. They exercise staged
visibility, one-cursor/no-independent-connection composition, acknowledged conflict,
commit uncertainty before/after durable effect, final lease expiry rollback,
stale successful replay versus fresh refusal, exact original exclusion, retained
other charges, identity/unit-factor mismatch, non-shrinking budgets and unchanged
external count floors. Existing admission-store regressions remain unchanged.
These are not evidence of real SQL lock scheduling, broker behavior, hosted
readiness or permission to trade. Disposable real SQL integration remains a
separate required CI check. No schema, order transport, reservation release,
runtime wiring or capacity change is introduced by these adapter methods.
