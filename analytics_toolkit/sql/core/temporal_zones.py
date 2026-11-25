"""Canonical IANA/fixed-offset rules shared by value codecs and write validation."""

from __future__ import annotations

import datetime as dt
import re
from importlib import import_module
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

try:
    _zoneinfo = import_module("zoneinfo")
except ImportError:
    _zoneinfo = import_module("backports.zoneinfo")

ZoneInfo = cast("Callable[[str], dt.tzinfo]", _zoneinfo.ZoneInfo)
ZoneInfoNotFoundError = cast("type[Exception]", _zoneinfo.ZoneInfoNotFoundError)


class UnresolvedWallTimeError(ValueError):
    """A naive local value does not identify one physical instant."""


TIMEZONE_POLICY_VERSION = 1
_MINUTE_SECONDS = 60
_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})(?::(\d{2}))?\Z")


def canonical_named_zone(name: str) -> dt.tzinfo:
    match = _OFFSET.fullmatch(name)
    if match:
        hours, minutes, seconds = (int(part or 0) for part in match.groups()[1:])
        if minutes >= _MINUTE_SECONDS or seconds >= _MINUTE_SECONDS:
            msg = "Invalid fixed timestamp timezone offset."
            raise ValueError(msg)
        delta = dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)
        return dt.timezone(delta if match.group(1) == "+" else -delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = "Timestamp timezone name could not be resolved."
        raise ValueError(msg) from exc


timezone_info = canonical_named_zone


def localize_wall(stamp: dt.datetime, zone: dt.tzinfo) -> dt.datetime:
    """Reject ambiguous or nonexistent naive wall times; aware inputs are explicit."""
    first = stamp.replace(tzinfo=zone, fold=0)
    second = stamp.replace(tzinfo=zone, fold=1)
    try:
        valid_first = (
            first.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None) == stamp
        )
        valid_second = (
            second.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None) == stamp
        )
    except OverflowError:
        return first  # Calendar range validation rejects unrepresentable UTC years.
    if not valid_first and not valid_second:
        msg = "Nonexistent destination local timestamp; provide an aware or raw instant."
        raise UnresolvedWallTimeError(msg)
    if valid_first and valid_second and first.utcoffset() != second.utcoffset():
        msg = "Ambiguous destination local timestamp; provide an aware or raw instant."
        raise UnresolvedWallTimeError(msg)
    return first if valid_first else second
