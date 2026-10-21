"""Strict intent checks with synthetic models and in-memory SQL doubles only."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy

import pytest

from pipmind.contracts import event_id, loads
from pipmind.dataset import load_cases
from pipmind.shadow import evaluation_preflight as preflight
from pipmind.shadow import evaluations
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.evaluation_provenance import capture_provenance
from pipmind.shadow.model import FixtureModel, FoundryModel


def inputs():
    selected = [case for case in load_cases() if case["case_id"] == "T01"]
    return selected, FixtureModel(selected[0]["model_responses"])


def facts(cases, model):
    return capture_provenance(
        cases, model, repetitions=2, grader_version=evaluations.GRADER_VERSION
    )


def intent(cases, model):
    return preflight.expected_local_provenance(
        facts(cases, model), model, observed_memory_policy=deepcopy(preflight.ABSENT_POLICY)
    )


class FakeSQL:
    """Only the runner's exact statements; never opens a network connection."""

    def __init__(self, *, pre_policy=None, initialized_policy=None, existing=False):
        self.states = {}
        self.reads = self.writes = self.calls = self.unpauses = 0
        self.pre_policy = pre_policy
        self.initialized_policy = initialized_policy
        self.existing = existing

    def store(self, connect, trader):
        assert connect is self
        sql = self
        state = self.states.setdefault(trader, {"policy": self.pre_policy, "initialized": False})

        class Store:
            def __init__(self):
                self.trader = trader

            @contextmanager
            def transaction(self):
                yield Cursor()

            def initialize(self, identity, playbook, moment, *, binding, records):
                sql.writes += 1
                state.update(
                    initialized=True,
                    paused=True,
                    policy=sql.initialized_policy,
                    working={"views": [], "position_theses": [], "waiting_for": []},
                    records=[
                        ({"part": "identity"}, identity),
                        ({"part": "playbook"}, playbook["text"]),
                        *records,
                    ],
                    inputs={},
                    outcomes={},
                    pending=[],
                )

            def set_paused(self, paused):
                sql.writes += 1
                sql.unpauses += not paused
                state["paused"] = paused

            def accept(self, source, events, moment):
                for envelope in events:
                    event = envelope["event"]
                    state["inputs"][event_id(event)] = event
                    state["pending"].append(event)

            def outcome(self, key):
                return state["outcomes"].get(key)

            def retained(self):
                return self.head(), state["records"]

            def head(self):
                return {"paused": state["paused"], "working": state["working"]}

        class Cursor:
            def execute(self, query, params):
                self.rows = []
                if query.startswith("SELECT"):
                    sql.reads += 1
                    if query.startswith("SELECT 1 FROM pm_minds"):
                        self.rows = [(1,)] if sql.existing or state["initialized"] else []
                    elif query.startswith("SELECT v FROM pm_runtime"):
                        self.rows = (
                            [(encoded(state["policy"]),)] if state["policy"] is not None else []
                        )
                    elif query.startswith("SELECT id,envelope"):
                        self.rows = [
                            (key, encoded(value)) for key, value in state["inputs"].items()
                        ]
                    else:
                        raise AssertionError(query)
                else:
                    sql.writes += 1
                    if query.startswith("UPDATE pm_minds SET working"):
                        state["working"] = loads(params[0])
                    elif query.startswith("INSERT pm_runtime"):
                        state[params[1]] = loads(params[2])
                    else:
                        assert query.startswith("UPDATE pm_records SET body")

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return self.rows

        return Store()

    def engine(self, store, model, *, clock, observer):
        sql = self
        state = self.states[store.trader]

        class Engine:
            async def step(self):
                if not state["pending"]:
                    return False
                event = state["pending"].pop(0)
                triage, usage = await model.triage(event, {}, store, clock())
                sql.calls += 1
                state["outcomes"][event_id(event)] = {
                    "event_id": event_id(event),
                    "status": "observed",
                    "dispatches": 0,
                    "triage": triage,
                    "usage": [usage],
                }
                return True

        return Engine()


def run(monkeypatch, sql, cases, model, expected, **kwargs):
    monkeypatch.setattr(evaluations, "SqlMind", sql.store)
    monkeypatch.setattr(evaluations, "ShadowEngine", sql.engine)
    return asyncio.run(
        evaluations.evaluate(
            sql, cases, model, repetitions=2, expected_local_provenance=expected, **kwargs
        )
    )


