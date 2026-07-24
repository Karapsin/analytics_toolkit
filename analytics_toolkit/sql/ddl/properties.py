from __future__ import annotations

# ruff: noqa: EM102, I001, TRY003

import re
from collections.abc import Mapping, Sequence
from typing import Any


def merge_ddl_properties(*layers: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            normalized = key.lower()
            if value is None:
                merged.pop(normalized, None)
            else:
                merged[normalized] = value
    return merged


def render_ddl_properties(properties: Mapping[str, Any], *, spaced: bool = False) -> str:
    separator = " = " if spaced else "="
    return ",\n        ".join(
        f"{key}{separator}{render_ddl_property_value(value)}" for key, value in properties.items()
    )


def render_ddl_property_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence):
        return "ARRAY[" + ", ".join(_render_array_item(item) for item in value) + "]"
    raise TypeError(f"Unsupported DDL property value: {value!r}")


def _render_array_item(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return render_ddl_property_value(value)


def overlay_with_properties(sql: str, overrides: Mapping[str, Any]) -> str:
    if not overrides:
        return sql
    bounds = _with_body_bounds(sql)
    if bounds is None:
        return sql
    body_start, body_end, clause_start = bounds
    body = sql[body_start:body_end]
    existing: dict[str, str] = {}
    for entry in _split_entries(body):
        key, separator, value = entry.partition("=")
        if separator:
            existing[key.strip().lower()] = value.strip()
    merged = merge_ddl_properties(existing, overrides)
    replacement = "WITH (" + render_ddl_properties(merged, spaced=" = " in body) + ")"
    return sql[:clause_start] + replacement + sql[body_end + 1 :]


def _with_body_bounds(sql: str) -> tuple[int, int, int] | None:
    match = re.search(r"\bWITH\s*\(", sql, flags=re.IGNORECASE)
    if match is None:
        return None
    depth = 1
    quote: str | None = None
    for index in range(match.end(), len(sql)):
        char = sql[index]
        if quote:
            if char == quote and sql[index - 1] != "\\":
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return match.end(), index, match.start()
    return None


def _split_entries(value: str) -> list[str]:
    entries: list[str] = []
    start = depth = 0
    quote: str | None = None
    for index, char in enumerate(value):
        if quote:
            if char == quote and (index == 0 or value[index - 1] != "\\"):
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            entries.append(value[start:index].strip())
            start = index + 1
    entries.append(value[start:].strip())
    return [entry for entry in entries if entry]
