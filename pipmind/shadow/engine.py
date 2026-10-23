"""Shadow processing deliberately has no gateway, OrderRequest or dispatch API."""

from copy import deepcopy

from pipmind.contracts import ContractError, Contracts, event_id, stamp, utc, validate_event
from pipmind.shadow.common import ShadowError, now
from pipmind.shadow.context import event_query
from pipmind.shadow.decision_budget import fit_decision_evidence
from pipmind.shadow.maintenance import (
    POLICY,
    entries,
    expand_repair_patch,
    punctuation_spacing,
    repair_budget,
    selected_note_formatting,
    spacing_repair,
    transition,
    validate_working,
    working_digest,
    working_sizes,
)
from pipmind.shadow.model import ModelFailure
from pipmind.shadow.step_diagnostics import StepDiagnostics


class ShadowEngine:
    def __init__(self, store, model, *, clock=now, price_interval=60, observer=None):
        if not 1 <= price_interval <= 3600:
            raise ShadowError("invalid_price_interval")
        self.store, self.model, self.clock, self.contracts = store, model, clock, Contracts()
        self.price_interval = price_interval
        self.observer = observer or (lambda phase, facts: None)
        self._diagnostics = StepDiagnostics()

    async def _correct_memory(
        self, event, context, reply, result, moment, reason="working_memory_bound"
    ):
        """At most one compression RPC; preserve the candidate and all usage."""
        original = deepcopy(reply)
        audit = result["memory_repair"] = {
            "candidate": original,
            "before_sha256": working_digest(context["working_memory"]),
            "reason": reason,
        }
        result["usage"].append({"phase": "memory_repair", "attempted": True, "usage_unknown": True})
        self.observer("memory_repair", {"event": event})
        try:
            correction = spacing_repair(
                context["working_memory"], original, event_id(event), moment
            )
            if correction is not None:
                audit["method"] = "punctuation_spacing"
                usage = {
                    "phase": "memory_repair",
                    "attempted": False,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "method": "punctuation_spacing",
                }
            else:
                with self._diagnostics.operation("memory_repair"):
                    correction, usage = await self.model.repair_memory(
                        deepcopy(event), deepcopy(context), deepcopy(original), self.store, moment
                    )
            result["usage"][-1] = usage
            audit["response"] = deepcopy(correction)
            if "replacements" in correction:
                self.contracts.validate("memory-repair-patch", correction)
                audit["patch_budget"] = repair_budget(
                    original["working_memory"],
                    before=context["working_memory"],
                    dispositions=original.get("memory_dispositions"),
                    event_ref=event_id(event),
                )
                correction = expand_repair_patch(
                    original, correction, event_id(event), before=context["working_memory"]
                )
                audit["expanded_response"] = deepcopy(correction)
                for row in audit["response"]["replacements"]:
                    applied = entries(correction["working_memory"])[row["from"]]
                    if row["text"] != applied:
                        normalized, proof = selected_note_formatting(row["text"])
                        audit.setdefault("patch_spacing_normalizations", []).append(
                            {
                                "key": row["from"],
                                "before_bytes": len(row["text"].encode()),
                                "after_bytes": len(applied.encode()),
                                **(proof if proof and normalized == applied else {}),
                            }
                        )
                edited = {row["from"] for row in audit["response"]["replacements"]}
                for key, text in entries(original["working_memory"]).items():
                    applied = entries(correction["working_memory"])[key]
                    if key in edited or applied == text:
                        continue
                    if applied != punctuation_spacing(text):
                        raise ShadowError("working_memory_repair_scope")
                    audit.setdefault("format_only_normalizations", []).append(
                        {
                            "key": key,
                            "before_bytes": len(text.encode()),
                            "after_bytes": len(applied.encode()),
                            "method": "punctuation_spacing",
                        }
                    )
            self.contracts.validate("memory-repair", correction)
            correction = deepcopy(correction)
            after = correction["working_memory"]
            audit["response_sizes"] = working_sizes(after)
            # A literal carry-forward is not a semantic update. Canonicalize
            # only this provable identity; leave the raw reply in the audit and
            # let the unchanged transition validator reject every other fault.
            targets = entries(after)
            for field, source in (
                ("memory_dispositions", context["working_memory"]),
                ("candidate_dispositions", original["working_memory"]),
            ):
                sources = entries(source)
                for row in correction[field]:
                    origin, target = row["from"], row["to"]
                    if (
                        origin in sources
                        and target in targets
                        and origin[0] == target[0]
                        and sources[origin] == targets[target]
                        and row["disposition"] in {"updated", "merged"}
                    ):
                        audit.setdefault("mapping_normalizations", []).append(
                            {
                                "field": field,
                                "from": origin,
                                "to": target,
                                "original_disposition": row["disposition"],
                                "applied_disposition": "retained",
                            }
                        )
                        row["disposition"] = "retained"
            if after["position_theses"] != original["working_memory"]["position_theses"]:
                raise ShadowError("working_memory_repair_theses")
            # Every candidate entry must survive, possibly in a same-kind merge.
            if any(row["to"] is None for row in correction["candidate_dispositions"]):
                raise ShadowError("working_memory_repair_dependency")
            refs = transition(
                original["working_memory"],
                after,
                correction["candidate_dispositions"],
                moment,
                initial=True,
            )
            refs |= transition(
                context["working_memory"], after, correction["memory_dispositions"], moment
            )
            previous = {row["from"]: row for row in original.get("memory_dispositions", [])}
            for row in correction["memory_dispositions"]:
                if (
                    row["from"].startswith("w")
                    and row["to"] is None
                    and row != previous.get(row["from"])
                ):
                    raise ShadowError("working_memory_repair_resolution")
            allowed = set(original["evidence_ids"]) | {event_id(event)}
            allowed.update(
                ref
                for row in original.get("memory_dispositions", [])
                for ref in row["evidence_ids"]
            )
            allowed.update(
                context["memory_policy"][key] for key in ("checkpoint", "origin_checkpoint")
            )
            if not refs <= allowed:
                raise ShadowError("working_memory_repair_evidence")
            audit["evidence_ids"] = sorted(refs)
            # The model never receives permission to alter action/size/reasoning/
            # evidence or other decision fields. Only these two fields change.
            repaired = deepcopy(original)
            repaired.update(
                working_memory=after, memory_dispositions=correction["memory_dispositions"]
            )
            return repaired
        except (ShadowError, ContractError, KeyError) as error:
            if isinstance(error, ModelFailure):
                result["usage"][-1] = error.trace
            audit["error"] = str(error) if isinstance(error, ShadowError) else type(error).__name__
            raise ShadowError("working_memory_repair_failed") from None

    async def step(self):
        with self._diagnostics.operation("step"):
            return await self._step()

    async def _step(self):
        moment = self.clock()
        if hasattr(self.store, "prepare_step"):
            self.observer("preparation", {})
            with self._diagnostics.operation("prepare"):
                prepared = self.store.prepare_step(moment)
            if prepared is None:
                return False
            event, context, reconsideration = prepared
            self.observer("context", {"event": event})
        else:
            with self._diagnostics.operation("prepare"):
                if self.store.head()["paused"]:
                    return False
                event = self.store.pending()
                if not event:
                    return False
                if validate_event(event, self.contracts) > moment:
                    raise ShadowError("future_shadow_input")
                self.observer("context", {"event": event})
                reconsideration = self.store.reconsideration(event)
                query = event_query(reconsideration["original_input"] if reconsideration else event)
                context = self.store.context(moment, query, history=False)
                context["broker_snapshots"] = self.store.runtime("broker_snapshots", {})
                context["feed_status"] = self.store.runtime(
                    "feed_status", {"coverage": "unverified"}
                )
        query = event_query(reconsideration["original_input"] if reconsideration else event)
        # Triage consumes only the current mind/facts. Defer SQL archive reads
        # until a decision needs them, keeping quiet inputs off the expensive path.
        if reconsideration:
            context["reconsideration"] = reconsideration
        result = {
            "mode": "shadow",
            "event_id": event_id(event),
            "ts": stamp(moment),
            "dispatches": 0,
            "usage": [],
            "reply": None,
            "runtime_updates": {},
        }
        self.observer("ready", {"event": event, "revision": context["revision"], "result": result})
        if event["kind"] in {"AccountSnapshot", "PositionSnapshot"}:
            snapshots = dict(context["broker_snapshots"])
            key = "account" if event["kind"] == "AccountSnapshot" else "positions"
            if key not in snapshots or utc(event["payload"]["as_of"]) >= utc(
                snapshots[key]["as_of"]
            ):
                snapshots[key] = event["payload"]
            result["runtime_updates"]["broker_snapshots"] = snapshots
            context["broker_snapshots"] = snapshots
            result["status"] = "snapshot_retained"
            with self._diagnostics.operation("commit"):
                self.store.commit(event, context["revision"], result, moment)
            self.observer("committed", {})
            return True
        if event["kind"] == "PriceEvent":
            price = event["payload"]
            key = "price:" + price["instrument"]
            aggregate = self.store.runtime(key, {"count": 0, "last_wake": None})
            aggregate.update(
                count=aggregate["count"] + 1,
                last=price,
                min_bid=min(aggregate.get("min_bid", price["bid"]), price["bid"]),
                max_bid=max(aggregate.get("max_bid", price["bid"]), price["bid"]),
            )
            context["price_aggregate"] = deepcopy(aggregate)
            due = (
                aggregate["last_wake"] is None
                or (moment - utc(aggregate["last_wake"])).total_seconds() >= self.price_interval
            )
            if due:
                aggregate["last_wake"] = stamp(moment)
                aggregate["count"] = 0
                aggregate["min_bid"] = aggregate["max_bid"] = price["bid"]
            result["runtime_updates"][key] = aggregate
            if not due:
                result["status"] = "price_coalesced"
                with self._diagnostics.operation("commit"):
                    self.store.commit(event, context["revision"], result, moment)
                self.observer("committed", {})
                return True
        loading_evidence = False
        try:
            result["context"] = deepcopy(context)
            if context.get("memory_policy"):
                if context["memory_policy"]["version"] != POLICY:
                    raise ShadowError("working_memory_policy")
                validate_working(context["working_memory"], moment)
            result["usage"].append({"phase": "triage", "attempted": True, "usage_unknown": True})
            with self._diagnostics.operation("triage"):
                triage, usage = await self.model.triage(
                    deepcopy(event), deepcopy(context), self.store, moment
                )
            self.contracts.validate("triage-result", triage)
            result.update(triage=triage)
            result["usage"][-1] = usage
            effective = reconsideration["original_input"] if reconsideration else event
            analysis = (
                effective["kind"] == "NewsEvent" and effective["payload"]["kind"] == "analysis"
            )
            if triage["wake"] and not analysis:
                self.observer("evidence", {"event": event})
                loading_evidence = True
                with self._diagnostics.operation("evidence"):
                    evidence = self.store.context(moment, query, revision=context["revision"])
                loading_evidence = False
                for key in ("recent_journal", "retrieved_memories"):
                    context[key] = evidence[key]
                result["context"] = deepcopy(context)
                result["usage"].append(
                    {"phase": "decision", "attempted": True, "usage_unknown": True}
                )
                with self._diagnostics.operation("decision"):
                    try:
                        reply, usage = await self.model.decide(
                            deepcopy(event), deepcopy(context), triage, self.store, moment
                        )
                    except ModelFailure as error:
                        # The only eligible failure occurred before any RPC.
                        # Keep that exact refusal and original context in the audit.
                        result["usage"][-1] = deepcopy(error.trace)
                        fitted = fit_decision_evidence(event, context, triage, moment, error.trace)
                        if fitted is None:
                            raise
                        decision_context, audit = fitted
                        result["decision_evidence_budget"] = audit
                        result["usage"].append(
                            {"phase": "decision", "attempted": True, "usage_unknown": True}
                        )
                        # One additional assembly, never a transport retry. The
                        # unchanged model guard still runs before its sole RPC.
                        reply, usage = await self.model.decide(
                            deepcopy(event), decision_context, triage, self.store, moment
                        )
                result["usage"][-1] = usage
                result["model_reply"] = deepcopy(reply)
                self.contracts.validate("model-reply", reply)
                if reply["tool_calls"]:
                    raise ShadowError("unexecuted_shadow_tools")
                positions = context["broker_snapshots"].get("positions", {}).get("positions", [])
                instruments = {
                    row["instrument"]
                    for row in positions
                    if row["size"] > 0 and row["side"] != "flat"
                }
                if any(
                    row["instrument"] not in instruments
                    for row in reply["working_memory"]["position_theses"]
                ):
                    raise ShadowError("unconfirmed_position_thesis")
                if context.get("memory_policy") and not instruments <= {
                    row["instrument"] for row in reply["working_memory"]["position_theses"]
                }:
                    raise ShadowError("working_memory_missing_thesis")
                if reply["action"] != "hold" and not reply.get("instrument"):
                    raise ShadowError("unusable_shadow_proposal")
                if reply["action"] in {"enter", "add"} and not {"side", "size"} <= set(reply):
                    raise ShadowError("unusable_shadow_proposal")
                if context.get("memory_policy"):
                    try:
                        transition(
                            context["working_memory"],
                            reply["working_memory"],
                            reply.get("memory_dispositions", []),
                            moment,
                        )
                    except ShadowError as error:
                        repairable = {
                            "working_memory_bound",
                            "working_memory_dispositions",
                            "working_memory_replacement",
                        }
                        if str(error) not in repairable or not hasattr(self.model, "repair_memory"):
                            raise
                        reply = await self._correct_memory(
                            event, context, reply, result, moment, str(error)
                        )
                        result["model_reply"] = deepcopy(reply)
                result["reply"] = reply
            result["status"] = "analysis_retained" if analysis else "observed"
        except (ShadowError, ContractError, KeyError) as error:
            if loading_evidence:
                # A database/revision failure is an operational hold. Preserve
                # the pending input and known triage usage for worker recovery.
                raise
            if isinstance(error, ModelFailure):
                result["usage"][-1] = error.trace
            code = (
                (error.trace.get("failure_code") or "")
                if isinstance(error, ModelFailure)
                else str(error)
            )
            if code in {"shadow_context_size", "shadow_context_projection"} or code.startswith(
                "working_memory_"
            ):
                with self._diagnostics.operation("hold"):
                    self.store.hold(event, context["revision"], result, moment, code)
                return False
            result.update(status="model_refused", reply=None, error="model_unavailable_or_invalid")
        try:
            with self._diagnostics.operation("commit"):
                self.store.commit(event, context["revision"], result, moment)
        except ShadowError as error:
            if str(error).startswith("working_memory_"):
                with self._diagnostics.operation("hold"):
                    self.store.hold(event, context["revision"], result, moment, str(error))
                return False
            elif str(error) in {"unavailable_shadow_evidence", "future_shadow_memory"}:
                # Journal a validated refusal, retaining the original input. No
                # invalid memory is committed, and no fabricated hold is invented.
                result.update(status="model_refused", reply=None, error=str(error))
                with self._diagnostics.operation("commit"):
                    self.store.commit(event, context["revision"], result, moment)
            else:
                raise
        self.observer("committed", {})
        return True
