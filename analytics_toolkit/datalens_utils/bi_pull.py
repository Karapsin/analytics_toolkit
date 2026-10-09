"""Three-way version-2 metadata import; extraction finishes before local writes."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

from .bi_datasets import field_handles, relation_snapshot, source_identities
from .bi_import_resources import dataset_cache
from .bi_pull_ui import pull_dashboard
from .editing.pull import pull_chart
from .editing.state import fingerprint, merge
from .errors import DataLensUtilsError
from .recipe_io import replace_files, stage_and_validate
from .resources.bi_store import safe_metadata
from .session import current_session


def pull_rls(entity: Any, result: dict[str, Any], fields: dict[str, Any]) -> None:
    if "rls" in result:
        for key, rule in list(result["rls"].items()):
            guid = fields[rule["field"]].guid
            matches = [
                value
                for value in entity.rls2.get(guid, ())
                if value.get("subject", {}).get("subject_id") == rule["subject_id"]
                and value.get("subject", {}).get("subject_type") == rule.get("subject_type", "user")
            ]
            if not matches:
                del result["rls"][key]
                continue
            if len(matches) != 1:
                raise DataLensUtilsError(
                    "RLS rule count changed; explicit stable-key reconciliation required: " + key
                )
            value = matches[0]
            rule.update(
                **value["subject"],
                allowed_value=value.get("allowed_value"),
                pattern_type=value.get("pattern_type", "value"),
            )


def pull_cache(entity: Any, result: dict[str, Any], fields: dict[str, Any]) -> None:
    if "cache_invalidation" in result:
        cache = entity.raw.get("dataset", {}).get("cache_invalidation_source")
        if not isinstance(cache, dict):
            msg = "Remote cache invalidation metadata cannot be faithfully imported."
            raise DataLensUtilsError(msg)
        previous = result["cache_invalidation"]
        extracted: dict[str, Any] = {}
        dataset_cache(entity, extracted, {field.guid: key for key, field in fields.items()}, {})
        result["cache_invalidation"] = extracted["cache_invalidation"]
        if cache["mode"] == "sql":
            if not previous.get("sql_file"):
                message = "Cache mode changed to SQL; specify a contained SQL asset before pulling."
                raise DataLensUtilsError(message)
            result["cache_invalidation"]["sql_file"] = previous["sql_file"]


def pull_default_filters(
    entity: Any, result: dict[str, Any], fields: dict[str, Any], old: dict[str, Any]
) -> None:
    if "default_filters" not in result:
        return
    filters = []
    for definition in result["default_filters"]:
        guid = fields[definition["field"]].guid
        retained = old.get("default_filters_owned", {}).get(guid)
        matches = [
            value
            for value in entity.default_filters
            if value.get("field_guid") == guid and (not retained or value.get("id") == retained)
        ]
        if not matches:
            continue
        if len(matches) != 1 or len(matches[0].get("default_filters", ())) != 1:
            message = "Managed default filter form cannot be faithfully imported."
            raise DataLensUtilsError(message)
        condition = matches[0]["default_filters"][0]
        filters.append(
            {
                "field": definition["field"],
                "operator": condition["operation"],
                "values": condition["values"],
            }
        )
    result["default_filters"] = filters


def extract_resource(  # noqa: PLR0913 - One baseline and its dependency snapshot.
    client: Any,
    resource: Any,
    entity: Any,
    old: dict[str, Any],
    base: dict[str, Any],
    datasets: dict[str, Any],
    charts: dict[str, Any],
) -> dict[str, Any]:
    incoming = copy.deepcopy(base)
    if resource.kind == "dataset":
        incoming["definition"] = extract_dataset(
            entity, SimpleNamespace(definition=base["definition"]), old
        )
        sources = source_identities(entity, base["definition"], old)
        for name, source in base["definition"]["sources"].items():
            if source.get("sql_file"):
                incoming[source["sql_file"]] = sources[name].parameters["subsql"]
        cache = base["definition"].get("cache_invalidation", {})
        if cache.get("sql_file") and incoming["definition"]["cache_invalidation"]["mode"] == "sql":
            incoming[cache["sql_file"]] = entity.raw["dataset"]["cache_invalidation_source"]["sql"]
    elif resource.kind == "chart":
        context = type("PullContext", (), {"client": client, "connection": None})()
        incoming["definition"], assets = pull_chart(entity, base["definition"], datasets, context)
        incoming.update(assets)
    elif resource.kind in {"connection", "collection", "workbook"}:
        incoming["definition"].update(
            name=entity.name, description=getattr(entity, "description", "")
        )
        if resource.kind == "connection" and resource.definition["mode"] == "managed":
            metadata = safe_metadata(entity, "connection")
            incoming["definition"]["parameters"] = {
                name: metadata["parameters"][name]
                for name in resource.definition.get("parameters", {})
                if name in metadata["parameters"]
            }
    else:
        incoming = pull_dashboard(entity, base, datasets, charts)
    return incoming


def propose_resource(
    resource: Any, key: str, merged: dict[str, Any], proposed: dict[str, Any]
) -> None:
    if resource.kind in {"connection", "collection", "workbook", "html_page", "dataset"}:
        filename = {
            "connection": "connections",
            "collection": "collections",
            "workbook": "workbooks",
            "html_page": "html_pages",
            "dataset": "datasets",
        }[resource.kind]
        relative = "configs/DL objects/" + filename + ".json"
        if relative not in proposed:
            proposed[relative] = json.loads(
                (current_session().paths.project_root / relative).read_text(encoding="utf-8")
            )
        proposed[relative][key.split(":", 1)[1]] = merged.pop("definition")
    elif resource.kind == "chart":
        paths = list((current_session().paths.project_root / "configs/DL objects").rglob("*.json"))
        path = next(
            path
            for path in paths
            if json.loads(path.read_text(encoding="utf-8")).get("key", path.stem)
            == key.split(":", 1)[1]
        )
        proposed[path.relative_to(current_session().paths.project_root).as_posix()] = merged.pop(
            "definition"
        )
    else:
        proposed["configs/DL objects/dashboard.json"] = merged.pop("definition")
    for relative, value in merged.items():
        if relative in proposed and proposed[relative] != value:
            raise DataLensUtilsError(
                "Selected resource imports disagree on a shared asset: " + relative
            )
        proposed[relative] = value


def extract_dataset(entity: Any, resource: Any, old: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(resource.definition)
    result.update(name=entity.name, description=entity.description)
    sources = source_identities(entity, result, old)
    for key, source in sources.items():
        value = result["sources"][key]
        value["id"] = source.id
        value["parameters"] = dict(source.parameters)
        if value.get("sql_file"):
            value["parameters"].pop("subsql", None)
    fields = field_handles(entity, result, sources)
    for key, value in result.get("fields", {}).items():
        field = fields[key]
        value.update(
            guid=field.guid,
            title=field.title,
            cast=field.cast,
            kind=field.type,
            aggregation=field.aggregation,
        )
        for name in ("description", "hidden"):
            if name in value or getattr(field, name):
                value[name] = getattr(field, name)
    for key, value in result.get("relations", {}).items():
        identifier = value.get("id") or old.get("relations", {}).get(key)
        actual = next(
            (relation for relation in entity.relations if relation.get("id") == identifier), None
        )
        if actual is None:
            raise DataLensUtilsError("Retained join topology cannot be imported: " + key)
        value.update(id=identifier, **relation_snapshot(actual))
    for name, definition in result.get("calculations", {}).items():
        field = entity.fields.by_name(name)
        definition.update(
            formula=field.formula, cast=field.cast, kind=field.type, aggregation=field.aggregation
        )
    for name, definition in result.get("parameters", {}).items():
        field = entity.fields.by_name(name)
        definition.update(type=field.cast, default=field.default_value)
    pull_rls(entity, result, fields)
    pull_cache(entity, result, fields)
    pull_default_filters(entity, result, fields, old)
    return result


def pull_resources(  # noqa: PLR0913 - Three-way baseline, dependency snapshots and selected branch.
    client: Any,
    store: Any,
    registry: Any,
    selected: set[str],
    entities: dict[str, Any],
    *,
    branch: str,
) -> dict[str, Any]:
    if selected - entities.keys():
        msg = "Pull requires retained resource IDs."
        raise DataLensUtilsError(msg)
    proposed: dict[str, Any] = {}
    baselines = {}
    datasets = {
        key.split(":", 1)[1]: entity
        for key, entity in entities.items()
        if key.startswith("dataset:")
    }
    charts = {
        key.split(":", 1)[1]: {
            **store.state["resources"]
            .get(key, {})
            .get("baseline", {})
            .get("definition", resource.definition),
            "id": entities[key].id if key in entities else resource.definition.get("id"),
        }
        for key, resource in registry.resources.items()
        if resource.kind == "chart"
    }
    for key in registry.order:
        if key not in selected:
            continue
        resource, entity = registry.resources[key], entities[key]
        old = store.state["resources"].get(store.internal_key(key), {})
        if not old.get("baseline"):
            raise DataLensUtilsError(key + " lacks a verified three-way baseline.")
        base = copy.deepcopy(old["baseline"])
        incoming = extract_resource(client, resource, entity, old, base, datasets, charts)
        merged = merge(base, resource.files, incoming, key)
        baselines[key] = incoming
        propose_resource(resource, key, merged, proposed)
    stage_and_validate(proposed)
    replace_files(proposed)
    store.readonly = False
    for key in selected:
        old = store.state["resources"][store.internal_key(key)]
        old.update(
            baseline=baselines[key],
            metadata=safe_metadata(entities[key], registry.resources[key].kind),
            branch=branch,
        )
        old["fingerprint"] = fingerprint(baselines[key])
    store.save_checkpoint()
    return {"pulled_resources": sorted(selected), "branch": branch}
