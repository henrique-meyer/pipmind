# Expected local evaluation preflight

PIP-42 adds an opt-in intent check to the isolated evaluation runner. It compares
the selected cases and local configuration before evaluation writes or model
calls. Existing `evaluate(..., manifest=...)` and `shadow evaluate` behavior stays
compatible: a descriptive manifest remains unverified and does not enable this
gate. Old reports do not acquire a certification when read by newer code.

Use `evaluate(..., expected_local_provenance=expected)` or the separate command:

```text
python -m pipmind.shadow.evaluation_preflight --dataset <dataset.jsonl> --cases T01,T02 --repetitions 3 --expected <private-expected.json> --output <new-private-report.json>
```

The separate command requires `--expected`, checks local intent before creating
the SQL connector, and uses the existing exclusive private report reservation and
atomic partial checkpoints. Exit status 2 and a fixed message indicate refusal;
private inputs and endpoints are not echoed. Configuration/model construction
does not make a model call. Actual evaluation still requires the existing SQL
configuration and explicit model-call opt-in. This feature does not activate a
policy, change a model budget, or authorize a hosted run.

## Preparing explicit intent

Generate the intent from the **selected ordered cases**, the same configured
model, and the intended repetition count. Review and save it privately before
execution. Generating an expectation from whatever happens to be current at
every run defeats change detection; generation itself is neither approval nor
verification. For example, in a private local preparation script:

```python
from pipmind.dataset import load_cases
from pipmind.shadow.bundle import write_private
from pipmind.shadow.common import encoded
from pipmind.shadow.evaluation_preflight import expected_local_provenance
from pipmind.shadow.evaluation_provenance import capture_provenance
from pipmind.shadow.evaluations import GRADER_VERSION
from pipmind.shadow.model import FoundryModel

cases = [case for case in load_cases("<dataset.jsonl>") if case["case_id"] in {"T01", "T02"}]
model = FoundryModel.environment()  # Configuration only, no inference.
observed = capture_provenance(cases, model, repetitions=3, grader_version=GRADER_VERSION)
expected = expected_local_provenance(
    observed, model,
    observed_memory_policy={"present": False, "version": None, "sha256": None},
)
write_private("<new-private-expected.json>", encoded(expected).encode())
```

`expected-local-evaluation-v1` requires every field produced by this function:
runner mode, ordered case IDs and single-case-list hashes, selected-list hash,
repetitions, effective local settings, and the explicit policy requirement.
Unexpected or missing fields refuse. Canonical JSON equality distinguishes
`false`, `0`, `null`, missing fields, and strings. The cohort, expected intent and
descriptive manifest are copied from strict built-in JSON before evaluation;
later caller/progress mutations cannot change the selected cases.

Local settings include the imported consumer prompt/context, repair, policy,
budget, codec, usage and grader version constants, with checks for inconsistent
consumer aliases. Exact `FoundryModel` settings include the existing hashed
endpoint/deployment binding, timeout, factory presence and credential-mode enum;
credentials and managed-identity IDs are excluded. Factory content and provider
behavior are not inspected. Exact `FixtureModel` settings include a canonical
scripted-response digest. Custom models/subclasses and unknown required facts
refuse; arbitrary introspection/serialization hooks are not invoked. Supported
model settings are checked again before each subsequent episode writes.

## Policy observation and refusal boundaries

Strict mode supports only `fixture-working-state` with **no installed memory
policy**. A request for a present, bounded or unknown policy refuses before
writes; this runner does not install the available bounded-policy capability.

After the local comparison, the runner allocates random evaluation names and
reads every proposed namespace under its existing source lock before the first
evaluation write. It requires both no mind and no policy row. This observes
absence **before creation**, not an active post-initialization policy. An existing
namespace, unexpected policy or failed read refuses without creating a mind or
calling a model. No SQL migration or schema change is involved.

After each `initialize()` commits a paused isolated mind, the runner reads its
actual policy before updating fixture state, unpausing, accepting inputs or
calling the model. Unexpected policy presence refuses without parsing it as
trusted policy data. The initialized paused mind is retained. A partial report
records `preflight_refusal.phase = post_initialization`, the fixed code and the
trader ID, with `failure_audit = report-only`. That path **has initialization
writes** and must not be described as a zero-write preflight refusal. Initialization
or unrelated SQL failures retain their existing failure behavior. This is not an
atomic namespace reservation against arbitrary external SQL writers; initialize's
existing lock and duplicate guard still apply.

Direct API callers can catch `EvaluationPreflightRefusal` (a `ShadowError`) and
read its independent `partial_report` copy even without a progress callback.
Exception text/repr contain only the fixed refusal code. A failing progress sink
cannot replace this exception or expose its arbitrary error text; the retained
report marks checkpoint persistence `unconfirmed`. `not_requested` means no sink
was supplied; `callback_returned` reports that a callback returned successfully,
without attesting filesystem durability. The callback receives a separate copy.

Successful strict reports add `local_preflight` with the expected-intent digest,
local match, precreation observation scope and namespace count. Existing runtime
provenance and per-episode policy observations remain separate. Reports retain
all prior outcome/failure/usage data and still require human calibration.

## What a match proves

A match means these locally observed inputs/settings matched explicit intent at
the checks described above. It is not a thread/process security boundary against
arbitrary mutation during a model call. It does not attest loaded bytecode,
deployed build, provider version, pricing, execution/billing, response quality,
active bounded policy, or production/shadow qualification. `BUILD_SHA`, provider
and price declarations stay unverified; on-disk artifact receipts remain observed
receipts and are deliberately outside the required comparison. Exact-match
intent must not be represented as trading or dispatch authority.

Dedicated offline tests use synthetic fixtures and in-memory store doubles;
required real SQL CI must separately verify durable zero-write refusal, paused
initialization retention and the successful report/runtime linkage. No live
model/provider or hosted validation is implied by those tests.
