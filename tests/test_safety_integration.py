"""Local integration input/mapping regressions; real transactions run in SQL CI."""

from dataclasses import replace
from decimal import Decimal

import pytest
from test_safety_admission_store import make_case

from pipmind.gateway.config import SymbolMap
from pipmind.safety.admission_sql import SqlAdmissionStore
from pipmind.safety.controls_sql import SqlControlStore
from pipmind.safety.integration_sql import IntegrationRefused, SqlSafetyGateway, _identifier
from pipmind.safety.lease_sql import LeaseBinding, SqlLeaseStore
from pipmind.safety.session_sql import SqlSessionStore


def symbols():
    return SymbolMap(
        [
            {
                "instrument": "EUR/USD",
                "market_id": 1,
                "unit": "unit",
                "quantity_factor": "1",
                "increment": "1",
                "minimum": "1",
            }
        ]
    )


def make_gateway():
    request, state, book = make_case()

    def no_sql():
        raise AssertionError("constructor must not connect")

    binding = state.spec.binding
    sessions = SqlSessionStore(no_sql, binding, state.spec.session_id)
    leases = SqlLeaseStore(no_sql, LeaseBinding(binding.account_id, "demo", "orders"), "epoch")
    admission = SqlAdmissionStore(no_sql, leases, sessions, book)
    controls = SqlControlStore(no_sql, binding)
    source = symbols()
    return (
        SqlSafetyGateway(
            admission, controls, source, base_units_per_size={"EUR/USD": Decimal("1")}
        ),
        request,
        source,
    )


def test_constructor_freezes_symbol_mapping_without_connecting():
    gateway, request, source = make_gateway()
    assert gateway._mapping_code(request) is None
    source.by_symbol.clear()
    assert gateway._mapping_code(request) is None


@pytest.mark.parametrize("value", [None, True, 2, "", " x", "x\x00y", "a" * 129, "\ud800"])
def test_untrusted_identifiers_refuse_without_sql(value):
    with pytest.raises(IntegrationRefused, match="^invalid_integration_id$"):
        _identifier(value)


def test_manual_origin_cannot_turn_dry_receipt_into_override_authority():
    gateway, request, _ = make_gateway()
    for origin, override_id in (("owner", None), ("model", "override-1"), (False, None)):
        with pytest.raises(IntegrationRefused, match="^unsupported_order_origin$"):
            gateway.accept(None, request, origin=origin, override_id=override_id)


def test_gateway_checks_exact_mapping_and_never_resizes():
    gateway, request, _ = make_gateway()
    altered = replace(request, order={**request.order, "size": Decimal("0.1")})
    assert gateway._mapping_code(altered) == "gateway_mapping_refused"
    assert altered.order["size"] == Decimal("0.1")
    unsupported = replace(request, order={**request.order, "instrument": "GBP/USD"})
    assert gateway._mapping_code(unsupported) == "gateway_mapping_refused"
    close = replace(request, order={**request.order, "action": "close"})
    assert gateway._mapping_code(close) == "unsupported_dry_action"


def test_trusted_base_units_cannot_be_replaced_by_smaller_request_factor():
    gateway, request, _ = make_gateway()
    altered = replace(
        request,
        order={**request.order, "size": Decimal("2500")},
        facts=replace(
            request.facts,
            proposal=replace(request.facts.proposal, base_units_per_size=Decimal("0.01")),
        ),
    )
    assert gateway._mapping_code(altered) == "gateway_unit_factor_mismatch"


@pytest.mark.parametrize(
    "factors",
    [
        None,
        {},
        {"EUR/USD": 1},
        {"EUR/USD": Decimal("NaN")},
        {"EUR/USD": Decimal("0")},
        {"GBP/USD": Decimal("1")},
    ],
)
def test_constructor_requires_exact_explicit_unit_factors(factors):
    gateway, _, _ = make_gateway()
    with pytest.raises(IntegrationRefused, match="^invalid_integration_unit_factors$"):
        SqlSafetyGateway(
            gateway.admission, gateway.controls, symbols(), base_units_per_size=factors
        )


def test_constructor_rejects_cross_account_controls():
    gateway, _, _ = make_gateway()
    wrong = replace(gateway.admission.binding, account_id="other")
    controls = SqlControlStore(lambda: None, wrong)
    with pytest.raises(IntegrationRefused, match="^integration_binding_mismatch$"):
        SqlSafetyGateway(
            gateway.admission, controls, symbols(), base_units_per_size={"EUR/USD": Decimal("1")}
        )
