from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

INTERNAL_IO: ContextVar[bool] = ContextVar("ch_outage_internal_io", default=False)
INSERT_KEY: ContextVar[str | None] = ContextVar("ch_outage_insert_key", default=None)
WRITING: ContextVar[bool] = ContextVar("ch_outage_writing", default=False)
PRESERVE_SOURCE_SCOPE: ContextVar[bool] = ContextVar("ch_preserve_source_scope", default=False)
LOGICAL_STATISTICS: ContextVar[bool] = ContextVar("ch_logical_statistics", default=False)
STATISTICS_TARGETS: ContextVar[tuple[tuple[str, str, str], ...]] = ContextVar(
    "ch_statistics_targets", default=()
)


@contextmanager
def preserve_source_scope() -> Iterator[None]:
    """Keep verified structural-copy sources in their selected execution scope."""
    token = PRESERVE_SOURCE_SCOPE.set(True)
    try:
        yield
    finally:
        PRESERVE_SOURCE_SCOPE.reset(token)


@contextmanager
def logical_statistics(*, targets: tuple[tuple[str, str, str], ...] = ()) -> Iterator[None]:
    """Keep an internal, explicitly scoped statistics query intact during routing."""
    token = LOGICAL_STATISTICS.set(True)
    targets_token = STATISTICS_TARGETS.set(targets)
    try:
        yield
    finally:
        STATISTICS_TARGETS.reset(targets_token)
        LOGICAL_STATISTICS.reset(token)


@contextmanager
def internal_io() -> Iterator[None]:
    token = INTERNAL_IO.set(True)
    try:
        yield
    finally:
        INTERNAL_IO.reset(token)
