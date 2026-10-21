"""python -m pipmind.feeds replay | serve; live information reads are opt-in."""

import argparse
import json
import sys
from pathlib import Path

from pipmind.contracts import ContractError, data_dir, private_output_allowed
from pipmind.feeds.fixtures import process_checks, run_fixtures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["replay", "serve", "reprocess", "reconfigure"])
    parser.add_argument("--key")
    parser.add_argument("--previous-config")
    parser.add_argument("--config")
    parser.add_argument("--process-checks", action="store_true")
    parser.add_argument("--output", type=Path, default=data_dir() / "feed-replay")
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args(argv)
    try:
        if args.command == "reconfigure":
            import os

            from pipmind.feeds.config import load
            from pipmind.feeds.sql_store import reconfigure
            from pipmind.shadow.common import now
            from pipmind.shadow.sql import connector

            if os.environ.get("PIPMIND_FEED_BACKEND") != "sql":
                raise ContractError("feed_reconfiguration_requires_sql")
            if not args.previous_config or not args.config:
                raise ContractError("previous_and_replacement_configuration_required")
            reconfigure(connector(), load(args.previous_config), load(args.config), now())
            print("Feed configuration changed; source and cursors preserved. Restart workers.")
            return 0
        if args.command == "reprocess":
            import os

            from pipmind.feeds import config as recipes
            from pipmind.feeds.common import identity
            from pipmind.feeds.sql_store import configured_store
            from pipmind.feeds.store import encoded

            if not args.key:
                raise ContractError("quarantine_key_required")
            config = recipes.load(os.environ["PIPMIND_FEED_CONFIG"])
            store = configured_store(
                os.environ.get("PIPMIND_FEED_STORE", ".data/feeds/events.sqlite3"),
                profile=identity(encoded(config)),
            )
            if args.key.startswith("rest-page:"):
                from datetime import UTC, datetime

                from pipmind.feeds.backfill import reprocess_capture
                from pipmind.feeds.common import Reads
                from pipmind.feeds.macro import Calendar

                moment = datetime.now(UTC)
                calendar = Calendar(
                    store,
                    Reads("PipMind private reprocessing"),
                    config["calendar_url"],
                    lambda: moment,
                )
                queued = reprocess_capture(store, args.key, config, calendar, moment)
            else:
                queued = store.reprocess(args.key)
            print("Quarantine record queued" if queued else "No quarantined record queued")
            return 0
        if args.command == "serve":
            import uvicorn

            from pipmind.feeds.service import from_env

            uvicorn.run(
                from_env(), host=args.host, port=args.port, access_log=False, server_header=False
            )
            return 0
        output = args.output.resolve()
        if not private_output_allowed(output):
            raise ContractError("output_must_be_private")
        report = run_fixtures()
        if args.process_checks:
            report["process_checks"] = process_checks()
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "summary.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"Information feeds: {sum(report['checks'].values())}/{len(report['checks'])} checks; "
            "no network/trading."
        )
        return 0 if report["passed"] and all(report.get("process_checks", {}).values()) else 1
    except (ContractError, OSError, ValueError, KeyError, TypeError):
        print(
            "Feed checks failed: invalid configuration, fixture or durable state", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
