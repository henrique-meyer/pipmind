"""Explicit v3 owner operations with bounded file, ZIP and SQL iteration."""

import os
import shutil
import tempfile
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pipmind.contracts import private_output_allowed, stamp, utc
from pipmind.portability import BundleError
from pipmind.shadow.bundle import RETRIEVAL_VERSION, SUPPORTED_CONTEXTS
from pipmind.shadow.common import ShadowError, digest, encoded, now
from pipmind.shadow.model import PROMPT_VERSION
from pipmind.stream_portability import (
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_TOTAL_BYTES,
    release_cache,
    validate_stream,
)


def private_path(path):
    path = Path(path).resolve()
    if not private_output_allowed(path):
        raise ShadowError("output_must_be_private")
    return path


@contextmanager
def exclusive_operation():
    # One operation across output directories, not merely one per file path.
    owner = str(os.getuid()) if hasattr(os, "getuid") else digest(str(Path.home()))[:16]
    lock = Path(tempfile.gettempdir()) / f"pipmind-stream-{owner}.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if hasattr(os, "getuid") and os.fstat(fd).st_uid != os.getuid():
            raise BundleError("stream_lock_owner")
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        # Closing releases the advisory lock after failures/process termination.
        # Retain the inode so another process cannot race through a replacement.
        os.close(fd)


@contextmanager
def operation(folder):
    """Refuse insufficient temporary headroom; never resize or pause a source."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    mounts = Path("/proc/self/mountinfo")
    if mounts.exists():
        matches = []
        for line in mounts.read_text().splitlines():
            fields, filesystem = line.split(" - ", 1)
            mount = Path(fields.split()[4].replace(r"\040", " "))
            if folder.resolve().is_relative_to(mount):
                matches.append((len(mount.parts), filesystem.split()[0]))
        if matches and max(matches)[1] in {"tmpfs", "ramfs"}:
            raise BundleError("stream_requires_disk_staging")
    if shutil.disk_usage(folder).free < 4 * MAX_TOTAL_BYTES:
        raise BundleError("stream_disk_headroom")
    for usage_path, limit_path in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    ):
        current, limit = Path(usage_path), Path(limit_path)
        if current.exists() and limit.exists() and limit.read_text().strip() != "max":
            if int(limit.read_text()) - int(current.read_text()) < 96 * 1048576:
                raise BundleError("stream_memory_headroom")
            break
    with exclusive_operation():
        with tempfile.TemporaryDirectory(prefix="pipmind-stream-", dir=folder) as private:
            os.chmod(private, 0o700)
            yield Path(private)


def _path(meta):
    part = meta["part"]
    if part in {"identity", "playbook", "working_memory"}:
        return "mind/" + {"working_memory": "working-memory"}.get(part, part) + ".md"
    folder = {
        "journal": "journal",
        "memory": "memories",
        "review": "reviews",
        "playbook_history": "playbook-history",
    }[part]
    return f"mind/{folder}/{digest(meta['record_id'])}.md"


def export_experience_file(store, path, moment=None, *, interrupt=None):
    """Atomic no-overwrite publication after complete validation of a SQL checkpoint."""
    path = private_path(path)
    with operation(path.parent) as folder:
        staged = folder / "export.zip"
        inventory, counts, earliest, total = [], Counter(), None, 0
        # A seekable private file avoids ZIP data descriptors and in-memory copies.
        with open(staged, "xb") as output:
            os.chmod(staged, 0o600)
            with zipfile.ZipFile(
                output, "w", compression=zipfile.ZIP_STORED, allowZip64=False
            ) as archive:
                with store.stream_retained() as (head, records):
                    moment = moment or now()
                    for meta, body in records:
                        when = utc(meta["ts"])
                        if when > moment:
                            raise BundleError("export_clock_before_checkpoint")
                        earliest = when if earliest is None else min(earliest, when)
                        content = b"---\n" + encoded(meta).encode() + b"\n---\n" + body.encode()
                        if len(content) > MAX_FILE_BYTES:
                            raise BundleError("file_size")
                        if len(inventory) >= MAX_ENTRIES - 1:
                            raise BundleError("entry_count")
                        name = _path(meta)
                        total += len(content)
                        if total > MAX_TOTAL_BYTES:
                            raise BundleError("expanded_size")
                        archive.writestr(name, content)
                        if len(inventory) % 8 == 0:
                            release_cache(output, written=True)
                        inventory.append(
                            {"path": name, "bytes": len(content), "sha256": digest(content)}
                        )
                        counts[meta["part"]] += 1
                        if output.tell() > MAX_TOTAL_BYTES:
                            raise BundleError("archive_size")
                        if interrupt:
                            interrupt()
                manifest = {
                    "format": "pipmind",
                    "schema_version": 3,
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
                        "retained_from": stamp(earliest),
                        "through": stamp(moment),
                        "gaps": [],
                        "record_counts": {
                            part: counts[part]
                            for part in (
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
                        "compatibility_decision": "shadow-stream-v1",
                    },
                    "files": inventory,
                }
                payload = encoded(manifest).encode()
                if len(payload) > MAX_MANIFEST_BYTES or total + len(payload) > MAX_TOTAL_BYTES:
                    raise BundleError("manifest_size")
                archive.writestr("manifest.json", payload)
            output.flush()
            os.fsync(output.fileno())
            release_cache(output)
        with validate_stream(staged, contracts=store.contracts, staging=folder) as bundle:
            temporary_bytes = staged.stat().st_size + bundle.validation_disk_bytes
        # Atomic link fails if destination exists, unlike replace/rename on Unix.
        os.link(staged, path)
        return {
            "schema_version": 3,
            "records": len(inventory),
            "bytes": path.stat().st_size,
            "checkpoint": manifest["checkpoint"]["checkpoint_id"],
            "complete": True,
            "temporary_disk_bytes": temporary_bytes,
            "checkpoint_lock_seconds": head.get("checkpoint_lock_seconds"),
            "checkpoint_transaction_seconds": head.get("checkpoint_transaction_seconds"),
            "checkpoint_isolation": head.get("checkpoint_isolation"),
        }


def import_experience_file(store, path, moment, binding, expected_identity, *, interrupt=None):
    path = private_path(path)
    with operation(path.parent) as folder:
        with validate_stream(path, contracts=store.contracts, staging=folder) as bundle:
            compatibility = bundle.manifest["compatibility"]
            if (
                compatibility["prompt_version"],
                compatibility["retrieval_version"],
            ) not in SUPPORTED_CONTEXTS or utc(bundle.manifest["checkpoint"]["as_of"]) > moment:
                raise BundleError("unsupported_experience_compatibility")
            report = store.clone_stream(
                bundle, moment, binding, expected_identity, interrupt=interrupt
            )
            report["temporary_disk_bytes"] = bundle.validation_disk_bytes
            if compatibility["prompt_version"] != PROMPT_VERSION:
                report["transformations"].append("legacy_context_retrieval_rebuilt")
            return report
