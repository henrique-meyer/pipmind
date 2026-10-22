"""Scripted metadata protocol checks, not actual SQL/permission qualification."""

import hashlib
from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from pipmind.safety import command_catalog_sql as catalog

DATABASE = "synthetic_catalog_database"
PREFIX = "pm_safety_command_"
TABLES = ("accounts_v1", "attempts_v1", "intents_v1", "unknown_v1")
OBJECTS = [("dbo", PREFIX + name, "U", 201 + index) for index, name in enumerate(TABLES)]


def native_columns():
    """Literal native catalog facts from the isolated command DDL contract."""
    columns = []
    for suffix in TABLES:
        fields = [
            ("account_key", "char", 64, 0, 0, False, 1, "Latin1_General_100_BIN2"),
        ]
        if suffix == "accounts_v1":
            fields += [
                ("account_id", "nvarchar", 256, 0, 0, False, 0, "Latin1_General_100_BIN2"),
                ("environment", "varchar", 4, 0, 0, False, 0, "Latin1_General_100_BIN2"),
                ("currency", "char", 3, 0, 0, False, 0, "Latin1_General_100_BIN2"),
            ]
        else:
            fields += [
                ("command_id", "nvarchar", 256, 0, 0, False, 2, "Latin1_General_100_BIN2"),
                ("body", "nvarchar", -1, 0, 0, False, 0, "Latin1_General_100_BIN2"),
                ("sha256", "char", 64, 0, 0, False, 0, "Latin1_General_100_BIN2"),
            ]
        columns += [("dbo", PREFIX + suffix, index, *row) for index, row in enumerate(fields, 1)]
    return columns


def proof():
    # SQL bit columns are returned as Boolean; HAS_PERMS_BY_NAME returns int.
    return [
        7,
        DATABASE,
        5,
        1,
        1,
        99,
        "P",
        "dbo",
        "pm_safety_command_catalog_v1",
        1,
        -2,
        True,
        True,
        catalog.MODULE_DEFINITION,
        0,
        1,
        1,
        *([0] * 11),
    ]


class Cursor:
    def __init__(self, *, absent=False):
        self.connection = SimpleNamespace(autocommit=False)
        self.before, self.after = proof(), proof()
        self.header = [(1, 7, DATABASE, 1, 1, 1)]
        self.objects = [] if absent else deepcopy(OBJECTS)
        self.columns = [] if absent else native_columns()
        self.executions, self.proofs = [], 0
        self.rowsets, self.position = [], 0
        self.extra_result = False
        self.broken_names = None

    @property
    def description(self):
        names = self.rowsets[self.position][0]
        if self.broken_names is not None:
            names = self.broken_names
        return [(name, None, None, None, None, None, None) for name in names]

    def execute(self, sql):
        self.executions.append(sql)
        if sql == catalog._PROOF_SQL:
            values = self.before if self.proofs == 0 else self.after
            self.proofs += 1
            self.rowsets = [(catalog._PROOF_NAMES, [tuple(values)])]
        elif sql == f"EXEC {catalog.MODULE_NAME}":
            self.rowsets = [
                (catalog._HEADER_NAMES, deepcopy(self.header)),
                (catalog._OBJECT_NAMES, deepcopy(self.objects)),
                (catalog._COLUMN_NAMES, deepcopy(self.columns)),
            ]
            if self.extra_result:
                self.rowsets.append((("unexpected",), [(1,)]))
        else:
            raise AssertionError("Unexpected statement; runtime must not execute DDL")
        self.position = 0

    def fetchmany(self, size):
        rows = self.rowsets[self.position][1]
        result, rows[:] = rows[:size], rows[size:]
        return result

    def nextset(self):
        if self.position + 1 < len(self.rowsets):
            self.position += 1
            return True
        return None


def read(cursor):
    return catalog.read_command_catalog_in_transaction(cursor, expected_database=DATABASE)


