import json
import os
import sys
from pathlib import Path

import pytest

from pipmind import dataset
from pipmind.shadow import __main__ as cli
from pipmind.shadow import evaluations
from pipmind.shadow.common import ShadowError


def setup_cli(monkeypatch, output, evaluator):
    calls = []
    connection = object()
    model = object()
    case = {"case_id": "T01"}

    def connect():
        assert json.loads(output.read_text()) == {
            "partial": True,
            "status": "reserved",
            "results": [],
        }
        calls.append("connector")
        return connection

    def model_factory():
        assert calls == ["connector"] and output.is_file()
        calls.append("model_factory")
        return model

    async def evaluate(actual_connect, cases, actual_model, **kwargs):
        assert actual_connect is connection and actual_model is model and cases == [case]
        assert kwargs["repetitions"] == 3 and kwargs["manifest"] is None
        calls.append("evaluate")
        return await evaluator(kwargs["progress"])

    monkeypatch.setattr(
        sys,
        "argv",
        ["shadow", "evaluate", "--dataset", "unused", "--cases", "T01", "--output", str(output)],
    )
    monkeypatch.setattr(dataset, "load_cases", lambda _: [case])
    monkeypatch.setattr(evaluations, "evaluate", evaluate)
    monkeypatch.setattr(cli, "connector", connect)
    monkeypatch.setattr(cli.FoundryModel, "environment", model_factory)
    return calls


def no_temporary_files(output):
    assert list(output.parent.glob("." + output.name + ".*")) == []


def test_evaluation_cli_atomically_saves_multiple_checkpoints_and_final(
    monkeypatch, tmp_path, capsys
):
    output = tmp_path / "private" / "report.json"

    async def evaluate(progress):
        for count in (1, 2, 3):
            progress({"run_id": "synthetic", "results": list(range(count))})
            assert json.loads(output.read_text())["results"] == list(range(count))
            no_temporary_files(output)
        return {"run_id": "synthetic", "results": [0, 1, 2], "summary": {"episodes": 3}}

    calls = setup_cli(monkeypatch, output, evaluate)
    cli.main()
    assert calls == ["connector", "model_factory", "evaluate"]
    assert json.loads(output.read_text()) == {
        "run_id": "synthetic",
        "results": [0, 1, 2],
        "summary": {"episodes": 3},
    }
    assert json.loads(capsys.readouterr().out) == {"episodes": 3}
    no_temporary_files(output)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling_symlink", "public"])
