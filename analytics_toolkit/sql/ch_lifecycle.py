from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .backend_adapters import ch_cluster_clause, get_backend_adapter
from .ddl.create_sql_table import (
    build_ch_distributed_create_table_sqls,
    build_ch_shard_table_name,
    _normalize_non_empty_string,
    _resolve_ch_cluster_name_for_wait,
    _sql_string_literal,
    _wait_for_ch_distributed_table_pair,
    _wait_for_ch_distributed_table_pair_absence,
)
from .labels import apply_query_label


@dataclass(frozen=True)
class ChDistributedTablePair:
    distributed_table: str
    shard_table: str


def ch_distributed_table_pair(
    table_name: str,
    shard_table: str | None = None,
) -> ChDistributedTablePair:
    return ChDistributedTablePair(
        distributed_table=table_name,
        shard_table=shard_table or build_ch_shard_table_name(table_name),
    )


def build_drop_ch_distributed_table_pair_sqls(
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
) -> list[str]:
    pair = ch_distributed_table_pair(table_name, shard_table)
    sqls = [
        _build_drop_ch_table_sql(pair.distributed_table, query_label=query_label),
        _build_drop_ch_table_sql(pair.shard_table, query_label=query_label),
    ]
    if ch_cluster is not None:
        sqls.extend(
            [
                _build_drop_ch_table_sql(
                    pair.distributed_table,
                    ch_cluster=ch_cluster,
                    query_label=query_label,
                ),
                _build_drop_ch_table_sql(
                    pair.shard_table,
                    ch_cluster=ch_cluster,
                    query_label=query_label,
                ),
            ]
        )
    return sqls


def drop_ch_distributed_table_pair(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
    wait_for_absence: bool = False,
    wait_timeout_seconds: int = 300,
    wait_poll_interval_seconds: float = 1,
    ch_retry_per_host_drops: bool = False,
    per_host_connection_factory: Callable[[str], Any] | None = None,
) -> None:
    pair = ch_distributed_table_pair(table_name, shard_table)
    _execute_ch_sqls(
        connection,
        build_drop_ch_distributed_table_pair_sqls(
            pair.distributed_table,
            ch_cluster=ch_cluster,
            shard_table=pair.shard_table,
            query_label=query_label,
        ),
    )
    if wait_for_absence or ch_retry_per_host_drops:
        try:
            _wait_for_ch_distributed_table_pair_absence(
                connection,
                pair.distributed_table,
                ch_cluster=ch_cluster,
                timeout_seconds=wait_timeout_seconds,
                poll_interval_seconds=wait_poll_interval_seconds,
            )
            return
        except TimeoutError as exc:
            if not ch_retry_per_host_drops:
                raise
            if ch_cluster is None:
                raise TimeoutError(
                    f"{exc} ch_retry_per_host_drops=True requires a non-null "
                    "ch_cluster."
                ) from exc
            if per_host_connection_factory is None:
                raise TimeoutError(
                    f"{exc} ch_retry_per_host_drops=True requires a per-host "
                    "ClickHouse connection factory."
                ) from exc

        _drop_ch_distributed_table_pair_on_cluster_hosts(
            connection,
            pair,
            ch_cluster=ch_cluster,
            query_label=query_label,
            per_host_connection_factory=per_host_connection_factory,
        )
        _wait_for_ch_distributed_table_pair_absence(
            connection,
            pair.distributed_table,
            ch_cluster=ch_cluster,
            timeout_seconds=wait_timeout_seconds,
            poll_interval_seconds=wait_poll_interval_seconds,
        )


def build_truncate_ch_distributed_table_pair_sqls(
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
) -> list[str]:
    pair = ch_distributed_table_pair(table_name, shard_table)
    return [
        _build_truncate_ch_table_sql(
            pair.shard_table,
            ch_cluster=ch_cluster,
            query_label=query_label,
        ),
        _build_truncate_ch_table_sql(
            pair.distributed_table,
            query_label=query_label,
        ),
    ]


def truncate_ch_distributed_table_pair(
    connection: Any,
    table_name: str,
    ch_cluster: str | None = "{cluster}",
    *,
    shard_table: str | None = None,
    query_label: str | None = None,
) -> None:
    _execute_ch_sqls(
        connection,
        build_truncate_ch_distributed_table_pair_sqls(
            table_name,
            ch_cluster=ch_cluster,
            shard_table=shard_table,
            query_label=query_label,
        ),
    )


