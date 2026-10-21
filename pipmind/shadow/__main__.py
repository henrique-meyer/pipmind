"""Explicit owner operations over a private shadow store; no trading commands."""

import argparse
import asyncio
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from pipmind.contracts import ContractError, private_output_allowed
from pipmind.shadow.bundle import export_experience, import_experience, write_private
from pipmind.shadow.common import ShadowError, bounded_json, digest, encoded, now, read_private
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import FoundryModel
from pipmind.shadow.sql import connector, fulltext, migrate
from pipmind.shadow.store import SqlMind


class EvaluationReport:
    """Reserve one new private report and atomically checkpoint only our file."""

    def __init__(self, path):
        supplied = Path(path).absolute()
        if supplied.is_symlink():
            raise ShadowError("evaluation_output_exists")
        self.path = supplied.parent.resolve() / supplied.name
        if not private_output_allowed(self.path):
            raise ShadowError("output_must_be_private")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = encoded({"partial": True, "status": "reserved", "results": []}).encode()
        fd, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                identity = self._identity(os.fstat(stream.fileno()))
            # Link publishes complete bytes atomically and refuses every existing
            # directory entry, including a dangling symlink. Same-directory
            # staging keeps both names on one filesystem; failure precedes SQL.
            os.link(temporary, self.path)
            self.identity = identity
            self.checksum = hashlib.sha256(content).digest()
        finally:
            os.unlink(temporary)

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def _owned(self):
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or self._identity(info) != self.identity:
                return False
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                return (
                    self._identity(os.fstat(stream.fileno())) == self.identity
                    and hashlib.file_digest(stream, "sha256").digest() == self.checksum
                    and self._identity(self.path.lstat()) == self.identity
                )
        except OSError:
            return False

    def write(self, report):
        if not self._owned():
            raise ShadowError("evaluation_output_changed")
        content = encoded(report).encode()
        fd, temporary = tempfile.mkstemp(prefix="." + self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                identity = self._identity(os.fstat(stream.fileno()))
            if not self._owned():
                raise ShadowError("evaluation_output_changed")
            os.replace(temporary, self.path)
            temporary = None
            self.identity = identity
            self.checksum = hashlib.sha256(content).digest()
        finally:
            if temporary is not None:
                os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="PipMind shadow-only runtime and SQL mind")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("replay")
    init = sub.add_parser("init")
    init.add_argument("--identity", required=True)
    init.add_argument("--playbook", required=True)
    init.add_argument("--version", required=True)
    for name in ("status", "pause", "resume-shadow", "run-once"):
        sub.add_parser(name)
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)
    export.add_argument("--format", choices=("v2", "stream-v3"), default="v2")
    size = sub.add_parser("export-size", help="Inspect stream-v3 sizes without creating an export")
    size.add_argument("--output", required=True)
    clone = sub.add_parser("clone")
    clone.add_argument("--input", required=True)
    clone.add_argument("--identity", required=True)
    clone.add_argument("--format", choices=("v2", "stream-v3"), default="v2")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--input", required=True)
    maintenance = sub.add_parser("maintain-working")
    maintenance.add_argument("--plan", required=True)
    reconsider = sub.add_parser("reconsider")
    reconsider.add_argument("--event-id", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8084)
    fixture = sub.add_parser("serve-fixture")
    fixture.add_argument("--host", default="127.0.0.1")
    fixture.add_argument("--port", type=int, default=8084)
    evaluation = sub.add_parser("evaluate")
    evaluation.add_argument("--dataset", required=True)
    evaluation.add_argument("--cases")
    evaluation.add_argument("--repetitions", type=int, default=3)
    evaluation.add_argument("--manifest")
    evaluation.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "export-size":
            from pipmind.shadow.stream_bundle import private_path

            output = private_path(args.output)
            if output.exists():
                raise ShadowError("output_already_exists")
        if args.command == "evaluate":
            from pipmind.dataset import load_cases
            from pipmind.shadow.evaluations import CASES, evaluate

            cases = load_cases(args.dataset)
            selected = set(args.cases.split(",")) if args.cases else CASES
            if not selected or not selected <= CASES:
                raise ShadowError("unsupported_shadow_evaluation_case")
            if selected - {c["case_id"] for c in cases}:
                raise ShadowError("missing_shadow_evaluation_case")
            manifest = bounded_json(read_private(args.manifest, 65536)) if args.manifest else None
            output = EvaluationReport(args.output)
            connect = connector()
            report = asyncio.run(
                evaluate(
                    connect,
                    [c for c in cases if c["case_id"] in selected],
                    FoundryModel.environment(),
                    repetitions=args.repetitions,
                    manifest=manifest,
                    progress=lambda report: output.write({**report, "partial": True}),
                )
            )
            output.write(report)
            print(encoded(report["summary"]))
            return
        connect = connector()
        if args.command == "replay":
            from pipmind.shadow.fixtures import replay

            print(encoded(asyncio.run(replay(connect))))
            return
        if args.command == "migrate":
            migrate(connect)
            fulltext(connect)
            print("PASS: SQL schema and full-text index ready")
            return
        store = SqlMind(connect, os.environ["PIPMIND_SHADOW_TRADER"])
        if args.command == "init":
            store.initialize(
                read_private(args.identity, 32768).decode("utf-8"),
                {
                    "text": read_private(args.playbook, 32768).decode("utf-8"),
                    "version": args.version,
                },
                now().isoformat(),
                binding=digest(os.environ["PIPMIND_SHADOW_BINDING_REF"]),
            )
        elif args.command == "status":
            print(encoded(store.status()))
        elif args.command in {"pause", "resume-shadow"}:
            store.set_paused(args.command == "pause")
        elif args.command == "export":
            if args.format == "stream-v3":
                from pipmind.shadow.stream_bundle import export_experience_file

                print(encoded(export_experience_file(store, args.output)))
            else:
                write_private(args.output, export_experience(store, now()))
        elif args.command == "export-size":
            from pipmind.shadow.stream_bundle import inspect_experience_size

            report = inspect_experience_size(store)
            write_private(output, encoded(report).encode())
            print(
                encoded(
                    {
                        "inspection": "size_only",
                        "complete_scan": report["complete_scan"],
                        "size_limits_met": report["size_limits_met"],
                        "validity_verified": False,
                        "export_created": False,
                        "report_saved": True,
                    }
                )
            )
            if not report["size_limits_met"]:
                parser.exit(
                    2, "Size inspection completed; current stream-v3 limits are exceeded.\n"
                )
        elif args.command == "clone":
            if args.format == "stream-v3":
                from pipmind.shadow.stream_bundle import import_experience_file

                report = import_experience_file(
                    store,
                    args.input,
                    now(),
                    digest(os.environ["PIPMIND_SHADOW_BINDING_REF"]),
                    read_private(args.identity, 32768).decode("utf-8"),
                )
                print(encoded(report))
                return
            report = import_experience(
                store,
                read_private(args.input, 33554432),
                now(),
                digest(os.environ["PIPMIND_SHADOW_BINDING_REF"]),
                read_private(args.identity, 32768).decode("utf-8"),
            )
            print(encoded(report))
        elif args.command == "ingest":
            page = bounded_json(read_private(args.input, 262144))
            if not isinstance(page, dict) or set(page) != {"source", "events"}:
                raise ShadowError("invalid_input_page")
            store.accept(page["source"], page["events"], now())
        elif args.command == "maintain-working":
            print(
                encoded(
                    store.maintain_working(bounded_json(read_private(args.plan, 131072)), now())
                )
            )
        elif args.command == "reconsider":
            print(
                encoded(
                    {
                        "event_id": store.queue_reconsideration(args.event_id, now()),
                        "paused": True,
                        "dispatches": 0,
                    }
                )
            )
        elif args.command == "run-once":
            asyncio.run(ShadowEngine(store, FoundryModel.environment()).step())
        else:
            import uvicorn

            from pipmind.shadow.service import from_env

            app = from_env() if args.command == "serve" else None
            if args.command == "serve-fixture":
                from pipmind.shadow.fixtures import app as fixture_app

                app = fixture_app(connect)
            uvicorn.run(app, host=args.host, port=args.port, server_header=False, access_log=False)
    except (ShadowError, ContractError, ValueError, OSError, KeyError, TypeError):
        parser.exit(
            1, "Shadow operation refused; check private configuration and service status.\n"
        )


if __name__ == "__main__":
    main()
