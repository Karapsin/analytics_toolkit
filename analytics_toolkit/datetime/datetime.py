from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal, Union

from dateutil.relativedelta import relativedelta

DatetimeInput = Union[str, date, datetime]
OutputPeriod = Literal["minute", "hour", "day", "week", "month", "quarter"]
OutputInterval = Literal[
    "seconds",
    "minutes",
    "hours",
    "days",
    "weeks",
    "months",
    "quarters",
]


def sanitize_datetime(dt: DatetimeInput) -> str:
    return _to_datetime(dt).strftime("%Y%m%d%H%M%S")


def format_datetime(dt: DatetimeInput) -> str:
    return _to_datetime(dt).strftime("%Y-%m-%d %H:%M:%S")


def add_seconds(
    dt: DatetimeInput,
    n: int,
    output_string: bool = True,
) -> str | datetime:
    return _format_output(_to_datetime(dt) + timedelta(seconds=n), output_string)


def add_minutes(
    dt: DatetimeInput,
    n: int,
    output_string: bool = True,
) -> str | datetime:
    return _format_output(_to_datetime(dt) + timedelta(minutes=n), output_string)


def add_hours(
    dt: DatetimeInput,
    n: int,
    output_string: bool = True,
) -> str | datetime:
    return _format_output(_to_datetime(dt) + timedelta(hours=n), output_string)


def add_days(
    dt: DatetimeInput,
    n: int,
    output_string: bool = True,
) -> str | datetime:
    return _format_output(_to_datetime(dt) + timedelta(days=n), output_string)


def add_weeks(
    dt: DatetimeInput,
    n: int,
    output_string: bool = True,
) -> str | datetime:
    return _format_output(_to_datetime(dt) + timedelta(weeks=n), output_string)


def add_months(
    dt: DatetimeInput,
    n: int,
    output_string: bool = True,
) -> str | datetime:
    return _format_output(_to_datetime(dt) + relativedelta(months=n), output_string)


def add_quarters(
    dt: DatetimeInput,
    n: int,
    output_string: bool = True,
) -> str | datetime:
    return _format_output(_to_datetime(dt) + relativedelta(months=3 * n), output_string)


def is_greater(
    dt: DatetimeInput,
    other_dt: DatetimeInput,
    inclusive: bool = False,
) -> bool:
    left = _to_datetime(dt)
    right = _to_datetime(other_dt)
    if inclusive:
        return left >= right
    return left > right


def is_less(
    dt: DatetimeInput,
    other_dt: DatetimeInput,
    inclusive: bool = False,
) -> bool:
    left = _to_datetime(dt)
    right = _to_datetime(other_dt)
    if inclusive:
        return left <= right
    return left < right


def is_between(
    dt: DatetimeInput,
    start_dt: DatetimeInput,
    end_dt: DatetimeInput,
    inclusive: bool = True,
) -> bool:
    value = _to_datetime(dt)
    start_value = _to_datetime(start_dt)
    end_value = _to_datetime(end_dt)

    if end_value < start_value:
        raise ValueError("end_dt must be greater than or equal to start_dt.")

    if inclusive:
        return start_value <= value <= end_value
    return start_value < value < end_value


def seconds_between(
    start_dt: DatetimeInput,
    end_dt: DatetimeInput,
    inclusive: bool = False,
) -> int:
    return _units_between(start_dt, end_dt, 1, inclusive)


def minutes_between(
    start_dt: DatetimeInput,
    end_dt: DatetimeInput,
    inclusive: bool = False,
) -> int:
    return _units_between(start_dt, end_dt, 60, inclusive)


def hours_between(
    start_dt: DatetimeInput,
    end_dt: DatetimeInput,
    inclusive: bool = False,
) -> int:
    return _units_between(start_dt, end_dt, 60 * 60, inclusive)


def days_between(
    start_dt: DatetimeInput,
    end_dt: DatetimeInput,
    inclusive: bool = False,
) -> int:
    return _units_between(start_dt, end_dt, 24 * 60 * 60, inclusive)


def datetime_bounds(
    dt: DatetimeInput,
    period: str = "day",
    output_string: bool = True,
) -> tuple[str, str] | tuple[datetime, datetime]:
    normalized_period = _normalize_period(period)
    value = _to_datetime(dt)
    return (
        _format_output(_period_start(value, normalized_period), output_string),
        _format_output(_period_end(value, normalized_period), output_string),
    )


def is_period_start(
    dt: DatetimeInput,
    period: str = "day",
) -> bool:
    normalized_period = _normalize_period(period)
    value = _to_datetime(dt)
    return value == _period_start(value, normalized_period)


