from __future__ import annotations

import pathlib
import re

from .project_metadata import read_project_field


def main() -> None:
    version = read_project_field("version")
    readme = pathlib.Path("README.md").read_text(encoding="utf-8")
    match = re.search(r"^\*\*Version:\*\* `([^`]+)`<br>$", readme, flags=re.MULTILINE)
    if match is None:
        raise SystemExit("README.md must contain a line formatted as: **Version:** `...`<br>")

    readme_version = match.group(1)
    if readme_version != version:
        raise SystemExit(
            f"README.md version {readme_version!r} does not match "
            f"pyproject.toml version {version!r}"
        )

    print(f"Package metadata version matches pyproject.toml: {version}")


if __name__ == "__main__":
    main()
