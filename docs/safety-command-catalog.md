# Restricted command-catalog reference

PIP-53 adds an opt-in, database-local metadata reader. It does not replace the
PIP-50 complete-history capture, grant runtime permissions, migrate a database,
activate recovery or authorize dispatch. The existing dbo-only complete-capture
path is unchanged. Actual Azure SQL Database S0 qualification remains PIP-47.

## Explicit trusted installation

`pipmind.safety.command_catalog_sql.bootstrap_sql()` returns the exact fixed
`CREATE PROCEDURE dbo.pm_safety_command_catalog_v1` text. A separately trusted
administrator installs it explicitly with `ANSI_NULLS` and
`QUOTED_IDENTIFIER` ON. There is no application installer, implicit bootstrap,
dynamic SQL, supplied object name or routing parameter. The procedure executes
as its actual dbo owner and reads local catalog metadata only; it does not read
command bodies or control a transaction.

The restricted application needs EXECUTE and object-scoped VIEW DEFINITION on
this exact module. It does not need dbo, db_owner, database-wide metadata
visibility, table DDL or IMPERSONATE. These are deployment requirements, not a
grant script or authorization to change the hosted identity.

SQL metadata may otherwise be filtered or empty under a restricted caller,
including when an explicit DENY hides an existing command table. Microsoft
documents the owner-executed metadata pattern in [metadata visibility
configuration](https://learn.microsoft.com/en-us/sql/relational-databases/security/metadata-visibility-configuration)
and the supported execution context in [EXECUTE AS](https://learn.microsoft.com/en-us/sql/t-sql/statements/execute-as-clause-transact-sql).
Actual dbo, the db_owner role and a default schema are different concepts; see
[database principals](https://learn.microsoft.com/en-us/sql/relational-databases/security/authentication-access/principals-database-engine).

## Same-cursor reader and results

Call `read_command_catalog_in_transaction(cur, expected_database=...)` using the
existing caller-owned cursor in an active, committable, non-autocommit
transaction. The explicit database must match the connected database. The reader
does not connect, load credentials, commit, roll back, acquire recovery authority
or change the application's execution context outside the module.

Before and after execution it checks the fixed module's exact UTF-16LE definition
digest, creation SET bits, procedure type, zero-argument shape, OWNER execution
marker and resolved dbo owner. It also checks the caller/database/transaction
boundary and a fixed list of direct unsafe effective permissions. Missing,
unreadable, changed or malformed evidence refuses with fixed errors. A valid
definition digest returned by an arbitrary procedure cannot certify itself.
`MODULE_DEFINITION` and `MODULE_DEFINITION_SHA256` are the locally expected
deployment artifact, not owner-provided claims.

The procedure returns exactly three bounded result sets: execution header,
objects and columns/key metadata. The immutable `CommandCatalog` records the
observed database/module/caller identifiers, expected definition digest and one
of two classifications:

| Complete observed catalog | Classification |
| --- | --- |
| No matching command objects | `absent` |
| Exactly the four supported dbo command tables and their exact supported columns/primary keys | `supported` |
| Partial, future, additional, wrong-schema/type/column/key, truncated or malformed result | Fixed refusal |

The expected schema reuses PIP-50's command definitions. A derived maximum plus
one is an overflow **refusal**, never permission to omit an object or certify a
prefix. Objects include unsupported matching views/synonyms/types so they
cannot silently become legacy absence. `dispatch_authorized` is always false.

Invalid explicit request data raises
`CommandCatalogRefused('invalid_command_catalog_request')`. An unavailable or
unverifiable SQL/module/catalog boundary raises
`CommandCatalogUnavailable('command_catalog_unavailable')`; raw SQL errors and
database payloads are not an application diagnostic channel.

## Trust and completeness limits

Installation and administrative DDL must remain trusted and stable for the
operation. Pre/post definition and ownership checks detect observed drift;
they do not prove that an omnipotent database owner could not transiently replace
and restore the module between checks. The returned object ID is observed
metadata, not an independently trusted lifetime identity or proof against every
drop/recreate sequence. Do not present this database-local artifact as external
restore authority, loaded-code attestation or an adversarial DBA boundary.

The direct permission checks are a narrow supported-reader condition, not a
complete audit of role membership, impersonation chains or every escalation
route. A deployment needs its own reviewed principal, privilege and DDL policy.
An absence result establishes only the supported command-catalog shape at that
read; it does not establish complete mind/feed/account history, command-body
integrity, settlement, source/target quiescence or readiness to resume.

PIP-50 still validates the full retained command history under its existing
transaction/lock and authoritative-context contract. This standalone reader is
not yet wired into that capture or the default recovery path. Native SQL Server
proof cannot establish actual S0 contained-identity permissions, physical
restore identity, managed backup provenance or independent authority behavior.

## Disposable native proof

The required native case is designed for a fresh isolated SQL Server database:
explicit trusted installation; restricted non-dbo caller with table metadata
DENY; genuinely hidden direct catalog versus exact owner-module results;
supported/absent positives; partial/future and module definition/owner/permission
mismatches; denied runtime ALTER/IMPERSONATE; and preserved caller context,
committable transaction, account application lock and staged-write rollback.
The test's permissions and DDL are confined to that disposable database.

Only the required CI result establishes whether this native case executed and
passed. Offline doubles or collection alone do not. No hosted database call,
permission change, deployment, PIP-17 change or default runtime integration is
part of PIP-53.
