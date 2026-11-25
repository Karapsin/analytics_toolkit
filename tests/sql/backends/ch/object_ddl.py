"""Legacy Object metadata remains its own native type through layout inspection."""

from __future__ import annotations

import pytest
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.backends.ch.reconfigure_ddl import (
    comparable_create_sql,
    parse_create_table,
    retarget_create,
)
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp


@pytest.mark.parametrize(
    "native",
    [
        "Object('json')",
        "Array(Object('json'))",
        "Tuple(payload Object('json'), marker String)",
    ],
)
def test_ch_legacy_object_ddl_preserves_native_type_and_structural_round_trip(native: str) -> None:
    statement = (
        f"CREATE TABLE db.events (`payload.with.dot` {native}) ENGINE=MergeTree ORDER BY tuple()"
    )
    create = parse_create_table(statement, "db.events")
    column = create.this.expressions[0]
    assert column.name == "payload.with.dot"
    assert column.kind.sql(dialect="clickhouse") == native
    transformed = retarget_create(create, "db.replacement", execution_cluster=None)
    rendered = transformed.sql(dialect=ClickHouseDialect)
    reparsed = parse_create_table(rendered, "db.replacement")
    assert reparsed.this.expressions[0].kind.sql(dialect="clickhouse") == native
    assert "Object('json')" in comparable_create_sql(create)
    assert create.this.this.name == "events"


def test_ch_legacy_object_type_does_not_rewrite_json_literals_comments_or_rand() -> None:
    literal = "Object('json')  literal\\path"
    escaped = literal.replace("\\", "\\\\").replace("'", "\\'")
    statement = (
        "CREATE TABLE db.events (payload Object('json'), "
        f"note String DEFAULT '{escaped}' COMMENT '{escaped}', "
        "n UInt64 DEFAULT rand()) ENGINE=MergeTree ORDER BY tuple()"
    )
    create = parse_create_table(statement, "db.events")
    assert [item.this for item in create.find_all(exp.Literal) if item.is_string] == [
        "json",
        literal,
        literal,
    ]
    rendered = create.sql(dialect=ClickHouseDialect)
    reparsed = parse_create_table(rendered, "db.events")
    assert [item.this for item in reparsed.find_all(exp.Literal) if item.is_string] == [
        "json",
        literal,
        literal,
    ]
    assert "rand()" in rendered
    assert "randCanonical()" not in rendered


@pytest.mark.parametrize(
    "native",
    [
        "Object()",
        "Object('json', 'xml')",
        "Object('xml')",
        "Object(1)",
        "Object(format)",
        "Object('json' alias)",
    ],
)
def test_ch_legacy_object_ddl_requires_its_exact_native_format(native: str) -> None:
    with pytest.raises(InvalidSqlInputError, match="parse ClickHouse DDL"):
        parse_create_table(
            f"CREATE TABLE db.events (payload {native}) ENGINE=MergeTree ORDER BY tuple()",
            "db.events",
        )
