"""Shared managed dataset comparisons for saved and published metadata."""

from __future__ import annotations

from typing import Any

from .bi_connections import source_spec
from .bi_dataset_semantics import (
    avatar_for,
    cache_matches,
    cache_source,
    desired_rls,
    field_handles,
    relation_conditions,
    relation_snapshot,
    source_identities,
)
from .dashboard_generation.datasets.create import dataset_issues
from .dataset_identity import check_components, resolved_definition


def dataset_field_issues(  # noqa: PLR0913 - Exact source and field identities with connection metadata.
    client: Any,
    entity: Any,
    definition: dict[str, Any],
    connections: dict[str, Any],
    sources: dict[str, Any],
    handles: dict[str, Any],
) -> list[str]:
    issues = []
    for key, source in sources.items() if client is not None else ():
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
            {
                name: spec[name]
                for name in ("cast", "hidden", "description", "ui_settings")
                if name in spec
            }
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


def dataset_removal_issues(
    entity: Any, definition: dict[str, Any], old: dict[str, Any], handles: dict[str, Any]
) -> list[str]:
    issues = []
    for key, identifier in old.get("relations", {}).items():
        if key not in definition.get("relations", {}) and any(
            value.get("id") == identifier for value in entity.relations
        ):
            issues.append("relations.removed." + key)
    if "default_filters" in definition:
        desired_guids = {handles[value["field"]].guid for value in definition["default_filters"]}
        for guid, identifier in old.get("default_filters_owned", {}).items():
            if guid not in desired_guids and any(
                value.get("id") == identifier for value in entity.default_filters
            ):
                issues.append("default_filters.removed." + guid)
    return issues


def dataset_issues_v2(
    client: Any,
    entity: Any,
    definition: dict[str, Any],
    old: dict[str, Any],
    connections: dict[str, Any],
) -> list[str]:
    check_components(entity, "dataset:" + str(entity.id), "verification")
    definition = resolved_definition(definition, old)
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
            {"column": guid, "operation": spec["operator"], "values": spec["values"]},
        ]
        if len(matches) != 1 or matches[0].get("default_filters") != expected_filters:
            issues.append("default_filters." + str(index))
    issues.extend(dataset_removal_issues(entity, definition, old, handles))
    issues.extend(dataset_rls_issues(entity, definition, old, handles))
    for key, value in definition.get("settings", {}).items():
        if entity.raw.get("dataset", {}).get("settings", {}).get(key) != value:
            issues.append("settings." + key)
    if "cache_invalidation" in definition and not cache_matches(
        entity, cache_source(definition, handles)
    ):
        issues.append("cache_invalidation")
    return issues
