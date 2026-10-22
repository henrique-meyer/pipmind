# SQL-backed dry safety gateway

PIP-28 connects the numerical boundary, reconciliation, ownership lease, session
loss accounting and risk reservations to a separately constructed **dry** gateway.
`pipmind.safety.integration_sql.SqlSafetyGateway` and
`pipmind.safety.service.create_app` have no broker/model client, send operation,
environment loader or default application wiring. They do not change the hosted
shadow engine, existing gateway, landing container or shared migrations.

## Explicit construction

Bootstrap the isolated lease, session, admission, control and integration schemas
explicitly on a disposable database for integration tests. Initialize the lease
and exact session/policy book, provide current complete equity/cumulative cash-flow
evidence, and initialize controls. Controls start **paused**. See
[session accounting](safety-sessions.md), [admission](safety-admission.md),
[same-cursor composition](safety-composition.md) and [owner controls](safety-controls.md).

Construct `SqlSafetyGateway(admission, controls, symbols,
base_units_per_size=explicit_factors)`. All stores must use the same physical
database; this is trusted configuration, not independently attested by the
constructor. The symbol map and exact positive Decimal base-unit factors are
copied and hashed. Venue quantity conversion and base-unit risk conversion are
different dimensions: both must be supplied explicitly. Every proposal and held
position mark must agree with the trusted risk factors. Mapping failures refuse
the exact request without shrinking or rounding it. These facts are deployment
inputs; the API does not establish their broker provenance.

## Transaction and hold semantics

The lock order is **lease -> session account -> controls account -> integration
account -> admission account -> policy book**. Control writers take only the
control lock; they do not wait for the model or broker. A transaction already in
progress may commit its dry result before a concurrent kill is acknowledged.
Once kill is acknowledged, subsequent fresh checks refuse. This is SQL
serialization, not an atomic SQL-to-broker send protocol.

- `reconcile(grant, request)` records a new immutable revalidation observation.
  Ready requires a complete positive fresh assessment and valid mapping. This
  conservative gate also holds on ordinary risk refusals, not just position
  mismatches. A previously accepted original is rechecked without double counting
  its own verified reservation. Readiness binds exact lease authority, owner,
  generation/resource, session, policy book, mapping and control revision.
- `accept(grant, request)` records an exact immutable refusal or `dry_accepted`
  result in the same fenced transaction as admission/reservation. Kill, pause,
  absent or mismatched readiness and mapping failures refuse before reserving.
  Admission receipts are freshly reassessed before a positive dry result; an old
  admission receipt cannot supply current authority.
- `check(grant, check_id, request)` checks a previously dry-accepted exact order
  against supplied refreshed evidence, current SQL session, controls and every
  other pending reservation. The original units and reserved budgets cannot
  silently shrink. It records `dry_checked` or an explicit refusal. Any assessed
  refusal reinstates the durable revalidation hold. No reservation is released.

Every positive is checked again against SQL time after journal staging, followed
by the lease's own final ownership/expiry checks. No result escapes before an
acknowledged commit. SQL loss, corrupt/missing state and callback exceptions fail
closed; callback failures normalize through the existing lease API. Lost commit
acknowledgements remain `LeaseCommitUnknown`, with no automatic replay or rollback
claim. Restart lookup through the exact IDs reconciles durable dry records.

Accept IDs are the account-wide `client_order_id`; check IDs have a separate
namespace. Exact retries return the **original historical receipt**, including
its original ownership and refusal. Changed bodies/session/book/mapping conflict.
A deliberately new attempt needs a new check ID or proposal ID as appropriate.
Replayed receipts do not revalidate controls or facts. Every response permanently
sets `dispatch_authorized=false`, `dispatch_enabled=false` and
`historical_receipt_only=true`; no response is a send permit. New owners and
control revisions must explicitly revalidate before a new dry check.

Full canonical request envelopes and receipts remain in the isolated immutable
journal, along with hashes and ownership/checkpoint metadata. The current hold is
a derived projection; history and reservations are never truncated or cleared.
Lease tokens are excluded. Module bounds are 8 MiB per retained request envelope
and 8 KiB per receipt, with SQL size guards before retrieval. The underlying
admission request remains 1 MiB. Existing runtime, model, feed, HTTP and export
limits are unchanged.

## Private HTTP interface

`create_app(gateway, controls, token, grant_provider=trusted_callable,
owner_id=trusted_identity)` constructs an opt-in internal FastAPI application.
It is not installed in existing targets or deployed by this change. The trusted
local grant supplier provides the current `LeaseGrant`; requests cannot supply
lease tokens, origin labels or actor identities. Caller authentication establishes
the configured trusted owner, not the provenance of market evidence.

| Route | Exact body / behavior |
| --- | --- |
| `GET /healthz` | Fixed dry health, no state/authority; public authentication exception |
| `GET /state` | Authenticated bound control state |
| `POST /reconcile`, `POST /orders` | `{ "request": "canonical encode_request output" }` |
| `POST /checks` | `{ "check_id": "explicit ID", "request": "canonical output" }` |
| `POST /controls/overrides` | `{ "command_id", "action", "expected_revision", "reason" }` |
| `POST /suggestions` | `{ "suggestion_id", "text" }` |
| `POST /suggestions/disposition` | `{ "suggestion_id", "disposition", "reason" }` |

Bearer authentication precedes streaming/parsing and SQL work. Unknown fields
refuse; all responses are `no-store`, errors are fixed codes and blocking work
runs outside the event loop. The new HTTP envelope has a separate 4 MiB bound
(some highly escaped requests may reach this transport bound before the 1 MiB
inner codec bound). Control bodies are 64 KiB and responses 512 KiB. Oversize
input refuses, never truncates. No CORS, public control route or documentation
endpoint is enabled. Use trusted internal TLS/network authorization when later
deploying; this constructor is not a deployment security configuration.

Suggestions record adopt/trial/reject with reasons and cannot mutate numerical
limits or place orders. Owner overrides remain separately tagged and excluded
from self-review. Kill/pause/resume/unkill apply without model agreement. Flatten
is explicitly `unsupported_not_executed`; no synthetic execution or fill is
created. Manual order execution and public override outcome publication require
their later actual paths.

## Qualification boundary

Offline tests exercise codecs, fresh-preview composition, mapping, authentication
and control semantics. Mandatory CI additionally uses disposable real SQL for
the authenticated integrated flow, concurrent controls/owners, stale proof,
staged rollback, uncertainty and retained state. Offline success alone does not
prove those SQL tests ran.

This code does not complete M4 or all operational M5 gates. PIP-29 retains
external restore fencing and original-trader recovery/handover. PIP-30 retains
demo-only venue transport, terminal reservation reconciliation/release and actual
order/fill ambiguity. PIP-17 controls/library/shared shadow files remain separate.
Owner risk numbers, trusted source production, explicit execution approval and
applicable M4 qualification are required before activation. No hosted operation
or deployment is part of this release.
