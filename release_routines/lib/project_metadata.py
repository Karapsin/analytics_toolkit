from __future__ import annotations

import ast
import pathlib
import sys
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python < 3.11
    tomllib = None


PROJECT_PATH = pathlib.Path("pyproject.toml")


def _read_array_value(lines: list[str], index: int, initial_value: str) -> tuple[list[str], int]:
    value = initial_value
    while not value.rstrip().endswith("]"):
        if index >= len(lines):
            raise SystemExit("Unterminated TOML list in pyproject.toml")
        value += "\n" + lines[index].strip()
        index += 1
    parsed = ast.literal_eval(value)
    if not isinstance(parsed, list):
        raise SystemExit(f"Expected TOML list, got: {value}")
    return parsed, index


def load_project() -> dict[str, Any]:
    if tomllib is not None:
        with PROJECT_PATH.open("rb") as pyproject_file:
            return tomllib.load(pyproject_file)["project"]

    project: dict[str, Any] = {}
    optional_dependencies: dict[str, list[str]] = {}
    lines = PROJECT_PATH.read_text(encoding="utf-8").splitlines()
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
        if " = " not in stripped:
            continue

        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        value = raw_value.strip()

        if section == "[project]":
            if key == "dependencies":
                project[key], index = _read_array_value(lines, index, value)
            elif key in {"name", "version", "requires-python"}:
                project[key] = ast.literal_eval(value)
        elif section == "[project.optional-dependencies]":
            optional_dependencies[key], index = _read_array_value(lines, index, value)

    if optional_dependencies:
        project["optional-dependencies"] = optional_dependencies
    return project


def read_project_field(field_name: str) -> Any:
    project = load_project()
    if field_name not in project:
        raise SystemExit(f"Could not find [project].{field_name} in pyproject.toml")
    return project[field_name]


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "read-field":
        raise SystemExit("Usage: python -m release_routines.lib.project_metadata read-field <field_name>")

    value = read_project_field(args[1])
    if not isinstance(value, str):
        raise SystemExit(f"[project].{args[1]} is not a string field")
    print(value)


if __name__ == "__main__":
    main()
