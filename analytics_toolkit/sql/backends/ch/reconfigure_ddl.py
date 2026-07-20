from __future__ import annotations

# ruff: noqa: EM101, EM102, FBT001, FBT003, PLR0913, PLR2004, S101, TRY003
import math
import re
from collections.abc import Mapping, Sequence
from typing import cast

from sqlglot import exp, parse_one

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from analytics_toolkit.sql.ddl.identifiers import (
    _identifier_name,
    _parse_table_name,
    quote_identifier,
)

from .ddl import _sql_string_literal
from .metadata import extract_clickhouse_distributed_shard_table

_SETTING_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def transform_create_table(
    create: exp.Create,
    *,
    table_name: str,
    execution_cluster: str | None,
    ch_engine: str | None,
    ch_partition_by: Sequence[str] | str | None,
    ch_order_by: Sequence[str] | str | None,
    ch_settings: Mapping[str, str | int | float | bool | None] | None,
    ch_reset_partition_by: bool,
    ch_reset_order_by: bool,
) -> exp.Create:
    transformed = retarget_create(create, table_name, execution_cluster)
    if ch_engine is not None:
        _replace_property(transformed, exp.EngineProperty, _engine_property(ch_engine))
    if ch_reset_partition_by:
        _replace_property(transformed, exp.PartitionedByProperty, None)
    elif ch_partition_by is not None:
        _replace_property(
            transformed,
            exp.PartitionedByProperty,
            _partition_property(expression_sql(ch_partition_by, "ch_partition_by")),
        )
    if ch_reset_order_by:
        _replace_property(transformed, exp.Order, _order_property("tuple()"))
    elif ch_order_by is not None:
        _replace_property(
            transformed,
            exp.Order,
            _order_property(expression_sql(ch_order_by, "ch_order_by")),
        )
    if ch_settings:
        _apply_settings_to_create(transformed, ch_settings)
    _validate_replicated_path(transformed)
    return transformed


def transform_distributed_create(
    create: exp.Create,
    *,
    table_name: str,
    execution_cluster: str | None,
    target_cluster: str,
    shard_table: str,
    ch_sharding_key: str | None,
) -> exp.Create:
    transformed = retarget_create(create, table_name, execution_cluster)
    database, relation = distributed_table_parts(shard_table)
    existing = extract_clickhouse_distributed_shard_table(
        engine_sql(create),
        database,
    )
    sharding_key = ch_sharding_key or _distributed_sharding_key(create) or "rand()"
    if existing is not None and ch_sharding_key is None:
        sharding_key = _distributed_sharding_key(create) or "rand()"
    distributed_engine = (
        "Distributed("
        f"{_sql_string_literal(target_cluster)}, "
        f"{_sql_string_literal(database)}, "
        f"{_sql_string_literal(relation)}, "
        f"{sharding_key})"
    )
    engine_property = _engine_property(distributed_engine)
    engine = engine_property.this
    if (
        sharding_key.strip().lower() == "rand()"
        and isinstance(engine, exp.Anonymous)
        and len(engine.expressions) >= 4
        and isinstance(engine.expressions[3], exp.Rand)
    ):
        _restore_clickhouse_rand(engine.expressions[3])
    _replace_property(
        transformed,
        exp.EngineProperty,
        engine_property,
    )
    return transformed


def retarget_create(
    create: exp.Create,
    table_name: str,
    execution_cluster: str | None,
) -> exp.Create:
    transformed = create.copy()
    schema = transformed.this
    if not isinstance(schema, exp.Schema):
        raise InvalidSqlInputError("ClickHouse CREATE TABLE statement has no column schema.")
    schema.set("this", _parse_table_name(table_name, "clickhouse"))
    transformed.set("exists", True)
    properties = _properties(transformed)
    properties.set(
        "expressions",
        [prop for prop in properties.expressions if not _is_transient_create_property(prop)],
    )
    if execution_cluster is not None:
        properties.append(
            "expressions",
            exp.OnCluster(this=exp.Literal.string(execution_cluster)),
        )
    return transformed


