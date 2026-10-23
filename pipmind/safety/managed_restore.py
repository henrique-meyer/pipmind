"""Managed Azure PITR sidecar references; never native backup or restore proof.

Receipt hashes name separately verified trusted artifacts. Matching this bounded
sidecar establishes neither artifact existence, restored contents nor execution
authority. No management API, file lookup or network request occurs here.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from pipmind.contracts import ContractError, utc
from pipmind.safety.azure_identity_sql import (
    AzureDatabaseIdentity,
    AzureIdentityRefused,
    decode_identity,
    encode_identity,
)
from pipmind.safety.identity_profiles import same_database

KIND = "azure-sql-pitr-v1"
MAX_REFERENCE_BYTES = 16384
_FIELDS = (
    "restore_id",
    "source_identity",
    "target_identity",
    "restore_point",
    "completed_at",
    "source_inventory_sha256",
    "management_receipt_sha256",
    "history_receipt_sha256",
)
_PIN_FIELDS = ("profile", "logical_server", "database", "physical_database_name")


class ManagedRestoreRefused(Exception):
    """Fixed invalid-reference or expected-binding refusal; no restore verdict."""


def _need(value, code="managed_restore_invalid_reference"):
    if not value:
        raise ManagedRestoreRefused(code)


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _pin(value):
    _need(type(value) is AzureDatabaseIdentity)
    try:
        return encode_identity(value)
    except (AzureIdentityRefused, AttributeError):
        raise ManagedRestoreRefused("managed_restore_invalid_reference") from None


def _validate(value):
    _need(type(value) is AzurePitrReference)
    try:
        _need(
            type(value.restore_id) is str
            and bool(value.restore_id)
            and "\x00" not in value.restore_id
            and len(value.restore_id.encode("utf-16-le")) <= 256
        )
        _pin(value.source_identity)
        _pin(value.target_identity)
        _need(not same_database(value.source_identity, value.target_identity))
        _need(type(value.restore_point) is str and type(value.completed_at) is str)
        _need(utc(value.restore_point) <= utc(value.completed_at))
        _need(
            all(
                _hash(item)
                for item in (
                    value.source_inventory_sha256,
                    value.management_receipt_sha256,
                    value.history_receipt_sha256,
                )
            )
        )
    except (AttributeError, UnicodeError, ContractError):
        raise ManagedRestoreRefused("managed_restore_invalid_reference") from None


@dataclass(frozen=True, slots=True, repr=False)
class AzurePitrReference:
    """Explicit source/target pins and references to separately verified evidence."""

    restore_id: str
    source_identity: AzureDatabaseIdentity
    target_identity: AzureDatabaseIdentity
    restore_point: str
    completed_at: str
    source_inventory_sha256: str
    management_receipt_sha256: str
    history_receipt_sha256: str

    def __post_init__(self):
        _validate(self)

    def __repr__(self):
        return "AzurePitrReference(<redacted>)"


def encode_reference(reference):
    """Encode only a validated exact DTO; do not fetch or verify its artifacts."""
    _validate(reference)
    value = {"schema_version": 1, "kind": KIND}
    for key in _FIELDS:
        item = getattr(reference, key)
        value[key] = json.loads(_pin(item)) if key.endswith("_identity") else item
    raw = json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    _need(len(raw.encode("utf-8")) <= MAX_REFERENCE_BYTES)
    return raw


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _decode_pin(value):
    # The enclosing canonical codec sorts nested keys. Reconstruct only the
    # existing Azure codec's fixed envelope order, then validate it exactly.
    _need(type(value) is dict and set(value) == set(_PIN_FIELDS))
    raw = json.dumps(
        {key: value[key] for key in _PIN_FIELDS}, ensure_ascii=True, separators=(",", ":")
    )
    return decode_identity(raw)


def decode_reference(raw):
    """Reject noncanonical, duplicate, extra or unknown-profile sidecar data."""
    try:
        _need(type(raw) is str and len(raw.encode("utf-8")) <= MAX_REFERENCE_BYTES)
        value = json.loads(raw, object_pairs_hook=_unique_object)
        _need(
            type(value) is dict
            and set(value) == {"schema_version", "kind", *_FIELDS}
            and type(value["schema_version"]) is int
            and value["schema_version"] == 1
            and value["kind"] == KIND
        )
        result = AzurePitrReference(
            **{
                key: _decode_pin(value[key]) if key.endswith("_identity") else value[key]
                for key in _FIELDS
            }
        )
        _need(encode_reference(result) == raw)
        return result
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        AzureIdentityRefused,
    ):
        raise ManagedRestoreRefused("managed_restore_invalid_reference") from None


def reference_sha256(reference):
    """Hash canonical references only; the digest is not artifact verification."""
    return hashlib.sha256(encode_reference(reference).encode("utf-8")).hexdigest()


def validate_restored_reference(reference, *, inventory_sha256, source_identity, target_identity):
    """Check exact expected bindings, with artifact verification left to the caller.

    The caller must independently verify trusted management and full-history
    receipts and compare actual complete restored contents. This reference does
    not provide an authority fence, lease barrier or positive activation permit.
    """
    _validate(reference)
    _need(_hash(inventory_sha256))
    _pin(source_identity)
    _pin(target_identity)
    _need(
        reference.source_inventory_sha256 == inventory_sha256
        and reference.source_identity == source_identity
        and reference.target_identity == target_identity,
        "managed_restore_reference_mismatch",
    )
    return reference
