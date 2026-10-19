"""Lossless file-backed portability and adversarial archive rejection."""

import os
import struct
import tempfile
import zipfile
from contextlib import contextmanager

import pytest

from pipmind.contracts import ROOT, Contracts, loads, utc
from pipmind.portability import BundleError, validate_bundle
from pipmind.shadow.common import digest, encoded
from pipmind.shadow.store import metadata
from pipmind.shadow.stream_bundle import export_experience_file, import_experience_file
from pipmind.stream_portability import validate_stream

MOMENT = utc("2026-10-19T00:00:00Z")


@pytest.fixture
def tmp_path():
    # CI /tmp is intentionally tmpfs; the format must stage on real disk.
    folder = ROOT / ".data"
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stream-test-", dir=folder) as path:
        yield __import__("pathlib").Path(path)


class Source:
    trader = "synthetic"

    def __init__(self, large=False):
        self.contracts = Contracts()
        self.large = large

    @contextmanager
    def stream_retained(self):
        def records():
            for part, body in (
                ("identity", "Immutable identity\nnaïve ≠ unsafe\n"),
                ("playbook", "# Exact playbook\n"),
                (
                    "working_memory",
                    encoded({"views": [], "position_theses": [], "waiting_for": []}),
                ),
            ):
                yield metadata(part, part, MOMENT.isoformat(), self.trader, version="v1"), body
            for i in range(36 if self.large else 1):
                yield (
                    metadata(
                        f"journal:{i}", "journal", MOMENT.isoformat(), self.trader, ["identity"]
                    ),
                    "x" * (1000000 if self.large else 20),
                )

        yield {"lineage": "source-lineage", "revision": 3, "watermark": "2"}, records()


def rewrite(path, transform):
    with zipfile.ZipFile(path) as archive:
        files = {i.filename: archive.read(i) for i in archive.infolist()}
    transform(files)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)


def manifest_change(files, change):
    manifest = loads(files["manifest.json"])
    change(manifest)
    files["manifest.json"] = encoded(manifest).encode()


def test_streams_complete_history_beyond_v2_limit_without_changing_old_validator(tmp_path):
    path = tmp_path / "large.zip"
    source = Source(large=True)
    report = export_experience_file(source, path, MOMENT)
    assert report["complete"] and report["bytes"] > 33554432
    with validate_stream(path) as bundle:
        assert bundle.manifest["schema_version"] == 3
        with source.stream_retained() as (_, original):
            for expected, actual in zip(original, bundle.records(), strict=True):
                assert actual == expected
    with pytest.raises(BundleError, match="archive_size"):
        validate_bundle(path.read_bytes())
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "failure", ["checksum", "inventory", "coverage", "future", "evidence", "profile"]
)
def test_rejects_corrupt_incomplete_or_wrong_profile_before_import(tmp_path, failure):
    path = tmp_path / "source.zip"
    export_experience_file(Source(), path, MOMENT)

    def mutate(files):
        if failure == "checksum":
            files["mind/identity.md"] += b"tampered"
        elif failure == "inventory":
            files["extra.md"] = b"unlisted"
        elif failure == "coverage":
            manifest_change(files, lambda m: m["coverage"].update(complete=False))
        elif failure == "profile":
            manifest_change(files, lambda m: m.update(schema_version=2))
        else:
            name = "mind/identity.md"
            header, body = files[name][4:].split(b"\n---\n", 1)
            meta = loads(header)
            if failure == "future":
                meta["ts"] = "2026-10-20T00:00:00Z"
            else:
                meta["evidence_ids"] = ["missing"]
            files[name] = b"---\n" + encoded(meta).encode() + b"\n---\n" + body
            manifest_change(
                files,
                lambda m: next(r for r in m["files"] if r["path"] == name).update(
                    bytes=len(files[name]), sha256=digest(files[name])
                ),
            )

    rewrite(path, mutate)
    with pytest.raises(BundleError):
        with validate_stream(path):
            pytest.fail("must reject before destination mutation")


