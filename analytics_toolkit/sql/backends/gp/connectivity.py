"""Bounded libpq connectivity probing without process-global wait callbacks."""

from __future__ import annotations

import select
import time
from dataclasses import dataclass
from typing import Any

from analytics_toolkit.sql.connection.validation import close_validation_resource

_PROBE_TIMEOUT = "Connectivity probe exceeded its validation budget."
_UNEXPECTED_STATE = "Unexpected libpq connectivity state."
_UNEXPECTED_RESULT = "Connectivity probe returned an unexpected result."


@dataclass
class ValidationConnection:
    _connection: Any
    _atk_validation_timeout: float

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def wait_ready(connection: Any, timeout: float) -> None:
    from psycopg2.extensions import (  # noqa: PLC0415 -- optional driver.
        POLL_OK,
        POLL_READ,
        POLL_WRITE,
    )

    deadline = time.monotonic() + timeout
    while True:
        state = connection.poll()
        if state == POLL_OK:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(_PROBE_TIMEOUT)
        descriptor = connection.fileno()
        if state == POLL_READ:
            ready = select.select([descriptor], [], [], remaining)[0]
        elif state == POLL_WRITE:
            ready = select.select([], [descriptor], [], remaining)[1]
        else:
            raise RuntimeError(_UNEXPECTED_STATE)
        if not ready:
            raise TimeoutError(_PROBE_TIMEOUT)


def validate_connection(adapter: Any, connection: Any) -> None:
    del adapter
    # Real validation connections are asynchronous; injected DB-API fakes and
    # already-ready clients still exercise the same execute-and-consume probe.
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT 1")
        if getattr(connection, "async_", False):
            wait_ready(connection, float(getattr(connection, "_atk_validation_timeout", 5)))
        rows = cursor.fetchall()
        if not rows or rows[0][0] != 1:
            raise RuntimeError(_UNEXPECTED_RESULT)
    finally:
        close_validation_resource(cursor)
