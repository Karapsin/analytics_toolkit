"""Typed collection/workbook creation and bounded, exact-name adoption."""

from __future__ import annotations

from typing import Any

from datalens_sdk import ConflictError

from .deployment import BIProjectDeployment
from .errors import DataLensUtilsError
from .resources.folders import ensure_target_folder
from .session import current_session


def create_or_check_container(
    client: Any, store: Any, resource: Any, parent: Any, entity: Any
) -> Any:
    definition = resource.definition
    getter = getattr(client.get, resource.kind)
    if entity is None:
        kwargs = {"parent" if resource.kind == "collection" else "collection": parent}
        builder = getattr(client.create, resource.kind)(name=definition["name"], **kwargs)
        if "description" in definition:
            builder.description(definition["description"])
        try:
            entity = builder.build()
        except ConflictError as error:
            if parent is None:
                raise DataLensUtilsError(
                    resource.key
                    + (
                        " root-name conflict cannot be adopted: SDK 3.1/3.2 lack root "
                        "container discovery. Configure the retained ID."
                    )
                ) from error
            matches = [
                entry
                for entry in parent.list_entries(mode=resource.kind + "s")
                if entry.name == definition["name"]
            ]
            if len(matches) != 1:
                raise
            entity = getter(by_id=matches[0].id)
            store.verify_identity(entity, matches[0].id)
        store.checkpoint(resource.key, entity, scope=resource.kind, mutation=True)
        identifier = entity.id
        entity = getter(by_id=identifier)
        store.verify_identity(entity, identifier)
    else:
        store.check_write(resource.key, entity)
    return entity


def reconcile_container(client: Any, store: Any, resource: Any, containers: dict[str, Any]) -> Any:
    definition = resource.definition
    parent = (
        containers.get("collection:" + definition["parent"]) if definition.get("parent") else None
    )
    old = store.state["resources"].get(resource.key, {})
    identifier = definition.get("id") or old.get("id")
    getter = getattr(client.get, resource.kind)
    entity = getter(by_id=identifier) if identifier else None
    if identifier:
        store.verify_identity(entity, identifier)
    if entity is None and parent is not None:
        matches = [
            entry
            for entry in parent.list_entries(mode=resource.kind + "s")
            if entry.name == definition["name"]
        ]
        if len(matches) > 1:
            raise DataLensUtilsError(resource.key + " has ambiguous name adoption.")
        if matches:
            entity = getter(by_id=matches[0].id)
            store.verify_identity(entity, matches[0].id)
    entity = create_or_check_container(client, store, resource, parent, entity)
    expected_parent = parent.id if parent is not None else None
    if (
        getattr(entity, "parent_id" if resource.kind == "collection" else "collection_id")
        != expected_parent
    ):
        raise DataLensUtilsError(
            resource.key + " is outside its configured parent; migration refused."
        )
    update = entity.update
    changed = False
    if entity.name != definition["name"]:
        update.name(definition["name"])
        changed = True
    if "description" in definition and entity.description != definition["description"]:
        update.description(definition["description"])
        changed = True
    if changed:
        entity = store.persisted(resource.key, update.execute(), getter)
    if entity.name != definition["name"] or (
        "description" in definition and entity.description != definition["description"]
    ):
        raise DataLensUtilsError(resource.key + " persisted container metadata differs.")
    store.checkpoint(resource.key, entity, scope=resource.kind)
    return entity


def resolve_location(client: Any, containers: dict[str, Any]) -> Any:
    deployment = current_session().deployment
    if not isinstance(deployment, BIProjectDeployment):
        return ensure_target_folder(client=client, path=deployment.target_path)
    target = deployment.target
    if target.kind == "path":
        return ensure_target_folder(client=client, path=target.value)
    if target.reference == "key":
        return containers["workbook:" + target.value]
    workbook = client.get.workbook(by_id=target.value)
    if workbook.id != target.value:
        msg = "Returned workbook identity differs from the configured target."
        raise DataLensUtilsError(msg)
    return workbook
