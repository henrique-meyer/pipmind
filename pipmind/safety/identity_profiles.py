"""Closed native/Azure pin codecs; no discovery or administrative separation claim."""

import hashlib

from pipmind.safety import azure_identity_sql, incarnation

DatabaseIdentity = incarnation.DatabaseIncarnation | azure_identity_sql.AzureDatabaseIdentity


class IdentityProfileRefused(Exception):
    """A fixed invalid, mixed-profile or mismatched pin refusal."""


class IdentityProfileUnavailable(Exception):
    """The fixed reader could not establish the expected database identity."""


def encode_identity(value):
    """Validate an exact known DTO and preserve its existing canonical bytes."""
    try:
        if type(value) is incarnation.DatabaseIncarnation:
            return incarnation.encode_incarnation(value)
        if type(value) is azure_identity_sql.AzureDatabaseIdentity:
            return azure_identity_sql.encode_identity(value)
    except (
        incarnation.IncarnationRefused,
        azure_identity_sql.AzureIdentityRefused,
        AttributeError,
    ):
        pass
    raise IdentityProfileRefused("identity_profile_invalid") from None


def decode_identity(body):
    """Decode only the two existing, disjoint canonical representations."""
    if type(body) is not str:
        raise IdentityProfileRefused("identity_profile_invalid")
    try:
        if body.startswith("["):
            return incarnation.decode_incarnation(body)
        if body.startswith("{"):
            return azure_identity_sql.decode_identity(body)
    except (incarnation.IncarnationRefused, azure_identity_sql.AzureIdentityRefused):
        pass
    raise IdentityProfileRefused("identity_profile_invalid") from None


def identity_sha256(value):
    return hashlib.sha256(encode_identity(value).encode("utf-8")).hexdigest()


def same_database(left, right):
    """Conservative physical/location comparison, never administrative independence.

    Location case folding can only cause refusal of a claimed separation. Actual
    pins and SQL-reader matching always retain their exact identifiers. Different
    identity profiles cannot establish separation from each other.
    """
    encode_identity(left)
    encode_identity(right)
    if type(left) is not type(right):
        raise IdentityProfileRefused("identity_profile_mixed")
    if type(left) is incarnation.DatabaseIncarnation:
        return incarnation.same_database(left, right)
    return (left.logical_server.casefold(), left.database.casefold()) == (
        right.logical_server.casefold(),
        right.database.casefold(),
    ) or left.physical_database_name == right.physical_database_name


def read_expected_identity(cursor, expected):
    """Dispatch only fixed readers on this cursor, with no adoption or repinning.

    This observation does not protect a caller's work on its own: bracket that
    work on the same transaction cursor, and require an acknowledged commit.
    Native callers with existing local reader aliases may retain those paths.
    """
    encode_identity(expected)
    try:
        if type(expected) is incarnation.DatabaseIncarnation:
            actual = incarnation.read_incarnation(cursor)
        else:
            actual = azure_identity_sql.read_expected_identity(cursor, expected)
        if actual != expected:
            raise IdentityProfileRefused("identity_profile_mismatch")
        return actual
    except (incarnation.IncarnationRefused, azure_identity_sql.AzureIdentityRefused):
        raise IdentityProfileRefused("identity_profile_mismatch") from None
    except (incarnation.IncarnationUnavailable, azure_identity_sql.AzureIdentityUnavailable):
        raise IdentityProfileUnavailable("identity_profile_unavailable") from None