def test_supported_metadata_is_frozen_without_history_reads_or_cursor_owner_mutation():
    cursor = Cursor()
    original = deepcopy((cursor.before, cursor.after, cursor.objects, cursor.columns))
    result = read(cursor)
    assert result.classification == "supported" and len(result.objects) == 4
    assert len(result.columns) == 16
    assert result.database_name == DATABASE and result.database_id == 7
    assert result.module_object_id == 99 and result.caller_principal_id == 5
    assert result.module_definition_sha256 == catalog.MODULE_DEFINITION_SHA256
    assert result.dispatch_authorized is False
    assert result.objects[0] == catalog.CatalogObject("dbo", PREFIX + "accounts_v1", "U", 201)
    assert result.columns[0].primary_key_ordinal == 1
    assert type(result.columns[0].is_nullable) is bool
    assert original == (cursor.before, cursor.after, cursor.objects, cursor.columns)
    assert cursor.executions == [
        catalog._PROOF_SQL,
        f"EXEC {catalog.MODULE_NAME}",
        catalog._PROOF_SQL,
    ]
    assert cursor.connection.autocommit is False
    with pytest.raises(FrozenInstanceError):
        result.classification = "altered"
    with pytest.raises(FrozenInstanceError):
        result.objects[0].object_id = 999


def test_wholly_absent_is_explicit_with_module_owner_proof_before_and_after():
    cursor = Cursor(absent=True)
    result = read(cursor)
    assert result.classification == "absent" and result.objects == result.columns == ()
    assert cursor.proofs == 2 and result.caller_principal_id != 1


def test_bootstrap_returns_fixed_create_only_and_definition_digest_is_exact_utf16():
    sql = catalog.bootstrap_sql()
    assert sql is catalog.MODULE_DEFINITION
    assert sql.startswith(f"CREATE PROCEDURE {catalog.MODULE_NAME}\nWITH EXECUTE AS OWNER")
    assert hashlib.sha256(sql.encode("utf-16-le")).hexdigest() == catalog.MODULE_DEFINITION_SHA256
    assert "@" not in sql.replace("@@TRANCOUNT", "")
    assert "GRANT " not in sql and "EXEC(" not in sql and "EXECUTE(" not in sql
    assert "BEGIN TRAN" not in sql and "COMMIT" not in sql and "ROLLBACK" not in sql
    assert "INSERT " not in sql and "UPDATE " not in sql and "DELETE " not in sql
    assert "TOP (5)" in sql and "TOP (17)" in sql
    assert "NOT (s.name=N'dbo' AND o.name=N'pm_safety_command_catalog_v1')" in sql


@pytest.mark.parametrize("value", [None, False, 7, "", "bad\x00name", "\ud800", "a" * 129])
def test_invalid_explicit_scope_refuses_before_any_database_action(value):
    cursor = Cursor()
    with pytest.raises(catalog.CommandCatalogRefused, match="^invalid_command_catalog_request$"):
        catalog.read_command_catalog_in_transaction(cursor, expected_database=value)
    assert cursor.executions == []


@pytest.mark.parametrize(
    "index,value",
    [
        (0, True),
        (0, 0),
        (1, "wrong database"),
        (2, 1),
        (2, True),
        (3, 0),
        (3, True),
        (4, -1),
        (5, 0),
        (6, "V"),
        (7, "other"),
        (8, "renamed"),
        (9, 5),
        (9, True),
        (10, None),
        (10, 1),
        (11, False),
        (12, False),
        (13, None),
        (13, catalog.MODULE_DEFINITION + " "),
        (14, 1),
        (15, 0),
        (16, 0),
        (17, 1),
        (27, None),
    ],
)
def test_caller_owner_definition_settings_permission_and_scope_negatives_prevent_execution(
    index, value
):
    cursor = Cursor()
    cursor.before[index] = value
    with pytest.raises(catalog.CommandCatalogUnavailable, match="^command_catalog_unavailable$"):
        read(cursor)
    assert cursor.executions == [catalog._PROOF_SQL]


