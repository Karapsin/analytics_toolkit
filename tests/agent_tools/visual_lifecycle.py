from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from rich.text import Text
from textual.app import ScreenStackError
from textual.css.query import NoMatches
from textual.widgets import Static

from agent_tools import sql_explorer_visual_scene as scenes
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from analytics_toolkit.sql_explorer.tabs import SaveChangesScreen


class ReadinessApp:
    _configure_pending_scene = cast("Any", scenes.VisualExplorerApp._configure_pending_scene)
    _scene_readiness_details = cast("Any", scenes.VisualExplorerApp._scene_readiness_details)
    _poll_scene_readiness = cast("Any", scenes.VisualExplorerApp._poll_scene_readiness)
    _refresh_visual_evidence = cast("Any", scenes.VisualExplorerApp._refresh_visual_evidence)
    _scene_state_ready = cast("Any", scenes.VisualExplorerApp._scene_state_ready)
    _mark_scene_ready = cast("Any", scenes.VisualExplorerApp._mark_scene_ready)
    _focus_save_choice = cast("Any", scenes.VisualExplorerApp._focus_save_choice)
    _configure_creation_scene = cast("Any", scenes.VisualExplorerApp._configure_creation_scene)

    def __init__(self, scene: str, screen: object) -> None:
        self.visual_scene_id = scene
        self.screen_stack = [screen]
        self.screen: Any = screen
        self.active_workspace = SimpleNamespace(file_label="quarterly_report.sql")
        self.focused: Any = None
        self.creation_scene_ready = False
        self.scene_configured = False
        self._pending_scene_screen: Any = None
        self.visual_evidence_path = Path("geometry.json")
        self.visual_manifest_path = Path("manifest")
        self._restore_primary_overflow_tab = lambda: True
        self.selector_snapshot: dict[str, Any] = {
            "collection_complete": True,
            "selector": {
                "expected_value": "table_schema",
                "selected_value": "table_schema",
                "visibility_ok": True,
            },
        }

    def _selector_readiness_snapshot(self) -> dict[str, Any]:
        return self.selector_snapshot


def save_app(scene: str) -> tuple[ReadinessApp, Mock]:
    screen = Mock(spec=SaveChangesScreen)
    screen.is_mounted = True
    screen.title = "quarterly_report.sql"
    button = Mock()
    button.is_mounted = True
    screen.query_one.return_value = button
    app = ReadinessApp(scene, screen)
    app.focused = button
    return app, screen


def create_app() -> tuple[ReadinessApp, Mock]:
    screen = Mock(spec=CreateTableScreen)
    screen.is_mounted = True
    screen.query.return_value = []
    # Exercise the real values() lookups, including queries performed inside it.
    screen.values.side_effect = lambda: CreateTableScreen.values(screen)
    screen.query_one.return_value = SimpleNamespace(value="table_schema", text="SELECT 1", is_mounted=True)
    app = ReadinessApp("create-table-invalid", screen)
    return app, screen


@pytest.mark.parametrize("scene", ["save-changes", "save-changes-cancel"])
@pytest.mark.parametrize(
    "state", ["empty", "wrong", "unmounted", "button_unmounted", "missing", "focus", "identity"]
)
def test_save_readiness_rejects_unavailable_or_inconsistent_state(scene: str, state: str) -> None:
    app, screen = save_app(scene)
    if state == "empty":
        app.screen_stack = []
    elif state == "wrong":
        app.screen = SimpleNamespace(is_mounted=True)
    elif state == "unmounted":
        screen.is_mounted = False
    elif state == "button_unmounted":
        screen.query_one.return_value.is_mounted = False
    elif state == "missing":
        screen.query_one.side_effect = NoMatches("requested button absent")
    elif state == "focus":
        app.focused = object()
    else:
        screen.title = "another.sql"
    assert not app._scene_state_ready()
    app._mark_scene_ready()
    assert not app.creation_scene_ready


@pytest.mark.parametrize("scene", ["save-changes", "save-changes-cancel"])
def test_save_readiness_accepts_only_requested_focus(scene: str) -> None:
    app, screen = save_app(scene)
    assert app._scene_state_ready()
    choice = "cancel" if scene.endswith("cancel") else "save"
    assert screen.query_one.call_args.args[0] == f"#save-changes-{choice}"
    app._mark_scene_ready()
    assert app.creation_scene_ready


@pytest.mark.parametrize(
    "selector", ["#create-table-name", "#create-table-source", "#create-from-sql"]
)
def test_create_readiness_handles_missing_controls_inside_values(selector: str) -> None:
    app, screen = create_app()

    def lookup(requested: str, *_args: object) -> object:
        if requested == selector:
            raise NoMatches("control removed during values")
        return SimpleNamespace(value="table_schema", text="SELECT 1", is_mounted=True)

    screen.query_one.side_effect = lookup
    assert not app._scene_state_ready()
    app._mark_scene_ready()
    assert not app.creation_scene_ready


