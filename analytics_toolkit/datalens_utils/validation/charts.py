"""Compare chart recipes with actual public SDK chart read models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, Iterator

from datalens_sdk import DataLensValidationError

from analytics_toolkit.datalens_utils.dashboard_generation.charts import wizard

VISUALIZATIONS = wizard.VISUALIZATIONS


def _subset(actual: Any, expected: Any) -> Any:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key == "settingsId" or (key in actual and _subset(actual[key], value))
            for key, value in expected.items()
        )
    if isinstance(expected, (tuple, list)):
        return (
            isinstance(actual, (tuple, list))
            and len(actual) == len(expected)
            and all(_subset(a, e) for a, e in zip(actual, expected))
        )
    return actual == expected


def _items(section: Any, slot: Any) -> Any:
    value = section.get(slot, {})
    return value.get("items", ()) if isinstance(value, Mapping) else ()


def _guids(section: Any, slot: Any) -> Any:
    return [item.get("guid") for item in _items(section, slot)]


def _snapshots(value: Any) -> Iterator[Any]:
    if isinstance(value, Mapping):
        if isinstance(value.get("guid"), str):
            yield value
        for child in value.values():
            yield from _snapshots(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _snapshots(child)


def _layer_issues(visualization: Any, spec: Any) -> Any:  # noqa: C901, PLR0912
    issues = []
    expected_layers = spec.geo_layers or spec.combined_layers
    actual_layers = visualization.get("layers", ())
    if len(actual_layers) != len(expected_layers):
        return ["layer count"]
    aliases = {
        "geopoint": "points",
        "polygon": "polygons",
        "polyline": "polylines",
        "grouping": "grouping",
        "size": "size",
        "color": "colors",
        "tooltips": "tooltip",
        "labels": "labels",
        "y": "y",
        "y2": "y2",
    }
    for index, (actual, expected) in enumerate(zip(actual_layers, expected_layers)):
        prefix = f"layer {index + 1}"
        if actual.get("type") != expected["layer_type"]:
            issues.append(f"{prefix} type")
        settings = actual.get("layerSettings", {})
        if settings.get("name") != (expected.get("name") or f"Layer {index + 1}"):
            issues.append(f"{prefix} name")
        if spec.geo_layers and settings.get("alpha") != expected["alpha"]:
            issues.append(f"{prefix} opacity")
        if spec.combined_layers and _guids(actual, "x") != [
            field.guid for field in spec.slots.get("x", ())
        ]:
            issues.append(f"{prefix} x fields")
        for public, slot in aliases.items():
            value = expected.get(public)
            fields = (
                value if isinstance(value, (tuple, list)) else (() if value is None else (value,))
            )
            # Heatmaps in the installed generated schema use the density slot.
            if public == "geopoint" and expected["layer_type"] == "heatmap" and slot not in actual:
                slot = "heatmap"  # noqa: PLW2901
            if fields and _guids(actual, slot) != [field.guid for field in fields]:
                issues.append(f"{prefix} {public} fields")
        if spec.geo_layers:
            expected_sort = (
                [(expected["sort_by"].guid, expected["sort_direction"].upper())]
                if expected.get("sort_by")
                else []
            )
            actual_sort = [
                (field.get("guid"), field.get("direction")) for field in _items(actual, "sort")
            ]
            if actual_sort != expected_sort:
                issues.append(f"{prefix} sorting")
            colors = actual.get("colors", {}).get("settings", {})
            for argument, key in (
                ("color_mode", "gradientMode"),
                ("color_palette", "gradientPalette"),
                ("color_reversed", "reversed"),
            ):
                if expected.get(argument) is not None and colors.get(key) != expected[argument]:
                    issues.append(f"{prefix} {argument}")
            expected_filters = [
                (rule.field.guid, rule.operation, tuple(rule.values))
                for rule in expected.get("filters", ())
            ]
            actual_filters = [
                (
                    field.get("guid"),
                    field.get("filter", {}).get("operation", {}).get("code"),
                    tuple(field.get("filter", {}).get("values", ())),
                )
                for field in _items(actual, "filters")
            ]
            if expected_filters != actual_filters:
                issues.append(f"{prefix} filters")
    return issues


def wizard_issues(chart: Any, context: Any, datasets: Any, definition: Any) -> Any:  # noqa: C901, PLR0912, PLR0915
    datasets[definition["dataset"]]
    builder = wizard.create_builder(
        context,
        datasets,
        definition,
        context.resources.folder_for("widget")
        if getattr(context, "resources", None)
        else chart.location,
    )
    spec = builder.to_spec()
    issues = []
    if chart.category != "wizard":
        issues.append("family")
    if chart.visualization_id != spec.visualization_type:
        issues.append("visualization")
    if chart.dataset_ids != spec.dataset_ids:
        issues.append("dataset reference")
    visualization = chart.data.get("visualization", {})
    map_keys = {"mapCenterMode", "mapCenterValue", "zoomMode", "zoomValue"}
    if not _subset(
        visualization.get("chartSettings", {}),
        {key: value for key, value in spec.chart_settings.items() if key not in map_keys},
    ):
        issues.append("chart settings")
    if not _subset(
        visualization.get("chartSettings", {}),
        {key: value for key, value in spec.chart_settings.items() if key in map_keys},
    ):
        issues.append("map center")
    for slot, expected in spec.slot_settings.items():
        if not _subset(visualization.get(slot, {}).get("settings", {}), expected):
            issues.append(f"{slot} settings")
    if spec.geo_layers or spec.combined_layers:
        issues.extend(_layer_issues(visualization, spec))
    selected = visualization
    if spec.geo_layers or spec.combined_layers:
        selected_id = visualization.get("selectedLayerId")
        selected = next(
            (
                layer
                for layer in visualization.get("layers", ())
                if layer.get("layerSettings", {}).get("id") == selected_id
            ),
            {},
        )
    for slot, fields in spec.slots.items():
        section = selected if spec.geo_layers or spec.combined_layers else visualization
        if _guids(section, slot) != [field.guid for field in fields]:
            issues.append(f"{slot} fields")
    if not spec.geo_layers:
        expected_sort = [
            (field.guid, direction.upper()) for field, direction in spec.sort_direction_items
        ]
        actual_sort = [
            (field.get("guid"), field.get("direction")) for field in _items(selected, "sort")
        ]
        if actual_sort != expected_sort:
            issues.append("sorting")
    all_fields = list(_snapshots(visualization))
    for field, formatting in spec.pending_measure_formats:
        formatting = {  # noqa: PLW2901
            "showRankDelimiter" if key == "show_rank_delimiter" else key: value
            for key, value in formatting.items()
        }
        if not any(
            value.get("guid") == field.guid and _subset(value.get("formatting", {}), formatting)
            for value in all_fields
        ):
            issues.append(f"{field.title} formatting")
    for field, property_name, expected in spec.item_mutations:
        if property_name == "_title_override":
            property_name = "fakeTitle"  # noqa: PLW2901
        if not any(
            value.get("guid") == field.guid and _subset(value.get(property_name), expected)
            for value in all_fields
        ):
            issues.append(f"{field.title} {property_name}")
    for expected in spec.local_fields:
        try:
            actual = chart.fields.by_guid(expected["guid"])
        except DataLensValidationError:
            issues.append(f"local field {expected['title']} missing")
            continue
        if any(
            getattr(actual, property_name) != expected[key]
            for property_name, key in (
                ("formula", "formula"),
                ("cast", "cast"),
                ("type", "type"),
                ("aggregation", "aggregation"),
            )
        ):
            issues.append(f"local field {expected['title']} formula/settings")
    color = spec.color_encoding
    if color:
        actual_colors = visualization.get("colors", {})
        values = actual_colors.get("items", ())
        settings = actual_colors.get("settings", {})
        if color.kind == "measure_name":
            if not values or values[0].get("type") != "PSEUDO":
                issues.append("color fields")
            if not _subset(
                settings.get("mountedColors", {}),
                {field.guid: value for field, value in color.colors_map.items()},
            ):
                issues.append("color settings")
        elif [item.get("guid") for item in values] != [color.field.guid]:
            issues.append("color fields")
        for expected, key in (
            (color.gradient_mode, "gradientMode"),
            (color.gradient_palette, "gradientPalette"),
            (color.reversed, "reversed"),
        ):
            if expected is not None and settings.get(key) != expected:
                issues.append("color settings")
    if (
        spec.colors_palette
        and visualization.get("colors", {}).get("settings", {}).get("palette")
        != spec.colors_palette
    ):
        issues.append("palette settings")
    shape = spec.shape_encoding
    if shape:
        shapes = visualization.get("shapes", {})
        expected = [shape.field.guid] if shape.field else None
        if (
            expected is not None
            and [item.get("guid") for item in shapes.get("items", ())] != expected
        ):
            issues.append("shape fields")
        if shape.shapes_map:
            mapping = {
                field.guid if hasattr(field, "guid") else field: value
                for field, value in shape.shapes_map.items()
            }
            if not _subset(shapes.get("settings", {}).get("mountedShapes", {}), mapping):
                issues.append("shape settings")
    if spec.geopoints_config and not _subset(
        visualization.get("size", {}).get("settings", {}), spec.geopoints_config
    ):
        issues.append("size settings")
    if spec.label_mode and any(
        item.get("formatting", {}).get("labelMode") != spec.label_mode
        for item in _items(selected, "labels")
    ):
        issues.append("label mode")
    if spec.labels_position:
        key = "position" if definition["type"] == "funnel" else "labelsPosition"
        expected = None if spec.labels_position == "auto" else spec.labels_position
        if selected.get("labels", {}).get("settings", {}).get(key) != expected:
            issues.append("label position")
    if (
        spec.description is not None
        and (chart.raw.get("annotation") or {}).get("description", "") != spec.description
    ):
        issues.append("description")
    if "filters" in definition:
        expected_filters = [
            (field.guid, operation, tuple(values))
            for field, operation, values in spec.pending_filters
        ]
        actual_filters = [
            (
                value["guid"],
                value["filter"]["operation"]["code"],
                tuple(value["filter"].get("value", ())),
            )
            for value in chart.data.get("sources", {}).get("filters", ())
        ]
        if actual_filters != expected_filters:
            issues.append("filters")
    return list(dict.fromkeys(issues))


def check_chart(chart: Any, *, context: Any, datasets: Any, definition: Any) -> Any:
    family = definition.get("family", "wizard")
    if family == "wizard":
        return wizard_issues(chart, context, datasets, definition)
    if family in {"ql", "editor"}:
        from analytics_toolkit.datalens_utils.dashboard_generation.charts import (  # noqa: PLC0415
            editor,
            ql,
        )

        return (ql if family == "ql" else editor).issues(chart, context, datasets, definition)
    return [f"unknown family {family}"]


def chart_issues(
    chart: Any, dataset: Any, definition: Any, *, context: Any = None, datasets: Any = None
) -> Any:
    """Compatibility entry point, with explicit context for QL and Editor."""
    datasets = datasets or {definition.get("dataset", "sales"): dataset}
    if context is not None:
        return check_chart(chart, context=context, datasets=datasets, definition=definition)
    if definition.get("family", "wizard") != "wizard":
        message = "QL/Editor verification requires context and datasets."
        raise TypeError(message)
    # Local typed factory introspection does not perform HTTP or mint tokens.
    from datalens_sdk import DataLensClientYC, StaticYCIAMAuthProvider  # noqa: PLC0415

    with DataLensClientYC(
        auth=StaticYCIAMAuthProvider(org_id="offline", token="offline-validation")  # noqa: S106
    ) as client:
        return wizard_issues(chart, SimpleNamespace(client=client), datasets, definition)
