# Safety operational inventory

`pipmind.safety.checkpoint_sql` captures exact retained table state within a
declared profile for an explicit `SessionBinding` and exact `LeaseBinding`. It opens no connection,
creates no schema, changes no state, and grants no operational authority. It does
not export a mind, create a backup, restore a trader, release reservations, reset
controls, or prove the provenance of broker facts.

The existing `capture_in_transaction` / `required_locks` APIs remain the exact
15-table v1 profile below. They do not include command-journal obligations.
Recovery uses `capture_complete_in_transaction` / `complete_required_locks`,
which add the command-journal lock and select a supported 19-table v2 profile
when its command schema exists. A verified wholly absent command schema retains
v1 compatibility; partial, unsupported or unverifiable schema refuses. Current
complete capture requires actual `dbo` execution context to establish catalog
visibility. No permission is granted or impersonation performed by the adapter.
See [command recovery](safety-command-recovery.md) for counts, held activation,
visibility limits and separate least-privileged hosted qualification.

The caller supplies an existing SQL transaction and already owns exclusive
transaction application locks in this order:

1. The external recovery authority, outside this database's restore domain.
2. `pipmind-safety-lease-v1:<lease binding key>`.
3. `pipmind-safety-session-v1:<account key>`.
4. `pipmind-safety-controls-v1:<account key>`.
5. `pipmind-safety-recovery-v1:<account key>`.
6. `pipmind-safety-integration-v1:<account key>`.
7. `pipmind-safety-admission-v1:<account key>`.

`required_locks(binding, lease_binding)` returns the six local resources. Capture
checks that these are `Exclusive` under `Transaction`, and that the transaction is
active and committable, before and after reading. It cannot verify the external
authority lock; the recovery coordinator owns that requirement. `HOLDLOCK` reads
also retain SQL serializable range/row protection. The caller keeps its transaction
and locks until the surrounding checkpoint operation finishes. Capture never
acquires these application locks or commits the caller's transaction.

```python
inventory = capture_in_transaction(cursor, session_binding, lease_binding)
encoded = encode_inventory(inventory)
assert decode_inventory(encoded) == inventory
```

The frozen `Inventory` contains `binding`, `resource`, `table_version`, an ordered
tuple of `TableDigest(name, count, sha256)`, and `sha256` over that complete
metadata. Its canonical JSON is bounded to 64 KiB. The table version is
`safety-operational-tables-v1`; unknown/missing columns, SQL types, primary keys,
or key collations refuse capture. Account currency pins must match the supplied
binding. Empty tables are represented explicitly, rather than omitted.

The existing `datetime2(6)` columns require catalog precision 26 and scale 6;
precision 27 is the default `datetime2(7)` precision, not the precision for every
column of that type. Microsoft's [SQL_VARIANT_PROPERTY documentation](https://learn.microsoft.com/en-us/sql/t-sql/functions/sql-variant-property-transact-sql?view=sql-server-ver17)
defines nonzero `datetime2(s)` precision as `s + 20`. The offline regression uses
literal lease catalog rows as an independent check; actual SQL CI must still
validate the real catalog and capture flow.

## Exact coverage

All account sessions and their observation history are included, not only the
current session. Reservations, refusals, suggestions and overrides are retained.
The lease scope is precisely the supplied resource; leases for other resources
are outside this inventory.

| Tables | Scope |
| --- | --- |
| `pm_safety_leases_v1` | Exact lease binding key |
| `pm_safety_session_accounts_v1`, `pm_safety_session_sessions_v1`, `pm_safety_session_observations_v1` | Every selected account row |
| `pm_safety_admission_accounts_v1`, `pm_safety_admission_journal_v1`, `pm_safety_admission_reservations_v1`, `pm_safety_admission_floors_v1` | Every selected account row, across sessions |
| `pm_safety_admission_books_v1` | Every book referenced by that account's complete admission journal |
| `pm_safety_controls_v1`, `pm_safety_control_overrides_v1`, `pm_safety_control_suggestions_v1`, `pm_safety_control_dispositions_v1` | Every selected account row |
| `pm_safety_integration_accounts_v1`, `pm_safety_integration_journal_v1` | Every selected account row |

All tables use the `dbo` schema. New recovery/assignment/checkpoint metadata is
excluded to avoid a checkpoint digest referring to itself. Native full-database
backup still includes that metadata, the mind and feeds, and all other database
history. Recovery must verify its separate assignment/plan/incarnation references;
this inventory cannot substitute for those checks or for a full database backup.

## Complete streaming and exact representations

Capture counts the complete selected range, reads deterministic primary-key
keyset pages of at most 64 rows, and rechecks the count and schema. There is no
history row limit, prefix acceptance, or omitted oversize body. Pages transport
only fixed-width native fields and lengths for `nvarchar(max)` values. Every such
value is read using binary `SUBSTRING` chunks of at most 64 KiB on the same cursor.
No full history body, list of historical keys, or table result accumulates in memory.
Memory is bounded by one 64-row metadata page and one content chunk; runtime and
SQL work necessarily grow with the complete retained history.

Hashes use separate table/row domains and typed length framing. SQL text is
hashed as exact UTF-16LE code units, including trailing spaces and embedded NULs.
Binary chunks can split code units without decoding or replacement. Native fixed
values distinguish NULL, Boolean, integer, string, bytes, naive SQL datetime,
Decimal coefficient/exponent and UUID. No float coercion, Decimal arithmetic,
JSON-body parsing, or normalization changes retained values. Table schema and
column order are bound into each table digest; row boundaries and deterministic
row order are bound into the table digest.

This is exact inventory evidence, not semantic validation of every stored JSON
record. A changed stored body changes the digest even if its redundant application
hash has not changed. Existing domain stores and recovery reconciliation still
validate record integrity and operational meaning. SQL/driver failures, short or
missing chunks, count/schema changes, lost locks and unexpected native types return
only fixed `CheckpointUnavailable("safety_inventory_unavailable")`; no partial
inventory is returned or logged. Invalid explicit bindings/codecs use
`CheckpointRefused`.

## Native backup identity

`NativeBackupReference(backup_id, sha256, created_at, source_inventory_sha256)` is
frozen, strictly typed metadata supplied independently by the trusted backup
workflow. `created_at` must be a conventional named-instant UTC string (`Z` or
`+00:00`, at most six fractional digits). Constructing it does not open a file,
verify that a backup exists, verify the hash of actual backup bytes, attest backup
ownership, or authorize recovery. The native SQL backup/restore workflow must
prove those facts and independently compare its restored complete inventory.

Offline cursor tests cover full-history tail sensitivity, multi-page scans,
chunk-boundary equivalence, scoped books/accounts, exact native representations,
strict codecs, schema/lock/read failures and caller immutability. Actual disposable
SQL and full native backup/restore tests remain separate required CI evidence.
