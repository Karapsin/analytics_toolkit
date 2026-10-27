"""Stage complete recipe merges and roll back failed local replacements."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .errors import DataLensConfigurationError
from .session import current_session


def publish_project(files: dict[str, Any]) -> None:
    """Publish one validated new project with a same-filesystem directory rename."""
    root = current_session().paths.project_root
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        msg = "Import destination changed while capturing; files preserved."
        raise DataLensConfigurationError(msg)
    temporary = Path(tempfile.mkdtemp(prefix=".datalens-import-", dir=root.parent))
    try:
        for relative, value in files.items():
            path = (temporary / relative).resolve()
            if temporary not in path.parents:
                raise DataLensConfigurationError("Import asset escapes the recipe: " + relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(serialized(relative, value))
        if root.exists():
            root.rmdir()  # Fails closed if another writer populated the destination.
        temporary.rename(root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def serialized(relative: str, value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if relative.endswith(".json") and not isinstance(value, str)
        else value
    ).encode("utf-8")


def stage_and_validate(files: dict[str, Any]) -> None:
    from .bi_engine import validate_registry  # noqa: PLC0415 - Lazy SDK boundary or module cycle.
    from .recipe import load_registry  # noqa: PLC0415 - Lazy SDK boundary or module cycle.

    state = current_session()
    with tempfile.TemporaryDirectory(prefix="datalens-recipe-") as temporary:
        root = Path(temporary).resolve()
        shutil.copytree(
            state.paths.project_root,
            root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".connections", ".git", ".venv", "__pycache__"),
        )
        for relative, value in files.items():
            path = (root / relative).resolve()
            if root not in path.parents:
                raise DataLensConfigurationError("Import asset escapes the recipe: " + relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(serialized(relative, value))
        with state.staged(root):
            current_session().runtime["schema_version"] = json.loads(
                (root / "configs/runtime.json").read_text()
            ).get("schema_version", 1)
            validate_registry(load_registry())


def replace_files(files: dict[str, Any]) -> None:
    """Replace recipe files with a durable, conflict-aware recovery journal."""
    from .local_transactions import replace  # noqa: PLC0415 - Local filesystem boundary.

    replace({name: serialized(name, value) for name, value in files.items()}, current_session())
