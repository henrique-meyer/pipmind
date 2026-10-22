"""Immutable assignment links and trusted SQL-only gateway hook topology."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from hashlib import sha256

import pytest
from test_safety_admission_store import NOW, make_case
from test_safety_integration import make_gateway, symbols

from pipmind.safety.admission import assess, encode_request
from pipmind.safety.attribution_sql import (
    AttributionCommitUnknown,
    AttributionLink,
    AttributionUnavailable,
    SqlAttributionStore,
    bootstrap,
    decode_link,
    encode_link,
)
from pipmind.safety.integration_sql import _CONFLICT, IntegrationRefused, SqlSafetyGateway
from pipmind.safety.recovery_sql import Assignment, RobotReference


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return sha256(value.encode()).hexdigest()


def source(channel="accept", record_id="request-A", *, epoch="e" * 64):
    request, state, _ = make_case()
    scope = {
        "authority_epoch": epoch,
        "owner_id": "owner",
        "generation": 1,
        "resource": "execution",
        "session_id": state.spec.session_id,
        "policy_book_sha256": "b" * 64,
        "mapping_sha256": "c" * 64,
        "control_revision": 1,
    }
    body = encoded(
        {
            "request": encode_request(request),
            "session_id": state.spec.session_id,
            "book": scope["policy_book_sha256"],
            "mapping": scope["mapping_sha256"],
        }
    )
    receipt = {
        "schema_version": 1,
        "channel": channel,
        "record_id": record_id,
        "account_id": state.spec.binding.account_id,
        "environment": "demo",
        "currency": "USD",
        "session_id": state.spec.session_id,
        "scope": scope,
        "request_sha256": digest(body),
        "control_revision": 1,
        "observed_at": "2026-10-21T12:00:00Z",
        "origin": "model",
        "review_excluded": False,
        "outcome": "dry_accepted",
        "code": "within_limits",
        "assessment": None,
        "dispatch_authorized": False,
        "dispatch_enabled": False,
        "historical_receipt_only": True,
    }
    assignment = Assignment(
        state.spec.binding,
        1,
        "assignment-A",
        RobotReference("robot-A", "version-A", "d" * 64),
        epoch,
        "a" * 64,
        "2026-10-21T12:00:00Z",
    )
    raw = encoded(receipt)
    return state.spec.binding, assignment, receipt, (body, digest(body), raw, digest(raw))


class Database:
    def __init__(self):
        self.gateway, self.links = {}, {}
        self.connections, self.statements = [], []
        self.commit_failure = None
        self.autocommit = False
        self.transcount = 1
        self.lock_mode = "Exclusive"
        self.fail_on = None
        self.on_execute = None

    def connect(self):
        db = Connection(self)
        self.connections.append(db)
        return db


class Connection:
    def __init__(self, database):
        self.database = database
        self.links = deepcopy(database.links)
        self.autocommit = database.autocommit
        self.closed = self.committed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        if self.database.commit_failure == "before":
            raise OSError("private SQL commit error")
        self.database.links = deepcopy(self.links)
        self.committed = True
        if self.database.commit_failure == "after":
            raise SystemExit("private lost commit acknowledgement")

    def close(self):
        self.closed = True


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = ("synthetic",)
        self.queue = []

    def execute(self, sql, params=()):
        db = self.connection.database
        q = " ".join(sql.split())
        db.statements.append((q, params))
        if db.on_execute is not None:
            hook, db.on_execute = db.on_execute, None
            hook()
        if db.fail_on and db.fail_on in q:
            raise OSError("private SQL endpoint text")
        self.queue = []
        if q.startswith("SET ") or q.startswith("IF OBJECT_ID"):
            pass
        elif "sp_getapplock" in q:
            self.queue = [(0,)]
        elif q == "SELECT @@TRANCOUNT":
            self.queue = [(db.transcount,)]
        elif "APPLOCK_MODE" in q:
            self.queue = [(db.lock_mode,)]
        elif "FROM dbo.pm_safety_attribution_v1" in q:
            row = self.connection.links.get((params[2], params[3]))
            if row is not None:
                self.queue = [row]
        elif "FROM dbo.pm_safety_integration_journal_v1" in q:
            row = db.gateway.get((params[3], params[4]))
            if row is not None:
                self.queue = [row]
        elif q.startswith("INSERT INTO dbo.pm_safety_attribution_v1"):
            _, channel, rid, raw, checksum = params
            self.connection.links[channel, rid] = (raw, checksum)
        elif q == "SELECT SYSUTCDATETIME()":
            self.queue = [(datetime(2026, 10, 21, 12),)]
        else:
            raise AssertionError("unexpected synthetic SQL")
        return self

    def fetchone(self):
        return self.queue.pop(0) if self.queue else None

    def nextset(self):
        return False


def setup(channel="accept", rid="request-A"):
    binding, assignment, receipt, raw = source(channel, rid)
    db = Database()
    db.gateway[channel, rid] = raw
    store = SqlAttributionStore(db.connect, binding)
    return db, store, assignment, receipt


def record(db, store, assignment, receipt):
    connection = db.connect()
    try:
        link = store.record_in_transaction(connection.cursor(), assignment, receipt)
        connection.commit()
        return link
    finally:
        connection.close()


def test_constructor_no_sql_explicit_bootstrap_and_positive_canonical_link():
    db, store, assignment, receipt = setup()
    assert not db.connections
    bootstrap(db.connect)
    assert sum(q.startswith("IF OBJECT_ID") for q, _ in db.statements) == 1
    original = deepcopy(receipt)
    connection = db.connect()
    link = store.record_in_transaction(connection.cursor(), assignment, receipt)
    assert isinstance(link, AttributionLink) and link.assignment == assignment
    assert not link.dispatch_authorized and not db.links and not connection.committed
    assert receipt == original
    connection.commit()
    connection.close()
    assert decode_link(encode_link(link)) == link
    with pytest.raises(FrozenInstanceError):
        link.assignment = assignment
    assert store.lookup("accept", "request-A") == link


@pytest.mark.parametrize(
    "channel,rid", [("accept", "request-A"), ("check", "check-A"), ("reconcile", "observation-A")]
)
def test_exact_replay_keeps_original_assignment_after_current_assignment_changes(channel, rid):
    db, store, assignment, receipt = setup(channel, rid)
    original = record(db, store, assignment, receipt)
    changed = replace(
        assignment,
        assignment_id="assignment-B",
        generation=2,
        epoch="f" * 64,
        robot=RobotReference("robot-B", "v2", "9" * 64),
    )
    before = deepcopy(db.links)
    assert record(db, store, changed, receipt) == original
    assert db.links == before and store.lookup(channel, rid).assignment == assignment
    assert all(conn.closed for conn in db.connections)


@pytest.mark.parametrize(
    "damage", ["new-epoch", "result", "request-hash", "receipt-hash", "missing"]
)
def test_missing_mismatched_or_corrupt_source_cannot_create_attribution(damage):
    db, store, assignment, receipt = setup()
    if damage == "new-epoch":
        assignment = replace(assignment, epoch="f" * 64)
    elif damage == "result":
        receipt["outcome"] = "different"
    elif damage == "missing":
        db.gateway.clear()
    else:
        row = list(db.gateway["accept", "request-A"])
        row[1 if damage == "request-hash" else 3] = "0" * 64
        db.gateway["accept", "request-A"] = tuple(row)
    with pytest.raises(AttributionUnavailable, match="^attribution_store_unavailable$"):
        record(db, store, assignment, receipt)
    assert not db.links


@pytest.mark.parametrize("damage", ["hash", "body", "binding", "receipt-changed"])
def test_retained_link_never_relabels_or_masks_changed_receipts(damage):
    db, store, assignment, receipt = setup()
    original = record(db, store, assignment, receipt)
    if damage == "hash":
        raw, _ = db.links["accept", "request-A"]
        db.links["accept", "request-A"] = (raw, "0" * 64)
    elif damage in ("body", "binding"):
        changed = (
            replace(original, receipt_sha256="0" * 64)
            if damage == "body"
            else replace(
                original,
                binding=replace(original.binding, account_id="other"),
                assignment=replace(
                    original.assignment, binding=replace(original.binding, account_id="other")
                ),
            )
        )
        raw = encode_link(changed)
        db.links["accept", "request-A"] = (raw, digest(raw))
    else:
        body, body_hash, _, _ = db.gateway["accept", "request-A"]
        receipt["code"] = "changed"
        raw = encoded(receipt)
        db.gateway["accept", "request-A"] = (body, body_hash, raw, digest(raw))
    before = deepcopy(db.links)
    with pytest.raises(AttributionUnavailable):
        record(db, store, replace(assignment, generation=2), receipt)
    assert db.links == before


@pytest.mark.parametrize("mode", ["transaction", "autocommit", "lock"])
def test_same_transaction_and_earlier_locks_are_required_without_new_locks(mode):
    db, store, assignment, receipt = setup()
    if mode == "transaction":
        db.transcount = 0
    elif mode == "autocommit":
        db.autocommit = True
    else:
        db.lock_mode = "NoLock"
    with pytest.raises(AttributionUnavailable):
        record(db, store, assignment, receipt)
    assert not db.links and not any("sp_getapplock" in q for q, _ in db.statements)


def test_receipt_frozen_before_sql_and_no_arbitrary_json_hooks():
    db, store, assignment, receipt = setup()
    db.on_execute = lambda: receipt.update(outcome="concurrent caller edit")
    link = record(db, store, assignment, receipt)
    assert link.assignment == assignment and receipt["outcome"] == "concurrent caller edit"

    class Unsafe(dict):
        def items(self):
            pytest.fail("custom serialization hook ran")

    with pytest.raises(AttributionUnavailable):
        record(db, store, assignment, Unsafe(receipt))


@pytest.mark.parametrize("phase", ["before", "after"])
def test_historical_lookup_commit_ambiguity_does_not_return_known_link(phase):
    db, store, assignment, receipt = setup()
    record(db, store, assignment, receipt)
    before = deepcopy(db.links)
    db.commit_failure = phase
    with pytest.raises(AttributionCommitUnknown, match="^attribution_commit_unknown$"):
        store.lookup("accept", "request-A")
    assert db.links == before and db.connections[-1].closed


@pytest.mark.parametrize(
    "before,after",
    [(lambda cur, grant: None, None), (None, lambda *a: None), (3, 4), (False, False)],
)
def test_gateway_requires_paired_trusted_callable_hooks(before, after):
    gateway, _, _ = make_gateway()
    with pytest.raises(IntegrationRefused, match="^invalid_integration_hooks$"):
        SqlSafetyGateway(
            gateway.admission,
            gateway.controls,
            symbols(),
            base_units_per_size=gateway._unit_factors,
            before=before,
            after=after,
        )


def hook_gateway(monkeypatch, *, prior=None, before=None, after=None):
    gateway, request, _ = make_gateway()
    gateway = SqlSafetyGateway(
        gateway.admission,
        gateway.controls,
        symbols(),
        base_units_per_size=gateway._unit_factors,
        before=before,
        after=after,
    )
    events, cursor, grant = [], object(), object()
    monkeypatch.setattr(
        gateway.admission.sessions, "read_in_transaction", lambda cur: events.append("session")
    )
    monkeypatch.setattr(gateway.controls, "read_in_transaction", lambda cur: object())
    monkeypatch.setattr(gateway, "_account", lambda cur: (None, "held"))
    monkeypatch.setattr(gateway, "_scope", lambda grant, state: {})
    monkeypatch.setattr(gateway, "_prior", lambda *args: prior)
    monkeypatch.setattr(gateway, "_accepted", lambda *args: False)
    _, state, book = make_case()
    assessment = assess(request, state, book, (), 0, now=NOW)
    assert assessment.within_limits
    monkeypatch.setattr(gateway.admission, "preview_in_transaction", lambda *a, **k: assessment)
    monkeypatch.setattr(gateway, "_hold", lambda *args: None)
    monkeypatch.setattr(
        gateway, "_record", lambda *a, **k: {"outcome": "dry_ready", "dispatch_authorized": False}
    )
    monkeypatch.setattr(
        gateway.admission.leases, "compare_and_act", lambda g, operation: operation(cursor)
    )
    return gateway, request, events, cursor, grant


def test_gateway_hooks_wrap_same_cursor_before_session_and_after_positive(monkeypatch):
    observed = []

    def before(cur, grant):
        assert not events
        observed.append(("before", cur, grant))
        return "trusted-context"

    def after(cur, context, result):
        assert events == ["session"]
        observed.append(("after", cur, context))
        return {"gateway_receipt": result, "dispatch_authorized": False}

    gateway, request, events, cursor, grant = hook_gateway(monkeypatch, before=before, after=after)
    result = gateway.reconcile(grant, request)
    assert observed == [("before", cursor, grant), ("after", cursor, "trusted-context")]
    assert events == ["session"] and result["gateway_receipt"]["outcome"] == "dry_ready"


def test_gateway_conflict_sentinel_bypasses_after_hook(monkeypatch):
    before_calls, after_calls = [], []
    gateway, request, _, _, grant = hook_gateway(
        monkeypatch,
        prior=_CONFLICT,
        before=lambda *a: before_calls.append(True),
        after=lambda *a: after_calls.append(True),
    )
    with pytest.raises(IntegrationRefused, match="^conflicting_integration_request$"):
        gateway.accept(grant, request)
    assert before_calls == [True] and after_calls == []


def test_gateway_exact_historical_result_passes_after_for_original_attribution(monkeypatch):
    old = {"outcome": "refused", "dispatch_authorized": False}
    seen = []
    gateway, request, _, _, grant = hook_gateway(
        monkeypatch,
        prior=old,
        before=lambda *a: "current-assignment",
        after=lambda cur, context, result: seen.append((context, result)) or result,
    )
    assert gateway.accept(grant, request) is old
    assert seen == [("current-assignment", old)]


def test_record_uses_only_caller_cursor_and_staged_error_never_commits():
    db, store, assignment, receipt = setup()
    store._connect = lambda: pytest.fail("independent connector used in attribution")
    db.fail_on = "INSERT INTO dbo.pm_safety_attribution_v1"
    with pytest.raises(AttributionUnavailable, match="^attribution_store_unavailable$"):
        record(db, store, assignment, receipt)
    assert not db.links and len(db.connections) == 1
    assert not db.connections[0].committed and db.connections[0].closed


def test_hook_failure_propagates_inside_outer_lease_callback(monkeypatch):
    def after(cur, context, result):
        raise OSError("synthetic after-hook failure")

    gateway, request, _, _, grant = hook_gateway(monkeypatch, before=lambda *a: None, after=after)
    with pytest.raises(OSError, match="^synthetic after-hook failure$"):
        gateway.reconcile(grant, request)
    # This topology test bypasses lease machinery; the real lease normalizes
    # this callback failure and aborts its shared transaction (separate tests).
