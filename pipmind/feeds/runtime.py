"""Three isolated workers with independent backoff, restarts and private heartbeats."""

import multiprocessing
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pipmind.contracts import ContractError, stamp
from pipmind.feeds.common import Reads, url
from pipmind.feeds.macro import Calendar, Macro
from pipmind.feeds.news import News
from pipmind.feeds.social import Social, XReads
from pipmind.feeds.sql_store import configured_store
from pipmind.feeds.stream import stream_once


class Stop:
    """Per-process stop file avoids shared semaphore corruption on SIGKILL."""

    def __init__(self, path):
        self.path = Path(path)

    def is_set(self):
        return self.path.exists()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        return self.is_set()


def worker(producer, path, config, mode, profile, stop_path):
    try:
        _worker(producer, path, config, mode, profile, stop_path)
    except (ContractError, OSError):
        # The supervisor retries. Never dump driver exceptions/private recipes.
        raise SystemExit(1) from None


def _worker(producer, path, config, mode, profile, stop_path):
    stop = Stop(stop_path)
    store = configured_store(path, mode=mode, profile=profile)

    def now():
        return datetime.now(UTC)

    reads = Reads(os.environ.get("PIPMIND_FEED_USER_AGENT", "PipMind/0.1 (private feed worker)"))
    calendar = Calendar(store, reads, config["calendar_url"], now)
    enabled = os.environ.get("PIPMIND_FEED_READS") == "true" and mode != "fixture"
    step = None
    if enabled:
        if producer == "macro":
            step = Macro(store, calendar, config["macro"], now)
            if os.environ.get("PIPMIND_FXMD_API_KEY") and config["macro"]:
                api_root = url(
                    os.environ.get("PIPMIND_FXMD_API_ROOT", "https://api.fxmacrodata.com")
                )

                def streaming():
                    backoff = 1
                    while not stop.is_set():
                        try:
                            stream_once(
                                store,
                                reads,
                                api_root,
                                os.environ["PIPMIND_FXMD_API_KEY"],
                                config["macro"],
                                now,
                                stop,
                            )
                            backoff = 1
                        except (ContractError, OSError):
                            store.alert("macro", "macro_stream_unavailable", now())
                            backoff = min(60, backoff * 2)
                        stop.wait(backoff)

                threading.Thread(target=streaming, daemon=True, name="macro-sse").start()
            if os.environ.get("PIPMIND_CANONICAL_READS") == "true":
                from pipmind.feeds.backfill import CanonicalBackfill

                def backfill():
                    source = CanonicalBackfill(
                        store,
                        reads,
                        os.environ.get("PIPMIND_FXMD_API_ROOT", "https://api.fxmacrodata.com"),
                        os.environ.get("PIPMIND_FXMD_API_KEY", ""),
                        calendar,
                        now,
                    )
                    while not stop.is_set():
                        for mapping in config["macro"]:
                            if stop.is_set():
                                return
                            try:
                                if mapping.get("bootstrap_latest", False):
                                    source.latest(mapping)
                                source.page(
                                    mapping,
                                    os.environ["PIPMIND_MACRO_BACKFILL_START"],
                                    now().date().isoformat(),
                                )
                            except (ContractError, OSError, KeyError, ValueError):
                                store.alert("macro", "canonical_backfill_failed", now())
                        stop.wait(30)

                threading.Thread(target=backfill, daemon=True, name="macro-backfill").start()
        elif producer == "news":
            step = News(store, reads, config["news"], now)
        else:
            token = os.environ.get("PIPMIND_X_READ_TOKEN")
            step = Social(
                store,
                reads,
                XReads(reads, token, store=store, now=now) if token else None,
                config["roles"],
                calendar,
                now,
            )
    while not stop.is_set():
        current = now()
        try:
            if mode == "fixture":
                from pipmind.feeds.fixtures import pulse

                with store.transaction() as db:
                    store.emit(db, producer, pulse(producer, current), current)
            elif producer == "macro":
                if enabled:
                    calendar.refresh()
                Macro(store, calendar, config["macro"], now).drain()
            elif producer == "news":
                (step or News(store, reads, [], now)).step()
            elif step:
                step.step()
            store.save(
                "worker:" + producer,
                {
                    "pid": os.getpid(),
                    "heartbeat": stamp(current),
                    "reads_enabled": enabled,
                    "error": None,
                },
            )
        except (ContractError, OSError, KeyError, TypeError):
            store.alert(producer, "feed_worker_step_failed", current)
            store.save(
                "worker:" + producer,
                {
                    "pid": os.getpid(),
                    "heartbeat": stamp(current),
                    "reads_enabled": enabled,
                    "error": "feed_worker_step_failed",
                },
            )
            stop.wait(5)
        stop.wait(0.5 if mode == "fixture" else 1)


class Supervisor:
    def __init__(self, path, config, *, mode="local-read", profile="synthetic"):
        self.path, self.config, self.mode, self.profile = str(path), config, mode, profile
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A pristine fork server shares read-only dependency pages on Linux.
        # Never fork this ASGI process: it already owns threads, SQL and HTTP
        # clients. Preload imports only; workers create their own connections.
        method = (
            "forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn"
        )
        if method == "forkserver":
            multiprocessing.set_forkserver_preload(
                ["pipmind.feeds.runtime", "azure.identity", "pytds", "OpenSSL", "service_identity"]
            )
        self.context = multiprocessing.get_context(method)
        self.stop_paths = {}
        self.processes, self.restarts, self.next_start = {}, {}, {}
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.thread = None

    def tick(self):
        with self.lock:
            for producer in ("macro", "news", "social"):
                process = self.processes.get(producer)
                if process and process.is_alive():
                    continue
                if time.monotonic() < self.next_start.get(producer, 0):
                    continue
                if process:
                    process.join(timeout=0)
                    self.restarts[producer] = self.restarts.get(producer, 0) + 1
                stop_path = Path(self.path).parent / (".stop-" + uuid.uuid4().hex)
                process = self.context.Process(
                    target=worker,
                    name="pipmind-" + producer,
                    args=(
                        producer,
                        self.path,
                        self.config,
                        self.mode,
                        self.profile,
                        str(stop_path),
                    ),
                )
                process.start()
                self.stop_paths[producer] = stop_path
                self.processes[producer] = process
                self.next_start[producer] = time.monotonic() + min(
                    30, 2 ** min(5, self.restarts.get(producer, 0))
                )

    def start(self):
        self.tick()

        def supervise():
            while not self.closed.wait(0.25):
                self.tick()

        self.thread = threading.Thread(target=supervise, name="feed-supervisor", daemon=True)
        self.thread.start()

    def status(self):
        with self.lock:
            return {
                name: {"pid": p.pid, "alive": p.is_alive(), "restarts": self.restarts.get(name, 0)}
                for name, p in self.processes.items()
            }

    def stop(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=2)
        for path in self.stop_paths.values():
            path.touch()
        for process in self.processes.values():
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        for path in self.stop_paths.values():
            path.unlink(missing_ok=True)
