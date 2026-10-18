"""Render only the sanitized replay summary as a GitHub job summary."""

import sys
from pathlib import Path

from pipmind.contracts import loads


def render(path):
    path = Path(path)
    if path.stat().st_size > 65_536:
        raise ValueError("summary_size_limit")
    report = loads(path.read_bytes())
    counts = report["counts"]
    lines = [
        "## Replay evaluation",
        "",
        f"{counts['passed']} passed · {counts['failed']} failed · {counts['deferred']} deferred",
        "",
        "| Area | Passed | Failed | Deferred |",
        "| --- | ---: | ---: | ---: |",
    ]
    for area, values in sorted(report["areas"].items()):
        lines.append(f"| {area} | {values['passed']} | {values['failed']} | {values['deferred']} |")
    lines += [
        "",
        f"Fixture week: {'passed' if report['fixture_week']['passed'] else 'FAILED'}.",
        f"Portability contracts: {sum(row['passed'] for row in report['portability'])}/"
        f"{len(report['portability'])}.",
        f"Dry gateway: {'passed' if report['gateway']['passed'] else 'FAILED'} "
        f"({sum(report['gateway']['checks'].values())}/{len(report['gateway']['checks'])}); "
        f"dispatches: {report['gateway']['dispatches']}.",
        f"Information feeds: {'passed' if report['feeds']['passed'] else 'FAILED'} "
        f"({sum(report['feeds']['checks'].values())}/{len(report['feeds']['checks'])}).",
        "Baseline differences: " + (", ".join(report["baseline_differences"]) or "none") + ".",
        "",
        "### Deferred real-path coverage",
        "",
        "These fixtures do not certify controls that have not been implemented.",
        "",
    ]
    for row in report["cases"]:
        if row["status"] == "deferred":
            lines.append(
                f"- {row['case_id']} — M{row['required_milestone']}: "
                + ", ".join(row["missing_capabilities"])
            )
        elif row["failures"]:
            lines.append(f"- FAILED {row['case_id']}: " + ", ".join(row["failures"]))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render(sys.argv[1]))
