"""Typed container adoption, target identity and create/read failure recovery."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils import BIProjectDeployment, TargetLocation
from analytics_toolkit.datalens_utils.bi_containers import reconcile_container, resolve_location
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from analytics_toolkit.datalens_utils.session import current_session
from datalens_sdk import (
    APIErrorContext,
    Collection,
    CollectionCreate,
    CollectionUpdate,
    ConflictError,
    DataLensClientYC,
    NoAuthProvider,
    Workbook,
    WorkbookCreate,
    WorkbookUpdate,
)

from tests.datalens_utils._support.bi import write


def setup(project, kind, *, retained=False, parent=False):
    filename = "collections" if kind == "collection" else "workbooks"
    definition = {"name": "Team", "description": "Shared"}
    if retained:
        definition["id"] = "team"
    if parent:
        definition["parent"] = "parent"
    write(
        project.paths.project_root, "configs/DL objects/" + filename + ".json", {"team": definition}
    )
    if parent:
        project.paths.project_root / "configs/DL objects/collections.json"
        values = {"parent": {"id": "parent", "name": "Parent"}}
        if kind == "collection":
            values["team"] = definition
        write(project.paths.project_root, "configs/DL objects/collections.json", values)
    registry = load_registry()
    resource = registry.resources[kind + ":team"]
    store = BIResourceStore(registry)
    return resource, store


def entity(kind, **kwargs):
    factory = Collection if kind == "collection" else Workbook
    return factory(id="team", name="Team", description="Shared", **kwargs)


@pytest.mark.parametrize("kind", ["collection", "workbook"])
def test_typed_container_create_and_noop(bi_project, monkeypatch, kind):
    resource, store = setup(bi_project, kind)
    value = entity(kind)
    builds = []
    updates = []
    create_type = CollectionCreate if kind == "collection" else WorkbookCreate
    update_type = CollectionUpdate if kind == "collection" else WorkbookUpdate
    monkeypatch.setattr(
        create_type, "build", lambda builder: builds.append(builder.to_spec()) or value
    )
    monkeypatch.setattr(
        update_type, "execute", lambda builder: updates.append(builder.to_spec()) or value
    )
    getter = Mock(return_value=value)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(create=public.create, get=SimpleNamespace(**{kind: getter}))
        assert reconcile_container(client, store, resource, {}).id == "team"
        store.remember(resource.key, value)
        assert reconcile_container(client, store, resource, {}).id == "team"
    assert len(builds) == 1
    assert not updates
    assert store.state["resources"][resource.key]["id"] == "team"


@pytest.mark.parametrize("kind", ["collection", "workbook"])
def test_nested_unique_adoption_and_metadata_update(bi_project, monkeypatch, kind):
    resource, store = setup(bi_project, kind, parent=True)
    parent = Collection(id="parent", name="Parent")
    parent_field = "parent_id" if kind == "collection" else "collection_id"
    value = entity(kind, **{parent_field: "parent"})
    summary = SimpleNamespace(id="team", name="Team")
    monkeypatch.setattr(Collection, "list_entries", lambda *args, **kwargs: [summary])
    getter = Mock(return_value=value)
    client = SimpleNamespace(get=SimpleNamespace(**{kind: getter}))
    assert reconcile_container(client, store, resource, {"collection:parent": parent}) is value
    store.remember(resource.key, value)
    resource.definition.update(name="Renamed", description="Updated")
    changed = replace(value, name="Renamed", description="Updated")
    update_type = CollectionUpdate if kind == "collection" else WorkbookUpdate
    updates = []

    def execute(builder):
        updates.append(builder.to_spec())
        getter.return_value = changed
        return changed

    monkeypatch.setattr(update_type, "execute", execute)
    result = reconcile_container(client, store, resource, {"collection:parent": parent})
    assert result.name == "Renamed"
    assert updates[0].changes == {"name": "Renamed", "description": "Updated"}


@pytest.mark.parametrize("kind", ["collection", "workbook"])
def test_container_creation_is_checkpointed_before_failed_read(bi_project, monkeypatch, kind):
    resource, store = setup(bi_project, kind)
    value = entity(kind)
    create_type = CollectionCreate if kind == "collection" else WorkbookCreate
    builds = []
    monkeypatch.setattr(
        create_type, "build", lambda builder: builds.append(builder.to_spec()) or value
    )
    getter = Mock(side_effect=RuntimeError("read failed"))
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(create=public.create, get=SimpleNamespace(**{kind: getter}))
        with pytest.raises(RuntimeError, match="read failed"):
            reconcile_container(client, store, resource, {})
        resumed = BIResourceStore(load_registry())
        getter.side_effect = None
        getter.return_value = value
        assert (
            reconcile_container(client, resumed, resumed.registry.resources[resource.key], {}).id
            == "team"
        )
    assert len(builds) == 1


@pytest.mark.parametrize(
    "problem", ["ambiguous", "parent", "metadata", "root_conflict", "nested_conflict"]
)
def test_container_conflicts_preserve_cloud_identity(bi_project, monkeypatch, problem):
    parent = Collection(id="parent", name="Parent")
    nested = problem in {"ambiguous", "parent", "nested_conflict"}
    resource, store = setup(
        bi_project, "workbook", parent=nested, retained=problem in {"parent", "metadata"}
    )
    value = entity(
        "workbook", collection_id="other" if problem == "parent" else "parent" if nested else None
    )
    entries = [SimpleNamespace(id="team", name="Team")] * (2 if problem == "ambiguous" else 0)
    monkeypatch.setattr(Collection, "list_entries", lambda *args, **kwargs: entries)
    if problem == "metadata":
        value = replace(value, description="Unexpected")
        monkeypatch.setattr(WorkbookUpdate, "execute", lambda builder: value)
    conflict = ConflictError(APIErrorContext(409, "CONFLICT", "synthetic"))
    monkeypatch.setattr(WorkbookCreate, "build", Mock(side_effect=conflict))
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(
            create=public.create, get=SimpleNamespace(workbook=Mock(return_value=value))
        )
        with pytest.raises((DataLensUtilsError, ConflictError)):
            reconcile_container(
                client, store, resource, {"collection:parent": parent} if nested else {}
            )


def test_workbook_target_resolution_and_folder_fallback(bi_project, monkeypatch):
    state = current_session()
    book = Workbook(id="book", name="Book")
    client = SimpleNamespace(get=SimpleNamespace(workbook=Mock(return_value=book)))
    state.deployment = BIProjectDeployment(
        "Dashboard", TargetLocation.workbook(key="team"), organization_id="offline"
    )
    assert resolve_location(client, {"workbook:team": book}) is book
    state.deployment = BIProjectDeployment(
        "Dashboard", TargetLocation.workbook(by_id="book"), organization_id="offline"
    )
    assert resolve_location(client, {}) is book
    client.get.workbook.return_value = replace(book, id="wrong")
    with pytest.raises(DataLensUtilsError, match="identity differs"):
        resolve_location(client, {})
    state.deployment = bi_project.deployment
    monkeypatch.setattr(
        "analytics_toolkit.datalens_utils.bi_containers.ensure_target_folder",
        lambda **kwargs: "folder",
    )
    assert resolve_location(client, {}) == "folder"


def test_wrong_container_identity_is_rejected_before_update(bi_project):
    resource, store = setup(bi_project, "workbook", retained=True)
    value = Workbook(id="wrong", name="Team", description="Shared")
    with pytest.raises(DataLensUtilsError, match="identity differs"):
        reconcile_container(
            SimpleNamespace(get=SimpleNamespace(workbook=Mock(return_value=value))),
            store,
            resource,
            {},
        )
    assert store.state["resources"][resource.key]["id"] == "team"
