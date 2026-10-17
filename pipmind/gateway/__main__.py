"""python -m pipmind.gateway replay | serve (dry only)."""

import argparse
import json
import sys
from pathlib import Path

from pipmind.contracts import ROOT, ContractError
from pipmind.gateway.recording import run_recording


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["replay", "serve"])
    parser.add_argument("--output", type=Path, default=ROOT / ".data" / "gateway-replay")
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn

            from pipmind.gateway.service import from_env

            uvicorn.run(
                from_env(), host=args.host, port=args.port, access_log=False, server_header=False
            )
            return 0
        output = args.output.resolve()
        if output.is_relative_to(ROOT) and not any(
            output.is_relative_to(base) for base in (ROOT / ".data", ROOT / "evals" / "reports")
        ):
            raise ContractError("output_must_be_private")
        output.mkdir(parents=True, exist_ok=True)
        # Every invocation starts an independent synthetic session/ledger.
        report = run_recording()
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            f"Gateway recording: {sum(report['checks'].values())}/{len(report['checks'])} "
            f"checks passed; dispatches={report['dispatches']}"
        )
        return 0 if report["passed"] else 1
    except (ContractError, OSError, ValueError, KeyError):
        print(
            "Gateway failed: invalid configuration, recording or durable storage", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
