"""Chart placement metadata cannot be lost by import or a writable pull baseline."""

import copy
import json

import pytest
from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import DashboardChartTab, DashboardTab

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils.bi.captured_pull import replace_data
from tests.datalens_utils.bi.captured_safety import with_revisions
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


def widget_source(source, grouped, *, supported_metadata=False):
    options = (
        {"description": "Authored description", "hint": "Authored hint"}
        if supported_metadata
        else {}
    )
    tab = DashboardTab("One", tab_id="one")
    if grouped:
        tab.add_chart_group(
            [
                DashboardChartTab(chart=source.chart, title="First", **options),
                DashboardChartTab(chart=source.chart, title="Second", **options),
            ],
            item_id="placement",
            at=(0, 0, 12, 6),
        )
    else:
        tab.add_chart(source.chart, item_id="placement", at=(0, 0, 12, 6), **options)
    dashboard = (
        source.api.client.create.dashboard(name="Source", location=source.api.folder)
        .add_tab(tab)
        .build()
    )
    source.dashboard = dashboard
    source.entities[dashboard.id] = dashboard
    source.kinds[dashboard.id] = "dash"
    return dashboard


def change_widget(data, feature):
    widget = data["tabs"][0]["items"][0]
    member = widget["data"]["tabs"][-1]
    if feature in {"defaults", "combined"}:
        widget["defaults"] = {"region": ["north"]}
    if feature in {"member", "combined"}:
        member["futureMetadata"] = {"keep": "authored"}
    elif feature == "placement":
        widget["data"]["futurePresentation"] = {"keep": "authored"}
    elif feature == "raw-property":
        widget["futurePlacement"] = {"keep": "authored"}
    elif feature == "member-defaults":
        member["defaults"] = {"region": ["north"]}
    elif feature == "namespace":
        widget["namespace"] = "authored-namespace"
    elif feature == "background":
        widget["data"]["backgroundSettings"] = {"color": {"light": "#ffffff", "dark": "#000000"}}
    elif feature == "disabled-hint":
        member.update(hint="Keep this hint", enableHint=False)
    elif feature == "disabled-description":
        member.update(description="Keep this description", enableDescription=False)


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize(
    "feature",
    [
        "defaults",
        "combined",
        "placement",
        "raw-property",
        "member",
        "member-defaults",
        "namespace",
        "background",
        "disabled-hint",
        "disabled-description",
    ],
)
def test_unmodeled_chart_placement_blocks_imported_writes_and_pull_baseline(
    import_source, tmp_path, grouped, feature
):
    dashboard = widget_source(import_source, grouped)
    original = tmp_path / "original"
    report = run_import(import_source, original, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, original)
    project.pull(resource_keys=["dashboard:main"])
    files = file_bytes(project.paths.project_root)
    checkpoint = project.paths.runtime_root / "resources.json"
    checkpoint_before = checkpoint.read_bytes()
    data = copy.deepcopy(dashboard.data)
    change_widget(data, feature)
    changed = replace_data(import_source, "dashboard", data)
    import_source.entities[changed.id] = with_revisions(
        changed, saved_id="remote", published_id="remote", rev_id="remote"
    )
    writes = list(import_source.api.writes)
    with pytest.raises(DataLensUtilsError, match=r"Chart widget.*cannot be faithfully written"):
        project.pull(resource_keys=["dashboard:main"])
    assert file_bytes(project.paths.project_root) == files
    assert checkpoint.read_bytes() == checkpoint_before
    with pytest.raises(DataLensUtilsError, match="changed remotely"):
        project.apply(resource_keys=["dashboard:main"])

    fresh = tmp_path / "fresh"
    report = run_import(import_source, fresh, dry_run=False)
    entry = report["resources"]["dashboard:main"]
    assert report["written"]
    assert report["capture_complete"]
    assert entry["capture"] == "complete"
    assert not entry["editable"]
    assert any("Chart widget" in item["reason"] for item in entry["restrictions"])
    assert json.loads((fresh / "project" / entry["source_file"]).read_text())["data"] == data
    captured = imported_project(import_source, fresh)
    assert not captured.verify(full=True)["mismatches"]
    with pytest.raises(DataLensUtilsError, match="Captured-only"):
        captured.apply(resource_keys=["dashboard:main"])
    assert import_source.api.writes == writes


@pytest.mark.parametrize("grouped", [False, True])
def test_supported_widget_descriptions_and_hints_survive_dashboard_rebuild(
    import_source, tmp_path, monkeypatch, grouped
):
    dashboard = widget_source(import_source, grouped, supported_metadata=True)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, tmp_path)
    path = project.paths.project_root / "configs/UI/tabs.json"
    tabs = json.loads(path.read_text())
    tabs["one"]["title"] = "Local tab title"
    path.write_text(json.dumps(tabs))
    import_source.client.get.dashboard = import_source.api.client.get.dashboard
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project.apply(resource_keys=["dashboard:main"])
    current = import_source.api.client.get.dashboard(by_id=dashboard.id, branch="published")
    assert current.data["tabs"][0]["title"] == "Local tab title"
    for member in current.data["tabs"][0]["items"][0]["data"]["tabs"]:
        assert member["description"] == "Authored description"
        assert member["enableDescription"]
        assert member["hint"] == "Authored hint"
        assert member["enableHint"]
    import_source.entities[current.id] = current
    assert not project.verify(full=True)["mismatches"]
