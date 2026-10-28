"""New supported UI properties survive pull, a later rebuild and full verification."""

import json

import pytest
from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import DashboardChartTab

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils._support.captured_widgets import widget_source
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


def added_properties(feature):
    return {
        "hint": {"hint": "Remote hint"},
        "description": {"description": "Remote description"},
        "both": {"hint": "Remote hint", "description": "Remote description"},
        "setting": {},
    }[feature]


def remote_dashboard_change(source, grouped, feature):
    update = source.dashboard.update
    if feature == "setting":
        update.settings(load_priority="selectors")
    else:
        properties = added_properties(feature)
        removed = update.remove_item("placement").mode("publish").execute()
        update = removed.update
        if grouped:
            update.add_chart_group(
                [
                    DashboardChartTab(chart=source.chart, title="First", **properties),
                    DashboardChartTab(chart=source.chart, title="Second", **properties),
                ],
                tab="one",
                item_id="placement",
                at=(0, 0, 12, 6),
            )
        else:
            original = source.dashboard.data["tabs"][0]["items"][0]["data"]["tabs"][0]
            update.add_chart(
                source.chart,
                title=original["title"],
                tab="one",
                item_id="placement",
                at=(0, 0, 12, 6),
                **properties,
            )
    changed = update.mode("publish").execute()
    source.entities[changed.id] = changed
    return changed


def widget_configuration(root, grouped):
    path = root / "configs/UI" / ("chart_groups.json" if grouped else "widgets.json")
    values = json.loads(path.read_text())
    widget = values["placement"]
    node = widget["charts"][0] if grouped else widget["presentation"]
    return path, values, node


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("feature", ["hint", "description", "both", "setting"])
def test_remote_ui_additions_survive_pull_rebuild_and_full_verify(
    import_source, tmp_path, monkeypatch, grouped, feature
):
    dashboard = widget_source(import_source, grouped)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, tmp_path)
    root = project.paths.project_root
    _path, _values, original = widget_configuration(root, grouped)
    properties = added_properties(feature)
    assert not set(properties) & set(original)
    if feature == "setting":
        assert "loadPriority" not in dashboard.data.get("settings", {})
    remote_dashboard_change(import_source, grouped, feature)
    writes = list(import_source.api.writes)
    project.pull(resource_keys=["dashboard:main"])
    _path, _values, pulled = widget_configuration(root, grouped)
    for name, value in properties.items():
        assert pulled[name] == value
    if feature == "setting":
        assert (
            json.loads((root / "configs/DL objects/dashboard.json").read_text())["settings"][
                "load_priority"
            ]
            == "selectors"
        )
    assert not project.verify(full=True)["mismatches"]
    assert import_source.api.writes == writes
    path = root / "configs/UI/tabs.json"
    tabs = json.loads(path.read_text())
    tabs["one"]["title"] = "Local title after pull"
    path.write_text(json.dumps(tabs))
    import_source.client.get.dashboard = import_source.api.client.get.dashboard
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project.apply(resource_keys=["dashboard:main"])
    current = import_source.api.client.get.dashboard(by_id=dashboard.id, branch="published")
    assert current.data["tabs"][0]["title"] == "Local title after pull"
    for member in current.data["tabs"][0]["items"][0]["data"]["tabs"]:
        for name, value in properties.items():
            assert member[name] == value
    if feature == "setting":
        assert current.data["settings"]["loadPriority"] == "selectors"
    import_source.entities[current.id] = current
    assert not project.verify(full=True)["mismatches"]


@pytest.mark.parametrize("grouped", [False, True])
def test_conflicting_new_widget_properties_keep_source_and_checkpoint(
    import_source, tmp_path, grouped
):
    widget_source(import_source, grouped)
    run_import(import_source, tmp_path, dry_run=False)
    project = imported_project(import_source, tmp_path)
    project.pull(resource_keys=["dashboard:main"])
    root = project.paths.project_root
    path, values, node = widget_configuration(root, grouped)
    node["hint"] = "Local hint"
    path.write_text(json.dumps(values))
    remote_dashboard_change(import_source, grouped, "hint")
    files = file_bytes(root)
    checkpoint = project.paths.runtime_root / "resources.json"
    before = checkpoint.read_bytes()
    writes = list(import_source.api.writes)
    with pytest.raises(DataLensUtilsError, match="changes conflict"):
        project.pull(resource_keys=["dashboard:main"])
    assert file_bytes(root) == files
    assert checkpoint.read_bytes() == before
    with pytest.raises(DataLensUtilsError, match="changed remotely"):
        project.apply(resource_keys=["dashboard:main"])
    assert import_source.api.writes == writes
