"""Transactional five-part mind, immutable event inbox and bounded SQL retrieval."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC
from time import perf_counter
from uuid import uuid4

from pipmind.contracts import (
    ContractError,
    Contracts,
    event_facts,
    event_id,
    loads,
    stamp,
    utc,
    validate_event,
)
from pipmind.shadow.common import ShadowError, digest, encoded, terms
from pipmind.shadow.context import query_terms, record_view, relevance, semantic_text
from pipmind.shadow.maintenance import POLICY, policy_context, transition, working_digest
from pipmind.shadow.sql import stream_transaction, transaction

PARTS = {
    "identity",
    "playbook",
    "working_memory",
    "journal",
    "review",
    "memory",
    "playbook_history",
}


def metadata(identifier, part, moment, trader, evidence=(), override=False, version=None):
    return {
        "schema_version": 1,
        "record_id": identifier,
        "part": part,
        "ts": moment,
        "source_trader": trader,
        "evidence_ids": list(evidence),
        "override": override,
        "rule_version": None,
        "rule_status": "none",
        "suggestion_disposition": "none",
        "rule_id": None,
        "playbook_version": version,
    }


class SqlMind:
    def __init__(self, connect, trader):
        if not isinstance(trader, str) or not 1 <= len(trader) <= 128 or not trader.isascii():
            raise ShadowError("invalid_trader_id")
        self.connect, self.trader, self.contracts = connect, trader, Contracts()

    def transaction(self):
        return transaction(self.connect, "shadow:" + self.trader)

    def _head(self, cur):
        cur.execute(
            "SELECT lineage,binding,revision,paused,created,working,playbook_version,parent "
            "FROM pm_minds WHERE trader=%s",
            (self.trader,),
        )
        row = cur.fetchone()
        if not row:
            raise ShadowError("mind_not_initialized")
        return {
            "lineage": row[0],
            "binding": row[1],
            "revision": row[2],
            "paused": bool(row[3]),
            "created": row[4],
            "working": loads(row[5]),
            "playbook_version": row[6],
            "parent": loads(row[7]) if row[7] else None,
        }

    def _insert(self, cur, meta, body):
        self.contracts.validate("markdown-front-matter", meta)
        if len(encoded(meta)) > 65536 or len(body.encode()) > 1048576:
            raise ShadowError("mind_record_size")
        cur.execute(
            "INSERT pm_records(trader,id,part,ts,meta,body) OUTPUT INSERTED.pk "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                self.trader,
                meta["record_id"],
                meta["part"],
                utc(meta["ts"]).replace(tzinfo=None),
                encoded(meta),
                body,
            ),
        )
        pk = cur.fetchone()[0]
        index_terms = terms(semantic_text(record_view(meta, body)["body"]))
        if index_terms:
            cur.execute(
                "INSERT pm_terms(term,record_pk) SELECT value,%s FROM OPENJSON(%s)",
                (pk, encoded(index_terms)),
            )

    def initialize(self, identity, playbook, moment, *, binding, records=()):
        if (
            not identity.strip()
            or not playbook["text"].strip()
            or len(binding) != 64
            or len(identity.encode()) > 32768
            or len(playbook["text"].encode()) > 32768
        ):
            raise ShadowError("invalid_initial_mind")
        moment = stamp(utc(moment))
        working = {"views": [], "position_theses": [], "waiting_for": []}
        with self.transaction() as cur:
            cur.execute("SELECT 1 FROM pm_minds WHERE trader=%s", (self.trader,))
            if cur.fetchone():
                raise ShadowError("mind_already_initialized")
            cur.execute(
                "INSERT pm_minds VALUES(%s,%s,%s,0,1,%s,%s,%s,NULL)",
                (self.trader, str(uuid4()), binding, moment, encoded(working), playbook["version"]),
            )
            for part, body in (
                ("identity", identity),
                ("playbook", playbook["text"]),
                ("working_memory", encoded(working)),
            ):
                self._insert(
                    cur,
                    metadata(
                        "mind:" + part,
                        part,
                        moment,
                        self.trader,
                        version=playbook["version"] if part == "playbook" else None,
                    ),
                    body,
                )
            for meta, body in records:
                if meta["part"] not in {"journal", "review", "memory", "playbook_history"}:
                    raise ShadowError("invalid_initial_record_part")
                if utc(meta["ts"]) > utc(moment):
                    raise ShadowError("future_initial_record")
                self._insert(cur, meta, body)

    def set_paused(self, paused):
        if type(paused) is not bool:
            raise ShadowError("invalid_pause")
        with self.transaction() as cur:
            self._head(cur)
            cur.execute(
                "UPDATE pm_minds SET paused=%s,revision=revision+1 WHERE trader=%s",
                (paused, self.trader),
            )

    def _runtime(self, cur, key, default=None):
        cur.execute("SELECT v FROM pm_runtime WHERE trader=%s AND k=%s", (self.trader, key))
        row = cur.fetchone()
        return loads(row[0]) if row else default

    def _set_runtime(self, cur, key, value):
        cur.execute("SELECT 1 FROM pm_runtime WHERE trader=%s AND k=%s", (self.trader, key))
        if cur.fetchone():
            cur.execute(
                "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k=%s",
                (encoded(value), self.trader, key),
            )
        else:
            cur.execute("INSERT pm_runtime VALUES(%s,%s,%s)", (self.trader, key, encoded(value)))

    def _evidence(self, cur, refs, moment):
        for ref in refs:
            cur.execute(
                "SELECT id FROM pm_records WHERE trader=%s AND id=%s "
                "AND part<>'working_memory' AND ts<=%s",
                (self.trader, ref, moment.replace(tzinfo=None)),
            )
            if not cur.fetchone():
                raise ShadowError("working_memory_evidence")

    def _write_working(self, cur, working, moment):
        cur.execute(
            "SELECT id FROM pm_records WHERE trader=%s AND part='working_memory'", (self.trader,)
        )
        identifier = cur.fetchone()[0]
        cur.execute(
            "UPDATE pm_minds SET working=%s WHERE trader=%s", (encoded(working), self.trader)
        )
        cur.execute(
            "UPDATE pm_records SET body=%s,ts=%s,meta=%s WHERE trader=%s AND part='working_memory'",
            (
                encoded(working),
                moment.replace(tzinfo=None),
                encoded(metadata(identifier, "working_memory", stamp(moment), self.trader)),
                self.trader,
            ),
        )

    def _working_checkpoint(self, cur, head, after, dispositions, moment, refs, *, initial=False):
        identifier = (
            "working-checkpoint:" + digest(head["lineage"])[:16] + ":" + str(head["revision"])
        )
        previous = self._runtime(cur, "memory_policy", {})
        refs = set(refs) | ({previous["checkpoint"]} if previous else set())
        self._insert(
            cur,
            metadata(identifier, "memory", stamp(moment), self.trader, sorted(refs)),
            encoded(
                {
                    "kind": "working-memory-transition",
                    "policy": POLICY,
                    "revision": head["revision"],
                    "as_of": stamp(moment),
                    "initial": initial,
                    "previous_checkpoint": previous.get("checkpoint"),
                    "before": head["working"],
                    "after": after,
                    "dispositions": dispositions,
                }
            ),
        )
        self._set_runtime(
            cur,
            "memory_policy",
            {
                "version": POLICY,
                "checkpoint": identifier,
                "as_of": stamp(moment),
                "origin_checkpoint": identifier if initial else previous["origin_checkpoint"],
            },
        )
        self._write_working(cur, after, moment)
        return identifier

    def maintain_working(self, plan, moment):
        """Explicit paused owner activation; a complete disposition plan is retained."""
        if set(plan) != {"revision", "before_sha256", "working_memory", "dispositions", "review"}:
            raise ShadowError("working_memory_plan")
        if plan["review"] != "no-unresolved-ambiguity":
            raise ShadowError("working_memory_review_required")
        with self.transaction() as cur:
            head = self._head(cur)
            if not head["paused"] or head["revision"] != plan["revision"]:
                raise ShadowError("shadow_revision_changed")
            if working_digest(head["working"]) != plan["before_sha256"]:
                raise ShadowError("working_memory_source_changed")
            refs = transition(
                head["working"], plan["working_memory"], plan["dispositions"], moment, initial=True
            )
            self._evidence(cur, refs, moment)
            identifier = self._working_checkpoint(
                cur,
                head,
                plan["working_memory"],
                plan["dispositions"],
                moment,
                refs,
                initial=self._runtime(cur, "memory_policy") is None,
            )
            cur.execute("UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (self.trader,))
            return {"checkpoint": identifier, "paused": True, "dispatches": 0}

    def hold(self, envelope, revision, result, moment, reason):
        """Audit a failed attempt and pause atomically, leaving its input pending."""
        identifier = event_id(envelope)
        with self.transaction() as cur:
            head = self._head(cur)
            if head["revision"] != revision or head["paused"]:
                raise ShadowError("shadow_revision_changed")
            cur.execute(
                "SELECT status FROM pm_inputs WHERE trader=%s AND id=%s", (self.trader, identifier)
            )
            if cur.fetchone() != ("pending",):
                raise ShadowError("shadow_input_not_pending")
            held_id = "held:" + digest(identifier) + ":" + str(revision)
            held = {
                **result,
                "status": "held",
                "error": reason,
                "reply": None,
                "input": envelope,
                "decision": None,
            }
            self._insert(
                cur, metadata(held_id, "journal", stamp(moment), self.trader), encoded(held)
            )
            self._set_runtime(
                cur,
                "reasoning_hold",
                {
                    "event_id": identifier,
                    "attempt_id": held_id,
                    "reason": reason,
                    "since": stamp(moment),
                    "input_pending": True,
                },
            )
            cur.execute(
                "UPDATE pm_minds SET paused=1,revision=revision+1 WHERE trader=%s", (self.trader,)
            )

    def queue_reconsideration(self, original_id, moment):
        """One new present-time attempt; the original input/outcome are immutable."""
        with self.transaction() as cur:
            head = self._head(cur)
            if not head["paused"]:
                raise ShadowError("reconsideration_requires_pause")
            cur.execute(
                "SELECT result FROM pm_outcomes WHERE trader=%s AND event_id=%s",
                (self.trader, original_id),
            )
            row = cur.fetchone()
            result = loads(row[0]) if row else {}
            if result.get("status") != "model_refused" or not any(
                trace.get("attempted") is False
                and trace.get("prompt_bytes", 0)
                > (16000 if trace.get("phase") == "triage" else 28000)
                for trace in result.get("usage", [])
            ):
                raise ShadowError("not_a_context_refusal")
            identifier = "reconsider:" + digest(original_id)
            cur.execute(
                "SELECT id FROM pm_inputs WHERE trader=%s AND id=%s", (self.trader, identifier)
            )
            if cur.fetchone():
                return identifier
            original = self._original_input(cur, original_id)
            if validate_event(original, self.contracts) > moment:
                raise ShadowError("future_shadow_input")
            event = {
                "schema_version": 1,
                "kind": "ClockEvent",
                "message_id": identifier,
                "available_at": stamp(moment),
                "payload": {
                    "schema_version": 1,
                    "event_id": identifier,
                    "ts": stamp(moment),
                    "reason": "reconsideration",
                    "reconsideration_of": original_id,
                },
            }
            validate_event(event, self.contracts)
            cur.execute(
                "INSERT pm_inputs(trader,id,facts,envelope,status) VALUES(%s,%s,%s,%s,'pending')",
                (self.trader, identifier, digest(encoded(event_facts(event))), encoded(event)),
            )
            authorization = "reconsideration-authorization:" + digest(original_id)
            self._insert(
                cur,
                metadata(
                    authorization,
                    "memory",
                    stamp(moment),
                    self.trader,
                    [original_id, "outcome:" + digest(original_id)],
                ),
                encoded(
                    {
                        "kind": "owner-reconsideration",
                        "original_event_id": original_id,
                        "new_event_id": identifier,
                        "original_outcome_sha256": digest(row[0]),
                        "requested_at": stamp(moment),
                        "historical_replay": False,
                    }
                ),
            )
            self._set_runtime(cur, "reconsideration:" + identifier, authorization)
            cur.execute("UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (self.trader,))
            return identifier

    def _original_input(self, cur, identifier):
        cur.execute(
            "SELECT envelope FROM pm_inputs WHERE trader=%s AND id=%s", (self.trader, identifier)
        )
        row = cur.fetchone()
        if not row:
            raise ShadowError("reconsideration_source_missing")
        return loads(row[0])

    def reconsideration(self, event):
        if event["kind"] != "ClockEvent" or event["payload"]["reason"] != "reconsideration":
            return None
        with self.transaction() as cur:
            authorization = self._runtime(cur, "reconsideration:" + event_id(event))
            if not authorization:
                raise ShadowError("reconsideration_not_authorized")
            original_id = event["payload"]["reconsideration_of"]
            return {
                "original_input": self._original_input(cur, original_id),
                "authorization": authorization,
                "original_outcome_id": "outcome:" + digest(original_id),
                "evaluation": "present-time reconsideration; original outcome remains unchanged",
            }

    def head(self):
        with self.transaction() as cur:
            return self._head(cur)

    def checkpoint(self, source):
        with self.transaction() as cur:
            self._head(cur)
            cur.execute(
                "SELECT seq FROM pm_cursors WHERE trader=%s AND source=%s", (self.trader, source)
            )
            row = cur.fetchone()
            return row[0] if row else 0

    def accept(self, source, batch, moment):
        """Durable acceptance and the source cursor commit together, before ack."""
        if (
            not isinstance(source, str)
            or not source
            or len(source) > 128
            or not isinstance(batch, list)
            or not 1 <= len(batch) <= 500
        ):
            raise ShadowError("invalid_input_page")
        with self.transaction() as cur:
            self._head(cur)
            cur.execute(
                "SELECT seq FROM pm_cursors WHERE trader=%s AND source=%s", (self.trader, source)
            )
            row = cur.fetchone()
            previous = row[0] if row else 0
            had_cursor = row is not None
            inputs = {}
            for item in batch:
                if not isinstance(item, dict) or not {"sequence", "event"} <= set(item):
                    raise ShadowError("invalid_input_page")
                seq, envelope = item["sequence"], item["event"]
                if type(seq) is not int or seq <= previous:
                    raise ShadowError("input_cursor_not_increasing")
                try:
                    available = validate_event(envelope, self.contracts)
                except ContractError:
                    raise ShadowError("invalid_shadow_input") from None
                if available > moment:
                    raise ShadowError("input_not_yet_available")
                if (
                    envelope["kind"] == "ClockEvent"
                    and envelope["payload"]["reason"] == "reconsideration"
                ):
                    raise ShadowError("reconsideration_owner_operation_required")
                identifier = event_id(envelope)
                if (
                    len(identifier.encode("utf-16-le")) > 512
                    or len(encoded(envelope).encode()) > 131072
                ):
                    raise ShadowError("shadow_input_bound")
                facts = digest(encoded(event_facts(envelope)))
                if identifier in inputs and inputs[identifier]["facts"] != facts:
                    raise ShadowError("conflicting_shadow_input")
                inputs.setdefault(
                    identifier,
                    {
                        "ordinal": len(inputs),
                        "id": identifier,
                        "facts": facts,
                        "envelope": encoded(envelope),
                    },
                )
                previous = seq
            packet = encoded(list(inputs.values()))
            if len(packet.encode()) > 8 * 1024 * 1024:
                raise ShadowError("shadow_input_page_bound")
            cur.execute(
                """
                SELECT b.id,i.id,i.facts,r.id,r.part,r.body FROM OPENJSON(%s)
                    WITH(id nvarchar(256) '$.id') b
                LEFT JOIN pm_inputs i ON i.trader=%s AND i.id=b.id COLLATE Latin1_General_100_BIN2
                LEFT JOIN pm_records r ON r.trader=%s AND r.id=b.id COLLATE Latin1_General_100_BIN2
                    AND i.id IS NULL
                """,
                (packet, self.trader, self.trader),
            )
            fresh = []
            for identifier, existing, facts, inherited, part, body in cur.fetchall():
                item = inputs[identifier]
                if existing is not None:
                    if facts != item["facts"]:
                        raise ShadowError("conflicting_shadow_input")
                    continue
                item["status"] = "pending"
                if inherited is not None:
                    try:
                        matches = (
                            part == "memory"
                            and digest(encoded(event_facts(loads(body)))) == item["facts"]
                        )
                    except (ValueError, KeyError, TypeError):
                        matches = False
                    if not matches:
                        raise ShadowError("shadow_record_identity_collision")
                    item["status"] = "historical"
                fresh.append(item)
            if fresh:
                cur.execute(
                    """
                    INSERT pm_inputs(trader,id,facts,envelope,status)
                        SELECT TOP(500) %s,id,facts,envelope,status FROM OPENJSON(%s)
                        WITH(ordinal int '$.ordinal',id nvarchar(256) '$.id',
                             facts char(64) '$.facts',envelope nvarchar(max) '$.envelope',
                             status varchar(16) '$.status') ORDER BY ordinal
                    """,
                    (self.trader, encoded(fresh)),
                )
            if had_cursor:
                cur.execute(
                    "UPDATE pm_cursors SET seq=%s WHERE trader=%s AND source=%s",
                    (previous, self.trader, source),
                )
            else:
                cur.execute("INSERT pm_cursors VALUES(%s,%s,%s)", (self.trader, source, previous))

    def pending(self):
        with self.transaction() as cur:
            self._head(cur)
            cur.execute(
                "SELECT TOP(1) envelope FROM pm_inputs WHERE trader=%s "
                "AND status='pending' ORDER BY CASE WHEN EXISTS(SELECT 1 FROM pm_runtime r "
                "WHERE r.trader=pm_inputs.trader AND r.k='reconsideration:'+pm_inputs.id) "
                "THEN 0 ELSE 1 END,pk",
                (self.trader,),
            )
            row = cur.fetchone()
            return loads(row[0]) if row else None

    def context(self, moment, query="", *, history=True, revision=None):
        with self.transaction() as cur:
            head = self._head(cur)
            if revision is not None and (head["revision"] != revision or head["paused"]):
                raise ShadowError("shadow_revision_changed")
            cur.execute(
                "SELECT part,body FROM pm_records WHERE trader=%s "
                "AND part IN ('identity','playbook')",
                (self.trader,),
            )
            prefix = dict(cur.fetchall())
            recent, memories = [], []
            if history:
                recent, memories = self._context_evidence(cur, head["working"], moment, query)
            return {
                "revision": head["revision"],
                "paused": head["paused"],
                "identity": prefix["identity"],
                "playbook": {"version": head["playbook_version"], "text": prefix["playbook"]},
                "working_memory": head["working"],
                "recent_journal": recent,
                "retrieved_memories": memories,
                **(
                    {"memory_policy": policy_context(policy)}
                    if (policy := self._runtime(cur, "memory_policy"))
                    else {}
                ),
            }

    def _context_evidence(self, cur, working, moment, query):
        cur.execute(
            "SELECT TOP(10) meta,body FROM pm_records WHERE trader=%s "
            "AND part='journal' AND ts<=%s "
            "AND COALESCE(JSON_VALUE(CASE WHEN ISJSON(body)=1 THEN body ELSE N'{}' END,"
            "'$.status'),'')<>'price_coalesced' ORDER BY pk DESC",
            (self.trader, moment.replace(tzinfo=None)),
        )
        recent = [record_view(loads(meta), body) for meta, body in reversed(cur.fetchall())]
        # Current working views persist regardless of retrieval. A recent
        # audit trace is not automatically relevant to the incoming event.
        recent = [row for row in recent if not query or relevance(row, query)][-3:]
        memories = self._search(cur, query, moment)
        active_query = " ".join(
            [
                *working["views"],
                *working["waiting_for"],
                *(row["instrument"] + " " + row["thesis"] for row in working["position_theses"]),
            ]
        )
        if active_query:
            active = self._search(cur, active_query, moment)
            combined = {row["metadata"]["record_id"]: row for row in [*memories[:3], *active[:3]]}
            memories = list(combined.values())
        return recent, memories

    def _search(self, cur, query, moment):
        identifier, _, focus = query.partition(" | ")
        cur.execute(
            "SELECT meta,body FROM pm_records WHERE trader=%s AND id=%s "
            "AND part IN ('memory','review','journal','playbook_history') AND ts<=%s",
            (self.trader, identifier[:500], moment.replace(tzinfo=None)),
        )
        exact = cur.fetchone()
        if exact:
            return [record_view(loads(exact[0]), exact[1], focus)]
        tokens = query_terms(query)
        if not tokens:
            return []
        # Transactional inverted index avoids an asynchronous full-text index's
        # visibility gap immediately after commit/import. No narrative is SQL.
        placeholders = ",".join(["%s"] * len(tokens))
        condition = " OR ".join('"' + term + '"' for term in tokens)
        candidates = []
        # Partition candidates so recursive legacy journal traces cannot crowd
        # original source events/reviews out of the archive search.
        for journal in (False, True):
            part_filter = (
                "r.part='journal'"
                if journal
                else "r.part IN ('memory','review','playbook_history')"
            )
            cur.execute(
                "DECLARE @query nvarchar(4000)=%s; "
                "SELECT TOP(40) r.meta,r.body,s.hits FROM pm_records r JOIN ("
                "SELECT record_pk,SUM(score) AS hits FROM ("
                f"SELECT record_pk,COUNT(*) AS score FROM pm_terms WHERE term IN ({placeholders}) "
                "GROUP BY record_pk UNION ALL "
                "SELECT [KEY],RANK FROM CONTAINSTABLE(pm_records,body,@query)) hits "
                "GROUP BY record_pk"
                ") s ON r.pk=s.record_pk WHERE r.trader=%s AND r.ts<=%s "
                f"AND {part_filter} "
                "ORDER BY s.hits DESC,r.pk DESC",
                (condition, *tokens, self.trader, moment.replace(tzinfo=None)),
            )
            while cur.description is None:
                if not cur.nextset():
                    raise ShadowError("memory_search_result_missing")
            candidates.extend(
                record_view(loads(meta), body, query) for meta, body, _ in cur.fetchall()
            )
        candidates = [row for row in candidates if relevance(row, query)]
        candidates.sort(
            key=lambda row: (relevance(row, query), row["metadata"]["ts"]), reverse=True
        )
        return candidates[:5]

    def search(self, query, moment):
        if not isinstance(query, str) or not 1 <= len(query) <= 500:
            raise ShadowError("invalid_memory_query")
        with self.transaction() as cur:
            return self._search(cur, query, moment)

    def runtime(self, key, default=None):
        with self.transaction() as cur:
            cur.execute("SELECT v FROM pm_runtime WHERE trader=%s AND k=%s", (self.trader, key))
            row = cur.fetchone()
            return loads(row[0]) if row else default

    def set_runtime(self, key, value):
        # Raw SQL state is separate from the smaller model-visible projection.
        # Keep full source facts; phase_context factors them within prompt guards.
        if key != "feed_status" or len(encoded(value).encode()) > 131072:
            raise ShadowError("invalid_runtime_status")
        with self.transaction() as cur:
            cur.execute("SELECT 1 FROM pm_runtime WHERE trader=%s AND k=%s", (self.trader, key))
            if cur.fetchone():
                cur.execute(
                    "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k=%s",
                    (encoded(value), self.trader, key),
                )
            else:
                cur.execute(
                    "INSERT pm_runtime VALUES(%s,%s,%s)", (self.trader, key, encoded(value))
                )

    def refresh_broker_facts(self, account, positions, moment):
        self.contracts.validate("account-snapshot", account)
        self.contracts.validate("position-snapshot", positions)
        if any(utc(value["as_of"]) > moment for value in (account, positions)):
            raise ShadowError("future_broker_snapshot")
        with self.transaction() as cur:
            current = self._runtime(cur, "broker_snapshots", {})
            for key, value in (("account", account), ("positions", positions)):
                if key not in current or utc(value["as_of"]) > utc(current[key]["as_of"]):
                    current[key] = value
            cur.execute(
                "SELECT 1 FROM pm_runtime WHERE trader=%s AND k='broker_snapshots'", (self.trader,)
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k='broker_snapshots'",
                    (encoded(current), self.trader),
                )
            else:
                cur.execute(
                    "INSERT pm_runtime VALUES(%s,'broker_snapshots',%s)",
                    (self.trader, encoded(current)),
                )

    def commit(self, envelope, revision, result, moment):
        """A stale concurrent model response cannot overwrite another decision."""
        identifier = event_id(envelope)
        with self.transaction() as cur:
            head = self._head(cur)
            if head["revision"] != revision or head["paused"]:
                raise ShadowError("shadow_revision_changed")
            cur.execute("SELECT MAX(ts) FROM pm_records WHERE trader=%s", (self.trader,))
            latest = cur.fetchone()[0]
            if latest and latest.replace(tzinfo=UTC) > moment:
                raise ShadowError("shadow_clock_went_backwards")
            cur.execute(
                "SELECT status FROM pm_inputs WHERE trader=%s AND id=%s", (self.trader, identifier)
            )
            row = cur.fetchone()
            if not row or row[0] != "pending":
                raise ShadowError("shadow_input_not_pending")
            # Retain the complete input as private evidence, including analysis.
            self._insert(
                cur, metadata(identifier, "memory", stamp(moment), self.trader), encoded(envelope)
            )
            reply = result.get("reply")
            result["decision"] = None
            if reply:
                if result.get("memory_repair"):
                    self._evidence(cur, result["memory_repair"]["evidence_ids"], moment)
                self.contracts.validate("model-reply", reply)
                if len(reply["evidence_ids"]) > 256:
                    raise ShadowError("unavailable_shadow_evidence")
                known = {identifier}
                for ref in set(reply["evidence_ids"]):
                    cur.execute(
                        "SELECT id FROM pm_records WHERE trader=%s AND id=%s "
                        "AND part<>'working_memory' AND ts<=%s",
                        (self.trader, ref, moment.replace(tzinfo=None)),
                    )
                    if cur.fetchone():
                        known.add(ref)
                if not set(reply["evidence_ids"]) <= known:
                    raise ShadowError("unavailable_shadow_evidence")
                if reply["tool_calls"]:
                    raise ShadowError("unexecuted_shadow_tools")
                result["decision"] = {
                    "schema_version": 1,
                    "decision_id": "outcome:" + digest(identifier),
                    "causation_id": identifier,
                    "action": reply["action"],
                    "reasoning": reply["reasoning"],
                    "evidence_ids": reply["evidence_ids"],
                    "playbook_version": head["playbook_version"],
                    "override": False,
                    "ts": stamp(moment),
                }
                for field in ("instrument", "side", "size"):
                    if field in reply:
                        result["decision"][field] = reply[field]
                self.contracts.validate("decision-record", result["decision"])
                if any(
                    utc(x["as_of"]) > moment for x in reply["working_memory"]["position_theses"]
                ):
                    raise ShadowError("future_shadow_memory")
                working = reply["working_memory"]
                if self._runtime(cur, "memory_policy"):
                    dispositions = reply.get("memory_dispositions", [])
                    refs = transition(head["working"], working, dispositions, moment)
                    self._evidence(cur, refs, moment)
                    if working != head["working"] and not reply["evidence_ids"]:
                        raise ShadowError("working_memory_evidence")
                    self._working_checkpoint(
                        cur, head, working, dispositions, moment, refs | set(reply["evidence_ids"])
                    )
                else:
                    self._write_working(cur, working, moment)
            self._insert(
                cur,
                metadata(
                    "outcome:" + digest(identifier),
                    "journal",
                    stamp(moment),
                    self.trader,
                    sorted({identifier, *(reply["evidence_ids"] if reply else [])}),
                ),
                encoded(result),
            )
            cur.execute(
                "INSERT pm_outcomes(trader,event_id,result) VALUES(%s,%s,%s)",
                (self.trader, identifier, encoded(result)),
            )
            cur.execute(
                "UPDATE pm_inputs SET status='complete' WHERE trader=%s AND id=%s",
                (self.trader, identifier),
            )
            for key, value in result.get("runtime_updates", {}).items():
                if key == "broker_snapshots":
                    current = self._runtime(cur, key, {})
                    value = {
                        **value,
                        **{
                            name: snapshot
                            for name, snapshot in current.items()
                            if name not in value
                            or utc(snapshot["as_of"]) >= utc(value[name]["as_of"])
                        },
                    }
                cur.execute("SELECT 1 FROM pm_runtime WHERE trader=%s AND k=%s", (self.trader, key))
                if cur.fetchone():
                    cur.execute(
                        "UPDATE pm_runtime SET v=%s WHERE trader=%s AND k=%s",
                        (encoded(value), self.trader, key),
                    )
                else:
                    cur.execute(
                        "INSERT pm_runtime VALUES(%s,%s,%s)", (self.trader, key, encoded(value))
                    )
            cur.execute("UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (self.trader,))
            self._set_runtime(cur, "reasoning_hold", None)

    def retained(self):
        with self.transaction() as cur:
            head = self._head(cur)
            cur.execute(
                "SELECT meta,body FROM pm_records WHERE trader=%s ORDER BY pk", (self.trader,)
            )
            records = [(loads(meta), body) for meta, body in cur.fetchall()]
            cur.execute(
                "SELECT COALESCE(MAX(pk),0) FROM pm_outcomes WHERE trader=%s", (self.trader,)
            )
            head["watermark"] = str(cur.fetchone()[0])
            return head, records

    @contextmanager
    def stream_retained(self, *, snapshot=True):
        """One consistent checkpoint, with at most one SQL record in Python."""
        with stream_transaction(self.connect, "shadow:" + self.trader, snapshot=snapshot) as (
            cur,
            isolation,
        ):
            started = perf_counter()
            head = self._head(cur)
            head["checkpoint_isolation"] = isolation
            cur.execute(
                "SELECT COALESCE(MAX(pk),0) FROM pm_outcomes WHERE trader=%s", (self.trader,)
            )
            head["watermark"] = str(cur.fetchone()[0])
            cur.execute(
                "SELECT meta,body FROM pm_records WHERE trader=%s ORDER BY pk", (self.trader,)
            )

            def records():
                while row := cur.fetchone():
                    yield loads(row[0]), row[1]

            try:
                yield head, records()
            finally:
                duration = perf_counter() - started
                head["checkpoint_transaction_seconds"] = duration
                head["checkpoint_lock_seconds"] = duration if isolation == "locked" else 0.0

    def clone_stream(self, bundle, moment, binding, expected_identity, *, interrupt=None):
        """Publish a completely validated file stream in one rollback-safe transaction."""
        identity, version = None, None
        for meta, body in bundle.records():
            if meta["part"] == "identity":
                identity = body
            elif meta["part"] == "playbook":
                version = meta["playbook_version"]
            elif meta["part"] == "working_memory":
                self.contracts.validate("working-memory", loads(body))
        if identity != expected_identity:
            raise ShadowError("clone_identity_policy_mismatch")
        if not version or len(binding) != 64:
            raise ShadowError("clone_configuration_required")
        fresh = {
            "views": [],
            "position_theses": [],
            "waiting_for": ["Refresh inherited context in shadow"],
        }
        manifest = bundle.manifest
        with self.transaction() as cur:
            started = perf_counter()
            cur.execute("SELECT 1 FROM pm_minds WHERE trader=%s", (self.trader,))
            if cur.fetchone():
                raise ShadowError("clone_requires_empty_destination")
            parent = {
                "trader_id": manifest["trader_id"],
                "lineage_id": manifest["lineage_id"],
                "checkpoint": manifest["checkpoint"],
                "compatibility": manifest["compatibility"],
            }
            cur.execute(
                "INSERT pm_minds VALUES(%s,%s,%s,0,1,%s,%s,%s,%s)",
                (
                    self.trader,
                    str(uuid4()),
                    binding,
                    stamp(moment),
                    encoded(fresh),
                    version,
                    encoded(parent),
                ),
            )
            for meta, body in bundle.records():
                if meta["part"] == "working_memory":
                    meta = {**meta, "part": "memory"}
                self._insert(cur, meta, body)
                if interrupt:
                    interrupt()
            self._insert(
                cur,
                metadata(
                    "working:" + digest(self.trader), "working_memory", stamp(moment), self.trader
                ),
                encoded(fresh),
            )
            transaction_seconds = perf_counter() - started
        return {
            "paused": True,
            "destination_transaction_seconds": transaction_seconds,
            "source_checkpoint": manifest["checkpoint"]["checkpoint_id"],
            "transformations": [
                "new_lineage",
                "source_working_archived",
                "active_working_reset",
                "destination_binding_only",
                "no_source_authority",
            ],
        }

    def status(self):
        with self.transaction() as cur:
            head = self._head(cur)
            cur.execute("SELECT COUNT(*) FROM pm_outcomes WHERE trader=%s", (self.trader,))
            outcomes = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM pm_inputs WHERE trader=%s AND status='pending'",
                (self.trader,),
            )
            return {
                "mode": "shadow",
                "paused": head["paused"],
                "revision": head["revision"],
                "outcomes": outcomes,
                "pending": cur.fetchone()[0],
                "dispatches": 0,
                "reasoning_hold": self._runtime(cur, "reasoning_hold"),
            }

    def outcome(self, identifier):
        with self.transaction() as cur:
            cur.execute(
                "SELECT result FROM pm_outcomes WHERE trader=%s AND event_id=%s",
                (self.trader, identifier),
            )
            row = cur.fetchone()
            return loads(row[0]) if row else None

    def clone(self, records, manifest, moment, binding, expected_identity, *, interrupt=None):
        """Stage validated records/index and activate once, paused, in one transaction."""
        identity = next(body for meta, body in records if meta["part"] == "identity")
        if identity != expected_identity:
            raise ShadowError("clone_identity_policy_mismatch")
        working = next((meta, body) for meta, body in records if meta["part"] == "working_memory")
        source_working = deepcopy(working[0])
        source_working["part"] = "memory"
        fresh = {
            "views": [],
            "position_theses": [],
            "waiting_for": ["Refresh inherited context in shadow"],
        }
        records = [(meta, body) for meta, body in records if meta["part"] != "working_memory"]
        records += [
            (source_working, working[1]),
            (
                metadata(
                    "working:" + digest(self.trader), "working_memory", stamp(moment), self.trader
                ),
                encoded(fresh),
            ),
        ]
        version = next(
            meta["playbook_version"] for meta, _ in records if meta["part"] == "playbook"
        )
        if not version or len(binding) != 64:
            raise ShadowError("clone_configuration_required")
        with self.transaction() as cur:
            cur.execute("SELECT 1 FROM pm_minds WHERE trader=%s", (self.trader,))
            if cur.fetchone():
                raise ShadowError("clone_requires_empty_destination")
            parent = {
                "trader_id": manifest["trader_id"],
                "lineage_id": manifest["lineage_id"],
                "checkpoint": manifest["checkpoint"],
                "compatibility": manifest["compatibility"],
            }
            cur.execute(
                "INSERT pm_minds VALUES(%s,%s,%s,0,1,%s,%s,%s,%s)",
                (
                    self.trader,
                    str(uuid4()),
                    binding,
                    stamp(moment),
                    encoded(fresh),
                    version,
                    encoded(parent),
                ),
            )
            for meta, body in records:
                self._insert(cur, meta, body)
                if interrupt:
                    interrupt()
        return {
            "paused": True,
            "source_checkpoint": manifest["checkpoint"]["checkpoint_id"],
            "transformations": [
                "new_lineage",
                "source_working_archived",
                "active_working_reset",
                "destination_binding_only",
                "no_source_authority",
            ],
        }
