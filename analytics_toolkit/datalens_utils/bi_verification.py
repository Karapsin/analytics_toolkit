"""Recipe comparisons using persisted public SDK metadata, without data queries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from . import bi_dataset_checks as _dataset_checks
from .bi_dashboard import dashboard_issues
from .bi_dataset_checks import dataset_issues_v2
from .bi_html import html_content
from .deployment import BIProjectDeployment, TargetLocation
from .editing.state import fingerprint
from .errors import DataLensUtilsError
from .import_manifest import is_reference, source
from .import_source import digest, source_document
from .resources.bi_store import entry_description, safe_metadata
from .session import current_session
from .validation.charts import check_chart

# Compatibility exports used by existing callers.
dataset_field_issues = _dataset_checks.dataset_field_issues
dataset_rls_issues = _dataset_checks.dataset_rls_issues


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
        target = (
            TargetLocation(**definition["location"])
            if definition.get("location")
            else deployment.target
            if isinstance(deployment, BIProjectDeployment)
            else None
        )
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


def captured_issues(resource: Any, entity: Any, issues: list[str]) -> list[str]:
    original = source(resource.capture) if resource.capture else None
    if original is None:
        return [*issues, "captured_source.unavailable"]
    actual = source_document(entity, resource.kind)
    # Revision pointers, including a newer saved draft, are not authored content.
    original = {name: value for name, value in original.items() if name != "metadata"}
    actual = {name: value for name, value in actual.items() if name != "metadata"}
    issues = [issue for issue in issues if issue != "revisions"]
    if digest(actual) != digest(original):
        issues.append("captured_source")
    return issues


def resource_issues(
    client: Any, store: Any, registry: Any, key: str, entities: dict[str, Any]
) -> list[str]:
    resource = registry.resources[key]
    entity, definition = entities[key], resource.definition
    old = store.state["resources"].get(store.internal_key(key), {})
    issues = common_resource_issues(store, resource, entity, old)
    if is_reference(resource):
        return captured_issues(resource, entity, issues)
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
        "captured_resources": {
            key: {
                "capture": registry.resources[key].capture["capture"],
                "editable": registry.resources[key].capture["editable"],
            }
            for key in sorted(selected)
            if registry.resources[key].capture
        },
        "verification": (
            "Managed persisted recipe metadata and revisions; no data queries "
            "or authored HTML download."
        ),
    }
