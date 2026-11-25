"""Resolve storage-specific bounds only for wide high-precision timestamp writes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TypedDict, cast

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from .timestamp_bounds import timestamp_coordinates, timestamp_type

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from analytics_toolkit.sql.backends.value_profiles import ColumnProfile

_LOGGER = logging.getLogger(__name__)
_NS_MIN, _NS_MAX = -(2**63), 2**63 - 1
_MICROSECOND_PRECISION = 6
_NANOSECOND_PRECISION = 9
_QUALIFIED_PARTS = 3
_SCHEMA_RELATION_PARTS = 2
_ICEBERG_NANO_VERSION = 3


class TrinoTimestampCapabilityError(ValueError):
    analytics_toolkit_sql_retry_safe = False


class _StorageReceipt(TypedDict):
    connector: str
    format_version: int | None
    timestamp_storage: str


def _timestamp_leaves(
    profile: ColumnProfile, native: str
) -> Iterator[tuple[ColumnProfile, tuple[int, bool]]]:
    # Nested resolution recursively initializes backend conversion policies.
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415
        nested_descriptor,
    )

    spec = timestamp_type(native)
    if spec is not None:
        yield profile, spec
        return
    kind, fields = nested_descriptor(native)
    if kind:
        for child, (_name, field) in zip(profile.children, fields):
            yield from _timestamp_leaves(child, field)


def _wide_columns(schema: Mapping[str, str], profiles: Mapping[str, ColumnProfile]) -> list[str]:
    result = []
    for name, native in schema.items():
        profile = profiles.get(name)
        if profile is None:
            continue
        for leaf, (precision, aware) in _timestamp_leaves(profile, native):
            lower, upper = timestamp_coordinates(
                leaf, aware=aware, destination_timezone=leaf.destination_timezone
            )
            if (
                precision > _MICROSECOND_PRECISION
                and lower is not None
                and (lower < _NS_MIN or upper is None or upper > _NS_MAX)
            ):
                result.append(name)
                break
    return result


def _high_precision(native: str) -> bool:
    # Keep recursive native-type policies out of backend startup imports.
    from analytics_toolkit.sql.backends.nested_types import (  # noqa: PLC0415
        nested_descriptor,
    )

    spec = timestamp_type(native)
    if spec is not None:
        return spec[0] > _MICROSECOND_PRECISION
    return any(_high_precision(child) for _name, child in nested_descriptor(native)[1])


def timestamp_capability_options(
    schema: Mapping[str, str], profiles: Mapping[str, ColumnProfile] | None = None
) -> dict[str, Any]:
    """Represent an unresolved capability step without source or metadata I/O."""
    names = (
        _wide_columns(schema, profiles)
        if profiles is not None
        else [name for name, native in schema.items() if _high_precision(native)]
    )
    return (
        {
            "pending_timestamp_capabilities": {
                "pending": True,
                "columns": names,
                "reason": "wide high-precision timestamp storage requires connector metadata",
            }
        }
        if names
        else {}
    )


def _query(connection: Any, statement: str, parameters: list[Any] | None = None) -> list[Any]:
    # Execution labels initialize cancellation/general helpers; defer to I/O.
    from analytics_toolkit.sql.execution.backend_options import (  # noqa: PLC0415
        current_backend_query_label,
    )
    from analytics_toolkit.sql.execution.labels import (  # noqa: PLC0415
        apply_query_label,
    )

    cursor = connection.cursor()
    try:
        cursor.execute(apply_query_label(statement, current_backend_query_label()), parameters)
        return list(cursor.fetchall())
    finally:
        try:
            cursor.close()
        except Exception:  # noqa: BLE001 -- cursor cleanup cannot change the metadata outcome.
            _LOGGER.warning("Failed to close Trino timestamp capability metadata cursor.")


def _relation(connection: Any, table: str) -> tuple[str, str]:
    # TableIdentifier needs the completed backend registry.
    from analytics_toolkit.sql.core.identifiers import (  # noqa: PLC0415
        TableIdentifier,
    )

    identity = TableIdentifier.parse(table, "trino")
    parts = identity.parts
    catalog = getattr(connection, "catalog", None) or getattr(connection, "_catalog", None)
    schema = getattr(connection, "schema", None) or getattr(connection, "_schema", None)
    if len(parts) == 1 and isinstance(schema, str):
        parts = (schema, *parts)
    if len(parts) == _SCHEMA_RELATION_PARTS and isinstance(catalog, str):
        parts = (catalog, *parts)
    if len(parts) != _QUALIFIED_PARTS:
        message = (
            "Fully qualify catalog.schema.table before certifying wide Trino timestamp storage."
        )
        raise TrinoTimestampCapabilityError(message)
    relation = ".".join(
        exp.Identifier(this=part, quoted=True).sql(dialect="trino") for part in parts
    )
    # Trino identifier lookup is case-insensitive; this metadata predicate is a
    # VARCHAR comparison, so use the catalog's resolved identifier spelling.
    return parts[0].lower(), relation


def _format_version(properties: Mapping[str, Any]) -> int | None:
    value = properties.get("format_version")
    if type(value) is int:
        return value
    if isinstance(value, str):
        try:
            literal = parse_one(value, read="trino")
        except SqlglotError:
            return None
        if isinstance(literal, exp.Literal) and literal.is_int:
            return int(literal.this)
    return None


def _stored_properties(connection: Any, relation: str) -> dict[str, Any]:
    rows = _query(connection, f"SHOW CREATE TABLE {relation}")
    if len(rows) != 1 or not rows[0] or not isinstance(rows[0][0], str):
        message = "Cannot certify wide Trino timestamp storage without complete table properties."
        raise TrinoTimestampCapabilityError(message)
    try:
        tree = parse_one(rows[0][0], read="trino")
    except SqlglotError:
        message = "Cannot certify wide Trino timestamp storage from table metadata."
        raise TrinoTimestampCapabilityError(message) from None
    if not isinstance(tree, exp.Create):
        message = "Cannot certify wide Trino timestamp storage from table metadata."
        raise TrinoTimestampCapabilityError(message)
    properties = tree.args.get("properties")
    if properties is not None:
        for field in properties.expressions:
            if (
                isinstance(field, exp.Property)
                and field.name.casefold() == "format_version"
                and field.args.get("value") is not None
            ):
                return {"format_version": field.args["value"].sql(dialect="trino")}
    return {}


def _read_storage_receipt(
    connection: Any,
    catalog: str,
    relation: str,
    *,
    creating: bool,
    properties: Mapping[str, Any] | None,
) -> _StorageReceipt:
    rows = _query(
        connection,
        "SELECT connector_name FROM system.metadata.catalogs WHERE catalog_name = ?",
        [catalog],
    )
    if len(rows) != 1 or not rows[0] or not isinstance(rows[0][0], str):
        message = (
            "Cannot certify wide Trino timestamp storage; enable catalog connector metadata access."
        )
        raise TrinoTimestampCapabilityError(message)
    connector = rows[0][0].casefold()
    version = None
    if connector == "iceberg":
        version = (
            _format_version(properties or {})
            if creating
            else _format_version(_stored_properties(connection, relation))
        )
        if version is None or version < _ICEBERG_NANO_VERSION:
            message = (
                "Wide high-precision Iceberg timestamps require a verified format-v3 "
                "native nanosecond column; no automatic format migration is performed."
            )
            raise TrinoTimestampCapabilityError(message)
    elif connector != "memory":
        message = (
            "Cannot certify wide Trino timestamp storage for this connector; "
            "use a verified native representation or typed text."
        )
        raise TrinoTimestampCapabilityError(message)
    return {
        "connector": connector,
        "format_version": version,
        "timestamp_storage": "int64-nanoseconds" if connector == "iceberg" else "trino-native",
    }


def require_timestamp_capabilities(  # noqa: PLR0913 -- preserve explicit schema, input and creation authorities.
    connection: Any,
    table: str,
    schema: Mapping[str, str],
    profiles: Mapping[str, ColumnProfile],
    cache: dict[str, Any],
    *,
    creating: bool = False,
    properties: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Certify actual storage or report a pending execution-time capability step.

    Catalog names are aliases, never connector proofs. This helper only reads
    metadata; it never changes table format versions or destination resources.
    """
    names = _wide_columns(schema, profiles)
    if not names:
        return None
    if dry_run:
        pending = cast(
            "dict[str, Any]",
            timestamp_capability_options(schema, profiles)["pending_timestamp_capabilities"],
        )
        cache["pending_timestamp_capabilities"] = pending
        return pending
    catalog, relation = _relation(connection, table)
    records = cache.setdefault("trino_timestamp_capabilities", {})
    key = (relation, creating, tuple(schema.items()))
    previous = records.get(key)
    if previous is None or not any(connection is checked for checked in previous[1]):
        receipt = _read_storage_receipt(
            connection, catalog, relation, creating=creating, properties=properties
        )
        if previous is not None and previous[0] != receipt:
            message = "Trino timestamp storage capabilities changed after input certification."
            raise TrinoTimestampCapabilityError(message)
        checked = [] if previous is None else previous[1]
        checked.append(connection)
        records[key] = (receipt, checked)
    receipt = cast("_StorageReceipt", records[key][0])
    if receipt["connector"] == "iceberg":
        _validate_iceberg_nanos(schema, profiles)
    cache.pop("pending_timestamp_capabilities", None)
    return {**receipt, "columns": names}


def _validate_iceberg_nanos(
    schema: Mapping[str, str], profiles: Mapping[str, ColumnProfile]
) -> None:
    for name, native in schema.items():
        profile = profiles.get(name)
        if profile is None:
            continue
        for leaf, (precision, aware) in _timestamp_leaves(profile, native):
            if precision <= _MICROSECOND_PRECISION:
                continue
            if precision != _NANOSECOND_PRECISION:
                message = "Iceberg high-precision timestamp storage requires native precision nine."
                raise TrinoTimestampCapabilityError(message)
            lower, upper = timestamp_coordinates(
                leaf, aware=aware, destination_timezone=leaf.destination_timezone
            )
            if lower is not None and (lower < _NS_MIN or upper is None or upper > _NS_MAX):
                message = (
                    "Selected input exceeds Iceberg's signed64 nanosecond timestamp storage range."
                )
                raise TrinoTimestampCapabilityError(message)
