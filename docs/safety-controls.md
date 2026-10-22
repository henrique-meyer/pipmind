# Durable controls and separate suggestions

`pipmind.safety.controls_sql` is an isolated trusted-caller SQL API. It is not
connected to an owner UI, model tool, order transport or the hosted application.
Calling it requires an already authorized owner/control adapter; an `actor_id`
string records attribution and does not authenticate anyone. Do not expose its
mutations to model-generated tool calls or accept actor/origin fields from an
untrusted request. The private HTTP boundary supplies its own configured owner
identity and authorization separately.

## Initialization and state

Explicit `bootstrap(connect)` creates only the isolated control state, override,
suggestion and disposition tables. Bootstrap has a separate transaction-owned
schema lock. `SqlControlStore(connect, binding)` performs no SQL; binding is the
existing `SessionBinding(account_id, environment, currency)`. Its account key
excludes currency, so changing currency cannot bypass the permanent exact pin.

`initialize()` creates revision 0 with **paused=True, killed=False**. Repeating it
preserves current flags, revision and all records. Missing state is uninitialized,
never an unpaused default; reads/mutations raise
`ControlRefused("controls_uninitialized")`. A missing state with orphan records
is unavailable and cannot be recreated through initialization.

`read()` returns frozen `ControlState(binding, revision, killed, paused)`. The
current row must agree with the latest indexed immutable override revision and
its receipt's flags. Revision 0 requires no override history. Type, binding,
hash, canonical-codec, latest-index or effective-flag disagreements refuse usable
state rather than silently repair/recover. This is a bounded latest-row integrity
check, not universal SQL tamper detection or protection against restoring an
entire older database. Trusted external authority renewal and reconciliation
remain operational recovery requirements.

## Owner overrides

`apply_override(command_id, actor_id, action, expected_revision, reason)` accepts
only these actions:

| Action | Effect |
| --- | --- |
| `kill` | Set killed and paused. |
| `pause` | Set paused; preserve killed. |
| `resume` | Clear paused only; never clear killed. |
| `unkill` | Clear killed only; never clear paused. |
| `flatten` | Record `unsupported_not_executed`; preserve both flags. |

Every new override, including flatten or an already-set flag, increments the
revision exactly once. An exact existing command ID/body returns its original
receipt before comparing the old expected revision. Changed arguments under the
same ID conflict; a new ID with stale expected revision refuses without writes.
This permits explicit retry after acknowledgement uncertainty without accepting a
different command or silently applying stale owner intent.

Kill and pause remain until their respective explicit lifts. A resume while
killed leaves killed=True, so new risk remains blocked. Unkill does not imply a
resume; a previously recorded explicit resume remains its own command. No
expiration, model decision, suggestion or startup clears either latch. Flatten
records intent only, increments the audit revision and does not claim an order,
execution, fill, cancellation or flat account. There is no risk-boundary mutation
or arbitrary SQL callback API.

Override records/receipts always retain `origin="trusted_owner_override"` and
`review_excluded=True`. These tags must survive into any derived execution/audit
record; a later review cannot treat an owner override as autonomous evidence.

## Suggestions are separate records

`submit_suggestion(id, actor, text)` retains the exact original suggestion without
changing control state. `resolve_suggestion(id, actor, disposition, reason)`
records one immutable `adopt`, `trial` or `reject` disposition for an existing
suggestion. Exact retries return the original submission/disposition; altered
payloads or a second different disposition conflict. Unknown suggestion IDs
refuse. A disposition records a decision and reason only: it neither changes
robot policy nor executes an override or updates risk limits.

Submission origin is `owner_suggestion`; disposition origin is
`suggestion_disposition`. Both retain `review_excluded=False`, distinct from the
override exclusion. Their provenance alone is not proof that an idea is correct,
was autonomously learned or was executed. Suggestion work does not increment the
control revision or clear kill/pause flags.

## Receipts, bounds and SQL composition

Receipts are plain private JSON dictionaries with exact keys:
`schema_version`, `kind`, `record_id`, `body`, `body_sha256`, `observed_at`,
`origin`, `review_excluded`, `outcome`, `state`. The body contains the exact
binding and original method arguments; `state` contains binding/revision/flags
observed for that record. Original actor, text/reason and UTC server time remain
available. Immutable body and receipt bytes/hashes are stored separately as well.
There are no credentials, raw lease tokens or driver exceptions in the metadata.

IDs are nonempty stripped valid-Unicode strings, NUL-free and at most 128 UTF-16
code units. Text/reasons are exact nonempty valid UTF-8 strings, NUL-free, preserving
whitespace. Each canonical body and receipt is bounded to 64 KiB UTF-8; a receipt
can exceed its bound even when its body fits, in which case the transaction
refuses without truncation. SQL transfer guards admit at most 128 KiB UTF-16
before exact UTF-8/hash/shape validation. Audit history has no count or age cap.

Direct writes take only the account's control application lock. They never take
a lease, session or admission lock. The reviewed integration order is
**lease -> session -> controls -> integration account -> admission -> book**.
`read_in_transaction(cur)` checks the caller has a transaction and takes the
control lock using the supplied cursor; it opens no connection and commits
nothing. `lookup_override_in_transaction(cur, command_id)` additionally requires
the caller already holds that exclusive control lock, normally after the read.
`lookup_override(command_id)` is an independently locked bounded private lookup
for reopening/retry. Returned historical receipts do not establish current
control state or become execution permits.

Connections must be fresh, non-pooled and autocommit=False. Staged records and
state updates commit atomically. Disposal closes the connection without adding
the pinned pytds rollback handshake that can remove the transport timeout.
Pre-commit input/CAS refusals use fixed `ControlRefused` codes; corruption/SQL
failures use `ControlUnavailable("control_store_unavailable")`. Errors omit raw
arguments, actor identifiers, reasons and driver text.

Any exception or interruption after commit was attempted becomes
`ControlCommitUnknown("control_commit_unknown")`. No receipt, rollback proof or
automatic replay is returned. Reopen/read/lookup and deliberately retry only the
same exact stable command ID/body when reconciling it. Pre-commit interruption
propagates after connection disposal; cleanup failures cannot mask the primary
result or uncertainty.

Offline tests use transactional SQL doubles. Required disposable SQL CI covers
CAS races, control-read/kill serialization, actual staged error/interruption/
disconnect rollback, acknowledged commit with lost acknowledgement, persistence,
unsupported flatten, separate suggestion journals and latest-receipt integrity.
These checks do not imply a hosted change, real order safety or M5 completion.
