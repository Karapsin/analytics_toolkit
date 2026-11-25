"""Decode exact GP clock and interval text before psycopg2 normalizes it."""

from __future__ import annotations

import re
from typing import Any, NoReturn

from analytics_toolkit.sql.backends.native_scalars import ExactInterval

_CLOCK = re.compile(r"([+-]?)([0-9]+):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?", re.ASCII)
_UNIT = re.compile(
    r"([+-]?[0-9]+(?:\.[0-9]{1,6})?)\s*"
    r"(years?|months?|mons?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)",
    re.ASCII,
)
_ISO = re.compile(
    r"([+-]?)P(?:(?P<years>[+-]?[0-9]+)Y)?(?:(?P<months>[+-]?[0-9]+)M)?"
    r"(?:(?P<days>[+-]?[0-9]+)D)?(?:T(?:(?P<hours>[+-]?[0-9]+)H)?"
    r"(?:(?P<minutes>[+-]?[0-9]+)M)?(?:(?P<seconds>[+-]?[0-9]+(?:\.[0-9]{1,6})?)S)?)?",
    re.ASCII,
)
_YEAR_MONTH = re.compile(r"([+-]?)([0-9]+)-([0-9]+)", re.ASCII)
_CLOCK_COMPONENT_BASE = 60


class SourceTemporalError(ValueError):
    """A source temporal value cannot be certified after a lossy decoding."""

    analytics_toolkit_sql_retry_safe = False


def _invalid_temporal() -> NoReturn:
    message = "Cannot preserve the exact native source temporal representation."
    raise SourceTemporalError(message)


def _micros(value: str) -> int:
    sign = -1 if value.startswith("-") else 1
    whole, dot, fraction = value.lstrip("+-").partition(".")
    if not whole.isascii() or not whole.isdigit() or (dot and not fraction.isdigit()):
        _invalid_temporal()
    return sign * (int(whole) * 10**6 + int(fraction.ljust(6, "0") or "0"))


def _clock_microseconds(value: str) -> int:
    match = _CLOCK.fullmatch(value)
    if match is None:
        _invalid_temporal()
    sign, hour, minute, second, fraction = match.groups()
    if int(minute) >= _CLOCK_COMPONENT_BASE or int(second) >= _CLOCK_COMPONENT_BASE:
        _invalid_temporal()
    result = (int(hour) * 3600 + int(minute) * 60 + int(second)) * 10**6
    result += int((fraction or "").ljust(6, "0") or "0")
    return -result if sign == "-" else result


def decode_time_text(value: str | None, _cursor: Any) -> Any:
    if value is None:
        return None
    from analytics_toolkit.sql.core import exact_values  # noqa: PLC0415 -- registry cycle.

    micros = _clock_microseconds(value)
    if not 0 <= micros <= 86400 * 10**6:
        _invalid_temporal()
    return exact_values.ExactClock(micros, 6)


def _iso_components(value: str) -> tuple[int, int, int]:
    match = _ISO.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        _invalid_temporal()
    fields = {name: token or "0" for name, token in match.groupdict().items()}
    sign = -1 if match[1] == "-" else 1
    return (
        sign * (int(fields["years"]) * 12 + int(fields["months"])),
        sign * int(fields["days"]),
        sign
        * (
            int(fields["hours"]) * 3600 * 10**6
            + int(fields["minutes"]) * 60 * 10**6
            + _micros(fields["seconds"])
        ),
    )


def _unit_components(value: str) -> tuple[int, int, int]:
    negate = value.endswith(" ago")
    text = value[2:] if value.startswith("@ ") else value
    text = (text[:-4] if negate else text).strip()
    if text == "0":
        return 0, 0, 0
    months = days = micros = 0
    index = 0
    for match in _UNIT.finditer(text):
        if text[index : match.start()].strip():
            _invalid_temporal()
        number, unit = match.groups()
        if unit.startswith(("year", "mon", "day")):
            if "." in number:
                _invalid_temporal()
            if unit.startswith("year"):
                months += int(number) * 12
            elif unit.startswith("mon"):
                months += int(number)
            else:
                days += int(number)
        else:
            multiplier = (
                3600 if unit.startswith(("hour", "hr")) else 60 if unit.startswith(("min",)) else 1
            )
            micros += _micros(number) * multiplier
        index = match.end()
    remainder = text[index:].strip()
    if remainder:
        micros += _clock_microseconds(remainder)
    sign = -1 if negate else 1
    return sign * months, sign * days, sign * micros


def _standard_components(value: str) -> tuple[int, int, int]:
    tokens = value.split()
    if not tokens:
        _invalid_temporal()
    months = days = micros = 0
    inherited_sign = -1 if tokens[0].startswith("-") else 1
    seen = set()
    for token in tokens:
        match = _YEAR_MONTH.fullmatch(token)
        sign = -1 if token.startswith("-") else 1 if token.startswith("+") else inherited_sign
        if match:
            field = "months"
            months = sign * (int(match[2]) * 12 + int(match[3]))
        elif ":" in token:
            field = "micros"
            micros = sign * abs(_clock_microseconds(token))
        elif re.fullmatch(r"[+-]?[0-9]+", token, flags=re.ASCII):
            field = "days"
            days = sign * abs(int(token))
        else:
            _invalid_temporal()
        if field in seen:
            _invalid_temporal()
        seen.add(field)
    return months, days, micros


def decode_interval_text(value: str | None, _cursor: Any) -> ExactInterval | None:
    if value is None:
        return None
    text = value.strip()
    if text.lstrip("+-").startswith("P"):
        months, days, micros = _iso_components(text)
    elif re.search(r"[A-Za-z@]", text):
        months, days, micros = _unit_components(text)
    else:
        months, days, micros = _standard_components(text)
    if not (-(2**31) <= months < 2**31 and -(2**31) <= days < 2**31 and -(2**63) <= micros < 2**63):
        _invalid_temporal()
    return ExactInterval(months, days, micros)
