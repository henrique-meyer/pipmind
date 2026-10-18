# M3 information feeds

The `macro`, `news` and `social` workers normalize external inputs into `ReleaseEvent`, `NewsEvent` and `PostEvent` respectively. They run as separate supervised processes in the engine/feed container, with independent retries and restarts. They cannot invoke a model, submit broker orders or write to X. The deployed landing application is unchanged; no vendor account, subscription or Azure resource was touched by M3.

## Run and validate offline

```sh
uv sync --locked --all-extras
uv run --all-extras python -m pipmind.feeds replay --process-checks
uv run --all-extras python -m pytest -q
uv run --all-extras python -m pipmind.replay run --ci
docker build --target feeds -t pipmind-feeds:ci .
python scripts/feeds_smoke.py
```

PowerShell without a host Python/uv installation can use the pinned development image:

```powershell
$replayImage = ((Get-Content Dockerfile -TotalCount 1) -split ' ')[1]
docker run --rm -v "${PWD}:/workspace" -w /workspace $replayImage uv sync --locked --all-extras
docker run --rm --network none -v "${PWD}:/workspace" -w /workspace $replayImage .venv/bin/python -m pipmind.feeds replay --process-checks
```

The [original synthetic fixtures](../evals/feeds/scenario-v1.json) drive real adapter/validation/storage paths. Fourteen deterministic feed gates cover cache/restart behavior, SSE/webhook deduplication, signatures/timestamps, consensus, unknown publication time, news/relay normalization, holder changes, failed lookups, X pagination and producer/event-type separation. Independent process checks kill each worker and verify both peers continue producing events and the victim restarts. The real container repeats those three kill/restart flows with no network, a non-root user, read-only root filesystem and dropped capabilities. Workers share no multiprocessing semaphore that a killed child could lock permanently.

Replay CI includes the feed gates and fixture digest in the reviewed baseline. Removed/failed checks or a nonzero dispatch count fail the gate. Only the bounded sanitized summary is uploaded for seven days. Raw inboxes, account/role state, events, cursors, webhook bodies and provider responses stay private. The 30 original starter cases remain at 14 passing and 16 later-path deferrals; M3 does not certify SQL ownership, risk controls, reviews, publication or actual trading.

## Private configuration and service

[.env.example](../.env.example) lists empty environment variables. The process does not automatically load a file or overwrite existing environment values. Copy the [entirely synthetic JSON recipe](../config/feeds.example.json) into an ignored local file and replace its `.invalid` URLs, source mappings and roles from verified provider/official specifications. Point `PIPMIND_FEED_CONFIG` at that file. The older [social role YAML example](../config/social-roles.example.yaml) describes the intended list and lookup workflow; its prose `parse` instructions are never executable configuration. Runtime recipes use exact text delimiters and optional JSON key paths, never `eval`, generated code or regex supplied by a response.

Set a private random `PIPMIND_FEED_TOKEN` of at least 32 characters, a writable `PIPMIND_FEED_STORE` path, and `PIPMIND_FEED_MODE=local-read`. Vendor GETs require the separate explicit `PIPMIND_FEED_READS=true` opt-in. Without it, the service can durably accept authenticated/signed local messages and drain them without polling vendors. `fixture` mode produces clearly synthetic pulses and uses a separate store binding; it is only for offline lifecycle tests and cannot reuse a `local-read` database.

```sh
uv run --all-extras python -m pipmind.feeds serve
```

The default listener is `127.0.0.1:8082`; the separate Docker target binds its private container interface. The default Docker target still serves the landing app, which has none of these feed routes. Mount a private writable state/config directory for UID/GID 10001, and keep the feed port internal. Production separation requires TLS/internal access controls; the existing Azure landing ingress has not been changed to expose this service.

