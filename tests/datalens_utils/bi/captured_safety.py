"""Refreshing published imports cannot silently expand their writer capabilities."""

import copy
import json
from dataclasses import fields, replace

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from datalens_sdk import EditorChart

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils.bi.captured_pull import replace_data
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


def with_revisions(entity, **changes):
    names = {field.name for field in fields(entity)}
    wire_names = {"saved_id": "savedId", "published_id": "publishedId", "rev_id": "revId"}
    return replace(
        entity,
        raw={**entity.raw, **{wire_names[key]: value for key, value in changes.items()}},
        **{key: value for key, value in changes.items() if key in names},
    )


def test_pull_refuses_to_baseline_an_unrepresented_new_editor_tab(import_source, tmp_path):
    original = import_source.chart
    data = {
        **dict.fromkeys(
            ["prepare", "config", "controls", "params", "sources"], "module.exports = {};"
        ),
        "meta": '{"links":{}}',
    }
    snapshot = copy.deepcopy(original.response_snapshot)
    snapshot["entry"]["data"] = data
    editor = EditorChart(
        id=original.id,
        name=original.name,
        location=original.location,
        wire_type="table_node",
        data=data,
        raw=copy.deepcopy(original.raw),
        response_snapshot=snapshot,
    )
    import_source.entities[editor.id] = editor
    import_source.client.get.editor_chart = import_source.client.get.chart
    report = run_import(import_source, tmp_path, dry_run=False)
    key = next(key for key, item in report["resources"].items() if item["id"] == editor.id)
    assert report["resources"][key]["editable"]
    project = imported_project(import_source, tmp_path)
    before = file_bytes(project.paths.project_root)
    data = {**data, "documentation": "New authored documentation"}
    import_source.entities[editor.id] = replace(editor, data=data)
    writes = list(import_source.api.writes)
    with pytest.raises(DataLensUtilsError, match="Authored Editor tabs changed"):
        project.pull(resource_keys=[key])
    assert file_bytes(project.paths.project_root) == before
    assert import_source.api.writes == writes


@pytest.mark.parametrize("change", ["empty-format", "different-format", "unmodeled-setting"])
def test_unsafe_published_pull_keeps_baseline_and_subsequent_apply_cannot_write(
    import_source, tmp_path, change
):
    report = run_import(import_source, tmp_path, dry_run=False)
    key = next(key for key, item in report["resources"].items() if item["kind"] == "chart")
    assert report["resources"][key]["editable"]
    project = imported_project(import_source, tmp_path)
    before = file_bytes(project.paths.project_root)
    checkpoint_path = project.paths.runtime_root / "resources.json"
    checkpoint = checkpoint_path.read_bytes() if checkpoint_path.exists() else None
    data = copy.deepcopy(import_source.chart.data)
    visualization = data["visualization"]
    if change == "unmodeled-setting":
        visualization.setdefault("chartSettings", {})["futureSetting"] = "preserve me"
        message = "Refreshed Wizard source cannot be faithfully edited"
    else:
        original = visualization["columns"]["items"][0]
        other = copy.deepcopy(original)
        original["formatting"] = {"precision": 2}
        other["formatting"] = {} if change == "empty-format" else {"precision": 4}
        visualization["columns"]["items"].append(other)
        message = "Conflicting per-slot field formats"
    changed = replace_data(import_source, "chart", data)
    changed = with_revisions(
        changed,
        saved_id="new-published",
        published_id="new-published",
        rev_id="new-published",
    )
    import_source.entities[changed.id] = changed
    writes = list(import_source.api.writes)
    with pytest.raises(DataLensUtilsError, match=message):
        project.pull(resource_keys=[key])
    assert file_bytes(project.paths.project_root) == before
    assert (checkpoint_path.read_bytes() if checkpoint_path.exists() else None) == checkpoint
    with pytest.raises(DataLensUtilsError, match=r"changed remotely|Capability preflight blocked"):
        project.apply(resource_keys=[key])
    assert import_source.api.writes == writes
    assert changed.data == data


