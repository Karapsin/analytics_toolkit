"""Bounded scene-state waits shared by service-free visual regressions."""

from __future__ import annotations

from typing import Any


def _form_text_ready(app: Any) -> bool:
    scene = getattr(app, "visual_scene_id", "")
    if not scene.startswith("create-table"):
        return True
    from agent_tools.sql_explorer_render_diagnostics import terminal_diagnostics  # noqa: PLC0415

    flags = terminal_diagnostics(app, scene)["regions"][:3]
    return len(flags) == 3 and all(
        region[layer]
        and region[layer][0]["text"].rstrip()
        == f"{region['state']['button_text']} {region['state']['label']}"
        for region in flags
        for layer in ("widget_rows", "composed_rows")
    )


async def wait_for_scene(app: Any, pilot: Any) -> None:
    """Wait for mounted, configured state and drain composition before observing it."""
    from agent_tools.sql_explorer_visual_capture import _scene_ready  # noqa: PLC0415

    for _attempt in range(100):
        await pilot.pause(0.1)
        if _scene_ready(app):
            await pilot.pause()
            if _scene_ready(app) and _form_text_ready(app):
                return
    message = f"Scene {app.visual_scene_id} did not reach its required state"
    raise AssertionError(message)
