"""Explicit owner operations over a private shadow store; no trading commands."""

import argparse
import asyncio
import os

from pipmind.contracts import ContractError
from pipmind.shadow.bundle import export_experience, import_experience, write_private
from pipmind.shadow.common import ShadowError, bounded_json, digest, encoded, now, read_private
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import FoundryModel
from pipmind.shadow.sql import connector, fulltext, migrate
from pipmind.shadow.store import SqlMind


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
    clone = sub.add_parser("clone")
    clone.add_argument("--input", required=True)
    clone.add_argument("--identity", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--input", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8084)
    fixture = sub.add_parser("serve-fixture")
    fixture.add_argument("--host", default="127.0.0.1")
    fixture.add_argument("--port", type=int, default=8084)
    args = parser.parse_args()
    try:
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
            write_private(args.output, export_experience(store, now()))
        elif args.command == "clone":
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