def test_exact_match_proceeds_with_precreation_reads_then_postinit_policy_and_paused_results(
    monkeypatch,
):
    cases, model = inputs()
    expected = intent(cases, model)
    before = deepcopy((cases, expected))
    sql = FakeSQL()
    report = run(monkeypatch, sql, cases, model, expected)
    assert sql.calls == 2 * len(cases[0]["events"]) and sql.unpauses == 2
    assert report["summary"]["hard_check_failures"] == 0
    assert report["local_preflight"] == {
        "version": preflight.VERSION,
        "expected_sha256": digest(encoded(expected)),
        "local_facts_matched": True,
        "policy_observed": True,
        "policy_observation": "absent_namespace_before_creation",
        "namespaces_checked": 2,
        "provider_attested": False,
        "loaded_code_attested": False,
    }
    assert all(state["paused"] for state in sql.states.values())
    assert (cases, expected) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e.pop("cases"),
        lambda e: e.update(repetitions=True),
        lambda e: e.update(selected_cases_sha256="0" * 64),
        lambda e: e["cases"][0].update(sha256="0" * 64),
        lambda e: e["effective_local_settings"]["capabilities"].update(normal_prompt_version="old"),
        lambda e: e["effective_local_settings"]["capabilities"]["working_budget"].update(
            max_bytes=3200
        ),
        lambda e: e["effective_local_settings"].update(fixture_responses_sha256=None),
        lambda e: e.update(
            observed_memory_policy={
                "present": True,
                "version": "bounded-working-v1",
                "sha256": "0" * 64,
            }
        ),
        lambda e: e.update(observed_memory_policy=None),
        lambda e: e.update(declared_build={"status": "verified"}),
    ],
)
def test_missing_unknown_mismatched_required_intent_refuses_before_sql_or_calls(
    monkeypatch, change
):
    cases, model = inputs()
    expected = intent(cases, model)
    change(expected)
    sql = FakeSQL()
    with pytest.raises(ShadowError, match="evaluation_preflight_"):
        run(monkeypatch, sql, cases, model, expected)
    assert sql.states == {} and sql.reads == sql.writes == sql.calls == 0


@pytest.mark.parametrize(
    "sql", [FakeSQL(existing=True), FakeSQL(pre_policy={"version": "bounded-working-v1"})]
)
def test_precreation_existing_namespace_or_policy_refuses_readonly(monkeypatch, sql):
    cases, model = inputs()
    with pytest.raises(ShadowError, match="evaluation_preflight_"):
        run(monkeypatch, sql, cases, model, intent(cases, model))
    assert sql.reads > 0 and sql.writes == sql.calls == sql.unpauses == 0


def test_postinitialization_policy_change_retains_paused_mind_and_distinct_partial_failure(
    monkeypatch,
):
    cases, model = inputs()
    sql = FakeSQL(initialized_policy={"version": "bounded-working-v1"})
    partial = []
    with pytest.raises(ShadowError, match="evaluation_preflight_policy_mismatch"):
        run(monkeypatch, sql, cases, model, intent(cases, model), progress=partial.append)
    assert sql.writes == 1 and sql.calls == sql.unpauses == 0
    assert sum(state["initialized"] for state in sql.states.values()) == 1
    assert next(state for state in sql.states.values() if state["initialized"])["paused"]
    refusal = partial[-1]["preflight_refusal"]
    assert refusal["phase"] == "post_initialization" and refusal["initialized_mind_retained"]
    assert refusal["failure_audit"] == "report-only" and not refusal["unpause_attempted"]


def test_progress_mutation_cannot_change_frozen_cases_intent_or_report(monkeypatch):
    cases, model = inputs()
    expected = intent(cases, model)
    original = deepcopy(cases)

    def mutate(report):
        cases.clear()
        expected.clear()
        report["runtime_provenance"]["cases"].clear()

    result = run(monkeypatch, FakeSQL(), cases, model, expected, progress=mutate)
    assert len(result["results"]) == 2
    assert result["dataset_sha256"] == digest(encoded(original))
    assert result["runtime_provenance"]["cases"] == facts(original, model)["cases"]


def test_changed_model_between_episodes_refuses_before_second_initialization(monkeypatch):
    cases, model = inputs()
    sql = FakeSQL()

    def mutate(report):
        model.responses = {}

    with pytest.raises(ShadowError, match="evaluation_preflight_mismatch"):
        run(monkeypatch, sql, cases, model, intent(cases, model), progress=mutate)
    assert sum(state["initialized"] for state in sql.states.values()) == 1


