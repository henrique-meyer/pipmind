import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipmind.shadow import __main__ as cli
from pipmind.shadow import stream_bundle
from pipmind.shadow.common import ShadowError


def setup_cli(monkeypatch, output, *, fits=True):
    store = SimpleNamespace(trader="synthetic-size-source")
    calls = []
    report = {
        "inspection": "size_only",
        "complete_scan": True,
        "size_limits_met": fits,
        "validity_verified": False,
        "export_created": False,
        "clone_verified": False,
        "records": 3,
    }

    def inspect(actual):
        assert actual is store
        calls.append("inspect")
        return report

    monkeypatch.setattr(sys, "argv", ["shadow", "export-size", "--output", str(output)])
    monkeypatch.setenv("PIPMIND_SHADOW_TRADER", store.trader)
    monkeypatch.setattr(cli, "connector", lambda: calls.append("connector"))
    monkeypatch.setattr(cli, "SqlMind", lambda *_: store)
    monkeypatch.setattr(stream_bundle, "inspect_experience_size", inspect)
    monkeypatch.setattr(
        cli.FoundryModel, "environment", lambda: pytest.fail("size inspection invoked a model")
    )
    return calls, report


def test_export_size_cli_saves_private_report_without_model_or_export(
    monkeypatch, tmp_path, capsys
):
    output = tmp_path / "private" / "size.json"
    calls, report = setup_cli(monkeypatch, output)
    cli.main()
    assert calls == ["connector", "inspect"]
    assert json.loads(output.read_text()) == report
    summary = json.loads(capsys.readouterr().out)
    assert summary["complete_scan"] and summary["size_limits_met"] and summary["report_saved"]
    assert not summary["validity_verified"] and not summary["export_created"]
    assert "trader" not in summary and "checkpoint" not in summary


def test_export_size_cli_retains_oversize_report_and_exits_two(monkeypatch, tmp_path, capsys):
    output = tmp_path / "size.json"
    _, report = setup_cli(monkeypatch, output, fits=False)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert json.loads(output.read_text()) == report
    assert json.loads(capsys.readouterr().out)["size_limits_met"] is False


@pytest.mark.parametrize("existing", [False, True])
def test_export_size_cli_refuses_bad_destination_before_sql(monkeypatch, tmp_path, existing):
    output = tmp_path / "existing.json" if existing else Path(cli.__file__).parent / "size.json"
    calls, _ = setup_cli(monkeypatch, output)
    if existing:
        output.write_text("owner's earlier report")
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1 and calls == []
    if existing:
        assert output.read_text() == "owner's earlier report"
    else:
        assert not output.exists()


def test_export_size_cli_interruption_cannot_publish_a_completed_report(
    monkeypatch, tmp_path, capsys
):
    output = tmp_path / "size.json"
    setup_cli(monkeypatch, output)

    def interrupted(_):
        raise ShadowError("synthetic_interruption")

    monkeypatch.setattr(stream_bundle, "inspect_experience_size", interrupted)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert not output.exists() and capsys.readouterr().out == ""
