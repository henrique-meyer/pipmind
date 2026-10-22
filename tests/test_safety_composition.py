"""Composed admission uses real assessment/lease code and transactional SQL doubles."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_safety_admission_store import make_case, setup_case

from pipmind.safety.admission import AdmissionRefused
from pipmind.safety.admission_sql import ADMISSION_CONFLICT, SqlAdmissionStore
from pipmind.safety.lease_sql import LeaseCommitUnknown, LeaseRefused, LeaseUnavailable


def preview(store, grant, request, *, recheck=None):
    return store.leases.compare_and_act(
        grant,
        lambda cur: store.preview_in_transaction(cur, request, recheck_request_id=recheck),
    )


def test_composed_admission_uses_one_cursor_and_outer_acknowledgement():
    db, store, grant, request = setup_case()
    store._connect = lambda: pytest.fail("independent admission connection")
    seen = []

    def operation(cur):
        store.sessions.read_in_transaction(cur)  # Outer layer already owns session lock.
        result = store.admit_in_transaction(cur, grant, request)
        assert not db.tables["journal"]  # Still provisional, not committed.
        assert len(cur.connection.tables["journal"]) == 1
        assert not cur.connection.committed
        seen.append(cur)
        return result

    receipt = store.finalize_admission(store.leases.compare_and_act(grant, operation))
    assert receipt["assessment"].within_limits and not receipt["dispatch_authorized"]
    assert len(db.connections) == len(seen) == 1
    assert seen[0].connection.closed and seen[0].connection.committed
    assert store.binding == db.state.spec.binding
    assert store.session_id == db.state.spec.session_id
    assert store.policy_book_sha256 == receipt["policy_book_sha256"]
    with pytest.raises(AttributeError):
        store.session_id = "replacement"


def test_conflict_sentinel_is_provisional_and_original_audit_never_changes():
    db, store, grant, request = setup_case()
    original = store.admit(grant, request)
    before = deepcopy(db.tables)
    changed = replace(request, order={**request.order, "size": D("999")})

    def operation(cur):
        result = store.admit_in_transaction(cur, grant, changed)
        assert result is ADMISSION_CONFLICT and store.is_conflict(result)
        assert not cur.connection.committed
        return result

    result = store.leases.compare_and_act(grant, operation)
    assert db.tables == before and not store.is_conflict(original)
    with pytest.raises(AdmissionRefused, match="^conflicting_admission_request$"):
        store.finalize_admission(result)


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("conflict", [False, True])
def test_outer_commit_uncertainty_suppresses_receipt_and_conflict(phase, conflict):
    db, store, grant, request = setup_case()
    if conflict:
        store.admit(grant, request)
        request = replace(request, order={**request.order, "size": D("999")})
    before = deepcopy(db.tables)
    db.commit_failure = phase
    with pytest.raises(LeaseCommitUnknown, match="^lease_commit_unknown$"):
        store.leases.compare_and_act(
            grant, lambda cur: store.admit_in_transaction(cur, grant, request)
        )
    if conflict or phase == "before":
        assert db.tables == before
    else:
        assert len(db.tables["journal"]) == 1


def test_outer_lease_expiry_rolls_back_composed_staged_effects():
    db, store, grant, request = setup_case()

    def operation(cur):
        receipt = store.admit_in_transaction(cur, grant, request)
        db.now += timedelta(seconds=31)
        return receipt

    with pytest.raises(LeaseRefused, match="^lease_expired$"):
        store.leases.compare_and_act(grant, operation)
    assert not any(db.tables.values())


def test_preview_creates_only_optional_pin_and_never_a_reservation_or_count_floor():
    db, store, grant, request = setup_case(count=3)
    before = deepcopy(request)
    assessment = preview(store, grant, request)
    assert assessment.within_limits and assessment.validated_external_count == 3
    assert assessment.dispatch_authorized is False
    assert request == before
    assert len(db.tables["accounts"]) == 1
    assert all(not db.tables[k] for k in ("books", "journal", "reservations", "floors"))
    assert len(db.connections) == 1


def test_exact_replay_does_not_masquerade_as_fresh_preview():
    db, store, grant, request = setup_case()
    receipt = store.admit(grant, request)
    before = deepcopy(db.tables)
    db.now += timedelta(seconds=31)
    db.lease_expires += timedelta(seconds=60)
    assert store.admit(grant, request) == receipt
    fresh = preview(store, grant, request, recheck="request-A")
    assert not fresh.within_limits and fresh.code != "within_limits"
    assert db.tables == before


def test_recheck_excludes_exact_original_once_and_preserves_other_charges():
    db, store, grant, first = setup_case(marks=("request-A", "request-B"))
    second, _, _ = make_case(request_id="request-B", marks=("request-A", "request-B"))
    original = store.admit(grant, first)
    assert store.admit(grant, second)["assessment"].within_limits
    before = deepcopy(db.tables)
    result = preview(store, grant, first, recheck="request-A")
    assert result.within_limits
    assert dict(result.calculations)["reserved_opens_this_session"] == 1
    assert (
        dict(result.calculations)["reserved_gross_exposure"]
        == original["assessment"].reservation.gross_budget
    )
    assert db.tables == before
    # Without explicit verified exclusion this is fresh refusal, never original replay.
    ordinary = preview(store, grant, first)
    assert not ordinary.within_limits and ordinary.code == "request_already_reserved"


@pytest.mark.parametrize("change", ["order", "factor", "factor_repr", "id", "missing", "refused"])
def test_recheck_requires_exact_accepted_order_book_session_and_dimension(change):
    db, store, grant, request = setup_case()
    if change == "refused":
        bad = replace(request, facts=replace(request.facts, kill_switch=True))
        assert not store.admit(grant, bad)["assessment"].within_limits
    elif change != "missing":
        store.admit(grant, request)
    before = deepcopy(db.tables)
    if change == "order":
        request = replace(request, order={**request.order, "stop": D("1.08")})
    elif change in ("factor", "factor_repr"):
        factor = D("0.5") if change == "factor" else D("1.0")
        request = replace(
            request,
            facts=replace(
                request.facts, proposal=replace(request.facts.proposal, base_units_per_size=factor)
            ),
        )
    elif change == "id":
        request = replace(request, order={**request.order, "client_order_id": "other"})
    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        preview(store, grant, request, recheck="request-A")
    assert db.tables == before


@pytest.mark.parametrize("margin, expected", [(D("39"), False), (D("40"), True), (D("41"), True)])
def test_recheck_cannot_shrink_immutable_original_budget(margin, expected):
    db, store, grant, request = setup_case()
    store.admit(grant, request)
    before = deepcopy(db.tables)
    refreshed = replace(
        request,
        facts=replace(
            request.facts, proposal=replace(request.facts.proposal, margin_required=margin)
        ),
    )
    result = preview(store, grant, refreshed, recheck="request-A")
    assert result.within_limits is expected
    if not expected:
        assert result.code == "recheck_budget_shrink" and result.reservation is None
    assert db.tables == before


def test_recheck_does_not_discount_external_count_or_floor():
    db, store, grant, request = setup_case(count=3, caps={"max_session_orders": 4})
    assert store.admit(grant, request)["assessment"].within_limits
    before = deepcopy(db.tables)
    result = preview(store, grant, request, recheck="request-A")
    assert result.within_limits and result.validated_external_count == 3
    assert dict(result.calculations)["reserved_opens_this_session"] == 0
    assert db.tables == before and next(iter(db.tables["floors"].values())) == 3


def test_preview_freezes_input_before_session_lock_and_observes_server_time():
    db, store, grant, request = setup_case()
    connection = db.connect()
    cur = connection.cursor()
    db.before_lock = lambda: request.order.update(size=D("999"))
    result = store.preview_in_transaction(cur, request)
    assert result.within_limits and result.reservation.base_units == D("1000")
    assert SqlAdmissionStore.server_time_in_transaction(cur).replace(tzinfo=None) == db.now
    assert not connection.committed
    connection.close()
    assert not any(db.tables.values())


@pytest.mark.parametrize("damage", ["book", "projection", "journal"])
def test_recheck_rejects_different_book_or_corrupt_retained_facts(damage):
    db, store, grant, request = setup_case()
    store.admit(grant, request)
    if damage == "book":
        changed = (replace(store._policies[0], max_gross_exposure=D("9999")),)
        store = SqlAdmissionStore(db.connect, store.leases, store.sessions, changed)
    elif damage == "projection":
        key = next(iter(db.tables["reservations"]))
        row = db.tables["reservations"][key]
        db.tables["reservations"][key] = (row[0], row[1], "0" * 64)
    else:
        key = next(iter(db.tables["journal"]))
        row = list(db.tables["journal"][key])
        row[3] = "0" * 64
        db.tables["journal"][key] = tuple(row)
    before = deepcopy(db.tables)
    with pytest.raises(LeaseUnavailable, match="^lease_store_unavailable$"):
        preview(store, grant, request, recheck="request-A")
    assert db.tables == before


def test_recheck_fresh_upward_budget_must_pass_combined_cap_with_other_reservations():
    db, store, grant, first = setup_case(
        caps={"max_gross_exposure": D("2210")}, marks=("request-A", "request-B")
    )
    second, _, _ = make_case(
        request_id="request-B",
        caps={"max_gross_exposure": D("2210")},
        marks=("request-A", "request-B"),
    )
    store.admit(grant, first)
    assert store.admit(grant, second)["assessment"].within_limits
    before = deepcopy(db.tables)
    updated = replace(
        first,
        facts=replace(
            first.facts, proposal=replace(first.facts.proposal, bid=D("1.1198"), ask=D("1.12"))
        ),
    )
    result = preview(store, grant, updated, recheck="request-A")
    assert not result.within_limits and result.code == "gross_exposure_limit"
    assert db.tables == before
