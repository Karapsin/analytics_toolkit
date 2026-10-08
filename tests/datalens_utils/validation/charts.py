"""Typed factory expectations detect corrupted metadata for every Wizard family."""

import copy
import dataclasses
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.validation import charts as verify
from datalens_sdk import DataLensValidationError

from tests.datalens_utils.gallery import GalleryTests


@pytest.fixture
def variants(session_state):
    value = GalleryTests()
    value.setUp()
    try:
        yield value, value.build_charts()
    finally:
        value.doCleanups()


def test_corrupt_wizard_metadata_reports_each_declared_property(variants):
    gallery, charts = variants
    detected = set()
    for role, actual in charts.items():
        definition = gallery.definitions[role]
        if definition["family"] != "wizard":
            continue
        data = copy.deepcopy(actual.data)
        visualization = data.setdefault("visualization", {})
        visualization["chartSettings"] = {}
        for key in (
            "colors",
            "shapes",
            "size",
            "labels",
            "x",
            "y",
            "y2",
            "columns",
            "rows",
            "measures",
            "sort",
            "points",
            "segments",
        ):
            visualization[key] = {"items": [], "settings": {}}
        for layer in visualization.get("layers", []):
            layer["type"] = "wrong"
            layer["layerSettings"] = {"id": "wrong", "name": "wrong", "alpha": 0}
            for key in (
                "points",
                "polygons",
                "polylines",
                "grouping",
                "size",
                "colors",
                "tooltip",
                "labels",
                "y",
                "y2",
                "x",
                "sort",
                "filters",
            ):
                layer[key] = {"items": [], "settings": {}}
        data["sources"] = {"filters": []}
        fields = Mock()
        fields.by_guid.side_effect = DataLensValidationError("offline missing field")
        corrupt = SimpleNamespace(
            category="wrong",
            visualization_id="wrong",
            dataset_ids=(),
            data=data,
            fields=fields,
            raw={},
            location=actual.location,
        )
        issues = verify.wizard_issues(corrupt, gallery.context, gallery.datasets, definition)
        assert "family" in issues
        assert "visualization" in issues
        assert "dataset reference" in issues
        detected.update(issues)
    assert "chart settings" in detected
    assert "sorting" in detected
    assert any("formatting" in issue for issue in detected)


def test_layer_count_and_heatmap_slot_alias_are_checked():
    field = SimpleNamespace(guid="point")
    spec = SimpleNamespace(
        geo_layers=[{"layer_type": "heatmap", "alpha": 0.5, "geopoint": field}],
        combined_layers=[],
        slots={},
    )
    assert verify._layer_issues({"layers": []}, spec) == ["layer count"]
    actual = {
        "layers": [
            {
                "type": "heatmap",
                "layerSettings": {"name": "Layer 1", "alpha": 0.5},
                "heatmap": {"items": [{"guid": "wrong"}]},
            }
        ]
    }
    assert "layer 1 geopoint fields" in verify._layer_issues(actual, spec)


def test_compatibility_entrypoint_and_unknown_family(variants):
    gallery, charts = variants
    chart = charts["wizard_line"]
    definition = gallery.definitions["wizard_line"]
    dataset = gallery.datasets[definition["dataset"]]
    assert (
        verify.chart_issues(
            chart, dataset, definition, context=gallery.context, datasets=gallery.datasets
        )
        == []
    )
    assert verify.chart_issues(chart, dataset, definition) == []
    with pytest.raises(TypeError, match="requires context"):
        verify.chart_issues(chart, dataset, {"family": "ql"})
    assert verify.check_chart(
        chart, context=gallery.context, datasets=gallery.datasets, definition={"family": "unknown"}
    ) == ["unknown family unknown"]


