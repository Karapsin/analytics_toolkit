from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from analytics_toolkit.general import time_print

from .cancellation import raise_if_cancelled
from .operation_runner import _format_duration


T = TypeVar("T")


def run_timed_query(backend: str, action: Callable[[], T]) -> T:
    raise_if_cancelled()
    started_at = time.perf_counter()
    status = "failed"
    try:
        result = action()
    except Exception:
        raise
    else:
        status = "success"
        return result
    finally:
        elapsed_seconds = time.perf_counter() - started_at
        message_prefix = "Failed SQL query" if status == "failed" else "Finished SQL query"
        time_print(
            f"{message_prefix} in {_format_duration(elapsed_seconds)}",
            backend=backend,
        )
