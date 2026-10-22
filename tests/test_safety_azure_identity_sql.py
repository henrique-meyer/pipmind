"""Scripted same-cursor proof only; these tests do not connect to Azure SQL."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from pipmind.safety.azure_identity_sql import (
    PROFILE,
    AzureDatabaseIdentity,
    AzureIdentityRefused,
    AzureIdentityUnavailable,
    decode_identity,
    encode_identity,
    identity_sha256,
    read_expected_identity,
)
from pipmind.safety.incarnation import DatabaseIncarnation, encode_incarnation


def pin(**changes):
    fields = dict(
        logical_server="synthetic-server",
        database="synthetic-app",
        physical_database_name="opaque physical / α",
    )
    return AzureDatabaseIdentity(**(fields | changes))


def context(**changes):
    fields = dict(
        edition=5,
        server="synthetic-server",
        database="synthetic-app",
        database_id=7,
        state=1,
        depth=1,
    )
    return tuple((fields | changes).values())


def physical(**changes):
    fields = dict(database_id=7, database="synthetic-app", physical="opaque physical / α")
    return tuple((fields | changes).values())


class Cursor:
    def __init__(self, rows=None, *, autocommit=False):
        self.connection = SimpleNamespace(autocommit=autocommit)
        self.rows = [[context()], [physical()], [context()], [physical()]] if rows is None else rows
        self.queries = []
        self.current = []

    def execute(self, query):
        self.queries.append(query)
        value = self.rows[len(self.queries) - 1]
        if isinstance(value, BaseException):
            raise value
        self.current = list(value)

    def fetchone(self):
        return self.current.pop(0) if self.current else None


def test_exact_opaque_pin_roundtrip_and_profile_separated_hash():
    expected = pin()
    body = encode_identity(expected)
    assert json.loads(body) == {
        "profile": PROFILE,
        "logical_server": expected.logical_server,
        "database": expected.database,
        "physical_database_name": expected.physical_database_name,
    }
    assert decode_identity(body) == expected
    assert identity_sha256(expected) == hashlib.sha256(body.encode()).hexdigest()
    assert identity_sha256(pin(physical_database_name="OPAQUE physical / α")) != (
        identity_sha256(expected)
    )
    assert "synthetic" not in repr(expected)
    assert "opaque" not in repr(expected)


@pytest.mark.parametrize("field", ["logical_server", "database", "physical_database_name"])
@pytest.mark.parametrize("value", [None, False, 3, "", "a\x00b", "\ud800", "a" * 129])
def test_invalid_pin_fields_fixed_refusal(field, value):
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_invalid$"):
        pin(**{field: value})


def test_identifiers_opaque_exact_utf16_bound_no_normalization():
    expected = pin(physical_database_name=" " + "𝄞" * 63 + " ")
    assert decode_identity(encode_identity(expected)) == expected
    assert expected.physical_database_name.startswith(" ")
    with pytest.raises(AzureIdentityRefused):
        pin(physical_database_name="𝄞" * 65)


@pytest.mark.parametrize(
    "body",
    [
        "null",
        "{}",
        "[]",
        '["native-v1","server","db","physical"]',
        '["azure-sql-physical-v1","server","db",false]',
        '["azure-sql-physical-v1", "server","db","physical"]',
        "x" * 4097,
        b"[]",
    ],
)
def test_codec_rejects_noncanonical_wrong_profile_or_malformed(body):
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_invalid$"):
        decode_identity(body)


def test_native_profile_is_not_accepted_or_relabelled():
    native = DatabaseIncarnation(
        "synthetic-server",
        "synthetic-app",
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
    cursor = Cursor()
    with pytest.raises(AzureIdentityRefused, match="azure_identity_invalid"):
        read_expected_identity(cursor, native)
    assert cursor.queries == []
    with pytest.raises(AzureIdentityRefused):
        decode_identity(encode_incarnation(native))


def test_native_server_equal_to_azure_profile_cannot_collide_with_new_envelope():
    native = DatabaseIncarnation(
        PROFILE,
        "synthetic-native-db",
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
    original = encode_incarnation(native)
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_invalid$"):
        decode_identity(original)
    azure = AzureDatabaseIdentity(native.database, native.database_guid, native.recovery_fork_guid)
    assert encode_identity(azure) != original
    assert encode_incarnation(native) == original


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "native-v1"},
        {"profile": True},
        {"profile": None},
        {"profile": []},
        {"logical_server": {"name": "server"}},
        {"database": ["db"]},
        {"physical_database_name": False},
        {"future_field": "unsupported"},
    ],
)
def test_named_envelope_rejects_unknown_fields_and_types(change):
    value = json.loads(encode_identity(pin())) | change
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_invalid$"):
        decode_identity(json.dumps(value, separators=(",", ":")))


@pytest.mark.parametrize(
    "field", ["profile", "logical_server", "database", "physical_database_name"]
)
def test_named_envelope_requires_every_field(field):
    value = json.loads(encode_identity(pin()))
    del value[field]
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_invalid$"):
        decode_identity(json.dumps(value, separators=(",", ":")))


@pytest.mark.parametrize(
    "field", ["profile", "logical_server", "database", "physical_database_name"]
)
def test_named_envelope_rejects_duplicate_keys_even_when_values_match(field):
    body = encode_identity(pin())
    value = json.loads(body)[field]
    duplicate = body[:-1] + "," + json.dumps(field) + ":" + json.dumps(value) + "}"
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_invalid$"):
        decode_identity(duplicate)


def test_named_envelope_requires_exact_order_spacing_and_unicode_encoding():
    body = encode_identity(pin())
    values = json.loads(body)
    variations = [
        json.dumps(dict(reversed(list(values.items()))), separators=(",", ":")),
        json.dumps(values),
        json.dumps(values, ensure_ascii=False, separators=(",", ":")),
    ]
    for changed in variations:
        assert changed != body
        with pytest.raises(AzureIdentityRefused, match="^azure_identity_invalid$"):
            decode_identity(changed)


def test_success_reads_both_context_and_physical_twice_without_transaction_control():
    cursor = Cursor()
    expected = pin()
    assert read_expected_identity(cursor, expected) == expected
    assert len(cursor.queries) == 4
    assert cursor.queries[0] == cursor.queries[2]
    assert cursor.queries[1] == cursor.queries[3]
    assert all(query.startswith("SELECT ") for query in cursor.queries)
    assert "physical_database_name" not in cursor.queries[0]
    assert "physical_database_name" in cursor.queries[1]
    assert "WHERE database_id=DB_ID()" in cursor.queries[1]


@pytest.mark.parametrize("edition", [1, 2, 3, 4, 8, 6, 11, 0, -1])
def test_non_azure_editions_refuse_before_azure_only_catalog_reference(edition):
    cursor = Cursor([[context(edition=edition)]])
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_unsupported$"):
        read_expected_identity(cursor, pin())
    assert len(cursor.queries) == 1
    assert "physical_database_name" not in cursor.queries[0]


@pytest.mark.parametrize("autocommit", [True, None, 0, "False"])
def test_unverifiable_or_autocommit_context_refuses_before_sql(autocommit):
    cursor = Cursor(autocommit=autocommit)
    with pytest.raises(AzureIdentityUnavailable, match="transaction_required"):
        read_expected_identity(cursor, pin())
    assert cursor.queries == []


def test_missing_connection_context_refuses_before_sql():
    cursor = Cursor()
    del cursor.connection
    with pytest.raises(AzureIdentityUnavailable, match="transaction_required"):
        read_expected_identity(cursor, pin())
    assert cursor.queries == []


@pytest.mark.parametrize(
    "changes",
    [
        {"edition": True},
        {"edition": "5"},
        {"database_id": True},
        {"database_id": 0},
        {"state": 0},
        {"state": -1},
        {"state": True},
        {"depth": 0},
        {"depth": True},
        {"server": None},
        {"database": "\ud800"},
    ],
)
def test_incomplete_context_is_never_a_matching_pin(changes):
    with pytest.raises(AzureIdentityUnavailable, match="^azure_identity_unavailable$"):
        read_expected_identity(Cursor([[context(**changes)]]), pin())


@pytest.mark.parametrize("at", range(4))
@pytest.mark.parametrize("fault", ["absent", "duplicate", "short", "permission"])
def test_every_read_requires_exact_cardinality_and_fixed_storage_errors(at, fault):
    rows = [[context()], [physical()], [context()], [physical()]]
    if fault == "absent":
        rows[at] = []
    elif fault == "duplicate":
        rows[at] *= 2
    elif fault == "short":
        rows[at] = [(1,)]
    else:
        rows[at] = RuntimeError("PRIVATE-DATABASE-DETAIL")
    with pytest.raises(AzureIdentityUnavailable) as caught:
        read_expected_identity(Cursor(rows), pin())
    assert str(caught.value) == "azure_identity_unavailable"
    assert "PRIVATE" not in str(caught.value)
    if fault == "permission":
        assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "changes",
    [{"physical": None}, {"physical": ""}, {"physical": 3}, {"database_id": True}],
)
def test_physical_unknown_or_invalid_is_not_automatic_pin(changes):
    rows = [[context()], [physical(**changes)]]
    with pytest.raises(AzureIdentityUnavailable):
        read_expected_identity(Cursor(rows), pin())


@pytest.mark.parametrize("at", [0, 2])
@pytest.mark.parametrize("changes", [{"server": "other-server"}, {"database": "renamed-app"}])
def test_wrong_logical_scope_or_rename_refuses(at, changes):
    rows = [[context()], [physical()], [context()], [physical()]]
    rows[at] = [context(**changes)]
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_mismatch$"):
        read_expected_identity(Cursor(rows), pin())


@pytest.mark.parametrize("at", [1, 3])
@pytest.mark.parametrize(
    "changes",
    [
        {"physical": "restored-new-physical"},
        {"physical": "benign-scale-new-physical"},
        {"database_id": 9},
        {"database": "renamed-app"},
    ],
)
def test_same_name_replacement_scale_and_torn_context_refuse(at, changes):
    rows = [[context()], [physical()], [context()], [physical()]]
    rows[at] = [physical(**changes)]
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_mismatch$"):
        read_expected_identity(Cursor(rows), pin())


def test_database_id_change_even_with_matching_new_rows_refuses_stability():
    rows = [[context()], [physical()], [context(database_id=8)], [physical(database_id=8)]]
    with pytest.raises(AzureIdentityRefused, match="^azure_identity_mismatch$"):
        read_expected_identity(Cursor(rows), pin())


def test_transaction_depth_change_during_observation_refuses():
    rows = [[context()], [physical()], [context(depth=2)], [physical()]]
    with pytest.raises(AzureIdentityRefused, match="azure_identity_mismatch"):
        read_expected_identity(Cursor(rows), pin())


def test_preexisting_interruption_propagates_without_claiming_sql_cleanup():
    cursor = Cursor([[context()], KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        read_expected_identity(cursor, pin())
    assert len(cursor.queries) == 2
