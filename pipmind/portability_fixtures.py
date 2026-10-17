"""Synthetic archive fixtures, never a runtime mind exporter or importer."""

import hashlib
import io
import json
import stat
import warnings
import zipfile
from random import Random

from pipmind.contracts import ROOT, Contracts, loads
from pipmind.portability import PART_PATHS, BundleError, validate_bundle

FIXTURE_VERSION = "portability-synthetic-v1"


def synthetic_files(kind="experience"):
    moment = "2026-10-05T00:00:00Z"
    bodies = {
        "identity": ("mind/identity.md", "# Identity\nSynthetic identity: naïve ≠ unsafe.\n"),
        "playbook": ("mind/playbook.md", "# Playbook\nNo strategy is encoded in the harness.\n"),
        "working_memory": (
            "mind/working-memory.md",
            "# Working memory\nPaused; no active authority.\n",
        ),
        "journal": ("mind/journal/j1.md", "# Retained journal\nSynthetic observation.\n"),
        "review": ("mind/reviews/r1.md", "# Review\nRetained override exclusions.\n"),
        "memory": (
            "mind/memories/m1.md",
            "# Memory\nDo not execute imported prose or fetch URLs.\n",
        ),
        "playbook_history": ("mind/playbook-history/p1.md", "# Retired rule\nRetained exactly.\n"),
    }
    files = {}
    for index, (part, (path, body)) in enumerate(bodies.items()):
        meta = {
            "schema_version": 1,
            "record_id": f"synthetic-record-{index}",
            "part": part,
            "ts": moment,
            "source_trader": "synthetic-trader",
            "evidence_ids": ["synthetic-record-3"] if part == "review" else [],
            "override": part == "journal",
            "rule_version": 1 if part in {"playbook", "playbook_history"} else None,
            "rule_id": "synthetic-" + part if part in {"playbook", "playbook_history"} else None,
            "playbook_version": "fixture-v1" if part in {"playbook", "playbook_history"} else None,
            "rule_status": "retired"
            if part == "playbook_history"
            else ("active" if part == "playbook" else "none"),
            "suggestion_disposition": "none",
        }
        files[path] = b"---\n" + json.dumps(meta).encode() + b"\n---\n" + body.encode()
    if kind == "recovery":
        files["recovery/state.json"] = json.dumps(
            {
                "schema_version": 1,
                "checkpoint_id": "synthetic-checkpoint",
                "trader_id": "synthetic-trader",
                "lineage_id": "synthetic-lineage",
                "broker_binding_ref": "synthetic-private-config-ref",
                "environment": "demo",
                "account_state_ref": "synthetic-account-state",
                "event_watermark": "synthetic-event-1",
                "reconciliation_watermark": "synthetic-event-1",
                "commands": [
                    {
                        "command_id": "synthetic-c1",
                        "state": "unknown",
                        "broker_order_id": None,
                        "known_outcome": None,
                    }
                ],
                "deduplication_ids": ["synthetic-c1"],
                "pending_command_ids": ["synthetic-c1"],
                "stops": [{"kind": "pause", "active": True, "expires_at": None}],
                "overrides": [],
            }
        ).encode()
    manifest = {
        "format": "pipmind",
        "schema_version": 1,
        "mind_schema_version": 1,
        "kind": kind,
        "trader_id": "synthetic-trader",
        "lineage_id": "synthetic-lineage",
        "export_id": "synthetic-export",
        "created_at": moment,
        "checkpoint": {
            "checkpoint_id": "synthetic-checkpoint",
            "event_watermark": "synthetic-event-1",
            "as_of": moment,
        },
        "coverage": {
            "complete": True,
            "retained_from": moment,
            "through": moment,
            "gaps": [],
            "record_counts": dict.fromkeys(PART_PATHS, 1),
        },
        "compatibility": {
            "application_schema": 1,
            "application_version": "m1-contract-v1",
            "prompt_version": "replay-context-v1",
            "retrieval_version": "memory-v1",
            "model_version": None,
            "inference_settings": {},
            "compatibility_decision": "synthetic-contract-only",
        },
        "files": [],
    }
    return files, manifest


