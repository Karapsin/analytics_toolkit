from __future__ import annotations

import ast
from pathlib import Path

from tests._support.paths import REPO_ROOT

TESTS_ROOT = REPO_ROOT / "tests"
ALLOWED_AREAS = {
    "ab_utils",
    "agent_tools",
    "atk",
    "dates",
    "datetime",
    "excel",
    "general",
    "release_routines",
    "repository",
    "sql",
    "sql_format",
}
FORBIDDEN_STEMS = {"coverage", "edges", "improvements", "round2", "smoke"}
MAX_TEST_FILE_LINES = 700


def _contains_tests(path: Path) -> bool:
    module = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        for node in module.body
    )


def test_pytest_discovers_short_module_names_only_inside_tests() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'testpaths = ["tests"]' in pyproject
    assert 'python_files = ["*.py"]' in pyproject
    assert 'python_functions = ["test_*"]' in pyproject
    assert 'norecursedirs = [".*", "build", "dist", "_support"]' in pyproject


def test_test_modules_follow_the_module_first_layout() -> None:
    failures: list[str] = []
    for path in sorted(TESTS_ROOT.glob("**/*.py")):
        relative = path.relative_to(TESTS_ROOT)
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > MAX_TEST_FILE_LINES:
            failures.append(f"{relative}: {line_count} lines")
        if not _contains_tests(path):
            continue
        if len(relative.parts) < 2:
            failures.append(f"{relative}: test module is not inside an area directory")
            continue
        if relative.parts[0] not in ALLOWED_AREAS:
            failures.append(f"{relative}: unknown top-level test area")
        if path.name.startswith("test_"):
            failures.append(f"{relative}: redundant test_ filename prefix")
        if path.stem in FORBIDDEN_STEMS:
            failures.append(f"{relative}: catch-all filename")
        if path.stem.startswith(f"{relative.parts[0]}_"):
            failures.append(f"{relative}: repeats its parent area")

    assert not failures, "\n".join(failures)
