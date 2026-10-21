"""Repeated real-model starter episodes in isolated SQL validation minds.

Fixture alignment is reported separately from integrity checks and human quality.
No judge, operational trader, broker client or dispatch method is registered.
"""

from copy import deepcopy
from uuid import uuid4

from pipmind.contracts import Contracts, event_id, loads, utc
from pipmind.shadow.common import ShadowError, digest, encoded, now
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.evaluation_provenance import capture_provenance, freeze_json
from pipmind.shadow.evaluation_usage import summarize_usage
from pipmind.shadow.model import PROMPT_VERSION
from pipmind.shadow.store import SqlMind, metadata

CASES = frozenset([*(f"T{i:02}" for i in range(1, 7)), *(f"D{i:02}" for i in range(1, 6)), "S10"])
GRADER_VERSION = "shadow-live-v2"


async def evaluate(connect, cases, model, *, repetitions=3, manifest=None, progress=None):
    # Validate built-in JSON types before copying; custom containers/copy hooks
    # cannot run while freezing caller-owned data for later repetitions.
    cases, manifest = freeze_json(cases), freeze_json(manifest)
    if not 2 <= repetitions <= 5 or not cases or len(cases) > 12:
        raise ShadowError("evaluation_bounds")
    if len({c["case_id"] for c in cases}) != len(cases) or any(
        c["case_id"] not in CASES for c in cases
    ):
        raise ShadowError("unsupported_shadow_evaluation_case")
    provenance = capture_provenance(
        cases, model, repetitions=repetitions, grader_version=GRADER_VERSION
    )
    provenance_sha256 = digest(encoded(provenance))
    report = {
        "version": GRADER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "started_at": now().isoformat(),
        "run_id": uuid4().hex,
        "dataset_sha256": digest(encoded(cases)),
        "model_manifest": manifest,
        "model_manifest_status": "descriptive_unverified"
        if manifest is not None
        else "not_supplied",
        "runtime_provenance": provenance,
        "runtime_provenance_sha256": provenance_sha256,
        "human_review": "pending; no model judge or profitability claim",
        "results": [],
    }
    contracts = Contracts()
    for case in cases:
        case_sha256 = digest(encoded([case]))
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
                cur.execute(
                    "SELECT v FROM pm_runtime WHERE trader=%s AND k='memory_policy'", (trader,)
                )
                policy_row = cur.fetchone()
                policy = loads(policy_row[0]) if policy_row else None
                observed_policy = {
                    "observation": "isolated_runtime_before_processing",
                    "present": policy_row is not None,
                    "version": policy.get("version") if isinstance(policy, dict) else None,
                    "sha256": digest(encoded(policy)) if policy_row is not None else None,
                }
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
                            "case_sha256": case_sha256,
                            "repetition": repetition + 1,
                            "runtime_provenance_sha256": provenance_sha256,
                            "observed_memory_policy": observed_policy,
                        },
                    ),
                ):
                    cur.execute("INSERT pm_runtime VALUES(%s,%s,%s)", (trader, key, encoded(value)))
            store.set_paused(False)
            active = {}
            operational_error = None
            failure_audit = None

            def observe(phase, details, active=active):
                if phase == "ready":
                    active.clear()
                    active.update(event=details["event"], result=details["result"])

            try:
                for sequence, event in enumerate(case["events"], 1):
                    moment = utc(event["available_at"])
                    store.accept("evaluation", [{"sequence": sequence, "event": event}], moment)
                    engine = ShadowEngine(
                        store, model, clock=lambda moment=moment: moment, observer=observe
                    )
                    while await engine.step():
                        pass
            except ShadowError as error:
                operational_error = str(error)
                # A completed RPC can precede a failed SQL commit. Preserve its
                # exact result/usage separately; never invent a committed outcome.
                try:
                    with store.transaction() as cur:
                        store._insert(
                            cur,
                            metadata(
                                "evaluation-failure:" + uuid4().hex,
                                "journal",
                                now().isoformat(),
                                trader,
                            ),
                            encoded(
                                {
                                    "kind": "evaluation-operational-failure",
                                    "error": operational_error,
                                    "attempt": active,
                                    "run_id": report["run_id"],
                                }
                            ),
                        )
                    failure_audit = "sql-and-report"
                except ShadowError:
                    failure_audit = "report-only; SQL persistence unconfirmed"
            finally:
                try:
                    store.set_paused(True)
                except ShadowError as error:
                    report["incomplete_episode"] = {
                        "trader": trader,
                        "case_id": case["case_id"],
                        "case_sha256": case_sha256,
                        "repetition": repetition + 1,
                        "runtime_provenance_sha256": provenance_sha256,
                        "observed_memory_policy": observed_policy,
                        "operational_error": operational_error,
                        "pause_error": str(error),
                        "attempt": deepcopy(active),
                        "pause_verified": False,
                    }
                    if progress:
                        progress(deepcopy(report))
                    raise
            ids = list(dict.fromkeys(event_id(e) for e in case["events"]))
            outcomes = [
                store.outcome(key)
                or {
                    "event_id": key,
                    "status": "not_completed",
                    "dispatches": 0,
                    "usage": [],
                    "missing_outcome": True,
                }
                for key in ids
            ]
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
            hard = {
                "complete_episode": operational_error is None
                and not any(r.get("missing_outcome") for r in outcomes),
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
                    "case_sha256": case_sha256,
                    "repetition": repetition + 1,
                    "runtime_provenance_sha256": provenance_sha256,
                    "observed_memory_policy": observed_policy,
                    "trader": trader,
                    "split": case["split"],
                    "hard_checks": hard,
                    "fixture_alignment": alignment,
                    "model_refusals": sum(r["status"] == "model_refused" for r in outcomes),
                    "operational_error": operational_error,
                    "failure_audit": failure_audit,
                    "uncommitted_attempt": deepcopy(active)
                    if operational_error
                    and not any(
                        r["event_id"] == active.get("event", {}).get("message_id")
                        and not r.get("missing_outcome")
                        for r in outcomes
                    )
                    else None,
                    "outcomes": outcomes,
                }
            )
            if progress:
                progress(deepcopy(report))
    usage = [u for r in report["results"] for o in r["outcomes"] for u in o.get("usage", [])]
    usage.extend(
        u
        for r in report["results"]
        for u in (r.get("uncommitted_attempt") or {}).get("result", {}).get("usage", [])
    )
    report["summary"] = {
        "episodes": len(report["results"]),
        **summarize_usage(usage),
        "hard_check_failures": sum(
            not v for r in report["results"] for v in r["hard_checks"].values()
        ),
        "model_refusals": sum(r["model_refusals"] for r in report["results"]),
        "operational_failures": sum(r["operational_error"] is not None for r in report["results"]),
        "fixture_alignment_failures": sum(
            not v for r in report["results"] for v in r["fixture_alignment"].values()
        ),
    }
    report["completed_at"] = now().isoformat()
    return report
