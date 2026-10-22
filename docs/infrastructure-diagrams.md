# Infrastructure diagrams

These diagrams explain PipMind using **generic service labels**. They contain no
instance resource names, hostnames, account identifiers or secret values. They
describe the recorded deployment and repository design as of **2026-10-22**;
they are not a live health check. Follow the arrows for data or control flow.

Read [workflow diagrams](workflow-diagrams.md) for what happens over time.

## 1. Hosted shadow layout

The public landing and private backend are separate Container Apps. The backend
groups the core and three feed workers in one container, with the broker gateway
in another. Logical services do not each require their own Azure resource.

```mermaid
flowchart TB
  visitor[Public visitor]
  owner[Authorized owner operations]
  providers[Macro, calendar, news and social providers<br/>Read APIs, RSS and SSE]
  broker[FOREX.com demo venue<br/>REST and Lightstreamer]

  subgraph azure[Dedicated Azure workload scope]
    subgraph env[Container Apps environment]
      landing[Public landing app<br/>HTTPS page and assets]
      subgraph backend[Private shadow app]
        subgraph engine[Engine container]
          workers[Macro / news / social workers]
          core[Shadow core<br/>Agent Framework loop]
        end
        gateway[Gateway container<br/>0.25 vCPU / 0.5 GiB<br/>Demo reads only]
      end
    end
    sql[(Azure SQL Standard S0<br/>Mind, events and audit)]
    foundry[Microsoft Foundry project<br/>Triage and decision models]
    logs[Essential monitoring]
  end
  analytics[Google Analytics]

  visitor -->|HTTPS| landing
  visitor -->|Browser analytics events| analytics
  owner -->|Private authenticated operations| core
  providers -->|Provider data| workers
  workers <-->|Inbox, outbox and cursors| sql
  workers -->|Durable event handoff| core
  core <-->|TLS - state and evidence| sql
  core <-->|Model calls| foundry
  broker -->|Prices, snapshots and news| gateway
  gateway -->|Authenticated local handoff| core
  backend -->|Sanitized diagnostics| logs
  landing -->|Diagnostics| logs
```

Arrows show runtime data/control connections. Request/response links may carry traffic in
both directions even when an arrow emphasizes the delivered data.

| Connection or component | Detail behind the overview |
| --- | --- |
| Provider reads | Macro actuals use FXMacroData REST/SSE; ForexFactory supplies calendar/consensus; RSS/Atom supplies news; X supplies selected read-only posts/lookups. |
| Feed workers | Independent supervised processes inside the engine. SQL inbox/outbox and cursors make handoff durable; broker headlines can be relayed to the news worker. |
| Private API | Authenticated status/input and feed handoff endpoints belong to the engine. Owner pause/export/clone operations use the private CLI; there is no public owner UI. |
| Managed identity | Shared backend identity authenticates SQL and project-scoped Foundry access. The two containers do not have independent identities. |
| Secrets | Built-in app secrets supply provider/internal API configuration to consumers; broker credentials belong only to the gateway configuration. |
| Backups | SQL managed backups are separate from private experience-bundle exports and future Blob archives. |
| Landing scale | One container, zero to one replicas. Backend scale is exactly one always-on replica. |
| Backend capacity | Engine and gateway each use 0.25 vCPU / 0.5 GiB. Default managed networking; no custom VNet. |

The landing has no connection to the private mind, model or broker. Its analytics
runs in the visitor's browser. The shadow backend records decisions and unsent
proposals; it has no order transport. Broker reads require the explicitly enabled
demo read-only profile and private configuration. A configured adapter does not
prove current provider health or complete source coverage.

The backend has internal ingress; it is not an owner-facing public web app.
Co-located containers communicate over internal/loopback endpoints with distinct
API tokens. They share identity, network and revision lifecycle. SQL uses TLS,
managed identity and explicit egress firewall rules. Default networking here
does not mean private endpoints or a custom VNet. Public signed-webhook routing
and a deployed owner browser interface remain separate qualification work.

## 2. Intended complete system

This is the target data flow, not a statement that these capabilities are all
deployed. Dashed connections identify paths that still require implementation,
integration or operational qualification.

