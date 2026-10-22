# Command obligations in dry recovery

PIP-50 makes recovery checkpoints explicitly include the immutable command
journal introduced by PIP-48. This is an isolated SQL reference implementation.
It adds no sender, terminal outcome, reservation release, automatic retry,
runtime activation, hosted permission or deployment.

## Versioned inventories

The existing `checkpoint_sql.capture_in_transaction`, `required_locks`,
`COVERED_TABLES` and v1 inventory encoding retain their fifteen-table behavior.
They remain available for consumers that explicitly require that legacy profile.
They do not certify command-journal coverage.

Recovery uses `capture_complete_in_transaction` and `complete_required_locks`.
The latter appends the command account lock after the six existing ordered locks.
Callers own a real non-autocommit SQL transaction; capture does not initialize
tables, connect, commit, rebind a lease or create missing records.

| Actual command schema | Complete capture result |
| --- | --- |
| All four command tables wholly absent under the supported authoritative catalog context | Explicit legacy v1 inventory; no command-schema claim |
| All four supported tables present, even with no rows for this account | v2 nineteen-table inventory with command counts |
| Partial, unsupported/future objects or schema, corrupt canonical data/hash/reference, orphan or inconsistent counts | Fixed refusal; no partial inventory |

The v2 profile includes command account, intent, attempt and unknown tables in
addition to the legacy fifteen. Counts satisfy
`commands = refused + unattempted + attempted` and `unknown <= attempted`;
unknown is a subset of attempted. Ordered tables, explicit profile and counts
participate in the inventory hash. A v1 sidecar cannot masquerade as the complete
v2 checkpoint of a restored database that contains command tables.

Complete command validation uses existing immutable codecs/projections and
bounded keyset/record reads. It scans the whole selected account history without
a history-count cutoff. Historical resources, native pins, assignment, owner,
generation and causation remain original facts; they are not rewritten to the
current owner or restore target. Validating historical records does not certify
that their scope is current authority.

## Catalog permission boundary

SQL catalog results can hide objects on which the caller has no permissions;
an empty result is therefore insufficient evidence of schema absence.
Microsoft documents this behavior in [metadata visibility
configuration](https://learn.microsoft.com/en-us/sql/relational-databases/security/metadata-visibility-configuration).

This reference complete-capture helper requires the actual database principal
`dbo` (`USER_ID()` exactly integer 1) before classifying the command catalog.
It does not grant permissions, impersonate, infer authority from a role bit or
fall back to a partial inventory. Membership in `db_owner`, a default schema named
`dbo` and the `dbo` principal are distinct concepts; see Microsoft's
[database principals](https://learn.microsoft.com/en-us/sql/relational-databases/security/authentication-access/principals-database-engine).
This deliberately restrictive reference boundary is not a production
least-privilege recommendation or evidence that the hosted SQL identity supports
it. PIP-47 retains qualified catalog/authority deployment design. No hosted
permission change is supplied or authorized.

## Held metadata and activation

Capture, restored-checkpoint comparison, plan preparation and confirmation bind
the complete available profile. Metadata confirmation can retain a new assignment
while the authority stays held and unresolved commands remain present.

Before held dry activation writes a reconciliation or attribution record, the
same leased application transaction captures the live complete command summary.
Any recorded unattempted or attempted intent blocks activation. An unknown marker
cannot clear that obligation. Refused audit-only commands and a complete empty
command schema do not themselves block this gate. The current journal has no
verified resolution transition: this change must never reset an attempt, invent
a terminal result, delete history or release its reservation to obtain activation.

The real independent authority guard sanitizes an ordinary inner application
refusal as `AuthorityUnavailable('authority_operation_failed')`. Tests of an
injected guard may inspect the inner fixed refusal, but must not claim that this
is the real adapter's outward exception. Commit uncertainty remains distinct and
does not become proof of rollback or safe resend.

A separately explicit trusted owner may retain an audit-only unknown observation
under a held external guard and the current native-pinned lease, with the required
same-cursor locks and assignment checks. This does not activate dry routing,
authorize dispatch, replace an original command/attempt scope or mark a venue
outcome resolved. The caller boundary remains explicit; the command store does
not independently attest external ACTIVE authority.

Source and target application barriers remain required. An external fence alone
is not application transaction quiescence after an independent connection loss.
The restored target cannot substitute for an unacknowledged original-source
barrier. See [authority](safety-authority.md) and the recovery guide.

## Verification and remaining gates

Mandatory disposable SQL cases cover legacy absence and empty-profile
compatibility, refused-only activation, each unresolved state retaining a hold,
and actual native BACKUP/RESTORE with all command bodies/hashes, original
causation/assignments, reservations and attribution preserved. Restored-target
schema/hash/reference mutations are rolled back and checked again through fresh
connections. A restricted no-login user demonstrates that hidden command tables
must refuse rather than select legacy absence. The suite's actual CI result,
not collection or these descriptions, establishes execution.

The nineteen-table inventory is still not the whole application backup. Recovery
metadata and attribution are deliberately outside its self-referential inventory;
native backup must preserve all application tables, mind/feed history and every
later operational table. A syntactically valid backup sidecar is a supplied
identity claim, not independent file-integrity verification. Native CHECKSUM and
restore checks do not convert an arbitrary supplied SHA into observed evidence.

Hosted S0/native-incarnation compatibility, independent authority deployment,
least-privilege catalog access, complete operational recovery, terminal command
reconciliation, M4 qualification and actual demo execution retain their separate
PIP-47/PIP-30/owner gates. No M5 production or trading qualification follows from
this dry code release.
