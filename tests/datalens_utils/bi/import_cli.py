"""Public import CLI forwards copy mode and exposes recoverable failures."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from analytics_toolkit.datalens_utils import DataLensComponentError
from analytics_toolkit.datalens_utils.cli import run_cli


def test_cli_forwards_explicit_copy_mode(capsys):
    call = Mock(return_value={"blockers": [], "written": False})
    project = SimpleNamespace(_import=call)
    assert (
        run_cli(
            project,
            [
                "import-tab",
                "--dashboard-id",
                "source",
                "--tab-id",
                "one",
                "--mode",
                "copy",
                "--json",
            ],
        )
        == 0
    )
    assert call.call_args.kwargs["mode"] == "copy"
    assert call.call_args.kwargs["dry_run"] is True
    assert json.loads(capsys.readouterr().out)["written"] is False


def test_json_cli_retains_component_failure_diagnostics(capsys):
    error = DataLensComponentError("dataset:sales", "create", "retained", [{"code": "duplicate"}])
    project = SimpleNamespace(_execute=Mock(side_effect=error))
    assert run_cli(project, ["reconcile", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["diagnostics"] == error.diagnostics
    assert report["diagnostics"]["id"] == "retained"
