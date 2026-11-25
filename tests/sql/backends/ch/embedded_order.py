"""Public creation renders one effective MergeTree sorting definition."""

from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit import sql
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from sqlglot import exp, parse

from tests.sql._support.ch_lifecycle import install_lifecycle


@pytest.mark.parametrize("source", ["schema", "dataframe", "query"])
def test_public_sql_creation_preserves_the_engine_embedded_order_without_duplication(
    monkeypatch, write_sql_connections, source
):
    options = (
        {"df": pd.DataFrame({"id": pd.Series([1], dtype="uint64")})}
        if source == "dataframe"
        else {"table_schema": {"id": "UInt64"}}
    )
    state = None
    if source == "query":
        state = install_lifecycle(monkeypatch, write_sql_connections, driver="http")
        options = {
            "sql": "SELECT CAST(1 AS UInt64) AS id",
            "insert_data": False,
            "ch_shard_on_cluster": "coordinator_only",
        }
    result = sql.create_table(
        "ch",
        "analytics.ordered_events",
        ch_distributed_table=False,
        ch_engine="MergeTree ORDER BY id",
        only_generate_sql=source != "query",
        retry_cnt=1,
        timeout_increment=0,
        **options,
    )

    generated = ";\n".join(state.commands) if state is not None else result
    assert isinstance(generated, str)
    creates = [
        tree for tree in parse(generated, read=ClickHouseDialect) if isinstance(tree, exp.Create)
    ]
    assert creates
    for create in creates:
        orders = list(create.find_all(exp.Order))
        assert len(orders) == 1
        assert len(orders[0].expressions) == 1
        column = orders[0].expressions[0].this
        assert isinstance(column, exp.Column)
        assert column.name == "id"


def test_public_sql_creation_retains_the_default_empty_sorting_tuple():
    generated = sql.create_table(
        "ch",
        "analytics.unordered_events",
        table_schema={"id": "UInt64"},
        ch_distributed_table=False,
        ch_engine="MergeTree",
        only_generate_sql=True,
    )

    assert isinstance(generated, str)
    creates = [
        tree for tree in parse(generated, read=ClickHouseDialect) if isinstance(tree, exp.Create)
    ]
    assert creates
    for create in creates:
        orders = list(create.find_all(exp.Order))
        assert len(orders) == 1
        assert "tuple()" in orders[0].sql(dialect=ClickHouseDialect)
