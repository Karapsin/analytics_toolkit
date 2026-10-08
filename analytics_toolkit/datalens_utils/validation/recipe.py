"""Validate the editable coverage contract and local recipe before cloud writes."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from analytics_toolkit.datalens_utils.dashboard_generation.charts import editor, ql, wizard
from analytics_toolkit.datalens_utils.dashboard_generation.datasets.create import (
    validate_definition,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.settings import asset_path, read_config, source_tables

ADAPTERS = {"wizard": wizard, "ql": ql, "editor": editor}
NUMERIC_TYPES = {"integer", "float", "number", "int", "uint", "decimal"}

# Official United Storage entry-name KEY_REG, pinned to the inspected revision:
# https://github.com/datalens-tech/datalens-us/blob/55105285b95a689efb86146743168796399b1756/src/components/validation-schema-compiler.ts#L19-L49
# re.ASCII keeps \w equivalent to JavaScript's ASCII class; Cyrillic is explicit.
_RESOURCE_NAME = re.compile(
    r"[\wА-Яа-яЁё_@()%](?:[\wА-Яа-яЁё_@().,:;'\u00A0\u180E\u2000-\u200B\u202F\u205F\u3000\uFEFF| \-–—−$*&%]*[\wА-Яа-яЁё_@()%]+)?",  # noqa: E501, RUF001 - Exact public storage grammar.
    re.ASCII,
)


def validate_resource_name(name: Any) -> Any:
    """Validate a persistent entry name; presentation titles are unrestricted."""
    if not isinstance(name, str) or _RESOURCE_NAME.fullmatch(name) is None:
        message = (
            "Invalid DataLens resource name "
            f"{name!r}"
            ": use the United Storage entry-name grammar. Display titles may "
            "contain additional symbols; persistent names cannot contain '/' "
            "or '·' or '×'."  # noqa: RUF001
        )
        raise DataLensUtilsError(message)
    return name


def _require(condition: Any, message: Any) -> Any:
    if not condition:
        message = f"Local recipe: {message}"
        raise DataLensUtilsError(message)


def _named_calculations(definition: Any, *, local: Any = False) -> Any:
    values = definition.get("local_fields" if local else "calculations", {})
    if isinstance(values, Mapping):
        return dict(values.items())
    return {value.get("title", value.get("name")): value for value in values}


def _dataset_fields(definition: Any) -> Any:
    return (
        {
            value.get("title", physical): value
            for physical, value in definition.get("fields", {}).items()
        }
        | _named_calculations(definition)
        | {
            name: {"cast": value["type"]}
            for name, value in definition.get("parameters", {}).items()
        }
    )


def _formula_references(formula: Any) -> Any:
    # Ignore brackets inside quoted string literals; these are field names,
    # not a semantic formula parser or proof of connection-specific support.
    plain = re.sub(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' """, "", formula, flags=re.VERBOSE)
    return set(re.findall(r"\[([^\]]+)\]", plain))


def _validate_formulas(owner: Any, calculations: Any, available: Any) -> Any:
    graph = {}
    for title, definition in calculations.items():
        references = _formula_references(definition.get("formula", ""))
        _require(
            not references - available,
            f"{owner}/{title} formula references unknown fields: {sorted(references - available)}",
        )
        graph[title] = references & set(calculations)
    active, complete = set(), set()

    def visit(title: Any) -> Any:
        _require(title not in active, f"{owner} has a circular calculation involving {title!r}.")
        if title in complete:
            return
        active.add(title)
        for dependency in graph[title]:
            visit(dependency)
        active.remove(title)
        complete.add(title)

    for title in graph:
        visit(title)


