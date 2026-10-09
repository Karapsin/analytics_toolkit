"""Authored dataset and chart import fidelity through public SDK read models."""

import json
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.bi_import_resources import (
    chart_definition,
    dataset_definition,
    semantic_key,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import (
    Connection,
    DataLensClientYC,
    EditorChart,
    EntryLocation,
    NoAuthProvider,
    QLChart,
    QLColumn,
    QLParam,
)

from tests.datalens_utils._support.bi import dataset
from tests.datalens_utils._support.sdk import MemoryDataLens


def rich_dataset():
    original = dataset()
    return replace(
        original,
        result_schema=(
            *original.result_schema,
            {
                "guid": "period",
                "title": "Period",
                "calc_mode": "parameter",
                "cast": "string",
                "default_value": "month",
            },
        ),
        rls2={
            "category": [
                {
                    "subject": {"subject_id": "reader", "subject_type": "user"},
                    "allowed_value": "West",
                    "pattern_type": "value",
                }
            ]
        },
        obligatory_filters=(
            {
                "id": "region-filter",
                "field_guid": "category",
                "default_filters": [{"operation": "EQ", "values": ["West"]}],
            },
        ),
        raw={"dataset": {"settings": {"data_export_forbidden": True}}},
    )


def test_dataset_import_preserves_fields_parameters_rules_settings_and_sql_assets():
    original = rich_dataset()
    source = replace(
        original.sources[0],
        source_type="CH_SUBSELECT",
        parameters={"db_name": "example", "subsql": "SELECT category FROM sales"},
    )
    value = replace(
        original,
        sources=(source,),
        raw={
            "dataset": {
                **original.raw["dataset"],
                "cache_invalidation_source": {
                    "mode": "sql",
                    "sql": "SELECT max(version) FROM sales",
                    "filters": [original.obligatory_filters[0]],
                },
            }
        },
    )
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        definition, assets = dataset_definition(client, value, {"ch": "connection"})
    source_definition = next(iter(definition["sources"].values()))
    assert source_definition["factory"] == "ch_subselect"
    assert assets[source_definition["sql_file"]] == "SELECT category FROM sales"
    assert assets[definition["cache_invalidation"]["sql_file"]].startswith("SELECT max")
    assert definition["parameters"]["Period"] == {
        "guid": "period",
        "type": "string",
        "default": "month",
    }
    assert definition["calculations"]["Amount"]["formula"] == "SUM(1)"
    assert definition["settings"] == {"data_export_forbidden": True}
    assert next(iter(definition["rls"].values()))["subject_id"] == "reader"
    assert definition["default_filters"][0]["values"] == ["West"]
    assert definition["cache_invalidation"]["filters"][0]["key"] == "region-filter"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("factory", "factory unavailable"),
        ("avatar", "one known source avatar"),
        ("calculation", "Unknown dataset field"),
        ("duplicate_formula", "Duplicate calculated field"),
        ("filter", "Default filter form"),
        ("cache_filter", "Cache filter form"),
    ],
)
def test_dataset_import_rejects_unexpressible_metadata(change, message):
    value = rich_dataset()
    if change == "factory":
        value = replace(value, sources=(replace(value.sources[0], source_type="UNKNOWN"),))
    elif change == "avatar":
        value = replace(value, source_avatars=())
    elif change == "calculation":
        value = replace(value, result_schema=({**value.result_schema[0], "calc_mode": "unknown"},))
    elif change == "duplicate_formula":
        value = replace(value, result_schema=(*value.result_schema, value.result_schema[1]))
    elif change == "filter":
        value = replace(
            value, obligatory_filters=({"field_guid": "category", "default_filters": []},)
        )
    else:
        value = replace(
            value,
            raw={
                "dataset": {
                    "cache_invalidation_source": {
                        "mode": "off",
                        "filters": [{"default_filters": []}],
                    }
                }
            },
        )
    expected = pytest.raises(DataLensUtilsError, match=message)
    with DataLensClientYC(auth=NoAuthProvider()) as client, expected:
        dataset_definition(client, value, {"ch": "ch"})


def editor_chart(**changes):
    return EditorChart(
        id="editor",
        name="Authored table",
        location=EntryLocation.path("Offline/Variants"),
        wire_type="table_node",
        data={
            "meta": json.dumps({"links": {"sales": "sales"}}),
            "prepare": "module.exports = {};",
        },
        **changes,
    )


