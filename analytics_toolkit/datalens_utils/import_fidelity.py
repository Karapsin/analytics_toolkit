"""Source-oriented checks; deployment verification alone cannot prove import parity."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .errors import DataLensUtilsError
from .import_source import wizard_binding_issues
from .validation.charts import wizard_issues

# Neutral defaults emitted by the supported SDK serializers. Non-default values
# must be reconstructed by a recipe setter or explicitly rejected.
AXIS_DEFAULTS = {
    "axisFormatMode": "auto",
    "axisLabelDateFormat": "DD.MM.YYYY",
    "axisModeMap": {},
    "axisVisibility": "show",
    "grid": "on",
    "gridStep": "auto",
    "gridStepValue": 50,
    "hideLabels": "no",
    "holidays": "off",
    "labelsView": "auto",
    "title": "off",
    "titleValue": "",
    "type": "linear",
    "nulls": "connect",
    "scale": "auto",
    "scaleValue": "min-max",
}


def check_encoding_settings(slot: str, kind: str, settings: Any) -> None:
    mapping = "mountedColors" if slot == "colors" else "mountedShapes"
    allowed = {"fieldGuid", mapping, "settingsId"}
    if slot == "colors":
        allowed.update(
            {
                "palette",
                "coloredByMeasure",
                "colorMode",
                "polygonBorders",
                "gradientMode",
                "gradientPalette",
                "reversed",
                "thresholdsMode",
            }
        )
        for wire, expected in (
            ("colorMode", "gradient" if kind == "measure" else "palette"),
            ("thresholdsMode", "auto"),
            ("polygonBorders", "show"),
            ("coloredByMeasure", kind == "measure_name"),
        ):
            if wire in settings and settings[wire] != expected:
                message = "Unrepresentable color setting: " + wire
                raise DataLensUtilsError(message)
        if kind != "measure" and any(
            key in settings for key in ("gradientMode", "gradientPalette", "reversed")
        ):
            message = "Gradient settings require a measure color binding."
            raise DataLensUtilsError(message)
        if kind != "measure_name" and settings.get(mapping):
            message = "Authored dimension/measure color maps cannot be represented."
            raise DataLensUtilsError(message)
    if set(settings) - allowed:
        message = "Unsupported " + slot + " settings: " + str(sorted(set(settings) - allowed))
        raise DataLensUtilsError(message)


def encoding_arguments(
    value: Any, settings: Any, names: Any, types: Any, slot: str
) -> tuple[str, dict[str, Any]]:
    kind = (
        "measure_name"
        if value.get("type") == "PSEUDO"
        else "measure"
        if value.get("type", types.get(value.get("guid"))) == "MEASURE"
        else "dimension"
    )
    check_encoding_settings(slot, kind, settings)
    arguments: dict[str, Any] = {}
    if kind != "measure_name":
        if value.get("guid") not in names:
            message = "Unknown " + slot + " field GUID."
            raise DataLensUtilsError(message)
        arguments["field"] = names[value["guid"]]
    mapping = "mountedColors" if slot == "colors" else "mountedShapes"
    if mapping in settings and (slot != "colors" or kind == "measure_name"):
        arguments["colors_map" if slot == "colors" else "shapes_map"] = {
            names[key] if kind == "measure_name" else key: item
            for key, item in settings[mapping].items()
        }
    if slot == "colors" and kind == "measure":
        for public, wire in (
            ("mode", "gradientMode"),
            ("palette", "gradientPalette"),
            ("reversed", "reversed"),
        ):
            if wire in settings:
                arguments[public] = settings[wire]
    return kind, arguments


def wizard_encodings(
    visualization: Any, names: dict[str, str], types: Any = None
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for slot, prefix in (("colors", "color"), ("shapes", "shape")):
        section = visualization.get(slot, {})
        values, settings = section.get("items", []), section.get("settings", {})
        if not values:
            if any(key != "settingsId" for key in settings):
                message = "Cannot preserve unbound " + slot + " settings."
                raise DataLensUtilsError(message)
            continue
        if len(values) != 1:
            message = "Cannot import multiple " + slot + " bindings."
            raise DataLensUtilsError(message)
        kind, arguments = encoding_arguments(values[0], settings, names, types or {}, slot)
        result[prefix + "_by_" + kind] = arguments
        if prefix == "color" and "palette" in settings:
            result["palette"] = {"id": settings["palette"]}
    return result


def normalized_measure_format(formatting: Any) -> dict[str, Any]:
    """Normalize public formatting keys without conflating label mode with measure format."""
    return {
        "show_rank_delimiter" if key == "showRankDelimiter" else key: value
        for key, value in (formatting or {}).items()
        if key != "labelMode"
    }


def occurrence_format_issues(visualization: Any, formats: Any) -> list[str]:
    """Compare field-wide builder formats against every observed placement, including empty ones."""
    issues = []
    for slot, section in visualization.items():
        if slot == "sort" or not isinstance(section, dict):
            continue
        for value in section.get("items", ()):
            guid = value.get("guid")
            if guid is not None and normalized_measure_format(
                value.get("formatting")
            ) != formats.get(guid, {}):
                issues.append("unrepresented occurrence formatting " + str(guid))
    return issues


def wizard_source_issues(client: Any, chart: Any, datasets: Any, definition: Any) -> list[str]:
    """Check both modeled output and observed source settings before claiming parity."""
    from .dashboard_generation.charts import wizard  # noqa: PLC0415

    binding_issues = wizard_binding_issues(chart, datasets, definition)
    if binding_issues:
        return binding_issues
    context = SimpleNamespace(client=client)
    spec = wizard.create_builder(context, datasets, definition, chart.location).to_spec()
    issues: list[str] = wizard_issues(chart, context, datasets, definition)
    visualization = chart.data.get("visualization", {})
    formats = {
        field.guid: normalized_measure_format(formatting)
        for field, formatting in spec.pending_measure_formats
    }
    issues.extend(occurrence_format_issues(visualization, formats))
    for key, value in visualization.get("chartSettings", {}).items():
        if key != "settingsId" and spec.chart_settings.get(key) != value:
            issues.append("unrepresented chart setting " + key)
    for slot, section in visualization.items():
        if not isinstance(section, dict) or slot in {"colors", "shapes", "chartSettings"}:
            continue
        if section.get("items") and slot not in spec.slots and slot != "sort":
            issues.append("unrepresented slot " + slot)
        for key, value in section.get("settings", {}).items():
            expected = {
                **(AXIS_DEFAULTS if slot in {"x", "y", "y2"} else {}),
                **spec.slot_settings.get(slot, {}),
            }
            if key != "settingsId" and expected.get(key) != value:
                issues.append("unrepresented " + slot + " setting " + key)
    return issues


def record(report: Any, resource: str, feature: str, status: str, reason: str) -> None:
    report["fidelity"].append(
        {
            "resource": resource,
            "feature": feature,
            "features": [feature],
            "status": status,
            "reason": reason,
        }
    )
