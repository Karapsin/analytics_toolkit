"""Exact namespace coverage shared by persistent and interactive metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .metadata_store import SnapshotKey, Snapshots


def removed_by_parent(key: SnapshotKey, parent: SnapshotKey, names: tuple[str, ...]) -> bool:
    kind, catalog, _schema = parent
    return (kind == "catalog" and key[0] != "catalog" and key[1] not in names) or (
        kind == "schema" and key[0] == "table" and key[1] == catalog and key[2] not in names
    )


def scope_keys(  # noqa: PLR0913 -- exact scope and its resolved namespace context.
    kind: str,
    catalog: str | None,
    schema: str | None,
    *,
    backend: str,
    context: dict[str, Any],
    snapshots: Snapshots,
    default_catalog: str | None,
) -> tuple[SnapshotKey, ...] | None:
    if kind == "catalog":
        return (("catalog", "", ""),)
    catalog = catalog or default_catalog or ""
    catalogs = snapshots.get(("catalog", "", ""))
    if backend == "trino" and catalogs is not None and catalog not in catalogs:
        return (("catalog", "", ""),)
    parent = ("schema", catalog, "")
    if kind == "schema":
        return (parent,)
    if kind != "table":
        return None
    if schema is None and backend in {"ch", "clickhouse"}:
        schema = context.get("database")
        if schema is None:
            return None
    schemas = snapshots.get(parent)
    return _table_scope_keys(catalog, schema, schemas)


def _table_scope_keys(
    catalog: str, schema: str | None, schemas: tuple[str, ...] | None
) -> tuple[SnapshotKey, ...] | None:
    parent = ("schema", catalog, "")
    if schema is not None:
        return (
            (parent,)
            if schemas is not None and schema not in schemas
            else (("table", catalog, schema),)
        )
    if schemas is None:
        return None
    return (parent, *(("table", catalog, name) for name in schemas))
