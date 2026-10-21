"""Local receipts only: no SQL, network, SDK or provider inference."""

from copy import deepcopy

import pytest

from pipmind.contracts import ContractError, Contracts
from pipmind.shadow import evaluation_provenance as provenance
from pipmind.shadow.common import ShadowError, digest, encoded
from pipmind.shadow.evaluations import GRADER_VERSION
from pipmind.shadow.model import FixtureModel, FoundryModel


def cases():
    return [{"case_id": "T01", "input": "USD café"}, {"case_id": "T02", "input": "EUR"}]


def capture(selected=None, model=None, repetitions=3):
    return provenance.capture_provenance(
        cases() if selected is None else selected,
        FixtureModel({}) if model is None else model,
        repetitions=repetitions,
        grader_version=GRADER_VERSION,
    )


def foundry(**kwargs):
    return FoundryModel(
        "https://synthetic.example/api/projects/fixture",
        "triage",
        "decision",
        enabled=True,
        **kwargs,
    )


def test_hash_domains_order_and_content_are_computed_without_changing_cases():
    selected = cases()
    before = deepcopy(selected)
    facts = capture(selected)
    assert selected == before
    assert facts["selected_cases_sha256"] == digest(encoded(selected))
    assert facts["case_hash_domain"] == "canonical-single-case-list"
    assert facts["cases"] == [
        {"case_id": c["case_id"], "sha256": digest(encoded([c]))} for c in selected
    ]
    assert (
        capture(list(reversed(selected)))["selected_cases_sha256"] != facts["selected_cases_sha256"]
    )
    selected[0]["input"] += " changed"
    assert capture(selected)["cases"][0]["sha256"] != facts["cases"][0]["sha256"]
    assert facts["repetitions"] == 3


@pytest.mark.parametrize("repetitions", [True, 1, 6, "3", None])
def test_invalid_repetitions_refuse(repetitions):
    with pytest.raises(ShadowError, match="evaluation_provenance_input"):
        capture(repetitions=repetitions)


@pytest.mark.parametrize("selected", [[], {}, [{"case_id": True}], [{"case_id": ""}], cases() * 2])
def test_invalid_case_id_count_or_shape_refuses(selected):
    with pytest.raises(ShadowError, match="evaluation_provenance_input"):
        capture(selected)


def test_grader_claim_must_match_imported_runner_constant():
    with pytest.raises(ShadowError, match="evaluation_provenance_input"):
        provenance.capture_provenance(
            cases(), FixtureModel({}), repetitions=3, grader_version="other"
        )


def test_available_versions_and_budget_are_capabilities_not_active_policy():
    facts = capture()
    versions = facts["available_capabilities"]
    assert versions["normal_prompt_version"] == provenance.PROMPT_VERSION
    assert versions["repair_prompt_version"] == provenance.REPAIR_PROMPT_VERSION
    assert versions["context_version"] == provenance.CONTEXT_VERSION
    assert versions["working_policy_version"] == provenance.POLICY
    assert versions["working_budget"]["max_bytes"] == provenance.MAX_BYTES
    assert versions["working_codec_version"] == provenance.VERSION
    assert versions["usage_accounting_version"] == provenance.ACCOUNTING_VERSION
    assert facts["runner_mode"] == "fixture-working-state"
    assert "active_policy" not in facts and "memory_policy" not in facts


@pytest.mark.parametrize("build", ["development", "a" * 40, "B" * 64])
def test_build_is_unverified_environment_declaration(monkeypatch, build):
    monkeypatch.setenv("BUILD_SHA", build)
    assert capture()["declared_build"] == {
        "source": "BUILD_SHA_environment",
        "value": build,
        "status": "unverified",
    }


def test_absent_or_invalid_build_is_unknown_and_invalid_value_not_copied(monkeypatch):
    monkeypatch.delenv("BUILD_SHA", raising=False)
    assert capture()["declared_build"]["value"] is None
    monkeypatch.setenv("BUILD_SHA", "PRIVATE_INVALID_VALUE")
    facts = capture()
    assert facts["declared_build"]["status"] == "unknown"
    assert "PRIVATE_INVALID_VALUE" not in encoded(facts)


def test_exact_model_allowlist_hashes_only_safe_binding_and_excludes_secrets():
    model = foundry(managed_identity_client_id="PRIVATE_MI", observer=lambda *_: None)
    model.credential = "PRIVATE_CREDENTIAL"
    model.unrelated = "PRIVATE_OTHER"
    facts = capture(model=model)
    cfg = facts["configured_model"]
    assert cfg["kind"] == "foundry" and cfg["execution_mode"] == "configured-foundry-client"
    assert cfg["binding_sha256"] == digest(
        encoded(
            {"endpoint": model.endpoint, "triage_model": "triage", "decision_model": "decision"}
        )
    )
    assert cfg["timeout_seconds"] == 90 and cfg["factory_present"] is False
    assert cfg["provider_version_status"] == cfg["rates_status"] == "unverified"
    text = encoded(facts)
    assert all(value not in text for value in ("PRIVATE_MI", "PRIVATE_CREDENTIAL", "PRIVATE_OTHER"))
    assert model.endpoint not in text
    model.triage_model = "another-triage"
    assert capture(model=model)["configured_model"]["binding_sha256"] != cfg["binding_sha256"]


