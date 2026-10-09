"""Keep the dependency-free facade testable on every supported interpreter."""

import importlib.util
import json
import shutil
import sys

import pytest
from analytics_toolkit.datalens_utils import Deployment, ProjectPaths
from analytics_toolkit.datalens_utils.session import Session, bind_session

from tests._support.paths import REPO_ROOT

collect_ignore = []
if sys.version_info < (3, 10) or importlib.util.find_spec("datalens_sdk") is None:
    collect_ignore = [
        "project.py",
        "configuration.py",
        "editing",
        "editor_adapter.py",
        "exports.py",
        "gallery.py",
        "ql_adapter.py",
        "auth",
        "resources",
        "validation",
        "dashboard_generation",
        "engine.py",
        "bi",
    ]


@pytest.fixture
def session_state(tmp_path):
    root = tmp_path / "recipe"
    shutil.copytree(REPO_ROOT / "tests/datalens_utils/_support/recipe", root)
    (root / "configs/runtime.json").write_text(
        json.dumps({"installation": "yc", "request_interval_seconds": 0})
    )
    state = Session.load(
        ProjectPaths(root, tmp_path / "runtime"),
        Deployment("offline", "Offline/Dashboards", "Dashboard", "CH", "conn", "profile"),
    )
    with bind_session(state):
        yield state


@pytest.fixture
def financial_case():
    from tests.datalens_utils._support.financial import FinancialCase  # noqa: PLC0415

    case = FinancialCase()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()
