"""Version-2 dependency reconciliation through public, typed SDK methods."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Sequence, cast

from datalens_sdk import (
    Connection,
    DataLensClientEnterprise,
    DataLensClientYC,
    Dataset,
    EntryLocation,
    NoAuthProvider,
)

from .auth.client import datalens_client
from .bi_connections import reconcile_connection, validate_factories
from .bi_containers import reconcile_container, resolve_location
from .bi_dashboard import load_ui, preflight_ui, reconcile_dashboard
from .bi_datasets import cache_source, reconcile_dataset
from .bi_html import html_content, reconcile_html
from .bi_preflight import operation_blockers
from .bi_pull import pull_resources
from .bi_verification import resource_issues, verify_resources
from .capabilities import get_capabilities, require_capability
from .dashboard_generation.charts.create import chart_adapter, chart_getter, create_charts
from .editing.state import fingerprint
from .errors import DataLensConfigurationError, DataLensUtilsError
from .import_manifest import existing_connection, is_reference
from .import_manifest import source as captured_source
from .planning import plan
from .recipe import load_registry
from .resources.bi_store import BIResourceStore, safe_metadata
from .session import current_session


def local_datasets(registry: Any) -> dict[str, Any]:
    """Compile typed field handles for local builder validation without requests."""
    result = {}
    for key, resource in registry.resources.items():
        if resource.kind != "dataset":
            continue
        if is_reference(resource):
            original = captured_source(resource.capture) if resource.capture else None
            result[key.split(":", 1)[1]] = Dataset(
                id=resource.definition["id"],
                name=resource.definition["name"],
                result_schema=tuple(original.get("fields", ())) if original else (),
                installation="yacloud"
                if current_session().runtime["installation"] == "yc"
                else "enterprise",
            )
            continue
        schema = []
        for name, value in resource.definition.get("fields", {}).items():
            aggregation = value.get("aggregation", "none")
            schema.append(
                {
                    "guid": value.get("guid", key + "/" + name),
                    "title": value.get("title", name),
                    "source": value["column"],
                    "avatar_id": value["source"],
                    "calc_mode": "direct",
                    "type": value.get("kind", "DIMENSION" if aggregation == "none" else "MEASURE"),
                    "cast": value.get("cast", "string"),
                    "data_type": value.get("cast", "string"),
                    "aggregation": aggregation,
                    "hidden": value.get("hidden", False),
                    "description": value.get("description", ""),
                    "ui_settings": value.get("ui_settings"),
                }
            )
        for name, value in resource.definition.get("calculations", {}).items():
            schema.append(
                {
                    "guid": value.get("guid", key + "/" + name),
                    "title": name,
                    "calc_mode": "formula",
                    "formula": value["formula"],
                    "cast": value.get("cast", "float"),
                    "data_type": value.get("cast", "float"),
                    "type": value.get("kind", "MEASURE"),
                    "aggregation": value.get("aggregation", "none"),
                }
            )
        for name, value in resource.definition.get("parameters", {}).items():
            schema.append(
                {
                    "guid": value.get("guid", key + "/" + name),
                    "title": name,
                    "calc_mode": "parameter",
                    "cast": value["type"],
                    "data_type": value["type"],
                    "type": "DIMENSION",
                    "aggregation": "none",
                    "default_value": value["default"],
                }
            )
        result[key.split(":", 1)[1]] = Dataset(
            id=resource.definition.get("id") or key,
            name=resource.definition["name"],
            result_schema=tuple(schema),
            installation="yacloud"
            if current_session().runtime["installation"] == "yc"
            else "enterprise",
        )
    return result


def validate_resource(client: Any, resource: Any, registry: Any, datasets: Any) -> None:
    if resource.kind == "html_page":
        html_content(resource.definition)
    elif resource.kind == "chart":
        adapter = chart_adapter(resource.definition)
        adapter.validate_definition(resource.definition)
        selected = registry.resources.get(
            "connection:" + resource.definition.get("connection", "default")
        )

        connection = (
            Connection(
                id=selected.definition.get("id") or selected.key,
                name=selected.definition["name"],
                type=selected.definition["connector"],
                installation=client.INSTALLATION,
            )
            if selected
            else None
        )
        context = SimpleNamespace(client=client, connection=connection)
        adapter.create_builder(
            context,
            datasets,
            resource.definition,
            EntryLocation.path("Offline/Validation"),
        ).to_spec()
    elif resource.kind == "dataset":
        dataset = datasets[resource.key.split(":", 1)[1]]
        handles = {
            key: dataset.fields.by_name(value.get("title", key))
            for section in ("fields", "calculations", "parameters")
            for key, value in resource.definition.get(section, {}).items()
        }
        for name, rule in resource.definition.get("rls", {}).items():
            if rule["field"] not in handles or rule.get("pattern_type", "value") not in {
                "value",
                "all",
                "userid",
            }:
                raise DataLensConfigurationError("Invalid RLS field or pattern: " + name)
            dataset.update.add_rls(
                field=handles[rule["field"]],
                **{key: value for key, value in rule.items() if key != "field"},
            ).to_spec()
        if "cache_invalidation" in resource.definition:
            dataset.update.update_cache_invalidation_source(
                source=cache_source(resource.definition, handles)
            ).to_spec()


def validate_registry(registry: Any) -> dict[str, Any]:
    """No client factory, authentication, network, or runtime filesystem writes."""
    installation = current_session().runtime["installation"]
    factory = DataLensClientYC if installation == "yc" else DataLensClientEnterprise
    kwargs = {} if installation == "yc" else {"base_url": "https://validation.invalid"}
    with factory(auth=NoAuthProvider(), **kwargs) as client:
        validate_factories(client, registry)
        datasets = local_datasets(registry)
        definitions = {
            key.split(":", 1)[1]: value.definition
            for key, value in registry.resources.items()
            if value.kind == "chart"
        }
        charts = {
            key: SimpleNamespace(id=value.get("id") or "chart:" + key)
            for key, value in definitions.items()
        }
        if not is_reference(registry.resources["dashboard:main"]):
            try:
                ui = load_ui(definitions)
                preflight_ui(
                    client,
                    ui,
                    charts,
                    datasets,
                    definitions,
                    registry.resources["dashboard:main"].definition,
                )
            except Exception as error:
                cast("Any", error).resource_key = "dashboard:main"
                raise
        for resource in registry.resources.values():
            if is_reference(resource):
                continue
            try:
                validate_resource(client, resource, registry, datasets)
            except Exception as error:
                cast("Any", error).resource_key = resource.key
                raise
    return {
        "schema_version": 2,
        "resource_count": len(registry.resources),
        **(
            {
                "captured_only": [
                    key for key, resource in registry.resources.items() if is_reference(resource)
                ]
            }
            if any(resource.capture for resource in registry.resources.values())
            else {}
        ),
    }


def getter(client: Any, resource: Any) -> Any:
    return (
        chart_getter(client, resource.definition)
        if resource.kind == "chart" and not is_reference(resource)
        else client.get.chart
        if resource.kind == "chart"
        else getattr(client.get, resource.kind)
    )


def read_entities(  # noqa: PLR0913 - Explicit read options and dependency snapshot.
    client: Any,
    store: Any,
    registry: Any,
    keys: Sequence[str],
    *,
    branch: str = "published",
    allow_missing: bool = False,
    resume_writes: set[str] | None = None,
) -> dict[str, Any]:
    result = {}
    ids = {key: store.state["resources"].get(store.internal_key(key), {}).get("id") for key in keys}
    revisions = [
        identifier
        for key, identifier in ids.items()
        if identifier
        and registry.resources[key].kind in {"dataset", "chart", "dashboard", "html_page"}
    ]
    inventory = (
        {entry.id: entry for entry in client.navigation.get_entries(ids=revisions, page_size=100)}
        if revisions
        else {}
    )
    for key in keys:
        resource = registry.resources[key]
        if ids[key]:
            if resource.kind in {"dataset", "chart", "dashboard", "html_page"}:
                entry = inventory.get(ids[key])
                if entry is None:
                    if allow_missing:
                        continue
                    raise DataLensUtilsError(
                        key + " retained resource is missing; recreation refused."
                    )
                scope = "artifact" if resource.kind == "html_page" else store.scope(resource.kind)
                if getattr(entry, "scope", scope) != scope:
                    raise DataLensUtilsError(key + " retained resource kind differs.")
                checkpoint = store.state["resources"].get(store.internal_key(key), {})
                selected_branch = (
                    "saved"
                    if resource.kind == "dataset"
                    and key in (resume_writes or ())
                    and checkpoint.get("pending_write")
                    else branch
                )
                entity = getter(client, resource)(by_id=ids[key], branch=selected_branch)
                if entity.id != ids[key]:
                    raise DataLensUtilsError(key + " returned identity differs.")
            else:
                entity = getter(client, resource)(by_id=ids[key])
                store.verify_identity(entity, ids[key])
            result[key] = entity
    return result


def validate_update_scope(
    client: Any, store: Any, registry: Any, writes: set[str], entities: dict[str, Any]
) -> None:
    dependencies = registry.read_closure(writes)[0]
    blockers = operation_blockers(client, store, registry, writes, entities)
    blocked = [value for value in blockers if value["status"] == "blocked"]
    if blocked:
        raise DataLensUtilsError("Capability preflight blocked: " + str(blocked))
    for key, entity in entities.items():
        resource = registry.resources[key]
        if existing_connection(resource) or (is_reference(resource) and key not in writes):
            issues = resource_issues(client, store, registry, key, entities)
            if issues:
                raise DataLensUtilsError(key + " reference differs: " + ", ".join(issues))
            continue
        if key in writes:
            store.check_write(key, entity)
        else:
            old = store.state["resources"].get(store.internal_key(key), {})
            if (
                not old.get("fingerprint")
                or old["fingerprint"] != fingerprint(registry.resources[key].files)
                or old.get("metadata") != safe_metadata(entity, registry.resources[key].kind)
            ):
                raise DataLensUtilsError(
                    "Unapplied or unverified dependency "
                    + key
                    + "; include it in the requested scope."
                )
    missing = set(dependencies) - writes - entities.keys()
    if missing:
        raise DataLensUtilsError(
            "Missing dependency " + ", ".join(sorted(missing)) + "; reconcile or select it first."
        )


def resolve_write_secrets(
    store: Any, registry: Any, writes: set[str], entities: dict[str, Any]
) -> None:
    # Resolve required secrets before the first persistence call.
    for key in writes:
        resource = registry.resources[key]
        if resource.kind == "connection" and resource.definition.get("mode") == "managed":
            old = store.state["resources"].get(key, {})
            if key not in entities or old.get("credentials_revision") != resource.definition.get(
                "credentials_revision"
            ):
                for reference in resource.definition.get("secrets", {}).values():
                    if not os.environ.get(reference):
                        raise DataLensConfigurationError(
                            "Missing credential environment reference: " + reference + "."
                        )


def reconcile_entry(client: Any, store: Any, resource: Any, inputs: dict[str, Any]) -> Any:
    connections, datasets, charts, definitions = (
        inputs[name] for name in ("connections", "datasets", "charts", "definitions")
    )
    _key, role = resource.key, resource.key.split(":", 1)[1]
    if resource.kind == "connection":
        connections[role] = reconcile_connection(client, store, resource)
    elif resource.kind == "dataset":
        datasets[role] = reconcile_dataset(client, store, resource, connections)
    elif resource.kind == "chart":
        context = SimpleNamespace(
            client=client,
            resources=store,
            folder=store.folder,
            connection=connections.get(resource.definition.get("connection", "default")),
        )
        charts[role] = create_charts(
            context=context, datasets=datasets, definitions={role: resource.definition}
        )[role]
    elif resource.kind == "dashboard":
        return reconcile_dashboard(client, store, resource, charts, datasets, definitions)
    else:
        return reconcile_html(client, store, resource)

    return inputs[resource.kind + "s"][role]


def apply_resources(
    client: Any, store: Any, registry: Any, writes: set[str], entities: dict[str, Any]
) -> None:
    containers = {
        key: value
        for key, value in entities.items()
        if registry.resources[key].kind in {"collection", "workbook"}
    }
    for key in registry.order:
        resource = registry.resources[key]
        if key in writes and resource.kind in {"collection", "workbook"}:
            entities[key] = containers[key] = reconcile_container(
                client, store, resource, containers
            )
            store.remember(key, entities[key])
    if any(
        not registry.resources[key].definition.get("location")
        for key in writes
        if registry.resources[key].kind not in {"collection", "workbook"}
    ):
        store.set_location(resolve_location(client, containers))
    connections = {
        key.split(":", 1)[1]: value
        for key, value in entities.items()
        if registry.resources[key].kind == "connection"
    }
    datasets = {
        key.split(":", 1)[1]: value
        for key, value in entities.items()
        if registry.resources[key].kind == "dataset"
    }
    charts = {
        key.split(":", 1)[1]: value
        for key, value in entities.items()
        if registry.resources[key].kind == "chart"
    }
    definitions = {
        key.split(":", 1)[1]: resource.definition
        for key, resource in registry.resources.items()
        if resource.kind == "chart"
    }
    inputs = {
        "connections": connections,
        "datasets": datasets,
        "charts": charts,
        "definitions": definitions,
    }
    for key in registry.order:
        resource = registry.resources[key]
        if key not in writes or resource.kind in {"collection", "workbook"}:
            continue
        store.use_resource_location(resource)
        entities[key] = reconcile_entry(client, store, resource, inputs)
        # A deliberate write changes the original capture. Verify its managed
        # result first; remember then stores the new complete authored baseline.
        verify_resources(client, store, registry, {key}, entities, captured=False)
        store.remember(key, entities[key])


def run(
    command: str, *, resource_keys: Sequence[str] | None = None, branch: str = "published"
) -> dict[str, Any]:
    registry = load_registry()
    coverage = validate_registry(registry)
    if command == "validate":
        return {"coverage": coverage}
    selected = registry.selection(resource_keys)
    dependencies, writes, _affected = registry.read_closure(selected)
    if command == "pull":
        report = get_capabilities(installation=current_session().runtime["installation"])
        for key in selected:
            if registry.resources[key].kind == "html_page":
                require_capability(report, "html_page.pull")
    if command == "status":
        result = plan(resource_keys=resource_keys)
        return {
            "resource_statuses": {item["resource"]: item["action"] for item in result["actions"]},
            **result,
        }
    store = BIResourceStore(registry, readonly=command in {"status", "verify", "pull"})
    inspected = dependencies
    with datalens_client() as client:
        validate_factories(client, registry)
        entities = read_entities(
            client,
            store,
            registry,
            inspected,
            branch=branch,
            resume_writes=writes if command not in {"verify", "pull"} else None,
        )
        if command == "pull":
            return pull_resources(client, store, registry, selected, entities, branch=branch)
        if command == "verify":
            missing = selected - entities.keys()
            if missing:
                raise DataLensUtilsError(
                    "Verification requires retained IDs: " + ", ".join(sorted(missing))
                )

            return verify_resources(client, store, registry, selected, entities)

        validate_update_scope(client, store, registry, writes, entities)
        resolve_write_secrets(store, registry, writes, entities)
        apply_resources(client, store, registry, writes, entities)
    dashboard = entities.get("dashboard:main")
    return {
        "resource_ids": {key: value.id for key, value in entities.items()},
        "dashboard_id": dashboard.id if dashboard else None,
        "orphans": sorted(
            set(store.state["resources"]) - {store.internal_key(key) for key in registry.resources}
        ),
    }
