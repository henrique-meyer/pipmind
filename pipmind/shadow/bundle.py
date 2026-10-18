"""Consistent SQL experience export and paused clone import. Recovery remains M5."""

import io
import os
import zipfile
from collections import Counter
from pathlib import Path
from uuid import uuid4

from pipmind.contracts import ROOT, loads, stamp, utc
from pipmind.portability import BundleError, validate_bundle
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.model import PROMPT_VERSION

RETRIEVAL_VERSION = "sql-memory-v2"
SUPPORTED_CONTEXTS = {
    ("shadow-context-v1", "sql-memory-v1"),
    (PROMPT_VERSION, RETRIEVAL_VERSION),
}


def export_experience(store, moment):
    head, records = store.retained()
    if any(utc(meta["ts"]) > moment for meta, _ in records):
        raise BundleError("export_clock_before_checkpoint")
    files = {}
    for meta, body in records:
        part = meta["part"]
        path = {
            "identity": "mind/identity.md",
            "playbook": "mind/playbook.md",
            "working_memory": "mind/working-memory.md",
        }.get(part)
        if path is None:
            folder = {
                "journal": "journal",
                "memory": "memories",
                "review": "reviews",
                "playbook_history": "playbook-history",
            }[part]
            path = f"mind/{folder}/{digest(meta['record_id'])}.md"
        files[path] = b"---\n" + encoded(meta).encode() + b"\n---\n" + body.encode()
    counts = Counter(meta["part"] for meta, _ in records)
    manifest = {
        "format": "pipmind",
        "schema_version": 2,
        "mind_schema_version": 1,
        "kind": "experience",
        "trader_id": store.trader,
        "lineage_id": head["lineage"],
        "export_id": str(uuid4()),
        "created_at": stamp(moment),
        "checkpoint": {
            "checkpoint_id": str(head["revision"]),
            "event_watermark": head["watermark"],
            "as_of": stamp(moment),
        },
        "coverage": {
            "complete": True,
            "retained_from": min((meta["ts"] for meta, _ in records), key=utc),
            "through": stamp(moment),
            "gaps": [],
            "record_counts": {
                key: counts[key]
                for key in (
                    "identity",
                    "playbook",
                    "working_memory",
                    "journal",
                    "review",
                    "memory",
                    "playbook_history",
                )
            },
        },
        "compatibility": {
            "application_schema": 1,
            "application_version": "shadow-v1",
            "prompt_version": PROMPT_VERSION,
            "retrieval_version": RETRIEVAL_VERSION,
            "model_version": None,
            "inference_settings": {"max_output_tokens": 4096},
            "compatibility_decision": "shadow-runtime-v1",
        },
        "files": [
            {"path": path, "sha256": digest(body), "bytes": len(body)}
            for path, body in sorted(files.items())
        ],
    }
    files["manifest.json"] = encoded(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    data = output.getvalue()
    # No partial archive is returned if limits/completeness/relationships fail.
    validate_bundle(data)
    return data


def import_experience(store, content, moment, binding, expected_identity, *, interrupt=None):
    bundle = validate_bundle(content)
    compatibility = bundle.manifest["compatibility"]
    if (
        bundle.manifest["kind"] != "experience"
        or bundle.manifest["schema_version"] != 2
        or (compatibility["prompt_version"], compatibility["retrieval_version"])
        not in SUPPORTED_CONTEXTS
        or utc(bundle.manifest["checkpoint"]["as_of"]) > moment
    ):
        raise BundleError("unsupported_experience_compatibility")
    records = []
    for path, content in bundle.files.items():
        if path == "manifest.json":
            continue
        header, body = content[4:].split(b"\n---\n", 1)
        records.append((loads(header), body.decode()))
    # The working-memory JSON must obey the same normative runtime contract.
    source_working = next(body for meta, body in records if meta["part"] == "working_memory")
    store.contracts.validate("working-memory", loads(source_working))
    report = store.clone(
        records, bundle.manifest, moment, binding, expected_identity, interrupt=interrupt
    )
    if compatibility["prompt_version"] != PROMPT_VERSION:
        report["transformations"].append("legacy_context_retrieval_rebuilt")
    return report


def write_private(path, content):
    path = Path(path).resolve()
    if path.is_relative_to(ROOT) and not any(
        path.is_relative_to(parent) for parent in (ROOT / ".data", ROOT / "evals/reports")
    ):
        raise ShadowError("output_must_be_private")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation avoids overwriting an owner's existing bundle.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(content)
