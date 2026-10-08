"""Project-scoped fixtures, without monkeypatching package module roots."""

import shutil
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from analytics_toolkit.datalens_utils import DataLensProject, Deployment
from analytics_toolkit.datalens_utils.session import ProjectPaths, bind_session, current_session

from tests._support.paths import REPO_ROOT


def recipe_root():
    return current_session().paths.project_root


@contextmanager
def project_root(root):
    with current_session().staged(root) as value:
        yield value


@contextmanager
def runtime_root(root):
    state = current_session()
    child = replace(
        state, paths=ProjectPaths(state.paths.project_root, root), runtime=dict(state.runtime)
    )
    with bind_session(child):
        yield child


class ProjectTestCase(unittest.TestCase):
    def run(self, result=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe = root / "recipe"
            shutil.copytree(REPO_ROOT / "tests/datalens_utils/_support/recipe", recipe)
            template = REPO_ROOT / ".agents/skills/datalens-dashboard/assets/project/dashboard.py"
            launcher = template.read_text(encoding="utf-8")
            values = {
                "ORGANIZATION_ID": "offline-org",
                "YC_PROFILE": "offline-profile",
                "TARGET_PATH": "Offline/Dashboards",
                "DASHBOARD_NAME": "Fixture dashboard",
                "CONNECTION_NAME": "Offline ClickHouse",
                "CONNECTION_ID": "offline-clickhouse",
            }
            for name, value in values.items():
                launcher = launcher.replace("@" + name + "@", repr(value))
            (recipe / "dashboard.py").write_text(launcher, encoding="utf-8")
            deployment = Deployment(
                *[
                    values[name]
                    for name in (
                        "ORGANIZATION_ID",
                        "TARGET_PATH",
                        "DASHBOARD_NAME",
                        "CONNECTION_NAME",
                        "CONNECTION_ID",
                        "YC_PROFILE",
                    )
                ]
            )
            project = DataLensProject(recipe, root / "runtime", deployment)
            with project.session(reporter=print):
                return super().run(result)
