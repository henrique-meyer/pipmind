"""Exact complete-checkpoint size inspection without an archive or inventory list."""

import gc
import tempfile
import tracemalloc
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

from pipmind import stream_portability
from pipmind.contracts import ROOT, Contracts, loads, utc
from pipmind.portability import BundleError
from pipmind.shadow import stream_bundle
from pipmind.shadow.common import encoded
from pipmind.shadow.store import metadata

MOMENT = utc("2026-10-20T00:00:00Z")


class Source:
    trader = "synthetic-size-source"

    def __init__(self, journals=1, body="naïve ≠ unsafe 😀\n", target_bytes=None):
        self.contracts = Contracts()
        self.journals = journals
        self.body = body
        self.target_bytes = target_bytes
        self.opened = self.closed = self.yielded = 0
        self.head = {"lineage": "synthetic-lignée", "revision": 99, "watermark": "1020"}

    @contextmanager
    def stream_retained(self):
        self.opened += 1
        head = {**self.head, "checkpoint_isolation": "snapshot"}

        def records():
            for part, body in (
                ("identity", "Immutable identity\nnaïve ≠ unsafe\n"),
                ("playbook", "# Exact playbook\n"),
                (
                    "working_memory",
                    encoded({"views": [], "position_theses": [], "waiting_for": []}),
                ),
            ):
                self.yielded += 1
                yield metadata(part, part, MOMENT.isoformat(), self.trader, version="v1"), body
            for index in range(self.journals):
                meta = metadata(
                    "journal:é😀:" + str(index),
                    "journal",
                    MOMENT.isoformat(),
                    self.trader,
                    ["identity"],
                )
                body = self.body
                if self.target_bytes is not None:
                    padding = (
                        self.target_bytes - 9 - len(encoded(meta).encode()) - len(body.encode())
                    )
                    assert padding >= 0
                    body += "q" * padding
                self.yielded += 1
                yield meta, body

        try:
            yield head, records()
        finally:
            head["checkpoint_transaction_seconds"] = 0.125
            head["checkpoint_lock_seconds"] = 0.0
            self.closed += 1


@pytest.fixture
def tmp_path():
    # CI /tmp is tmpfs; existing export semantics require ordinary disk staging.
    folder = ROOT / ".data"
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stream-size-test-", dir=folder) as path:
        yield Path(path)


def inspect(source, **kwargs):
    return stream_bundle.inspect_experience_size(source, MOMENT, **kwargs)


@pytest.mark.parametrize("content_bytes", [999, 1000, 9999, 10000])
def test_inspection_matches_actual_zip_and_manifest_utf8_and_digit_boundaries(
    tmp_path, content_bytes
):
    source = Source(journals=2, target_bytes=content_bytes)
    before = deepcopy(source.head), source.body, source.journals
    report = inspect(source)
    path = tmp_path / "exact.pipmind"
    stream_bundle.export_experience_file(source, path, MOMENT)
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        raw_manifest = archive.read("manifest.json")
        manifest = loads(raw_manifest)
        assert report["archive_bytes"] == path.stat().st_size
        assert report["expanded_bytes"] == sum(row.file_size for row in entries)
        assert report["manifest_bytes"] == len(raw_manifest)
        assert report["inventory_bytes"] == len(encoded(manifest["files"]).encode())
        assert report["record_content_bytes"] == sum(
            row.file_size for row in entries if row.filename != "manifest.json"
        )
        assert report["maximum_record_bytes"] == content_bytes
        assert report["directory_bytes"] == path.stat().st_size - archive.start_dir - 22
        assert report["entries"] == len(entries) == report["records"] + 1
        assert report["record_counts"] == manifest["coverage"]["record_counts"]
        assert report["checkpoint"] == manifest["checkpoint"]
    assert report["complete_scan"] and report["size_limits_met"]
    assert (
        not report["validity_verified"]
        and not report["export_created"]
        and not report["clone_verified"]
    )
    assert report["inspection"] == "size_only" and report["schema_version"] == 3
    assert report["checkpoint_transaction_seconds"] == 0.125
    assert report["checkpoint_lock_seconds"] == 0.0
    assert report["checkpoint_isolation"] == "snapshot"
    assert (source.head, source.body, source.journals) == before
    assert source.opened == source.closed == 2