def test_factory_presence_and_fixture_are_explicit_not_live_provider_proof():
    factory = foundry(factory=lambda **_: None)
    assert capture(model=factory)["configured_model"]["execution_mode"] == "configured-factory"
    cfg = capture()["configured_model"]
    assert cfg["kind"] == "fixture" and cfg["execution_mode"] == "scripted"
    assert cfg["binding_sha256"] is None


def test_custom_models_and_subclasses_never_run_introspection_hooks():
    class Unknown:
        def __getattribute__(self, name):
            raise AssertionError("custom getter executed")

        def __repr__(self):
            raise AssertionError("repr executed")

    class CustomFoundry(FoundryModel, Unknown):
        pass

    for model in (Unknown(), object.__new__(CustomFoundry)):
        cfg = capture(model=model)["configured_model"]
        assert cfg["kind"] == cfg["execution_mode"] == "unknown"
        assert cfg["binding_sha256"] is None


def test_custom_binding_values_are_not_serialized_or_represented():
    class Secret:
        def __repr__(self):
            raise AssertionError("secret repr executed")

    model = foundry()
    model.endpoint = Secret()
    assert capture(model=model)["configured_model"]["kind"] == "unknown"


def test_fixed_artifacts_have_raw_and_crlf_normalized_hashes_without_contents(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(provenance, "SOURCE_ROOT", tmp_path)
    path = tmp_path / provenance.ARTIFACTS[0]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"first\r\nsecond\n")
    facts = capture()
    row = facts["artifacts"][0]
    assert row == {
        "path": provenance.ARTIFACTS[0],
        "status": "available",
        "size_bytes": 14,
        "raw_sha256": digest(b"first\r\nsecond\n"),
        "lf_normalized_sha256": digest(b"first\nsecond\n"),
    }
    assert all(item["status"] == "missing" for item in facts["artifacts"][1:])
    assert facts["artifact_scope"] == "observed-on-disk-not-loaded-bytecode"
    assert "first" not in encoded(facts) and str(tmp_path) not in encoded(facts)


def test_oversize_artifact_is_explicit_without_hash_or_size_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "SOURCE_ROOT", tmp_path)
    monkeypatch.setattr(provenance, "MAX_ARTIFACT_BYTES", 8)
    path = tmp_path / provenance.ARTIFACTS[0]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 9)
    row = capture()["artifacts"][0]
    assert row["status"] == "oversize"
    assert row["size_bytes"] is row["raw_sha256"] is row["lf_normalized_sha256"] is None


def test_artifact_symlink_is_unavailable_without_following(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "SOURCE_ROOT", tmp_path)
    source = tmp_path / provenance.ARTIFACTS[0]
    source.parent.mkdir(parents=True)
    target = tmp_path / "private"
    target.write_text("PRIVATE_TARGET")
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("Local filesystem does not permit unprivileged symlinks")
    row = capture()["artifacts"][0]
    assert row["status"] == "unavailable" and row["raw_sha256"] is None


def test_freeze_rejects_custom_containers_and_objects_before_hooks():
    class CustomDict(dict):
        def items(self):
            raise AssertionError("items hook executed")

        def __deepcopy__(self, memo):
            raise AssertionError("deepcopy hook executed")

    class CustomString(str):
        pass

    for value in (CustomDict(), {"nested": CustomDict()}, {CustomString("key"): 1}, object()):
        with pytest.raises(ShadowError, match="evaluation_provenance_json"):
            provenance.freeze_json(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_freeze_rejects_nonfinite_json(value):
    with pytest.raises(ShadowError, match="evaluation_provenance_json"):
        provenance.freeze_json(value)


def test_freeze_adds_no_smaller_case_byte_or_depth_limit():
    value = "x" * 2000001
    assert provenance.freeze_json(value) == value
    nested = 1
    for _ in range(80):
        nested = [nested]
    assert provenance.freeze_json(nested) == nested


def test_freeze_rejects_cycle_and_returns_independent_plain_snapshot():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ShadowError, match="evaluation_provenance_json"):
        provenance.freeze_json(cycle)
    original = {"cases": [1, None, False, 1.5, "café"]}
    frozen = provenance.freeze_json(original)
    original["cases"].append("later")
    assert frozen == {"cases": [1, None, False, 1.5, "café"]}


def test_schema_rejects_extra_claims_wrong_hashes_and_types():
    contracts = Contracts()
    for field, value in (
        ("verified", True),
        ("selected_cases_sha256", "bad"),
        ("repetitions", True),
    ):
        facts = capture()
        facts[field] = value
        with pytest.raises(ContractError):
            contracts.validate("evaluation-provenance", facts)
