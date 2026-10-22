# External SQL authority and native database incarnation

PIP-29 adds a reference authority adapter in `pipmind.safety.authority_sql` and
native identity observation in `pipmind.safety.incarnation`. Neither module is
connected to the hosted application or an order transport. They select no
production authority service, provision no resources and grant no trading access.

## Native identity and supported platforms

`DatabaseIncarnation(server, database, database_guid, recovery_fork_guid)` is a
frozen value with bounded exact text and canonical nonzero UUID strings.
`read_incarnation(cursor)` reads SQL Server native `SERVERPROPERTY('ServerName')`,
`DB_NAME()` and the `database_guid` / `recovery_fork_guid` fields from
`sys.database_recovery_status` for `DB_ID()`. It creates no connection and commits
nothing. It explicitly permits SQL Server engine editions 1–4 and Managed
Instance edition 8; missing metadata, insufficient visibility, invalid GUIDs,
Azure SQL Database edition 5 and unknown platforms fail closed. There is no
fallback to a database name, application table or random application-local marker.

Microsoft documents this catalog view for SQL Server and Azure SQL Managed
Instance, and states that restoring to a database that does not yet exist creates
a new database GUID. Copy/attach can duplicate GUIDs. See the
[official catalog documentation](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-database-recovery-status-transact-sql).
These observations do not prove every restore changes every identity component,
nor authenticate an arbitrary server against a hostile endpoint. Use trusted
connector identity and explicit independent fencing after recovery.

**The hosted Azure SQL S0 deployment is not supported by this native adapter.**
A suitable production incarnation mechanism and an independently protected
external-authority deployment remain open activation gates. This reference
implementation and disposable SQL Server tests do not authorize replacing S0,
adding capacity or creating another service.

## Explicit configuration and independent restore domain

`SqlAuthorityStore(connect, binding, expected_incarnation)` accepts an exact
`LeaseBinding(account_id, environment, resource)` and a trusted, preconfigured
native incarnation pin for the authority database. Its constructor performs no
SQL and never learns or adopts that pin from the current database. Explicit
`bootstrap(connect, expected_incarnation)` creates only isolated
`pm_safety_authority_v1`, `pm_safety_authority_fences_v1` and
`pm_safety_authority_barriers_v1` tables, under the same native pin checks.

Every transaction uses a fresh non-pooled `autocommit=False` connection,
serializable isolation, a transaction-owned exclusive account/resource application
lock, and native incarnation comparison before and after its work. A changed
configured authority database GUID/fork refuses; it is never automatically
reinitialized or trusted. Connectors must provide bounded driver timeouts and
trusted server/database identity. Pinned pytds rollback-handshake behavior is
avoided by disposing fresh uncommitted connections.

The authority must live outside the application database's restore and
administrative failure domain. Equal server/database locations (case-insensitive
conservative comparison) or equal database GUIDs refuse initialization/fencing.
Distinct names or GUIDs alone do not establish independent operational protection.
The disposable two-database CI layout demonstrates protocol behavior only; it is
not a production independence claim. Restoring authority together with the
application can rewind authority and invalidate the guarantee. Authority recovery,
its own trusted pin replacement and backup administration require a separate
trusted operational procedure; no automatic authority recovery/reset exists.

## Held state, fencing and activation

`initialize(application_incarnation)` explicitly creates revision 0, a fresh
256-bit random epoch, mode `held`, no active assignment/robot and assignment
generation high-water 0. Its paired `source_incarnation` / `source_epoch` fields
start null. Repeating it returns existing state without clearing
active assignments or rotating authority. A different application incarnation
refuses until an explicit fence; startup cannot silently adopt a restored database.
`read()` returns exact frozen `AuthorityState` after acknowledged completion.
`encode_authority_state` / `decode_authority_state` preserve strict canonical
state facts for immutable references; a serialized state is not a live permit.

`fence(operation_id, actor_id, expected_revision, expected_epoch,
application_incarnation, reason)` performs a compare-and-set, increments revision,
rotates the epoch, binds the explicitly supplied application incarnation and
leaves mode `held`. It clears the active assignment ID/robot hash while preserving
the assignment-generation high-water. Fencing an active assignment also records a
required source application incarnation and the **new fence epoch**. Fencing an
already held state preserves that source pointer even if the target incarnation
changes. A barrier recorded before the active period cannot satisfy this new
fence identity. It retains the exact original operation,
before/after states, native application binding, server time and hashes in an
immutable receipt. Operation IDs are unique per authority binding. Exact retry
returns the original historical receipt, even if later activation advanced state;
changed bodies conflict. Neither historical success nor a returned receipt
unfences anything. Conflict also waits for acknowledged transaction completion.
All receipts set `historical_receipt_only=true` and `dispatch_authorized=false`.

`guard(expected_state, *, mode='active')` is a context manager. It requires exact
current binding, revision, epoch, mode, assignment, generation, robot hash and
application incarnation. With a healthy external connection it holds the authority
transaction lock through nested application SQL work and its commit. Independent
connection loss can release that lock while the application transaction is still
live; the durable barrier protocol below addresses this gap. Mode `held` must be requested
explicitly for recovery planning/confirmation. A fence waits for an existing
guard's external transaction to end, or returns the bounded lock-busy refusal.
Once fencing is acknowledged, old expected states cannot enter another guard.
An application transaction which already entered can nevertheless commit after
that acknowledgement if its separate external connection was lost. An external
fence alone therefore does not prove application quiescence.

