"""Durable, private full-session capture. This never certifies M4 or sends orders."""

import asyncio
import os
import re
from datetime import timedelta
from pathlib import Path
from urllib.request import Request, build_opener
from zoneinfo import ZoneInfo

from pipmind.contracts import loads, stamp, utc
from pipmind.shadow.common import ShadowError, encoded, now
from pipmind.shadow.service import NoRedirect

ACTIVE = "qualification:active"


def open_fx_window(start):
    zone = ZoneInfo("America/New_York")
    for n in range(25):
        local = (start + timedelta(hours=min(n, 24) - (0.000001 if n == 24 else 0))).astimezone(
            zone
        )
        if (
            local.weekday() == 5
            or (local.weekday() == 4 and local.hour >= 17)
            or (local.weekday() == 6 and local.hour < 17)
        ):
            return False
    return True


def arm(store, run_id, instruments, build, moment, *, quote_policy="fresh_ticks"):
    if not re.fullmatch(r"[a-z0-9-]{1,40}", run_id) or not instruments or len(instruments) > 50:
        raise ShadowError("invalid_qualification_request")
    if any(not re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", x) for x in instruments):
        raise ShadowError("invalid_qualification_request")
    if quote_policy not in {"fresh_ticks", "observed_staleness"}:
        raise ShadowError("invalid_qualification_request")
    with store.transaction() as c:
        previous = store._runtime(c, ACTIVE)
        if previous and previous["state"] in {"warming", "running"}:
            raise ShadowError("qualification_already_active")
        value = {
            "id": run_id,
            "requested_at": stamp(moment),
            "state": "warming",
            "build": build,
            "instruments": sorted(set(instruments)),
            "quote_policy": quote_policy,
            "quote_stale_seconds": 15,
            "interval": 60,
            "max_gap": 180,
            "max_input_lag": 300,
            "max_pending": 100,
            "samples": 0,
            "failures": [],
            "last_slot": None,
            "last_at": None,
            "from_outcome": None,
            "start": None,
            "end": None,
        }
        if store._runtime(c, "qualification:" + run_id + ":request"):
            raise ShadowError("qualification_id_exists")
        store._set_runtime(c, "qualification:" + run_id + ":request", value)
        store._set_runtime(c, ACTIVE, value)
    return value


def read_gateway(gateway, path):
    request = Request(gateway.url + path, headers={"Authorization": "Bearer " + gateway.token})
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        raw = response.read(2_097_153)
    if len(raw) > 2_097_152:
        raise ShadowError("qualification_response_bound")
    return loads(raw)


def healthy(sample, spec, moment):
    reasons = []
    s, b = sample.get("shadow", {}), sample.get("broker", {})
    w = s.get("reasoning_worker", {})
    if s.get("paused") is not False or s.get("reasoning_hold") or s.get("last_error"):
        reasons.append("engine_not_ready")
    if not all(w.get(k) is True for k in ("alive", "loop_alive", "supervisor_alive")):
        reasons.append("worker_not_ready")
    if (
        b.get("dispatch_enabled") is not False
        or not b.get("broker_verified")
        or not b.get("worker_alive")
    ):
        reasons.append("broker_not_ready")
    if (
        b.get("last_error")
        or not b.get("last_cycle")
        or (moment - utc(b["last_cycle"])).total_seconds() > 90
    ):
        reasons.append("broker_read_gap")
    prices = {x["instrument"]: x for x in b.get("prices", [])}
    feeds = sample.get("feeds")
    if feeds is not None and any(
        not feeds.get("workers", {}).get(name, {}).get("alive")
        or not feeds.get("worker_heartbeats", {}).get(name, {}).get("heartbeat")
        or (moment - utc(feeds["worker_heartbeats"][name]["heartbeat"])).total_seconds() > 180
        for name in ("macro", "news", "social")
    ):
        reasons.append("feed_worker_gap")
    if spec.get("quote_policy", "fresh_ticks") == "observed_staleness":
        if any(x not in prices for x in spec["instruments"]):
            reasons.append("broker_prices_missing")
        try:
            checked = utc(b["checked_at"])
            if not 0 <= (moment - checked).total_seconds() <= 90:
                reasons.append("broker_quote_check_gap")
            for instrument in spec["instruments"]:
                price = prices[instrument]
                age = (checked - utc(price["venue_ts"])).total_seconds()
                if (
                    age < 0
                    or utc(price["received_at"]) > checked
                    or type(price.get("stale")) is not bool
                    or (age >= spec["quote_stale_seconds"] and not price["stale"])
                ):
                    reasons.append("broker_quote_freshness_invalid")
                    break
        except (KeyError, TypeError, ValueError):
            reasons.append("broker_quote_freshness_invalid")
        fresh_rest = all(
            x in prices and prices[x].get("source") == "rest" and prices[x].get("stale") is False
            for x in spec["instruments"]
        )
        if b.get("stream_connected") is not True and not fresh_rest:
            reasons.append("broker_transport_gap")
    elif any(x not in prices or prices[x].get("stale") is not False for x in spec["instruments"]):
        reasons.append("broker_prices_stale")
    snapshots = b.get("snapshots")
    if (
        not snapshots
        or len(snapshots) != 2
        or any(not 0 <= (moment - utc(x["as_of"])).total_seconds() <= 120 for x in snapshots)
    ):
        reasons.append("broker_snapshots_stale")
    evidence = sample.get("order_evidence", {})
    e = evidence.get("evidence") or {}
    if (
        evidence.get("last_error")
        or not e.get("complete")
        or e.get("environment") != "demo"
        or (not e.get("as_of") or not 0 <= (moment - utc(e["as_of"])).total_seconds() <= 420)
    ):
        reasons.append("broker_evidence_gap")
    if e.get("activity_count") != 0:
        reasons.append("broker_activity_present")
    if s.get("dispatches") != 0:
        reasons.append("dispatch_reported")
    if sample.get("oldest_pending_age", 1e12) > spec["max_input_lag"] or (
        s.get("pending", 1e12) > spec["max_pending"]
    ):
        reasons.append("startup_backlog")
    return reasons


class Recorder:
    def __init__(self, store, gateway, state, feed_state=None):
        self.store, self.gateway, self.state = store, gateway, state
        self.feed_state = feed_state
        self.last_error = None

    def capture(self, moment=None):
        live_clock = moment is None
        moment = moment or now()
        spec = self.store.runtime(ACTIVE)
        if not spec or spec["state"] not in {"warming", "running"}:
            return
        slot = int(moment.timestamp()) // spec["interval"]
        if spec["last_slot"] is not None and slot <= spec["last_slot"]:
            return
        sample = {"at": stamp(moment), "build": os.environ.get("BUILD_SHA"), "errors": []}
        try:
            sample["shadow"] = self.state()
            sample["broker"] = read_gateway(self.gateway, "/state")
            sample["order_evidence"] = read_gateway(self.gateway, "/evidence")
            if self.feed_state:
                sample["feeds"] = self.feed_state()
        except Exception as error:
            sample["errors"].append(type(error).__name__)
        # A broker worker can refresh its snapshot while these GETs run. Use
        # receipt completion for freshness, never compare a new response with
        # the earlier request-start clock and label it future/stale.
        if live_clock:
            moment = now()
            slot = int(moment.timestamp()) // spec["interval"]
            sample["at"] = stamp(moment)
        sample["quote_ages"] = {}
        for price in sample.get("broker", {}).get("prices", []):
            try:
                sample["quote_ages"][price["instrument"]] = {
                    "seconds": (moment - utc(price["venue_ts"])).total_seconds(),
                    "stale": price["stale"],
                    "source": price["source"],
                }
            except (KeyError, TypeError, ValueError):
                pass  # The readiness check retains invalid/missing quote failures.
        with self.store.transaction() as c:
            current = self.store._runtime(c, ACTIVE)
            if (
                current["id"] != spec["id"]
                or current["state"] not in {"warming", "running"}
                or (current["last_slot"] is not None and slot <= current["last_slot"])
            ):
                return
            c.execute(
                "SELECT MIN(JSON_VALUE(envelope,'$.available_at')) FROM pm_inputs "
                "WHERE trader=%s AND status='pending'",
                (self.store.trader,),
            )
            oldest = c.fetchone()[0]
            sample["oldest_pending_age"] = (
                max(0, (moment - utc(oldest)).total_seconds()) if oldest else 0
            )
            sample["model_calls"] = []
            c.execute(
                "SELECT COALESCE(MAX(pk),0) FROM pm_outcomes WHERE trader=%s", (self.store.trader,)
            )
            maximum = c.fetchone()[0]
            if current["from_outcome"] is not None:
                c.execute(
                    "SELECT TOP(1001) pk,JSON_VALUE(result,'$.status'),"
                    "JSON_QUERY(result,'$.usage') "
                    "FROM pm_outcomes WHERE trader=%s AND pk>%s AND pk<=%s ORDER BY pk",
                    (self.store.trader, current["from_outcome"], maximum),
                )
                for pk, status, raw in c:
                    # Retain counters/settings/errors, not full duplicate prompts.
                    calls = [
                        {k: v for k, v in call.items() if k not in {"request", "response", "tools"}}
                        for call in loads(raw or "[]")
                    ]
                    sample["model_calls"].append({"pk": pk, "status": status, "calls": calls})
                if len(sample["model_calls"]) > 1000:
                    raise ShadowError("qualification_outcome_bound")
            sample["memory_bytes"] = next(
                (
                    int(Path(p).read_text())
                    for p in (
                        "/sys/fs/cgroup/memory.current",
                        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
                    )
                    if Path(p).exists()
                ),
                None,
            )
            sample["memory_failcnt"] = (
                int(Path("/sys/fs/cgroup/memory/memory.failcnt").read_text())
                if (Path("/sys/fs/cgroup/memory/memory.failcnt").exists())
                else None
            )
            sample["process_rss_bytes"] = {}
            pids = {"engine": os.getpid()}
            pids.update(
                {
                    name: worker.get("pid")
                    for name, worker in sample.get("feeds", {}).get("workers", {}).items()
                }
            )
            for name, pid in pids.items():
                if type(pid) is not int or pid <= 0:
                    continue
                try:
                    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            sample["process_rss_bytes"][name] = int(line.split()[1]) * 1024
                            break
                except OSError:
                    pass  # Process metrics are optional outside Linux or during a restart.
            cpu = Path("/sys/fs/cgroup/cpuacct/cpuacct.usage")
            sample["cpu_usage_ns"] = int(cpu.read_text()) if cpu.exists() else None
            sample["feed_status"] = self.store._runtime(c, "feed_status", {})
            sample["readiness"] = healthy(sample, current, moment)
            if sample["errors"]:
                sample["readiness"].append("sample_read_failed")
            if current["state"] == "warming":
                if sample.get("build") != current["build"]:
                    current["state"] = "warmup_failed"
                    current["failures"] = ["build_changed"]
                elif (moment - utc(current["requested_at"])).total_seconds() > 86400:
                    current["state"] = "warmup_failed"
                    current["failures"] = ["warmup_exceeded_24h"]
                elif not sample["readiness"] and open_fx_window(moment):
                    current.update(
                        state="running",
                        start=stamp(moment),
                        end=stamp(moment + timedelta(hours=24)),
                        binding=sample["order_evidence"]["evidence"]["binding_sha256"],
                    )
                    sample["window_started"] = True
            else:
                if (
                    current["last_at"]
                    and (moment - utc(current["last_at"])).total_seconds() > current["max_gap"]
                ):
                    sample["readiness"].append("capture_gap")
                if sample.get("build") != current["build"]:
                    sample["readiness"].append("build_changed")
                e = sample.get("order_evidence", {}).get("evidence") or {}
                if e.get("binding_sha256") != current["binding"] or (
                    e.get("from") and utc(e["from"]) > utc(current["start"])
                ):
                    sample["readiness"].append("broker_evidence_window_changed")
                current["failures"] = sorted(set(current["failures"]) | set(sample["readiness"]))
                if moment >= utc(current["end"]):
                    current["state"] = (
                        "capture_failed" if current["failures"] else "captured_pending_review"
                    )
            key = "qualification:" + current["id"] + ":sample:" + str(slot)
            if len(encoded(sample).encode()) > 2_097_152:
                raise ShadowError("qualification_sample_bound")
            # Slot insertion and cursor advance are one transaction. Overlapping
            # single-app revisions cannot overwrite samples or silently skip one.
            c.execute(
                "INSERT pm_runtime VALUES(%s,%s,%s)", (self.store.trader, key, encoded(sample))
            )
            current.update(
                last_at=stamp(moment),
                last_slot=slot,
                samples=current["samples"] + 1,
                from_outcome=maximum,
                last_readiness=sample["readiness"],
            )
            self.store._set_runtime(c, ACTIVE, current)
            self.last_error = None

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.capture)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = type(error).__name__
            await asyncio.sleep(10)
