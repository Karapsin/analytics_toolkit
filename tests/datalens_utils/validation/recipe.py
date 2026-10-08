"""Formula graph reuse, layer fields, and optional coverage manifests."""

import copy
from unittest.mock import patch

from analytics_toolkit.datalens_utils.dashboard_generation.dashboard.configuration import (
    read_contents,
)
from analytics_toolkit.datalens_utils.settings import read_chart_definitions, read_config
from analytics_toolkit.datalens_utils.validation import recipe


def test_formula_dependency_dag_reuses_completed_nodes():
    recipe._validate_formulas(
        "sales",
        {"A": {"formula": "[Raw]"}, "B": {"formula": "[A]"}, "C": {"formula": "[A] + [B]"}},
        {"Raw", "A", "B", "C"},
    )


def test_layer_filter_and_measure_name_maps_reference_declared_fields():
    dataset = {"fields": {"raw": {"title": "Field"}}, "calculations": {"Metric": {"formula": "1"}}}
    recipe._check_wizard_fields(
        "map",
        {
            "dataset": "sales",
            "layers": [{"filters": [{"field": "Field"}]}],
            "settings": {
                "column_title": {"field": "Field"},
                "color_by_measure_name": {"colors_map": {"Metric": "red"}},
                "shape_by_measure_name": {"shapes_map": {"Metric": "circle"}},
            },
        },
        {"sales": dataset},
    )


def test_optional_manifest_without_editor_or_type_examples(session_state):
    datasets = read_config("DL objects/datasets.json")
    charts = read_chart_definitions()
    tabs = read_config("UI/tabs.json")
    contents = read_contents()
    manifest = copy.deepcopy(read_config("coverage.json"))
    manifest.pop("editor_renderers", None)
    manifest.pop("clickhouse_types", None)
    manifest["selector_coverage"].pop("editor", None)
    manifest["formulas"] = []
    with patch.object(recipe, "read_config", return_value=manifest):
        recipe._coverage(manifest, datasets, charts, tabs, contents)


def test_table_dataset_without_projection_asset_is_valid(session_state):
    datasets = read_config("DL objects/datasets.json")
    for definition in datasets.values():
        definition.pop("projection_file", None)
        definition["source"] = "ch_table"
    recipe.validate_recipe(
        datasets, read_chart_definitions(), read_config("UI/tabs.json"), read_contents()
    )
