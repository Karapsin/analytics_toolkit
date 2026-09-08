from __future__ import annotations

from typing import Any

from analytics_toolkit.sql.execution.labels import apply_query_label
from analytics_toolkit.sql.execution.query_timing import run_timed_query

from .ch.creation_policy import _physical_as_sql


def execute_source_setup(adapter: Any, connection: Any, statements: list[str]) -> None:
    """Keep Greenplum setup in its transaction until schema inspection or CTAS."""
    sql = ";\n".join(statements)
    if adapter.backend == "gp":
        cursor = connection.cursor()
        try:
            run_timed_query(adapter.backend, lambda: cursor.execute(sql), phase="setup")
        finally:
            cursor.close()
        return
    adapter.execute_sql(
        connection,
        sql,
        print_queries=False,
        gp_break_query=False,
        gp_commit_each_statement=False,
        progress=False,
    )


def commit_source_setup(adapter: Any, connection: Any) -> None:
    if adapter.supports_transactions:
        connection.commit()


def source_materialization_sqls(
    adapter: Any,
    stage: str,
    source_sql: str,
    *,
    policy: Any,
    query_label: str | None,
) -> tuple[str, str | None]:
    if policy is None:
        return adapter.build_materialize_transfer_source_sql(
            stage, source_sql, query_label=query_label
        ), None
    # Cluster-routed ClickHouse staging must populate once after every shard is ready.
    create_sql = _physical_as_sql(
        "CREATE TABLE",
        stage,
        source_sql,
        None,
        None,
        policy.shard_engine,
        policy.shard_on_cluster,
        empty="EMPTY ",
    )
    insert_sql = f"INSERT INTO {stage} {source_sql}"
    return apply_query_label(create_sql, query_label), apply_query_label(insert_sql, query_label)
