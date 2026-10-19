# Roadmap

Status of the repo today: **hosted M4 shadow foundation.** Initial real SQL/Foundry/feed/journal/clone flows are verified alongside the preserved landing. Full provider/broker coverage, a complete FX day and model-quality/capacity/cost qualification remain open. The gateway has no order submission path; there are no orders or public track record.

The rollout order is fixed even where the milestone numbers have groundwork in between:

**replay harness → shadow mode (decisions, no orders) → demo orders → self-review on → public launch.**

Live trading is not one of these milestones. A Forex.com account is bound to a single environment. M6 points the gateway at the broker's demo environment. Pointing it at a live account is a later owner decision, after demo reconciliation has been clean.

Numeric risk limits are chosen before M6, as configuration, not inside a model prompt. They are unset on purpose in the design docs.

The hosted demo profile is one Azure Container App with two containers, default managed networking, Azure SQL S0, Blob snapshots, a free static frontend, built-in secrets, and essential monitoring. X is read-only throughout. The application keeps independently runnable components and configurable infrastructure for stronger, more expensive production deployments. The [implementation plan](plan.md#demo-delivery-requirements) maps these requirements to the milestones below; the [cost model](cost-model.md) uses an $180 monthly demo planning budget. The landing-only environment is deployed; the full trading profile is not provisioned, and additional resource creation still requires approval.

## M0 — Design baseline

Public repo, AGPL-3.0, this documentation, the ADRs, and the example social-role config.

Done when the docs are on `main` and the architecture diagram renders.

## M1 — Contracts and replay harness

**Delivered by [PIP-7](plan.md#pip-7--m1-contracts-and-replay-evaluations).** See [the replay guide](replay.md): 30 starter cases, 13 passing applicable cases, 17 explicit deferrals, mutation-tested graders, bounded synthetic bundle validation and a reproducible baseline. The fixture week has 20 events, 15 scripted decisions, five unsent proposals and zero dispatches. Gateway/SQL/review/publication controls and real model quality are not certified by M1.

Check the JSON schemas in as the source of truth. GitHub Actions runs lint and schema checks on fakes, with no Azure calls and no broker calls.

Define the [Markdown mind portability contract](architecture.md#markdown-mind-export-and-import): a ZIP-based `.pipmind` bundle with a versioned manifest, complete retained five-part mind, checksums and compatibility metadata. Specify experience-clone and original-trader recovery semantics, including a matching operational sidecar for recovery. Add synthetic round-trip and corrupt/incomplete/incompatible/unsafe-archive fixtures; mark SQL/recovery checks deferred until their actual paths exist. A saved mind does not guarantee identical future trades.

Include the [evaluation foundation](architecture.md#evaluations-and-replay): 30 versioned starter scenarios covering triage, decisions/memory, safety/recovery, self-review, public redaction and X read-only behavior; deterministic graders tested with valid and deliberately invalid traces; and a saved baseline report. Case metadata identifies required capabilities, initial state, clock, event availability, expected constraints and evidence. Preserve chronology, isolate case/variant memory and reserve held-out episodes.

PR CI validates fixtures and graders, runs the replay and all applicable mandatory checks without credentials, and reports baseline differences and deferred coverage. Cases for unimplemented gateway/SQL/review/publication paths remain visibly deferred until their milestones; tests of fake traces cannot certify those controls. Optional real Microsoft Foundry comparisons and model judges are explicitly triggered, versioned and cost-recorded, with repetitions and human-calibrated rubrics.

The harness replays a recorded week of `ReleaseEvent`, `NewsEvent`, `PostEvent` and `PriceEvent`. The core writes decisions and reasoning. It has no gateway.

Done when a fixture week runs from the command line, resulting decisions and a baseline report are stored in ignored output next to the fixture, the 30-case dataset validates, graders reject seeded failures, and CI passes every mandatory check for delivered capabilities while listing later-milestone cases as deferred. No real broker dispatch is possible from the harness.

## M2 — broker-gateway

**Delivered by [PIP-8](plan.md#pip-8--m2-dry-broker-gateway).** The [gateway guide](gateway.md) documents the separate container/internal service, optional read adapter and 15-check synthetic recorded session. S01 now exercises actual durable mapping refusal: 14 starter cases pass and 16 remain deferred. Actual broker connectivity, hosted SQL/fencing and ambiguous venue recovery remain subject to their explicit gates; no broker account or Azure resource was changed.

Forex.com CIAPI implementation: Trading API v2 session, Lightstreamer prices, REST fallback, stale-stream watchdog, re-login on an expired session, symbol map, snapshots.

`OrderRequest` is accepted and acknowledged in a dry mode that does not send anything to the venue. `client_order_id` dedupe works in that mode.

Done when a recorded session shows a quiet stream flipping to REST and back, and a repeated command id producing a single acknowledgement. Activate the corresponding M1 gateway/recovery cases against the real adapter in dry mode.

## M3 — Information feeds

**Delivered by [PIP-9](plan.md#pip-9--m3-information-feeds).** The [feed guide](feeds.md) covers the isolated worker/container runtime, durable local inbox/cursors, FXMacroData stream/webhook, hourly ForexFactory cache, RSS/Atom/relay and read-only role-based social inputs. Fourteen synthetic logic gates and actual kill/restart checks pass offline. Live provider compatibility and hosted SQL/delivery wiring remain M4 validation work; no vendor or Azure account was changed.

`macro-feed`, `news-feed` and `social-feed` emit only their own event type and run as independently supervised engine workers in the demo. X supports post reads and necessary account/role lookups only, with no write features or permissions.

Done when each feed can be killed without silencing the other two; the ForexFactory file is served from cache inside the hour; a fixture holder change moves the personal handle and leaves the institutional account in place; a broken lookup raises an alert and does not clear the holder.

## M4 — Shadow mode

**Application foundation delivered by [PIP-12](plan.md#pip-12--m4-shadow-runtime-and-persistent-mind), initial hosted deployment by PIP-13.** See the [shadow guide](shadow.md) for the SQL adapter, Foundry connector, private engine/feed container, mandatory checks and paused experience clones. PIP-15 separates compact model context from full audit retention; PIP-14 still owns complete provider/broker coverage, model quality, full-day capacity/cost/export and independent zero-order evidence. A successful initial deployment does not close M4.

Wake filter, `gpt-6-luna` triage, `gpt-6.1-sol` decisions, persistent mind. The core logs `enter`, `add`, `exit` and `hold` with reasoning and updates the mind. It sends no `OrderRequest`.

Shadow starts on the M1 harness, then on live events once M2 and M3 are up. Validate the shared demo allocation (0.5 vCPU / 1 GiB total), SQL-backed state/search, and actual API/model costs while proving zero broker orders.

Implement consistent SQL-backed mind export and staged atomic import. Experience clone creates a new trader lineage, preserves retained learning/provenance, archives source position theses as history, and initializes destination account state separately. Start paused and validate in replay/shadow; imported account history, commands, permissions, overrides and numerical risk limits cannot become active destination state. Verify full retained-record round trips, exact identity/playbook text, rebuilt retrieval and failure with no active-state mutation. Model weights, secrets and live-trading authorization are outside the bundle.

After resource approval, provision the Microsoft Foundry resource/project and model deployments, verify regional availability/quota and prices, and configure managed-identity access through the Agent Framework Foundry connector. Keep the agent loop, tools and mind in Container Apps/SQL. Verify a project-backed inference call before the shadow run; provisioning a Foundry-hosted agent runtime is outside this baseline.

Done when a trading day of live events produces a journal and zero broker orders, and SQL-backed export/import and experience-clone checks pass with the documented transformations and no order submissions.

M4 also maintains bounded current state with audited before/after dispositions and retrieval of historical detail; unresolved conditions and confirmed theses must survive consolidation. Bounds/transition failures hold the pending input and pause reasoning. Playbook self-revision stays in M7.

PIP-16 supplies a [blank starter](../templates/blank/README.md) compatible with existing paused initialization and the [private owner chat design](architecture.md#private-owner-chat-and-confirmed-edits). Future owner-interface work adds separate blank/clone/recovery onboarding, evidence-grounded explanations, suggestions and confirmed direct chat edits. It does not reset the hosted trader, close PIP-14 qualification or change autonomous nightly/market/experience learning. The UI and durable chat-edit path remain planned.

Also define the [compact robot-save profile](architecture.md#compact-robot-saves-and-restore), separate from full experience/recovery and account history, with named manual saves, compatible imports, exact rules/guide round trips and confirmed restore as a new version. Robot identity/training/lessons and account data/authority are distinct. These planned controls do not reinterpret existing runtime bundles or certify M4 qualification.

The planned [robot library](architecture.md#robot-library-and-active-working-copy) holds many inactive imported/saved starting points, including external robot-only files. Load creates an independent unnamed Active robot working copy; Save a copy preserves it as an immutable library item, without changing the source save. Provide favorites and bounded rolling autosave retention with protected manual/imported/favorite saves. Validate archive safety, provenance and compatibility before an explicit Load; future sharing does not authorize a public service.

## M5 — Safety and reconciliation

Outer hard boundary with real numbers in configuration, daily-loss stop fed by `AccountSnapshot`, single-instance lease, remote kill switch, suggestion channel and override channel.

Owner-chat controls add version-bound exact-diff confirmation only for edits made through chat. Verify atomic apply/audit, stale/expired/replayed/concurrent request refusal, interruption rollback, immutable-boundary checks and clone/recovery separation through actual SQL paths. Grounded questions/suggestion discussion remain distinct from direct edits. Test that an autonomous revision invalidates a conflicting pending chat diff and that pending/rejected chat edits never block or silently execute through existing nightly review/learning. M7's autonomous self-review contract remains unchanged.

Implement explicit robot/account/assignment identities and owner-confirmed replacement even with open positions. Fence the old runtime, reconcile pending/ambiguous execution, supply the new robot with current information/obligations and atomically change assignment; preserve account protections, historical robot attribution and one active owner. Restore robot text through a versioned pre-restore save without rewinding account/market/operational state or automatically resuming. Test failure, stale outputs, concurrent review/saves and incompatible position obligations through SQL; validate actual demo handover/no duplicate execution at M6. Autosave precedes any durable robot-policy change, including owner edits/restores, without gating autonomous learning on human consent.

Test atomic working-copy load, generation-isolated retrieval rebuild, stale learning-output refusal, exact restored learned content and unchanged account data. Prove favorite/unfavorite/load/cleanup races and shared-content references cannot delete protected saves, active content or account/audit records. External imports must remain inactive and cannot set Auto Learn, permissions, tools or account authority.

Reconciliation takes positions and balances from gateway snapshots. A mismatch halts new risk.

Implement original-trader recovery from a `.pipmind` recovery bundle with matching operational checkpoint, command/deduplication records, pending outcomes and stop/override state. Validate the same privately configured broker account/environment, fence out the previous owner even after database restore, acquire fresh ownership, and reconcile activity since export before explicit owner resumption. Test stale checkpoints, late/ambiguous orders, account mismatch, interrupted import and overlapping owners through the actual SQL/gateway paths; unresolved or missing state keeps dispatch disabled. A mind-only bundle is insufficient for recovery.

Done when a request outside the boundary is refused and journaled; a second or stale owner cannot send during revision overlap; SQL loss blocks dispatch; a stale snapshot blocks new opens; an override is executed, tagged, and absent from the self-review evidence set. Exercise graceful handover, external heartbeat failure, and a restored database followed by broker reconciliation. Activate all corresponding M1 safety cases against actual enforcement paths, including SQL Server integration, and require every mandatory case to pass.

## M6 — Demo orders

The gateway sends orders to the demo environment. Open, modify and close go through `OrderRequest`. Fills and snapshots come back. The broker's snapshot wins any disagreement.

Done when a scripted round-trip is flat at the end and the core's position matches `PositionSnapshot` without a manual edit. Exercise recovery with known and ambiguous demo-order outcomes, proving no duplicate dispatch, preserved protective orders and agreement with fresh broker snapshots before resumption.

## M7 — Self-review

The owner-facing feature is **Auto Learn**, implemented by scheduled learning from daily activity/evidence. On keeps autonomous add/trial/sharpen/retire rules; Off freezes every automatic durable robot rule/guide/lesson writer, including in-flight commits after the setting is acknowledged. Current market/account information, decisions, journals and protections continue. Explicit owner edits/loads remain possible. Keep this owner control across restart/load and outside imported robot instructions; do not silently replay queued learning when re-enabled.

The nightly review can add a rule, sharpen one, or retire one. Retirement requires repeated evidence. Playbook versions are stored, and a public diff document is produced. Override-tagged rows do not count as evidence.

Before writing a changed robot policy, atomically preserve a durable pre-review compact robot save. No owner confirmation is required for autosave or the autonomous review. Test exact pre-change content, save failure without policy mutation and compatible manual restore as a new version; review/evidence/override semantics remain unchanged. Account history, positions and risk state never enter or rewind with that robot save.

Exercise configured rolling autosave expiry/count limits while preserving favorite/manual/imported saves and in-flight dependencies. Test Auto Learn Off against every automatic mutation/retrieval path, pause-vs-learning independence, restart/load persistence, and exact saved learned content without post-save lesson leakage. These are implementation gates, not existing runtime behavior.

Done when a fixture journal shows an add, a sharpen, and a refused retirement that cited only one outcome. Activate the M1 self-review cases against the implementation, including valid repeated-evidence retirement, override exclusion and evidence grounding. Verify clone/recovery round trips preserve current/retired rule versions, review and suggestion history, evidence references and override exclusions; subsequent reviews continue monotonic versions.

## M8 — Public launch

Azure Static Web Apps Free serves the frontend, which reads sanitized public Blob snapshots: own fills, P&L, reasoning, playbook diffs, override labels, disclaimer. The snapshot has no bid, no ask, and no raw account snapshot. Visitors do not reach the broker or the models. Archives remain private; caching and visible snapshot age are validated. Confirm provider display rights and any required add-on before launch.

Daily health and the remote kill are running. Spend alerts are running. The page states that this is a public experiment, not investment advice, with no signals, no copy-trading and no affiliate links.

Done when the site still renders with the trading host firewalled off from the public internet, and a published snapshot contains no quote stream.