```mermaid
flowchart TB
  sources[Macro, news and social providers]
  venue[Broker demo venue]
  feeds[Macro / news / social services]
  gateway[Broker gateway]
  core[Core trader<br/>Wake, triage and decision]
  state[(SQL transactional state<br/>Mind, events, account facts and audit)]
  models[Foundry models]
  safety[Ownership, controls, risk admission<br/>Reconciliation and command ledger]
  learning[Autonomous review<br/>Auto Learn control]
  owner[Private owner interface<br/>Questions, suggestions and controls]
  publisher[Public redaction and snapshot publisher]
  publicBlob[(Public sanitized Blob snapshots)]
  archive[(Private archives and exports)]
  frontend[Static Web Apps frontend]
  visitor[Public visitor]

  sources --> feeds -->|Versioned events| core
  venue -->|Prices, snapshots and news| gateway
  gateway -->|Prices and account facts| core
  core <--> state
  core <--> models
  core -.->|Proposed action| safety
  safety -.->|Accepted and freshly checked command| gateway
  gateway -.->|Demo order transport| venue
  venue -.->|Order updates and fills| gateway
  gateway -.->|Confirmed outcomes for reconciliation| safety
  safety -.->|Confirmed account state| state
  state -.->|Attributed evidence| learning
  learning -.->|Versioned learned policy| state
  owner -.->|Authorized operations| core
  owner -.->|Deterministic pause and kill controls| safety
  state -.->|Allowed publication fields only| publisher
  publisher -.-> publicBlob
  state -.->|Protected export workflow| archive
  visitor -.->|HTTPS| frontend
  publicBlob -.->|Read-only JSON| frontend
```

The planned public record reads sanitized snapshots; visitors cannot invoke a
model or broker. Raw prices, vendor text, account snapshots and private audit
stay private. Public Blob access and private archive access are separate security
boundaries; these labels do not mandate two storage accounts. Blob publishing
uses managed identity with shared-key access disabled when deployed.

The target engine/gateway grouping fits the demo profile; production can split
services and identities through separately approved infrastructure. There is no
separate message broker, Redis cache, hosted-agent runtime or vector database in
this baseline: SQL provides durable delivery, state and search.

## 3. Source, checks and delivery

```mermaid
flowchart LR
  change[Story and focused branch] --> pr[Pull request]
  pr --> ci[GitHub Actions checks<br/>Offline tests and replay<br/>Disposable SQL integration<br/>Bicep compile, Docker builds and secret scan]
  ci -->|Required checks pass| merge[Merge to main]
  merge --> classify{Authorized release path?}
  classify -->|Documentation only| done[No application deployment]
  classify -->|Landing changes| image[Build and publish immutable GHCR image]
  image --> deploy[OIDC login and Bicep deployment]
  classify -->|Explicit approved backend release| backend[Backend image and release workflow]
  backend --> deploy
  deploy --> verify[Verify pinned images, ready revision<br/>Health, expected build and actual flow]
```

Deployment identity/configuration lives in protected GitHub configuration and
private inputs. Images contain application code, not local credentials, runtime
history or exports. A merged safety module is not automatically activated in the
hosted runtime, and passing CI is not proof of a successful release.

## Capability status

| Area | What these diagrams mean |
| --- | --- |
| Public landing | Previously deployed HTTPS app and browser analytics; separate from the future trading-record frontend. |
| Shadow engine, SQL and Foundry | Hosted foundation verified in recorded releases; complete live-day quality, coverage, cost and capacity qualification remains open. |
| Broker gateway | Read-only demo profile exists; no venue order transport. |
| M5 safety and recovery | Newer isolated dry SQL implementations and CI proofs exist, including command obligations and restricted complete checkpoints. They are not evidence of hosted activation. |
| Recovery on hosted S0 | Explicit Azure SQL identity and catalog candidates exist; actual permissions, trusted module deployment and independent authority/barriers require PIP-47 qualification. |
| Robot library and owner UI | Separate workstream; do not infer availability from design or an open PR. |
| Demo execution, autonomous review and public record | Later rollout stages; live-account trading requires its own owner decision. |

Sources: [architecture and deployment profiles](architecture.md#deployment),
[shadow runtime](shadow.md), [gateway read-only profile](gateway.md#hosted-read-only-profile-and-broker-evidence-pip-14),
[feed runtime](feeds.md), [dry safety integration](safety-integration.md),
[recovery](safety-recovery.md), [Azure SQL identity](safety-azure-identity.md),
[restricted checkpoints](safety-restricted-checkpoints.md), and
[roadmap](roadmap.md). Deployment structure is defined in
[the backend template](../infra/shadow-app.bicep) and
[the foundation template](../infra/shadow-foundation.bicep).
