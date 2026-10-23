"""Synthetic managed-PITR sidecar checks; no Azure call or restore proof."""

import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256

import pytest

from pipmind.safety.azure_identity_sql import AzureDatabaseIdentity
from pipmind.safety.checkpoint_sql import NativeBackupReference
from pipmind.safety.incarnation import DatabaseIncarnation
from pipmind.safety.managed_restore import (
    KIND,
    MAX_REFERENCE_BYTES,
    AzurePitrReference,
    ManagedRestoreRefused,
    decode_reference,
    encode_reference,
    reference_sha256,
    validate_restored_reference,
)

SOURCE = AzureDatabaseIdentity("synthetic-sql", "source", "source-physical")
TARGET = AzureDatabaseIdentity("synthetic-sql", "restored", "restored-physical")
NATIVE = DatabaseIncarnation(
    "synthetic-native",
    "source",
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
)


def reference(**changes):
    values = {
        "restore_id": "synthetic-restore",
        "source_identity": SOURCE,
        "target_identity": TARGET,
        "restore_point": "2026-10-22T10:00:00Z",
        "completed_at": "2026-10-22T10:05:00.123456Z",
        "source_inventory_sha256": "a" * 64,
        "management_receipt_sha256": "b" * 64,
        "history_receipt_sha256": "c" * 64,
    }
    values.update(changes)
    return AzurePitrReference(**values)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def validate(value, **changes):
    expected = {"inventory_sha256": "a" * 64, "source_identity": SOURCE, "target_identity": TARGET}
    expected.update(changes)
    return validate_restored_reference(value, **expected)


def test_strict_tagged_roundtrip_hash_and_identity_return_without_claiming_proof():
    value = reference()
    raw = encode_reference(value)
    assert decode_reference(raw) == value
    assert validate(value) is value
    assert reference_sha256(value) == sha256(raw.encode("utf-8")).hexdigest()
    body = json.loads(raw)
    assert body["kind"] == KIND == "azure-sql-pitr-v1"
    assert type(body["schema_version"]) is int and body["schema_version"] == 1
    assert body["source_identity"]["profile"] == "azure-sql-physical-v1"
    assert body["target_identity"]["physical_database_name"] == "restored-physical"
    assert set(body) == {
        "kind",
        "schema_version",
        "restore_id",
        "source_identity",
        "target_identity",
        "restore_point",
        "completed_at",
        "source_inventory_sha256",
        "management_receipt_sha256",
        "history_receipt_sha256",
    }
    assert "backup_sha256" not in body and "dispatch_authorized" not in body
    # Deliberately arbitrary synthetic hashes: the codec validates references,
    # not the existence/trust/content of the separately verified artifacts.
    with pytest.raises(FrozenInstanceError):
        value.completed_at = "2026-10-22T12:00:00Z"
    assert "synthetic-restore" not in repr(value)


@pytest.mark.parametrize(
    "name",
    [
        "restore_id",
        "source_identity",
        "target_identity",
        "restore_point",
        "completed_at",
        "source_inventory_sha256",
        "management_receipt_sha256",
        "history_receipt_sha256",
    ],
)
def test_every_retained_field_contributes_to_reference_hash(name):
    value = reference()
    changed = {
        "restore_id": "other-restore",
        "source_identity": replace(SOURCE, physical_database_name="other-source-physical"),
        "target_identity": replace(TARGET, physical_database_name="other-target-physical"),
        "restore_point": "2026-10-22T09:59:59Z",
        "completed_at": "2026-10-22T10:05:01Z",
        "source_inventory_sha256": "d" * 64,
        "management_receipt_sha256": "e" * 64,
        "history_receipt_sha256": "f" * 64,
    }
    assert reference_sha256(replace(value, **{name: changed[name]})) != reference_sha256(value)


@pytest.mark.parametrize(
    "restore_id", [None, True, 7, b"restore", "", "a\x00b", "x" * 129, "\U0001f600" * 65, "\ud800"]
)
def test_restore_id_exact_opaque_utf16_bound(restore_id):
    with pytest.raises(ManagedRestoreRefused, match="^managed_restore_invalid_reference$"):
        reference(restore_id=restore_id)


