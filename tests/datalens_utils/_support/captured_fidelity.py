"""Public workflow assertions for readable metadata with no faithful UI writer."""

import copy
import json

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils.bi.captured_pull import replace_data
from tests.datalens_utils.bi.captured_safety import with_revisions
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


def assert_readable_only(source, tmp_path, mutate, *, pull_error="cannot be faithfully written"):
    original = tmp_path / "original"
    report = run_import(source, original, dry_run=False)
    assert report["resources"]["dashboard:main"]["editable"]
    project = imported_project(source, original)
    project.pull(resource_keys=["dashboard:main"])
    files = file_bytes(project.paths.project_root)
    checkpoint = project.paths.runtime_root / "resources.json"
    before = checkpoint.read_bytes()
    data = copy.deepcopy(source.dashboard.data)
    mutate(data)
    changed = replace_data(source, "dashboard", data)
    source.entities[changed.id] = with_revisions(
        changed, saved_id="remote", published_id="remote", rev_id="remote"
    )
    writes = list(source.api.writes)
    with pytest.raises(DataLensUtilsError, match=pull_error):
        project.pull(resource_keys=["dashboard:main"])
    assert file_bytes(project.paths.project_root) == files
    assert checkpoint.read_bytes() == before
    with pytest.raises(DataLensUtilsError, match="changed remotely"):
        project.apply(resource_keys=["dashboard:main"])

    fresh = tmp_path / "fresh"
    report = run_import(source, fresh, dry_run=False)
    entry = report["resources"]["dashboard:main"]
    assert report["written"]
    assert report["capture_complete"]
    assert entry["capture"] == "complete"
    assert not entry["editable"]
    assert any("cannot be faithfully written" in item["reason"] for item in entry["restrictions"])
    assert json.loads((fresh / "project" / entry["source_file"]).read_text())["data"] == data
    assert all(
        item["editable"] for item in report["resources"].values() if item["kind"] == "dataset"
    )
    captured = imported_project(source, fresh)
    assert not captured.verify(full=True)["mismatches"]
    with pytest.raises(DataLensUtilsError, match="Captured-only"):
        captured.apply(resource_keys=["dashboard:main"])
    assert source.api.writes == writes
