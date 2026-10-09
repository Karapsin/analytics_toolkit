"""Connector validation, read-only references and intentional credential rotation."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from analytics_toolkit.datalens_utils.bi_connections import (
    connection_builder,
    connection_credentials,
    reconcile_connection,
    source_spec,
    validate_connection_fields,
    validate_factories,
)
from analytics_toolkit.datalens_utils.errors import (
    DataLensCapabilityError,
    DataLensConfigurationError,
    DataLensUtilsError,
)
from analytics_toolkit.datalens_utils.recipe import load_registry
from analytics_toolkit.datalens_utils.resources.bi_store import BIResourceStore
from datalens_sdk import (
    Connection,
    ConnectionUpdate,
    DataLensClientYC,
    EntryLocation,
    NoAuthProvider,
)

from tests.datalens_utils._support.bi import write

LOCATION = EntryLocation.path("Offline/Variants")


def definition(**changes):
    return {
        "mode": "managed",
        "name": "CH",
        "connector": "clickhouse",
        "parameters": {"host": "db", "port": 9000},
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"parameters": []},
        {"secrets": []},
        {"parameters": {"unknown": 1}},
        {"parameters": {"type": "clickhouse"}},
        {"parameters": {"password": "inline"}},
        {"parameters": {"ssl_ca": "not allowlisted"}},
        {"parameters": {"host": "db"}},
        {
            "parameters": {"host": "db", "port": 9000, "username": "both"},
            "secrets": {"username": "USER_ENV"},
        },
    ],
)
def test_invalid_connector_parameters_fail_without_requests(bi_project, monkeypatch, changes):
    with DataLensClientYC(auth=NoAuthProvider()) as client, pytest.raises(
        DataLensConfigurationError
    ):
        connection_builder(client, definition(**changes), LOCATION)
    assert not bi_project.paths.runtime_root.exists()


def test_connector_inventory_is_authoritative(bi_project):
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        with pytest.raises(DataLensCapabilityError, match="absent"):
            connection_builder(client, definition(connector="invented"), LOCATION)
        builder = connection_builder(client, definition(description="Managed"), LOCATION)
        assert not builder.missing_required()


def test_credentials_are_read_only_during_explicit_rotation(monkeypatch):
    monkeypatch.delenv("BI_PASSWORD", raising=False)
    value = definition(secrets={"password": "BI_PASSWORD"}, credentials_revision="v2")
    with pytest.raises(DataLensConfigurationError, match="BI_PASSWORD"):
        connection_credentials(value, {}, creating=True)
    assert connection_credentials(value, {"credentials_revision": "v2"}, creating=False) == {}
    monkeypatch.setenv("BI_PASSWORD", "synthetic value")
    assert connection_credentials(value, {"credentials_revision": "v1"}, creating=False) == {
        "password": "synthetic value"
    }
    assert connection_credentials(value, {"credentials_revision": "v2"}, creating=True) == {
        "password": "synthetic value"
    }


@pytest.mark.parametrize("problem", ["factory", "parameters", "connection", "sql_parameter"])
def test_source_factories_and_parameters_are_checked_before_build(bi_project, problem):
    source = {"factory": "ch_table", "parameters": {"db_name": "example", "table_name": "sales"}}
    connection = Connection(id="ch", name="CH", type="clickhouse", installation="yacloud")
    if problem == "factory":
        source["factory"] = "invented"
    elif problem == "parameters":
        source["parameters"]["unknown"] = "value"
    elif problem == "connection":
        connection = replace(connection, type="postgresql")
    else:
        source["sql_file"] = "assets/source.sql"
        source["parameters"]["subsql"] = "SELECT 1"
    with DataLensClientYC(auth=NoAuthProvider()) as client, pytest.raises(
        DataLensConfigurationError
    ):
        source_spec(client, source, connection, "source")


def test_subselect_source_uses_contained_asset_and_join_boundary(bi_project):
    root = bi_project.paths.project_root
    (root / "assets").mkdir()
    (root / "assets/source.sql").write_text("SELECT 1")
    connection = Connection(id="ch", name="CH", type="clickhouse", installation="yacloud")
    with DataLensClientYC(auth=NoAuthProvider()) as client:
        factory, parameters = source_spec(
            client,
            {"factory": "ch_subselect", "sql_file": "assets/source.sql"},
            connection,
            "source",
        )
        assert callable(factory)
        assert parameters == {"subsql": "SELECT 1"}
        registry = load_registry()
        other = registry.resources["connection:ch"]
        registry.resources["connection:second"] = replace(other, key="connection:second")
        dataset = registry.resources["dataset:sales"].definition
        dataset["sources"]["right"] = {**dataset["sources"]["source"], "connection": "second"}
        dataset["relations"] = {"joined": {"left": "source", "right": "right"}}
        with pytest.raises(DataLensConfigurationError, match="one supported connection"):
            validate_factories(client, registry)


@pytest.mark.parametrize("problem", ["connector", "name", "location", "missing"])
def test_existing_connection_is_never_modified(bi_project, problem):
    registry = load_registry()
    resource = registry.resources["connection:ch"]
    resource.definition["target"] = {"path": "Offline/Variants"}
    value = Connection(
        id="ch", name="CH", type="clickhouse", installation="yacloud", location=LOCATION
    )
    if problem == "connector":
        value = replace(value, type="postgresql")
    elif problem == "name":
        value = replace(value, name="Other")
    elif problem == "location":
        value = replace(value, location=EntryLocation.path("Elsewhere"))
    else:
        resource.definition.pop("id")
    store = BIResourceStore(registry)
    store.set_location(SimpleNamespace(key="Offline/Variants/", list_entries=list))
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(
            INSTALLATION=public.INSTALLATION,
            capabilities=public.capabilities,
            create=public.create,
            get=SimpleNamespace(connection=Mock(return_value=value)),
        )
        with pytest.raises(DataLensUtilsError):
            reconcile_connection(client, store, resource)


def test_managed_creation_rotation_and_noop_do_not_persist_secrets(bi_project, monkeypatch):
    root = bi_project.paths.project_root
    configured = definition(secrets={"password": "BI_PASSWORD"}, credentials_revision="v1")
    write(root, "configs/DL objects/connections.json", {"ch": configured})
    monkeypatch.setenv("BI_PASSWORD", "synthetic credential")
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(SimpleNamespace(key="Offline/Variants/", list_entries=list))
    value = Connection(
        id="ch",
        name="CH",
        type="clickhouse",
        installation="yacloud",
        location=LOCATION,
        raw={"host": "db", "port": 9000},
    )
    builds, updates = [], []
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        builder = connection_builder(public, configured, LOCATION)

        def build(selected):
            builds.append(selected.to_spec())
            return value

        def execute(selected):
            updates.append(selected.to_spec())
            return value

        monkeypatch.setattr(type(builder), "build", build)
        monkeypatch.setattr(ConnectionUpdate, "execute", execute)
        client = SimpleNamespace(
            INSTALLATION=public.INSTALLATION,
            capabilities=public.capabilities,
            create=public.create,
            get=SimpleNamespace(connection=Mock(return_value=value)),
        )
        resource = registry.resources["connection:ch"]
        assert reconcile_connection(client, store, resource).id == "ch"
        store.remember(resource.key, value)
        monkeypatch.delenv("BI_PASSWORD")
        assert reconcile_connection(client, store, resource).id == "ch"
        assert len(builds) == 1
        assert not updates
        monkeypatch.setenv("BI_PASSWORD", "rotated synthetic credential")
        resource.definition["credentials_revision"] = "v2"
        reconcile_connection(client, store, resource)
        store.remember(resource.key, value)
        assert len(updates) == 1
        assert "synthetic" not in store.path.read_text()
        assert store.state["resources"][resource.key]["credentials_revision"] == "v2"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@db",
        "https://db?api_key=synthetic",
        "https://db?key=synthetic",
        "https://db#synthetic",
    ],
)
def test_connection_url_credentials_are_rejected_before_persistence(url):
    builder = SimpleNamespace(fields_help=lambda: {"url": {}})
    with pytest.raises(DataLensConfigurationError, match="credentials"):
        validate_connection_fields(builder, {"parameters": {"url": url}})


def test_existing_connection_wrong_identity_is_read_only(bi_project):
    registry = load_registry()
    store = BIResourceStore(registry)
    store.set_location(SimpleNamespace(key="Offline/Variants/", list_entries=list))
    value = Connection(id="wrong", name="CH", type="clickhouse", location=LOCATION)
    with DataLensClientYC(auth=NoAuthProvider()) as public:
        client = SimpleNamespace(
            INSTALLATION=public.INSTALLATION,
            capabilities=public.capabilities,
            create=public.create,
            get=SimpleNamespace(connection=Mock(return_value=value)),
        )
        with pytest.raises(DataLensUtilsError, match="identity differs"):
            reconcile_connection(client, store, registry.resources["connection:ch"])
    assert store.state["resources"]["connection:ch"]["id"] == "ch"
