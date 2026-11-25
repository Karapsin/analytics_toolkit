"""Integer Gregorian calendar coordinates beyond Python's datetime range."""

from __future__ import annotations

_MONTHS_PER_YEAR = 12
_FEBRUARY = 2
_MARCH_BASED_JANUARY = 10
_MAX_FOUR_DIGIT_YEAR = 9999


def days_from_civil(year: int, month: int, day: int) -> int:
    """Return exact days since 1970-01-01, including year zero and signed years."""
    if any(type(value) is not int for value in (year, month, day)):
        message = "Calendar coordinates must be integers."
        raise TypeError(message)
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    lengths = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not 1 <= month <= _MONTHS_PER_YEAR or not 1 <= day <= lengths[month - 1]:
        message = "Invalid Gregorian calendar fields."
        raise ValueError(message)
    shifted = year - (month <= _FEBRUARY)
    era, within = divmod(shifted, 400)
    march_month = month - 3 if month > _FEBRUARY else month + 9
    ordinal = (153 * march_month + 2) // 5 + day - 1
    return era * 146097 + within * 365 + within // 4 - within // 100 + ordinal - 719468


def civil_from_days(days: int) -> tuple[int, int, int]:
    """Invert :func:`days_from_civil` without bounded datetime constructors."""
    if type(days) is not int:
        message = "Calendar day coordinates must be integers."
        raise TypeError(message)
    era, day_of_era = divmod(days + 719468, 146097)
    year_of_era = (
        day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096
    ) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    march_month = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * march_month + 2) // 5 + 1
    month = march_month + 3 if march_month < _MARCH_BASED_JANUARY else march_month - 9
    return year + (month <= _FEBRUARY), month, day


def iso_date(year: int, month: int, day: int) -> str:
    """Render expanded ISO years while retaining ordinary four-digit dates."""
    days_from_civil(year, month, day)
    label = (
        f"{year:04d}"
        if 0 <= year <= _MAX_FOUR_DIGIT_YEAR
        else f"{'+' if year > 0 else '-'}{abs(year):04d}"
    )
    return f"{label}-{month:02d}-{day:02d}"
