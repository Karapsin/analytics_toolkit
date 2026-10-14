"""Source-qualified dataset identities and persisted metadata representations."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, cast

from datalens_sdk import (
    CacheInvalidationField,
    CacheInvalidationFilter,
    CacheInvalidationFilterCondition,
    CacheInvalidationFormula,
    CacheInvalidationSource,
    JoinCondition,
)

from .errors import DataLensUtilsError
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


def direct_field_candidates(dataset: Any, definition: Any, sources: Any, field: Any) -> list[Any]:
    avatar = avatar_for(
        dataset, sources[field["source"]], definition["sources"][field["source"]].get("avatar_id")
    )
    return [
        value
        for value in dataset.fields
        if value.calc_mode == "direct"
        and value.source == field["column"]
        and value.avatar_id == avatar
    ]


def field_handles(
    dataset: Any, definition: dict[str, Any], sources: dict[str, Any], *, missing_ok: bool = False
) -> dict[str, Any]:
    handles = {}
    for key, field in definition.get("fields", {}).items():
        matches = direct_field_candidates(dataset, definition, sources, field)
        if field.get("guid"):
            if (
                matches
                and not any(value.guid == field["guid"] for value in matches)
                and any(value.title == field.get("title", key) for value in matches)
            ):
                message = (
                    f"Field {key} cannot resolve to exactly one retained GUID; identity differs."
                )
                raise DataLensUtilsError(message)
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


def desired_rls(
    definition: dict[str, Any], handles: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in definition.get("rls", {}).values():
        field = handles.get(rule["field"])
        if field is None:
            message = "RLS references an unknown exact field: " + rule["field"]
            raise DataLensUtilsError(message)
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
    actual = dataset.raw.get("dataset", {}).get("cache_invalidation_source")
    # An absent cache source is the backend's disabled-cache representation.
    if actual is None:
        actual = {"mode": "off", "filters": []}
    elif isinstance(actual, dict):
        actual = {"filters": [], **actual}
    return bool(actual == exported)
