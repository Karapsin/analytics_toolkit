"""Editing cache identity, status classification, and atomic file replacement."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.editing import state
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError


def entry(**changes):
    return SimpleNamespace(id="id", saved_id="rev", published_id="rev", key="Root/Chart", **changes)


def test_inventory_rejects_duplicate_ids_and_skips_empty_inventory():
    client = Mock()
    with pytest.raises(DataLensUtilsError, match="unique"):
        state.inventory(client, {"a": {"id": "same"}, "b": {"id": "same"}})
    assert state.inventory(client, {"a": {"id": None}}) == {"a": None}
    client.navigation.get_entries.assert_not_called()


@pytest.mark.parametrize("problem", ["version", "target"])
def test_cache_ownership_rejected_or_explicitly_reset(session_state, problem):
    cache = state.EditState({"target": "one"})
    cache.value["version"] = 2 if problem == "version" else 1
    cache.save()
    with pytest.raises(DataLensUtilsError, match="another deployment"):
        state.EditState({"target": "two" if problem == "target" else "one"})
    if problem == "target":
        assert state.EditState({"target": "two"}, reset=True).value["target"] == {"target": "two"}


@pytest.mark.parametrize(
    ("local", "remote", "expected"),
    [
        (False, False, "unchanged"),
        (True, False, "local"),
        (False, True, "remote"),
        (True, True, "both"),
    ],
)
def test_changes_classify_independent_local_and_remote_edits(
    session_state, local, remote, expected
):
    cache = state.EditState({})
    files = {"file": {"value": 1}}
    unit = {"files": files, "fingerprint": state.fingerprint(files)}
    current = entry()
    cache.remember("item", unit, current)
    if local:
        unit["fingerprint"] = "changed"
    if remote:
        current.published_id = None
    assert cache.changes({"item": unit}, {"item": current}) == {"item": expected}
    assert cache.changes({"item": unit}, {"item": None}) == {"item": "missing"}
    assert cache.changes({"untracked": unit}, {"untracked": current}) == {"untracked": "untracked"}


def test_saved_baseline_requires_publish_and_cache_cleanup_on_failure(session_state):
    cache = state.EditState({})
    current = entry()
    current.saved_id = "draft"
    unit = {"files": {}, "fingerprint": state.fingerprint({})}
    cache.remember("item", unit, current, branch="saved")
    assert cache.changes({"item": unit}, {"item": current}) == {"item": "local"}
    with pytest.raises(DataLensUtilsError, match="Unplanned"):
        cache.record_write("unknown", current)
    with patch.object(
        state.Path, "replace", side_effect=OSError("failed atomic write")
    ), pytest.raises(OSError, match="failed atomic write"):
        cache.save()
    assert not list(cache.path.parent.glob(".edit-state-*"))


def test_pull_writes_known_files_preserves_json_indent_and_skips_equal_values(session_state):
    root = session_state.paths.project_root
    (root / "four.json").write_text('{\n    "value": 1\n}\n', encoding="utf-8")
    (root / "two.json").write_text('{\n  "value": 1\n}\n', encoding="utf-8")
    (root / "asset.sql").write_text("SELECT 1", encoding="utf-8")
    files = {"four.json": {"value": 2}, "two.json": {"value": 2}, "asset.sql": "SELECT 2"}
    state.write_files(files)
    assert '\n    "value"' in (root / "four.json").read_text()
    assert '\n  "value"' in (root / "two.json").read_text()
    assert (root / "asset.sql").read_text() == "SELECT 2"
    with patch.object(state.Path, "replace") as replace:
        state.write_files(files)
    replace.assert_not_called()
    for path in ("missing.sql", "../escape.sql"):
        with pytest.raises(DataLensUtilsError, match="unknown project file"):
            state.write_files({path: "SELECT 3"})
    assert (root / "asset.sql").read_text() == "SELECT 2"


def test_configuration_rejects_escaping_asset(session_state):
    path = session_state.paths.project_root / "configs/DL objects/datasets.json"
    definitions = json.loads(path.read_text())
    next(iter(definitions.values()))["projection_file"] = "../outside.sql"
    path.write_text(json.dumps(definitions), encoding="utf-8")
    with pytest.raises(DataLensUtilsError, match="outside"):
        state.configuration()


def test_three_way_merge_preserves_remote_deletion():
    assert state.merge({"x": 1, "y": 1}, {"x": 2, "y": 1}, {"x": 1}) == {"x": 2}
