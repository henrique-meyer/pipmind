# Robot library and owner controls

PIP-17 implements the private **shadow** robot library, compact saves, favorites,
confirmed Load and the durable Auto Learn setting. SQL remains authoritative.
This source change does not deploy the interface or change the hosted robot.
Live-account handover/reconciliation remains M5/M6; nightly autonomous review and
its evidence grading remain M7. There is no order transport or conversational
owner-chat editor in this interface.

## Owner workspace

After the explicit SQL migration, use the shadow service's `/owner` page over
the existing private connection (loopback for local development). Unlock with
`PIPMIND_SHADOW_TOKEN`. Only an empty static shell is unauthenticated; every
mind/library read and mutation requires the bearer token. The browser keeps it
in memory until Lock/reload, never in a URL, cookie or local storage. Do not
expose this backend as a public owner login service. Preserve private ingress,
TLS, token handling and authorization when deploying it separately.

- **Save a copy** captures the committed active mind or duplicates an existing
  save. Optional names/notes belong to library metadata. Saved content never
  changes as the active robot evolves.
- **Add blank slate** adds the checked-in `blank-v2` template inactive. Imported
  `.pipmind` robots also remain inactive. Multiple copies can coexist.
- **Favorite** protects any save from rolling cleanup. The favorites filter
  narrows the library view. Manual copies, blank starters and imports are retained.
- **Preview Load** shows all four document diffs, target hash and effects.
  **Confirm Load** checks the same base revision/hash, learning generation,
  target and ten-minute expiry under the SQL lock. A changed base requires a
  refreshed preview. Repeated confirmation cannot apply twice.
- Load atomically saves an undo copy and creates a new active version/generation.
  The new working copy is independent of its source. Shadow reasoning stays
  paused; **Resume shadow** is a separate explicit action.
- **Auto Learn** is independent of pause, remains set across restart/load, and
  cannot be enabled by imported robot text. The initial workspace default is On.
  On permits future autonomous writers; no nightly review scheduler is introduced.

Load replaces identity/playbook/guides/lessons and clears previous market views
and waiting conditions. Current account snapshots, position obligations, source
events, feed cursors, outcome history and account/risk runtime values remain.
Old working views are retained in the load audit. Position theses are dated
obligations attributed to the former decision owner, not inherited strategy.
Queued reconsiderations are superseded. Old model results fail their revision
check. Existing orders cannot be changed or replayed by this shadow application.

## Compact format

The new manifest version **3**, profile `robot-save`, compatibility
`pipmind-robot-v1`, contains exactly five files:

```text
manifest.json
identity.md
playbook.md
guides.md
lessons.md
```

The manifest records an export UUID/time, source save/version where known,
per-file byte lengths/checksums and a canonical content hash. UTF-8 text is
preserved exactly, including line endings. Each document is at most 32 KiB;
canonical content is at most 128 KiB; the archive is at most 512 KiB. Bounds
refuse the complete operation rather than truncate text. Model input guards
remain smaller and may refuse an oversized mind; a valid save is not a promise
that every model can use it within the configured context budget.

Validators reject unexpected/duplicate/missing names, unsafe entry types,
encryption, unsupported compression/compatibility, invalid schema/UTF-8 and
hash/size mismatches. Nothing is extracted or executed. Robot text grants no
tools, account authority, owner settings or network access. Load requires the
owner's explicit review of the exact text. Metadata claims do not authenticate
an external author. Account history, positions, transcripts, secrets and broker
state are not package fields; review your own Markdown before sharing it.

Full **experience** export remains manifest v2 and retains all five-part records.
An optional validated `robot_state` extension preserves guides/lessons and the
active retrieval boundary when these controls have been used. Clone rebuilds
that boundary while keeping source records exact and starting paused with fresh
destination controls/binding. It does not clone library entries, favorites or
owner Auto Learn/retention settings. Legacy v1/v2 bundles keep their existing
semantics; the compact archive cannot be passed to the experience importer.
Original-trader operational recovery remains M5.

