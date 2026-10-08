"""Dependency-free imports and the preinstalled dashboard environment contract."""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils import (
    DataLensConfigurationError,
    DataLensDependencyError,
    DataLensProject,
    Deployment,
    settings,
)
from analytics_toolkit.datalens_utils.cli import run_cli
from analytics_toolkit.datalens_utils.project import require_sdk
from analytics_toolkit.datalens_utils.session import ProjectPaths, Session, bind_session

from tests._support.paths import REPO_ROOT

SKILL = REPO_ROOT / ".agents/skills/datalens-dashboard"


def test_recipe_manifests_are_not_ignored_as_runtime_coverage_reports():
    manifests = [
        ".agents/skills/datalens-dashboard/assets/project/configs/coverage.json",
        "tests/datalens_utils/_support/recipe/configs/coverage.json",
        "tests/datalens_utils/_support/financial_recipe/configs/coverage.json",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "coverage.json", *manifests],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["coverage.json"]
    assert all((REPO_ROOT / path).is_file() for path in manifests)


def test_base_import_and_constructor_do_not_load_sdk(tmp_path):
    code = """
import importlib.abc, sys
class BlockSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'datalens_sdk' or fullname.startswith('datalens_sdk.'):
            raise AssertionError('unexpected SDK import')
sys.meta_path.insert(0, BlockSDK())
from analytics_toolkit.datalens_utils import DataLensProject, Deployment
identity = Deployment('org', 'Root/dash', 'Name', 'CH', 'id', 'profile')
p = DataLensProject('absent', 'external', identity)
assert not p.paths.project_root.exists()
assert not p.paths.runtime_root.exists()
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_facade_parses_on_python38():
    for name in ("__init__.py", "project.py", "session.py", "errors.py", "cli.py"):
        ast.parse(
            (REPO_ROOT / "analytics_toolkit/datalens_utils" / name).read_text(),
            feature_version=(3, 8),
        )


def test_required_sdk_version_errors():
    with patch("analytics_toolkit.datalens_utils.project.sys.version_info", (3, 8)), pytest.raises(
        DataLensDependencyError, match=r"3\.10"
    ):
        require_sdk()
    with patch("analytics_toolkit.datalens_utils.project.sys.version_info", (3, 11)), patch(
        "analytics_toolkit.datalens_utils.project.version", return_value="3.0.0"
    ), pytest.raises(DataLensDependencyError, match=r"found 3\.0\.0"):
        require_sdk()


def test_runtime_settings_and_reporter(tmp_path):
    root = tmp_path / "recipe"
    (root / "configs").mkdir(parents=True)
    paths = ProjectPaths(root, tmp_path / "runtime")
    deployment = Deployment("org", "Root/dash", "Name", "CH", "id", "profile")
    (root / "configs/runtime.json").write_text(json.dumps({"installation": "bi"}))
    with pytest.raises(DataLensConfigurationError, match="YC"):
        Session.load(paths, deployment)
    (root / "configs/runtime.json").write_text(json.dumps({"installation": "yc"}))
    reporter = Mock()
    state = Session.load(paths, deployment, reporter=reporter)
    state.emit("hello", "world", sep="/", end="", flush=True)
    assert state.messages == ["hello/world"]
    reporter.assert_called_once_with("hello", "world", sep="/", end="", flush=True)
    assert deployment.as_dict()["yc_binary"] == "yc"
    with pytest.raises(DataLensConfigurationError, match="Deployment"):
        DataLensProject(root, tmp_path / "runtime", {})
    with bind_session(state):
        assert state.runtime.get("request_interval_seconds", 1.2) == 1.2


def test_scaffold_uses_existing_environment_without_setup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("scaffold", SKILL / "scripts/scaffold.py")
    scaffold = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scaffold)
    target = tmp_path / "recipe"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scaffold",
            str(target),
            "--table",
            "example.sales",
            "--organization-id",
            "offline",
            "--yc-profile",
            "profile",
            "--target-path",
            "Offline/Dashboards",
            "--connection-name",
            "CH",
            "--connection-id",
            "conn",
        ],
    )
    scaffold.main()
    forbidden = {".venv", ".python-version", "pyproject.toml", "uv.lock", ".tools"}
    assert not forbidden.intersection(p.name for p in target.iterdir())
    launcher = target / "dashboard.py"
    assert "@" not in launcher.read_text()
    spec = importlib.util.spec_from_file_location("dashboard", launcher)
    dashboard = importlib.util.module_from_spec(spec)
    with patch("analytics_toolkit.datalens_utils.bootstrap.ensure_environment") as bootstrap:
        spec.loader.exec_module(dashboard)
        project = dashboard.make_project()
        with patch.object(dashboard, "run_cli", return_value=0) as cli:
            assert dashboard.main(["validate"]) == 0
            cli.assert_called_once()
    bootstrap.assert_not_called()
    assert project.deployment.yc_binary == "yc"
    if sys.version_info >= (3, 10) and importlib.util.find_spec("datalens_sdk") is not None:
        with patch("analytics_toolkit.datalens_utils.auth.client.datalens_client") as client:
            assert project.validate()["coverage"]["object_count"] == 4
        client.assert_not_called()
    assert not project.paths.runtime_root.exists()
    monkeypatch.setenv("DATALENS_YC_BIN", "/existing/yc")
    assert dashboard.yc_binary() == "/existing/yc"
    with pytest.raises(SystemExit):
        scaffold.main()
    monkeypatch.setattr(sys, "argv", ["scaffold", str(tmp_path / "invalid"), "--table", "a; DROP"])
    with pytest.raises(SystemExit):
        scaffold.main()
    assert not (tmp_path / "invalid").exists()


def test_cli_reports_library_errors_and_validate_success(capsys):
    project = Mock()
    project._execute.side_effect = DataLensConfigurationError("invalid recipe")
    assert run_cli(project, ["validate"]) == 1
    assert capsys.readouterr().err == "invalid recipe\n"
    project._execute.side_effect = None
    assert run_cli(project, ["validate"]) == 0
    assert "no cloud requests" in capsys.readouterr().out


def test_cli_reconcile_defaults_and_runtime_boundary_defense(session_state):
    project = Mock()
    assert run_cli(project) == 0
    assert project._execute.call_args.args == ("reconcile",)
    with patch.object(
        session_state,
        "paths",
        Mock(project_root=Path("/recipe"), runtime_root=Path("/recipe/runtime")),
    ), pytest.raises(DataLensConfigurationError, match="outside"):
        settings.validate_deployment(target_path="Root", connection_name="CH", connection_id="conn")
