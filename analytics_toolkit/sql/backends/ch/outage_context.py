from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

INTERNAL_IO: ContextVar[bool] = ContextVar("ch_outage_internal_io", default=False)
INSERT_KEY: ContextVar[str | None] = ContextVar("ch_outage_insert_key", default=None)
WRITING: ContextVar[bool] = ContextVar("ch_outage_writing", default=False)


@contextmanager
def internal_io() -> Iterator[None]:
    token = INTERNAL_IO.set(True)
    try:
        yield
    finally:
        INTERNAL_IO.reset(token)
