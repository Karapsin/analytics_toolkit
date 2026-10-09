"""Discover ordinary BI resources using public SDK read models."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, cast

from .bi_datasets import relation_snapshot, source_identities
from .dashboard_generation.charts import editor, ql, wizard
from .editing.pull import items, pull_chart
from .errors import DataLensUtilsError


def semantic_key(kind: str, identifier: str) -> str:

    return kind + "_" + sha256(identifier.encode()).hexdigest()[:16]


def connection_definition(entity: Any) -> dict[str, Any]:
    return {
        "mode": "existing",
        "id": entity.id,
        "name": entity.name,
        "connector": entity.type,
        "target": {"workbook_id": entity.workbook_id, "path": entity.dir_path},
    }


def dataset_fields(
    entity: Any, definition: dict[str, Any], avatars: dict[str, str]
) -> dict[str, str]:
    fields = {}
    for field in entity.fields:
        if field.calc_mode == "direct":
            name = semantic_key("field", field.guid)
            definition["fields"][name] = {
                "guid": field.guid,
                "source": avatars[field.avatar_id],
                "column": field.source,
                "title": field.title,
                "cast": field.cast,
                "kind": field.type,
                "aggregation": field.aggregation,
                "hidden": field.hidden,
                "description": field.description or "",
            }
        elif field.calc_mode == "formula":
            name = field.title
            if name in definition["calculations"]:
                raise DataLensUtilsError("Duplicate calculated field title: " + name)
            definition["calculations"][name] = {
                "guid": field.guid,
                "formula": field.formula,
                "cast": field.cast,
                "kind": field.type,
                "aggregation": field.aggregation,
            }
        elif field.calc_mode == "parameter":
            name = field.title
            definition["parameters"][name] = {
                "guid": field.guid,
                "type": field.cast,
                "default": field.default_value,
            }
        else:
            raise DataLensUtilsError("Unknown dataset field calculation mode: " + field.calc_mode)
        fields[field.guid] = name
    return fields


def dataset_cache(
    entity: Any, definition: dict[str, Any], fields: dict[str, str], assets: dict[str, str]
) -> None:
    key = semantic_key("dataset", entity.id)
    cache = entity.raw.get("dataset", {}).get("cache_invalidation_source")
    if cache:
        value = {"mode": cache["mode"]}
        if cache["mode"] == "sql":
            value["sql_file"] = "assets/sql/imported/" + key + "_cache.sql"
            assets[value["sql_file"]] = cache["sql"]
        elif cache["mode"] == "formula":
            field = cache["field"]
            value["field"] = {
                "guid": field["guid"],
                "type": field["type"],
                "formula": field["calc_spec"],
            }
        value["filters"] = []
        for rule in cache.get("filters", ()):
            conditions = rule.get("default_filters", ())
            if len(conditions) != 1:
                msg = "Cache filter form cannot be imported faithfully."
                raise DataLensUtilsError(msg)
            condition = conditions[0]
            value["filters"].append(
                {
                    "key": rule["id"],
                    "field": fields[rule["field_guid"]],
                    "operation": condition["operation"],
                    "values": condition["values"],
                }
            )
        definition["cache_invalidation"] = value


def editor_definition(
    entity: Any, definition: dict[str, Any], datasets: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    key = definition["key"]
    assets: dict[str, str] = {}
    definition["type"] = next(
        (name for name, wire in editor.WIRE_TYPES.items() if wire == entity.wire_type), None
    )
    if not definition["type"]:
        raise DataLensUtilsError("Unsupported Editor renderer: " + str(entity.wire_type))
    definition["scripts"] = {}
    for name, source in entity.data.items():
        if name not in editor.TABS[definition["type"]]:
            raise DataLensUtilsError("Unsupported authored Editor tab: " + name)
        if not isinstance(source, str):
            raise DataLensUtilsError("Editor source is not authored text: " + name)
        relative = "assets/js/imported/" + key + "/" + name + (".json" if name == "meta" else ".js")
        definition["scripts"][name] = relative
        assets[relative] = source
    meta = json.loads(entity.data.get("meta", "{}"))
    definition["links"] = {}
    for alias, identifier in meta.get("links", {}).items():
        if identifier not in {value.id for value in datasets.values()}:
            raise DataLensUtilsError("Unresolved Editor dataset dependency: " + identifier)
        definition["links"][alias] = next(
            name for name, value in datasets.items() if value.id == identifier
        )
    return definition, assets


def ql_definition(
    entity: Any, definition: dict[str, Any], connections: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    key = definition["key"]
    assets: dict[str, str] = {}
    definition["type"] = next(
        (name for name, value in ql.VISUALIZATIONS.items() if value == entity.visualization_id),
        None,
    )
    if not definition["type"]:
        msg = "Unsupported QL visualization."
        raise DataLensUtilsError(msg)
    definition["connection"] = connections[(entity.connection or {})["entryId"]]
    definition["query_file"] = "assets/sql/imported/" + key + ".sql"
    assets[definition["query_file"]] = entity.query_value
    definition["params"] = [
        {"name": value["name"], "type": value["type"], "default": value.get("defaultValue")}
        for value in entity.params
    ]
    definition["columns"] = {}
    definition["fields"] = {}
    for slot in ql.PLACEMENTS[definition["type"]] + ql.DECORATIONS[definition["type"]]:
        columns = ql.actual_columns(entity, definition["type"], slot)
        if columns is None:
            raise DataLensUtilsError("Unsupported QL placement: " + slot)
        definition["fields"][slot] = [name for name, _ in columns]
        definition["columns"].update(columns)
    if entity.description:
        definition["description"] = entity.description
    return definition, assets


def dataset_default_filters(
    entity: Any, definition: dict[str, Any], fields: dict[str, str]
) -> None:
    for rule in entity.default_filters:
        conditions = rule.get("default_filters", ())
        if len(conditions) != 1:
            msg = "Default filter form cannot be imported faithfully."
            raise DataLensUtilsError(msg)
        condition = conditions[0]
        definition["default_filters"].append(
            {
                "field": fields[rule["field_guid"]],
                "operator": condition["operation"],
                "values": condition["values"],
            }
        )


def dataset_definition(
    client: Any, entity: Any, connections: dict[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    key = semantic_key("dataset", entity.id)
    definition: dict[str, Any] = {
        "id": entity.id,
        "name": entity.name,
        "description": entity.description or "",
        "sources": {},
        "fields": {},
        "calculations": {},
        "parameters": {},
        "relations": {},
        "rls": {},
    }
    assets = {}
    avatars = {}
    for source in entity.sources:
        name = semantic_key("source", source.id)
        inventory = client.capabilities["dataset_sources"].get(source.source_type)
        if inventory is None:
            raise DataLensUtilsError("Source factory unavailable: " + source.source_type)
        matches = [
            avatar for avatar in entity.source_avatars if avatar.get("source_id") == source.id
        ]
        if len(matches) != 1:
            raise DataLensUtilsError("Import needs one known source avatar: " + source.id)
        avatars[matches[0]["id"]] = name
        value = {
            "id": source.id,
            "avatar_id": matches[0]["id"],
            "connection": connections[source.connection_id],
            "factory": inventory["method"],
            "parameters": dict(source.parameters),
        }
        if "subsql" in value["parameters"]:
            value["sql_file"] = "assets/sql/imported/" + key + "_" + name + ".sql"
            assets[value["sql_file"]] = value["parameters"].pop("subsql")
        definition["sources"][name] = value
    fields = dataset_fields(entity, definition, avatars)
    for relation in entity.relations:
        name = semantic_key("relation", relation["id"])
        definition["relations"][name] = {
            "id": relation["id"],
            "left": avatars[relation["left_avatar_id"]],
            "right": avatars[relation["right_avatar_id"]],
            **relation_snapshot(relation),
        }
    for guid, rules in entity.rls2.items():
        for index, rule in enumerate(rules):
            name = semantic_key("rls", guid + "/" + str(index))
            definition["rls"][name] = {
                "field": fields[guid],
                **rule["subject"],
                "allowed_value": rule.get("allowed_value"),
                "pattern_type": rule.get("pattern_type", "value"),
            }
    definition["default_filters"] = []
    dataset_default_filters(entity, definition, fields)
    settings = entity.raw.get("dataset", {}).get("settings", {})
    if settings:
        definition["settings"] = dict(settings)
    dataset_cache(entity, definition, fields, assets)
    return definition, assets


def retain_dataset_keys(
    entity: Any, incoming: dict[str, Any], local: dict[str, Any], checkpoint: dict[str, Any]
) -> None:
    """Reuse authored keys when importing resources already present in the recipe."""
    sources = source_identities(entity, local, checkpoint)
    source_keys = {semantic_key("source", source.id): key for key, source in sources.items()}
    incoming["sources"] = {
        source_keys.get(key, key): value for key, value in incoming["sources"].items()
    }
    retained = {
        field.get("guid") or checkpoint.get("field_guids", {}).get(key): key
        for key, field in local.get("fields", {}).items()
    }
    field_keys = {}
    for key, field in incoming["fields"].items():
        field["source"] = source_keys.get(field["source"], field["source"])
        field_keys[key] = retained.get(field["guid"], key)
    incoming["fields"] = {field_keys[key]: value for key, value in incoming["fields"].items()}
    for relation in incoming["relations"].values():
        for side in ("left", "right"):
            relation[side] = source_keys.get(relation[side], relation[side])
    for rule in (*incoming["rls"].values(), *incoming["default_filters"]):
        rule["field"] = field_keys.get(rule["field"], rule["field"])
    for rule in incoming.get("cache_invalidation", {}).get("filters", ()):
        rule["field"] = field_keys.get(rule["field"], rule["field"])


def chart_definition(
    entity: Any, datasets: dict[str, Any], connections: dict[str, str], tab: str
) -> tuple[dict[str, Any], dict[str, str]]:
    key = semantic_key("chart", entity.id)
    definition: dict[str, Any] = {
        "key": key,
        "id": entity.id,
        "name": entity.name,
        "title": entity.name,
        "tab": tab,
        "family": entity.category,
    }
    if entity.category == "editor":
        return editor_definition(entity, definition, datasets)
    if entity.category == "ql":
        return ql_definition(entity, definition, connections)
    definition["type"] = next(
        (name for name, wire in wizard.VISUALIZATIONS.items() if wire == entity.visualization_id),
        None,
    )
    if not definition["type"] or len(entity.dataset_ids) != 1:
        msg = "Wizard import requires a supported visualization and expressible dataset bindings."
        raise DataLensUtilsError(msg)
    definition["dataset"] = next(
        name for name, value in datasets.items() if value.id == entity.dataset_ids[0]
    )
    visualization = entity.data.get("visualization", {})
    aliases = (
        {"x": "dimensions", "y": "measures"}
        if definition["type"] in {"pie", "donut", "treemap", "funnel"}
        else {"y": "measures"}
        if definition["type"] == "indicator"
        else {}
    )
    definition["fields"] = {
        slot: [] for slot in wizard.SLOTS if items(visualization, aliases.get(slot, slot))
    }
    return cast(
        "tuple[dict[str, Any], dict[str, str]]", pull_chart(entity, definition, datasets, None)
    )
