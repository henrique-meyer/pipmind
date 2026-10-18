"""Bounded bundle validation. This module never extracts files or activates state."""

import hashlib
import io
import re
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType

from pipmind.contracts import ContractError, Contracts, loads, utc

MAX_ENTRIES = 128
MAX_FILE_BYTES = 131_072
MAX_TOTAL_BYTES = 2_097_152
MAX_RATIO = 100
RUNTIME_MAX_ENTRIES = 10000
RUNTIME_MAX_TOTAL_BYTES = 33_554_432
RUNTIME_MAX_FILE_BYTES = 1_048_576
PART_PATHS = {
    "identity": r"mind/identity\.md",
    "playbook": r"mind/playbook\.md",
    "working_memory": r"mind/working-memory\.md",
    "journal": r"mind/journal/[a-z0-9_-]+\.md",
    "review": r"mind/reviews/[a-z0-9_-]+\.md",
    "memory": r"mind/memories/[a-z0-9_-]+\.md",
    "playbook_history": r"mind/playbook-history/[a-z0-9_-]+\.md",
}


class BundleError(ValueError):
    """A stable code without imported content or private identifiers."""


@dataclass(frozen=True)
class ValidatedBundle:
    manifest: dict
    files: object
    records: tuple


def _check(name, value, contracts):
    try:
        contracts.validate(name, value)
    except ContractError as error:
        raise BundleError(f"schema:{name}") from error


def _json(content):
    try:
        return loads(content)
    except ContractError as error:
        raise BundleError("invalid_json") from error


def _read_archive(content):
    if len(content) > RUNTIME_MAX_TOTAL_BYTES:
        raise BundleError("archive_size")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > RUNTIME_MAX_ENTRIES:
                raise BundleError("entry_count")
            names = [entry.filename for entry in entries]
            if len(set(names)) != len(names):
                raise BundleError("duplicate_entry")
            runtime = False
            if "manifest.json" in names:
                item = archive.getinfo("manifest.json")
                if item.file_size > MAX_TOTAL_BYTES:
                    raise BundleError("file_size")
                hint = _json(archive.read(item))
                runtime = isinstance(hint, dict) and hint.get("schema_version") == 2
            if not runtime and len(content) > MAX_TOTAL_BYTES:
                raise BundleError("archive_size")
            if not runtime and len(entries) > MAX_ENTRIES:
                raise BundleError("entry_count")
            total = 0
            for entry in entries:
                name = entry.filename
                if not re.fullmatch(r"[a-z0-9][a-z0-9/._-]*", name) or any(
                    part in {"", ".", ".."} for part in name.split("/")
                ):
                    raise BundleError("unsafe_path")
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in {0, stat.S_IFREG} or entry.is_dir():
                    raise BundleError("unsafe_entry_type")
                if entry.flag_bits & 1:
                    raise BundleError("encrypted_entry")
                if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise BundleError("compression_method")
                limit = (
                    (MAX_TOTAL_BYTES if name == "manifest.json" else RUNTIME_MAX_FILE_BYTES)
                    if runtime
                    else MAX_FILE_BYTES
                )
                if entry.file_size > limit:
                    raise BundleError("file_size")
                if entry.file_size > max(entry.compress_size, 1) * MAX_RATIO:
                    raise BundleError("compression_ratio")
                total += entry.file_size
            if total > (RUNTIME_MAX_TOTAL_BYTES if runtime else MAX_TOTAL_BYTES):
                raise BundleError("expanded_size")
            result = {}
            for entry in entries:
                with archive.open(entry) as stream:
                    limit = (
                        (
                            MAX_TOTAL_BYTES
                            if entry.filename == "manifest.json"
                            else RUNTIME_MAX_FILE_BYTES
                        )
                        if runtime
                        else MAX_FILE_BYTES
                    )
                    payload = stream.read(limit + 1)
                if len(payload) > limit:
                    raise BundleError("file_size")
                result[entry.filename] = payload
            return result
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, NotImplementedError) as error:
        raise BundleError("invalid_zip") from error


