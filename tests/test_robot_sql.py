"""Real SQL library workflows, concurrent owner controls and generation boundaries."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from pipmind.contracts import stamp, utc
from pipmind.shadow.bundle import export_experience, import_experience
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.engine import ShadowEngine
from pipmind.shadow.library import RobotLibrary
from pipmind.shadow.model import FixtureModel
from pipmind.shadow.robot import export_robot, import_robot
from pipmind.shadow.service import create_app
from pipmind.shadow.sql import connector, fulltext, migrate
from pipmind.shadow.store import SqlMind, metadata

pytestmark = pytest.mark.skipif(
    os.environ.get("PIPMIND_SQL_TESTS") != "true", reason="separate SQL integration gate"
)
MOMENT = utc("2026-10-19T00:00:00Z")


@pytest.fixture
def mind():
    connect = connector()
    migrate(connect)
    fulltext(connect)
    store = SqlMind(connect, "robot-" + uuid4().hex)
    store.initialize(
        "Standing mandate",
        {"version": "v1", "text": "Initial playbook"},
        stamp(MOMENT - timedelta(days=1)),
        binding=digest("synthetic-binding"),
    )
    return store


def evidence(mind, identifier="evidence", override=False):
    with mind.transaction() as cur:
        mind._insert(
            cur,
            metadata(identifier, "journal", stamp(MOMENT), mind.trader, override=override),
            "Observed a synthetic outcome",
        )
    return identifier


def load(library, identifier, moment=MOMENT):
    plan = library.preview(identifier, moment)
    return library.load(plan["id"], plan["target_sha256"], moment)


def learn(library, mind, text, moment=MOMENT):
    return library.commit_learning(
        library.learning_ticket(),
        {**library.status()["active"]["content"], "lessons": text},
        [evidence(mind, "evidence-" + uuid4().hex)],
        moment,
    )


def test_independent_saves_favorites_restore_and_account_separation(mind):
    library = RobotLibrary(mind)
    first = library.save_copy(MOMENT, name="Known good", note="Before exploring")
    original = library.content(first)
    library.favorite(first, True, MOMENT)
    copied = library.save_copy(MOMENT, source=first, name="Independent copy")
    autosave = learn(library, mind, "Discard this new lesson")
    assert library.content(autosave) == original == library.content(copied)
    assert library.status()["active"]["content"]["lessons"] != original["lessons"]
    mind.set_runtime("feed_status", {"coverage": "unchanged"})
    head, records = mind.retained()
    with mind.transaction() as cur:
        mind._set_runtime(cur, "broker_snapshots", {"account": {"balance": 123}, "positions": []})
        mind._set_runtime(cur, "risk-stop", {"stopped": True})
    result = load(library, first)
    active = RobotLibrary(SqlMind(mind.connect, mind.trader)).status()["active"]
    assert active["content"] == original and active["version"] == 2
    assert active["revision"] > head["revision"] and active["paused"]
    assert library.content(result["undo_save"])["lessons"] == "Discard this new lesson"
    assert library.content(first) == original
    assert mind.head()["binding"] == head["binding"] and mind.head()["lineage"] == head["lineage"]
    assert mind.runtime("broker_snapshots")["account"]["balance"] == 123
    assert (
        mind.runtime("risk-stop")["stopped"]
        and mind.runtime("feed_status")["coverage"] == "unchanged"
    )
    after = {meta["record_id"]: body for meta, body in mind.retained()[1]}
    assert all(
        after[meta["record_id"]] == body
        for meta, body in records
        if meta["part"] not in {"identity", "playbook", "working_memory"}
    )


def test_auto_learn_off_is_durable_load_independent_and_fences_old_tickets(mind):
    library = RobotLibrary(mind)
    saved = library.save_copy(MOMENT)
    ticket = library.learning_ticket()
    content = {**library.content(saved), "lessons": "Stale result"}
    refs = [evidence(mind)]
    acknowledged = library.set_auto_learn(False, MOMENT)
    assert acknowledged["sha256"] == ticket["sha256"]
    with pytest.raises(ShadowError, match="robot_learning_disabled"):
        library.learning_ticket()
    with pytest.raises(ShadowError, match="robot_learning_disabled"):
        library.commit_learning(ticket, content, refs, MOMENT)
    load(library, saved)
    fresh = RobotLibrary(SqlMind(mind.connect, mind.trader))
    assert fresh.status()["active"]["auto_learn"] is False
    fresh.set_auto_learn(True, MOMENT)
    with pytest.raises(ShadowError, match="robot_learning_stale"):
        fresh.commit_learning(ticket, content, refs, MOMENT)


def test_off_does_not_stop_observations_and_shadow_decisions(mind):
    library = RobotLibrary(mind)
    frozen = library.set_auto_learn(False, MOMENT)["sha256"]
    event = {
        "schema_version": 1,
        "kind": "ClockEvent",
        "message_id": "clock-off",
        "available_at": stamp(MOMENT),
        "payload": {
            "schema_version": 1,
            "event_id": "clock-off",
            "ts": stamp(MOMENT),
            "reason": "heartbeat",
        },
    }
    mind.accept("fixture", [{"sequence": 1, "event": event}], MOMENT)
    mind.set_paused(False)
    model = FixtureModel({"clock-off": {"triage": {"wake": False, "summary": "Current fact"}}})
    assert asyncio.run(ShadowEngine(mind, model, clock=lambda: MOMENT).step())
    assert mind.status()["outcomes"] == 1 and mind.status()["paused"] is False
    assert library.status()["active"]["sha256"] == frozen


def test_load_and_learning_failure_roll_back_snapshot_content_revision_and_audit(mind):
    library = RobotLibrary(mind)
    saved = library.add_blank(MOMENT)
    plan = library.preview(saved, MOMENT)
    before = library.status()

    def interrupt():
        raise ShadowError("injected_failure")

    with pytest.raises(ShadowError, match="injected_failure"):
        library.load(plan["id"], plan["target_sha256"], MOMENT, interrupt=interrupt)
    assert library.status() == before
    refs = [evidence(mind)]
    with pytest.raises(ShadowError, match="injected_failure"):
        library.commit_learning(
            library.learning_ticket(),
            {**before["active"]["content"], "lessons": "Won't commit"},
            refs,
            MOMENT,
            interrupt=interrupt,
        )
    assert library.status() == before
    library.load(plan["id"], plan["target_sha256"], MOMENT)
    with pytest.raises(ShadowError, match="robot_confirmation_missing"):
        library.load(plan["id"], plan["target_sha256"], MOMENT)


@pytest.mark.parametrize("change", ["pause", "learn", "toggle", "expire", "hash"])
def test_stale_or_inexact_confirmation_cannot_load(mind, change):
    library = RobotLibrary(mind)
    saved = library.add_blank(MOMENT)
    plan = library.preview(saved, MOMENT)
    moment, target = MOMENT, plan["target_sha256"]
    if change == "pause":
        mind.set_paused(True)
    if change == "learn":
        learn(library, mind, "New lesson")
    if change == "toggle":
        library.set_auto_learn(False, MOMENT)
    if change == "expire":
        moment += timedelta(minutes=10)
    if change == "hash":
        target = "0" * 64
    before = library.status()
    with pytest.raises(ShadowError, match="robot_confirmation_stale"):
        library.load(plan["id"], target, moment)
    assert library.status() == before


def test_restoration_excludes_discarded_policy_but_retains_original_facts_and_exact_save(mind):
    library = RobotLibrary(mind)
    saved = library.save_copy(MOMENT)
    with mind.transaction() as cur:
        for identifier, part, body in [
            ("discarded", "memory", "Quasar means buy every dip: discarded lesson"),
            ("old-review", "review", "Quasar learned policy"),
            (
                "fact",
                "memory",
                encoded(
                    {
                        "kind": "NewsEvent",
                        "message_id": "fact",
                        "available_at": stamp(MOMENT),
                        "payload": {"headline": "Quasar dated source fact"},
                    }
                ),
            ),
        ]:
            mind._insert(cur, metadata(identifier, part, stamp(MOMENT), mind.trader), body)
    assert {r["metadata"]["record_id"] for r in mind.search("Quasar", MOMENT)} >= {
        "discarded",
        "fact",
    }
    load(library, saved)
    assert [r["metadata"]["record_id"] for r in mind.search("Quasar", MOMENT)] == ["fact"]
    assert mind.search("discarded", MOMENT) == []
    assert mind.context(MOMENT, "Quasar")["learned_lessons"] == ""
    assert len(mind.retained()[1]) >= 6


def test_full_experience_clone_preserves_new_learned_material_and_retrieval_boundary(mind):
    library = RobotLibrary(mind)
    saved = library.save_copy(MOMENT)
    evidence(mind, "old-policy")
    load(library, saved)
    learn(library, mind, "Exact saved lesson\nnaïve ≠ unsafe\n")
    library.set_auto_learn(False, MOMENT)
    raw = export_experience(mind, MOMENT)
    clone = SqlMind(mind.connect, "clone-" + uuid4().hex)
    import_experience(clone, raw, MOMENT, digest("separate-binding"), "Standing mandate")
    active = RobotLibrary(clone).status()["active"]
    assert active["content"] == library.status()["active"]["content"]
    assert active["paused"] and active["auto_learn"]  # New owner controls, not imported authority.
    assert clone.search("old-policy", MOMENT) == []
    assert clone.context(MOMENT)["learned_lessons"] == "Exact saved lesson\nnaïve ≠ unsafe\n"


def test_retention_keeps_favorites_manual_imports_copies_and_pending_loads(mind):
    library = RobotLibrary(mind)
    manual = library.save_copy(MOMENT)
    imported = library.save_copy(
        MOMENT, content=import_robot(export_robot(library.content(manual))), kind="import"
    )
    autos = [learn(library, mind, f"Lesson {i}", MOMENT + timedelta(seconds=i)) for i in range(5)]
    library.favorite(autos[0], True, MOMENT)
    copied = library.save_copy(MOMENT, source=autos[1])
    library.preview(autos[1], MOMENT)
    library.retention(1, 1, MOMENT)
    removed = library.cleanup(MOMENT + timedelta(minutes=1))
    assert set(removed) == set(autos[2:4])
    assert all(
        library.content(key) for key in (manual, imported, autos[0], autos[1], copied, autos[4])
    )
    library.cleanup(MOMENT + timedelta(days=2))
    assert all(library.content(key) for key in (manual, imported, autos[0], copied))
    assert library.status()["active"]["content"]["lessons"] == "Lesson 4"


def test_concurrent_off_acknowledgment_cannot_be_followed_by_old_learning(mind):
    library = RobotLibrary(mind)
    ticket = library.learning_ticket()
    content = {**library.status()["active"]["content"], "lessons": "Concurrent result"}
    refs, barrier = [evidence(mind)], Barrier(2)

    def commit():
        barrier.wait()
        try:
            return library.commit_learning(ticket, content, refs, MOMENT)
        except ShadowError as error:
            return str(error)

    def freeze():
        barrier.wait()
        return library.set_auto_learn(False, MOMENT)

    with ThreadPoolExecutor(2) as pool:
        left, right = pool.submit(commit), pool.submit(freeze)
        result, acknowledgment = left.result(), right.result()
    assert library.status()["active"]["sha256"] == acknowledgment["sha256"]
    assert result == "robot_learning_disabled" or library.content(result)["lessons"] == ""
    with pytest.raises(ShadowError, match="robot_learning_disabled"):
        library.commit_learning(ticket, content, refs, MOMENT)


def test_learning_cannot_edit_identity_or_use_override_evidence(mind):
    library = RobotLibrary(mind)
    ticket = library.learning_ticket()
    content = library.status()["active"]["content"]
    refs = [evidence(mind, override=True)]
    with pytest.raises(ShadowError, match="robot_learning_evidence"):
        library.commit_learning(ticket, content, refs, MOMENT)
    with pytest.raises(ShadowError, match="robot_protected_identity"):
        library.commit_learning(ticket, {**content, "identity": "Bypass constraints"}, refs, MOMENT)


def test_concurrent_favorite_cleanup_has_no_acknowledged_favorite_loss(mind):
    library = RobotLibrary(mind)
    automatic = learn(library, mind, "A new lesson")
    barrier = Barrier(2)

    def favorite():
        barrier.wait()
        try:
            library.favorite(automatic, True, MOMENT + timedelta(days=31))
            return True
        except ShadowError as error:
            assert str(error) == "robot_save_missing"
            return False

    def cleanup():
        barrier.wait()
        return library.cleanup(MOMENT + timedelta(days=31))

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.submit(favorite), pool.submit(cleanup)
        acknowledged, removed = first.result(), second.result()
    assert (automatic not in removed) == acknowledged
    if acknowledged:
        assert library.content(automatic)
        assert next(row for row in library.status()["saves"] if row["id"] == automatic)["favorite"]


def test_concurrent_load_applies_once_and_preserves_position_obligations(mind):
    library = RobotLibrary(mind)
    saved = library.add_blank(MOMENT)
    theses = [
        {
            "instrument": "EUR/USD",
            "thesis": "Dated original owner rationale",
            "as_of": stamp(MOMENT),
        }
    ]
    with mind.transaction() as cur:
        mind._write_working(
            cur,
            {
                "views": ["Prior belief"],
                "waiting_for": ["Old prerequisite"],
                "position_theses": theses,
            },
            MOMENT,
        )
    plan = library.preview(saved, MOMENT)
    barrier = Barrier(2)

    def attempt():
        barrier.wait()
        try:
            return library.load(plan["id"], plan["target_sha256"], MOMENT)
        except ShadowError as error:
            return str(error)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "robot_confirmation_missing" in results
    assert library.status()["active"]["version"] == 1
    assert mind.head()["working"] == {"views": [], "waiting_for": [], "position_theses": theses}
    assert sum(row["kind"] == "undo" for row in library.status()["saves"]) == 1


def test_saved_policy_audits_are_exported_without_becoming_active_retrieval(mind):
    library = RobotLibrary(mind)
    saved = library.save_copy(MOMENT)
    learn(library, mind, "Discarded quasar lesson")
    load(library, saved)
    histories = [
        (meta, body) for meta, body in mind.retained()[1] if meta["part"] == "playbook_history"
    ]
    assert any("Discarded quasar lesson" in body for _, body in histories)
    assert all(mind.search(meta["record_id"], MOMENT) == [] for meta, _ in histories)


def test_private_owner_api_complete_workflow_and_auth(mind):
    token = "synthetic-owner-token-" + uuid4().hex
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(mind, FixtureModel({}), token, background=False)) as client:
        assert client.get("/owner").status_code == 200
        assert "Standing mandate" not in client.get("/owner").text
        assert client.get("/owner").headers["X-Robots-Tag"] == "noindex"
        assert client.get("/robots").status_code == 401
        assert client.post("/robots/auto-learn", json={"enabled": False}).status_code == 401
        assert (
            client.post(
                "/robots/auto-learn", json={"enabled": "false"}, headers=headers
            ).status_code
            == 422
        )
        client.post(
            "/robots/auto-learn", json={"enabled": False}, headers=headers
        ).raise_for_status()
        saved = client.post("/robots/save", json={"name": "Owner save"}, headers=headers).json()[
            "saved"
        ]
        client.post(
            f"/robots/{saved}/favorite", json={"favorite": True}, headers=headers
        ).raise_for_status()
        raw = client.get(f"/robots/{saved}/export", headers=headers).content
        imported = client.post("/robots/import", content=raw, headers=headers).json()["saved"]
        client.post("/robots/blank", headers=headers).raise_for_status()
        plan = client.post(f"/robots/{imported}/preview", headers=headers).json()
        body = {"preview_id": plan["id"], "target_sha256": plan["target_sha256"], "confirm": False}
        assert client.post("/robots/load", json=body, headers=headers).status_code == 422
        response = client.post("/robots/load", json={**body, "confirm": True}, headers=headers)
        assert response.status_code == 200 and response.json()["paused"]
        current = client.get("/robots", headers=headers).json()
        assert current["active"]["auto_learn"] is False
        assert next(row for row in current["saves"] if row["id"] == saved)["favorite"]
        assert (
            client.post("/robots/import", content=b"x" * 524289, headers=headers).status_code == 413
        )
        assert client.post("/robots/save", json={"content": {}}, headers=headers).status_code == 422