@pytest.mark.parametrize("state", ["empty", "wrong", "unmounted", "notice", "valid", "no_error"])
def test_invalid_create_readiness_requires_incomplete_rows_and_real_notice(state: str) -> None:
    app, screen = create_app()
    screen.values.side_effect = None
    screen.values.return_value = {"rows": [("id", "")]}
    notice = Mock()
    notice.is_mounted = True
    notice.renderable = Text("Each column needs a name and SQL type")
    screen.query_one.return_value = notice
    if state == "empty":
        app.screen_stack = []
    elif state == "wrong":
        app.screen = SimpleNamespace(is_mounted=True)
    elif state == "unmounted":
        screen.is_mounted = False
    elif state == "notice":
        screen.query_one.side_effect = NoMatches("notice absent")
    elif state == "valid":
        screen.values.return_value = {"rows": [("id", "BIGINT")]}
    else:
        notice.renderable = Text("")
    assert not app._scene_state_ready()


def test_invalid_create_readiness_accepts_validated_incomplete_row() -> None:
    app, screen = create_app()
    screen.values.side_effect = None
    screen.values.return_value = {"rows": [("id", "")]}
    notice = Mock()
    notice.is_mounted = True
    notice.renderable = Text("Each column needs a name and SQL type")
    screen.query_one.return_value = notice
    assert app._scene_state_ready()


@pytest.mark.parametrize("kind", ["save", "create"])
@pytest.mark.parametrize("state", ["empty", "inactive", "unmounted", "missing"])
def test_delayed_configuration_after_teardown_never_marks_ready(kind: str, state: str) -> None:
    app, screen = save_app("save-changes") if kind == "save" else create_app()
    if state == "empty":
        app.screen_stack = []
    elif state == "inactive":
        app.screen = object()
    elif state == "unmounted":
        screen.is_mounted = False
    else:
        screen.query_one.side_effect = NoMatches("removed control")
    if kind == "save":
        app._focus_save_choice(screen)
    else:
        app._configure_creation_scene(screen, "create-table-invalid")
    assert not app.creation_scene_ready
    screen.call_after_refresh.assert_not_called()


@pytest.mark.parametrize("error", [NoMatches("gone"), ScreenStackError("gone")])
def test_stack_teardown_is_unready(error: Exception) -> None:
    class ClosingApp(ReadinessApp):
        @property
        def screen(self) -> object:
            raise error

        @screen.setter
        def screen(self, value: object) -> None:
            pass

    app = ClosingApp("save-changes", object())
    assert not app._scene_state_ready()
    app._focus_save_choice(Mock(spec=SaveChangesScreen))
    app._configure_creation_scene(Mock(spec=CreateTableScreen), "create-table-invalid")


@pytest.mark.parametrize("operation", ["readiness", "save_callback", "create_callback"])
def test_unexpected_errors_propagate(operation: str) -> None:
    app, screen = create_app() if operation == "create_callback" else save_app("save-changes")
    screen.query_one.side_effect = RuntimeError("unexpected defect")
    with pytest.raises(RuntimeError, match="unexpected defect"):
        if operation == "readiness":
            app._scene_state_ready()
        elif operation == "save_callback":
            app._focus_save_choice(screen)
        else:
            app._configure_creation_scene(screen, "create-table-invalid")


@pytest.mark.parametrize("scene", ["save-changes", "save-changes-cancel"])
def test_delayed_save_focus_marks_ready_only_after_requested_focus(scene: str) -> None:
    app, screen = save_app(scene)
    button = screen.query_one.return_value
    app.focused = None
    button.focus.side_effect = lambda: setattr(app, "focused", button)
    screen.call_after_refresh.side_effect = lambda callback: callback()
    app._focus_save_choice(screen)
    assert app.creation_scene_ready
    assert app.focused is button


@pytest.mark.parametrize(
    "scene", ["create-table-schema", "create-table-types", "create-table-advanced", "create-table-sql"]
)
def test_all_creation_branches_require_available_controls(scene: str) -> None:
    app, screen = create_app()
    app.visual_scene_id = scene
    screen.values.side_effect = NoMatches("required control absent")
    assert not app._scene_state_ready()
    app._configure_creation_scene(screen, scene)
    assert not app.creation_scene_ready
    screen.call_after_refresh.assert_not_called()


