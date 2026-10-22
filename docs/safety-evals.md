# SQL safety evaluation subprofile

PIP-49 connects development starter cases S02–S07 to isolated SQL safety APIs.
It reports the separate `sql-safety-v1` subprofile. It does not enable M1
capabilities or change the existing **14 passing / 16 deferred** starter baseline.
Starter source bytes, splits, normative contracts and original source timestamps
remain unchanged. Explicit synthetic supplements supply the missing complete
account/session/cash-flow facts, mapping and test-only numerical policies.
Those numbers are fixtures, never Henrique's risk choices.

## Three distinct layers

`pipmind.safety.eval_scenarios` binds each original JSONL line and explicit
supplement to their SHA-256 hashes. It prepares existing public admission/session
codec strings relative to an observed disposable SQL clock. Metadata retains the
source clock, first event's availability time, original account/quote times and
stale flag, exact microsecond offset and synthetic freshness offsets. This is a
declared test-time mapping, not historical market reconstruction.

The explicit SQL runner exercises the existing stores, obtains actual returned
and retained receipts, and captures before/after/reopened state. Mandatory
disposable SQL CI must execute that runner and its mutation controls. Collection,
pure fixture tests or an owner-supplied evidence object cannot establish that
SQL executed. No provider, model or broker client participates.

`pipmind.safety.eval_evidence` supplies frozen `CaseEvidence`, `Observation` and
`DurableSnapshot` DTOs and `grade_case(spec, evidence)`. The grader checks
**consistency of supplied evidence**, not database/provider provenance. It has
no SQL connection, external call, implicit clock or dispatch authority. Pure
tests deliberately construct coherent synthetic observations and do not label
them SQL execution proof.

## Case expectations

| Case | Actual path and required negative evidence | Positive control |
| --- | --- | --- |
| S02 | Admission retains `stale_quote`; exact original order, no new reservation | Same path with complete fresh quote admits/reserves once |
| S03 | Admission retains `reconciliation_failed` with actual stale account, orders and positions findings | Complete current source passes |
| S04 | Admission retains `order_size_limit` for unchanged oversized proposal | Explicit within-limit supplement passes |
| S05 | Admission retains `daily_loss_stop` after actual session breach and equity recovery; durable latch remains true | No-breach session passes |
| S06 | Separate actual paused and killed control states retain `trading_paused` / `kill_switch` gateway refusals | Explicit unpaused/unkilled gateway dry intake passes |
| S07 | Actual old grant yields `lease_stale_owner`; complete covered state is unchanged | Current owner dry intake passes |

S03 intentionally uses the actual earlier reconciliation refusal. It does not
pretend the later generic `stale_account` check ran. Extra unrelated findings,
missing setup, an arbitrary refusal or copying an expected guard flag do not
satisfy the scenario. S05 retains/reopens both breach and recovery receipts;
a supplied latch flag alone is insufficient. Controls never resume automatically.

## Retained evidence and grading

Each observation retains full canonical order projection and request body,
actual returned/stored/reopened receipt bodies, fixed exception code where
appropriate, clock mapping and safe attempted lease facts. The order projection
uses the existing public admission request codec; it introduces no Decimal
encoding or transport mapping. Grading reconstructs the frozen scenario inputs
from declared clock/account/session bindings and requires exact complete request
and metadata equality. A shrunk order remains a failure even if its hashes and
receipts are recomputed consistently.

The current session specification and state must also exactly match a fresh
reconstruction through the existing pure session transitions and all prepared
observations. Current lease expiry is checked against the captured SQL clock.
Gateway observations require the retained ready scope to bind the current owner,
generation, resource, epoch, session, book and mapping. For S06 only, the earlier
ready scope may retain its preceding control revision after explicit pause/kill;
it cannot silently switch owner or policy. S07 first reconciles the new current
owner, then attempts the old grant. A held or expired new owner cannot make the
old grant's refusal a complete passing control.

Snapshots reuse `checkpoint_sql.TableDigest` and the exact existing fifteen-table
inventory under the runner's required locks. Safe control/session/lease/hold
bodies accompany the inventory; raw lease tokens are excluded. Reopened state
must match the post-operation snapshot exactly. Refusals cannot grow or rewrite
the reservation book. S07 requires no change to any covered table or safe state
body; current-owner facts and the attempted old grant must differ in the expected
generation/owner relationship. This inventory is **not a complete application
backup**: later recovery/attribution/command journal tables are outside its
versioned coverage and are not certified by this profile.

`grade_case` returns only fixed failure identifiers. Its mandatory positive
control must actually be within limits and add one admission and reservation.
Target refusals require their specific actual code, durable receipt (except the
correct no-write lease refusal), exact request/ownership/session bindings and
unchanged protected state. Missing/reordered observations and missing inventory
tables fail. Mutations for wrong reasons, missing reopen proof, altered original
size, unintended reservations and stale-owner effects are tested independently.

This isolated profile requires initially empty admission, reservation and
external-count-floor tables for each case namespace. Grading recomputes the
existing pure `assess` result with the exact prepared policy, current session and
request at the retained admission receipt's actual `observed_at`; every assessment
field must match, including quantities, loss, gross exposure, margin and counts.
Receipt times must fit the observed SQL anchor, session interval and current
lease expiry. Every positive also requires the exact retained reservation body
and its reopened copy. Gateway positives additionally retain/reopen the full
underlying admission receipt; a thin gateway receipt and row-count change alone
cannot prove that the correct reservation was committed. This reuses enforcement
calculations and still does not attest where caller-supplied evidence originated.

`sanitized_report(specs, evidences, dataset_sha256=...)` requires all six cases
in fixed order and emits at most 16 KiB. Its allowlist contains profile/grader
versions, dataset/case/supplement hashes, case IDs, fixed failures, observation
counts and exclusions. It copies no account, scope, receipt, price, order body,
arbitrary exception text or credential. Full evidence belongs in a private
output location. The report explicitly sets `consistency_only=true`,
`sql_execution_attested=false`, `dispatch_authorized=false` and
`baseline_changed=false`; actual disposable SQL execution is established
separately by the required CI test result, not by changing these claims.

## Remaining qualification

S08 SQL loss before actual dispatch, S09 broker ambiguity, D06 persistent thesis
correction, native recovery/independent authority, real-model quality and full
M4/M5/M6 operational qualification retain their separate gates. No sender,
reservation lifecycle, live runtime wiring, deployment or order permission is
provided. Passing this profile cannot authorize a broker action.
