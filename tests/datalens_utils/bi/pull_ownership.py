"""Refreshing captured UI cannot adopt remotely added placements through layout inference."""

import copy
import json

from analytics_toolkit.datalens_utils import bi_engine

from tests.datalens_utils._support.captured_import import imported_project, run_import


def remote_unmanaged_widget(source, key):
    changed = (
        source.dashboard.update.add_chart(
            source.chart, tab="one", item_id=key, title=source.chart.name, at=(12, 0, 12, 6)
        )
        .mode("publish")
        .execute()
    )
    # The fake service owns this readable extension; the SDK has no setter for it.
    document = copy.deepcopy(source.api.documents[changed.id])
    item = next(item for item in document["entry"]["data"]["tabs"][0]["items"] if item["id"] == key)
    item["data"]["futureMetadata"] = {"text": "Keep unmanaged authored metadata"}
    source.api.documents[changed.id] = document
    source.api.revisions[changed.published_id] = copy.deepcopy(document)
    current = source.api.client.get.dashboard(by_id=changed.id, branch="published")
    source.entities[current.id] = current
    return copy.deepcopy(item)


def test_captured_pull_preserves_unmanaged_widget_whose_id_matches_a_chart_key(
    import_source, tmp_path, monkeypatch
):
    report = run_import(import_source, tmp_path, dry_run=False)
    chart_key = next(
        key.split(":", 1)[1]
        for key, entry in report["resources"].items()
        if entry["kind"] == "chart"
    )
    project = imported_project(import_source, tmp_path)
    expected = remote_unmanaged_widget(import_source, chart_key)
    writes = list(import_source.api.writes)
    project.pull(resource_keys=["dashboard:main"])
    root = project.paths.project_root
    layout = json.loads((root / "configs/UI/layout.json").read_text())
    assert chart_key not in layout["one"]
    assert not project.verify(full=True)["mismatches"]
    assert import_source.api.writes == writes
    path = root / "configs/UI/tabs.json"
    tabs = json.loads(path.read_text())
    tabs["one"]["title"] = "Managed local title"
    path.write_text(json.dumps(tabs))
    import_source.client.get.dashboard = import_source.api.client.get.dashboard
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project.apply(resource_keys=["dashboard:main"])
    current = import_source.api.client.get.dashboard(
        by_id=import_source.dashboard.id, branch="published"
    )
    actual = next(item for item in current.data["tabs"][0]["items"] if item["id"] == chart_key)
    assert actual == expected
    import_source.entities[current.id] = current
    assert not project.verify(full=True)["mismatches"]
