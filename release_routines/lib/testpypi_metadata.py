from __future__ import annotations

import pathlib
import sys


def replace_project_name_text(text: str, new_name: str) -> str:
    """Replace only [project].name, validating the marker before writing."""
    lines = text.splitlines(keepends=True)
    in_project = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("[") and stripped.endswith("]"):
            break
        if in_project and line.startswith("name = "):
            lines[index] = f'name = "{new_name}"\n'
            return "".join(lines)
    message = "Could not find [project].name in pyproject.toml"
    raise ValueError(message)


def replace_project_name(new_name: str) -> None:
    path = pathlib.Path("pyproject.toml")
    try:
        text = replace_project_name_text(path.read_text(encoding="utf-8"), new_name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "replace-project-name":
        raise SystemExit(
            "Usage: python -m release_routines.lib.testpypi_metadata "
            "replace-project-name <new_name>"
        )
    replace_project_name(args[1])


if __name__ == "__main__":
    main()
