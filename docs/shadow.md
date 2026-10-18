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

`init` and imports start paused. `resume-shadow` enables only journaling/model calls, and grants no trading authority. Pause increments the revision; an in-flight stale model response cannot commit. `run-once` processes one accepted event; `ingest --input <private JSON>` accepts a bounded `{source, events:[{sequence,event}]}` page. The HTTP equivalent is authenticated POST `/inputs`, at most 256 KiB. GET `/status` is authenticated; `/healthz` identifies shadow mode with `trading:false`. Health is process liveness, not proof of feed/model/SQL health; inspect status and progress separately. Docs/order/control routes are absent.

Integrated feed routes are mounted at `/feeds`, with their own bearer policy and HMAC validation for `/feeds/webhooks/fxmacrodata`. Webhooks acknowledge durable inbox acceptance before model work. Never publish private status/input/relay routes without suitable access controls. Separate deployment layouts can retain the standalone feed API instead.

## Processing and persistence

SQL stores the immutable identity/current playbook, working context, journal, retained memories/reviews and playbook history. Each input plus source checkpoint is one serializable transaction. Completion atomically writes input evidence, the structured decision/refusal and context/tool/usage trace, working memory and aggregation state. Unique identities and fact hashes reject conflicting redelivery. A per-trader transaction application lock and optimistic mind revision prevent concurrent response overwrites; these are **not M5 broker fencing**. A crash after inference but before commit may repeat a billable call; it cannot duplicate a committed outcome.

Every accepted logical input remains evidence. Analysis is retained without a strong decision wake. Price inputs are durably coalesced into per-instrument count/range/last-quote windows with at most one wake per 60 seconds; snapshots are observations, never invented fills. A 15-minute durable heartbeat supplies clock wakes; explicit daily/session clock inputs use the ClockEvent contract. No unverified timezone/session schedule is inferred. Source outages become explicit context/status gaps while already accepted inputs continue processing.

`PIPMIND_SHADOW_PRICE_INTERVAL` (1–3,600 seconds) and `PIPMIND_SHADOW_CLOCK_INTERVAL` (60–86,400 seconds) configure those wake intervals. They are batching settings, not monetary cutoffs. Coalesced quote records remain retained but do not crowd actual decisions out of the recent-journal prompt tail. Qualify wake volume and export size against actual event rates; the defaults are not a verified cost/capacity profile.

Context contains the five mind parts, broker observations and feed quality flags. Identity/playbook form a stable immutable prompt prefix. Retrieval uses SQL full-text search plus a synchronous term index for immediate post-commit visibility, filtered by trader and time. Queries use at most 12 terms and return at most five passages; recent journal uses ten bounded passages. Read tools are `read_mind` and `search_memory` only, at most eight calls / four invocation iterations, with bounded captured results. Untrusted input cannot register a tool or rewrite identity/playbook.

Each model call has a 90-second timeout, 4,096 output-token setting, strict local schema validation and no provider-side stored conversation. Private SQL traces retain context, native read results, responses/refusals, prompt version, latency and available token counts. Failed usage and deployment version/prices remain explicitly unknown until measured. No real-model score, verified token price or cost saving is inferred from scripted responses.

## Experience bundles and cloning

Runtime exports use manifest version 2 / `shadow-runtime-v1`, retaining the v1 Markdown/front-matter contracts. The v1 synthetic archive profile remains supported by its validator, but cannot activate a shadow clone. A single SQL checkpoint captures all retained records and metadata, with exact identity/playbook text, provenance, evidence, history and override tags. Checksums, inventory, chronology, compatibility and all references are validated; archives are never extracted as files.

The bounded runtime profile permits 10,000 ZIP entries including the manifest, 32 MiB archive/expanded total, 1 MiB per Markdown entry, 2 MiB manifest and a 100:1 expansion ratio. Exports use stored ZIP entries, so high repetition cannot trigger ratio failures. A complete export exceeding a bound fails; it never silently truncates retained experience. Monitor retained counts/bytes and qualify the full-day export within these limits before declaring M4 complete. Larger retention/export workloads require an explicit format/capacity extension; they are not certified for the demo allocation by these fixtures.

