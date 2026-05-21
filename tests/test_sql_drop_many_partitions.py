from __future__ import annotations

import importlib

import pytest

from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    UnsupportedConnectionTypeError,
)
from tests.sql_fakes import FakeClickHouseClient, FakeDbapiConnection


sql_module = importlib.import_module("analytics_toolkit.sql")
dml_table_module = importlib.import_module("analytics_toolkit.sql.dml.table")
table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.table_ops")


def test_drop_many_partitions_is_public_and_timed() -> None:
    assert sql_module.drop_many_partitions is table_ops_module.drop_many_partitions
    assert dml_table_module.drop_many_partitions is table_ops_module.drop_many_partitions
    assert "drop_many_partitions" in sql_module.__all__
    assert "drop_many_partitions" in dml_table_module.__all__
    assert "build_drop_many_partitions_sqls" in dml_table_module.__all__
    assert "drop_many_partitions" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.drop_many_partitions, "__sql_public_timing__", False)


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
        partition_column="dt",
    ) == [
        "DELETE FROM sandbox.events\n"
        "WHERE dt IN (DATE '2025-05-01', DATE '2025-05-02')"
    ]
    assert table_ops_module.build_drop_many_partitions_sqls(
        "ch",
        "sandbox.events",
        partition_keys,
    ) == [
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' "
        "DROP PARTITION '2025-05-01'",
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' "
        "DROP PARTITION '2025-05-02'",
    ]


def test_drop_many_partitions_executes_greenplum_in_order_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = table_ops_module.drop_many_partitions(
        "gp",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
        retry_cnt=1,
        timeout_increment=0,
        return_metadata=True,
    )

    assert connection.executed == [
        "ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-01')",
        "ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-02')",
    ]
    assert connection.commit_calls == 1
    assert connection.close_calls == 1
    assert result.rows is None
    assert result.metadata.statement_count == 2
    assert result.metadata.retry_attempts == 1
    assert result.metadata.operation_status == "success"
    assert result.plan.operation == "drop_many_partitions"


def test_drop_many_partitions_executes_trino_delete_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    table_ops_module.drop_many_partitions(
        "trino",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
        partition_column="dt",
        retry_cnt=1,
        timeout_increment=0,
    )

    assert connection.executed == [
        "DELETE FROM sandbox.events\n"
        "WHERE dt IN (DATE '2025-05-01', DATE '2025-05-02')"
    ]
    assert connection.commit_calls == 0
    assert connection.close_calls == 1


def test_drop_many_partitions_executes_clickhouse_shard_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClickHouseClient()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: client,
    )

    table_ops_module.drop_many_partitions(
        "ch",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
        retry_cnt=1,
        timeout_increment=0,
    )

    assert client.commands == [
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' "
        "DROP PARTITION '2025-05-01'",
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' "
        "DROP PARTITION '2025-05-02'",
    ]
    assert client.close_calls == 1


def test_drop_many_partitions_dry_run_returns_plan_without_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = table_ops_module.drop_many_partitions(
        "ch",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
        dry_run=True,
        query_label="drop partitions",
    )

    assert plan.operation == "drop_many_partitions"
    assert plan.target_alias == "ch"
    assert plan.target_backend == "ch"
    assert plan.target_table == "sandbox.events"
    assert plan.metadata.statement_count == 2
    assert plan.metadata.query_label == "drop partitions"
    assert [statement.phase for statement in plan.statements] == [
        "drop_partitions",
        "drop_partitions",
    ]
    assert plan.sqls[0].startswith(
        "/* analytics_toolkit query_label=drop partitions */"
    )


def test_drop_many_partitions_validates_required_inputs() -> None:
    with pytest.raises(InvalidSqlInputError, match="partition_column"):
        table_ops_module.drop_many_partitions(
            "trino",
            "sandbox.events",
            ["2025-05-01"],
            dry_run=True,
        )

    with pytest.raises(InvalidSqlInputError, match="partition_keys_list"):
        table_ops_module.drop_many_partitions(
            "gp",
            "sandbox.events",
            [],
            dry_run=True,
        )

    with pytest.raises(InvalidSqlInputError, match="Partition values"):
        table_ops_module.drop_many_partitions(
            "gp",
            "sandbox.events",
            ["2025-05-01", " "],
            dry_run=True,
        )

    with pytest.raises(InvalidSqlInputError, match="Table name"):
        table_ops_module.drop_many_partitions(
            "gp",
            " ",
            ["2025-05-01"],
            dry_run=True,
        )

    with pytest.raises(UnsupportedConnectionTypeError, match="gp_truncate"):
        table_ops_module.drop_many_partitions(
            "ch",
            "sandbox.events",
            ["2025-05-01"],
            gp_truncate=True,
            dry_run=True,
        )
