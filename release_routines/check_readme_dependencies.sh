#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

python <<'PY'
from __future__ import annotations

import ast
import pathlib
import re

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


def load_project() -> dict[str, object]:
    if tomllib is not None:
        with open("pyproject.toml", "rb") as pyproject_file:
            return tomllib.load(pyproject_file)["project"]

    project: dict[str, object] = {}
    optional_dependencies: dict[str, list[str]] = {}
    lines = pathlib.Path("pyproject.toml").read_text(encoding="utf-8").splitlines()
    index = 0
    section = ""

    while index < len(lines):
        stripped = lines[index].strip()
        index += 1
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section == "[project]" and stripped.startswith("requires-python = "):
            project["requires-python"] = ast.literal_eval(stripped.split("=", 1)[1].strip())
            continue
        if section == "[project]" and stripped.startswith("dependencies = "):
            value = stripped.split("=", 1)[1].strip()
            while not value.rstrip().endswith("]"):
                value += "\n" + lines[index].strip()
                index += 1
            project["dependencies"] = ast.literal_eval(value)
            continue
        if section == "[project.optional-dependencies]" and " = " in stripped:
            extra, value = stripped.split("=", 1)
            value = value.strip()
            while not value.rstrip().endswith("]"):
                value += "\n" + lines[index].strip()
                index += 1
            optional_dependencies[extra.strip()] = ast.literal_eval(value)

    if "requires-python" not in project or "dependencies" not in project:
        raise SystemExit("Could not parse project dependency metadata from pyproject.toml")
    project["optional-dependencies"] = optional_dependencies
    return project


def split_requirement(requirement: str) -> tuple[str, str]:
    match = re.match(r"^([A-Za-z0-9_.-]+)(.*)$", requirement)
    if match is None:
        raise SystemExit(f"Unsupported dependency requirement: {requirement}")
    return match.group(1), match.group(2)


def read_readme_line(label: str) -> str:
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    pattern = rf"^\*\*{re.escape(label)}:\*\* (.+)<br>$"
    match = re.search(pattern, readme, flags=re.MULTILINE)
    if match is None:
        raise SystemExit(f"README.md must contain a {label} metadata line")
    return match.group(1)


def parse_python_depends(value: str) -> str:
    match = re.fullmatch(r"Python \(`([^`]+)`\)", value)
    if match is None:
        raise SystemExit(f"Unsupported README Depends line: {value}")
    return match.group(1)


def parse_imports(value: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw_entry in value.split(", "):
        match = re.fullmatch(
            r"\[([^\]]+)\]\(https://pypi\.org/project/[^/]+/\) \(`([^`]+)`\)",
            raw_entry,
        )
        if match is None:
            raise SystemExit(f"Unsupported README Imports entry: {raw_entry}")
        entries.append((match.group(1), match.group(2)))
    return entries


def parse_suggests(value: str) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    for raw_entry in value.split(", "):
        match = re.fullmatch(
            r"\[([^\]]+)\]\(https://pypi\.org/project/[^/]+/\) \(`([^`]+)`; optional extra `([^`]+)`\)",
            raw_entry,
        )
        if match is None:
            raise SystemExit(f"Unsupported README Suggests entry: {raw_entry}")
        entries.append((match.group(1), match.group(2), match.group(3)))
    return entries


project = load_project()
failures: list[str] = []

requires_python = str(project["requires-python"])
readme_depends = parse_python_depends(read_readme_line("Depends"))
if readme_depends != requires_python:
    failures.append(
        f"README Depends Python spec {readme_depends!r} does not match pyproject {requires_python!r}"
    )

expected_imports = [split_requirement(requirement) for requirement in project["dependencies"]]
readme_imports = parse_imports(read_readme_line("Imports"))
if readme_imports != expected_imports:
    failures.append(
        f"README Imports {readme_imports!r} do not match pyproject dependencies {expected_imports!r}"
    )

optional_dependencies = project.get("optional-dependencies", {})
expected_suggests = [
    (*split_requirement(requirement), extra)
    for extra, requirements in optional_dependencies.items()
    for requirement in requirements
]
readme_suggests = parse_suggests(read_readme_line("Suggests"))
if readme_suggests != expected_suggests:
    failures.append(
        f"README Suggests {readme_suggests!r} do not match pyproject optional dependencies {expected_suggests!r}"
    )

if failures:
    raise SystemExit("\n".join(failures))

print("README dependency metadata matches pyproject.toml.")
PY
