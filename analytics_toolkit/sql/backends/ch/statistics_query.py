# ruff: noqa: S608 - all dynamic identities are rendered as escaped ClickHouse literals.
"""Native metadata aggregates with explicit logical and physical coverage."""

from __future__ import annotations

from typing import Any

from .dialect import sql_string_literal


def build_shard_statistics_query(cluster: str, shard_refs: set[Any]) -> str:
    table_filters = ", ".join(
        f"({sql_string_literal(ref.database)}, {sql_string_literal(ref.table)})"
        for ref in sorted(shard_refs, key=lambda value: (value.database, value.table))
    )
    cluster_sql = sql_string_literal(cluster)
    return f"""
WITH host_scope AS (
    SELECT row_number() OVER (ORDER BY shard_num, replica_num) AS position, shard_num
    FROM system.clusters
    WHERE cluster = {cluster_sql}
), eligible_statistics AS (
    SELECT source.database AS database, source.name AS name,
        host_scope.shard_num AS shard_num, source._shard_num AS position,
        source.engine AS engine,
        startsWith(source.engine, 'Replicated')
            OR startsWith(source.engine, 'Shared') AS replicated,
        source.total_rows AS total_rows,
        source.total_bytes AS total_bytes
    FROM clusterAllReplicas({cluster_sql}, system, tables) AS source
    INNER JOIN host_scope ON source._shard_num = host_scope.position
    LEFT JOIN (
        SELECT _shard_num AS replica_position, database AS replica_database,
            table AS replica_table, is_session_expired
        FROM clusterAllReplicas({cluster_sql}, system, replicas)
    ) AS replicas ON source._shard_num = replicas.replica_position
        AND source.database = replicas.replica_database
        AND source.name = replicas.replica_table
    WHERE (source.database, source.name) IN ({table_filters})
        AND (NOT startsWith(source.engine, 'Replicated')
             OR (replicas.replica_position > 0 AND replicas.is_session_expired = 0))
), table_shards AS (
    SELECT database, name, shard_num, engine,
        replicated,
        if(replicated,
           tupleElement(argMin(tuple(total_rows, total_bytes), position), 1),
           if(count(total_rows) = count(), sum(total_rows), NULL)) AS row_count,
        if(replicated,
           tupleElement(argMin(tuple(total_rows, total_bytes), position), 2),
           if(count(total_bytes) = count(), sum(total_bytes), NULL)) AS table_size_bytes,
        count() AS physical_hosts
    FROM eligible_statistics
    GROUP BY database, name, shard_num, engine, replicated
)
SELECT database AS shard_database, name AS shard_table,
    if(count(shards.row_count) = count(), sum(shards.row_count), NULL) AS row_count,
    if(count(shards.table_size_bytes) = count(), sum(shards.table_size_bytes), NULL)
        AS table_size_bytes,
    count(DISTINCT engine) = 1 AND
        if(min(replicated),
           count(DISTINCT shard_num) = (SELECT count(DISTINCT shard_num) FROM host_scope),
           sum(physical_hosts) = (SELECT count() FROM host_scope)) AS statistics_complete
FROM table_shards AS shards
GROUP BY database, name
ORDER BY database, name
""".strip()
