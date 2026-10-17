# Offline replay and evaluation

M1 implements an offline event-processing core, injected clock/model/store interfaces, versioned contracts, a synthetic fixture week, 30 starter scenarios, deterministic graders and CI gates. The scripted model is not an AI trader, a trading strategy, a profitability claim or proof of production safety. The deployed landing page is unchanged; these commands create no cloud resources or broker sessions.

## Run from the repository root

Use Python 3.12 and `uv`:

```sh
uv sync --locked --extra gateway
uv run python -m pipmind.replay validate
uv run --extra gateway python -m pytest -q
uv run python -m pipmind.replay run --ci
```

Only dependency installation requires network access. CI runs tests and replay with `--network none`, a read-only container filesystem, no additional capabilities and no provider credentials. Offline CI containers run as the host runner UID so they can write ignored workspace output without restoring root capabilities; the public commit SHA is forwarded explicitly for provenance. Existing lint, Bicep, secret and real landing-container HTTP checks remain enabled.

With the existing Docker setup instead of a host `uv` installation, PowerShell can use the pinned development image from the Dockerfile:

```powershell
$replayImage = ((Get-Content Dockerfile -TotalCount 1) -split ' ')[1]
docker run --rm -v "${PWD}:/workspace" -w /workspace $replayImage uv sync --locked --extra gateway
docker run --rm --network none -v "${PWD}:/workspace" -w /workspace $replayImage .venv/bin/python -m pipmind.replay run --ci
```

This prepares a Linux virtual environment in the ignored `.venv` directory. Re-sync dependencies when switching between Docker and a host-native environment.

Generated output lives in ignored `evals/reports/latest/`: private per-case/repetition traces, fixture-week `decisions.jsonl`, full `report.json` and sanitized `summary.json`. A two-repetition run includes both week repetitions in the decisions file. Each case, repetition and variant receives fresh mind and simulated broker snapshot state. Full traces contain inputs, memory, tool results and reasoning; never upload them.

The [tracked baseline](../evals/baseline-v1.json) contains sanitized synthetic outcomes, counts, deferrals and version/hash metadata. Source/contracts/fixtures use LF through `.gitattributes` so content hashes agree across Windows and Linux. Reports record source commit/dirty state, implementation content hash, dataset/week/portability/gateway-fixture hashes, context/grader/model versions, starting playbook hashes, repetitions, fake call counts and measured latency. Token/cost counts are explicitly synthetic zeros. Real-provider metering belongs with the M4 adapter; M1 cannot measure it.

## Delivered coverage

| Area | Passing delivered cases | Deferred | Real-path gate |
| --- | ---: | ---: | --- |
| Triage | 6 | 0 | — |
| Decision/memory | 5 | 1 | M5 reconciliation |
| Safety/recovery | 2 | 8 | M2 mapping refusal delivered; remaining M5 boundary, SQL ownership and broker recovery |
| Self-review | 0 | 6 | M7 review implementation |
| Public/read-only | 1 | 1 | M8 publisher/redaction |
| **Total** | **14** | **16** | Deferred cases are not passing controls. |

The [starter JSONL](../evals/fixtures/starter-v1.jsonl) covers ordinary releases, analysis retention, breaking news, role posts, duplicate delivery, missing PMI actuals, memory continuity, conflicting reports, delayed revisions, retrieval and prompt injection. The unsupported-instrument case now uses the M2 dry gateway; later cases specify stale quotes/snapshots, excess size, daily loss, pause/kill, lost ownership, SQL outage, uncertain broker outcomes, reconciliation, review add/sharpen/retire/override/grounding and public redaction. X is read-only input with no mutation tool.

The [fixture week](../evals/fixtures/week-v1.jsonl) has 20 Release/News/Post/Price events across five days, producing 15 scripted decisions, five unsent proposals and zero dispatches. Non-waking items remain in the journal. Duplicate receipts do not repeat model calls. Revisions enter context only at availability time, and evidence must already exist. Invalid/future evidence, unavailable models and unregistered/malformed tools produce journaled refusals without a keyword fallback. Only mind reads and bounded memory search are registered. The simulated gateway has no transport and unconditionally refuses dispatch.

Expectations are independent of scripted replies and never enter the core/model. Positive controls require wakes, decisions and memory updates, so refusing everything fails. Tests mutate good traces to reject malformed schemas, dropped items, unknown/future evidence, forbidden dispatch, duplicate commands, absent refusal records, changed identity/playbook, invented positions, bad review versions/evidence, override learning, private prose/raw quotes and X writes. Fake safety/review/public traces test graders only; they do not activate later real-path cases.

