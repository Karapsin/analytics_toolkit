"""Grouped widgets and dependent selector settings are checked explicitly."""

import copy
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.validation import dashboard, recipe


@pytest.mark.parametrize("drift", ["wrapper", "count", "chart", "title", "default", "parameters"])
def test_grouped_widget_drift_reports_the_specific_gap(drift):
    wanted = {
        "chart": SimpleNamespace(id="chart"),
        "title": "Chart",
        "default": True,
        "params": {"alpha": ["0.01"]},
    }
    widget = {
        "chartId": "chart",
        "title": "Chart",
        "isDefault": True,
        "params": {"alpha": ["0.01"]},
    }
    actual = copy.deepcopy(widget)
    for name, key, value in (
        ("chart", "chartId", "other"),
        ("title", "title", "Changed"),
        ("default", "isDefault", False),
        ("parameters", "params", {}),
    ):
        if drift == name:
            actual[key] = value
    item = SimpleNamespace(
        id="group",
        item_type="wrong" if drift == "wrapper" else "widget",
        data={"tabs": [] if drift == "count" else [actual]},
    )
    tab = SimpleNamespace(items=[item], global_items=[], layout=[])
    issues = dashboard.item_issues(
        tab, "group", {"kind": "chart_group", "tabs": [wanted], "at": [0, 0, 36, 10]}, {}
    )
    assert (
        "chart group tabs"
        if drift in {"wrapper", "count"}
        else "chart group reference, title, default or parameters"
    ) in issues


def test_cascading_selectors_require_enabled_dashboard_setting():
    contents = {
        "business": {
            "selectors": {
                "period_control": {
                    "key": "period",
                    "source": {"kind": "manual"},
                    "recipients": ["date"],
                },
                "date_control": {"key": "date", "source": {"kind": "dataset"}, "recipients": []},
            }
        }
    }
    assert dashboard.requires_dependent_selectors(contents)
    view = SimpleNamespace(tabs=[], data={}, raw={}, validate=list)
    issues = dashboard.dashboard_issues(
        view,
        tab_definitions={},
        contents=contents,
        datasets={},
        charts={},
        chart_definitions={},
        description="",
        hide_tabs=False,
    )
    assert issues == ["dependent selectors disabled"]


def test_manual_wizard_parameter_selects_numeric_control_coverage():
    definition = {
        "source": {"kind": "manual", "param_name": "threshold"},
        "control": {"element": "input"},
        "recipients": ["dependent_date_selector", "chart"],
    }
    assert (
        recipe._native_variant(
            definition,
            {"sales": {"parameters": {"threshold": {"type": "float"}}}},
            {"chart": {"family": "wizard", "dataset": "sales"}},
        )
        == "numeric_input"
    )
