"""Opt-in local evaluation intent matching; no provider/deployment attestation."""

from copy import deepcopy

from pipmind.contracts import ContractError, Contracts
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.evaluation_provenance import effective_local_settings, freeze_json

VERSION = "expected-local-evaluation-v1"
ABSENT_POLICY = {"present": False, "version": None, "sha256": None}


class EvaluationPreflightRefusal(ShadowError):
    """Retain private partial evidence without including it in exception text."""

    def __init__(self, partial_report):
        super().__init__("evaluation_preflight_policy_mismatch")
        self.partial_report = deepcopy(partial_report)


def _local_projection(provenance, model):
    facts = freeze_json(provenance)
    settings = effective_local_settings(model)
    # Reuse the normative fact types without changing the descriptive receipt.
    typed = {**facts, "available_capabilities": settings["capabilities"]}
    try:
        Contracts().validate("evaluation-provenance", typed)
    except ContractError:
        raise ShadowError("evaluation_preflight_unknown_facts") from None
    versions = settings["capabilities"]
    if any(
        value.casefold() in {"unknown", "unavailable", "unverified"}
        for value in (
            *(value for value in versions.values() if type(value) is str),
            versions["working_budget"]["version"],
        )
    ):
        raise ShadowError("evaluation_preflight_unknown_facts")
    return {
        "version": VERSION,
        "runner_mode": facts["runner_mode"],
        "selected_cases_sha256": facts["selected_cases_sha256"],
        "case_hash_domain": facts["case_hash_domain"],
        "cases": facts["cases"],
        "repetitions": facts["repetitions"],
        "effective_local_settings": settings,
    }


def expected_local_provenance(provenance, model, *, observed_memory_policy):
    """Construct reviewable intent; the caller must explicitly choose policy.

    Generation is not approval or observation of a future evaluation namespace.
    This runner supports only fixture working state without an installed policy.
    """
    policy = freeze_json(observed_memory_policy)
    if encoded(policy) != encoded(ABSENT_POLICY):
        raise ShadowError("evaluation_preflight_unsupported_policy")
    return {**_local_projection(provenance, model), "observed_memory_policy": policy}


def compare_expected_local_provenance(expected, provenance, model):
    """Refuse incomplete, unknown or changed local intent before SQL access.

    The policy field is a requirement here; a separate read must observe it.
    Exact canonical JSON comparison prevents bool/int or null/missing aliases.
    Descriptive builds, rates, provider versions and artifact hashes are excluded.
    """
    expected = freeze_json(expected)
    actual = expected_local_provenance(provenance, model, observed_memory_policy=ABSENT_POLICY)
    if type(expected) is not dict or expected.keys() != actual.keys():
        raise ShadowError("evaluation_preflight_expected_contract")
    if encoded(expected) != encoded(actual):
        raise ShadowError("evaluation_preflight_mismatch")
    return {
        "version": VERSION,
        "expected_sha256": digest(encoded(expected)),
        "local_facts_matched": True,
        "policy_observed": False,
        "provider_attested": False,
        "loaded_code_attested": False,
    }


def compare_observed_policy(expected, observed):
    """Require the exact independently read policy facts, with fixed errors."""
    expected, observed = freeze_json(expected), freeze_json(observed)
    if (
        type(expected) is not dict
        or encoded(expected.get("observed_memory_policy")) != encoded(ABSENT_POLICY)
        or encoded(observed) != encoded(ABSENT_POLICY)
    ):
        raise ShadowError("evaluation_preflight_policy_mismatch")


def main():
    """Separate opt-in CLI; existing shadow evaluate retains its old behavior."""
    import argparse
    import asyncio

    from pipmind.dataset import load_cases
    from pipmind.shadow.__main__ import EvaluationReport
    from pipmind.shadow.common import bounded_json, read_private
    from pipmind.shadow.evaluation_provenance import capture_provenance
    from pipmind.shadow.evaluations import CASES, GRADER_VERSION, evaluate
    from pipmind.shadow.model import FoundryModel
    from pipmind.shadow.sql import connector

    parser = argparse.ArgumentParser(description="Evaluate only matching explicit local intent")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--cases")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        cases = freeze_json(load_cases(args.dataset))
        selected = set(args.cases.split(",")) if args.cases else CASES
        if not selected or not selected <= CASES:
            raise ShadowError("unsupported_shadow_evaluation_case")
        if selected - {case["case_id"] for case in cases}:
            raise ShadowError("missing_shadow_evaluation_case")
        cases = [case for case in cases if case["case_id"] in selected]
        expected = bounded_json(read_private(args.expected, 65536), 65536)
        manifest = (
            bounded_json(read_private(args.manifest, 65536), 65536) if args.manifest else None
        )
        model = FoundryModel.environment()  # Configuration only; makes no RPC.
        facts = capture_provenance(
            cases, model, repetitions=args.repetitions, grader_version=GRADER_VERSION
        )
        compare_expected_local_provenance(expected, facts, model)
        output = EvaluationReport(args.output)
        report = asyncio.run(
            evaluate(
                connector(),
                cases,
                model,
                repetitions=args.repetitions,
                manifest=manifest,
                expected_local_provenance=expected,
                progress=lambda report: output.write({**report, "partial": True}),
            )
        )
        output.write(report)
        print(encoded(report["summary"]))
        return 0
    except (ShadowError, ContractError, OSError):
        # Do not echo caller fields, paths, endpoints or private manifest values.
        print("evaluation_preflight_failed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