@pytest.mark.parametrize("kind", ["save", "create"])
def test_queued_readiness_callback_rechecks_after_teardown(kind: str) -> None:
    app, screen = save_app("save-changes") if kind == "save" else create_app()
    if kind == "save":
        app._focus_save_choice(screen)
    else:
        app.visual_scene_id = "create-table-schema"
        app._configure_creation_scene(screen, app.visual_scene_id)
    callback = screen.call_after_refresh.call_args.args[0]
    screen.is_mounted = False
    callback()
    assert not app.creation_scene_ready


@pytest.mark.parametrize("mounted", [False, True])
def test_evidence_refresh_requires_mounted_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mounted: bool
) -> None:
    app = SimpleNamespace(screen_stack=[object()], screen=SimpleNamespace(is_mounted=mounted))
    write = Mock()
    monkeypatch.setattr(scenes, "_write_evidence", write)
    refreshed = scenes._refresh_evidence_if_mounted(
        cast("Any", app), "save-changes", tmp_path / "geometry.json", tmp_path / "manifest"
    )
    assert refreshed is mounted
    assert write.call_count == int(mounted)


@pytest.mark.parametrize("error", [NoMatches("gone"), ScreenStackError("gone")])
def test_evidence_refresh_handles_stack_lookup_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    class ClosingApp:
        @property
        def screen_stack(self) -> object:
            raise error

    write = Mock()
    monkeypatch.setattr(scenes, "_write_evidence", write)
    assert not scenes._refresh_evidence_if_mounted(
        cast("Any", ClosingApp()),
        "save-changes",
        tmp_path / "geometry.json",
        tmp_path / "manifest",
    )
    write.assert_not_called()


def test_evidence_refresh_propagates_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = SimpleNamespace(screen_stack=[object()], screen=SimpleNamespace(is_mounted=True))
    monkeypatch.setattr(scenes, "_write_evidence", Mock(side_effect=RuntimeError("unexpected defect")))
    with pytest.raises(RuntimeError, match="unexpected defect"):
        scenes._refresh_evidence_if_mounted(
            cast("Any", app), "save-changes", tmp_path / "geometry.json", tmp_path / "manifest"
        )


class MountedNotice(Static):
    @property
    def is_mounted(self) -> bool:
        return True


def validated_app() -> tuple[ReadinessApp, Mock, MountedNotice]:
    app, screen = create_app()
    screen.values.side_effect = None
    screen.values.return_value = {"rows": [("customer_id", ""), ("revenue", "NUMERIC(18,2)")]}
    notice = MountedNotice(Text(""))
    control = SimpleNamespace(is_mounted=True)
    screen.query_one.side_effect = lambda selector, *_args: (
        notice if selector == "#create-table-notice" else control
    )
    return app, screen, notice


def test_delayed_validation_is_polled_without_resubmitting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def exercise() -> None:
        app, screen, notice = validated_app()
        lookup = screen.query_one.side_effect
        submit = Mock(is_mounted=True)
        screen.query_one.side_effect = lambda selector, *args: (
            submit if selector == "#create-table-submit" else lookup(selector, *args)
        )
        app._configure_creation_scene(screen, app.visual_scene_id)
        assert app.scene_configured
        assert not app.creation_scene_ready
        submit.press.assert_called_once()
        app.visual_evidence_path = tmp_path / "geometry.json"
        app.visual_manifest_path = tmp_path / "manifest"
        refresh = Mock()
        monkeypatch.setattr(scenes, "_refresh_evidence_if_mounted", refresh)
        app._refresh_visual_evidence()
        refresh.assert_not_called()
        notice.update(Text("Each column needs a name and SQL type"))
        app._refresh_visual_evidence()
        assert app.creation_scene_ready
        refresh.assert_called_once()
        assert app._scene_readiness_details()["notice_text"] == (
            "Each column needs a name and SQL type"
        )
        submit.press.assert_called_once()
        app._configure_creation_scene(screen, app.visual_scene_id)
        submit.press.assert_called_once()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "content",
    ["Each column needs a name and SQL type", Text("Each column needs a name and SQL type")],
)
def test_invalid_notice_reads_real_static_renderable(content: Any) -> None:
    async def exercise() -> None:
        app, screen, _notice = validated_app()
        notice = MountedNotice(content)
        lookup = screen.query_one.side_effect
        screen.query_one.side_effect = lambda selector, *args: (
            notice if selector == "#create-table-notice" else lookup(selector, *args)
        )
        assert app._scene_state_ready()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "selector", ["#create-table-name", "#create-table-submit", "#create-table-notice"]
)
@pytest.mark.parametrize("state", ["missing", "unmounted"])
def test_configuration_rejects_unavailable_required_control(selector: str, state: str) -> None:
    async def exercise() -> None:
        app, screen, notice = validated_app()
        notice.update(Text("Each column needs a name and SQL type"))
        lookup = screen.query_one.side_effect

        def unavailable(requested: str, *args: object) -> object:
            if requested == selector:
                if state == "missing":
                    raise NoMatches("required control absent")
                return SimpleNamespace(is_mounted=False)
            return lookup(requested, *args)

        screen.query_one.side_effect = unavailable
        app._configure_creation_scene(screen, app.visual_scene_id)
        assert not app.scene_configured
        assert not app._scene_state_ready()
        details = app._scene_readiness_details()
        key = "missing_controls" if state == "missing" else "unmounted_controls"
        assert details[key] == [selector]
        screen.call_after_refresh.assert_not_called()

    asyncio.run(exercise())


