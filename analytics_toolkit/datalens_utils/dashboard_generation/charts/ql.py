"""Parameterized SQL charts translated through the public typed QL surface.

This module reads and saves SQL text; it never executes queries or reads rows.
The dashboard widget owns its title because QL has no typed title setter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from datalens_sdk import FieldsProxy, QLColumn, QLParam

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session

VISUALIZATIONS = {
    "area": "area",
    "area_100p": "area100p",
    "bar": "bar",
    "bar_100p": "bar100p",
    "column": "column",
    "column_100p": "column100p",
    "donut": "donut",
    "flat_table": "flatTable",
    "indicator": "metric",
    "line": "line",
    "pie": "pie",
    "scatter": "scatter",
    "treemap": "treemap",
}
PLACEMENTS = {
    "area": ("x", "y"),
    "area_100p": ("x", "y"),
    "bar": ("y", "x"),
    "bar_100p": ("y", "x"),
    "column": ("x", "y"),
    "column_100p": ("x", "y"),
    "donut": ("dimensions", "colors", "measures"),
    "flat_table": ("flat_table_columns",),
    "indicator": ("measures", "colors"),
    "line": ("x", "y", "y2"),
    "pie": ("dimensions", "colors", "measures"),
    "scatter": ("x", "y", "points", "size"),
    "treemap": ("dimensions", "measures"),
}
DECORATIONS = {
    kind: (
        ("colors", "tooltips")
        if kind == "flat_table"
        else ("tooltips",)
        if kind == "indicator"
        else ("colors", "shapes", "tooltips")
        if kind == "scatter"
        else ("colors", "tooltips")
        if kind == "treemap"
        else ("colors", "labels", "shapes", "tooltips")
        if kind == "line"
        else ("labels", "tooltips")
        if kind in {"pie", "donut"}
        else ("colors", "labels", "tooltips")
    )
    for kind in VISUALIZATIONS
}
REQUIRED = {
    "area": ("x",),
    "area_100p": ("x",),
    "bar": (),
    "bar_100p": (),
    "column": (),
    "column_100p": (),
    "donut": ("measures",),
    "flat_table": ("flat_table_columns",),
    "indicator": ("measures",),
    "line": ("x",),
    "pie": ("measures",),
    "scatter": ("x", "y"),
    "treemap": ("dimensions", "measures"),
}
CAPACITIES = {
    "area": {"x": 1},
    "area_100p": {"x": 1},
    "bar": {"y": 2},
    "bar_100p": {"y": 2},
    "column": {"x": 2},
    "column_100p": {"x": 2},
    "donut": {"dimensions": 1, "colors": 1, "measures": 1},
    "indicator": {"measures": 1},
    "line": {"x": 1},
    "pie": {"dimensions": 1, "colors": 1, "measures": 1},
    "scatter": {"x": 1, "y": 1, "points": 1, "size": 1},
    "treemap": {"measures": 1},
    "flat_table": {},
}
_TABLE_TOKEN = re.compile(r"__TABLE_([A-Z][A-Z0-9_]*)__")
_TABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_PARAMETER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_ALIAS = re.compile(r'\bAS\s+(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))', re.IGNORECASE)


@dataclass(frozen=True)
class _Recipe:
    query: str
    params: tuple[Any, ...]
    slots: dict[str, Any]
    description: str | None


def _kind(definition: Any) -> Any:
    kind = definition.get("type")
    if kind not in VISUALIZATIONS:
        message = f"Unsupported public QL visualization {kind!r}."
        raise DataLensUtilsError(message)
    return kind


def _read_query(relative: Any) -> Any:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        message = "QL query references must be nonempty project-relative paths."
        raise DataLensUtilsError(message)
    path = (session().paths.project_root / relative).resolve()
    if session().paths.project_root.resolve() not in path.parents:
        message = f"QL query path escapes the project: {relative!r}."
        raise DataLensUtilsError(message)
    if not path.is_file():
        message = f"QL query does not exist: {relative!r}."
        raise DataLensUtilsError(message)
    with path.open(encoding="utf-8", newline="") as source:
        query = source.read()
    if not query.strip():
        message = f"QL query is empty: {relative!r}."
        raise DataLensUtilsError(message)
    return query


def _columns(values: Any, aliases: Any) -> Any:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        message = "Each QL field slot must contain a list of SQL aliases or name/cast objects."
        raise DataLensUtilsError(message)
    columns = []
    name: Any
    for value in values:
        if isinstance(value, str):
            name, cast = value, aliases.get(value, "string")
        elif isinstance(value, Mapping) and set(value) <= {"name", "cast"}:
            name = value.get("name")
            cast = value.get("cast", aliases.get(name, "string"))
        else:
            message = "QL columns must be aliases or objects with name and cast."
            raise DataLensUtilsError(message)
        if not isinstance(name, str) or not name:
            message = "QL column names must be nonempty SQL output aliases."
            raise DataLensUtilsError(message)
        columns.append(QLColumn(name, cast=cast))
    return tuple(columns)


def _params(definition: Any, query: Any) -> Any:
    definitions = definition.get("params", [])
    if not isinstance(definitions, Sequence) or isinstance(definitions, (str, bytes)):
        message = "QL params must be a list of name/type/default objects."
        raise DataLensUtilsError(message)
    params = []
    names: set[str] = set()
    expected: set[str] = set()
    for value in definitions:
        if not isinstance(value, Mapping) or set(value) != {"name", "type", "default"}:
            message = "QL parameters require exactly name, type, and default."
            raise DataLensUtilsError(message)
        name, kind, default = value["name"], value["type"], value["default"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in names
        ):
            message = "QL parameter names must be unique SQL identifiers."
            raise DataLensUtilsError(message)
        names.add(name)
        if kind == "date-interval":
            if (
                not isinstance(default, Mapping)
                or set(default) != {"from", "to"}
                or any(not isinstance(item, str) or not item for item in default.values())
            ):
                message = "QL date-interval defaults require nonempty from and to strings."
                raise DataLensUtilsError(message)
            params.append(QLParam.date_interval(name, default=default))
            expected.update((name + "_from", name + "_to"))
        elif kind in {"number", "string"}:
            if not isinstance(default, str):
                message = "QL string and number parameter defaults must be strings."
                raise DataLensUtilsError(message)
            params.append(getattr(QLParam, kind)(name, default=default))
            expected.add(name)
        else:
            message = f"Unsupported QL parameter type {kind!r}."
            raise DataLensUtilsError(message)
    actual = set(_PARAMETER.findall(query))
    if actual != expected:
        message = (
            "QL SQL parameter references differ from declarations: missing "
            f"{sorted(expected - actual)!r}"
            ", undeclared "
            f"{sorted(actual - expected)!r}"
            "."
        )
        raise DataLensUtilsError(message)
    return tuple(params)


def validate_definition(definition: Any) -> Any:  # noqa: C901, PLR0912
    """Validate local inputs without needing credentials or dataset data."""
    kind = _kind(definition)
    if definition.get("sort") or definition.get("formats"):
        message = "The public QL API has no sort or formatting setters; express ordering in SQL."
        raise DataLensUtilsError(message)
    aliases = definition.get("columns", {})
    if not isinstance(aliases, Mapping) or any(
        not isinstance(name, str) or not name for name in aliases
    ):
        message = "QL columns must map SQL aliases to casts."
        raise DataLensUtilsError(message)
    for name, cast in aliases.items():
        QLColumn(name, cast=cast)
    fields = definition.get("fields", {})
    settings = definition.get("settings", {})
    if not isinstance(fields, Mapping) or not isinstance(settings, Mapping):
        message = "QL fields and settings must be objects."
        raise DataLensUtilsError(message)
    supported = PLACEMENTS[kind] + DECORATIONS[kind]
    if set(fields) - set(supported):
        message = f"QL {kind!r} does not support slots {sorted(set(fields) - set(supported))!r}."
        raise DataLensUtilsError(message)
    description = definition.get("description")
    combined = dict(fields)
    for method, kwargs in settings.items():
        if method == "description" and isinstance(kwargs, Mapping) and set(kwargs) == {"text"}:
            if description is not None and description != kwargs["text"]:
                message = "QL description is defined inconsistently in description and settings."
                raise DataLensUtilsError(message)
            description = kwargs["text"]
        elif method in supported and isinstance(kwargs, Mapping) and set(kwargs) == {"columns"}:
            if method in combined:
                message = f"QL slot {method!r} is defined in both fields and settings."
                raise DataLensUtilsError(message)
            combined[method] = kwargs["columns"]
        else:
            message = (
                f"QL setting {method!r} is not a supported typed column or description setter."
            )
            raise DataLensUtilsError(message)
    if description is not None and not isinstance(description, str):
        message = "QL description must be a string."
        raise DataLensUtilsError(message)
    slots = {slot: _columns(combined.get(slot, []), aliases) for slot in supported}
    for slot in REQUIRED[kind]:
        if not slots[slot]:
            message = f"QL {kind!r} requires a nonempty {slot!r} slot."
            raise DataLensUtilsError(message)
    for slot, capacity in CAPACITIES[kind].items():
        if len(slots[slot]) > capacity:
            message = f"QL {kind!r} supports at most {capacity} columns in {slot!r}."
            raise DataLensUtilsError(message)
    query = _read_query(definition.get("query_file"))
    output_aliases = {next(value for value in groups if value) for groups in _ALIAS.findall(query)}
    missing = {column.name for columns in slots.values() for column in columns} - output_aliases
    if missing:
        message = f"QL slots reference missing explicit SQL output aliases: {sorted(missing)!r}."
        raise DataLensUtilsError(message)
    return _Recipe(query, _params(definition, query), slots, description)


def _recipe(context: Any, definition: Any) -> Any:
    recipe = validate_definition(definition)
    sources = getattr(context, "source_tables", None)
    if sources is None:
        sources = getattr(context, "sources", {})
    if not isinstance(sources, Mapping):
        message = "QL source tables must be a mapping of dataset keys to qualified table names."
        raise DataLensUtilsError(message)

    def replace(match: Any) -> Any:
        role = match.group(1).lower()
        table = sources.get(role)
        if not isinstance(table, str) or not _TABLE_NAME.fullmatch(table):
            message = f"QL source {role!r} needs a qualified database.table identifier."
            raise DataLensUtilsError(message)
        return table

    query = _TABLE_TOKEN.sub(replace, recipe.query)
    if "__TABLE_" in query:
        message = "QL query contains an unresolved table placeholder."
        raise DataLensUtilsError(message)
    return _Recipe(query, recipe.params, recipe.slots, recipe.description)


def getter(client: Any) -> Any:
    return client.get.ql_chart


def validate_change(chart: Any, definition: Any) -> Any:
    """Retain the existing object when a family/type transition is unsupported."""
    if chart.category != "ql" or chart.visualization_id != VISUALIZATIONS[_kind(definition)]:
        message = (
            "Changing the QL visualization of "
            f"{definition['name']!r}"
            " requires a supported explicit transition; its resource was "
            "retained."
        )
        raise DataLensUtilsError(message)


def _actual_columns(chart: Any, kind: Any, slot: Any) -> Any:
    if slot in PLACEMENTS[kind]:
        visualization = chart.data.get("visualization", {})
        placeholders = (
            visualization.get("placeholders", ()) if isinstance(visualization, Mapping) else ()
        )
        identifier = slot.replace("_", "-")
        matches = [
            placeholder
            for placeholder in placeholders
            if isinstance(placeholder, Mapping) and placeholder.get("id") == identifier
        ]
        if len(matches) != 1:
            return None
        items = matches[0].get("items", ())
    else:
        items = chart.data.get(slot, ())
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return None
    if any(not isinstance(item, Mapping) for item in items):
        return None
    return tuple((field.name, field.cast) for field in FieldsProxy(items))


def configure(builder: Any, context: Any, datasets: Any, definition: Any) -> Any:  # noqa: ARG001
    """Apply typed replacements, clearing removed recipe-owned placements."""
    recipe = _recipe(context, definition)
    chart = getattr(builder, "chart", None)
    if chart is not None:
        validate_change(chart, definition)
    connection = chart.connection or {} if chart is not None else {}
    if (
        chart is None
        or connection.get("entryId") != context.connection.id
        or connection.get("type") != context.connection.type
    ):
        builder.connection(context.connection)
    if chart is None or chart.query_value != recipe.query:
        builder.query(recipe.query)
    if chart is None or chart.params != [
        dict(parameter.to_mapping()) for parameter in recipe.params
    ]:
        builder.params(recipe.params)
    kind = _kind(definition)
    for slot, columns in recipe.slots.items():
        expected = tuple((column.name, column.cast) for column in columns)
        if chart is None or _actual_columns(chart, kind, slot) != expected:
            getattr(builder, slot)(columns)
    if recipe.description is not None and (
        chart is None or chart.description != recipe.description
    ):
        builder.description(recipe.description)
    return builder


def create_builder(context: Any, datasets: Any, definition: Any, folder: Any) -> Any:
    """Return a fully configured builder without persisting anything."""
    kind = _kind(definition)
    validate_definition(definition)
    if kind not in context.client.capabilities["chart_factories"]["ql"]:
        message = f"The installation does not support QL visualization {kind!r}."
        raise DataLensUtilsError(message)
    builder = getattr(context.client.create.ql_chart, kind)(
        name=definition["name"], location=folder
    )
    return configure(builder, context, datasets, definition)


def issues(chart: Any, context: Any, datasets: Any, definition: Any) -> Any:  # noqa: ARG001
    """Compare real query, binding, parameters, and each managed QL slot."""
    recipe = _recipe(context, definition)
    kind, problems = _kind(definition), []
    if chart.category != "ql":
        return ["QL chart family"]
    if chart.visualization_id != VISUALIZATIONS[kind]:
        problems.append("QL visualization")
    if chart.query_value != recipe.query:
        problems.append("query text")
    connection = chart.connection or {}
    if (
        connection.get("entryId") != context.connection.id
        or connection.get("type") != context.connection.type
    ):
        problems.append("connection reference")
    if chart.params != [dict(parameter.to_mapping()) for parameter in recipe.params]:
        problems.append("parameters")
    for slot, columns in recipe.slots.items():
        if _actual_columns(chart, kind, slot) != tuple(
            (column.name, column.cast) for column in columns
        ):
            problems.append(f"{slot} columns")
    if recipe.description is not None and (chart.description or "") != recipe.description:
        problems.append("description")
    return problems
