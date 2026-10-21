"""Deterministic arithmetic over explicit owner limits and caller-supplied facts.

This is not reconciliation, ownership fencing, stop execution or order authority.
Every monetary/price/quantity input is Decimal; binary floats are never converted.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import (
    Context,
    Decimal,
    DecimalException,
    FloatOperation,
    Inexact,
    Rounded,
    localcontext,
)

from pipmind.contracts import ContractError, utc


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    policy_id: str
    account_id: str
    environment: str
    currency: str
    instrument: str
    max_order_base_units: Decimal
    max_instrument_base_units: Decimal
    max_gross_exposure: Decimal
    max_planned_order_loss: Decimal
    max_total_planned_loss: Decimal
    max_daily_loss: Decimal
    max_daily_risk: Decimal
    min_margin_available: Decimal
    max_session_orders: int
    snapshot_max_age_seconds: int
    quote_max_age_seconds: int
    conversion_max_age_seconds: int


@dataclass(frozen=True, slots=True)
class Conversion:
    from_currency: str
    to_currency: str
    rate: Decimal
    as_of: str


@dataclass(frozen=True, slots=True)
class ProposalFacts:
    instrument: str
    bid: Decimal
    ask: Decimal
    as_of: str
    base_units_per_size: Decimal
    margin_required: Decimal
    conversion: Conversion


@dataclass(frozen=True, slots=True)
class Exposure:
    obligation_id: str
    instrument: str
    side: str
    base_units: Decimal
    reference_price: Decimal
    stop_price: Decimal
    as_of: str
    conversion: Conversion


@dataclass(frozen=True, slots=True)
class RiskFacts:
    account_id: str
    environment: str
    as_of: str
    complete: bool
    reconciled: bool
    kill_switch: bool
    daily_loss_latched: bool
    session_started_at: str
    session_ends_at: str
    session_start_equity: Decimal
    external_cash_flow: Decimal
    orders_this_session: int
    proposal: ProposalFacts
    exposures: tuple[Exposure, ...]


@dataclass(frozen=True, slots=True)
class PreflightResult:
    within_limits: bool
    code: str
    calculations: tuple[tuple[str, Decimal], ...]

    @property
    def dispatch_authorized(self):
        return False


class _Refusal(Exception):
    pass


def _need(condition, code):
    if not condition:
        raise _Refusal(code)


def _text(value):
    return type(value) is str and 0 < len(value) <= 256 and value.strip() == value


def _currency(value):
    return type(value) is str and re.fullmatch(r"[A-Z]{3}", value) is not None


def _pair(value):
    _need(
        type(value) is str and re.fullmatch(r"[A-Z]{3}/[A-Z]{3}", value), "unsupported_instrument"
    )
    base, quote = value.split("/")
    _need(base != quote, "unsupported_instrument")
    return quote


def _decimal(value, code, *, positive=False, nonnegative=False):
    _need(type(value) is Decimal and value.is_finite(), code)
    digits = value.as_tuple()
    _need(len(digits.digits) <= 64 and abs(digits.exponent) <= 64, "numeric_capacity_exceeded")
    _need(not positive or value > 0, code)
    _need(not nonnegative or value >= 0, code)
    return value


def _time(value):
    _need(type(value) is str, "invalid_timestamp")
    try:
        return utc(value)
    except ContractError:
        raise _Refusal("invalid_timestamp") from None


def _fresh(value, now, ceiling, seconds, code):
    stamp = _time(value)
    _need(stamp <= ceiling <= now, "future_facts")
    age = now - stamp
    micros = ((age.days * 86400 + age.seconds) * 1000000) + age.microseconds
    _need(micros <= seconds * 1000000, code)
    return stamp


def _conversion(value, quote, policy, now, ceiling):
    _need(type(value) is Conversion, "invalid_conversion")
    _need(_currency(value.from_currency) and _currency(value.to_currency), "invalid_conversion")
    _need(
        value.from_currency == quote and value.to_currency == policy.currency, "conversion_mismatch"
    )
    rate = _decimal(value.rate, "invalid_conversion", positive=True)
    _need(quote != policy.currency or rate == 1, "conversion_mismatch")
    _fresh(value.as_of, now, ceiling, policy.conversion_max_age_seconds, "stale_conversion")
    return rate


def _loss(side, entry, stop, quantity, rate, code):
    _need(side in ("buy", "sell"), code)
    _need((stop < entry if side == "buy" else stop > entry), code)
    return abs(entry - stop) * quantity * rate


def _validate_policy(policy):
    _need(type(policy) is RiskPolicy, "invalid_policy")
    _need(_text(policy.policy_id) and _text(policy.account_id), "invalid_policy")
    _need(policy.environment in ("demo", "live") and _currency(policy.currency), "invalid_policy")
    _pair(policy.instrument)
    for field in (
        "max_order_base_units",
        "max_instrument_base_units",
        "max_gross_exposure",
        "max_planned_order_loss",
        "max_total_planned_loss",
        "max_daily_loss",
        "max_daily_risk",
    ):
        _decimal(getattr(policy, field), "invalid_policy", positive=True)
    _decimal(policy.min_margin_available, "invalid_policy", nonnegative=True)
    for field in (
        "max_session_orders",
        "snapshot_max_age_seconds",
        "quote_max_age_seconds",
        "conversion_max_age_seconds",
    ):
        _need(type(getattr(policy, field)) is int and getattr(policy, field) > 0, "invalid_policy")


def _transport_shapes(order, account):
    """The supported v1 typed subset, with no schema-file or other I/O at runtime."""
    _need(
        {"schema_version", "client_order_id", "action", "instrument"} <= order.keys()
        and order.keys()
        <= {
            "schema_version",
            "client_order_id",
            "action",
            "instrument",
            "side",
            "size",
            "order_type",
            "stop",
            "limit",
            "related_client_order_id",
            "time_in_force",
        },
        "invalid_contract",
    )
    _need(
        type(order["client_order_id"]) is str and bool(order["client_order_id"]), "invalid_contract"
    )
    _need(type(order["instrument"]) is str, "invalid_contract")
    _need(order["action"] in ("open", "close", "modify"), "invalid_contract")
    for key, values in (
        ("side", ("buy", "sell")),
        ("order_type", ("market", "limit")),
        ("time_in_force", ("gtc", "day", "ioc")),
    ):
        _need(key not in order or order[key] in values, "invalid_contract")
    _need(
        "related_client_order_id" not in order or type(order["related_client_order_id"]) is str,
        "invalid_contract",
    )
    if order["action"] == "open":
        _need({"side", "size", "order_type"} <= order.keys(), "invalid_contract")
        _need(type(order["size"]) is Decimal and order["size"] > 0, "invalid_contract")
    if order.get("order_type") == "limit":
        _need(type(order.get("limit")) is Decimal and order["limit"] > 0, "invalid_contract")
    _need(
        {"schema_version", "as_of", "balance", "currency"} <= account.keys()
        and account.keys()
        <= {
            "schema_version",
            "as_of",
            "balance",
            "currency",
            "equity",
            "margin_used",
            "margin_available",
        },
        "invalid_contract",
    )
    _need(type(account["currency"]) is str and type(account["as_of"]) is str, "invalid_contract")


def preflight(order, account, facts, policy, *, now):
    """Assess only an open's supplied numerical facts; every refusal is a fixed code.

    Limits are inclusive except the reached/latching daily-loss stop. Inputs and
    ambient Decimal context are unchanged. Passing never authorizes dispatch.
    """
    calculations = ()
    try:
        _validate_policy(policy)
        _need(
            type(now) is datetime and now.tzinfo is not None and now.utcoffset() == timedelta(0),
            "invalid_clock",
        )
        _need(type(order) is dict and type(account) is dict, "invalid_contract")
        # Schema numbers may be floats; this API deliberately requires Decimal
        # for all quantities and refuses rather than guessing a float conversion.
        _need(
            all(type(v) in (str, int, Decimal, type(None)) for v in order.values()), "invalid_order"
        )
        _need(all(type(v) in (str, int, Decimal) for v in account.values()), "invalid_account")
        _need(
            type(order.get("schema_version")) is int and order["schema_version"] == 1,
            "invalid_contract",
        )
        _need(
            type(account.get("schema_version")) is int and account["schema_version"] == 1,
            "invalid_contract",
        )
        for field in ("size", "stop", "limit"):
            if field in order and order[field] is not None:
                _decimal(order[field], "invalid_order")
        _transport_shapes(order, account)
        _need(order["action"] == "open", "unsupported_action")
        _need(order["instrument"] == policy.instrument, "instrument_mismatch")
        _pair(order["instrument"])
        size = _decimal(order["size"], "invalid_order", positive=True)
        stop = _decimal(order.get("stop"), "missing_or_invalid_stop", positive=True)
        _need(type(facts) is RiskFacts and type(facts.proposal) is ProposalFacts, "invalid_facts")
        _need(_text(facts.account_id) and type(facts.environment) is str, "invalid_facts")
        _need(
            facts.account_id == policy.account_id and facts.environment == policy.environment,
            "account_binding_mismatch",
        )
        _need(account["currency"] == policy.currency, "account_currency_mismatch")
        _need(facts.as_of == account["as_of"], "account_snapshot_mismatch")
        for value in (
            facts.complete,
            facts.reconciled,
            facts.kill_switch,
            facts.daily_loss_latched,
        ):
            _need(type(value) is bool, "invalid_facts")
        _need(facts.complete, "incomplete_facts")
        _need(facts.reconciled, "not_reconciled")
        _need(not facts.kill_switch, "kill_switch_active")
        _need(not facts.daily_loss_latched, "daily_loss_stop")
        _need(
            type(facts.orders_this_session) is int and facts.orders_this_session >= 0,
            "invalid_facts",
        )
        ceiling = _fresh(
            account["as_of"], now, now, policy.snapshot_max_age_seconds, "stale_account"
        )
        _need(
            _time(facts.session_started_at) <= ceiling <= now < _time(facts.session_ends_at),
            "invalid_session",
        )
        _decimal(account.get("balance"), "invalid_account", nonnegative=True)
        equity = _decimal(account.get("equity"), "missing_or_invalid_equity", positive=True)
        used = _decimal(account.get("margin_used"), "missing_or_invalid_margin", nonnegative=True)
        available = _decimal(
            account.get("margin_available"), "missing_or_invalid_margin", nonnegative=True
        )
        baseline = _decimal(facts.session_start_equity, "invalid_facts", positive=True)
        flow = _decimal(facts.external_cash_flow, "invalid_facts")
        quote = facts.proposal
        _need(quote.instrument == order["instrument"], "instrument_mismatch")
        _fresh(quote.as_of, now, ceiling, policy.quote_max_age_seconds, "stale_quote")
        bid = _decimal(quote.bid, "invalid_quote", positive=True)
        ask = _decimal(quote.ask, "invalid_quote", positive=True)
        _need(bid <= ask, "invalid_quote")
        factor = _decimal(quote.base_units_per_size, "invalid_units", positive=True)
        margin = _decimal(quote.margin_required, "invalid_margin_requirement", nonnegative=True)
        rate = _conversion(quote.conversion, _pair(quote.instrument), policy, now, ceiling)
        entry = ask if order["side"] == "buy" else bid
        if order["order_type"] == "limit":
            entry = _decimal(order.get("limit"), "invalid_order", positive=True)
        # Planned fill/stop arithmetic and current gross valuation are distinct.
        # A marketable sell limit below bid must not reduce its gross notional.
        gross_reference = max(ask, entry)
        _need(type(facts.exposures) is tuple, "invalid_exposures")
        context = Context(prec=1024)
        for signal in (Inexact, Rounded, FloatOperation):
            context.traps[signal] = True
        with localcontext(context):
            _need(equity == used + available, "inconsistent_account")
            _need(baseline + flow > 0, "inconsistent_account")
            quantity = size * factor
            order_loss = _loss(order["side"], entry, stop, quantity, rate, "invalid_stop_direction")
            gross = quantity * gross_reference * rate
            total_loss, instrument_units = order_loss, quantity
            identifiers = set()
            for row in facts.exposures:
                _need(type(row) is Exposure and _text(row.obligation_id), "invalid_exposures")
                _need(row.obligation_id not in identifiers, "duplicate_exposure")
                identifiers.add(row.obligation_id)
                _fresh(row.as_of, now, ceiling, policy.quote_max_age_seconds, "stale_exposure")
                units = _decimal(row.base_units, "invalid_exposures", positive=True)
                reference = _decimal(row.reference_price, "invalid_exposures", positive=True)
                protective = _decimal(row.stop_price, "invalid_exposures", positive=True)
                conversion = _conversion(
                    row.conversion, _pair(row.instrument), policy, now, ceiling
                )
                gross += units * reference * conversion
                total_loss += _loss(
                    row.side, reference, protective, units, conversion, "invalid_exposure_stop"
                )
                if row.instrument == policy.instrument:
                    instrument_units += units
            daily_loss = max(Decimal(0), baseline + flow - equity)
            remaining_margin = available - margin
            calculations = tuple(
                {
                    "order_base_units": quantity,
                    "order_entry_price": entry,
                    "order_gross_price": gross_reference,
                    "instrument_gross_base_units": instrument_units,
                    "order_planned_loss": order_loss,
                    "total_planned_loss": total_loss,
                    "gross_exposure": gross,
                    "daily_loss": daily_loss,
                    "daily_risk": daily_loss + total_loss,
                    "remaining_margin": remaining_margin,
                }.items()
            )
            for condition, code in (
                (daily_loss < policy.max_daily_loss, "daily_loss_stop"),
                (quantity <= policy.max_order_base_units, "order_size_limit"),
                (instrument_units <= policy.max_instrument_base_units, "instrument_exposure_limit"),
                (gross <= policy.max_gross_exposure, "gross_exposure_limit"),
                (order_loss <= policy.max_planned_order_loss, "order_loss_limit"),
                (total_loss <= policy.max_total_planned_loss, "total_loss_limit"),
                (daily_loss + total_loss <= policy.max_daily_risk, "daily_risk_limit"),
                (remaining_margin >= policy.min_margin_available, "insufficient_margin"),
                (facts.orders_this_session + 1 <= policy.max_session_orders, "session_order_limit"),
            ):
                _need(condition, code)
        return PreflightResult(True, "within_supplied_limits", calculations)
    except _Refusal as error:
        return PreflightResult(False, str(error), calculations)
    except DecimalException:
        return PreflightResult(False, "numeric_capacity_exceeded", calculations)
