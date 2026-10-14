"""Run core tests and the optional suites supported on the current minor."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .python_support import load_policy, supports_extra


def test_arguments(root: Path, version: str) -> list[str]:
    policy = load_policy(root)
    arguments = [sys.executable, "-m", "pytest", "-q"]
    if not supports_extra(policy, "tui", version):
        arguments += [
            "--ignore=tests/sql/explorer",
            "--ignore=tests/agent_tools/visual.py",
            "--ignore=tests/sql/metadata/completion_queries.py",
        ]
    if not supports_extra(policy, "datalens", version):
        arguments += ["--ignore=tests/datalens_utils"]
    return arguments


def main() -> None:
    root = Path.cwd()
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    subprocess.run(test_arguments(root, version), cwd=root, check=True)


if __name__ == "__main__":
    main()
