"""Dashboard recovery removes stale wrappers before reusing reserved identities."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.dashboard_generation.dashboard import populate
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError


def dashboard(tabs):
    return SimpleNamespace(id="dash", tabs=tabs, data={}, raw={}, is_draft=False, update=Mock())


def tab(identity="main", **options):
    return SimpleNamespace(
        id=identity,
        title="Main",
        hidden=False,
        items=[],
        global_items=[],
        controls=[],
        connections=[],
        aliases={},
        **options,
    )


@pytest.mark.parametrize("problem", ["extra tab", "duplicate identity"])
def test_recovery_rejects_ambiguous_ownership_before_any_write(problem):
    value = dashboard([tab("foreign")] if problem == "extra tab" else [])
    context = Mock()
    context.resources.state = {"resources": {"dashboard": {}}}
    context.client.get.dashboard.return_value = value
    definitions = {"main": {}, "second": {}} if problem == "duplicate identity" else {"main": {}}
    with patch.object(
        populate, "definition_items", return_value={"same": {"kind": "chart"}}
    ), pytest.raises(DataLensUtilsError):
        populate.populate_dashboard(
            context=context,
            dashboard=value,
            datasets={},
            charts={},
            tab_definitions=definitions,
            contents={},
            chart_definitions={},
            description="",
            hide_tabs=False,
        )
    value.update.execute.assert_not_called()


@pytest.mark.parametrize(
    "scenario", ["moved", "duplicate", "obsolete", "new tab", "metadata", "replacement"]
)
def test_recovery_stages_removals_and_restores_managed_content(scenario):
    main = tab()
    second = tab("second")
    second.hidden = True
    second.global_items = [SimpleNamespace(id="foreign", item_type="text", data={})]
    wrapper = SimpleNamespace(id="owned", item_type="text", data={})
    main.items = [wrapper]
    if scenario == "moved":
        main.items, second.items = [], [wrapper]
    if scenario == "duplicate":
        second.items = [wrapper]
    value = dashboard([main, second])
    if scenario == "new tab":
        value.tabs = []
    updated = dashboard([tab()])
    context = Mock()
    context.client.get.dashboard.side_effect = (
        [value, value, updated]
        if scenario in {"moved", "duplicate", "obsolete", "replacement"}
        else [value]
    )
    context.resources.state = {"resources": {"dashboard": {"managed_items": ["owned"]}}}
    context.resources.persisted.side_effect = lambda entity, result, getter, **kwargs: updated
    expected = (
        {}
        if scenario == "obsolete"
        else {"owned": {"kind": "text", "text": "Text", "at": [0, 0, 36, 1]}}
    )
    contents = {"main": {}}
    with patch.object(populate, "definition_items", return_value=expected), patch.object(
        populate, "item_issues", return_value=["text"] if scenario == "replacement" else []
    ), patch.object(
        populate,
        "expected_edges",
        return_value={("owned", "filter")} if scenario == "new tab" else set(),
    ), patch.object(
        populate,
        "alias_groups",
        return_value={frozenset({"a", "b"})} if scenario == "new tab" else set(),
    ), patch.object(populate, "dashboard_issues", return_value=[]):
        result = populate.populate_dashboard(
            context=context,
            dashboard=value,
            datasets={},
            charts={},
            tab_definitions={
                "main": {"title": "Changed" if scenario == "metadata" else "Main", "hidden": False}
            },
            contents=contents,
            chart_definitions={},
            description="Changed" if scenario == "metadata" else "",
            hide_tabs=scenario == "metadata",
        )
    assert result in (value, updated)
    if scenario in {"moved", "duplicate", "obsolete", "replacement"}:
        value.update.remove_item.assert_called_with("owned")
        assert value.update.mode.call_args.args == ("save",)
    if scenario == "new tab":
        value.update.add_tab.assert_called_once()
    if scenario == "metadata":
        value.update.update_tab.assert_called_once_with("main", title="Changed", hidden=False)
        value.update.settings.assert_called_once_with(hide_tabs=True)
        value.update.description.assert_called_once_with("Changed")


def test_wiring_replaces_only_known_aliases_and_managed_edges():
    update = Mock()
    value = dashboard([tab(), tab("foreign")])
    datasets = {"sales": SimpleNamespace(fields=[SimpleNamespace(guid=x) for x in "abcd"])}
    with patch.object(populate, "expected_edges", return_value={("chart", "wanted")}), patch.object(
        populate, "managed_edges", return_value={("chart", "old"): ("chart", "old")}
    ), patch.object(populate, "alias_groups", return_value={frozenset({"c", "d"})}), patch.object(
        populate,
        "actual_alias_groups",
        return_value={frozenset({"a", "b"}), frozenset({"external"})},
    ):
        assert populate._reconcile_wiring(
            update, value, {"main": {}}, datasets, {"main": [["a", "b"]]}
        )
    update.remove_connection.assert_called_once_with(from_item="chart", to_item="old", tab="main")
    update.remove_alias.assert_called_once_with("a", "b", tab="main")
    update.add_alias.assert_called_once_with("c", "d", tab="main")


@pytest.mark.parametrize(
    "problem",
    [
        "chart params",
        "uncovered",
        "missing member",
        "shape",
        "source",
        "no changes",
        "operation",
        "clear operation",
    ],
)
def test_point_updates_apply_only_when_all_differences_are_supported(problem):  # noqa: C901 - Distinct point-update cases.
    source = SimpleNamespace(
        element_type="select", multiselect=False, is_range=False, raw={}, operation=None
    )
    member = SimpleNamespace(id="filter", source=source, source_type="manual")
    value = tab()
    value.controls = [SimpleNamespace(id="group", members=[member])]
    wanted = {"title": "Region", "source": {"kind": "manual"}, "control": {"element": "select"}}
    definition = {"kind": "selector_group", "definitions": {"filter": wanted}, "at": [0, 0, 1, 1]}
    issues = ["filter: selector identity or title"]
    if problem == "chart params":
        definition = {"kind": "chart", "params": {"region": "North"}}
        issues = ["chart widget parameters"]
    if problem == "uncovered":
        value.controls = []
        issues = ["structural"]
    if problem == "missing member":
        definition["definitions"] = {}
    if problem == "shape":
        source.element_type = "checkbox"
    if problem == "source":
        member.source_type = "dataset"
    if problem == "no changes":
        issues = []
    if problem == "operation":
        wanted["control"]["operation"] = "IN"
    if problem == "clear operation":
        source.operation = "IN"
    update = Mock()
    with patch.object(populate, "item_issues", return_value=issues):
        result = populate.patch_item(update, value, "group", definition, {})
    assert result is (problem in {"chart params", "operation"})
    if problem == "operation":
        assert update.update_selector.call_args.kwargs["operation"] == "IN"
    if not result:
        assert update.mock_calls == []


def test_reconciliation_enables_required_dependent_selectors():
    value = dashboard([])
    context = Mock()
    context.client.get.dashboard.return_value = value
    context.resources.state = {"resources": {"dashboard": {}}}
    context.resources.persisted.return_value = value
    with patch.object(populate, "requires_dependent_selectors", return_value=True), patch.object(
        populate, "dashboard_issues", return_value=[]
    ):
        assert (
            populate.populate_dashboard(
                context=context,
                dashboard=value,
                datasets={},
                charts={},
                tab_definitions={},
                contents={},
                chart_definitions={},
                description="",
                hide_tabs=False,
            )
            is value
        )
    value.update.settings.assert_called_once_with(dependent_selectors=True)
