"""Bounded local evaluation receipts, never provider or deployment attestation."""

import math
import os
import re
from pathlib import Path

from pipmind.contracts import Contracts, loads
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.context import CONTEXT_VERSION
from pipmind.shadow.evaluation_usage import ACCOUNTING_VERSION
from pipmind.shadow.maintenance import (
    BUDGET_VERSION,
    MAX_BYTES,
    MAX_ENTRIES,
    MAX_TEXT_BYTES,
    POLICY,
)
from pipmind.shadow.model import PROMPT_VERSION, REPAIR_PROMPT_VERSION, FixtureModel, FoundryModel
from pipmind.shadow.working_refs import VERSION

SOURCE_ROOT = Path(__file__).resolve().parents[2]
MAX_ARTIFACT_BYTES = 1048576
ARTIFACTS = (
    "pipmind/shadow/model.py",
    "pipmind/shadow/context.py",
    "pipmind/shadow/engine.py",
    "pipmind/shadow/store.py",
    "pipmind/shadow/maintenance.py",
    "pipmind/shadow/working_refs.py",
    "pipmind/shadow/evaluations.py",
    "pipmind/shadow/evaluation_usage.py",
    "pipmind/shadow/evaluation_provenance.py",
    "evals/rubrics/quality-v1.json",
    "uv.lock",
    "contracts/v1/evaluation-provenance.schema.json",
    "contracts/v1/model-reply.schema.json",
    "contracts/v1/working-memory.schema.json",
    "contracts/v1/memory-dispositions.schema.json",
    "contracts/v1/memory-repair.schema.json",
)


def freeze_json(value):
    """Reject custom hooks/cycles before making a plain-JSON snapshot.

    Existing dataset/parser limits remain authoritative; this adds no smaller
    byte, node or depth policy to otherwise valid JSON inputs.
    """
    active = set()

    def plain(item):
        kind = type(item)
        if item is None or kind in (str, int, bool):
            return
        if kind is float and math.isfinite(item):
            return
        if kind in (list, dict):
            identity = id(item)
            if identity in active:
                raise ShadowError("evaluation_provenance_json")
            active.add(identity)
            try:
                if kind is list:
                    for child in item:
                        plain(child)
                else:
                    for key, child in item.items():
                        if type(key) is not str:
                            raise ShadowError("evaluation_provenance_json")
                        plain(child)
            finally:
                active.remove(identity)
            return
        raise ShadowError("evaluation_provenance_json")

    try:
        plain(value)
        text = encoded(value)
        text.encode()  # Match the UTF-8 domain used by the eventual digest.
        return loads(text)
    except (ValueError, UnicodeError, RecursionError):
        raise ShadowError("evaluation_provenance_json") from None


def _configured_model(model):
    result = {
        "kind": "unknown",
        "execution_mode": "unknown",
        "binding_sha256": None,
        "timeout_seconds": None,
        "factory_present": None,
        "provider_version_status": "unverified",
        "rates_status": "unverified",
    }
    if type(model) is FixtureModel:
        return {**result, "kind": "fixture", "execution_mode": "scripted"}
    if type(model) is not FoundryModel:
        return result
    fields = vars(model)
    binding = {key: fields.get(key) for key in ("endpoint", "triage_model", "decision_model")}
    if not all(type(value) is str and 0 < len(value) <= 4096 for value in binding.values()):
        return result
    timeout = fields.get("timeout")
    if type(timeout) not in (int, float) or timeout <= 0:
        return result
    try:
        if not math.isfinite(timeout):
            return result
    except OverflowError:
        return result
    factory = fields.get("factory") is not None
    return {
        **result,
        "kind": "foundry",
        "execution_mode": "configured-factory" if factory else "configured-foundry-client",
        "binding_sha256": digest(encoded(binding)),
        "timeout_seconds": timeout,
        "factory_present": factory,
    }


def _artifact(path):
    receipt = {
        "path": path,
        "status": "unavailable",
        "size_bytes": None,
        "raw_sha256": None,
        "lf_normalized_sha256": None,
    }
    candidate = SOURCE_ROOT / path
    try:
        components = [
            SOURCE_ROOT.joinpath(*Path(path).parts[:index])
            for index in range(1, len(Path(path).parts) + 1)
        ]
        if any(part.is_symlink() for part in components) or not candidate.resolve().is_relative_to(
            SOURCE_ROOT.resolve()
        ):
            return receipt
        with candidate.open("rb") as stream:
            content = stream.read(MAX_ARTIFACT_BYTES + 1)
    except FileNotFoundError:
        return {**receipt, "status": "missing"}
    except (OSError, RuntimeError):
        return receipt
    if len(content) > MAX_ARTIFACT_BYTES:
        return {**receipt, "status": "oversize"}
    return {
        **receipt,
        "status": "available",
        "size_bytes": len(content),
        "raw_sha256": digest(content),
        "lf_normalized_sha256": digest(content.replace(b"\r\n", b"\n")),
    }


def capture_provenance(cases, model, *, repetitions, grader_version):
    """Observe local inputs/configuration; leave external claims unverified."""
    from pipmind.shadow.evaluations import GRADER_VERSION

    cases = freeze_json(cases)
    if (
        type(repetitions) is not int
        or not 2 <= repetitions <= 5
        or type(cases) is not list
        or not 1 <= len(cases) <= 12
        or type(grader_version) is not str
        or grader_version != GRADER_VERSION
        or any(
            type(case) is not dict
            or type(case.get("case_id")) is not str
            or not 1 <= len(case["case_id"]) <= 128
            for case in cases
        )
    ):
        raise ShadowError("evaluation_provenance_input")
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ShadowError("evaluation_provenance_input")
    build = os.environ.get("BUILD_SHA")
    known_build = build == "development" or (
        type(build) is str and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", build) is not None
    )
    facts = {
        "schema_version": 1,
        "kind": "local-evidence-only",
        "runner_mode": "fixture-working-state",
        "selected_cases_sha256": digest(encoded(cases)),
        "case_hash_domain": "canonical-single-case-list",
        "cases": [
            {"case_id": case["case_id"], "sha256": digest(encoded([case]))} for case in cases
        ],
        "repetitions": repetitions,
        "available_capabilities": {
            "normal_prompt_version": PROMPT_VERSION,
            "repair_prompt_version": REPAIR_PROMPT_VERSION,
            "context_version": CONTEXT_VERSION,
            "working_policy_version": POLICY,
            "working_budget": {
                "version": BUDGET_VERSION,
                "max_bytes": MAX_BYTES,
                "max_entries_per_list": MAX_ENTRIES,
                "max_text_bytes": MAX_TEXT_BYTES,
            },
            "working_codec_version": VERSION,
            "usage_accounting_version": ACCOUNTING_VERSION,
            "grader_version": GRADER_VERSION,
        },
        "declared_build": {
            "source": "BUILD_SHA_environment",
            "value": build if known_build else None,
            "status": "unverified" if known_build else "unknown",
        },
        "configured_model": _configured_model(model),
        "artifact_scope": "observed-on-disk-not-loaded-bytecode",
        "artifacts": [_artifact(path) for path in ARTIFACTS],
    }
    Contracts().validate("evaluation-provenance", facts)
    return facts
