from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from analytics_toolkit import cli, sql_explorer
from analytics_toolkit.sql_explorer import launcher
from analytics_toolkit.sql_explorer.errors import (
    SqlExplorerConfigurationError,
    SqlExplorerDependencyError,
    SqlExplorerEnvironmentError,
)


class _TerminalStream:
    def isatty(self) -> bool:
        return True


def test_launcher_rejects_non_terminal_streams() -> None:
    with pytest.raises(SqlExplorerEnvironmentError, match="interactive terminal"):
        launcher._require_terminal(SimpleNamespace(isatty=lambda: False), _TerminalStream())
    with pytest.raises(SqlExplorerEnvironmentError, match="interactive terminal"):
        launcher._require_terminal(_TerminalStream(), SimpleNamespace(isatty=lambda: False))


def test_run_launches_app_and_restores_time_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []
    fake_app_module = ModuleType("analytics_toolkit.sql_explorer.app")

    class FakeApp:
        def __init__(self, session: Any) -> None:
            events.append(("app", session))

        def run(self) -> None:
            events.append("run")

    fake_app_module.DatabasePickerApp = object  # type: ignore[attr-defined]
    fake_app_module.SqlExplorerApp = FakeApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "analytics_toolkit.sql_explorer.app", fake_app_module)
    monkeypatch.setattr(launcher.sys, "stdin", _TerminalStream())
    monkeypatch.setattr(launcher.sys, "stdout", _TerminalStream())
    monkeypatch.setattr(launcher, "ExplorerSession", lambda key: f"session:{key}")
    monkeypatch.setattr(launcher.sql, "get_time_print_sink", lambda: "stdout")
    monkeypatch.setattr(
        launcher.sql,
        "set_time_print_sink",
        lambda value: events.append(("sink", value)),
    )

    launcher.run("gp")

    assert events == [
        ("sink", "logging"),
        ("app", "session:gp"),
        "run",
        ("sink", "stdout"),
    ]


def test_run_restores_sink_when_session_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(launcher.sys, "stdin", _TerminalStream())
    monkeypatch.setattr(launcher.sys, "stdout", _TerminalStream())
    monkeypatch.setattr(launcher.sql, "get_time_print_sink", lambda: "stdout")
    monkeypatch.setattr(launcher.sql, "set_time_print_sink", events.append)

    def fail_session(key: str) -> None:
        raise SqlExplorerConfigurationError(key)

    monkeypatch.setattr(launcher, "ExplorerSession", fail_session)

    with pytest.raises(SqlExplorerConfigurationError, match="gp"):
        launcher.run("gp")
    assert events == ["logging", "stdout"]


def test_run_without_key_uses_terminal_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    fake_app_module = ModuleType("analytics_toolkit.sql_explorer.app")

    class FakePicker:
        def __init__(self, choices: Any) -> None:
            events.append(("choices", choices))

        def run(self) -> str:
            return "warehouse"

    class FakeApp:
        def __init__(self, session: Any) -> None:
            events.append(("session", session))

        def run(self) -> None:
            events.append("run")

    fake_app_module.DatabasePickerApp = FakePicker  # type: ignore[attr-defined]
    fake_app_module.SqlExplorerApp = FakeApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "analytics_toolkit.sql_explorer.app", fake_app_module)
    monkeypatch.setattr(launcher.sys, "stdin", _TerminalStream())
    monkeypatch.setattr(launcher.sys, "stdout", _TerminalStream())
    monkeypatch.setattr(launcher, "_database_choices", lambda: (("warehouse", "trino"),))
    monkeypatch.setattr(launcher, "ExplorerSession", lambda key: f"selected:{key}")

    launcher.run()

    assert events == [
        ("choices", (("warehouse", "trino"),)),
        ("session", "selected:warehouse"),
        "run",
    ]


def test_run_stops_cleanly_when_picker_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_app_module = ModuleType("analytics_toolkit.sql_explorer.app")

    class FakePicker:
        def __init__(self, choices: Any) -> None:
            del choices

        def run(self) -> None:
            return None

    fake_app_module.DatabasePickerApp = FakePicker  # type: ignore[attr-defined]
    fake_app_module.SqlExplorerApp = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "analytics_toolkit.sql_explorer.app", fake_app_module)
    monkeypatch.setattr(launcher.sys, "stdin", _TerminalStream())
    monkeypatch.setattr(launcher.sys, "stdout", _TerminalStream())
    monkeypatch.setattr(launcher, "_database_choices", lambda: (("gp", "gp"),))
    monkeypatch.setattr(
        launcher,
        "ExplorerSession",
        lambda key: pytest.fail(f"unexpected session for {key}"),
    )

    launcher.run()


def test_database_choices_include_only_valid_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        SimpleNamespace(connection_key="gp", backend="gp", valid=True),
        SimpleNamespace(connection_key="broken", backend=None, valid=False),
    ]
    monkeypatch.setattr(launcher.sql, "validate_connections", lambda connect: results)

    assert launcher._database_choices() == (("gp", "gp"),)

    monkeypatch.setattr(launcher.sql, "validate_connections", lambda connect: results[1:])
    with pytest.raises(SqlExplorerConfigurationError, match="No valid SQL connections"):
        launcher._database_choices()


def test_missing_tui_dependency_has_install_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "stdin", _TerminalStream())
    monkeypatch.setattr(launcher.sys, "stdout", _TerminalStream())
    monkeypatch.setattr(launcher, "ExplorerSession", lambda key: key)
    monkeypatch.delitem(sys.modules, "analytics_toolkit.sql_explorer.app", raising=False)
    real_import = __import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "app" or name.endswith("sql_explorer.app"):
            error = ModuleNotFoundError("No module named 'textual'")
            error.name = "textual"
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)

    with pytest.raises(SqlExplorerDependencyError, match=r"analytics-toolkit\[tui\]"):
        launcher.run("gp")


def test_unexpected_missing_dependency_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher.sys, "stdin", _TerminalStream())
    monkeypatch.setattr(launcher.sys, "stdout", _TerminalStream())
    monkeypatch.delitem(sys.modules, "analytics_toolkit.sql_explorer.app", raising=False)
    real_import = __import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "app" or name.endswith("sql_explorer.app"):
            error = ModuleNotFoundError("No module named 'unexpected'")
            error.name = "unexpected"
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)

    with pytest.raises(ModuleNotFoundError, match="unexpected"):
        launcher.run("gp")


def test_cli_explore_dispatches_to_public_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    keys: list[str] = []
    monkeypatch.setattr(sql_explorer, "run", keys.append)

    assert cli.main(["sql", "explore", "warehouse"]) == 0
    assert keys == ["warehouse"]

    assert cli.main(["sql", "explore"]) == 0
    assert keys == ["warehouse", None]


def test_cli_explore_reports_domain_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(key: str) -> None:
        raise SqlExplorerEnvironmentError(key)

    monkeypatch.setattr(sql_explorer, "run", fail)

    assert cli.main(["sql", "explore", "gp"]) == 2
    assert "ERROR: gp" in capsys.readouterr().err


def test_cli_help_does_not_import_tui_implementation(capsys: pytest.CaptureFixture[str]) -> None:
    sys.modules.pop("analytics_toolkit.sql_explorer.app", None)

    with pytest.raises(SystemExit) as caught:
        cli.main(["sql", "explore", "--help"])

    assert caught.value.code == 0
    assert "Connection key from .connections" in capsys.readouterr().out
    assert "analytics_toolkit.sql_explorer.app" not in sys.modules