@pytest.mark.parametrize(
    "kind", ["traversal", "symlink", "duplicate", "deflated", "trailing", "preamble"]
)
def test_archive_ambiguity_and_unsafe_members_are_rejected(tmp_path, kind):
    path = tmp_path / "source.zip"
    export_experience_file(Source(), path, MOMENT)
    if kind in {"trailing", "preamble"}:
        content = path.read_bytes()
        path.write_bytes(content + b"hidden" if kind == "trailing" else b"hidden" + content)
    else:
        with zipfile.ZipFile(
            path, "a", compression=zipfile.ZIP_DEFLATED if kind == "deflated" else 0
        ) as archive:
            entry = zipfile.ZipInfo("../escape.md" if kind == "traversal" else "extra.md")
            if kind == "duplicate":
                entry.filename = "manifest.json"
            if kind == "symlink":
                entry.external_attr = 0o120777 << 16
            if kind == "deflated":
                entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, b"bad")
    with pytest.raises(BundleError):
        with validate_stream(path):
            pytest.fail("unsafe archive accepted")


def test_interrupted_export_publishes_nothing_and_never_overwrites(tmp_path):
    path = tmp_path / "target.zip"

    def interrupted():
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        export_experience_file(Source(), path, MOMENT, interrupt=interrupted)
    assert list(tmp_path.iterdir()) == []
    path.write_bytes(b"owner file")
    with pytest.raises(FileExistsError):
        export_experience_file(Source(), path, MOMENT)
    assert path.read_bytes() == b"owner file"


def test_import_uses_private_validated_copy_when_source_path_changes(tmp_path):
    path = tmp_path / "source.zip"
    export_experience_file(Source(), path, MOMENT)

    class Destination:
        contracts = Contracts()

        def clone_stream(self, bundle, *args, **kwargs):
            expected = list(bundle.records())
            path.write_bytes(b"replaced after validation")
            assert list(bundle.records()) == expected
            return {"transformations": [], "paused": True}

    assert import_experience_file(Destination(), path, MOMENT, "binding", "identity")["paused"]


def test_byte_validator_does_not_accept_even_small_v3(tmp_path):
    path = tmp_path / "small.zip"
    export_experience_file(Source(), path, MOMENT)
    with pytest.raises(BundleError):
        validate_bundle(path.read_bytes())


def test_declared_excessive_entry_count_is_refused_before_zipinfo_allocation(tmp_path):
    path = tmp_path / "many.zip"
    path.write_bytes(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 20001, 20001, 0, 0, 0))
    with pytest.raises(BundleError, match="entry_count"):
        with validate_stream(path):
            pytest.fail("oversized directory accepted")


def test_archive_above_explicit_256_mib_limit_is_refused(tmp_path):
    path = tmp_path / "oversized.zip"
    with open(path, "wb") as stream:
        stream.truncate(268435457)
    with pytest.raises(BundleError, match="archive_size"):
        with validate_stream(path):
            pytest.fail("oversized archive accepted")


def test_mismatched_local_header_cannot_hide_a_second_archive_layout(tmp_path):
    path = tmp_path / "layout.zip"
    export_experience_file(Source(), path, MOMENT)
    with open(path, "r+b") as stream:
        stream.seek(18)  # Local compressed size, central directory remains exact.
        stream.write(struct.pack("<L", 1))
    with pytest.raises(BundleError, match="zip_layout"):
        with validate_stream(path):
            pytest.fail("ambiguous local header accepted")


def test_operations_serialize_across_different_output_directories(tmp_path):
    from pipmind.shadow.stream_bundle import operation

    with operation(tmp_path / "first"):
        with pytest.raises(OSError):
            with operation(tmp_path / "second"):
                pytest.fail("concurrent memory-heavy operation admitted")
    with operation(tmp_path / "second"):
        pass


@pytest.mark.parametrize("version", [1, 2])
def test_headroom_guard_covers_both_hosted_cgroup_layouts(tmp_path, monkeypatch, version):
    from pathlib import Path

    from pipmind.shadow.stream_bundle import operation

    paths = (
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        )
        if version == 1
        else ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max")
    )
    values = {paths[0]: str(450 * 1048576), paths[1]: str(512 * 1048576)}
    exists, read = Path.exists, Path.read_text
    monkeypatch.setattr(
        Path,
        "exists",
        lambda p: (
            str(p).replace("\\", "/") in values
            or (False if str(p).replace("\\", "/").startswith("/sys/fs/cgroup") else exists(p))
        ),
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *a, **k: (
            values[str(p).replace("\\", "/")]
            if str(p).replace("\\", "/") in values
            else read(p, *a, **k)
        ),
    )
    with pytest.raises(BundleError, match="stream_memory_headroom"):
        with operation(tmp_path):
            pytest.fail("insufficient container headroom accepted")
