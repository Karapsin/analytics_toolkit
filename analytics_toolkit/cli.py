from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def _prefer_local_package_path() -> None:
    package = sys.modules.get(__package__)
    package_path = getattr(package, "__path__", None)
    if package_path is None:
        return

    local_path = str(Path(__file__).resolve().parent)
    existing_paths = [str(path) for path in package_path]
    package.__path__ = [
        local_path,
        *[path for path in existing_paths if not _same_path(path, local_path)],
    ]


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return left == right


_prefer_local_package_path()

from .sql.connection import validate_connections  # noqa: E402
from .sql.core.capabilities import format_support_matrix  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    return int(args.handler(args))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analytics-toolkit")
    subparsers = parser.add_subparsers(dest="command")

    sql_parser = subparsers.add_parser("sql")
    sql_subparsers = sql_parser.add_subparsers(dest="sql_command")

    validate_parser = sql_subparsers.add_parser("validate")
    validate_parser.add_argument("keys", nargs="*")
    validate_parser.add_argument(
        "--connect",
        action="store_true",
        help="Open and close each configured connection after validating config.",
    )
    validate_parser.set_defaults(handler=_handle_sql_validate)

    support_parser = sql_subparsers.add_parser("support-matrix")
    support_parser.set_defaults(handler=_handle_sql_support_matrix)

    explore_parser = sql_subparsers.add_parser(
        "explore",
        help="Open the optional exploratory SQL terminal interface.",
    )
    explore_parser.add_argument(
        "db_key",
        nargs="?",
        help="Connection key from .connections; omit it to open the terminal picker.",
    )
    explore_parser.set_defaults(handler=_handle_sql_explore)

    return parser


def _handle_sql_validate(args: argparse.Namespace) -> int:
    results = validate_connections(args.keys or None, connect=args.connect)
    for result in results:
        status = "OK" if result.valid else "ERROR"
        backend = result.backend or "-"
        connected = ""
        if args.connect:
            connected = " connected=yes" if result.connected else " connected=no"
        line = f"{status} {result.connection_key} backend={backend}{connected}"
        if result.error:
            line += f" error={result.error}"
        print(line)
    return 0 if all(result.valid for result in results) else 1


def _handle_sql_support_matrix(args: argparse.Namespace) -> int:
    del args
    print(format_support_matrix())
    return 0


def _handle_sql_explore(args: argparse.Namespace) -> int:
    from .sql_explorer import SqlExplorerError, run  # noqa: PLC0415

    try:
        run(args.db_key)
    except SqlExplorerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
