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
table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.partitions")


def test_drop_paritions_is_public_and_timed() -> None:
    assert sql_module.drop_paritions is table_ops_module.drop_paritions
    assert dml_table_module.drop_paritions is table_ops_module.drop_paritions
    assert "drop_paritions" in sql_module.__all__
    assert "drop_paritions" in dml_table_module.__all__
    assert "build_drop_many_partitions_sqls" in dml_table_module.__all__
    assert "drop_paritions" in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES
    assert getattr(sql_module.drop_paritions, "__sql_public_timing__", False)


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


def test_drop_paritions_executes_greenplum_in_order_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    result = table_ops_module.drop_paritions(
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


def test_drop_paritions_executes_trino_delete_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeDbapiConnection()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    table_ops_module.drop_paritions(
        "trino",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
        trino_partition_column="dt",
    )

    assert connection.executed == [
        "DELETE FROM sandbox.events\n"
        "WHERE dt IN (DATE '2025-05-01', DATE '2025-05-02')"
    ]
    assert connection.commit_calls == 0
    assert connection.close_calls == 1


def test_drop_paritions_executes_clickhouse_shard_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeClickHouseClient()
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: connection,
    )

    table_ops_module.drop_paritions(
        "ch",
        "sandbox.events",
        ["2025-05-01", "2025-05-02"],
    )

    assert connection.commands == [
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' "
        "DROP PARTITION '2025-05-01'",
        "ALTER TABLE sandbox.events_shard ON CLUSTER '{cluster}' "
        "DROP PARTITION '2025-05-02'",
    ]
    assert connection.close_calls == 1


def test_drop_paritions_dry_run_returns_plan_without_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        table_ops_module,
        "get_sql_connection",
        lambda key: pytest.fail("connection should not be opened"),
    )

    plan = table_ops_module.drop_paritions(
        "gp",
        "sandbox.events",
        ["2025-05-01"],
        dry_run=True,
    )

    assert plan.operation == "drop_paritions"
    assert plan.metadata.statement_count == 1
    assert plan.sqls == ["ALTER TABLE sandbox.events DROP PARTITION FOR ('2025-05-01')"]


def test_drop_paritions_validates_required_inputs() -> None:
    with pytest.raises(InvalidSqlInputError):
        table_ops_module.drop_paritions(
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
        table_ops_module.drop_paritions(
            "gp",
            "sandbox.events",
            ["2025-05-01"],
            trino_partition_column="dt",
        )
    with pytest.raises(UnsupportedConnectionTypeError):
        table_ops_module.drop_paritions(
            "unknown",
            "sandbox.events",
            ["2025-05-01"],
        )