def test_old_optional_behavior_has_no_preflight_report_or_namespace_check(monkeypatch):
    cases, model = inputs()
    sql = FakeSQL()
    report = run(monkeypatch, sql, cases, model, None, manifest={"build": "descriptive"})
    assert "local_preflight" not in report
    assert report["model_manifest_status"] == "descriptive_unverified"
    assert len(report["results"]) == 2


def test_effective_imported_version_change_and_inconsistent_alias_refuse(monkeypatch):
    from pipmind.shadow import context
    from pipmind.shadow import model as models

    cases, model = inputs()
    expected = intent(cases, model)
    monkeypatch.setattr(models, "PROMPT_VERSION", "new-imported-version")
    with pytest.raises(ShadowError, match="evaluation_preflight_mismatch"):
        preflight.compare_expected_local_provenance(expected, facts(cases, model), model)
    monkeypatch.setattr(context, "CONTEXT_VERSION", "inconsistent")
    with pytest.raises(ShadowError, match="evaluation_preflight_inconsistent_imports"):
        preflight.compare_expected_local_provenance(expected, facts(cases, model), model)


def test_exact_foundry_config_change_refuses_without_rpc_or_secret_disclosure():
    cases, _ = inputs()
    model = FoundryModel(
        "https://synthetic.example/api/projects/test", "triage", "decision", enabled=True
    )
    expected = intent(cases, model)
    model.managed_identity_client_id = "PRIVATE_MI"
    assert preflight.compare_expected_local_provenance(expected, facts(cases, model), model)
    assert "PRIVATE_MI" not in encoded(expected)
    model.credential_mode = "azure-cli"
    with pytest.raises(ShadowError, match="evaluation_preflight_mismatch"):
        preflight.compare_expected_local_provenance(expected, facts(cases, model), model)


def test_custom_models_never_run_hooks_and_cannot_pass_strict_gate():
    class Custom:
        def __getattribute__(self, name):
            raise AssertionError("custom introspection")

    cases, fixture = inputs()
    expected = intent(cases, fixture)
    with pytest.raises(ShadowError, match="evaluation_preflight_unknown_model"):
        preflight.compare_expected_local_provenance(expected, facts(cases, Custom()), Custom())


def test_descriptive_build_artifact_and_provider_claims_do_not_become_pins(monkeypatch):
    cases, model = inputs()
    original = facts(cases, model)
    expected = intent(cases, model)
    changed = deepcopy(original)
    changed["declared_build"] = {
        "source": "BUILD_SHA_environment",
        "value": "a" * 40,
        "status": "unverified",
    }
    changed["artifacts"][0] = {
        **changed["artifacts"][0],
        "status": "missing",
        "size_bytes": None,
        "raw_sha256": None,
        "lf_normalized_sha256": None,
    }
    result = preflight.compare_expected_local_provenance(expected, changed, model)
    assert result["provider_attested"] is result["loaded_code_attested"] is False


def test_case_order_and_content_are_both_required():
    selected = [case for case in load_cases() if case["case_id"] in {"T01", "T02"}]
    model = FixtureModel({})
    expected = intent(selected, model)
    for changed in (selected[::-1], deepcopy(selected)):
        if changed[0]["case_id"] == "T01":
            changed[0]["description"] = "changed"
        with pytest.raises(ShadowError, match="evaluation_preflight_mismatch"):
            preflight.compare_expected_local_provenance(expected, facts(changed, model), model)


@pytest.mark.parametrize("matching", [False, True])
def test_separate_cli_checks_intent_before_connector_and_reserves_private_report(
    monkeypatch, tmp_path, capsys, matching
):
    import sys

    from pipmind import dataset
    from pipmind.shadow import sql as sql_module

    cases, model = inputs()
    expected = intent(cases, model)
    if not matching:
        expected["repetitions"] = 3
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(encoded(expected), encoding="utf-8")
    output = tmp_path / "report.json"
    sql = FakeSQL()
    monkeypatch.setattr(dataset, "load_cases", lambda _: cases)
    monkeypatch.setattr(FoundryModel, "environment", lambda: model)
    monkeypatch.setattr(evaluations, "SqlMind", sql.store)
    monkeypatch.setattr(evaluations, "ShadowEngine", sql.engine)
    connectors = []

    def connector():
        assert output.is_file()
        connectors.append(True)
        return sql

    monkeypatch.setattr(sql_module, "connector", connector)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluation-preflight",
            "--dataset",
            "unused",
            "--cases",
            "T01",
            "--repetitions",
            "2",
            "--expected",
            str(expected_path),
            "--output",
            str(output),
        ],
    )
    assert preflight.main() == (0 if matching else 2)
    if matching:
        assert connectors == [True]
        assert loads(output.read_bytes())["local_preflight"]["local_facts_matched"]
    else:
        assert connectors == [] and not output.exists()
        assert capsys.readouterr().out.strip() == "evaluation_preflight_failed"


