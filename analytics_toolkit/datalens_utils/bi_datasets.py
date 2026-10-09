"""Named sources, identity-preserving joins, RLS ownership, and cache settings."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from datalens_sdk import (
    CacheInvalidationField,
    CacheInvalidationFilter,
    CacheInvalidationFilterCondition,
    CacheInvalidationFormula,
    CacheInvalidationSource,
    JoinCondition,
)

from .bi_connections import source_spec
from .capabilities import get_capabilities
from .dashboard_generation.datasets.create import _configure_fields, dataset_issues
from .errors import DataLensCapabilityError, DataLensUtilsError
from .settings import asset_path


def source_identities(
    dataset: Any, definition: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    result = {}
    for key, source in definition["sources"].items():
        identifier = source.get("id") or checkpoint.get("sources", {}).get(key, {}).get("id")
        matches = (
            [value for value in dataset.sources if value.id == identifier]
            if identifier
            else [value for value in dataset.sources if value.title == key]
        )
        if len(matches) != 1:
            raise DataLensUtilsError(
                (
                    "Source topology changes require explicit avatar creation, "
                    "unavailable in this SDK: "
                )
                + key
            )
        result[key] = matches[0]
    if len({value.id for value in result.values()}) != len(result):
        msg = "Named sources cannot resolve to the same source ID."
        raise DataLensUtilsError(msg)
    return result


def avatar_for(dataset: Any, source: Any, retained: str | None = None) -> str:
    matches = [
        avatar
        for avatar in dataset.source_avatars
        if avatar.get("source_id") == source.id
        and (retained is None or avatar.get("id") == retained)
    ]
    if len(matches) != 1:
        raise DataLensUtilsError(
            "Source requires one explicitly identified avatar: " + source.title
        )
    return cast("str", matches[0]["id"])


def field_handles(
    dataset: Any, definition: dict[str, Any], sources: dict[str, Any], *, missing_ok: bool = False
) -> dict[str, Any]:
    handles = {}
    for key, field in definition.get("fields", {}).items():
        avatar = avatar_for(
            dataset,
            sources[field["source"]],
            definition["sources"][field["source"]].get("avatar_id"),
        )
        matches = [
            value
            for value in dataset.fields
            if value.calc_mode == "direct"
            and value.source == field["column"]
            and value.avatar_id == avatar
        ]
        if field.get("guid"):
            matches = [value for value in matches if value.guid == field["guid"]]
        if not matches and missing_ok:
            continue
        if len(matches) != 1:
            msg = f"Field {key} cannot resolve to exactly one source-qualified identity."
            raise DataLensUtilsError(msg)
        handles[key] = matches[0]
    for key in (*definition.get("calculations", {}), *definition.get("parameters", {})):
        field = dataset.find_field(key)
        if field is not None:
            handles[key] = field
    return handles


def relation_conditions(value: dict[str, Any]) -> list[JoinCondition]:
    return [JoinCondition(**condition) for condition in value["conditions"]]


def relation_snapshot(value: Any) -> dict[str, Any]:
    return {
        "type": value.get("join_type"),
        "drop_duplicates": value.get("required", False),
        "conditions": [
            {
                "left": rule["left"]["source"],
                "right": rule["right"]["source"],
                "operator": rule["operator"],
            }
            for rule in value.get("conditions", ())
        ],
    }


def validate_relation_columns(
    dataset: Any, relation: dict[str, Any], left: str, right: str, *, conditions_changed: bool
) -> None:
    if conditions_changed:
        for condition in relation["conditions"]:
            for side, avatar in (("left", left), ("right", right)):
                candidates = {
                    field.avatar_id
                    for field in dataset.fields
                    if field.calc_mode == "direct" and field.source == condition[side]
                }
                if candidates != {avatar}:
                    report = get_capabilities()
                    report["operations"]["dataset.join.update"]["status"] = "blocked"
                    msg = "dataset.join.update"
                    raise DataLensCapabilityError(msg, report)


def remove_owned_relations(
    update: Any, dataset: Any, owned: dict[str, Any], definition: dict[str, Any]
) -> None:
    for key in owned.keys() - definition.get("relations", {}).keys():
        if any(value.get("id") == owned[key] for value in dataset.relations):
            update.delete_relation(relation_id=owned[key])


def configure_relations(
    update: Any,
    dataset: Any,
    definition: dict[str, Any],
    sources: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, str]:
    owned = checkpoint.get("relations", {})
    result = {}
    for key, relation in definition.get("relations", {}).items():
        left = avatar_for(dataset, sources[relation["left"]])
        right = avatar_for(dataset, sources[relation["right"]])
        identifier = relation.get("id") or owned.get(key)
        matches = (
            [value for value in dataset.relations if value.get("id") == identifier]
            if identifier
            else [
                value
                for value in dataset.relations
                if value.get("left_avatar_id") == left and value.get("right_avatar_id") == right
            ]
        )
        if len(matches) > 1:
            raise DataLensUtilsError("Ambiguous join adoption: " + key)
        saved = matches[0] if matches else None
        if saved and (saved.get("left_avatar_id") != left or saved.get("right_avatar_id") != right):
            raise DataLensUtilsError("Join topology changed: " + key)
        if identifier and saved is None:
            raise DataLensUtilsError("Retained join identity disappeared: " + key)
        expected = {
            "type": relation["type"],
            "drop_duplicates": relation.get("drop_duplicates", False),
            "conditions": [
                {"left": value.left, "right": value.right, "operator": value.operator}
                for value in relation_conditions(relation)
            ],
        }
        actual = relation_snapshot(saved) if saved else None
        conditions_changed = actual is None or actual["conditions"] != expected["conditions"]
        validate_relation_columns(
            dataset, relation, left, right, conditions_changed=conditions_changed
        )
        if saved is None:
            update.add_relation(
                type=relation["type"],
                conditions=relation_conditions(relation),
                drop_duplicates=relation.get("drop_duplicates", False),
            )
        elif actual != expected:
            kwargs = {
                "relation_id": saved["id"],
                "type": relation["type"],
                "drop_duplicates": relation.get("drop_duplicates", False),
            }
            if conditions_changed:
                kwargs["conditions"] = relation_conditions(relation)
            update.update_relation(**kwargs)
        if saved:
            result[key] = saved["id"]
    remove_owned_relations(update, dataset, owned, definition)
    return result


def _rule_identity(rule: dict[str, Any]) -> tuple[str, str]:
    subject = rule.get("subject", {})
    if (
        set(rule) - {"subject", "allowed_value", "field_guid", "pattern_type"}
        or set(subject) - {"subject_id", "subject_type", "subject_name"}
        or subject.get("subject_type") not in {"user", "group", "all", "userid"}
        or rule.get("pattern_type", "value") not in {"value", "all", "userid"}
    ):
        msg = "Cannot reconstruct an unknown remote RLS rule form."
        raise DataLensUtilsError(msg)
    return subject["subject_type"], subject["subject_id"]


def desired_rls(
    definition: dict[str, Any], handles: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in definition.get("rls", {}).values():
        field = handles.get(rule["field"])
        if field is None:
            raise DataLensUtilsError("RLS references an unknown exact field: " + rule["field"])
        result[field.guid].append(
            {
                "field_guid": field.guid,
                "subject": {
                    "subject_id": rule["subject_id"],
                    "subject_type": rule.get("subject_type", "user"),
                    **(
                        {"subject_name": rule["subject_name"]}
                        if rule.get("subject_name") is not None
                        else {}
                    ),
                },
                "allowed_value": rule.get("allowed_value"),
                "pattern_type": rule.get("pattern_type", "value"),
            }
        )
    return dict(result)


def configure_rls(
    update: Any,
    dataset: Any,
    definition: dict[str, Any],
    handles: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    if "rls" not in definition:
        return dict(checkpoint.get("rls_owned", {}))
    desired = desired_rls(definition, handles)
    owned = checkpoint.get("rls_owned", {})
    new_owned = {
        guid: [list(identity) for identity in dict.fromkeys(_rule_identity(rule) for rule in rules)]
        for guid, rules in desired.items()
    }
    for guid in desired.keys() | owned.keys():
        existing = list(dataset.rls2.get(guid, []))
        removed = {tuple(value) for value in owned.get(guid, [])} - {
            tuple(value) for value in new_owned.get(guid, [])
        }
        if removed:
            remaining = [rule for rule in existing if _rule_identity(rule) not in removed]
            # The SDK deletes by field; preserve every unrelated subject in the same execution.
            update.delete_rls(field=guid)
            for rule in remaining:
                update.add_rls(
                    field=guid,
                    **rule["subject"],
                    allowed_value=rule.get("allowed_value"),
                    pattern_type=rule.get("pattern_type", "value"),
                )
            existing = remaining
        for identity in {tuple(value) for value in new_owned.get(guid, [])}:
            wanted = [rule for rule in desired[guid] if _rule_identity(rule) == identity]
            actual = [rule for rule in existing if _rule_identity(rule) == identity]
            if actual != wanted:
                for index, rule in enumerate(wanted):
                    method = update.update_rls if index == 0 else update.add_rls
                    method(
                        field=guid,
                        **rule["subject"],
                        allowed_value=rule.get("allowed_value"),
                        pattern_type=rule.get("pattern_type", "value"),
                    )
    return new_owned


def cache_source(definition: dict[str, Any], handles: dict[str, Any]) -> CacheInvalidationSource:
    cache = definition["cache_invalidation"]
    field = None
    if cache["mode"] == "formula":
        value = cache["field"]
        field = CacheInvalidationField(
            guid=value["guid"],
            type=value.get("type", "MEASURE"),
            calc_spec=CacheInvalidationFormula(**value["formula"]),
        )
    filters = []
    for rule in cache.get("filters", []):
        guid = handles[rule["field"]].guid
        filters.append(
            CacheInvalidationFilter(
                field_guid=guid,
                id=rule["key"],
                default_filters=(
                    CacheInvalidationFilterCondition(
                        guid, rule["operation"], tuple(rule.get("values", []))
                    ),
                ),
            )
        )
    return CacheInvalidationSource(
        mode=cache["mode"],
        sql=asset_path(cache["sql_file"]).read_text(encoding="utf-8")
        if cache["mode"] == "sql"
        else None,
        field=field,
        filters=tuple(filters),
    )


def cache_matches(dataset: Any, desired: CacheInvalidationSource) -> bool:
    exported = (
        dataset.update.update_cache_invalidation_source(source=desired)
        .to_spec()
        .actions[-1]["cache_invalidation_source"]
    )
    return bool(dataset.raw.get("dataset", {}).get("cache_invalidation_source") == exported)


def configure_sources(
    update: Any,
    definition: dict[str, Any],
    sources: dict[str, Any],
    connections: dict[str, Any],
    client: Any,
) -> None:
    for key, source in sources.items():
        spec = definition["sources"][key]
        connection = connections[spec["connection"]]
        factory, parameters = source_spec(client, spec, connection, key)
        if (
            source.connection_id != connection.id
            or source.source_type not in client.capabilities["dataset_sources"]
        ):
            raise DataLensUtilsError("Source connection/type identity changed: " + key)
        # Validate the factory-selected source type without a server schema read.
        factory(alias=key, **parameters)
        expected_type = next(
            name
            for name, value in client.capabilities["dataset_sources"].items()
            if value["method"] == spec["factory"]
        )
        if source.source_type != expected_type:
            raise DataLensUtilsError("Source factory identity changed: " + key)
        if any(source.parameters.get(name) != value for name, value in parameters.items()):
            update.update_source(
                source_id=source.id, parameters={**source.parameters, **parameters}
            )


def configure_direct_fields(
    update: Any,
    dataset: Any,
    definition: dict[str, Any],
    sources: dict[str, Any],
    handles: dict[str, Any],
) -> None:
    for key, expected in definition.get("fields", {}).items():
        field = handles.get(key)
        arguments = {
            name: expected[name]
            for name in ("cast", "aggregation", "description", "hidden")
            if name in expected
        }
        arguments["title"] = expected.get("title", key)
        if field is None:
            update.add_field(
                source=expected["column"],
                avatar_id=avatar_for(dataset, sources[expected["source"]]),
                kind=expected.get(
                    "kind",
                    "DIMENSION" if expected.get("aggregation", "none") == "none" else "MEASURE",
                ),
                guid=expected.get("guid") or str(uuid5(NAMESPACE_URL, dataset.id + "/" + key)),
                **arguments,
            )
        else:
            aggregation = expected.get("aggregation", "none")
            kind = expected.get("kind", "DIMENSION" if aggregation == "none" else "MEASURE")
            if field.aggregation != aggregation or field.type != kind:
                update.change_field_aggregation(field=field, to=aggregation)
            changes = {
                name: value for name, value in arguments.items() if getattr(field, name) != value
            }
            if changes:
                update.update_field(field=field, **changes)


def configure_default_filters(
    update: Any,
    dataset: Any,
    definition: dict[str, Any],
    handles: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, str]:
    if "default_filters" not in definition:
        return dict(checkpoint.get("default_filters_owned", {}))
    owned = {}
    desired = {
        handles[value["field"]].guid
        for value in definition["default_filters"]
        if value["field"] in handles
    }
    for expected in definition.get("default_filters", []):
        field = handles.get(expected["field"])
        if field is None:
            continue
        matches = [
            value for value in dataset.default_filters if value.get("field_guid") == field.guid
        ]
        if len(matches) > 1:
            msg = "Ambiguous default-filter identity."
            raise DataLensUtilsError(msg)
        if not matches:
            update.add_default_filter(
                field=field, operator=expected["operator"], values=expected["values"]
            )
        elif matches[0].get("default_filters") != [
            {"column": field.guid, "operation": expected["operator"], "values": expected["values"]}
        ]:
            update.update_default_filter(
                filter_id=matches[0]["id"], operator=expected["operator"], values=expected["values"]
            )
        if matches:
            owned[field.guid] = matches[0]["id"]
    for guid, identifier in checkpoint.get("default_filters_owned", {}).items():
        if guid not in desired and any(
            value.get("id") == identifier for value in dataset.default_filters
        ):
            update.delete_default_filter(filter_id=identifier)
    return owned


def configure_dataset(  # noqa: PLR0913 - Typed dataset context and preserved ownership.
    dataset: Any,
    definition: dict[str, Any],
    sources: dict[str, Any],
    connections: dict[str, Any],
    client: Any,
    checkpoint: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    update = dataset.update
    handles = field_handles(dataset, definition, sources, missing_ok=True)
    configure_sources(update, definition, sources, connections, client)
    configure_direct_fields(update, dataset, definition, sources, handles)
    # Reuse existing formula/parameter semantics without source-column lookup.
    _configure_fields(
        update,
        dataset,
        {
            **{key: value for key, value in definition.items() if key != "fields"},
            "description": definition.get("description", dataset.description),
        },
    )
    filters_owned = configure_default_filters(update, dataset, definition, handles, checkpoint)
    for name, value in definition.get("settings", {}).items():
        if dataset.raw.get("dataset", {}).get("settings", {}).get(name) != value:
            update.update_setting(name=name, value=value)
    relations = configure_relations(update, dataset, definition, sources, checkpoint)
    spec = update.to_spec()
    ownership = {
        "default_filters_owned": filters_owned,
        "sources": {
            key: {"id": source.id, "avatar_id": avatar_for(dataset, source)}
            for key, source in sources.items()
        },
        "relations": relations,
    }
    # New direct/formula fields must be saved as a draft and re-fetched before RLS resolves them.
    if not spec.actions:
        ownership["rls_owned"] = configure_rls(update, dataset, definition, handles, checkpoint)
        if "cache_invalidation" in definition:
            desired = cache_source(definition, handles)
            if not cache_matches(dataset, desired):
                update.update_cache_invalidation_source(source=desired)
    return update, ownership


def create_dataset(
    client: Any, store: Any, resource: Any, connections: dict[str, Any], definition: dict[str, Any]
) -> Any:
    key = resource.key
    sources = {}
    for alias, spec in definition["sources"].items():
        factory, parameters = source_spec(client, spec, connections[spec["connection"]], alias)
        sources[alias] = factory(alias=alias, **parameters).build(strict=True)
    builder = client.create.dataset(name=definition["name"], location=store.entry_location).sources(
        list(sources.values())
    )
    builder.description(definition.get("description", ""))
    for relation in definition.get("relations", {}).values():
        builder.add_relation(
            type=relation["type"],
            conditions=relation_conditions(relation),
            left_source=sources[relation["left"]],
            right_source=sources[relation["right"]],
            drop_duplicates=relation.get("drop_duplicates", False),
        )
    field_guids = {}
    for name, field in definition.get("fields", {}).items():
        guid = field.get("guid") or str(uuid5(NAMESPACE_URL, resource.key + "/" + name))
        field["guid"] = field_guids[name] = guid
        builder.add_field(
            title=field.get("title", name),
            source=field["column"],
            avatar_id=sources[field["source"]].id,
            guid=guid,
            kind=field.get(
                "kind", "DIMENSION" if field.get("aggregation", "none") == "none" else "MEASURE"
            ),
            **{
                attribute: field[attribute]
                for attribute in ("cast", "aggregation", "description", "hidden")
                if attribute in field
            },
        )
    for name, field in definition.get("calculations", {}).items():
        field.setdefault("guid", str(uuid5(NAMESPACE_URL, resource.key + "/calculation/" + name)))
        field_guids[name] = field["guid"]
        builder.add_calculation(name=name, **field)
    for name, parameter in definition.get("parameters", {}).items():
        parameter.setdefault("guid", str(uuid5(NAMESPACE_URL, resource.key + "/parameter/" + name)))
        field_guids[name] = parameter["guid"]
        builder.add_parameter(name=name, **parameter)
    for rule in definition.get("rls", {}).values():
        builder.add_rls(
            field=field_guids.get(rule["field"], rule["field"]),
            **{attribute: value for attribute, value in rule.items() if attribute != "field"},
        )
    for expected in definition.get("default_filters", []):
        builder.add_default_filter(
            field=field_guids.get(expected["field"], expected["field"]),
            operator=expected["operator"],
            values=expected["values"],
        )
    for name, value in definition.get("settings", {}).items():
        builder.update_setting(name=name, value=value)
    if "cache_invalidation" in definition:
        handles = {name: type("Field", (), {"guid": guid})() for name, guid in field_guids.items()}
        builder.update_cache_invalidation_source(source=cache_source(definition, handles))
    store.pending_metadata[key] = {"field_guids": field_guids}
    # The create getter defaults to saved; the successful ID is already durable.
    return store.create(key, definition["name"], builder, client.get.dataset, scope="dataset")


def verify_dataset_draft(
    dataset: Any, definition: dict[str, Any], sources: dict[str, Any], key: str
) -> None:
    handles = field_handles(dataset, definition, sources)
    # Verify the full draft before publishing; no cleared-RLS intermediate publication.
    issues = []
    for field_key, expected in definition.get("fields", {}).items():
        field = handles[field_key]
        if field.title != expected.get("title", field_key) or any(
            getattr(field, attribute) != expected[attribute]
            for attribute in ("cast", "aggregation", "hidden", "description")
            if attribute in expected
        ):
            issues.append(field_key)
    issues.extend(
        dataset_issues(
            dataset,
            {
                **{name: value for name, value in definition.items() if name != "fields"},
                "description": definition.get("description", dataset.description),
            },
        )
    )
    desired = desired_rls(definition, handles) if "rls" in definition else {}
    issues.extend(
        "RLS " + guid
        for guid, rules in desired.items()
        for rule in rules
        if rule not in dataset.rls2.get(guid, [])
    )
    if "cache_invalidation" in definition and not cache_matches(
        dataset, cache_source(definition, handles)
    ):
        issues.append("cache invalidation")
    if issues:
        raise DataLensUtilsError(
            key + " persisted draft does not match its recipe: " + ", ".join(issues)
        )


def reconcile_dataset(client: Any, store: Any, resource: Any, connections: dict[str, Any]) -> Any:
    definition, key = copy.deepcopy(resource.definition), resource.key
    dataset = store.existing(key, definition["name"], client.get.dataset, scope="dataset")
    checkpoint = store.state["resources"].get(key, {})
    for name, field in definition.get("fields", {}).items():
        if not field.get("guid") and checkpoint.get("field_guids", {}).get(name):
            field["guid"] = checkpoint["field_guids"][name]
    if dataset is None:
        dataset = create_dataset(client, store, resource, connections, definition)
    else:
        store.check_write(key, dataset)
    checkpoint = store.state["resources"].get(key, checkpoint)
    sources = source_identities(dataset, definition, checkpoint)
    for _ in range(2):
        sources = source_identities(dataset, definition, checkpoint)
        update, ownership = configure_dataset(
            dataset, definition, sources, connections, client, checkpoint
        )
        spec = update.to_spec()
        if spec.actions or spec.rls2_changes:
            store.pending_metadata[key] = ownership
            dataset = store.persisted(
                key, update.mode("save").execute(), client.get.dataset, branch="saved"
            )
        checkpoint.update(ownership)
    verify_dataset_draft(dataset, definition, sources, key)
    store.save_checkpoint()
    if dataset.saved_id != dataset.published_id:
        dataset = store.persisted(
            key,
            dataset.update.description(dataset.description).mode("publish").execute(),
            client.get.dataset,
        )
    return dataset
