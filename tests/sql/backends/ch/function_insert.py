"""Native INSERT FUNCTION column lists share routing and outage grammar."""

from __future__ import annotations

import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect, sql_string_literal
from analytics_toolkit.sql.backends.ch.routing import ChClusterRouting, route_sql
from sqlglot import exp, parse, parse_one
from sqlglot.errors import ParseError


def _target(statement):
    assert isinstance(statement, exp.Insert)
    assert statement.args["is_function"] is True
    target = statement.this
    columns = target.expressions if isinstance(target, exp.Schema) else []
    function = target.this if isinstance(target, exp.Schema) else target
    assert isinstance(function, exp.Anonymous)
    return function, columns


@pytest.mark.parametrize("function", ["cluster", "clusterAllReplicas"])
@pytest.mark.parametrize("key", ["rand", "randCanonical"])
@pytest.mark.parametrize("columns", [(), ("id",), ("id", "payload"), ("select", "payload.name")])
def test_function_target_columns_roundtrip_without_consuming_source(function, key, columns):
    extra = "path\\file'quoted,(SELECT)"
    column_sql = "(" + ", ".join(f"`{name}`" for name in columns) + ")" if columns else ""
    query = (
        "/* insert label */ INSERT /* into note */ INTO FUNCTION "
        f"{function}('core', 'db.with.dot', 'table.with.dot', "
        f"{key}(), {sql_string_literal(extra)}) "
        f"{column_sql} "
        "WITH src AS (SELECT 1 AS id, 'literal SELECT (,)' AS payload) "
        "SELECT /* source note */ src.id, src.payload FROM src "
        "SETTINGS max_threads = 1"
    )
    statement = parse_one(query, read=ClickHouseDialect)
    target, parsed_columns = _target(statement)
    assert target.name == function
    assert [item.this for item in target.expressions[:3]] == [
        "core",
        "db.with.dot",
        "table.with.dot",
    ]
    assert target.expressions[3].sql(dialect=ClickHouseDialect).casefold() == f"{key.casefold()}()"
    assert target.expressions[4].this == extra
    assert [column.name for column in parsed_columns] == list(columns)
    assert all(isinstance(column, exp.Identifier) for column in parsed_columns)
    assert all(column.args["quoted"] for column in parsed_columns)
    assert isinstance(statement.expression, exp.Select)
    assert statement.expression.args["with_"] is not None
    assert [column.table for column in statement.expression.expressions] == ["src", "src"]

    rendered = statement.sql(dialect=ClickHouseDialect)
    assert "source note" in rendered
    assert "literal SELECT (,)" in rendered
    assert "max_threads" in rendered
    reparsed = parse_one(rendered, read=ClickHouseDialect)
    replay_target, replay_columns = _target(reparsed)
    assert [column.name for column in replay_columns] == list(columns)
    assert replay_target.expressions[4].this == extra
    assert (
        replay_target.expressions[3].sql(dialect=ClickHouseDialect).casefold()
        == f"{key.casefold()}()"
    )


def test_function_target_grammar_does_not_rewrite_ordinary_insert_or_nested_functions():
    statements = parse(
        "INSERT INTO FUNCTION cluster('core', 'db', 'target', "
        "cityHash64(tuple(rand(), randCanonical()))) "
        "(`id`) SELECT rand(); "
        "INSERT INTO db.target (`id`) SELECT randCanonical(); "
        "INSERT INTO FUNCTION cluster('core', 'db', 'target', rand()) VALUES (9)",
        read=ClickHouseDialect,
    )
    target, columns = _target(statements[0])
    assert [column.name for column in columns] == ["id"]
    key_sql = target.expressions[3].sql(dialect=ClickHouseDialect)
    assert "rand()" in key_sql
    assert "randCanonical()" in key_sql
    ordinary = statements[1]
    assert isinstance(ordinary, exp.Insert)
    assert ordinary.args.get("is_function") is not True
    assert isinstance(ordinary.this, exp.Schema)
    assert isinstance(ordinary.this.this, exp.Table)
    assert ordinary.this.this.db == "db"
    assert ordinary.this.this.name == "target"
    assert [column.name for column in ordinary.this.expressions] == ["id"]
    assert ordinary.expression.expressions[0].sql(dialect=ClickHouseDialect) == "randCanonical()"
    target_without_columns, empty = _target(statements[2])
    assert target_without_columns.name == "cluster"
    assert empty == []
    assert isinstance(statements[2].expression, exp.Values)


def test_function_target_context_is_restored_after_parse_error_and_parser_reuse():
    dialect = ClickHouseDialect()
    parser = dialect.parser()
    invalid = "INSERT INTO FUNCTION cluster('core', 'db', 'target', rand()) (`id` SELECT 9"
    with pytest.raises(ParseError):
        parser.parse(dialect.tokenize(invalid), invalid)
    assert getattr(parser, "_atk_insert_target", None) is False

    ordinary = "INSERT INTO db.target (`id`) SELECT rand()"
    statement = parser.parse(dialect.tokenize(ordinary), ordinary)[0]
    assert isinstance(statement.this, exp.Schema)
    assert isinstance(statement.this.this, exp.Table)
    assert statement.this.this.name == "target"
    assert statement.expression.expressions[0].sql(dialect=ClickHouseDialect) == "rand()"
    assert getattr(parser, "_atk_insert_target", None) is False


def test_routing_keeps_function_target_columns_and_source_bindings():
    query = (
        "INSERT INTO FUNCTION cluster('core', 'db.with.dot', 'target.with.dot', "
        "rand(), 'extra,(arg)') "
        "(`select`, `payload.name`) SELECT /* bound source */ s.id, s.payload FROM db.source AS s"
    )
    routed = route_sql(query, routing=ChClusterRouting("core"), database="db")
    statement = parse_one(routed, read=ClickHouseDialect)
    target, columns = _target(statement)
    assert [column.name for column in columns] == ["select", "payload.name"]
    assert [item.this for item in target.expressions[:3]] == [
        "core",
        "db.with.dot",
        "target.with.dot",
    ]
    assert target.expressions[4].this == "extra,(arg)"
    source = statement.expression.args["from_"].this
    assert source.alias == "s"
    assert isinstance(source.this, exp.Anonymous)
    assert [item.this for item in source.this.expressions[:3]] == ["core", "db", "source"]
    assert [column.table for column in statement.expression.expressions] == ["s", "s"]
    assert "bound source" in routed


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("policy", ["stop", "healthy_shards"])
def test_public_outage_function_insert_with_columns_reaches_real_command(outage, routed, policy):
    cluster, configure = outage
    configure(cluster_routing={"cluster": "core"} if routed else None)
    query = "INSERT INTO FUNCTION cluster('core', 'db', 'events_shard', rand()) (`id`) VALUES (9)"

    assert sql.execute("ch", query, ch_unavailable_shards=policy, retry_cnt=1) is None

    rows = [row for host in cluster.hosts for row in cluster.tables[(host, "db.events_shard")].rows]
    assert sorted(rows) == [(1,), (2,), (9,)]
    assert cluster.commands
    submitted = [
        parse_one(statement, read=ClickHouseDialect)
        for _host, statement, _settings in cluster.commands
    ]
    assert all(isinstance(statement, exp.Insert) for statement in submitted)
    assert all(
        [column.name for column in statement.this.expressions] == ["id"] for statement in submitted
    )
