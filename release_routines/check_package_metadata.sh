#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(CDPATH='' cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

python <<'PY'
import pathlib
import re

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


def read_project_version() -> str:
    if tomllib is not None:
        with open("pyproject.toml", "rb") as pyproject_file:
            return tomllib.load(pyproject_file)["project"]["version"]

    in_project = False
    for line in pathlib.Path("pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("[") and stripped.endswith("]"):
            break
        if in_project and stripped.startswith("version = "):
            value = stripped.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                return value[1:-1]
            raise SystemExit(f"Unsupported pyproject value for version: {value}")

    raise SystemExit("Could not find [project].version in pyproject.toml")


version = read_project_version()
readme = pathlib.Path("README.md").read_text(encoding="utf-8")
match = re.search(r"^\*\*Version:\*\* `([^`]+)`<br>$", readme, flags=re.MULTILINE)
if match is None:
    raise SystemExit("README.md must contain a line formatted as: **Version:** `...`<br>")

readme_version = match.group(1)
if readme_version != version:
    raise SystemExit(
        f"README.md version {readme_version!r} does not match pyproject.toml version {version!r}"
    )

print(f"Package metadata version matches pyproject.toml: {version}")
PY
