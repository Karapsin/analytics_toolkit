"""Editor wrapper source survives capture and cannot enter an unsafe writer."""

import copy
import json

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils._support.captured_selectors import selector_source
from tests.datalens_utils.bi.captured_pull import replace_data
from tests.datalens_utils.bi.captured_safety import with_revisions
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


@pytest.mark.parametrize(
    "setting", ["auto-height", "fixed-height", "border", "defaults", "source", "title"]
)
def test_editor_wrapper_settings_block_imported_writes_and_baseline_refresh(
    import_source, tmp_path, setting
):
    dashboard = selector_source(import_source, "external", False)
    original = tmp_path / "original"
    report = run_import(import_source, original, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(import_source, original)
    project.pull(resource_keys=["dashboard:main"])
    files = file_bytes(project.paths.project_root)
    checkpoint = project.paths.runtime_root / "resources.json"
    checkpoint_before = checkpoint.read_bytes()
    data = copy.deepcopy(dashboard.data)
    wrapper = next(item for item in data["tabs"][0]["items"] if item["id"] == "filters")
    if setting == "defaults":
        wrapper["defaults"] = {"region": ["north"]}
    elif setting == "source":
        wrapper["data"]["source"]["parameters"] = {"region": "north"}
    elif setting == "title":
        wrapper["data"]["title"] = {"text": "Authored title"}
    else:
        key, value = {
            "auto-height": ("autoHeight", True),
            "fixed-height": ("autoHeight", False),
            "border": ("borderRadius", 8),
        }[setting]
        wrapper["data"][key] = value
    changed = replace_data(import_source, "dashboard", data)
    import_source.entities[changed.id] = with_revisions(
        changed, saved_id="remote", published_id="remote", rev_id="remote"
    )
    writes = list(import_source.api.writes)
    with pytest.raises(DataLensUtilsError, match="Editor selector wrapper settings"):
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
    assert any(
        "Editor selector wrapper settings" in item["reason"] for item in entry["restrictions"]
    )
    assert json.loads((fresh / "project" / entry["source_file"]).read_text())["data"] == data
    captured = imported_project(import_source, fresh)
    assert not captured.verify(full=True)["mismatches"]
    with pytest.raises(DataLensUtilsError, match="Captured-only"):
        captured.apply(resource_keys=["dashboard:main"])
    assert import_source.api.writes == writes
