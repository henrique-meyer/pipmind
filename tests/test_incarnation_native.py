"""Native SQL catalog GUID formatting, independent of operational credentials."""

import pytest

from pipmind.safety.incarnation import (
    DatabaseIncarnation,
    IncarnationRefused,
    read_incarnation,
)


def test_native_sql_uppercase_guids_normalize_without_relaxing_pin_codec():
    class Cursor:
        def execute(self, _):
            pass

        def fetchone(self):
            return (
                3,
                "synthetic-server",
                "synthetic-database",
                "AAAAAAAA-1111-4111-8111-111111111111",
                "BBBBBBBB-2222-4222-8222-222222222222",
            )

    pin = read_incarnation(Cursor())
    assert pin.database_guid == "aaaaaaaa-1111-4111-8111-111111111111"
    assert pin.recovery_fork_guid == "bbbbbbbb-2222-4222-8222-222222222222"
    with pytest.raises(IncarnationRefused):
        DatabaseIncarnation(
            pin.server, pin.database, pin.database_guid.upper(), pin.recovery_fork_guid
        )
