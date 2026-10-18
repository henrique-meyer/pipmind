# Implementation plan

The [roadmap](roadmap.md) defines the application milestones and rollout order. This file tracks requested work against Jira stories; documentation and branding do not change the trading rollout.

## PIP-15 — Compact model context and hosted token measurement

- Story: [PIP-15](https://nitro.atlassian.net/browse/PIP-15)
- Status: In Progress
- Scope: phase-specific compact triage and relevant decision evidence, non-recursive journal/memory projections and bounded read tools, while preserving full accepted input and audit traces in SQL. Explain the existing Markdown saved-mind and curated knowledge-library fit without creating a second live-state store.
- Acceptance: regression checks for large recursive histories, relevant/conflicting evidence, provenance/time/trader isolation and actual SQL audit/export retention; existing CI remains green. Deploy only the authorized backend release with unchanged resources/quota/landing, then measure actual tokens/latency/refusals against the previous behavior on retained representative inputs. Keep controlled comparisons separate from live samples and monthly cost assumptions.
- Boundaries: no history deletion/rewrite, event dropping, deterministic trading strategy, new provider/resource purchase, broker execution or M7 playbook mutation. Full-day qualification remains PIP-14.
- Memory continuity: every decision retains current views, all position theses and waiting conditions. Search the whole retained archive without an age cutoff; preserve month-old relevant and contrary evidence amid unrelated recent traces, with dates/provenance and explicit retrieval limits. A recent window is not a replacement for the persistent market state.
- Implementation checks: phase-specific prompt views and exact request audit, structured journal projections, query-aware source passages, whole-archive/source-ID retrieval and separate source/journal candidate pools. Eight new offline regressions cover recursion, continuing state, long articles, unstructured archive content, wire context, tool projection and oversized mandatory-state refusal. The real SQL suite adds month-old/contrary evidence, legacy-noise, time/trader isolation and unchanged-audit checks. Hosted paired measurement follows the authorized release; complete-day quality/cost remain PIP-14.

## PIP-12 — M4 shadow runtime and persistent mind

- Story: [PIP-12](https://nitro.atlassian.net/browse/PIP-12)
- Status: Complete (application portion); hosted deployment is PIP-13 and complete-day qualification is PIP-14
- Scope: shadow-only wake/triage/decision loop, project-backed Agent Framework Foundry adapter, SQL-backed five-part mind and bounded retrieval, durable input/consumer checkpoints, consistent Markdown export and staged atomic experience-clone import. Close feed delivery/reprocessing prerequisites and add private CLI/container operation and CI.
- Acceptance: actual local SQL Server persistence/restart/transaction and clone round trips; replay/injected model contract checks; schema/chronology/evidence validation; failures preserve durable state and prevent dispatch. No order transport or trading authority is registered. Keep real configuration and operational data private.
- Deployment boundary: prepare the hosted configuration and validation procedure without provisioning Azure resources or enabling paid/provider calls. Resource approval, live Foundry inference, provider coverage/capacity/cost validation and a full trading day of live shadow journaling remain explicit M4 completion gates. M5 ownership/recovery and M6 orders remain separate.
- Validation: mandatory CI combines the offline suite and twelve starter scenarios through actual SQL, full-text/transaction/clone tests, eight SQL replay gates, authenticated integrated feed handoff, non-root container allocation, app/SQL restart and outage refusal checks. Existing 14/16 replay coverage, 28 portability, 15 gateway and 14 feed gates remain intact. Lint/format, Bicep compilation, dependency-license review, local documentation/JSON checks and secret scanning cover the release. No live model/provider/broker call or cloud deployment is claimed.

## PIP-13 — Hosted M4 deployment and initial validation

- Story: [PIP-13](https://nitro.atlassian.net/browse/PIP-13)
- Status: Complete (approved deployment and initial validation); full M4 qualification remains PIP-14
- Prerequisite: PIP-12 application implementation; this story covers the separate hosted/live milestone gates, not duplicate implementation work.
- Authorization: implement the approved resources/configuration in Bicep and deploy them; verify actual model offers/quota/rates before provisioning. Preserve the existing landing image, domain, analytics and hosting settings. The owner separately approved the FXMacroData Individual monthly plan ($50/month after its trial) and completed checkout; annual billing is not approved. Provision provider read credentials into ignored configuration. Order dispatch and extra paid network isolation remain outside scope.
- Deployment: preserve the current public landing; add the private always-on engine/feed and read-only dry-gateway backend, SQL S0 and Microsoft Foundry resource/project/deployments. Verify actual offer/region/quota/API/pricing and explicit SQL firewall/identity/bootstrap policy, then deploy approved Bicep through existing OIDC. The temporary extra landing is a disclosed layout/cost difference. No paid network isolation, hosted agent runtime or order capability is implicit.
- Acceptance: pinned ready images/containers, actual managed-identity SQL/full-text and Foundry calls, private API/feed/gateway authentication, initial normalized provider data and continuous shadow journaling without an order transport; initial private export/paused-clone checks. Complete-day provider/broker/coverage/capacity/model-quality qualification continues in PIP-14; deploying resources does not complete M4.
- Hosted checkpoint: PRs #23 and #24 supply the approved foundation/backend and correct the SQL client-ID binding and pending-query percent escaping. CI passes 300 offline and 24 actual SQL tests, including notification/canonical-batch separation. Actual encrypted managed-identity SQL access has table-specific permissions and no DDL authority; temporary owner firewall access is removed. Both deployed containers are ready and the three feed workers have current error-free heartbeats. Initial ingestion retains five macro and 35 news events without quarantine. Real project-backed triage and decision calls pass JSON contracts; an isolated actual-model journal/export/paused-clone/retrieval round trip passes. The main shadow process is resumed without broker reads or an order transport. The public landing remains on its previous image/configuration. Full-day/provider/broker/capacity qualification remains open in PIP-14.
- Runtime verification: the main journal records its first four actual-model observations with six model calls, no model refusals and zero dispatches; the pending queue decreases. All three workers remain alive without restarts and their current heartbeats are error-free; historical startup alerts remain retained. Public HTTPS smoke confirms build, health, SEO, analytics, assets, navigation and private-route isolation, while an independent comparison confirms the landing template/configuration is unchanged. Initial container/SQL metrics are saved privately; a short startup window does not qualify full-day capacity or billed costs. The release workflow now waits a bounded five minutes for revision readiness before enforcing its image/topology checks.

## PIP-14 — Complete M4 live qualification

- Story: [PIP-14](https://nitro.atlassian.net/browse/PIP-14)
- Status: Pending remaining provider coverage and full-day qualification
- Scope: remaining live milestone gates moved from PIP-13, without duplicating runtime implementation. The owner-approved PipMind X app-only credential now passes public account/timeline GETs; continuous polling still needs verified role recipes and a controlled configuration-binding migration. Resolve FOREX.com demo API enablement, verified indicator/unit/source coverage and approved signed callback delivery. Preserve explicit timing, revision, backfill and outage gaps.
- Acceptance: a complete declared FX trading day with independent zero-order evidence; measured container/SQL capacity, lag, freshness, quarantine and actual model usage/cost; repeated real-model evaluations and owner human calibration; complete retained-day export and paused clone with retrieval/provenance and separate destination binding. See the [hosted qualification procedure](shadow.md#hosted-m4-approval-and-live-qualification). M4 remains open until these pass.
- Boundaries: no additional provider purchase, public-route exposure or capacity/security change without specific approval; no X writes, live broker orders, M5 recovery/leases, M6 execution, M7 review engine or M8 publication. Internal ingress alone cannot receive a provider's public webhook callback.

## PIP-6 — Portable Markdown mind export/import design

- Story: [PIP-6](https://nitro.atlassian.net/browse/PIP-6)
- Status: Design complete; export/import implementation scheduled below
- Scope: specify a ZIP-based `.pipmind` bundle containing Markdown mind files and a manifest, separate experience-clone and original-trader recovery modes, and delivery requirements across the existing milestones. SQL remains the runtime state store.
- Acceptance: preserve the complete retained five-part mind and provenance at a consistent checkpoint; define compatibility/integrity validation and atomic import; separate historical experience from active account authority; require matching operational state, fresh ownership and broker reconciliation for recovery. Promise the same saved mind, without promising identical future decisions.
- Excludes: application implementation, model weights/fine-tuning, cloud changes, paid calls and authorization to trade.
- Validation: reviewed architecture, persistent-mind ADR, roadmap and plan for consistency; checked 33 local Markdown links/anchors, 12 JSON examples, fenced blocks, diff whitespace and Gitleaks. Verified Git ignores `.pipmind` files and a built Docker image excludes a synthetic archive placed inside an otherwise included application folder. Required PR CI covers lint/format, Bicep compilation, secrets, image build and HTTP smoke checks.

### Mind portability delivery requirements

These requirements extend the existing milestones; this documentation story does not implement export/import. See [the bundle and import contract](architecture.md#markdown-mind-export-and-import).

| Milestone | Delivery and acceptance |
| --- | --- |
| M1 — Contracts/replay | Define the ZIP-based `.pipmind` format, versioned Markdown/front-matter, manifest and recovery-sidecar contracts, clone/recovery semantics and compatibility rules. Add synthetic round-trip and invalid-bundle fixtures, including unsafe archive paths/expansion, with explicit capability/deferred coverage; recovery fixtures cannot dispatch to a broker. |
| M4 — Shadow | Implement SQL-backed consistent export and staged atomic import of all five mind parts; preserve exact identity/playbook text and retained history, IDs, evidence and override tags. Verify experience-clone lineage, historical position theses, separate destination account initialization, rebuilt retrieval and zero orders. Reject incomplete, corrupt, incompatible or unsafe bundles without changing active state. |
| M5 — Safety/recovery | Implement recovery of the original trader from a matching mind/operational checkpoint; validate private destination account binding, retained stop/override state, fresh fenced ownership, pending-command reconciliation and explicit resume. Test SQL round trips, interrupted import, stale exports, account mismatch, overlapping owners and ambiguous orders against actual enforcement paths. |
| M6 — Demo orders | Exercise recovery against the demo gateway with known and uncertain command outcomes; verify no duplicate dispatch, preserved protective orders and agreement with fresh broker snapshots before resumption. |
| M7 — Self-review | Round-trip current and retired rule versions, reviews, suggestion outcomes and override exclusions; preserve evidence references and monotonic versions after clone/recovery and subsequent review. |

Keep real bundles in ignored private data/output paths and outside images/public snapshots. Record integrity and compatibility results without private contents. Full operational recovery also requires protected database backups/event records; a Markdown-only mind export cannot authorize or reconstruct account execution state.

## PIP-2 — Demo infrastructure and production deployment options

- Story: [PIP-2](https://nitro.atlassian.net/browse/PIP-2)
- Status: Complete
- Scope: update architecture, cost model, and implementation planning for one Container App with two containers, Azure SQL S0, default networking, Blob Storage, a free static frontend, built-in secrets, and essential monitoring. Keep X read-only and describe production deployment options separately.
- Acceptance: consistent docs, explicit deployment/security tradeoffs, checked cost arithmetic and assumptions, an $180 monthly planning budget, and unchanged trading rollout order. Reconcile directly conflicting README, roadmap, ADR, and data-source statements.
- Excludes: cloud provisioning, application implementation, provider subscriptions, and live-trading authorization.
- Validation: checked local Markdown links/anchors, JSON examples, cost totals and model-volume scenarios, explicit X read-only scope, and consistency across the architecture, cost model, roadmap, README, data sources, and storage ADR.

### Demo delivery requirements

These are requirements for future implementation stories, not claims that the system is deployed. Create the individual Jira stories before implementation. Keep the roadmap order: replay harness → shadow mode → demo orders → self-review → public launch.

| Milestone | Revised deployment work and acceptance |
| --- | --- |
| M1 — Contracts/replay | Define versioned service and ownership envelopes plus store interfaces. Deliver the initial 30-case evaluation dataset, deterministic graders with seeded-failure tests, baseline report and credential-free PR checks described below. Replay uses fakes or an isolated local store; CI makes no Azure or broker calls. Database integration checks exercise the SQL Server adapter and migrations when available. |
| M2 — Gateway | Keep the gateway independently runnable with broker credentials only in its environment. Test durable command IDs, broker-identifier mapping, and recovery from uncertain outcomes in dry mode. |
| M3 — Feeds | Run macro/news/social as independently supervised engine workers. X reads posts and necessary account/role data only; request no write permissions. Test feed isolation and signed webhook durable acceptance/replay protection. |
| M4 — Shadow | After resource approval, use one Container App with engine and gateway containers at 0.25 vCPU / 0.5 GiB each, one replica, default networking, and SQL S0. Provision Microsoft Foundry resource/project and configurable model deployments; verify availability/quota/pricing, managed-identity access and project-backed inference through the Agent Framework Foundry connector. Measure CPU/memory, SQL latency/search, model tokens, and API charges; demonstrate zero orders. |
| M5 — Safety/recovery | Verify shared SQL ownership and stale-owner rejection across overlapping revisions, shutdown/takeover reconciliation, SQL-outage refusal, authenticated controls that run independently of model calls, external heartbeat, and backup restore. Set numerical risk limits before demo orders. |
| M6 — Demo orders | Point explicitly at the broker's demo environment. Exercise round-trip orders, ambiguous-result recovery, persistent deduplication, and broker-snapshot reconciliation. Hosting in a production profile does not select a live broker account. |
| M7 — Self-review | Version the SQL-backed playbook, preserve the five-part mind, and publish review diffs while excluding override evidence. |
| M8 — Public launch | Static Web Apps Free reads sanitized Blob snapshots without broker/model access. Validate cache/staleness behavior, private archive permissions, provider display rights, secret-free frontend, monitoring, and the $180 planning alerts. |

The demo uses built-in Container Apps secrets, scoped container environment references, SQL/Blob managed identity access, TLS, and appropriate firewall rules. It accepts shared app identity/lifecycle and one-instance availability, with managed SQL durability and recovery checks. The planning estimate is $59.14/month hosting and $151.31/$159.64 total under annual/monthly FXMacroData billing, before tax and any display add-on; assumptions and exclusions are in the [cost model](cost-model.md).

### Production deployment work

Keep resource sizes, service addresses, identity/secret providers, network access, retention, and monitoring configurable. Separately approved production work can split the containers and feed workers into independent apps/identities, increase SQL and compute capacity, add private networking and stronger access controls, and define redundancy and tested recovery objectives. Retain one active order owner per broker account even with standby replicas. Cost and validate that profile separately; it is outside this documentation story and the demo's $180 planning budget. Production hosting and live broker-account authorization remain separate owner decisions.

## PIP-1 — PipMind logo assets

- Story: [PIP-1](https://nitro.atlassian.net/browse/PIP-1)
- Status: Complete
- Scope: review the design docs and the BoltClock, DocPilot, and QuillSign logo family; create a distinct PipMind symbol with square and transparent SVG/PNG variants; document usage and palette.
- Acceptance: editable SVG sources, matching PNG exports, and visual checks on light/dark backgrounds and at small icon sizes.
- Excludes: application code, trading integrations, and cloud provisioning.
- Validation: inspected alongside all three reference logos, on white/navy surfaces, and at 16/24/32/48/64 pixels; checked identical SVG mark geometry, valid SVG XML, PNG dimensions, and transparency.

## PIP-3 — Replay evaluation design and initial landing deployment

- Story: [PIP-3](https://nitro.atlassian.net/browse/PIP-3)
- Status: Complete; custom-domain HTTPS verified
- Scope: specify versioned evaluation scenarios, graders, baseline reports and credential-free CI in M1; build and deploy a branded landing-only container using dedicated Azure Container Apps resources, GHCR and private deployment configuration.
- Acceptance: replay evaluation requirements are explicit; local container and deployed revision serve the page and health/version response; CI validates code, image, infrastructure and secrets; return concrete DNS records for the owner to configure, then bind managed TLS once DNS resolves.
- Excludes: implementing the trading/replay engine, SQL/model/feed provisioning, paid model evaluation runs, broker credentials and live trading. The initial scenario dataset and graders are M1 delivery requirements.
- Validation: local and GitHub Actions lint/format, locked image build and actual HTTP checks, Bicep compilation, PowerShell syntax, Gitleaks, private-configuration scan, local Markdown/JSON checks and desktop/mobile browser review. Verify the deployed image digest, ready revision and public health/build/page before merging the deployment completion. The source image is published to GHCR; the minimal Azure landing environment is separate from the future trading profile.
- Custom-domain completion: owner DNS records verified; Bicep managed certificate issued and bound. Verified the trusted TLS certificate chain and hostname, HTTP-to-HTTPS redirect, expected source build, page/assets, navigation, security headers and private-route isolation through the custom domain. The ready revision retains its immutable image and secure ingress. Actual hostname, DNS values and certificate identifiers remain in ignored private deployment state. Azure manages certificate renewal while the documented DNS and ingress requirements remain satisfied.

### M1 evaluation delivery checklist

The M1 foundation is implemented by [PIP-7](#pip-7--m1-contracts-and-replay-evaluations); [the replay guide](replay.md) records active coverage and explicit later-path deferrals.

- Versioned JSONL scenarios: six triage, six decision/memory, ten safety/recovery, six review and two public/read-only cases. Include positive controls, adversarial feed content, duplicate/uncertain outcomes, stale state, SQL/lease failures, override evidence exclusion and refusal records.
- Each case identifies required milestone/capabilities, input events and availability times, simulated clock, starting mind/playbook and broker state, tool results, allowed behavior, expected evidence and rubric version. Use licensed sanitized recordings or synthetic data; reserve chronological held-out episodes.
- Deterministic graders check schemas, evidence references, permitted dispatch, idempotency, state consistency, review evidence and public redaction. Test graders using intentionally invalid traces so they cannot pass by construction. Add model/human rubrics for reasoning and summary fidelity; model judgments never overrule hard checks.
- Replay the fixture week with injected fake models, clock, isolated state and simulated gateway; reset independent cases, baseline/candidate variants and repetitions. Store reports under ignored generated output. Record dataset/source/prompt/playbook/grader/model versions, per-case failures, deferred coverage, latency and tokens/cost.
- GitHub Actions validates scenarios and graders, runs the replay and applicable mandatory cases, and reports baseline differences with bounded sanitized artifacts. No secrets, vendor/model APIs or broker connections in PR checks. Fail on mandatory failures or silent skips of implemented capabilities.
- Activate deferred gateway, SQL ownership, review and public-output cases at M2/M5/M7/M8 against real application paths. M1 fake traces demonstrate grader behavior only. Run optional real-model/judge comparisons through configured Microsoft Foundry deployments explicitly with repetitions and human calibration; track their usage outside the runtime allowance.
- Keep evaluation quality, safety and profitability separate. Promotion requires mandatory safety cases to pass plus review of quality/cost regressions; continue through shadow mode and demo trading regardless of replay scores.

## PIP-4 — Microsoft Foundry AI platform baseline

- Story: [PIP-4](https://nitro.atlassian.net/browse/PIP-4)
- Status: Complete
- Scope: replace outdated AI-platform references across stack, architecture, evals, roadmap and cost assumptions with Microsoft Foundry; align local agent instructions.
- Acceptance: Foundry resource/project and configurable model deployments are explicit; Agent Framework orchestration remains in Container Apps; model availability/pricing remains subject to Foundry deployment verification; no stale platform references remain.
- Excludes: cloud provisioning, AI runtime implementation, model changes and landing-container deployment.
- Validation: audited tracked platform references and the landing-only infrastructure/dependencies; checked Markdown links/anchors, JSON examples, diff whitespace, secret scan and CI. The existing $35 model allowance and totals remain provisional until Foundry deployment prices and availability are verified.

## PIP-5 — Shared landing footer

- Story: [PIP-5](https://nitro.atlassian.net/browse/PIP-5)
- Status: Complete
- Scope: match the DocPilot/QuillSign footer wording and links to the personal site and this repository; put the forex loss disclaimer in its own paragraph; remove the public-experiment credit and separate license link.
- Acceptance: requested content and links, existing license preserved, adjacent disclaimer lines at the same text size as the credit; desktop credit right-aligned and top-aligned with the first disclaimer line, with responsive stacking when needed; green periods in every wordmark including hover/focus; checked container and actual HTTPS deployment.
- Validation: local container HTTP checks, adjacent disclaimer paragraphs, matching text sizes and exact desktop top/right alignment verified in the browser; both outbound link destinations and green periods in both wordmarks verified; lint/format, image, Bicep and secret checks run in CI. Release verification checks the published image, ready revision, health/build and custom-domain HTTPS footer while preserving its certificate binding.

## PIP-7 — M1 contracts and replay evaluations

- Story: [PIP-7](https://nitro.atlassian.net/browse/PIP-7)
- Status: Complete
- Scope: versioned JSON contracts, injectable offline replay core/clock/model/store, synthetic fixture week, 30-case evaluation dataset, deterministic graders with seeded-failure checks, reproducible baseline comparison and credential-free CI. Include the M1 portability manifest/front-matter/sidecar contracts and synthetic invalid-bundle checks from PIP-6.
- Acceptance: command-line replay writes decisions and reports to ignored output; independent cases/variants/repetitions are isolated, evidence respects availability, positive controls pass, graders reject known-bad traces, and mandatory failures or missing delivered coverage fail CI. Gateway/SQL ownership, real self-review/publication and SQL clone/recovery remain explicitly deferred to their milestones.
- Excludes: live inference/vendor/broker calls, Azure provisioning, actual trading, SQL-backed clone/recovery and production safety certification.
- Validation: 118 tests pass with networking disabled, covering contracts, actual replay paths, grader mutations, isolation, baseline gates and archive rejection. The saved baseline has 13 passing cases and 17 explicit deferrals; the fixture week has 20 events, 15 decisions, five unsent proposals and zero dispatches; all 28 portability fixtures pass. Checked locked dependencies, lint/format, Markdown links/JSON examples and secret handling. CI runs the same offline checks and uploads only a bounded seven-day sanitized summary, alongside existing landing-image, Bicep and secret checks.

## PIP-8 — M2 dry broker gateway

- Story: [PIP-8](https://nitro.atlassian.net/browse/PIP-8)
- Status: Complete
- Scope: independently runnable CIAPI v2 session/read adapter, Lightstreamer prices, per-instrument stale watchdog and REST fallback, symbol/size mapping, normalized snapshots and durable dry command acknowledgement/recovery. Add a recorded-session CLI and applicable M1 gateway checks against the actual dry adapter.
- Acceptance: recorded quiet stream switches to REST and back; duplicate commands across restarts/concurrent instances produce one durable acknowledgement; payload collisions, unknown symbols, unrepresentable sizes and ambiguous outcomes fail closed. Intent precedes acknowledgement, broker identifiers remain mapped, and no order submission is possible. Offline tests and CI exercise the real adapter with synthetic transports.
- Excludes: Azure changes, actual broker/model calls, order submission, hosted SQL/ownership enforcement (M5) and broker demo execution (M6). Local SQLite is an isolated dry development/replay ledger only. Live read compatibility requires a separately configured broker session; synthetic tests do not certify broker access.
- Validation: 180 tests cover the replay foundation and gateway, with offline recorded-session gates, concurrent/restarted durable acceptance, rollback on failed acknowledgement, mapping/size rejection, bounded read retries, auth restrictions, per-symbol freshness, unsafe input and seeded failures. All 15 gateway checks, 28 portability fixtures and the unchanged fixture week pass; the reviewed baseline activates S01 (14 passes, 16 deferrals). The actual non-root gateway container passes auth/duplicate/restart checks with networking disabled; the default landing container passes its existing HTTP/private-route smoke checks. Lint/format, locked dependencies, local documentation links/JSON examples and secret scanning are checked; required PR CI repeats offline tests/replay and both container checks. No real broker session or Azure deployment is claimed.

## PIP-9 — M3 information feeds

- Story: [PIP-9](https://nitro.atlassian.net/browse/PIP-9)
- Status: Complete
- Provider contract refinement: preserve unknown schedule/publication times as null with explicit publication evidence/precision, through additive v1 fields. Use explicit per-indicator value/scale mappings so employment levels, headline changes and percent variants cannot be silently combined with different consensus units.
- Scope: independently supervised macro/news/social workers; normalized durable local development events/cursors; FXMacroData SSE and signed webhook deduplication; hourly ForexFactory cache and consensus mapping; RSS/Atom and HeadlineRelay; GET-only X pagination, role refresh, identity checks and polling policy.
- Acceptance: killing each worker leaves the other two progressing and restarts the failed worker; cache/backoff survives restart; atomic cursor/event commits and signed webhook acceptance prevent lost or duplicate release effects; holder changes replace only personal accounts, broken lookups alert without clearing saved state, and X has no mutation path. Offline fixture gates, process/container checks and CI validate the delivered paths.
- Excludes: Azure changes, provider purchases or subscription writes, live model/broker/vendor calls and trading. SQLite is isolated local development/replay only; hosted SQL/live deployment remains M4 and requires separate resource approval. Provider compatibility is unverified until an explicit live read smoke run.
- Validation: 247 tests pass offline, including real worker kill/restart isolation, transactional input/cursor rollback, cross-transport deduplication, tampered/stale webhook rejection, cache/backoff persistence, RSS/Atom safety, role verification/failure/dormancy, X pagination and no-write policy. All 14 feed gates, 15 gateway gates, 28 portability fixtures and the fixture week pass; the baseline remains 14 passing / 16 deferred starter cases. The actual non-root feed image passes auth and all three kill/restart flows without network access. Locked dependencies, lint/format, documentation links/anchors/JSON examples, secret scans and required PR CI checks cover the change. No real provider connectivity, complete outage backfill, hosted SQL delivery or Azure deployment is claimed.

## PIP-10 — Landing page analytics

- Story: [PIP-10](https://nitro.atlassian.net/browse/PIP-10)
- Status: Complete; demo activation deployed and GA4 Realtime receipt verified
- Scope: optional runtime-configured GA4 for the public landing page, validated measurement ID, restrictive CSP, configuration examples and deployment documentation. Preserve the current visual design and keep private endpoints uninstrumented.
- Acceptance: unset configuration loads no Google resources; configured tracking initializes once and measures the landing page; invalid configuration is rejected. Verify container HTTP behavior and browser execution. Actual measurement ID stays outside source and images.
- Deployment: on 2026-10-18 the owner approved enabling the existing stream's measurement ID on the demo app, deploying it and verifying Realtime event receipt. Supply the private value through deployment configuration and existing Bicep/OIDC; preserve other hosting settings and Enhanced Measurement OFF. No new service, credentials or advertising links are in scope.
- Original implementation validation: six landing HTTP/configuration tests, three offline JavaScript behavior checks, Bicep compilation and both enabled/disabled non-root container smoke checks pass. Browser verification confirms the local loader inserts the Google tag, navigation works and no console errors appear. CI repeats these checks alongside the existing offline replay/gateway/feed suite. These initial checks did not establish live GA4 event receipt; the approved activation below does.
- Activation evidence (2026-10-18): configured the approved stream through the demo environment variable and deployed the existing immutable landing image using [workflow run 39135423994](https://github.com/henrique-meyer/pipmind/actions/runs/39135423994). Bicep deployment, current ready revision/image and public HTTP smoke pass. Independent before/after checks confirm image, public origin, resources, scale, probes, ingress/TLS and environment are preserved. The live browser loads the matching Google tag and navigation works without console errors. In the correct PipMind property, Realtime shows one active user, the landing title with one view, and received page_view, first_visit and session_start events. Screenshots and private configuration remain local; Enhanced Measurement remains off. No new services, credentials or advertising links were created.

## PIP-11 — Landing page SEO

- Story: [PIP-11](https://nitro.atlassian.net/browse/PIP-11)
- Status: Complete; approved SEO release deployed and verified
- Scope: accurate landing metadata, runtime-configured canonical/social URLs, appropriate structured data and public-page discovery through robots/sitemap. Preserve current branding/footer and private-route isolation. Save repeatable instructions locally in an ignored Markdown file, referenced by local agent instructions.
- Acceptance: configured production origin is validated independently of request Host; public metadata and sitemap agree; unset configuration avoids accidental preview indexing. Verify actual container responses, browser navigation, metadata/structured-data parsing and CI. Real hostname remains private configuration.
- Deployment: owner approved the existing demo landing image and public-origin configuration update on 2026-10-18. Preserve current sizing, networking, replicas, domain/TLS bindings and analytics state. No paid SEO tools, Search Console setup or GA activation is included. Verify the immutable image, ready revision and public HTTPS flow after deployment.
- Validation: 272 offline Python tests and three JavaScript behavior checks pass, with the replay baseline unchanged. Configured and unconfigured non-root landing containers pass HTTP smoke for metadata, structured data, image, robots/sitemap, HEAD and route isolation. Focused tests reject unsafe origins and prove Host/forwarded-host/query inputs cannot alter canonical metadata; the JSON-LD CSP hash matches its exact content. Desktop/mobile browser checks confirm navigation, no horizontal overflow and no console/CSP errors. Lint/format, Bicep compilation and staged secret scanning pass. The approved release from PR #18 was deployed through [workflow run 39129344245](https://github.com/henrique-meyer/pipmind/actions/runs/39129344245) on 2026-10-18. The immutable image and latest ready revision match; both default-host and custom-domain smoke checks pass. Independent checks verify trusted HTTPS, HTTP redirect, canonical/social metadata, structured data, robots/sitemap, assets, navigation and private-route isolation. Capacity, replica bounds/timing, health probes, TLS bindings and inactive analytics are unchanged. No Search Console submission, indexing or ranking outcome is claimed.

### Search Console follow-up (PIP-11)

- Status: Complete; ownership verified and sitemap processed successfully
- Owner request: configure the production site's Search Console property, verify ownership and submit the existing sitemap. Use the established Google session and preserve unrelated properties/settings.
- Scope: reuse the correct property or create a production URL-prefix property; verify ownership by an appropriate supported method; submit the live sitemap and inspect the saved status. Keep real hostname/verification values in ignored local state and the user handback.
- Acceptance: ownership and sitemap submission are verified from saved Google UI state. Record processing/indexing status accurately; submission is not proof of indexing. GA activation and advertising links remain outside this task.
- Validation: created the production URL-prefix property in the established owner session and saved Google's dedicated DNS CNAME without changing application routing. Google confirmed ownership through the domain provider. Submitted the existing sitemap; after an initial fetch failure and one retry, the saved report shows Success and one discovered page. Public DNS resolution, sitemap XML and robots checks pass. Search Console remains open in the Codex browser; screenshots and verification values are saved locally. Indexing and ranking outcomes remain unverified. No application, Azure or analytics configuration changed.
