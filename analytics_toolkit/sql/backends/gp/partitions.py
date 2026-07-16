from __future__ import annotations

# ruff: noqa: EM101, EM102, TRY003
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Union, cast

from dateutil.relativedelta import relativedelta

from analytics_toolkit.sql.backends.utils import sql_string_literal


@dataclass(frozen=True)
class GpRangePartitionSpec:
    start: str
    end: str
    interval: str


@dataclass(frozen=True)
class GpListPartition:
    name: str
    value: str


@dataclass(frozen=True)
class GpListPartitionSpec:
    partitions: tuple[GpListPartition, ...]


GpPartitionSpec = Union[GpRangePartitionSpec, GpListPartitionSpec]


def normalize_gp_partitions(
    value: Mapping[str, Any] | None,
    *,
    partition_by: Sequence[str] | str | None,
) -> GpPartitionSpec | None:
    if value is None:
        if partition_by is not None:
            raise _invalid_sql_input("Greenplum partition_by requires gp_partitions.")
        return None
    if partition_by is None:
        raise _invalid_sql_input("gp_partitions requires partition_by.")
    normalize_gp_partition_column(partition_by)
    if not isinstance(value, Mapping):
        raise _invalid_sql_input("gp_partitions must be a mapping.")

    keys = set(value)
    if keys == {"start", "end", "interval"}:
        return _normalize_range_spec(value)
    if keys == {"values"}:
        return _normalize_list_spec(value["values"])
    if not keys:
        raise _invalid_sql_input("gp_partitions must not be empty.")
    raise _invalid_sql_input(
        "gp_partitions must contain exactly start, end, and interval, or exactly values."
    )


def normalize_gp_partition_column(partition_by: Sequence[str] | str) -> str:
    if isinstance(partition_by, str):
        return _normalize_non_empty_string(partition_by, "partition_by")
    if isinstance(partition_by, (bytes, Mapping)):
        raise _invalid_sql_input("partition_by for Greenplum must contain exactly one column.")
    try:
        columns = list(partition_by)
    except TypeError as exc:
        raise _invalid_sql_input(
            "partition_by for Greenplum must contain exactly one column."
        ) from exc
    if len(columns) != 1:
        raise _invalid_sql_input("partition_by for Greenplum must contain exactly one column.")
    return _normalize_non_empty_string(columns[0], "partition_by")


def render_gp_partition_clause(
    partition_by: Sequence[str] | str | None,
    spec: GpPartitionSpec | None,
    *,
    quote_identifier: Any,
) -> str:
    if partition_by is None or spec is None:
        return ""
    column = quote_identifier(normalize_gp_partition_column(partition_by), "gp")
    if isinstance(spec, GpRangePartitionSpec):
        return (
            f" PARTITION BY RANGE ({column})\n"
            "(\n"
            f"    START ({sql_string_literal(spec.start)}) INCLUSIVE\n"
            f"    END ({sql_string_literal(spec.end)}) EXCLUSIVE\n"
            f"    EVERY (INTERVAL {sql_string_literal(spec.interval)})\n"
            ")"
        )
    children = ",\n".join(
        f"    PARTITION {partition.name} VALUES ({sql_string_literal(partition.value)})"
        for partition in spec.partitions
    )
    return f" PARTITION BY LIST ({column})\n(\n{children}\n)"


def sanitize_gp_partition_name_token(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z_]+", "_", str(value).strip())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "partition"


def render_gp_partition_name(value: str, name_template: str = "p_{}") -> str:
    return name_template.format(sanitize_gp_partition_name_token(value))


def _normalize_range_spec(value: Mapping[str, Any]) -> GpRangePartitionSpec:
    start = _parse_partition_date(value["start"], "gp_partitions start")
    end = _parse_partition_date(value["end"], "gp_partitions end")
    if end <= start:
        raise _invalid_sql_input("gp_partitions end must be after start.")
    count, unit, interval = _parse_partition_interval(value["interval"])
    current = start
    while current < end:
        current = _increment_partition_date(current, count, unit)
    if current != end:
        raise _invalid_sql_input("gp_partitions interval increments must reach end exactly.")
    return GpRangePartitionSpec(
        start=start.isoformat(),
        end=end.isoformat(),
        interval=interval,
    )


def _normalize_list_spec(value: Any) -> GpListPartitionSpec:
    if isinstance(value, (str, bytes, Mapping)):
        raise _invalid_sql_input("gp_partitions values must be a non-empty sequence of strings.")
    try:
        values = list(value)
    except TypeError as exc:
        raise _invalid_sql_input(
            "gp_partitions values must be a non-empty sequence of strings."
        ) from exc
    if not values:
        raise _invalid_sql_input("gp_partitions values must be a non-empty sequence of strings.")

    partitions: list[GpListPartition] = []
    seen_values: set[str] = set()
    seen_names: set[str] = set()
    for raw_value in values:
        normalized = _normalize_non_empty_string(raw_value, "gp_partitions values")
        if "\x00" in normalized:
            raise _invalid_sql_input("gp_partitions values must not contain NUL bytes.")
        if normalized in seen_values:
            raise _invalid_sql_input("gp_partitions values must not contain duplicates.")
        name = render_gp_partition_name(normalized)
        folded_name = name.lower()
        if folded_name in seen_names:
            raise _invalid_sql_input("gp_partitions values must produce unique partition names.")
        seen_values.add(normalized)
        seen_names.add(folded_name)
        partitions.append(GpListPartition(name=name, value=normalized))
    return GpListPartitionSpec(tuple(partitions))


def _parse_partition_date(value: Any, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise _invalid_sql_input(f"{name} must be an ISO date, date, or datetime.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise _invalid_sql_input(f"{name} must be an ISO date, date, or datetime.") from exc


def _parse_partition_interval(value: Any) -> tuple[int, str, str]:
    if not isinstance(value, str):
        raise _invalid_sql_input(
            "gp_partitions interval must be a positive whole number plus "
            "day(s), week(s), month(s), or year(s)."
        )
    match = re.fullmatch(
        r"([1-9][0-9]*)\s+(day|days|week|weeks|month|months|year|years)",
        value.strip(),
        flags=re.IGNORECASE,
    )
    if match is None:
        raise _invalid_sql_input(
            "gp_partitions interval must be a positive whole number plus "
            "day(s), week(s), month(s), or year(s)."
        )
    count = int(match.group(1))
    unit = match.group(2).lower().rstrip("s")
    rendered_unit = unit if count == 1 else f"{unit}s"
    return count, unit, f"{count} {rendered_unit}"


def _increment_partition_date(value: date, count: int, unit: str) -> date:
    if unit == "day":
        return value + timedelta(days=count)
    if unit == "week":
        return value + timedelta(weeks=count)
    if unit == "month":
        return cast("date", value + relativedelta(months=count))
    if unit == "year":
        return cast("date", value + relativedelta(years=count))
    raise AssertionError(f"Unexpected Greenplum partition interval unit: {unit}")


def _normalize_non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _invalid_sql_input(f"{name} must contain strings.")
    normalized = value.strip()
    if not normalized:
        raise _invalid_sql_input(f"{name} must not contain empty strings.")
    return normalized


def _invalid_sql_input(message: str) -> Exception:
    from analytics_toolkit.sql.connection.errors import (  # noqa: PLC0415
        InvalidSqlInputError,
    )

    return InvalidSqlInputError(message)