| Route | Access and behavior |
| --- | --- |
| `GET /healthz` | Short process liveness response identifying `pipmind-feeds`; not proof of fresh vendor data. |
| `GET /status` | Bearer auth; private worker PID/restart/liveness, heartbeat/read-enable/error state, per-feed event counts, bounded alert codes and quarantine count. |
| `GET /events?after=0&limit=100` | Bearer auth; ordered durable event envelopes with sequence numbers, maximum 500/page. This is a private handoff, not a public market-data API. |
| `POST /webhooks/fxmacrodata` | HMAC verification instead of bearer auth; bounded exact body, signed timestamp/event ID and durable inbox acceptance before 2xx. |
| `POST /relays/headlines` | Bearer auth; validates and durably accepts `HeadlineRelay` without broker credentials or logging in to CIAPI. The M2 gateway does not yet collect Reuters headlines; this is the receiving path for its later read integration. |

All private responses use `Cache-Control: no-store`; API docs and access logging are disabled. Errors return fixed codes rather than raw provider errors or private URLs. The webhook checks `X-FXMD-Timestamp`, `X-FXMD-Event-ID` and `X-FXMD-Signature: v1=<hex>` over `timestamp.event_id.raw_body`, allows at most five minutes of clock skew, and requires the body IDs to match. Configure the subscription's signing secret privately in `PIPMIND_FXMD_SIGNING_SECRET`. No subscription is created or modified by this application. Invalid signatures/stale requests are 403, malformed data is 422, changed facts under one delivery ID are 409, and unavailable durable storage is 503. Valid duplicate event IDs return 2xx with `duplicate: true`, including after restart.

## Macro data and chronology

ForexFactory is a persistent, shared hourly cache. A refresh attempt is reserved transactionally before HTTP; concurrent workers/restarts cannot refetch within the hour. Failures and 429s retain the previous cache and alert; they do not retry in a loop. ETags support 304 responses. The cache stores only schedule/impact/forecast/previous; a calendar row never emits an invented actual.

FXMacroData uses a GET-only `payload=full` SSE connection, `X-API-Key` header authentication and a persisted `Last-Event-ID`. Complete frames enter the private inbox atomically with the cursor. Heartbeats, partial frames and uncommitted records never advance it. SSE and signed webhook copies use the same logical event identity; one notification has one normalized effect. Facts that change require a distinct vendor event ID, with `update_of_event_id` normalized to `revision_of`. Receipt/availability remain separate from publication and schedule.

The additive v1 contract now permits unknown `scheduled_at`/`released_at` as null and carries optional `publication_time_status`/`publication_time_precision`. A notification timestamp is never substituted for publication. Confirmed, unverified, assumed-historical and unknown evidence stay distinct. Explicit indicator recipes select `val`, a headline `change`, or a percent variant, along with unit/scale and previous-value field. ForexFactory consensus wins only when its configured currency/title/scheduled instant matches; a captured vendor forecast is a fallback only if explicitly pre-release and configured on the same value field/scale. This prevents an employment level from being compared with a jobs-change consensus.

Incomplete/unmapped rows, unsupported batch notifications (`records_written != 1`), invalid chronology and schema failures are retained in quarantine and alerted rather than silently turned into precise events. M4 adds separately opt-in canonical REST page capture/revision expansion; live long-outage completeness remains a qualification gate. Do not claim an unknown publication instant, complete vintage history or real latency from fixtures.

## News and social policy

RSS and Atom parse with entity expansion/external references disabled, bounded bodies/entry counts, explicit timezone-aware publication dates and safe plain text. Source recipes label breaking/analysis; there is no keyword trading strategy. Analysis remains available to the later triage/review paths. Feed-provided summaries are retained only with explicit permission and not for paywalled sources. Linked articles are never fetched. Each RSS source backs off independently; a dead source does not prevent the other sources or a pending broker relay from being processed. Updates to an entry's content get a distinct identity rather than overwriting prior evidence.

X has only two allowed GET endpoints: selected user lookups and selected-user timelines. It has no post/reply/repost/like/follow/message or other mutation method. It requests replies/reposts exclusion and retains all selected original posts with per-account pagination state. Each page's events and continuation state commit together; `since_id` advances only at the end, so a failed older page resumes safely after restart. Partial-error responses are refused. Rate-limit backoff is persisted. P1 polls every two minutes in the configured UTC active-hours approximation and every ten minutes otherwise; P2 every fifteen minutes. P3, RSS-only and dormant accounts do not receive ordinary timeline polls. API retention/entitlement limits still require live coverage validation; no local cursor can recover posts the provider no longer serves.

