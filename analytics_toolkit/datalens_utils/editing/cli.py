from __future__ import annotations

import argparse
from typing import Any


def parser() -> Any:
    value = argparse.ArgumentParser(
        description="Reconcile the full gallery or edit selected DataLens objects."
    )
    commands = value.add_subparsers(dest="command")
    value.add_argument("--json", action="store_true", help="Emit one structured JSON result.")
    for name in ("import-tab", "import-dashboard"):
        command = commands.add_parser(
            name, help="Preview or merge published dashboard content into this recipe."
        )
        command.add_argument("--dashboard-id", required=True)
        if name == "import-tab":
            command.add_argument("--tab-id", required=True)
        command.add_argument(
            "--write", action="store_true", help="Write a blocker-free staged merge."
        )
        command.add_argument("--upgrade-recipe", action="store_true")
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for name in ("capabilities", "plan", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "plan":
            command.add_argument("--resource", metavar="KIND:KEY", action="append")
    validate = commands.add_parser(
        "validate", help="Validate local configuration; no cloud requests or runtime writes."
    )
    validate.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    status = commands.add_parser(
        "status", help="Compare local files and batched live revisions; no writes."
    )
    status.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for name in ("apply", "pull"):
        command = commands.add_parser(name)
        scope = command.add_mutually_exclusive_group()
        scope.add_argument("--chart", metavar="KEY", action="append")
        scope.add_argument("--dataset", metavar="KEY", action="append")
        scope.add_argument("--ui", action="store_true")
        scope.add_argument("--resource", metavar="KIND:KEY", action="append")
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if name == "pull":
            command.add_argument("--branch", choices=("published", "saved"), default="published")
    verify = commands.add_parser(
        "verify", help="Read and verify all published objects; no cloud writes."
    )
    verify.add_argument("--full", required=True, action="store_true")
    verify.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return value
