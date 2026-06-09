from __future__ import annotations

from pathlib import Path


def discover_markdown_files(root: str | Path = ".") -> list[Path]:
    """Return documentation Markdown files that should be part of the RAG index."""

    root_path = Path(root).resolve()
    files: list[Path] = []

    readme = root_path / "README.md"
    if readme.is_file():
        files.append(readme)

    docs_dir = root_path / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(path for path in docs_dir.rglob("*.md") if path.is_file()))

    return sorted(files, key=lambda path: path.relative_to(root_path).as_posix())