def is_period_end(
    dt: DatetimeInput,
    period: str = "day",
) -> bool:
    normalized_period = _normalize_period(period)
    value = _to_datetime(dt)
    return value == _period_end(value, normalized_period)


def gen_datetimes_list(
    start_dttm: DatetimeInput,
    end_dttm: DatetimeInput,
    interval: str = "hours",
    output_string: bool = True,
) -> list[str] | list[datetime]:
    start_value = _to_datetime(start_dttm)
    end_value = _to_datetime(end_dttm)
    normalized_interval = _normalize_interval(interval)

    if end_value < start_value:
        return []

    result: list[str] | list[datetime] = []
    current = start_value
    while current <= end_value:
        result.append(_format_output(current, output_string))
        current = _add_interval(current, normalized_interval)

    return result


def _to_datetime(value: DatetimeInput) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        result = datetime.fromisoformat(value)
    else:
        raise TypeError("Datetime value must be a string, date, or datetime.")

    if result.tzinfo is not None and result.utcoffset() is not None:
        raise ValueError("Datetime values must be timezone-naive.")

    return result.replace(microsecond=0)


def _format_output(value: datetime, output_string: bool) -> str | datetime:
    normalized_value = value.replace(microsecond=0)
    if output_string:
        return normalized_value.strftime("%Y-%m-%d %H:%M:%S")
    return normalized_value


def _units_between(
    start_dt: DatetimeInput,
    end_dt: DatetimeInput,
    unit_seconds: int,
    inclusive: bool,
) -> int:
    total_seconds = (_to_datetime(end_dt) - _to_datetime(start_dt)).total_seconds()
    units = int(total_seconds / unit_seconds)
    if not inclusive:
        return units
    if total_seconds < 0:
        return units - 1
    return units + 1


def _normalize_period(period: str) -> OutputPeriod:
    normalized = period.strip().lower()
    if normalized in {"minute", "hour", "day", "week", "month", "quarter"}:
        return normalized  # type: ignore[return-value]
    raise ValueError("period must be one of: 'minute', 'hour', 'day', 'week', 'month', 'quarter'.")


def _normalize_interval(interval: str) -> OutputInterval:
    normalized = interval.strip().lower()
    if normalized in {"second", "seconds"}:
        return "seconds"
    if normalized in {"minute", "minutes"}:
        return "minutes"
    if normalized in {"hour", "hours"}:
        return "hours"
    if normalized in {"day", "days"}:
        return "days"
    if normalized in {"week", "weeks"}:
        return "weeks"
    if normalized in {"month", "months"}:
        return "months"
    if normalized in {"quarter", "quarters"}:
        return "quarters"
    raise ValueError(
        "interval must be one of: 'seconds', 'minutes', 'hours', 'days', "
        "'weeks', 'months', 'quarters'.",
    )


def _period_start(value: datetime, period: OutputPeriod) -> datetime:
    if period == "minute":
        return value.replace(second=0)
    if period == "hour":
        return value.replace(minute=0, second=0)
    if period == "day":
        return value.replace(hour=0, minute=0, second=0)
    if period == "week":
        return _period_start(value, "day") - timedelta(days=value.weekday())
    if period == "quarter":
        quarter_start_month = ((value.month - 1) // 3) * 3 + 1
        return value.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0)
    return value.replace(day=1, hour=0, minute=0, second=0)


def _period_end(value: datetime, period: OutputPeriod) -> datetime:
    if period == "minute":
        return _period_start(value, "minute") + timedelta(minutes=1) - timedelta(seconds=1)
    if period == "hour":
        return _period_start(value, "hour") + timedelta(hours=1) - timedelta(seconds=1)
    if period == "day":
        return _period_start(value, "day") + timedelta(days=1) - timedelta(seconds=1)
    if period == "week":
        return _period_start(value, "week") + timedelta(weeks=1) - timedelta(seconds=1)
    if period == "quarter":
        return _period_start(value, "quarter") + relativedelta(months=3) - timedelta(seconds=1)
    return _period_start(value, "month") + relativedelta(months=1) - timedelta(seconds=1)


def _add_interval(value: datetime, interval: OutputInterval) -> datetime:
    if interval == "seconds":
        return value + timedelta(seconds=1)
    if interval == "minutes":
        return value + timedelta(minutes=1)
    if interval == "hours":
        return value + timedelta(hours=1)
    if interval == "days":
        return value + timedelta(days=1)
    if interval == "weeks":
        return value + timedelta(weeks=1)
    if interval == "quarters":
        return value + relativedelta(months=3)
    return value + relativedelta(months=1)