## Learning and retention boundary

`RobotLibrary.learning_ticket()` checks On before a future mutation-capable
review call. `commit_learning()` checks the same generation/content/version
under the mind lock, protects identity and rejects absent/future/override journal
evidence. It atomically saves the exact prior mind, applies the new policy and
records attribution. Snapshot/write failure rolls back both. This is a storage
gate for future M7 writers, not implementation or certification of repeated
evidence, rule retirement or learning quality. No mutation endpoint is registered
as a model tool. Every future automatic policy writer must use this boundary.

Off advances the generation and acknowledges the frozen content hash. An old
ticket stays invalid even after On is restored. Decisions, factual working state,
source capture and journals may continue; none promotes content into the four
durable robot documents. There is no paid review call while Off in this delivery.

After Load, older journal/review/generic-memory/playbook-history and working
transition text cannot enter active model retrieval, even by exact ID. Original
normalized source envelopes remain available as dated facts, and subsequent
generation observations are searchable. Saved guides/lessons are supplied exactly
in both model phases. The retained audit is separate from the active policy and
is never silently used to restore discarded lessons.

Rolling cleanup runs daily in the service and is available manually. Defaults
are 30 days and the newest 30 unprotected automatic/undo saves. Favorite,
manual/imported/blank, pending nonexpired load targets, current source and latest
undo saves are protected. Cleanup rechecks under the same lock as favorite/load;
retained content is self-contained. Expired bodies become metadata/hash
tombstones. Account history and policy/load audit are never deleted by cleanup.
Audit retention is separate and can continue growing. Saving/learning fails on
storage exhaustion rather than deleting protected content.

## CLI and API

Run the explicit migration with the configured schema-administration identity
before the new service. It adds four tables without changing existing mind rows.
The existing runtime identity needs the corresponding table read/write grants.
No automatic cloud migration/deployment is performed by this feature.

```sh
python -m pipmind.shadow migrate
python -m pipmind.shadow robot list
python -m pipmind.shadow robot save --name "Before experiment" --note "Current baseline"
python -m pipmind.shadow robot blank
python -m pipmind.shadow robot favorite --save <save-id> --value on
python -m pipmind.shadow robot export --save <save-id> --output .data/exports/robot.pipmind
python -m pipmind.shadow robot import --input .data/exports/robot.pipmind
python -m pipmind.shadow robot preview --save <save-id> --output .data/load-preview.json
# Read the private preview before confirming its target hash:
python -m pipmind.shadow robot load --preview .data/load-preview.json --confirm-sha256 <target-hash>
python -m pipmind.shadow robot auto-learn --value off
python -m pipmind.shadow robot retention --days 30 --count 30
python -m pipmind.shadow robot cleanup
```

Outputs contain private mind content. Keep them in `.data/` or another private
destination; files use exclusive creation, so existing exports are not overwritten.
`save --source <save-id>` makes an independent retained copy.

The bearer-authenticated JSON API uses GET `/robots`, POST `/robots/save`,
`/robots/blank`, `/robots/import` (raw archive), `/robots/load`,
`/robots/auto-learn`, `/robots/retention`, `/robots/cleanup`, `/robots/pause`, and
POST `/robots/<id>/favorite`, `/robots/<id>/preview`; GET
`/robots/<id>/export` downloads a robot-only archive. Responses contain action
feedback; no replayable notice/query-string or cookie login is used.

Actual SQL integration tests in `tests/test_robot_sql.py` run in the mandatory
`scripts/shadow_smoke.py` gate alongside existing shadow tests. They cover
transaction failure, concurrency, restore/retrieval isolation, owner workflow,
experience compatibility, control persistence and account separation. Archive
rejection tests run offline. Synthetic results do not qualify M4 live coverage,
M5 broker fencing, M6 orders or M7 review quality.
