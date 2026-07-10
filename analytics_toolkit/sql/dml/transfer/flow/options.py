from __future__ import annotations

import math
from numbers import Real
from typing import Any

from ....backends import get_backend_adapter
from ....execution.operation_runner import validate_retry_options
from ....execution.validation import (
    validate_non_negative_number,
    validate_optional_positive_int,
    validate_optional_positive_number,
    validate_positive_int,
    validate_positive_number,
)
from ..runtime.models import TrinoTransferMode


_DEFAULT_TARGET_BATCH_SECONDS = 10.0


def validate_transfer_runtime_options(
    *,
    batch_size: Any,
    min_batch_size: Any,
    max_batch_size: Any,
    adaptive_batch_size_step: Any,
    target_batch_seconds: Any,
    min_batch_seconds: Any,
    max_batch_seconds: Any,
    target_batch_memory_mb: Any,
    min_batch_memory_mb: Any,
    max_batch_memory_mb: Any,
    target_rows_per_second_window: Any,
    target_rows_per_second_deadband: Any,
    retry_cnt: Any,
    timeout_increment: Any,
    full_retry_cnt: Any,
    full_timeout_increment: Any,
    gp_insert_chunk_size: Any,
    trino_insert_chunk_size: Any,
    concurrency: Any,
) -> None:
    validate_positive_int(batch_size, "batch_size")
    validate_positive_int(min_batch_size, "min_batch_size")
    validate_optional_positive_int(max_batch_size, "max_batch_size")
    step = validate_positive_number(
        adaptive_batch_size_step,
        "adaptive_batch_size_step",
    )
    if step >= 1:
        raise ValueError(
            "adaptive_batch_size_step must be a finite number greater than 0 "
            "and less than 1."
        )
    for value, option_name in (
        (target_batch_seconds, "target_batch_seconds"),
        (min_batch_seconds, "min_batch_seconds"),
        (max_batch_seconds, "max_batch_seconds"),
        (target_batch_memory_mb, "target_batch_memory_mb"),
        (min_batch_memory_mb, "min_batch_memory_mb"),
        (max_batch_memory_mb, "max_batch_memory_mb"),
    ):
        validate_optional_positive_number(value, option_name)
    validate_positive_int(
        target_rows_per_second_window,
        "target_rows_per_second_window",
    )
    validate_non_negative_number(
        target_rows_per_second_deadband,
        "target_rows_per_second_deadband",
    )
    validate_retry_options(retry_cnt, timeout_increment)
    validate_positive_int(full_retry_cnt, "full_retry_cnt")
    validate_non_negative_number(
        full_timeout_increment,
        "full_timeout_increment",
    )
    validate_optional_positive_int(
        gp_insert_chunk_size,
        "gp_insert_chunk_size",
    )
    validate_optional_positive_int(
        trino_insert_chunk_size,
        "trino_insert_chunk_size",
    )
    validate_positive_int(concurrency, "concurrency")


def resolve_trino_mode(
    trino_mode: TrinoTransferMode | str | None,
    *,
    target_backend: str,
    transfer_staging_schema: str | None,
    transfer_staging_location: str | None,
) -> TrinoTransferMode | None:
    return get_backend_adapter(target_backend).resolve_transfer_staging_mode(
        trino_mode,
        transfer_staging_schema=transfer_staging_schema,
        transfer_staging_location=transfer_staging_location,
    )


def resolve_target_adaptation_mode(
    *,
    adaptive_batch_size: bool,
    target_rows_per_second: bool,
    target_batch_seconds: float | None,
    target_batch_memory_mb: float | None,
) -> bool:
    if not isinstance(target_rows_per_second, bool):
        raise ValueError("target_rows_per_second must be a boolean.")
    if not adaptive_batch_size:
        return target_rows_per_second

    explicit_targets: list[str] = []
    if target_rows_per_second is False:
        explicit_targets.append("target_rows_per_second")
    if target_batch_seconds is not None:
        explicit_targets.append("target_batch_seconds")
    if target_batch_memory_mb is not None:
        explicit_targets.append("target_batch_memory_mb")

    if len(explicit_targets) > 1:
        raise ValueError(
            "Only one transfer batch target may be configured. "
            "Set at most one of target_rows_per_second, target_batch_seconds, "
            "or target_batch_memory_mb."
        )

    if target_batch_memory_mb is not None:
        return False
    if target_batch_seconds is not None:
        return False
    return target_rows_per_second


