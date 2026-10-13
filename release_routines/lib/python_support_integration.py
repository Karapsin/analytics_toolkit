"""Offline interpreter selection for pinned disposable SQL integration dependencies."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .python_support import extra_range, load_policy

# Airflow versioned prerequisite tables; Playwright 1.54 and s3fs 2025.2
# require Python >=3.9. Agent MCP tooling requires Python >=3.10.
AUTH_RELEASES = (
    ("2", "2.10.5", 8, 12, "integration/requirements-auth.txt"),
    ("3", "3.3.2", 10, 14, "integration/requirements-auth-airflow3.txt"),
)


def integration_plan(policy: dict) -> dict:
    lower, upper = extra_range(policy, "clickhouse-native")
    lower = max(lower, 10)
    if lower > upper:
        message = "No supported integration interpreter for native transport and agent tools"
        raise ValueError(message)
    core = f"3.{min(max(11, lower), upper)}"
    airflow_lower, airflow_upper = extra_range(policy, "airflow")
    auth = []
    for major, release, first, last, requirements in AUTH_RELEASES:
        first = max(first, lower, airflow_lower)
        last = min(last, upper, airflow_upper)
        if first <= last:
            auth.append(
                {
                    "airflow": major,
                    "release": release,
                    "python": f"3.{min(max(11, first), last)}",
                    "requirements": requirements,
                }
            )
    if not auth:
        message = "Pinned Airflow integration dependencies need a compatible upgrade"
        raise ValueError(message)
    return {"core": core, "auth": {"include": auth}, "fault": auth[0]}


def auth_requirements(root: Path, row: dict[str, str]) -> str:
    text = (root / row["requirements"]).read_text(encoding="utf-8")
    # Preserve the committed local Python 3.11 constraint, adapting only the CI input.
    return re.sub(
        r"(?m)^-c https://raw.githubusercontent.com/apache/airflow/constraints-[^/]+/constraints-\S+",
        f'-c https://raw.githubusercontent.com/apache/airflow/constraints-{row["release"]}/'
        f'constraints-{row["python"]}.txt',
        text,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", choices=["2", "3", "fault"])
    args = parser.parse_args()
    root = Path.cwd()
    plan = integration_plan(load_policy(root))
    if args.requirements:
        row = (
            plan["fault"]
            if args.requirements == "fault"
            else next(
                row for row in plan["auth"]["include"] if row["airflow"] == args.requirements
            )
        )
        print(auth_requirements(root, row), end="")
        return
    for major, *_rest in AUTH_RELEASES:
        if not any(row["airflow"] == major for row in plan["auth"]["include"]):
            print(
                f"Airflow {major}: pinned integration release has no remaining overlap",
                file=sys.stderr,
            )
    # JSON consumed by workflows without executing/installing package code.
    print(json.dumps(plan))


if __name__ == "__main__":
    main()