def parse_create_table(sql: str, table: str) -> exp.Create:
    try:
        parsed = parse_one(sql.strip().rstrip(";"), read="clickhouse")
    except Exception as exc:
        raise InvalidSqlInputError(f"Could not parse ClickHouse DDL for {table}: {exc}") from exc
    if not isinstance(parsed, exp.Create) or str(parsed.args.get("kind", "")).upper() != "TABLE":
        raise InvalidSqlInputError(f"{table} is not a supported CREATE TABLE object.")
    if not isinstance(parsed.this, exp.Schema):
        raise InvalidSqlInputError(f"ClickHouse table {table} has no explicit schema.")
    return parsed


def engine_sql(create: exp.Create) -> str:
    prop = _property(create, exp.EngineProperty)
    if not isinstance(prop, exp.EngineProperty):
        raise InvalidSqlInputError("ClickHouse table DDL does not define an engine.")
    return cast("str", prop.this.sql(dialect="clickhouse"))


def engine_name(create: exp.Create) -> str:
    prop = _property(create, exp.EngineProperty)
    if not isinstance(prop, exp.EngineProperty):
        raise InvalidSqlInputError("ClickHouse table DDL does not define an engine.")
    engine = prop.this
    if isinstance(engine, exp.Anonymous):
        return str(engine.this)
    return cast("str", engine.sql(dialect="clickhouse")).split("(", 1)[0]


def require_merge_tree(create: exp.Create, table: str) -> None:
    if not engine_name(create).lower().endswith("mergetree"):
        raise InvalidSqlInputError(f"ClickHouse table {table} must use a MergeTree-family engine.")


def distributed_table_parts(table_name: str) -> tuple[str, str]:
    table = _parse_table_name(table_name, "clickhouse")
    relation = _identifier_name(table.this)
    database = table.args.get("db")
    return (
        _identifier_name(database) if isinstance(database, exp.Identifier) else "default",
        relation,
    )


def comparable_create_sql(create: exp.Create) -> str:
    comparable = create.copy()
    _restore_clickhouse_rand(comparable)
    comparable.set("exists", False)
    properties = _properties(comparable)
    properties.set(
        "expressions",
        [item for item in properties.expressions if not _is_transient_create_property(item)],
    )
    return comparable.sql(dialect="clickhouse")


def _restore_clickhouse_rand(expression: exp.Expression) -> None:
    for item in list(expression.find_all(exp.Rand)):
        item.replace(exp.Anonymous(this="rand"))


def _is_transient_create_property(expression: exp.Expression) -> bool:
    return isinstance(expression, exp.OnCluster) or type(expression).__name__ == "UuidProperty"


def expression_sql(value: Sequence[str] | str, option_name: str) -> str:
    if isinstance(value, str):
        normalized = _non_empty_string(value, option_name)
        try:
            parse_one(normalized, read="clickhouse")
        except Exception as exc:
            raise InvalidSqlInputError(f"Invalid {option_name}: {exc}") from exc
        return normalized
    if isinstance(value, (bytes, bytearray, Mapping)):
        raise InvalidSqlInputError(
            f"{option_name} must be a SQL expression or sequence of column names."
        )
    columns = [_non_empty_string(str(item), option_name) for item in value]
    if not columns:
        raise InvalidSqlInputError(f"{option_name} must not be empty.")
    if len(columns) != len(set(columns)):
        raise InvalidSqlInputError(f"{option_name} must not contain duplicates.")
    quoted = [quote_identifier(column, "ch") for column in columns]
    return quoted[0] if len(quoted) == 1 else f"({', '.join(quoted)})"


def setting_value_sql(value: str | float | bool) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidSqlInputError("ClickHouse setting values must be finite.")
        return repr(value)
    if isinstance(value, str):
        return _sql_string_literal(value)
    raise InvalidSqlInputError(
        "ClickHouse setting values must be strings, numbers, booleans, or None."
    )


def normalize_setting_name(name: str) -> str:
    normalized = str(name).strip()
    if not _SETTING_NAME_RE.fullmatch(normalized):
        raise InvalidSqlInputError(f"Invalid ClickHouse setting name: {name!r}.")
    return normalized


