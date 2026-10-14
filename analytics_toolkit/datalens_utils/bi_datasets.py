"""Named sources, identity-preserving joins, RLS ownership, and cache settings."""

from __future__ import annotations

import copy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from . import bi_dataset_semantics as _semantics
from .bi_connections import source_spec
from .bi_dataset_checks import dataset_issues_v2
from .capabilities import get_capabilities
from .dashboard_generation.datasets.create import _configure_fields
from .dataset_identity import check_components, resolved_definition
from .errors import DataLensCapabilityError, DataLensUtilsError

# Preserve the existing helper exports while sharing their implementation.
avatar_for = _semantics.avatar_for
cache_matches = _semantics.cache_matches
cache_source = _semantics.cache_source
desired_rls = _semantics.desired_rls
direct_field_candidates = _semantics.direct_field_candidates
field_handles = _semantics.field_handles
relation_conditions = _semantics.relation_conditions
relation_snapshot = _semantics.relation_snapshot
source_identities = _semantics.source_identities


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
            message = "Ambiguous join adoption: " + key
            raise DataLensUtilsError(message)
        saved = matches[0] if matches else None
        if saved and (saved.get("left_avatar_id") != left or saved.get("right_avatar_id") != right):
            message = "Join topology changed: " + key
            raise DataLensUtilsError(message)
        if identifier and saved is None:
            message = "Retained join identity disappeared: " + key
            raise DataLensUtilsError(message)
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
        if removed and any(_rule_identity(rule) in removed for rule in existing):
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
            message = "Source connection/type identity changed: " + key
            raise DataLensUtilsError(message)
        # Validate the factory-selected source type without a server schema read.
        factory(alias=key, **parameters)
        expected_type = next(
            name
            for name, value in client.capabilities["dataset_sources"].items()
            if value["method"] == spec["factory"]
        )
        if source.source_type != expected_type:
            message = "Source factory identity changed: " + key
            raise DataLensUtilsError(message)
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
            for name in ("cast", "aggregation", "description", "hidden", "ui_settings")
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
                **{name: value for name, value in arguments.items() if name != "ui_settings"},
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
    # Schema inference happens before field actions. Create only the source draft,
    # then adopt its source-qualified fields rather than adding duplicates.
    store.pending_metadata[key] = {
        "recipe_field_guids": {
            name: field.get("guid") for name, field in definition.get("fields", {}).items()
        }
    }
    # The create getter defaults to saved; the successful ID is already durable.
    return store.create(key, definition["name"], builder, client.get.dataset, scope="dataset")


def verify_dataset_draft(  # noqa: PLR0913 - Compatibility arguments plus full managed verification context.
    dataset: Any,
    definition: dict[str, Any],
    sources: dict[str, Any],
    key: str,
    *,
    client: Any = None,
    checkpoint: dict[str, Any] | None = None,
    connections: dict[str, Any] | None = None,
) -> None:
    check_components(dataset, key, "draft verification")
    retained = (
        checkpoint
        if checkpoint is not None
        else {"sources": {name: {"id": source.id} for name, source in sources.items()}}
    )
    issues = dataset_issues_v2(client, dataset, definition, retained, connections or {})
    if issues:
        raise DataLensUtilsError(
            key + " persisted draft does not match its recipe: " + ", ".join(issues)
        )


def retain_removal_ownership(checkpoint: Any, desired: Any) -> dict[str, Any]:
    """Keep old and desired ownership durable until fetched metadata verifies removals."""
    retained = dict(desired)
    for name in ("relations", "default_filters_owned"):
        retained[name] = {**checkpoint.get(name, {}), **desired.get(name, {})}
    rules = copy.deepcopy(checkpoint.get("rls_owned", {}))
    for guid, identities in desired.get("rls_owned", {}).items():
        rules[guid] = [
            list(value)
            for value in dict.fromkeys(
                tuple(value) for value in (*rules.get(guid, ()), *identities)
            )
        ]
    retained["rls_owned"] = rules
    return retained


