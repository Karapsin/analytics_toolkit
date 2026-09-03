"""Read-only, SSH-local SQL file browsing helpers."""

from __future__ import annotations

from pathlib import Path


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def safe_entries(root: Path, *, browse_root: Path | None = None) -> list[Path]:
    """Return in-root directories and regular files contained by *root*.

    Filesystem errors are deliberately propagated so the TUI can report them in
    its message surface instead of silently presenting an empty directory.
    """
    result: list[Path] = []
    root = root.resolve()
    browse_root = (browse_root or root).resolve()
    entries = list(root.iterdir())
    for entry in entries:
        if entry.is_symlink():
            try:
                if not _is_relative_to(entry.resolve(), browse_root):
                    continue
            except OSError:
                continue
        try:
            if entry.is_dir() or entry.is_file():
                result.append(entry)
        except OSError:
            continue
    return sorted(result, key=lambda path: (not path.is_dir(), path.name.casefold()))


def completion_entries(root: Path, value: str) -> tuple[Path, tuple[Path, ...]]:
    """Return the directory and entries matching a shell-style path prefix."""
    browse_root = root.resolve()
    if not value:
        directory = browse_root
        prefix = ""
    else:
        entered = Path(value).expanduser()
        target = entered if entered.is_absolute() else browse_root / entered
        if value.endswith("/"):
            directory = target.resolve()
            prefix = ""
        else:
            directory = target.parent.resolve()
            prefix = target.name
    if not _is_relative_to(directory, browse_root):
        message = f"Path must remain inside {browse_root}"
        raise ValueError(message)
    entries = tuple(
        entry
        for entry in safe_entries(directory, browse_root=browse_root)
        if entry.name.casefold().startswith(prefix.casefold())
    )
    return directory, entries


def read_sql_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


__all__ = ["completion_entries", "read_sql_file", "safe_entries"]
