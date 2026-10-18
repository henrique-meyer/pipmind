"""Run offline replay: python -m pipmind.replay --help."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

from pipmind.contracts import ROOT, ContractError, Contracts, loads, utc
from pipmind.core import (
    CONTEXT_VERSION,
    INSTRUCTIONS,
    InMemoryStore,
    ReplayCore,
    ReplayGateway,
    ScriptedModel,
    SimulatedClock,
)
from pipmind.dataset import IMPLEMENTED, dataset_digest, load_cases
from pipmind.feeds.fixtures import run_fixtures as run_feed_fixtures
from pipmind.gateway.recording import exercise_mapping_case, run_recording
from pipmind.graders import GRADER_VERSION, grade
from pipmind.portability_fixtures import FIXTURE_VERSION, run_fixtures

DEFAULT_DATASET = ROOT / "evals" / "fixtures" / "starter-v1.jsonl"
DEFAULT_WEEK = ROOT / "evals" / "fixtures" / "week-v1.jsonl"
DEFAULT_BASELINE = ROOT / "evals" / "baseline-v1.json"


def source_provenance():
    files = sorted((ROOT / "pipmind").rglob("*.py")) + sorted(
        (ROOT / "contracts" / "v1").glob("*.json")
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, stderr=subprocess.DEVNULL
            )
        )
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    origin = "git" if commit else "unavailable"
    supplied = os.environ.get("PIPMIND_SOURCE_SHA", "")
    if commit is None and re.fullmatch(r"[0-9a-f]{40}", supplied):
        commit, origin = supplied, "environment"
    return {
        "source_commit": commit,
        "working_tree_dirty": dirty,
        "implementation_sha256": digest.hexdigest(),
        "source_commit_origin": origin,
    }


def replay_episode(case, *, model=None, contracts=None):
    contracts = contracts or Contracts()
    # Independent runs always receive fresh copies, including repeated or variant runs.
    store = InMemoryStore(case["initial_mind"])
    clock = SimulatedClock(utc(case["clock_start"]))
    core = ReplayCore(
        case["case_id"],
        clock,
        model or ScriptedModel(case["model_responses"]),
        store,
        contracts,
        ReplayGateway(case["initial_account"], case["initial_positions"]),
    )
    for event in case["events"]:
        core.process(deepcopy(event))
    trace = core.finish()
    if "gateway" in case["required_capabilities"]:
        exercise_mapping_case(case, trace)
    return trace, grade(case, trace, contracts)


def run_suite(cases, week, *, repetitions=2, overrides=None, output=None, split="all"):
    if not 1 <= repetitions <= 20:
        raise ContractError("repetitions_out_of_bounds")
    contracts = Contracts()
    overrides = overrides or {}
    contracts.validate("model-overrides", overrides)
    if set(overrides) - {case["case_id"] for case in cases}:
        raise ContractError("unknown_override_case")
    results, traces = [], []
    for original in cases:
        if split != "all" and original["split"] != split:
            continue
        missing = sorted(set(original["required_capabilities"]) - IMPLEMENTED)
        row = {
            "case_id": original["case_id"],
            "family": original["family"],
            "split": original["split"],
            "mandatory": original["mandatory"],
            "required_milestone": original["required_milestone"],
            "missing_capabilities": missing,
        }
        if missing:
            results.append({**row, "status": "deferred", "failures": [], "runs": []})
            continue
        case = deepcopy(original)
        changed = overrides.get(case["case_id"], {})
        if set(changed) - set(case["model_responses"]):
            raise ContractError("unknown_override_event")
        case["model_responses"].update(changed)
        runs = []
        for repetition in range(repetitions):
            trace, failures = replay_episode(case, contracts=contracts)
            traces.append((case["case_id"], repetition, trace))
            runs.append(
                {
                    "repetition": repetition,
                    "failures": failures,
                    "model_calls": trace["usage"]["model_calls"],
                    "latency_ms": round(trace["usage"]["latency_ms"], 4),
                }
            )
        failures = sorted({code for run in runs for code in run["failures"]})
        results.append(
            {
                **row,
                "status": "failed" if failures else "passed",
                "failures": failures,
                "runs": runs,
                "starting_playbook_sha256": hashlib.sha256(
                    case["initial_mind"]["playbook"]["text"].encode()
                ).hexdigest(),
            }
        )
    week_runs = []
    for repetition in range(repetitions):
        trace, failures = replay_episode(week, contracts=contracts)
        traces.append((week["case_id"], repetition, trace))
        week_runs.append(
            {
                "repetition": repetition,
                "failures": failures,
                "events": len(trace["seen"]),
                "decisions": len(trace["decisions"]),
                "proposals": len(trace["proposals"]),
                "dispatches": len(trace["dispatches"]),
            }
        )
    portability = run_fixtures()
    counts = Counter(row["status"] for row in results)
    areas = {}
    for row in results:
        group = areas.setdefault(row["family"], {"passed": 0, "failed": 0, "deferred": 0})
        group[row["status"]] += 1
    report = {
        "schema_version": 1,
        "dataset_version": "starter-v1",
        "grader_version": GRADER_VERSION,
        "context_version": CONTEXT_VERSION,
        "instructions_sha256": hashlib.sha256(INSTRUCTIONS.encode()).hexdigest(),
        "model_version": "scripted-v1",
        "deployment_version": None,
        "inference_settings": {"mode": "scripted", "network": "none", "sampling": False},
        "portability_version": FIXTURE_VERSION,
        "source": source_provenance(),
        "split": split,
        "variant": "candidate" if overrides else "baseline",
        "repetitions": repetitions,
        "implemented_capabilities": sorted(IMPLEMENTED),
        "counts": {status: counts[status] for status in ("passed", "failed", "deferred")},
        "areas": areas,
        "cases": results,
        "fixture_week": {
            "passed": all(not row["failures"] for row in week_runs),
            "runs": week_runs,
        },
        "portability": portability,
        "gateway": run_recording(),
        "feeds": run_feed_fixtures(),
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0,
            "accounting": "synthetic model calls only; no provider usage",
            "model_calls": sum(t[2]["usage"]["model_calls"] for t in traces),
            "latency_ms": round(sum(t[2]["usage"]["latency_ms"] for t in traces), 4),
        },
    }
    if output is not None:
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        for case_id, repetition, trace in traces:
            (output / f"{case_id}-r{repetition}.trace.json").write_text(
                json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        (output / "decisions.jsonl").write_text(
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n"
                for case_id, _, trace in traces
                if case_id == week["case_id"]
                for row in trace["decisions"]
            ),
            encoding="utf-8",
        )
    return report


def sanitized_summary(report):
    """Only this bounded allowlist is suitable for public CI artifacts."""
    return {
        key: deepcopy(report[key])
        for key in (
            "schema_version",
            "dataset_version",
            "grader_version",
            "context_version",
            "instructions_sha256",
            "model_version",
            "deployment_version",
            "inference_settings",
            "portability_version",
            "source",
            "split",
            "variant",
            "repetitions",
            "implemented_capabilities",
            "counts",
            "areas",
            "fixture_week",
            "portability",
            "usage",
            "dataset_sha256",
            "fixture_week_sha256",
            "portability_fixture_sha256",
            "gateway_fixture_sha256",
            "feed_fixture_sha256",
        )
    } | {
        "feeds": {
            key: deepcopy(report["feeds"][key])
            for key in ("version", "network", "dispatches", "checks", "passed")
        },
        "gateway": {
            key: deepcopy(report["gateway"][key])
            for key in ("version", "mode", "dispatches", "checks", "passed")
        },
        "cases": [
            {
                key: row[key]
                for key in (
                    "case_id",
                    "family",
                    "split",
                    "status",
                    "mandatory",
                    "required_milestone",
                    "missing_capabilities",
                    "failures",
                )
            }
            for row in report["cases"]
        ],
    }


def baseline_snapshot(report):
    summary = sanitized_summary(report)
    # Content hash identifies the implementation without a circular Git commit reference.
    summary["source"] = {"implementation_sha256": report["source"]["implementation_sha256"]}
    summary.pop("usage")
    summary["fixture_week"] = {
        "passed": report["fixture_week"]["passed"],
        "runs": report["fixture_week"]["runs"],
    }
    return summary


def compare(report, baseline):
    differences = []
    for key in (
        "dataset_sha256",
        "fixture_week_sha256",
        "portability_fixture_sha256",
        "grader_version",
        "dataset_version",
        "gateway_fixture_sha256",
        "feed_fixture_sha256",
    ):
        if report[key] != baseline[key]:
            differences.append("baseline_metadata_changed")
    actual = {row["case_id"]: row for row in report["cases"]}
    previous = {row["case_id"]: row for row in baseline["cases"]}
    if report["split"] == "all" and actual.keys() != previous.keys():
        differences.append("baseline_coverage_changed")
    for identifier, row in actual.items():
        old = previous.get(identifier)
        if old is None or old["status"] == "passed" and row["status"] != "passed":
            differences.append("baseline_case_regression")
        elif old["status"] == "deferred" and row["status"] == "failed":
            differences.append("baseline_case_regression")
    if baseline["fixture_week"]["passed"] and not report["fixture_week"]["passed"]:
        differences.append("baseline_week_regression")
    previous_ports = {row["case_id"] for row in baseline["portability"]}
    if previous_ports != {row["case_id"] for row in report["portability"]}:
        differences.append("baseline_portability_coverage_changed")
    old_gateway, gateway = baseline.get("gateway"), report.get("gateway")
    if old_gateway and (not gateway or old_gateway["checks"].keys() != gateway["checks"].keys()):
        differences.append("baseline_gateway_coverage_changed")
    if gateway and (
        not gateway["passed"] or not all(gateway["checks"].values()) or gateway["dispatches"] != 0
    ):
        differences.append("baseline_gateway_regression")
    old_feeds, feeds = baseline.get("feeds"), report.get("feeds")
    if old_feeds and (not feeds or old_feeds["checks"].keys() != feeds["checks"].keys()):
        differences.append("baseline_feed_coverage_changed")
    if feeds and (
        not feeds["passed"] or not all(feeds["checks"].values()) or feeds["dispatches"] != 0
    ):
        differences.append("baseline_feed_regression")
    return sorted(set(differences))


def successful(report):
    return (
        not report["counts"]["failed"]
        and report["counts"]["passed"] > 0
        and report["fixture_week"]["passed"]
        and all(row["passed"] for row in report["portability"])
        and report["gateway"]["passed"]
        and all(report["gateway"]["checks"].values())
        and report["gateway"]["dispatches"] == 0
        and report["feeds"]["passed"]
        and all(report["feeds"]["checks"].values())
        and report["feeds"]["dispatches"] == 0
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "run", "record-baseline"])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--week", type=Path, default=DEFAULT_WEEK)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "reports" / "latest")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--responses", type=Path, help="Candidate scripted response overrides, no code"
    )
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--split", choices=["all", "development", "holdout"], default="all")
    parser.add_argument("--ci", action="store_true", help="Require full coverage and baseline gate")
    args = parser.parse_args(argv)
    try:
        output = args.output.resolve()
        if output.is_relative_to(ROOT) and not any(
            output.is_relative_to(base) for base in (ROOT / ".data", ROOT / "evals" / "reports")
        ):
            raise ContractError("output_must_be_private")
        cases, weeks = load_cases(args.dataset), load_cases(args.week, starter=False)
        if len(weeks) != 1:
            raise ContractError("exactly_one_fixture_week_required")
        if args.ci and (args.split != "all" or args.repetitions < 2 or args.command != "run"):
            raise ContractError("incomplete_ci_run")
        if args.command == "validate":
            if not all(row["passed"] for row in run_fixtures()):
                raise ContractError("portability_fixture_failure")
            print(f"Validated {len(cases)} scenarios, fixture week and portability fixtures.")
            return 0
        overrides = loads(args.responses.read_bytes()) if args.responses else None
        report = run_suite(
            cases,
            weeks[0],
            repetitions=args.repetitions,
            overrides=overrides,
            output=args.output,
            split=args.split,
        )
        report.update(
            dataset_sha256=dataset_digest(args.dataset),
            fixture_week_sha256=dataset_digest(args.week),
            portability_fixture_sha256=dataset_digest(
                ROOT / "evals" / "portability" / "cases.json"
            ),
            gateway_fixture_sha256=dataset_digest(ROOT / "evals" / "gateway" / "session-v1.json"),
            feed_fixture_sha256=dataset_digest(ROOT / "evals" / "feeds" / "scenario-v1.json"),
        )
        if args.command == "record-baseline":
            if not successful(report) or args.split != "all" or overrides:
                raise ContractError("cannot_record_failing_or_partial_baseline")
            args.baseline.write_text(
                json.dumps(baseline_snapshot(report), indent=2) + "\n", encoding="utf-8"
            )
            differences = []
        else:
            try:
                differences = compare(report, loads(args.baseline.read_bytes()))
            except (OSError, KeyError, ContractError, TypeError):
                differences = ["baseline_unavailable_or_invalid"]
        report["baseline_differences"] = differences
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        summary = sanitized_summary(report) | {"baseline_differences": differences}
        encoded = json.dumps(summary, indent=2) + "\n"
        if len(encoded.encode()) > 65_536:
            raise ContractError("summary_size_limit")
        (args.output / "summary.json").write_text(encoded, encoding="utf-8")
        print(
            f"M1 replay: {report['counts']}; fixture week={report['fixture_week']['passed']}; "
            f"portability={sum(row['passed'] for row in report['portability'])}/"
            f"{len(report['portability'])}; baseline differences={differences}"
        )
        return 0 if successful(report) and not differences else 1
    except (ContractError, OSError, KeyError) as error:
        # Never print raw fixture contents or exception messages from external input.
        if isinstance(error, ContractError):
            code = str(error)
        elif isinstance(error, OSError):
            code = f"io_error:{error.errno}"
        else:
            code = "invalid_input_or_baseline"
        print(f"Replay failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
