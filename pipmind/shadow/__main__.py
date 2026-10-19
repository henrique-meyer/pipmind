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
    maintenance = sub.add_parser("maintain-working")
    maintenance.add_argument("--plan", required=True)
    reconsider = sub.add_parser("reconsider")
    reconsider.add_argument("--event-id", required=True)
    robot = sub.add_parser("robot", help="Private robot library and owner controls")
    actions = robot.add_subparsers(dest="robot_action", required=True)
    for name in ("list", "blank", "cleanup"):
        actions.add_parser(name)
    save = actions.add_parser("save")
    save.add_argument("--source")
    save.add_argument("--name", default="")
    save.add_argument("--note", default="")
    imported = actions.add_parser("import")
    imported.add_argument("--input", required=True)
    imported.add_argument("--name", default="Imported robot")
    exported = actions.add_parser("export")
    exported.add_argument("--save", required=True)
    exported.add_argument("--output", required=True)
    preview = actions.add_parser("preview")
    preview.add_argument("--save", required=True)
    preview.add_argument("--output", required=True)
    load = actions.add_parser("load")
    load.add_argument("--preview", required=True, help="Previously reviewed private preview JSON")
    load.add_argument("--confirm-sha256", required=True)
    favorite = actions.add_parser("favorite")
    favorite.add_argument("--save", required=True)
    favorite.add_argument("--value", required=True, choices=("on", "off"))
    learn = actions.add_parser("auto-learn")
    learn.add_argument("--value", required=True, choices=("on", "off"))
    retention = actions.add_parser("retention")
    retention.add_argument("--days", required=True, type=int)
    retention.add_argument("--count", required=True, type=int)
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
        connect = connector()
        if args.command == "evaluate":
            from pipmind.dataset import load_cases
            from pipmind.shadow.evaluations import CASES, evaluate

            cases = load_cases(args.dataset)
            selected = set(args.cases.split(",")) if args.cases else CASES
            if not selected or not selected <= CASES:
                raise ShadowError("unsupported_shadow_evaluation_case")
            if selected - {c["case_id"] for c in cases}:
                raise ShadowError("missing_shadow_evaluation_case")
            report = asyncio.run(
                evaluate(
                    connect,
                    [c for c in cases if c["case_id"] in selected],
                    FoundryModel.environment(),
                    repetitions=args.repetitions,
                    manifest=bounded_json(read_private(args.manifest, 65536))
                    if args.manifest
                    else None,
                )
            )
            write_private(args.output, encoded(report).encode())
            print(encoded(report["summary"]))
            return
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
        if args.command == "robot":
            from pipmind.shadow.library import RobotLibrary
            from pipmind.shadow.robot import MAX_ARCHIVE

            library = RobotLibrary(store)
            action, result = args.robot_action, None
            if action == "list":
                result = library.status()
            elif action == "blank":
                result = {"saved": library.add_blank(now())}
            elif action == "save":
                result = {
                    "saved": library.save_copy(
                        now(), source=args.source, name=args.name, note=args.note
                    )
                }
            elif action == "import":
                result = {
                    "saved": library.import_archive(
                        read_private(args.input, MAX_ARCHIVE), now(), args.name
                    )
                }
            elif action == "export":
                write_private(args.output, library.export(args.save))
            elif action == "preview":
                write_private(args.output, encoded(library.preview(args.save, now())).encode())
            elif action == "load":
                plan = bounded_json(read_private(args.preview, 524288), 524288)
                result = library.load(plan["id"], args.confirm_sha256, now())
            elif action == "favorite":
                library.favorite(args.save, args.value == "on", now())
            elif action == "auto-learn":
                result = library.set_auto_learn(args.value == "on", now())
            elif action == "retention":
                library.retention(args.days, args.count, now())
            elif action == "cleanup":
                result = {"removed": library.cleanup(now())}
            if result is not None:
                print(encoded(result))
        elif args.command == "init":
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
