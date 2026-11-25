"""Internal execution results: keep an unknown affected count distinct from zero."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analytics_toolkit.general import time_print


@dataclass(frozen=True)
class ExecutionOutcome:
    public_result: Any = None
    affected_rows: int | None = None


def close_cursor(cursor: Any, *, backend: str) -> None:
    try:
        cursor.close()
    except Exception as exc:  # noqa: BLE001 - close cannot change the established query outcome
        time_print(
            f"Cursor close failed after SQL outcome was established: {type(exc).__name__}",
            level="warning",
            backend=backend,
            phase="close",
        )
