# Exact open-position read evidence

PIP-51 adds a standalone reader and projection for **already obtained** broker
open-position response bodies. It preserves supplied bytes, identities, exact
quantities and capture timing for existing reconciliation consumers. It makes
no HTTP request and changes no gateway, shadow runtime, account, SQL state,
configuration, deployment or order permission.

## Supported input and trust boundary

`pipmind.broker_read_capture.RawPositionRead` contains immutable body bytes,
their lowercase SHA-256, explicit trading-account ID and UTC request/receipt
times. Valid zero-offset clocks are stored as standard UTC copies so caller
timezone callbacks cannot escape later validation or arithmetic. Its endpoint meaning is fixed: a caller declares these bytes were retained
from `GET /order/openpositions` for that selected account. There is no arbitrary
URL, header, credential or query interface. Matching the supplied digest does
**not** authenticate that declaration or establish an exhaustive account book.

The [ListOpenPositions contract](https://docs.labs.gaincapital.com/Content/HTTP%20Services/ListOpenPositions.htm)
requires selected trading-account scope and describes an open-position list.
The documented response does not establish a pagination watermark or atomic
account snapshot. An empty response must therefore remain an unverified scope,
rather than proof of a flat account.

Call `decode_positions_read(capture, observed_at=..., freshness=...)` to obtain
a new exact-decoded body, or call
`pipmind.broker_positions.project_position_read(capture, binding=..., symbols=...,
observed_at=..., freshness=...)` for the supported position projection. The caller
must explicitly supply `Binding`, verified `SymbolMap` inputs and freshness
bounds. These are caller evidence, not authenticated demo identity, owner risk
defaults or venue attestation. For reproducible replay, retain the exact binding
and symbol map/factors separately. The raw body digest does not bind those
caller inputs or make the projected result a self-contained attestation.

The reader requires exact UTF-8 JSON with no duplicate keys, nonfinite numbers,
conflicting aliases, unsupported wrapper shape or nonempty supplemental order
book. Parsed numbers remain integer/Decimal values. Each decode returns a private
tree; mutating it cannot change the capture or another read. Fixed error codes
omit payload values, and capture representations omit the body.

The technical body limit reuses CIAPI's existing 2,000,000-byte bound. This
standalone decoder additionally bounds JSON depth at 128, numeric lexemes at
4,300 characters and exponent-digit length at 18 before conversion. Consumed
numeric values use the existing bounded broker-number domain. These are local
representation guards; they neither truncate accepted bodies nor change an
existing service, model, prompt, history, feed or owner-risk limit.

## Projection and existing comparator

The [open-position DTO](https://docs.labs.gaincapital.com/Content/Data%20Types/ApiOpenPositionDTO.htm)
provides an opening order ID, selected trading-account ID, market ID, direction
and quantity. The projection keeps every supported distinct row, including
same-market same-direction trades and hedges. It never nets or overwrites by
instrument. Position IDs are the exact positive order IDs rendered as strings;
source paths preserve the actual PascalCase/lower-camel wrapper alias and index.
An unknown market, duplicate identity, foreign account, unsupported status or
unknown row shape refuses instead of disappearing from the book.

Broker quantity is divided by the explicitly supplied broker quantity factor
using isolated exact Decimal arithmetic. Nonrepresentable division refuses;
there is no rounding or smaller substitute. This factor is not a risk
base-unit conversion, a sizing recommendation, or proof that a future order meets
the venue's current minimum/increment rules.

`PositionProjection.positions` is a frozen tuple of frozen projected positions.
Its `source_sha256` identifies the **exact supplied body bytes**, including
whitespace. A canonical hash of an older parsed JSON object cannot be relabeled
as that byte hash. Original lexical representations and supported fields remain
in those bytes even where the projection exposes only an exact numeric value.

`snapshot_facts()` creates a fresh existing `SnapshotFacts` object and fresh
mutable v1 position payload. It carries the caller's binding and parallel exact
position IDs. Account data is `None`; account/position/order/protection
completeness, orders and protective orders remain unknown. Every projection
retains these explicit gaps:

- `account_scope_unknown`
- `positions_scope_unverified`
- `orders_scope_unknown`
- `protection_scope_unknown`
- `environment_unverified`

`dispatch_authorized` is always false. The existing real `reconcile` function
can compare the projected position values/identities against an independent
expected book, but still produces its actual missing account/order/scope
findings. A successful projection is not a successful full reconciliation or
risk admission. Do not copy the observed book into expected state and treat
self-consistency as external agreement.

## Separate clocks and unsupported protection

The position payload's `as_of` is the conservative request-start time. The reader
requires request â‰¤ receipt â‰¤ observation, bounded total age and bounded request
duration. A later observation cannot refresh an old or incomplete capture.
The projected `venue_time`, when present, represents `LastChangedDateTimeUTC`;
it is separate from request/receipt times. A legitimate old edit can occur in a
fresh read. Missing edit time stays unknown, and a timestamp after the captured
receipt refuses. Other supported source timestamps are not invented as fallbacks.

The venue documents `Price` as the opening rate and last-change time as an edit
time that excludes current market-price changes. Neither is a fresh liquidation
quote or a common atomic snapshot cut. A null/missing opening price remains
unknown; the producer supplies no proposal or exposure valuation.

The [basic stop/limit DTO](https://docs.labs.gaincapital.com/Content/Data%20Types/ApiBasicStopLimitOrderDTO.htm)
contains limited child fields. It does not establish the complete lifecycle,
executed/remaining size and individual-position proof required by the existing
safety boundary. Non-null protection-bearing objects, net associated orders and
actual managed-position shapes are unsupported here. Null/empty supported
placeholders do not remove any gap. The reader never fabricates client-order
IDs, opposite-side stops, working updates or zero filled quantities.

## Verification and remaining work

Independent tests project flat books and three distinct same-market positions,
then exercise the actual comparator and retain its incomplete findings. They
also check byte/hash preservation, fresh decode/snapshot isolation, Decimal
precision and ambient-context isolation, alias paths, unsupported protective
shapes, cross-account/duplicate/unknown identities and request/event timing.
These are synthetic offline controls, not recordings from a broker account.

Full admission still requires separately trusted account/currency aggregation,
complete source scope, fresh quotes/marks/conversions, explicit owner policy and
units, current session/opening equity/cumulative cash-flow evidence, external
order counts and verified individual protection. Actual capture provenance,
account compatibility and complete venue coverage require their own authorized
validation. No new capture call, sender, command resolution, reservation release,
recovery activation or M4/M5/M6 qualification is supplied by this slice.
