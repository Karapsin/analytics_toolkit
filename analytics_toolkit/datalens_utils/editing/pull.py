from __future__ import annotations

import copy
from typing import Any

from analytics_toolkit.datalens_utils.dashboard_generation.charts import editor, ql, wizard
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.validation.dashboard import normalized_edges, selector_default


def items(section: Any, slot: Any) -> Any:
    return section.get(slot, {}).get("items", [])


def field_names(datasets: Any, chart: Any = None) -> Any:
    names = {field.guid: field.title for dataset in datasets.values() for field in dataset.fields}
    if chart is not None:
        names.update(
            {guid: value["title"] for guid, value in wizard.registered_local_fields(chart).items()}
        )
    return names


def pull_chart(chart: Any, definition: Any, datasets: Any, context: Any) -> Any:  # noqa: C901, PLR0912, PLR0915
    result, assets = copy.deepcopy(definition), {}
    result["name"] = chart.name
    if "description" in result or chart.description:
        result["description"] = chart.description or ""
    family = definition["family"]
    if family == "editor":
        editor.validate_change(chart, definition)
        for tab, relative in definition["scripts"].items():
            if tab not in chart.data:
                message = f"Editor tab {tab!r} is missing in {chart.id}."
                raise DataLensUtilsError(message)
            assets[relative] = chart.data[tab]
        return result, assets
    if family == "ql":
        ql.validate_change(chart, definition)
        current_query = ql._recipe(context, definition).query  # noqa: SLF001
        if chart.query_value != current_query:
            # A changed remote query is imported verbatim. Unchanged template
            # assets retain their deployment table tokens and formatting.
            assets[definition["query_file"]] = chart.query_value
        result["params"] = [
            {"name": value["name"], "type": value["type"], "default": value.get("defaultValue")}
            for value in chart.params
        ]
        result["settings"] = {
            key: value
            for key, value in result.get("settings", {}).items()
            if key not in ql.PLACEMENTS[definition["type"]] + ql.DECORATIONS[definition["type"]]
        }
        result["fields"], columns = {}, dict(result.get("columns", {}))
        for slot in ql.PLACEMENTS[definition["type"]] + ql.DECORATIONS[definition["type"]]:
            values = ql._actual_columns(chart, definition["type"], slot)  # noqa: SLF001
            if values is None:
                message = f"Cannot import QL slot {slot!r}."
                raise DataLensUtilsError(message)
            result["fields"][slot] = [name for name, _ in values]
            columns.update(values)
        result["columns"] = columns
        return result, assets

    if chart.visualization_id != wizard.VISUALIZATIONS[definition["type"]]:
        message = "Pull cannot change a Wizard visualization or resource family."
        raise DataLensUtilsError(message)
    names = field_names(datasets, chart)
    visualization = chart.data.get("visualization", {})
    settings = visualization.get("chartSettings", {})
    if definition["type"] != "indicator" and "title" in settings:
        result["title"] = settings["title"]
        if "show_title" in result or settings.get("titleMode", "show") == "hide":
            result["show_title"] = settings.get("titleMode", "show") != "hide"
    if definition["type"] in {"combined_chart", "geolayer"}:
        # Layer topology has no typed update in SDK 3.1. Never invent a recipe
        # that the incremental publisher cannot safely apply.
        wizard.validate_change(chart, context, datasets, definition)
        return result, assets

    def name(value: Any) -> Any:
        guid = value.get("guid")
        if guid not in names:
            message = f"Unknown field GUID {guid!r}; pull its dataset first."
            raise DataLensUtilsError(message)
        return names[guid]

    slot_aliases = (
        {"x": "dimensions", "y": "measures"}
        if definition["type"] in {"pie", "donut", "treemap", "funnel"}
        else {}
    )
    if definition["type"] == "indicator":
        slot_aliases = {"y": "measures"}
    result["fields"] = {
        slot: [name(value) for value in items(visualization, slot_aliases.get(slot, slot))]
        for slot in definition.get("fields", {})
    }
    result["sort"] = [
        [name(value), value.get("direction", "ASC").lower()]
        for value in items(visualization, "sort")
    ]
    formats = {}
    aliases = []
    for slot in wizard.SLOTS | {"colors", "shapes", "tooltip"}:
        for value in items(visualization, slot):
            if not value.get("guid"):
                continue
            formatting = value.get("formatting", {})
            # measure_format exposes these exact public arguments.
            supported = {
                "format",
                "precision",
                "unit",
                "prefix",
                "postfix",
                "showRankDelimiter",
                "labelMode",
            }
            unsupported = set(formatting) - supported
            if unsupported:
                message = (
                    f"Formatting cannot be expressed through the recipe: {sorted(unsupported)}"
                )
                raise DataLensUtilsError(message)
            if formatting:
                measure_format = {
                    "show_rank_delimiter" if key == "showRankDelimiter" else key: item
                    for key, item in formatting.items()
                    if key != "labelMode"
                }
                if measure_format:
                    formats[name(value)] = measure_format
            if "fakeTitle" in value:
                aliases.append({"field": name(value), "title": value["fakeTitle"]})
    result["formats"] = formats
    result.setdefault("settings", {}).pop("column_title", None)
    if aliases:
        result["settings"]["column_title"] = aliases
    for method, wire, argument in (
        ("freeze_columns", "pinnedColumns", "count"),
        ("table_size", "size", "size"),
    ):
        if wire in settings:
            result["settings"][method] = {argument: settings[wire]}
    for method in ("totals", "pagination"):
        if method in settings:
            result["settings"][method] = {"enabled": settings[method] == "on"}
            if method == "pagination" and "limit" in settings:
                result["settings"][method]["limit"] = settings["limit"]
    if "totals" in result and "totals" in settings:
        result["totals"] = settings["totals"] == "on"
    if "legendMode" in settings:
        result["settings"]["legend"] = {"mode": settings["legendMode"]}
    for axis in ("x", "y", "y2"):
        axis_settings = visualization.get(axis, {}).get("settings", {})
        if axis_settings.get("title") == "on":
            result.setdefault("axis_titles", {})[axis] = axis_settings.get("titleValue", "")
    registered = wizard.registered_local_fields(chart)
    result["local_fields"] = [
        {
            "guid": guid,
            "title": value["title"],
            "formula": value["formula"],
            "cast": value["cast"],
            "kind": value["type"],
            "aggregation": value.get("aggregation", "none"),
        }
        for guid, value in registered.items()
    ]
    filters = chart.data.get("sources", {}).get("filters", [])
    if filters or "filters" in result:
        result["filters"] = [
            {
                "field": name(value),
                "operation": value["filter"]["operation"]["code"],
                "values": value["filter"].get("value", []),
            }
            for value in filters
        ]
    return result, assets