def resolve_adaptive_batch_bounds(
    *,
    batch_size: int,
    min_batch_size: int,
    max_batch_size: int | None,
    target_batch_seconds: float | None,
    min_batch_seconds: float | None,
    max_batch_seconds: float | None,
    adaptive_batch_size: bool,
    unlimited_default_max: bool = False,
) -> tuple[int, int | None, float, float | None, float | None]:
    if not isinstance(adaptive_batch_size, bool):
        raise ValueError("adaptive_batch_size must be a boolean.")
    if batch_size <= 0:
        raise ValueError("batch_size must be a positive integer.")
    if min_batch_size <= 0:
        raise ValueError("min_batch_size must be a positive integer.")
    if max_batch_size is not None and max_batch_size <= 0:
        raise ValueError("max_batch_size must be a positive integer.")
    if target_batch_seconds is None:
        target_batch_seconds = _DEFAULT_TARGET_BATCH_SECONDS
    try:
        resolved_target_batch_seconds = float(target_batch_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_batch_seconds must be positive.") from exc
    if resolved_target_batch_seconds <= 0:
        raise ValueError("target_batch_seconds must be positive.")

    resolved_min_batch_seconds = resolve_positive_number(
        min_batch_seconds,
        "min_batch_seconds",
    )
    resolved_max_batch_seconds = resolve_positive_number(
        max_batch_seconds,
        "max_batch_seconds",
    )

    if (
        resolved_min_batch_seconds is not None
        and resolved_max_batch_seconds is not None
        and resolved_min_batch_seconds > resolved_max_batch_seconds
    ):
        raise ValueError(
            "min_batch_seconds must be less than or equal to max_batch_seconds."
        )

    if resolved_min_batch_seconds is not None:
        resolved_target_batch_seconds = max(
            resolved_target_batch_seconds,
            resolved_min_batch_seconds,
        )
    if resolved_max_batch_seconds is not None:
        resolved_target_batch_seconds = min(
            resolved_target_batch_seconds,
            resolved_max_batch_seconds,
        )

    resolved_min_batch_size = min_batch_size
    if resolved_min_batch_size > batch_size and min_batch_size == 1_000:
        resolved_min_batch_size = batch_size

    if max_batch_size is None and unlimited_default_max:
        resolved_max_batch_size = None
    else:
        resolved_max_batch_size = (
            batch_size * 4 if max_batch_size is None else max_batch_size
        )
    if resolved_min_batch_size > batch_size:
        raise ValueError("min_batch_size must be less than or equal to batch_size.")
    if resolved_max_batch_size is not None and batch_size > resolved_max_batch_size:
        raise ValueError("max_batch_size must be greater than or equal to batch_size.")
    if (
        resolved_max_batch_size is not None
        and resolved_min_batch_size > resolved_max_batch_size
    ):
        raise ValueError("min_batch_size must be less than or equal to max_batch_size.")
    return (
        resolved_min_batch_size,
        resolved_max_batch_size,
        resolved_target_batch_seconds,
        resolved_min_batch_seconds,
        resolved_max_batch_seconds,
    )


def resolve_target_batch_memory(
    target_batch_memory_mb: float | None,
) -> tuple[float | None, int | None]:
    if target_batch_memory_mb is None:
        return None, None
    if isinstance(target_batch_memory_mb, bool) or not isinstance(
        target_batch_memory_mb,
        Real,
    ):
        raise ValueError("target_batch_memory_mb must be a positive number.")

    resolved_target_batch_memory_mb = float(target_batch_memory_mb)
    if (
        not math.isfinite(resolved_target_batch_memory_mb)
        or resolved_target_batch_memory_mb <= 0
    ):
        raise ValueError("target_batch_memory_mb must be a positive number.")

    return (
        resolved_target_batch_memory_mb,
        max(1, int(resolved_target_batch_memory_mb * 1024 * 1024)),
    )


def resolve_target_batch_memory_limits(
    *,
    min_batch_memory_mb: float | None,
    max_batch_memory_mb: float | None,
) -> tuple[float | None, int | None, float | None, int | None]:
    resolved_min_batch_memory_mb = resolve_positive_number(
        min_batch_memory_mb,
        "min_batch_memory_mb",
    )
    resolved_max_batch_memory_mb = resolve_positive_number(
        max_batch_memory_mb,
        "max_batch_memory_mb",
    )

    if (
        resolved_min_batch_memory_mb is not None
        and resolved_max_batch_memory_mb is not None
        and resolved_min_batch_memory_mb > resolved_max_batch_memory_mb
    ):
        raise ValueError(
            "min_batch_memory_mb must be less than or equal to max_batch_memory_mb."
        )

    resolved_min_batch_memory_bytes = (
        None
        if resolved_min_batch_memory_mb is None
        else max(1, int(resolved_min_batch_memory_mb * 1024 * 1024))
    )
    resolved_max_batch_memory_bytes = (
        None
        if resolved_max_batch_memory_mb is None
        else max(1, int(resolved_max_batch_memory_mb * 1024 * 1024))
    )
    return (
        resolved_min_batch_memory_mb,
        resolved_min_batch_memory_bytes,
        resolved_max_batch_memory_mb,
        resolved_max_batch_memory_bytes,
    )


def resolve_target_rows_per_second_window(
    target_rows_per_second_window: int,
) -> int:
    if isinstance(target_rows_per_second_window, bool) or not isinstance(
        target_rows_per_second_window,
        int,
    ):
        raise ValueError("target_rows_per_second_window must be a positive integer.")
    if target_rows_per_second_window < 1:
        raise ValueError("target_rows_per_second_window must be a positive integer.")
    return target_rows_per_second_window


def resolve_target_rows_per_second_deadband(
    target_rows_per_second_deadband: float,
) -> float:
    if isinstance(target_rows_per_second_deadband, bool) or not isinstance(
        target_rows_per_second_deadband,
        Real,
    ):
        raise ValueError(
            "target_rows_per_second_deadband must be a finite non-negative number."
        )
    resolved_target_rows_per_second_deadband = float(
        target_rows_per_second_deadband,
    )
    if (
        not math.isfinite(resolved_target_rows_per_second_deadband)
        or resolved_target_rows_per_second_deadband < 0
    ):
        raise ValueError(
            "target_rows_per_second_deadband must be a finite non-negative number."
        )
    return resolved_target_rows_per_second_deadband


def resolve_adaptive_batch_size_step(
    adaptive_batch_size_step: float,
) -> float:
    if isinstance(adaptive_batch_size_step, bool) or not isinstance(
        adaptive_batch_size_step,
        Real,
    ):
        raise ValueError(
            "adaptive_batch_size_step must be a finite number greater than 0 "
            "and less than 1."
        )
    resolved_adaptive_batch_size_step = float(adaptive_batch_size_step)
    if (
        not math.isfinite(resolved_adaptive_batch_size_step)
        or resolved_adaptive_batch_size_step <= 0
        or resolved_adaptive_batch_size_step >= 1
    ):
        raise ValueError(
            "adaptive_batch_size_step must be a finite number greater than 0 "
            "and less than 1."
        )
    return resolved_adaptive_batch_size_step


def resolve_positive_number(
    value: float | None,
    label: str,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a positive number.")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return resolved
