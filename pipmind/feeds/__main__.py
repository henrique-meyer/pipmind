"""python -m pipmind.feeds replay | serve; live information reads are opt-in."""

import argparse
import json
import sys
from pathlib import Path

from pipmind.contracts import ROOT, ContractError
from pipmind.feeds.fixtures import process_checks, run_fixtures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["replay", "serve"])
    parser.add_argument("--process-checks", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".data" / "feed-replay")
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn

            from pipmind.feeds.service import from_env

            uvicorn.run(
                from_env(), host=args.host, port=args.port, access_log=False, server_header=False
            )
            return 0
        output = args.output.resolve()
        if output.is_relative_to(ROOT) and not any(
            output.is_relative_to(p) for p in (ROOT / ".data", ROOT / "evals/reports")
        ):
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
