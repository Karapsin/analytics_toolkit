from __future__ import annotations

import importlib

import pytest
from analytics_toolkit.sql.backends.ch.dialect import ClickHouseDialect
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    UnsupportedConnectionTypeError,
)
from sqlglot import exp, parse_one

from tests.sql._support.assertions import strip_query_label
from tests.sql._support.ch_partition_scope import install_partition_scope
from tests.sql._support.fakes import FakeDbapiConnection

sql_module = importlib.import_module("analytics_toolkit.sql")
dml_table_module = importlib.import_module("analytics_toolkit.sql.dml.table")
table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.partitions")


def test_drop_partitions_is_public_and_timed() -> None:
    assert sql_module.drop_partitions is table_ops_module.drop_partitions
    assert dml_table_module.drop_partitions is table_ops_module.drop_partitions
    assert "drop_partitions" in sql_module.__all__
    assert "drop_partitions" in dml_table_module.__all__
    assert "build_drop_many_partitions_sqls" in dml_table_module.__all__
    assert "drop_partitions" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.drop_partitions, "__sql_public_timing__", False)
    assert "drop_paritions" not in sql_module.__all__
    assert not hasattr(sql_module, "drop_paritions")
    assert "drop_paritions" not in dml_table_module.__all__
    assert not hasattr(dml_table_module, "drop_paritions")


def test_build_drop_many_partitions_sqls_renders_backend_sql() -> None:
    partition_keys = ["2025-05-01", "2025-05-02"]

    assert table_ops_module.build_drop_many_partitions_sqls(
        "gp",
        "sandbox.events",
        partition_keys,
    ) == [
        "ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-01')",
        "ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-02')",
    ]
    assert table_ops_module.build_drop_many_partitions_sqls(
        "gp",
        "sandbox.events",
        partition_keys,
        gp_truncate=True,
    ) == [
        "ALTER TABLE sandbox.events TRUNCATE PARTITION FOR ('2025-05-01')",
        "ALTER TABLE sandbox.events TRUNCATE PARTITION FOR ('2025-05-02')",
    ]
    assert table_ops_module.build_drop_many_partitions_sqls(
        "trino",
        "sandbox.events",
        partition_keys,
        trino_partition_column="dt",
    ) == ["DELETE FROM sandbox.events\nWHERE dt IN (DATE '2025-05-01', DATE '2025-05-02')"]
    assert table_ops_module.build_drop_many_partitions_sqls(
        "ch",
        "sandbox.events",
        partition_keys,
    ) == [
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' DROP PARTITION '2025-05-01'",
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' DROP PARTITION '2025-05-02'",
    ]


def test_drop_partitions_executes_greenplum_in_order_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = table_ops_module.drop_partitions(
        "gp",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
    )

    assert result is None
    assert connection.executed == [
        "ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-01')",
        "ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-02')",
    ]
    assert connection.commit_calls == 1
    assert connection.rollback_calls == 0
    assert connection.close_calls == 1


def test_drop_partitions_executes_trino_delete_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    table_ops_module.drop_partitions(
        "trino",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
        trino_partition_column="dt",
    )

    assert connection.executed == [
        "DELETE FROM sandbox.events\nWHERE dt IN (DATE '2025-05-01', DATE '2025-05-02')"
    ]
    assert connection.commit_calls == 0
    assert connection.close_calls == 1


def test_drop_partitions_executes_clickhouse_shard_drops(
    monkeypatch: pytest.MonkeyPatch,
    write_sql_connections,
) -> None:
    state = install_partition_scope(
        monkeypatch, write_sql_connections, driver="http", layout="pair"
    )
    physical = state.host_tables[state.remote]["analytics.events_shard"]
    physical.rows = [("2025-05-01", 1), ("2025-05-02", 2), ("kept", 3)]
    before = state.snapshot()

    table_ops_module.drop_partitions(
        "ch",
        "analytics.events",
        ["2025-05-01", "2025-05-02"],
        retry_cnt=1,
        timeout_increment=0,
    )

    before[state.remote]["analytics.events_shard"]["rows"] = [("kept", 3)]
    assert state.snapshot() == before
    statements = [
        parse_one(strip_query_label(statement), read=ClickHouseDialect)
        for statement in state.commands
    ]
    assert len(statements) == 2
    for statement, value in zip(statements, ("2025-05-01", "2025-05-02")):
        assert isinstance(statement, exp.Alter)
        assert statement.this.db == "analytics"
        assert statement.this.name == "events_shard"
        cluster = statement.find(exp.OnCluster)
        assert cluster is not None
        assert cluster.this.name == "storage"
        partition = statement.find(exp.DropPartition)
        assert partition is not None
        operand = partition.expressions[0].expressions[0]
        assert isinstance(operand, exp.Literal)
        assert operand.is_string
        assert operand.this == value
    assert state.opened
    assert all(connection.close_calls == 1 for connection in state.opened)


def test_drop_partitions_dry_run_returns_plan_without_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = table_ops_module.drop_partitions(
        "gp",
        "sandbox.events",
        ["2025-05-01"],
        dry_run=True,
    )

    assert plan.operation == "drop_partitions"
    assert plan.metadata.statement_count == 1
    assert plan.sqls == ["ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-01')"]


def test_drop_partitions_validates_required_inputs() -> None:
    with pytest.raises(InvalidSqlInputError):
        table_ops_module.drop_partitions(
            "gp",
            "sandbox.events",
            [],
        )
    with pytest.raises(InvalidSqlInputError):
        table_ops_module.build_drop_many_partitions_sqls(
            "gp",
            "sandbox.events",
            [],
        )
    with pytest.raises(InvalidSqlInputError):
        table_ops_module.drop_partitions(
            "gp",
            "sandbox.events",
            ["2025-05-01"],
            trino_partition_column="dt",
        )
    with pytest.raises(UnsupportedConnectionTypeError):
        table_ops_module.drop_partitions(
            "unknown",
            "sandbox.events",
            ["2025-05-01"],
        )
