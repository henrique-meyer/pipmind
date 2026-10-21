from pipmind import contracts
from pipmind.contracts import ROOT, data_dir, private_output_allowed, reports_dir


def test_defaults_are_ignored_in_repo_roots(monkeypatch):
    monkeypatch.delenv("PIPMIND_DATA_DIR", raising=False)
    monkeypatch.delenv("PIPMIND_REPORTS_DIR", raising=False)
    assert data_dir() == ROOT / ".data"
    assert reports_dir() == ROOT / "evals" / "reports"
    assert private_output_allowed(ROOT / ".data" / "x")
    assert private_output_allowed(ROOT / "evals" / "reports" / "latest")
    assert not private_output_allowed(ROOT / "docs" / "leak.json")


def test_configured_roots_outside_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPMIND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PIPMIND_REPORTS_DIR", str(tmp_path / "reports"))
    assert data_dir() == (tmp_path / "data").resolve()
    assert reports_dir() == (tmp_path / "reports").resolve()
    assert private_output_allowed(tmp_path / "data" / "feed-replay")
    assert not private_output_allowed(ROOT / "pipmind" / "leak.json")


def test_blank_values_fall_back(monkeypatch):
    monkeypatch.setenv("PIPMIND_DATA_DIR", "  ")
    assert contracts.data_dir() == ROOT / ".data"
