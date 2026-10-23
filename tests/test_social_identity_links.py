"""Bounded publisher JSON-LD association, never account/control qualification."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from pipmind.contracts import utc
from pipmind.feeds.common import MAX_BODY
from pipmind.feeds.fixtures import FakeReads, response
from pipmind.feeds.social import (
    MAX_IDENTITY_SCRIPTS,
    MAX_JSONLD_CONTAINERS,
    MAX_JSONLD_DEPTH,
    MAX_JSONLD_SCRIPT_BYTES,
    MAX_JSONLD_SCRIPTS,
    MAX_JSONLD_TOTAL_BYTES,
    MAX_SAME_AS_LINKS,
    Social,
    official_account,
    official_association,
    verified,
)
from pipmind.feeds.store import FeedStore

TARGET = "https://publisher.invalid/home"
HANDLE = "ExampleWire"
ROLE = {"official_profile_url": TARGET, "institutional_words": ["news"], "role_words": ["news"]}


def organization():
    return {
        "@context": "https://schema.org",
        "@type": "NewsMediaOrganization",
        "name": "Example Wire",
        "url": "https://publisher.invalid",
        "sameAs": "https://x.com/ExampleWire",
    }


def script(value):
    raw = value if isinstance(value, str) else json.dumps(value)
    return '<script type="application/ld+json">' + raw + "</script>"


def association(html, *, institutional=True, target=TARGET):
    reads = FakeReads([response(html)])
    value = official_association(
        reads, {**ROLE, "official_profile_url": target}, HANDLE, institutional=institutional
    )
    assert reads.calls == [target]  # No context/account/linked-resource fetch.
    return value


@pytest.mark.parametrize("kind", ["Organization", "NewsMediaOrganization"])
@pytest.mark.parametrize(
    "same_as",
    [
        "https://x.com/ExampleWire",
        "https://twitter.com/examplewire/",
        ["https://publisher.invalid/about", "https://www.x.com/ExampleWire"],
    ],
)
def test_top_level_publisher_org_exact_scalar_or_list_association(kind, same_as):
    value = {**organization(), "@type": kind, "sameAs": same_as}
    assert association(script(value)) == "official_organization_same_as"


@pytest.mark.parametrize(
    "context",
    [
        "https://schema.org",
        "https://schema.org/",
        "http://schema.org",
        "http://schema.org/",
    ],
)
def test_known_schema_contexts_are_inert_vocabulary_identifiers(context):
    assert association(script({**organization(), "@context": context}))


@pytest.mark.parametrize(
    "context",
    [
        "https://schema.org.attacker.invalid",
        "https://schema.org/remote-context.json",
        "http://attacker.invalid/context",
        "https://user@schema.org",
        "https://schema.org:8443",
        ["https://schema.org"],
        {"@vocab": "https://schema.org/"},
        None,
    ],
)
def test_other_contexts_cannot_supply_publisher_proof(context):
    assert association(script({**organization(), "@context": context})) is None


def test_default_boolean_contract_retains_anchors_and_requires_institutional_opt_in():
    body = script(organization())
    assert not official_account(FakeReads([response(body)]), ROLE, HANDLE)
    assert official_account(FakeReads([response(body)]), ROLE, HANDLE, institutional=True)
    assert not association(body, institutional=False)
    assert (
        association(
            '<a href="https://x.com/ExampleWire?tracking=old#legacy">Account</a>',
            institutional=False,
        )
        == "official_profile_link"
    )


@pytest.mark.parametrize(
    "tail",
    [
        script("invalid-json"),
        script({"@graph": [organization()]}),
        '<script type="application/ld+json">truncated',
    ],
)
def test_existing_anchor_proof_is_unchanged_by_malformed_or_unsupported_jsonld(tail):
    assert (
        association(
            '<a href="https://twitter.com/ExampleWire/">Account</a>' + tail, institutional=False
        )
        == "official_profile_link"
    )


@pytest.mark.parametrize(
    "link",
    [
        "http://x.com/ExampleWire",
        "https://x.com.attacker.invalid/ExampleWire",
        "https://x.com/ExampleWireFake",
        "https://x.com/ExampleWire/status/123",
        "https://x.com:8443/ExampleWire",
        "https://user@x.com/ExampleWire",
        "https://user:password@x.com/ExampleWire",
        "https://x.com//ExampleWire",
        "https://x.com/ExampleWire//",
        "https://x.com/ExampleWire?next=Other",
        "https://x.com/ExampleWire#Other",
        "https://x.com/ExampleWire\\Other",
        "https://x.com/ExampleWire\n",
        "https://x.com/%45xampleWire",
        "//x.com/ExampleWire",
        "https://x.com./ExampleWire",
        "https://xn--x-0ga.com/ExampleWire",
    ],
)
def test_structured_account_url_refuses_lookalikes_and_ambiguous_paths(link):
    assert association(script({**organization(), "sameAs": link})) is None


@pytest.mark.parametrize(
    "publisher",
    [
        "http://publisher.invalid",
        "https://other.invalid",
        "https://publisher.invalid.attacker.invalid",
        "https://user@publisher.invalid",
        "https://publisher.invalid:8443",
        "https://publisher.invalid/not-canonical",
        "https://publisher.invalid?other=1",
        "https://publisher.invalid/#other",
        None,
        {},
        ["https://publisher.invalid"],
    ],
)
def test_exact_official_publisher_origin_and_root_canonical_url_required(publisher):
    assert association(script({**organization(), "url": publisher})) is None


def test_equivalent_https_default_ports_and_root_slash_are_allowed():
    assert association(script({**organization(), "url": "https://publisher.invalid:443/"}))
    assert association(script(organization()), target="https://publisher.invalid:443/home")


@pytest.mark.parametrize(
    "body",
    [
        [organization()],
        {"@context": "https://schema.org", "@graph": [organization()]},
        {"@type": "WebPage", "publisher": organization()},
        {**organization(), "@type": "Person"},
        {**organization(), "@type": ["Organization", "Person"]},
        {**organization(), "@context": "https://attacker.invalid/context"},
        {**organization(), "@context": {"sameAs": "https://attacker.invalid/relation"}},
        {**organization(), "@graph": []},
        {**organization(), "sameAs": {"@id": "https://x.com/ExampleWire"}},
        {**organization(), "sameAs": ["https://x.com/ExampleWire", "https://x.com/OtherWire"]},
        {**organization(), "name": ""},
        {**organization(), "name": "x" * 121},
    ],
)
def test_only_unambiguous_direct_organization_proof_is_supported(body):
    assert association(script(body)) is None


def test_nested_author_same_as_cannot_nominate_an_institutional_account():
    body = organization()
    del body["sameAs"]
    body["author"] = {"@type": "Person", "sameAs": "https://x.com/ExampleWire"}
    assert association(script(body)) is None


def test_multiple_publisher_objects_are_ambiguous_and_other_top_level_types_are_ignored():
    assert association(script(organization()) + script(organization())) is None
    unrelated = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "url": "https://publisher.invalid",
    }
    assert (
        association(script(unrelated) + script(organization())) == "official_organization_same_as"
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"@type":"Person","@type":"Organization"}',
        json.dumps(organization())[:-1] + ',"sameAs":"https://x.com/ExampleWire"}',
        '{"broken":',
        "NaN",
        "",
        '{"nested":{"key":1,"key":2}}',
    ],
)
def test_malformed_or_duplicate_key_json_cannot_supply_structured_proof(raw):
    assert association(script(raw) + script(organization())) is None


@pytest.mark.parametrize(
    "body",
    [
        '<script src="https://publisher.invalid/identity.json" '
        'type="application/ld+json"></script>',
        '<script type="application/ld+json" type="application/ld+json">{}</script>',
        '<script type="application/ld+json">' + json.dumps(organization()),
        '<script type="application/ld+json"/>',
    ],
)
def test_remote_duplicate_attribute_or_truncated_scripts_are_refused(body):
    assert association(body) is None


def test_script_and_page_resource_bounds_are_explicit():
    assert association(" " * (MAX_BODY + 1)) is None
    oversized = {**organization(), "description": "x" * MAX_JSONLD_SCRIPT_BYTES}
    assert association(script(oversized)) is None
    too_many = script({}) * MAX_JSONLD_SCRIPTS + script(organization())
    assert association(too_many) is None
    too_many_any = "<script>void 0</script>" * MAX_IDENTITY_SCRIPTS + script(organization())
    assert association(too_many_any) is None
    piece = script({"@type": "WebSite", "description": "x" * 50000})
    assert MAX_JSONLD_SCRIPT_BYTES > 50000 and MAX_JSONLD_TOTAL_BYTES < 150000
    assert association(piece * 3 + script(organization())) is None


def test_json_depth_container_and_same_as_list_bounds_are_independent():
    assert (
        association(script("[" * (MAX_JSONLD_DEPTH + 1) + "0" + "]" * (MAX_JSONLD_DEPTH + 1)))
        is None
    )
    containers = {"ignored": [{} for _ in range(MAX_JSONLD_CONTAINERS)]}
    assert association(script(containers) + script(organization())) is None
    links = ["https://publisher.invalid/about"] * MAX_SAME_AS_LINKS + ["https://x.com/ExampleWire"]
    assert association(script({**organization(), "sameAs": links})) is None
    assert association(script({**organization(), "sameAs": ["x" * 4097]})) is None
    assert association(script({**organization(), "description": "{[" * 1000 + '\\"quoted'}))


@pytest.mark.parametrize(
    "identifier",
    [
        "https://thirdparty.invalid/#organization",
        "https://publisher.invalid/thirdparty#organization",
        "http://publisher.invalid/#organization",
        "https://user@publisher.invalid/#organization",
        "https://publisher.invalid:8443/#organization",
        {},
    ],
)
def test_conflicting_organization_id_cannot_disguise_a_third_party(identifier):
    assert association(script({**organization(), "@id": identifier})) is None


def test_local_organization_fragment_id_is_not_a_remote_graph_lookup():
    assert association(script({**organization(), "@id": "https://publisher.invalid/#organization"}))
    assert association(script({**organization(), "name": " "})) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("parody", True),
        ("parody", "false"),
        ("protected", True),
        ("verified", False),
        ("verified_type", "none"),
        ("description", "unrelated"),
    ],
)
def test_structured_association_never_overrides_existing_profile_refusals(key, value):
    user = {
        "id": "123",
        "username": HANDLE,
        "name": "Example Wire",
        "description": "news",
        "verified": True,
        "verified_type": "blue",
        "protected": False,
    }
    linked = bool(association(script(organization())))
    assert linked
    assert not verified(
        {**user, key: value}, None, ROLE, institutional=True, officially_linked=linked
    )


@pytest.mark.parametrize("kind,accepted", [("institutional", True), ("personal", False)])
def test_refresh_retains_precise_proof_type_and_institutional_scope(tmp_path, kind, accepted):
    role = {
        **deepcopy(ROLE),
        "id": "wire",
        "priority": 1,
        "holder": "Example Wire",
        "accounts": [{"handle": HANDLE, "kind": kind, "poll": "full_timeline"}],
    }
    user = {
        "id": "123",
        "username": HANDLE,
        "name": "Example Wire",
        "description": "news",
        "verified": True,
        "verified_type": "blue",
        "protected": False,
    }
    store = FeedStore(tmp_path / "social.sqlite3")
    reads, x = FakeReads([response(script(organization()))]), Mock()
    x.users.return_value = [user]
    social = Social(store, reads, x, [role], None, lambda: utc("2026-10-23T16:00:00Z"))
    assert social.refresh(role)
    account = store.state("role:wire")["accounts"][0]
    assert account["unverified"] is not accepted
    if accepted:
        assert account["identity_evidence"] == "official_organization_same_as"
        assert account["user_id"] == "123"
    else:
        assert "user_id" not in account
    assert reads.calls == [TARGET]
    x.users.assert_called_once_with([HANDLE])
    x.timeline.assert_not_called()
    assert store.events() == []
