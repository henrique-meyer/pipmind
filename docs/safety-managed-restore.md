# Explicit managed-PITR dry recovery

PIP-57 composes the PIP-56 identity profiles with the isolated dry recovery
coordinator. It adds no runtime wiring, cloud connection, restore operation,
order transport or authority deployment.

`managed_restore.AzurePitrReference` is a distinct, canonical
`azure-sql-pitr-v1` sidecar with exact Azure source/target pins, restore point and
completion time, source safety-inventory hash, management-receipt hash and
full-history-receipt hash. These name independently retained trusted artifacts.
They are neither native backup-file digests nor proof that artifacts exist or
that restored contents are complete. The codec rejects unknown/extra/duplicate
fields, noncanonical representations, mixed profiles and contradictory dates.
Its 16 KiB bound affects this new sidecar only.

`DryRecoveryCoordinator(..., inventory_profile="restricted-command-catalog-v1")`
selects the fixed PIP-54 complete-capture implementation. The default remains
`native-dbo-v1`. Every coordinator capture and the held activation wrapper use
the selected policy under the existing seven locks and exact transaction pin
checks. No supplied callback or inventory assertion can replace the real reader.

`verify_managed_restored(inventory, reference, expected_source_identity=...,
management_receipt_sha256=..., history_receipt_sha256=...)` requires a non-null,
matching Azure source lineage in the independently held authority state, exact
target pin, separately expected receipt hashes and equality with the actual
complete target safety inventory. The trusted caller must separately verify the
management operation and full mind/feed/history receipts, including later source
deltas. A matching reference or lineage pointer acknowledges neither database's
barrier and grants no execution authority. Native backup verification remains a
separate native-profile path.

Original-source drain, target barrier and both commit acknowledgements remain
mandatory for recovery. Unknown outcomes, reservations, loss history, controls
and attribution are preserved. A missing source or uncertain acknowledgement
holds recovery; there is no automatic repin, reset, resend or activation.

Scripted Azure tests establish codec/orchestration behavior. Required disposable
SQL Server tests exercise the non-dbo restricted coordinator, complete populated
command history, prepare/confirm, unresolved-command refusal and rollback under
hidden schema or revoked permissions. They do not certify Azure S0 metadata,
managed PITR contents, independent administration or hosted operational recovery.
Those remain PIP-47 gates; the full shadow-day qualification remains PIP-14.
