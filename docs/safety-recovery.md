# Original-trader recovery and confirmed dry handover

PIP-29 composes the isolated safety stores through
`pipmind.safety.recovery_flow`. It records held recovery plans and explicit robot
assignments, fences recovery-aware dry operations through an independent authority,
and preserves original receipt attribution. It contains no SQL restore command,
robot loader, broker/model client, order transport, engine-resume operation, or
automatic account takeover.

This is an opt-in reference integration. The existing hosted app, SQL S0,
capacity, configuration, landing and runtime controls are unchanged. PIP-17's
library/load/owner UI is not wired to these APIs. PIP-30 venue transport and
applicable M4/operational qualification remain separate gates.

## Platform and authority prerequisites

Read [external authority and native identity](safety-authority.md),
[operational inventory](safety-checkpoints.md),
[held assignment storage](safety-recovery-store.md),
[dry safety integration](safety-integration.md), and
[session accounting](safety-sessions.md) before using this composition.

The supplied native incarnation adapter supports SQL Server engine editions 1–4
and Managed Instance edition 8. It reads native database GUID/recovery-fork
metadata and refuses Azure SQL Database edition 5, unknown platforms, missing
metadata and insufficient visibility. **The current hosted Azure SQL S0 database
is not supported by this recovery adapter.** There is no fallback to an
application-local marker, database name or newly generated token. Selecting and
qualifying a suitable hosted identity mechanism and independent authority is an
activation decision, not permission to migrate S0 or add a paid service.

The reference `SqlAuthorityStore` belongs outside the application's restore and
administrative failure domain. Its trusted connector and expected native
incarnation are explicit deployment inputs. A second database on the same
disposable CI server demonstrates the protocol; different database names do not
establish production independence. Never restore the authority alongside the
application. Authority recovery and pin replacement require their own trusted
procedure and are not implemented here.

All connectors must use trusted server/database identity, bounded driver
timeouts, fresh nonpooled connections and `autocommit=False`. Recovery-aware
`SqlLeaseStore` instances require an explicit application `incarnation` pin.
Native identity is checked before and after their transaction. External guards
hold the authority transaction lock through application commit acknowledgement;
this is not a distributed transaction or a SQL-to-venue send protocol.

## Backup and inventory are separate evidence

A complete native application-database backup preserves the mind, feeds, all
operational history, safety tables, assignment plans/transitions and attribution.
It must be produced, retained and independently verified by the trusted backup
workflow. This coordinator does not create, copy, hash or restore a backup file.
Its native backup reference is metadata, not proof that such a file exists.

The backup sidecar includes the exact complete safety inventory and a
`NativeBackupReference(backup_id, sha256, created_at, source_inventory_sha256)`.
The trusted workflow must verify actual backup bytes/identity and bind the
sidecar to the same backed-up checkpoint. Quiesce relevant writes or otherwise
establish that consistent relationship; do not combine a convenient inventory
from one time with a backup from another. Restored comparison refuses a mismatch.

The inventory covers precisely the 15 legacy safety tables declared in
[the inventory guide](safety-checkpoints.md): the exact lease resource, all account
sessions/observations, admission history and referenced books, reservations/count
floors, controls/overrides/suggestions/dispositions, and dry integration state/history.
It reads complete history with bounded memory, not a count-limited prefix.
It excludes mind/feed tables and new recovery/assignment/attribution metadata;
the complete native backup still preserves those. Separate exact assignment,
plan, attribution and native-incarnation checks remain necessary. A 15-table
digest cannot certify a full saved mind or replace a database backup.

## Explicit owner procedure

These are trusted owner operations, not model tools or public HTTP endpoints.
Actor IDs, robot references, source facts, backup identity and connector pins must
come from the trusted adapter. Supplying metadata alone does not authenticate an
owner or establish the provenance of a market snapshot.

