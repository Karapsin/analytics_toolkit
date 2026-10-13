"""Offline Python support policy, generated metadata, and validation matrices."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .check_readme_dependencies import split_requirement
from .project_metadata import load_project

POLICY_PATH = Path("release_routines/python_support.json")
EXTRAS = ("airflow", "clickhouse-native", "tui", "datalens")
NORMAL_WINDOW = 7
MINIMUM_MINOR = 9

# Upstream versioned installation prerequisites (Python support tables):
# https://airflow.apache.org/docs/apache-airflow/{release}/installation/prerequisites.html
AIRFLOW_RELEASES = (("2.4.3", 7, 10), ("2.11.0", 8, 12), ("3.0.0", 9, 12), ("3.3.2", 10, 14))


def minor(version: str) -> int:
    if not isinstance(version, str) or re.fullmatch(r"3\.[0-9]+", version) is None:
        message = f"Expected a Python 3 minor version, got {version!r}"
        raise ValueError(message)
    return int(version.split(".")[1])


def load_policy(root: Path) -> dict:
    policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    validate_policy(policy)
    return policy


def validate_policy(policy: dict) -> None:
    if set(policy) != {"normal_window", "minimum", "maximum", "extras"}:
        message = "Invalid Python support policy fields"
        raise ValueError(message)
    if policy["normal_window"] != NORMAL_WINDOW:
        message = "The normal Python support window must contain seven minors"
        raise ValueError(message)
    if (
        minor(policy["minimum"]) < MINIMUM_MINOR
        or minor(policy["maximum"]) < minor(policy["minimum"])
    ):
        message = "Invalid core range; Python 3.8 must not be reintroduced"
        raise ValueError(message)
    if set(policy["extras"]) != set(EXTRAS):
        message = "Every optional extra must have an explicit support range"
        raise ValueError(message)
    for support in policy["extras"].values():
        if (
            set(support) != {"minimum", "maximum"}
            or minor(support["minimum"]) > minor(support["maximum"])
        ):
            message = "Invalid extra support range"
            raise ValueError(message)


def versions(policy: dict) -> list[str]:
    return [f"3.{value}" for value in range(minor(policy["minimum"]), minor(policy["maximum"]) + 1)]


def supports_extra(policy: dict, extra: str, version: str) -> bool:
    if not minor(policy["minimum"]) <= minor(version) <= minor(policy["maximum"]):
        return False
    if extra == "all":
        return all(supports_extra(policy, item, version) for item in EXTRAS)
    support = policy["extras"][extra]
    return minor(support["minimum"]) <= minor(version) <= minor(support["maximum"])


def extra_range(policy: dict, extra: str) -> tuple[int, int]:
    ranges = policy["extras"].values() if extra == "all" else [policy["extras"][extra]]
    return (
        max(minor(policy["minimum"]), *(minor(item["minimum"]) for item in ranges)),
        min(minor(policy["maximum"]), *(minor(item["maximum"]) for item in ranges)),
    )


def supported_extras(policy: dict, version: str) -> list[str]:
    return [extra for extra in EXTRAS if supports_extra(policy, extra, version)]


def coverage_python(policy: dict) -> str:
    midpoint = (minor(policy["minimum"]) + minor(policy["maximum"])) // 2
    lower, upper = extra_range(policy, "all")
    return f"3.{min(max(midpoint, lower), upper) if lower <= upper else midpoint}"


def coverage_versions(policy: dict) -> list[str]:
    """Cover the complete suite using compatible test selections."""
    selected = [coverage_python(policy)]
    midpoint = minor(selected[0])
    for extra in EXTRAS:
        lower, upper = extra_range(policy, extra)
        if lower > upper:
            message = f"No supported coverage environment for extra {extra}"
            raise ValueError(message)
        if not any(supports_extra(policy, extra, version) for version in selected):
            selected.append(f"3.{min(max(midpoint, lower), upper)}")
    return selected


def airflow_matrix(policy: dict) -> list[dict[str, str]]:
    lower, upper = extra_range(policy, "airflow")
    rows = []
    for release, first, last in AIRFLOW_RELEASES:
        compatible_first, compatible_last = max(first, lower), min(last, upper)
        if compatible_first <= compatible_last:
            rows.extend(
                {"airflow": release, "python": f"3.{value}"}
                for value in sorted({compatible_first, compatible_last})
            )
    return rows


def retired_airflow_majors(policy: dict) -> list[str]:
    covered = {row["airflow"].split(".")[0] for row in airflow_matrix(policy)}
    return sorted({"2", "3"} - covered)


def local_matrix(policy: dict) -> list[str]:
    # Each coverage run uses exactly the same test selection as its latest run.
    covered = {env_name(version) for version in coverage_versions(policy)}
    return [row["env_name"] for row in test_matrix(policy) if row["env_name"] not in covered] + [
        env_name(sdk_python(policy), suffix) for suffix in ("sdk31", "sdk32")
    ]


def env_name(version: str, suffix: str = "latest") -> str:
    return f"py{version.replace('.', '')}-{suffix}"


def test_matrix(policy: dict) -> list[dict[str, str]]:
    return [
        {"python-version": version, "env_name": env_name(version)} for version in versions(policy)
    ] + [{"python-version": policy["minimum"], "env_name": env_name(policy["minimum"], "min")}]


def extra_matrix(policy: dict) -> list[dict[str, str]]:
    matrix = []
    for version in versions(policy):
        extras = supported_extras(policy, version)
        matrix.extend({"python-version": version, "extra": extra} for extra in extras)
        if len(extras) == len(EXTRAS):
            matrix.append({"python-version": version, "extra": "all"})
    return matrix


def sdk_python(policy: dict) -> str:
    return f"3.{max(minor(policy['minimum']), minor(policy['extras']['datalens']['minimum']))}"


def transition(policy: dict, latest: str, ready: set[str]) -> dict:
    """Retain the floor during overlap; shrink only after every extra and all pass."""
    validate_policy(policy)
    if minor(latest) < minor(policy["maximum"]) or not ready <= {*EXTRAS, "all"}:
        message = "A support transition cannot move backwards or accept unknown extras"
        raise ValueError(message)
    result = json.loads(json.dumps(policy))
    result["maximum"] = latest
    for extra in EXTRAS:
        if extra in ready:
            if minor(latest) < minor(result["extras"][extra]["minimum"]):
                message = "Readiness cannot override an extra's minimum Python version"
                raise ValueError(message)
            result["extras"][extra]["maximum"] = latest
    if ready == {*EXTRAS, "all"}:
        result["minimum"] = f"3.{max(minor(policy['minimum']), minor(latest) - NORMAL_WINDOW + 1)}"
    validate_policy(result)
    return result


def stable_minors(releases: list[dict]) -> list[str]:
    """Use published final Python 3 releases, independent of patch versions."""
    found = set()
    for release in releases:
        match = re.fullmatch(r"Python 3\.([0-9]+)\.[0-9]+", release.get("name", ""))
        if match and release.get("is_published") is True and release.get("pre_release") is False:
            found.add(int(match[1]))
    if not found:
        message = "No published stable Python 3 releases found"
        raise ValueError(message)
    return [f"3.{value}" for value in sorted(found)]


def render_tox(policy: dict) -> str:
    supported = versions(policy)
    canonical = coverage_python(policy)
    sdk = sdk_python(policy)
    names = ["lint", "type", "coverage", "artifacts"]
    names += [env_name(version) for version in supported]
    names += [
        env_name(policy["minimum"], "min"),
        env_name(sdk, "sdk31"),
        env_name(sdk, "sdk32"),
    ]
    interpreters = "\n".join(
        f"    py{version.replace('.', '')}: "
        f"{{env:PYTHON{version.replace('.', '')}:python{version}}}"
        for version in supported
    )
    tool_python = f"{{env:PYTHON{canonical.replace('.', '')}:python{canonical}}}"
    type_python = f"{{env:PYTHON{policy['minimum'].replace('.', '')}:python{policy['minimum']}}}"
    header = "# Generated by release_routines.lib.python_support sync; edit the support policy.\n"
    text = header + "[tox]\nrequires = tox>=4\nenv_list =\n"
    text += "".join(f"    {name}\n" for name in names)
    text += f"""
