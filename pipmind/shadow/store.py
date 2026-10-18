"""Transactional five-part mind, immutable event inbox and bounded SQL retrieval."""

from copy import deepcopy
from datetime import UTC
from uuid import uuid4

from pipmind.contracts import Contracts, event_facts, event_id, loads, stamp, utc, validate_event
from pipmind.shadow.common import ShadowError, digest, encoded, terms
from pipmind.shadow.context import query_terms, record_view, relevance, semantic_text
from pipmind.shadow.sql import transaction

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
        for term in terms(semantic_text(record_view(meta, body)["body"])):
            cur.execute("INSERT pm_terms VALUES(%s,%s)", (term, pk))

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
            for item in batch:
                if not isinstance(item, dict) or not {"sequence", "event"} <= set(item):
                    raise ShadowError("invalid_input_page")
                seq, envelope = item["sequence"], item["event"]
                if type(seq) is not int or seq <= previous:
                    raise ShadowError("input_cursor_not_increasing")
                if validate_event(envelope, self.contracts) > moment:
                    raise ShadowError("input_not_yet_available")
                identifier = event_id(envelope)
                if len(identifier) > 256 or len(encoded(envelope).encode()) > 131072:
                    raise ShadowError("shadow_input_bound")
                facts = digest(encoded(event_facts(envelope)))
                cur.execute(
                    "SELECT facts FROM pm_inputs WHERE trader=%s AND id=%s",
                    (self.trader, identifier),
                )
                old = cur.fetchone()
                if old and old[0] != facts:
                    raise ShadowError("conflicting_shadow_input")
                if not old:
                    cur.execute(
                        "SELECT part,body FROM pm_records WHERE trader=%s AND id=%s",
                        (self.trader, identifier),
                    )
                    inherited = cur.fetchone()
                    status = "pending"
                    if inherited:
                        try:
                            matches = (
                                inherited[0] == "memory"
                                and digest(encoded(event_facts(loads(inherited[1])))) == facts
                            )
                        except (ValueError, KeyError, TypeError):
                            matches = False
                        if not matches:
                            raise ShadowError("shadow_record_identity_collision")
                        status = "historical"
                    cur.execute(
                        "INSERT pm_inputs(trader,id,facts,envelope,status) VALUES(%s,%s,%s,%s,%s)",
                        (self.trader, identifier, facts, encoded(envelope), status),
                    )
                previous = seq
            if row:
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
                "AND status='pending' ORDER BY pk",
                (self.trader,),
            )
            row = cur.fetchone()
            return loads(row[0]) if row else None

    def context(self, moment, query=""):
        with self.transaction() as cur:
            head = self._head(cur)
            cur.execute(
                "SELECT part,body FROM pm_records WHERE trader=%s "
                "AND part IN ('identity','playbook')",
                (self.trader,),
            )
            prefix = dict(cur.fetchall())
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
            working = head["working"]
            active_query = " ".join(
                [
                    *working["views"],
                    *working["waiting_for"],
                    *(
                        row["instrument"] + " " + row["thesis"]
                        for row in working["position_theses"]
                    ),
                ]
            )
            if active_query:
                active = self._search(cur, active_query, moment)
                combined = {
                    row["metadata"]["record_id"]: row for row in [*memories[:3], *active[:3]]
                }
                memories = list(combined.values())
            return {
                "revision": head["revision"],
                "paused": head["paused"],
                "identity": prefix["identity"],
                "playbook": {"version": head["playbook_version"], "text": prefix["playbook"]},
                "working_memory": head["working"],
                "recent_journal": recent,
                "retrieved_memories": memories,
            }

    def _search(self, cur, query, moment):
        cur.execute(
            "SELECT meta,body FROM pm_records WHERE trader=%s AND id=%s "
            "AND part IN ('memory','review','journal','playbook_history') AND ts<=%s",
            (self.trader, query[:500], moment.replace(tzinfo=None)),
        )
        exact = cur.fetchone()
        if exact:
            return [record_view(loads(exact[0]), exact[1])]
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
        if key != "feed_status" or len(encoded(value).encode()) > 16384:
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
                cur.execute(
                    "SELECT id FROM pm_records WHERE trader=%s AND part='working_memory'",
                    (self.trader,),
                )
                working_id = cur.fetchone()[0]
                cur.execute(
                    "UPDATE pm_minds SET working=%s WHERE trader=%s",
                    (encoded(working), self.trader),
                )
                cur.execute(
                    "UPDATE pm_records SET body=%s,ts=%s,meta=%s "
                    "WHERE trader=%s AND part='working_memory'",
                    (
                        encoded(working),
                        moment.replace(tzinfo=None),
                        encoded(metadata(working_id, "working_memory", stamp(moment), self.trader)),
                        self.trader,
                    ),
                )
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
