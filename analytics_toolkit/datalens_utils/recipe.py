"""Versioned recipe parsing and dependency registry; no runtime writes."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from .deployment import BIProjectDeployment
from .errors import DataLensConfigurationError
from .session import current_session
from .settings import asset_path, read_chart_definitions, read_config

RECIPE_SCHEMA_VERSION = 2

if TYPE_CHECKING:
    from pathlib import Path

_RUNTIME_FIELDS = {"schema_version", "installation", "request_interval_seconds"}
_FIELDS = {
    "connection": {
        "mode",
        "connector",
        "id",
        "name",
        "parameters",
        "secrets",
        "credentials_revision",
        "description",
        "target",
    },
    "collection": {"id", "name", "parent", "description"},
    "workbook": {"id", "name", "parent", "description"},
    "html_page": {"id", "name", "description", "content_file"},
    "dataset": {
        "id",
        "name",
        "description",
        "sources",
        "relations",
        "fields",
        "calculations",
        "parameters",
        "default_filters",
        "aggregations",
        "rls",
        "cache_invalidation",
        "settings",
    },
    "source": {"connection", "factory", "parameters", "sql_file", "id", "avatar_id"},
    "relation": {"left", "right", "type", "conditions", "drop_duplicates", "id"},
    "condition": {"left", "right", "operator"},
    "field": {
        "source",
        "column",
        "guid",
        "title",
        "cast",
        "kind",
        "aggregation",
        "description",
        "hidden",
    },
    "rls": {"field", "subject_id", "subject_type", "subject_name", "allowed_value", "pattern_type"},
    "cache": {"mode", "sql_file", "field", "filters"},
    "cache_field": {"guid", "type", "formula"},
    "cache_formula": {"formula", "guid_formula"},
    "cache_filter": {"key", "field", "operation", "values"},
    "dashboard": {
        "id",
        "description",
        "hide_tabs",
        "folders",
        "settings",
        "global_parameters",
        "support_description",
        "access_description",
    },
    "chart": {
        "key",
        "id",
        "family",
        "type",
        "name",
        "title",
        "tab",
        "dataset",
        "connection",
        "fields",
        "local_fields",
        "aggregated_measures",
        "hierarchies",
        "settings",
        "layers",
        "map_center",
        "sort",
        "filters",
        "show_title",
        "description",
        "axis_titles",
        "labels",
        "labels_position",
        "formats",
        "totals",
        "query_file",
        "scripts",
        "links",
        "params",
        "columns",
        "visualization",
        "enable_action_params",
        "presentation",
    },
}


def strict(value: Any, allowed: set[str], context: str) -> None:
    if not isinstance(value, dict):
        raise DataLensConfigurationError(context + " must be an object.")
    unknown = set(value) - allowed
    if unknown:
        msg = f"Unknown {context} fields: {sorted(unknown)}."
        raise DataLensConfigurationError(msg)


def validate_runtime(value: dict[str, Any]) -> None:
    strict(value, _RUNTIME_FIELDS, "version-2 runtime")


def definitions_file(root: Path, name: str) -> dict[str, Any]:
    path = root / "configs/DL objects" / (name + ".json")
    if not path.exists():
        return {}
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise DataLensConfigurationError(name + " definitions must be an object.")
    return result


@dataclass(frozen=True)
class Resource:
    key: str
    kind: str
    definition: dict[str, Any]
    files: dict[str, Any]
    dependencies: tuple[str, ...] = ()


class ResourceRegistry:
    """Named resource contracts and their minimal dependency closure."""

    def __init__(self, resources: dict[str, Resource]) -> None:
        self.resources = resources
        for resource in resources.values():
            missing = set(resource.dependencies) - resources.keys()
            if missing:
                msg = f"{resource.key} references missing resources: {sorted(missing)}."
                raise DataLensConfigurationError(msg)
        self.order = self._order(set(resources))

    def _order(self, keys: set[str]) -> list[str]:
        result: list[str] = []
        visiting: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise DataLensConfigurationError("Resource dependency cycle at " + key + ".")
            if key in result:
                return
            visiting.add(key)
            for dependency in self.resources[key].dependencies:
                visit(dependency)
            visiting.remove(key)
            result.append(key)

        for key in sorted(keys):
            visit(key)
        return result

    def selection(self, keys: Sequence[str] | None) -> set[str]:
        selected = set(self.resources) if keys is None else set(keys)
        missing = selected - self.resources.keys()
        if missing:
            msg = f"Unknown resource keys: {sorted(missing)}."
            raise DataLensConfigurationError(msg)
        if keys is not None and not selected:
            msg = "resource_keys cannot be empty."
            raise DataLensConfigurationError(msg)
        return selected

    def closure(self, selected: set[str]) -> tuple[list[str], list[str]]:
        dependencies = self._order(selected)
        affected = set(selected)
        while True:
            expanded = affected | {
                key
                for key, resource in self.resources.items()
                if set(resource.dependencies) & affected
            }
            if expanded == affected:
                break
            affected = expanded
        return dependencies, [key for key in self.order if key in affected - selected]

    def read_closure(self, selected: set[str]) -> tuple[list[str], set[str], list[str]]:
        """Read prerequisites of every affected resource without enlarging writes."""
        _, affected = self.closure(selected)
        writes = selected | set(affected)
        return self._order(writes), writes, affected


def validate_connection(key: str, definition: dict[str, Any]) -> None:
    for section in ("parameters", "secrets"):
        if section in definition and not isinstance(definition[section], dict):
            raise DataLensConfigurationError(key + " " + section + " must be an object.")
    if definition.get("mode") not in {"existing", "managed"}:
        raise DataLensConfigurationError(key + " mode must be existing or managed.")
    if not definition.get("connector"):
        raise DataLensConfigurationError(key + " requires a connector.")
    if definition["mode"] == "existing" and (
        definition.get("parameters") or definition.get("secrets")
    ):
        raise DataLensConfigurationError(key + " existing connections are read-only references.")
    if definition.get("secrets") and not definition.get("credentials_revision"):
        raise DataLensConfigurationError(key + " secrets require a nonsecret credentials_revision.")
    if not all(
        isinstance(value, str) and value for value in definition.get("secrets", {}).values()
    ):
        raise DataLensConfigurationError(
            key + " secrets must reference environment variable names."
        )


def validate_dataset_sources(key: str, definition: dict[str, Any]) -> None:
    for section in ("sources", "relations", "fields", "calculations", "parameters", "rls"):
        if section in definition and not isinstance(definition[section], dict):
            raise DataLensConfigurationError(key + " " + section + " must be an object.")
    if not definition.get("sources"):
        raise DataLensConfigurationError(key + " requires named sources.")
    for name, source in definition["sources"].items():
        strict(source, _FIELDS["source"], key + " source " + name)
        if not source.get("connection") or not source.get("factory"):
            raise DataLensConfigurationError(key + " sources require connection and factory.")
    for name, relation in definition.get("relations", {}).items():
        strict(relation, _FIELDS["relation"], key + " relation " + name)
        if (
            relation.get("left") not in definition["sources"]
            or relation.get("right") not in definition["sources"]
        ):
            raise DataLensConfigurationError(key + " relation references an unknown source.")
        if (
            relation.get("left") == relation.get("right")
            or relation.get("type") not in {"inner", "left", "right", "full"}
            or not relation.get("conditions")
        ):
            raise DataLensConfigurationError(
                key + " relation requires distinct sources, a join type, and conditions."
            )
        for condition in relation["conditions"]:
            strict(condition, _FIELDS["condition"], key + " join condition")


def validate_dataset_fields(key: str, definition: dict[str, Any]) -> None:
    names = [
        name
        for section in ("fields", "calculations", "parameters")
        for name in definition.get(section, {})
    ]
    guids = [
        value["guid"]
        for section in ("fields", "calculations", "parameters")
        for value in definition.get(section, {}).values()
        if value.get("guid")
    ]
    if len(names) != len(set(names)) or len(guids) != len(set(guids)):
        message = "Dataset field keys and retained GUIDs must be unique: " + key
        raise DataLensConfigurationError(message)
    for name, field in definition.get("fields", {}).items():
        strict(field, _FIELDS["field"], key + " field " + name)
        if field.get("source") not in definition["sources"] or not field.get("column"):
            raise DataLensConfigurationError(key + " direct fields require source and column.")
        kind = "DIMENSION" if field.get("aggregation", "none") == "none" else "MEASURE"
        if field.get("kind", kind) != kind:
            raise DataLensConfigurationError(key + " direct field kind must match aggregation.")
    for name, field in definition.get("calculations", {}).items():
        strict(
            field,
            {"formula", "kind", "cast", "aggregation", "guid"},
            key + " calculation " + name,
        )
    for name, field in definition.get("parameters", {}).items():
        strict(field, {"type", "default", "guid"}, key + " parameter " + name)
    for field in definition.get("default_filters", []):
        strict(field, {"field", "operator", "values"}, key + " default filter")
    for name, rule in definition.get("rls", {}).items():
        strict(rule, _FIELDS["rls"], key + " RLS " + name)
        if rule.get("subject_type", "user") not in {
            "user",
            "group",
            "all",
            "userid",
        } or not rule.get("subject_id"):
            raise DataLensConfigurationError(key + " RLS requires a valid subject identity.")


def validate_cache(key: str, definition: dict[str, Any]) -> None:
    cache = definition["cache_invalidation"]
    strict(cache, _FIELDS["cache"], key + " cache")
    if cache.get("mode") not in {"off", "sql", "formula"}:
        raise DataLensConfigurationError(key + " cache mode must be off, sql, or formula.")
    if cache.get("mode") == "sql" and not cache.get("sql_file"):
        raise DataLensConfigurationError(key + " SQL cache requires sql_file.")
    if cache.get("mode") == "formula":
        strict(cache.get("field"), _FIELDS["cache_field"], key + " cache field")
        strict(cache["field"].get("formula"), _FIELDS["cache_formula"], key + " cache formula")
        if not all(cache["field"]["formula"].get(name) for name in ("formula", "guid_formula")):
            raise DataLensConfigurationError(key + " cache formula requires both representations.")
    for item in cache.get("filters", []):
        strict(item, _FIELDS["cache_filter"], key + " cache filter")
        if not item.get("key"):
            raise DataLensConfigurationError(key + " cache filters require stable keys.")


def _validate_v2(kind: str, key: str, definition: dict[str, Any]) -> None:
    strict(definition, _FIELDS[kind], key)
    if kind != "dashboard" and (
        not isinstance(definition.get("name"), str) or not definition["name"].strip()
    ):
        raise DataLensConfigurationError(key + " requires a nonempty name.")
    if kind == "connection":
        validate_connection(key, definition)
    elif kind == "dataset":
        validate_dataset_sources(key, definition)
        validate_dataset_fields(key, definition)
        if "cache_invalidation" in definition:
            validate_cache(key, definition)


def resource_dependencies(
    kind: str,
    definition: dict[str, Any],
    definitions: dict[str, Any],
    target_dependency: tuple[str, ...],
    *,
    v2: bool,
) -> tuple[str, ...]:
    dependencies = (
        list(target_dependency)
        if kind in {"connection", "dataset", "chart", "dashboard", "html_page"}
        else []
    )
    if kind in {"collection", "workbook"} and definition.get("parent"):
        dependencies.append("collection:" + definition["parent"])
    if kind == "dataset":
        dependencies += (
            [
                "connection:" + source["connection"]
                for source in definition.get("sources", {}).values()
            ]
            if v2
            else ["connection:default"]
        )
    if kind == "chart":
        roles = {definition["dataset"]} if definition.get("dataset") else set()
        roles |= set(definition.get("links", {}).values())
        roles |= {
            layer["dataset"] for layer in definition.get("layers", []) if layer.get("dataset")
        }
        dependencies += ["dataset:" + role for role in sorted(roles)]
        if definition["family"] == "ql":
            dependencies.append("connection:" + definition.get("connection", "default"))
    if kind == "dashboard":
        dependencies += ["chart:" + role for role in definitions["chart"]]
        dependencies += ["dataset:" + role for role in definitions["dataset"]]
    return tuple(dict.fromkeys(dependencies))


def resource_files(kind: str, definition: dict[str, Any], root: Path) -> dict[str, Any]:
    files = {"definition": definition}
    assets = list(definition.get("scripts", {}).values())
    assets += [
        definition[name]
        for name in ("projection_file", "query_file", "content_file")
        if definition.get(name)
    ]
    assets += [
        source["sql_file"]
        for source in definition.get("sources", {}).values()
        if source.get("sql_file")
    ]
    cache = definition.get("cache_invalidation", {})
    if cache.get("sql_file"):
        assets.append(cache["sql_file"])
    for relative in assets:
        files[relative] = asset_path(relative).read_text(encoding="utf-8")
    if kind == "dashboard":
        for path in sorted((root / "configs/UI").rglob("*.json")):
            files[path.relative_to(root).as_posix()] = json.loads(path.read_text(encoding="utf-8"))
    return files


def load_registry() -> ResourceRegistry:
    state = current_session()
    root = state.paths.project_root
    v2 = state.runtime.get("schema_version", 1) == RECIPE_SCHEMA_VERSION
    definitions = {
        "dataset": read_config("DL objects/datasets.json"),
        "chart": read_chart_definitions(),
        "dashboard": {"main": read_config("DL objects/dashboard.json")},
    }
    for kind, filename in (
        ("connection", "connections"),
        ("collection", "collections"),
        ("workbook", "workbooks"),
        ("html_page", "html_pages"),
    ):
        definitions[kind] = definitions_file(root, filename) if v2 else {}
    if not v2 or not isinstance(state.deployment, BIProjectDeployment):
        deployment = state.deployment
        if isinstance(deployment, BIProjectDeployment):
            msg = "BIProjectDeployment requires schema_version 2."
            raise DataLensConfigurationError(msg)
        definitions["connection"].setdefault(
            "default",
            {
                "mode": "existing",
                "connector": "clickhouse",
                "name": deployment.connection_name,
                "id": deployment.connection_id,
            },
        )
    resources = {}
    target_dependency: tuple[str, ...] = ()
    if (
        isinstance(state.deployment, BIProjectDeployment)
        and state.deployment.target.kind == "workbook"
        and state.deployment.target.reference == "key"
    ):
        target_dependency = ("workbook:" + state.deployment.target.value,)
    for kind, values in definitions.items():
        for name, original in values.items():
            key = kind + ":" + name
            definition = copy.deepcopy(original)
            if v2:
                _validate_v2(kind, key, definition)
            dependencies = resource_dependencies(
                kind, definition, definitions, target_dependency, v2=v2
            )
            files = resource_files(kind, definition, root)
            resources[key] = Resource(key, kind, definition, files, dependencies)
    return ResourceRegistry(resources)
