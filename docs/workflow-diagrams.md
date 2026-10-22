# Workflow diagrams

Read [infrastructure diagrams](infrastructure-diagrams.md) for where these steps
run. These workflows use generic labels and describe the repository contracts
as of **2026-10-22**, not live instance health. Each section states whether it is
the implemented shadow path, an isolated dry implementation, or a future path.

PipMind has several loops: provider delivery, event-driven reasoning, future
order reconciliation, future nightly learning, and operational recovery. The
end of one event's processing returns to waiting for another event; it does not
end the trader.

## 1. Provider intake and durable delivery

**Implemented foundation; live coverage and outage/backfill qualification remain
open.** The workers run independently. X access is read-only.

```mermaid
flowchart TB
  start[Worker starts or reconnects] --> cursor[Read durable cursor and provider state]
  cursor --> fetch[Poll, stream or receive a delivery]
  fetch --> validate{Valid signature where required,<br/>schema, identity and chronology?}
  validate -->|No| refused[Retain refusal or quarantine evidence<br/>Expose source gap]
  refused --> wait[Wait / provider-specific backoff]
  validate -->|Yes| persist[Commit inbox, canonical event,<br/>cursor and outbox atomically]
  persist --> ack[Acknowledge durable acceptance<br/>Webhook 2xx only after commit]
  ack --> handoff[Deliver pending outbox page to shadow]
  handoff --> accepted{Shadow acceptance committed?}
  accepted -->|No or acknowledgement lost| replay[Retain outbox and retry exact identities]
  replay --> handoff
  accepted -->|Yes| checkpoint[Advance delivery checkpoint]
  checkpoint --> wait
  wait --> fetch
```

SQL retains accepted source facts before reasoning. Exact redelivery deduplicates;
changed facts under an existing identity refuse rather than overwrite history.
SSE reconnect uses the saved cursor. Calendar caching and provider-specific pacing
prevent a tight retry loop. Signed webhook code exists; the diagram does not
claim an approved public callback route is deployed.

## 2. Continuous shadow reasoning loop

**Implemented M4 path.** It produces journaled observations, decisions, refusals
and unsent proposals. It cannot place an order.

```mermaid
flowchart TB
  boot[Start / restart] --> load[Load committed SQL mind, inputs and checkpoints]
  load --> running{Reasoning enabled<br/>and no durable hold?}
  running -->|No| paused[Remain paused<br/>Feed ingestion can continue]
  paused -->|Explicit authorized shadow resume| running
  running -->|Yes| next[Take next durable input<br/>Include current broker and feed facts]
  next --> wake{Input handling}
  wake -->|Snapshot or quote window not due| retain[Retain evidence / refresh observations<br/>No model call for this handling path]
  wake -->|News, release, post, due price or clock wake| context[Build bounded current-state triage context]
  context --> triage[Triage model]
  triage --> needed{Strong decision needed<br/>and input is not analysis?}
  needed -->|No| record[Prepare triage-only outcome]
  needed -->|Yes| evidence[Select dated and contrary evidence<br/>Build decision context]
  evidence --> decide[Decision model<br/>Bounded archive search if needed]
  decide --> valid{Output and memory transition valid?}
  valid -->|Yes| decision[Prepare decision and updated working memory]
  valid -->|Ordinary model/schema failure| refusal[Prepare retained model refusal<br/>No invented decision]
  valid -->|Repairable working-memory transition| repair[One bounded memory correction attempt<br/>Keep action and evidence fixed]
  repair --> repaired{Corrected transition valid?}
  repaired -->|Yes| decision
  repaired -->|No| hold
  valid -->|Working-state invariant or bounds failure| hold[Retain failed attempt, hold input<br/>Durably pause reasoning]
  context -->|Mandatory context cannot fit| hold
  triage -->|Timeout or invalid response| refusal
  retain --> commit[Atomic SQL completion<br/>Evidence, outcome, trace, usage and checkpoint<br/>Check expected mind revision]
  record --> commit
  decision --> commit
  refusal --> commit
  commit -->|Committed| next
  commit -->|Stale revision / interrupted / uncertain| inspect[Reload durable state before retry<br/>Do not claim an unacknowledged commit]
  inspect --> running
  hold --> paused
```

The mind combines identity, playbook, current views/theses/waiting conditions,
journal and searchable older evidence. Full audit stays in SQL; bounded prompt
projections do not delete history. Triage chooses whether to involve the stronger
model. Deterministic aggregation is transport/wake management, not a hidden
trading strategy. Clock inputs let the loop wake without a new headline.

