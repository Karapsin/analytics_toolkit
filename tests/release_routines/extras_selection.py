from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from release_routines.lib import python_support

from tests._support import extras
from tests._support.paths import REPO_ROOT

if TYPE_CHECKING:
    from pathlib import Path

TUI_MODULES = (
    "textual",
    "rich",
    "tree_sitter",
    "tree_sitter_languages",
    "tree_sitter_sql",
    "pyperclip",
)
BLOCKED_COLLECTION = """
import importlib.abc, sys
blocked = set(sys.argv[1].split(","))
class Missing(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Missing())
import pytest
sys.exit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", "tests"]))
"""


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_transitive_extra_imports_are_ignored_only_when_the_extra_is_omitted(
    tmp_path: Path,
) -> None:
    tests_root = tmp_path / "tests"
    _write(tmp_path, "tests/__init__.py", "")
    _write(tmp_path, "tests/pkg/__init__.py", "")
    _write(tmp_path, "tests/pkg/_support/__init__.py", "")
    _write(tmp_path, "tests/pkg/_support/ui.py", "from textual.app import App\n")
    _write(tmp_path, "tests/pkg/absolute.py", "from tests.pkg._support import ui\n")
    _write(tmp_path, "tests/pkg/relative.py", "from ._support.ui import App\n")
    _write(tmp_path, "tests/pkg/chained.py", "import tests.pkg.relative\n")
    _write(tmp_path, "tests/pkg/core.py", "import json\nfrom . import _support\n")
    _write(tmp_path, "tests/clip/conftest.py", "import pyperclip\n")
    _write(tmp_path, "tests/clip/case.py", "")
    _write(tmp_path, "tests/explorer.py", "from analytics_toolkit.sql_explorer import run\n")

    ignored = extras.collect_ignore(tests_root, {extras.ENV_VAR: "datalens, tui"})
    assert ignored == [
        "clip",
        "explorer.py",
        "pkg/_support/ui.py",
        "pkg/absolute.py",
        "pkg/chained.py",
        "pkg/relative.py",
    ]
    assert extras.collect_ignore(tests_root, {extras.ENV_VAR: "datalens"}) == []
    assert extras.collect_ignore(tests_root, {}) == []


def test_repository_collects_cleanly_without_tui_dependencies() -> None:
    result = subprocess.run(
        [sys.executable, "-c", BLOCKED_COLLECTION, ",".join(TUI_MODULES)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, extras.ENV_VAR: "tui"},
        timeout=300,
    )
    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "tests collected" in summary
    assert "error" not in summary


def test_compatibility_matrix_omits_lagging_tui_tests_and_renderer_steps() -> None:
    policy = python_support.load_policy(REPO_ROOT)
    assert {
        row["without_extras"] for row in python_support.compatibility_matrix(policy)["include"]
    } == {""}
    lagging_policy = {**policy, "newest": "3.15", "minimum": "3.8", "lagging_extras": ["tui"]}
    rows = {
        row["env_name"]: row
        for row in python_support.compatibility_matrix(lagging_policy)["include"]
    }
    assert rows["py315-latest"]["without_extras"] == "tui"
    assert rows["py315-latest"]["install_target"] == ".[datalens]"
    assert rows["py314-latest"]["without_extras"] == ""
    workflow = (REPO_ROOT / ".github/workflows/compatibility.yml").read_text(encoding="utf-8")
    tests_job = workflow.split("\n  tests:\n", 1)[1].split("\n  sdk-conformance:\n", 1)[0]
    assert "ATK_WITHOUT_EXTRAS: ${{ matrix.without_extras }}" in tests_job
    assert tests_job.count("!contains(matrix.without_extras, 'tui')") == 3
