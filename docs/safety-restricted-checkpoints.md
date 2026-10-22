# Restricted complete safety checkpoints

PIP-54 composes the fixed [command-catalog module](safety-command-catalog.md)
with the existing [complete command checkpoint](safety-command-recovery.md).
It is an explicit, read-only reference API. It adds no connection, installer,
permission grant, default migration, recovery activation, sender or runtime
wiring. Actual Azure SQL Database S0 qualification remains PIP-47.

## Explicit opt-in API

```python
from pipmind.safety.command_checkpoint_sql import (
    capture_complete_restricted_in_transaction,
)

# Trusted caller already owns the live non-autocommit transaction and all
# seven complete_required_locks(binding, lease_binding), in the existing order.
inventory = capture_complete_restricted_in_transaction(
    cursor,
    binding,
    lease_binding,
    expected_database=expected_database,
)
```

The return type is the existing `checkpoint_sql.Inventory`, with its existing
encoding and hash domains. Identical retained state produces the same table
order, row counts, table digests, command counts, overall hash and encoded bytes
as the default actual-dbo capture. The new function does not accept a caller's
catalog DTO, absence flag, expected outcome or evidence callback as authority.

The default `capture_complete_in_transaction` and direct command-capture APIs
retain their actual-dbo requirement. They are not silently switched to this
path. The new path requires a non-dbo caller; dbo uses the default API for its
capture. No recovery coordinator, CLI, owner interface or hosted identity selects
the new API automatically.

## Caller and deployment prerequisites

- Explicitly install the exact PIP-53 owner-executed catalog module through the
  separate trusted administrative procedure. Its module definition, settings,
  resolved dbo owner and restricted execution contract must remain valid.
- Give the restricted caller EXECUTE and object-scoped VIEW DEFINITION on that
  exact module. It must also have actual SELECT on every required retained table
  and the metadata visibility needed for the fifteen known legacy-table schema
  checks. A supported nineteen-table profile requires SELECT on all nineteen;
  authoritative legacy absence requires the fifteen legacy reads.
- Use the existing cursor in an active, committable, non-autocommit transaction.
  Hold the same seven ordered exclusive transaction application locks returned
  by `complete_required_locks`. This API checks them; it does not acquire them.
- Bind the explicit expected database, account/environment/currency and lease
  resource correctly. A name is a logical binding, not a physical restore pin or
  independent server attestation.

No dbo/db_owner membership, schema/table ALTER or dbo impersonation is required
for the caller. These prerequisites do not authorize changing the hosted SQL
identity or granting deployment permissions.

SELECT may expose a known table's existence despite denied VIEW DEFINITION.
Do not infer that an object-level definition DENY makes every catalog row
invisible. The authoritative module is needed because additional objects can
still be hidden from the restricted caller. Microsoft documents filtered
metadata in [metadata visibility configuration](https://learn.microsoft.com/en-us/sql/relational-databases/security/metadata-visibility-configuration).
The catalog module's exact deployment and trust limits remain in its guide.

## Complete scan and refusal behavior

The function first checks the seven locks and executes the real PIP-53 module
proof before reading user history. It then retains the existing legacy capture
and, for a supported command schema, the existing typed command-table scan,
canonical body/hash checks, account pins, command/attempt/unknown reference
validation, projections and physical/count reconciliation. It does not replace
these with aggregate labels, a row-count-only check or a shortened history.

| Authoritative command catalog and full data reads | Existing result |
| --- | --- |
| All four command tables absent | Legacy fifteen-table v1 inventory |
| Four supported tables, including an empty journal | Nineteen-table v2 inventory with exact `CommandCounts` |
| Refused-only history | Same v2 counts/digests; refusal remains an immutable audit fact |
| Unattempted, attempted or unknown obligations | Same v2 counts/digests; unresolved history remains unresolved |
| Hidden future/partial/unsupported schema, missing permissions, module drift, corrupt body/hash/reference or inconsistent counts | Fixed refusal; no partial inventory |

After all scan and count work, the API executes the actual catalog proof again
and requires equality of its full observed DTO, including module/object/schema/
column/context facts. Final lock checks and the existing inventory encoder still
apply. Missing or unverifiable boundaries normalize to
`CheckpointUnavailable('safety_inventory_unavailable')`; interruption by a
`BaseException` propagates to the caller's transaction cleanup. No returned
inventory is a dispatch permit, settlement result or proof of a backup file.

Streaming page/chunk and per-record limits are unchanged. Complete history has
no new row-count cutoff, and a catalog overflow is refusal rather than a
truncated success. The function never commits or rolls back. The trusted caller
must clean up its own failed or interrupted transaction and must not infer an
acknowledged commit from a captured inventory.

## Native proof and its limits

The required disposable SQL case reuses one real journal/authority fixture for
one isolated application database. It compares dbo and restricted captures on
the same locked cursor for genuine legacy absence, empty v2, refused-only and
mixed unattempted/attempted/unknown history. It retains an actual dry admission
reservation and exact original command/attempt/unknown bodies, hashes and
provenance. The old dbo-only gate still refuses the restricted caller.

The case checks caller identity, database, transaction and all seven application
locks, plus rollback of a staged probe write. Missing and denied SELECT,
missing module permissions, changed module text, a partial command schema,
hidden unsupported future object and corrupted retained hash must refuse.
The hidden future object is verified to exist under the administrative context
and to be invisible without SELECT/metadata permission under the caller.

After history is populated, every negative DDL, permission or data mutation
stays inside the test's transaction and is rolled back. A fresh dbo capture,
exact journal codecs and retained mind state must equal their baseline after
each negative. Dropping and committing a populated command table is not a
recovery or testing shortcut. The initial legacy-absence phase drops only an
independently verified empty command schema.

Only an actual required CI result establishes native execution; offline doubles,
collection or this guide do not certify it. Disposable SQL Server behavior does
not establish actual S0 managed-identity permissions or hosted module deployment
integrity. Stable trusted administrative DDL remains required: pre/post checks
detect observed drift, not transient malicious-dbo replacement or every
privilege-escalation path. Independent restore identity/authority, source/target
barriers, full backup provenance, operational M4/M5 qualification, PIP-17 loading
and explicit demo-order permission remain separate gates.
