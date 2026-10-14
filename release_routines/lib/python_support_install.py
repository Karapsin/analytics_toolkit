"""Install test dependencies appropriate to the current Python and support policy."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .python_support import load_policy, supported_extras


def installation(root: Path, version: str, mode: str) -> list[str]:
    policy = load_policy(root)
    # Airflow is exercised separately, without changing the toolkit test runtime.
    extras = [extra for extra in supported_extras(policy, version) if extra != "airflow"]
    requirement = f".[{','.join(extras)}]" if extras else "."
    arguments = [sys.executable, "-m", "pip", "install"]
    if mode == "latest":
        arguments += ["--upgrade", "--upgrade-strategy", "eager"]
    if mode == "min":
        arguments += ["-c", "constraints/python-min.txt"]
    return [*arguments, "-e", requirement]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["latest", "min"])
    args = parser.parse_args()
    root = Path.cwd()
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    subprocess.run(installation(root, version, args.mode), cwd=root, check=True)


if __name__ == "__main__":
    main()
