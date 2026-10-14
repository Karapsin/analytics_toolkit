"""Offline source-parity and relocation regressions for a repeatedly placed tab."""

import copy
import json
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.bi_import_resources import dataset_definition
from analytics_toolkit.datalens_utils.dashboard_generation.charts import editor
from analytics_toolkit.datalens_utils.errors import DataLensConfigurationError, DataLensUtilsError
from analytics_toolkit.datalens_utils.import_copy import chart_defaults
from datalens_sdk import Connection, DashboardTab, DataLensClientYC, NoAuthProvider

from tests.datalens_utils._support.bi import dataset, write
from tests.datalens_utils._support.sdk import MemoryDataLens


def make_source(api, key):
    original = dataset()
    fields = []
    for index in range(6):
        measure = index in (1, 2)
        fields.append(
            {
                "guid": key + str(index),
                "title": "value" + str(index),
                "source": "value" + str(index),
                "avatar_id": "avatar",
                "calc_mode": "direct",
                "cast": "float" if measure else "string",
                "data_type": "float" if measure else "string",
                "type": "MEASURE" if measure else "DIMENSION",
                "aggregation": "sum" if measure else "none",
            }
        )
    for name, default in (("period_type", "month"), ("value_type", "absolute")):
        fields.append(
            {
                "guid": key + name,
                "title": name,
                "calc_mode": "parameter",
                "cast": "string",
                "type": "DIMENSION",
                "default_value": default,
            }
        )
    return replace(original, id=key, name=key, location=api.folder, result_schema=tuple(fields))


