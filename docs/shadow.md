# Shadow runtime (M4)

PIP-12 implements the application foundation for M4: SQL-backed wake/triage/decision processing, a private service/container, the Foundry connector and experience-clone import/export. It has **no order transport, OrderRequest producer or dispatch route**. `enter`, `add` and `exit` are recorded proposals; `hold` is a decision. A model failure is a refusal with no fabricated decision. PIP-13 adds the approved hosted resources and qualification work while preserving the landing.

M4 is complete only after approved hosted resources, project-backed inference, provider/capacity/cost checks and a full FX trading day of live journaling with zero orders. Offline checks do not satisfy those gates. M5 owns broker ownership/fencing, risk enforcement, reconciliation, authenticated operational recovery and trading resumption; M6 owns actual demo execution. Self-review/playbook changes remain M7.

## Local SQL and mandatory checks

Use Python 3.12 and locked dependencies:

```sh
uv sync --locked --all-extras
uv run --all-extras python -m pytest -q
docker build -f infra/sql.Dockerfile -t pipmind-sql:local .
docker build --target shadow -t pipmind-shadow:ci .
python scripts/shadow_smoke.py
```

The smoke command creates its own internal Docker network, SQL Server Developer container with full-text search, random temporary credentials and synthetic database. It runs the SQL replay, every SQL integration test, the authenticated private API, app/database restart and SQL-outage refusal checks, and the integrated feed-to-shadow handoff. Test credentials are temporary files under ignored `.data/` and never command arguments. Only its own containers/network are removed. SQL Server Developer is for local/CI checks, not the hosted database.

Ordinary offline pytest deliberately skips the SQL test module unless `PIPMIND_SQL_TESTS=true`; **CI separately requires the smoke command**, so skipped SQL tests cannot replace the real database gate. CI retains only the bounded synthetic `.data/shadow-ci/summary.json` for seven days. Full SQL state, traces and bundles are private.

The [shadow scenarios](../evals/shadow/scenario-v1.json) require twelve starter triage/memory cases through SQL, plus eight replay gates with a positive proposal control. Scripted native-read fixtures adapt M1's sequential decision IDs to SQL's causation-derived IDs. They exercise storage and orchestration, not model quality. Separate tests exercise the locked Agent Framework/Foundry request serialization through an HTTP mock, invalid/oversized responses, timeouts, read-tool limits, chronology, stale revisions, clone rollback and actual full-text retrieval. The M1 baseline remains 14 passing / 16 deferred; M5/M7/M8 controls are not promoted by shadow tests.

## Private configuration and owner commands

Copy variable names from [the empty example](../.env.example) into an ignored private environment file. This module does not load `.env` files or override process values. Never use fixture commands against hosted SQL. Required configuration:

| Area | Variables / behavior |
| --- | --- |
| SQL | `PIPMIND_SQL_SERVER`, `PIPMIND_SQL_DATABASE`; port defaults to 1433. Hosted connections use managed identity, encrypted transport and hostname/CA validation. `PIPMIND_SQL_CA_FILE` optionally selects a trust store. |
| Local SQL only | Explicit `PIPMIND_SQL_LOCAL=true`, user/password and host `sql`, `localhost` or `127.0.0.1`. Unencrypted local transport stays on an isolated development network. |
| Mind | `PIPMIND_SHADOW_TRADER` and private destination `PIPMIND_SHADOW_BINDING_REF`. The stored binding is a hash, not a credential or broker permission. |
| Foundry | `PIPMIND_MODEL_CALLS=true`, project endpoint ending `/api/projects/<project>`, triage and decision deployment names. Credential defaults to managed identity; explicit `azure-cli` is available for authorized local inference. There is no API-key fallback. |
| Private API | Distinct random `PIPMIND_SHADOW_TOKEN`, `PIPMIND_FEED_TOKEN`, `PIPMIND_GATEWAY_TOKEN`, at least 32 characters. Keep endpoints internal and disable access logging. |
| Integrated feeds | `PIPMIND_SHADOW_RUN_FEEDS=true`, `PIPMIND_FEED_BACKEND=sql`, private recipe/config and provider variables. Three independently supervised feed workers run inside the engine container. Temporary stop files use `/tmp/pipmind-feeds/`; no hosted SQLite state. |
| Separate feeds | Instead of integrated feeds, set `PIPMIND_SHADOW_FEED_URL` and pin `PIPMIND_SHADOW_SOURCE_ID` to the saved feed-store UUID. Changed source identity refuses consumption. |
| Gateway reads | Optional internal `PIPMIND_SHADOW_GATEWAY_URL` and distinct stable `PIPMIND_SHADOW_GATEWAY_SOURCE_ID`. Only authenticated GET `/state` is called; a dispatch-enabled gateway is refused. Broker reads retain their separate opt-in. |

Initialize/migrate using an explicitly authorized schema-administration identity. Runtime access should have only required table reads/writes and application-lock/full-text query permissions; do not make the application the SQL server administrator. Hosted identity/firewall/bootstrap permissions must be tested during deployment. Feed schema initialization uses `SqlFeedStore` with the same recipe profile before restricting runtime permissions.

```sh
python -m pipmind.shadow migrate
python -m pipmind.shadow init --identity .data/identity.md --playbook .data/playbook.md --version initial-v1
python -m pipmind.shadow status
python -m pipmind.shadow resume-shadow
python -m pipmind.shadow serve --host 127.0.0.1 --port 8084
python -m pipmind.shadow pause
python -m pipmind.shadow export --output .data/exports/checkpoint.pipmind
```

`init` and imports start paused. `resume-shadow` enables only journaling/model calls, and grants no trading authority. Pause increments the revision; an in-flight stale model response cannot commit. `run-once` processes one accepted event; `ingest --input <private JSON>` accepts a bounded `{source, events:[{sequence,event}]}` page. The HTTP equivalent is authenticated POST `/inputs`, at most 256 KiB. GET `/status` is authenticated; `/healthz` identifies shadow mode with `trading:false`. Health includes background worker liveness, not proof of feed/model/SQL quality; inspect status and progress separately. Docs/order/control routes are absent.

The reasoning worker owns one persistent event loop in a separate thread; SQL/SDK work does not block private HTTP handling. Status exposes its current phase, phase start, input ID and sanitized exception type. Child SDK cancellation is a retained model refusal; caller shutdown cancellation still propagates. Cleanup faults retain a valid response and known usage. Unexpected worker failures durably pause without consuming the input. A phase stalled for 120 seconds or a stopped worker triggers an audited hold before health returns 503, allowing the existing platform probe to restart the process. A late result cannot cross the pause revision. Storage failures retry the hold rather than falsely claiming a durable pause; ordinary SQL/provider errors and explicitly paused operation do not intentionally fail liveness. Inspect the retained attempt before owner-controlled resumption. No automatic retry rewrites an earlier outcome.

Integrated feed routes are mounted at `/feeds`, with their own bearer policy and HMAC validation for `/feeds/webhooks/fxmacrodata`. Webhooks acknowledge durable inbox acceptance before model work. Never publish private status/input/relay routes without suitable access controls. Separate deployment layouts can retain the standalone feed API instead.

## Processing and persistence

SQL stores the immutable identity/current playbook, working context, journal, retained memories/reviews and playbook history. Each input plus source checkpoint is one serializable transaction. Completion atomically writes input evidence, the structured decision/refusal and context/tool/usage trace, working memory and aggregation state. Unique identities and fact hashes reject conflicting redelivery. A per-trader transaction application lock and optimistic mind revision prevent concurrent response overwrites; these are **not M5 broker fencing**. A crash after inference but before commit may repeat a billable call; it cannot duplicate a committed outcome.

The three feed workers remain separate processes with their own connections, stop files, heartbeats and restart backoff. Linux starts them through a pristine fork server with import-only dependency preloading, sharing read-only library pages within the demo container. It never forks the running ASGI process or shares active SQL/HTTP clients. Platforms without fork-server support retain spawn. CI loads the actual Foundry SDK alongside SQL and the integrated workers under 0.25 vCPU / 512 MiB, checks memory headroom and preserves the independent worker/process restart gates. This regression check does not replace full-day capacity measurement.