The yielded guard exposes read-only `.state`. In a held guard,
`activate(assignment_id, assignment_generation, robot_sha256)` can stage activation
in the same epoch only with a strictly higher explicit generation and a valid
robot hash. It also requires verified durable barriers for the current target
incarnation/current epoch and, when present, the preserved source incarnation/
source-fence epoch. Missing or corrupt proof refuses. It increments revision and
returns the **pending** new state. It
updates `.state` but does not commit. A closed guard cannot activate, and merely
entering/exiting a held guard never activates. There is no generic caller SQL
callback, exposed cursor, default assignment, lease token or automatic unfence.
Python cannot sandbox trusted callers or make private attributes a security
boundary.

Activation validates authority structure and monotonic identity only. The calling
recovery coordinator must separately prove exact checkpoint/robot selection,
owner confirmation, current reconciliation, controls, pending obligations and
application-database incarnation before requesting it. The authority adapter
does not inspect robot content or claim that it was loaded. Recovery-aware
application leases must themselves enforce the native application incarnation;
a valid external state alone cannot detect which application database a caller
actually opened.

## Acknowledged application barriers

After the external fence is acknowledged, the trusted recovery root must obtain
an acknowledged application SQL lease rebind/barrier on the **same physical source
incarnation**. That application transaction takes the existing lease lock, waits
for any earlier application lease transaction to finish, clears the old holder
and advances its generation/epoch. It must verify the native application identity
before and after that work. No source barrier is inferred from an external lock,
a failed callback, a target database or a successful SQL connection.

Only after that application commit is acknowledged may the trusted root call
`guard.record_application_barrier(application_incarnation, generation)` inside a
held external guard for that same current application incarnation. The exact
nonnegative integer generation is the acknowledged application lease generation,
not an owner risk default. The adapter cannot independently observe another
database's commit; this is a deliberately restricted trusted-caller evidence API.

The method records an immutable canonical receipt keyed by authority binding,
barrier epoch and native-incarnation hash. It retains the exact binding, native
pin, lease generation, SQL time, barrier epoch and `acknowledged_under_epoch`.
When the guarded application is also the required source, it records the required
source-fence identity as well, even if a later explicit held fence returned to
that source under a newer epoch. The acknowledgement-under field preserves that
actual chronology instead of pretending the later drain occurred at the old fence.
The source pointer and current authority revision/state are not changed by
recording a barrier. Exact repeat keeps the first receipt when the supplied
acknowledged generation is at least the recorded generation; regression refuses.
All returned receipts remain provisional until the external context commit is
acknowledged, and they remain historical evidence rather than dispatch permits.

For a restore into a different database, rebinding **only the target** cannot
satisfy the source requirement. The trusted sequence is to fence/hold against the
old source, acknowledge its application lease barrier, then fence/hold against the
target and acknowledge its target barrier before activation. If authority was
already retargeted without draining the source, the source pointer persists:
explicitly fence back to the source, complete and record its barrier, then retarget
and record the target barrier. A source that is unavailable or cannot be proven
must remain held; a target barrier cannot erase or replace it. Any alternative
physical fencing mechanism requires a separately reviewed evidence protocol;
there is no bypass flag or automatic force activation here.

This barrier concerns the old risk-operation lease scope. It is not a global
freeze of unrelated feed/mind writes or a distributed SQL-to-broker transaction.
The calling coordinator must keep all recovery-aware risk operations behind the
external held/active checks. Arbitrary legacy callers and administrative restore
operations remain outside that trusted protocol. Native full backup must retain
all authority barrier rows in their independently protected authority domain;
restoring that authority alongside the application defeats the protection.

## Cross-store uncertainty

Return a positive result only after the outer authority context also exits
successfully. An external connection failure can release its lock before an
already running application commit; do not report the fence alone as quiescence.
The acknowledged source/target barrier gates apply before replacement activation.
There is no distributed transaction between those stores. If application commit
succeeds and later work fails, external rollback cannot roll back the application.
The coordinator must retain held/indeterminate recovery state and reconcile exact
IDs/checkpoints; it must not replay a pending order or claim universal rollback.

Inner `LeaseCommitUnknown` is preserved unchanged after external transaction
abort/disposal. Other inner `Exception` values normalize to fixed
`AuthorityUnavailable('authority_operation_failed')`, without private callback
text. That error does **not** prove application rollback. Pre-commit
`KeyboardInterrupt`/`SystemExit` propagate after disposal. Any exception or
interruption once the external commit was attempted becomes fixed
`AuthorityCommitUnknown('authority_commit_unknown')`, whether the commit actually
persisted or not. Cleanup errors cannot erase commit uncertainty. No automatic
retry is implemented. Reopen/read and explicit exact-ID reconciliation are
required; an active state read after uncertainty is not itself an execution permit.

## Verification boundary

Offline tests exercise native metadata/platform refusal, exact state codecs,
held activation, monotonic generations, stale guards, pre/post incarnation pins,
same-database rejection, immutable fence retries/conflicts, paired source-pointer
codecs, fresh post-active source requirements, held retarget preservation, missing/
corrupt barrier refusals, immutable barrier replay and source-return/target activation,
callback exceptions,
independent application-commit uncertainty, staged rollback and external commit
ambiguity. They use transactional SQL doubles, not a model or venue.
Disposable SQL CI must additionally prove actual row/application locks, competing
fences, fresh reopen, own-session disconnect/interruption rollback and lost commit
acknowledgements. Native BACKUP/RESTORE evidence belongs to the composed recovery
suite. No passing offline test establishes hosted deployment support, production
restore fencing, readiness to dispatch or completion of M4/M5.
