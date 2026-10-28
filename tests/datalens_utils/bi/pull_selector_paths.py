"""Captured selector pulls retain authored filenames and reject missing identities."""

import json

import pytest
from analytics_toolkit.datalens_utils import bi_engine
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError

from tests.datalens_utils._support.captured_import import imported_project, run_import
from tests.datalens_utils._support.captured_selectors import selector_source
from tests.datalens_utils.bi.occurrence_fidelity import file_bytes


def change_remote_selector(source, dashboard, action):
    removed = dashboard.update.remove_item("filters").mode("publish").execute()
    if action == "remove":
        return removed
    update = removed.update
    update.add_selector(
        group="filters",
        item_id="choice",
        param_name="choice",
        element="select",
        title="Choice",
        options=["A", "B"],
        hint="New remote hint",
    )
    update.add_group_selector(group="filters", tab="one", item_id="filters", at=(0, 6, 12, 2))
    return update.mode("publish").execute()


@pytest.mark.parametrize("action", ["hint", "remove"])
def test_captured_pull_retains_selector_paths_and_stable_keys(
    import_source, tmp_path, monkeypatch, action
):
    dashboard = selector_source(import_source, "local", False)
    run_import(import_source, tmp_path, dry_run=False)
    project = imported_project(import_source, tmp_path)
    root = project.paths.project_root
    canonical = root / "configs/UI/selectors/choice.json"
    authored = canonical.with_name("business_filter.json")
    canonical.rename(authored)
    import_source.client.get.dashboard = import_source.api.client.get.dashboard
    monkeypatch.setattr(bi_engine, "resolve_location", lambda *_args: import_source.api.folder)
    project.apply(resource_keys=["dashboard:main"])
    current = import_source.api.client.get.dashboard(by_id=dashboard.id, branch="published")
    import_source.entities[current.id] = current
    assert not project.verify(full=True)["mismatches"]
    files = file_bytes(root)
    checkpoint = project.paths.runtime_root / "resources.json"
    before = checkpoint.read_bytes()
    changed = change_remote_selector(import_source, current, action)
    import_source.entities[changed.id] = changed
    writes = list(import_source.api.writes)
    if action == "remove":
        with pytest.raises(DataLensUtilsError, match="Managed selector is missing: choice"):
            project.pull(resource_keys=["dashboard:main"])
        assert file_bytes(root) == files
        assert checkpoint.read_bytes() == before
        with pytest.raises(DataLensUtilsError, match="changed remotely"):
            project.apply(resource_keys=["dashboard:main"])
    else:
        project.pull(resource_keys=["dashboard:main"])
        assert not canonical.exists()
        assert json.loads(authored.read_text())["control"]["hint"] == "New remote hint"
        assert not project.verify(full=True)["mismatches"]
    assert import_source.api.writes == writes
