"""Public connector factories and intentional credential rotation."""

from __future__ import annotations

import inspect
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from datalens_sdk import Connection, EntryLocation

from .capabilities import get_capabilities
from .errors import DataLensCapabilityError, DataLensConfigurationError, DataLensUtilsError
from .resources.bi_store import CONNECTION_METADATA_FIELDS, safe_parameter
from .settings import asset_path

_SECRET_FIELD = re.compile(
    r"password|token|secret|credential|private_key|api_key|jwt|auth_header|plain_headers",
    re.IGNORECASE,
)
_RESERVED = {"name", "type", "dir_path", "workbook_id", "collection_id", "description"}


def validate_connection_fields(builder: Any, definition: dict[str, Any]) -> None:
    fields = builder.fields_help()
    parameters, secrets = definition.get("parameters", {}), definition.get("secrets", {})
    if not isinstance(parameters, dict) or not isinstance(secrets, dict):
        msg = "Connection parameters and secrets must be objects."
        raise DataLensConfigurationError(msg)
    unknown = (parameters.keys() | secrets.keys()) - (fields.keys() - _RESERVED)
    if unknown:
        msg = f"Unknown or reserved connector fields: {sorted(unknown)}."
        raise DataLensConfigurationError(msg)
    if parameters.keys() & secrets.keys():
        msg = "Connection fields cannot be both parameters and secret references."
        raise DataLensConfigurationError(msg)
    if any(_SECRET_FIELD.search(key) for key in parameters):
        msg = "Credentials must use environment references in secrets."
        raise DataLensConfigurationError(msg)
    if set(parameters) - CONNECTION_METADATA_FIELDS:
        msg = "Connection parameters must belong to the nonsecret metadata allowlist."
        raise DataLensConfigurationError(msg)

    for key, value in parameters.items():
        if key in {"url", "portal"} and isinstance(value, str):
            url = urlsplit(value)
            if (
                url.username
                or url.password
                or any(_SECRET_FIELD.search(name) for name, _ in parse_qsl(url.query))
                or safe_parameter(key, value) != value
            ):
                msg = "Connection URLs must not contain credentials."
                raise DataLensConfigurationError(msg)


def connection_builder(client: Any, definition: dict[str, Any], location: Any) -> Any:
    connector = definition["connector"]
    if connector not in client.capabilities["connectors"]:
        report = get_capabilities(
            installation="yc" if client.INSTALLATION == "yacloud" else "enterprise"
        )
        report["operations"]["connection.create"] = {
            "status": "blocked",
            "reason": "Connector absent from configured client inventory: " + connector,
            "requirements": [],
        }
        msg = "connection.create"
        raise DataLensCapabilityError(msg, report)
    builder = getattr(client.create.connection, connector)(
        name=definition["name"] or "Offline connection validation", location=location
    )
    validate_connection_fields(builder, definition)
    parameters, secrets = definition.get("parameters", {}), definition.get("secrets", {})
    for name, value in parameters.items():
        getattr(builder, name)(value)
    if "description" in definition:
        builder.description(definition["description"])
    missing = set(builder.missing_required()) - secrets.keys()
    if definition["mode"] == "managed" and missing:
        msg = f"Missing required connector fields: {sorted(missing)}."
        raise DataLensConfigurationError(msg)
    return builder


def source_spec(
    client: Any, source: dict[str, Any], connection: Any, alias: str
) -> tuple[Any, dict[str, Any]]:
    inventory = client.capabilities["dataset_sources"]
    matches = [value for value in inventory.values() if value["method"] == source["factory"]]
    if len(matches) != 1 or matches[0]["connection_type"] != connection.type:
        raise DataLensConfigurationError(
            "Source factory is unavailable or incompatible with the connection: "
            + source["factory"]
        )
    parameters = dict(source.get("parameters", {}))
    if source.get("sql_file"):
        if "subsql" in parameters:
            msg = "Use sql_file to author a subselect source."
            raise DataLensConfigurationError(msg)
        parameters["subsql"] = asset_path(source["sql_file"]).read_text(encoding="utf-8")
    factory = getattr(client.create.source(using=connection), matches[0]["method"])
    try:
        inspect.signature(factory).bind(alias=alias, **parameters)
    except TypeError as error:
        raise DataLensConfigurationError("Invalid source parameters: " + str(error)) from None
    return factory, parameters


