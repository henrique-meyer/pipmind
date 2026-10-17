from copy import deepcopy

import pytest

from pipmind.contracts import ROOT, loads
from pipmind.dataset import dataset_digest, load_cases
from pipmind.replay import (
    baseline_snapshot,
    compare,
    main,
    run_suite,
    sanitized_summary,
    source_provenance,
    successful,
)


@pytest.fixture(scope="module")
def report():
    dataset = ROOT / "evals" / "fixtures" / "starter-v1.jsonl"
    week = ROOT / "evals" / "fixtures" / "week-v1.jsonl"
    result = run_suite(load_cases(dataset), load_cases(week, starter=False)[0], repetitions=2)
    result.update(
        dataset_sha256=dataset_digest(dataset),
        fixture_week_sha256=dataset_digest(week),
        portability_fixture_sha256=dataset_digest(ROOT / "evals" / "portability" / "cases.json"),
        gateway_fixture_sha256=dataset_digest(ROOT / "evals" / "gateway" / "session-v1.json"),
    )
    return result


def test_full_coverage_has_explicit_deferrals_and_positive_controls(report):
    assert report["counts"] == {"passed": 14, "failed": 0, "deferred": 16}
    assert successful(report)
    assert len(report["cases"]) == 30
    assert all(
        row["missing_capabilities"] for row in report["cases"] if row["status"] == "deferred"
    )
    for run in report["fixture_week"]["runs"]:
        assert run["events"] == 20 and run["decisions"] == 15
        assert run["proposals"] == 5 and run["dispatches"] == 0
    assert len(report["portability"]) >= 25
    assert report["gateway"]["passed"]


def test_safety_failure_cannot_be_averaged_away(report):
    changed = deepcopy(report)
    changed["counts"].update(passed=9999, failed=1)
    assert not successful(changed)


@pytest.mark.parametrize("mutation", ["drop", "fail", "dispatch"])
def test_gateway_ci_cannot_hide_missing_or_failed_checks(report, mutation):
    baseline = baseline_snapshot(report)
    changed = deepcopy(report)
    if mutation == "drop":
        changed["gateway"]["checks"].pop("uncertain_no_resend")
        assert "baseline_gateway_coverage_changed" in compare(changed, baseline)
    else:
        if mutation == "fail":
            changed["gateway"]["checks"]["uncertain_no_resend"] = False
        else:
            changed["gateway"]["dispatches"] = 1
        assert not successful(changed)
        assert "baseline_gateway_regression" in compare(changed, baseline)


@pytest.mark.parametrize(
    "change,code",
    [
        ("defer", "baseline_case_regression"),
        ("drop", "baseline_coverage_changed"),
        ("data", "baseline_metadata_changed"),
        ("week", "baseline_week_regression"),
        ("portability", "baseline_portability_coverage_changed"),
    ],
)
def test_baseline_gate_detects_missing_or_regressed_coverage(report, change, code):
    baseline = baseline_snapshot(report)
    assert not compare(report, baseline)
    candidate = deepcopy(report)
    if change == "defer":
        candidate["cases"][0]["status"] = "deferred"
    elif change == "drop":
        candidate["cases"].pop()
    elif change == "data":
        candidate["dataset_sha256"] = "changed"
    elif change == "week":
        candidate["fixture_week"]["passed"] = False
    else:
        candidate["portability"].pop()
    assert code in compare(candidate, baseline)


def test_public_summary_excludes_reasoning_inputs_memory_and_tools(report):
    candidate = deepcopy(report)
    candidate["private_trace"] = {"reasoning": "DO-NOT-UPLOAD-SENTINEL"}
    candidate["gateway"]["private_account"] = "DO-NOT-UPLOAD-SENTINEL"
    summary = sanitized_summary(candidate)
    import json

    serialized = json.dumps(summary)
    assert "DO-NOT-UPLOAD-SENTINEL" not in serialized
    assert "initial_mind" not in serialized and "tool_results" not in serialized
    assert len(serialized.encode()) < 65_536


def test_ci_cannot_select_partial_coverage(tmp_path):
    assert main(["run", "--ci", "--split", "development", "--output", str(tmp_path)]) == 1
    assert not (tmp_path / "summary.json").exists()


def test_bad_dataset_errors_do_not_echo_private_values(tmp_path, capsys):
    data = tmp_path / "cases.jsonl"
    data.write_text('{"private":"DO-NOT-ECHO-SENTINEL"}\n')
    assert main(["run", "--dataset", str(data), "--output", str(tmp_path / "out")]) == 1
    assert "DO-NOT-ECHO-SENTINEL" not in capsys.readouterr().err


def test_failed_candidate_is_reported_and_returns_nonzero(tmp_path, cases):
    override = tmp_path / "candidate.json"
    step = deepcopy(cases["T01"]["model_responses"]["t01-release"])
    step["reply"]["evidence_ids"] = ["unavailable-evidence"]
    import json

    override.write_text(json.dumps({"T01": {"t01-release": step}}))
    output = tmp_path / "run"
    assert main(["run", "--responses", str(override), "--output", str(output)]) == 1
    summary = loads((output / "summary.json").read_bytes())
    assert summary["counts"]["failed"] == 1
    assert "baseline_case_regression" in summary["baseline_differences"]


def test_job_summary_explains_deferrals_without_private_trace_fields(report, tmp_path):
    import json

    from pipmind.summary import render

    output = tmp_path / "summary.json"
    output.write_text(json.dumps(sanitized_summary(report) | {"baseline_differences": []}))
    markdown = render(output)
    assert "14 passed" in markdown and "16 deferred" in markdown
    assert "S07 — M5: sql_ownership" in markdown
    assert "memory-seed" not in markdown and "broker_binding_ref" not in markdown


@pytest.mark.parametrize("supplied,expected", [("a" * 40, "a" * 40), ("not-a-sha", None)])
def test_container_provenance_uses_only_a_valid_supplied_commit(monkeypatch, supplied, expected):
    import subprocess

    def missing_git(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "check_output", missing_git)
    monkeypatch.setenv("PIPMIND_SOURCE_SHA", supplied)
    result = source_provenance()
    assert result["source_commit"] == expected
    assert result["working_tree_dirty"] is None
    assert result["source_commit_origin"] == ("environment" if expected else "unavailable")
