"""Three-way plans use their deployment's checkpoint without writing files."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from analytics_toolkit.datalens_utils import planning
from analytics_toolkit.datalens_utils.editing.state import configuration, metadata
from analytics_toolkit.datalens_utils.recipe import Resource, load_registry

from tests.datalens_utils._support.bi import write


@pytest.mark.parametrize(
    ("local_changed", "remote_changed", "draft", "expected"),
    [
        (False, False, False, "unchanged"),
        (True, False, False, "update"),
        (False, True, False, "pull"),
        (True, True, False, "conflict"),
        (False, False, True, "conflict"),
    ],
)
def test_legacy_three_way_actions(local_changed, remote_changed, draft, expected):
    resource = Resource("dataset:sales", "dataset", {}, {})
    entry = SimpleNamespace(id="sales", key="Folder/Sales", saved_id="r", published_id="r")
    old = {**metadata(entry), "fingerprint": "local"}
    if remote_changed:
        entry.saved_id = entry.published_id = "remote"
    if draft:
        entry.saved_id = "draft"
    result, conflicts = planning.legacy_action(
        resource, entry, old, "changed" if local_changed else "local", entry.id
    )
    assert result == expected
    assert bool(conflicts) == (draft or (remote_changed and local_changed))


@pytest.mark.parametrize(
    ("identifier", "entry", "old", "expected"),
    [
        (None, None, None, "create_or_adopt"),
        ("sales", None, None, "missing"),
        ("sales", SimpleNamespace(id="sales"), None, "verify"),
    ],
)
def test_legacy_initial_and_missing_resources(identifier, entry, old, expected):
    result, conflicts = planning.legacy_action(
        Resource("dataset:sales", "dataset", {}, {}), entry, old, "local", identifier
    )
    assert result == expected
    assert bool(conflicts) == (expected == "missing")


def legacy_client(state):
    units = configuration()
    entries = [
        SimpleNamespace(id=value["id"], saved_id="r", published_id="r", key="Folder/" + key)
        for key, value in units.items()
        if value["id"]
    ]
    client = SimpleNamespace(
        navigation=SimpleNamespace(get_entries=lambda **kwargs: entries),
        get=SimpleNamespace(connection=lambda **kwargs: SimpleNamespace(id="conn")),
    )
    state.client_factory = lambda _: nullcontext(client)
    return units, entries


def test_legacy_plan_preserves_current_state_and_classifies_sql_edits(session_state):
    units, entries = legacy_client(session_state)
    state = {
        key: {
            **metadata(next(entry for entry in entries if entry.id == unit["id"])),
            "fingerprint": unit["fingerprint"],
        }
        for key, unit in units.items()
        if unit["id"]
    }
    write(session_state.paths.runtime_root, "edit-state.json", {"resources": state})
    write(
        session_state.paths.runtime_root,
        "resources.json",
        {"resources": {"orphan": {"id": "orphan"}}},
    )
    before = {path: path.read_bytes() for path in session_state.paths.runtime_root.iterdir()}
    report = planning.plan()
    assert not report["conflicts"]
    assert report["orphans"] == ["orphan"]
    assert all(item["action"] in {"unchanged", "reference"} for item in report["actions"])
    key = next(key for key in units if key.startswith("dataset:"))
    asset = next(path for path in units[key]["files"] if path.endswith(".sql"))
    path = session_state.paths.project_root / asset
    path.write_text(path.read_text() + "\n-- changed locally\n")
    report = planning.plan(resource_keys=[key])
    assert (
        next(value for value in report["actions"] if value["resource"] == key)["action"] == "update"
    )
    assert {
        path: path.read_bytes() for path in session_state.paths.runtime_root.iterdir()
    } == before


def test_legacy_plan_reports_configured_id_conflicts_and_duplicate_ids(session_state):
    legacy_client(session_state)
    registry = load_registry()
    key = next(key for key in registry.resources if key.startswith("dataset:"))
    write(
        session_state.paths.runtime_root, "resources.json", {"resources": {key: {"id": "retained"}}}
    )
    report = planning.plan(resource_keys=[key])
    assert any("Configured ID differs" in value["reason"] for value in report["conflicts"])
    path = session_state.paths.project_root / "configs/DL objects/datasets.json"
    datasets = json.loads(path.read_text())
    roles = list(datasets)
    datasets[roles[1]]["id"] = datasets[roles[0]]["id"]
    path.write_text(json.dumps(datasets))
    with pytest.raises(Exception, match="IDs must be unique"):
        planning.plan()


@pytest.mark.parametrize(
    ("old", "live", "expected"),
    [
        ({}, None, "create_or_adopt"),
        ({"id": "a"}, None, "missing"),
        ({}, {"saved_id": "r", "published_id": "r"}, "verify"),
        (
            {"pending_write": {"fingerprint": "local", "metadata": {"saved_id": "draft"}}},
            {"saved_id": "draft"},
            "recover",
        ),
        ({}, {"saved_id": "draft", "published_id": "r"}, "conflict"),
    ],
)
def test_v2_initial_recovery_and_draft_classification(old, live, expected):
    assert planning.classify("local", old, live) == expected
