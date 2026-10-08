"""Public facade, isolation, dependency, and namespace relocation contracts."""

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from analytics_toolkit.datalens_utils import (
    DataLensConfigurationError,
    DataLensDependencyError,
    DataLensProject,
    Deployment,
    ProjectPaths,
)
from analytics_toolkit.datalens_utils.session import current_session
from analytics_toolkit.datalens_utils.settings import read_config

from tests._support.paths import REPO_ROOT

PACKAGE = REPO_ROOT / "analytics_toolkit/datalens_utils"


def deployment():
    return Deployment(
        "offline-org",
        "Offline/Dashboards",
        "Test dashboard",
        "Offline connection",
        "offline-connection",
        "offline-profile",
    )


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def project(self, name, **kwargs):
        root = self.root / name
        (root / "configs").mkdir(parents=True)
        (root / "configs/runtime.json").write_text(
            json.dumps({"installation": "yc", "request_interval_seconds": 0})
        )
        (root / "configs/marker.json").write_text(json.dumps({"project": name}))
        return DataLensProject(root, self.root / (name + "-runtime"), deployment(), **kwargs)

    def test_constructor_does_not_read_recipe_or_create_runtime(self):
        project = DataLensProject(self.root / "absent", self.root / "runtime", deployment())
        assert not project.paths.project_root.exists()
        assert not project.paths.runtime_root.exists()

    def test_runtime_cannot_be_inside_recipe_even_through_symlink(self):
        root = self.root / "recipe"
        root.mkdir()
        (self.root / "alias").symlink_to(root, target_is_directory=True)
        with pytest.raises(DataLensConfigurationError):
            ProjectPaths(root, self.root / "alias/runtime")

    def test_nested_sessions_restore_roots_and_keep_counters_separate(self):
        left, right = self.project("left"), self.project("right")
        with left.session() as first:
            first.runtime["_request_count"] = 7
            with right.session() as second:
                assert read_config("marker.json") == {"project": "right"}
                second.runtime["_request_count"] = 2
            assert current_session() is first
            assert first.runtime["_request_count"] == 7
            assert read_config("marker.json") == {"project": "left"}
        with pytest.raises(DataLensConfigurationError):
            current_session()

    def test_concurrent_projects_do_not_share_active_configuration(self):
        projects = [self.project("left"), self.project("right")]

        def work(project):
            with project.session() as state:
                for count in range(30):
                    assert read_config("marker.json")["project"] == project.paths.project_root.name
                    state.runtime["_request_count"] = count
                return read_config("marker.json")["project"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(work, projects)) == ["left", "right"]

    def test_staged_validation_restores_session_after_exception(self):
        project = self.project("live")
        stage = self.project("stage")
        with project.session() as original:
            with pytest.raises(RuntimeError, match="intentional"), original.staged(  # noqa: PT012
                stage.paths.project_root
            ):
                assert read_config("marker.json") == {"project": "stage"}
                message = "intentional"
                raise RuntimeError(message)
            assert current_session() is original

    def test_invalid_pacing_rejects_nan_and_boolean_values(self):
        project = self.project("recipe")
        for value in (float("nan"), float("inf"), True, -1, 61):
            (project.paths.project_root / "configs/runtime.json").write_text(
                json.dumps({"installation": "yc", "request_interval_seconds": value})
            )
            with self.subTest(value=value), pytest.raises(
                DataLensConfigurationError
            ), project.session():
                pass

    def test_missing_sdk_has_actionable_optional_extra_error(self):
        from importlib.metadata import PackageNotFoundError  # noqa: PLC0415

        project = self.project("recipe")
        with patch(
            "analytics_toolkit.datalens_utils.project.version", side_effect=PackageNotFoundError
        ), pytest.raises(DataLensDependencyError, match=r"analytics-toolkit\[datalens\]"):
            project.validate()
        assert not project.paths.runtime_root.exists()

    def test_unsupported_python_keeps_base_import_available(self):
        project = self.project("recipe")
        with patch(
            "analytics_toolkit.datalens_utils.project.sys.version_info", (3, 8)
        ), pytest.raises(DataLensDependencyError, match="3\\.10"):
            project.status()

    def test_sdk_error_type_survives_client_boundary(self):
        from datalens_sdk import APIErrorContext, NotFoundError  # noqa: PLC0415

        error = NotFoundError(APIErrorContext(404, "NOT_FOUND", "offline missing resource"))

        @contextmanager
        def factory(state):
            raise error
            yield

        project = self.project("recipe", client_factory=factory)
        with project.session():
            from analytics_toolkit.datalens_utils.auth.client import (  # noqa: PLC0415 - Scoped adapter.
                datalens_client,
            )

            with pytest.raises(NotFoundError) as caught, datalens_client():
                pass
        assert caught.value is error

    def test_request_counter_stays_with_its_client_during_nested_sessions(self):
        from unittest.mock import MagicMock  # noqa: PLC0415

        from analytics_toolkit.datalens_utils.auth import client as auth_client  # noqa: PLC0415

        first, second = self.project("first"), self.project("second")
        with patch.object(
            auth_client, "extract_credentials", return_value="offline-test-token"
        ), patch.object(
            auth_client, "DataLensClientYC", return_value=MagicMock()
        ) as constructor, first.session() as left, auth_client.datalens_client():
            left_hook = constructor.call_args.kwargs["event_hooks"]["request"][0]
            with second.session() as right, auth_client.datalens_client():
                right_hook = constructor.call_args.kwargs["event_hooks"]["request"][0]
                left_hook(object())
                assert left.runtime["_request_count"] == 1
                assert right.runtime["_request_count"] == 0
                right_hook(object())
                assert right.runtime["_request_count"] == 1

    def test_namespace_relocation_works_without_original_package(self):
        root = self.root / "relocated"
        namespace = root / "analytics_toolkit"
        namespace.mkdir(parents=True)
        (namespace / "__init__.py").write_text("")
        shutil.copytree(
            PACKAGE, namespace / "datalens_utils", ignore=shutil.ignore_patterns("__pycache__")
        )
        code = """
import importlib.abc, sys
class BlockOriginal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'datalens_utils' or fullname.startswith('datalens_utils.'):
            raise AssertionError('absolute extraction namespace import: ' + fullname)
sys.meta_path.insert(0, BlockOriginal())
from analytics_toolkit.datalens_utils import DataLensProject, Deployment
from analytics_toolkit.datalens_utils.dashboard_generation.charts import ql, wizard, editor
from analytics_toolkit.datalens_utils.editing import commands
from analytics_toolkit.datalens_utils import engine
from analytics_toolkit.datalens_utils import bootstrap
from pathlib import Path
assert (Path(bootstrap.__file__).parent / 'bootstrap_assets/bootstrap.sh').is_file()
print('namespace relocation passed')
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    def test_facade_is_parseable_as_python_38(self):
        for name in (
            "__init__.py",
            "errors.py",
            "project.py",
            "session.py",
            "cli.py",
            "editing/cli.py",
        ):
            ast.parse((PACKAGE / name).read_text(), feature_version=(3, 8))

    def test_full_verifier_uses_declared_folders_without_a_create_checkpoint(self):
        from unittest.mock import Mock  # noqa: PLC0415

        from analytics_toolkit.datalens_utils.resources.store import ResourceStore  # noqa: PLC0415
        from analytics_toolkit.datalens_utils.validation import checks  # noqa: PLC0415

        project = self.project("recipe")
        folders = {
            scope: SimpleNamespace(key="Offline/" + scope)
            for scope in ("dash", "dataset", "widget")
        }
        entities = {
            scope: SimpleNamespace(id=scope, key=folders[scope].key + "/Example", workbook_id=None)
            for scope in folders
        }
        with project.session():
            resources = ResourceStore(folders["dash"], {}, entries=[])
            resources.set_folders(folders, entries=[])
            context = SimpleNamespace(resources=resources, client=Mock())
            with patch.object(checks, "dataset_issues", return_value=[]), patch.object(
                checks, "check_chart", return_value=[]
            ), patch.object(checks, "dashboard_issues", return_value=[]), patch.object(
                checks.recipes, "validate_dashboard_refs", return_value=[]
            ), patch.object(checks, "export_bundle"):
                checks.verify_and_export(
                    context=context,
                    dashboard=entities["dash"],
                    datasets={"sales": entities["dataset"]},
                    charts={"trend": entities["widget"]},
                    dataset_definitions={"sales": {"name": "Example"}},
                    chart_definitions={"trend": {"name": "Example"}},
                    tab_definitions={},
                    contents={},
                    description="",
                    hide_tabs=False,
                    refetch=False,
                )
            assert resources.state["resources"] == {}


if __name__ == "__main__":
    unittest.main()