SQL is not locked across a long inference call. The completion checks the mind
revision before committing, so a pause or concurrent change invalidates stale
output. A crash before completion can repeat a billable inference call but cannot
duplicate an already committed outcome. Holds need inspection and explicit
resumption; they are not automatically erased by a retry.

## 3. Proposed action to confirmed broker outcome

**Future integrated execution loop.** Isolated M5 dry safety/admission/command
code exists. It permanently denies dispatch authority. M6 demo venue transport
and its operational qualification are separate work; no current shadow arrow
enters this execution path.

```mermaid
flowchart TB
  proposal[Model proposes enter, add, exit or hold] --> action{Action requires an order?}
  action -->|No| journal[Journal decision and continue observing]
  action -->|Yes| checks[Fresh owner/lease and control checks<br/>Reconcile account and protective orders<br/>Apply explicit risk policy and loss latch]
  checks --> ready{Complete, current and within boundary?}
  ready -->|No| refuse[Retain refusal or revalidation hold]
  ready -->|Yes| reserve[Atomically reserve gross risk<br/>Retain exact command intent and identity]
  reserve --> final[Fresh pre-send ownership, controls<br/>and evidence check]
  final --> permitted{Actual execution path authorized?}
  permitted -->|No - current dry implementation| dry[Historical dry receipt only<br/>No send and no reservation release]
  permitted -->|Future qualified demo path| attempt[Retain attempt obligation before send]
  attempt --> send[Gateway submits exact command to demo venue]
  send --> known{Venue outcome known?}
  known -->|No / connection lost| uncertain[Keep unresolved obligation<br/>Read venue evidence - never blindly resend]
  uncertain --> reconcile[Reconcile orders, fills, positions and balances]
  known -->|Yes| reconcile
  reconcile --> matched{Fresh and consistent outcome?}
  matched -->|No| blocked[Hold new risk and resolve discrepancy]
  blocked -->|New trusted evidence| reconcile
  matched -->|Yes| confirmed[Commit confirmed outcome and account facts<br/>Preserve original attribution]
  confirmed --> journal
  journal --> observe[Wait for next event or account update]
  observe --> proposal
```

Only the gateway speaks the venue protocol. The broker is authoritative for
positions and balances; an unsent proposal or dry receipt is not a fill. Exact
command identity prevents a retry from becoming a second command. The current
command journal retains unattempted/attempted/unknown obligations; terminal
resolution and reservation release must be implemented and verified before this
future loop can clear them. A new process or restored database cannot grant
itself fresh authority from an old receipt.

## 4. Autonomous learning and owner influence

**Planned learning and owner-interface behavior.** Current working-memory updates
are different from rewriting learned rules. Library/Auto Learn controls have a
separate implementation workstream; this diagram does not claim they are hosted.

```mermaid
flowchart TB
  daily[Daily review trigger] --> enabled{Auto Learn enabled?}
  enabled -->|No| frozen[Keep durable learned policy unchanged<br/>Facts, decisions and journals continue]
  frozen --> daily
  enabled -->|Yes| collect[Collect attributed outcomes and evidence<br/>Exclude override-tagged outcomes from learning]
  collect --> review[Review rules and lessons<br/>Add, trial, sharpen or retire with evidence]
  review --> change{Justified policy change?}
  change -->|No| unchanged[Record review with unchanged policy]
  change -->|Yes| save[Durably save exact pre-change robot]
  save --> gate{Same base and Auto Learn still on?}
  gate -->|No / save fails| failed[Keep policy unchanged<br/>Retain conflict or failure]
  gate -->|Yes| commit[Atomically commit new policy version and audit]
  commit --> next[Use new policy in subsequent decisions]
  next --> daily
  unchanged --> daily

  chat[Private owner conversation] --> intent{Owner intent}
  intent -->|Question| answer[Explain recorded evidence<br/>No policy mutation]
  intent -->|Suggestion| suggestion[Retain nonbinding suggestion<br/>Adopt / trial / reject with reasons]
  suggestion --> collect
  intent -->|Direct text edit| diff[Prepare exact diff against current version]
  diff --> consent{Authenticated exact-diff confirmation<br/>and current unexpired base?}
  consent -->|No| pending[Pending, rejected or stale<br/>No policy mutation]
  consent -->|Yes| edit[Save pre-change robot and atomically<br/>apply confirmed edit as a new version]
  edit --> next
```

