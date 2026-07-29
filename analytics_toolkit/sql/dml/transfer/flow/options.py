from __future__ import annotations

# ruff: noqa: EM101, FBT001, TRY003, TRY004
import math
from numbers import Real
from typing import TYPE_CHECKING, Any, cast

from analytics_toolkit.sql.execution.operation_runner import (
    validate_progress_option,
    validate_retry_options,
)

from ....backends import get_backend_adapter
from ....execution.validation import (
    validate_non_negative_number,
    validate_optional_positive_int,
    validate_optional_positive_number,
    validate_positive_int,
    validate_positive_number,
)
from ..runtime.models import TrinoTransferMode
from .concurrency import CONCURRENCY_CONFLICT_ERROR, validate_concurrency_value

if TYPE_CHECKING:
    from ..runtime.models import TransferOptions

_DEFAULT_TARGET_BATCH_SECONDS = 10.0


def validate_built_transfer_options(options: TransferOptions, target_adapter: Any) -> None:
    if options.from_db_key == options.to_db_key:
        raise ValueError("from_db and to_db must be different.")
    if not options.target_table:
        raise ValueError("to_table must not be empty.")
    if options.write_mode == "upsert" and not options.key_columns:
        raise ValueError("key_columns are required for write_mode='upsert'.")
    if (
        options.write_mode == "upsert"
        and target_adapter.uses_partition_replacement_upsert()
        and options.upsert_partition_column is None
    ):
        raise ValueError(
            "upsert_partition_column is required for write_mode='upsert' "
            "when to_db has type 'trino' or 'ch'."
        )
    if (
        options.write_mode == "upsert"
        and target_adapter.needs_upsert_partition_drop_template()
        and not options.trino_upsert_partition_drop_sql_template
    ):
        raise ValueError(
            "Trino write_mode='upsert' requires upsert_partition_drop_sql_template "
            "in the target connection config."
        )
    validate_progress(options.progress)
    validate_estimate_total_rows(options.estimate_total_rows)
    validate_row_count_options(options.validate_row_count, options.ch_count_limit_read)
    target_adapter.validate_gp_distributed_by_key_option(
        options.gp_distributed_by_key, option_owner="to_db"
    )
    target_adapter.validate_gp_insert_chunk_size_option(
        options.gp_insert_chunk_size, option_owner="to_db"
    )
    target_adapter.validate_trino_insert_chunk_size_option(
        options.trino_insert_chunk_size, option_owner="to_db"
    )
    target_adapter.validate_ch_create_table_options(
        option_owner="to_db",
        partition_by=options.partition_by,
        order_by=options.order_by,
        ch_engine=options.ch_engine,
        ch_cluster=options.ch_cluster,
        ch_sharding_key=options.ch_sharding_key,
        ch_only_shard=options.ch_only_shard,
    )


def resolve_transfer_write_mode(to_db_backend: str, write_mode: str | None) -> str:
    if write_mode is None:
        return "append"
    return cast("str", get_backend_adapter(to_db_backend).validate_write_mode(write_mode))


def validate_progress(progress: bool) -> None:
    validate_progress_option(progress)


def validate_estimate_total_rows(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("estimate_total_rows must be a boolean.")


def validate_row_count_options(validate_row_count: bool, ch_count_limit_read: bool) -> None:
    if not isinstance(validate_row_count, bool):
        raise ValueError("validate_row_count must be a boolean.")
    if not isinstance(ch_count_limit_read, bool):
        raise ValueError("ch_count_limit_read must be a boolean.")


def normalize_only_shard(value: bool) -> bool:
    if not isinstance(value, bool):
        raise ValueError("ch_only_shard must be a boolean.")
    return value


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
    read_concurrency: Any,
    write_concurrency: Any,
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
            "adaptive_batch_size_step must be a finite number greater than 0 and less than 1."
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
    validate_concurrency_value(concurrency, "concurrency")
    validate_concurrency_value(read_concurrency, "read_concurrency")
    validate_concurrency_value(write_concurrency, "write_concurrency")
    if concurrency is not None and (read_concurrency is not None or write_concurrency is not None):
        raise ValueError(CONCURRENCY_CONFLICT_ERROR)


def resolve_trino_mode(
    trino_mode: TrinoTransferMode | str | None,
    *,
    target_backend: str,
    transfer_staging_schema: str | None,
    transfer_staging_location: str | None,
) -> TrinoTransferMode | None:
    return cast(
        "TrinoTransferMode | None",
        get_backend_adapter(target_backend).resolve_transfer_staging_mode(
            trino_mode,
            transfer_staging_schema=transfer_staging_schema,
            transfer_staging_location=transfer_staging_location,
        ),
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
        raise ValueError("min_batch_seconds must be less than or equal to max_batch_seconds.")

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
        resolved_max_batch_size = batch_size * 4 if max_batch_size is None else max_batch_size
    if resolved_min_batch_size > batch_size:
        raise ValueError("min_batch_size must be less than or equal to batch_size.")
    if resolved_max_batch_size is not None and batch_size > resolved_max_batch_size:
        raise ValueError("max_batch_size must be greater than or equal to batch_size.")
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
    if not math.isfinite(resolved_target_batch_memory_mb) or resolved_target_batch_memory_mb <= 0:
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
        raise ValueError("min_batch_memory_mb must be less than or equal to max_batch_memory_mb.")

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
        raise ValueError("target_rows_per_second_deadband must be a finite non-negative number.")
    resolved_target_rows_per_second_deadband = float(
        target_rows_per_second_deadband,
    )
    if (
        not math.isfinite(resolved_target_rows_per_second_deadband)
        or resolved_target_rows_per_second_deadband < 0
    ):
        raise ValueError("target_rows_per_second_deadband must be a finite non-negative number.")
    return resolved_target_rows_per_second_deadband


def resolve_adaptive_batch_size_step(
    adaptive_batch_size_step: float,
) -> float:
    if isinstance(adaptive_batch_size_step, bool) or not isinstance(
        adaptive_batch_size_step,
        Real,
    ):
        raise ValueError(
            "adaptive_batch_size_step must be a finite number greater than 0 and less than 1."
        )
    resolved_adaptive_batch_size_step = float(adaptive_batch_size_step)
    if (
        not math.isfinite(resolved_adaptive_batch_size_step)
        or resolved_adaptive_batch_size_step <= 0
        or resolved_adaptive_batch_size_step >= 1
    ):
        raise ValueError(
            "adaptive_batch_size_step must be a finite number greater than 0 and less than 1."
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
