# Durable local command and unresolved-attempt journal

PIP-48 adds an isolated SQL audit journal beneath the future PIP-30 demo-order
path. It contains no sender, worker, service route, broker/model client, default
runtime wiring or automatic restart work. Every command, attempt, unknown result
and projection has `dispatch_authorized=False`. A stored intent is not an order
submission, risk admission, venue acknowledgement or permission to send.

## Immutable facts and provenance

The pure command contract stores the exact canonical v1 `OrderRequest` JSON as
`order_body`. Its command ID must equal `client_order_id`. Numeric literals use
exact integers/Decimal values, without float conversion or ambient Decimal
rounding. Missing fields and explicit null remain distinct. Existing normative
order/update/fill schemas are unchanged.

A command also retains its causal ID, local `recorded` or `refused` outcome,
fixed code and original scope. Scope binds the actual retained assignment and
robot reference, account/environment, native application incarnation, owner,
lease generation/resource/expiry and control revision. SQL constructs scope and
timestamps from current retained state/server time; callers do not supply a
fabricated scope or recorded timestamp to the SQL methods. There are no lease
tokens in these audit records.

An attempt-intent marker references the exact original command hash. There can
be at most one marker per logical command. It may be recorded by a fresh verified
lease owner/generation for the same original assignment, native incarnation and
resource. A replacement assignment cannot start an old unattempted command. The
marker says only that the trusted local boundary recorded an intent to attempt;
it does not prove transport was called or establish what a venue received.

An unknown-outcome record requires that retained attempt and binds both original
hashes. Its observer scope is separate: a current later assignment/native
incarnation for the same account binding/resource can preserve uncertainty
without relabelling the original command or attempt. It supplies no invented
terminal status. Exact replays retain original facts and attribution; changed
bodies, causal facts or conflicting attempt IDs refuse instead of overwriting.

## Trusted same-cursor boundary

Explicitly bootstrap the isolated schema, then construct
`SqlCommandJournal(connect, leases, recovery, controls)` with the real native-pinned
lease, recovery and control stores on the same trusted application database.
There is no automatic shared migration. The constructor is not an attestation
that independently supplied connectors select the same database.

Mutation methods take the caller's transaction cursor, actual `LeaseGrant` and
current `Assignment`:

- `record_in_transaction(cur, grant, assignment, order, *, causation_id,
  local_outcome='recorded', code='locally_recorded')` records immutable local facts.
- `attempt_in_transaction(cur, grant, assignment, command_id, attempt_id)` records
  the sole unresolved attempt-intent marker.
- `unknown_in_transaction(cur, grant, assignment, command_id,
  *, code='outcome_unresolved')` records the unresolved observation.

The caller first holds exclusive transaction locks in lease → controls → recovery
order, then the journal obtains its account lock. Existing broader safety
compositions must preserve their established intermediate lock order. Use the
trusted lease `compare_and_act` boundary and consume results before committing;
the journal methods do not open another transaction or commit the caller's one.
Current native identity, ownership/expiry, retained assignment and control facts
are checked before and after staging. New attempt intent requires controls to be
unpaused and not killed. Recording a local refusal or preserving an unknown
obligation is distinct from authorizing an attempt.

External ACTIVE authority guards remain a caller prerequisite for any later
recovery-aware operational use. This adapter does not establish independent
authority, fresh source/quote/account facts, numerical risk, a retained admission,
broker provenance or a final dispatch check. In particular it does not parse a
dry gateway receipt into a send permit. Trusted Python callbacks are not sandboxed.

## Reads, restart and uncertainty

`lookup(command_id)` and `lookup_in_transaction(cur, command_id)` return the
historical projection or None. `iter_history()` streams complete account history
through bounded keyset reads; it does not authorize or schedule any action.
Order bodies are bounded to 16 KiB and complete individual records to 64 KiB;
identifiers fit 128 UTF-16 units. These isolated technical bounds refuse
oversized/invalid facts rather than truncate them. A complete history can grow
without accepting a bounded prefix as complete. Projection states `recorded` and
`refused` describe local facts; both `attempt_intent` and `unknown` are unresolved,
nonterminal states. They cannot schedule a second attempt.

Transaction rollback/interruption preserves the previous exact projection. A
lost commit acknowledgement means persistence is uncertain, not that the record
is absent. Reopen the correctly pinned store and reconcile the exact immutable
ID/body/hash before further owner action. An existing attempt or unknown remains
an unresolved obligation after restart, owner change or restore. There is no
retry, reset, resend, terminal-result invention, reservation expiry/release or
background processing API. Historical lookup is never a fresh ownership check.

Complete native application-database backup/restore must retain these additional
tables and original scopes together with mind, feeds, risk reservations, controls,
recovery and attribution. The earlier 15-table safety inventory has its declared
scope; it is not silently extended into a command-journal/full-database export.
PIP-50 adds an explicit 19-table complete recovery profile with exact command
history and unresolved counts, described in [command recovery](safety-command-recovery.md).
The coordinator remains held for recorded unattempted commands or any immutable
attempt/unknown outcome. Native backup preservation alone never resolves those
obligations or permits actual dispatch. Future transport still needs verified
venue resolution and final send checks; dropping history is not a workaround.
Restored historical lookup does not activate its old owner. Preserve native-pin,
independent source/target barriers and explicit fresh recovery requirements.

## Validation and deployment limits

Offline codec tests and disposable real SQL tests cover immutable replay,
colliding IDs, competing attempts, staged rollback, reopen, lost commit
acknowledgements and complete native backup/restore preservation. Test-only
explicit policies and synthetic observations do not demonstrate actual broker
behaviour, transport idempotency or a venue outcome. Actual CI execution must be
reported separately from collecting or compiling a SQL test fragment.

PIP-30 still owns real demo transport, final send checks, ambiguous venue
reconciliation and terminal budget disposition. PIP-47 still owns supported S0
recovery identity/independent authority qualification. PIP-17 robot loading/UI,
owner risk/session choices, applicable M4 gates and explicit demo-order permission
remain separate. No hosted application, resource/capacity/configuration, landing,
runtime control or current history limit changes as part of this isolated code.