def remember_inferred_fields(dataset: Any, definition: Any, sources: Any, checkpoint: Any) -> None:
    """Claim each inferred field once; distinct-title clones keep separate GUIDs."""
    fields = definition.get("fields", {})
    owned = checkpoint.setdefault("field_guids", {})
    for key, spec in fields.items():
        matches = direct_field_candidates(dataset, definition, sources, spec)
        for field in matches:
            if field.guid == spec.get("guid"):
                owned.setdefault(key, field.guid)
    for key, spec in fields.items():
        if key in owned:
            continue
        matches = [
            field
            for field in direct_field_candidates(dataset, definition, sources, spec)
            if field.guid not in owned.values()
        ]
        if len(matches) > 1:
            message = (
                "Field "
                + key
                + " cannot resolve to exactly one inferred source-qualified identity."
            )
            raise DataLensUtilsError(message)
        if matches:
            field = matches[0]
            peers = [
                name
                for name, value in fields.items()
                if name not in owned
                and value["source"] == spec["source"]
                and value["column"] == spec["column"]
            ]
            owner = next(
                (name for name in peers if fields[name].get("title", name) == field.title), peers[0]
            )
            owned[owner] = field.guid
    # Persist deterministic creation GUIDs for declarations not supplied by inference.
    for key, spec in fields.items():
        owned.setdefault(key, spec.get("guid") or str(uuid5(NAMESPACE_URL, dataset.id + "/" + key)))


def reconcile_dataset(client: Any, store: Any, resource: Any, connections: dict[str, Any]) -> Any:
    definition, key = copy.deepcopy(resource.definition), resource.key
    dataset = store.existing(key, definition["name"], client.get.dataset, scope="dataset")
    checkpoint = store.state["resources"].get(key, {})
    definition = resolved_definition(definition, checkpoint)
    if dataset is None:
        dataset = create_dataset(client, store, resource, connections, definition)
    else:
        store.check_write(key, dataset)
    checkpoint = store.state["resources"].get(key, checkpoint)
    check_components(dataset, key, "reconciliation fetch")
    if "recipe_field_guids" in checkpoint:
        sources = source_identities(dataset, definition, checkpoint)
        remember_inferred_fields(dataset, definition, sources, checkpoint)
        store.save_checkpoint()
        definition = resolved_definition(definition, checkpoint)
    sources = source_identities(dataset, definition, checkpoint)
    # Field creation, dependent filters, and RLS/cache need separate fetched
    # drafts. Stop when the builder is empty; cap retries of a nonconverging API.
    for _ in range(4):
        sources = source_identities(dataset, definition, checkpoint)
        update, ownership = configure_dataset(
            dataset, definition, sources, connections, client, checkpoint
        )
        spec = update.to_spec()
        if spec.actions or spec.rls2_changes:
            store.pending_metadata[key] = retain_removal_ownership(checkpoint, ownership)
            dataset = store.persisted(
                key, update.mode("save").execute(), client.get.dataset, branch="saved"
            )
        checkpoint.update(retain_removal_ownership(checkpoint, ownership))
        if not spec.actions and not spec.rls2_changes:
            break
    sources = source_identities(dataset, definition, checkpoint)
    verify_dataset_draft(
        dataset,
        definition,
        sources,
        key,
        client=client,
        checkpoint=checkpoint,
        connections=connections,
    )
    # Derive new join/filter IDs from the final fetched draft, including the last pass.
    _, ownership = configure_dataset(dataset, definition, sources, connections, client, checkpoint)
    checkpoint.update(ownership)
    store.save_checkpoint()
    if dataset.saved_id != dataset.published_id:
        dataset = store.persisted(
            key,
            dataset.update.description(dataset.description).mode("publish").execute(),
            client.get.dataset,
        )
    return dataset