def pull_dataset(dataset: Any, definition: Any) -> Any:
    result = copy.deepcopy(definition)
    result["name"], result["description"] = dataset.name, dataset.description or ""
    for physical, field in result.get("fields", {}).items():
        matches = [
            value
            for value in dataset.fields
            if value.calc_mode == "direct" and value.source == physical
        ]
        if len(matches) != 1:
            message = (
                "Cannot import source column "
                f"{physical!r}"
                "; source changes require a full reconciliation."
            )
            raise DataLensUtilsError(message)
        actual = matches[0]
        field.update(
            title=actual.title, cast=actual.cast, aggregation=actual.aggregation, kind=actual.type
        )
    calculations = {
        field.title: {
            "formula": field.formula,
            "cast": field.cast,
            "kind": field.type,
            "aggregation": field.aggregation,
        }
        for field in dataset.fields
        if field.calc_mode == "formula"
    }
    if isinstance(result.get("calculations"), list):
        result["calculations"] = [{"name": name, **value} for name, value in calculations.items()]
    else:
        result["calculations"] = calculations
    for name, parameter in result.get("parameters", {}).items():
        field = dataset.fields.by_name(name)
        parameter.update(type=field.cast, default=field.default_value)
    return result


def pull_ui(dashboard: Any, files: Any, datasets: Any, chart_definitions: Any) -> Any:  # noqa: C901, PLR0912, PLR0915
    result = copy.deepcopy(files)
    settings = result["configs/DL objects/dashboard.json"]
    settings.update(
        description=(dashboard.raw.get("annotation") or {}).get("description", ""),
        hide_tabs=dashboard.data.get("settings", {}).get("hideTabs", False),
    )
    layouts, tabs = (result["configs/UI/layout.json"], result["configs/UI/tabs.json"])
    params = result["configs/UI/links/chart_params.json"]
    connections = result["configs/UI/links/connections.json"]
    chart_groups = result.get("configs/UI/chart_groups.json", {})
    aliases = result["configs/UI/links/aliases.json"]
    selector_groups = result["configs/UI/selectors/selector_groups.json"]
    fields = {
        field.guid: {"dataset": role, "field": field.title}
        for role, dataset in datasets.items()
        for field in dataset.fields
    }
    selector_files = {
        value["key"]: (relative, value)
        for relative, value in result.items()
        if relative.startswith("configs/UI/selectors/")
        and isinstance(value, dict)
        and value.get("key")
    }
    for tab in dashboard.tabs:
        if tab.id not in tabs:
            continue
        tabs[tab.id].update(title=tab.title, hidden=tab.hidden)
        positions = {
            value.item_id: [
                int(number) if float(number).is_integer() else number
                for number in (value.x, value.y, value.w, value.h)
            ]
            for value in tab.layout
        }
        for key in layouts[tab.id]:
            if key not in positions:
                message = (
                    f"Managed dashboard item {key!r} is missing; structural imports need "
                    f"explicit configuration."
                )
                raise DataLensUtilsError(message)
            layouts[tab.id][key] = positions[key]
        edges = set(normalized_edges(tab))
        for item in (*tab.items, *tab.global_items):
            if item.id in chart_definitions and item.item_type == "widget":
                widget = item.data.get("tabs", [])
                if len(widget) != 1 or widget[0].get("chartId") != chart_definitions[item.id]["id"]:
                    message = f"Widget {item.id!r} changed its chart binding."
                    raise DataLensUtilsError(message)
                result["chart_titles"][item.id] = widget[0].get("title", "")
                values = widget[0].get("params", {})
                if values or item.id in params:
                    params[item.id] = values
            if item.id in chart_groups and item.item_type == "widget":
                configured = {member["key"]: member for member in chart_groups[item.id]["charts"]}
                identifiers = {chart_definitions[key]["id"]: key for key in configured}
                incoming = []
                for widget in item.data.get("tabs", []):
                    key = identifiers.get(widget.get("chartId"))
                    if key is None:
                        message = f"Chart group {item.id!r} has an unknown chart binding."
                        raise DataLensUtilsError(message)
                    member = copy.deepcopy(configured[key])
                    member.update(
                        title=widget.get("title", ""), default=widget.get("isDefault", False)
                    )
                    if widget.get("params") or "params" in member:
                        member["params"] = widget.get("params", {})
                    incoming.append(member)
                if len(incoming) != len(configured) or len(
                    {member["key"] for member in incoming}
                ) != len(configured):
                    message = f"Chart group {item.id!r} changed its members."
                    raise DataLensUtilsError(message)
                chart_groups[item.id]["charts"] = incoming
            for kind in ("titles", "texts"):
                path = f"configs/UI/{kind}.json"
                values = result[path].get(tab.id, {})
                if item.id in values:
                    values[item.id]["text"] = item.data.get("text", "")
                    if kind == "titles":
                        values[item.id]["size"] = item.data.get("size", "m")
        for control in tab.controls:
            if getattr(control, "id", None) in selector_groups:
                item = next(
                    item for item in (*tab.items, *tab.global_items) if item.id == control.id
                )
                group = selector_groups[control.id]
                for public, wire, default in (
                    ("apply_button", "buttonApply", False),
                    ("reset_button", "buttonReset", False),
                    ("update_on_change", "updateControlsOnChange", True),
                    ("show_group_name", "showGroupName", False),
                    ("auto_height", "autoHeight", False),
                ):
                    value = item.data.get(wire, default)
                    if public in group or value != default:
                        group[public] = value
            for member in control.members:
                if member.id not in selector_files:
                    continue
                _, definition = selector_files[member.id]
                definition["title"] = member.title
                if definition["source"]["kind"] == "editor":
                    continue
                actual = member.source
                expected = definition["control"]
                expected["element"] = actual.element_type
                for key, value in (
                    ("multiselect", actual.multiselect),
                    ("is_range", actual.is_range),
                    ("required", actual.required),
                ):
                    if key in expected or value:
                        expected[key] = value
                default = actual.default_value
                if actual.element_type == "checkbox" and default in ("true", "false"):
                    default = default == "true"
                normalized = selector_default(expected.get("default_value"))
                if actual.element_type == "select" and isinstance(normalized, str):
                    normalized = [normalized]
                if default != normalized:
                    expected["default_value"] = default
                if actual.operation is not None:
                    expected["operation"] = actual.operation
                else:
                    expected.pop("operation", None)
                for public, wire in (
                    ("show_title", "showTitle"),
                    ("title_placement", "titlePlacement"),
                    ("inner_title", "innerTitle"),
                    ("hint", "hint"),
                ):
                    defaults = {
                        "show_title": True,
                        "title_placement": "left",
                        "inner_title": None,
                        "hint": None,
                    }
                    if wire in actual.raw and (
                        public in expected or actual.raw[wire] != defaults[public]
                    ):
                        expected[public] = actual.raw[wire]
                if member.source_type == "manual" and actual.element_type == "select":
                    expected["options"] = list(actual.acceptable_values)
                if member.source_type == "dataset":
                    binding = fields.get(actual.dataset_field_id)
                    if binding is None:
                        message = f"Selector {member.id!r} has an unknown dataset field."
                        raise DataLensUtilsError(message)
                    definition["source"].update(binding)
                receivers = set(chart_definitions) | set(chart_groups)
                receivers = {key for key in receivers if key in layouts[tab.id]}
                receivers.update(
                    key
                    for key in connections.get(member.id, [])
                    if key in selector_files and key != member.id
                )
                wanted_receivers = {key for key in receivers if (key, member.id) not in edges}
                previous = connections.get(member.id, [])
                connections[member.id] = [
                    key for key in previous if key in wanted_receivers
                ] + sorted(wanted_receivers - set(previous))
        groups = [
            [fields.get(value, {"parameter": value}) for value in group]
            for group in tab.aliases.get("default", [])
        ]

        def signature(group: Any) -> Any:
            return frozenset(tuple(sorted(value.items())) for value in group)

        previous = aliases.get(tab.id, [])
        if {signature(group) for group in groups} != {signature(group) for group in previous}:
            aliases[tab.id] = groups
    return result