1. **Establish the backup checkpoint and stop old authority.** Retain the complete
   native backup and matching sidecar. Before recovery or handover, explicitly
   fence the external authority using its exact current revision/epoch, an
   immutable operation ID, actor and reason. Wait for acknowledgement. Fencing
   rotates the epoch, preserves the assignment-generation high-water and enters
   `held`; old expected states cannot enter another recovery-aware guard. A guard
   already running may finish before the fence is acknowledged.
2. **Restore only the application database when restoring is required.** Use the
   independently verified complete native backup through trusted native tooling.
   Observe and explicitly bind the resulting application incarnation in a held
   external fence. If maintenance required a fence before the target incarnation
   was known, a further explicit compare-and-set fence can bind the observed
   target while remaining held. Never adopt a changed incarnation on startup.
   An in-place robot handover skips restore and uses the existing pinned database.
3. **Compare restored safety state before changing the lease.** Construct pinned
   lease/recovery stores and a `DryRecoveryCoordinator(authority, held_state,
   leases, recovery)`. For restoration, call
   `verify_restored(inventory, backup_reference)` before `rebind_held`. It validates
   the sidecar linkage and compares the entire restored safety inventory under
   local locks and the held external guard. The trusted backup workflow separately
   verifies full mind/feed/recovery history and backup bytes. This check does not
   acquire a usable old lease or treat restored authority as current.
4. **Explicitly rebind and acquire fresh ownership.** Call
   `rebind_held(authority, held_state, connect, previous_epoch)` only after the
   acknowledged fence and required restore checks. It validates identity, changes
   the application lease epoch, advances its generation and clears its old owner,
   token hash and expiry. It does not reset any account data, kill/pause control,
   loss latch, reservation, pending obligation or history. Exact replay preserves
   the current row. Separately acquire a new lease using the held epoch and pinned
   application incarnation; an old restored grant is never reused.
5. **Prepare the exact proposed assignment.** Initialize recovery storage only
   where explicitly required; repeated initialization preserves existing state.
   Call `prepare(grant, plan_id=..., actor_id=..., expected_generation=...,
   target_robot=RobotReference(...), reason=..., position_obligation_ids=...)`.
   It captures the complete locked safety inventory and stores a full immutable
   plan binding its digest, current assignment generation, target robot/version/
   content hash, held epoch, application-incarnation hash, owner and exact ordered
   open-obligation identities. A `RobotReference` is opaque: this API neither loads
   its content nor certifies that a robot was installed.
6. **Review and confirm the complete plan hash.** Present the actual target,
   account, robot content identity, expected generation, checkpoint, obligations
   and reason to the owner. Pass the returned plan ID and exact digest to
   `confirm(grant, plan_id, digest)`. Under the external guard and same application
   transaction it recomputes the inventory, validates the plan/incarnation and
   compare-and-swaps the assignment generation. Any source, control or lease
   change that changes inventory requires a newly prepared plan; do not truncate
   history, silently update a plan, renew a lease unnoticed, or bypass a mismatch.
   Confirmation records assignment metadata only and leaves recovery held.
7. **Activate dry routing separately with fresh facts.** Supply the confirmed
   assignment and a complete current `AdmissionRequest` to
   `activate_dry(gateway, attribution, grant, assignment, request)`. The exact
   observed obligation identities must match the confirmed plan. Its
   recovery-aware wrapper validates the current assignment and native ownership,
   runs full fresh positive safety reconciliation and records immutable
   attribution in the application transaction. Only after that transaction is
   acknowledged does the held external guard stage activation for the strictly
   newer assignment generation and robot hash. Wait for external acknowledgement
   too. Failed, stale, incomplete or ambiguous facts leave no dry activation.
8. **Use only the recovery-aware wrapper for subsequent dry operations.** Construct
   `RecoveryAwareDryGateway` with the acknowledged active external state, exact
   assignment, gateway, recovery store and attribution store. Every `reconcile`,
   `accept` and `check` enters that exact external guard and retains it through
   the application transaction. The legacy PIP-28 gateway remains independently
   dry; it is not a recovery-aware execution boundary or a permitted bypass.

