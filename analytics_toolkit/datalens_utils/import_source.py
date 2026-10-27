"""Lossless authored source and revision-keyed, public SDK read-model caches."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import fields
from hashlib import sha256
from pathlib import Path
from typing import Any

from .errors import DataLensConfigurationError, DataLensUtilsError

CAPTURE_VERSION = 3


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def location(entity: Any) -> dict[str, str] | None:
    workbook = getattr(entity, "workbook_id", None)
    if workbook:
        return {"kind": "workbook", "value": workbook, "reference": "id"}
    path = getattr(entity, "dir_path", None)
    if path is None:
        key = getattr(entity, "key", None)
        path = key.rpartition("/")[0] if key else None
    return {"kind": "path", "value": path or "/", "reference": "id"} if path is not None else None


def metadata(entity: Any) -> dict[str, Any]:
    return {
        name: getattr(entity, name, None)
        for name in ("id", "name", "key", "workbook_id", "rev_id", "saved_id", "published_id")
    }


def model_record(entity: Any) -> dict[str, Any]:
    """Cache explicit public constructor fields, never clients or callable objects."""
    result = {}
    for item in fields(entity):
        if item.name.startswith("_") or item.name in {"location", "sources"}:
            continue
        result[item.name] = copy.deepcopy(getattr(entity, item.name))
    result["location"] = location(entity)
    if hasattr(entity, "sources"):
        result["sources"] = [
            {item.name: copy.deepcopy(getattr(source, item.name)) for item in fields(source)}
            for source in entity.sources
        ]
    # Connection credentials and unrecognized transport fields are not needed for references.
    if type(entity).__name__ == "Connection":
        from .resources.bi_store import safe_metadata  # noqa: PLC0415 - Optional SDK boundary.

        result["raw"] = {**safe_metadata(entity, "connection")["parameters"], **metadata(entity)}
        result["response_snapshot"] = {}
    return {"type": type(entity).__name__, "values": result}


def restore_model(record: dict[str, Any]) -> Any:
    import datalens_sdk  # noqa: PLC0415 - Optional SDK boundary.

    allowed = {"Dataset", "WizardChart", "QLChart", "EditorChart", "Dashboard", "Connection"}
    if record.get("type") not in allowed:
        msg = "Unknown cached public read model."
        raise DataLensConfigurationError(msg)
    values = copy.deepcopy(record["values"])
    target = values.pop("location", None)
    if target:
        values["location"] = (
            datalens_sdk.EntryLocation.path(target["value"])
            if target["kind"] == "path"
            else datalens_sdk.EntryLocation.workbook(target["value"])
        )
    if "sources" in values:
        values["sources"] = tuple(datalens_sdk.Source(**item) for item in values["sources"])
    return getattr(datalens_sdk, record["type"])(**values)


def source_document(entity: Any, kind: str) -> dict[str, Any]:
    """Keep authored content separate from private transport snapshots."""
    document: dict[str, Any] = {
        "metadata": metadata(entity),
        "location": location(entity),
        "kind": kind,
    }
    snapshot = entity.response_snapshot
    if kind == "connection":
        from .resources.bi_store import safe_metadata  # noqa: PLC0415 - Optional SDK boundary.

        document["reference"] = safe_metadata(entity, kind)
    elif kind == "dataset":
        if not isinstance(snapshot.get("dataset"), dict):
            msg = "Public dataset getter did not expose complete authored source."
            raise DataLensUtilsError(msg)
        document.update(
            definition=copy.deepcopy(snapshot["dataset"]),
            sources=[
                {item.name: copy.deepcopy(getattr(source, item.name)) for item in fields(source)}
                for source in entity.sources
            ],
            fields=[dict(field.raw) for field in entity.fields],
        )
    else:
        entry = snapshot.get("entry", {})
        if not isinstance(entry.get("data"), dict) or entry["data"] != entity.data:
            msg = "Public getter/export authored content differs or is unavailable."
            raise DataLensUtilsError(msg)
        document.update(
            data=copy.deepcopy(entity.data),
            annotation=copy.deepcopy(entry.get("annotation")),
            meta=copy.deepcopy(entry.get("meta")),
        )
        if kind == "chart":
            document.update(category=entity.category, wire_type=entity.wire_type)
    return document


def source_assets(document: dict[str, Any]) -> dict[str, str]:
    identifier = document["metadata"]["id"]
    prefix = sha256(identifier.encode()).hexdigest()[:16]
    assets = {}
    if document["kind"] == "dataset":
        for source in document["sources"]:
            query = source["parameters"].get("subsql")
            if isinstance(query, str):
                suffix = sha256(source["id"].encode()).hexdigest()[:16]
                assets[f"assets/sql/source/{prefix}/{suffix}.sql"] = query
        cache_query = document.get("definition", {}).get("cache_invalidation_source") or {}
        if isinstance(cache_query.get("sql"), str):
            assets[f"assets/sql/source/{prefix}/cache.sql"] = cache_query["sql"]
    elif document.get("category") == "editor":
        for name, value in document["data"].items():
            if isinstance(value, str):
                # Authored tab names remain in the JSON; filenames must be contained/portable.
                suffix = (
                    name if name.replace("_", "").isalnum() else sha256(name.encode()).hexdigest()
                )
                extension = ".md" if name.startswith("documentation") else ".js"
                assets[f"assets/js/source/{prefix}/{suffix}{extension}"] = value
    elif document.get("category") == "ql":
        value = document["data"].get("queryValue", document["data"].get("query"))
        if isinstance(value, str):
            assets[f"assets/sql/source/{prefix}.sql"] = value
    return assets


def binding_occurrences(
    value: Any, path: str, owners: dict[str, set[str]], local_guids: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    occurrences: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    if isinstance(value, dict):
        guid = value.get("guid", value.get("fieldGuid"))
        identity = value.get("datasetId", value.get("dataset_id"))
        if guid and guid in owners:
            candidates = owners[guid]
            if identity:
                candidates = candidates & {identity}
            if len(candidates) == 1:
                resolved = next(iter(candidates))
                occurrences.append({"path": path, "dataset_id": resolved, "guid": guid})
            else:
                ambiguous.append({"path": path, "guid": guid, "dataset_id": identity})
        elif guid and identity and guid not in local_guids:
            ambiguous.append({"path": path, "guid": guid, "dataset_id": identity})
        for name, item in value.items():
            found, unresolved = binding_occurrences(item, path + "/" + name, owners, local_guids)
            occurrences.extend(found)
            ambiguous.extend(unresolved)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found, unresolved = binding_occurrences(
                item, path + "/" + str(index), owners, local_guids
            )
            occurrences.extend(found)
            ambiguous.extend(unresolved)

    return occurrences, ambiguous


def explicit_dataset_ids(value: Any) -> set[str]:
    """Follow authored field/filter identities, including undeclared references."""
    if isinstance(value, dict):
        direct = {
            item
            for name, item in value.items()
            if name in {"datasetId", "dataset_id"} and isinstance(item, str) and item
        }
        for item in value.values():
            direct.update(explicit_dataset_ids(item))
        return direct
    if isinstance(value, (list, tuple)):
        return set().union(*(explicit_dataset_ids(item) for item in value))
    return set()


def wizard_bindings(entity: Any, datasets: dict[str, Any]) -> dict[str, Any]:
    """Resolve authored occurrences by dataset identity and GUID, never list position."""
    declared = list(entity.dataset_ids)
    attached_or_referenced = set(declared) | explicit_dataset_ids(entity.data)
    owners: dict[str, set[str]] = {}
    for dataset in datasets.values():
        if dataset.id in attached_or_referenced:
            for field in dataset.fields:
                owners.setdefault(field.guid, set()).add(dataset.id)
    ambiguous: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    sources = entity.data.get("sources", {})
    local_fields = [
        item.get("field", {}) for item in sources.get("updates", ()) if isinstance(item, dict)
    ]
    locals_ = {
        item.get("guid")
        for item in local_fields
        if item.get("local") or item.get("calc_mode") == "formula"
    }
    for item in local_fields:
        if item.get("guid") and item.get("datasetId") in attached_or_referenced:
            owners.setdefault(item["guid"], set()).add(item["datasetId"])

    # Dataset declarations and the full attached schema are not actual chart bindings.
    for section in (
        "visualization",
        "filters",
        "sort",
        "localFields",
        "aggregatedFields",
        "hierarchies",
    ):
        found, unresolved = binding_occurrences(entity.data.get(section), section, owners, locals_)
        occurrences.extend(found)
        ambiguous.extend(unresolved)
    for section in ("filters", "sort", "hierarchies", "updates"):
        found, unresolved = binding_occurrences(
            sources.get(section), "sources/" + section, owners, locals_
        )
        occurrences.extend(found)
        ambiguous.extend(unresolved)
    used = {item["dataset_id"] for item in occurrences}
    unresolved_ids: set[str] = set()
    for item in ambiguous:
        if item.get("dataset_id"):
            unresolved_ids.add(item["dataset_id"])
        else:
            unresolved_ids.update(owners.get(item["guid"], attached_or_referenced))
    return {
        "declared_dataset_ids": declared,
        "referenced_dataset_ids": sorted(used),
        "unresolved_dataset_ids": sorted(unresolved_ids),
        "unused_dataset_ids": [
            identifier for identifier in declared if identifier not in used | unresolved_ids
        ],
        "bindings": occurrences,
        "ambiguous": ambiguous,
    }
