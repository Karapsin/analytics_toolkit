"""Named view certificates do not infer ownership of storage companions."""

from __future__ import annotations

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError

from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

from .dialect import ClickHouseDialect
from .reconfigure_ddl import comparable_create_sql, engine_name, engine_sql, parse_create_table


def is_view_definition(create: exp.Create) -> bool:
    return str(create.args.get("kind", "")).upper() == "VIEW"


def parse_drop_definition(ddl: str, table: str) -> exp.Create:
    """Recognize a static table or view while binding it to the certified name."""
    message = "Cannot certify the selected ClickHouse table or view definition for drop."
    try:
        create = parse_one(ddl.strip().rstrip(";"), read=ClickHouseDialect)
        expected = parse_one(table, read=ClickHouseDialect, into=exp.Table)
    except SqlglotError:
        raise InvalidSqlInputError(message) from None
    if not isinstance(create, exp.Create):
        raise InvalidSqlInputError(message)
    if str(create.args.get("kind", "")).upper() == "TABLE":
        return parse_create_table(ddl, table)
    if not is_view_definition(create) or not isinstance(expected, exp.Table):
        raise InvalidSqlInputError(message)
    target = create.this.this if isinstance(create.this, exp.Schema) else create.this
    if (
        not isinstance(target, exp.Table)
        or not isinstance(target.this, exp.Identifier)
        or target.catalog
        or target.name != expected.name
        or (target.db and target.db != expected.db)
    ):
        raise InvalidSqlInputError(message)
    if isinstance(create.this, exp.Schema):
        create.this.set("this", expected)
    else:
        create.set("this", expected)
    return create


def drop_definition_engine(ddl: str, table: str) -> str:
    create = parse_drop_definition(ddl, table)
    if is_view_definition(create):
        return "MaterializedView" if create.find(exp.MaterializedProperty) else "View"
    return engine_name(create)


def drop_definition_signature(ddl: str, table: str) -> str:
    from .managed_routing import _ClusterObject, _normalized_definition  # noqa: PLC0415
    from .replacement_scope import _physical_signature  # noqa: PLC0415 -- layout import cycle.

    create = parse_drop_definition(ddl, table)
    if not is_view_definition(create):
        return _physical_signature(ddl, table)
    has_engine = create.find(exp.EngineProperty) is not None
    return _normalized_definition(
        _ClusterObject(
            "",
            engine_name(create) if has_engine else "View",
            engine_sql(create) if has_engine else "",
            comparable_create_sql(create),
            "",
        )
    )