def _apply_settings_to_create(
    create: exp.Create,
    settings: Mapping[str, str | int | float | bool | None],
) -> None:
    current = _property(create, exp.SettingsProperty)
    expressions = (
        []
        if not isinstance(current, exp.SettingsProperty)
        else [item.copy() for item in current.expressions]
    )
    by_name: dict[str, exp.Expression] = {}
    for item in expressions:
        if isinstance(item, exp.EQ):
            by_name[item.this.sql(dialect="clickhouse")] = item
    for raw_name, value in settings.items():
        name = normalize_setting_name(raw_name)
        by_name.pop(name, None)
        if value is not None:
            by_name[name] = cast(
                "exp.Expression",
                parse_one(
                    f"{name}={setting_value_sql(value)}",
                    read="clickhouse",
                ),
            )
    replacement = exp.SettingsProperty(expressions=list(by_name.values())) if by_name else None
    _replace_property(create, exp.SettingsProperty, replacement)


def _properties(create: exp.Create) -> exp.Properties:
    properties = create.args.get("properties")
    if not isinstance(properties, exp.Properties):
        properties = exp.Properties(expressions=[])
        create.set("properties", properties)
    return properties


def _property(create: exp.Create, kind: type[exp.Expression]) -> exp.Expression | None:
    return next(
        (item for item in _properties(create).expressions if isinstance(item, kind)),
        None,
    )


def _replace_property(
    create: exp.Create,
    kind: type[exp.Expression],
    replacement: exp.Expression | None,
) -> None:
    properties = _properties(create)
    result: list[exp.Expression] = []
    inserted = False
    for item in properties.expressions:
        if isinstance(item, kind):
            if replacement is not None and not inserted:
                result.append(replacement)
                inserted = True
            continue
        result.append(item)
    if replacement is not None and not inserted:
        result.append(replacement)
    properties.set("expressions", result)


def _engine_property(engine_sql: str) -> exp.EngineProperty:
    normalized = _non_empty_string(engine_sql, "ch_engine")
    dummy = parse_create_table(
        f"CREATE TABLE x (id UInt8) ENGINE = {normalized} ORDER BY tuple()",
        "x",
    )
    prop = _property(dummy, exp.EngineProperty)
    assert isinstance(prop, exp.EngineProperty)
    return prop.copy()


def _partition_property(expression: str) -> exp.PartitionedByProperty:
    dummy = parse_create_table(
        f"CREATE TABLE x (id UInt8) ENGINE = MergeTree PARTITION BY {expression} ORDER BY tuple()",
        "x",
    )
    prop = _property(dummy, exp.PartitionedByProperty)
    assert isinstance(prop, exp.PartitionedByProperty)
    return prop.copy()


def _order_property(expression: str) -> exp.Order:
    dummy = parse_create_table(
        f"CREATE TABLE x (id UInt8) ENGINE = MergeTree ORDER BY {expression}",
        "x",
    )
    prop = _property(dummy, exp.Order)
    assert isinstance(prop, exp.Order)
    return prop.copy()


def _validate_replicated_path(create: exp.Create) -> None:
    prop = _property(create, exp.EngineProperty)
    if not isinstance(prop, exp.EngineProperty) or not isinstance(prop.this, exp.Anonymous):
        return
    engine = prop.this
    if not str(engine.this).lower().startswith("replicated") or not engine.expressions:
        return
    path = engine.expressions[0]
    if isinstance(path, exp.Literal) and path.is_string:
        value = str(path.this)
        if "{table}" not in value and "{uuid}" not in value:
            raise InvalidSqlInputError(
                "ReplicatedMergeTree paths must contain {table} or {uuid} so a "
                "replacement table cannot reuse the source replication path."
            )


def _distributed_sharding_key(create: exp.Create) -> str | None:
    prop = _property(create, exp.EngineProperty)
    if not isinstance(prop, exp.EngineProperty) or not isinstance(prop.this, exp.Anonymous):
        return None
    values = prop.this.expressions
    if len(values) < 4:
        return None
    if isinstance(values[3], exp.Rand):
        return "rand()"
    return cast("str", values[3].sql(dialect="clickhouse"))


def _non_empty_string(value: str, option_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError(f"{option_name} must not be empty.")
    return normalized


__all__ = [
    "comparable_create_sql",
    "distributed_table_parts",
    "engine_name",
    "engine_sql",
    "expression_sql",
    "normalize_setting_name",
    "parse_create_table",
    "require_merge_tree",
    "retarget_create",
    "setting_value_sql",
    "transform_create_table",
    "transform_distributed_create",
]
