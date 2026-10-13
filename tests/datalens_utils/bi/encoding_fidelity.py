"""Observed encoding details are either expressible or explicit import blockers."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.bi_import_resources import (
    chart_definition,
    dataset_definition,
)
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.import_copy import (
    chart_defaults,
    check_copy_key,
    place_defaults,
    relocate,
)
from analytics_toolkit.datalens_utils.import_fidelity import wizard_encodings, wizard_source_issues
from datalens_sdk import DataLensClientYC, NoAuthProvider

from tests.datalens_utils._support.bi import dataset
from tests.datalens_utils._support.sdk import MemoryDataLens
from tests.datalens_utils.bi.copy_fidelity import make_source


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"items": [], "settings": {"palette": "default"}}, "unbound"),
        ({"items": [{"guid": "a"}, {"guid": "b"}]}, "multiple"),
        ({"items": [{"guid": "missing"}]}, "Unknown"),
        ({"items": [{"guid": "a"}], "settings": {"unknown": True}}, "Unsupported"),
        ({"items": [{"guid": "a"}], "settings": {"mountedColors": {"a": "red"}}}, "color maps"),
        ({"items": [{"guid": "a"}], "settings": {"colorMode": "gradient"}}, "color setting"),
        ({"items": [{"guid": "a"}], "settings": {"gradientMode": "2-point"}}, "Gradient"),
    ],
)
def test_unexpressible_color_metadata_is_rejected(section, message):
    with pytest.raises(DataLensUtilsError, match=message):
        wizard_encodings({"colors": section}, {"a": "Dimension"})


def test_gradient_and_dimension_shape_roundtrip_through_public_builders():
    api = MemoryDataLens()
    ds = make_source(api, "source")
    chart = (
        api.client.create.wizard_chart.scatter(name="Scatter", location=api.folder)
        .dataset(ds)
        .x([ds.fields.by_name("value1")])
        .y([ds.fields.by_name("value2")])
        .color_by_measure(
            ds.fields.by_name("value1"), mode="2-point", palette="blue", reversed=True
        )
        .shape_by_dimension(ds.fields.by_name("value0"), shapes_map={"visit": "circle"})
        .build()
    )
    definition, _ = chart_definition(chart, {"sales": ds}, {}, "one")
    assert definition["settings"]["color_by_measure"] == {
        "field": "value1",
        "mode": "2-point",
        "palette": "blue",
        "reversed": True,
    }
    assert definition["settings"]["shape_by_dimension"] == {
        "field": "value0",
        "shapes_map": {"visit": "circle"},
    }
    assert not wizard_source_issues(api.client, chart, {"sales": ds}, definition)
    api.client.close()


def test_defaults_on_group_members_preserve_narrow_overrides():
    report = {"fidelity": []}
    chart = SimpleNamespace(category="wizard", data={}, raw={"params": {"period": ["week"]}})
    ui = {
        "configs/UI/widgets.json": {},
        "configs/UI/chart_groups.json": {
            "group": {
                "charts": [{"chart": "trend", "params": {"period": ["day"]}}, {"chart": "trend"}]
            }
        },
    }
    place_defaults(ui, {"id": chart}, {"id": "trend"}, report)
    assert [item["params"] for item in ui["configs/UI/chart_groups.json"]["group"]["charts"]] == [
        {"period": ["day"]},
        {"period": ["week"]},
    ]
    chart.data["params"] = {"period": ["month"]}
    with pytest.raises(DataLensUtilsError, match="Ambiguous"):
        chart_defaults(chart)
    chart.data["params"] = chart.raw["params"] = {"": "value"}
    with pytest.raises(DataLensUtilsError, match="nonempty"):
        chart_defaults(chart)


def test_copy_relocation_resets_relations_and_protects_local_keys():
    value = {
        "id": "old",
        "sources": {"facts": {"id": "source", "avatar_id": "avatar"}},
        "relations": {"join": {"id": "join", "left": "facts"}},
    }
    relocate(value, "dataset", "old", {"fidelity": []}, "dataset:sales")
    assert value["sources"] == {"facts": {}}
    assert value["relations"] == {"join": {"left": "facts"}}
    with pytest.raises(DataLensUtilsError, match="already exists"):
        check_copy_key(SimpleNamespace(resources={"dataset:sales": None}), "dataset:sales", "copy")


@pytest.mark.parametrize("problem", ["label_mode", "conflicting_formats"])
def test_per_slot_formatting_that_cannot_be_rebuilt_is_blocked(problem):
    api = MemoryDataLens()
    ds = make_source(api, "source")
    chart = (
        api.client.create.wizard_chart.line(name="Line", location=api.folder)
        .dataset(ds)
        .x([ds.fields.by_name("value0")])
        .y([ds.fields.by_name("value1")])
        .color_by_measure_name()
        .y2([ds.fields.by_name("value1"), ds.fields.by_name("value2")])
        .build()
    )
    chart.data["visualization"]["y"]["items"][0]["formatting"] = (
        {"labelMode": "absolute"} if problem == "label_mode" else {"precision": 2}
    )
    if problem == "conflicting_formats":
        chart.data["visualization"]["y2"]["items"][0]["formatting"] = {"precision": 4}
    with pytest.raises(DataLensUtilsError, match="faithfully"):
        chart_definition(chart, {"sales": ds}, {}, "one")
    api.client.close()


def test_import_preserves_label_mode_on_the_label_slot():
    api = MemoryDataLens()
    ds = make_source(api, "source")
    chart = (
        api.client.create.wizard_chart.line(name="Labels", location=api.folder)
        .dataset(ds)
        .x([ds.fields.by_name("value0")])
        .y([ds.fields.by_name("value1")])
        .labels([ds.fields.by_name("value1")])
        .label_mode(mode="absolute")
        .build()
    )
    definition, _ = chart_definition(chart, {"sales": ds}, {}, "one")
    assert definition["settings"]["label_mode"] == {"mode": "absolute"}
    assert not wizard_source_issues(api.client, chart, {"sales": ds}, definition)
    api.client.close()


def test_dataset_direct_formatting_is_preserved_and_unmodeled_formula_metadata_blocks():
    original = dataset()
    formatting = '{"numberFormatting": {"precision": 2}}'
    value = replace(
        original,
        result_schema=(
            {**original.result_schema[0], "ui_settings": formatting},
            original.result_schema[1],
        ),
    )
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        definition, _ = dataset_definition(client, value, {"ch": "ch"})
        assert next(iter(definition["fields"].values()))["ui_settings"] == formatting
        value = replace(
            value,
            result_schema=(value.result_schema[0], {**value.result_schema[1], "hidden": True}),
        )
        with pytest.raises(DataLensUtilsError, match="cannot be imported faithfully"):
            dataset_definition(client, value, {"ch": "ch"})
