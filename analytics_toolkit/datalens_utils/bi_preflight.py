"""Compile remote-sensitive edits without persistence or checkpoint writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .bi_datasets import configure_dataset, source_identities
from .capabilities import get_capabilities
from .dashboard_generation.charts.create import chart_adapter
from .errors import DataLensUtilsError
from .session import current_session


def validate_source_topology(entity: Any, sources: dict[str, Any]) -> None:
    if {source.id for source in sources.values()} != {source.id for source in entity.sources}:
        msg = "Source topology removal requires an explicit migration."
        raise DataLensUtilsError(msg)


def operation_blockers(
    client: Any, store: Any, registry: Any, keys: set[str], entities: dict[str, Any]
) -> list[dict[str, Any]]:
    """Use public update builders to reject unsupported topology before any write."""
    blockers = sql_access_issues(registry, entities, keys)
    datasets = {
        key.split(":", 1)[1]: value
        for key, value in entities.items()
        if registry.resources[key].kind == "dataset"
    }
    connections = {
        key.split(":", 1)[1]: value
        for key, value in entities.items()
        if registry.resources[key].kind == "connection"
    }
    report = get_capabilities(installation=current_session().runtime["installation"])
    for key in registry.order:
        if key not in keys:
            continue
        resource = registry.resources[key]
        entity = entities.get(key)
        try:
            if resource.kind == "chart" and entity is not None:
                adapter = chart_adapter(resource.definition)
                if resource.definition["family"] == "wizard":
                    context = SimpleNamespace(client=client)
                    adapter.validate_change(entity, context, datasets, resource.definition)
                else:
                    adapter.validate_change(entity, resource.definition)
            elif resource.kind == "dataset" and entity is not None:
                old = store.state["resources"].get(store.internal_key(key), {})
                sources = source_identities(entity, resource.definition, old)
                validate_source_topology(entity, sources)
                if all(
                    source["connection"] in connections
                    for source in resource.definition["sources"].values()
                ):
                    configure_dataset(
                        entity, resource.definition, sources, connections, client, old
                    )
            elif (
                resource.kind in {"collection", "workbook"}
                and entity is None
                and not resource.definition.get("parent")
            ):
                # Creation is supported; root name adoption is explicitly unavailable.
                blockers.append(
                    {
                        "resource": key,
                        "operation": resource.kind + ".adopt_root",
                        "status": "requirement",
                        "reason": report["operations"][resource.kind + ".adopt_root"]["reason"],
                    }
                )
        except DataLensUtilsError as error:
            blockers.append(
                {
                    "resource": key,
                    "operation": getattr(error, "operation", resource.kind + ".update"),
                    "status": "blocked",
                    "reason": str(error),
                }
            )
    return blockers


def sql_access_issues(
    registry: Any, entities: dict[str, Any], keys: set[str]
) -> list[dict[str, Any]]:
    result = []
    for key in keys:
        resource = registry.resources[key]
        requirements = []
        if resource.kind == "chart" and resource.definition["family"] == "ql":
            requirements.append((resource.definition.get("connection", "default"), {"dashsql"}))
        if resource.kind == "dataset":
            requirements.extend(
                (source["connection"], {"subselect", "template", "dashsql"})
                for source in resource.definition["sources"].values()
                if source.get("sql_file") or source["factory"].endswith("_subselect")
            )
        for reference, levels in requirements:
            entity = entities.get("connection:" + reference)
            connection_key = "connection:" + reference
            definition = registry.resources[connection_key].definition
            desired = definition.get("parameters", {}).get("raw_sql_level")
            level = (
                desired
                if connection_key in keys
                and definition["mode"] == "managed"
                and desired is not None
                else entity.raw.get("raw_sql_level", "off")
                if entity is not None
                else desired or "off"
            )
            if level is not None and level not in levels:
                result.append(
                    {
                        "resource": key,
                        "operation": "connection.sql_access",
                        "status": "blocked",
                        "reason": "Connection "
                        + reference
                        + " requires SQL level "
                        + ", ".join(sorted(levels))
                        + ".",
                    }
                )
    return result
