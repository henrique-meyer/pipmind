"""Dataset loading and coverage gates, independent from model responses."""

from collections import Counter
from hashlib import sha256
from pathlib import Path

from pipmind.contracts import (
    ROOT,
    ContractError,
    Contracts,
    event_id,
    loads,
    utc,
    validate_event,
    validate_initial_mind,
)

CAPABILITIES = {
    "replay_core": 1,
    "gateway": 2,
    "boundary": 5,
    "sql_ownership": 5,
    "reconciliation": 5,
    "broker_recovery": 5,
    "self_review": 7,
    "public_publisher": 8,
}
IMPLEMENTED = frozenset({"replay_core", "gateway"})
FAMILIES = {
    "triage": 6,
    "decision_memory": 6,
    "safety_recovery": 10,
    "self_review": 6,
    "public_read_only": 2,
}


def validate_episode(case, contracts):
    contracts.validate("scenario", case)
    capabilities = case["required_capabilities"]
    if case["required_milestone"] != max(CAPABILITIES[cap] for cap in capabilities):
        raise ContractError("capability_milestone_mismatch")
    start = utc(case["clock_start"])
    mind = case["initial_mind"]
    validate_initial_mind(mind, start, contracts)
    identifiers = [row["record_id"] for row in mind["journal"] + mind["long_term"]]
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("duplicate_initial_record")
    if any(utc(row["ts"]) > start for row in mind["journal"] + mind["long_term"]):
        raise ContractError("future_initial_memory")
    if any(utc(row["as_of"]) > start for row in mind["working_memory"]["position_theses"]):
        raise ContractError("future_position_thesis")
    if any(utc(case[key]["as_of"]) > start for key in ("initial_account", "initial_positions")):
        raise ContractError("future_initial_snapshot")
    previous = start
    known = set(identifiers)
    for envelope in case["events"]:
        now = validate_event(envelope, contracts)
        if now < previous:
            raise ContractError("invalid_event_chronology")
        if event_id(envelope) in identifiers:
            raise ContractError("event_memory_identity_collision")
        known.add(event_id(envelope))
        previous = now
    expected = case["expected"]
    if not set(expected["required_evidence"]) <= known:
        raise ContractError("unknown_expected_evidence")
    if set(expected["retained_event_ids"]) != {event_id(event) for event in case["events"]}:
        raise ContractError("incomplete_retention_expectation")
    if expected["minimum_decisions"] > expected["maximum_decisions"]:
        raise ContractError("invalid_decision_bounds")
    if set(case["model_responses"]) != {event_id(event) for event in case["events"]}:
        raise ContractError("missing_model_step")
    if set(expected["wake_labels"]) - set(case["model_responses"]):
        raise ContractError("unknown_wake_label")
    return case


def load_cases(path=ROOT / "evals" / "fixtures" / "starter-v1.jsonl", *, starter=True):
    path = Path(path)
    if path.stat().st_size > 2_000_000:
        raise ContractError("dataset_too_large")
    contracts = Contracts()
    cases = [validate_episode(loads(line), contracts) for line in path.read_bytes().splitlines()]
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ContractError("duplicate_case_id")
    if starter:
        if Counter(case["family"] for case in cases) != FAMILIES:
            raise ContractError("starter_coverage_changed")
        development = [
            utc(event["available_at"])
            for case in cases
            if case["split"] == "development"
            for event in case["events"]
        ]
        holdout = [utc(case["clock_start"]) for case in cases if case["split"] == "holdout"]
        if not development or not holdout or min(holdout) <= max(development):
            raise ContractError("holdout_not_chronologically_separate")
    return cases


def dataset_digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()
