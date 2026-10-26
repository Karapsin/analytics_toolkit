"""Rolling CPython support policy, compatibility matrix, and repository consistency.

The committed ``release_routines/python_support.json`` policy is the single source
of the supported CPython window. The window contains the seven newest stable
minors; while any optional extra cannot install on the newest minor it is
temporarily extended to eight. ``python_support_drift`` compares this policy with
actual CPython releases and extra support after merges.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .project_metadata import load_project

POLICY_PATH = Path("release_routines/python_support.json")
DOC_PATH = Path("docs/PYTHON_SUPPORT.md")
EXTRAS = ("airflow", "clickhouse-native", "tui", "datalens")
NORMAL_WINDOW = 7
EXTENDED_WINDOW = 8
MATRIX_EXTRAS = ("tui", "datalens")
POLICY_FIELDS = {
    "schema_version",
    "normal_window",
    "extended_window",
    "newest",
    "minimum",
    "runtime_floor",
    "lagging_extras",
    "extras",
    "extra_floors",
}
TABLE_START = "<!-- python-support-table -->"
TABLE_END = "<!-- /python-support-table -->"
CLASSIFIER = re.compile(r'^  "Programming Language :: Python :: (3\.[0-9]+)",$', re.MULTILINE)
REQUIRES_PYTHON = re.compile(r'^requires-python = "([^"]+)"$', re.MULTILINE)
README_DEPENDS = re.compile(r"\*\*Depends:\*\* Python \(`([^`]+)`\)")


class PolicyError(ValueError):
    """The committed Python support policy is internally inconsistent."""


def minor(version: str) -> int:
    """Return the minor number of a ``3.N`` version string."""
    if not isinstance(version, str) or re.fullmatch(r"3\.[0-9]+", version) is None:
        message = f"Expected a Python 3 minor version such as '3.14', got {version!r}"
        raise PolicyError(message)
    return int(version.split(".", 1)[1])


def version(value: int) -> str:
    return f"3.{value}"


def window_size(lagging_extras: list[str] | tuple[str, ...] | set[str]) -> int:
    """Seven minors normally; eight while any extra lags the newest minor."""
    return EXTENDED_WINDOW if lagging_extras else NORMAL_WINDOW


def expected_minimum(newest: str, lagging_extras: list[str] | tuple[str, ...] | set[str]) -> str:
    return version(minor(newest) - window_size(lagging_extras) + 1)


def _validate_lagging(lagging: Any) -> None:
    if not isinstance(lagging, list) or len(set(lagging)) != len(lagging):
        message = "lagging_extras must be a list without duplicates"
        raise PolicyError(message)
    if not set(lagging) <= set(EXTRAS):
        message = f"Unknown lagging extras: {sorted(set(lagging) - set(EXTRAS))}"
        raise PolicyError(message)
    if lagging != sorted(lagging):
        message = "lagging_extras must be sorted"
        raise PolicyError(message)


def validate_policy(policy: dict[str, Any]) -> None:
    """Reject malformed policies and windows that do not follow the 7/8 rule."""
    if set(policy) != POLICY_FIELDS or policy["schema_version"] != 1:
        message = f"Python support policy must contain exactly {sorted(POLICY_FIELDS)}"
        raise PolicyError(message)
    if (policy["normal_window"], policy["extended_window"]) != (NORMAL_WINDOW, EXTENDED_WINDOW):
        message = "The Python support window is seven minors, temporarily extended to eight"
        raise PolicyError(message)
    if tuple(policy["extras"]) != EXTRAS:
        message = f"Policy extras must be exactly {list(EXTRAS)}"
        raise PolicyError(message)
    lagging = policy["lagging_extras"]
    _validate_lagging(lagging)
    expected = expected_minimum(policy["newest"], lagging)
    if policy["minimum"] != expected:
        message = (
            f"Policy minimum {policy['minimum']} does not match a {window_size(lagging)}-minor "
            f"window ending at {policy['newest']}; expected {expected}"
        )
        raise PolicyError(message)
    floor = minor(policy["runtime_floor"])
    if not minor(policy["minimum"]) <= floor <= minor(policy["newest"]):
        message = "runtime_floor must lie inside the supported window"
        raise PolicyError(message)
    for extra, extra_floor in policy["extra_floors"].items():
        if extra not in EXTRAS or not minor(policy["minimum"]) <= minor(extra_floor) <= minor(
            policy["newest"]
        ):
            message = f"Invalid Python floor {extra_floor!r} for extra {extra!r}"
            raise PolicyError(message)


def load_policy(root: Path) -> dict[str, Any]:
    policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    validate_policy(policy)
    return dict(policy)


def supported_minors(policy: dict[str, Any]) -> list[str]:
    return [
        version(value) for value in range(minor(policy["minimum"]), minor(policy["newest"]) + 1)
    ]


def runtime_minors(policy: dict[str, Any]) -> list[str]:
    """Minors exercised by the post-merge compatibility matrix."""
    floor = max(minor(policy["minimum"]), minor(policy["runtime_floor"]))
    return [version(value) for value in range(floor, minor(policy["newest"]) + 1)]


def requires_python(policy: dict[str, Any]) -> str:
    return f">={policy['minimum']},<{version(minor(policy['newest']) + 1)}"


def env_name(python: str, profile: str = "latest") -> str:
    return f"py{python.replace('.', '')}-{profile}"


def minimum_constraints(policy: dict[str, Any]) -> str:
    return f"constraints/{env_name(policy['runtime_floor'], 'min')}.txt"


def omitted_extras(policy: dict[str, Any], python: str) -> list[str]:
    """Matrix extras that cannot install on ``python`` because they lag the newest minor."""
    lagging = set(policy["lagging_extras"]) if python == policy["newest"] else set()
    return [extra for extra in MATRIX_EXTRAS if extra in lagging]


def install_target(policy: dict[str, Any], python: str) -> str:
    """Editable install target with the matrix extras that support ``python``."""
    omitted = omitted_extras(policy, python)
    extras = [extra for extra in MATRIX_EXTRAS if extra not in omitted]
    return f".[{','.join(extras)}]" if extras else "."


def compatibility_matrix(policy: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """GitHub matrix for ``.github/workflows/compatibility.yml``."""
    rows = [
        {
            "python-version": python,
            "env_name": env_name(python),
            "profile": "latest",
            "install_target": install_target(policy, python),
            "without_extras": ",".join(omitted_extras(policy, python)),
            "constraints": "",
        }
        for python in runtime_minors(policy)
    ]
    floor = policy["runtime_floor"]
    rows.append(
        {
            "python-version": floor,
            "env_name": env_name(floor, "min"),
            "profile": "min",
            "install_target": install_target(policy, floor),
            "without_extras": ",".join(omitted_extras(policy, floor)),
            "constraints": minimum_constraints(policy),
        }
    )
    return {"include": rows}


def extra_range(policy: dict[str, Any], extra: str) -> str:
    first = policy["extra_floors"].get(extra, policy["minimum"])
    last = minor(policy["newest"]) - (1 if extra in policy["lagging_extras"] else 0)
    return f"{first}-{version(last)}"


def render_support_table(policy: dict[str, Any]) -> str:
    rows = [
        "| Installation | Supported CPython minors |",
        "| --- | --- |",
        f"| Core (`requires-python {requires_python(policy)}`) "
        f"| {policy['minimum']}-{policy['newest']} |",
    ]
    rows.extend(f"| `{extra}` extra | {extra_range(policy, extra)} |" for extra in EXTRAS)
    window = window_size(policy["lagging_extras"])
    lagging = ", ".join(f"`{extra}`" for extra in policy["lagging_extras"]) or "none"
    rows.extend(
        [
            "",
            f"Current window: {window} minors ending at {policy['newest']}; "
            f"extras lagging the newest minor: {lagging}. Post-merge runtime "
            f"validation covers {policy['runtime_floor']}-{policy['newest']}.",
        ]
    )
    return "\n".join(rows)


def _table_block(text: str) -> str | None:
    match = re.search(re.escape(TABLE_START) + r"\n(.*?)\n" + re.escape(TABLE_END), text, re.DOTALL)
    return match.group(1) if match else None


def consistency_failures(root: Path, policy: dict[str, Any]) -> list[str]:
    """Return every repository file that disagrees with the committed policy."""
    failures = []
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    expected = requires_python(policy)
    declared = REQUIRES_PYTHON.search(pyproject)
    if declared is None or declared.group(1) != expected:
        failures.append(f"pyproject.toml requires-python must be {expected!r}")
    if CLASSIFIER.findall(pyproject) != supported_minors(policy):
        failures.append(
            "pyproject.toml Python classifiers must list exactly "
            + ", ".join(supported_minors(policy))
        )
    readme = README_DEPENDS.search((root / "README.md").read_text(encoding="utf-8"))
    if readme is None or readme.group(1) != expected:
        failures.append(f"README.md Depends line must declare Python (`{expected}`)")
    project = load_project(root / "pyproject.toml")
    declared_extras = set(project.get("optional-dependencies", {})) - {"all"}
    if declared_extras != set(EXTRAS):
        failures.append(f"pyproject.toml extras {sorted(declared_extras)} differ from the policy")
    doc = root / DOC_PATH
    table = _table_block(doc.read_text(encoding="utf-8")) if doc.exists() else None
    if table != render_support_table(policy):
        failures.append(f"{DOC_PATH} support table is stale; run python_support sync")
    tox = (root / "tox.ini").read_text(encoding="utf-8")
    missing = [
        name
        for name in [env_name(python) for python in runtime_minors(policy)]
        + [env_name(policy["runtime_floor"], "min")]
        if re.search(rf"^\s+{re.escape(name)}$", tox, re.MULTILINE) is None
    ]
    if missing:
        failures.append(f"tox.ini env_list is missing {', '.join(missing)}")
    if not (root / minimum_constraints(policy)).exists():
        failures.append(f"Missing minimum dependency constraints {minimum_constraints(policy)}")
    return failures


def sync(root: Path, policy: dict[str, Any]) -> list[Path]:
    """Rewrite generated policy metadata; tox and constraints remain reviewed edits."""
    expected = requires_python(policy)
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = REQUIRES_PYTHON.sub(f'requires-python = "{expected}"', pyproject)
    pyproject = re.sub(
        r'^  "Programming Language :: Python :: 3\.[0-9]+",\n', "", pyproject, flags=re.MULTILINE
    )
    generic = '  "Programming Language :: Python :: 3",\n'
    classifiers = "".join(
        f'  "Programming Language :: Python :: {python}",\n' for python in supported_minors(policy)
    )
    pyproject = pyproject.replace(generic, generic + classifiers, 1)
    readme = README_DEPENDS.sub(
        f"**Depends:** Python (`{expected}`)", (root / "README.md").read_text(encoding="utf-8")
    )
    doc = (root / DOC_PATH).read_text(encoding="utf-8")
    doc = re.sub(
        re.escape(TABLE_START) + r"\n.*?\n" + re.escape(TABLE_END),
        lambda _match: f"{TABLE_START}\n{render_support_table(policy)}\n{TABLE_END}",
        doc,
        flags=re.DOTALL,
    )
    changed = []
    for path, text in (
        (Path("pyproject.toml"), pyproject),
        (Path("README.md"), readme),
        (DOC_PATH, doc),
    ):
        if (root / path).read_text(encoding="utf-8") != text:
            (root / path).write_text(text, encoding="utf-8")
            changed.append(path)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("matrix", "check", "sync"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.root)
    except PolicyError as exc:
        print(f"::error title=Invalid Python support policy::{exc}", file=sys.stderr)
        return 1
    if args.action == "matrix":
        print(json.dumps(compatibility_matrix(policy), separators=(",", ":")))
        return 0
    if args.action == "sync":
        for path in sync(args.root, policy):
            print(f"updated {path}")
    failures = consistency_failures(args.root, policy)
    for failure in failures:
        print(f"::error title=Python support metadata drift::{failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
