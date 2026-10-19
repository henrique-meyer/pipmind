"""File-backed v3 experience validation; no extraction or mutable runtime state.

V3 deliberately uses a canonical stored ZIP. Legacy compressed v1/v2 archives
continue through their unchanged validator and limits.
"""

import hashlib
import os
import re
import sqlite3
import stat
import struct
import tempfile
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from pipmind.contracts import Contracts, utc
from pipmind.portability import PART_PATHS, BundleError, _check, _json

MAX_ENTRIES = 20000
MAX_TOTAL_BYTES = 268435456
MAX_FILE_BYTES = 1048576
MAX_MANIFEST_BYTES = 2097152
MAX_DIRECTORY_BYTES = 8 * 1048576


def release_cache(stream, *, written=False):
    """Do not let large manual operations crowd normal workers with file cache."""
    if written:
        stream.flush()
        os.fsync(stream.fileno())
    if hasattr(os, "posix_fadvise"):
        os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)


def _safe_name(name):
    if (
        len(name) > 256
        or not re.fullmatch(r"[a-z0-9][a-z0-9/._-]*", name)
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise BundleError("unsafe_path")


def _preflight(stream):
    """Bound ZipInfo allocation *before* zipfile parses any central directory."""
    stream.seek(0, 2)
    size = stream.tell()
    if not 22 <= size <= MAX_TOTAL_BYTES:
        raise BundleError("archive_size")
    stream.seek(size - 22)
    end = struct.unpack("<4s4H2LH", stream.read(22))
    if end[0] != b"PK\x05\x06" or end[1:3] != (0, 0) or end[3] != end[4] or end[7]:
        raise BundleError("zip_profile")
    count, directory_size, offset = end[4:7]
    if not 4 <= count <= MAX_ENTRIES or directory_size > MAX_DIRECTORY_BYTES:
        raise BundleError("entry_count")
    if offset + directory_size != size - 22:
        raise BundleError("zip_profile")
    stream.seek(offset)
    directory = stream.read(directory_size)
    cursor, total, entries = 0, 0, {}
    for _ in range(count):
        if cursor + 46 > len(directory):
            raise BundleError("invalid_zip")
        item = struct.unpack_from("<4s6H3L5H2L", directory, cursor)
        length, extra, comment = item[10:13]
        if (
            item[0] != b"PK\x01\x02"
            or item[3] & ~0x800
            or item[4] != zipfile.ZIP_STORED
            or extra
            or comment
            or item[13]
            or item[8] != item[9]
            or not 1 <= length <= 256
        ):
            raise BundleError("zip_profile")
        name = directory[cursor + 46 : cursor + 46 + length].decode("ascii")
        _safe_name(name)
        if name in entries:
            raise BundleError("duplicate_entry")
        if (
            stat.S_IFMT(item[15] >> 16) not in {0, stat.S_IFREG}
            or item[15] & 0x10
            or name.endswith("/")
        ):
            raise BundleError("unsafe_entry_type")
        limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES
        if item[9] > limit:
            raise BundleError("file_size")
        total += item[9]
        entries[name] = (item[16], item[3], item[4], item[7], item[8], item[9])
        cursor += 46 + length
    if cursor != len(directory) or total > MAX_TOTAL_BYTES:
        raise BundleError("expanded_size")
    expected = 0
    for name, item in sorted(entries.items(), key=lambda row: row[1][0]):
        if item[0] != expected:
            raise BundleError("zip_layout")
        stream.seek(expected)
        header = stream.read(30)
        if len(header) != 30:
            raise BundleError("invalid_zip")
        local = struct.unpack("<4s5H3L2H", header)
        if (
            local[0] != b"PK\x03\x04"
            or local[2:4] != item[1:3]
            or local[6:9] != item[3:6]
            or local[10]
            or stream.read(local[9]) != name.encode("ascii")
        ):
            raise BundleError("zip_layout")
        expected += 30 + local[9] + item[5]
    if expected != offset:
        raise BundleError("zip_layout")


def record(content, path, contracts):
    if not content.startswith(b"---\n") or b"\n---\n" not in content[4:]:
        raise BundleError("front_matter")
    header, body = content[4:].split(b"\n---\n", 1)
    if len(header) > 262144:
        raise BundleError("front_matter_size")
    meta = _json(header)
    _check("markdown-front-matter", meta, contracts)
    if len(meta["record_id"].encode("utf-16-le")) > 512:
        raise BundleError("record_id_size")
    if not re.fullmatch(PART_PATHS[meta["part"]], path):
        raise BundleError("record_path")
    return meta, body.decode("utf-8")


class StreamBundle:
    def __init__(self, archive, manifest, contracts):
        self.archive, self.manifest, self.contracts = archive, manifest, contracts

    def records(self):
        # Recheck payloads during consumption, even after complete validation.
        for row in self.manifest["files"]:
            content = self.archive.read(row["path"])
            release_cache(self.archive.fp)
            if len(content) != row["bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise BundleError("checksum_mismatch")
            yield record(content, row["path"], self.contracts)


def _validate(bundle, index):
    manifest = bundle.manifest
    _check("bundle-stream-manifest", manifest, bundle.contracts)
    paths = [row["path"] for row in manifest["files"]]
    if len(set(paths)) != len(paths):
        raise BundleError("duplicate_inventory")
    if set(paths) != set(bundle.archive.namelist()) - {"manifest.json"}:
        raise BundleError("inventory_mismatch")
    coverage, checkpoint = manifest["coverage"], manifest["checkpoint"]
    start, end = utc(coverage["retained_from"]), utc(checkpoint["as_of"])
    if not coverage["complete"] or coverage["gaps"]:
        raise BundleError("incomplete_coverage")
    if not start <= end == utc(coverage["through"]) <= utc(manifest["created_at"]):
        raise BundleError("checkpoint_chronology")
    counts = Counter()
    index.executescript(
        "PRAGMA temp_store=FILE; PRAGMA cache_size=-2048; PRAGMA journal_mode=OFF;"
        "PRAGMA synchronous=OFF; PRAGMA max_page_count=131072;"
        "CREATE TABLE records(id TEXT PRIMARY KEY,ts TEXT NOT NULL);"
        "CREATE TABLE refs(owner TEXT,ref TEXT,ts TEXT);"
    )
    for meta, _body in bundle.records():
        when = utc(meta["ts"])
        if not start <= when <= end:
            raise BundleError("record_chronology")
        timestamp = when.isoformat(timespec="microseconds")
        index.execute("INSERT INTO records VALUES(?,?)", (meta["record_id"], timestamp))
        index.executemany(
            "INSERT INTO refs VALUES(?,?,?)",
            ((meta["record_id"], ref, timestamp) for ref in meta["evidence_ids"]),
        )
        counts[meta["part"]] += 1
    if any(counts[part] != 1 for part in ("identity", "playbook", "working_memory")):
        raise BundleError("missing_mind_part")
    if Counter(coverage["record_counts"]) != counts:
        raise BundleError("record_coverage")
    if index.execute(
        "SELECT 1 FROM refs LEFT JOIN records ON records.id=refs.ref "
        "WHERE records.id IS NULL OR records.ts>refs.ts LIMIT 1"
    ).fetchone():
        raise BundleError("broken_evidence")


@contextmanager
def validate_stream(path, *, contracts=None, staging=None):
    """Own a private bounded copy until validation and destination commit finish."""
    contracts = contracts or Contracts()
    try:
        with tempfile.TemporaryDirectory(prefix="pipmind-bundle-", dir=staging) as folder:
            os.chmod(folder, 0o700)
            payload = Path(folder) / "payload.zip"
            with open(path, "rb") as source, open(payload, "xb") as target:
                os.chmod(payload, 0o600)
                total = 0
                while chunk := source.read(MAX_FILE_BYTES):
                    total += len(chunk)
                    if total > MAX_TOTAL_BYTES:
                        raise BundleError("archive_size")
                    target.write(chunk)
                    if total % (8 * MAX_FILE_BYTES) == 0:
                        release_cache(target, written=True)
                        release_cache(source)
                release_cache(target, written=True)
                release_cache(source)
            with open(payload, "rb") as stream:
                _preflight(stream)
                with zipfile.ZipFile(stream) as archive:
                    if "manifest.json" not in archive.namelist():
                        raise BundleError("missing_manifest")
                    bundle = StreamBundle(archive, _json(archive.read("manifest.json")), contracts)
                    index = sqlite3.connect(Path(folder) / "relationships.sqlite")
                    try:
                        _validate(bundle, index)
                        yield bundle
                    finally:
                        index.close()
    except sqlite3.IntegrityError as error:
        raise BundleError("duplicate_record") from error
    except (zipfile.BadZipFile, UnicodeError, EOFError, struct.error, sqlite3.Error) as error:
        raise BundleError("invalid_stream_bundle") from error
