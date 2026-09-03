"""Read-only, SSH-local SQL file browsing helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def safe_entries(root: Path, *, browse_root: Path | None = None) -> list[Path]:
    """Return visible directories and SQL files contained by *root*.

    Filesystem errors are deliberately propagated so the TUI can report them in
    its message surface instead of silently presenting an empty directory.
    """
    result: list[Path] = []
    root = root.resolve()
    browse_root = (browse_root or root).resolve()
    entries = list(root.iterdir())
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            try:
                if not _is_relative_to(entry.resolve(), browse_root):
                    continue
            except OSError:
                continue
        try:
            if entry.is_dir() or (entry.is_file() and entry.suffix.lower() == ".sql"):
                result.append(entry)
        except OSError:
            continue
    return sorted(result, key=lambda path: (not path.is_dir(), path.name.casefold()))


def read_sql_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


__all__ = ["read_sql_file", "safe_entries"]
