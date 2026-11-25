"""Evaluate clock casts independently of the production clock conversion code."""

from __future__ import annotations

import datetime as dt
import re

from analytics_toolkit.sql.core.exact_values import ExactClock
from sqlglot import exp, parse_one

_CLOCK = re.compile(r"TIME(?:\((\d+)\))?( WITH TIME ZONE)?", re.ASCII)
_CLOCK_TEXT = re.compile(
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,12}))?"
    r"(?: ?([+-][0-9]{2}:[0-9]{2}))?",
    re.ASCII,
)


def is_clock_type(native):
    return _CLOCK.fullmatch(native.upper()) is not None


def _cast(value, native, session_timezone):
    if value is None:
        return None
    spec = _CLOCK.fullmatch(native.upper())
    assert spec is not None, f"Unsupported clock fixture type: {native}"
    precision = int(spec[1]) if spec[1] is not None else 3
    zoned = spec[2] is not None
    text = isinstance(value, str)
    clock = _coordinates(value)
    offset = clock.offset_minutes
    if zoned and offset is None:
        assert not text, "A VARCHAR cast to TIME WITH TIME ZONE requires an offset."
        delta = session_timezone.utcoffset(None)
        offset = int(delta.total_seconds()) // 60
    if precision < clock.scale:
        factor = 10 ** (clock.scale - precision)
        ticks = (clock.ticks + factor // 2) // factor
    else:
        ticks = clock.ticks * 10 ** (precision - clock.scale)
    return ExactClock(ticks % (86400 * 10**precision), precision, offset if zoned else None).time()


def _coordinates(value):
    if isinstance(value, ExactClock):
        return value
    if isinstance(value, dt.time):
        offset = value.utcoffset()
        minutes = int(offset.total_seconds()) // 60 if offset is not None else None
        ticks = ((value.hour * 60 + value.minute) * 60 + value.second) * 10**6
        return ExactClock(ticks + value.microsecond, 6, minutes)
    assert isinstance(value, str), "Clock casts require a clock or VARCHAR input."
    match = _CLOCK_TEXT.fullmatch(value.strip())
    assert match is not None, "Invalid clock fixture VARCHAR input."
    hour, minute, second = (int(part) for part in match.groups()[:3])
    dt.time(hour, minute, second)  # Validate fields before computing ticks.
    digits = match[4] or ""
    offset = match[5]
    minutes = None
    if offset:
        minutes = (int(offset[1:3]) * 60 + int(offset[4:])) * (-1 if offset[0] == "-" else 1)
        assert abs(minutes) <= 14 * 60
        assert int(offset[4:]) < 60
    ticks = (hour * 3600 + minute * 60 + second) * 10 ** len(digits) + int("0" + digits)
    return ExactClock(ticks, len(digits), minutes)


def _wire_value(wire):
    node = parse_one(wire, read="trino")
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Cast) and is_clock_type(node.args["to"].sql(dialect="trino")):
        # The SDK's TIME literal has precision determined by its own fraction.
        return _coordinates(node.this.this).time()
    assert isinstance(node, exp.Literal)
    assert node.is_string
    return node.this


def _evaluate(node, wires, source_value, session_timezone):
    if isinstance(node, exp.Alias):
        return _evaluate(node.this, wires, source_value, session_timezone)
    if isinstance(node, exp.Placeholder):
        return _wire_value(next(wires))
    if isinstance(node, exp.Column):
        assert node.name == "payload", "Unknown clock fixture column."
        return source_value
    if isinstance(node, exp.Cast):
        value = _evaluate(node.this, wires, source_value, session_timezone)
        return _cast(value, node.args["to"].sql(dialect="trino"), session_timezone)
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    message = f"Unsupported clock fixture expression: {type(node).__name__}"
    raise AssertionError(message)


def inserted_clock_values(database, identity, tree, wire):
    native = database.table_types[identity]
    session_timezone = getattr(database, "session_timezone", dt.timezone.utc)
    wires = iter(wire)
    if isinstance(tree.expression, exp.Select):
        query = tree.expression
        assert len(query.expressions) == 1
        source = database.table_key(query.args["from_"].this)
        expressions = [(query.expressions[0], value) for value in database.table_rows[source]]
    else:
        expressions = []
        for row in tree.expression.expressions:
            assert len(row.expressions) == 1
            expressions.append((row.expressions[0], None))
    inserted = [
        _cast(_evaluate(node, wires, value, session_timezone), native, session_timezone)
        for node, value in expressions
    ]
    assert next(wires, None) is None, "Unused clock fixture parameters."
    return inserted
