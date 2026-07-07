from __future__ import annotations

from collections.abc import Sequence


DEFAULT_CH_ENGINE = "ReplicatedMergeTree"
DEFAULT_CH_CLUSTER = "{cluster}"
DEFAULT_CH_SHARDING_KEY = "rand()"
DEFAULT_CH_PER_HOST_DROP_WORKERS = 5


def normalize_ch_columns_or_expression(
    value: Sequence[str] | str | None,
    option_name: str,
) -> list[str] | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return normalize_ch_string(value, option_name)

    normalized = [normalize_ch_string(column, option_name) for column in value]
    if not normalized:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{option_name} must not contain duplicate column names.")
    return normalized


def normalize_ch_string(value: str, option_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{option_name} must not be empty.")
    return normalized


def validate_ch_options_not_used(
    *,
    target_backend: str,
    option_owner: str,
    partition_by: list[str] | str | None,
    order_by: list[str] | str | None,
    ch_engine: str,
    ch_cluster: str,
    ch_sharding_key: str,
    ch_only_shard: bool = False,
) -> None:
    from ..backends import get_backend_adapter

    get_backend_adapter(target_backend).validate_ch_create_table_options(
        option_owner=option_owner,
        partition_by=partition_by,
        order_by=order_by,
        ch_engine=ch_engine,
        ch_cluster=ch_cluster,
        ch_sharding_key=ch_sharding_key,
        ch_only_shard=ch_only_shard,
    )


def validate_ch_columns_in_columns(
    value: list[str] | str | None,
    columns: Sequence[str],
    option_name: str,
    *,
    data_name: str,
) -> None:
    if value is None or isinstance(value, str):
        return

    available_columns = {str(column) for column in columns}
    missing_columns = [column for column in value if column not in available_columns]
    if missing_columns:
        raise ValueError(
            f"{option_name} columns were not found in the {data_name}: "
            + ", ".join(missing_columns)
        )
