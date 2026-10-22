# Held recovery plans and assignment metadata

`pipmind.safety.recovery_sql` provides an isolated SQL journal for an owner's
confirmed recovery/handover plan. It performs no robot loading, order operation,
lease acquisition, reconciliation, account control change, or risk-policy change.
An opaque `RobotReference(robot_id, version, content_sha256)` is an explicit trusted
adapter boundary. Neither SQL approval nor that reference certifies that PIP-17
content was loaded or interpreted correctly.

Call `bootstrap(connect)` explicitly to create three isolated v1 tables, then
`SqlRecoveryStore(connect, SessionBinding(...)).initialize()`. Constructors perform
no SQL. Initialization starts at generation zero, unassigned and held. Repeated
initialization preserves existing state; missing/corrupt/orphaned state is never
silently repaired. Currency is permanently pinned to account/environment.

`create_plan(plan_id, actor_id, expected_generation, source_checkpoint_sha256,
target_robot, target_epoch, application_incarnation, reason,
position_obligation_ids)` retains the full immutable plan and returns
`{"plan": RecoveryPlan, "digest": sha256, "created_at": UTC_string}`. The digest
binds every field, including the exact ordered unique tuple of open obligation
identities and the application-incarnation SHA-256. An exact ID replay returns the
original record even after subsequent generations; a changed body conflicts.
New plans require the current assignment generation. Actor identity is a trusted
caller assertion, not authentication supplied by this SQL store.

The caller must hold its externally fenced authority guard, validate the current
epoch and application incarnation, and complete fresh broker reconciliation before
confirmation. In the caller's existing SQL transaction, use
`read_in_transaction(cur)` (which acquires the account recovery lock), then
`plan_in_transaction(cur, plan_id)` for bounded plan inspection, and
`confirm_in_transaction(cur, plan_id, expected_digest, epoch=...,
checkpoint_sha256=..., obligation_ids=...)`. Confirmation requires that same cursor
to hold the transaction-owned exclusive recovery lock. It neither opens another
connection nor commits. The root composition owns the outer transaction, lease
checks, authoritative guard, commit acknowledgement and failure handling.

The lock resource is `pipmind-safety-recovery-v1:` plus `account_key(binding)`.
Lock order is external authority → lease → session → controls → recovery account
→ integration → admission → book. Direct plan creation takes only recovery's
account lock. Do not call a direct connection method from inside an already
locked outer transaction.

Confirmation matches the complete digest, epoch, checkpoint and exact obligations,
then compare-and-swaps the expected generation, increments it, and appends an
immutable assignment transition and receipt. Previous robot attribution remains
in the receipt and prior transitions. This module does not move or erase open
position obligations. Every resulting `RecoveryState.held` and receipt `held` is
`True`: metadata confirmation never activates the application. The root guard's
separate state is responsible for any later explicitly authorized dry operation.
The `Assignment.activated_at` field records SQL confirmation time only.

Exact confirmation replay returns its original receipt, including old attribution
and timestamp, even if a newer assignment now exists. It is historical evidence,
never a fresh lease, reconciliation result, resume permit or proof of zero orders.
The external current-epoch guard must authorize every invocation, including replay.

Plans, assignments, receipts and hashes are stored as bounded canonical JSON;
reads check indexed identity/generation against retained bodies and the latest
transition. These checks detect specified inconsistency; they are not immutable
attestation against a privileged database attacker or a restored database. The
external trusted epoch/high-water authority and fresh reconciliation remain
prerequisites after restore. Native database backups retain all three tables;
legacy experience-export inventories do not thereby include or validate them.
There are no deletes, history truncation or reset/takeover bypasses.

Technical limits are 128 UTF-16 code units per identifier, lowercase 64-hex hashes,
64 KiB UTF-8 per canonical body, and signed-bigint generations. They do not choose
owner trading risk limits. A plan whose resulting receipt cannot fit is refused
before any transition write.

Direct operations use a fresh nonpooled connection, serializable transaction and
transaction-owned application lock. Disposal without an explicit rollback
handshake follows the pinned driver's existing cleanup pattern: close/disconnect
must discard any uncommitted transaction. `RecoveryRefused` carries fixed
validation/CAS codes; storage errors become fixed `RecoveryUnavailable`. Any
`BaseException` after a direct commit attempt becomes `RecoveryCommitUnknown`:
no result, automatic retry or authority is inferred, even if close later fails.
Ordinary precommit interruption propagates after disposal. The outer composition
must provide the equivalent discipline for same-cursor confirmation.

Offline tests use synthetic SQL doubles. The required SQL integration suite adds
real concurrent CAS, same-cursor locking, persistence/replay, staged rollback,
disconnect/interruption, lost acknowledgement and corruption regressions. No
hosted service or broker is contacted by these checks.
