"""Synthetic >32 MiB SQL round trip in a 512 MiB container; bounded summary only."""

import hashlib
import json
import resource
import time
from pathlib import Path
from uuid import uuid4

from pipmind.contracts import stamp
from pipmind.shadow.common import digest, encoded, now
from pipmind.shadow.sql import connector
from pipmind.shadow.store import SqlMind, metadata
from pipmind.shadow.stream_bundle import export_experience_file, import_experience_file


def fingerprint(store, *, cloned=False):
    value, count = hashlib.sha256(), 0
    with store.stream_retained() as (_, records):
        for meta, body in records:
            if cloned and meta["part"] == "working_memory":
                continue
            if cloned and meta["record_id"] == "mind:working_memory":
                meta = {**meta, "part": "working_memory"}
            value.update(encoded([meta, body]).encode())
            count += 1
    return value.hexdigest(), count


def main():
    connect = connector()
    source = SqlMind(connect, "stream-source-" + uuid4().hex)
    start = now()
    source.initialize(
        "Synthetic identity",
        {"version": "v1", "text": "Exact synthetic playbook"},
        stamp(start),
        binding=digest("source-only"),
    )
    with source.transaction() as cur:
        for i in range(260):
            source._insert(
                cur,
                metadata(f"large:{i}", "journal", stamp(start), source.trader, ["mind:identity"]),
                f"Exact entry {i}\n" + "a" * 1000000,
            )
    original = fingerprint(source)
    before = source.head()
    path = Path("/exports") / (uuid4().hex + ".zip")
    started = time.monotonic()
    report = export_experience_file(source, path, now())
    exported = time.monotonic()
    destination = SqlMind(connect, "stream-destination-" + uuid4().hex)
    result = import_experience_file(
        destination, path, now(), digest("destination-only"), "Synthetic identity"
    )
    imported = time.monotonic()
    assert fingerprint(destination, cloned=True) == original
    assert source.head() == before and result["paused"]
    assert destination.head()["binding"] == digest("destination-only")
    assert destination.status()["outcomes"] == destination.status()["pending"] == 0
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    cgroup_peak = int(Path("/sys/fs/cgroup/memory.peak").read_text())
    assert 260000000 < report["bytes"] < 268435456 and peak_rss < 160 * 1048576
    assert cgroup_peak < 448 * 1048576
    summary = {
        "complete": True,
        "records": report["records"],
        "archive_bytes": report["bytes"],
        "exact_roundtrip": True,
        "source_unchanged": True,
        "clone_paused": True,
        "separate_authority": True,
        "dispatches": 0,
        "peak_process_bytes": peak_rss,
        "peak_container_bytes": cgroup_peak,
        "export_seconds": round(exported - started, 3),
        "import_seconds": round(imported - exported, 3),
        "checkpoint_lock_seconds": round(report["checkpoint_lock_seconds"], 3),
        "destination_transaction_seconds": round(result["destination_transaction_seconds"], 3),
        "export_temporary_disk_bytes": report["temporary_disk_bytes"],
        "import_temporary_disk_bytes": result["temporary_disk_bytes"],
    }
    path.unlink()
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
