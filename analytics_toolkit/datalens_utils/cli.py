"""Project CLI adapter; library operations retain structured reports and errors."""

from __future__ import annotations

import json
import sys
from typing import Any

from .editing.cli import parser
from .errors import DataLensUtilsError


def print_report(arguments: Any, report: Any) -> None:
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False))
    elif arguments.command == "validate":
        print("Local configuration valid; no cloud requests.")
    elif arguments.command in {"capabilities", "plan", "import-tab", "import-dashboard"}:
        print(json.dumps(report, ensure_ascii=False, indent=2))


def run_cli(project: Any, argv: Any = ()) -> Any:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command in {"import-tab", "import-dashboard"}:
            report = project._import(  # noqa: SLF001 - CLI delegates to the facade.
                dashboard_id=arguments.dashboard_id,
                tab_id=getattr(arguments, "tab_id", None),
                dry_run=not arguments.write,
                upgrade_recipe=arguments.upgrade_recipe,
                reporter=(lambda *_args, **_kwargs: None) if arguments.json else print,
            )
        else:
            report = project._execute(  # noqa: SLF001
                arguments.command or "reconcile",
                chart_keys=getattr(arguments, "chart", None),
                dataset_keys=getattr(arguments, "dataset", None),
                resource_keys=getattr(arguments, "resource", None),
                ui=getattr(arguments, "ui", False),
                branch=getattr(arguments, "branch", "published"),
                reporter=(lambda *_args, **_kwargs: None) if arguments.json else print,
            )
        print_report(arguments, report)
        if isinstance(report, dict) and report.get("blockers"):
            return 1
    except DataLensUtilsError as error:
        if arguments.json:
            print(
                json.dumps(
                    {
                        "operation": arguments.command or "reconcile",
                        "error": str(error),
                        "capabilities": getattr(error, "report", None),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        from datalens_sdk import DataLensAPIError  # noqa: PLC0415

        if not isinstance(error, DataLensAPIError):
            raise
        context = error.context
        if arguments.json:
            print(
                json.dumps(
                    {
                        "operation": arguments.command or "reconcile",
                        "error": {
                            "status_code": context.status_code,
                            "code": context.code,
                            "message": context.message,
                            "request_id": context.request_id,
                        },
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(
                f"DataLens API error {context.status_code} {context.code}: "
                f"{context.message} (request_id={context.request_id})",
                file=sys.stderr,
            )
        return 1
    return 0
