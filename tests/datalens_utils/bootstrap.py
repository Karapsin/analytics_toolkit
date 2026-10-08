"""Bootstrap owns tools explicitly and preserves the caller's project boundary."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from analytics_toolkit.datalens_utils import DataLensConfigurationError, bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "Example project"
        self.project.mkdir()
        (self.project / ".python-version").write_text("3.12.13\n")
        self.runtime = self.root / "external-runtime"

    def test_default_and_override_runtime_are_external(self):
        with patch.dict(os.environ, {}, clear=True):
            assert (
                bootstrap.default_runtime_root(self.project) == self.root / ".local/Exampleproject"
            )
        with patch.dict(os.environ, {"DATALENS_RUNTIME_DIR": str(self.runtime)}):
            assert bootstrap.default_runtime_root(self.project) == self.runtime

    def test_invalid_runtime_stops_before_setup(self):
        with patch.object(bootstrap.subprocess, "run") as run, pytest.raises(
            DataLensConfigurationError
        ):
            bootstrap.ensure_environment(self.project, self.project / "runtime")
        run.assert_not_called()

    def test_missing_runtime_uses_bundled_script_and_preserves_arguments(self):
        for native, platform in (("linux-amd64", "linux"), ("windows-amd64", "win32")):
            with self.subTest(platform=platform), patch.object(
                bootstrap, "platform_key", return_value=native
            ), patch.object(bootstrap.sys, "platform", platform), patch.dict(
                os.environ, {"VIRTUAL_ENV": "/unrelated/environment"}
            ), patch.object(
                bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run:
                with pytest.raises(SystemExit) as completed:
                    bootstrap.ensure_environment(
                        self.project,
                        self.runtime,
                        argv=["status"],
                        organization_id="offline-org",
                        yc_profile="offline-profile",
                    )
                assert completed.value.code == 0
                setup, relaunch = run.call_args_list
                assert setup.args[0][-1] == str(self.project)
                assert any("bootstrap_assets" in arg for arg in setup.args[0])
                assert setup.kwargs["cwd"] == self.project
                environment = setup.kwargs["env"]
                assert environment["DATALENS_RUNTIME_DIR"] == str(self.runtime)
                assert environment["DATALENS_BOOTSTRAP_MODULE"] == bootstrap.__name__
                assert environment["DATALENS_YC_PROFILE"] == "offline-profile"
                assert "VIRTUAL_ENV" not in environment
                assert relaunch.args[0][-2:] == [str(self.project / "dashboard.py"), "status"]
                assert not self.runtime.exists()

    def test_ready_managed_environment_does_not_install_or_relaunch(self):
        native = "linux-amd64"
        python = self.runtime / ".venv/bin/python"
        yc = self.runtime / ".tools" / native / "bin/yc"
        for path in (python, yc):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        with patch.object(bootstrap, "platform_key", return_value=native), patch.object(
            bootstrap.sys, "platform", "linux"
        ), patch.object(bootstrap.sys, "prefix", str(self.runtime / ".venv")), patch.object(
            bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=0)
        ) as run:
            assert bootstrap.ensure_environment(self.project, self.runtime) == python
        assert run.call_count == 1
        assert "3.12.13" in run.call_args.args[0]

    def test_bootstrap_import_has_no_sdk_or_setup_side_effect(self):
        code = """
import importlib.abc, sys
class BlockSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'datalens_sdk' or fullname.startswith('datalens_sdk.'):
            raise AssertionError('bootstrap imported SDK')
sys.meta_path.insert(0, BlockSDK())
import analytics_toolkit.datalens_utils.bootstrap
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    unittest.main()


def test_bootstrap_failure_and_invalid_managed_probe(tmp_path):
    project = tmp_path / "recipe"
    project.mkdir()
    (project / ".python-version").write_text("3.12.13")
    runtime = tmp_path / "runtime"
    with patch.object(bootstrap, "platform_key", return_value="linux-amd64"), patch.object(
        bootstrap.subprocess, "run", return_value=SimpleNamespace(returncode=7)
    ), pytest.raises(SystemExit) as caught:
        bootstrap.ensure_environment(project, runtime)
    assert caught.value.code == 7
    python = runtime / ".venv/bin/python"
    yc = runtime / ".tools/linux-amd64/bin/yc"
    for path in (python, yc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    with patch.object(bootstrap, "platform_key", return_value="linux-amd64"), patch.object(
        bootstrap.subprocess,
        "run",
        side_effect=[OSError("probe unavailable"), SimpleNamespace(returncode=7)],
    ), pytest.raises(SystemExit) as caught:
        bootstrap.ensure_environment(project, runtime)
    assert caught.value.code == 7


@pytest.mark.skipif(sys.version_info < (3, 10), reason="Optional SDK requires Python 3.10+")
@pytest.mark.parametrize("outcome", ["missing", "windows", "not ready", "ready"])
def test_environment_checks_only_mocked_preflight(tmp_path, outcome, capsys):
    project = tmp_path / "recipe"
    project.mkdir()
    (project / "example.py").write_text("x = 1")
    (project / "__pycache__").mkdir()
    (project / "__pycache__/invalid.py").write_text("not valid python!")
    runtime = tmp_path / "runtime"
    binary = runtime / "yc"
    if outcome != "missing":
        runtime.mkdir()
        binary.touch()
    stdout = "---PREFLIGHT---\nSTATUS=ready\n" if outcome == "ready" else "not ready"
    with patch.object(bootstrap, "managed_yc_binary", return_value=binary), patch.object(
        bootstrap.sys, "platform", "win32" if outcome == "windows" else "linux"
    ), patch.object(
        bootstrap.subprocess, "run", return_value=SimpleNamespace(stdout=stdout)
    ) as run:
        if outcome in {"missing", "not ready"}:
            with pytest.raises(DataLensConfigurationError):
                bootstrap.check_environment(project, runtime)
        else:
            bootstrap.check_environment(project, runtime)
            assert "Python" in capsys.readouterr().out
    if outcome in {"missing", "windows"}:
        run.assert_not_called()
    else:
        assert run.call_args.kwargs["env"]["PYTHON"] == sys.executable
