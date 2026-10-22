# Explicit safety identity profiles

PIP-56 adds a closed, opt-in identity composition for the existing native
`DatabaseIncarnation` and Azure SQL `AzureDatabaseIdentity`. It changes no
runtime default, deployment, grant, hosted connection or trading transport.
`identity_profiles` accepts exact supported DTO types and their existing
canonical encodings; the native four-item array remains byte-identical, while
the Azure object retains its explicit `azure-sql-physical-v1` profile.
Unknown types, profiles, malformed bodies and mixed-profile separation claims
refuse. No arbitrary reader callback, supplied-success flag, discovery or
repinning is accepted.

`encode_identity`, `decode_identity` and `identity_sha256` retain that profile
separation. `same_database` is a conservative physical/location comparison;
case folding can only refuse a claimed separation and never relaxes exact pin
matching. It does not prove different administrative or restore failure domains.
`read_expected_identity` dispatches only the two fixed readers on a supplied
cursor and observes an explicit pin; callers still own transaction protection.

## Explicit guarded use

An explicitly supplied Azure pin is accepted by `SqlLeaseStore` through its
existing `incarnation` argument. Protected work checks that pin on the same
cursor before and after the operation. Native reader aliases and unpinned
legacy/default behavior are retained. A matching observation alone is not a
lease or detached execution permit. Existing locks, acknowledged commit and
uncertain-commit behavior remain required.

`SqlAuthorityStore` accepts the same explicit profile for its own database and
application/source/barrier identities. Its own and application/source profiles
must be homogeneous. A new profile never makes two databases independent:
trusted administrative deployment and restore permissions still need separate
qualification. Native state, fence and barrier bytes remain unchanged; Azure
nested values are reconstructed only through the fixed envelope and enclosing
canonical audit validation. Source and target acknowledgements, high-water,
immutable receipts and commit uncertainty remain enforced.

`JournalScope` accepts the two supported profiles. New command/attempt/unknown
records retain their explicit original scope, attribution and hashes. The SQL
journal validates its current explicit pin under the existing locks on both
sides of work. A later unknown-outcome observer may retain an original command
under a separately supplied later pin, as its existing audit-only semantics
allow; it neither rewrites that command nor settles it or grants authority.
Native history and readers remain compatible.

## Qualification boundaries

Focused tests exercise actual fixed-reader SQL protocols with scripted cursor
responses, exact native golden bytes, profile substitution, stale/unavailable
metadata, effects rolled back after a changed pin, and commit uncertainty.
Scripted Azure responses do not certify actual hosted recovery. Required native
SQL integration protects the existing supported reference path; an Azure SQL
positive must still be measured on Azure.

The recovery coordinator's managed-restore reference, restricted complete
inventory selection and activation composition remain separate PIP-47 work.
This deliverable does not bootstrap safety state, redirect the demo, establish
independent authority, acknowledge source/target barriers, restore a backup or
authorize orders. Preserve exact pins and raw receipts in private configuration;
never place their actual values in published examples or logs.
