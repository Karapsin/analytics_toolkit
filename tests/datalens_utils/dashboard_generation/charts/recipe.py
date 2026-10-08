"""Typed chart recipes reject invalid declarations before SDK writes."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.charts import editor, ql, wizard
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.settings import read_chart_definitions, read_config

from tests.datalens_utils._support.sdk import configured_datasets


@pytest.mark.parametrize(
    "values", [None, "field", [1], [{}], [{"name": ""}], [{"name": "x", "unknown": 1}]]
)
def test_ql_columns_reject_invalid_shapes(values):
    with pytest.raises(DataLensUtilsError):
        ql._columns(values, {})


@pytest.mark.parametrize(
    "params",
    [
        None,
        "bad",
        [1],
        [{"name": "x"}],
        [{"name": 1, "type": "string", "default": ""}],
        [{"name": "not an identifier", "type": "string", "default": ""}],
        [{"name": "x", "type": "string", "default": ""}] * 2,
        [{"name": "x", "type": "date-interval", "default": "bad"}],
        [{"name": "x", "type": "date-interval", "default": {"from": "", "to": "now"}}],
        [{"name": "x", "type": "number", "default": 1}],
        [{"name": "x", "type": "boolean", "default": "true"}],
    ],
)
def test_ql_parameter_contracts(params):
    with pytest.raises(DataLensUtilsError):
        ql._params({"params": params}, "{{x}}")


@pytest.mark.parametrize(
    "change",
    [
        {"type": "unknown"},
        {"columns": []},
        {"columns": {"": "string"}},
        {"fields": []},
        {"settings": []},
        {"fields": {"unsupported": ["x"]}},
        {"description": 1},
        {"fields": {}},
        {"fields": {"x": ["missing"]}},
        {"fields": {"x": ["EventDate", "EventDate"]}},
        {"params": []},
        {"sort": ["EventDate"]},
    ],
)
def test_ql_definition_rejects_incompatible_settings(session_state, change):
    definition = copy.deepcopy(read_chart_definitions()["ql_line"])
    definition.update(change)
    with pytest.raises(DataLensUtilsError):
        ql.validate_definition(definition)


@pytest.mark.parametrize("reference", [None, "", "/outside.sql", "missing.sql", "../outside.sql"])
def test_ql_query_references_are_local(session_state, reference):
    with pytest.raises(DataLensUtilsError):
        ql._read_query(reference)


def test_ql_empty_query_invalid_sources_and_corrupt_slot_metadata(session_state):
    path = session_state.paths.project_root / "empty.sql"
    path.write_text(" ", encoding="utf-8")
    with pytest.raises(DataLensUtilsError, match="empty"):
        ql._read_query("empty.sql")
    definition = read_chart_definitions()["ql_line"]
    with pytest.raises(DataLensUtilsError, match="mapping"):
        ql._recipe(SimpleNamespace(source_tables=[]), definition)
    for data in (
        {"visualization": []},
        {"visualization": {"placeholders": []}},
        {"visualization": {"placeholders": [{"id": "x", "items": "invalid"}]}},
        {"visualization": {"placeholders": [{"id": "x", "items": [1]}]}},
    ):
        assert ql._actual_columns(SimpleNamespace(data=data), "line", "x") is None


@pytest.mark.parametrize(
    "definition",
    [
        {"type": "unknown"},
        {"type": "line", "fields": {"bad": []}},
        {"type": "line", "settings": {"unknown": {}}},
        {"type": "geolayer"},
        {"type": "combined_chart"},
        {"type": "line", "local_fields": [{"title": "Local", "kind": "OTHER"}]},
        {"type": "line", "local_fields": [{"title": "Local"}]},
        {
            "type": "line",
            "local_fields": [
                {"title": "Local", "guid": "stable", "kind": "DIMENSION", "aggregation": "sum"}
            ],
        },
    ],
)
def test_wizard_invalid_local_fields_and_topology(definition):
    with pytest.raises(DataLensUtilsError):
        wizard.validate_definition(definition)


def test_wizard_local_dimension_mapping_retains_guid():
    local = wizard.local_fields(
        {
            "local_fields": {
                "Local": {"guid": "local-guid", "kind": "DIMENSION", "formula": "[Region]"}
            }
        }
    )
    assert local["Local"].guid == "local-guid"
    assert local["Local"].type == "DIMENSION"


@pytest.mark.parametrize(
    "meta",
    [
        "[]",
        "{}",
        '{"links": []}',
        '{"links": {"": "id"}}',
        '{"links": {"alias": ""}}',
        '{"links": {"alias": 1}}',
    ],
)
def test_editor_meta_requires_nonempty_string_aliases(meta):
    with pytest.raises(DataLensUtilsError):
        editor._meta(meta)


@pytest.mark.parametrize("reference", [None, "", "/outside.js", "missing.js"])
def test_editor_source_references_are_local(session_state, reference):
    with pytest.raises(DataLensUtilsError):
        editor._read_source(reference)


def test_editor_invalid_scripts_and_invalid_implicit_meta(session_state):
    with pytest.raises(DataLensUtilsError, match="scripts object"):
        editor.validate_definition({"type": "table", "scripts": []})
    assert editor._same_tab("meta", "not json", "{}", explicit_meta=False) is False


def test_ql_description_and_column_settings_conflicts(session_state):
    source = read_chart_definitions()["ql_line"]
    for change in (
        {"description": "one", "settings": {"description": {"text": "two"}}},
        {"settings": {"x": {"columns": ["EventDate"]}}},
    ):
        definition = {**source, **change}
        with pytest.raises(DataLensUtilsError):
            ql.validate_definition(definition)
    with pytest.raises(DataLensUtilsError, match="mapping"):
        ql._recipe(SimpleNamespace(sources=[]), source)


def test_editor_and_ql_capability_guards_and_metadata_family(session_state):
    definitions = read_chart_definitions()
    context = SimpleNamespace(
        client=Mock(capabilities={"chart_factories": {"ql": [], "editor": []}})
    )
    with pytest.raises(DataLensUtilsError, match="installation"):
        editor.create_builder(context, {}, definitions["js_table"], None)
    with pytest.raises(DataLensUtilsError, match="installation"):
        ql.create_builder(context, {}, definitions["ql_line"], None)
    context.connection = SimpleNamespace(id="connection", type="clickhouse")
    context.source_tables = {
        "retail": "example.retail",
        "acquisition": "example.acquisition",
        "delivery": "example.delivery",
    }
    assert ql.issues(SimpleNamespace(category="wizard"), context, {}, definitions["ql_line"]) == [
        "QL chart family"
    ]
    chart = SimpleNamespace(
        category="ql",
        visualization_id="wrong",
        query_value="wrong",
        connection={},
        params=[],
        data={},
        description="",
    )
    assert "QL visualization" in ql.issues(chart, context, {}, definitions["ql_line"])
    assert editor._same_tab("meta", "{", "{}", explicit_meta=False) is False


def test_wizard_style_field_maps_and_saved_filter_replacement(session_state):
    datasets = configured_datasets(read_config("DL objects/datasets.json"))
    dataset = datasets["retail"]
    builder = Mock()
    builder.chart = SimpleNamespace(
        fields=[dataset.fields.by_name("Region")],
        data={
            "sources": {
                "filters": [{"guid": "old-filter"}],
                "updates": [
                    {"action": "other"},
                    {"action": "add", "field": {"calc_mode": "direct"}},
                    {"action": "add", "field": {"calc_mode": "formula"}},
                ],
            }
        },
    )
    definition = {
        "dataset": "retail",
        "type": "line",
        "title": "Line",
        "fields": {},
        "filters": [],
        "description": "Description",
        "axis_titles": {"x": "Axis"},
        "labels": ["Region"],
        "labels_position": "outside",
        "totals": False,
        "settings": {"color_by_measure_name": {"colors_map": {"Revenue": "red"}}},
    }
    wizard.configure(builder, None, datasets, definition)
    builder.delete_filter.assert_called_once_with("old-filter")
    builder.color_by_measure_name.assert_called_once_with(
        colors_map={dataset.fields.by_name("Revenue"): "red"}
    )
    with pytest.raises(DataLensUtilsError, match="needs a field"):
        wizard._settings(builder, dataset, {}, {"settings": {"column_title": {"title": "New"}}})
    wizard._layers(
        builder,
        dataset,
        {},
        {
            "dataset": "retail",
            "type": "geolayer",
            "map_center": {"mode": "auto"},
            "layers": [
                {
                    "type": "geopoint",
                    "dataset": "delivery",
                    "geopoint": "Coordinates",
                    "tooltips": ["Region"],
                    "filters": [{"field": "Region", "operation": "IN", "values": ["North"]}],
                }
            ],
        },
        datasets,
    )
    builder.add_dataset.assert_called_once_with(datasets["delivery"])
    assert builder.add_layer.call_args.kwargs["dataset"] is datasets["delivery"]
    builder.map_center.assert_called_once_with(mode="auto")


def test_wizard_change_rejects_visualization_dataset_and_local_declaration(session_state):
    datasets = configured_datasets(read_config("DL objects/datasets.json"))
    definition = {"type": "line", "name": "Line", "dataset": "retail"}
    chart = SimpleNamespace(visualization_id="wrong", dataset_ids=(), data={})
    with pytest.raises(DataLensUtilsError, match="visualization"):
        wizard.validate_change(chart, None, datasets, definition)
    chart.visualization_id = wizard.VISUALIZATIONS["line"]
    with pytest.raises(DataLensUtilsError, match="different dataset"):
        wizard.validate_change(chart, None, datasets, definition)
    chart.dataset_ids = (datasets["retail"].id,)
    definition["local_fields"] = [
        {"guid": "local", "title": "Local", "cast": "integer", "formula": "1"}
    ]
    chart.data = {
        "sources": {
            "updates": [
                {
                    "action": "add",
                    "field": {"guid": "local", "calc_mode": "formula", "title": "Old"},
                }
            ]
        }
    }
    with pytest.raises(DataLensUtilsError, match="stable declaration"):
        wizard.validate_change(chart, None, datasets, definition)


def test_editor_verifier_rejects_wrong_renderer(session_state):
    definition = read_chart_definitions()["js_table"]
    datasets = configured_datasets(read_config("DL objects/datasets.json"))
    chart = SimpleNamespace(category="wizard", wire_type="wrong", data={}, description="")
    assert "Editor renderer" in editor.issues(chart, None, datasets, definition)


def test_wizard_replaces_owned_formula_in_saved_chart(session_state):
    datasets = configured_datasets(read_config("DL objects/datasets.json"))
    builder = Mock()
    builder.chart = SimpleNamespace(
        fields=[],
        data={
            "sources": {
                "updates": [
                    {
                        "action": "add",
                        "field": {"guid": "local", "calc_mode": "formula", "formula": "old"},
                    }
                ]
            }
        },
    )
    definition = {
        "type": "indicator",
        "dataset": "retail",
        "filters": [{"field": "Region", "operation": "IN", "values": ["North"]}],
        "local_fields": [{"guid": "local", "title": "Local", "formula": "1", "cast": "integer"}],
        "fields": {},
    }
    wizard.configure(builder, None, datasets, definition)
    builder.replace_formula.assert_called_once_with("local", formula="1")
    assert builder.add_filter.call_args.kwargs == {"operation": "IN", "values": ["North"]}
