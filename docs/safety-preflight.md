# Pure numerical safety preflight

PIP-35 provides `pipmind.safety.preflight(order, account, facts, policy, *, now)`.
It is an offline deterministic calculation, with no orders, network, SQL,
configuration loading or runtime integration. A result reports `within_limits`,
a fixed `code`, and immutable Decimal calculation pairs. Its
`dispatch_authorized` property is always false. Passing is not M5 enforcement,
reconciliation, ownership fencing, permission to trade, or a broker guarantee.

## Required explicit inputs

Construct `RiskPolicy`, `RiskFacts`, `ProposalFacts`, `Conversion` and `Exposure`
from `pipmind.safety`. All fields are required; there are no owner risk defaults.
Every price, quantity, amount, conversion and monetary policy limit must be a
finite **Decimal**, not a float, bool, integer or numeric string. JSON callers
must parse numeric amounts directly as Decimal before calling; do not first
round through a float. Schema versions, session counts and age limits use strict
integers; switches use strict booleans. No example numbers here choose Henrique's
limits; the test suite's limits are synthetic fixtures only.

The existing [OrderRequest](../contracts/v1/order-request.schema.json) and
[AccountSnapshot](../contracts/v1/account-snapshot.schema.json) remain normative.
The preflight validates both and requires additional facts their contracts do
not supply. It supports **open** proposals only, for explicitly configured
`AAA/BBB` FX pairs. This format is a preflight restriction, not a guarantee made
by the OrderRequest contract, whose instrument is any string. An add can be
represented by an additional open; exposures are gross and never netted against
opposite-side positions. Close/modify requests receive `unsupported_action`;
they are not certified as risk reducing.

- Policy binds one instrument, account ID, demo/live environment and account
  currency. It explicitly supplies order/base-unit and instrument/gross exposure
  caps, per-order/total planned-loss caps, session daily-loss/daily-risk caps,
  order count, minimum remaining margin and three freshness windows.
- Order size is in the instrument map's declared core unit. The caller supplies
  positive `base_units_per_size`; no lots, pip values, increments, broker
  rounding or contract multipliers are assumed. Broker mapping/representability
  remains a separate required check.
- Account equity and both margin values are required, even though optional in
  the transport contract. Supported snapshots have positive equity,
  nonnegative balance/margins and `equity == margin_used + margin_available`.
  Different margin accounting is unsupported and refused rather than guessed.
- Facts must match the account timestamp and explicit account/environment
  binding, and say complete/reconciled. Those flags are **caller evidence**, not
  independent attestations. Include every position and pending/reserved
  risk-increasing obligation in `exposures`; exclude none because hedged or old.
  Entries use unique obligation IDs, base-unit quantity, side, a fresh reference
  price, protective stop and explicit quote-to-account conversion. For a held
  position the reference price must be its current liquidation-side mark, not
  historical entry; for a pending entry it is the supplied entry bound.
- A fresh bid/ask is required for the proposal. Market buys use ask, sells use
  bid for planned entry; limit orders use their explicit limit price for that
  calculation. Proposal gross valuation independently uses the greater of ask
  and planned entry, so a marketable sell limit below bid cannot reduce gross
  notional. All require a positive
  protective stop on the loss side of that reference. Missing/crossed/unknown
  stops refuse. These are planned stop-distance estimates; gaps, slippage,
  execution, commissions, financing and guaranteed-stop status are not modeled.
- Every valuation requires an explicit positive conversion from the pair's
  quote currency **to** account currency. The rate means account units per one
  quote-currency unit. No reciprocal, triangulation or live lookup occurs. Even
  identity conversion is explicit and must equal1. Supplied rates are not
  independently verified or reconciled to other market quotes here.
- Caller supplies proposal margin required in account currency. Available margin
  must already reflect existing/reserved obligations; this function cannot
  authenticate broker margin rules or completeness. It subtracts only the new
  requirement, without estimating leverage or margin rates.
- Session start/end, opening equity, signed external net cash flow, count
  including reserved opens and a latched daily-loss stop are explicit. No local
  midnight/time-zone reset is invented. New risk refuses at a reached or latched
  loss stop; recovery never clears the latch here.

All timestamps are explicit UTC strings; `now` is an aware UTC datetime supplied
by the caller. Future facts, facts newer than the account checkpoint, expired
sessions or exceeded age windows refuse. Equality at the freshness deadline is
accepted; a later microsecond is stale. This function cannot prove source clocks
or provider freshness, and does not replace the gateway's stale classifications.
The quote-not-newer-than-account rule is deliberate: this slice accepts a
coherent supplied checkpoint. A fresh quote newer than that account snapshot
requires a refreshed coherent fact set; the function does not silently combine
checkpoints or assume the older account is still reconciled.

## Exact supported calculations

For each valuation with base units `q`, reference price `p`, protective stop `s`
and quote-to-account rate `r`:

- Existing gross exposure is `q * p * r`; every obligation adds, without hedge
  offsets. Proposed gross exposure uses `q * max(ask, planned_entry) * r`.
- Planned loss is `abs(p - s) * q * r`, with the required stop direction checked.
- Proposed base units are `OrderRequest.size * base_units_per_size`.
- Daily loss is `max(0, session_start_equity + external_cash_flow - equity)`.
- Daily risk is daily loss plus all existing and proposed planned losses.
- Remaining margin is account available margin less the new margin requirement.

All numerical caps are inclusive except daily-loss stop, which refuses on
equality. A new open also requires `orders_this_session + 1 <= max_session_orders`.
These calculations prove only compliance with the supplied facts and owner
limits. They cannot prove that stops will cap realized losses or that the input
inventory is true, complete, current or authorized. No sizing recommendation or
smaller retry is produced.

Decimals are limited to64coefficient digits and exponents between-64and64 for
this calculator's representable input domain. Arithmetic uses an isolated
1024-digit context with rounding/inexact traps; unsupported capacity refuses
`numeric_capacity_exceeded`. This is a representation bound, not an owner risk
default. Inputs, their Decimal values and the caller's Decimal context remain
unchanged; no quantization or silent rounding occurs.

## Refusals and later integration

Fixed codes distinguish invalid policy/contracts/facts, unsupported actions or
instruments, account/environment/currency/checkpoint mismatches, missing equity,
margin/units/stops/conversions, incomplete/unreconciled facts, future/stale data,
invalid sessions, duplicate exposures, kill/loss stops and each exceeded cap.
No raw input is interpolated into a refusal. At numerical boundaries the result
also retains the calculated values. The caller must persist the exact proposal,
policy identity/version, input evidence and result together for an audit; the
result alone is neither a durable journal nor a reusable authorization token.

M5 still needs authoritative broker/account binding and reconciliation, durable
kill/loss state and counters/reservations, atomic concurrent admission, lease
fencing, execution-time freshness/rechecks, broker units/stops/margin validation,
ledger/idempotency and uncertain-outcome handling. M6/demo or live trading still
needs explicit owner authorization. This module is not connected to the shadow
application, gateway or any dispatch path.
