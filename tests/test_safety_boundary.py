"""Synthetic numerical policies only; no owner defaults, venue or runtime wiring."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext, localcontext

import pytest

from pipmind.contracts import Contracts
from pipmind.safety import Conversion, Exposure, ProposalFacts, RiskFacts, RiskPolicy, preflight

D = Decimal
NOW = datetime(2026, 1, 5, 12, 0, 10, tzinfo=UTC)
STAMP = "2026-01-05T12:00:00Z"


@pytest.fixture
def inputs():
    policy = RiskPolicy(
        policy_id="synthetic-policy",
        account_id="synthetic-account",
        environment="demo",
        currency="USD",
        instrument="EUR/USD",
        max_order_base_units=D("2000"),
        max_instrument_base_units=D("5000"),
        max_gross_exposure=D("10000"),
        max_planned_order_loss=D("50"),
        max_total_planned_loss=D("100"),
        max_daily_loss=D("100"),
        max_daily_risk=D("200"),
        min_margin_available=D("0"),
        max_session_orders=10,
        snapshot_max_age_seconds=30,
        quote_max_age_seconds=15,
        conversion_max_age_seconds=15,
    )
    order = {
        "schema_version": 1,
        "client_order_id": "synthetic-open",
        "action": "open",
        "instrument": "EUR/USD",
        "side": "buy",
        "size": D("1000"),
        "order_type": "market",
        "stop": D("1.09"),
    }
    account = {
        "schema_version": 1,
        "as_of": STAMP,
        "currency": "USD",
        "balance": D("10000"),
        "equity": D("10000"),
        "margin_used": D("1000"),
        "margin_available": D("9000"),
    }
    conversion = Conversion("USD", "USD", D("1"), STAMP)
    quote = ProposalFacts("EUR/USD", D("1.1000"), D("1.1002"), STAMP, D("1"), D("40"), conversion)
    exposure = Exposure(
        "existing-position", "GBP/USD", "buy", D("1000"), D("1.30"), D("1.29"), STAMP, conversion
    )
    facts = RiskFacts(
        "synthetic-account",
        "demo",
        STAMP,
        True,
        True,
        False,
        False,
        "2026-01-05T00:00:00Z",
        "2026-01-06T00:00:00Z",
        D("10000"),
        D("0"),
        0,
        quote,
        (exposure,),
    )
    return order, account, facts, policy


def evaluate(inputs, **kwargs):
    order, account, facts, policy = inputs
    return preflight(order, account, facts, policy, now=kwargs.get("now", NOW))


def test_exact_arithmetic_and_no_dispatch_authority_or_input_mutation(inputs):
    original = deepcopy(inputs)
    result = evaluate(inputs)
    assert result.within_limits and result.code == "within_supplied_limits"
    assert result.dispatch_authorized is False and inputs == original
    assert dict(result.calculations) == {
        "order_base_units": D("1000"),
        "order_entry_price": D("1.1002"),
        "order_gross_price": D("1.1002"),
        "instrument_gross_base_units": D("1000"),
        "order_planned_loss": D("10.2"),
        "total_planned_loss": D("20.2"),
        "gross_exposure": D("2400.2"),
        "daily_loss": D("0"),
        "daily_risk": D("20.2"),
        "remaining_margin": D("8960"),
    }
    with pytest.raises(FrozenInstanceError):
        result.within_limits = False


def test_supported_positive_fixtures_match_normative_v1_transport_contracts(inputs):
    order, account, _, _ = inputs
    contracts = Contracts()
    contracts.validate("order-request", order)
    contracts.validate("account-snapshot", account)
    order.update(side="sell", order_type="limit", limit=D("1.12"), stop=D("1.13"))
    contracts.validate("order-request", order)


def test_preflight_performs_no_file_or_network_access(inputs, monkeypatch):
    from pathlib import Path
    from socket import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight attempted external access")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "connect", forbidden)
    assert evaluate(inputs).within_limits


@pytest.mark.parametrize(
    "field,metric,code",
    [
        ("max_order_base_units", "order_base_units", "order_size_limit"),
        ("max_instrument_base_units", "instrument_gross_base_units", "instrument_exposure_limit"),
        ("max_gross_exposure", "gross_exposure", "gross_exposure_limit"),
        ("max_planned_order_loss", "order_planned_loss", "order_loss_limit"),
        ("max_total_planned_loss", "total_planned_loss", "total_loss_limit"),
        ("max_daily_risk", "daily_risk", "daily_risk_limit"),
    ],
)
def test_inclusive_boundary_and_exact_small_excess_refusal(inputs, field, metric, code):
    order, account, facts, policy = inputs
    limit = dict(evaluate(inputs).calculations)[metric]
    assert preflight(
        order, account, facts, replace(policy, **{field: limit}), now=NOW
    ).within_limits
    refused = preflight(
        order, account, facts, replace(policy, **{field: limit - D("1E-12")}), now=NOW
    )
    assert not refused.within_limits and refused.code == code and not refused.dispatch_authorized


def test_unit_factor_is_explicit_and_never_assumed_to_mean_lots(inputs):
    order, account, facts, policy = inputs
    order["size"] = D("1")
    facts = replace(facts, proposal=replace(facts.proposal, base_units_per_size=D("1000")))
    result = preflight(order, account, facts, policy, now=NOW)
    assert result.within_limits and dict(result.calculations)["order_base_units"] == D("1000")
    for bad in (None, D("0"), 1000, 1000.0):
        result = preflight(
            order,
            account,
            replace(facts, proposal=replace(facts.proposal, base_units_per_size=bad)),
            policy,
            now=NOW,
        )
        assert not result.within_limits and result.code == "invalid_units"


def test_opposite_sides_are_gross_not_netted(inputs):
    order, account, facts, policy = inputs
    opposite = replace(
        facts.exposures[0],
        obligation_id="hedge",
        instrument="EUR/USD",
        side="sell",
        base_units=D("1000"),
        reference_price=D("1.10"),
        stop_price=D("1.12"),
    )
    facts = replace(facts, exposures=(opposite,))
    result = preflight(order, account, facts, policy, now=NOW)
    assert dict(result.calculations)["instrument_gross_base_units"] == D("2000")
    assert dict(result.calculations)["gross_exposure"] == D("2200.2")


def test_marketable_sell_limit_cannot_hide_current_gross_notional(inputs):
    order, account, facts, policy = inputs
    order.update(side="sell", order_type="limit", limit=D("1.05"), stop=D("1.13"))
    facts = replace(facts, exposures=())
    policy = replace(policy, max_gross_exposure=D("1075"), max_planned_order_loss=D("100"))
    result = preflight(order, account, facts, policy, now=NOW)
    metrics = dict(result.calculations)
    assert D("1050") < policy.max_gross_exposure < D("1100.2")
    assert not result.within_limits and result.code == "gross_exposure_limit"
    assert metrics["order_entry_price"] == D("1.05")
    assert metrics["order_gross_price"] == facts.proposal.ask
    assert metrics["gross_exposure"] == D("1100.2")
    assert metrics["order_planned_loss"] == D("80")


def test_cashflows_equity_stop_and_latch_cannot_be_bypassed_with_balance(inputs):
    order, account, facts, policy = inputs
    account.update(equity=D("9900"), margin_available=D("8900"), balance=D("50000"))
    assert evaluate(inputs).code == "daily_loss_stop"  # Equity loss reaches the boundary.
    withdrawn = replace(facts, external_cash_flow=D("-100"))
    assert preflight(order, account, withdrawn, policy, now=NOW).within_limits
    account.update(equity=D("10100"), margin_available=D("9100"))
    assert (
        preflight(order, account, replace(facts, daily_loss_latched=True), policy, now=NOW).code
        == "daily_loss_stop"
    )
    account.update(equity=D("10000"), margin_available=D("9000"))
    assert (
        preflight(order, account, replace(facts, external_cash_flow=D("100")), policy, now=NOW).code
        == "daily_loss_stop"
    )


def test_margin_and_session_count_boundaries(inputs):
    order, account, facts, policy = inputs
    exact = replace(policy, min_margin_available=D("8960"))
    assert preflight(order, account, facts, exact, now=NOW).within_limits
    assert (
        preflight(
            order, account, facts, replace(exact, min_margin_available=D("8960.01")), now=NOW
        ).code
        == "insufficient_margin"
    )
    assert preflight(
        order, account, replace(facts, orders_this_session=9), policy, now=NOW
    ).within_limits
    assert (
        preflight(order, account, replace(facts, orders_this_session=10), policy, now=NOW).code
        == "session_order_limit"
    )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("kill_switch", True, "kill_switch_active"),
        ("complete", False, "incomplete_facts"),
        ("reconciled", False, "not_reconciled"),
        ("complete", 1, "invalid_facts"),
        ("orders_this_session", True, "invalid_facts"),
        ("account_id", "foreign-account", "account_binding_mismatch"),
        ("environment", "live", "account_binding_mismatch"),
        ("as_of", "2026-01-05T11:59:59Z", "account_snapshot_mismatch"),
        ("session_ends_at", STAMP, "invalid_session"),
    ],
)
def test_incomplete_unreconciled_killed_and_inconsistent_bindings_refuse(
    inputs, field, value, code
):
    order, account, facts, policy = inputs
    result = preflight(order, account, replace(facts, **{field: value}), policy, now=NOW)
    assert not result.within_limits and result.code == code


@pytest.mark.parametrize("value", [None, True, 1, 1.5, "1", D("NaN"), D("Infinity"), D("0")])
def test_owner_limits_are_required_exact_finite_positive_decimals(inputs, value):
    order, account, facts, policy = inputs
    result = preflight(order, account, facts, replace(policy, max_daily_risk=value), now=NOW)
    assert not result.within_limits and result.code == "invalid_policy"


@pytest.mark.parametrize("value", [None, True, 1000, 1000.0, D("NaN"), D("-1")])
def test_missing_or_invalid_order_quantity_never_uses_float_or_boolean_coercion(inputs, value):
    order, account, facts, policy = inputs
    order["size"] = value
    result = evaluate(inputs)
    assert not result.within_limits
    assert result.code in {"invalid_order", "invalid_contract"}


def test_nonfinite_even_unused_market_limit_refuses(inputs):
    inputs[0]["limit"] = D("NaN")
    assert evaluate(inputs).code == "invalid_order"


@pytest.mark.parametrize("field", ["equity", "margin_used", "margin_available"])
def test_optional_contract_account_facts_are_required_for_risk(inputs, field):
    del inputs[1][field]
    assert not evaluate(inputs).within_limits


def test_inconsistent_margins_and_nonfinite_account_refuse(inputs):
    inputs[1]["margin_available"] = D("9000.01")
    assert evaluate(inputs).code == "inconsistent_account"
    inputs[1]["equity"] = D("NaN")
    assert evaluate(inputs).code == "missing_or_invalid_equity"


def test_freshness_boundary_is_exact_to_microsecond_and_future_facts_refuse(inputs):
    order, account, facts, policy = inputs
    assert evaluate(inputs, now=NOW + timedelta(seconds=5)).within_limits
    assert evaluate(inputs, now=NOW + timedelta(seconds=5, microseconds=1)).code == "stale_quote"
    assert evaluate(inputs, now=NOW + timedelta(seconds=21)).code == "stale_account"
    future = replace(facts.proposal, as_of="2026-01-05T12:00:01Z")
    assert (
        preflight(order, account, replace(facts, proposal=future), policy, now=NOW).code
        == "future_facts"
    )


@pytest.mark.parametrize(
    "conversion,code",
    [
        (None, "invalid_conversion"),
        (Conversion("EUR", "USD", D("1"), STAMP), "conversion_mismatch"),
        (Conversion("USD", "EUR", D("1"), STAMP), "conversion_mismatch"),
        (Conversion("USD", "USD", D("2"), STAMP), "conversion_mismatch"),
        (Conversion("USD", "USD", D("0"), STAMP), "invalid_conversion"),
        (Conversion("USD", "USD", D("1"), "2026-01-05T11:59:00Z"), "stale_conversion"),
    ],
)
def test_missing_or_wrong_direction_identity_and_stale_conversion_refuse(inputs, conversion, code):
    order, account, facts, policy = inputs
    facts = replace(facts, proposal=replace(facts.proposal, conversion=conversion))
    assert preflight(order, account, facts, policy, now=NOW).code == code


def test_explicit_cross_currency_conversion_and_limit_sell_calculation(inputs):
    order, account, facts, policy = inputs
    policy = replace(policy, currency="EUR")
    account["currency"] = "EUR"
    rate = Conversion("USD", "EUR", D("0.9"), STAMP)
    facts = replace(facts, proposal=replace(facts.proposal, conversion=rate), exposures=())
    order.update(side="sell", order_type="limit", limit=D("1.12"), stop=D("1.13"))
    result = preflight(order, account, facts, policy, now=NOW)
    assert result.within_limits
    assert dict(result.calculations)["order_planned_loss"] == D("9")
    assert dict(result.calculations)["gross_exposure"] == D("1008")


def test_missing_crossed_stop_and_incomplete_exposure_never_disappear(inputs):
    order, account, facts, policy = inputs
    order.pop("stop")
    assert evaluate(inputs).code == "missing_or_invalid_stop"
    order["stop"] = D("1.2")
    assert evaluate(inputs).code == "invalid_stop_direction"
    order["stop"] = D("1.09")
    for exposures in ([facts.exposures[0]], (None,), facts.exposures * 2):
        result = preflight(order, account, replace(facts, exposures=exposures), policy, now=NOW)
        assert not result.within_limits
    assert (
        preflight(
            order,
            account,
            replace(facts, exposures=(replace(facts.exposures[0], conversion=None),)),
            policy,
            now=NOW,
        ).code
        == "invalid_conversion"
    )


@pytest.mark.parametrize("action", ["close", "modify"])
def test_close_and_modify_are_outside_supported_slice_not_approved_risk_reductions(inputs, action):
    inputs[0]["action"] = action
    assert evaluate(inputs).code == "unsupported_action"


def test_unsupported_instrument_and_unknown_contract_properties_refuse(inputs):
    order, account, facts, policy = inputs
    assert (
        preflight(order, account, facts, replace(policy, instrument="EURUSD"), now=NOW).code
        == "unsupported_instrument"
    )
    order["invented"] = "unknown"
    assert evaluate(inputs).code == "invalid_contract"


def test_decimal_context_does_not_change_arithmetic_or_leak_context(inputs):
    with localcontext() as context:
        context.prec = 2
        before = deepcopy(getcontext())
        result = evaluate(inputs)
        assert result.within_limits and dict(result.calculations)["gross_exposure"] == D("2400.2")
        assert getcontext().prec == before.prec and getcontext().flags == before.flags


def test_supported_numeric_capacity_fails_closed_instead_of_rounding(inputs):
    order, account, facts, policy = inputs
    order["size"] = D("1E100")
    assert evaluate(inputs).code == "numeric_capacity_exceeded"
    assert preflight(order, account, facts, None, now=NOW).code == "invalid_policy"
