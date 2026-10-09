"""Recipe comparisons using persisted public SDK metadata, without data queries."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

from .bi_connections import source_spec
from .bi_dashboard import dashboard_issues
from .bi_datasets import (
    avatar_for,
    cache_matches,
    cache_source,
    desired_rls,
    field_handles,
    relation_conditions,
    relation_snapshot,
    source_identities,
)
from .bi_html import html_content
from .dashboard_generation.datasets.create import dataset_issues
from .deployment import BIProjectDeployment
from .editing.state import fingerprint
from .errors import DataLensUtilsError
from .resources.bi_store import entry_description, safe_metadata
from .session import current_session
from .validation.charts import check_chart


def dataset_field_issues(  # noqa: PLR0913 - Exact source and field identities with connection metadata.
    client: Any,
    entity: Any,
    definition: dict[str, Any],
    connections: dict[str, Any],
    sources: dict[str, Any],
    handles: dict[str, Any],
) -> list[str]:
    issues = []
    for key, source in sources.items():
        spec = definition["sources"][key]
        _, params = source_spec(client, spec, connections[spec["connection"]], key)
        expected_type = next(
            name
            for name, value in client.capabilities["dataset_sources"].items()
            if value["method"] == spec["factory"]
        )
        if (
            source.connection_id != connections[spec["connection"]].id
            or source.source_type != expected_type
            or any(source.parameters.get(name) != value for name, value in params.items())
        ):
            issues.append("sources." + key)
    for key, spec in definition.get("fields", {}).items():
        field = handles[key]
        expected = {
            "title": spec.get("title", key),
            "aggregation": spec.get("aggregation", "none"),
            "type": spec.get(
                "kind", "DIMENSION" if spec.get("aggregation", "none") == "none" else "MEASURE"
            ),
        }
        expected.update(
            {name: spec[name] for name in ("cast", "hidden", "description") if name in spec}
        )
        issues.extend(
            "fields." + key + "." + name
            for name, value in expected.items()
            if getattr(field, name) != value
        )
    issues.extend(
        dataset_issues(
            entity,
            {
                **{key: value for key, value in definition.items() if key != "fields"},
                "description": definition.get("description", entity.description),
            },
        )
    )
    return issues


def dataset_rls_issues(
    entity: Any, definition: dict[str, Any], old: dict[str, Any], handles: dict[str, Any]
) -> list[str]:
    issues = []
    if "rls" in definition:
        desired = desired_rls(definition, handles)
        for guid, rules in desired.items():
            identities = {
                (rule["subject"]["subject_type"], rule["subject"]["subject_id"]) for rule in rules
            }
            actual = [
                rule
                for rule in entity.rls2.get(guid, ())
                if (
                    rule.get("subject", {}).get("subject_type"),
                    rule.get("subject", {}).get("subject_id"),
                )
                in identities
            ]
            if sorted(actual, key=repr) != sorted(rules, key=repr):
                issues.append("rls." + guid)
        for guid, owned in old.get("rls_owned", {}).items():
            wanted = {
                (value["subject"]["subject_type"], value["subject"]["subject_id"])
                for value in desired.get(guid, ())
            }
            removed = {tuple(value) for value in owned} - wanted
            if any(
                (
                    value.get("subject", {}).get("subject_type"),
                    value.get("subject", {}).get("subject_id"),
                )
                in removed
                for value in entity.rls2.get(guid, ())
            ):
                issues.append("rls.removed." + guid)
    return issues


def dataset_issues_v2(
    client: Any,
    entity: Any,
    definition: dict[str, Any],
    old: dict[str, Any],
    connections: dict[str, Any],
) -> list[str]:
    definition = copy.deepcopy(definition)
    for key, field in definition.get("fields", {}).items():
        if not field.get("guid") and old.get("field_guids", {}).get(key):
            field["guid"] = old["field_guids"][key]
    sources = source_identities(entity, definition, old)
    handles = field_handles(entity, definition, sources)
    issues = dataset_field_issues(client, entity, definition, connections, sources, handles)
    for key, spec in definition.get("relations", {}).items():
        identifier = spec.get("id") or old.get("relations", {}).get(key)
        left = avatar_for(entity, sources[spec["left"]])
        right = avatar_for(entity, sources[spec["right"]])
        matches = [
            value
            for value in entity.relations
            if (
                value.get("id") == identifier
                if identifier
                else value.get("left_avatar_id") == left and value.get("right_avatar_id") == right
            )
        ]
        expected = {
            "type": spec["type"],
            "drop_duplicates": spec.get("drop_duplicates", False),
            "conditions": [
                {"left": value.left, "right": value.right, "operator": value.operator}
                for value in relation_conditions(spec)
            ],
        }
        if (
            len(matches) != 1
            or matches[0].get("left_avatar_id") != left
            or matches[0].get("right_avatar_id") != right
            or relation_snapshot(matches[0]) != expected
        ):
            issues.append("relations." + key)
    for index, spec in enumerate(definition.get("default_filters", ())):
        guid = handles[spec["field"]].guid
        matches = [value for value in entity.default_filters if value.get("field_guid") == guid]
        expected_filters = [
            {"column": guid, "operation": spec["operator"], "values": spec["values"]}
        ]
        if len(matches) != 1 or matches[0].get("default_filters") != expected_filters:
            issues.append("default_filters." + str(index))
    issues.extend(dataset_rls_issues(entity, definition, old, handles))
    for key, value in definition.get("settings", {}).items():
        if entity.raw.get("dataset", {}).get("settings", {}).get(key) != value:
            issues.append("settings." + key)
    if "cache_invalidation" in definition and not cache_matches(
        entity, cache_source(definition, handles)
    ):
        issues.append("cache_invalidation")
    return issues


def common_resource_issues(
    store: Any, resource: Any, entity: Any, old: dict[str, Any]
) -> list[str]:
    definition = resource.definition
    issues = []
    if old.get("id") and entity.id != old["id"]:
        issues.append("id")
    if definition.get("name") and entity.name != definition["name"]:
        issues.append("name")
    if "description" in definition and entry_description(entity) != definition["description"]:
        issues.append("description")
    revisioned = resource.kind in {"chart", "dataset", "dashboard", "html_page"}
    if revisioned and (not entity.published_id or entity.saved_id != entity.published_id):
        issues.append("revisions")
    if resource.kind not in {"connection", "collection", "workbook"} or (
        resource.kind == "connection" and definition["mode"] == "managed"
    ):
        deployment = current_session().deployment
        target = deployment.target if isinstance(deployment, BIProjectDeployment) else None
        if target and target.kind == "workbook":
            workbook = (
                target.value
                if target.reference == "id"
                else store.state["resources"].get("workbook:" + target.value, {}).get("id")
            )
            if entity.workbook_id != workbook:
                issues.append("location.workbook")
        else:
            path = target.value if target else getattr(deployment, "target_path", "")
            if entity.workbook_id or (entity.key or "").rpartition("/")[0] != path.rstrip("/"):
                issues.append("location.path")
    return issues


def connection_issues(entity: Any, definition: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if entity.type != definition["connector"]:
        issues.append("connector")
    if definition["mode"] == "managed":
        actual = safe_metadata(entity, "connection")["parameters"]
        issues.extend(
            "parameters." + name
            for name, value in definition.get("parameters", {}).items()
            if actual.get(name) != value
        )
    target = definition.get("target", {})
    if ("workbook_id" in target and entity.workbook_id != target["workbook_id"]) or (
        "path" in target and entity.dir_path != target["path"]
    ):
        issues.append("reference.location")
    return issues


def resource_issues(
    client: Any, store: Any, registry: Any, key: str, entities: dict[str, Any]
) -> list[str]:
    resource = registry.resources[key]
    entity, definition = entities[key], resource.definition
    old = store.state["resources"].get(store.internal_key(key), {})
    issues = common_resource_issues(store, resource, entity, old)
    if resource.kind == "connection":
        issues.extend(connection_issues(entity, definition))
    elif resource.kind in {"collection", "workbook"}:
        parent = (
            store.state["resources"].get("collection:" + definition.get("parent", ""), {}).get("id")
        )
        if safe_metadata(entity, resource.kind)["parent_id"] != parent:
            issues.append("parent")
    elif resource.kind == "dataset":
        connections = {
            name.split(":", 1)[1]: value
            for name, value in entities.items()
            if name.startswith("connection:")
        }
        issues.extend(dataset_issues_v2(client, entity, definition, old, connections))
    elif resource.kind == "chart":
        datasets = {
            name.split(":", 1)[1]: value
            for name, value in entities.items()
            if name.startswith("dataset:")
        }
        connection = entities.get("connection:" + definition.get("connection", "default"))
        context = SimpleNamespace(client=client, connection=connection)
        issues.extend(
            check_chart(entity, context=context, datasets=datasets, definition=definition)
        )
    elif resource.kind == "dashboard":
        datasets = {
            name.split(":", 1)[1]: value
            for name, value in entities.items()
            if name.startswith("dataset:")
        }
        charts = {
            name.split(":", 1)[1]: value
            for name, value in entities.items()
            if name.startswith("chart:")
        }
        definitions = {
            name.split(":", 1)[1]: value.definition
            for name, value in registry.resources.items()
            if value.kind == "chart"
        }
        issues.extend(dashboard_issues(entity, definition, charts, datasets, definitions))
    elif resource.kind == "html_page" and old.get("source_fingerprint") != fingerprint(
        html_content(definition)
    ):
        issues.append("content_file.authored_fingerprint")
    return issues


def verify_resources(
    client: Any, store: Any, registry: Any, selected: set[str], entities: dict[str, Any]
) -> dict[str, Any]:
    mismatches = [
        {"resource": key, "path": issue}
        for key in registry.order
        if key in selected
        for issue in resource_issues(client, store, registry, key, entities)
    ]
    if mismatches:
        error = DataLensUtilsError("Persisted recipe verification failed: " + str(mismatches))
        error.mismatches = mismatches
        raise error
    return {
        "verified_resources": sorted(selected),
        "mismatches": [],
        "verification": (
            "Managed persisted recipe metadata and revisions; no data queries "
            "or authored HTML download."
        ),
    }