@pytest.mark.parametrize(
    "restore_id",
    ["x" * 128, "\U0001f600" * 64, " opaque ", "https://synthetic.invalid/opaque-restore-id"],
)
def test_restore_id_preserves_opaque_source_text_without_network_lookup(restore_id):
    value = reference(restore_id=restore_id)
    assert decode_reference(encode_reference(value)).restore_id == restore_id


@pytest.mark.parametrize(
    "field", ["source_inventory_sha256", "management_receipt_sha256", "history_receipt_sha256"]
)
@pytest.mark.parametrize(
    "bad", [None, False, 0, b"a" * 64, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n"]
)
def test_all_three_hashes_exact_lowercase_digest_text(field, bad):
    with pytest.raises(ManagedRestoreRefused, match="^managed_restore_invalid_reference$"):
        reference(**{field: bad})


@pytest.mark.parametrize("field", ["restore_point", "completed_at"])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        0,
        b"2026-10-22T10:00:00Z",
        "",
        "2026-10-22",
        "2026-10-22T10:00:00",
        "2026-10-22T10:00:00-04:00",
        "2026-10-22T10:00:00.1234567Z",
        "2026-02-30T10:00:00Z",
    ],
)
def test_times_require_explicit_utc_source_strings(field, bad):
    with pytest.raises(ManagedRestoreRefused, match="^managed_restore_invalid_reference$"):
        reference(**{field: bad})


def test_chronology_is_instant_order_not_lexical_and_source_precision_is_preserved():
    point, completed = "2026-10-22T10:00:00+00:00", "2026-10-22T10:00:00Z"
    value = reference(restore_point=point, completed_at=completed)
    assert decode_reference(encode_reference(value)).restore_point == point
    assert value.completed_at == completed
    with pytest.raises(ManagedRestoreRefused):
        reference(restore_point="2026-10-22T10:05:00.123457Z")


@pytest.mark.parametrize("field", ["source_identity", "target_identity"])
@pytest.mark.parametrize(
    "bad",
    [None, NATIVE, {}, ["synthetic-sql", "source", "physical"], "azure-sql-physical-v1", True],
)
def test_native_mixed_untyped_pins_are_never_azure_references(field, bad):
    with pytest.raises(ManagedRestoreRefused, match="^managed_restore_invalid_reference$"):
        reference(**{field: bad})


@pytest.mark.parametrize(
    "target",
    [
        SOURCE,
        replace(SOURCE, physical_database_name="other"),
        replace(
            SOURCE,
            logical_server="SYNTHETIC-SQL",
            database="SOURCE",
            physical_database_name="other",
        ),
        replace(TARGET, physical_database_name=SOURCE.physical_database_name),
    ],
)
def test_source_target_need_distinct_physical_and_conservative_logical_locations(target):
    with pytest.raises(ManagedRestoreRefused):
        reference(target_identity=target)


def test_exact_dto_types_and_revalidation_of_bypassed_dataclass_construction():
    class PinSubclass(AzureDatabaseIdentity):
        pass

    class ReferenceSubclass(AzurePitrReference):
        pass

    with pytest.raises(ManagedRestoreRefused):
        reference(source_identity=PinSubclass("sql", "source", "physical"))
    with pytest.raises(ManagedRestoreRefused):
        ReferenceSubclass(
            *[
                getattr(reference(), field)
                for field in (
                    "restore_id",
                    "source_identity",
                    "target_identity",
                    "restore_point",
                    "completed_at",
                    "source_inventory_sha256",
                    "management_receipt_sha256",
                    "history_receipt_sha256",
                )
            ]
        )
    for value in (object.__new__(AzurePitrReference), object.__new__(AzureDatabaseIdentity)):
        for operation in (encode_reference, reference_sha256, validate):
            with pytest.raises(ManagedRestoreRefused):
                operation(value)
    corrupted = reference()
    object.__setattr__(corrupted, "history_receipt_sha256", "not-hash")
    for operation in (encode_reference, reference_sha256, validate):
        with pytest.raises(ManagedRestoreRefused):
            operation(corrupted)


