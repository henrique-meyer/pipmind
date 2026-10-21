"""Core-only, read-only comparison of expected books and supplied broker facts.

Supplementary identity/completeness is caller evidence, not venue attestation.
Nothing in this module acquires ownership, authorizes dispatch or changes state.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from pipmind.contracts import ContractError, Contracts, utc


@dataclass(frozen=True)
class Binding:
    account_id: str
    environment: str

    def __post_init__(self):
        if (
            type(self.account_id) is not str
            or not self.account_id
            or type(self.environment) is not str
            or self.environment not in {"demo", "live"}
        ):
            raise ValueError("reconciliation_binding")


@dataclass(frozen=True)
class Freshness:
    max_age: timedelta
    max_snapshot_skew: timedelta

    def __post_init__(self):
        for value in (self.max_age, self.max_snapshot_skew):
            if not isinstance(value, timedelta) or value < timedelta(0):
                raise ValueError("reconciliation_freshness")


@dataclass(frozen=True)
class ProtectiveOrder:
    """Supplementary projection using an unchanged normalized OrderRequest.

    The caller must supply a broker-observed order state, not merely repeat its
    own request. Position ID and broker order ID are explicit external bindings.
    """

    position_id: str
    broker_order_id: str
    request: Mapping


@dataclass(frozen=True)
class SnapshotFacts:
    """Unmodified v1 payloads plus separately supplied identity and scope facts."""

    binding: Binding | None
    account: Mapping | None
    positions: Mapping | None
    position_ids: Sequence[str] | None = None
    account_complete: bool | None = None
    positions_complete: bool | None = None
    order_updates: Sequence[Mapping] | None = None
    orders_as_of: str | None = None
    orders_complete: bool | None = None
    protective_orders: Sequence[ProtectiveOrder] | None = None
    protections_as_of: str | None = None
    protections_complete: bool | None = None


@dataclass(frozen=True, order=True)
class Finding:
    code: str
    subject: str
    identity: str = ""
    field: str = ""


@dataclass(frozen=True)
class Reconciliation:
    observed_at: datetime
    findings: tuple[Finding, ...]

    @property
    def consistent(self):
        """No discrepancy within supplied scope; never an execution capability."""
        return not self.findings


@lru_cache(maxsize=1)
def _contracts():
    return Contracts()


def _number(value):
    if type(value) not in (int, float, Decimal):
        raise ValueError("reconciliation_number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("reconciliation_number") from None
    if not number.is_finite():
        raise ValueError("reconciliation_number")
    return number


def _equal(left, right):
    if type(left) in (int, float, Decimal) or type(right) in (int, float, Decimal):
        return _number(left) == _number(right)
    return type(left) is type(right) and left == right


def reconcile(
    expected: SnapshotFacts,
    observed: SnapshotFacts,
    *,
    observed_at: datetime,
    freshness: Freshness,
    commands: Sequence[Mapping] | None,
    command_requests: Sequence[Mapping] = (),
    pending_command_ids: Sequence[str] = (),
    protective_obligations: Sequence[ProtectiveOrder] | None = None,
) -> Reconciliation:
    """Compare normalized books without mutation, networking or implicit clocks.

    ``commands`` uses recovery-state v1 command rows. OrderUpdate facts are a
    caller-declared current reconciliation scope, not a deduplicated event log.
    Explicit empty complete scopes differ from unknown/missing evidence.
    """
    if (
        not isinstance(expected, SnapshotFacts)
        or not isinstance(observed, SnapshotFacts)
        or not isinstance(freshness, Freshness)
        or not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() != timedelta(0)
    ):
        raise ValueError("reconciliation_configuration")
    findings = set()

    def add(code, subject, identity="", field=""):
        findings.add(Finding(code, subject, identity, field))

    def sequence(value, subject):
        if not isinstance(value, (list, tuple)):
            add("incomplete" if value is None else "invalid", subject)
            return ()
        return value

    def validate(schema, value, subject):
        if value is None:
            add("incomplete", subject)
            return False

        def finite(item):
            if type(item) in (int, float, Decimal):
                _number(item)
            elif isinstance(item, Mapping):
                for child in item.values():
                    finite(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    finite(child)

        try:
            finite(value)
            _contracts().validate(schema, value)
        except (ContractError, TypeError, ValueError, InvalidOperation):
            add("invalid", subject)
            return False
        return True

    def age(value, subject, baseline=None):
        try:
            moment = utc(value)
        except (ContractError, TypeError, ValueError, AttributeError):
            add("incomplete" if value is None else "invalid_time", subject)
            return None
        if moment > observed_at:
            add("future", subject)
        elif observed_at - moment > freshness.max_age:
            add("stale", subject)
        if baseline is not None and moment < utc(baseline):
            add("late", subject)
        return moment

    for field in ("account_complete", "positions_complete", "orders_complete"):
        if getattr(observed, field) is not True:
            add("incomplete", field.removesuffix("_complete"))
    if not isinstance(expected.binding, Binding) or not isinstance(observed.binding, Binding):
        add("incomplete", "binding")
    elif expected.binding != observed.binding:
        for field in ("account_id", "environment"):
            if getattr(expected.binding, field) != getattr(observed.binding, field):
                add("changed", "binding", field=field)

    valid = {}
    for side, book in (("expected", expected), ("observed", observed)):
        for kind in ("account", "positions"):
            valid[side, kind] = validate(
                "account-snapshot" if kind == "account" else "position-snapshot",
                getattr(book, kind),
                side + "." + kind,
            )
            if kind == "account" and valid[side, kind] and not book.account["currency"]:
                add("invalid", side + ".account", field="currency")
    times = []
    for kind in ("account", "positions"):
        if valid["observed", kind]:
            payload = getattr(observed, kind)
            baseline = getattr(expected, kind)["as_of"] if valid["expected", kind] else None
            moment = age(payload["as_of"], kind, baseline)
            if moment is not None:
                times.append(moment)
    if len(times) == 2 and max(times) - min(times) > freshness.max_snapshot_skew:
        add("conflicting_time", "snapshots")

    if all(valid[s, "account"] for s in ("expected", "observed")):
        for field, value in expected.account.items():
            if field in {"as_of", "schema_version"}:
                continue
            if field not in observed.account:
                add("missing", "account", field=field)
            else:
                try:
                    same = _equal(value, observed.account[field])
                except ValueError:
                    add("invalid", "account", field=field)
                else:
                    if not same:
                        add("changed", "account", field=field)

    def position_index(book, side):
        if not valid[side, "positions"]:
            return {}
        rows = book.positions["positions"]
        ids = book.position_ids
        if ids is not None and not isinstance(ids, (list, tuple)):
            add("invalid", side + ".position_identities")
            return {}
        if ids is None or len(ids) != len(rows):
            if rows or ids not in (None, (), []):
                add("incomplete", side + ".position_identities")
            return {}
        groups = defaultdict(list)
        for identity, row in zip(ids, rows, strict=True):
            if type(identity) is not str or not identity:
                add("incomplete", side + ".position_identities")
                continue
            try:
                size = _number(row["size"])
                if not row["instrument"]:
                    add("invalid", side + ".positions", identity, "instrument")
                for field in ("average_price", "unrealized_pnl"):
                    if row.get(field) is not None:
                        _number(row[field])
                if (row["side"] == "flat") != (size == 0):
                    add("conflicting", side + ".positions", identity, "side_size")
            except ValueError:
                add("invalid", side + ".positions", identity)
            groups[identity].append(row)
        for identity, values in groups.items():
            if len(values) > 1:
                add("duplicate", side + ".positions", identity)
                if any(value != values[0] for value in values[1:]):
                    add("conflicting", side + ".positions", identity)
        return {identity: values[0] for identity, values in groups.items() if len(values) == 1}

    expected_positions = position_index(expected, "expected")
    observed_positions = position_index(observed, "observed")
    if all(valid[s, "positions"] for s in ("expected", "observed")):
        for identity in expected_positions.keys() - observed_positions.keys():
            add("missing", "positions", identity)
        for identity in observed_positions.keys() - expected_positions.keys():
            add("unexpected", "positions", identity)
        for identity in expected_positions.keys() & observed_positions.keys():
            for field, value in expected_positions[identity].items():
                if value is None:
                    add("incomplete", "positions", identity, field)
                    continue
                if field not in observed_positions[identity]:
                    add("missing", "positions", identity, field)
                    continue
                try:
                    same = _equal(value, observed_positions[identity][field])
                except ValueError:
                    add("invalid", "positions", identity, field)
                else:
                    if not same:
                        add("changed", "positions", identity, field)

    updates = defaultdict(list)
    order_time = age(observed.orders_as_of, "orders")
    if observed.order_updates is None:
        add("incomplete", "orders")
    else:
        for update in sequence(observed.order_updates, "orders"):
            if not validate("order-update", update, "orders"):
                continue
            identity = update["client_order_id"]
            if not identity:
                add("incomplete", "orders", field="client_order_id")
                continue
            # ts is the last OrderUpdate event, not a fresh observation timestamp.
            # An unchanged old working order can be observed in a fresh scope.
            if utc(update["ts"]) > observed_at:
                add("future", "orders", identity)
            if order_time is not None and utc(update["ts"]) > order_time:
                add("conflicting_time", "orders", identity)
            for field in ("size", "filled_size"):
                if update.get(field) is not None and _number(update[field]) < 0:
                    add("invalid", "orders", identity, field)
            updates[identity].append(update)
        for identity, values in updates.items():
            if len(values) > 1:
                add("duplicate", "orders", identity)
                if any(value != values[0] for value in values[1:]):
                    add("conflicting", "orders", identity)
        broker_groups = defaultdict(set)
        for identity, rows in updates.items():
            for row in rows:
                if row.get("broker_order_id"):
                    broker_groups[row["broker_order_id"]].add(identity)
        for identities in broker_groups.values():
            if len(identities) > 1:
                for identity in identities:
                    add("conflicting", "order_broker_identity", identity)

    obligations = {}
    requests = {}
    for request in sequence(command_requests, "command_requests"):
        if validate("order-request", request, "command_requests"):
            identity = request["client_order_id"]
            if identity in requests:
                add("duplicate", "command_requests", identity)
                if request != requests[identity]:
                    add("conflicting", "command_requests", identity)
            requests[identity] = request
    if commands is None:
        add("incomplete", "commands")
    else:
        for command in sequence(commands, "commands"):
            if (
                not isinstance(command, Mapping)
                or set(command) != {"command_id", "state", "broker_order_id", "known_outcome"}
                or type(command["command_id"]) is not str
                or not command["command_id"]
                or type(command["state"]) is not str
                or command["state"]
                not in {"accepted", "dispatching", "unknown", "reconciled", "refused"}
                or not all(
                    command[k] is None or type(command[k]) is str
                    for k in ("broker_order_id", "known_outcome")
                )
            ):
                add("invalid", "commands")
                continue
            identity = command["command_id"]
            if identity in obligations:
                add("duplicate", "commands", identity)
                if command != obligations[identity]:
                    add("conflicting", "commands", identity)
            obligations[identity] = command
            if identity not in requests:
                add("incomplete", "command_requests", identity)
            elif len(updates.get(identity, [])) == 1:
                row = updates[identity][0]
                for field in ("instrument", "side", "size"):
                    if field not in requests[identity] or requests[identity][field] is None:
                        continue
                    if field not in row or row[field] is None:
                        add("incomplete", "command_outcome", identity, field)
                    elif not _equal(requests[identity][field], row[field]):
                        add("changed", "command_outcome", identity, field)
            if command["state"] in {"accepted", "dispatching"}:
                add("pending", "commands", identity)
            if command["state"] == "unknown":
                add("ambiguous", "commands", identity)
            if command["state"] == "reconciled":
                rows = updates.get(identity, [])
                if len(rows) != 1:
                    add("missing", "command_outcome", identity)
                else:
                    row = rows[0]
                    if not command["broker_order_id"] or command["known_outcome"] not in {
                        "accepted",
                        "working",
                        "partial",
                        "filled",
                        "cancelled",
                        "rejected",
                    }:
                        add("ambiguous", "commands", identity)
                    for left, right in (
                        ("broker_order_id", "broker_order_id"),
                        ("known_outcome", "status"),
                    ):
                        if command[left] != row.get(right):
                            add("changed", "command_outcome", identity, right)
            if command["state"] == "refused" and identity in updates:
                add("conflicting", "commands", identity)
    for identity in requests.keys() - obligations.keys():
        add("missing", "commands", identity)
    broker_ids = defaultdict(list)
    for identity, command in obligations.items():
        if command["broker_order_id"]:
            broker_ids[command["broker_order_id"]].append(identity)
    for identities in broker_ids.values():
        if len(identities) > 1:
            for identity in identities:
                add("conflicting", "command_broker_identity", identity)
    for identity in sequence(pending_command_ids, "pending_commands"):
        if type(identity) is not str or not identity:
            add("invalid", "pending_commands")
        else:
            add("pending", "commands", identity)
            if identity not in obligations:
                add("missing", "commands", identity)
            elif obligations[identity]["state"] in {"reconciled", "refused"}:
                add("conflicting", "commands", identity)

    def protections_index(values, subject):
        groups = defaultdict(list)
        for value in values:
            if (
                not isinstance(value, ProtectiveOrder)
                or type(value.position_id) is not str
                or not value.position_id
                or type(value.broker_order_id) is not str
                or not value.broker_order_id
                or not validate("order-request", value.request, subject)
            ):
                add("invalid", subject)
                continue
            identity = value.request["client_order_id"]
            try:
                prices = [
                    value.request[k] for k in ("stop", "limit") if value.request.get(k) is not None
                ]
                if not prices or any(_number(price) <= 0 for price in prices):
                    raise ValueError
                if value.request.get("size") is not None:
                    _number(value.request["size"])
            except ValueError:
                add("invalid", subject, identity)
                continue
            if value.position_id not in observed_positions:
                add("missing", subject, identity, "position_id")
            elif observed_positions[value.position_id]["instrument"] != value.request["instrument"]:
                add("changed", subject, identity, "instrument")
            groups[identity].append(value)
        for identity, values in groups.items():
            if len(values) > 1:
                add("duplicate", subject, identity)
                if any(value != values[0] for value in values[1:]):
                    add("conflicting", subject, identity)
        return {identity: values[0] for identity, values in groups.items() if len(values) == 1}

    if protective_obligations is None:
        add("incomplete", "protective_obligations")
    wanted = protections_index(
        sequence(protective_obligations, "protective_obligations"), "protective_obligations"
    )
    actual = protections_index(
        sequence(observed.protective_orders, "protective_orders")
        if observed.protective_orders is not None
        else (),
        "protective_orders",
    )
    if protective_obligations or observed.protective_orders:
        if observed.protections_complete is not True or observed.protective_orders is None:
            add("incomplete", "protective_orders")
        age(observed.protections_as_of, "protective_orders")
    for identity in wanted.keys() - actual.keys():
        add("missing", "protective_orders", identity)
    for identity in actual.keys() - wanted.keys():
        add("unexpected", "protective_orders", identity)
    for identity in wanted.keys() & actual.keys():
        want, fact = wanted[identity], actual[identity]
        for field in ("position_id", "broker_order_id"):
            if getattr(want, field) != getattr(fact, field):
                add("changed", "protective_orders", identity, field)
        for field, value in want.request.items():
            try:
                same = field in fact.request and _equal(value, fact.request[field])
            except ValueError:
                same = False
            if not same:
                add("changed", "protective_orders", identity, field)
        rows = updates.get(identity, [])
        if len(rows) != 1 or rows[0]["status"] != "working":
            add("missing", "protective_order_working", identity)
        elif rows[0].get("broker_order_id") != fact.broker_order_id:
            add("changed", "protective_orders", identity, "broker_order_id")
        else:
            for field in ("instrument", "side", "size"):
                if field not in fact.request or fact.request[field] is None:
                    continue
                if field not in rows[0] or rows[0][field] is None:
                    add("incomplete", "protective_orders", identity, field)
                elif not _equal(fact.request[field], rows[0][field]):
                    add("changed", "protective_orders", identity, field)
    for identity in updates.keys() - obligations.keys() - wanted.keys():
        add("unexpected", "orders", identity)
    return Reconciliation(observed_at.astimezone(UTC), tuple(sorted(findings)))
