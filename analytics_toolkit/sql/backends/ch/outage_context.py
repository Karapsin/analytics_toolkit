from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

INTERNAL_IO: ContextVar[bool] = ContextVar("ch_outage_internal_io", default=False)
INSERT_KEY: ContextVar[str | None] = ContextVar("ch_outage_insert_key", default=None)
WRITING: ContextVar[bool] = ContextVar("ch_outage_writing", default=False)
LOGICAL_STATISTICS: ContextVar[bool] = ContextVar("ch_logical_statistics", default=False)


@contextmanager
def logical_statistics() -> Iterator[None]:
    """Keep an internal, explicitly scoped statistics query intact during routing."""
    token = LOGICAL_STATISTICS.set(True)
    try:
        yield
    finally:
        LOGICAL_STATISTICS.reset(token)


@contextmanager
def internal_io() -> Iterator[None]:
    token = INTERNAL_IO.set(True)
    try:
        yield
    finally:
        INTERNAL_IO.reset(token)