def _check_wizard_fields(key: Any, definition: Any, datasets: Any) -> Any:  # noqa: C901
    local = _named_calculations(definition, local=True)
    primary = set(_dataset_fields(datasets[definition["dataset"]])) | set(local)
    _validate_formulas(f"chart:{key}", local, primary)

    def fields(names: Any, available: Any = primary) -> Any:
        names = [names] if isinstance(names, str) else names
        _require(
            not set(names) - available,
            f"chart {key} references unknown fields: {sorted(set(names) - available)}",
        )

    for names in definition.get("fields", {}).values():
        fields(names)
    for name, _ in definition.get("sort", []):
        fields(name)
    for rule in definition.get("filters", []):
        fields(rule["field"])
    fields(definition.get("labels", []))
    union = set(primary)
    for layer in definition.get("layers", []):
        role = layer.get("dataset", definition["dataset"])
        _require(role in datasets, f"chart {key} layer refers to unknown dataset {role!r}.")
        available = set(_dataset_fields(datasets[role])) | set(local)
        union.update(available)
        for option in (
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
            if layer.get(option) is not None:
                fields(layer[option], available)
        for option in ("labels", "tooltips"):
            fields(layer.get(option, []), available)
        for rule in layer.get("filters", []):
            fields(rule["field"], available)
    fields(definition.get("formats", {}), union)
    for method, settings in definition.get("settings", {}).items():
        for arguments in settings if isinstance(settings, list) else [settings]:
            if arguments.get("field") is not None:
                fields(arguments["field"], union)
            if method.endswith("measure_name"):
                fields(arguments.get("colors_map", {}), union)
                fields(arguments.get("shapes_map", {}), union)


def _selectors(contents: Any) -> Any:
    definitions = {}
    for content in contents.values():
        for definition in content.get("selectors", {}).values():
            _require(
                definition["key"] not in definitions,
                f"selector {definition['key']} is placed more than once.",
            )
            definitions[definition["key"]] = definition
        for group in content.get("selector_groups", {}).values():
            members = group["members"]
            _require(
                len(members) == len(set(members)) and set(members) == set(group["definitions"]),
                "native selector group members differ from their definitions.",
            )
            for key, definition in group["definitions"].items():
                _require(key not in definitions, f"selector {key} is placed more than once.")
                definitions[key] = definition
    return definitions


def _native_variant(definition: Any, datasets: Any, charts: Any, contents: Any = None) -> Any:
    control, source = (definition["control"], definition["source"])
    element = control["element"]
    if element == "select":
        return "multiselect" if control.get("multiselect") else "single_select"
    if element == "date":
        return "date_range" if control.get("is_range") else "single_date"
    if element == "checkbox":
        return "checkbox"
    if source["kind"] == "dataset":
        casts = {_dataset_fields(datasets[source["dataset"]])[source["field"]].get("cast")}
    else:
        casts = set()
        groups = {
            key: group
            for content in (contents or {}).values()
            for key, group in content.get("chart_groups", {}).items()
        }
        recipients = [
            member["key"]
            for key in definition["recipients"]
            for member in groups.get(key, {"charts": [{"key": key}]})["charts"]
        ]
        for key in recipients:
            chart = charts.get(key)
            if chart is None:
                continue
            if chart["family"] == "wizard":
                parameter = (
                    datasets[chart["dataset"]].get("parameters", {}).get(source["param_name"], {})
                )
                casts.add(parameter.get("type"))
            else:
                casts.update(
                    parameter.get("type")
                    for parameter in chart.get("params", [])
                    if parameter.get("name") == source["param_name"]
                )
    return "numeric_input" if casts & NUMERIC_TYPES else "text_input"


def _coverage(manifest: Any, datasets: Any, charts: Any, tabs: Any, contents: Any) -> Any:  # noqa: C901, PLR0912, PLR0915
    _require(manifest.get("version") == 1, "unsupported coverage manifest version.")
    _require(
        set(manifest.get("dataset_roles", [])) == set(datasets),
        "dataset roles differ from the coverage manifest.",
    )
    _require(
        set(manifest.get("tab_keys", [])) == set(tabs),
        "tab keys differ from the coverage manifest.",
    )
    visual = {
        key: definition for key, definition in charts.items() if definition["type"] != "selector"
    }
    external = {
        key: definition for key, definition in charts.items() if definition["type"] == "selector"
    }
    actual: Any
    for name, actual in (
        ("object_count", len(charts)),
        ("visual_chart_count", len(visual)),
        ("external_selector_count", len(external)),
    ):
        _require(
            manifest.get(name) == actual, f"{name} expects {manifest.get(name)}, found {actual}."
        )
    families = manifest.get("families", {})
    _require(
        set(families) == {definition["family"] for definition in visual.values()},
        "visual chart families differ from the coverage manifest.",
    )
    for family, expected in families.items():
        actual = {
            key: definition for key, definition in visual.items() if definition["family"] == family
        }
        _require(
            expected["count"] == len(actual) and set(expected["keys"]) == set(actual),
            f"{family} chart keys/count differ from the coverage manifest.",
        )
        _require(
            set(expected["types"]) == {definition["type"] for definition in actual.values()},
            f"{family} chart types differ from the coverage manifest.",
        )
    for key, layer_type in manifest.get("wizard_geo_layers", {}).items():
        definition = charts.get(key, {})
        _require(
            definition.get("type") == "geolayer"
            and layer_type in {layer.get("type") for layer in definition.get("layers", [])},
            f"map {key} lacks the required {layer_type} layer.",
        )
    for key, series in manifest.get("editor_series", {}).items():
        _require(
            charts.get(key, {}).get("series_type") == series,
            f"Editor {key} lacks the required {series} series.",
        )
    for key, variant in manifest.get("editor_variants", {}).items():
        _require(
            charts.get(key, {}).get("coverage", {}).get("variant") == variant,
            f"Editor {key} lacks the required {variant} variant.",
        )
    if "editor_renderers" in manifest:
        _require(
            set(manifest["editor_renderers"])
            == {
                definition["type"]
                for definition in charts.values()
                if definition["family"] == "editor"
            },
            "Editor renderers differ from the coverage manifest.",
        )
    placements = Counter(key for content in contents.values() for key in content.get("charts", {}))
    placements.update(
        member["key"]
        for content in contents.values()
        for group in content.get("chart_groups", {}).values()
        for member in group["charts"]
    )
    _require(
        set(placements) == set(visual) and all(count == 1 for count in placements.values()),
        "every visual chart must be placed exactly once.",
    )
    selectors = _selectors(contents)
    external_placements = Counter(
        value["source"]["chart"]
        for value in selectors.values()
        if value["source"]["kind"] == "editor"
    )
    _require(
        set(external_placements) == set(external)
        and all(count == 1 for count in external_placements.values()),
        "every external selector must be placed exactly once.",
    )
    selector_coverage = manifest.get("selector_coverage", {})
    for source, expected in selector_coverage.get("native", {}).items():
        actual = [value for value in selectors.values() if value["source"]["kind"] == source]
        _require(
            len(actual) == expected["count"]
            and set(expected["variants"])
            == {_native_variant(value, datasets, charts, contents) for value in actual},
            f"{source} native selector coverage differs from the manifest.",
        )
    expected = selector_coverage.get("editor")
    if expected:
        definition = external.get(expected["key"], {})
        _require(definition is not None, "the declared Editor selector object is missing.")
        source = asset_path(definition["scripts"]["controls"]).read_text(encoding="utf-8")
        controls = set(re.findall("\\btype\\s*:\\s*['\\\"]([^'\\\"]+)['\\\"]", source))
        _require(
            set(expected["control_types"]) == set(definition.get("control_types", [])) == controls,
            "Editor selector control coverage differs from the manifest/source.",
        )
        for variant in expected.get("select_variants", []):
            value = "true" if variant == "multiple" else "false"
            _require(
                re.search(f"\\bmultiselect\\s*:\\s*{value}\\b", source) is not None,
                f"Editor selector lacks the {variant} select variant.",
            )
        for action in expected.get("actions", []):
            _require(
                re.search(f"""\\baction\\s*:\\s*['\\"]{re.escape(action)}['\\"]""", source)
                is not None,
                f"Editor selector lacks the {action} action.",
            )
    for expected in manifest.get("formulas", []):
        family, key = expected["owner"].split(":", 1)
        definition = (
            datasets.get(key)
            if family == "dataset"
            else charts.get(key)
            if family == "chart"
            else None
        )
        _require(definition is not None, f"formula owner {expected['owner']} is missing.")
        formulas = _named_calculations(definition, local=family == "chart")
        _require(
            formulas.get(expected["field"], {}).get("formula") == expected["formula"],
            f"formula {expected['owner']}/{expected['field']} differs from the coverage manifest.",
        )
    for family, section in (("ql", "sql_formulas"), ("editor", "js_formulas")):
        for key, categories in manifest.get(section, {}).items():
            _require(
                charts.get(key, {}).get("family") == family
                and isinstance(categories, list)
                and bool(categories),
                f"{section} refers to an unknown owner or empty example categories: {key}.",
            )
    examples = manifest.get("clickhouse_types", {}).get("persisted_examples", [])
    if examples:
        _require(
            len(examples) == manifest["clickhouse_types"]["persisted_family_examples"],
            "ClickHouse example count differs from the coverage manifest.",
        )
        _require(
            len({value["family"] for value in examples}) == len(examples),
            "ClickHouse type family examples must be unique.",
        )
        for value in examples:
            definition = datasets.get(value["dataset"])
            _require(
                definition is not None,
                f"ClickHouse type {value['family']} references an unknown dataset.",
            )
            fields = _dataset_fields(definition)
            field, native_type = (
                fields.get(value["value_field"], {}),
                fields.get(value["type_field"], {}),
            )
            _require(
                field.get("cast") == value["dataset_cast"] and native_type.get("cast") == "string",
                f"ClickHouse type {value['family']} lacks its declared value/type projections.",
            )
            _require(
                field.get("native_column")
                == native_type.get("native_column")
                == value["physical_column"],
                f"ClickHouse type {value['family']} physical column bindings differ.",
            )


def validate_recipe(
    dataset_definitions: Any, chart_definitions: Any, tab_definitions: Any, contents: Any
) -> Any:
    """Local-only preflight; the editable manifest controls required coverage."""
    context = SimpleNamespace(source_tables=source_tables(dataset_definitions))
    for role, definition in dataset_definitions.items():
        validate_resource_name(definition.get("name"))
        validate_definition(context, role, definition)
        if definition.get("projection_file"):
            asset_path(definition["projection_file"])
        fields = _dataset_fields(definition)
        _validate_formulas(f"dataset:{role}", _named_calculations(definition), set(fields))
    for key, definition in chart_definitions.items():
        validate_resource_name(definition.get("name"))
        family = definition.get("family")
        _require(family in ADAPTERS, f"chart {key} has an unsupported family {family!r}.")
        role = definition.get("dataset")
        _require(
            role is None or role in dataset_definitions,
            f"chart {key} refers to an unknown dataset {role!r}.",
        )
        _require(family != "wizard" or role is not None, f"Wizard chart {key} requires a dataset.")
        for linked in definition.get("links", {}).values():
            _require(
                linked in dataset_definitions,
                f"Editor chart {key} refers to an unknown linked dataset {linked!r}.",
            )
        ADAPTERS[family].validate_definition(definition)
        for relative in list(definition.get("scripts", {}).values()) + (
            [definition["query_file"]] if definition.get("query_file") else []
        ):
            asset_path(relative)
        if family == "wizard":
            _check_wizard_fields(key, definition, dataset_definitions)
        elif family == "ql":
            query = asset_path(definition["query_file"]).read_text(encoding="utf-8")
            referenced = {
                value.lower() for value in re.findall(r"__TABLE_([A-Z][A-Z0-9_]*)__", query)
            }
            _require(
                not referenced - set(dataset_definitions),
                f"QL chart {key} has unknown source-table bindings.",
            )
            remainder = re.sub(r"__TABLE_([A-Z][A-Z0-9_]*)__", "resolved_table", query)
            _require(
                re.search(r"__TABLE", remainder, re.IGNORECASE) is None,
                f"QL chart {key} contains a malformed source-table token.",
            )
        else:
            linked = set(definition.get("links", {}).values()) | ({role} if role else set())
            available = set().union(
                *(set(_dataset_fields(dataset_definitions[value])) for value in linked)
            )
            for parameter, field in definition.get("parameter_bindings", {}).items():
                _require(
                    field == "presentation" or field in available,
                    f"Editor chart {key} parameter {parameter} refers "
                    f"to an unknown linked field {field!r}.",
                )
    _require(set(contents) == set(tab_definitions), "tab contents and tab definitions differ.")
    manifest = read_config("coverage.json")
    _coverage(manifest, dataset_definitions, chart_definitions, tab_definitions, contents)
    return manifest
