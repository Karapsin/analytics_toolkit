from __future__ import annotations

import inspect
import sys
import sysconfig
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError


_FALLBACK_STDLIB_MODULE_NAMES = frozenset(
    {
        "abc",
        "argparse",
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "functools",
        "importlib",
        "inspect",
        "itertools",
        "json",
        "logging",
        "multiprocessing",
        "os",
        "pathlib",
        "queue",
        "re",
        "runpy",
        "site",
        "subprocess",
        "sys",
        "threading",
        "time",
        "typing",
        "unittest",
        "urllib",
        "warnings",
    }
)


def here(filename: str) -> str:
    normalized_name, base_dir = _resolve_here_parts(filename)
    return str(base_dir / normalized_name)


def from_here(filename: str, levels_up: int = 0) -> str:
    if type(levels_up) is not int:
        raise TypeError("levels_up must be an integer.")
    if levels_up < 0:
        raise ValueError("levels_up must be greater than or equal to 0.")
    if levels_up == 0:
        return here(filename)

    normalized_name = Path(filename.replace("\\", "/"))
    base_dir = _resolve_base_dir() or Path.cwd()
    for _ in range(levels_up):
        base_dir = base_dir.parent

    return str(base_dir / normalized_name)


def _resolve_here_parts(filename: str) -> tuple[Path, Path]:
    normalized_name = Path(filename.replace("\\", "/"))

    base_dir = _resolve_base_dir()
    if base_dir is not None:
        return normalized_name, base_dir

    cwd_candidate = Path.cwd() / normalized_name
    if cwd_candidate.exists():
        return normalized_name, Path.cwd()

    recursive_match = _find_unique_recursive_match(Path.cwd(), normalized_name)
    if recursive_match is not None:
        return normalized_name, _base_dir_for_recursive_match(
            recursive_match,
            normalized_name,
        )

    return normalized_name, Path.cwd()


def _base_dir_for_recursive_match(
    recursive_match: Path,
    normalized_name: Path,
) -> Path:
    base_dir = recursive_match
    for _ in normalized_name.parts:
        base_dir = base_dir.parent
    return base_dir


def _resolve_base_dir() -> Path | None:
    positron_dir = _resolve_positron_editor_dir()
    if positron_dir is not None:
        return positron_dir

    main_dir = _resolve_main_file_dir()
    if main_dir is not None:
        return main_dir

    module_path = Path(__file__).expanduser().resolve()
    for frame_info in inspect.stack()[1:]:
        frame_name = frame_info.filename
        if frame_name.startswith("<"):
            continue

        frame_path = Path(frame_name).expanduser().resolve()
        if _is_this_module_path(frame_path, module_path) or _is_runtime_path(frame_path):
            continue
        return frame_path.parent

    return None


def _resolve_positron_editor_dir() -> Path | None:
    editor_dir = None
    try:
        ipython_module = import_module("IPython")
        get_ipython = getattr(ipython_module, "get_ipython", None)
        if callable(get_ipython):
            shell = get_ipython()
            kernel = getattr(shell, "kernel", None)
            get_parent = getattr(kernel, "get_parent", None)
            if callable(get_parent):
                uri = _positron_code_location_uri(get_parent("shell"))
                if uri is not None:
                    parsed_uri = urlparse(uri)
                    if parsed_uri.scheme.lower() == "file" and parsed_uri.path:
                        decoded_path = url2pathname(parsed_uri.path)
                        local_path = (
                            f"//{parsed_uri.netloc}{decoded_path}"
                            if parsed_uri.netloc
                            else decoded_path
                        )
                        editor_dir = Path(local_path).parent
    except Exception:  # noqa: BLE001, S110 -- optional IDE metadata must fail closed.
        pass
    return editor_dir


def _positron_code_location_uri(parent: object) -> str | None:
    uri = None
    if isinstance(parent, dict):
        content = parent.get("content")
        if isinstance(content, dict):
            positron = content.get("positron")
            if isinstance(positron, dict):
                code_location = positron.get("code_location")
                if isinstance(code_location, dict):
                    candidate = code_location.get("uri")
                    if isinstance(candidate, str):
                        uri = candidate
    return uri


def _is_this_module_path(path: Path, module_path: Path) -> bool:
    return (
        path == module_path
        or path.as_posix().endswith("/analytics_toolkit/general/read_file.py")
    )


def _find_unique_recursive_match(cwd: Path, normalized_name: Path) -> Path | None:
    if normalized_name.is_absolute():
        return None

    relative_pattern = normalized_name.as_posix()
    relative_matches = sorted(cwd.rglob(relative_pattern))
    if len(relative_matches) == 1:
        return relative_matches[0]
    if len(relative_matches) > 1 or len(normalized_name.parts) > 1:
        return None

    basename_matches = sorted(cwd.rglob(normalized_name.name))
    if len(basename_matches) == 1:
        return basename_matches[0]
    return None


def _resolve_main_file_dir() -> Path | None:
    main_module = sys.modules.get("__main__")
    main_file = getattr(main_module, "__file__", None)
    if main_file and not str(main_file).startswith("<"):
        main_path = Path(main_file).expanduser().resolve()
        if not _is_runtime_path(main_path):
            return main_path.parent
    return None


def _is_runtime_path(path: Path) -> bool:
    normalized = path.as_posix()
    runtime_fragments = (
        "/IPython/",
        "/ipykernel_",
        "/site-packages/",
        "/dist-packages/",
        "/Contents/Resources/app/extensions/",
        "/Contents/plugins/python",
        "/.vscode/extensions/",
        "/debugpy/",
        "/pydev/",
        "/pydevd/",
        "/pydevd.py",
        "/tmp/",
        "/var/folders/",
    )
    if any(fragment in normalized for fragment in runtime_fragments):
        return True

    if _looks_like_stdlib_path(path):
        return True

    runtime_prefixes = {
        Path(prefix).expanduser().resolve()
        for prefix in (
            sys.prefix,
            sys.base_prefix,
            sys.exec_prefix,
            sysconfig.get_paths().get("stdlib"),
        )
        if prefix
    }
    return any(path == prefix or prefix in path.parents for prefix in runtime_prefixes)


def _looks_like_stdlib_path(path: Path) -> bool:
    stdlib_modules = getattr(
        sys,
        "stdlib_module_names",
        _FALLBACK_STDLIB_MODULE_NAMES,
    )
    for index, part in enumerate(path.parts[:-1]):
        if not part.startswith("python"):
            continue
        if index == 0 or path.parts[index - 1] != "lib":
            continue
        top_level = path.parts[index + 1]
        module_name = Path(top_level).stem
        if top_level in stdlib_modules or module_name in stdlib_modules:
            return True
    return False


def read_file(
    file_path: str,
    params_dict: dict[str, Any] | None = None,
    *,
    here: bool = False,
) -> str:
    path = Path(globals()["here"](file_path) if here else file_path).expanduser()
    if not path.exists():
        raise InvalidSqlInputError(f"SQL file does not exist: {file_path}")

    from .logging import time_print

    time_print(f"Reading file {path}")
    text = path.read_text(encoding="utf-8")

    if params_dict is None:
        return text

    return text.format(**params_dict)


def read_file_here(
    file_path: str,
    params_dict: dict[str, Any] | None = None,
) -> str:
    return read_file(file_path, params_dict, here=True)


def write_file(file_path: str, text: str) -> None:
    path = Path(file_path).expanduser()

    from .logging import time_print

    time_print(f"Writing file {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