def test_evaluation_cli_refuses_existing_or_public_output_before_sql(monkeypatch, tmp_path, kind):
    output = tmp_path / "report.json"
    target = tmp_path / "original.json"
    if kind == "file":
        output.write_text("earlier original")
    elif kind == "directory":
        output.mkdir()
    elif kind in {"symlink", "dangling_symlink"}:
        if kind == "symlink":
            target.write_text("earlier original")
        try:
            output.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation requires local OS permission")
    else:
        output = Path(cli.__file__).parent / "evaluation-report.json"

    async def forbidden(_):
        pytest.fail("refused output reached evaluator")

    calls = setup_cli(monkeypatch, output, forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1 and calls == []
    if kind == "file":
        assert output.read_text() == "earlier original"
    elif kind == "symlink":
        assert target.read_text() == "earlier original" and output.is_symlink()
    elif kind == "dangling_symlink":
        assert not target.exists() and output.is_symlink()
    elif kind == "public":
        assert not output.exists()


@pytest.mark.parametrize("error_type", [ShadowError, KeyboardInterrupt])
def test_evaluation_cli_failure_retains_latest_complete_partial_checkpoint(
    monkeypatch, tmp_path, capsys, error_type
):
    output = tmp_path / "report.json"

    async def evaluate(progress):
        progress({"results": ["first"]})
        progress({"results": ["first", "second"], "incomplete_episode": {"pause_verified": False}})
        raise error_type("synthetic interruption")

    setup_cli(monkeypatch, output, evaluate)
    with pytest.raises(SystemExit if error_type is ShadowError else KeyboardInterrupt):
        cli.main()
    assert json.loads(output.read_text()) == {
        "partial": True,
        "results": ["first", "second"],
        "incomplete_episode": {"pause_verified": False},
    }
    assert capsys.readouterr().out == ""
    no_temporary_files(output)


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_evaluation_cli_failed_checkpoint_keeps_previous_bytes_and_cleans_temp(
    monkeypatch, tmp_path, operation
):
    output = tmp_path / "report.json"

    async def evaluate(progress):
        progress({"results": ["first"]})
        original = output.read_bytes()

        def fail(*_):
            assert output.read_bytes() == original
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(cli.os, operation, fail)
        progress({"results": ["first", "second"]})
        pytest.fail("failed checkpoint did not abort")

    setup_cli(monkeypatch, output, evaluate)
    with pytest.raises(SystemExit):
        cli.main()
    assert json.loads(output.read_text()) == {"partial": True, "results": ["first"]}
    no_temporary_files(output)


@pytest.mark.parametrize("replacement", ["in_place", "different_file", "symlink"])
def test_evaluation_cli_never_replaces_a_report_changed_by_another_writer(
    monkeypatch, tmp_path, replacement
):
    output = tmp_path / "report.json"
    external = tmp_path / "external.json"
    external.write_text("external original")

    async def evaluate(progress):
        progress({"results": ["first"]})
        if replacement == "in_place":
            output.write_text("external original")
        elif replacement == "different_file":
            os.replace(external, output)
        else:
            output.unlink()
            try:
                output.symlink_to(external)
            except OSError:
                pytest.skip("symlink creation requires local OS permission")
        progress({"results": ["first", "second"]})
        pytest.fail("changed report was overwritten")

    setup_cli(monkeypatch, output, evaluate)
    with pytest.raises(SystemExit):
        cli.main()
    assert output.read_text() == "external original"
    if replacement == "symlink":
        assert output.is_symlink() and external.read_text() == "external original"
    no_temporary_files(output)


def test_evaluation_cli_initial_staging_failure_removes_reservation_before_sql(
    monkeypatch, tmp_path
):
    output = tmp_path / "report.json"

    async def forbidden(_):
        pytest.fail("reservation failure reached evaluator")

    calls = setup_cli(monkeypatch, output, forbidden)

    def fail(_):
        raise OSError("synthetic initial staging failure")

    monkeypatch.setattr(cli.os, "fsync", fail)
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == [] and not output.exists()
    no_temporary_files(output)


def test_evaluation_cli_first_publication_has_complete_bytes_and_exclusive_reservation(
    monkeypatch, tmp_path
):
    output = tmp_path / "report.json"
    link = os.link
    published = []

    def publish(source, destination):
        assert not output.exists()
        assert json.loads(Path(source).read_text()) == {
            "partial": True,
            "status": "reserved",
            "results": [],
        }
        link(source, destination)
        published.append(True)

    async def evaluate(progress):
        assert published == [True]
        progress({"results": ["first"]})
        return {"results": ["first"], "summary": {"episodes": 1}}

    setup_cli(monkeypatch, output, evaluate)
    monkeypatch.setattr(cli.os, "link", publish)
    cli.main()
    assert json.loads(output.read_text())["summary"] == {"episodes": 1}
    no_temporary_files(output)


@pytest.mark.parametrize("failure", ["unsupported_filesystem", "competing_reservation"])
def test_evaluation_cli_cannot_fallback_to_overwrite_when_initial_link_fails(
    monkeypatch, tmp_path, failure
):
    output = tmp_path / "report.json"
    link = os.link

    def fail(source, destination):
        if failure == "competing_reservation":
            output.write_text("other owner's original")
            link(source, destination)
        raise OSError("synthetic hard-link failure")

    async def forbidden(_):
        pytest.fail("failed reservation reached evaluator")

    calls = setup_cli(monkeypatch, output, forbidden)
    monkeypatch.setattr(cli.os, "link", fail)
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == []
    if failure == "competing_reservation":
        assert output.read_text() == "other owner's original"
    else:
        assert not output.exists()
    no_temporary_files(output)