@pytest.mark.parametrize(
    "meta",
    [
        "null",
        "[]",
        '"text"',
        '{"links":null}',
        '{"links":[]}',
        '{"links":{"bad":[]}}',
        '{"links":{"bad":null}}',
        '{"links":{"bad":""}}',
        '{"links":{"":"dataset"}}',
    ],
)
def test_malformed_editor_meta_keeps_source_and_unrelated_resources(import_source, tmp_path, meta):
    original = import_source.chart
    data = {"meta": meta, "prepare": "module.exports = {};"}
    snapshot = copy.deepcopy(original.response_snapshot)
    snapshot["entry"].update(entryId="malformed-editor", data=data)
    editor = EditorChart(
        id="malformed-editor",
        name="Readable Editor",
        location=original.location,
        wire_type="table_node",
        data=data,
        raw=copy.deepcopy(original.raw),
        response_snapshot=snapshot,
    )
    import_source.entities[editor.id] = editor
    import_source.kinds[editor.id] = "widget"
    dashboard = copy.deepcopy(import_source.dashboard.data)
    tab = dashboard["tabs"][0]
    item = copy.deepcopy(tab["items"][0])
    item["id"] = "malformed"
    item["data"]["tabs"][0]["chartId"] = editor.id
    tab["items"].append(item)
    placement = copy.deepcopy(tab["layout"][0])
    placement.update(i="malformed", y=6)
    tab["layout"].append(placement)
    replace_data(import_source, "dashboard", dashboard)
    writes = list(import_source.api.writes)
    report = run_import(import_source, tmp_path, dry_run=False)
    assert report["written"]
    assert not report["capture_complete"]
    assert report["coverage"]["captured"] == 5
    entry = next(value for value in report["resources"].values() if value["id"] == editor.id)
    assert entry["capture"] == "complete"
    assert not entry["editable"]
    assert any("Editor Meta" in item["reason"] for item in entry["restrictions"])
    assert any(item["feature"] == "dependency closure" for item in report["capture_issues"])
    root = tmp_path / "project"
    assert json.loads((root / entry["source_file"]).read_text())["data"] == data
    assert any(path.read_text() == meta for path in (root / "assets/js/source").rglob("meta.js"))
    assert all(
        entry["editable"]
        for entry in report["resources"].values()
        if entry["id"] in {original.id, import_source.dataset.id}
    )
    assert imported_project(import_source, tmp_path).validate()["coverage"]["resource_count"] == 5
    assert import_source.api.writes == writes


@pytest.mark.parametrize("kind", ["chart", "dataset", "dashboard"])
@pytest.mark.parametrize("when", ["before-import", "after-import"])
def test_published_import_verifies_with_newer_draft_but_write_remains_blocked(
    import_source, tmp_path, kind, when
):
    original = getattr(import_source, kind)
    if when == "before-import":
        import_source.entities[original.id] = with_revisions(original, saved_id="newer-draft")
    report = run_import(import_source, tmp_path, dry_run=False)
    key = next(key for key, item in report["resources"].items() if item["id"] == original.id)
    assert report["resources"][key]["editable"]
    if when == "after-import":
        import_source.entities[original.id] = with_revisions(original, saved_id="newer-draft")
    project = imported_project(import_source, tmp_path)
    writes = list(import_source.api.writes)
    assert not project.verify(full=True)["mismatches"]
    with pytest.raises(DataLensUtilsError, match=r"remote draft|changed remotely"):
        project.apply(resource_keys=[key])
    assert import_source.api.writes == writes


@pytest.mark.parametrize("captured_only", [False, True])
@pytest.mark.parametrize("revision", ["saved-branch", "no-published-revision"])
def test_import_verification_still_requires_actual_published_source(
    import_source, tmp_path, captured_only, revision
):
    if captured_only:
        data = copy.deepcopy(import_source.chart.data)
        data["visualization"]["type"] = "future-visualization"
        replace_data(import_source, "chart", data)
    run_import(import_source, tmp_path, dry_run=False)
    original = import_source.entities[import_source.chart.id]
    changed = (
        replace(original, raw={**original.raw, "revId": "saved-only"})
        if revision == "saved-branch"
        else with_revisions(original, published_id=None)
    )
    import_source.entities[original.id] = changed
    with pytest.raises(DataLensUtilsError, match="revisions"):
        imported_project(import_source, tmp_path).verify(full=True)