The assignment-generation high-water is preserved outside application restore.
If a stale backup cannot produce a confirmed generation above that high-water,
activation refuses. Do not reset the external store or manufacture intermediate
assignments to catch up. Resolve the missing/old checkpoint through a separately
reviewed recovery procedure while remaining held.

`RecoveryState.held` remains true even after assignment confirmation; the external
authority's state governs later dry routing. `Assignment.activated_at` is the SQL
confirmation timestamp, not proof of engine resume or execution. `activate_dry`
returns `dispatch_authorized=False` and `engine_resumed=False`. The existing
`SqlMind` pause state is unchanged, and owner kill/pause controls are preserved;
clearing a control requires its own explicit authorized action. No API sends,
replays, flattens or cancels an order. Pending/ambiguous venue state and unsupported
protection shapes cannot be waved through by an assignment or robot reference.

## Locking, history and attribution

The composition orders locks as external authority → lease → session → controls
→ recovery → integration → admission → policy book. Inventory checks caller-owned
locks; same-cursor assignment and attribution operations neither open another
application connection nor commit. The external guard remains open until the
application commit has returned. Plan preparation uses separate acknowledged
application operations under the same external held guard; confirmation rechecks
the exact complete inventory before its transition.

Recovery plans, assignment transitions, fence operations and dry gateway records
are immutable and exact-ID replays return their original evidence. Existing
receipt-attribution links preserve their original assignment, robot identity and
request/receipt/scope hashes across handover; a replay is not relabelled as a new
robot decision. New records receive the currently validated assignment in the
same application transaction. Missing or conflicting evidence refuses rather
than inventing old attribution. A historical receipt remains historical even
when retrieved under a new owner, and every gateway response remains dry.

## Failure and cross-store uncertainty

The application database and external authority have separate commits. There is
no universal rollback promise across them. If application work committed and
external activation later failed, external rollback cannot erase the application
reconciliation/attribution. A generic outer operation error can also occur after
an inner acknowledged commit; it does not establish that nothing persisted.

| Result | Required interpretation |
| --- | --- |
| Fixed validation, stale generation/epoch/incarnation or checkpoint mismatch | Refused or held; inspect exact current state and prepare a new authorized plan if appropriate |
| `LeaseCommitUnknown` | Application commit may have persisted; preserve uncertainty and reconcile exact IDs and retained records |
| `RecoveryCommitUnknown` from a direct store operation | Its plan/transition commit may have persisted; inspect the exact ID before further owner action |
| `AuthorityCommitUnknown` | External activation/fence may have persisted; reopen the pinned authority and reconcile exact state, plan and application results |
| Native backup/inventory mismatch, missing provider evidence or unsupported platform | Remain held; no fallback identity, partial restore or automatic reset |

Do not automatically retry a pending order, assume a lost acknowledgement means
rollback, or infer send authority from an active state read after uncertainty.
Reconcile both stores and the independently retained checkpoint/backup identities.
If a new fence is required, it is an explicit owner action against fresh expected
state. New ownership never erases pending budgets, losses, kill controls or old
robot outcomes.

## Verification and remaining deployment gates

Offline tests establish local codecs, inventory completeness/streaming, metadata
CAS, attribution and protocol counterexamples using synthetic inputs. Required
disposable SQL tests must additionally demonstrate native full-database
backup/restore, exact retained history, restored-owner refusal, real row/application
locks, concurrent fences/assignments, staged rollback, interruption/disconnect,
independent application/external commit uncertainty and immutable replay.
Passing offline tests alone is not evidence those SQL cases ran.

No hosted recovery, Azure SQL S0 compatibility, production authority independence,
actual robot loading, full FX-day qualification or permission to trade is claimed
by this code. Select and qualify those deployment/operational capabilities with
the owner before activation; preserve the current hosted app in the meantime.