def build_create_ch_distributed_table_pair_sqls(
    *,
    table_name: str,
    joined_columns: str,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = False,
    query_label: str | None = None,
) -> list[str]:
    return [
        apply_query_label(sql, query_label)
        for sql in build_ch_distributed_create_table_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            ch_partition_by=ch_partition_by,
            ch_order_by=ch_order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_replace_table=ch_replace_table,
        )
    ]


def create_ch_distributed_table_pair(
    connection: Any,
    *,
    table_name: str,
    joined_columns: str,
    ch_partition_by: Sequence[str] | str | None = None,
    ch_order_by: Sequence[str] | str | None = None,
    ch_engine: str = "ReplicatedMergeTree",
    ch_cluster: str = "{cluster}",
    ch_sharding_key: str = "rand()",
    ch_replace_table: bool = False,
    query_label: str | None = None,
    wait_for_table: bool = False,
) -> None:
    _execute_ch_sqls(
        connection,
        build_create_ch_distributed_table_pair_sqls(
            table_name=table_name,
            joined_columns=joined_columns,
            ch_partition_by=ch_partition_by,
            ch_order_by=ch_order_by,
            ch_engine=ch_engine,
            ch_cluster=ch_cluster,
            ch_sharding_key=ch_sharding_key,
            ch_replace_table=ch_replace_table,
            query_label=query_label,
        ),
    )
    if wait_for_table:
        _wait_for_ch_distributed_table_pair(
            connection,
            table_name,
            ch_cluster=ch_cluster,
        )


def _build_drop_ch_table_sql(
    table_name: str,
    *,
    ch_cluster: str | None = None,
    query_label: str | None = None,
) -> str:
    return apply_query_label(
        f"DROP TABLE IF EXISTS {table_name}{ch_cluster_clause(ch_cluster)}",
        query_label,
    )


def _build_truncate_ch_table_sql(
    table_name: str,
    *,
    ch_cluster: str | None = None,
    query_label: str | None = None,
) -> str:
    return apply_query_label(
        f"TRUNCATE TABLE IF EXISTS {table_name}{ch_cluster_clause(ch_cluster)}",
        query_label,
    )


def _execute_ch_sqls(connection: Any, sqls: list[str]) -> None:
    adapter = get_backend_adapter("ch")
    for sql in sqls:
        adapter.execute_command(connection, sql)


def _drop_ch_distributed_table_pair_on_cluster_hosts(
    connection: Any,
    pair: ChDistributedTablePair,
    *,
    ch_cluster: str,
    query_label: str | None,
    per_host_connection_factory: Callable[[str], Any],
) -> None:
    hosts = _query_ch_configured_cluster_hosts(connection, ch_cluster)
    if not hosts:
        raise TimeoutError(
            "ch_retry_per_host_drops=True could not find any configured "
            f"ClickHouse hosts for cluster {ch_cluster!r}."
        )

    errors: list[str] = []
    for host in hosts:
        host_connection = None
        try:
            host_connection = per_host_connection_factory(host)
            _execute_ch_sqls(
                host_connection,
                [
                    _build_drop_ch_table_sql(
                        pair.distributed_table,
                        query_label=query_label,
                    ),
                    _build_drop_ch_table_sql(
                        pair.shard_table,
                        query_label=query_label,
                    ),
                ],
            )
        except Exception as exc:
            errors.append(f"{host}: {exc!r}")
        finally:
            if host_connection is not None:
                close = getattr(host_connection, "close", None)
                if callable(close):
                    close()

    if errors:
        raise TimeoutError(
            "ch_retry_per_host_drops=True failed to locally drop ClickHouse "
            f"table pair {pair.distributed_table} / {pair.shard_table} on "
            "some host(s): " + "; ".join(errors)
        )


def _query_ch_configured_cluster_hosts(
    connection: Any,
    ch_cluster: str,
) -> list[str]:
    cluster_name = _normalize_non_empty_string(ch_cluster, "ch_cluster")
    cluster_name = _resolve_ch_cluster_name_for_wait(connection, cluster_name)
    sql = (
        "SELECT DISTINCT host_name\n"
        "FROM system.clusters\n"
        f"WHERE cluster = {_sql_string_literal(cluster_name)}\n"
        "ORDER BY host_name"
    )
    result = connection.query(sql)
    rows = getattr(result, "result_rows", None) or []
    hosts: list[str] = []
    for row in rows:
        if not row:
            continue
        host = str(row[0]).strip()
        if host:
            hosts.append(host)
    return hosts
