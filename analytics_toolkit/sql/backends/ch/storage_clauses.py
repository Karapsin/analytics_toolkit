"""Engine-aware physical clauses without rewriting caller SQL expressions."""

from __future__ import annotations

from functools import lru_cache

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from .dialect import ClickHouseDialect

_INVALID_ENGINE = "Cannot parse the ClickHouse engine expression for storage clauses."
_MEMORY_KEYS = "ClickHouse Memory engine does not support explicit storage key clauses."
_KEY_PROPERTIES = (exp.Order, exp.PartitionedByProperty, exp.PrimaryKey, exp.SampleProperty)


class _StorageClauseError(ValueError):
    analytics_toolkit_sql_retry_safe = False


@lru_cache(maxsize=128)
def _engine_features(engine: str) -> tuple[str, bool]:
    try:
        create = parse_one(
            f"CREATE TABLE __atk_storage__ (id UInt8) ENGINE = {engine}",
            read=ClickHouseDialect,
        )
    except SqlglotError:
        raise _StorageClauseError(_INVALID_ENGINE) from None
    if not isinstance(create, exp.Create):
        raise _StorageClauseError(_INVALID_ENGINE)
    properties = create.args.get("properties")
    engine_property = create.find(exp.EngineProperty)
    if properties is None or engine_property is None:
        raise _StorageClauseError(_INVALID_ENGINE)
    name = engine_property.this.name or engine_property.this.key
    return name.casefold(), any(
        isinstance(property_, _KEY_PROPERTIES) for property_ in properties.expressions
    )


def physical_storage_clauses(
    engine: str, *, partition_sql: str, order_sql: str, explicit_order: bool
) -> str:
    """Keep explicit keys, validate Memory, and default only MergeTree sorting.

    Other engines may support their own keys. Their explicit expressions remain
    authoritative; an omitted ordering option never creates a synthetic key.
    Only the engine is parsed: rendered keys and engine literals are returned by
    their existing builders without a parse/render round trip.
    """
    name, engine_keys = _engine_features(engine)
    if name == "memory" and (partition_sql or explicit_order or engine_keys):
        raise _StorageClauseError(_MEMORY_KEYS)
    if not explicit_order and not name.endswith("mergetree"):
        order_sql = ""
    return partition_sql + order_sql
