import io
import zipfile

import pytest

from pipmind.contracts import ROOT, loads
from pipmind.portability import BundleError, validate_bundle
from pipmind.portability_fixtures import fixture_archive, synthetic_files

FIXTURES = loads((ROOT / "evals" / "portability" / "cases.json").read_bytes())


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["case_id"])
def test_archive_contract_accepts_or_rejects_explicit_fixture(fixture, contracts):
    data = fixture_archive(fixture["operation"], fixture["kind"])
    if fixture["expected_code"] == "valid":
        bundle = validate_bundle(data, contracts)
        assert len(bundle.records) == 7
        assert bundle.manifest["kind"] == fixture["kind"]
    else:
        with pytest.raises(BundleError) as error:
            validate_bundle(data, contracts)
        assert str(error.value) == fixture["expected_code"]


@pytest.mark.parametrize("kind", ["experience", "recovery"])
def test_synthetic_round_trip_preserves_all_bytes_versions_and_override_flags(kind, contracts):
    source, _ = synthetic_files(kind)
    bundle = validate_bundle(fixture_archive(kind=kind), contracts)
    assert {path: body for path, body in bundle.files.items() if path != "manifest.json"} == source
    assert {row["part"] for row in bundle.records} == {
        "identity",
        "playbook",
        "working_memory",
        "journal",
        "review",
        "memory",
        "playbook_history",
    }
    assert any(row["override"] for row in bundle.records)
    assert any(row["rule_status"] == "retired" for row in bundle.records)
    assert any(row["rule_id"] and row["playbook_version"] for row in bundle.records)
    with pytest.raises(TypeError):
        bundle.files["mind/identity.md"] = b"changed"


def test_validation_never_extracts_or_executes_imported_prose(monkeypatch, contracts):
    def forbidden(*args, **kwargs):
        raise AssertionError("archive extraction is not part of validation")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    validate_bundle(fixture_archive(), contracts)
    with pytest.raises(BundleError, match="unsafe_path"):
        validate_bundle(fixture_archive("traversal"), contracts)


def test_inherited_record_provenance_is_not_reassigned_to_checkpoint_owner(contracts):
    bundle = validate_bundle(fixture_archive("inherited_provenance"), contracts)
    assert bundle.manifest["trader_id"] == "synthetic-trader"
    assert any(row["source_trader"] == "synthetic-ancestor" for row in bundle.records)


@pytest.mark.parametrize("content", [b"not a ZIP", b"PK", b"", b"x" * 2_097_153])
def test_invalid_or_oversized_containers_fail_closed(content, contracts):
    with pytest.raises(BundleError):
        validate_bundle(content, contracts)


def test_missing_operational_state_cannot_be_a_recovery_bundle(contracts):
    raw = fixture_archive(kind="recovery")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = loads(archive.read("manifest.json"))
    assert manifest["kind"] == "recovery"
    assert any(row["path"] == "recovery/state.json" for row in manifest["files"])
    with pytest.raises(BundleError):
        validate_bundle(raw[:-30], contracts)