Enabled autonomous review does not wait for owner approval. Confirmation applies
to the direct owner edit. A pending edit never blocks independent learning; a
learning commit can make that edit stale. Auto Learn Off must also reject stale
in-flight automatic writers. Pause/kill/flatten are separate deterministic
controls and cannot depend on a model agreeing; flatten has no execution in the
current dry implementation.

## 5. Save, clone and recovery are different workflows

**Experience export/paused clone is implemented.** Compact library saves are a
separate workstream. Recovery/handover has an isolated dry reference integration;
actual hosted S0 identity, permissions and independent authority/barriers remain
operational qualification gates.

```mermaid
flowchart TB
  goal{What should be preserved?}
  goal -->|Rules, guides and distilled lessons only| compact[Compact robot save / library item<br/>Separate planned profile]
  compact --> load[Validate chosen save and confirm Load<br/>Create independent active working copy]
  load --> separate[Reconcile current account and obligations<br/>Do not rewind trades, balances or risk]

  goal -->|Retained mind and experience| checkpoint[Read one consistent complete SQL checkpoint]
  checkpoint --> bundle[Build and validate private experience bundle<br/>Inventory, hashes, relationships and limits]
  bundle --> clone[Atomic import into new empty destination<br/>New lineage - source context becomes history]
  clone --> paused[Destination starts paused<br/>Separate account state and authority]

  goal -->|Original trader and operational obligations| backup[Trusted full database backup<br/>Matching complete operational sidecar]
  backup --> fence[Fence independent authority<br/>Acknowledge original-source application barrier]
  fence --> restore[Restore inactive target<br/>Verify physical identity and complete checkpoint]
  restore --> barrier[Acknowledge target barrier<br/>Preserve controls, command history and attribution]
  barrier --> facts[Reconcile fresh broker facts and every<br/>unresolved command / account obligation]
  facts --> safe{All activation gates satisfied?}
  safe -->|No| held[Remain held - never invent resolution]
  held -->|New verified evidence| facts
  safe -->|Yes| confirm[Explicit owner confirmation<br/>Fresh authority and assignment generation]
  confirm --> activate[Only separately qualified runtime may resume]
```

A compact save cannot replace a complete experience archive or native database
backup. A clone cannot inherit live account authority, active source positions or
pending owner consent. Recovery cannot reset a daily-loss latch, clear an unknown
order, or reactivate an old lease. An external fence alone is not proof that an
old application transaction stopped; source and target barriers are separate.
Current dry command obligations can deliberately keep activation held because
there is no verified terminal-resolution path yet.

Exports fail if a complete checkpoint exceeds its selected format limits;
neither history nor a bundle is silently truncated. Private bundles stay outside
source, images and public snapshots. See [experience bundles](shadow.md#experience-bundles-and-cloning),
[recovery](safety-recovery.md) and [complete command checkpoints](safety-command-recovery.md).

## 6. Public record publication and visitor reads

**Planned M8 path.** The deployed landing is informational and has no track record.

```mermaid
flowchart LR
  sql[(Committed private SQL evidence)] --> publisher[Select public fields and redact]
  publisher --> validate{Publication contract valid?}
  validate -->|No| retain[Refuse publication<br/>Keep last valid snapshot and alert]
  validate -->|Yes| blob[(Publish versioned sanitized JSON to Blob)]
  blob --> frontend[Static frontend reads snapshot<br/>Shows timestamp and staleness]
  visitor[Visitor opens / refreshes page] --> frontend
  sql -->|New confirmed result or policy diff| publisher
```

Publish the account's own confirmed fills, P&L, recorded rationale, public
playbook diffs and override labels after the required rights/redaction checks.
Raw quotes, vendor feeds, secrets, account snapshots and private audit remain
private. Visitor reads do not trigger reasoning or brokerage. Publication failure
must not manufacture a result or hide the age of the last successful snapshot.

## Reading the loops together

1. Providers keep delivering and SQL retains accepted inputs independently.
2. The shadow core consumes inputs, reasons where needed and persists outcomes;
   the next input sees the newly committed mind.
3. A future qualified execution path closes its loop with confirmed broker facts.
4. A future enabled review loop updates learned policy for subsequent decisions.
5. Save/clone/recovery are explicit operations with different account semantics.
6. A future publisher exposes a sanitized record independently of visitor traffic.

Detailed contracts: [feeds](feeds.md), [shadow](shadow.md),
[gateway](gateway.md), [dry safety integration](safety-integration.md),
[command journal](safety-command-journal.md),
[owner chat](architecture.md#private-owner-chat-and-confirmed-edits),
[Auto Learn](architecture.md#auto-learn-and-exact-saved-mind), and
[rollout order](roadmap.md).
