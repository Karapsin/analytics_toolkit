"""Metadata drift is reported separately from numeric or rendering checks."""

from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.validation import dashboard as verify


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        ({"from": "+1d", "to": "-1d"}, "__interval___relative_+1d___relative_-1d"),
        (
            {"from": "2020-01-01T00:00:00+03:00", "to": "2020-01-02T00:00:00"},
            "__interval_2020-01-01T00:00:00+03:00_2020-01-02T00:00:00Z",
        ),
    ],
)
def test_selector_default_canonicalization(value, expected):
    assert verify.selector_default(value) == expected


def member(kind="manual"):
    source = SimpleNamespace(
        raw={},
        dataset_id="wrong",
        dataset_field_id="wrong",
        param_name="wrong",
        element_type="input",
        multiselect=False,
        is_range=False,
        required=False,
        operation=None,
        default_value="wrong",
        acceptable_values=[],
    )
    return SimpleNamespace(
        id="filter", title="Wrong", source_type=kind, source=source, impact_type="unexpected"
    )


@pytest.mark.parametrize("kind", ["dataset", "manual"])
def test_selector_identity_bindings_control_and_options_drift(kind):
    value = member(kind)
    source = {"kind": kind, "dataset": "sales", "field": "Region", "param_name": "region"}
    definition = {
        "source": source,
        "title": "Region",
        "control": {
            "element": "select",
            "default_value": "North",
            "options": ["North", {"value": "South", "title": "South"}, ("East", "East")],
        },
    }
    dataset = SimpleNamespace(
        id="dataset", fields=SimpleNamespace(by_name=lambda name: SimpleNamespace(guid="region"))
    )
    issues = verify._member_issues(value, definition, {"sales": dataset})
    assert "selector identity or title" in issues
    assert "selector control settings" in issues
    assert "selector default" in issues
    assert "unexpected selector influence scope" in issues
    assert ("selector dataset field" if kind == "dataset" else "selector parameter name") in issues
    if kind == "manual":
        assert "selector options" in issues


def tab(item=None, controls=()):
    return SimpleNamespace(
        id="tab",
        items=[] if item is None else [item],
        global_items=[],
        controls=list(controls),
        layout=[],
        connections=[],
        aliases={},
    )


@pytest.mark.parametrize("kind", ["chart", "external_selector", "selector_group", "title", "text"])
def test_item_missing_or_wrong_wrapper_reports_specific_gap(kind):
    definition = {
        "kind": kind,
        "title": "Title",
        "text": "Text",
        "chart": SimpleNamespace(id="chart"),
        "members": ["filter"],
        "definitions": {},
        "at": [0, 0, 36, 1],
    }
    assert verify.item_issues(tab(), "item", definition, {}) == ["item missing"]
    item = SimpleNamespace(id="item", item_type="wrong", data={})
    assert verify.item_issues(tab(item), "item", definition, {})


@pytest.mark.parametrize("kind", ["chart", "external_selector", "selector_group", "title"])
def test_valid_wrapper_reports_contents_and_placement_drift(kind):
    definition = {
        "kind": kind,
        "title": "Title",
        "text": "Text",
        "chart": SimpleNamespace(id="chart"),
        "members": ["filter"],
        "definitions": {},
        "at": [0, 0, 36, 1],
        "params": {"region": ["North"]},
    }
    item = SimpleNamespace(
        id="item",
        item_type={
            "chart": "widget",
            "external_selector": "control",
            "selector_group": "group_control",
            "title": "title",
        }[kind],
        data={"tabs": [{"chartId": "wrong", "title": "wrong", "params": {}}]},
    )
    control = SimpleNamespace(
        id="item",
        members=[
            SimpleNamespace(
                id="unknown",
                source_type="external",
                source=SimpleNamespace(chart_id="wrong"),
                title="wrong",
            )
        ],
    )
    issues = verify.item_issues(tab(item, [control]), "item", definition, {})
    assert "placement" in issues
    assert len(issues) >= 2


def test_dashboard_drift_covers_missing_tabs_settings_bindings_and_aliases():
    current = tab()
    current.id = "wizard"
    current.title = "Wrong"
    current.hidden = True
    unknown = tab()
    unknown.id = "unknown"
    unknown.hidden = False
    dashboard = SimpleNamespace(
        validate=list,
        tabs=[current, unknown],
        data={"settings": {"hideTabs": True}},
        raw={"annotation": {"description": "Wrong"}},
    )
    dataset = SimpleNamespace(
        fields=SimpleNamespace(by_name=lambda name: SimpleNamespace(guid=name))
    )
    content = {
        "charts": {"chart": [0, 0, 36, 1]},
        "selectors": {"filter": {"key": "filter", "source": {"kind": "manual"}, "recipients": []}},
        "aliases": [
            [{"dataset": "sales", "field": "Region"}, {"dataset": "sales", "field": "Channel"}]
        ],
    }
    # Definitions are independently checked elsewhere; isolate top-level metadata drift.
    content["selectors"].clear()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(verify, "definition_items", lambda *args: {"chart": {"kind": "chart"}})
        monkeypatch.setattr(verify, "item_issues", lambda *args: ["item missing"])
        monkeypatch.setattr(verify, "expected_edges", lambda value: {("chart", "filter")})
        issues = verify.dashboard_issues(
            dashboard,
            tab_definitions={"wizard": {"title": "Wizard", "hidden": False}, "missing": {}},
            contents={"wizard": content},
            datasets={"sales": dataset},
            charts={},
            chart_definitions={},
            description="Expected",
            hide_tabs=False,
        )
    assert "dashboard tab visibility" in issues
    assert "dashboard description" in issues
    assert "missing tab missing" in issues
    assert "tab settings wizard" in issues
    assert "dataset field aliases wizard" in issues
    assert "selector receiver bindings wizard" in issues