@pytest.mark.parametrize(
    "name",
    [
        "schema_version",
        "kind",
        "restore_id",
        "source_identity",
        "target_identity",
        "restore_point",
        "completed_at",
        "source_inventory_sha256",
        "management_receipt_sha256",
        "history_receipt_sha256",
    ],
)
def test_missing_or_extra_sidecar_fields_refuse(name):
    body = json.loads(encode_reference(reference()))
    body.pop(name)
    with pytest.raises(ManagedRestoreRefused):
        decode_reference(canonical(body))
    body = json.loads(encode_reference(reference()))
    body["extra"] = "never ignored"
    with pytest.raises(ManagedRestoreRefused):
        decode_reference(canonical(body))


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"{}",
        True,
        0,
        "",
        "[]",
        "null",
        "{}",
        "not-json",
        "\ufeff{}",
        "x" * (MAX_REFERENCE_BYTES + 1),
    ],
)
def test_wrong_raw_type_malformed_oversize_and_unknown_shapes_refuse(raw):
    with pytest.raises(ManagedRestoreRefused, match="^managed_restore_invalid_reference$"):
        decode_reference(raw)


def test_duplicate_nested_and_outer_keys_and_alternate_json_spelling_refuse():
    raw = encode_reference(reference(restore_id="synthetic-\u00e9"))
    variants = [
        raw + " ",
        " " + raw,
        raw.replace(":", ": ", 1),
        raw.replace(
            '"kind":"azure-sql-pitr-v1"', '"kind":"azure-sql-pitr-v1","kind":"azure-sql-pitr-v1"'
        ),
        raw.replace(
            '"profile":"azure-sql-physical-v1"',
            '"profile":"azure-sql-physical-v1","profile":"azure-sql-physical-v1"',
        ),
        raw.replace('"schema_version":1', '"schema_version":true'),
        raw.replace('"schema_version":1', '"schema_version":1.0'),
        raw.replace('"schema_version":1', '"schema_version":2'),
        raw.replace("azure-sql-pitr-v1", "native-backup-v1"),
        raw.replace("azure-sql-physical-v1", "sql-server-native-v1"),
        raw.replace("\\u00e9", "\u00e9"),
        json.dumps(json.loads(raw)),
    ]
    for changed in variants:
        with pytest.raises(ManagedRestoreRefused):
            decode_reference(changed)


@pytest.mark.parametrize(
    "source_pin",
    [
        {"profile": "azure-sql-physical-v1"},
        {
            "profile": "azure-sql-physical-v1",
            "logical_server": "sql",
            "database": "source",
            "physical_database_name": "physical",
            "success": True,
        },
        [
            "sql",
            "source",
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ],
        {
            "profile": "azure-sql-physical-v1",
            "logical_server": "sql",
            "database": "source",
            "physical_database_name": None,
        },
    ],
)
def test_nested_closed_pin_profile_cannot_accept_success_flags_or_native_arrays(source_pin):
    body = json.loads(encode_reference(reference()))
    body["source_identity"] = source_pin
    with pytest.raises(ManagedRestoreRefused):
        decode_reference(canonical(body))


@pytest.mark.parametrize(
    "field,expected",
    [
        ("inventory_sha256", "d" * 64),
        ("source_identity", replace(SOURCE, physical_database_name="different")),
        ("source_identity", replace(SOURCE, logical_server="SYNTHETIC-SQL")),
        ("target_identity", replace(TARGET, physical_database_name="different")),
        ("target_identity", SOURCE),
    ],
)
def test_expected_source_target_inventory_bindings_are_exact_and_never_adopted(field, expected):
    value = reference()
    before = encode_reference(value)
    with pytest.raises(ManagedRestoreRefused, match="^managed_restore_reference_mismatch$"):
        validate(value, **{field: expected})
    assert encode_reference(value) == before


@pytest.mark.parametrize(
    "field,expected",
    [("inventory_sha256", True), ("source_identity", NATIVE), ("target_identity", {})],
)
def test_expected_bindings_are_validated_not_interpreted_as_success(field, expected):
    with pytest.raises(ManagedRestoreRefused, match="^managed_restore_invalid_reference$"):
        validate(reference(), **{field: expected})


def test_native_backup_reference_cannot_be_managed_reference_or_back_again():
    native = NativeBackupReference("native", "a" * 64, "2026-10-22T10:00:00Z", "b" * 64)
    for operation in (encode_reference, reference_sha256, validate):
        with pytest.raises(ManagedRestoreRefused):
            operation(native)
    with pytest.raises(ManagedRestoreRefused):
        decode_reference(
            canonical(
                {
                    "backup_id": "native",
                    "sha256": "a" * 64,
                    "created_at": "2026-10-22T10:00:00Z",
                    "source_inventory_sha256": "b" * 64,
                }
            )
        )
