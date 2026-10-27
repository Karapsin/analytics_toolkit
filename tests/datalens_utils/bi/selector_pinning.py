"""Readable selector pinning never becomes a writer that drops authored state."""

import copy
import json

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import DashboardTab

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils.bi.captured_pull import replace_data
from tests.datalens_utils.bi.captured_safety import with_revisions
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


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


def selector_source(source, scope, pinned):
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


@pytest.mark.parametrize("scope", ["local", "shared", "external"])
@pytest.mark.parametrize("pinned", [True, False])
def test_pinned_selectors_capture_exact_source_and_block_only_unsafe_writers(
    import_source, tmp_path, scope, pinned
):
    dashboard = selector_source(import_source, scope, pinned)
    writes = list(import_source.api.writes)
    report = run_import(import_source, tmp_path, dry_run=False)
    entry = report["resources"]["dashboard:main"]
    assert report["written"]
    assert report["capture_complete"]
    assert entry["editable"] is not pinned
    assert entry["capture"] == "complete"
    if pinned:
        assert any("Selector pinning" in item["reason"] for item in entry["restrictions"])
    assert (
        json.loads((tmp_path / "project" / entry["source_file"]).read_text())["data"]
        == dashboard.data
    )
    assert all(
        value["editable"] for value in report["resources"].values() if value["kind"] == "dataset"
    )
    project = imported_project(import_source, tmp_path)
    assert not project.verify(full=True)["mismatches"]
    if pinned:
        with pytest.raises(DataLensUtilsError, match="Captured-only"):
            project.apply(resource_keys=["dashboard:main"])
    assert import_source.api.writes == writes


def test_new_remote_pinning_cannot_replace_a_writable_baseline(import_source, tmp_path):
    dashboard = selector_source(import_source, "local", False)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, tmp_path)
    before = file_bytes(project.paths.project_root)
    data = copy.deepcopy(dashboard.data)
    assert set_native_pin(data, "filters", True)
    changed = replace_data(import_source, "dashboard", data)
    import_source.entities[changed.id] = with_revisions(
        changed, saved_id="new", published_id="new", rev_id="new"
    )
    writes = list(import_source.api.writes)
    with pytest.raises(DataLensUtilsError, match="Selector pinning"):
        project.pull(resource_keys=["dashboard:main"])
    assert file_bytes(project.paths.project_root) == before
    with pytest.raises(DataLensUtilsError, match="changed remotely"):
        project.apply(resource_keys=["dashboard:main"])
    assert import_source.api.writes == writes


def test_supported_chart_widget_pinning_does_not_block_dashboard_writer(import_source, tmp_path):
    data = copy.deepcopy(import_source.dashboard.data)
    data["tabs"][0]["items"][0]["data"]["pinned"] = True
    replace_data(import_source, "dashboard", data)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["capture_complete"]
    assert report["resources"]["dashboard:main"]["editable"]
