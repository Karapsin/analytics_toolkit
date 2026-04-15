from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


def here(filename: str) -> str:
    normalized_name = Path(filename.replace("\\", "/"))

    base_dir = _resolve_base_dir()
    if base_dir is not None:
        return str(base_dir / normalized_name)

    cwd_candidate = Path.cwd() / normalized_name
    if cwd_candidate.exists():
        return str(cwd_candidate)

    matches = sorted(Path.cwd().rglob(normalized_name.name))
    if len(matches) == 1:
        return str(matches[0])

    return str(cwd_candidate)


def _resolve_base_dir() -> Path | None:
    main_module = sys.modules.get("__main__")
    main_file = getattr(main_module, "__file__", None)
    if main_file and not str(main_file).startswith("<"):
        return Path(main_file).expanduser().resolve().parent

    module_path = Path(__file__).expanduser().resolve()
    for frame_info in inspect.stack()[1:]:
        frame_name = frame_info.filename
        if frame_name.startswith("<"):
            continue

        frame_path = Path(frame_name).expanduser().resolve()
        if frame_path == module_path:
            continue
        return frame_path.parent

    return None


def read_file(file_path: str, params_dict: dict[str, Any] | None = None) -> str:
    path = Path(file_path).expanduser()
    if not path.exists():
        raise InvalidSqlInputError(f"SQL file does not exist: {file_path}")

    from .logging import time_print

    time_print(f"Reading file {path}")
    text = path.read_text(encoding="utf-8")

    if params_dict is None:
        return text

    return text.format(**params_dict)
