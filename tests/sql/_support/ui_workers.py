"""Wait for Textual workers and the UI callbacks that may replace them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual.worker import WorkerCancelled

if TYPE_CHECKING:
    from textual.pilot import Pilot
    from textual.worker_manager import WorkerManager


async def wait_for_ui_workers(pilot: Pilot, workers: WorkerManager) -> None:
    async def settle() -> None:
        while True:
            await pilot.pause()
            try:
                await workers.wait_for_complete()
            except WorkerCancelled:
                # Exclusive workers cancel obsolete selections; follow their replacements.
                continue
            await pilot.pause()
            if not list(workers):
                return

    await asyncio.wait_for(settle(), timeout=10)
