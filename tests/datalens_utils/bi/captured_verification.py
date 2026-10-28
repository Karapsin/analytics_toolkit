"""Authored identity and complete source remain verifiable beyond managed subsets."""

import copy
import json
from dataclasses import replace

import pytest
from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.bi_import_resources import chart_definition
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.import_fidelity import wizard_source_issues

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils.bi.captured_pull import replace_data
from tests.datalens_utils.bi.captured_safety import with_revisions


@pytest.mark.parametrize("binding", ["foreign", "ambiguous"])
def test_dataset_guid_collision_cannot_enable_an_unsafe_import_writer(
    import_source, tmp_path, binding
):
    second = replace(import_source.dataset, id="second-dataset")
    import_source.entities[second.id] = second
    import_source.kinds[second.id] = "dataset"
    datasets = {"first": import_source.dataset, "second": second}
    definition, _assets = chart_definition(import_source.chart, datasets, {}, "one")
    assert definition["dataset"] == "first"
    data = copy.deepcopy(import_source.chart.data)
    field = data["visualization"]["columns"]["items"][0]
    if binding == "foreign":
        field["datasetId"] = second.id
    else:
        field.pop("datasetId", None)
        data["sources"].setdefault("updates", []).append(
            {"field": {"guid": field["guid"], "datasetId": second.id}}
        )
    changed = replace_data(import_source, "chart", data)
    assert wizard_source_issues(import_source.client, changed, datasets, definition) == [
        "dataset identity and GUID bindings cannot be reproduced by this recipe"
    ]
    writes = list(import_source.api.writes)
    report = run_import(import_source, tmp_path, dry_run=False)
    key, entry = next(
        (key, value) for key, value in report["resources"].items() if value["id"] == changed.id
    )
    assert report["capture_complete"]
    assert not entry["editable"]
    assert any("dataset identity" in item["reason"] for item in entry["restrictions"])
    assert json.loads((tmp_path / "project" / entry["source_file"]).read_text())["data"] == data
    if binding == "foreign":
        assert second.id in entry["dataset_bindings"]["referenced_dataset_ids"]
    else:
        assert entry["dataset_bindings"]["ambiguous"]
    project = imported_project(import_source, tmp_path)
    assert not project.verify(full=True)["mismatches"]
    with pytest.raises(DataLensUtilsError, match="Captured-only"):
        project.apply(resource_keys=[key])
    assert import_source.api.writes == writes


@pytest.mark.parametrize("kind", ["chart", "dataset", "dashboard"])
def test_full_verify_detects_new_unmodeled_source_on_editable_imports(
    import_source, tmp_path, kind
):
    report = run_import(import_source, tmp_path, dry_run=False)
    key = next(key for key, value in report["resources"].items() if value["kind"] == kind)
    assert report["resources"][key]["editable"]
    project = imported_project(import_source, tmp_path)
    verified = project.verify(full=True)
    assert set(verified["source_verification"]) == set(report["resources"])
    assert verified["source_verification"][key] == {"status": "matched", "baseline": "capture"}
    entity = getattr(import_source, kind)
    if kind == "dataset":
        snapshot = copy.deepcopy(entity.response_snapshot)
        snapshot["dataset"]["futureAuthoredSetting"] = {"preserve": "this"}
        changed = replace(entity, response_snapshot=snapshot)
    else:
        data = copy.deepcopy(entity.data)
        target = data["visualization"].setdefault("chartSettings", {}) if kind == "chart" else data
        target["futureAuthoredSetting"] = {"preserve": "this"}
        changed = replace_data(import_source, kind, data)
    import_source.entities[entity.id] = with_revisions(
        changed, saved_id="new-published", published_id="new-published", rev_id="new-published"
    )
    writes = list(import_source.api.writes)
    with pytest.raises(DataLensUtilsError, match="captured_source") as caught:
        project.verify(full=True)
    assert {"resource": key, "path": "captured_source"} in caught.value.mismatches
    assert import_source.api.writes == writes


def test_verified_managed_write_retains_new_source_baseline_and_detects_later_drift(
    import_source, tmp_path, monkeypatch
):
    report = run_import(import_source, tmp_path, dry_run=False)
    key, entry = next(
        (key, value) for key, value in report["resources"].items() if value["kind"] == "chart"
    )
    root = tmp_path / "project"
    source_before = (root / entry["source_file"]).read_bytes()
    path = next((root / "configs/DL objects/charts").rglob("*.json"))
    definition = json.loads(path.read_text())
    definition["chart_title"] = "Managed new title"
    path.write_text(json.dumps(definition))
    import_source.client.get.wizard_chart = import_source.api.client.get.wizard_chart
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project = imported_project(import_source, tmp_path)
    project.apply(resource_keys=[key])
    verified = project.verify(full=True)
    assert verified["source_verification"][key] == {
        "status": "matched",
        "baseline": "verified_checkpoint",
    }
    assert (root / entry["source_file"]).read_bytes() == source_before
    current = import_source.api.client.get.wizard_chart(by_id=entry["id"], branch="published")
    data = copy.deepcopy(current.data)
    data["visualization"]["chartSettings"]["futureAuthoredSetting"] = "unmodeled"
    snapshot = copy.deepcopy(current.response_snapshot)
    snapshot["entry"]["data"] = data
    changed = with_revisions(
        replace(current, data=data, response_snapshot=snapshot),
        saved_id="remote",
        published_id="remote",
        rev_id="remote",
    )
    import_source.client.get.wizard_chart = lambda **_kwargs: changed
    with pytest.raises(DataLensUtilsError, match="captured_source"):
        project.verify(full=True)