@pytest.mark.parametrize(
    "constant,key,code",
    [
        ("MAX_ENTRIES", "entries", "entry_count"),
        ("MAX_FILE_BYTES", "maximum_record_bytes", "file_size"),
        ("MAX_MANIFEST_BYTES", "manifest_bytes", "manifest_size"),
        ("MAX_TOTAL_BYTES", "archive_bytes", "archive_size"),
        ("MAX_DIRECTORY_BYTES", "directory_bytes", "directory_size"),
    ],
)
def test_limit_equality_accepts_actual_export_and_one_byte_or_entry_overflow_is_visible(
    tmp_path,
    monkeypatch,
    constant,
    key,
    code,
):
    source = Source(journals=2, target_bytes=1000)
    baseline = inspect(source)
    for module in [stream_bundle, stream_portability]:
        monkeypatch.setattr(module, constant, baseline[key])
    equality = inspect(source)
    assert equality["size_limits_met"] and equality["violations"] == []
    stream_bundle.export_experience_file(source, tmp_path / "equal.pipmind", MOMENT)
    for module in [stream_bundle, stream_portability]:
        monkeypatch.setattr(module, constant, baseline[key] - 1)
    overflow = inspect(source)
    assert overflow["complete_scan"] and not overflow["size_limits_met"]
    assert {"code": code, "actual": baseline[key], "limit": baseline[key] - 1} in overflow[
        "violations"
    ]
    assert overflow["records"] == baseline["records"]


def test_expanded_limit_includes_manifest_and_full_scan_continues_past_all_limits(monkeypatch):
    source = Source(journals=6, target_bytes=2000)
    baseline = inspect(source)
    for module in [stream_bundle, stream_portability]:
        monkeypatch.setattr(module, "MAX_TOTAL_BYTES", baseline["expanded_bytes"])
    equal = inspect(source)
    assert "expanded_size" not in {row["code"] for row in equal["violations"]}
    assert "archive_size" in {row["code"] for row in equal["violations"]}
    for name in [
        "MAX_TOTAL_BYTES",
        "MAX_FILE_BYTES",
        "MAX_MANIFEST_BYTES",
        "MAX_ENTRIES",
        "MAX_DIRECTORY_BYTES",
    ]:
        monkeypatch.setattr(stream_bundle, name, 1)
    measured = inspect(source)
    assert measured["records"] == 9 and measured["complete_scan"]
    assert {row["code"] for row in measured["violations"]} == {
        "expanded_size",
        "archive_size",
        "file_size",
        "manifest_size",
        "entry_count",
        "directory_size",
    }
    assert measured["archive_bytes"] == baseline["archive_bytes"]


