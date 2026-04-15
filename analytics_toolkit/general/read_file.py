from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


def here(filename: str) -> str:
    normalized_name = filename.replace("\\", "/")
    caller_candidate = _resolve_caller_path(normalized_name)
    if caller_candidate is not None:
        return str(caller_candidate)

    cwd_candidate = Path.cwd() / normalized_name
    if cwd_candidate.exists():
        return str(cwd_candidate)

    matches = sorted(Path.cwd().rglob(Path(normalized_name).name))
    if len(matches) == 1:
        return str(matches[0])

    return str(cwd_candidate)


def _resolve_caller_path(filename: str) -> Path | None:
    for frame_info in inspect.stack()[1:]:
        frame_name = frame_info.filename.replace("\\", "/")
        if not _is_user_frame(frame_name):
            continue

        frame_path = Path(frame_info.filename).expanduser()
        return frame_path.resolve().parent / filename

    return None


def _is_user_frame(frame_name: str) -> bool:
    if frame_name.startswith("<"):
        return False

    if frame_name.endswith("/analytics_toolkit/general/read_file.py"):
        return False

    excluded_fragments = (
        "/IPython/",
        "/ipykernel_",
        "/site-packages/",
        "/dist-packages/",
        "/Contents/Resources/app/extensions/",
        "/tmp/",
        "/var/folders/",
    )
    if any(fragment in frame_name for fragment in excluded_fragments):
        return False

    normalized_prefixes = {
        Path(prefix).expanduser().resolve().as_posix()
        for prefix in (sys.prefix, sys.base_prefix, sys.exec_prefix)
        if prefix
    }
    return not any(frame_name.startswith(prefix + "/") or frame_name == prefix for prefix in normalized_prefixes)


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
