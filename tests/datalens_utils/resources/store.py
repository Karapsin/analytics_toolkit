"""Durable IDs, move boundaries, and create/rename race recovery."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.resources.store import ResourceStore
from datalens_sdk import APIErrorContext, ConflictError


def folder(path="Root/"):
    return SimpleNamespace(key=path, list_entries=list)


def entity(name="Chart", path="Root/Chart", workbook_id=None):
    value = SimpleNamespace(id="offline", name=name, key=path, workbook_id=workbook_id)
    value.rename = lambda name: entity(name, path.rpartition("/")[0] + "/" + name)
    value.move = lambda target: entity(name, target.key.rstrip("/") + "/" + name)
    return value


def test_seed_conflicts_and_atomic_checkpoint_cleanup(session_state):
    store = ResourceStore(folder(), {"folder_path": "Root"})
    store.seed_configured_ids(
        {"chart:x": {"id": "offline", "name": "Chart", "scope": "widget"}, "empty": {}}
    )
    store.seed_configured_ids({"chart:x": {"id": "offline", "name": "Chart", "scope": "widget"}})
    assert json.loads(store.path.read_text())["resources"]["chart:x"]["id"] == "offline"
    with pytest.raises(DataLensUtilsError, match="disagree"):
        store.seed_configured_ids({"chart:x": {"id": "other"}})
    with patch(
        "analytics_toolkit.datalens_utils.resources.store.Path.replace",
        side_effect=OSError("failed replacement"),
    ), pytest.raises(OSError, match="failed replacement"):
        store._save()
    assert not list(store.path.parent.glob(".resources-*"))


@pytest.mark.parametrize(
    "problem", ["version", "identity", "scope", "duplicate", "outside", "name", "workbook"]
)
def test_store_rejects_wrong_ownership(session_state, problem):
    store = ResourceStore(folder(), {"folder_path": "Root"})
    if problem in {"version", "identity"}:
        store.state["version"] = 2 if problem == "version" else 1
        store._save()
        target = {"folder_path": "Root" if problem == "version" else "Other"}
        with pytest.raises(DataLensUtilsError, match="belongs"):
            ResourceStore(folder(), target)
    elif problem == "scope":
        store.checkpoint("chart:x", entity(), scope="dataset")
        with pytest.raises(DataLensUtilsError, match="kind"):
            store.existing("chart:x", "Chart", lambda **kwargs: entity(), scope="widget")
    elif problem == "duplicate":
        store.entries["widget"] = [SimpleNamespace(id="offline", scope="widget", name="Chart")] * 2
        with pytest.raises(DataLensUtilsError, match="Multiple"):
            store.existing("chart:x", "Chart", Mock(), scope="widget")
    else:
        value = entity(
            path="Else/Chart" if problem == "outside" else "Root/Chart",
            workbook_id="workbook" if problem == "workbook" else None,
        )
        with pytest.raises(DataLensUtilsError):
            store.verify_location(value, "Other" if problem == "name" else "Chart", scope="widget")


def test_create_conflict_adopts_exact_match_and_missing_match_rethrows(session_state):
    store = ResourceStore(folder(), {})
    value = entity()
    builder = Mock()
    conflict = ConflictError(APIErrorContext(409, "CONFLICT", "offline"))
    builder.build.side_effect = conflict
    store.folders["widget"] = SimpleNamespace(
        key="Root/",
        list_entries=lambda: [SimpleNamespace(id="offline", scope="widget", name="Chart")],
    )
    getter = Mock(return_value=value)
    assert store.create("chart:x", "Chart", builder, getter, scope="widget") is value
    store.state["resources"].clear()
    store.folders["widget"].list_entries = list
    with pytest.raises(ConflictError) as caught:
        store.create("chart:x", "Chart", builder, getter, scope="widget")
    assert caught.value is conflict


def test_rename_and_mutation_recorder_restore_prior_callback(session_state):
    store = ResourceStore(folder(), {})
    first = entity()
    second = entity("Changed", "Root/Changed")
    callback = Mock()
    old = Mock()
    store.mutation_recorder = old
    getter = Mock(return_value=second)
    store.checkpoint("chart:x", first, scope="widget")
    with store.record_mutations(callback):
        assert store.rename("chart:x", first, "Changed", getter) is second
        store.persisted("chart:x", second, getter, branch="saved")
    assert store.mutation_recorder is old
    assert callback.call_count == 2
    getter.assert_called_with(by_id="offline", branch="saved")
    store.checkpoint("chart:x", SimpleNamespace(id="offline", name="Changed", key=None))
    store.phase("chart:x", "saved", pending_items=[], managed_items=["x"])
    assert store.state["resources"]["chart:x"]["managed_items"] == ["x"]


def test_foreign_recorded_folder_never_expands_move_boundary(session_state):
    store = ResourceStore(folder("Previous/"), {"folder_path": "Previous"})
    store.checkpoint("item", entity(path="Foreign/Chart"), scope="widget")
    store.complete_target()
    moved = ResourceStore(folder(), {"folder_path": "Root"}, allow_folder_move=True)
    moved.set_folders({"widget": folder("Root/charts/")}, entries=[])
    with pytest.raises(DataLensUtilsError, match="outside"):
        moved.existing(
            "item", "Chart", lambda **kwargs: entity(path="Foreign/Chart"), scope="widget"
        )


@pytest.mark.parametrize(
    ("scope", "recorded", "workbook"),
    [
        ("widget", "Previous/charts", False),
        ("widget", "Previous/old", False),
        ("dash", "Previous", False),
        ("widget", "Previous/charts", True),
    ],
)
def test_move_only_recorded_source_and_commit_target_after_verification(
    session_state, scope, recorded, workbook
):
    store = ResourceStore(folder("Previous/"), {"folder_path": "Previous", "connection_id": "conn"})
    store.checkpoint("item", entity(path=recorded + "/Chart"), scope=scope)
    store.complete_target()
    target = {"folder_path": "Root", "connection_id": "conn"}
    moved = ResourceStore(folder(), target, allow_folder_move=True)
    destination = folder("Root/charts/" if scope == "widget" else "Root/")
    moved.set_folders({"widget": destination, "dash": folder(), "dataset": folder()}, entries=[])
    before = entity(path=recorded + "/Chart", workbook_id="book" if workbook else None)
    after = entity(path=destination.key + "Chart")
    getter = Mock(side_effect=[before, after])
    if workbook:
        with pytest.raises(DataLensUtilsError, match="outside"):
            moved.existing("item", "Chart", getter, scope=scope)
    else:
        assert moved.existing("item", "Chart", getter, scope=scope) is after
        assert moved.state["target"]["folder_path"] == "Previous"
        moved.complete_target()
        assert moved.state["target"] == target
        moved.complete_target()