[testenv]
description = run the test suite in {{env_name}}
skip_install = true
base_python =
{interpreters}
set_env =
    PYTHONPYCACHEPREFIX = /tmp/utils_dev_pycache
pass_env =
    CPPFLAGS
    LDFLAGS
    PKG_CONFIG_PATH
    PYTHON3*
deps =
    latest: pytest
    min: -c constraints/python-min.txt
    min: pytest==8.3.5
commands_pre =
    latest: python -m release_routines.lib.python_support_install latest
    min: python -m release_routines.lib.python_support_install min
commands =
    python -m compileall analytics_toolkit atk tests
    min: python -m release_routines.lib.check_minimum_constraints
    min: python -m pip check
    python -m release_routines.lib.python_support_test

[testenv:lint]
description = prevent new strict Ruff lint and formatting debt
base_python = {tool_python}
deps = ruff>=0.15.20,<0.16
commands = python -m release_routines.lib.quality_debt lint

[testenv:type]
description = prevent new strict mypy debt against the supported Python floor
base_python = {type_python}
deps = mypy>=1.16.1,<1.17
commands_pre = python -m release_routines.lib.python_support_install latest
commands = python -m release_routines.lib.quality_debt type

[testenv:coverage]
description = enforce branch coverage across all production modules
base_python = {tool_python}
deps =
    coverage>=7.15,<8
    tox>=4,<5
