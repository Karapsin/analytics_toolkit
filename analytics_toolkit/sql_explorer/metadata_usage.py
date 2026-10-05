"""Translate successful journal object references into narrowly scoped scan requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlglot
from sqlglot import exp

if TYPE_CHECKING:
    from collections.abc import Callable

    from .metadata_ledger import ObjectKey
    from .metadata_store import SnapshotKey, Snapshots


@dataclass(frozen=True)
class ReferenceContext:
    backend: str
    defaults: dict[str, Any]
    snapshots: Snapshots
    resolve: Callable[[str, dict[str, Any]], tuple[str, str] | None]
    legacy: bool
    ddl: bool = False


def _canonical_trino(obj: ObjectKey, snapshots: Snapshots) -> ObjectKey:
    kind, catalog, schema, name = obj

    def canonical(value: str, names: tuple[str, ...]) -> str:
        return next((item for item in names if item.casefold() == value.casefold()), value.lower())

    catalog = canonical(catalog, snapshots.get(("catalog", "", ""), ()))
    schema = canonical(schema, snapshots.get(("schema", catalog, ""), ()))
    if kind == "catalog":
        name = catalog
    elif kind == "schema":
        name = schema
    else:
        name = canonical(name, snapshots.get(("table", catalog, schema), ()))
    return kind, catalog, schema, name


def _resolve_object(obj: dict[str, Any], reference: ReferenceContext) -> ObjectKey | None:
    backend, context = reference.backend, reference.defaults
    kind, name = obj["kind"], obj.get("name")
    if not name:
        return None
    catalog = (obj.get("catalog") or context.get("catalog") or "") if backend == "trino" else ""
    schema = obj.get("schema") or ""
    if kind in {"schema", "database"}:
        return "schema", catalog, name, name
    if kind == "catalog":
        return kind, name, "", name
    if kind not in {"table", "view"}:
        return None
    if not schema:
        namespace = _unqualified(name, catalog, reference)
        if namespace is None:
            return None
        catalog, schema = namespace
    return "table", catalog, schema, name


def _unqualified(name: str, catalog: str, reference: ReferenceContext) -> tuple[str, str] | None:
    backend, context, snapshots = reference.backend, reference.defaults, reference.snapshots
    if reference.legacy or reference.ddl:
        matches = [
            (c, s)
            for (kind, c, s), names in snapshots.items()
            if kind == "table" and name in names and (not catalog or catalog == c)
        ]
        if reference.legacy:
            return matches[0] if len(matches) == 1 else None
        if matches and backend == "gp":
            return reference.resolve(name, {**context, "candidates": matches})
    schema = context.get("database") if backend == "ch" else context.get("schema")
    if schema:
        return catalog, schema
    return reference.resolve(name, context)


def _object_scopes(
    obj: ObjectKey,
    snapshots: Snapshots,
    *,
    backend: str,
    action: str,
    ddl: bool,
) -> dict[SnapshotKey, str]:
    kind, catalog, schema, name = obj
    reason = "ddl" if ddl else ""
    scopes: dict[SnapshotKey, str] = {}
    if backend == "trino":
        known_catalogs = snapshots.get(("catalog", "", ""), ())
        scopes[("catalog", "", "")] = (
            "missing" if catalog not in known_catalogs else reason if kind == "catalog" else ""
        )
    if kind == "catalog":
        if action != "drop":
            scopes[("schema", catalog, "")] = reason
        return scopes
    schemas = snapshots.get(("schema", catalog, ""), ())
    scopes[("schema", catalog, "")] = (
        "missing" if schema not in schemas else reason if kind in {"schema", "database"} else ""
    )
    if kind in {"schema", "database"}:
        if action != "drop":
            scopes[("table", catalog, schema)] = reason
    else:
        names = snapshots.get(("table", catalog, schema), ())
        scopes[("table", catalog, schema)] = reason or ("missing" if name not in names else "")
    return scopes


def event_usage(
    event: dict[str, Any],
    snapshots: Snapshots,
    defaults: dict[str, Any],
    resolve: Callable[[str, dict[str, Any]], tuple[str, str] | None],
) -> tuple[dict[SnapshotKey, str], list[ObjectKey]]:
    """Legacy unresolved names are ignored rather than assigned a guessed namespace."""
    saved_context = event["context"].get("metadata", {})
    context = dict(saved_context or defaults)
    scopes: dict[SnapshotKey, str] = {}
    objects: list[ObjectKey] = []
    for statement in event["statements"]:
        context.update(statement.get("namespace", {}))
        targets = statement.get("targets", [])
        if "targets" not in statement and statement["action"] in {
            "create",
            "drop",
            "alter",
            "rename",
        }:
            targets = statement["objects"]
        for obj in statement["objects"]:
            reference = ReferenceContext(
                event["backend"], context, snapshots, resolve, not saved_context, obj in targets
            )
            resolved = _resolve_object(obj, reference)
            if resolved is None:
                continue
            if event["backend"] == "trino":
                resolved = _canonical_trino(resolved, snapshots)
            objects.append(resolved)
            additions = _object_scopes(
                resolved,
                snapshots,
                backend=event["backend"],
                action=statement["action"],
                ddl=obj in targets,
            )
            for key, reason in additions.items():
                scopes[key] = reason or scopes.get(key, "")
    return scopes, objects


def column_in_scopes(table: str, backend: str, scopes: set[SnapshotKey]) -> bool:
    """Unqualified column lookups may belong to any affected namespace."""
    dialect = {"gp": "postgres", "ch": "clickhouse", "trino": "trino"}[backend]
    try:
        parsed = sqlglot.parse_one(table, read=dialect, into=exp.Table)
        if not parsed.db:
            return True
        return any(
            (not catalog or not parsed.catalog or catalog == parsed.catalog)
            and (kind == "catalog" or (kind == "table" and schema == parsed.db))
            for kind, catalog, schema in scopes
        )
    except (ValueError, sqlglot.errors.SqlglotError):
        return True