def test_editor_authored_sources_and_dataset_links_are_preserved():
    value, assets = chart_definition(editor_chart(), {"sales": dataset()}, {}, "one")
    assert value["family"] == "editor"
    assert value["type"] == "table"
    assert value["links"] == {"sales": "sales"}
    assert assets[value["scripts"]["prepare"]] == "module.exports = {};"
    assert json.loads(assets[value["scripts"]["meta"]])["links"]["sales"] == "sales"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("renderer", "Unsupported Editor renderer"),
        ("tab", "Unsupported authored Editor tab"),
        ("text", "not authored text"),
        ("dependency", "Unresolved Editor dataset"),
    ],
)
def test_editor_import_rejects_unsupported_authored_forms(change, message):
    value = editor_chart()
    if change == "renderer":
        value.wire_type = "unknown"
    elif change == "tab":
        value.data = {"unknown": "source"}
    elif change == "text":
        value.data = {"prepare": {"dynamic": True}}
    else:
        value.data = {"meta": json.dumps({"links": {"foreign": "missing"}})}
    with pytest.raises(DataLensUtilsError, match=message):
        chart_definition(value, {"sales": dataset()}, {}, "one")


def test_whole_dashboard_import_discovers_chart_dataset_connection_closure(bi_project):
    api = MemoryDataLens()
    source = dataset()
    chart = (
        api.client.create.wizard_chart.line(name="Imported trend", location=api.folder)
        .dataset(source)
        .x([source.fields.by_name("category")])
        .y([source.fields.by_name("Amount")])
        .build()
    )
    from datalens_sdk import DashboardTab  # noqa: PLC0415 - Fixture-only SDK construction.

    tab = DashboardTab("Imported", tab_id="imported")
    tab.add_chart(chart, item_id="imported-widget", at=(0, 0, 12, 6))
    dashboard = api.client.create.dashboard(name="Source", location=api.folder).add_tab(tab).build()
    connection = Connection(id="ch", name="CH", type="clickhouse", installation="yacloud")
    client = SimpleNamespace(
        capabilities=api.client.capabilities,
        navigation=SimpleNamespace(get_entries=lambda **kwargs: iter(())),
        get=SimpleNamespace(
            dashboard=lambda **kwargs: dashboard,
            chart=lambda **kwargs: chart,
            dataset=lambda **kwargs: source,
            connection=lambda **kwargs: connection,
        ),
    )
    bi_project.client_factory = lambda _: nullcontext(client)
    before = list(api.writes)
    report = bi_project.import_dashboard(dashboard_id=dashboard.id)
    assert not report["blockers"]
    assert not report["written"]
    assert api.writes == before
    charts = [value for name, value in report["proposed_files"].items() if "/charts/" in name]
    assert len(charts) == 1
    assert (
        bi_project.paths.project_root / "configs/DL objects/charts/wizard/line/trend.json"
    ).exists()
    imported = next(value for value in charts if value["id"] == chart.id)
    assert imported["dataset"] == "sales"
    assert (
        report["proposed_files"]["configs/UI/widgets.json"]["imported-widget"]["chart"]
        == imported["key"]
    )
    assert semantic_key("chart", chart.id) == imported["key"]
    api.client.close()


def test_unknown_ql_visualization_is_explicitly_blocked():
    with pytest.raises(DataLensUtilsError, match="Unsupported QL visualization"):
        chart_definition(QLChart(id="ql", data={"visualization": {"id": "unknown"}}), {}, {}, "one")


def test_ql_import_preserves_sql_parameters_columns_and_description():
    api = MemoryDataLens()
    chart = (
        api.client.create.ql_chart.flat_table(name="SQL", location=api.folder)
        .connection(api.connection)
        .query("SELECT {{region}} AS region")
        .params([QLParam.string("region", default="West")])
        .flat_table_columns([QLColumn("region")])
        .description("Authored SQL")
        .build()
    )
    definition, assets = chart_definition(chart, {}, {api.connection.id: "ch"}, "one")
    assert definition["connection"] == "ch"
    assert definition["type"] == "flat_table"
    assert definition["description"] == "Authored SQL"
    assert definition["params"] == [{"name": "region", "type": "string", "default": "West"}]
    assert definition["fields"]["flat_table_columns"] == ["region"]
    assert assets[definition["query_file"]] == "SELECT {{region}} AS region"
    api.client.close()
