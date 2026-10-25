"""Bounded scene-state waits shared by service-free visual regressions."""

from __future__ import annotations

from typing import Any


async def wait_for_scene(app: Any, pilot: Any) -> None:
    """Wait for mounted, configured state and drain composition before observing it."""
    from agent_tools.sql_explorer_visual_capture import _scene_ready  # noqa: PLC0415

    for _attempt in range(100):
        await pilot.pause(0.1)
        if _scene_ready(app):
            await pilot.pause()
            if _scene_ready(app):
                return
    message = f"Scene {app.visual_scene_id} did not reach its required state"
    raise AssertionError(message)
