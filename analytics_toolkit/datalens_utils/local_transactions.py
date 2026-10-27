"""Durable, conflict-aware rollback for multi-file local recipe transactions."""

from __future__ import annotations

import base64
import importlib
import json
import os
import tempfile
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from .errors import DataLensConfigurationError, DataLensUtilsError
from .import_source import atomic_json

if TYPE_CHECKING:
    from .session import ProjectPaths, Session

_PRIVATE = {".connections", ".env", ".certs", ".git", ".venv", "__pycache__"}
_WINDOWS = os.name == "nt"


def _path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if (
        root not in path.parents
        or Path(relative).is_absolute()
        or _PRIVATE.intersection(Path(relative).parts)
        or any(
            part.is_symlink()
            for part in (root / relative, *(root / relative).parents)
            if part != root and root in part.parents
        )
    ):
        raise DataLensConfigurationError("Import asset escapes the recipe: " + relative)
    return path


def snapshot(root: Path) -> dict[str, str]:
    """Fingerprint shareable files without opening credentials or private directories."""
    root = root.resolve()
    result = {}
    for parent, directories, names in os.walk(root):
        directories[:] = [name for name in directories if name not in _PRIVATE]
        for name in names + [name for name in directories if (Path(parent) / name).is_symlink()]:
            if name not in _PRIVATE:
                path = Path(parent) / name
                relative = path.relative_to(root).as_posix()
                content = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
                result[relative] = ("link:" if path.is_symlink() else "file:") + sha256(
                    content
                ).hexdigest()
    return result


def _sync_directory(path: Path) -> None:
    if not _WINDOWS:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _directory(path: Path) -> None:
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(exist_ok=True, mode=0o700)
        _sync_directory(parent.parent)


def _journal(paths: ProjectPaths) -> Path:
    identity = sha256(str(paths.project_root).encode()).hexdigest()
    return paths.runtime_root / "local-transactions" / (identity + ".json")


@contextmanager
def _locked(journal: Path) -> Iterator[None]:
    _directory(journal.parent)
    with journal.with_suffix(".lock").open("a+b") as stream:
        locking = importlib.import_module("msvcrt" if _WINDOWS else "fcntl")
        if _WINDOWS:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
        try:
            if _WINDOWS:
                locking.locking(stream.fileno(), locking.LK_NBLCK, 1)
            else:
                locking.flock(stream.fileno(), locking.LOCK_EX | locking.LOCK_NB)
        except OSError as error:
            message = "Another local recipe transaction is active."
            raise DataLensUtilsError(message) from error
        try:
            yield
        finally:
            if _WINDOWS:
                stream.seek(0)
                locking.locking(stream.fileno(), locking.LK_UNLCK, 1)
            else:
                locking.flock(stream.fileno(), locking.LOCK_UN)


def _save(journal: Path, value: dict[str, Any]) -> None:
    atomic_json(journal, value)
    _sync_directory(journal.parent)


def _remove(journal: Path) -> None:
    journal.unlink()
    _sync_directory(journal.parent)


def _bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _write(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        _sync_directory(path.parent)
        return
    _directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _decode(value: Any) -> bytes | None:
    return base64.b64decode(value, validate=True) if value is not None else None


def _entries(journal: Path, root: Path) -> tuple[dict[str, Any], dict[Path, tuple[Any, Any]]]:
    try:
        value = json.loads(journal.read_text(encoding="utf-8"))
        if (
            value["version"] != 1
            or value["project_root"] != str(root)
            or value["state"] not in {"prepared", "committed", "rolled_back"}
            or not isinstance(value["files"], dict)
        ):
            message = "Invalid transaction identity or state"
            raise ValueError(message)  # noqa: TRY301 - Retain the invalid journal.
        entries = {
            _path(root, relative): (_decode(item["before"]), _decode(item["after"]))
            for relative, item in value["files"].items()
        }
    except (ValueError, TypeError, KeyError) as error:
        raise DataLensConfigurationError(
            "Invalid local recovery journal: " + str(journal)
        ) from error
    return value, entries


def _recover(journal: Path, root: Path) -> bool:
    if not journal.exists():
        return False
    value, entries = _entries(journal, root)
    if value["state"] == "prepared":
        # Inspect every destination before restoring anything. Never erase an
        # editor's intervening changes, even if other files need rollback.
        for path, versions in entries.items():
            if _bytes(path) not in versions:
                raise DataLensUtilsError(
                    "Local recovery conflict; destination edits and journal preserved: " + str(path)
                )
        for path, (before, after) in reversed(list(entries.items())):
            if before != after and _bytes(path) == after:
                _write(path, before)
        value["state"] = "rolled_back"
        _save(journal, value)
    _remove(journal)
    return True


def recover(paths: ProjectPaths) -> bool:
    """Recover before reading recipe configuration, or refuse concurrent/user edits."""
    journal = _journal(paths)
    if not journal.exists():
        return False
    with _locked(journal):
        return _recover(journal, paths.project_root)


def _apply(entries: dict[Path, tuple[Any, Any]]) -> None:
    for path, (before, after) in entries.items():
        if _bytes(path) != before:
            raise DataLensUtilsError("Local destination changed before replacement: " + str(path))
        if before != after:
            _write(path, after)
    if any(_bytes(path) != after for path, (_, after) in entries.items()):
        message = "Local destination changed during replacement; journal retained."
        raise DataLensUtilsError(message)


def replace(files: dict[str, bytes], state: Session) -> None:
    """Durably record both versions, compare destination fingerprints, then replace."""
    root = state.paths.project_root
    journal = _journal(state.paths)
    with _locked(journal):
        _recover(journal, root)
        if state.destination_snapshot is not None and snapshot(root) != state.destination_snapshot:
            message = "Local destination changed during the operation; files preserved."
            raise DataLensUtilsError(message)
        entries = {
            _path(root, name): (_bytes(_path(root, name)), data) for name, data in files.items()
        }
        value = {
            "version": 1,
            "project_root": str(root),
            "state": "prepared",
            "files": {
                path.relative_to(root).as_posix(): {
                    "before": base64.b64encode(before).decode() if before is not None else None,
                    "after": base64.b64encode(after).decode(),
                }
                for path, (before, after) in entries.items()
            },
        }
        _save(journal, value)
        try:
            _apply(entries)
            value["state"] = "committed"
            _save(journal, value)
        except BaseException:
            _recover(journal, root)
            raise
        _remove(journal)
        state.destination_snapshot = snapshot(root)
