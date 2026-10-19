"""Owner library and durable policy boundary, sharing the mind's SQL lock.

Only shadow activation is supported. This is not M5 broker ownership/recovery.
Content is immutable in saves; metadata and compact tombstones are independent.
"""

import difflib
from datetime import timedelta
from uuid import uuid4

from pipmind.contracts import loads, stamp, utc
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.robot import (
    blank_content,
    content_hash,
    export_robot,
    import_robot,
    label,
    validate_content,
)


class RobotLibrary:
    def __init__(self, mind):
        self.mind = mind

    @property
    def trader(self):
        return self.mind.trader

    def _state(self, cur):
        head = self.mind._head(cur)
        cur.execute("SELECT state FROM pm_robot_state WHERE trader=%s", (self.trader,))
        row = cur.fetchone()
        if row:
            return loads(row[0])
        cur.execute(
            "SELECT part,body FROM pm_records WHERE trader=%s AND part IN ('identity','playbook')",
            (self.trader,),
        )
        prefix = dict(cur.fetchall())
        content = validate_content({**prefix, "guides": "", "lessons": ""})
        state = {
            "content": content,
            "sha256": content_hash(content),
            "version": 0,
            "generation": 0,
            "auto_learn": True,
            "cutoff": 0,
            "retention_days": 30,
            "retention_count": 30,
            "restored_from": None,
            "origin": {"lineage": head["lineage"], "revision": head["revision"]},
        }
        cur.execute("INSERT pm_robot_state VALUES(%s,%s)", (self.trader, encoded(state)))
        return state

    def _write(self, cur, state):
        cur.execute(
            "UPDATE pm_robot_state SET state=%s WHERE trader=%s", (encoded(state), self.trader)
        )

    def _audit(self, cur, moment, detail):
        cur.execute(
            "INSERT pm_robot_audit(trader,ts,detail) VALUES(%s,%s,%s)",
            (self.trader, stamp(moment), encoded(detail)),
        )

    def _save(self, cur, content, moment, kind, name, note, provenance):
        identifier = str(uuid4())
        cur.execute(
            "INSERT pm_robot_saves VALUES(%s,%s,%s,%s,%s,%s,0,%s,%s,%s)",
            (
                self.trader,
                identifier,
                stamp(moment),
                kind,
                name,
                note,
                content_hash(content),
                encoded(content),
                encoded(provenance),
            ),
        )
        self._audit(
            cur,
            moment,
            {
                "action": "save",
                "id": identifier,
                "kind": kind,
                "sha256": content_hash(content),
                "provenance": provenance,
            },
        )
        return identifier

    def _get(self, cur, identifier):
        if not isinstance(identifier, str) or len(identifier) != 36:
            raise ShadowError("robot_save_missing")
        cur.execute(
            "SELECT content,sha256,provenance FROM pm_robot_saves "
            "WHERE trader=%s AND id=%s AND content IS NOT NULL",
            (self.trader, identifier),
        )
        row = cur.fetchone()
        if not row:
            raise ShadowError("robot_save_missing")
        content = validate_content(loads(row[0]))
        if content_hash(content) != row[1]:
            raise ShadowError("robot_save_integrity")
        return content, loads(row[2])

    def status(self):
        with self.mind.transaction() as cur:
            state = self._state(cur)
            head = self.mind._head(cur)
            cur.execute(
                "SELECT id,created,kind,name,note,favorite,sha256 FROM pm_robot_saves "
                "WHERE trader=%s AND content IS NOT NULL ORDER BY created DESC,id",
                (self.trader,),
            )
            keys = ("id", "created", "kind", "name", "note", "favorite", "sha256")
            return {
                "active": {**state, "revision": head["revision"], "paused": head["paused"]},
                "saves": [dict(zip(keys, row, strict=True)) for row in cur.fetchall()],
            }

    def content(self, identifier):
        with self.mind.transaction() as cur:
            self.mind._head(cur)
            return self._get(cur, identifier)[0]

    def export(self, identifier):
        with self.mind.transaction() as cur:
            self.mind._head(cur)
            content, provenance = self._get(cur, identifier)
            return export_robot(content, {"save_id": identifier, "version": provenance["version"]})

    def import_archive(self, raw, moment, name="Imported robot"):
        content, manifest = import_robot(raw, with_manifest=True)
        name = label(name, 120)
        with self.mind.transaction() as cur:
            self._state(cur)
            return self._save(
                cur,
                content,
                moment,
                "import",
                name,
                "",
                {
                    "import_export_id": manifest["export_id"],
                    "source": manifest["source"],
                    "version": manifest["source"]["version"] if manifest["source"] else 0,
                    "archive_sha256": digest(raw),
                },
            )

    def save_copy(self, moment, *, source=None, content=None, name="", note="", kind="manual"):
        name, note = label(name, 120), label(note, 500)
        if kind not in {"manual", "import", "blank"} or (
            source is not None and content is not None
        ):
            raise ShadowError("robot_invalid_save")
        if content is not None:
            content = validate_content(content)
        with self.mind.transaction() as cur:
            state = self._state(cur)
            source_version = state["version"]
            if source is not None:
                content, source_provenance = self._get(cur, source)
                source_version = source_provenance["version"]
            content = state["content"] if content is None else content
            return self._save(
                cur,
                content,
                moment,
                kind,
                name,
                note,
                {
                    "source_save": source,
                    "source_sha256": content_hash(content),
                    "origin": state["origin"],
                    "version": source_version,
                },
            )

    def add_blank(self, moment):
        return self.save_copy(moment, content=blank_content(), name="Blank slate", kind="blank")

    def favorite(self, identifier, favorite, moment):
        if type(favorite) is not bool:
            raise ShadowError("robot_invalid_favorite")
        with self.mind.transaction() as cur:
            self.mind._head(cur)
            self._get(cur, identifier)
            cur.execute(
                "UPDATE pm_robot_saves SET favorite=%s WHERE trader=%s AND id=%s",
                (favorite, self.trader, identifier),
            )
            self._audit(cur, moment, {"action": "favorite", "id": identifier, "value": favorite})

    def set_auto_learn(self, enabled, moment):
        if type(enabled) is not bool:
            raise ShadowError("robot_invalid_auto_learn")
        with self.mind.transaction() as cur:
            state = self._state(cur)
            if state["auto_learn"] != enabled:
                state.update(auto_learn=enabled, generation=state["generation"] + 1)
                self._write(cur, state)
                cur.execute(
                    "UPDATE pm_minds SET revision=revision+1 WHERE trader=%s", (self.trader,)
                )
                self._audit(
                    cur,
                    moment,
                    {
                        "action": "auto_learn",
                        "enabled": enabled,
                        "generation": state["generation"],
                        "sha256": state["sha256"],
                    },
                )
            return {key: state[key] for key in ("auto_learn", "generation", "sha256", "version")}

    def preview(self, identifier, moment):
        with self.mind.transaction() as cur:
            state = self._state(cur)
            head = self.mind._head(cur)
            content, _ = self._get(cur, identifier)
            plan = {
                "id": str(uuid4()),
                "save_id": identifier,
                "revision": head["revision"],
                "generation": state["generation"],
                "base_sha256": state["sha256"],
                "target_sha256": content_hash(content),
                "expires": stamp(moment + timedelta(minutes=10)),
                "diff": "\n".join(
                    line
                    for key in ("identity", "playbook", "guides", "lessons")
                    for line in difflib.unified_diff(
                        state["content"][key].splitlines(keepends=True),
                        content[key].splitlines(keepends=True),
                        fromfile="active/" + key + ".md",
                        tofile="saved/" + key + ".md",
                    )
                ),
                "effects": [
                    "pre-load undo save",
                    "new active copy",
                    "shadow paused",
                    "previous views and waiting conditions cleared",
                    "prior learned retrieval excluded; source facts retained",
                    "account facts, history, position obligations and Auto Learn preserved",
                ],
            }
            cur.execute(
                "DELETE pm_robot_previews WHERE trader=%s AND JSON_VALUE(plan,'$.expires')<%s",
                (self.trader, stamp(moment)),
            )
            cur.execute(
                "INSERT pm_robot_previews VALUES(%s,%s,%s)",
                (self.trader, plan["id"], encoded(plan)),
            )
            return plan

    def load(self, preview_id, target_sha256, moment, *, interrupt=None):
        if (
            not isinstance(preview_id, str)
            or len(preview_id) != 36
            or not isinstance(target_sha256, str)
            or len(target_sha256) != 64
        ):
            raise ShadowError("robot_confirmation_required")
        with self.mind.transaction() as cur:
            state = self._state(cur)
            head = self.mind._head(cur)
            cur.execute(
                "SELECT plan FROM pm_robot_previews WHERE trader=%s AND id=%s",
                (self.trader, preview_id),
            )
            row = cur.fetchone()
            if not row:
                raise ShadowError("robot_confirmation_missing")
            plan = loads(row[0])
            if (
                utc(plan["expires"]) <= moment
                or plan["revision"] != head["revision"]
                or plan["generation"] != state["generation"]
                or plan["base_sha256"] != state["sha256"]
                or target_sha256 != plan["target_sha256"]
            ):
                raise ShadowError("robot_confirmation_stale")
            content, provenance = self._get(cur, plan["save_id"])
            if content_hash(content) != target_sha256:
                raise ShadowError("robot_save_integrity")
            undo = self._save(
                cur,
                state["content"],
                moment,
                "undo",
                "Before load",
                "",
                {"version": state["version"], "origin": state["origin"]},
            )
            cur.execute(
                "SELECT COALESCE(MAX(pk),0) FROM pm_records WHERE trader=%s", (self.trader,)
            )
            cutoff = cur.fetchone()[0]
            previous = dict(state)
            state.update(
                content=content,
                sha256=target_sha256,
                version=state["version"] + 1,
                generation=state["generation"] + 1,
                cutoff=cutoff,
                restored_from=plan["save_id"],
                undo_save=undo,
                origin={"source_save": plan["save_id"], "source_sha256": target_sha256},
            )
            self._apply(cur, state, moment)
            working = {
                "views": [],
                "waiting_for": [],
                "position_theses": head["working"]["position_theses"],
            }
            self.mind._write_working(cur, working, moment)
            cur.execute(
                "UPDATE pm_inputs SET status='superseded' WHERE trader=%s "
                "AND status='pending' "
                "AND JSON_VALUE(envelope,'$.payload.reason')='reconsideration'",
                (self.trader,),
            )
            # The former memory-maintenance chain is audit, not the new robot's views.
            cur.execute(
                "DELETE pm_runtime WHERE trader=%s AND (k='memory_policy' "
                "OR k='reasoning_hold' OR k LIKE %s)",
                (self.trader, "reconsideration:%"),
            )
            self._audit(
                cur,
                moment,
                {
                    "action": "load",
                    "save_id": plan["save_id"],
                    "undo": undo,
                    "before": previous,
                    "after": state,
                    "previous_working": head["working"],
                },
            )
            cur.execute("UPDATE pm_minds SET paused=1 WHERE trader=%s", (self.trader,))
            cur.execute("DELETE pm_robot_previews WHERE trader=%s", (self.trader,))
            if interrupt:
                interrupt()
            return {
                "paused": True,
                "undo_save": undo,
                "version": state["version"],
                "sha256": state["sha256"],
                "dispatches": 0,
            }

    def _apply(self, cur, state, moment):
        version = "robot-" + str(state["version"]) + "-" + state["sha256"][:16]
        # Existing five-part exports keep the actual active identity/playbook.
        for part in ("identity", "playbook"):
            cur.execute(
                "SELECT meta FROM pm_records WHERE trader=%s AND part=%s", (self.trader, part)
            )
            meta = loads(cur.fetchone()[0])
            meta["ts"] = stamp(moment)
            if part == "playbook":
                meta["playbook_version"] = version
            cur.execute(
                "UPDATE pm_records SET body=%s,meta=%s,ts=%s WHERE trader=%s AND part=%s",
                (
                    state["content"][part],
                    encoded(meta),
                    moment.replace(tzinfo=None),
                    self.trader,
                    part,
                ),
            )
        self._write(cur, state)
        cur.execute(
            "UPDATE pm_minds SET revision=revision+1,playbook_version=%s WHERE trader=%s",
            (version, self.trader),
        )

    def learning_ticket(self):
        """Call before any future mutation-capable review/model call, not afterward."""
        with self.mind.transaction() as cur:
            state = self._state(cur)
            if not state["auto_learn"]:
                raise ShadowError("robot_learning_disabled")
            return {key: state[key] for key in ("generation", "sha256", "version")}

    def commit_learning(self, ticket, content, evidence_ids, moment, *, interrupt=None):
        """Storage gate for future M7 writers; does not implement review/evidence grading."""
        content = validate_content(content)
        if not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= 64:
            raise ShadowError("robot_learning_evidence")
        with self.mind.transaction() as cur:
            state = self._state(cur)
            if not state["auto_learn"]:
                raise ShadowError("robot_learning_disabled")
            if ticket != {key: state[key] for key in ("generation", "sha256", "version")}:
                raise ShadowError("robot_learning_stale")
            if content["identity"] != state["content"]["identity"]:
                raise ShadowError("robot_protected_identity")
            for identifier in evidence_ids:
                cur.execute(
                    "SELECT meta,ts FROM pm_records WHERE trader=%s AND id=%s "
                    "AND part='journal' AND pk>%s",
                    (self.trader, identifier, state["cutoff"]),
                )
                row = cur.fetchone()
                if not row or loads(row[0])["override"] or row[1] > moment.replace(tzinfo=None):
                    raise ShadowError("robot_learning_evidence")
            before = dict(state)
            saved = self._save(
                cur,
                state["content"],
                moment,
                "auto",
                "Before learning",
                "",
                {"version": state["version"], "origin": state["origin"]},
            )
            state.update(
                content=content, sha256=content_hash(content), version=state["version"] + 1
            )
            self._apply(cur, state, moment)
            self._audit(
                cur,
                moment,
                {
                    "action": "learn",
                    "before": before,
                    "after": state,
                    "autosave": saved,
                    "evidence_ids": evidence_ids,
                },
            )
            if interrupt:
                interrupt()
            return saved

    def retention(self, days, count, moment):
        if (
            type(days) is not int
            or type(count) is not int
            or not 1 <= days <= 3650
            or not 1 <= count <= 1000
        ):
            raise ShadowError("robot_invalid_retention")
        with self.mind.transaction() as cur:
            state = self._state(cur)
            state.update(retention_days=days, retention_count=count)
            self._write(cur, state)
            self._audit(cur, moment, {"action": "retention", "days": days, "count": count})

    def cleanup(self, moment):
        with self.mind.transaction() as cur:
            state = self._state(cur)
            cur.execute(
                "SELECT id,created FROM pm_robot_saves WHERE trader=%s "
                "AND content IS NOT NULL AND kind IN ('auto','undo') AND favorite=0 "
                "ORDER BY created DESC,id DESC",
                (self.trader,),
            )
            candidates = cur.fetchall()
            cur.execute("SELECT plan FROM pm_robot_previews WHERE trader=%s", (self.trader,))
            protected = {
                loads(row[0])["save_id"]
                for row in cur.fetchall()
                if utc(loads(row[0])["expires"]) > moment
            }
            if state["restored_from"]:
                protected.add(state["restored_from"])
            if state.get("undo_save"):
                protected.add(state["undo_save"])
            removed = []
            candidates = [
                (identifier, created)
                for identifier, created in candidates
                if identifier not in protected
            ]
            for i, (identifier, created) in enumerate(candidates):
                if i >= state["retention_count"] or utc(created) <= moment - timedelta(
                    days=state["retention_days"]
                ):
                    cur.execute(
                        "UPDATE pm_robot_saves SET content=NULL WHERE trader=%s AND id=%s",
                        (self.trader, identifier),
                    )
                    removed.append(identifier)
            if removed:
                self._audit(cur, moment, {"action": "cleanup", "removed": removed})
            return removed
