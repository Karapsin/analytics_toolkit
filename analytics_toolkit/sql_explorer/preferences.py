"""Save explicit UI choices as defaults without changing other tabs' layouts."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .settings import ExplorerSettings, save_settings

if TYPE_CHECKING:
    from .app import SqlExplorerApp
    from .workspace import SqlExplorerWorkspace


def save_preferences(
    app: SqlExplorerApp, workspace: SqlExplorerWorkspace, *, keyboard_only: bool = False
) -> None:
    settings = replace(workspace.session.settings, keyboard_mode=app.keyboard_mode)
    if not keyboard_only:
        settings = replace(
            settings,
            results_orientation=workspace.results_orientation,
            horizontal_size=_size(workspace.result_sizes["horizontal"]),
            vertical_size=_size(workspace.result_sizes["vertical"]),
        )
    for owner in app._workspaces.values():  # noqa: SLF001 -- shared app preference state.
        owner.session.settings = settings
    path = getattr(workspace.session, "settings_path", None)
    if path is not None:
        try:
            save_settings(settings, path)
        except OSError as exc:
            app._set_notice(f"Could not save Explorer preferences: {exc}", workspace)  # noqa: SLF001


def _size(value: int | None) -> int | None:
    return max(0, value) if value is not None else None


def apply_preferences(
    app: SqlExplorerApp, workspace: SqlExplorerWorkspace, settings: ExplorerSettings
) -> None:
    """Save the whole form atomically before updating live application state."""
    path = getattr(workspace.session, "settings_path", None)
    if path is not None:
        save_settings(settings, path)
    for owner in app._workspaces.values():  # noqa: SLF001 -- shared app preference state.
        owner.session.settings = settings
    app.keyboard_mode = settings.keyboard_mode
    if app.keyboard_mode:
        app.capture_mouse(None)
    for owner in app._workspaces.values():  # noqa: SLF001
        app._update_editor_status(owner)  # noqa: SLF001
    app._install_primary_binding(settings.run_binding)  # noqa: SLF001
    workspace.results_orientation = settings.results_orientation
    workspace.result_sizes.update(
        horizontal=settings.horizontal_size, vertical=settings.vertical_size
    )
    workspace.apply_results_layout()
    app._update_all_statuses()  # noqa: SLF001
