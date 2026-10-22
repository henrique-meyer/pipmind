# Explicit Azure SQL physical identity

`pipmind.safety.azure_identity_sql` is a separate, opt-in observation primitive.
It is not selected by a runtime, lease, authority or recovery default. It creates
no connection, table, grant, environment loader, callback, transaction or deployment.
The existing SQL Server/Managed Instance GUID/fork profile remains unchanged.

`AzureDatabaseIdentity(logical_server, database, physical_database_name)` holds
three exact, nonempty Unicode strings. Identifiers are limited to 128 UTF-16 code
units, reject NUL and invalid Unicode, and are not stripped, case-folded or
converted to UUIDs. `logical_server` is the expected exact
`SERVERPROPERTY('ServerName')` representation, not an inferred host name. A trusted
caller must prepare the expected pin explicitly and bind its configured connector
to the intended logical server/database. SQL's representation and caller labels
do not independently attest TLS, tenant, subscription, endpoint ownership or
administrative separation.

`encode_identity`, `decode_identity` and `identity_sha256` use the distinct
`azure-sql-physical-v1` profile in a canonical named JSON object with the exact
keys `profile`, `logical_server`, `database`, `physical_database_name`, in that
order. The digest binds that profile and all three exact strings. Duplicate,
missing and unknown keys, other profiles and noncanonical bytes are refused.
The named envelope cannot overlap the native profile's untagged four-element
array even when its server string equals the Azure profile identifier; legacy
native bytes remain unchanged. Canonical bodies are bounded to 4 KiB UTF-8, comfortably
covering every valid bounded pin. Pins and their codecs are private configuration
and evidence: do not emit them into public reports, prompts or sanitized summaries.
The dataclass representation redacts every identifier. A digest is a binding of
supplied metadata, not an immutable attestation or current authority grant.

## Same-cursor reader

`read_expected_identity(cursor, expected)` requires an exact
`AzureDatabaseIdentity`, a supplied DB-API cursor with detectable
`cursor.connection.autocommit is False`, and a current active transaction. It
never discovers a value and adopts it as the expected pin.

The fixed context query reads EngineEdition, ServerName, DB_NAME, DB_ID,
XACT_STATE and @@TRANCOUNT. Exact integer EngineEdition **5** is required before
any reference to Azure SQL's physical-name column. SQL Server, Managed Instance,
Fabric and unknown editions are refused. XACT_STATE must be integer 1 and the
transaction count a positive integer. Current logical names must exactly match
the expected pin.

The fixed physical query reads `database_id`, `name` and
`physical_database_name` from `sys.databases` for `DB_ID()`. It requires exactly
one complete row, the matching current database ID/name and the exact expected
physical value. Context and physical queries both run again, with all evidence
required to remain equal. Missing, duplicate, malformed or unavailable evidence
never yields a match. The reader performs SELECTs only and neither commits,
rolls back, acquires an authority lease nor disposes the caller's connection.

The before/after reads cover this observation, not an application callback. A
future protected composition must explicitly call the reader on both sides of
its operation, preserve the same transaction and existing lock order, and return
results only after acknowledged commit. This module does not implement that
composition, classify its commit uncertainty or prove that a caller followed it.
Interruption propagates to the owner of the transaction for cleanup; the reader
cannot claim the transaction rolled back.

`AzureIdentityRefused` carries fixed `azure_identity_invalid`,
`azure_identity_unsupported` or `azure_identity_mismatch` codes.
`AzureIdentityUnavailable` carries fixed `azure_identity_unavailable` or
`azure_identity_transaction_required` codes. Driver text and identifiers are not
included in these messages. SQL failures have suppressed exception context.

## Lifecycle and authority limits

Microsoft documents
[`physical_database_name`](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-databases-transact-sql?view=sql-server-ver17)
as Azure SQL's current physical database identity, which changes on restore or a
service-objective change. It is deliberately opaque here. A benign scale change
therefore also refuses the old expected pin; there is no automatic repair,
repinning or interpretation of why it changed. A renamed logical database is
also a mismatch. Reusing a name with a different physical identity cannot satisfy
the old pin.

That catalog exposes the current database row, but this scripted implementation
does not qualify actual S0 permissions, lifecycle behavior or failover. Azure SQL
[PITR restores to a new database](https://learn.microsoft.com/en-us/azure/azure-sql/database/recovery-using-backups?tabs=azure-portal&view=azuresql),
and managed backup bytes are not a native downloadable SQL Server backup file.
This profile does not interpret a managed restore receipt or certify its history.

The physical pin is not an external monotonic authority, lease generation,
source/target quiescence receipt, risk approval or command reconciliation. Same
values at two observations do not exclude an intervening ABA change or a
privileged administrator fabricating metadata. It cannot fence another database
or an old runtime, establish administrative independence, authorize dispatch or
settle outstanding commands. Those remain explicit prerequisites of a separately
qualified recovery composition, including acknowledged barriers for both the
original source and the target. A mismatch must remain held until that procedure
is independently approved and completed; merely creating this object grants no
permission to resume.

## Evidence and remaining qualification

`tests/test_safety_azure_identity_sql.py` supplies scripted cursor rows. Matching
opaque pins are positive tests of local behavior only; other cases exercise
native-profile confusion, malformed/absent/duplicate metadata, fixed permission
errors, missing transaction evidence, wrong editions, rename/name reuse,
service-objective identity changes and changing context. No Azure SQL connector
or resource is contacted. A disposable SQL Server CI refusal can demonstrate that
its wrong edition never reaches the Azure-only query, but cannot supply an S0
positive. Actual least-privileged S0 reads, PITR/name reuse/scale/failover behavior,
independent authority and composed source/target recovery remain unperformed
PIP-47 qualification gates. No production identity path or order transport is
enabled by this module.

## Hosted qualification checkpoint

PIP-47 has now exercised the explicit reader against the actual demo S0 database
through its contained managed identity, with encrypted certificate-validated
transport. An exact privately prepared pin passes; deliberately wrong logical
server, database and physical-name pins each produce `azure_identity_mismatch`.
These are actual database observations, rather than scripted positive fixtures.
The observed principal has no database role memberships and only CONNECT at
database scope; its application table permissions remain object scoped.

The separately installed fixed owner catalog passes under that actual managed
identity with only EXECUTE and object VIEW DEFINITION added on the module. It
reports command tables absent. SQL denies runtime module alteration and dbo
impersonation. Actual S0 administrator transactions impersonating the restricted
principal also refuse missing EXECUTE, missing VIEW DEFINITION, changed module
text and a future command object invisible in the caller's direct catalog. Each
transaction rolls back and the original exact catalog proof passes afterward.

The hosted database contains M4 mind/feed state; no M5 safety history was created
to manufacture a complete-checkpoint positive. Restricted full safety capture,
restored-database lifecycle, independently protected authority, both barriers and
operational recovery remain separate qualification gates. The app images,
capacity, runtime controls and public landing are unchanged by these probes.
Private pins, source artifacts and raw receipts stay in the private repository.
