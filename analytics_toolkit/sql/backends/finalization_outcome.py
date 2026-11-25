"""Carry certified final counts without changing backend finalizer return values."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class FinalizationOutcome:
    final_target_rows: int | None = None


_OUTCOME: ContextVar[FinalizationOutcome | None] = ContextVar(
    "analytics_toolkit_finalization_outcome", default=None
)


@contextmanager
def capture_finalization_outcome() -> Iterator[FinalizationOutcome]:
    outcome = FinalizationOutcome()
    token = _OUTCOME.set(outcome)
    try:
        yield outcome
    finally:
        _OUTCOME.reset(token)


def record_final_target_rows(rows: int) -> None:
    """Publish a backend's already validated full-target count to its exact caller."""
    outcome = _OUTCOME.get()
    if outcome is not None:
        outcome.final_target_rows = rows


__all__ = ["FinalizationOutcome", "capture_finalization_outcome", "record_final_target_rows"]