Initial identity resolution and a Sunday 16:00–20:00 UTC closed-market maintenance window verify accounts and holder recipes. Primary/fallback parse failures alert and leave saved holder/account state intact. A confirmed new holder retires the old personal account while retaining institutions; official profile links may nominate the new personal handle, which must match the holder name, role description and suitable verified type, with no parody/protected flag. An X lookup failure after a confirmed holder change disables the former personal selection rather than continuing to read it. Accounts' dormancy is checked from successful timeline evidence and reevaluated weekly; errors cannot silently mark an account dormant or erase its holder.

Event-window accounts are read only five minutes before through thirty minutes after cached high-impact calendar times. Additional intervention/headline windows require explicit evidence/configuration work; no headline keyword heuristic is implemented. A configured free public RSS/Atom mirror can be the primary social source. An X `backup` account is eligible only after that source fails or exceeds its configured quiet interval; no primary mirror endpoint is guessed. Feed-provided mirror summaries require explicit `primary_allow_summary` permission. Provider reads have no local spending cutoff; choosing/authorizing a real list and confirming charges, terms, API fields, role recipes and mirror reliability remain owner/live-validation work.

## Storage and deployment boundary

The SQLite store remains isolated local development/replay storage. M4 adds `PIPMIND_FEED_BACKEND=sql`, using the same encrypted managed-identity SQL connector as the mind, atomic inbox/outbox/cursors, a stable delivery-source UUID and durable shadow consumer checkpoints. No hosted SQLite, broker command ledger or lease is implied. Both stores bind the recipe/profile; keep a private backup and perform a deliberate migration before changing that profile. Events have no trading authority. The [shadow guide](shadow.md) covers integrated workers, private routes and pending hosted validation.

### Canonical backfill and reprocessing

With both `PIPMIND_FEED_READS=true` and `PIPMIND_CANONICAL_READS=true`, plus an authorized API key and explicit `PIPMIND_MACRO_BACKFILL_START`, the macro worker captures bounded GET `/v1/announcements/{currency}/{indicator}` pages with `revisions=all`. Date bounds are observation periods, not publication windows. Each hourly scan has a durable pagination cursor, validates series identity/pagination, stores the raw page and expands retained vintages with distinct IDs. Normalized events and page cursor commit together. Unknown publication times remain null. Page size is 100, expanded events at most 500/page and maximum offset 10,000; bound failures alert rather than silently skip records. Data-quality flags and capture status remain visible in `/events` and shadow context with `complete_history_proven:false`.

Stop the relevant source worker while diagnosing a quarantined record. `python -m pipmind.feeds reprocess --key <private-key>` requeues an ordinary notification, or re-normalizes a retained `rest-page:` capture without a vendor request or cursor rewind. It preserves receipt/provenance and never edits the raw capture; invalid pages stay quarantined. Correcting code can reprocess the same recipe profile; changing mappings/units requires an explicit profile migration. Feed-source replacement also requires deliberate consumer identity configuration.

Provider pagination, retained vintages, SSE retention, mutable windows and unknown/source-gap flags prevent a claim of complete publisher history. Signed hosted ingress, actual API fields/permissions, outage overlap and end-to-end coverage still require approved live checks. See the official [canonical endpoint](https://fxmacrodata.com/documentation/announcement-endpoint) and [response fields](https://fxmacrodata.com/documentation/response-fields).

Official references checked for M3: [FXMacroData stream contract](https://fxmacrodata.com/documentation/sse-streams), [webhook signatures/delivery](https://fxmacrodata.com/documentation/webhooks), [publication evidence and normalized values](https://fxmacrodata.com/documentation/response-fields), [provider contract changes](https://fxmacrodata.com/documentation/changelog), [ForexFactory calendar exports](https://www.forexfactory.com/calendar), [X selected-user posts](https://docs.x.com/x-api/users/get-posts), and [X user lookup](https://docs.x.com/x-api/users/lookup/introduction). These document contracts; M3 made no real vendor, model or broker calls and incurred no provider usage.