def validate_factories(client: Any, registry: Any) -> None:
    """Compile local factory contracts before any cloud request or persistence."""
    for resource in registry.resources.values():
        definition = resource.definition
        if resource.kind == "connection":
            connection_builder(client, definition, EntryLocation.path("Offline/Preflight"))
        elif resource.kind == "dataset":
            types = set()
            connection_keys = set()
            for alias, source in definition.get("sources", {}).items():
                selected = registry.resources["connection:" + source["connection"]].definition
                connection = Connection(
                    id=selected.get("id") or "offline",
                    name=selected["name"],
                    type=selected["connector"],
                    installation=client.INSTALLATION,
                )
                factory, parameters = source_spec(client, source, connection, alias)
                factory(alias=alias, **parameters)
                types.add(connection.type)
                connection_keys.add(source["connection"])
            if definition.get("relations") and (len(types) != 1 or len(connection_keys) != 1):
                msg = "Dataset joins require sources from one supported connection."
                raise DataLensConfigurationError(msg)
        elif resource.kind == "chart":
            if definition["type"] not in client.capabilities["chart_factories"].get(
                definition["family"], ()
            ):
                msg = "Chart factory absent from the configured client inventory."
                raise DataLensConfigurationError(msg)
            if (
                definition.get("links")
                and definition["family"] == "wizard"
                and definition["type"] != "geolayer"
            ):
                msg = "wizard.multi_dataset.create"
                raise DataLensCapabilityError(
                    msg,
                    get_capabilities(
                        installation="yc" if client.INSTALLATION == "yacloud" else "enterprise"
                    ),
                )


def connection_credentials(
    definition: dict[str, Any], old: dict[str, Any], *, creating: bool
) -> dict[str, str]:
    rotating = creating or old.get("credentials_revision") != definition.get("credentials_revision")
    credentials = {}
    if rotating:
        for field, reference in definition.get("secrets", {}).items():
            value = os.environ.get(reference)
            if not value:
                raise DataLensConfigurationError(
                    "Missing credential environment reference: " + reference + "."
                )
            credentials[field] = value
    return credentials


def update_connection(
    connection: Any, client: Any, store: Any, resource: Any, credentials: dict[str, str]
) -> Any:
    definition, key = resource.definition, resource.key
    parameters = {
        field: value
        for field, value in definition.get("parameters", {}).items()
        if connection.raw.get(field) != value
    }
    update = connection.update
    for field, value in {**parameters, **credentials}.items():
        update.set(field, value)
    description = (
        "description" in definition and connection.description != definition["description"]
    )
    if description:
        update.description(definition["description"])
    if parameters or credentials or description:
        store.pending_metadata[key] = {
            "credentials_revision": definition.get("credentials_revision")
        }
        connection = store.persisted(key, update.execute(), client.get.connection)
    connection = store.rename(key, connection, definition["name"], client.get.connection)
    return connection


def existing_connection(client: Any, store: Any, resource: Any) -> Any:
    definition, key = resource.definition, resource.key
    if definition["mode"] == "existing" and definition.get("id"):
        connection = client.get.connection(by_id=definition["id"])
        store.verify_identity(connection, definition["id"])
        target = definition.get("target")
        if target:
            if getattr(connection, "workbook_id", None) != target.get("workbook_id") or (
                target.get("path") is not None and connection.dir_path != target["path"]
            ):
                raise DataLensUtilsError(
                    key + " existing reference is outside its configured location."
                )
        elif hasattr(store.folder, "collection_id") and not hasattr(store.folder, "key"):
            store.verify_location(connection, definition["name"], scope="connection")
        store.checkpoint(key, connection, scope="connection")
    else:
        connection = store.existing(
            key, definition["name"], client.get.connection, scope="connection"
        )
    return connection


def reconcile_connection(client: Any, store: Any, resource: Any) -> Any:
    definition = resource.definition
    key = resource.key
    builder = connection_builder(client, definition, store.entry_location)
    connection = existing_connection(client, store, resource)
    if connection is not None:
        if connection.type != definition["connector"]:
            raise DataLensUtilsError(key + " connector identity differs.")
        if definition["mode"] == "existing":
            if connection.name != definition["name"]:
                raise DataLensUtilsError(key + " existing connection name differs.")
            return connection
        store.check_write(key, connection)
    elif definition["mode"] == "existing":
        raise DataLensUtilsError(key + " existing reference was not found.")
    old = store.state["resources"].get(key, {})
    credentials = connection_credentials(definition, old, creating=connection is None)
    if connection is None:
        store.pending_metadata[key] = {
            "credentials_revision": definition.get("credentials_revision")
        }
        for field, value in credentials.items():
            getattr(builder, field)(value)
        connection = store.create(
            key, definition["name"], builder, client.get.connection, scope="connection"
        )
    else:
        connection = update_connection(connection, client, store, resource, credentials)
    # Credential values never become checkpoint input or fingerprint material.
    store.state["resources"][key]["credentials_revision"] = definition.get("credentials_revision")
    store.save_checkpoint()
    issues = [
        field
        for field, value in definition.get("parameters", {}).items()
        if connection.raw.get(field) != value
    ]
    if connection.type != definition["connector"] or issues:
        raise DataLensUtilsError(
            key + " persisted connector metadata differs: " + ", ".join(issues)
        )
    return connection