@pytest.mark.parametrize("index,value", [(0, 8), (2, 6), (3, 2), (5, 100)])
def test_otherwise_valid_postproof_drift_cannot_return_a_catalog(index, value):
    cursor = Cursor()
    cursor.after[index] = value
    with pytest.raises(catalog.CommandCatalogUnavailable, match="^command_catalog_unavailable$"):
        read(cursor)
    assert cursor.proofs == 2


@pytest.mark.parametrize("index,value", [(0, True), (1, 8), (2, "other"), (3, 5), (4, 2), (5, 0)])
def test_owner_module_header_must_match_current_database_and_transaction(index, value):
    cursor = Cursor()
    changed = list(cursor.header[0])
    changed[index] = value
    cursor.header = [tuple(changed)]
    with pytest.raises(catalog.CommandCatalogUnavailable):
        read(cursor)


@pytest.mark.parametrize(
    "variant", ["partial", "future", "other-schema", "view", "duplicates", "overflow"]
)
def test_unknown_or_partial_object_shape_never_becomes_absence_or_a_supported_prefix(variant):
    cursor = Cursor()
    if variant == "partial":
        cursor.objects.pop()
    elif variant == "future":
        cursor.objects[-1] = ("dbo", "pm_safety_command_unknown_v2", "U", 204)
    elif variant == "other-schema":
        cursor.objects[-1] = ("other", PREFIX + "unknown_v1", "U", 204)
    elif variant == "view":
        cursor.objects[-1] = ("dbo", PREFIX + "unknown_v1", "V", 204)
    elif variant == "duplicates":
        cursor.objects[-1] = cursor.objects[0]
    else:
        cursor.objects.append(("dbo", "pm_safety_command_future_v2", "U", 205))
    with pytest.raises(catalog.CommandCatalogUnavailable):
        read(cursor)


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "other"),
        (1, "wrong"),
        (2, True),
        (3, "renamed"),
        (4, "varchar"),
        (5, 32),
        (6, 8),
        (7, 2),
        (8, True),
        (9, 0),
        (10, "SQL_Latin1_General_CP1_CI_AS"),
    ],
)
def test_column_identity_type_native_precision_nullability_key_and_collation_are_bound(
    index, value
):
    cursor = Cursor()
    changed = list(cursor.columns[0])
    changed[index] = value
    cursor.columns[0] = tuple(changed)
    with pytest.raises(catalog.CommandCatalogUnavailable):
        read(cursor)


@pytest.mark.parametrize("variant", ["missing", "overflow", "orphan", "extra-set", "names"])
def test_protocol_does_not_accept_truncated_extra_or_mislabeled_metadata(variant):
    cursor = Cursor()
    if variant == "missing":
        cursor.columns.pop()
    elif variant == "overflow":
        cursor.columns.append(cursor.columns[-1])
    elif variant == "orphan":
        cursor.objects.clear()
    elif variant == "extra-set":
        cursor.extra_result = True
    else:
        cursor.broken_names = ("unexpected",)
    with pytest.raises(catalog.CommandCatalogUnavailable):
        read(cursor)


@pytest.mark.parametrize("value", [True, None, 0])
def test_nontransactional_connector_configuration_fails_before_metadata_read(value):
    cursor = Cursor()
    cursor.connection.autocommit = value
    with pytest.raises(catalog.CommandCatalogUnavailable):
        read(cursor)
    assert cursor.executions == []


def test_unknown_driver_errors_are_fixed_without_private_text_or_chain():
    cursor = Cursor()

    def fail(sql):
        raise RuntimeError("PRIVATE-CONNECTION-OR-CREDENTIAL")

    cursor.execute = fail
    with pytest.raises(catalog.CommandCatalogUnavailable) as caught:
        read(cursor)
    assert str(caught.value) == "command_catalog_unavailable"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_interruption_is_not_misreported_as_a_completed_catalog_or_hidden_as_storage_failure():
    cursor = Cursor()

    def interrupt(sql):
        raise KeyboardInterrupt

    cursor.execute = interrupt
    with pytest.raises(KeyboardInterrupt):
        read(cursor)
