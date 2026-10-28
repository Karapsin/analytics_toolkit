"""Package CLI for published dashboard capture; no consumer bootstrap scripts."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .import_api import DataLensAuthConfig, import_dashboard


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("datalens", help="DataLens project workflows.")
    commands = parser.add_subparsers(dest="datalens_command")
    command = commands.add_parser(
        "import-dashboard",
        help="Capture published source into a new local project; no cloud writes.",
    )
    command.add_argument(
        "source", help="Dashboard ID or URL; all tabs are imported unless --tab is supplied."
    )
    command.add_argument("--destination", required=True)
    command.add_argument("--runtime-dir")
    command.add_argument("--tab", action="append")
    command.add_argument("--write", action="store_true")
    command.add_argument("--resume", action="store_true")
    command.add_argument("--json", action="store_true")
    command.add_argument(
        "--installation",
        choices=("yc", "enterprise"),
        default=os.environ.get("DATALENS_INSTALLATION", "yc"),
    )
    command.add_argument("--organization-id", default=os.environ.get("DATALENS_ORG_ID"))
    command.add_argument("--yc-profile", default=os.environ.get("DATALENS_YC_PROFILE"))
    command.add_argument("--yc-binary", default=os.environ.get("DATALENS_YC_BIN", "yc"))
    command.add_argument("--base-url", default=os.environ.get("DATALENS_BASE_URL"))
    command.add_argument(
        "--token-env",
        help="Environment variable name containing an existing token; never a token value.",
    )
    command.set_defaults(handler=run_import)


def run_import(arguments: Any) -> int:
    try:
        auth = DataLensAuthConfig(
            **{
                name: getattr(arguments, name)
                for name in (
                    "installation",
                    "organization_id",
                    "yc_profile",
                    "yc_binary",
                    "base_url",
                    "token_env",
                )
            }
        )
        report = import_dashboard(
            arguments.source,
            arguments.destination,
            auth=auth,
            runtime_root=arguments.runtime_dir,
            tab_ids=arguments.tab,
            dry_run=not arguments.write,
            resume=arguments.resume,
            reporter=lambda *args, **kwargs: print(*args, file=sys.stderr, **kwargs),  # noqa: T201 - CLI progress.
        )
    except Exception as error:  # noqa: BLE001 - CLI reports typed failures as JSON.
        context = getattr(error, "context", None)
        report = {
            "operation": "import-dashboard",
            "written": False,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
                "request_id": getattr(context, "request_id", None),
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=None if arguments.json else 2))  # noqa: T201 - CLI result.
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=None if arguments.json else 2))  # noqa: T201 - CLI result.
    return 0 if report["capture_complete"] else 2
