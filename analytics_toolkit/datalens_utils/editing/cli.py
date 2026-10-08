from __future__ import annotations

import argparse
from typing import Any


def parser() -> Any:
    value = argparse.ArgumentParser(
        description="Reconcile the full gallery or edit selected DataLens objects."
    )
    commands = value.add_subparsers(dest="command")
    commands.add_parser(
        "validate", help="Validate local configuration; no cloud requests or runtime writes."
    )
    commands.add_parser("status", help="Compare local files and batched live revisions; no writes.")
    for name in ("apply", "pull"):
        command = commands.add_parser(name)
        scope = command.add_mutually_exclusive_group()
        scope.add_argument("--chart", metavar="KEY", action="append")
        scope.add_argument("--dataset", metavar="KEY", action="append")
        scope.add_argument("--ui", action="store_true")
        if name == "pull":
            command.add_argument("--branch", choices=("published", "saved"), default="published")
    verify = commands.add_parser(
        "verify", help="Read and verify all published objects; no cloud writes."
    )
    verify.add_argument("--full", required=True, action="store_true")
    return value
