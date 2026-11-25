from __future__ import annotations

import argparse
import pathlib
import re
import shlex
from collections import defaultdict
from typing import TYPE_CHECKING, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from .project_metadata import load_project

if TYPE_CHECKING:
    from packaging.markers import Marker

PROJECT_PATH = pathlib.Path("pyproject.toml")
CONSTRAINTS_PATH = pathlib.Path("constraints/py39-min.txt")
_NAME_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)")
_NUMERIC_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")
_Branch = Tuple[str, str]
_Comparison = Tuple[str, str, str]
_COMPARISON_LENGTH = 3
_VERSION_MARKERS = {"python_version", "python_full_version", "implementation_version"}


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
        specifier.strip()[2:].strip()
        for specifier in specifier_text.split(",")
        if specifier.strip().startswith(">=")
    ]
    if len(lower_bounds) != 1 or not lower_bounds[0]:
        return name, None
    return name, lower_bounds[0]


def _branch(requirement: Requirement) -> _Branch:
    return _normalize_name(requirement.name), str(requirement.marker) if requirement.marker else ""


def _simple_marker(marker: Marker | None) -> _Comparison | None:
    if marker is None:
        return None
    rendered = str(marker)
    tokens = shlex.split(rendered)
    if len(tokens) != _COMPARISON_LENGTH or rendered.startswith(('"', "'")):
        return None
    return tokens[0], tokens[1], tokens[2]


def _bounds_disjoint(left: _Comparison, right: _Comparison) -> bool:
    if left[0] not in _VERSION_MARKERS:
        return False
    _, left_op, left_value = left
    _, right_op, right_value = right
    if left_op in {"<", "<="} and right_op in {">", ">="}:
        upper, lower = left_value, right_value
        exclusive = left_op == "<" or right_op == ">"
    elif right_op in {"<", "<="} and left_op in {">", ">="}:
        upper, lower = right_value, left_value
        exclusive = right_op == "<" or left_op == ">"
    else:
        return False
    try:
        upper_version, lower_version = Version(upper), Version(lower)
    except InvalidVersion:
        return False
    return upper_version < lower_version or (upper_version == lower_version and exclusive)


def _markers_disjoint(left: Marker | None, right: Marker | None) -> bool:
    """Prove common marker partitions disjoint; unknown Boolean forms fail closed."""
    first, second = _simple_marker(left), _simple_marker(right)
    if first is None or second is None or first[0] != second[0]:
        return False
    if first[1] == "==" and right is not None:
        return not right.evaluate({first[0]: first[2]})
    if second[1] == "==" and left is not None:
        return not left.evaluate({second[0]: second[2]})
    return _bounds_disjoint(first, second)


def _runtime_minimums(dependencies: list[str]) -> tuple[dict[_Branch, str], list[str]]:
    expected: dict[_Branch, str] = {}
    markers: dict[str, list[Marker | None]] = defaultdict(list)
    failures = []
    for requirement in dependencies:
        name, lower_bound = _parse_runtime_minimum(requirement)
        if lower_bound is None:
            failures.append(
                f"runtime dependency {requirement!r} must declare exactly one inclusive lower bound"
            )
            continue
        parsed = Requirement(requirement)
        if any(not _markers_disjoint(previous, parsed.marker) for previous in markers[name]):
            failures.append(
                f"runtime dependency {name!r} is declared more than once "
                "with overlapping or unproven-disjoint markers"
            )
            continue
        markers[name].append(parsed.marker)
        expected[_branch(parsed)] = lower_bound
    return expected, failures


def _parse_constraints(constraints: str) -> tuple[dict[_Branch, list[str]], list[str]]:
    parsed: dict[_Branch, list[str]] = defaultdict(list)
    failures: list[str] = []
    for line_number, raw_line in enumerate(constraints.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            requirement = None
        specifiers = list(requirement.specifier) if requirement else []
        if (
            requirement is None
            or requirement.url
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            failures.append(
                f"constraints line {line_number} must be an exact name==version pin: {line!r}"
            )
            continue
        parsed[_branch(requirement)].append(specifiers[0].version)
    return dict(parsed), failures


def _branch_label(branch: _Branch) -> str:
    name, marker = branch
    return repr(name) + (f" [{marker}]" if marker else "")


def validate_minimum_constraints(
    dependencies: list[str],
    constraints: str,
) -> list[str]:
    expected, failures = _runtime_minimums(dependencies)
    actual, parse_failures = _parse_constraints(constraints)
    failures.extend(parse_failures)

    for branch, minimum in expected.items():
        label = _branch_label(branch)
        pins = actual.get(branch, [])
        if not pins:
            failures.append(f"minimum constraint is missing for runtime dependency {label}")
            continue
        if len(pins) != 1:
            failures.append(
                f"runtime dependency {label} must have exactly one minimum constraint; "
                f"found {pins!r}"
            )
            continue
        if _normalize_version(pins[0]) != _normalize_version(minimum):
            failures.append(
                f"minimum constraint for {label} is {pins[0]!r}, expected lower bound {minimum!r}"
            )

    failures.extend(
        f"constraint {_branch_label(branch)} is not a direct runtime dependency"
        for branch in sorted(actual.keys() - expected.keys())
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
