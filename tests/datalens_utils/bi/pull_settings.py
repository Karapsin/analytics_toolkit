"""Dataset setting refreshes participate in the public three-way pull contract."""

import copy
import json
from dataclasses import replace

import pytest
from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils.bi.captured_safety import with_revisions
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


def remote_settings(source, settings, revision):
    entity = source.entities[source.dataset.id]
    raw = copy.deepcopy(entity.raw)
    snapshot = copy.deepcopy(entity.response_snapshot)
    raw["dataset"]["settings"] = copy.deepcopy(settings)
    snapshot["dataset"]["settings"] = copy.deepcopy(settings)
    changed = with_revisions(
        replace(entity, raw=raw, response_snapshot=snapshot),
        saved_id=revision,
        published_id=revision,
        rev_id=revision,
    )
    source.entities[entity.id] = changed
    return changed


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"load_preview_by_default": True}, {"load_preview_by_default": False}),
        ({"load_preview_by_default": True}, {}),
        ({}, {"load_preview_by_default": False}),
    ],
)
def test_remote_dataset_settings_refresh_then_apply_never_restores_old_values(
    import_source, tmp_path, monkeypatch, before, after
):
    remote_settings(import_source, before, "before")
    report = run_import(import_source, tmp_path, dry_run=False)
    key = next(key for key, entry in report["resources"].items() if entry["kind"] == "dataset")
    assert report["resources"][key]["editable"]
    project = imported_project(import_source, tmp_path)
    remote_settings(import_source, after, "after")
    writes = list(import_source.api.writes)
    project.pull(resource_keys=[key])
    path = project.paths.project_root / "configs/DL objects/datasets.json"
    assert json.loads(path.read_text())[key.split(":", 1)[1]].get("settings", {}) == after
    manifest = json.loads((project.paths.project_root / "configs/import.json").read_text())
    assert manifest["resources"][key]["baseline_definition"].get("settings", {}) == after
    assert not project.verify(full=True)["mismatches"]
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project.apply(resource_keys=[key])
    assert import_source.entities[import_source.dataset.id].raw["dataset"]["settings"] == after
    assert import_source.api.writes == writes


def test_conflicting_dataset_setting_edits_preserve_files_and_checkpoint(import_source, tmp_path):
    remote_settings(import_source, {"load_preview_by_default": True}, "before")
    report = run_import(import_source, tmp_path, dry_run=False)
    key = next(key for key, entry in report["resources"].items() if entry["kind"] == "dataset")
    project = imported_project(import_source, tmp_path)
    project.pull(resource_keys=[key])
    path = project.paths.project_root / "configs/DL objects/datasets.json"
    definitions = json.loads(path.read_text())
    definitions[key.split(":", 1)[1]]["settings"]["load_preview_by_default"] = False
    path.write_text(json.dumps(definitions))
    remote_settings(import_source, {}, "after")
    files = file_bytes(project.paths.project_root)
    checkpoint = project.paths.runtime_root / "resources.json"
    checkpoint_before = checkpoint.read_bytes()
    writes = list(import_source.api.writes)
    with pytest.raises(
        DataLensUtilsError, match=r"changes conflict.*settings/load_preview_by_default"
    ):
        project.pull(resource_keys=[key])
    assert file_bytes(project.paths.project_root) == files
    assert checkpoint.read_bytes() == checkpoint_before
    with pytest.raises(DataLensUtilsError, match="changed remotely"):
        project.apply(resource_keys=[key])
    assert import_source.api.writes == writes
