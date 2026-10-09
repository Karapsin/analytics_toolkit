"""Bulk metadata reads, scoped prerequisites and pre-write capability blockers."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils.bi_engine import (
    read_entities,
    resolve_write_secrets,
    validate_update_scope,
)
from analytics_toolkit.datalens_utils.bi_preflight import operation_blockers, sql_access_issues
from analytics_toolkit.datalens_utils.errors import DataLensUtilsError
from analytics_toolkit.datalens_utils.recipe import Resource, ResourceRegistry, load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import Connection, DataLensClientYC, NoAuthProvider

from tests.datalens_utils._support.bi import dataset


def test_bulk_reads_keep_requested_branch_and_fetch_nonrevisioned_references(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry, readonly=True)
    inventory = Mock(return_value=[SimpleNamespace(id="sales", scope="dataset")])
    client = SimpleNamespace(
        navigation=SimpleNamespace(get_entries=inventory),
        get=SimpleNamespace(
            dataset=Mock(return_value=dataset()),
            connection=Mock(return_value=Connection(id="ch", name="CH", type="clickhouse")),
        ),
    )
    entities = read_entities(
        client, store, registry, ["connection:ch", "dataset:sales"], branch="saved"
    )
    assert set(entities) == {"connection:ch", "dataset:sales"}
    inventory.assert_called_once_with(ids=["sales"], page_size=100)
    client.get.dataset.assert_called_once_with(by_id="sales", branch="saved")
    client.get.connection.assert_called_once_with(by_id="ch")
    assert not store.path.exists()


@pytest.mark.parametrize("problem", ["missing", "kind", "identity"])
def test_retained_read_mismatches_block_recreation(bi_project, problem):
    registry = load_registry()
    store = BIResourceStore(registry, readonly=True)
    entries = (
        []
        if problem == "missing"
        else [SimpleNamespace(id="sales", scope="widget" if problem == "kind" else "dataset")]
    )
    value = replace(dataset(), id="wrong") if problem == "identity" else dataset()
    client = SimpleNamespace(
        navigation=SimpleNamespace(get_entries=lambda **kwargs: entries),
        get=SimpleNamespace(dataset=Mock(return_value=value)),
    )
    with pytest.raises(DataLensUtilsError, match=r"missing|kind differs|identity differs"):
        read_entities(client, store, registry, ["dataset:sales"])
    if problem != "identity":
        client.get.dataset.assert_not_called()
    if problem == "missing":
        assert read_entities(client, store, registry, ["dataset:sales"], allow_missing=True) == {}


def test_unretained_resources_do_not_request_inventory(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry, readonly=True)
    store.state["resources"].pop("dataset:sales")
    client = Mock()
    assert not read_entities(client, store, registry, ["dataset:sales"])
    client.navigation.get_entries.assert_not_called()


@pytest.mark.parametrize("problem", ["name", "connector", "unverified", "missing"])
def test_scoped_updates_require_matching_references_and_verified_dependencies(bi_project, problem):
    registry = load_registry()
    store = BIResourceStore(registry, readonly=True)
    connection = Connection(
        id="ch",
        name="Wrong" if problem == "name" else "CH",
        type="postgres" if problem == "connector" else "clickhouse",
    )
    entities = {"connection:ch": connection}
    if problem == "unverified":
        entities["dataset:sales"] = dataset()
    with DataLensClientYC(auth=NoAuthProvider()) as client, pytest.raises(
        DataLensUtilsError, match=r"reference differs|unverified dependency|Missing dependency"
    ):
        validate_update_scope(client, store, registry, {"chart:trend", "dashboard:main"}, entities)
    assert not store.path.exists()


def test_credential_preflight_requires_environment_only_for_intentional_rotation(
    bi_project, monkeypatch
):
    registry = load_registry()
    resource = registry.resources["connection:ch"]
    resource.definition.update(
        mode="managed", credentials_revision="v2", secrets={"password": "BI_ROTATION_PASSWORD"}
    )
    store = BIResourceStore(registry, readonly=True)
    monkeypatch.delenv("BI_ROTATION_PASSWORD", raising=False)
    with pytest.raises(DataLensUtilsError, match="environment reference"):
        resolve_write_secrets(store, registry, {resource.key}, {})
    store.state["resources"][resource.key]["credentials_revision"] = "v2"
    resolve_write_secrets(
        store, registry, {resource.key}, {resource.key: Connection(id="ch", type="clickhouse")}
    )
    store.state["resources"][resource.key]["credentials_revision"] = "v1"
    monkeypatch.setenv("BI_ROTATION_PASSWORD", "synthetic")
    resolve_write_secrets(store, registry, {resource.key}, {})
    assert not store.path.exists()


@pytest.mark.parametrize(
    ("level", "blocked"), [("off", True), ("subselect", True), ("dashsql", False)]
)
def test_sql_editor_access_is_required_only_for_ql_resources(level, blocked):
    connection = Resource("connection:ch", "connection", {"mode": "existing"}, {})
    chart = Resource(
        "chart:sql", "chart", {"family": "ql", "connection": "ch"}, {}, (connection.key,)
    )
    registry = ResourceRegistry({value.key: value for value in (connection, chart)})
    entities = {
        connection.key: Connection(id="ch", type="clickhouse", raw={"raw_sql_level": level})
    }
    assert bool(sql_access_issues(registry, entities, {chart.key})) == blocked
    assert not sql_access_issues(registry, entities, {connection.key})


def test_topology_removal_is_a_pre_persistence_blocker(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry, readonly=True)
    value = dataset()
    value = replace(
        value, sources=(*value.sources, replace(value.sources[0], id="unmanaged", title="other"))
    )
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        blockers = operation_blockers(
            client, store, registry, {"dataset:sales"}, {"dataset:sales": value}
        )
    assert blockers[0]["status"] == "blocked"
    assert "topology removal" in blockers[0]["reason"]
