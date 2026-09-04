from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import SqlExplorerConfigurationError

SETTINGS_VERSION = 2
DEFAULT_RUN_BINDING = "ctrl+enter"
DEFAULT_QUERY_CONCURRENCY = 1
_FUNCTION_KEY_RE = re.compile(r"f(?:[1-9]|1[0-2])\Z")
_MODIFIED_KEY_RE = re.compile(r"(?:ctrl|alt)\+(?:enter|[a-z])\Z")
_BINDING_ALIASES = {
    "c-enter": "ctrl+enter",
    "control+enter": "ctrl+enter",
    "option+enter": "alt+enter",
}
_RESERVED_BINDINGS = {
    "alt+left",
    "alt+right",
    "ctrl+a",
    "ctrl+backspace",
    "ctrl+c",
    "ctrl+delete",
    "ctrl+e",
    "ctrl+f",
    "ctrl+home",
    "ctrl+left",
    "ctrl+n",
    "ctrl+o",
    "ctrl+right",
    "ctrl+s",
    "ctrl+shift+z",
    "ctrl+v",
    "ctrl+t",
    "ctrl+w",
    "ctrl+x",
    "ctrl+y",
    "ctrl+z",
    "delete",
    "escape",
    "shift+tab",
    "tab",
}


@dataclass(frozen=True)
class ExplorerSettings:
    version: int = SETTINGS_VERSION
    run_binding: str = DEFAULT_RUN_BINDING
    confirm_mutations: bool = True
    max_concurrent_queries: int = DEFAULT_QUERY_CONCURRENCY


@dataclass(frozen=True)
class SettingsLoadResult:
    settings: ExplorerSettings
    warning: str | None = None


def explorer_settings_path() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "analytics-toolkit" / "sql-explorer.json"

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_root = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return config_root / "analytics-toolkit" / "sql-explorer.json"


def normalize_run_binding(value: str) -> str:
    normalized = str(value).strip().lower().replace(" ", "")
    normalized = _BINDING_ALIASES.get(normalized, normalized)
    if normalized in _RESERVED_BINDINGS:
        message = f"Shortcut {normalized!r} is reserved for editing or pane navigation."
        raise SqlExplorerConfigurationError(message)
    if not (_FUNCTION_KEY_RE.fullmatch(normalized) or _MODIFIED_KEY_RE.fullmatch(normalized)):
        message = "Shortcut must be F1-F12, Ctrl+Enter, Alt+Enter, Ctrl+letter, or Alt+letter."
        raise SqlExplorerConfigurationError(message)
    return normalized


def load_settings(path: Path | None = None) -> SettingsLoadResult:
    settings_path = path or explorer_settings_path()
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
        return SettingsLoadResult(_settings_from_mapping(raw))
    except FileNotFoundError:
        return SettingsLoadResult(ExplorerSettings())
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return SettingsLoadResult(
            ExplorerSettings(),
            f"Ignoring invalid SQL explorer settings at {settings_path}: {exc}",
        )


def save_settings(settings: ExplorerSettings, path: Path | None = None) -> Path:
    settings_path = path or explorer_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=settings_path.parent,
            prefix=f".{settings_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path = Path(temp_name)
        temp_path.chmod(0o600)
        temp_path.replace(settings_path)
    except OSError:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
        raise
    return settings_path


def _settings_from_mapping(raw: Any) -> ExplorerSettings:
    if not isinstance(raw, dict):
        message = "settings must contain a JSON object"
        raise TypeError(message)
    version = raw.get("version")
    if version not in {1, SETTINGS_VERSION}:
        message = f"unsupported settings version {raw.get('version')!r}"
        raise ValueError(message)
    confirm_mutations = raw.get("confirm_mutations")
    if not isinstance(confirm_mutations, bool):
        message = "confirm_mutations must be a boolean"
        raise TypeError(message)
    run_binding = raw.get("run_binding")
    if not isinstance(run_binding, str):
        message = "run_binding must be a string"
        raise TypeError(message)
    concurrency = raw.get("max_concurrent_queries", DEFAULT_QUERY_CONCURRENCY)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        message = "max_concurrent_queries must be a positive integer"
        raise TypeError(message)
    return ExplorerSettings(
        run_binding=normalize_run_binding(run_binding),
        confirm_mutations=confirm_mutations,
        max_concurrent_queries=concurrency,
    )


__all__ = [
    "DEFAULT_QUERY_CONCURRENCY",
    "DEFAULT_RUN_BINDING",
    "ExplorerSettings",
    "SettingsLoadResult",
    "explorer_settings_path",
    "load_settings",
    "normalize_run_binding",
    "save_settings",
]
