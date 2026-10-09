"""Unsupported UI declarations are rejected before persistence."""

import json

import pytest
from analytics_toolkit.datalens_utils.bi_dashboard import grid_position, scope_tabs
from analytics_toolkit.datalens_utils.errors import (
    DataLensConfigurationError,
    DataLensUtilsError,
)

from tests.datalens_utils._support.bi import write


@pytest.mark.parametrize("position", [None, [1, 2], [True, 0, 1, 2], ["x", 0, 1, 2]])
def test_layout_requires_four_numeric_components(position):
    with pytest.raises(DataLensConfigurationError, match="Layout"):
        grid_position(position, "widget")


@pytest.mark.parametrize("scope", [None, "unknown", [], ["missing"], ["main", "main"]])
def test_selector_scope_rejects_unknown_or_duplicate_tabs(scope):
    with pytest.raises(DataLensConfigurationError, match="scope"):
        scope_tabs(scope, "main", {"main": {}})


def test_selector_display_and_influence_scopes_are_independent():
    tabs = {"main": {}, "other": {}}
    assert scope_tabs("all", "main", tabs) == ("main", "other")
    assert scope_tabs("all_tabs", "main", tabs, influence=True) == ("main", "other")
    assert scope_tabs("current", "main", tabs) == ("main",)
    assert scope_tabs(["other"], "main", tabs) == ("other",)


@pytest.mark.parametrize(
    ("file", "value", "message"),
    [
        ("widgets.json", {"widget": {"chart": "unknown", "tab": "main"}}, "known chart and tab"),
        ("chart_groups.json", {"group": {"tab": "main", "charts": []}}, "known tab and members"),
        (
            "chart_groups.json",
            {"group": {"tab": "main", "charts": [{"key": "a", "chart": "unknown"}]}},
            "Unknown or repeated",
        ),
        (
            "chart_groups.json",
            {
                "group": {
                    "tab": "main",
                    "charts": [{"key": "a", "chart": "trend"}, {"key": "a", "chart": "trend"}],
                }
            },
            "keys must be unique",
        ),
        ("titles.json", {"missing": {"title": {"text": "Missing"}}}, "unknown tab"),
        ("layout.json", {}, "explicit layout"),
        ("links/connections.json", {"unknown": ["trend"]}, "wiring identity"),
        ("tabs.json", {"other": {"title": "Other"}}, "unknown business tab"),
    ],
)
def test_invalid_ui_is_rejected_without_runtime_writes(bi_project, file, value, message):
    write(bi_project.paths.project_root, "configs/UI/" + file, value)
    with pytest.raises(DataLensConfigurationError, match=message):
        bi_project.validate()
    assert not bi_project.paths.runtime_root.exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"family": "editor", "type": "table", "fields": {}}, "[Aa]ction"),
        ({"type": "indicator", "fields": {"y": ["Amount"]}}, "[Aa]ction"),
        ({"presentation": {"unknown": True}}, "Unknown widget presentation"),
    ],
)
def test_cross_filtering_and_presentation_are_explicitly_validated(bi_project, change, message):
    root = bi_project.paths.project_root
    path = "configs/DL objects/charts/wizard/line/trend.json"
    definition = json.loads((root / path).read_text())
    definition.update(change)
    if "presentation" not in change:
        definition["enable_action_params"] = True
    write(root, path, definition)
    with pytest.raises(DataLensUtilsError, match=message):
        bi_project.validate()
    assert not bi_project.paths.runtime_root.exists()
