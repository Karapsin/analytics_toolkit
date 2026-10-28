"""Public SDK selector source factories with readable native pinning fixtures."""

import copy

from datalens_sdk import DashboardTab

from tests.datalens_utils.bi.captured_pull import replace_data


def set_native_pin(value, wrapper, pinned):
    """Inject the readable wire field that has no public selector setter."""
    count = 0
    if isinstance(value, dict):
        if value.get("id") == wrapper and value.get("type") in {"control", "group_control"}:
            value["data"]["pinned"] = pinned
            count += 1
        for child in value.values():
            count += set_native_pin(child, wrapper, pinned)
    elif isinstance(value, list):
        for child in value:
            count += set_native_pin(child, wrapper, pinned)
    return count


def selector_source(source, scope, pinned, *, default_value=None, operation=None):
    tab = DashboardTab("One", tab_id="one").add_chart(
        source.chart, item_id="first", at=(0, 0, 12, 6)
    )
    if scope == "external":
        editor = (
            source.api.client.create.editor_chart.selector(
                name="Selector", location=source.api.folder
            )
            .meta('{"links":{}}')
            .params("module.exports = {};")
            .sources("module.exports = {};")
            .controls("module.exports = [];")
            .build()
        )
        source.entities[editor.id] = editor
        source.kinds[editor.id] = "widget"
        source.client.get.editor_chart = source.client.get.chart
        tab.add_selector(chart=editor, item_id="filters", at=(0, 6, 12, 2))
    else:
        tab.add_selector(
            group="filters",
            item_id="choice",
            param_name="choice",
            element="select",
            title="Choice",
            options=["A", "B"],
            default_value=default_value,
            operation=operation,
        )
        tab.add_group_selector(
            group="filters",
            item_id="filters",
            at=(0, 6, 12, 2),
            show_on_tabs="all" if scope == "shared" else "current",
        )
    dashboard = (
        source.api.client.create.dashboard(name="Source", location=source.api.folder)
        .add_tab(tab)
        .add_tab(DashboardTab("Two", tab_id="two"))
        .build()
    )
    source.dashboard = dashboard
    source.entities[dashboard.id] = dashboard
    source.kinds[dashboard.id] = "dash"
    data = copy.deepcopy(dashboard.data)
    assert set_native_pin(data, "filters", pinned)
    return replace_data(source, "dashboard", data)
