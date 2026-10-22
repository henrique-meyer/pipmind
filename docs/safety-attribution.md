# Trusted gateway hooks and immutable assignment attribution

PIP-29 adds opt-in SQL-only hooks to the standalone dry safety gateway and an
isolated assignment-attribution journal. They do not activate a robot, change an
assignment, send an order or confer dispatch authority. Root's recovery coordinator
supplies the independently checked current assignment and external-authority
scope; this adapter records the association without certifying those prerequisites.

## Same-cursor hooks

`SqlSafetyGateway(..., before=None, after=None)` retains its existing behavior
when both are omitted. Both hooks must be supplied together as trusted callables.
Within the existing lease's SQL callback:

1. `context = before(cursor, grant)` runs before the gateway acquires its session,
   control, integration and admission locks. The recovery coordinator may acquire
   the earlier session/control/recovery locks here; the gateway subsequently
   reacquires those same session/control locks.
2. The existing dry gateway operation runs, including exact historical replay,
   freshness checks, controls, reconciliation holds and reservation staging.
3. `after(cursor, context, result)` runs only for an actual result, including an
   exact historical receipt. It may return an enclosing result with assignment
   attribution. The internal conflict sentinel bypasses this hook unchanged.
4. The lease performs its final identity/expiry checks and commit. Only an
   acknowledged commit permits the returned result to escape.

Hooks receive exactly that lease cursor. They must do SQL-only work, consume
results before returning, and never open another connection, perform network/model/
broker work, invoke a nested lease callback, commit or close the transaction.
They are trusted code, not a sandboxed extension or user/model-selected callback.
Exceptions stay inside the lease callback and receive its existing failure and
commit-uncertainty behavior. No dispatch hook exists. Current external-authority
checks belong to the enclosing recovery coordinator; a historical receipt remains
historical even when a current guard permits looking it up.

## Immutable links

Explicit `pipmind.safety.attribution_sql.bootstrap(connect)` creates only
`pm_safety_attribution_v1`; construction never creates schema or reads SQL.
`SqlAttributionStore(connect, binding)` binds an exact `SessionBinding` without
inventing an assignment or database identity. All connectors must target the same
trusted physical application database.

`record_in_transaction(cursor, assignment, gateway_result)` requires an existing
transaction and verifies already-held **exclusive recovery and integration account
application locks** with `APPLOCK_MODE`. It acquires no new application lock after
admission/book work and opens/commits no connection. The root lock order remains
external authority -> application lease -> session -> controls -> recovery ->
integration -> admission -> book. Recovery/integration lock ownership is necessary
for atomic recording; it does not by itself prove the supplied assignment is
current. The trusted before hook/coordinator must establish that separately.

The method freezes the result, reads its actual immutable integration-journal row
using the same cursor, and verifies canonical request/receipt bytes and hashes,
account/environment/currency, channel/ID, session/book/mapping scope and permanent
false dispatch flags. It references the exact original request and receipt by
hash; those full bodies remain in the integration journal rather than being copied
into every link. A new link requires a valid complete assignment with matching
binding and the receipt's authority epoch.

The frozen `AttributionLink` contains:

- binding, channel and record ID;
- original gateway request, receipt and scope hashes;
- the full original `Assignment`, including immutable robot reference, assignment
  ID/generation, epoch, checkpoint hash and activation time;
- SQL observation time, schema version and `dispatch_authorized=False`.

`encode_link` / `decode_link` are strict canonical codecs. Custom dictionaries,
custom JSON hooks, floats, duplicate/noncanonical/corrupt JSON, invalid types and
over-bound links refuse. Input facts are not mutated. Link bodies are bounded to
64 KiB UTF-8, with SQL transfer guards before decoding. There is no history count,
age cutoff or truncation.

A link is unique by account/environment, channel and record ID. On exact replay,
the retained link is verified against the original gateway row **before** comparing
the caller's current assignment. It returns that original assignment even after a
handover changed the current robot, generation or epoch. It never retroactively
labels an old refusal or acceptance as the replacement robot's decision. A missing
link for an old receipt cannot be silently created under a different epoch;
changed/missing/corrupt source records or mismatched hashes fail closed. An old
link is audit evidence, not a fresh permission to consume the receipt.

`lookup(channel, record_id)` is a bounded historical read using its own fresh,
non-pooled transaction, acquiring recovery then integration locks. It returns
null only when no link exists, and revalidates any link against its referenced
gateway record. It performs no assignment activation or authority check and must
not be used as a dispatch permit. Construction and lookup do not assert native
database incarnation; the recovery-aware outer coordinator and pinned application
lease enforce that separate boundary.

## Atomicity, restore and remaining gates

Attribution insertion is staged in the same application lease transaction as its
gateway journal and admission reservation. Any after-hook failure prevents a
positive result and lets the lease abort its uncommitted transaction. Own bootstrap/
lookup commit failures or interruptions become fixed `AttributionCommitUnknown`;
recording under a lease retains the outer lease's `LeaseCommitUnknown` semantics.
No uncertain commit is converted into a known success, rollback proof or automatic
retry. Reopen and reconcile exact immutable identities.

The 15-table legacy operational checkpoint inventory **does not include these new
recovery/attribution tables and is not a complete native-history backup**. Full
native application database BACKUP/RESTORE must preserve the integration journal,
attribution links, recovery metadata, controls, reservations and all other tables.
A retained link whose referenced gateway record is absent refuses usable lookup;
that refusal does not repair or reconstruct missing history. Native full-backup
and composed recovery SQL tests must demonstrate preservation of the new links.

Offline tests cover link staging, canonical hashes, original-assignment replay,
all three channels, scope/identity/corruption failures, required transaction/locks,
caller mutation, no independent recording connection, commit uncertainty and hook
ordering/conflict bypass. They do not prove real SQL scheduling or native restore.
Disposable SQL integration and root's recovery coordinator remain required.
The native incarnation reference adapter still does not support the hosted Azure
SQL S0 deployment. Production incarnation/authority deployment, M4 qualification,
owner limits and separate transport/execution authorization remain open; no hosted
configuration, capacity, resources or runtime state change is part of this code.
