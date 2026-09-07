from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit import cli, general, sql, sql_explorer
from analytics_toolkit.general.connections import get_connections_path_override
from analytics_toolkit.sql_explorer import app as app_module
from analytics_toolkit.sql_explorer import connections, connections_picker, launcher
from analytics_toolkit.sql_explorer.settings import ExplorerSettings, load_settings, save_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))


def test_activate_remembers_path_and_preserves_settings(tmp_path: Path) -> None:
    original = ExplorerSettings(run_binding="f9", confirm_mutations=False)
    save_settings(original)
    path = tmp_path / ".connections"
    assert connections.activate_connections_file(path) == path
    assert load_settings().settings == replace(original, connections_path=str(path))
    assert get_connections_path_override() == path
    assert sql.validate_connections(connect=False)[0].valid


def test_failed_activation_restores_source_and_remembered_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / ".connections"
    connections.activate_connections_file(original)
    previous = load_settings().settings
    bad = tmp_path / "bad" / ".connections"
    bad.parent.mkdir()
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="No valid"):
        connections.activate_connections_file(bad)
    assert get_connections_path_override() == original
    assert load_settings().settings == previous
    bad.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    def fail_save(*args: object) -> None:
        message = "settings read-only"
        raise OSError(message)

    monkeypatch.setattr(connections, "save_settings", fail_save)
    with pytest.raises(OSError, match="read-only"):
        connections.activate_connections_file(bad)
    assert get_connections_path_override() == original


@pytest.mark.parametrize("content", ["not-json", "[]"])
def test_candidate_check_does_not_change_active_source(tmp_path: Path, content: str) -> None:
    original = tmp_path / ".connections"
    general.set_connections_path(original)
    candidate = tmp_path / "other" / ".connections"
    candidate.parent.mkdir()
    candidate.write_text(content, encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError)):
        connections.check_connections_file(candidate)
    assert get_connections_path_override() == original


def test_remembered_file_skips_picker_on_later_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".connections"
    connections.activate_connections_file(path)
    general.set_connections_path(None)

    def unexpected_picker(*args: object, **kwargs: object) -> None:
        pytest.fail("A remembered file must not trigger a new scan")

    monkeypatch.setattr(connections_picker, "ConnectionsPickerApp", unexpected_picker)
    assert launcher._prepare_connections()
    assert get_connections_path_override() == path


def test_explicit_path_wins_over_saved_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = tmp_path / ".connections"
    general.set_connections_path(original)
    save_settings(ExplorerSettings(connections_path="/missing/.connections"))
    assert launcher._prepare_connections()
    assert get_connections_path_override() == original


def test_missing_saved_file_rediscovery_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_settings(ExplorerSettings(connections_path="/missing/.connections"))
    options: list[dict[str, object]] = []

    def picker(**kwargs: object) -> SimpleNamespace:
        options.append(kwargs)
        return SimpleNamespace(run=lambda: None)

    monkeypatch.setattr(connections_picker, "ConnectionsPickerApp", picker)
    assert not launcher._prepare_connections()
    assert options[0]["auto_select"] is True


def test_invalid_saved_file_returns_to_picker_without_autoselect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bad = tmp_path / ".connections"
    bad.write_text("{}", encoding="utf-8")
    save_settings(ExplorerSettings(connections_path=str(bad)))
    options = []

    def picker(**kwargs: object) -> SimpleNamespace:
        options.append(kwargs)
        return SimpleNamespace(run=lambda: None)

    monkeypatch.setattr(connections_picker, "ConnectionsPickerApp", picker)
    assert not launcher._prepare_connections()
    assert options[0]["auto_select"] is False
    assert "No valid" in options[0]["error"]


def test_tui_cli_dispatches_with_optional_key(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = []
    monkeypatch.setattr(sql_explorer, "run", keys.append)
    assert cli.main(["tui"]) == 0
    assert cli.main(["tui", "gp"]) == 0
    assert keys == [None, "gp"]


def test_atk_help_uses_alias_and_does_not_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["/venv/bin/atk"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["tui", "--help"])
    assert exc.value.code == 0
    assert "usage: atk tui" in capsys.readouterr().out


def test_settings_accept_remembered_path_and_reject_wrong_type(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    payload = {
        "version": 3,
        "run_binding": "f5",
        "confirm_mutations": True,
        "connections_path": 123,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert "connections_path" in load_settings(path).warning


def test_restart_reselects_database_and_reloads_same_alias_from_new_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / ".connections"
    second = tmp_path / "second" / ".connections"
    second.parent.mkdir()
    content = json.loads(first.read_text(encoding="utf-8"))
    second.write_text(json.dumps({"gp": content["ch"]}), encoding="utf-8")
    connections.activate_connections_file(first)
    backends = []
    picker_choices = []

    class FakeApp:
        def __init__(self, session: object) -> None:
            backends.append(session.database.backend)

        def run(self) -> connections.ConnectionsRestart | None:
            return connections.ConnectionsRestart(second) if len(backends) == 1 else None

    class FakePicker:
        def __init__(self, choices: object) -> None:
            picker_choices.append(choices)

        def run(self) -> str:
            return "gp"

    monkeypatch.setattr(launcher, "_require_terminal", lambda *args: None)
    monkeypatch.setattr(app_module, "SqlExplorerApp", FakeApp)
    monkeypatch.setattr(app_module, "DatabasePickerApp", FakePicker)
    launcher.run("gp")
    assert backends == ["gp", "ch"]
    assert picker_choices == [(("gp", "ch"),)]
    assert load_settings().settings.connections_path == str(second)


def test_failed_activation_restores_implicitly_remembered_path(tmp_path: Path) -> None:
    original = tmp_path / ".connections"
    general.set_connections_path(None)
    connections.remember_connections_path(original)
    bad = tmp_path / "bad" / ".connections"
    bad.parent.mkdir()
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="No valid"):
        connections.activate_connections_file(bad)
    assert connections.get_last_connections_path() == original
    assert get_connections_path_override() is None


def test_first_launch_activates_picker_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        connections_picker,
        "ConnectionsPickerApp",
        lambda **kwargs: SimpleNamespace(run=lambda: tmp_path / ".connections"),
    )
    assert launcher._prepare_connections()
    assert load_settings().settings.connections_path == str(tmp_path / ".connections")


def test_launcher_returns_when_source_picker_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_require_terminal", lambda *args: None)
    monkeypatch.setattr(launcher, "_prepare_connections", lambda path: False)
    launcher.run()
