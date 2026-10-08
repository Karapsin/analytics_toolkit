#!/usr/bin/env python3
"""Copy a portable recipe without deploying, installing, or overwriting files."""

import argparse
import json
from pathlib import Path
import re
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--table", required=True)
    parser.add_argument("--organization-id", default="")
    parser.add_argument("--yc-profile", default="")
    parser.add_argument("--target-path", default="")
    parser.add_argument("--dashboard-name", default="Sales dashboard")
    parser.add_argument("--connection-name", default="")
    parser.add_argument("--connection-id", default="")
    arguments = parser.parse_args()
    destination = arguments.destination.expanduser().resolve()
    if destination.exists():
        parser.error("destination already exists; choose a new project directory")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", arguments.table):
        parser.error("table must be an ordinary database.table identifier")
    replacements = {"@TABLE@": arguments.table}
    for name in ("organization_id", "yc_profile", "target_path", "dashboard_name", "connection_name", "connection_id"):
        replacements["@" + name.upper() + "@"] = json.dumps(getattr(arguments, name), ensure_ascii=False)
    assets = Path(__file__).resolve().parents[1] / "assets/project"
    shutil.copytree(assets, destination)
    for path in destination.rglob("*"):
        if path.is_file():
            value = path.read_text(encoding="utf-8")
            for token, replacement in replacements.items():
                value = value.replace(token, replacement)
            path.write_text(value, encoding="utf-8")
    print("Created " + str(destination))
    print("Check the source field mapping and manual options, then run python dashboard.py validate in your existing analytics-toolkit environment.")


if __name__ == "__main__":
    main()
