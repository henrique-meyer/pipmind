import json
from copy import deepcopy

import pytest

from pipmind.contracts import ContractError, Contracts, loads, utc
from pipmind.dataset import validate_episode


def test_every_external_message_contract_has_a_valid_control(contracts, messages):
    assert len(messages) == 12
    for name, value in messages.items():
        contracts.validate(name, value)
        invalid = deepcopy(value)
        invalid["schema_version"] = 2
        with pytest.raises(ContractError):
            contracts.validate(name, invalid)
        invalid = deepcopy(value)
        invalid["unexpected"] = True
        with pytest.raises(ContractError):
            contracts.validate(name, invalid)


@pytest.mark.parametrize("change", ["null_size", "missing_limit", "negative_size", "boolean_size"])
def test_open_order_contract_rejects_unsafe_or_incomplete_shapes(contracts, messages, change):
    order = deepcopy(messages["order-request"])
    if change == "missing_limit":
        del order["limit"]
    else:
        order["size"] = {"null_size": None, "negative_size": -1, "boolean_size": True}[change]
    with pytest.raises(ContractError):
        contracts.validate("order-request", order)


def test_close_and_modify_preserve_null_and_omission_semantics(contracts):
    base = {"schema_version": 1, "client_order_id": "synthetic", "instrument": "EUR/USD"}
    contracts.validate("order-request", base | {"action": "close", "size": None})
    contracts.validate("order-request", base | {"action": "modify", "stop": None})
    contracts.validate("order-request", base | {"action": "modify"})


def test_ownership_envelope_is_structural_not_reusable_authority(contracts, messages):
    envelope = {
        "schema_version": 1,
        "command": messages["order-request"],
        "ownership": {
            "owner_id": "synthetic",
            "generation": 1,
            "lease_expires_at": "2026-10-05T00:01:00Z",
            "account_ref": "demo",
        },
        "accepted_at": "2026-10-05T00:00:00Z",
        "causation_id": "synthetic-event",
    }
    contracts.validate("ownership-envelope", envelope)
    envelope["ownership"]["generation"] = 0
    with pytest.raises(ContractError):
        contracts.validate("ownership-envelope", envelope)


@pytest.mark.parametrize("value", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_json_rejects_duplicates_and_nonfinite_values(value):
    with pytest.raises(ContractError):
        loads(value)


def test_finite_numbers_and_utc_are_enforced(contracts, messages):
    quote = deepcopy(messages["price-event"])
    quote["bid"] = float("inf")
    with pytest.raises(ContractError):
        contracts.validate("price-event", quote)
    for value in ("2026-10-05T00:00:00", "2026-10-05T01:00:00+01:00"):
        with pytest.raises(ContractError):
            utc(value)
    quote = deepcopy(messages["price-event"])
    quote["received_at"] = "2026-10-05T01:00:00+01:00"
    with pytest.raises(ContractError):
        contracts.validate("price-event", quote)


def test_unregistered_schema_references_cannot_trigger_network_retrieval(tmp_path):
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:test",
        "$ref": "https://example.invalid/untrusted.json",
    }
    (tmp_path / "remote.schema.json").write_text(json.dumps(schema))
    with pytest.raises(ContractError, match="unknown_schema_reference"):
        Contracts(tmp_path)


@pytest.mark.parametrize(
    "invalid",
    [
        "not-a-dateZ",
        "2026-13-05T00:00:00Z",
        "2026-10-05 00:00:00Z",
        "2026-10-05T00:00:00.1234567Z",
    ],
)
def test_date_time_validation_does_not_depend_on_optional_format_packages(
    contracts, messages, invalid
):
    record = deepcopy(messages["decision-record"])
    record["ts"] = invalid
    with pytest.raises(ContractError, match="schema:decision-record"):
        contracts.validate("decision-record", record)


@pytest.mark.parametrize(
    "change",
    [
        "out_of_order",
        "future_receipt",
        "future_memory",
        "missing_step",
        "capability_mismatch",
        "incomplete_retention",
        "crossed_quote",
    ],
)
def test_dataset_semantics_cannot_silently_weaken_cases(cases, contracts, change):
    case = deepcopy(cases["D04"])
    if change == "out_of_order":
        case["events"].reverse()
    elif change == "future_receipt":
        case["events"][0]["payload"]["received_at"] = "2026-10-06T00:00:00Z"
    elif change == "future_memory":
        case["initial_mind"]["long_term"][0]["ts"] = "2026-10-06T00:00:00Z"
    elif change == "missing_step":
        case["model_responses"].clear()
    elif change == "capability_mismatch":
        case["required_capabilities"] = ["sql_ownership"]
    elif change == "incomplete_retention":
        case["expected"]["retained_event_ids"] = []
    else:
        case = deepcopy(cases["S02"])
        case["events"][0]["payload"]["bid"] = 2
    with pytest.raises(ContractError):
        validate_episode(case, contracts)