def test_custom_expected_containers_refuse_without_hooks_or_sql(monkeypatch):
    class Evil(dict):
        def items(self):
            raise AssertionError("custom items hook")

    cases, model = inputs()
    sql = FakeSQL()
    with pytest.raises(ShadowError, match="evaluation_provenance_json"):
        run(monkeypatch, sql, cases, model, Evil())
    assert sql.states == {} and sql.calls == sql.writes == 0


@pytest.mark.parametrize("failed_progress", [False, True])
def test_postinit_api_exception_preserves_private_report_without_progress_or_on_sink_failure(
    monkeypatch, failed_progress
):
    cases, model = inputs()
    sql = FakeSQL(initialized_policy="unknown policy declaration")
    callback_snapshots = []

    def fail(report):
        callback_snapshots.append(report)
        report["preflight_refusal"]["code"] = "callback mutation"
        raise RuntimeError("PRIVATE_CALLBACK_ERROR")

    with pytest.raises(preflight.EvaluationPreflightRefusal) as raised:
        run(
            monkeypatch,
            sql,
            cases,
            model,
            intent(cases, model),
            progress=fail if failed_progress else None,
        )
    error = raised.value
    assert str(error) == "evaluation_preflight_policy_mismatch"
    assert "PRIVATE_CALLBACK_ERROR" not in repr(error)
    assert "preflight_refusal" not in repr(error) and "trader" not in repr(error)
    refusal = error.partial_report["preflight_refusal"]
    assert refusal["code"] == "evaluation_preflight_policy_mismatch"
    assert refusal["checkpoint_persistence"] == (
        "unconfirmed" if failed_progress else "not_requested"
    )
    if failed_progress:
        callback_snapshots[0].clear()
        assert error.partial_report["preflight_refusal"] == refusal
    assert sql.calls == sql.unpauses == 0
    assert next(state for state in sql.states.values() if state["initialized"])["paused"]


def test_postinit_refusal_callback_can_read_retained_state_after_sql_lock_released(monkeypatch):
    cases, model = inputs()
    policy = {"version": "unexpected-synthetic-policy"}
    sql = FakeSQL(initialized_policy=policy)
    original_store = sql.store
    lock_depth, retained_reads = 0, []

    def locked_store(connect, trader):
        store = original_store(connect, trader)
        original_transaction = store.transaction

        @contextmanager
        def transaction():
            nonlocal lock_depth
            # A second connection cannot reacquire the same transaction-owned
            # exclusive SQL app lock while the policy observation holds it.
            assert lock_depth == 0, "same-mind callback read blocked by active SQL lock"
            lock_depth += 1
            try:
                with original_transaction() as cursor:
                    yield cursor
            finally:
                lock_depth -= 1

        store.transaction = transaction
        return store

    monkeypatch.setattr(sql, "store", locked_store)

    def progress(report):
        trader = report["preflight_refusal"]["trader"]
        retained = sql.store(sql, trader)
        with retained.transaction():
            retained_reads.append(deepcopy(retained.retained()))
        report["preflight_refusal"]["code"] = "callback mutation"

    with pytest.raises(preflight.EvaluationPreflightRefusal) as raised:
        run(monkeypatch, sql, cases, model, intent(cases, model), progress=progress)

    assert len(retained_reads) == 1 and lock_depth == 0
    head, records = retained_reads[0]
    assert head == {
        "paused": True,
        "working": {"views": [], "position_theses": [], "waiting_for": []},
    }
    assert any(meta["part"] == "identity" for meta, _ in records)
    refusal = raised.value.partial_report["preflight_refusal"]
    assert refusal["code"] == "evaluation_preflight_policy_mismatch"
    assert refusal["checkpoint_persistence"] == "callback_returned"
    state = sql.states[refusal["trader"]]
    assert state["policy"] == policy
    assert state["inputs"] == state["outcomes"] == {}
    assert state["pending"] == []
    assert "evaluation" not in state and "broker_snapshots" not in state
    assert sql.writes == 1 and sql.calls == sql.unpauses == 0
