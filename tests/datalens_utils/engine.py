"""Operation routing and report contracts without cloud or filesystem deployment."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from analytics_toolkit.datalens_utils import (
    DataLensConfigurationError,
    DataLensProject,
    engine,
    settings,
)
from analytics_toolkit.datalens_utils import dashboard_generation as builders
from analytics_toolkit.datalens_utils.editing import commands
from analytics_toolkit.datalens_utils.resources import context
from analytics_toolkit.datalens_utils.validation import checks


def test_facade_routes_operations_and_preserves_reports(session_state):
    project = DataLensProject(
        session_state.paths.project_root, session_state.paths.runtime_root, session_state.deployment
    )
    with patch.object(engine, "validate", return_value={"object_count": 1}), patch.object(
        engine, "reconcile", return_value={"dashboard_id": "offline"}
    ), patch.object(
        commands, "run", return_value={"resource_statuses": {"chart:x": "clean"}}
    ) as run:
        assert project.validate()["coverage"] == {"object_count": 1}
        assert project.reconcile()["dashboard_id"] == "offline"
        for call in (project.status, project.apply, project.pull, project.verify):
            report = call()
            assert report["request_count"] == 0
            assert report["elapsed_seconds"] >= 0
            assert report["messages"] == []
        assert run.call_args.args[0].full is True
        run.return_value = None
        assert project.status()["operation"] == "status"
    for call in (
        lambda: project.apply(chart_keys=["a"], ui=True),
        lambda: project.pull(branch="unknown"),
        lambda: project.verify(full=False),
    ):
        with pytest.raises(DataLensConfigurationError):
            call()
    with pytest.raises(DataLensConfigurationError, match="Set"), patch.object(
        settings.session(), "runtime", {}
    ):
        settings.validate_deployment(target_path="", connection_name="", connection_id="")


def test_reconcile_reports_verified_identity_and_checkpoint(session_state):
    dashboard = SimpleNamespace(id="offline-dashboard")
    datasets = {"role": SimpleNamespace(id="offline-dataset")}
    charts = {"trend": SimpleNamespace(id="offline-chart")}
    managed = SimpleNamespace(verified_datasets=datasets, verified_charts=charts)
    with patch.object(
        context, "dashboard_context", return_value=nullcontext(managed)
    ), patch.object(builders, "create_datasets", return_value=datasets), patch.object(
        builders, "create_tabs", return_value=[]
    ), patch.object(builders, "create_dashboard", return_value=dashboard), patch.object(
        builders, "create_charts", return_value=charts
    ), patch.object(builders, "populate_dashboard", return_value=dashboard), patch.object(
        checks, "verify_and_export", return_value=dashboard
    ), patch.object(checks, "record_configured_ids") as record, patch.object(
        commands, "remember_full"
    ) as remember:
        report = engine.reconcile()
    assert report["resource_ids"] == {
        "dashboard": "offline-dashboard",
        "dataset:role": "offline-dataset",
        "chart:trend": "offline-chart",
    }
    assert report["dashboard_url"] == "https://datalens.ru/offline-dashboard"
    record.assert_called_once_with(dashboard, datasets=datasets, charts=charts)
    remember.assert_called_once()
    assert session_state.messages[-1].endswith("offline-dashboard")


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Windows", "AMD64", "windows-amd64"),
        ("Other", "x86_64", None),
        ("Linux", "sparc", None),
        ("Windows", "arm64", None),
    ],
)
def test_platform_contract(system, machine, expected):
    with patch.object(settings.platform, "system", return_value=system), patch.object(
        settings.platform, "machine", return_value=machine
    ):
        if expected:
            assert settings.platform_key() == expected
        else:
            with pytest.raises(DataLensConfigurationError, match="Supported"):
                settings.platform_key()


def test_existing_yc_cli_and_incomplete_chart_diagnostics(session_state):
    with patch.object(settings, "which", return_value="/existing/yc"):
        assert str(settings.yc_binary()) == "/existing/yc"
    with patch.object(settings, "which", return_value=None), pytest.raises(
        DataLensConfigurationError, match="existing executable"
    ):
        settings.yc_binary()
    path = session_state.paths.project_root / "configs/DL objects/charts/incomplete.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(DataLensConfigurationError, match="Incomplete"):
        settings.read_chart_definitions()