def validate_bundle(content, contracts=None):
    contracts = contracts or Contracts()
    files = _read_archive(content)
    if "manifest.json" not in files:
        raise BundleError("missing_manifest")
    manifest = _json(files["manifest.json"])
    _check("bundle-manifest", manifest, contracts)
    if (manifest["schema_version"] == 2) != (
        manifest["compatibility"]["compatibility_decision"] == "shadow-runtime-v1"
    ):
        raise BundleError("incompatible_bundle_profile")
    inventory = manifest["files"]
    paths = [row["path"] for row in inventory]
    if len(set(paths)) != len(paths):
        raise BundleError("duplicate_inventory")
    if set(paths) != set(files) - {"manifest.json"}:
        raise BundleError("inventory_mismatch")
    for row in inventory:
        body = files[row["path"]]
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise BundleError("checksum_mismatch")
    coverage = manifest["coverage"]
    checkpoint = manifest["checkpoint"]
    if not coverage["complete"] or coverage["gaps"]:
        raise BundleError("incomplete_coverage")
    if not (
        utc(coverage["retained_from"])
        <= utc(coverage["through"])
        == utc(checkpoint["as_of"])
        <= utc(manifest["created_at"])
    ):
        raise BundleError("checkpoint_chronology")
    records, counts = [], Counter()
    for path, body in files.items():
        if path in {"manifest.json", "recovery/state.json"}:
            continue
        if not body.startswith(b"---\n") or b"\n---\n" not in body[4:]:
            raise BundleError("front_matter")
        header, text = body[4:].split(b"\n---\n", 1)
        meta = _json(header)
        _check("markdown-front-matter", meta, contracts)
        try:
            text.decode("utf-8")
        except UnicodeError as error:
            raise BundleError("markdown_encoding") from error
        if not re.fullmatch(PART_PATHS[meta["part"]], path):
            raise BundleError("record_path")
        # Inherited experience retains its source trader; the manifest identifies
        # the current checkpoint owner, not the author of every historical row.
        if not utc(coverage["retained_from"]) <= utc(meta["ts"]) <= utc(checkpoint["as_of"]):
            raise BundleError("record_chronology")
        counts[meta["part"]] += 1
        records.append(meta)
    for part in ("identity", "playbook", "working_memory"):
        if counts[part] != 1:
            raise BundleError("missing_mind_part")
    if Counter(coverage["record_counts"]) != counts:
        raise BundleError("record_coverage")
    ids = [row["record_id"] for row in records]
    if len(ids) != len(set(ids)):
        raise BundleError("duplicate_record")
    timestamps = {row["record_id"]: utc(row["ts"]) for row in records}
    for row in records:
        if any(
            ref not in timestamps or timestamps[ref] > utc(row["ts"]) for ref in row["evidence_ids"]
        ):
            raise BundleError("broken_evidence")
    if manifest["kind"] == "experience":
        if "recovery/state.json" in files:
            raise BundleError("experience_has_authority")
    else:
        if "recovery/state.json" not in files:
            raise BundleError("missing_recovery_state")
        state = _json(files["recovery/state.json"])
        _check("recovery-state", state, contracts)
        if any(state[key] != manifest[key] for key in ("trader_id", "lineage_id")) or (
            state["checkpoint_id"] != checkpoint["checkpoint_id"]
            or state["event_watermark"] != checkpoint["event_watermark"]
        ):
            raise BundleError("recovery_checkpoint")
        commands = {row["command_id"]: row for row in state["commands"]}
        if len(commands) != len(state["commands"]):
            raise BundleError("duplicate_command")
        if set(state["deduplication_ids"]) != set(commands):
            raise BundleError("deduplication_coverage")
        pending = {
            key
            for key, row in commands.items()
            if row["state"] in {"accepted", "dispatching", "unknown"}
        }
        if set(state["pending_command_ids"]) != pending:
            raise BundleError("pending_coverage")
    return ValidatedBundle(manifest, MappingProxyType(files), tuple(records))