def test_poll_rejects_state_loss_after_configuration() -> None:
    async def exercise() -> None:
        app, screen, notice = validated_app()
        notice.update(Text("Each column needs a name and SQL type"))
        app.scene_configured = True
        app._poll_scene_readiness()
        assert app.creation_scene_ready
        screen.is_mounted = False
        app._poll_scene_readiness()
        assert not app.creation_scene_ready

    asyncio.run(exercise())


def test_invalid_notice_rejects_unexpected_renderable() -> None:
    async def exercise() -> None:
        app, screen, _notice = validated_app()
        lookup = screen.query_one.side_effect
        notice = SimpleNamespace(is_mounted=True, renderable=object())
        screen.query_one.side_effect = lambda selector, *args: (
            notice if selector == "#create-table-notice" else lookup(selector, *args)
        )
        with pytest.raises(TypeError, match="Validation notice must contain plain text"):
            app._scene_state_ready()

    asyncio.run(exercise())


def test_configuration_does_not_read_values_of_unmounted_controls() -> None:
    async def exercise() -> None:
        app, screen, _notice = validated_app()
        lookup = screen.query_one.side_effect
        screen.query_one.side_effect = lambda selector, *args: (
            SimpleNamespace(is_mounted=False)
            if selector == "#create-table-name"
            else lookup(selector, *args)
        )
        screen.values.side_effect = RuntimeError("unmounted values accessed")
        app._configure_creation_scene(screen, app.visual_scene_id)
        screen.values.assert_not_called()
        assert not app.scene_configured
        assert not app.creation_scene_ready

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["incomplete", "wrong_value", "clipped"])
def test_selector_failure_revokes_readiness_after_configuration(failure: str) -> None:
    async def exercise() -> None:
        app, _screen, notice = validated_app()
        notice.update(Text("Each column needs a name and SQL type"))
        app.scene_configured = True
        app._poll_scene_readiness()
        assert app.creation_scene_ready
        if failure == "incomplete":
            app.selector_snapshot["collection_complete"] = False
        elif failure == "wrong_value":
            app.selector_snapshot["selector"]["selected_value"] = "from_sql"
        else:
            app.selector_snapshot["selector"]["visibility_ok"] = False
        app._poll_scene_readiness()
        assert not app.creation_scene_ready
        assert not app._scene_state_ready()

    asyncio.run(exercise())


def test_pending_save_configuration_retries_mounting_and_never_restores_later_focus() -> None:
    app, screen = save_app("save-changes-cancel")
    app._pending_scene_screen = screen
    button = screen.query_one.return_value
    button.focus.side_effect = lambda: setattr(app, "focused", button)
    screen.call_after_refresh.side_effect = lambda callback: callback()
    screen.is_mounted = False
    app.focused = None
    app._configure_pending_scene()
    assert not app.scene_configured
    button.focus.assert_not_called()
    screen.is_mounted = True
    app._configure_pending_scene()
    assert app.scene_configured
    assert app.creation_scene_ready
    assert app._pending_scene_screen is None
    button.focus.assert_called_once()
    app.focused = object()
    app._configure_pending_scene()
    app._poll_scene_readiness()
    assert not app.creation_scene_ready
    button.focus.assert_called_once()


def test_pending_invalid_configuration_retries_missing_controls_and_submits_once() -> None:
    async def exercise() -> None:
        app, screen, notice = validated_app()
        app._pending_scene_screen = screen
        lookup = screen.query_one.side_effect
        submit = Mock(is_mounted=True)
        screen.query_one.side_effect = NoMatches("controls still mounting")
        app._configure_pending_scene()
        assert not app.scene_configured
        submit.press.assert_not_called()
        screen.query_one.side_effect = lambda selector, *args: (
            submit if selector == "#create-table-submit" else lookup(selector, *args)
        )
        app._configure_pending_scene()
        assert app.scene_configured
        assert not app.creation_scene_ready
        assert app._pending_scene_screen is None
        submit.press.assert_called_once()
        app._configure_pending_scene()
        notice.update(Text("Each column needs a name and SQL type"))
        app._poll_scene_readiness()
        assert app.creation_scene_ready
        submit.press.assert_called_once()

    asyncio.run(exercise())
