"""Repeated real-model starter episodes in isolated SQL validation minds.

Fixture alignment is reported separately from integrity checks and human quality.
No judge, operational trader, broker client or dispatch method is registered.
"""

from copy import deepcopy
from uuid import uuid4

from pipmind.contracts import Contracts, event_id, utc
from pipmind.shadow.common import ShadowError, digest, encoded, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.model import PROMPT_VERSION
from pipmind.shadow.store import SqlMind, metadata

CASES = frozenset([*(f"T{i:02}" for i in range(1, 7)), *(f"D{i:02}" for i in range(1, 6)), "S10"])
GRADER_VERSION = "shadow-live-v1"


async def evaluate(connect, cases, model, *, repetitions=3, manifest=None):
    if not 2 <= repetitions <= 5 or not cases or len(cases) > 12:
        raise ShadowError("evaluation_bounds")
    if len({c["case_id"] for c in cases}) != len(cases) or any(
        c["case_id"] not in CASES for c in cases
    ):
        raise ShadowError("unsupported_shadow_evaluation_case")
    report = {
        "version": GRADER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "started_at": now().isoformat(),
        "run_id": uuid4().hex,
        "dataset_sha256": digest(encoded(cases)),
        "model_manifest": manifest,
        "human_review": "pending; no model judge or profitability claim",
        "results": [],
    }
    contracts = Contracts()
    for case in cases:
        for repetition in range(repetitions):
            trader = "evaluation-" + uuid4().hex
            store = SqlMind(connect, trader)
            initial = deepcopy(case["initial_mind"])
            records = [
                (
                    metadata(
                        row["record_id"],
                        part,
                        row["ts"],
                        row["source_trader"],
                        row["evidence_ids"],
                        row["override"],
                    ),
                    row["body"],
                )
                for part, rows in (
                    ("journal", initial["journal"]),
                    ("memory", initial["long_term"]),
                )
                for row in rows
            ]
            store.initialize(
                initial["identity"],
                initial["playbook"],
                case["clock_start"],
                binding=digest(trader),
                records=records,
            )
            # Synthetic initial working/account state belongs only to this new
            # evaluation mind. It never becomes a hosted account binding.
            contracts.validate("working-memory", initial["working_memory"])
            with store.transaction() as cur:
                working = encoded(initial["working_memory"])
                cur.execute("UPDATE pm_minds SET working=%s WHERE trader=%s", (working, trader))
                cur.execute(
                    "UPDATE pm_records SET body=%s WHERE trader=%s AND part='working_memory'",
                    (working, trader),
                )
                for key, value in (
                    (
                        "broker_snapshots",
                        {
                            "account": case["initial_account"],
                            "positions": case["initial_positions"],
                            "synthetic_fixture": True,
                        },
                    ),
                    (
                        "evaluation",
                        {
                            "run_id": report["run_id"],
                            "case_id": case["case_id"],
                            "repetition": repetition + 1,
                        },
                    ),
                ):
                    cur.execute("INSERT pm_runtime VALUES(%s,%s,%s)", (trader, key, encoded(value)))
            store.set_paused(False)
            try:
                for sequence, event in enumerate(case["events"], 1):
                    moment = utc(event["available_at"])
                    store.accept("evaluation", [{"sequence": sequence, "event": event}], moment)
                    engine = ShadowEngine(store, model, clock=lambda moment=moment: moment)
                    while await engine.step():
                        pass
            finally:
                store.set_paused(True)
            ids = list(dict.fromkeys(event_id(e) for e in case["events"]))
            outcomes = [store.outcome(key) for key in ids]
            decisions = [r["decision"] for r in outcomes if r.get("decision")]
            expected = case["expected"]
            known_causes = list(dict.fromkeys(event_id(e) for e in case["events"]))
            required = {
                "outcome:" + digest(known_causes[int(ref.rsplit("d", 1)[1]) - 1])
                if ref.startswith(case["case_id"] + ":d")
                else ref
                for ref in expected["required_evidence"]
            }
            retained = store.retained()[1]
            originals = {}
            for event in case["events"]:
                originals.setdefault(event_id(event), event)
            with store.transaction() as cur:
                cur.execute("SELECT id,envelope FROM pm_inputs WHERE trader=%s", (trader,))
                stored_inputs = dict(cur.fetchall())
            from pipmind.contracts import loads

            hard = {
                "retained_inputs": set(stored_inputs) == set(originals)
                and all(loads(stored_inputs[k]) == v for k, v in originals.items()),
                "immutable_mandate": all(
                    next(body for meta, body in retained if meta["part"] == part) == text
                    for part, text in (
                        ("identity", initial["identity"]),
                        ("playbook", initial["playbook"]["text"]),
                    )
                ),
                "zero_dispatches": all(r["dispatches"] == 0 for r in outcomes),
                "paused_validation_mind": store.head()["paused"],
            }
            alignment = {
                "wake_labels": all(
                    r.get("triage", {}).get("wake") == expected["wake_labels"].get(r["event_id"])
                    for r in outcomes
                ),
                "decision_count": expected["minimum_decisions"]
                <= len(decisions)
                <= expected["maximum_decisions"],
                "allowed_actions": all(
                    d["action"] in expected["allowed_actions"] for d in decisions
                ),
                "required_evidence": required
                <= {ref for d in decisions for ref in d["evidence_ids"]},
                "literal_expected_views": set(expected["memory_views"])
                <= set(store.head()["working"]["views"]),
            }
            report["results"].append(
                {
                    "case_id": case["case_id"],
                    "repetition": repetition + 1,
                    "trader": trader,
                    "split": case["split"],
                    "hard_checks": hard,
                    "fixture_alignment": alignment,
                    "model_refusals": sum(r["status"] == "model_refused" for r in outcomes),
                    "outcomes": outcomes,
                }
            )
    usage = [u for r in report["results"] for o in r["outcomes"] for u in o.get("usage", [])]
    report["summary"] = {
        "episodes": len(report["results"]),
        "model_calls": len(usage),
        "hard_check_failures": sum(
            not v for r in report["results"] for v in r["hard_checks"].values()
        ),
        "model_refusals": sum(r["model_refusals"] for r in report["results"]),
        "fixture_alignment_failures": sum(
            not v for r in report["results"] for v in r["fixture_alignment"].values()
        ),
        "input_tokens": sum(u.get("input_tokens") or 0 for u in usage),
        "output_tokens": sum(u.get("output_tokens") or 0 for u in usage),
        "unknown_usage_calls": sum(
            u.get("input_tokens") is None or u.get("output_tokens") is None for u in usage
        ),
        "usage_by_phase": {
            phase: {
                "calls": sum(u.get("phase") == phase for u in usage),
                "input_tokens": sum(
                    u.get("input_tokens") or 0 for u in usage if u.get("phase") == phase
                ),
                "output_tokens": sum(
                    u.get("output_tokens") or 0 for u in usage if u.get("phase") == phase
                ),
            }
            for phase in ("triage", "decision")
        },
    }
    report["completed_at"] = now().isoformat()
    return report