@pytest.mark.parametrize("color_kind", ["measure_name", "dimension"])
def test_optional_style_encodings_and_filter_mismatches(variants, color_kind):
    gallery, charts = variants
    chart = charts["wizard_line"]
    definition = {**gallery.definitions["wizard_line"], "filters": []}
    builder = verify.wizard.create_builder(
        gallery.context, gallery.datasets, definition, gallery.backend.folder
    )
    original = builder.to_spec()
    spec = SimpleNamespace(
        **{field.name: getattr(original, field.name) for field in dataclasses.fields(original)}
    )
    field = gallery.datasets["retail"].fields.by_name("Region")
    spec.color_encoding = SimpleNamespace(
        kind=color_kind,
        colors_map={field: "red"},
        field=field,
        gradient_mode="continuous",
        gradient_palette="red",
        reversed=True,
    )
    spec.colors_palette = "required-palette"
    spec.shape_encoding = SimpleNamespace(
        field=field, shapes_map={field: "circle", "Other": "square"}
    )
    spec.geopoints_config = {"min": 1}
    spec.label_mode = "percent"
    spec.labels_position = "outside"
    spec.description = "Expected"
    spec.slot_settings = {"x": {"required": True}}
    spec.pending_filters = [(field, "IN", ("North",))]
    data = copy.deepcopy(chart.data)
    data["visualization"]["colors"] = {"items": [{"type": "OTHER"}], "settings": {}}
    data["visualization"]["shapes"] = {"items": [], "settings": {}}
    data["visualization"]["labels"] = {"items": [{"formatting": {}}], "settings": {}}
    data["sources"]["filters"] = []
    corrupt = SimpleNamespace(
        category=chart.category,
        visualization_id=chart.visualization_id,
        dataset_ids=chart.dataset_ids,
        data=data,
        fields=chart.fields,
        raw={},
        location=chart.location,
    )
    with patch.object(
        verify.wizard, "create_builder", return_value=SimpleNamespace(to_spec=lambda: spec)
    ):
        problems = verify.wizard_issues(corrupt, gallery.context, gallery.datasets, definition)
    assert {
        "color fields",
        "color settings",
        "palette settings",
        "shape fields",
        "shape settings",
        "size settings",
        "label mode",
        "label position",
        "description",
        "filters",
        "x settings",
    }.issubset(problems)


def test_layer_filter_drift_and_changed_local_formula(variants):
    gallery, charts = variants
    field = gallery.datasets["retail"].fields.by_name("Region")
    spec = SimpleNamespace(
        geo_layers=[
            {
                "layer_type": "geopoint",
                "alpha": 0.5,
                "filters": [SimpleNamespace(field=field, operation="IN", values=["North"])],
            }
        ],
        combined_layers=[],
        slots={},
    )
    assert "layer 1 filters" in verify._layer_issues(
        {"layers": [{"type": "geopoint", "layerSettings": {"name": "Layer 1", "alpha": 0.5}}]}, spec
    )
    definition = gallery.definitions["wizard_flat_table"]
    chart = charts["wizard_flat_table"]
    # Use a typed factory spec and a tolerant read view with a changed local field.
    factory = verify.wizard.create_builder(
        gallery.context, gallery.datasets, definition, gallery.backend.folder
    )
    original = factory.to_spec()
    if original.local_fields:
        proxy = Mock()
        proxy.by_guid.return_value = SimpleNamespace(
            formula="wrong", cast="wrong", type="wrong", aggregation="wrong"
        )
        changed = SimpleNamespace(
            category=chart.category,
            visualization_id=chart.visualization_id,
            dataset_ids=chart.dataset_ids,
            data=chart.data,
            fields=proxy,
            raw=chart.raw,
            location=chart.location,
        )
        assert any(
            "formula/settings" in issue
            for issue in verify.wizard_issues(
                changed, gallery.context, gallery.datasets, definition
            )
        )


@pytest.mark.parametrize("shape_map", [False, True])
def test_matching_optional_color_shape_slot_and_filter_metadata(variants, shape_map):
    gallery, charts = variants
    chart = charts["wizard_line"]
    definition = {**gallery.definitions["wizard_line"], "filters": []}
    original = verify.wizard.create_builder(
        gallery.context, gallery.datasets, definition, gallery.backend.folder
    ).to_spec()
    spec = SimpleNamespace(
        **{field.name: getattr(original, field.name) for field in dataclasses.fields(original)}
    )
    field = gallery.datasets["retail"].fields.by_name("Region")
    spec.slot_settings = {"x": {"required": True}}
    spec.color_encoding = SimpleNamespace(
        kind="measure_name",
        colors_map={field: "red"},
        gradient_mode=None,
        gradient_palette=None,
        reversed=None,
    )
    spec.shape_encoding = SimpleNamespace(
        field=None, shapes_map={field: "circle"} if shape_map else {}
    )
    spec.pending_filters = []
    data = copy.deepcopy(chart.data)
    data["visualization"]["x"]["settings"] = {"required": True}
    data["visualization"]["colors"] = {
        "items": [{"type": "PSEUDO"}],
        "settings": {"mountedColors": {field.guid: "red"}},
    }
    data["visualization"]["shapes"] = {
        "items": [],
        "settings": {"mountedShapes": {field.guid: "circle"}},
    }
    data.setdefault("sources", {})["filters"] = []
    view = SimpleNamespace(
        category=chart.category,
        visualization_id=chart.visualization_id,
        dataset_ids=chart.dataset_ids,
        data=data,
        fields=chart.fields,
        raw=chart.raw,
        location=chart.location,
    )
    with patch.object(
        verify.wizard, "create_builder", return_value=SimpleNamespace(to_spec=lambda: spec)
    ):
        assert verify.wizard_issues(view, gallery.context, gallery.datasets, definition) == []
