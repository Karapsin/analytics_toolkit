from __future__ import annotations

import argparse
import pathlib
import re
from collections import defaultdict

from .project_metadata import load_project

PROJECT_PATH = pathlib.Path("pyproject.toml")
CONSTRAINTS_PATH = pathlib.Path("constraints/py38-min.txt")
_NAME_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)")
_EXACT_CONSTRAINT_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)\s*==\s*([^\s;#]+)(?:\s*(?:;|#).*)?$")
_NUMERIC_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_version(version: str) -> tuple[str, object]:
    if _NUMERIC_VERSION_PATTERN.fullmatch(version):
        components = [int(component) for component in version.split(".")]
        while len(components) > 1 and components[-1] == 0:
            components.pop()
        return "numeric", tuple(components)
    return "text", version.lower()


def _parse_runtime_minimum(requirement: str) -> tuple[str, str] | tuple[str, None]:
    requirement_without_marker = requirement.split(";", 1)[0].strip()
    match = _NAME_PATTERN.match(requirement_without_marker)
    if match is None:
        message = f"Unsupported dependency requirement: {requirement}"
        raise ValueError(message)

    name = _normalize_name(match.group(1))
    specifier_text = requirement_without_marker[match.end() :].strip()
    lower_bounds = [
        specifier[2:].strip()
        for specifier in specifier_text.split(",")
        if specifier.strip().startswith(">=")
    ]
    if len(lower_bounds) != 1 or not lower_bounds[0]:
        return name, None
    return name, lower_bounds[0]


def _parse_constraints(constraints: str) -> tuple[dict[str, list[str]], list[str]]:
    parsed: dict[str, list[str]] = defaultdict(list)
    failures: list[str] = []
    for line_number, raw_line in enumerate(constraints.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _EXACT_CONSTRAINT_PATTERN.fullmatch(line)
        if match is None:
            failures.append(
                f"constraints line {line_number} must be an exact name==version pin: {line!r}"
            )
            continue
        parsed[_normalize_name(match.group(1))].append(match.group(2))
    return dict(parsed), failures


def validate_minimum_constraints(
    dependencies: list[str],
    constraints: str,
) -> list[str]:
    failures: list[str] = []
    expected: dict[str, str] = {}
    for requirement in dependencies:
        name, lower_bound = _parse_runtime_minimum(requirement)
        if lower_bound is None:
            failures.append(
                f"runtime dependency {requirement!r} must declare exactly one inclusive lower bound"
            )
            continue
        if name in expected:
            failures.append(f"runtime dependency {name!r} is declared more than once")
            continue
        expected[name] = lower_bound

    actual, parse_failures = _parse_constraints(constraints)
    failures.extend(parse_failures)

    for name, minimum in expected.items():
        pins = actual.get(name, [])
        if not pins:
            failures.append(f"minimum constraint is missing for runtime dependency {name!r}")
            continue
        if len(pins) != 1:
            failures.append(
                f"runtime dependency {name!r} must have exactly one minimum constraint; "
                f"found {pins!r}"
            )
            continue
        if _normalize_version(pins[0]) != _normalize_version(minimum):
            failures.append(
                f"minimum constraint for {name!r} is {pins[0]!r}, expected lower bound {minimum!r}"
            )

    failures.extend(
        f"constraint {name!r} is not a direct runtime dependency"
        for name in sorted(actual.keys() - expected.keys())
    )
    return failures


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Validate direct runtime dependency minimum constraints."
    )
    parser.add_argument("--project", type=pathlib.Path, default=PROJECT_PATH)
    parser.add_argument("--constraints", type=pathlib.Path, default=CONSTRAINTS_PATH)
    args = parser.parse_args(argv)

    project = load_project(args.project)
    dependencies = [str(requirement) for requirement in project.get("dependencies", [])]
    constraints = args.constraints.read_text(encoding="utf-8")
    failures = validate_minimum_constraints(dependencies, constraints)
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Minimum constraints match all {len(dependencies)} direct runtime dependencies.")


if __name__ == "__main__":
    main()