Every accepted logical input remains evidence. Analysis is retained without a strong decision wake. Price inputs are durably coalesced into per-instrument count/range/last-quote windows with at most one wake per 60 seconds; snapshots are observations, never invented fills. A 15-minute durable heartbeat supplies clock wakes; explicit daily/session clock inputs use the ClockEvent contract. No unverified timezone/session schedule is inferred. Source outages become explicit context/status gaps while already accepted inputs continue processing.

`PIPMIND_SHADOW_PRICE_INTERVAL` (1–3,600 seconds) and `PIPMIND_SHADOW_CLOCK_INTERVAL` (60–86,400 seconds) configure those wake intervals. They are batching settings, not monetary cutoffs. Coalesced quote records remain retained but do not crowd actual decisions out of the recent-journal prompt tail. Qualify wake volume and export size against actual event rates; the defaults are not a verified cost/capacity profile.

Context uses phase-specific views of the five-part mind. Identity/playbook appear once in the stable instruction prefix; both phases retain current views, all position theses, waiting conditions, broker observations and feed quality. Triage receives the current event and this continuing state, without the journal/archive passages or the decision schema. Decisions additionally receive evidence selected for the current event and active working views. No age cutoff excludes month-old material developments. Selection is lexical retrieval, not a scripted trading or wake strategy; all accepted events remain retained and visible to triage.

Prompt `shadow-context-v4` factors repeated feed-quality values without removing facts. `coverage-table-v1` supplies shared defaults, a table of object-key paths and each series' indexed overrides; applying those values reconstructs the exact original JSON, including missing fields, nulls, booleans and numeric types. The full 33-series retained example falls from about 30 KiB to 6 KiB before other context. Original runtime facts and full traces stay in SQL, and the existing input guards remain unchanged. Known v1/v2/v3 experience bundles remain importable; unknown compatibility versions still refuse.

Raw feed status can retain up to 128 KiB in private SQL before the model-visible projection is factored; the 16,000/28,000-byte model prompt guards are separate and unchanged. This permits the complete expanded coverage map to reach the engine without clipping source facts or falsely marking the source unavailable.

Prompt `shadow-context-v5` / context `selected-evidence-v3` also factors shared literal word phrases in current views and waiting conditions. `working-phrases-v1` keeps a literal dictionary; each list-valued entry joins its string chunks and integer-indexed phrases with single spaces. Plain strings stay exact, including irregular whitespace. Responses still use ordinary working-memory strings. No stored view, condition or position thesis is rewritten or dropped. Small/unprofitable or out-of-bounds inputs fall back intact; the 16,000/28,000-byte prompt guards still refuse oversized mandatory state. A bounded cache avoids repeating the projection work for an unchanged mind.

SQL acceptance and source pages use bounded set-based operations, and transactional retrieval terms are inserted together. This preserves ordered identities, duplicate/collision rules and atomic checkpoints while allowing peer work within the existing lock timeout. Runtime status refreshes after each completed step or idle check, rather than waiting for an entire backlog batch. These changes do not establish full-day capacity or total billing.

Retrieval uses SQL full-text plus a synchronous term index, filtered by trader and decision time. Up to 12 meaningful query terms select candidates separately from source memories/reviews and journals, so copied context in legacy journals cannot crowd source evidence out of the same candidate pool. Candidates are reranked using their projected content; dates, record IDs, provenance and override tags remain attached. Long source text selects a relevant bounded passage. Journal projections contain the outcome/summary/reasoning and citations, never prior prompts, nested contexts, tool results or usage traces. At most five search results and three relevant recent journal cards are considered; current-event and active-view searches can contribute separate results, deduplicated within an 8,000-byte evidence allowance. These are bounded, non-exhaustive results, not proof that absent evidence does not exist. The current mind is already supplied; the sole native read tool, `search_memory`, looks further through the retained archive instead of rereading the same prompt. It allows at most eight calls / four iterations and 16,000 captured-result bytes. Untrusted content never grants authority or registers tools.

The full current event and mandatory standing/current state are not silently clipped to fit a monetary budget. The combined instruction/message byte limits are 16,000 for triage and 28,000 for decisions; oversized mandatory state is refused and retained for inspection. These byte limits are not exact tokenizer counts. Full original input, selected context, exact model-visible instructions/message, registered tools, build, tool results, responses/refusals and available usage remain in private SQL. Existing audit history is neither rewritten nor pruned to reduce prompt size. New retrieval indexing projects semantic evidence rather than historical audit plumbing; old source records remain searchable without rewriting their contents.

Each model call has a 90-second timeout, 512 output tokens for triage or 4,096 for a decision, strict local schema validation and no provider-side stored conversation. Traces identify prompt/context versions, byte size, latency and measured usage; usage remains unknown where no provider counters are available, including interrupted calls. No real-model quality score or verified monthly saving is inferred from scripted responses or a short startup sample.

### Markdown knowledge and continuing memory

The existing owner initialization reads identity and playbook from private Markdown files, and experience exports contain the retained mind as Markdown records with provenance. The [blank starter](../templates/blank/README.md) provides strategy-neutral files for a new, paused trader without inherited history; it is not an experience clone. SQL is the runtime authority. A new process reloads standing policy, current views and retrieved evidence from that store; restarting does not require the owner to explain the trader's history again. A model still needs the selected text in its context or a tool result; putting text in a repository does not make it free permanent model memory.

