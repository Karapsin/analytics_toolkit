from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from analytics_toolkit.general import time_print

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


def run_ch_readiness_wait(
    operation: Callable[[float], T],
    *,
    timeout_seconds: float,
    extension_cnt: int,
    timeout_increment_seconds: float,
    wait_label: str,
) -> T:
    last_error: TimeoutError | None = None
    for readiness_attempt in range(extension_cnt + 1):
        current_timeout = timeout_seconds if readiness_attempt == 0 else timeout_increment_seconds
        if readiness_attempt > 0:
            time_print(
                "ClickHouse target is still converging; extending "
                f"{wait_label} wait by {timeout_increment_seconds:g} second(s) "
                f"({readiness_attempt}/{extension_cnt})",
                backend="ch",
                phase="validate_target",
            )
        try:
            return operation(current_timeout)
        except TimeoutError as exc:
            last_error = exc
            if readiness_attempt >= extension_cnt or timeout_increment_seconds <= 0:
                break

    if last_error is None:
        message = "ClickHouse readiness failed without capturing an exception."
        raise RuntimeError(message)
    total_timeout = timeout_seconds + extension_cnt * timeout_increment_seconds
    message = (
        f"ClickHouse {wait_label} did not finish within {total_timeout:g} second(s), "
        f"including {extension_cnt} timeout extension(s): {last_error}"
    )
    raise TimeoutError(message) from last_error


__all__ = ["run_ch_readiness_wait"]
