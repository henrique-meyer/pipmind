"""Local-only JSON Schema validation; references can never fetch remote schemas."""

import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "contracts" / "v1"


def _configured_dir(name, default):
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default


def data_dir():
    """Private working-data root: PIPMIND_DATA_DIR, else the ignored in-repo .data/."""
    return _configured_dir("PIPMIND_DATA_DIR", ROOT / ".data")


def reports_dir():
    """Private report root: PIPMIND_REPORTS_DIR, else the ignored in-repo evals/reports/."""
    return _configured_dir("PIPMIND_REPORTS_DIR", ROOT / "evals" / "reports")


def private_output_allowed(path):
    """Outside the checkout anything goes; inside it only the ignored private roots."""
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        return True
    roots = (ROOT / ".data", ROOT / "evals" / "reports", data_dir(), reports_dir())
    return any(path.is_relative_to(root) for root in roots)


class ContractError(ValueError):
    """Invalid versioned input; messages intentionally omit private payload values."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise ContractError("nonfinite_json_number")


def loads(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError) as error:
        raise ContractError("invalid_json") from error


def utc(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value
        ):
            raise ValueError
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError) as error:
        raise ContractError("utc_timestamp_required") from error


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("nonfinite_json_number")
    if isinstance(value, dict):
        for child in value.values():
            _finite(child)
    elif isinstance(value, list):
        for child in value:
            _finite(child)


class Contracts:
    def __init__(self, directory=SCHEMA_ROOT):
        self.schemas = {
            path.name.removesuffix(".schema.json"): loads(path.read_bytes())
            for path in sorted(Path(directory).glob("*.schema.json"))
        }
        if not self.schemas:
            raise ContractError("missing_schemas")
        for schema in self.schemas.values():
            Draft202012Validator.check_schema(schema)
        identifiers = {schema["$id"] for schema in self.schemas.values()}
        if len(identifiers) != len(self.schemas):
            raise ContractError("duplicate_schema_id")

        def check_references(value):
            if isinstance(value, dict):
                if "$ref" in value and value["$ref"].split("#")[0] not in identifiers:
                    raise ContractError("unknown_schema_reference")
                for child in value.values():
                    check_references(child)
            elif isinstance(value, list):
                for child in value:
                    check_references(child)

        for schema in self.schemas.values():
            check_references(schema)
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in self.schemas.values()
        )
        checker = FormatChecker()

        @checker.checks("date-time", raises=ContractError)
        def check_datetime(value):
            if isinstance(value, str):
                utc(value)
            return True

        self.validators = {
            name: Draft202012Validator(schema, registry=registry, format_checker=checker)
            for name, schema in self.schemas.items()
        }

    def validate(self, name, value):
        _finite(value)
        if name not in self.validators:
            raise ContractError("unknown_contract")
        if next(self.validators[name].iter_errors(value), None) is not None:
            raise ContractError(f"schema:{name}")
        return value


def event_id(envelope):
    return envelope["message_id"]


def event_facts(envelope):
    return {
        "kind": envelope["kind"],
        "message_id": event_id(envelope),
        "payload": {
            key: value for key, value in envelope["payload"].items() if key != "received_at"
        },
    }


def receipt_time(envelope):
    payload = envelope["payload"]
    return utc(
        payload.get(
            "received_at", payload.get("ts", payload.get("as_of", payload.get("observed_at")))
        )
    )


def validate_event(envelope, contracts):
    contracts.validate("event-envelope", envelope)
    payload = envelope["payload"]
    received = receipt_time(envelope)
    available = utc(envelope["available_at"])
    if received > available:
        raise ContractError("event_available_before_receipt")
    if "event_id" in payload and payload["event_id"] != event_id(envelope):
        raise ContractError("event_identity_mismatch")
    for field in ("ts", "venue_ts", "released_at", "published_at", "observed_at", "as_of"):
        if field in payload and payload[field] is not None and utc(payload[field]) > received:
            raise ContractError("event_from_future")
    if envelope["kind"] == "PriceEvent" and payload["bid"] > payload["ask"]:
        raise ContractError("crossed_quote")
    if envelope["kind"] == "ReleaseEvent" and "publication_time_status" in payload:
        unknown = payload["publication_time_status"] == "unknown"
        if unknown != (payload["released_at"] is None):
            raise ContractError("inconsistent_publication_evidence")
        if unknown and payload.get("publication_time_precision", "unknown") != "unknown":
            raise ContractError("inconsistent_publication_precision")
    return available


def validate_initial_mind(mind, now, contracts):
    contracts.validate("mind", mind)
    records = mind["journal"] + mind["long_term"]
    times = {row["record_id"]: utc(row["ts"]) for row in records}
    if len(times) != len(records):
        raise ContractError("duplicate_initial_record")
    if any(moment > now for moment in times.values()):
        raise ContractError("future_initial_memory")
    for row in records:
        if any(ref not in times or times[ref] > utc(row["ts"]) for ref in row["evidence_ids"]):
            raise ContractError("invalid_initial_evidence")
    if any(utc(row["as_of"]) > now for row in mind["working_memory"]["position_theses"]):
        raise ContractError("future_position_thesis")
