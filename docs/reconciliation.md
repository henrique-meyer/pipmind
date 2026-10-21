# Read-only core reconciliation

`pipmind.reconciliation.reconcile` compares an expected book with explicitly
supplied broker facts. It is deterministic: the caller supplies the UTC observation
time and `Freshness(max_age, max_snapshot_skew)`; there is no clock, network, model,
database, ledger mutation, lease or gateway dependency in the comparison path.
Policy remains in the core as required by ADR 0002. The gateway remains a producer.

`SnapshotFacts.account` and `.positions` use the unchanged v1 AccountSnapshot and
PositionSnapshot contracts. Expected values are obligations: every supplied
account/position field is compared, using `Decimal(str(value))` for finite numeric
values. No tolerance, rounding, netting, instrument case-folding or position merging
is performed. Distinct long and short positions remain distinct even when their
quantities offset. Findings contain fixed codes, subjects, opaque identities and
field names, without copying raw payloads or exception messages. Inputs are not
modified. `Reconciliation.consistent` only means no findings within supplied scope.
It is **not** an authorization or safety capability.

## Evidence gaps in the existing contracts

PositionSnapshot v1 has no position IDs, protective-order details or completeness
field. AccountSnapshot has no account/environment identity. Supply a `Binding`
and a parallel `position_ids` sequence as separate, explicitly obtained evidence;
do not insert invented fields into normative payloads. Nonempty position rows
without identities are incomplete. The existing CIAPI adapter checks OrderId but
does not retain it in normalized PositionSnapshot; restoring that identity through
an approved adapter/core handoff remains integration work. This module does not
change that adapter or pretend those IDs can be recovered from instrument/side.

The observed `account_complete`, `positions_complete` and `orders_complete` flags
must be exactly `True`. Unknown flags are not an empty/flat book. A complete current
order scope is supplied as normalized OrderUpdate rows plus `orders_as_of`; that
timestamp is when the scope was observed, while each row's `ts` remains its event
time. An unchanged old working-order event can still appear in a fresh observed
scope. Duplicate rows and conflicting identities remain findings, not silent
deduplication. A caller assertion of scope completeness is not venue attestation,
an independent activity capture or independent zero-order evidence.

## Commands and protective obligations

Pass `commands` and `protective_obligations` explicitly, even when empty; unknown
obligations are incomplete. Command rows use the existing recovery-state
command shape (`command_id`, `state`, `broker_order_id`, `known_outcome`). Supply
corresponding normalized OrderRequest rows through `command_requests` to bind
instrument, side and quantity obligations. Accepted/dispatching commands remain
pending; unknown commands remain ambiguous even if a later OrderUpdate appears.
Reconciled rows must match their known broker identity/outcome; refused commands
must not acquire broker activity. Pending IDs remain explicit. This comparison
does not resolve, retry, resend, cancel, accept or advance a ledger command.

`ProtectiveOrder(position_id, broker_order_id, request)` is an optional core-side
supplement, not a new gateway transport schema. Its `request` uses OrderRequest v1
fields for a supported stop and/or limit price, quantity and exact instrument.
Observed supplements must come from broker-observed protective-order state with
the corresponding OrderUpdate identity and `working` status; copying the core's
original intent is not observed evidence. Supply fresh `protections_as_of` and
`protections_complete=True`. Missing price/ID/scope, accepted or partial order
status, and absent/cancelled protection cannot establish the obligation. Existing
OrderUpdate alone has no stop/limit price, so it cannot prove price protection.
Broker-to-supplement provenance and mapping are explicit remaining integration
requirements, not facts this module manufactures.

## Findings and integration boundary

Results distinguish missing, unexpected, changed, duplicate, conflicting,
incomplete, invalid, stale, future and late evidence, conflicting snapshot times,
and pending/ambiguous commands. Observations older than the expected checkpoint
are late; ages strictly greater than the configured bound are stale. A future
snapshot/order event or incoherent account/position observation cannot clear.

PIP-36 is a synthetic, read-only foundation. Hosted use still requires trustworthy
binding, position identity, completeness and protective-order projections, durable
command/reconciliation watermarks and a separately reviewed owner/lease/dispatch
gate. It grants no live-account permission or independent zero-order proof. Tests
exercise actual comparisons, including positive flat/long/short and protected
books; refusing everything does not pass. No hosted resources or runtime policy
are changed by this module.
