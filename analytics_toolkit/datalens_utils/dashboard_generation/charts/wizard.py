"""Typed Wizard construction and safe updates for the chart gallery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from datalens_sdk import GeoLayerFilter, WizardLocalField

from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

VISUALIZATIONS = {
    "line": "line",
    "area": "area",
    "area_100p": "area100p",
    "column": "column",
    "column_100p": "column100p",
    "bar": "bar",
    "bar_100p": "bar100p",
    "combined_chart": "combined-chart",
    "pie": "pie",
    "donut": "donut",
    "flat_table": "flatTable",
    "pivot_table": "pivotTable",
    "indicator": "metric",
    "scatter": "scatter",
    "treemap": "treemap",
    "funnel": "funnel",
    "geolayer": "geolayer",
}

# Only chart configuration operations enter the recipe interpreter. Lifecycle,
# opaque data/meta, publication and structural replacement cannot be configured.
SETTING_METHODS = {
    "legend",
    "tooltip",
    "tooltip_sum",
    "labels_position",
    "label_mode",
    "tooltip_percentage_base",
    "shape",
    "navigator",
    "axis_visibility",
    "axis_title",
    "axis_scale",
    "grid",
    "hide_labels",
    "nulls_mode",
    "palette",
    "color_by_dimension",
    "color_by_measure",
    "color_by_measure_name",
    "shape_by_dimension",
    "shape_by_measure_name",
    "point_size_range",
    "font_size",
    "font_color",
    "measure_title_mode",
    "column_background",
    "column_bars",
    "column_title",
    "freeze_columns",
    "pagination",
    "table_size",
    "totals",
    "subtotals",
}
FIELD_SETTINGS = {
    "color_by_dimension",
    "color_by_measure",
    "shape_by_dimension",
    "column_background",
    "column_bars",
    "column_title",
    "subtotals",
}
SLOTS = {"x", "y", "y2", "columns", "rows", "measures", "points", "size", "labels", "segments"}


def getter(client: Any) -> Any:
    return client.get.wizard_chart


def local_fields(definition: Any) -> Any:
    result = {}
    values = definition.get("local_fields", ())
    if isinstance(values, Mapping):
        values = [{"title": title, **value} for title, value in values.items()]
    for value in values:
        spec = dict(value)
        kind = spec.pop("kind", "MEASURE")
        if kind not in {"MEASURE", "DIMENSION"}:
            message = f"Unsupported Wizard local field kind {kind!r}."
            raise DataLensUtilsError(message)
        if not spec.get("guid"):
            message = "Every Wizard local field needs a stable explicit guid."
            raise DataLensUtilsError(message)
        factory = WizardLocalField.measure if kind == "MEASURE" else WizardLocalField.dimension
        if kind == "DIMENSION":
            aggregation = spec.pop("aggregation", "none")
            if aggregation != "none":
                message = "Wizard local dimensions cannot have a measure aggregation."
                raise DataLensUtilsError(message)
        result[spec["title"]] = factory(**spec)
    return result


def _resolve(dataset: Any, owned: Any, name: Any) -> Any:
    return owned[name] if name in owned else dataset.fields.by_name(name)


def registered_local_fields(chart: Any) -> Any:
    """Field declarations can outlive placements in a saved rebuild draft."""
    declarations: dict[str, Any] = {}
    for operation in chart.data.get("sources", {}).get("updates", ()):
        if operation.get("action") in {"add", "add_field", "update", "update_field"}:
            field = operation.get("field", {})
            if field.get("calc_mode") == "formula" and field.get("guid"):
                declarations.setdefault(field["guid"], {}).update(field)
    return declarations


def validate_definition(definition: Any) -> Any:
    if definition["type"] not in VISUALIZATIONS:
        message = f"Unsupported Wizard factory {definition['type']!r}."
        raise DataLensUtilsError(message)
    unknown = set(definition.get("fields", {})) - SLOTS
    if unknown:
        message = f"Unknown Wizard field slots: {sorted(unknown)}"
        raise DataLensUtilsError(message)
    unknown = set(definition.get("settings", {})) - SETTING_METHODS
    if unknown:
        message = f"Unsupported Wizard settings: {sorted(unknown)}"
        raise DataLensUtilsError(message)
    local_fields(definition)
    if definition["type"] in {"combined_chart", "geolayer"} and not definition.get("layers"):
        message = f"Wizard {definition['type']} requires configured layers."
        raise DataLensUtilsError(message)


def _settings(builder: Any, dataset: Any, owned: Any, definition: Any) -> Any:
    for method, values in definition.get("settings", {}).items():
        for value in values if isinstance(values, list) else [values]:
            arguments = dict(value)
            field = arguments.pop("field", None)
            if method in FIELD_SETTINGS:
                if field is None:
                    message = f"Wizard setting {method!r} needs a field."
                    raise DataLensUtilsError(message)
                getattr(builder, method)(_resolve(dataset, owned, field), **arguments)
            else:
                for map_name in ("colors_map", "shapes_map"):
                    if map_name in arguments and method.endswith("measure_name"):
                        arguments[map_name] = {
                            _resolve(dataset, owned, name): mapped
                            for name, mapped in arguments[map_name].items()
                        }
                getattr(builder, method)(**arguments)
    return builder


def _layers(builder: Any, dataset: Any, owned: Any, definition: Any, datasets: Any) -> Any:  # noqa: ARG001
    registered = {definition["dataset"]}
    for layer in definition.get("layers", ()):
        options = dict(layer)
        layer_type = options.pop("type")
        role = options.pop("dataset", definition["dataset"])
        layer_dataset = datasets[role]
        if role not in registered:
            builder.add_dataset(layer_dataset)
            registered.add(role)
        if definition["type"] == "geolayer" and role != definition["dataset"]:
            options["dataset"] = layer_dataset
        for name in (
            "geopoint",
            "polygon",
            "polyline",
            "grouping",
            "size",
            "color",
            "sort_by",
            "y",
            "y2",
        ):
            if options.get(name) is not None:
                options[name] = _resolve(layer_dataset, owned, options[name])
        for name in ("tooltips", "labels"):
            if name in options:
                options[name] = [_resolve(layer_dataset, owned, field) for field in options[name]]
        if "filters" in options:
            options["filters"] = [
                GeoLayerFilter(
                    field=_resolve(layer_dataset, owned, rule["field"]),
                    operation=rule["operation"],
                    values=rule.get("values", ()),
                )
                for rule in options["filters"]
            ]
        builder.add_layer(layer_type, **options)
    if "map_center" in definition:
        builder.map_center(**definition["map_center"])
    return builder


def configure(  # noqa: C901, PLR0912
    builder: Any,
    context: Any,  # noqa: ARG001 - Shared adapter keyword contract.
    datasets: Any,
    definition: Any,
    *,
    creation: Any = False,
) -> Any:
    dataset = datasets[definition["dataset"]]
    owned = local_fields(definition)
    chart = getattr(builder, "chart", None)
    active = {field.guid for field in chart.fields if field.guid} if chart is not None else set()
    registered = registered_local_fields(chart) if chart is not None else {}
    for field in owned.values():
        if field.guid not in registered:
            builder.add_local_field(field)
        elif registered[field.guid].get("formula") != field.formula:
            builder.replace_formula(field.guid, formula=field.formula)
    for slot, names in definition.get("fields", {}).items():
        getattr(builder, slot)([_resolve(dataset, owned, name) for name in names])
    if creation:
        _layers(builder, dataset, owned, definition, datasets)
    # Sort is append-only. A structural rebuild first saves all removals as a
    # draft; ordinary title/style updates retain the existing sorting.
    if creation or not active:
        for name, direction in definition.get("sort", ()):
            builder.add_sort(_resolve(dataset, owned, name), direction=direction)
    if chart is not None and "filters" in definition:
        for value in chart.data.get("sources", {}).get("filters", ()):
            builder.delete_filter(value["guid"])
    for rule in definition.get("filters", ()):
        builder.add_filter(
            _resolve(dataset, owned, rule["field"]),
            operation=rule["operation"],
            values=rule.get("values", ()),
        )
    if definition["type"] != "indicator":
        builder.chart_title(
            text=definition["title"], mode="show" if definition.get("show_title", True) else "hide"
        )
    if "description" in definition:
        builder.description(definition["description"])
    for axis, title in definition.get("axis_titles", {}).items():
        builder.axis_title(axis, mode="manual", text=title)
    if "labels" in definition:
        builder.labels([_resolve(dataset, owned, name) for name in definition["labels"]])
    if "labels_position" in definition:
        builder.labels_position(mode=definition["labels_position"])
    for name, formatting in definition.get("formats", {}).items():
        builder.measure_format(_resolve(dataset, owned, name), **formatting)
    if "totals" in definition:
        builder.totals(enabled=definition["totals"])
    return _settings(builder, dataset, owned, definition)


def create_builder(context: Any, datasets: Any, definition: Any, folder: Any) -> Any:
    validate_definition(definition)
    builder = getattr(context.client.create.wizard_chart, definition["type"])(
        name=definition["name"], location=folder
    )
    return configure(
        builder.dataset(datasets[definition["dataset"]]),
        context,
        datasets,
        definition,
        creation=True,
    )


def validate_change(chart: Any, context: Any, datasets: Any, definition: Any) -> Any:
    if chart.visualization_id != VISUALIZATIONS[definition["type"]]:
        message = (
            "Changing Wizard visualization of "
            f"{definition['name']!r}"
            " is unsupported; its existing ID was retained."
        )
        raise DataLensUtilsError(message)
    expected_ids = tuple(
        dict.fromkeys(
            [
                datasets[definition["dataset"]].id,
                *(
                    datasets[layer["dataset"]].id
                    for layer in definition.get("layers", ())
                    if layer.get("dataset")
                ),
            ]
        )
    )
    if chart.dataset_ids != expected_ids:
        message = f"Wizard chart {definition['name']!r} references a different dataset."
        raise DataLensUtilsError(message)
    registered = registered_local_fields(chart)
    for field in local_fields(definition).values():
        saved = registered.get(field.guid)
        if saved and any(
            saved.get(attribute) != getattr(field, attribute)
            for attribute in ("title", "cast", "type", "aggregation")
        ):
            message = (
                "Changing the title/type/aggregation of Wizard local field "
                f"{field.title!r}"
                " is unsupported; keep its stable declaration and edit its "
                "formula."
            )
            raise DataLensUtilsError(message)
    if definition["type"] in {"combined_chart", "geolayer"}:
        from analytics_toolkit.datalens_utils.validation.charts import (  # noqa: PLC0415 - Adapter cycle.
            wizard_issues,
        )

        # Layer and map topology are create-only in this SDK. Never enter a
        # field-removal rebuild that would dismantle an unrepairable map.
        problems = [
            issue
            for issue in wizard_issues(chart, context, datasets, definition)
            if issue.startswith(("layer ", "map center"))
            and not (definition["type"] == "combined_chart" and issue.endswith("x fields"))
        ]
        if problems:
            message = (
                "Changing layers/map topology of "
                f"{definition['name']!r}"
                " is unsupported: "
                f"{', '.join(problems)}"
                "."
            )
            raise DataLensUtilsError(message)