commands_pre =
commands = python -m release_routines.lib.python_support_coverage

[testenv:artifacts]
description = build and smoke-test wheel and sdist artifacts outside the checkout
base_python = {tool_python}
deps =
    build>=1.5,<2
    twine>=6.2,<7
    check-wheel-contents>=0.6.3,<0.7
commands = python -m release_routines.lib.artifact_smoke
"""
    for version in coverage_versions(policy):
        text += f"""
[testenv:{env_name(version, "coverage")}]
description = collect compatible branch coverage on Python {version}
base_python = {{env:PYTHON{version.replace('.', '')}:python{version}}}
deps =
    coverage>=7.15,<8
    pytest
commands_pre = python -m release_routines.lib.python_support_install latest
commands = python -m release_routines.lib.python_support_coverage --collect {version}
"""
    for suffix, sdk_version in (("sdk31", "3.1.0"), ("sdk32", "3.2.0")):
        text += f"""
[testenv:{env_name(sdk, suffix)}]
description = offline DataLens conformance against SDK {sdk_version} on Python {sdk}
deps =
    pytest
    datalens-sdk=={sdk_version}
commands_pre = python -m pip install -e .
commands = pytest -q tests/datalens_utils
"""
    return text


def generated_files(root: Path, policy: dict) -> dict[Path, str]:
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    requirement = f">={policy['minimum']},<3.{minor(policy['maximum']) + 1}"
    project = re.sub(
        r'^requires-python = "[^"]+"',
        f'requires-python = "{requirement}"',
        project,
        flags=re.MULTILINE,
    )
    project = re.sub(
        r'^  "Programming Language :: Python :: 3\.[0-9]+",\n', "", project, flags=re.MULTILINE
    )
    classifiers = "".join(f'  "Programming Language :: Python :: {v}",\n' for v in versions(policy))
    project = project.replace(
        '  "Programming Language :: Python :: 3",\n',
        '  "Programming Language :: Python :: 3",\n' + classifiers,
    )
    project = re.sub(
        r'^python_version = "3\.[0-9]+"',
        f'python_version = "{policy["minimum"]}"',
        project,
        flags=re.MULTILINE,
    )
    readme = (root / "README.md").read_text(encoding="utf-8")
    readme = re.sub(
        r"\*\*Depends:\*\* Python \(`[^`]+`\)", f"**Depends:** Python (`{requirement}`)", readme
    )
    metadata = load_project(root / "pyproject.toml")
    imports = []
    suggests = []
    for requirement_text in metadata["dependencies"]:
        name, constraint = split_requirement(requirement_text)
        imports.append(f"[{name}](https://pypi.org/project/{name}/) (`{constraint}`)")
    for extra, requirements in metadata["optional-dependencies"].items():
        for requirement_text in requirements:
            name, constraint = split_requirement(requirement_text)
            suggests.append(
                f"[{name}](https://pypi.org/project/{name}/) "
                f"(`{constraint}`; optional extra `{extra}`)"
            )
    for label, entries in (("Imports", imports), ("Suggests", suggests)):
        readme = re.sub(
            rf"^\*\*{label}:\*\* .+<br>$",
            f"**{label}:** {', '.join(entries)}<br>",
            readme,
            flags=re.MULTILINE,
        )
    result = {
        Path("pyproject.toml"): project,
        Path("README.md"): readme,
        Path("tox.ini"): render_tox(policy),
    }
    doc_path = Path("docs/PYTHON_SUPPORT.md")
    if (root / doc_path).exists():
        doc = (root / doc_path).read_text(encoding="utf-8")
        rows = [f"| Core | {policy['minimum']}-{policy['maximum']} |"]
        for extra in (*EXTRAS, "all"):
            lower, upper = extra_range(policy, extra)
            value = f"3.{lower}-3.{upper}" if lower <= upper else "No common supported minor"
            rows.append(f"| `{extra}` | {value} |")
        table = "| Installation | Supported Python minors |\n| --- | --- |\n" + "\n".join(rows)
        result[doc_path] = re.sub(
            r"<!-- support-table -->.*?<!-- /support-table -->",
            "<!-- support-table -->\n" + table + "\n<!-- /support-table -->",
            doc,
            flags=re.DOTALL,
        )
    return result


def sync(root: Path, policy: dict, *, check: bool = False) -> None:
    validate_policy(policy)
    for path, text in generated_files(root, policy).items():
        current = (root / path).read_text(encoding="utf-8")
        if current != text:
            if check:
                message = f"Python support metadata drift in {path}; run python-support sync"
                raise ValueError(message)
            (root / path).write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "sync",
            "check",
            "matrix",
            "extras",
            "local-matrix",
            "coverage",
            "sdk",
            "airflow",
            "coverage-pythons",
            "tool-pythons",
        ],
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    policy = load_policy(args.root)
    if args.action in {"sync", "check"}:
        sync(args.root, policy, check=args.action == "check")
    elif args.action in {"matrix", "extras"}:
        matrix = test_matrix(policy) if args.action == "matrix" else extra_matrix(policy)
        print(json.dumps({"include": matrix}))
    elif args.action == "airflow":
        for major in retired_airflow_majors(policy):
            print(f"Airflow {major}: no remaining compatible Python overlap", file=sys.stderr)
        print(json.dumps({"include": airflow_matrix(policy)}))
    elif args.action == "local-matrix":
        print(",".join(local_matrix(policy)))
    elif args.action in {"coverage-pythons", "tool-pythons"}:
        selected = coverage_versions(policy)
        if args.action == "tool-pythons":
            selected = sorted({*selected, policy["minimum"]}, key=minor)
        print("\n".join(selected))
    else:
        print(coverage_python(policy) if args.action == "coverage" else sdk_python(policy))


if __name__ == "__main__":
    main()
