"""Shadow processing deliberately has no gateway, OrderRequest or dispatch API."""

from copy import deepcopy

from pipmind.contracts import ContractError, Contracts, event_id, stamp, utc, validate_event
from pipmind.shadow.common import ShadowError, now
from pipmind.shadow.context import event_query
from pipmind.shadow.maintenance import POLICY, validate_working
from pipmind.shadow.model import ModelFailure


class ShadowEngine:
    def __init__(self, store, model, *, clock=now, price_interval=60):
        if not 1 <= price_interval <= 3600:
            raise ShadowError("invalid_price_interval")
        self.store, self.model, self.clock, self.contracts = store, model, clock, Contracts()
        self.price_interval = price_interval

    async def step(self):
        moment = self.clock()
        if self.store.head()["paused"]:
            return False
        event = self.store.pending()
        if not event:
            return False
        if validate_event(event, self.contracts) > moment:
            raise ShadowError("future_shadow_input")
        reconsideration = self.store.reconsideration(event)
        query = event_query(reconsideration["original_input"] if reconsideration else event)
        context = self.store.context(moment, query)
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
        context["broker_snapshots"] = self.store.runtime("broker_snapshots", {})
        context["feed_status"] = self.store.runtime("feed_status", {"coverage": "unverified"})
        if event["kind"] in {"AccountSnapshot", "PositionSnapshot"}:
            snapshots = dict(context["broker_snapshots"])
            snapshots["account" if event["kind"] == "AccountSnapshot" else "positions"] = event[
                "payload"
            ]
            result["runtime_updates"]["broker_snapshots"] = snapshots
            context["broker_snapshots"] = snapshots
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
                self.store.commit(event, context["revision"], result, moment)
                return True
        try:
            result["context"] = deepcopy(context)
            if context.get("memory_policy"):
                if context["memory_policy"]["version"] != POLICY:
                    raise ShadowError("working_memory_policy")
                validate_working(context["working_memory"], moment)
            result["usage"].append({"phase": "triage", "attempted": True, "usage_unknown": True})
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
                result["usage"].append(
                    {"phase": "decision", "attempted": True, "usage_unknown": True}
                )
                reply, usage = await self.model.decide(
                    deepcopy(event), deepcopy(context), triage, self.store, moment
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
                result["reply"] = reply
            result["status"] = "analysis_retained" if analysis else "observed"
        except (ShadowError, ContractError, KeyError) as error:
            if isinstance(error, ModelFailure):
                result["usage"][-1] = error.trace
            code = (
                (error.trace.get("failure_code") or "")
                if isinstance(error, ModelFailure)
                else str(error)
            )
            if code == "shadow_context_size" or code.startswith("working_memory_"):
                self.store.hold(event, context["revision"], result, moment, code)
                return False
            result.update(status="model_refused", reply=None, error="model_unavailable_or_invalid")
        try:
            self.store.commit(event, context["revision"], result, moment)
        except ShadowError as error:
            if str(error).startswith("working_memory_"):
                self.store.hold(event, context["revision"], result, moment, str(error))
                return False
            elif str(error) in {"unavailable_shadow_evidence", "future_shadow_memory"}:
                # Journal a validated refusal, retaining the original input. No
                # invalid memory is committed, and no fabricated hold is invented.
                result.update(status="model_refused", reply=None, error=str(error))
                self.store.commit(event, context["revision"], result, moment)
            else:
                raise
        return True