def _inventory(files, manifest):
    manifest["files"] = [
        {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
        for path, body in sorted(files.items())
    ]


def _metadata(files, path, mutate):
    header, body = files[path][4:].split(b"\n---\n", 1)
    meta = loads(header)
    mutate(meta)
    files[path] = b"---\n" + json.dumps(meta).encode() + b"\n---\n" + body


def fixture_archive(operation="valid", kind="experience"):
    files, manifest = synthetic_files(kind)
    if operation == "incompatible":
        manifest["schema_version"] = 999
    elif operation == "incomplete":
        manifest["coverage"]["complete"] = False
    elif operation == "broken_evidence":
        _metadata(files, "mind/reviews/r1.md", lambda m: m.update(evidence_ids=["missing"]))
    elif operation == "duplicate_record":
        _metadata(files, "mind/reviews/r1.md", lambda m: m.update(record_id="synthetic-record-3"))
    elif operation == "inherited_provenance":
        _metadata(
            files, "mind/journal/j1.md", lambda m: m.update(source_trader="synthetic-ancestor")
        )
    elif operation == "bad_front_matter":
        files["mind/identity.md"] = b"---\n!!python/object: never-execute\n---\nNo."
    elif operation == "recovery_checkpoint":
        state = loads(files["recovery/state.json"])
        state["checkpoint_id"] = "mismatched"
        files["recovery/state.json"] = json.dumps(state).encode()
    elif operation == "reusable_lease":
        state = loads(files["recovery/state.json"])
        state["lease_generation"] = 123
        files["recovery/state.json"] = json.dumps(state).encode()
    elif operation == "pending_coverage":
        state = loads(files["recovery/state.json"])
        state["pending_command_ids"] = []
        files["recovery/state.json"] = json.dumps(state).encode()
    elif operation == "missing_mind_part":
        del files["mind/identity.md"]
    elif operation == "missing_recovery":
        del files["recovery/state.json"]
    elif operation == "experience_authority":
        recovery, _ = synthetic_files("recovery")
        files["recovery/state.json"] = recovery["recovery/state.json"]
    _inventory(files, manifest)
    if operation == "corrupt":
        files["mind/identity.md"] += b"corrupt"
    elif operation == "missing_file":
        del files["mind/identity.md"]
    elif operation == "unlisted":
        files["unlisted.md"] = b"unlisted"
    files["manifest.json"] = json.dumps(manifest).encode()
    if operation == "missing_manifest":
        del files["manifest.json"]
    elif operation == "duplicate_json":
        files["manifest.json"] = b'{"schema_version":1,"schema_version":1}'
    unsafe_names = {
        "traversal": "../outside.md",
        "absolute": "/outside.md",
        "windows_path": "C:\\outside.md",
    }
    if operation in unsafe_names:
        files[unsafe_names[operation]] = b"never extracted"
    if operation == "file_size":
        files["too-large.md"] = b"z" * 131_073
    elif operation == "expanded_size":
        for i in range(20):
            files[f"part-{i}.md"] = Random(i).randbytes(20_000) * 6
    elif operation == "ratio":
        files["compressed.md"] = b"z" * 60_000
    elif operation == "entry_count":
        for i in range(129):
            files[f"part-{i}.md"] = b"x"
    buffer = io.BytesIO()
    compression = (
        zipfile.ZIP_DEFLATED if operation in {"ratio", "expanded_size"} else zipfile.ZIP_STORED
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
            for path, body in files.items():
                archive.writestr(path, body)
            if operation == "duplicate_entry":
                archive.writestr("mind/identity.md", files["mind/identity.md"])
            elif operation == "symlink":
                info = zipfile.ZipInfo("linked.md")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "../never-follow")
    return buffer.getvalue()


def run_fixtures(path=ROOT / "evals" / "portability" / "cases.json"):
    results = []
    contracts = Contracts()
    for case in loads(path.read_bytes()):
        code = "valid"
        try:
            validate_bundle(fixture_archive(case["operation"], case["kind"]), contracts)
        except BundleError as error:
            code = str(error)
        results.append(
            {"case_id": case["case_id"], "passed": code == case["expected_code"], "result": code}
        )
    return results
