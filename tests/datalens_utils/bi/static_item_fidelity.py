"""Text/title defaults are writable while unrepresented authored metadata is protected."""

import json

import pytest
from analytics_toolkit.datalens_utils import bi_engine
from datalens_sdk import DashboardTab

from tests.datalens_utils._support.captured_fidelity import assert_readable_only
from tests.datalens_utils._support.captured_import import imported_project, run_import


def static_source(source, kind):
    tab = DashboardTab("One", tab_id="one").add_chart(
        source.chart, item_id="first", at=(0, 0, 12, 6)
    )
    getattr(tab, "add_" + kind)("Authored text", item_id="note", at=(0, 6, 12, 2))
    dashboard = (
        source.api.client.create.dashboard(name="Source", location=source.api.folder)
        .add_tab(tab)
        .build()
    )
    source.dashboard = dashboard
    source.entities[dashboard.id] = dashboard
    source.kinds[dashboard.id] = "dash"
    return dashboard


def static_item(data):
    return next(item for item in data["tabs"][0]["items"] if item["id"] == "note")


def mutate_static(data, feature):
    item = static_item(data)
    mutations = {
        "defaults": (item, "defaults", {"region": ["north"]}),
        "raw-property": (item, "futureMetadata", {"keep": "authored"}),
        "property": (item["data"], "futurePresentation", {"keep": "authored"}),
        "auto-height": (item["data"], "autoHeight", False),
        "pinning": (item["data"], "pinned", True),
        "namespace": (item, "namespace", "authored-namespace"),
        "identity": (item, "id", None),
        "background": (item["data"], "backgroundSettings", {"color": "#ff0000"}),
        "toc": (item["data"], "showInTOC", True),
        "text-size": (item["data"], "size", "xl"),
    }
    target, name, value = mutations[feature]
    target[name] = value


@pytest.mark.parametrize("kind", ["text", "title"])
@pytest.mark.parametrize(
    "feature",
    [
        "defaults",
        "raw-property",
        "property",
        "auto-height",
        "pinning",
        "namespace",
        "identity",
        "background",
    ],
)
def test_unsupported_static_item_properties_stay_readable_and_block_writes(
    import_source, tmp_path, kind, feature
):
    static_source(import_source, kind)
    assert_readable_only(
        import_source,
        tmp_path,
        lambda data: mutate_static(data, feature),
        pull_error="Layout must position"
        if feature == "identity"
        else "cannot be faithfully written",
    )


@pytest.mark.parametrize(("kind", "feature"), [("title", "toc"), ("text", "text-size")])
def test_unrepresented_static_type_specific_metadata_is_protected(
    import_source, tmp_path, kind, feature
):
    static_source(import_source, kind)
    assert_readable_only(import_source, tmp_path, lambda data: mutate_static(data, feature))


@pytest.mark.parametrize("kind", ["text", "title"])
def test_native_static_defaults_survive_dashboard_rebuild(
    import_source, tmp_path, monkeypatch, kind
):
    dashboard = static_source(import_source, kind)
    before = static_item(dashboard.data)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, tmp_path)
    path = project.paths.project_root / "configs/UI/tabs.json"
    tabs = json.loads(path.read_text())
    tabs["one"]["title"] = "Updated tab"
    path.write_text(json.dumps(tabs))
    import_source.client.get.dashboard = import_source.api.client.get.dashboard
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project.apply(resource_keys=["dashboard:main"])
    current = import_source.api.client.get.dashboard(by_id=dashboard.id, branch="published")
    assert static_item(current.data) == before
    import_source.entities[current.id] = current
    assert not project.verify(full=True)["mismatches"]