def audit_tab(api):
    datasets = {key: make_source(api, key) for key in ("first", "second")}
    charts = {}
    for index in range(13):
        ds = datasets["first" if index % 2 else "second"]
        builder = (
            api.client.create.wizard_chart.line(name="Chart " + str(index), location=api.folder)
            .dataset(ds)
            .x([ds.fields.by_name("value0")])
            .y([ds.fields.by_name("value1"), ds.fields.by_name("value2")])
        )
        if index == 0:
            builder.color_by_dimension(ds.fields.by_name("value0"))
        else:
            builder.color_by_measure_name(colors_map={ds.fields.by_name("value1"): "#ff0000"})
            builder.shape_by_measure_name(shapes_map={ds.fields.by_name("value2"): "circle"})
        chart = builder.build()
        chart = replace(
            chart, data={**chart.data, "params": {"period_type": ["week"], "value_type": ["share"]}}
        )
        charts[chart.id] = chart
    for index in range(2):
        ds = datasets["first" if index == 0 else "second"]
        chart = (
            api.client.create.editor_chart.selector(
                name="Selector " + str(index), location=api.folder
            )
            .meta(json.dumps({"links": {"dataset": ds.id}, "authored": {"keep": True}}))
            .params("module.exports = {};")
            .sources("module.exports = {};")
            .controls("module.exports = [];")
            .build()
        )
        charts[chart.id] = chart
    tab = DashboardTab("Audit", tab_id="audit")
    wizard_charts = [value for value in charts.values() if value.category == "wizard"]
    for index in range(35):
        tab.add_chart(
            wizard_charts[index % 13],
            item_id="placement" + str(index),
            params={"period_type": ["day"]} if index == 0 else {},
            at=((index % 2) * 12, (index // 2) * 6, 12, 6),
        )
    for index, chart in enumerate(value for value in charts.values() if value.category == "editor"):
        tab.add_selector(chart=chart, item_id="selector" + str(index), at=(index * 12, 108, 12, 2))
    dashboard = api.client.create.dashboard(name="Source", location=api.folder).add_tab(tab).build()
    client = SimpleNamespace(
        create=api.client.create,
        capabilities=api.client.capabilities,
        INSTALLATION=api.client.INSTALLATION,
        navigation=SimpleNamespace(get_entries=lambda **kwargs: iter(())),
        get=SimpleNamespace(
            dashboard=lambda **kwargs: dashboard,
            chart=lambda **kwargs: charts[kwargs["by_id"]],
            dataset=lambda **kwargs: datasets[kwargs["by_id"]],
            connection=lambda **kwargs: Connection(
                id="ch", name="CH", type="clickhouse", installation="yacloud"
            ),
        ),
    )
    return client, dashboard, charts


def clear_destination(project):
    root = project.paths.project_root
    write(root, "configs/DL objects/datasets.json", {})
    write(root, "configs/DL objects/dashboard.json", {})
    write(root, "configs/UI/layout.json", {})
    write(root, "configs/UI/tabs.json", {})
    (root / "configs/DL objects/charts/wizard/line/trend.json").unlink()


def test_copy_audit_tab_retains_all_placements_defaults_encodings_and_links(bi_project):
    api = MemoryDataLens()
    client, source, charts = audit_tab(api)
    clear_destination(bi_project)
    bi_project.client_factory = lambda _: nullcontext(client)
    before = copy.deepcopy(api.documents)
    preview = bi_project.import_tab(dashboard_id=source.id, tab_id="audit", mode="copy")
    assert not preview["blockers"], preview["blockers"]
    assert not preview["written"]
    files = preview["proposed_files"]
    copied = [value for name, value in files.items() if "/charts/" in name]
    assert len(copied) == 15
    assert len(files["configs/DL objects/datasets.json"]) == 2
    assert len(files["configs/UI/widgets.json"]) == 35
    assert all("id" not in value and value["copy_from"] in charts for value in copied)
    assert all("id" not in value for value in files["configs/DL objects/datasets.json"].values())
    placements = files["configs/UI/widgets.json"]
    assert placements["placement0"]["params"]["period_type"] == ["day"]
    assert placements["placement1"]["params"] == {"period_type": ["week"], "value_type": ["share"]}
    assert all(
        value["meta_links_mode"] == "managed" for value in copied if value["family"] == "editor"
    )
    assert any(
        "color_by_dimension" in value["settings"] for value in copied if value["family"] == "wizard"
    )
    assert any(
        "shape_by_measure_name" in value["settings"]
        for value in copied
        if value["family"] == "wizard"
    )
    assert files["configs/DL objects/connections.json"]["ch"]["id"] == "ch"
    assert api.documents == before
    assert not bi_project.paths.runtime_root.exists()
    merged = bi_project.import_tab(
        dashboard_id=source.id, tab_id="audit", mode="copy", dry_run=False
    )
    assert merged["written"], merged["blockers"]
    assert bi_project.validate()["coverage"]["schema_version"] == 2
    assert api.documents == before
    api.client.close()


def test_source_setting_loss_blocks_import(bi_project):
    api = MemoryDataLens()
    client, source, charts = audit_tab(api)
    chart = next(iter(charts.values()))
    chart.data["visualization"]["x"]["settings"]["unknownGroupingMode"] = "semantic"
    clear_destination(bi_project)
    bi_project.client_factory = lambda _: nullcontext(client)
    result = bi_project.import_dashboard(dashboard_id=source.id, mode="copy", dry_run=False)
    assert not result["written"]
    assert any("unknownGroupingMode" in value["reason"] for value in result["blockers"])
    assert any(value["status"] == "unsupported" for value in result["fidelity"])
    api.client.close()


def test_import_source_metadata_normalizes_manual_and_blocks_unknown_parameters():
    source = dataset()
    source = replace(
        source,
        sources=(
            replace(source.sources[0], parameters={**source.sources[0].parameters, "manual": True}),
        ),
    )
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        definition, _ = dataset_definition(client, source, {"ch": "ch"})
        parameters = next(iter(definition["sources"].values()))["parameters"]
        assert "manual" not in parameters
        source = replace(
            source,
            sources=(replace(source.sources[0], parameters={**parameters, "unknown": True}),),
        )
        with pytest.raises(DataLensUtilsError, match="not constructible"):
            dataset_definition(client, source, {"ch": "ch"})


def test_managed_editor_meta_rewrites_only_managed_aliases(session_state):
    root = session_state.paths.project_root
    definition = {
        "family": "editor",
        "type": "table",
        "name": "Copy",
        "scripts": {"meta": "meta.json", "prepare": "prepare.js"},
        "links": {"dataset": "sales"},
        "meta_links_mode": "managed",
    }
    write(root, "meta.json", {"links": {"dataset": "old", "other": "external"}, "authored": [1, 2]})
    (root / "prepare.js").write_text("module.exports = {};")
    for tab in ("config", "controls", "params", "sources"):
        definition["scripts"][tab] = "prepare.js"
    sources = editor._sources({"sales": replace(dataset(), id="new")}, definition)
    meta = json.loads(sources["meta"])
    assert meta == {"links": {"dataset": "new", "other": "external"}, "authored": [1, 2]}
    definition.pop("meta_links_mode")
    with pytest.raises(DataLensUtilsError, match="differ"):
        editor._sources({"sales": replace(dataset(), id="new")}, definition)


def test_copy_requires_new_dashboard_and_runtime_and_valid_mode(bi_project):
    from tests.datalens_utils.bi.importing import (  # noqa: PLC0415 - Reused offline fixture.
        source_dashboard,
    )

    bi_project.client_factory = lambda _: nullcontext(
        SimpleNamespace(get=SimpleNamespace(dashboard=lambda **kwargs: source_dashboard()))
    )
    assert bi_project.import_dashboard(dashboard_id="source", mode="copy")["blockers"]
    with pytest.raises(DataLensConfigurationError, match="mode"):
        bi_project.import_dashboard(dashboard_id="source", mode="move")
    clear_destination(bi_project)
    write(
        bi_project.paths.runtime_root,
        "resources.json",
        {"resources": {"dataset:old": {"id": "source"}}},
    )
    assert bi_project.import_dashboard(dashboard_id="source", mode="copy")["blockers"]


def test_unexpressible_defaults_are_not_silently_dropped():
    chart = SimpleNamespace(category="wizard", data={"params": []}, raw={})
    with pytest.raises(DataLensUtilsError, match="mapping"):
        chart_defaults(chart)

@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("setting", ["color_by_measure_name", "shape_by_measure_name"])
def test_unsupported_scatter_encoding_blocks_copy_import(bi_project, dry_run, setting):
    api = MemoryDataLens()
    try:
        client, source, charts = audit_tab(api)
        original = next(iter(charts.values()))
        ds = client.get.dataset(by_id=original.dataset_ids[0])
        scatter = (
            api.client.create.wizard_chart.scatter(name=original.name, location=api.folder)
            .dataset(ds)
            .x([ds.fields.by_name("value1")])
            .y([ds.fields.by_name("value2")])
            .build()
        )
        slot, map_name = (
            ("colors", "mountedColors")
            if setting == "color_by_measure_name"
            else ("shapes", "mountedShapes")
        )
        scatter.data["visualization"][slot] = {
            "items": [{"type": "PSEUDO"}],
            "settings": {
                map_name: {
                    ds.fields.by_name("value1").guid: "red"
                    if setting == "color_by_measure_name"
                    else "circle",
                },
            },
        }
        charts[original.id] = replace(scatter, id=original.id)
        clear_destination(bi_project)
        bi_project.client_factory = lambda _: nullcontext(client)
        before = {
            path: path.read_bytes()
            for path in bi_project.paths.project_root.rglob("*")
            if path.is_file()
        }
        documents = copy.deepcopy(api.documents)
        api.writes.clear()
        result = bi_project.import_dashboard(dashboard_id=source.id, mode="copy", dry_run=dry_run)
        assert not result["written"]
        assert any(
            "scatter" in value["reason"] and setting in value["reason"]
            for value in result["blockers"]
        )
        assert any(
            value["status"] == "unsupported" and setting in value["reason"]
            for value in result["fidelity"]
        )
        assert {
            path: path.read_bytes()
            for path in bi_project.paths.project_root.rglob("*")
            if path.is_file()
