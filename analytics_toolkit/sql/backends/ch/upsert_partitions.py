"""Select physical ClickHouse partitions from complete target-typed incoming rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.backends.upsert import incoming_stage_source_sql
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .dialect import ClickHouseDialect

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


PARTITION_VALUE_COLUMN = "__atk_physical_partition"
_INPUT_ALIAS = "physical_input"
_UNRESOLVABLE = (
    exp.Query,
    exp.Subquery,
    exp.Table,
    exp.Star,
    exp.Window,
    exp.AggFunc,
    exp.Lambda,
    exp.Placeholder,
    exp.Parameter,
)


@dataclass(frozen=True)
class PartitionSelector:
    """Keep an immutable rendering of the actual storage expression and native types."""

    expression_sql: str
    column_types: tuple[tuple[str, str], ...]


def _native_type(native: str) -> str:
    if not isinstance(native, str) or not native.strip():
        message = "ClickHouse partition inputs require static native target types."
        raise InvalidSqlInputError(message)
    try:
        statements = ClickHouseDialect().parse(f"SELECT CAST(NULL AS {native})")
    except (SqlglotError, TypeError, ValueError) as exc:
        message = "Cannot resolve a ClickHouse partition input's native target type."
        raise InvalidSqlInputError(message) from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        message = "Cannot resolve a ClickHouse partition input's native target type."
        raise InvalidSqlInputError(message)
    query = statements[0]
    if (
        any(value for key, value in query.args.items() if key != "expressions")
        or len(query.expressions) != 1
    ):
        message = "ClickHouse partition inputs require static native target types."
        raise InvalidSqlInputError(message)
    cast = query.expressions[0]
    if not isinstance(cast, exp.Cast) or not isinstance(cast.args.get("to"), exp.DataType):
        message = "ClickHouse partition inputs require static native target types."
        raise InvalidSqlInputError(message)
    return str(cast.args["to"].sql(dialect=ClickHouseDialect))


def build_partition_selector(
    ddl: str, table: str, column_types: Mapping[str, str]
) -> PartitionSelector:
    """Resolve actual storage syntax without guessing a partition column or function set."""
    from .reconfigure_ddl import (  # noqa: PLC0415 -- DDL imports the backend registry.
        parse_create_table,
    )

    create = parse_create_table(ddl, table)
    properties = create.args.get("properties")
    partitions = (
        [item for item in properties.expressions if isinstance(item, exp.PartitionedByProperty)]
        if properties is not None
        else []
    )
    if len(partitions) != 1 or not isinstance(partitions[0].this, exp.Expression):
        message = "ClickHouse upsert requires a resolvable physical PARTITION BY expression."
        raise InvalidSqlInputError(message)
    if not column_types or any(not isinstance(name, str) or not name for name in column_types):
        message = "ClickHouse partition selection requires the complete writable target schema."
        raise InvalidSqlInputError(message)
    expression = partitions[0].this.copy()
    if any(isinstance(node, _UNRESOLVABLE) for node in expression.walk()):
        message = "ClickHouse upsert cannot resolve this physical partition expression safely."
        raise InvalidSqlInputError(message)
    for column in expression.find_all(exp.Column):
        if column.table or column.db or column.catalog or column.name not in column_types:
            message = "A physical partition expression references an unresolved writable column."
            raise InvalidSqlInputError(message)
        column.set("this", exp.to_identifier(column.name, quoted=True))
    for node in expression.walk():
        node.comments = None
    types = tuple((name, _native_type(native)) for name, native in column_types.items())
    return PartitionSelector(expression.sql(dialect=ClickHouseDialect), types)


def target_partition_sql(selector: PartitionSelector, alias: str) -> str:
    """Qualify a fresh expression with the row source used by a preservation query."""
    expression = parse_one(selector.expression_sql, read=ClickHouseDialect)
    for column in expression.find_all(exp.Column):
        column.set("table", exp.to_identifier(alias, quoted=True))
    return str(expression.sql(dialect=ClickHouseDialect))


def typed_incoming_source(
    adapter: Any,
    stage_table: str,
    selector: PartitionSelector,
    *,
    incoming_stage_tables: Sequence[str] | None = None,
) -> str:
    """Cast every incoming writable value before evaluating physical partition syntax."""
    return (
        _typed_incoming_query(
            adapter, stage_table, selector, incoming_stage_tables=incoming_stage_tables
        )
        .subquery()
        .sql(dialect=ClickHouseDialect)
    )


def _typed_incoming_query(
    adapter: Any,
    stage_table: str,
    selector: PartitionSelector,
    *,
    incoming_stage_tables: Sequence[str] | None = None,
) -> exp.Select:
    source = incoming_stage_source_sql(
        adapter,
        stage_table,
        incoming_stage_tables=incoming_stage_tables,
        columns=[name for name, _native in selector.column_types],
    )
    columns = [
        exp.alias_(
            exp.Cast(
                this=exp.column(name, quoted=True),
                to=exp.DataType.build(native, dialect=ClickHouseDialect),
            ),
            name,
            quoted=True,
        )
        for name, native in selector.column_types
    ]
    return exp.select(*columns).from_(source, dialect=ClickHouseDialect)


def incoming_partition_sql(
    adapter: Any,
    stage_table: str,
    selector: PartitionSelector,
    *,
    incoming_stage_tables: Sequence[str] | None = None,
) -> str:
    """Read the exact native values that ClickHouse DROP PARTITION will address."""
    source = _typed_incoming_query(
        adapter, stage_table, selector, incoming_stage_tables=incoming_stage_tables
    )
    expression = target_partition_sql(selector, _INPUT_ALIAS)
    return (
        exp.select(
            exp.alias_(
                parse_one(expression, read=ClickHouseDialect),
                PARTITION_VALUE_COLUMN,
                quoted=True,
            )
        )
        .distinct()
        .from_(source.subquery(alias=_INPUT_ALIAS))
        .sql(dialect=ClickHouseDialect)
    )
