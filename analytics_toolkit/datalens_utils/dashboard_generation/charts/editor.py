"""Project-local Editor tab sources applied through public typed SDK setters.

JavaScript stays in editable files. Dataset aliases in Meta are resolved from
the current run's dataset objects, so no live resource IDs belong in scripts.
Stored source verification proves persistence; rendering needs a browser check.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.session import current_session as session

WIRE_TYPES = {
    "advanced_chart": "advanced-chart_node",
    "gravity_charts": "d3_node",
    "markdown": "markdown_node",
    "selector": "control_node",
    "table": "table_node",
}
COMMON_TABS = frozenset({"controls", "meta", "params", "sources"})
TABS = {
    "advanced_chart": COMMON_TABS | {"prepare"},
    "gravity_charts": COMMON_TABS | {"activities", "config", "prepare"},
    "markdown": COMMON_TABS | {"prepare"},
    "selector": COMMON_TABS | {"activities"},
    "table": COMMON_TABS | {"activities", "config", "prepare"},
}


def _renderer(definition: Any) -> Any:
    renderer = definition.get("type")
    if not isinstance(renderer, str) or renderer not in WIRE_TYPES:
        message = f"Unsupported public Editor renderer {renderer!r}."
        raise DataLensUtilsError(message)
    return renderer


def _read_source(relative: Any) -> Any:
    if not isinstance(relative, str) or not relative:
        message = "Editor script references must be nonempty project-relative paths."
        raise DataLensUtilsError(message)
    path = Path(relative)
    if path.is_absolute():
        message = f"Editor script path must be project-relative: {relative!r}."
        raise DataLensUtilsError(message)
    path = (session().paths.project_root / path).resolve()
    if session().paths.project_root.resolve() not in path.parents:
        message = f"Editor script path escapes the project: {relative!r}."
        raise DataLensUtilsError(message)
    if not path.is_file():
        message = f"Editor script does not exist: {relative!r}."
        raise DataLensUtilsError(message)
    # Preserve complete tab text, including its original newline characters.
    with path.open(encoding="utf-8", newline="") as source:
        return source.read()


def _meta(source: Any) -> Any:
    try:
        value = json.loads(source)
    except (TypeError, ValueError) as error:
        message = "Editor Meta must contain JSON, rather than a JavaScript module."
        raise DataLensUtilsError(message) from error
    if not isinstance(value, dict) or not isinstance(value.get("links"), dict):
        message = "Editor Meta must contain a JSON object with an object-valued 'links' member."
        raise DataLensUtilsError(message)
    if any(
        not isinstance(alias, str) or not alias or not isinstance(identifier, str) or not identifier
        for alias, identifier in value["links"].items()
    ):
        message = "Editor Meta link aliases and resource IDs must be nonempty strings."
        raise DataLensUtilsError(message)
    return value


def validate_definition(definition: Any) -> Any:
    """Check renderer tab support and local sources before any cloud writes."""
    renderer = _renderer(definition)
    scripts = definition.get("scripts")
    if not isinstance(scripts, Mapping):
        message = "Editor chart definitions require a scripts object."
        raise DataLensUtilsError(message)
    unknown = set(scripts) - TABS[renderer]
    if unknown:
        message = f"Editor renderer {renderer!r} cannot write tabs {sorted(unknown)!r}."
        raise DataLensUtilsError(message)
    required = TABS[renderer] - {"meta", "activities"}
    missing = required - set(scripts)
    if missing:
        message = (
            f"Editor renderer {renderer!r} requires explicit sources for tabs {sorted(missing)!r}."
        )
        raise DataLensUtilsError(message)
    sources = {tab: _read_source(path) for tab, path in scripts.items()}
    if "meta" in sources:
        _meta(sources["meta"])
    links = definition.get("links")
    if "links" in definition and (
        not isinstance(links, Mapping)
        or any(
            not isinstance(alias, str) or not alias or not isinstance(role, str) or not role
            for alias, role in links.items()
        )
    ):
        message = "Editor links must map nonempty aliases to dataset semantic keys."
        raise DataLensUtilsError(message)
    return sources


def _sources(datasets: Any, definition: Any) -> Any:
    sources = validate_definition(definition)
    if "links" in definition:
        roles = definition["links"]
    elif definition.get("dataset"):
        roles = {"dataset": definition["dataset"]}
    else:
        roles = {}
    links = {}
    for alias, role in roles.items():
        if role not in datasets:
            message = f"Editor link {alias!r} references unknown dataset {role!r}."
            raise DataLensUtilsError(message)
        links[alias] = datasets[role].id
    if "meta" in sources:
        if links and _meta(sources["meta"])["links"] != links:
            message = "Explicit Editor Meta links differ from the configured dataset bindings."
            raise DataLensUtilsError(message)
    elif links:
        sources["meta"] = (
            json.dumps({"links": links}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
    return sources


def getter(client: Any) -> Any:
    """Return the typed getter used by the shared chart lifecycle."""
    return client.get.editor_chart


def validate_change(chart: Any, definition: Any) -> Any:
    """Keep IDs while rejecting unsupported family or renderer transitions."""
    expected = WIRE_TYPES[_renderer(definition)]
    if chart.category != "editor" or chart.wire_type != expected:
        message = (
            "Changing the Editor renderer of "
            f"{definition['name']!r}"
            " requires a supported explicit transition; its resource was "
            "retained."
        )
        raise DataLensUtilsError(message)


def _same_tab(tab: Any, actual: Any, expected: Any, *, explicit_meta: Any) -> Any:
    if tab != "meta" or explicit_meta:
        return actual == expected
    try:
        return json.loads(actual) == json.loads(expected)
    except (TypeError, ValueError):
        return False


def configure(builder: Any, context: Any, datasets: Any, definition: Any) -> Any:  # noqa: ARG001
    """Replace only intended tabs, preserving other stored Editor source."""
    sources = _sources(datasets, definition)
    chart = getattr(builder, "chart", None)
    if chart is not None:
        validate_change(chart, definition)
    current = chart.data if chart is not None else {}
    explicit_meta = "meta" in definition["scripts"]
    for tab, source in sources.items():
        if chart is None or not _same_tab(
            tab, current.get(tab), source, explicit_meta=explicit_meta
        ):
            getattr(builder, tab)(source)
    if "description" in definition and (
        chart is None or chart.description != definition["description"]
    ):
        builder.description(definition["description"])
    return builder


def create_builder(context: Any, datasets: Any, definition: Any, folder: Any) -> Any:
    """Build a complete typed Editor recipe without persisting it."""
    renderer = _renderer(definition)
    supported = context.client.capabilities["chart_factories"]["editor"]
    if renderer not in supported:
        message = f"The installation does not support Editor renderer {renderer!r}."
        raise DataLensUtilsError(message)
    builder = getattr(context.client.create.editor_chart, renderer)(
        name=definition["name"], location=folder
    )
    return configure(builder, context, datasets, definition)


def issues(chart: Any, context: Any, datasets: Any, definition: Any) -> Any:  # noqa: ARG001
    """Compare public chart metadata and exact declared tab-source strings."""
    sources = _sources(datasets, definition)
    problems = []
    if chart.category != "editor" or chart.wire_type != WIRE_TYPES[_renderer(definition)]:
        problems.append("Editor renderer")
    explicit_meta = "meta" in definition["scripts"]
    for tab, source in sources.items():
        if not _same_tab(tab, chart.data.get(tab), source, explicit_meta=explicit_meta):
            problems.append(
                "Meta links" if tab == "meta" and not explicit_meta else f"{tab} tab source"
            )
    if "description" in definition and chart.description != definition["description"]:
        problems.append("description")
    return problems