The [planned Auto Learn/library controls](architecture.md#auto-learn-and-exact-saved-mind) are not current CLI features. Auto Learn Off will freeze durable robot rules/guides/lessons while current market/account facts and decisions continue; it is distinct from `pause`/`resume-shadow`. Load will create an unnamed active working copy from a validated blank/save/import, with exact learned-content retrieval and no source-save mutation. Favorites/manual copies will survive rolling autosave cleanup. The current full-experience clone/export commands must not be mistaken for these compact robot-save/library controls or external robot-sharing support.

A curated private Markdown knowledge library could organize durable topic briefs, rules and lessons. A useful brief would carry an as-of date, supporting and contrary source IDs, unresolved questions and conditions that invalidate its current view. Current working views/theses/waiting conditions provide continuing state now, while archive retrieval supplies dated evidence. Automatic thematic brief maintenance and self-written playbook revisions require their own reviewed writer/evaluation workflow; M7 remains the self-review milestone. The [planned owner chat](architecture.md#private-owner-chat-and-confirmed-edits) adds explanations, suggestions and confirmed direct edits without restricting nightly review or other autonomous learning paths. Only direct edits applied by chat require chat confirmation. The current shadow runtime has no owner-chat interface or durable chat-edit service; this design does not claim one is deployed. This release does not introduce a second mutable file store or automatic Git synchronization. Raw articles and private experience stay out of the public source repository; article storage still follows provider retention rights.

### Hosted context measurement (PIP-15)

The refined `shadow-context-v3` release was deployed and verified through [workflow 39148772345](https://github.com/henrique-meyer/pipmind/actions/runs/39148772345). Required CI passes 309 offline and 27 actual SQL tests, including older/contrary evidence amid unrelated legacy traces, exact audit retention and known v1/v2 bundle imports. Actual backend images/build/readiness, existing allocation/configuration/identity/egress, current error-free worker heartbeats and the preserved public landing are independently checked.

Three retained cases (one release, two news items) were re-run through real Foundry deployments. They use the same source events and original historical working/broker/feed state, with retrieval limited to evidence available at each original step. Paired decisions use the original triage result and are explicitly forced for measurement; this is not a measured live wake frequency. All six refined responses pass local contracts and evidence checks, with no tool round trips in this sample:

| Phase | Mean original input tokens | Mean refined input tokens | Reduction | Maximum refined input |
| --- | ---: | ---: | ---: | ---: |
| Triage | 44,563.7 | 2,416.7 | 94.58% | 2,688 |
| Decision | 44,896.7 | 5,452 | 87.86% | 5,854 |

The original 80 immutable SQL audit records match their predeployment fingerprint. New comparison requests/responses/usage, including the initial guarded trial, remain in separate paused SQL validation minds and ignored private reports. A real owner-triggered clock event also completes through the deployed engine with 1,980 input / 134 output tokens, resumed state, no pending input/current runtime error and zero dispatches. No extra resource, model quota, provider access or landing change was needed.

This is a small controlled input-token comparison plus an actual clock smoke flow. It does not establish full-day reasoning quality, source coverage, sustained capacity, caching savings or a monthly bill. Model output, call volume, retries and evaluation usage still need PIP-14 qualification. Older relevant facts remain available; the optimization changes the selected view, not the retained history.

## Repeated real-model starter evaluations

The opt-in owner command runs the twelve applicable M4 starter cases in fresh SQL validation minds, with two to five repetitions. Each episode restores its synthetic initial views and account observations, processes events at their recorded availability times through the actual engine, retains complete model/tool/usage traces, and ends paused. The operational trader, its cursors and broker binding are untouched. Later-capability starter cases remain deferred.

```sh
python -m pipmind.shadow evaluate --dataset evals/fixtures/starter-v1.jsonl --repetitions 3 --manifest .data/config/model-manifest.json --output evals/reports/live-shadow.json
```

Real calls require the existing SQL/Foundry configuration and explicit `PIPMIND_MODEL_CALLS=true`. The private manifest records verified deployments, versions/settings and reference rates. Reports identify the dataset digest, prompt and grader version, fresh lineage IDs, per-phase token usage, unknown usage and refusals. Full outputs stay private in SQL and the requested ignored report; CI exercises the runner with scripted responses and actual SQL, without cloud credentials.

Integrity checks cover original input retention, unchanged mandate, paused isolation and zero dispatches. Wake/action/citation and literal-view alignment against fixture expectations are reported separately; literal wording differences are not an automatic reasoning-quality score. Invalid or refused responses remain visible. Human calibration against the versioned quality rubric remains required, and no uncalibrated model judge is invoked. Known token counts and reference rates do not establish a provider invoice or monthly cost.

## PIP-14 qualification checkpoint

The restart work separates import-only SDK preloading from worker execution, shares frozen dependency pages and uses bounded atomic SQL batches for feed inputs, retrieval indexes and checkpoints. Workers publish status after each completed step. Failed storage commits remain pending; valid recovered captures can initialize a missing checkpoint without rewinding newer state. Source configuration changes use a fenced active binding while retaining immutable metadata and the previous cursor/history state.

A zero-model-call sizing sweep over 2,580 actual pending inputs fits the existing 16,000-byte triage guard, with a maximum of 15,475 bytes. The retained working-state projection falls from 7,190 to 5,397 bytes; every original view, waiting condition and position thesis is preserved. This measures prompt size, not reasoning quality or sustained throughput.

The first repeated real-model matrix uses prompt v4: 12 starter cases, three repetitions each, 36 isolated SQL episodes, 85 phase calls and all 144 integrity checks passing. One 90-second decision timeout is safely refused with unknown usage. Fifty differences across 180 unchanged literal fixture checks remain separate from integrity and pending human quality calibration. Known counters total 79,655 input and 12,171 output tokens, about $0.169 at the verified uncached rates, excluding unknown usage, caching and invoice differences. Full exact traces remain in private SQL; private review copies contain the original fixture evidence and versioned rubric. The isolated v5 repeat has 36 episodes, 86 phase calls and all 144 integrity checks passing; it records six 90-second decision timeouts with unknown usage and 52 literal differences across 180 checks. Known counters are 77,771 input / 10,885 output tokens (about $0.152 reference, excluding unknown attempts); this is not evidence of lower billed cost. Median triage/decision latency is 1.97/41.72 seconds, and decision p95 reaches the 90-second deadline. Timeout reliability and human quality remain qualification gaps.

The expanded retained v5 probe records two valid triage replies and one refusal at the unchanged 512-token ceiling. One of three forced decisions is valid with all four application checks passing; two reach the 90-second deadline. These refusals remain in the audit. Prompt v6 adds only triage-specific guidance for one short event-focused summary while supplying the complete current state. Three actual v6 triage calls on the exact retained event/context are valid (5,047 input tokens each, 169–264 output tokens, 129–197 summary characters). Its decision request is byte-for-byte unchanged from the retained v5 actual request. The subsequent zero-RPC sweep fits all 2,794 queued inputs under the unchanged guard, with a maximum of 15,695 bytes. The operational state stays unchanged during those isolated probes. The sizing sweep measures that retained state only.

The v6 release is deployed through [workflow 39244651326](https://github.com/henrique-meyer/pipmind/actions/runs/39244651326), following CI with 349 offline and 42 real SQL tests. Independent checks confirm pinned images, one ready replica, unchanged capacity/configuration/identity/SQL egress and the unchanged public landing HTTPS/SEO/analytics flow. The earlier 2,154 feed events, immutable metadata and saved X history cursors match their retained baseline without a metadata UPDATE grant. The controlled warm run produces one valid observation/decision and then 24 pre-RPC context-size refusals: working memory grows from 11 views / 11 waiting conditions to 15 / 15, and its lossless projection grows from 5,397 to 7,287 bytes. Later prompts exceed the unchanged 16,000-byte guard. That v6 run was paused while source ingestion continued with zero dispatches. Original inputs and every refusal remain in SQL. A rising outcome counter includes refusals and must not be presented as successful inference. The owner subsequently approved bounded working-state maintenance, durable overflow holds and audited reconsiderations, described in the measurements below. These checks do not erase recorded model refusals or source outages.

Neither the repeated synthetic cases nor the sizing sweep qualifies a full FX day. Continuous broker reads, complete source/role coverage, signed callback routing, owner calibration, sustained capacity/total cost and complete retained-day export/clone remain open. See the [plan](plan.md#pip-14--complete-m4-live-qualification) and the qualification procedure below.

## Experience bundles and cloning

SQL sessions are explicitly non-pooled. On an operation failure, closing the
session aborts its uncommitted work; cleanup cannot mask the original error or
turn an acknowledged commit into a failure. A lost commit acknowledgement still
requires reading durable state before retrying. Local SQL feed outages use the
same retry path as HTTP source outages, and failed durable-pause attempts retain
the active attempt while retrying. Private status reports work-loop and
supervisor liveness as well as thread liveness; a live thread alone is insufficient.

The explicit `stream-v3` owner format extends complete experience portability
without changing v1/v2 behavior or limits:

```sh
python -m pipmind.shadow export --format stream-v3 --output .data/exports/complete.pipmind
python -m pipmind.shadow clone --format stream-v3 --input .data/exports/complete.pipmind --identity .data/identity.md
```

Its separate [manifest schema](../contracts/v1/bundle-stream-manifest.schema.json)
identifies version 3 / `shadow-stream-v1`. Bounds are 256 MiB for both the entire
archive and expanded content, 20,000 entries including the manifest, 1 MiB per
Markdown entry and 2 MiB for the manifest. V3 accepts stored ZIP members only
(an expansion ratio of 1:1, stricter than the existing 100:1 ceiling). Compressed
legacy archives remain on the original validator. Limits are simultaneous;
manifest overhead can become the limiting factor before the entry count.

SQL cursor iteration captures one complete consistent checkpoint. Each record
is written and hashed separately; no full-history byte dictionary is created.
Validation bounds the ZIP directory before allocating its entries, rejects
ambiguous ZIP layouts and unsafe paths/types, and checks exact inventory,
hashes, schemas, complete coverage, timestamps and evidence relationships.
Relationships use a bounded private disk index, not a second runtime store.
The complete archive is validated before an atomic, exclusive file publication.
Existing output files are never overwritten. An import owns a private validated
copy through its single SQL commit, rechecks every payload while consuming it,
and rolls back all destination state on interruption or corruption. Identity,
playbook, source metadata and history remain exact; source working context is
archived and the destination starts paused with separate authority.

Use private **disk-backed** staging with at least 1 GiB free. RAM-backed staging
is refused. One manual operation per container user runs at a time, and Linux
cgroup headroom must exceed 96 MiB before starting. Large file cache is released
between batches. A crashed operation can leave a private staging directory;
the process lock releases automatically. Verify no operation is running before
removing abandoned private staging files.
No persistent export service, new Azure resource or capacity increase is needed.
Qualification measures process RSS and total container usage separately; a
synthetic near-cap test does not certify headroom beside every live workload.

When transaction snapshot isolation is already enabled, export uses that existing
capability for one consistent read without taking the source application lock.
It never changes database options. Without that capability, export coordinates
writers with the existing source lock. A failed snapshot refuses the operation;
it never switches versions midway. This follows SQL's
[transaction snapshot semantics](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-transaction-isolation-level-transact-sql).
Clone holds its destination lock until commit. Schedule a reviewed paused checkpoint
when normal workers lack memory headroom or a long lock would disrupt them.
The commands never silently pause, resize or truncate a source. Measure lock
time, temporary disk, transfer, SQL clone growth and total container usage at the
actual deployment before qualifying a retained-day clone. No inference or
provider call is made by export/import; variable SQL I/O and transfer still apply.

Runtime exports use manifest version 2 / `shadow-runtime-v1`, retaining the v1 Markdown/front-matter contracts. The v1 synthetic archive profile remains supported by its validator, but cannot activate a shadow clone. A single SQL checkpoint captures all retained records and metadata, with exact identity/playbook text, provenance, evidence, history and override tags. Checksums, inventory, chronology, compatibility and all references are validated; archives are never extracted as files.

The bounded runtime profile permits 10,000 ZIP entries including the manifest, 32 MiB archive/expanded total, 1 MiB per Markdown entry, 2 MiB manifest and a 100:1 expansion ratio. Exports use stored ZIP entries, so high repetition cannot trigger ratio failures. A complete export exceeding a bound fails; it never silently truncates retained experience. Monitor retained counts/bytes and qualify the full-day export within these limits before declaring M4 complete. Larger retention/export workloads require an explicit format/capacity extension; they are not certified for the demo allocation by these fixtures.

Select a **new, empty destination trader ID** and its private destination binding, then:

```sh
python -m pipmind.shadow clone --input .data/exports/checkpoint.pipmind --identity .data/identity.md
python -m pipmind.shadow status
```

Import validates the entire bundle before staging records and the synchronous retrieval index in one SQL transaction. Interruption rolls everything back. Existing destinations are refused, preserving active state. Expected identity must match exactly. Source working memory is archived as history, current working memory is cleared, a new lineage/parent checkpoint is recorded and the destination starts paused. Account snapshots, operational cursors/outcomes, permissions, stops/overrides and broker credentials are not imported. Exact historical event redelivery is recognized as retained experience without generating a new active decision; changed facts under its ID are refused. No source order or authority can become active.

Context/retrieval versions are explicit. The current pair is `shadow-context-v8` / `sql-memory-v2`; known v1 through v7 experience bundles remain importable through a declared retrieval rebuild, with the source compatibility metadata retained in destination lineage and a transformation in the import report. Historical record bodies remain exact. Unknown version pairs are refused; this is not permission to silently migrate arbitrary future formats.

Exports are exclusively created, private files. In-repository outputs must be under ignored `.data/` or `evals/reports/`; use protected transport/storage outside the ZIP format. Clone reports list transformations without private contents. This is experience cloning, not same-account recovery, a backup of broker execution state, model-weight transfer or a guarantee of identical decisions.

## Hosted M4 approval and live qualification

No Azure resources, provider subscriptions or live calls were created by PIP-12. On 2026-10-18 the owner approved SQL S0, Foundry and the always-on backend, with availability/quota/pricing checks first. Keep the current landing deployment and domain/analytics intact. The approved deployment is:

1. In PipMind's dedicated group and existing managed environment, add a private backend Container App containing shadow engine/feed workers and the read-only dry gateway, each 0.25 vCPU / 0.5 GiB, exactly one always-on replica, default networking and built-in secrets. Keeping the existing scale-to-zero landing during validation temporarily adds a second app; it is not silently substituted into the one-backend/free-static-site planning estimate.
2. Add Azure SQL Standard S0, encrypted managed-identity access, explicit least-privilege contained users and approved firewall/egress rules. Do not silently enable broad Azure-service SQL access or buy a VNet/NAT/private endpoint. Confirm connectivity and deployment/restore behavior against the actual approved network policy.
3. Add a Microsoft Foundry resource/project with selected triage/decision deployments and scoped managed-identity roles. The preflight verified GA models, Responses support, quota and retail rates; see the [updated cost model](cost-model.md#models). No Foundry-hosted agent runtime, managed memory/search or paid evaluation platform is needed.
4. Use existing essential monitoring and owner-approved read-only provider credentials/recipes. No Blob publisher, frontend migration or broker order capability is required to run private shadow validation. Re-price the temporary layout and actual models/providers against the [cost model](cost-model.md) before approval. Represent the specific approved change in Bicep and deploy it through the established OIDC path.

After approval, verify the pinned images, ready revision, managed identities, SQL permissions/TLS/firewall, private auth/HMAC flow and one project-backed inference call. Run selected provider reads and canonical outage/backfill/revision checks, preserving coverage gaps. Confirm read-only broker account/environment and independent evidence of no order requests; application counters alone are insufficient broker evidence.

Run a complete declared FX trading day. Record private input/outcome counts, backlog/lag, feed freshness/quarantine/coverage, process restarts, SQL DTU/latency/lock failures, CPU/memory, Foundry deployment/version/settings/tokens/latency/errors and actual usage charges. Export/clone the complete retained day, verify retrieval/provenance, review triage and reasoning with repeated model evals/human calibration, and compare actual costs with the allowance. Unexplained gaps, capacity pressure or unverified source/broker evidence keep the gate open. Archive only sanitized validation findings in public docs; private journals and live event contents stay private.

Connector references: [Agent Framework Foundry provider](https://learn.microsoft.com/en-us/agent-framework/agents/providers/azure-ai-foundry?pivots=programming-language-python), [python-tds](https://github.com/denisenkom/pytds), and [SQL Server full-text installation](https://learn.microsoft.com/en-us/sql/linux/install-upgrade/setup-full-text-search?view=sql-server-ver17).

### Hosted deployment procedure

`infra/shadow-foundation.bicep` creates only the approved identity, SQL server/S0 database, Foundry resource/project/models and project-scoped Foundry User grant. Owner bootstrap applies this separately; CI's existing resource-group Contributor identity is not given role-assignment authority. SQL uses Entra-only authentication, TLS, Proxy connections and explicit address ranges. Subscription capability checks require a separate supported SQL region for this demo; both East US SQL locations were restricted. Private parameters select Central US at the same S0 retail rate.

The existing managed environment advertises more egress ranges than SQL's 256 server-rule limit. Keep the exact ranges: up to 254 server rules plus a temporary owner bootstrap IP; the remaining ranges use database-level rules (also limited to 256). Never substitute the all-Azure `0.0.0.0` rule. `scripts/bootstrap_shadow_sql.py --config <ignored JSON>` applies the database rules, schema/full-text, paused initial observation-only mind and contained identity grants through the owner's existing Azure CLI login. Configuration contains `sqlHostname`, `databaseName`, `identityClientId`, `trader`, `bindingRef`, `databaseFirewallFile` and `feedConfigFile`; all real values stay under ignored `.azure/`. For a managed identity, SQL's SID must derive from its **application/client ID**, while Azure role assignments use its different **principal/object ID**. Verify a real runtime login; owner impersonation does not prove that binding. Remove temporary owner access after verification. Recheck actual backend egress coverage and alert on environment address changes; default networking provides no static-egress guarantee. [SQL firewall limits](https://learn.microsoft.com/en-us/azure/azure-sql/database/firewall-configure), [SQL managed-identity SID guidance](https://learn.microsoft.com/en-us/sql/connect/python/mssql-django/microsoft-entra-authentication).

Runtime identity uses optional `PIPMIND_MANAGED_IDENTITY_CLIENT_ID` for both SQL and Foundry. Grants cover only required table operations; the app cannot create users, change firewall rules or perform schema DDL. The pinned TDS driver calls removed pyOpenSSL extension APIs, so `shadow.tls` supplies SAN hostname validation through MIT-licensed service-identity/cryptography, retaining verified certificate chains and encrypted transport. Negative hostname/wildcard tests and a real encrypted SQL connection verify this path. [Verifier API](https://service-identity.readthedocs.io/en/stable/api.html).

`infra/shadow-app.bicep` manages only the new internal-ingress backend: one replica, engine and gateway at 0.25 vCPU / 0.5 GiB each, user-assigned identity, health probes and container-specific secret recipe volumes. The manual **Publish and deploy demo shadow** workflow builds the two targets from a main commit, publishes existing public GHCR package tags, resolves immutable digests and deploys via the existing demo OIDC identity. Supply the ignored ARM parameter JSON as environment secret `SHADOW_APP_PARAMETERS`; `appSecrets` is a secure object with secret-name keys. Images contain no real configuration. The landing template and release workflow are independent.

`PIPMIND_GATEWAY_UNCONFIGURED=true` starts an authenticated sidecar with empty prices/snapshots and explicit `broker_reads_disabled` status. Every command is refused and enabling broker reads in this state is rejected; no invented broker identifiers or synthetic snapshots enter hosted data. Owner-provided demo credentials now pass direct read-only account/market requests, normalized Lightstreamer prices and snapshots, and Reuters news reads against the confirmed Live server pair. Demo identity is verified independently of that server label. The owner-approved read-only profile below provides continuous handoff, paced snapshot/price capture and broker-origin evidence; full-day comparison still needs qualification. Current empty positions/orders and existing historical records establish a baseline only. See the [read adapter guide](gateway.md).

The owner selected the FXMacroData $50 monthly plan and completed checkout; authenticated EUR/USD macro history reads succeeded. Its issued key stays in ignored `.env` and built-in runtime secrets. Annual billing is not selected. A paid subscription and successful REST request alone do not certify SSE/webhook timing, complete provider coverage or a full M4 trading day.

The separately approved PipMind X app uses an app-only credential in private configuration. Nine institutional account recipes now have hosted verification using actual profile fields and exact official-page backlinks, and timeline reads are active. A controlled SQL binding change preserves source identity, retained events and cursors without expanding metadata permissions. Initial reads seed at most the latest page; previously unfinished history cursors remain retained, and later live posts retain complete pagination. This is a verified institutional cohort, not completion of the broader personal office-holder, correspondent, event-window squawk or Truth Social mirror plan. No user access, X write or new credit purchase was requested. Full-day provider/broker/capacity/model-quality qualification remains tracked by PIP-14.


## Source coverage qualification (PIP-14)

The hosted source audit distinguishes configured recipes, successful provider
reads, retained normalized events and complete-history or point-in-time claims.
Thirty-three native-unit macro mappings cover eight currencies; unsupported
consensus crosswalks, unknown publication/vintage and incomplete monitoring stay
explicit. A captured REST window is not proof of complete source history. Signed
public callback routing still needs specific approval; SSE and REST backfill do
not certify that backup route. PMI headline coverage is not structured PMI actuals.

Calendar cache snapshots also become immutable, dated SQL evidence. Model feed
facts carry only the snapshot's record ID, fetch time and row count. Decisions
can use `search_memory` with `record_id | currency and event title` to retrieve
relevant schedule/consensus rows. Projections disclose omitted rows, while the
complete original remains in SQL and experience exports. Snapshots do not flood
ordinary news retrieval with a week's keywords. Unknown publication time stays
unknown; future scheduled dates are observations of a schedule, not released
actuals. Revised snapshots get distinct IDs without changing earlier evidence,
working memory, account authority or input ordering. Cached references refresh
at most once a minute; the provider retains its hourly polling limit.

The gateway reads a bounded Reuters FX headline window every minute using its
existing read-only session. It validates the vendor publication time separately
from receipt, retains revisions with distinct content identities, and hands the
cached page to the SQL news store atomically before the price handoff cadence.
Repeated reads and consumer restarts deduplicate by stable identity. The engine
receives normalized breaking news; it receives no broker credentials, article
body reader or order transport. A news-only outage retains the last successful
cache and an explicit error without interrupting price capture. Missing or future
vendor publication times are refused. The latest 100 headlines are a bounded
polling window: a full window or an outage never proves lossless historical wire
coverage. News status preserves this limitation and each RSS source's last
success independently of its latest failure. Worker liveness is not source health.

Free source additions require live parsing checks and the existing controlled
additive SQL recipe migration. Keep the original source identity, events and
cursors; do not drop an unavailable recipe to claim complete coverage. Broader
personal office-holder/correspondent/squawk/primary-channel recipes require their
own identity, availability and cost evidence. The verified institutional cohort
does not certify those roles. Repeated model integrity, literal fixture alignment,
retained-context semantic fidelity and owner calibration remain separate results.

The hosted additive cohort now has sixteen RSS recipes. New MarketWatch,
Bundesbank, BLS employment and BLS CPI recipes each deliver normalized events;
BabyPips remains an explicit HTTP 400 outage with its prior successes retained.
The RBNZ candidate remains unavailable from the hosted network and is not added.
The nine institutional X identities are unchanged; three broader candidate
profiles remain excluded because official identity evidence is insufficient.
All 33 macro catalogue selections are available, but every provider quality
window still reports that point-in-time safety and complete source monitoring
are unproven. Two legacy percentage labels are explicitly year-over-year series
with unit scale one; their original events and labels are preserved.

Hosted release checks confirm a full 100-headline Reuters cache and normalized
SQL delivery, with the full-window limitation retained. The exact 86-row calendar
snapshot and focused native retrieval pass. Original source metadata, saved
cursors, immutable mind records, inputs, outcomes and feed prefixes match through
the additive migration and release. No new capacity, identity/network access,
public callback, paid provider or trading capability is introduced.

The repeated-evaluation grader `shadow-live-v2` also requires every episode's
inputs to have completed outcomes. An operational SQL failure is reported
separately from a model refusal. Known responses/usage before a failed commit
are retained as an uncommitted attempt and a separate SQL diagnostic when
available; they never become invented outcomes. The CLI saves partial reports
after each episode and before an unconfirmed pause aborts the run. A clean retry
does not erase an earlier partial episode or unknown usage.

### Repeated model and retained-context measurements

The current no-calendar request path was exercised in twelve starter cases,
three repetitions each, through isolated SQL minds. The selected complete cohort
passes all 144 `shadow-live-v1` integrity checks, with one retained 90-second
decision timeout and 43 differences across the unchanged literal fixture checks.
Integrity success does not establish semantic quality. The cohort has 86 phase
calls and 95,800 known input / 14,728 output tokens; the timeout has unknown usage.

Two original interrupted SQL runs are retained separately from their clean
retries: four initialized episodes contain five committed outcomes and one pending
input. Seven calls have retained usage; the phase, response and usage of the
remaining unconfirmed attempt are not reconstructed. All validation minds are
paused. The newer grader's complete-episode and commit-failure diagnostics do
not retroactively relabel these older reports.

Three retained-context trajectories process four inputs each: nine observations
and three retained analysis items, with working state between 2,852 and 2,877
bytes. These are present-time replays using frozen current context, with decisions
forced for measurement. They are neither historical point-in-time tests nor a
measurement of live decision frequency. Original prerequisites remain unresolved.

Three initial calendar daily briefs did not use the available schedule. Prompt
`shadow-context-v11` adds calendar guidance only for a daily-brief decision with
a snapshot reference. Three corrected trials each retrieve calendar evidence,
distinguish schedule/consensus from actuals, cite the snapshot and preserve exact
working state. The new compiler also reproduces all 86 archived ordinary
requests exactly with zero model calls. Prior v9/v10 experience bundles remain
explicitly supported. Full original snapshots, model/tool traces and failed
attempts remain private in SQL.

Across the complete cohort, original interrupted runs, retained trajectories,
calendar baselines, an extra validator run that still used v10, and the corrected
v11 trials, 132 retained phase entries contain 378,269 known input / 40,602 known
output tokens. Their uncached rate reference is about $0.85, excluding one known
timeout's unknown usage and the unconfirmed pending attempt, cache adjustments
and invoice differences. This is evaluation usage, not a monthly cost or a claim
that timeout reliability is qualified. Three actual responses are prepared for
owner calibration; human labels and the full-day quality/capacity gates remain
open.

## Bounded working memory (PIP-14)

Owner-approved `bounded-working-v1` keeps concise current views and active prerequisites in prompts while retaining exact earlier state, events, responses, dates and provenance in SQL. It is M4 state maintenance, separate from M7 playbook revision. Identity/playbook and all confirmed position theses remain intact. Neither age nor an unavailable article resolves a condition. Headline catalogues and general coverage gaps belong in retained evidence rather than becoming one permanent current view per item.

Activation is an explicit paused owner operation. Prepare a private JSON plan with the current revision, SHA-256 of canonical working state, replacement working memory, a disposition for **every** old view/waiting entry and review acknowledgment. The plan preparer records `no-unresolved-ambiguity` only after checking every disposition; resolve any semantic ambiguity with the owner before activation. This acknowledgment does not claim that the owner personally reviewed an unambiguous plan. The implementation checks the revision/hash under the SQL lock, validates references and exact initial theses, and writes the complete before/after/dispositions checkpoint atomically. No source history is rewritten. Keep the full review plan private.

```sh
python -m pipmind.shadow maintain-working --plan .data/working-plan.json
```

The active policy allows at most 5,000 UTF-8 bytes for the complete working state, eight views and eight waiting entries, and 300 UTF-8 bytes per view/condition. Theses are not clipped to make room; an oversized mandatory state holds reasoning. These are context bounds, not a trading strategy or spending cap. Ordinary decision replies use the existing working-memory contract plus `memory_dispositions` for each changed/removed old entry. Keys `v0`/`w0` refer to the supplied before-state array indexes; targets refer to the after-state. Each disposition names a retained/updated/merged/archived/resolved status, reason and known dated evidence. An unresolved condition needs a same-kind replacement; a null waiting target requires an explicit evidence-backed resolution. Structural checks cannot prove semantic fidelity: model/human evaluations must test lost prerequisites, contrary evidence and false resolutions.

Every accepted policy update retains an immutable checkpoint and previous-checkpoint link; prompts name both latest and original checkpoints. Search `checkpoint-id | topic words` to retrieve matching exact entry passages/dispositions. Partial results disclose that they are incomplete; the complete original remains exportable in SQL. There is no archive age cutoff. Experience clones retain all checkpoint records exactly but clear source current state/account authority and require their own reviewed activation.

Pre-RPC request-projection failure, prompt overflow, bounded-state overflow, missing dispositions or invalid transition evidence atomically record a held attempt, pause the mind and leave the input pending. The authenticated status reports durable `reasoning_hold`, including after a process restart. Source ingestion continues. A held attempt is not an outcome or a successful inference; known model usage/candidates remain audited. After correcting state/policy, explicit resume retries the pending input. The 16,000/28,000-byte prompt guards and 512/4,096 output limits remain unchanged.

Prior releases consumed context refusals as immutable outcomes. For those, the paused owner CLI can queue exactly one separate present-time reconsideration per original input:

```sh
python -m pipmind.shadow reconsider --event-id retained-input-id
```

It verifies a retained pre-RPC size refusal, stores authorization and the original outcome hash, and queues a new clock input linked to the original. Owner-queued reconsiderations precede ordinary pending inputs; duplicate requests reuse their ID. Ordinary input ingestion cannot forge this control event. The model receives the original input with current state and current time; this never fabricates a decision at the original receipt time or resets an outcome/cursor. All attempts remain separate. Full-day quality, throughput/cost, complete retained-day clone and independent broker evidence remain open M4 gates.

Checkpoint cards are idempotent across SQL selection, request assembly and native archive reads. Request-projection failures retain their exact source context as a known pre-RPC held attempt (`attempted:false`, `usage_unknown:false`) and pause without consuming the input. An unconfirmed placeholder usage record must not be described as a model timeout; retain the original attempt and a separate diagnostic classification.

### Bounded-state hosted measurements

The complete initial disposition review consolidates 15 views / 15 waiting entries (9,843 bytes) into five institution/indicator views and five unresolved prerequisites (1,987 bytes). No unknown prerequisite is resolved. Exact original text, dates and individual dependencies remain in SQL checkpoints; an original QT/APF dependency is retrievable after export and cloning. Current identity/playbook are unchanged.

Two isolated retained-context trajectories exercise actual project-backed calls and sequential working-state updates. The corrected cohort contains eight attempts: six observations and two original pre-RPC projection failures. The two affected cases succeed on separately retained repeats, without rewriting their failures. Fourteen model RPCs execute (eight triage, six decision); working state stays at 1,987–2,060 bytes and all five initial prerequisites remain unresolved. Dated older and contrary publication evidence is preserved. Mean input is 4,257.75 triage / 7,227.67 decision tokens; median phase latency is 5.11 / 15.39 seconds. Known uncached reference usage costs $0.1556254. Decisions were forced for measurement after recording real triage, so this is not live decision frequency. No actual model timeout occurs in this controlled cohort; earlier timeout trials remain retained, and human calibration is still required.

Before activation, a zero-RPC sweep checks 2,800 pending inputs plus 24 reconsiderations. The largest triage request is 13,761 bytes, or 15,264 bytes with the full 3,200-byte working allowance, below the unchanged 16,000-byte guard. This is fixed-context sizing, not a guarantee against every future event or mandatory-state overflow. Actual main activation produces separate observed outcomes for all 24 reconsiderations while their original refused outcomes remain exact.

A complete retained checkpoint exports at 12,364,949 bytes with 378 files and 377 source records. The paused clone preserves exact source bodies and metadata with the declared working-part transformation, separate destination authority and original-condition retrieval. Source authority remains unchanged. This checkpoint is not a complete FX day; the 32 MiB / 10,000-entry limits still fail rather than truncate a larger history.

After the initial live sample, reasoning stops progressing at outcome 171 despite healthy HTTP responses. The original active phase and RPC usage are unknown. Its input and held attempt are preserved. [Release 39265812217](https://github.com/henrique-meyer/pipmind/actions/runs/39265812217), after CI with 364 offline and 59 real SQL tests, adds the worker supervision described above. Independent checks verify pinned images, one ready replica and unchanged allocation, configuration, identity, SQL egress and public landing. The preserved input completes and clears the operational hold. A longer live run measures progress and failure labels separately; successful startup alone does not qualify worker reliability or M4.

At the owner's restart checkpoint, final SQL inspection records 89 further outcomes: 88 observations and one 90,077-ms triage timeout with unknown usage. All 171 earlier outcomes and the original 80 audit records remain exact. The timeout is retained as a refusal, and the worker continues processing. There are 134 executed calls (89 triage, 45 decision), with known counters of 712,586 input / 60,008 output tokens and a $1.1995773 uncached reference excluding the unknown attempt. Median triage/decision latency is 4.38 / 15.84 seconds. Working state remains exactly 2,556 bytes with six views, six unresolved prerequisites and no position theses; the original conditions are unchanged. This is a backlog RSS/X sample, not steady-day wake frequency or a monthly invoice.

The private observer retains 118 samples over 35.5 minutes and 88 further outcomes. Four status polls return HTTP 503, with three captured as `shadow_store_busy`; its first unhandled 503 causes a 206-second observation gap before a retrying observer resumes. The source continues progressing, and both containers have zero restarts. Observed cgroup memory peaks at 445.6 MiB within 512 MiB, including diagnostics; this is limited headroom, not full-day capacity certification. The owner requests a restart before the 100-outcome target is reached. Temporary observers stop and their reports are saved; the Azure source remains resumed with no current worker error/hold and zero dispatches. Complete the validation target and investigate contention/model reliability before claiming full M4 qualification.


### Resumed worker validation and deferred archive reads

After the owner restart, durable SQL inspection extends the worker sample to 194 outcomes: 193 observations and the same single triage timeout. All 171 preceding outcomes and the original 80 audit records remain exact; the preserved stalled input has completed. Known counters total 1,448,622 input / 117,416 output tokens over 278 executed calls. The source is resumed with zero dispatches, no current hold/error and unchanged 2,556-byte working state. This exceeds the earlier 100-outcome progress target, but includes a gap in local observation during the owner restart and remains a backlog sample rather than a complete FX day.

Engine cgroup samples range from 467.5 to 478.9 MiB of the existing 512 MiB allocation, predominantly anonymous application memory. Both containers have zero restarts and no cgroup OOM kill is recorded. These samples establish limited headroom, not a proven linear leak or adequate full-day capacity. No capacity increase is authorized by this investigation.

A read-only sidecar profile of the deployed context builder transfers about 10.5 MB of candidate archive text per sample, holds the mind lock for about 3.4 seconds and reaches about 12.5 MB of traced transient allocations. Triage then excludes that archive from its request. The candidate defers archive retrieval until triage requests a decision; quiet inputs, analysis and coalesced prices do not need it. Three read-only candidate samples hold the lock for 173–243 ms and reach 98–103 KB of traced allocations. End-to-end elapsed time still includes waiting for the deployed worker's locks. These are diagnostic measurements, not postdeployment capacity certification.

Decisions retain the same event-query and active-view searches, original chronology cutoff, selected evidence and native historical search. Before loading decision evidence, SQL verifies the revision captured for triage and rejects a concurrent pause/state change. Database/evidence-loading failures retain the pending input and known triage usage for the worker's operational hold; they are not consumed as model refusals. Original audit bodies remain in SQL. Decision archive reads still have their previous cost, and a new hosted release requires independent verification and further memory/status observations.


### Audited bounded-output correction

The earlier prompt `shadow-context-v10` aimed below the then-current 3,200-byte working-state ceiling and tells the model to copy unchanged entries exactly. A schema-valid decision whose state exceeds its bounds or has invalid/missing structural dispositions receives at most one tool-free `memory_repair` call. The correction sees exact current mandatory facts and the rejected candidate, without archive passages. It returns only working state and mappings from both the prior state and every candidate entry. Each candidate entry needs a same-kind replacement; candidate position theses remain exact, and a correction cannot newly resolve an old waiting condition. Action, instrument, side, size, reasoning and decision evidence are copied unchanged from the original decision. SQL still validates provenance, chronology, revision and ordinary memory transitions atomically.

The journal retains the exact original candidate, correction response, both mapping sets, before-state hash and every call's known/unknown usage. Invalid, timed-out or oversized correction holds the input pending and pauses; there is no retry loop, implicit resumption, state clipping or raised limit. Structural mappings do not prove semantic fidelity: retained-case and repeated-growth review remain required. A previously held source requires explicit owner-authorized resumption after validation. One correction RPC is additional measured decision-model usage; Azure capacity and providers remain unchanged.

`shadow-context-v12` supplies exact candidate UTF-8 entry and serialized sizes to
the correction. Its instructions distinguish the independent 300-byte entry and
3,200-byte total limits, allowing up to eight entries per list rather than forcing
an oversized merge. The correction schema also rejects more than eight entries,
duplicates and strings longer than 300 characters; runtime validation still
checks UTF-8 bytes, which can exceed character counts. Successful schema parsing
alone cannot certify those byte limits or dependency fidelity. All original
responses and known/unknown usage remain retained; the single-call budget and
fail-closed hold are unchanged.

The compression-only call uses low reasoning effort and concise mapping reasons
to reduce timeouts; normal triage and decision settings are unchanged. Already
resolved before-state dispositions retain their exact original reasons/evidence,
including longer reasons. Correction traces record the effort setting. The
90-second deadline and 4,096-output-token ceiling remain unchanged.

### Repair-hold release measurements (2026-10-20)

A retained 3,285-byte candidate received a 2,871-byte correction whose merged
waiting condition was 311 bytes. The independent 300-byte entry guard correctly
held the input. Three final v12 retained corrections pass all mappings and byte
checks at 2,872-2,954 bytes, taking 49.6-68.2 seconds. Existing SQL evidence is
verified separately; the current event remains the exact pending input that the
ordinary commit archives before validating its references. Normal triage and
decision requests compile byte for byte identically to the retained requests.

All twelve correction trials, including earlier variants, remain separately
retained: nine successes and three 90-second timeouts with unknown usage. Known
validation usage totals 60,705 input / 22,350 output tokens, excluding the original
source attempt and production retry. Unknown calls are not zero-cost calls;
these counts are neither an invoice nor a monthly estimate. All 4,302 sampled
pending inputs fit triage without a model RPC: maximum 15,386 bytes, or 15,851
allowing the full working-state allocation. These are current-context checks,
not proof every future prompt will fit.

CI passes 454 offline / 90 actual SQL tests and the required container,
infrastructure and secret checks. The backend release preserves capacity,
configuration, identity, SQL egress and the landing. Its held input commits as an
observation and reasoning resumes. Complete original prefixes match: 4,043
immutable mind records, 1,817 outcomes, 6,101 inputs, 4,253 feed events and 1,282
raw inbox entries. Binding, lineage, dependency origin and retained cursor
progress are preserved. The prior warm-up remains retained and superseded; its
replacement is warming on startup backlog. No formal 24-hour day or full M4
qualification is established by this release.

### Exact carry-forward mapping correction

Prompt `shadow-context-v13` distinguishes literal carry-forwards from wording
changes in both correction mapping arrays. Before validation, the engine converts
an `updated` or `merged` label to `retained` only when its named source and
same-kind target have exactly equal text. It records each label conversion and
keeps the raw model response unchanged in the audit. No state text, decision,
evidence or resolution is inferred or changed. Changed text, missing/duplicate
mappings, cross-kind targets, new resolutions and oversized state still pass
through the same strict validators and fail closed. This deterministic identity
check prevents a redundant update label from holding an otherwise valid repair;
it does not prove semantic preservation for actual rewrites.

When total size, entry counts and uniqueness already fit and only particular
entries exceed 300 UTF-8 bytes, the single correction uses the
`memory-repair-patch` contract. It may replace exactly those entries; the engine
copies every other candidate entry and position thesis literally, retains the
decision's before-state mappings, and derives all candidate carry-forwards.
Missing, duplicate or extra replacement keys fail. The raw patch and expanded
state/mappings are audited, then the ordinary byte, dependency, evidence and
transition checks run unchanged. Total-size, count or uniqueness overflow uses full
consolidation. Both paths share the existing one-call, timeout and output budget;
there is no automatic retry loop or fallback that discards a failed attempt.

Small entry overflows first try removing only optional ASCII spaces after
commas/semicolons/colons before letters. Words, punctuation and numbers stay in order;
quoted/code-like text, URLs and numeric punctuation spacing are untouched. This requires all
ordinary transitions to validate and uses zero model calls. When a model patch
is needed, its raw proposal allows up to 1,000 characters for audit/validation;
the same spacing operation may compact an overlong proposal before validation.
If the expanded patch exceeds only the total limit, that representation-only
operation also applies to its selected entries, even when each entry fits. All
unselected entries remain exact unless selected-entry formatting is insufficient.
Then the engine may apply the same lossless formatter to literal carry-forwards,
stopping when the bound fits. These are separate `format_only_normalizations`,
never additional model edits: each must equal the safe formatter and gets an
explicit same-kind mapping. Quoted/code-like text and numeric punctuation spacing
remain protected; all theses and decision fields stay exact. Residual overflow still holds; no words or
additional model calls are used to repair formatting.
The committed entry limit remains **300 UTF-8 bytes**, never 1,000 characters.
The exact candidate, raw patch, expanded applied text and spacing conversions
remain auditable; residual overflow or any dependency/transition fault holds.

Repair prompt `shadow-repair-v17` uses full consolidation for total overflow,
even when counts and individual entries fit. Fixed-topology total patches
repeatedly returned the same dense catalogue above the bound. Consolidation may
factor repeated qualifications with explicitly named scope and same-kind merges;
it must preserve every dependency, measurement label, date, unit, uncertainty and
verification distinction, including EACH-item and FULL TEXT prerequisites.
The target is below 2,400 bytes, with hard 300-byte entry and owner-approved 5,000-byte
total guards. Missing provenance, a newly resolved condition or residual overflow
still holds. One model response cannot guarantee semantic fidelity; retained
case review and broader human calibration remain necessary.
Repair requests use correction instructions instead of inheriting normal decision
update instructions. They still receive the exact BEFORE/candidate state and
standing identity/playbook; neither tools nor new facts are authorized.

The owner approved the larger active total after repeated valid-entry states exceeded the earlier total. The 2,400-byte target, entry/count limits, mandatory-state refusal, prompt guards and single-call budget remain. `working-budget-v2` is recorded in new prompt contexts and transition checkpoints; legacy `bounded-working-v1` provenance, original source records and bundle/storage protocols remain exact.

### Processing throughput

SQL-backed steps prepare the pending input, current mind, policy and latest broker/feed facts in one transaction. It closes before inference; no connection or lock spans a model call. Reconsideration priority, present-time authorization, future-input refusal and commit-time revision/pause checks remain. Every subsequent input reads a fresh frame; no mutable mind or broker snapshot is cached, and SQL sessions remain non-pooled.

Normal Foundry decisions use the lossless `working-refs-v1` wire representation. In the complete ordered `views` and `waiting_for` arrays, an integer copies the complete expanded BEFORE string at that same-kind index. Literal strings supply new or changed text. Position theses retain their ordinary shape. The adapter expands references before validating the existing normative reply and all transition, evidence, dependency, count and byte guards. Dropping a waiting condition still requires an explicit supported resolution. Invalid indexes fail closed; booleans/floats are not indexes. Persistent working state and portable bundles always contain full strings.

The audit retains the exact supplied context, raw reference response, decoded reply, index mapping and encoding version. This reduces repeated text generation without changing model roles, reasoning settings, output/call limits or the trading policy. Triage and correction schemas remain unchanged. Full prompt guards still apply to the wire schema/instructions; inference and sustained net drainage require hosted measurement. A successful deployment or completion count alone does not establish that arrivals are being outpaced or start the formal day.

Calculated text budgets remain available for scoped entry patches and the
lossless preflight/replay path. They account for exact JSON framing, escapes,
unselected text and theses. Targets are guidance, never permission to clip text.
BEFORE-to-candidate mappings are composed with the indexed patch: a previously
unmapped literal carry-forward receives a mapping only when that exact entry is
now edited. Existing resolutions remain exact; missing provenance for another
candidate change is never invented. Candidate mappings, calculated budgets,
patches and derived state remain audited. Normal triage/decision requests retain
the `shadow-context-v15` prompt version. Triage receives all current memory but omits decision-only update/disposition instructions; decisions retain those instructions with the approved total. The
single 90-second repair call and 4,096-token ceiling are unchanged.

Backlog estimates require measured completions and new arrivals over comparable
healthy intervals. Resumption alone is insufficient, and a paused interval is not
a measure of active throughput. If arrivals meet or exceed completions, there is
no finite clearance estimate at that rate. Existing pending-count, input-lag and
other readiness gates remain unchanged.

## Full-session capture and readiness (PIP-14)

Enable the private in-process recorder with `PIPMIND_SHADOW_QUALIFICATION_ENABLED=true`; it shares the existing engine process and SQL tables and adds no service, resource, capacity or grant. Set `PIPMIND_SHADOW_GATEWAY_INTERVAL=300` for the demo's paced durable handoff, while refreshing broker facts before every step. The recorder is independent of the reasoning batch and survives replica restarts through SQL state. It cannot send commands, change the mind or certify M4.

Ordered backlog steps continue immediately after successful progress; idle/error polling retains its backoff. Every step still refreshes source/broker facts and commits one input in order. No historical input is skipped to meet the lag threshold. Encrypted SQL connections reuse a bounded verified TLS trust-store context, invalidated when its CA file changes. SQL sessions, managed-identity authentication, hostname/chain verification and separate TLS connections remain unchanged. This avoids repeated native CA allocations; sustained container memory still requires measurement.

Local feed handoff reads the next bounded event page, coverage, quarantine count
and any due calendar in one profile-checked store transaction. It does not read
unused event totals or alert lists on each input. Events/cursors and provider
polling schedules are unchanged; a read that crosses the calendar refresh
boundary still fetches the now-due calendar. Confirmed UTC clock slots are
memoized only after a successful durable check/acceptance. Boundaries, failures
and service restarts recheck SQL, preserving duplicate suppression. Every input
still refreshes actual broker facts; no source backlog is hidden or deferred.
These changes reduce redundant reads without changing model settings, memory
bounds, receipt timestamps, capacities or full-day readiness gates. Measure net
incoming/completed work after deployment before estimating a start.

An authenticated owner operation calls `pipmind.shadow.qualification.arm(store, run_id, instruments, build, moment)` with a fresh private run ID. Only one warming/running request is allowed. The immutable request and minute samples live under namespaced private SQL runtime keys; slot insertion and cursor advance are atomic, so overlapping revisions cannot overwrite samples. The private `/status` reports the run and recorder liveness/error. All source minds, events, failures, outcomes and dependencies remain intact.

Warm-up capture begins immediately. A precisely declared continuous **24-hour open-FX window** starts automatically only when the engine/work/supervisor and feed workers are alive, broker demo/market verification, the request's quote policy and fresh snapshots/order evidence pass, no activity is reported, and startup backlog is at most 100 pending inputs with oldest receipt lag at most 300 seconds. The window uses named `America/New_York` weekend boundaries, including DST; it cannot include closed weekend hours. Time-zone data uses the Python-maintained Apache-2.0 `tzdata` package for portable behavior. Warm-up exceeding 24 hours fails explicitly rather than waiting indefinitely.

Existing requests retain `quote_policy="fresh_ticks"`. A new request can explicitly select `arm(..., quote_policy="observed_staleness")` to validate continuous capture and honest stale-data handling during quiet sessions. This keeps the 15-second stale classification and exact venue/receipt timestamps. It requires every instrument to be present, a current gateway check with honest age/flags and a connected shared stream (or fresh REST fallback for every pair). Missing/future/false-fresh quotes and transport failure still fail. Account/position snapshots, independent order evidence, worker/engine health, backlog and other gates keep their thresholds. Per-instrument quote ages, sources and stale intervals remain retained for later coverage review; recording an aged quote is not permission to trade it or proof of provider quality.

The gateway distinguishes instrument silence from shared transport failure: quiet pairs retain their stale flags and paced REST fallback without disconnecting healthy peers. It drains callbacks again after REST/account/history I/O so those reads do not delay publication of already received fresh updates. London/New York session comparisons are separate provider qualification evidence; a quiet quote alone does not establish an outage or its cause.

Each minute retains broker-origin order evidence, read freshness, pending/input lag, outcome/model counters/settings/known and unknown usage, provider coverage/worker heartbeats/restarts, engine cgroup memory/CPU counters and current build/account binding. Exact evidence is private; prompts are not duplicated into telemetry. Operator-side Azure metrics supply SQL DTU/CPU/storage and replica/capacity measurements using existing owner access; no monitoring role is added to runtime identities. Warm-up observations are distinct from the declared day.

Per-process resident memory is also sampled for attribution; it includes shared pages and must not be summed as actual container usage. The cgroup value remains authoritative. A changed build fails warm-up before it can declare a day. Retain that failed request and all samples, then arm a new build-specific run rather than overwriting its history.

After the start, capture gaps over 180 seconds, an account or evidence-window change, build change, quote-policy violations, stale snapshots/order evidence, unhealthy workers, excess backlog or broker activity remain accumulated failed checks. Failed samples and earlier outcomes are never erased. Elapsed wall time alone cannot pass. The recorder ends as `capture_failed` or `captured_pending_review`, **never qualified**. Full retained-day streaming export/paused clone, provider coverage explanations, independent end-of-day broker comparison, model-quality/human calibration and actual cost/capacity review still gate M4. Known source outages and unapproved signed callbacks remain explicit coverage gaps even when capture is running. Capacity stays unchanged and the public landing/HTTPS/SEO/analytics are independent.


## Checked owner resume (PIP-32)

Operational release tools can use `SqlMind.resume_checked` to resume an exact owner-paused source. The caller supplies the complete expected paused head, complete selected pending envelope, retained owner-control journal ID and body hash, an explicit request ID and the current timestamp. The existing source lock covers all comparisons, the no-hold check, immutable resume audit and paused/revision update in one transaction. Mismatches refuse without changing source state. This internal operation adds no public route, model tool, trading ownership or order authority.

An identical retry can acknowledge only the immediately resumed, otherwise unchanged state with the same pending input and no new hold. A changed request, advanced source, new pause/hold or changed pending selection refuses; an old successful receipt never authorizes a later resume. Existing ordinary pause operations remain unchanged. The bounded request and its exact source/control dependencies are retained in the journal, and rollback preserves both the pause and audit atomicity.

This source implementation and its SQL tests do not deploy or resume the hosted application. Qualification arming, reviewed release selection, fresh provider/source baselines, independent broker evidence and post-release runtime validation remain explicit operational steps. Production fencing/reconciliation remains M5 work.
