from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer import settings as settings_module
from analytics_toolkit.sql_explorer.errors import SqlExplorerConfigurationError
from analytics_toolkit.sql_explorer.settings import (
    DEFAULT_RUN_BINDING,
    ExplorerSettings,
    explorer_settings_path,
    load_settings,
    normalize_run_binding,
    save_settings,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_missing_settings_use_safe_defaults(tmp_path: Path) -> None:
    loaded = load_settings(tmp_path / "missing.json")

    assert loaded.settings == ExplorerSettings()
    assert loaded.warning is None


def test_version_one_settings_migrate_to_current_version(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        '{"version": 1, "run_binding": "f5", "confirm_mutations": true}',
        encoding="utf-8",
    )

    loaded = load_settings(path)

    assert loaded.warning is None
    assert loaded.settings.version == 2


def test_settings_round_trip_atomically_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "sql-explorer.json"
    expected = ExplorerSettings(run_binding="f9", confirm_mutations=False)

    assert save_settings(expected, path) == path
    assert load_settings(path).settings == expected
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", ["ctrl+a", "delete", "tab", "escape", "bad-key"])
def test_editing_and_invalid_shortcuts_are_rejected(value: str) -> None:
    with pytest.raises(SqlExplorerConfigurationError, match="Shortcut"):
        normalize_run_binding(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Control+Enter ", DEFAULT_RUN_BINDING),
        ("option+enter", "alt+enter"),
        ("F12", "f12"),
        ("CTRL+R", "ctrl+r"),
    ],
)
def test_shortcut_aliases_are_normalized(value: str, expected: str) -> None:
    assert normalize_run_binding(value) == expected


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"version": 99, "run_binding": "f5", "confirm_mutations": true}',
        '{"version": 1, "run_binding": 5, "confirm_mutations": true}',
        '{"version": 1, "run_binding": "f5", "confirm_mutations": "yes"}',
    ],
)
def test_invalid_settings_are_ignored_with_warning(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(payload, encoding="utf-8")

    loaded = load_settings(path)

    assert loaded.settings == ExplorerSettings()
    assert str(path) in (loaded.warning or "")


def test_settings_path_uses_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert explorer_settings_path() == tmp_path / "analytics-toolkit" / "sql-explorer.json"


def test_settings_path_uses_windows_appdata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings_module.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert explorer_settings_path() == tmp_path / "analytics-toolkit" / "sql-explorer.json"

    monkeypatch.delenv("APPDATA")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert explorer_settings_path() == tmp_path / "xdg" / "analytics-toolkit" / "sql-explorer.json"


def test_failed_atomic_save_removes_temporary_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"

    def fail_chmod(self: Path, mode: int) -> None:
        del self, mode
        message = "chmod failed"
        raise OSError(message)

    monkeypatch.setattr(settings_module.Path, "chmod", fail_chmod)

    with pytest.raises(OSError, match="chmod failed"):
        save_settings(ExplorerSettings(), path)
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_temporary_file_creation_is_propagated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_temporary_file(**kwargs: object) -> None:
        del kwargs
        message = "temporary file failed"
        raise OSError(message)

    monkeypatch.setattr(settings_module.tempfile, "NamedTemporaryFile", fail_temporary_file)

    with pytest.raises(OSError, match="temporary file failed"):
        save_settings(ExplorerSettings(), tmp_path / "settings.json")
