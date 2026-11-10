from __future__ import annotations

import importlib
from dataclasses import replace

import pytest
from analytics_toolkit.sql.dml.table.models import ChCreateTableAsOptions
from sqlglot import exp, parse_one

legacy = importlib.import_module("analytics_toolkit.sql.backends.ch.create_table_as")
ddl = importlib.import_module("analytics_toolkit.sql.backends.ch.reconfigure_ddl")


@pytest.mark.parametrize("only_shard", [False, True])
def test_exported_legacy_sql_renderers_keep_local_vs_pair_shapes(only_shard):
    options = ChCreateTableAsOptions(
        "ch",
        "ch",
        "db.events",
        "SELECT 1 AS id",
        ch_only_shard=only_shard,
        order_by="id",
        ch_engine="MergeTree",
        ch_cluster="core",
    )
    drops = legacy._build_ch_create_table_as_dry_run_drop_sqls(options)
    assert len(drops) == (1 if only_shard else 4)
    assert all(sql.startswith("DROP TABLE") for sql in drops)
    unknown = legacy._build_ch_create_table_as_dry_run_create_sqls(options)
    assert len(unknown) == (1 if only_shard else 4)
    assert all("<query schema>" in sql for sql in unknown)
    known = legacy._build_ch_create_table_as_dry_run_create_sqls(
        replace(options, table_schema={"id": "Int64"})
    )
    assert len(known) == (1 if only_shard else 4)
    for sql in known:
        column = parse_one(sql, read="clickhouse").find(exp.ColumnDef)
        assert column.name == "id"
        assert column.args["kind"].sql(dialect="clickhouse") == "Int64"
    if only_shard:
        assert all("events_shard" not in sql for sql in known)


def test_exported_insert_renderer_and_rand_restoration_preserve_expected_expressions():
    assert (
        legacy._build_insert_select_sql("db.events", "SELECT 1")
        == "INSERT INTO db.events\nSELECT 1"
    )
    expression = exp.select(exp.Rand(), exp.Literal.string("randCanonical()"))
    ddl._restore_clickhouse_rand(expression)
    assert expression.expressions[0].sql(dialect="clickhouse") == "rand()"
    assert expression.expressions[1].this == "randCanonical()"
    ddl._restore_clickhouse_rand(exp.select("id"))