Select a **new, empty destination trader ID** and its private destination binding, then:

```sh
python -m pipmind.shadow clone --input .data/exports/checkpoint.pipmind --identity .data/identity.md
python -m pipmind.shadow status
```

Import validates the entire bundle before staging records and the synchronous retrieval index in one SQL transaction. Interruption rolls everything back. Existing destinations are refused, preserving active state. Expected identity must match exactly. Source working memory is archived as history, current working memory is cleared, a new lineage/parent checkpoint is recorded and the destination starts paused. Account snapshots, operational cursors/outcomes, permissions, stops/overrides and broker credentials are not imported. Exact historical event redelivery is recognized as retained experience without generating a new active decision; changed facts under its ID are refused. No source order or authority can become active.

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

The existing managed environment advertises more egress ranges than SQL's 256 server-rule limit. Keep the exact ranges: up to 254 server rules plus a temporary owner bootstrap IP; the remaining ranges use database-level rules (also limited to 256). Never substitute the all-Azure `0.0.0.0` rule. `scripts/bootstrap_shadow_sql.py --config <ignored JSON>` applies the database rules, schema/full-text, paused initial observation-only mind and contained identity grants through the owner's existing Azure CLI login. Configuration contains `sqlHostname`, `databaseName`, `identityPrincipalId`, `trader`, `bindingRef`, `databaseFirewallFile` and `feedConfigFile`; all real values stay under ignored `.azure/`. Remove temporary owner access after verification. Recheck actual backend egress coverage and alert on environment address changes; default networking provides no static-egress guarantee. [SQL firewall limits](https://learn.microsoft.com/en-us/azure/azure-sql/database/firewall-configure).

Runtime identity uses optional `PIPMIND_MANAGED_IDENTITY_CLIENT_ID` for both SQL and Foundry. Grants cover only required table operations; the app cannot create users, change firewall rules or perform schema DDL. The pinned TDS driver calls removed pyOpenSSL extension APIs, so `shadow.tls` supplies SAN hostname validation through MIT-licensed service-identity/cryptography, retaining verified certificate chains and encrypted transport. Negative hostname/wildcard tests and a real encrypted SQL connection verify this path. [Verifier API](https://service-identity.readthedocs.io/en/stable/api.html).

`infra/shadow-app.bicep` manages only the new internal-ingress backend: one replica, engine and gateway at 0.25 vCPU / 0.5 GiB each, user-assigned identity, health probes and container-specific secret recipe volumes. The manual **Publish and deploy demo shadow** workflow builds the two targets from a main commit, publishes existing public GHCR package tags, resolves immutable digests and deploys via the existing demo OIDC identity. Supply the ignored ARM parameter JSON as environment secret `SHADOW_APP_PARAMETERS`; `appSecrets` is a secure object with secret-name keys. Images contain no real configuration. The landing template and release workflow are independent.

Until genuine venue mappings and API access exist, `PIPMIND_GATEWAY_UNCONFIGURED=true` starts an authenticated sidecar with empty prices/snapshots and explicit `broker_reads_disabled` status. Every command is refused and enabling broker reads in this state is rejected; no invented broker identifiers or synthetic snapshots enter hosted data. FOREX.com API access requires its service team's enablement. This keeps broker qualification pending rather than claiming a working broker session. [Broker setup](https://www.forex.com/en-us/premium-trader-tools/api-trading/).

The owner selected the FXMacroData $50 monthly plan and completed checkout; authenticated EUR/USD macro history reads succeeded. Its issued key stays in ignored `.env` and built-in runtime secrets. Annual billing is not selected. A paid subscription and successful REST request alone do not certify SSE/webhook timing, complete provider coverage or a full M4 trading day.
