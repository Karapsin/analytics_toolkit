"""Collect compatible branch coverage runs and enforce the existing combined ratchets."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .python_support import coverage_versions, env_name, load_policy
from .python_support_test import test_arguments


def data_path(root: Path, version: str) -> Path:
    return root / ".tox" / "coverage-data" / f".coverage.{env_name(version)}"


def collect(root: Path, version: str) -> None:
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    if version != actual or version not in coverage_versions(load_policy(root)):
        message = "Coverage collection requires the planned compatible interpreter"
        raise ValueError(message)
    path = data_path(root, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    arguments = test_arguments(root, version)[3:]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            f"--data-file={path}",
            "-m",
            "pytest",
            *arguments,
        ],
        cwd=root,
        check=True,
    )


def combine(root: Path, selected: list[str]) -> None:
    if selected != coverage_versions(load_policy(root)):
        message = "Coverage runs do not match the complete policy plan"
        raise ValueError(message)
    paths = [data_path(root, version) for version in selected]
    if not all(path.is_file() for path in paths):
        message = "Missing required coverage data; refusing partial coverage"
        raise ValueError(message)
    commands = [
        ["coverage", "combine", "--keep", *(str(path) for path in paths)],
        ["coverage", "json", "-o", "coverage.json"],
        ["coverage", "report"],
        ["release_routines.lib.check_coverage", "coverage.json", "--update-targets-managed"],
    ]
    for command in commands:
        subprocess.run([sys.executable, "-m", *command], cwd=root, check=True)


def run(root: Path) -> None:
    selected = coverage_versions(load_policy(root))
    directory = root / ".tox" / "coverage-data"
    if directory.exists():
        shutil.rmtree(directory)
    subprocess.run([sys.executable, "-m", "coverage", "erase"], cwd=root, check=True)
    for version in selected:
        subprocess.run(
            [sys.executable, "-m", "tox", "-e", env_name(version, "coverage")],
            cwd=root,
            check=True,
        )
    combine(root, selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect")
    args = parser.parse_args()
    if args.collect:
        collect(Path.cwd(), args.collect)
    else:
        run(Path.cwd())


if __name__ == "__main__":
    main()
