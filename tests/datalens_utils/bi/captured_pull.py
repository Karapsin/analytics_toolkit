"""Captured sources retain identity and writer restrictions through local refreshes."""

import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.import_refresh import refresh_source
from analytics_toolkit.datalens_utils.recipe import load_registry
from datalens_sdk import EntryLocation

from tests.datalens_utils._support.captured_import import imported_project, run_import


def replace_data(source, kind, data):
    entity = getattr(source, kind)
    snapshot = copy.deepcopy(entity.response_snapshot)
    snapshot["entry"]["data"] = data
    changed = replace(entity, data=data, response_snapshot=snapshot)
    source.entities[entity.id] = changed
    return changed


@pytest.mark.parametrize("kind", ["chart", "dataset", "dashboard"])
def test_supported_pull_refreshes_source_and_keeps_verified_baselines(
    import_source, tmp_path, kind
):
    report = run_import(import_source, tmp_path, dry_run=False)
    key = next(key for key, item in report["resources"].items() if item["kind"] == kind)
    project = imported_project(import_source, tmp_path)
    writes = list(import_source.api.writes)
    result = project.pull(resource_keys=[key])
    assert key in result["pulled_resources"]
    assert not project.verify(full=True)["mismatches"]
    assert import_source.api.writes == writes
    manifest = json.loads((tmp_path / "project/configs/import.json").read_text())
    assert (
        manifest["resources"][key]["baseline_definition"]
        == report["resources"][key]["baseline_definition"]
    )


@pytest.mark.parametrize("kind", ["chart", "dashboard"])
def test_read_only_pull_preserves_new_native_content_and_writer_restriction(
    import_source, tmp_path, kind
):
    data = copy.deepcopy(getattr(import_source, kind).data)
    if kind == "chart":
        data["visualization"]["type"] = "future-visualization"
    else:
        data["tabs"][0]["items"].append(
            {"id": "future", "type": "future_widget", "data": {"text": "retained"}}
        )
    replace_data(import_source, kind, data)
    report = run_import(import_source, tmp_path, dry_run=False)
    key, entry = next(
        (key, item) for key, item in report["resources"].items() if item["kind"] == kind
    )
    assert not entry["editable"]
    data["future-authored-setting"] = {"text": "new value"}
    replace_data(import_source, kind, data)
    project = imported_project(import_source, tmp_path)
    project.pull(resource_keys=[key])
    root = tmp_path / "project"
    manifest = json.loads((root / "configs/import.json").read_text())
    updated = manifest["resources"][key]
    assert not updated["editable"]
    assert updated["baseline_definition"]["read_only"]
    assert json.loads((root / updated["source_file"]).read_text())["data"] == data
    assert not project.verify(full=True)["mismatches"]


@pytest.mark.parametrize("failure", ["location", "missing-source", "dynamic-dependencies"])
def test_refresh_guards_fail_before_proposing_local_writes(import_source, tmp_path, failure):
    run_import(import_source, tmp_path, dry_run=False)
    project = imported_project(import_source, tmp_path)
    proposed = {}
    with project.session():
        registry = load_registry()
        resource = next(item for item in registry.resources.values() if item.kind == "chart")
        entity = import_source.chart
        if failure == "location":
            entity = replace(entity, location=EntryLocation.workbook("moved"))
            message = "Source location changed"
        elif failure == "missing-source":
            resource.capture["source_file"] = None
            message = "Previously inaccessible source"
        else:
            entity = SimpleNamespace(
                id=entity.id, category="editor", data={"meta": "module.exports = compute()"}
            )
            message = "Cannot establish refreshed dependency closure"
        with pytest.raises(DataLensUtilsError, match=message):
            refresh_source(resource, entity, registry, {"resources": {}}, proposed)
    assert proposed == {}


def test_source_overlap_blocks_dashboard_writer_but_keeps_exact_layout(import_source, tmp_path):
    data = copy.deepcopy(import_source.dashboard.data)
    tab = data["tabs"][0]
    duplicate = copy.deepcopy(tab["items"][0])
    duplicate["id"] = "second"
    tab["items"].append(duplicate)
    placement = copy.deepcopy(tab["layout"][0])
    placement["i"] = "second"
    tab["layout"].append(placement)
    replace_data(import_source, "dashboard", data)
    report = run_import(import_source, tmp_path, dry_run=False)
    entry = report["resources"]["dashboard:main"]
    assert report["capture_complete"]
    assert not entry["editable"]
    assert any("overlap" in item["reason"] for item in entry["restrictions"])
    assert json.loads((tmp_path / "project" / entry["source_file"]).read_text())["data"] == data
