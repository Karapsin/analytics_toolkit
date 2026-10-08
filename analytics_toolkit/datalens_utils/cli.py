"""Project CLI adapter; library operations retain structured reports and errors."""

from __future__ import annotations

import sys
from typing import Any

from .editing.cli import parser
from .errors import DataLensUtilsError


def run_cli(project: Any, argv: Any = ()) -> Any:
    arguments = parser().parse_args(argv)
    try:
        project._execute(  # noqa: SLF001
            arguments.command or "reconcile",
            chart_keys=getattr(arguments, "chart", None),
            dataset_keys=getattr(arguments, "dataset", None),
            ui=getattr(arguments, "ui", False),
            branch=getattr(arguments, "branch", "published"),
            reporter=print,
        )
        if arguments.command == "validate":
            print("Local configuration valid; no cloud requests.")
    except DataLensUtilsError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        from datalens_sdk import DataLensAPIError  # noqa: PLC0415

        if not isinstance(error, DataLensAPIError):
            raise
        context = error.context
        print(
            f"DataLens API error {context.status_code} {context.code}: "
            f"{context.message} (request_id={context.request_id})",
            file=sys.stderr,
        )
        return 1
    return 0