def test_no_zip_staging_disk_check_or_body_digest_is_used(monkeypatch):
    source = Source(journals=3, body="Unique journal body " * 300)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Inspection must not create an archive, stage files or check disk space"
        )

    monkeypatch.setattr(stream_bundle.zipfile, "ZipFile", forbidden)
    monkeypatch.setattr(stream_bundle.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(stream_bundle.shutil, "disk_usage", forbidden)
    original_digest = stream_bundle.digest

    def short_digest(value):
        assert isinstance(value, str) and len(value) < 256
        return original_digest(value)

    monkeypatch.setattr(stream_bundle, "digest", short_digest)
    report = inspect(source)
    assert report["complete_scan"] and report["records"] == 6


def test_default_clock_is_captured_inside_snapshot_and_headroom_guard_precedes_it(monkeypatch):
    source = Source()
    clocks = []

    def current_time():
        assert source.opened == 1 and source.closed == 0
        clocks.append(True)
        return MOMENT

    monkeypatch.setattr(stream_bundle, "now", current_time)
    report = stream_bundle.inspect_experience_size(source)
    assert clocks == [True] and utc(report["checkpoint"]["as_of"]) == MOMENT

    def no_headroom():
        raise BundleError("stream_memory_headroom")

    monkeypatch.setattr(stream_bundle, "_memory_headroom", no_headroom)
    unopened = Source()
    with pytest.raises(BundleError, match="stream_memory_headroom"):
        stream_bundle.inspect_experience_size(unopened)
    assert unopened.opened == unopened.closed == 0


def test_inspection_serializes_with_existing_manual_operations_and_releases_after_error():
    with stream_bundle.exclusive_operation():
        with pytest.raises(OSError):
            inspect(Source())
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        inspect(
            Source(),
            interrupt=lambda: (_ for _ in ()).throw(RuntimeError("synthetic interruption")),
        )
    assert inspect(Source())["complete_scan"]


@pytest.mark.parametrize(
    "fault",
    [
        "future",
        "bad_meta",
        "bad_body",
        "invalid_utf8",
        "bad_clock",
        "missing_mind_part",
        "bad_head",
    ],
)
def test_corrupt_source_and_future_clock_abort_without_report_and_close_stream(fault):
    class InvalidSource(Source):
        @contextmanager
        def stream_retained(self):
            with super().stream_retained() as (head, rows):

                def invalid_rows():
                    for index, (meta, body) in enumerate(rows):
                        if index == 0:
                            if fault == "future":
                                meta["ts"] = "2026-10-21T00:00:00Z"
                            elif fault == "bad_meta":
                                meta["part"] = "not-a-part"
                            elif fault == "bad_body":
                                body = None
                            elif fault == "invalid_utf8":
                                body = "\ud800"
                            elif fault == "missing_mind_part":
                                continue
                        yield meta, body

                if fault == "bad_head":
                    head["revision"] = True
                yield head, invalid_rows()

    source = InvalidSource()
    result = None
    with pytest.raises(BundleError):
        result = stream_bundle.inspect_experience_size(
            source,
            "not a datetime" if fault == "bad_clock" else MOMENT,
        )
    assert result is None and source.opened == source.closed == 1
    assert inspect(Source())["complete_scan"]


def test_interruption_and_source_read_failure_close_the_complete_snapshot():
    source = Source(journals=10)
    callbacks = []

    def interrupt():
        callbacks.append(True)
        if len(callbacks) == 4:
            raise RuntimeError("interrupted complete scan")

    with pytest.raises(RuntimeError, match="interrupted complete scan"):
        inspect(source, interrupt=interrupt)
    assert source.yielded == 4 and source.opened == source.closed == 1

    class ReadFailure(Source):
        @contextmanager
        def stream_retained(self):
            with super().stream_retained() as (head, rows):

                def fail():
                    yield next(rows)
                    raise RuntimeError("synthetic source read failed")

                yield head, fail()

    failed = ReadFailure()
    with pytest.raises(RuntimeError, match="synthetic source read failed"):
        inspect(failed)
    assert failed.opened == failed.closed == 1


def test_valid_sizes_are_not_a_dependency_or_complete_export_certification():
    class MissingEvidence(Source):
        @contextmanager
        def stream_retained(self):
            with super().stream_retained() as (head, rows):

                def missing():
                    for meta, body in rows:
                        if meta["part"] == "journal":
                            meta["evidence_ids"] = ["unavailable-evidence"]
                        yield meta, body

                yield head, missing()

    result = inspect(MissingEvidence())
    assert result["size_limits_met"]
    assert (
        not result["validity_verified"]
        and not result["export_created"]
        and not result["clone_verified"]
    )


def test_oversized_record_is_measured_visibly_without_full_utf8_byte_copy():
    # Allocate source text before tracing; inspection should not duplicate it in bytes.
    body = "é" * 1048576
    source = Source(body=body)
    gc.collect()
    tracemalloc.start()
    try:
        result = inspect(source)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["complete_scan"] and not result["size_limits_met"]
    assert result["maximum_record_bytes"] > 2 * 1048576
    assert "file_size" in {row["code"] for row in result["violations"]}
    assert peak < 1048576  # A complete UTF-8 body copy alone would exceed 2 MiB.
    assert source.opened == source.closed == 1


def test_many_records_do_not_retain_inventory_or_paths(monkeypatch):
    source = Source(journals=5000, body="Small bounded record")
    monkeypatch.setattr(stream_bundle, "MAX_ENTRIES", 100)
    gc.collect()
    tracemalloc.start()
    try:
        result = inspect(source)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["records"] == 5003 and source.yielded == 5003 and result["complete_scan"]
    assert result["inventory_bytes"] > 900000
    assert not result["size_limits_met"] and peak < 1048576
    assert source.opened == source.closed == 1