## Candidate and baseline review

Candidate response files replace selected scripted model steps and contain data, not code or network configuration. Example `.data/candidate.json`:

```json
{
  "T01": {
    "t01-release": {
      "triage": { "wake": false, "summary": "Candidate chooses not to wake." }
    }
  }
}
```

```sh
uv run python -m pipmind.replay run --responses .data/candidate.json --output .data/candidate-report
```

This weak candidate exits nonzero because it misses a required wake and decision. `--split development` or `--split holdout` permits partial inspection; `--ci` requires all cases and at least two repetitions.

Development episodes precede the six chronological holdout episodes. Keep holdouts out of prompt/rule tuning, and retain state only inside a declared episode/week. Public synthetic fixtures are not a blind benchmark: scripted scores do not establish real-model quality. [Quality rubrics](../evals/rubrics/quality-v1.json) include two human-labeled calibration examples. Optional Foundry model/judge comparisons remain opt-in M4 work, with repetitions, human calibration, versioned prompts/models and real usage accounting. Judges cannot override hard checks.

Record an intentionally reviewed synthetic baseline with:

```sh
uv run python -m pipmind.replay record-baseline
git diff -- evals/baseline-v1.json
```

The command refuses failing, candidate or partial baselines. CI never rewrites it. Dataset/week/portability changes require explicit baseline review; missing cases, previously passing cases becoming failed/deferred, fixture-week regressions and removed portability fixtures fail the gate. Hard failures cannot be averaged away. Capability activation requires a real adapter, tests and reviewed registry changes; do not relabel cases to hide missing implementation.

## Portable mind contract checks

M1 validates synthetic ZIP `.pipmind` bundles in memory. It does not export a real SQL mind or import/activate state. Consistent SQL export, atomic import and clone semantics remain M4; same-account fenced recovery and reconciliation remain M5/M6. A synthetic round trip does not certify those paths.

The [manifest](../contracts/v1/bundle-manifest.schema.json) inventories every file with SHA-256 and byte count, complete retention/record coverage, checkpoint/provenance and compatibility metadata. Identity, playbook and working memory have fixed paths; journal, reviews, memories and playbook history use their declared directories. Empty retained collections have zero counts. Recovery additionally requires the [operational sidecar](../contracts/v1/recovery-state.schema.json), matching trader/lineage/checkpoint/watermark, command/deduplication/pending coverage and retained stops/overrides. Reusable lease authority is forbidden. Actual private account binding and fresh SQL ownership cannot be verified by this validator.

Markdown front matter is literal `---` plus LF, a JSON object matching [markdown-front-matter](../contracts/v1/markdown-front-matter.schema.json), LF plus `---` plus LF, then exact UTF-8 Markdown bytes. JSON is a bounded YAML-compatible subset; no YAML tags, executable parsing, URL fetching or narrative tool authorization is supported. IDs, chronology, provenance, evidence, rule versions/status, override tags and identity/playbook bytes survive the synthetic round trip.

The validator never extracts files. It rejects bad checksums, missing/unlisted inventory, duplicate names/IDs/JSON keys, incomplete coverage, broken/future evidence, unsupported versions, recovery mismatches, invalid ledgers, absolute/traversal/Windows paths, links and encrypted/unsupported entry types. Limits: 128 entries, 128 KiB/file, 2 MiB compressed input, 2 MiB total expansion and 100:1 per-entry expansion. Only stored/deflated entries are supported. The [fixture catalog](../evals/portability/cases.json) includes positive experience/recovery controls and these rejection families.

Real bundles/contents stay private, ignored, excluded from images/public snapshots and absent from CI artifacts. Checksums detect corruption, not sender identity; compression provides no confidentiality. Validation grants no account access, resume permission, live-trading authority or identical-future-decision guarantee.

## CI and later gates

CI runs unit/mutation/archive tests and full replay with networking disabled, compares the baseline, and renders per-area results and every deferral in the job summary. Only allowlisted `summary.json`, bounded to 64 KiB, is uploaded for seven days. Raw events, reasoning, mind, tool results and bundles are excluded.

M2 activates S01 against durable mapping refusal and adds the [15-check dry gateway session](gateway.md). M5/M7/M8 must activate the remaining respective cases through their actual implementations. SQL behavior needs the SQL Server adapter/integration checks. Continue through live-event shadow mode and broker demo orders regardless of M1 scores; live trading and new resources remain separate owner decisions.
